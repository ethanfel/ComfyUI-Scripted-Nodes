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

## Image

| Script | Purpose |
| --- | --- |
| [`image/load_image_from_folder.py`](image/load_image_from_folder.py) | Load one naturally sorted image from a directory using a 1-based counter. |
| [`image/load_image_from_subfolders.py`](image/load_image_from_subfolders.py) | Recursively load images from an `original` tree and return the matching `upscaled` output folder. |
| [`image/resize_to_recommended_size.py`](image/resize_to_recommended_size.py) | Choose the closest recommended aspect ratio and resize an image batch to that exact size. |

### `load_image_from_folder.py` behavior

- `counter = 1` loads the first image in the directory, `counter = 2` loads
  the second, and so on.
- Filenames use natural ordering, so `image2.png` comes before `image10.png`.
- Supported extensions are BMP, GIF, JPEG, PNG, TIFF, and WebP,
  case-insensitively. Only files directly inside the directory are considered.
- EXIF orientation is applied, animated files use their first frame, and the
  image is returned as a one-image RGB batch.
- `file_name` contains only the basename without its extension.
- An empty directory, unreadable image, or counter outside the available range
  produces a clear node execution error.

### `load_image_from_subfolders.py` behavior

- The supplied directory may be the `original` root or any directory inside
  it, such as `original/matting-press`. Every supported image below the
  supplied directory is included, including images directly in that directory.
- The 1-based counter follows natural ordering across the complete relative
  path, so `pose2/image2.png` comes before `pose2/image10.png`, which comes
  before `pose10/image1.png`.
- `file_name` contains the selected basename without its extension.
- `upscaled_folder_path` replaces the supplied `original` root with its
  `upscaled` sibling while retaining the selected image's complete subfolder
  structure.
  For example,
  `/data/original/pov-ballsucking/selected_target/frame.png` produces
  `/data/upscaled/pov-ballsucking/selected_target/`.
- The returned folder path is absolute and ends in `/`. The script only
  returns the path; it does not create the `upscaled` directory.
- Image formats, EXIF handling, animated-file handling, and RGB conversion
  match `load_image_from_folder.py`.

### `resize_to_recommended_size.py` behavior

- The selected target is the closest aspect-ratio match among `1024×1024`,
  `896×1152`, `832×1216`, `1152×896`, and `1216×832`.
- Images are center-cropped only as much as needed to match the selected aspect
  ratio, so they are not stretched or padded.
- Downscaling uses area resampling. Inputs smaller than their selected target
  are upscaled with bicubic resampling.
- The complete batch is resized, and the selected dimensions are returned
  through `target_width` and `target_height`.
- An image already at its selected dimensions is returned unchanged.

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
