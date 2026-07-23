"""Backend for the ComfyUI Scripted Node.

Schema analysis is deliberately separate from execution.  ``parse_script_schema``
only parses Python's AST and applies ``ast.literal_eval`` to the two declaration
assignments; user code is only compiled/executed from ``ComfyScriptedNode.execute``
when ComfyUI runs the queued node.
"""

from __future__ import annotations

import ast
import functools
import hashlib
import json
import keyword
import sys
from collections.abc import Mapping
from typing import Any


MAX_OUTPUTS = 32
COMPILE_CACHE_SIZE = 64

DEFAULT_CODE = '''INPUTS = {
    "value": ("FLOAT", {"default": 1.0}),
}

OUTPUTS = {
    "result": "FLOAT",
}

def run(value):
    return {"result": value}
'''


class ScriptSchemaError(ValueError):
    """Raised when INPUTS or OUTPUTS is not a valid literal declaration."""


class ScriptExecutionError(RuntimeError):
    """Raised when a queued script cannot be executed or normalized."""


class ScriptSchema(dict):
    """JSON-serializable schema mapping with convenient attribute helpers."""

    def __init__(self, inputs: list[dict[str, Any]], outputs: list[dict[str, str]]):
        super().__init__(inputs=inputs, outputs=outputs)

    @property
    def inputs(self) -> list[dict[str, Any]]:
        return self["inputs"]

    @property
    def outputs(self) -> list[dict[str, str]]:
        return self["outputs"]

    def to_dict(self) -> dict[str, Any]:
        return {
            "inputs": [dict(item) for item in self.inputs],
            "outputs": [dict(item) for item in self.outputs],
        }

    def as_dict(self) -> dict[str, Any]:
        return self.to_dict()


class AnyType(str):
    """Legacy-compatible ComfyUI wildcard socket type."""

    def __ne__(self, other: object) -> bool:
        return False


ANY_TYPE = AnyType("*")


class FlexibleOptionalInputs(dict):
    """Tell legacy ComfyUI that every frontend-created input name is accepted."""

    def __init__(
        self,
        socket_type: str = ANY_TYPE,
        explicit: Mapping[str, Any] | None = None,
    ):
        super().__init__(explicit or {})
        self.socket_type = socket_type

    def __contains__(self, key: object) -> bool:
        return True

    def __getitem__(self, key: str) -> Any:
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        return (self.socket_type,)

    def get(self, key: str, default: Any = None) -> Any:
        if dict.__contains__(self, key):
            return dict.__getitem__(self, key)
        return (self.socket_type,)


def _schema_error(message: str) -> ScriptSchemaError:
    return ScriptSchemaError(f"Script schema error: {message}")


def _find_literal_assignment(
    tree: ast.Module, declaration: str
) -> tuple[ast.AST, int]:
    assignments: list[tuple[ast.AST, int]] = []

    for statement in tree.body:
        if isinstance(statement, ast.Assign):
            if any(
                isinstance(target, ast.Name) and target.id == declaration
                for target in statement.targets
            ):
                assignments.append((statement.value, statement.lineno))
        elif (
            isinstance(statement, ast.AnnAssign)
            and isinstance(statement.target, ast.Name)
            and statement.target.id == declaration
            and statement.value is not None
        ):
            assignments.append((statement.value, statement.lineno))

    if not assignments:
        raise _schema_error(
            f"missing top-level `{declaration} = {{...}}` declaration"
        )
    if len(assignments) > 1:
        lines = ", ".join(str(line) for _, line in assignments)
        raise _schema_error(
            f"`{declaration}` must be assigned exactly once (found lines {lines})"
        )

    return assignments[0]


def _literal_mapping(
    tree: ast.Module, declaration: str
) -> Mapping[Any, Any]:
    expression, line = _find_literal_assignment(tree, declaration)
    try:
        value = ast.literal_eval(expression)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError) as exc:
        raise _schema_error(
            f"`{declaration}` on line {line} must be a literal mapping; "
            "names, calls, comprehensions, and expressions are not allowed"
        ) from exc

    if not isinstance(value, Mapping):
        raise _schema_error(
            f"`{declaration}` on line {line} must evaluate to a mapping, "
            f"not {type(value).__name__}"
        )
    return value


def _validate_run_definition(tree: ast.Module) -> None:
    definitions = [
        statement
        for statement in tree.body
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
        and statement.name == "run"
    ]
    if not definitions:
        raise _schema_error("missing top-level `def run(...):` function")
    if len(definitions) > 1:
        lines = ", ".join(str(statement.lineno) for statement in definitions)
        raise _schema_error(
            f"`run` must be defined exactly once (found lines {lines})"
        )
    if isinstance(definitions[0], ast.AsyncFunctionDef):
        raise _schema_error("`run` must be synchronous; `async def run` is unsupported")


def _validate_socket_name(name: Any, declaration: str) -> str:
    if not isinstance(name, str) or not name:
        raise _schema_error(f"all `{declaration}` keys must be non-empty strings")
    if not name.isidentifier() or keyword.iskeyword(name):
        raise _schema_error(
            f"`{name}` in `{declaration}` is not a valid Python parameter name"
        )
    if name in {"code", "schema_json"}:
        raise _schema_error(
            f"`{name}` is reserved by the Scripted Node and cannot be a socket name"
        )
    return name


def _validate_socket_type(value: Any, context: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise _schema_error(f"{context} type must be a non-empty string")
    return value.strip()


def _validate_json_value(value: Any, path: str) -> None:
    if value is None or isinstance(value, (str, bool, int)):
        return
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")):
            raise _schema_error(f"{path} contains a non-finite float")
        return
    if isinstance(value, (list, tuple)):
        for index, item in enumerate(value):
            _validate_json_value(item, f"{path}[{index}]")
        return
    if isinstance(value, Mapping):
        for key, item in value.items():
            if not isinstance(key, str):
                raise _schema_error(f"{path} contains a non-string option key")
            _validate_json_value(item, f"{path}.{key}")
        return
    raise _schema_error(
        f"{path} contains {type(value).__name__}, which cannot be stored in a workflow"
    )


def _parse_input_descriptor(name: str, descriptor: Any) -> dict[str, Any]:
    if isinstance(descriptor, str):
        socket_type = _validate_socket_type(descriptor, f"input `{name}`")
        options: Mapping[str, Any] = {}
    elif isinstance(descriptor, (tuple, list)):
        if len(descriptor) not in (1, 2):
            raise _schema_error(
                f"input `{name}` must be `\"TYPE\"` or "
                "`(\"TYPE\", {options})`"
            )
        socket_type = _validate_socket_type(descriptor[0], f"input `{name}`")
        options = descriptor[1] if len(descriptor) == 2 else {}
        if not isinstance(options, Mapping):
            raise _schema_error(f"options for input `{name}` must be a mapping")
    else:
        raise _schema_error(
            f"input `{name}` must be `\"TYPE\"` or `(\"TYPE\", {{...}})`"
        )

    _validate_json_value(options, f"options for input `{name}`")
    # Round-trip through JSON so tuples become lists and endpoint payloads are
    # guaranteed to be JSON-safe without changing declaration order.
    json_options = json.loads(json.dumps(options, ensure_ascii=False))
    return {"name": name, "type": socket_type, "options": json_options}


def _parse_output_descriptor(name: str, descriptor: Any) -> dict[str, str]:
    if isinstance(descriptor, (tuple, list)) and len(descriptor) == 1:
        descriptor = descriptor[0]
    socket_type = _validate_socket_type(descriptor, f"output `{name}`")
    return {"name": name, "type": socket_type}


def parse_script_schema(code: str) -> ScriptSchema:
    """Extract ordered INPUTS/OUTPUTS declarations without executing *code*."""

    if not isinstance(code, str):
        raise _schema_error(f"code must be a string, not {type(code).__name__}")
    try:
        tree = ast.parse(code, filename="<scripted-node>", mode="exec")
    except SyntaxError as exc:
        location = f"line {exc.lineno}"
        if exc.offset is not None:
            location += f", column {exc.offset}"
        raise _schema_error(f"invalid Python syntax at {location}: {exc.msg}") from exc

    raw_inputs = _literal_mapping(tree, "INPUTS")
    raw_outputs = _literal_mapping(tree, "OUTPUTS")
    _validate_run_definition(tree)

    inputs = [
        _parse_input_descriptor(
            _validate_socket_name(name, "INPUTS"), descriptor
        )
        for name, descriptor in raw_inputs.items()
    ]
    outputs = [
        _parse_output_descriptor(
            _validate_socket_name(name, "OUTPUTS"), descriptor
        )
        for name, descriptor in raw_outputs.items()
    ]

    if not outputs:
        raise _schema_error("`OUTPUTS` must declare at least one output")
    if len(outputs) > MAX_OUTPUTS:
        raise _schema_error(
            f"`OUTPUTS` declares {len(outputs)} outputs; the maximum is {MAX_OUTPUTS}"
        )

    return ScriptSchema(inputs=inputs, outputs=outputs)


# A short alias is useful to callers embedding the parser outside ComfyUI.
parse_schema = parse_script_schema


def schema_to_json(schema: Mapping[str, Any]) -> str:
    return json.dumps(
        schema, ensure_ascii=False, separators=(",", ":"), sort_keys=False
    )


@functools.lru_cache(maxsize=COMPILE_CACHE_SIZE)
def _compile_cached(source_hash: str, code: str) -> Any:
    # source_hash intentionally participates in the cache key.  Keeping `code`
    # as well makes hash collisions harmless.
    expected_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
    if source_hash != expected_hash:
        raise ScriptExecutionError("Internal error: script source hash mismatch")
    return compile(code, "<scripted-node>", "exec")


def clear_compile_cache() -> None:
    """Clear compiled scripts, primarily useful during development and tests."""

    _compile_cached.cache_clear()


def compile_cache_info() -> Any:
    """Return the standard functools cache statistics."""

    return _compile_cached.cache_info()


def _decode_schema_json(schema_json: str) -> Mapping[str, Any]:
    try:
        value = json.loads(schema_json)
    except (TypeError, json.JSONDecodeError) as exc:
        raise ScriptExecutionError(
            "The stored socket schema is invalid. Press Apply Script before queueing."
        ) from exc

    if not isinstance(value, Mapping):
        raise ScriptExecutionError(
            "The stored socket schema is invalid. Press Apply Script before queueing."
        )
    return value


def _normalize_result(
    result: Any, output_names: list[str]
) -> tuple[Any, ...]:
    expected = len(output_names)

    if isinstance(result, Mapping):
        missing = [name for name in output_names if name not in result]
        unexpected = [name for name in result if name not in output_names]
        if missing or unexpected:
            details = []
            if missing:
                details.append("missing: " + ", ".join(missing))
            if unexpected:
                details.append(
                    "unexpected: " + ", ".join(str(name) for name in unexpected)
                )
            raise ScriptExecutionError(
                "run() returned an output mapping with the wrong keys ("
                + "; ".join(details)
                + ")"
            )
        values = tuple(result[name] for name in output_names)
    elif isinstance(result, (tuple, list)):
        if len(result) != expected:
            raise ScriptExecutionError(
                f"run() returned {len(result)} values, but OUTPUTS declares "
                f"{expected}"
            )
        values = tuple(result)
    else:
        if expected != 1:
            raise ScriptExecutionError(
                f"run() returned a single value, but OUTPUTS declares {expected} "
                "outputs; return a tuple/list or a name-keyed mapping"
            )
        values = (result,)

    return values + (None,) * (MAX_OUTPUTS - expected)


class ComfyScriptedNode:
    """A trusted-code node whose visible sockets are managed by the frontend."""

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "code": (
                    "STRING",
                    {
                        "default": DEFAULT_CODE,
                        "multiline": True,
                        "dynamicPrompts": False,
                    },
                ),
            },
            # schema_json must be a real V1 input so ComfyUI serializes it.  The
            # frontend hides its widget; unknown keys fall back to wildcard
            # sockets created from the INPUTS declaration.
            "optional": FlexibleOptionalInputs(
                ANY_TYPE,
                {
                    "schema_json": ("STRING", {"default": ""}),
                },
            ),
        }

    RETURN_TYPES = tuple(ANY_TYPE for _ in range(MAX_OUTPUTS))
    RETURN_NAMES = tuple(f"output_{index + 1}" for index in range(MAX_OUTPUTS))
    FUNCTION = "execute"
    CATEGORY = "utils/scripted"
    DESCRIPTION = (
        "Executes trusted Python from the workflow. INPUTS and OUTPUTS are "
        "literal mappings; press Apply Script after changing them."
    )

    def execute(
        self, code: str, schema_json: str = "", **kwargs: Any
    ) -> tuple[Any, ...]:
        schema = parse_script_schema(code)

        if schema_json:
            stored_schema = _decode_schema_json(schema_json)
            if stored_schema != schema:
                raise ScriptExecutionError(
                    "The script declarations differ from the applied socket schema. "
                    "Press Apply Script before queueing."
                )

        missing_inputs: list[str] = []
        call_kwargs: dict[str, Any] = {}
        for input_spec in schema.inputs:
            name = input_spec["name"]
            options = input_spec["options"]
            if name in kwargs:
                call_kwargs[name] = kwargs[name]
            elif "default" in options:
                call_kwargs[name] = options["default"]
            elif options.get("optional") is True:
                # Omitting the keyword allows a Python default parameter (or
                # **kwargs-based run function) to implement optional behavior.
                continue
            else:
                missing_inputs.append(name)

        if missing_inputs:
            raise ScriptExecutionError(
                "Missing declared input value(s): "
                + ", ".join(missing_inputs)
                + ". Connect them or provide widget values before queueing."
            )

        source_hash = hashlib.sha256(code.encode("utf-8")).hexdigest()
        try:
            code_object = _compile_cached(source_hash, code)
        except SyntaxError as exc:
            # Normally caught by parse_script_schema, retained as a defensive
            # error in case Python's parser/compile behavior changes.
            raise ScriptExecutionError(
                f"Could not compile script at line {exc.lineno}: {exc.msg}"
            ) from exc

        namespace: dict[str, Any] = {
            "__builtins__": __builtins__,
            "__name__": "__comfy_scripted_node__",
        }
        try:
            exec(code_object, namespace, namespace)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise ScriptExecutionError(
                f"Script setup failed with {type(exc).__name__}: {exc}"
            ) from exc

        run_function = namespace.get("run")
        if not callable(run_function):
            raise ScriptExecutionError(
                "Script must define a callable `run(**inputs)` function"
            )

        try:
            result = run_function(**call_kwargs)
        except BaseException as exc:
            if isinstance(exc, (KeyboardInterrupt, SystemExit)):
                raise
            raise ScriptExecutionError(
                f"run() failed with {type(exc).__name__}: {exc}"
            ) from exc

        output_names = [item["name"] for item in schema.outputs]
        return _normalize_result(result, output_names)


def _register_schema_route() -> bool:
    """Register the analysis endpoint when imported by a running ComfyUI."""

    # Importing ComfyUI's server module outside an already-running application
    # initializes most of ComfyUI (and may probe CUDA).  Custom nodes are loaded
    # after `server` exists, so only use the live module here.
    server_module = sys.modules.get("server")
    if server_module is None:
        return False

    try:
        from aiohttp import web
    except ImportError:
        return False

    PromptServer = getattr(server_module, "PromptServer", None)
    if PromptServer is None:
        return False
    prompt_server = getattr(PromptServer, "instance", None)
    if prompt_server is None or not hasattr(prompt_server, "routes"):
        return False

    @prompt_server.routes.post("/scripted_nodes/schema")
    async def analyze_script_schema(request: Any) -> Any:
        try:
            payload = await request.json()
        except Exception:
            return web.json_response(
                {"ok": False, "error": "Request body must be valid JSON"},
                status=400,
            )

        if not isinstance(payload, Mapping) or not isinstance(
            payload.get("code"), str
        ):
            return web.json_response(
                {"ok": False, "error": "Request JSON must contain string `code`"},
                status=400,
            )

        try:
            schema = parse_script_schema(payload["code"])
        except ScriptSchemaError as exc:
            return web.json_response(
                {"ok": False, "error": str(exc)},
                status=400,
            )

        return web.json_response(
            {
                "ok": True,
                "schema": schema.to_dict(),
                "schema_json": schema_to_json(schema),
            }
        )

    return True


SCHEMA_ROUTE_REGISTERED = _register_schema_route()


__all__ = [
    "ANY_TYPE",
    "COMPILE_CACHE_SIZE",
    "DEFAULT_CODE",
    "FlexibleOptionalInputs",
    "MAX_OUTPUTS",
    "ComfyScriptedNode",
    "ScriptExecutionError",
    "ScriptSchema",
    "ScriptSchemaError",
    "clear_compile_cache",
    "compile_cache_info",
    "parse_schema",
    "parse_script_schema",
    "schema_to_json",
]
