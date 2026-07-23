# Script collection

These files are complete source snippets for the Scripted Node. They are not
registered as separate ComfyUI nodes. They also appear as read-only entries in
the **Script Browser** node.

To use one:

1. Open the script file and copy its entire contents.
2. Paste it into a Scripted Node.
3. Choose **Apply Script**.
4. Connect values to the generated inputs and queue the workflow.

The current Scripted Node creates linkable sockets for per-script inputs. Use
Primitive or other provider nodes for values such as `STRING` and `INT`.

## Text

| Script | Purpose |
| --- | --- |
| [`text/load_text_file.py`](text/load_text_file.py) | Read a UTF-8 text file and return its complete content with numbered lines. |
| [`text/line_from_file.py`](text/line_from_file.py) | Read one line from a UTF-8 text file using a 1-based line number. |

### `load_text_file.py` behavior

- The complete file is returned through the `content` STRING output.
- Each line is prefixed with its 1-based line number, such as `1: First line`.
- Blank lines are preserved and receive their own line number.
- LF and CRLF input line endings are supported; output lines use LF.
- UTF-8 files with or without a byte-order mark are supported.
- An empty file returns an empty string.

### `line_from_file.py` behavior

- `line_number = 1` returns the first line.
- Interior blank lines are returned as an empty string.
- LF and CRLF line endings are supported and are not included in the output.
- A final newline terminates the last line; it does not create another
  selectable blank line.
- UTF-8 files with or without a byte-order mark are supported.
- An empty file or a line number outside the available range produces a clear
  node execution error.

Absolute paths are the most predictable. Relative paths are resolved from
ComfyUI's working directory. The script can read any file available to the
ComfyUI process, including through symbolic links, so only use trusted
workflows and paths.
