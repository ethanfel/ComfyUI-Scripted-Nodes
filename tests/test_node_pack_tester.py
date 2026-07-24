from __future__ import annotations

import asyncio
import importlib.util
import json
import math
from pathlib import Path
import sys
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_NAME = "_node_pack_tester_tests"


def _load_module():
    existing = sys.modules.get(MODULE_NAME)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        MODULE_NAME,
        ROOT / "node_pack_tester.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


tester = _load_module()


@pytest.mark.parametrize(
    ("value", "slug"),
    [
        ("owner/repository", "owner/repository"),
        (
            "https://github.com/owner/repository",
            "owner/repository",
        ),
        (
            "https://github.com/owner/repository.git/",
            "owner/repository",
        ),
    ],
)
def test_normalize_github_source_accepts_canonical_inputs(value, slug):
    result = tester.normalize_github_source(value)

    assert result.slug == slug
    assert result.url == f"https://github.com/{slug}.git"


@pytest.mark.parametrize(
    "value",
    [
        "",
        "http://github.com/owner/repository",
        "https://github.example/owner/repository",
        "https://api.github.com/owner/repository",
        "https://user:secret@github.com/owner/repository",
        "https://github.com:443/owner/repository",
        "https://github.com:bad/owner/repository",
        "https://github.com/owner/repository?ref=main",
        "https://github.com/owner/repository#main",
        "https://github.com/owner/repository/tree/main",
        "https://github.com/owner/repository%2Ftree",
        "git@github.com:owner/repository.git",
        "file:///tmp/repository",
        "../repository",
    ],
)
def test_normalize_github_source_rejects_unsafe_inputs(value):
    with pytest.raises(tester.RepositoryValidationError):
        tester.normalize_github_source(value)


def test_normalize_revision_uses_explicit_ref_kinds():
    assert tester.normalize_revision("default", "") == ("default", "HEAD")
    assert tester.normalize_revision("branch", "feature/test") == (
        "branch",
        "refs/heads/feature/test",
    )
    assert tester.normalize_revision("tag", "v1.2.3") == (
        "tag",
        "refs/tags/v1.2.3",
    )
    commit = "a" * 40
    assert tester.normalize_revision("commit", commit) == ("commit", commit)


@pytest.mark.parametrize(
    ("kind", "value"),
    [
        ("unknown", ""),
        ("default", "main"),
        ("branch", ""),
        ("branch", "--upload-pack=bad"),
        ("branch", "feature..bad"),
        ("branch", "feature//bad"),
        ("branch", "feature@{bad"),
        ("branch", "feature:bad"),
        ("branch", ".hidden"),
        ("tag", "release.lock"),
        ("commit", "abc123"),
        ("commit", "g" * 40),
    ],
)
def test_normalize_revision_rejects_unsafe_values(kind, value):
    with pytest.raises(tester.RevisionValidationError):
        tester.normalize_revision(kind, value)


@pytest.mark.parametrize("kind", ["branch", "tag"])
def test_normalize_revision_rejects_lone_at_sign(kind):
    with pytest.raises(tester.RevisionValidationError):
        tester.normalize_revision(kind, "@")


def test_normalize_subdirectory_accepts_only_relative_posix_paths():
    assert tester.normalize_subdirectory("") == ""
    assert tester.normalize_subdirectory(".") == ""
    assert tester.normalize_subdirectory(" packs/example/ ") == "packs/example"

    for value in (
        "/absolute",
        "../escape",
        "packs/../escape",
        "packs//example",
        r"packs\example",
        "drive:name",
        "bad\x00name",
    ):
        with pytest.raises(tester.SubdirectoryValidationError):
            tester.normalize_subdirectory(value)


def _tree_entry(
    path: str,
    object_id: str,
    *,
    mode: str = "100644",
    object_type: str = "blob",
) -> bytes:
    return f"{mode} {object_type} {object_id}\t{path}\0".encode("utf-8")


def test_tree_parser_filters_non_runtime_files_and_links():
    object_id = "1" * 40
    tree = b"".join(
        [
            _tree_entry("pack/__init__.py", object_id),
            _tree_entry("pack/nodes/basic.py", object_id),
            _tree_entry("pack/tests/fixture.py", object_id),
            _tree_entry(
                "pack/linked.py",
                object_id,
                mode="120000",
            ),
            _tree_entry("pack/requirements.txt", object_id),
            _tree_entry("outside.py", object_id),
        ]
    )

    candidates, metadata = tester._parse_tree(tree, "pack")

    assert candidates == [
        ("__init__.py", object_id),
        ("nodes/basic.py", object_id),
    ]
    assert metadata == ("requirements.txt",)


def test_tree_parser_rejects_missing_subdirectory_and_large_files(
    monkeypatch,
):
    object_id = "2" * 40
    entry = _tree_entry("nodes.py", object_id)

    with pytest.raises(tester.SubdirectoryValidationError):
        tester._parse_tree(entry, "missing")

    monkeypatch.setattr(tester, "MAX_SOURCE_FILE_BYTES", 10)
    with pytest.raises(tester.NodePackTooLargeError):
        tester._parse_blob_sizes(
            f"{object_id} blob 11\n".encode(),
            [("nodes.py", object_id)],
        )


def test_batch_blob_decoder_checks_ids_sizes_and_boundaries():
    first_id = "a" * 40
    second_id = "b" * 40
    first = b"print('one')\n"
    second = b""
    output = (
        f"{first_id} blob {len(first)}\n".encode()
        + first
        + b"\n"
        + f"{second_id} blob 0\n".encode()
        + b"\n"
    )

    decoded = tester._decode_batch_blobs(
        output,
        [
            ("one.py", first_id, len(first)),
            ("empty.py", second_id, 0),
        ],
    )

    assert decoded == {"one.py": first, "empty.py": second}


PACK_SOURCE = """
class Basic:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"image": "IMAGE"}}

    RETURN_TYPES = ("IMAGE",)
    RETURN_NAMES = ("image",)
    FUNCTION = "run"

    def run(self, image):
        return (image,)


class DynamicInputs:
    @classmethod
    def INPUT_TYPES(cls):
        return make_inputs()

    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"

    def run(self):
        return ("ok",)


class OutputNode:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}

    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    OUTPUT_NODE = True

    def run(self):
        return ("ok",)


class HiddenInput:
    @classmethod
    def INPUT_TYPES(cls):
        return {
            "required": {},
            "hidden": {"unique_id": "UNIQUE_ID"},
        }

    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"

    def run(self, unique_id):
        return (unique_id,)


class ListInput:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"images": "IMAGE"}}

    RETURN_TYPES = ("IMAGE",)
    FUNCTION = "run"
    INPUT_IS_LIST = True

    def run(self, images):
        return (images[0],)


class AsyncNode:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}

    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"

    async def run(self):
        return ("no",)


NODE_CLASS_MAPPINGS = {
    "Basic": Basic,
    "Dynamic": DynamicInputs,
    "Output": OutputNode,
    "Hidden": HiddenInput,
    "List": ListInput,
    "Async": AsyncNode,
}

NODE_DISPLAY_NAME_MAPPINGS = {
    "Basic": "Basic Display",
}
"""

BASIC_MAPPED_NODE_SOURCE = """
class BasicNode:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("ok",)

NODE_CLASS_MAPPINGS = {"Basic": BasicNode}
"""


def test_static_analyzer_classifies_core_and_advanced_nodes():
    report = tester.analyze_python_sources(
        {"__init__.py": PACK_SOURCE},
        source={"repository": "owner/pack"},
    )
    by_id = {node["class_id"]: node for node in report["nodes"]}

    assert report["summary"] == {
        "compatible": 1,
        "partial": 2,
        "unsupported": 3,
        "total": 6,
    }
    assert by_id["Basic"]["status"] == "compatible"
    assert by_id["Basic"]["display_name"] == "Basic Display"
    assert by_id["Dynamic"]["status"] == "partial"
    assert "computed dynamically" in " ".join(by_id["Dynamic"]["reasons"])
    assert by_id["Output"]["status"] == "partial"
    assert "OUTPUT_NODE" in " ".join(by_id["Output"]["reasons"])
    assert by_id["Hidden"]["status"] == "unsupported"
    assert "hidden" in " ".join(by_id["Hidden"]["reasons"])
    assert by_id["List"]["status"] == "unsupported"
    assert "INPUT_IS_LIST" in " ".join(by_id["List"]["reasons"])
    assert by_id["Async"]["status"] == "unsupported"
    assert "asynchronous" in " ".join(by_id["Async"]["reasons"])


def test_static_analyzer_resolves_imported_classes_and_dynamic_candidates():
    sources = {
        "__init__.py": """
from .nodes import ImportedNode as PublicNode
NODE_CLASS_MAPPINGS = {"Public": PublicNode}
""",
        "nodes.py": """
class ImportedNode:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("ok",)

class DynamicallyRegistered:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("ok",)
""",
    }

    report = tester.analyze_python_sources(sources)
    by_id = {node["class_id"]: node for node in report["nodes"]}

    assert by_id["Public"]["status"] == "compatible"
    assert by_id["Public"]["class_name"] == "ImportedNode"
    assert by_id["DynamicallyRegistered"]["status"] == "partial"
    assert by_id["DynamicallyRegistered"]["mapped"] is False


def test_static_analyzer_ignores_classes_and_mappings_in_nested_scopes():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
def make_nodes():
    class NotARegisteredNode:
        @classmethod
        def INPUT_TYPES(cls):
            return {"required": {}}
        RETURN_TYPES = ("STRING",)
        FUNCTION = "run"
        def run(self):
            return ("no",)

    NODE_CLASS_MAPPINGS = {"Nested": NotARegisteredNode}
    return NODE_CLASS_MAPPINGS
"""
        }
    )

    assert report["summary"]["total"] == 0
    assert report["nodes"] == []


def test_static_analyzer_rejects_unadaptable_literal_schemas():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
BASE_INPUTS = {"hidden": {"prompt": "PROMPT"}}

class ThreePartDescriptor:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"value": ("INT", {}, "extra")}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self, value):
        return (str(value),)

class BlankSocket:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"value": "   "}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self, value):
        return (value,)

class UnpackedInputs:
    @classmethod
    def INPUT_TYPES(cls):
        return {**BASE_INPUTS, "required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("no",)

class InstanceInputs:
    def INPUT_TYPES(self):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("no",)

class NonJsonOptions:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"value": ("INT", {"choices": {1, 2}})}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self, value):
        return (str(value),)

class OutputListMismatch:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING", "INT")
    OUTPUT_IS_LIST = (False,)
    FUNCTION = "run"
    def run(self):
        return ("ok", 1)

NODE_CLASS_MAPPINGS = {
    "ThreePart": ThreePartDescriptor,
    "Blank": BlankSocket,
    "Unpacked": UnpackedInputs,
    "Instance": InstanceInputs,
    "NonJson": NonJsonOptions,
    "OutputListMismatch": OutputListMismatch,
}
"""
        }
    )
    by_id = {node["class_id"]: node for node in report["nodes"]}

    assert report["summary"]["unsupported"] == 6
    assert "more than type and options" in " ".join(by_id["ThreePart"]["reasons"])
    assert "empty socket type" in " ".join(by_id["Blank"]["reasons"])
    assert "dictionary unpacking" in " ".join(by_id["Unpacked"]["reasons"])
    assert "requires instance" in " ".join(by_id["Instance"]["reasons"])
    assert "not JSON-serializable" in " ".join(by_id["NonJson"]["reasons"])
    assert "length does not match" in " ".join(by_id["OutputListMismatch"]["reasons"])


def test_static_analyzer_downgrades_only_unresolved_external_bases():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
class LocalBase:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("ok",)

class LocalChild(LocalBase):
    pass

class ExternalChild(UnknownFrameworkBase):
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("ok",)

NODE_CLASS_MAPPINGS = {
    "Local": LocalChild,
    "External": ExternalChild,
}
"""
        }
    )
    by_id = {node["class_id"]: node for node in report["nodes"]}

    assert by_id["Local"]["status"] == "compatible"
    assert by_id["External"]["status"] == "partial"
    assert "unresolved base" in " ".join(by_id["External"]["reasons"])


def test_static_analyzer_rejects_dynamic_sections_and_ignores_nested_returns():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
HIDDEN = "hidden"

class DynamicSection:
    @classmethod
    def INPUT_TYPES(cls):
        return {HIDDEN: {"prompt": "PROMPT"}, "required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("no",)

class NestedReturnOnly:
    @classmethod
    def INPUT_TYPES(cls):
        def helper():
            return {"required": {}}
        helper()
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("no",)

NODE_CLASS_MAPPINGS = {
    "DynamicSection": DynamicSection,
    "NestedReturnOnly": NestedReturnOnly,
}
"""
        }
    )
    by_id = {node["class_id"]: node for node in report["nodes"]}

    assert by_id["DynamicSection"]["status"] == "unsupported"
    assert "dynamic section" in " ".join(by_id["DynamicSection"]["reasons"])
    assert by_id["NestedReturnOnly"]["status"] == "partial"
    assert "computed dynamically" in " ".join(by_id["NestedReturnOnly"]["reasons"])


def test_static_analyzer_checks_execution_signature_and_return_shape():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
class MissingInput:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"value": "STRING"}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("no",)

class MissingReturn:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        pass

class WrongArity:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING", "INT")
    FUNCTION = "run"
    def run(self):
        return ("one",)

class DynamicReturn:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return make_result()

NODE_CLASS_MAPPINGS = {
    "MissingInput": MissingInput,
    "MissingReturn": MissingReturn,
    "WrongArity": WrongArity,
    "DynamicReturn": DynamicReturn,
}
"""
        }
    )
    by_id = {node["class_id"]: node for node in report["nodes"]}

    assert by_id["MissingInput"]["status"] == "unsupported"
    assert "does not accept input" in " ".join(by_id["MissingInput"]["reasons"])
    assert by_id["MissingReturn"]["status"] == "unsupported"
    assert "does not return" in " ".join(by_id["MissingReturn"]["reasons"])
    assert by_id["WrongArity"]["status"] == "unsupported"
    assert "return count" in " ".join(by_id["WrongArity"]["reasons"])
    assert by_id["DynamicReturn"]["status"] == "partial"
    assert "return shape" in " ".join(by_id["DynamicReturn"]["reasons"])


def test_optional_inputs_require_optional_execution_parameters():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
class RequiredAtRuntime:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}, "optional": {"value": "STRING"}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self, value):
        return (value,)

class OptionalAtRuntime:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}, "optional": {"value": "STRING"}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self, value=""):
        return (value,)

NODE_CLASS_MAPPINGS = {
    "RequiredAtRuntime": RequiredAtRuntime,
    "OptionalAtRuntime": OptionalAtRuntime,
}
"""
        }
    )
    by_id = {node["class_id"]: node for node in report["nodes"]}

    assert by_id["RequiredAtRuntime"]["status"] == "unsupported"
    assert "requires optional input" in " ".join(by_id["RequiredAtRuntime"]["reasons"])
    assert by_id["OptionalAtRuntime"]["status"] == "compatible"


def test_conditional_and_unreachable_mappings_are_never_green():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
class Candidate:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("ok",)

if False:
    NODE_CLASS_MAPPINGS = {"Unreachable": Candidate}

if MAYBE_ENABLED:
    NODE_CLASS_MAPPINGS = {"Conditional": Candidate}
"""
        }
    )
    by_id = {node["class_id"]: node for node in report["nodes"]}

    assert "Unreachable" not in by_id
    assert by_id["Conditional"]["status"] == "partial"
    assert "conditional" in " ".join(by_id["Conditional"]["reasons"])


def test_external_import_does_not_resolve_to_unrelated_local_class():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
from external_package import Node
NODE_CLASS_MAPPINGS = {"External": Node}
""",
            "other.py": """
class Node:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("local",)
""",
        }
    )
    by_id = {node["class_id"]: node for node in report["nodes"]}

    assert by_id["External"]["status"] == "partial"
    assert "could not be resolved" in " ".join(by_id["External"]["reasons"])
    assert all(node["status"] != "compatible" for node in report["nodes"])


def test_external_base_does_not_resolve_to_unrelated_local_base():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
from external_package import Base

class PublicNode(Base):
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("ok",)

NODE_CLASS_MAPPINGS = {"Public": PublicNode}
""",
            "other.py": "class Base: pass",
        }
    )

    node = next(node for node in report["nodes"] if node["class_id"] == "Public")
    assert node["status"] == "partial"
    assert "unresolved base" in " ".join(node["reasons"])


def test_dynamic_mapping_mutations_downgrade_literal_entries():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
class Basic:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("ok",)

NODE_CLASS_MAPPINGS = {"Basic": Basic}
NODE_CLASS_MAPPINGS.update(make_more_nodes())
"""
        }
    )

    node = next(node for node in report["nodes"] if node["class_id"] == "Basic")
    assert node["status"] == "partial"
    assert "mutates node registrations dynamically" in " ".join(node["reasons"])


def test_dynamic_flags_assigned_hooks_and_async_constructor_are_not_green():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
class DynamicFlag:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    OUTPUT_NODE = FLAG
    def run(self):
        return ("ok",)

class AssignedValidation:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    VALIDATE_INPUTS = validator
    def run(self):
        return ("ok",)

class AssignedLazy:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    check_lazy_status = callback
    def run(self):
        return ("ok",)

class AsyncConstructor:
    async def __init__(self):
        pass
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("ok",)

NODE_CLASS_MAPPINGS = {
    "DynamicFlag": DynamicFlag,
    "AssignedValidation": AssignedValidation,
    "AssignedLazy": AssignedLazy,
    "AsyncConstructor": AsyncConstructor,
}
"""
        }
    )
    by_id = {node["class_id"]: node for node in report["nodes"]}

    assert by_id["DynamicFlag"]["status"] == "partial"
    assert by_id["AssignedValidation"]["status"] == "partial"
    assert by_id["AssignedLazy"]["status"] == "unsupported"
    assert by_id["AsyncConstructor"]["status"] == "unsupported"


def _assert_node_ids_are_not_compatible(report, *class_ids):
    for class_id in class_ids:
        assert not any(
            node["class_id"] == class_id and node["status"] == "compatible"
            for node in report["nodes"]
        )


def test_constructor_receiver_accepts_arbitrary_names_and_varargs():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
class ArbitraryReceiver:
    def __init__(this):
        pass
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("ok",)

class VariadicReceiver:
    def __init__(*args):
        pass
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("ok",)

class MissingReceiver:
    def __init__():
        pass
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("no",)

NODE_CLASS_MAPPINGS = {
    "ArbitraryReceiver": ArbitraryReceiver,
    "VariadicReceiver": VariadicReceiver,
    "MissingReceiver": MissingReceiver,
}
"""
        }
    )
    by_id = {node["class_id"]: node for node in report["nodes"]}

    assert by_id["ArbitraryReceiver"]["status"] == "compatible"
    assert by_id["VariadicReceiver"]["status"] == "compatible"
    assert by_id["MissingReceiver"]["status"] == "unsupported"
    assert "constructor requires arguments" in " ".join(
        by_id["MissingReceiver"]["reasons"]
    )


def test_execution_varargs_can_receive_instance_but_receiver_name_cannot_be_input():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
class VariadicExecution:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(*args):
        return ("ok",)

class ReceiverCollision:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {"self": ("STRING",)}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self, **kwargs):
        return (kwargs["self"],)

NODE_CLASS_MAPPINGS = {
    "VariadicExecution": VariadicExecution,
    "ReceiverCollision": ReceiverCollision,
}
"""
        }
    )
    by_id = {node["class_id"]: node for node in report["nodes"]}

    assert by_id["VariadicExecution"]["status"] == "compatible"
    assert by_id["ReceiverCollision"]["status"] == "unsupported"
    assert "bound method receiver" in " ".join(by_id["ReceiverCollision"]["reasons"])


def test_starred_execution_returns_are_partial_not_fixed_arity():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
class StarredTuple:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        values = []
        return (*values,)

class StarredResult:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        values = []
        return {"result": (*values,)}

NODE_CLASS_MAPPINGS = {
    "StarredTuple": StarredTuple,
    "StarredResult": StarredResult,
}
"""
        }
    )
    by_id = {node["class_id"]: node for node in report["nodes"]}

    for class_id in ("StarredTuple", "StarredResult"):
        assert by_id[class_id]["status"] == "partial"
        assert "iterable unpacking" in " ".join(by_id[class_id]["reasons"])


def test_subscript_registration_without_mapping_binding_is_not_green():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
class Candidate:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("ok",)

NODE_CLASS_MAPPINGS["Candidate"] = Candidate
"""
        }
    )

    node = next(node for node in report["nodes"] if node["class_id"] == "Candidate")
    assert node["status"] == "partial"
    assert "mutates node registrations dynamically" in " ".join(node["reasons"])


def test_input_types_generator_is_unsupported():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
class GeneratorInputs:
    @classmethod
    def INPUT_TYPES(cls):
        if False:
            yield None
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("no",)

NODE_CLASS_MAPPINGS = {"GeneratorInputs": GeneratorInputs}
"""
        }
    )

    node = next(
        node for node in report["nodes"] if node["class_id"] == "GeneratorInputs"
    )
    assert node["status"] == "unsupported"
    assert "INPUT_TYPES is a generator" in node["reasons"]


def test_abstractmethod_hierarchy_is_not_green():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
from abc import ABC, abstractmethod

class AbstractBase(ABC):
    @abstractmethod
    def unresolved(self):
        pass

class InheritedAbstract(AbstractBase):
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("no",)

class DirectAbstract(ABC):
    @abstractmethod
    def unresolved(self):
        pass
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("no",)

NODE_CLASS_MAPPINGS = {
    "InheritedAbstract": InheritedAbstract,
    "DirectAbstract": DirectAbstract,
}
"""
        }
    )
    by_id = {node["class_id"]: node for node in report["nodes"]}

    _assert_node_ids_are_not_compatible(
        report,
        "InheritedAbstract",
        "DirectAbstract",
    )
    assert "abstract methods" in " ".join(by_id["InheritedAbstract"]["reasons"])
    assert "abstract methods" in " ".join(by_id["DirectAbstract"]["reasons"])


def test_assigned_noncallable_constructor_and_allocator_are_unsupported():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
class MissingConstructor:
    __init__ = None
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("no",)

class MissingAllocator:
    __new__ = None
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("no",)

NODE_CLASS_MAPPINGS = {
    "MissingConstructor": MissingConstructor,
    "MissingAllocator": MissingAllocator,
}
"""
        }
    )
    by_id = {node["class_id"]: node for node in report["nodes"]}

    assert by_id["MissingConstructor"]["status"] == "unsupported"
    assert "non-callable" in " ".join(by_id["MissingConstructor"]["reasons"])
    assert by_id["MissingAllocator"]["status"] == "unsupported"
    assert "non-callable" in " ".join(by_id["MissingAllocator"]["reasons"])


@pytest.mark.parametrize(
    "root_source",
    (
        "not valid Python (",
        "return\n",
    ),
    ids=("parse-error", "semantic-error"),
)
def test_invalid_root_entrypoint_prevents_nonroot_green_results(root_source):
    report = tester.analyze_python_sources(
        {
            "__init__.py": root_source,
            "nodes.py": BASIC_MAPPED_NODE_SOURCE,
        }
    )

    _assert_node_ids_are_not_compatible(report, "Basic")
    node = next(node for node in report["nodes"] if node["class_id"] == "Basic")
    assert "root __init__.py" in " ".join(node["reasons"])


def test_unreferenced_nonroot_mapping_is_not_green():
    report = tester.analyze_python_sources(
        {
            "__init__.py": "",
            "unused.py": BASIC_MAPPED_NODE_SOURCE,
        }
    )

    node = next(node for node in report["nodes"] if node["class_id"] == "Basic")
    assert node["status"] == "partial"
    assert "import reachability is unverified" in " ".join(node["reasons"])


def test_mapping_redefinition_preserves_ambiguous_runtime_targets():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
class Candidate:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("ok",)

NODE_CLASS_MAPPINGS = {"Public": Candidate}

class Candidate:
    @classmethod
    async def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    async def run(self):
        return ("no",)

NODE_CLASS_MAPPINGS["Public"] = Candidate
"""
        }
    )
    public_results = [node for node in report["nodes"] if node["class_id"] == "Public"]

    assert len(public_results) == 2
    assert all(node["status"] != "compatible" for node in public_results)
    assert any(node["status"] == "unsupported" for node in public_results)
    assert any(
        "multiple possible classes" in reason
        for node in public_results
        for reason in node["reasons"]
    )


def test_cross_file_same_name_mappings_are_kept_ambiguous():
    async_source = BASIC_MAPPED_NODE_SOURCE.replace(
        "def INPUT_TYPES(cls):",
        "async def INPUT_TYPES(cls):",
    ).replace(
        "def run(self):",
        "async def run(self):",
    )
    report = tester.analyze_python_sources(
        {
            "__init__.py": "from .b import NODE_CLASS_MAPPINGS\n",
            "a.py": BASIC_MAPPED_NODE_SOURCE,
            "b.py": async_source,
        }
    )
    basic_results = [node for node in report["nodes"] if node["class_id"] == "Basic"]

    assert len(basic_results) == 2
    assert {node["source_file"] for node in basic_results} == {"a.py", "b.py"}
    assert all(node["status"] != "compatible" for node in basic_results)
    assert any(node["status"] == "unsupported" for node in basic_results)
    assert any("multiple classes" in warning for warning in report["warnings"])


def test_zero_argument_classmethod_input_types_is_not_compatible():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
class MissingBoundClass:
    @classmethod
    def INPUT_TYPES():
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("no",)

NODE_CLASS_MAPPINGS = {"MissingBoundClass": MissingBoundClass}
"""
        }
    )

    node = next(
        node for node in report["nodes"] if node["class_id"] == "MissingBoundClass"
    )
    assert node["status"] == "unsupported"


def test_mapping_before_class_definition_is_not_compatible():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
NODE_CLASS_MAPPINGS = {"DefinedLater": DefinedLater}

class DefinedLater:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("no",)
"""
        }
    )

    _assert_node_ids_are_not_compatible(report, "DefinedLater")


def test_child_defined_before_base_is_not_compatible():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
class Child(Base):
    pass

class Base:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("no",)

NODE_CLASS_MAPPINGS = {"Child": Child}
"""
        }
    )

    _assert_node_ids_are_not_compatible(report, "Child")


@pytest.mark.parametrize(
    "shadowing_statement",
    (
        "from .broken import Candidate",
        "Candidate = object()",
    ),
)
def test_mapping_resolution_respects_later_symbol_bindings(shadowing_statement):
    report = tester.analyze_python_sources(
        {
            "__init__.py": f"""
class Candidate:
    @classmethod
    def INPUT_TYPES(cls):
        return {{"required": {{}}}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("decoy",)

{shadowing_statement}
NODE_CLASS_MAPPINGS = {{"Shadowed": Candidate}}
""",
            "broken.py": "class Candidate: pass",
        }
    )

    _assert_node_ids_are_not_compatible(report, "Shadowed")


def test_qualified_relative_module_alias_resolves_the_submodule():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
from .nodes import sub as selected
NODE_CLASS_MAPPINGS = {"Selected": selected.Candidate}
""",
            "nodes/__init__.py": """
class Candidate:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("decoy",)
""",
            "nodes/sub.py": "class Candidate: pass",
        }
    )

    _assert_node_ids_are_not_compatible(report, "Selected")


def test_mapping_reassignment_does_not_leave_stale_compatible_entries():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
class Candidate:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("ok",)

NODE_CLASS_MAPPINGS = {"Stale": Candidate}
NODE_CLASS_MAPPINGS = {"Live": Candidate}
"""
        }
    )

    _assert_node_ids_are_not_compatible(report, "Stale")


def test_direct_mapping_deletion_does_not_leave_compatible_entries():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
class Candidate:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("ok",)

NODE_CLASS_MAPPINGS = {"Deleted": Candidate}
del NODE_CLASS_MAPPINGS
"""
        }
    )

    _assert_node_ids_are_not_compatible(report, "Deleted")


def test_called_execution_decorator_is_not_compatible():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
def replace_method():
    def decorator(method):
        return None
    return decorator

class Decorated:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    @replace_method()
    def run(self):
        return ("no",)

NODE_CLASS_MAPPINGS = {"Decorated": Decorated}
"""
        }
    )

    _assert_node_ids_are_not_compatible(report, "Decorated")


def test_dynamic_base_expression_is_not_compatible():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
def make_base():
    class Base:
        def __init__(self, required):
            self.required = required
    return Base

class DynamicBase(make_base()):
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("no",)

NODE_CLASS_MAPPINGS = {"DynamicBase": DynamicBase}
"""
        }
    )

    _assert_node_ids_are_not_compatible(report, "DynamicBase")


def test_required_new_argument_is_unsupported():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
class RequiredNew:
    def __new__(cls, required):
        return super().__new__(cls)
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("no",)

NODE_CLASS_MAPPINGS = {"RequiredNew": RequiredNew}
"""
        }
    )

    node = next(node for node in report["nodes"] if node["class_id"] == "RequiredNew")
    assert node["status"] == "unsupported"


def test_deep_local_inheritance_does_not_escape_as_recursion_error(monkeypatch):
    monkeypatch.setattr(tester, "MAX_DISCOVERED_CLASSES", 2_000)
    source_parts = [
        """
class Base0:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("ok",)
"""
    ]
    source_parts.extend(
        f"class Base{index}(Base{index - 1}):\n    pass\n" for index in range(1, 1_200)
    )
    source_parts.append('NODE_CLASS_MAPPINGS = {"Deep": Base1199}\n')

    try:
        tester.analyze_python_sources({"__init__.py": "\n".join(source_parts)})
    except tester.NodePackTooLargeError:
        return


def test_definitions_after_top_level_raise_are_not_compatible():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
raise RuntimeError("module cannot finish loading")

class Unreachable:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("no",)

NODE_CLASS_MAPPINGS = {"Unreachable": Unreachable}
"""
        }
    )

    _assert_node_ids_are_not_compatible(report, "Unreachable")


def test_definitions_after_literal_true_raise_are_not_compatible():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
if True:
    raise RuntimeError("module cannot finish loading")

class Unreachable:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("no",)

NODE_CLASS_MAPPINGS = {"Unreachable": Unreachable}
"""
        }
    )

    _assert_node_ids_are_not_compatible(report, "Unreachable")


def test_unbound_framework_base_is_not_compatible():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
class MissingABC(ABC):
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("no",)

NODE_CLASS_MAPPINGS = {"MissingABC": MissingABC}
"""
        }
    )

    _assert_node_ids_are_not_compatible(report, "MissingABC")


@pytest.mark.parametrize(
    ("binding_source", "extra_sources"),
    (
        ("(Candidate := object())", {}),
        (
            "from .other import *",
            {"other.py": "class Candidate: pass\n"},
        ),
        (
            "match object():\n    case Candidate:\n        pass",
            {},
        ),
        (
            "try:\n    raise Exception()\nexcept Exception as Candidate:\n    pass",
            {},
        ),
    ),
    ids=("named-expression", "star-import", "match-capture", "except-target"),
)
def test_module_rebindings_do_not_resolve_a_stale_local_class(
    binding_source,
    extra_sources,
):
    sources = {
        "__init__.py": f"""
class Candidate:
    @classmethod
    def INPUT_TYPES(cls):
        return {{"required": {{}}}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("decoy",)

{binding_source}
NODE_CLASS_MAPPINGS = {{"Shadowed": Candidate}}
""",
        **extra_sources,
    }

    report = tester.analyze_python_sources(sources)

    _assert_node_ids_are_not_compatible(report, "Shadowed")


@pytest.mark.parametrize(
    "source",
    (
        """
class Good:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("ok",)

NODE_CLASS_MAPPINGS = {DefinedLater.NAME: Good}

class DefinedLater:
    NAME = "LateKey"
""",
        """
class ReboundKey:
    NAME = "StaleKey"

ReboundKey = object()

class Good:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("ok",)

NODE_CLASS_MAPPINGS = {ReboundKey.NAME: Good}
""",
    ),
    ids=("defined-after-mapping", "rebound-before-mapping"),
)
def test_dynamic_mapping_key_respects_symbol_bindings(source):
    report = tester.analyze_python_sources({"__init__.py": source})

    _assert_node_ids_are_not_compatible(report, "LateKey", "StaleKey")


def test_shadowed_classmethod_decorator_is_not_compatible():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
classmethod = lambda method: None

class ShadowedDecorator:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("no",)

NODE_CLASS_MAPPINGS = {"ShadowedDecorator": ShadowedDecorator}
"""
        }
    )

    _assert_node_ids_are_not_compatible(report, "ShadowedDecorator")


@pytest.mark.parametrize(
    "source",
    (
        """
class OverwrittenRun:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("no",)
    run = None

NODE_CLASS_MAPPINGS = {"OverwrittenRun": OverwrittenRun}
""",
        """
class OverwrittenInputs:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    INPUT_TYPES = None
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("no",)

NODE_CLASS_MAPPINGS = {"OverwrittenInputs": OverwrittenInputs}
""",
    ),
    ids=("execution-method", "input-schema-method"),
)
def test_class_member_overwrite_is_not_compatible(source):
    report = tester.analyze_python_sources({"__init__.py": source})

    _assert_node_ids_are_not_compatible(
        report,
        "OverwrittenRun",
        "OverwrittenInputs",
    )


def test_class_body_raise_is_not_compatible():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
class ClassBodyRaise:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("no",)
    raise RuntimeError("class cannot finish loading")

NODE_CLASS_MAPPINGS = {"ClassBodyRaise": ClassBodyRaise}
"""
        }
    )

    _assert_node_ids_are_not_compatible(report, "ClassBodyRaise")


def test_decorated_inherited_base_is_not_compatible():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
def replace_class(cls):
    return object

@replace_class
class DecoratedBase:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("decoy",)

class Child(DecoratedBase):
    pass

NODE_CLASS_MAPPINGS = {"Child": Child}
"""
        }
    )

    _assert_node_ids_are_not_compatible(report, "Child")


def test_multiple_inheritance_uses_python_mro_for_member_resolution():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
class Root:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("decoy",)

class Override(Root):
    FUNCTION = "missing"

class Left(Root):
    pass

class PublicNode(Left, Override):
    pass

NODE_CLASS_MAPPINGS = {"PublicNode": PublicNode}
"""
        }
    )

    _assert_node_ids_are_not_compatible(report, "PublicNode")


def test_decorated_constructor_is_not_compatible():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
def add_required_argument(method):
    return lambda self, required: None

class DecoratedConstructor:
    @add_required_argument
    def __init__(self):
        pass
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("no",)

NODE_CLASS_MAPPINGS = {"DecoratedConstructor": DecoratedConstructor}
"""
        }
    )

    _assert_node_ids_are_not_compatible(report, "DecoratedConstructor")


def test_constructor_returning_non_none_is_unsupported():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
class InvalidConstructorReturn:
    def __init__(self):
        return 1
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("no",)

NODE_CLASS_MAPPINGS = {
    "InvalidConstructorReturn": InvalidConstructorReturn,
}
"""
        }
    )

    node = next(
        node
        for node in report["nodes"]
        if node["class_id"] == "InvalidConstructorReturn"
    )
    assert node["status"] == "unsupported"


def test_static_scan_never_executes_repository_source(tmp_path):
    sentinel = tmp_path / "must-not-exist"
    malicious_source = f"""
from pathlib import Path
Path({str(sentinel)!r}).write_text("executed")

class SafeShape:
    @classmethod
    def INPUT_TYPES(cls):
        return {{"required": {{}}}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("ok",)

NODE_CLASS_MAPPINGS = {{"SafeShape": SafeShape}}
"""

    report = tester.analyze_python_sources({"__init__.py": malicious_source})

    assert report["summary"]["compatible"] == 1
    assert not sentinel.exists()


def test_pack_features_and_parse_failures_are_reported_as_notes():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
WEB_DIRECTORY = "./web"
def comfy_entrypoint():
    return None
""",
            "broken.py": "not valid Python (",
        },
        metadata_files=("requirements.txt",),
    )

    notes = "\n".join(report["warnings"])
    assert "not install" in notes
    assert "web directory" in notes
    assert "V3" in notes
    assert "broken.py" in notes
    assert report["summary"] == {
        "compatible": 0,
        "partial": 0,
        "unsupported": 1,
        "total": 1,
    }
    assert report["nodes"][0]["class_id"] == "comfy_entrypoint"
    assert "V3-only" in report["report_text"]


def test_pack_requirements_downgrade_basic_classes_to_partial():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
WEB_DIRECTORY = "./web"

class Basic:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("ok",)

NODE_CLASS_MAPPINGS = {"Basic": Basic}
"""
        },
        metadata_files=("requirements.txt",),
    )

    node = report["nodes"][0]
    assert node["status"] == "partial"
    reasons = " ".join(node["reasons"])
    assert "dependencies" in reasons
    assert "custom frontend" in reasons


def test_structured_and_text_reports_escape_repository_control_characters():
    report = tester.analyze_python_sources(
        {
            "__init__.py": """
class Basic:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("ok",)

NODE_CLASS_MAPPINGS = {"Bad\\nId": Basic}
NODE_DISPLAY_NAME_MAPPINGS = {"Bad\\nId": "Display\\tName"}
"""
        },
        source={"repository": "owner/pack\ninjected"},
    )

    node = report["nodes"][0]
    assert node["class_id"] == r"Bad\nId"
    assert node["display_name"] == r"Display\tName"
    assert "owner/pack\\ninjected" in report["report_text"]
    assert "owner/pack\ninjected" not in report["report_text"]


def test_bounded_process_output_is_rejected():
    with pytest.raises(tester.NodePackTooLargeError):
        tester._run_process(
            [
                sys.executable,
                "-c",
                "import sys; sys.stdout.buffer.write(b'x' * 4096)",
            ],
            timeout=5,
            max_output_bytes=64,
        )


def test_process_disk_monitor_terminates_oversized_git_data(tmp_path):
    output_path = tmp_path / "large-object"
    with pytest.raises(tester.NodePackTooLargeError):
        tester._run_process(
            [
                sys.executable,
                "-c",
                (
                    "from pathlib import Path; import sys, time; "
                    "Path(sys.argv[1]).write_bytes(b'x' * 4096); "
                    "time.sleep(5)"
                ),
                str(output_path),
            ],
            timeout=5,
            disk_watch_path=tmp_path,
            max_disk_bytes=64,
        )


def test_analysis_enforces_cumulative_ast_and_class_limits(monkeypatch):
    source = """
class Node:
    @classmethod
    def INPUT_TYPES(cls):
        return {"required": {}}
    RETURN_TYPES = ("STRING",)
    FUNCTION = "run"
    def run(self):
        return ("ok",)
NODE_CLASS_MAPPINGS = {"Node": Node}
"""
    monkeypatch.setattr(tester, "MAX_TOTAL_AST_NODES", 1)
    with pytest.raises(tester.NodePackTooLargeError):
        tester.analyze_python_sources({"__init__.py": source})

    monkeypatch.setattr(tester, "MAX_TOTAL_AST_NODES", 1_000_000)
    monkeypatch.setattr(tester, "MAX_DISCOVERED_CLASSES", 0)
    with pytest.raises(tester.NodePackTooLargeError):
        tester.analyze_python_sources({"__init__.py": source})


def test_source_token_limit_stops_before_ast_parsing(monkeypatch):
    parser_called = False

    def fail_if_parsed(*args, **kwargs):
        nonlocal parser_called
        parser_called = True
        raise AssertionError("oversized token streams must be rejected before parsing")

    monkeypatch.setattr(tester, "MAX_SOURCE_TOKENS_PER_FILE", 4)
    monkeypatch.setattr(tester.ast, "parse", fail_if_parsed)

    report = tester.analyze_python_sources({"__init__.py": "value = 1\n" * 100})

    assert parser_called is False
    assert report["files"]["python_parsed"] == 0
    assert any("too many lexical tokens" in warning for warning in report["warnings"])


def test_scanner_rejects_work_when_concurrency_limit_is_busy(monkeypatch):
    class BusySemaphore:
        @staticmethod
        def acquire(*, blocking):
            assert blocking is False
            return False

        @staticmethod
        def release():
            raise AssertionError("a semaphore that was not acquired was released")

    monkeypatch.setattr(tester, "_SCAN_SEMAPHORE", BusySemaphore())

    with pytest.raises(tester.NodePackBusyError):
        tester.test_node_pack("owner/repository")


def test_fetch_repository_uses_a_bare_git_database_without_checkout(
    monkeypatch,
):
    source_bytes = PACK_SOURCE.encode("utf-8")
    object_id = "b" * 40
    commit = "c" * 40
    calls = []

    def fake_run(arguments, **kwargs):
        calls.append((list(arguments), kwargs))
        command = list(arguments)
        if "rev-parse" in command:
            return f"{commit}\n".encode()
        if "ls-tree" in command:
            return _tree_entry(
                "__init__.py",
                object_id,
            )
        if "cat-file" in command:
            assert kwargs["input_bytes"] == f"{object_id}\n".encode()
            if any(argument.startswith("--batch-check=") for argument in command):
                return f"{object_id} blob {len(source_bytes)}\n".encode()
            return (
                f"{object_id} blob {len(source_bytes)}\n".encode()
                + source_bytes
                + b"\n"
            )
        return b""

    monkeypatch.setattr(tester.shutil, "which", lambda executable: "/usr/bin/git")
    monkeypatch.setattr(tester, "_run_process", fake_run)

    fetched = tester.fetch_repository("owner/repository")

    assert fetched.resolved_commit == commit
    assert fetched.python_sources["__init__.py"] == PACK_SOURCE
    flattened = [argument for call, _ in calls for argument in call]
    assert "checkout" not in flattened
    assert "--no-recurse-submodules" in flattened
    assert "protocol.allow=never" in flattened
    assert "credential.helper=" in flattened
    temporary_roots = {
        Path(call[call.index("-C") + 1]).parent for call, _ in calls if "-C" in call
    }
    assert temporary_roots
    assert all(not path.exists() for path in temporary_roots)


def test_fetch_stops_before_sizing_later_candidate_batches(monkeypatch):
    object_ids = [f"{index:040x}" for index in range(tester.GIT_BLOB_SIZE_BATCH + 1)]
    commit = "c" * 40
    batch_inputs = []

    def fake_run(arguments, **kwargs):
        command = list(arguments)
        if "rev-parse" in command:
            return f"{commit}\n".encode()
        if "ls-tree" in command:
            return b"".join(
                _tree_entry(f"node_{index}.py", object_id)
                for index, object_id in enumerate(object_ids)
            )
        if any(argument.startswith("--batch-check=") for argument in command):
            requested = kwargs["input_bytes"].decode().splitlines()
            batch_inputs.append(requested)
            return b"".join(
                (f"{object_id} blob {tester.MAX_SOURCE_FILE_BYTES + 1}\n").encode()
                for object_id in requested
            )
        if "cat-file" in command:
            raise AssertionError("blob bodies must not be fetched")
        return b""

    monkeypatch.setattr(tester.shutil, "which", lambda executable: "/usr/bin/git")
    monkeypatch.setattr(tester, "_run_process", fake_run)

    with pytest.raises(tester.NodePackTooLargeError):
        tester.fetch_repository("owner/repository")

    assert len(batch_inputs) == 1
    assert len(batch_inputs[0]) == tester.GIT_BLOB_SIZE_BATCH
    assert object_ids[-1] not in batch_inputs[0]


def test_fetch_commands_share_one_overall_deadline(monkeypatch):
    clock = [0.0]
    observed_timeouts = []

    def fake_monotonic():
        return clock[0]

    def fake_run(arguments, **kwargs):
        del arguments
        observed_timeouts.append(kwargs["timeout"])
        clock[0] += 1.0
        return b""

    monkeypatch.setattr(tester, "TOTAL_FETCH_TIMEOUT_SECONDS", 3)
    monkeypatch.setattr(tester.time, "monotonic", fake_monotonic)
    monkeypatch.setattr(tester.shutil, "which", lambda executable: "/usr/bin/git")
    monkeypatch.setattr(tester, "_run_process", fake_run)

    with pytest.raises(tester.NodePackFetchTimeout):
        tester.fetch_repository("owner/repository")

    assert observed_timeouts == pytest.approx([3.0, 2.0, 1.0])


def test_node_contract_and_execution_payload(monkeypatch):
    report = {
        "summary": {
            "compatible": 1,
            "partial": 0,
            "unsupported": 0,
            "total": 1,
        },
        "nodes": [],
        "warnings": [],
        "source": {},
        "files": {},
        "confidence": "static",
        "report_text": "report body",
    }
    monkeypatch.setattr(tester, "test_node_pack", lambda *args: report)

    result = tester.ComfyNodePackTesterNode().inspect_pack("owner/repository")

    assert not hasattr(tester.ComfyNodePackTesterNode, "OUTPUT_NODE")
    assert math.isnan(tester.ComfyNodePackTesterNode.IS_CHANGED())
    assert result["result"][0] == "report body"
    assert json.loads(result["result"][1])["summary"]["compatible"] == 1
    assert result["ui"]["compatibility_report"] == ["report body"]
    assert result["ui"]["compatibility_source"] == [
        {
            "repository": "owner/repository",
            "ref_kind": "default",
            "ref": "",
            "subdirectory": "",
        }
    ]


class _CapturedRoutes:
    def __init__(self):
        self.handlers = {}

    def post(self, path):
        def decorator(handler):
            self.handlers[("POST", path)] = handler
            return handler

        return decorator


class _Request:
    def __init__(self, payload):
        self.payload = payload

    async def json(self):
        return self.payload


def test_rest_endpoint_returns_static_report_and_validation_errors(
    monkeypatch,
):
    routes = _CapturedRoutes()
    fake_server = ModuleType("server")
    fake_server.PromptServer = SimpleNamespace(instance=SimpleNamespace(routes=routes))

    class Response:
        def __init__(self, payload, status=200):
            self.payload = payload
            self.status = status

    fake_web = SimpleNamespace(
        json_response=lambda payload, status=200: Response(payload, status)
    )
    fake_aiohttp = ModuleType("aiohttp")
    fake_aiohttp.web = fake_web
    monkeypatch.setitem(sys.modules, "server", fake_server)
    monkeypatch.setitem(sys.modules, "aiohttp", fake_aiohttp)

    route_module_name = "_node_pack_tester_route_tests"
    spec = importlib.util.spec_from_file_location(
        route_module_name,
        ROOT / "node_pack_tester.py",
    )
    assert spec is not None and spec.loader is not None
    route_module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, route_module_name, route_module)
    spec.loader.exec_module(route_module)

    report = {
        "summary": {
            "compatible": 1,
            "partial": 0,
            "unsupported": 0,
            "total": 1,
        },
        "nodes": [],
        "warnings": [],
        "source": {},
        "files": {},
        "confidence": "static",
        "report_text": "static report",
    }
    monkeypatch.setattr(
        route_module,
        "test_node_pack",
        lambda **kwargs: report,
    )

    assert route_module.NODE_PACK_TEST_ROUTE_REGISTERED is True
    handler = routes.handlers[("POST", "/scripted_nodes/node-packs/test")]

    async def exercise_route():
        response = await handler(
            _Request(
                {
                    "repository": "owner/repository",
                    "ref_kind": "default",
                    "ref": "",
                    "subdirectory": "",
                }
            )
        )
        assert response.status == 200
        assert response.payload["report_text"] == "static report"

        malformed = await handler(_Request(None))
        assert malformed.status == 400

        def fail(**kwargs):
            raise route_module.RepositoryValidationError("bad repository")

        monkeypatch.setattr(route_module, "test_node_pack", fail)
        invalid = await handler(
            _Request(
                {
                    "repository": "bad",
                    "ref_kind": "default",
                    "ref": "",
                    "subdirectory": "",
                }
            )
        )
        assert invalid.status == 400
        assert invalid.payload["code"] == "invalid_repository"

    asyncio.run(exercise_route())
