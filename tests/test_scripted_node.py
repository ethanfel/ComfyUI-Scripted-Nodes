from __future__ import annotations

import importlib.util
import json
from pathlib import Path
import sys
from typing import Any

import pytest


ROOT = Path(__file__).resolve().parents[1]
PACKAGE_NAME = "_comfyui_scripted_nodes_under_test"


def _load_extension():
    """Load the custom-node directory as ComfyUI would load a package."""
    existing = sys.modules.get(PACKAGE_NAME)
    if existing is not None:
        return existing

    spec = importlib.util.spec_from_file_location(
        PACKAGE_NAME,
        ROOT / "__init__.py",
        submodule_search_locations=[str(ROOT)],
    )
    assert spec is not None and spec.loader is not None
    package = importlib.util.module_from_spec(spec)
    sys.modules[PACKAGE_NAME] = package
    spec.loader.exec_module(package)
    return package


extension = _load_extension()
scripted_node = sys.modules[f"{PACKAGE_NAME}.scripted_node"]

ComfyScriptedNode = scripted_node.ComfyScriptedNode
MAX_OUTPUTS = scripted_node.MAX_OUTPUTS
ScriptExecutionError = scripted_node.ScriptExecutionError
ScriptSchemaError = scripted_node.ScriptSchemaError
parse_script_schema = scripted_node.parse_script_schema


VALID_SCRIPT = """
INPUTS = {
    "image": "IMAGE",
    "strength": (
        "FLOAT",
        {"default": 1.0, "min": 0.0, "max": 2.0, "step": 0.05},
    ),
    "mask": ("MASK", {"optional": True}),
}

OUTPUTS = {
    "result": "IMAGE",
    "description": "STRING",
}

def run(image, strength, mask=None):
    return {
        "description": f"strength={strength}; mask={mask is not None}",
        "result": image,
    }
"""


def _schema_dict(schema: Any) -> dict[str, Any]:
    """Accept the documented dict or lightweight schema-object representation."""
    if hasattr(schema, "to_dict"):
        value = schema.to_dict()
    elif hasattr(schema, "as_dict"):
        value = schema.as_dict()
    elif isinstance(schema, dict):
        value = dict(schema)
    else:
        value = {"inputs": schema.inputs, "outputs": schema.outputs}
    # The result is also the payload stored in schema_json, so it must be JSON-safe.
    return json.loads(json.dumps(value))


def _entries(schema: dict[str, Any], side: str) -> list[dict[str, Any]]:
    """Normalize list-style and ordered-mapping-style schema payloads."""
    entries = schema[side]
    if isinstance(entries, list):
        return entries
    if isinstance(entries, dict):
        normalized = []
        for name, descriptor in entries.items():
            if isinstance(descriptor, str):
                descriptor = {"type": descriptor}
            normalized.append({"name": name, **descriptor})
        return normalized
    raise AssertionError(f"{side} must be a list or mapping, got {type(entries)!r}")


def _execute(code: str, **inputs: Any) -> tuple[Any, ...]:
    schema_json = json.dumps(_schema_dict(parse_script_schema(code)))
    return ComfyScriptedNode().execute(
        code=code,
        schema_json=schema_json,
        **inputs,
    )


def test_parse_schema_preserves_order_types_options_and_optionality():
    schema = _schema_dict(parse_script_schema(VALID_SCRIPT))
    inputs = _entries(schema, "inputs")
    outputs = _entries(schema, "outputs")

    assert [entry["name"] for entry in inputs] == [
        "image",
        "strength",
        "mask",
    ]
    assert [entry["type"] for entry in inputs] == ["IMAGE", "FLOAT", "MASK"]
    assert inputs[0].get("required", True) is True
    assert inputs[1]["options"] == {
        "default": 1.0,
        "min": 0.0,
        "max": 2.0,
        "step": 0.05,
    }
    assert inputs[2]["options"]["optional"] is True
    assert inputs[2].get("required", False) is False

    assert [(entry["name"], entry["type"]) for entry in outputs] == [
        ("result", "IMAGE"),
        ("description", "STRING"),
    ]


def test_schema_is_a_json_safe_workflow_payload():
    schema = _schema_dict(parse_script_schema(VALID_SCRIPT))

    assert json.loads(json.dumps(schema)) == schema


def test_schema_parser_never_executes_declarations(tmp_path):
    marker = tmp_path / "parser-must-not-run-code"
    code = f"""
def make_inputs():
    open({str(marker)!r}, "w").write("executed")
    return {{"value": "FLOAT"}}

INPUTS = make_inputs()
OUTPUTS = {{"result": "FLOAT"}}

def run(value):
    return value
"""

    with pytest.raises(ScriptSchemaError):
        parse_script_schema(code)

    assert not marker.exists()


@pytest.mark.parametrize(
    "code",
    [
        # Socket names become keyword arguments, so they must be identifiers.
        """
INPUTS = {"not a valid name": "FLOAT"}
OUTPUTS = {"result": "FLOAT"}
def run(**kwargs):
    return 1.0
""",
        # Input descriptors have only the two documented forms.
        """
INPUTS = {"value": ("FLOAT", {"default": 1.0}, "extra")}
OUTPUTS = {"result": "FLOAT"}
def run(value):
    return value
""",
        # A script must expose the execution entry point.
        """
INPUTS = {}
OUTPUTS = {"result": "FLOAT"}
""",
    ],
)
def test_invalid_script_shapes_raise_schema_errors(code):
    with pytest.raises(ScriptSchemaError):
        parse_script_schema(code)


def test_schema_rejects_more_than_the_fixed_output_capacity():
    outputs = ", ".join(f'"out_{index}": "FLOAT"' for index in range(33))
    code = f"""
INPUTS = {{}}
OUTPUTS = {{{outputs}}}
def run():
    return ()
"""

    with pytest.raises(ScriptSchemaError, match="32"):
        parse_script_schema(code)


def test_comfyui_class_declares_controls_and_fixed_output_capacity():
    input_types = ComfyScriptedNode.INPUT_TYPES()
    declared_controls = {
        name
        for group in ("required", "optional", "hidden")
        for name in input_types.get(group, {})
    }

    assert {"code", "schema_json"} <= declared_controls
    assert ComfyScriptedNode.FUNCTION == "execute"
    assert MAX_OUTPUTS == 32
    assert len(ComfyScriptedNode.RETURN_TYPES) == MAX_OUTPUTS
    assert len(ComfyScriptedNode.RETURN_NAMES) == MAX_OUTPUTS


def test_extension_exports_comfyui_registration_metadata():
    mapping_keys = [
        key
        for key, value in extension.NODE_CLASS_MAPPINGS.items()
        if value is extension.ComfyScriptedNode
    ]

    assert len(mapping_keys) == 1
    assert extension.NODE_DISPLAY_NAME_MAPPINGS[mapping_keys[0]] == "Scripted Node"
    assert extension.WEB_DIRECTORY == "./web"


def test_dict_results_are_mapped_by_output_declaration_not_return_order():
    result = _execute(
        VALID_SCRIPT,
        image="pixels",
        strength=0.5,
        mask=None,
    )

    assert result[:2] == ("pixels", "strength=0.5; mask=False")
    assert len(result) == MAX_OUTPUTS
    assert result[2:] == (None,) * (MAX_OUTPUTS - 2)


def test_tuple_results_are_positional_and_padded():
    code = """
INPUTS = {"value": "INT"}
OUTPUTS = {"doubled": "INT", "label": "STRING"}
def run(value):
    return value * 2, f"value={value}"
"""

    result = _execute(code, value=4)

    assert result[:2] == (8, "value=4")
    assert result[2:] == (None,) * (MAX_OUTPUTS - 2)


def test_a_single_declared_output_accepts_a_bare_value():
    code = """
INPUTS = {"value": "INT"}
OUTPUTS = {"result": "INT"}
def run(value):
    return value + 1
"""

    result = _execute(code, value=9)

    assert result[0] == 10
    assert result[1:] == (None,) * (MAX_OUTPUTS - 1)


def test_omitted_input_uses_its_schema_default():
    code = """
INPUTS = {
    "value": "FLOAT",
    "scale": ("FLOAT", {"default": 2.5}),
}
OUTPUTS = {"result": "FLOAT"}
def run(value, scale):
    return value * scale
"""

    assert _execute(code, value=4.0)[0] == 10.0


def test_omitted_optional_input_uses_the_python_parameter_default():
    code = """
INPUTS = {
    "value": "STRING",
    "suffix": ("STRING", {"optional": True}),
}
OUTPUTS = {"result": "STRING"}
def run(value, suffix=" from default"):
    return value + suffix
"""

    assert _execute(code, value="hello")[0] == "hello from default"


@pytest.mark.parametrize(
    ("return_statement", "match"),
    [
        ('return {"first": 1}', "second"),
        ('return {"first": 1, "second": 2, "third": 3}', "third"),
        ("return (1,)", "2"),
        ("return (1, 2, 3)", "2"),
        ("return 1", "2"),
    ],
)
def test_result_shape_must_exactly_match_declared_outputs(
    return_statement,
    match,
):
    code = f"""
INPUTS = {{}}
OUTPUTS = {{"first": "INT", "second": "INT"}}
def run():
    {return_statement}
"""

    with pytest.raises(ScriptExecutionError, match=match):
        _execute(code)


def test_script_exception_is_wrapped_with_its_message_and_cause():
    code = """
INPUTS = {}
OUTPUTS = {"result": "INT"}
def run():
    raise RuntimeError("deliberate boom")
"""

    with pytest.raises(ScriptExecutionError, match="deliberate boom") as error:
        _execute(code)

    assert isinstance(error.value.__cause__, RuntimeError)
