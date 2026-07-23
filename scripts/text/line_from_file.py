from pathlib import Path


INPUTS = {
    "file_path": "STRING",
    "line_number": (
        "INT",
        {"default": 1, "min": 1, "step": 1},
    ),
}

OUTPUTS = {
    "line": "STRING",
}


def run(file_path, line_number):
    if not file_path.strip():
        raise ValueError("file_path cannot be empty")

    path = Path(file_path).expanduser()
    lines = path.read_text(encoding="utf-8-sig").splitlines()

    if not lines:
        raise ValueError(f"Text file contains no lines: {path}")
    if not 1 <= line_number <= len(lines):
        raise ValueError(
            f"line_number {line_number} is outside 1..{len(lines)} for {path}"
        )

    return {"line": lines[line_number - 1]}
