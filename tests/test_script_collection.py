from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
BACKEND_MODULE_NAME = "_scripted_node_collection_tests"
LINE_FROM_FILE = ROOT / "scripts" / "text" / "line_from_file.py"


def _load_backend():
    existing = sys.modules.get(BACKEND_MODULE_NAME)
    if existing is not None:
        return existing

    spec = importlib.util.spec_from_file_location(
        BACKEND_MODULE_NAME,
        ROOT / "scripted_node.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[BACKEND_MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


backend = _load_backend()
CODE = LINE_FROM_FILE.read_text(encoding="utf-8")


def _run(file_path: Path | str, line_number: int) -> str:
    schema = backend.parse_script_schema(CODE)
    result = backend.ComfyScriptedNode().execute(
        code=CODE,
        schema_json=backend.schema_to_json(schema),
        file_path=str(file_path),
        line_number=line_number,
    )
    return result[0]


def test_line_from_file_declares_expected_schema():
    schema = backend.parse_script_schema(CODE)

    assert schema.inputs == [
        {"name": "file_path", "type": "STRING", "options": {}},
        {
            "name": "line_number",
            "type": "INT",
            "options": {"default": 1, "min": 1, "step": 1},
        },
    ]
    assert schema.outputs == [
        {"name": "line", "type": "STRING"},
    ]


def test_line_from_file_handles_bom_crlf_unicode_and_blank_lines(tmp_path):
    text_file = tmp_path / "lines.txt"
    text_file.write_bytes(
        b"\xef\xbb\xbfalpha\r\ncaf\xc3\xa9\r\n\r\nomega\r\n"
    )

    assert _run(text_file, 1) == "alpha"
    assert _run(text_file, 2) == "café"
    assert _run(text_file, 3) == ""
    assert _run(text_file, 4) == "omega"


@pytest.mark.parametrize("line_number", [0, -1, 4])
def test_line_from_file_rejects_out_of_range_numbers(tmp_path, line_number):
    text_file = tmp_path / "lines.txt"
    text_file.write_text("one\ntwo\nthree", encoding="utf-8")

    with pytest.raises(
        backend.ScriptExecutionError,
        match=rf"line_number {line_number} is outside 1\.\.3",
    ):
        _run(text_file, line_number)


def test_line_from_file_rejects_an_empty_file(tmp_path):
    text_file = tmp_path / "empty.txt"
    text_file.write_text("", encoding="utf-8")

    with pytest.raises(
        backend.ScriptExecutionError,
        match="Text file contains no lines",
    ):
        _run(text_file, 1)


def test_line_from_file_rejects_an_empty_path():
    with pytest.raises(
        backend.ScriptExecutionError,
        match="file_path cannot be empty",
    ):
        _run("   ", 1)


def test_line_from_file_reports_a_missing_file(tmp_path):
    missing = tmp_path / "missing.txt"

    with pytest.raises(
        backend.ScriptExecutionError,
        match="FileNotFoundError",
    ):
        _run(missing, 1)
