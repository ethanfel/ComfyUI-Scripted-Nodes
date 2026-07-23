from pathlib import Path


INPUTS = {
    "file_path": "STRING",
}

OUTPUTS = {
    "content": "STRING",
}


def run(file_path):
    if not file_path.strip():
        raise ValueError("file_path cannot be empty")

    path = Path(file_path).expanduser()
    lines = path.read_text(encoding="utf-8-sig").splitlines()
    content = "\n".join(
        f"{line_number}: {line}"
        for line_number, line in enumerate(lines, start=1)
    )
    return {"content": content}
