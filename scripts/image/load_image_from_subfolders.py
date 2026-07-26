import re
from pathlib import Path

IMAGE_EXTENSIONS = {
    ".bmp",
    ".gif",
    ".jpe",
    ".jpeg",
    ".jfif",
    ".jpg",
    ".png",
    ".tif",
    ".tiff",
    ".webp",
}

INPUTS = {
    "directory_path": "STRING",
    "counter": (
        "INT",
        {"default": 1, "min": 1, "step": 1},
    ),
}

OUTPUTS = {
    "image": "IMAGE",
    "file_name": "STRING",
    "upscaled_folder_path": "STRING",
}


def _natural_component_key(component):
    parts = re.split(r"([0-9]+)", component.casefold())
    return tuple(
        (1, int(part), len(part)) if part.isdigit() else (0, part) for part in parts
    )


def _natural_relative_key(path, directory):
    relative = path.relative_to(directory)
    relative_text = relative.as_posix()
    return (
        tuple(_natural_component_key(part) for part in relative.parts),
        relative_text.casefold(),
        relative_text,
    )


def _list_images(directory):
    return sorted(
        (
            path
            for path in directory.rglob("*")
            if path.is_file() and path.suffix.casefold() in IMAGE_EXTENSIONS
        ),
        key=lambda path: _natural_relative_key(path, directory),
    )


def _load_image(path):
    import numpy as np
    import torch
    from PIL import Image, ImageOps

    try:
        with Image.open(path) as opened_image:
            opened_image.seek(0)
            oriented_image = ImageOps.exif_transpose(opened_image)
            rgb_image = oriented_image.convert("RGB")
            pixels = np.array(rgb_image, dtype=np.float32, copy=True)
            pixels /= 255.0
    except (OSError, ValueError, Image.DecompressionBombError) as exc:
        raise ValueError(f"Could not load image `{path}`: {exc}") from exc

    pixels = np.ascontiguousarray(pixels)
    return torch.from_numpy(pixels).unsqueeze(0)


def _upscaled_folder(image_path):
    folder_parts = list(image_path.parent.parts)
    original_indexes = [
        index for index, part in enumerate(folder_parts) if part == "original"
    ]
    if not original_indexes:
        raise ValueError(
            "directory_path must point to `original` or one of its "
            "subdirectories so it can be mapped to `upscaled`"
        )

    folder_parts[original_indexes[-1]] = "upscaled"
    return Path(*folder_parts)


def run(directory_path, counter):
    if not isinstance(directory_path, str) or not directory_path.strip():
        raise ValueError("directory_path cannot be empty")

    directory = Path(directory_path.strip()).expanduser().absolute()
    if not directory.exists():
        raise FileNotFoundError(f"Image directory does not exist: {directory}")
    if not directory.is_dir():
        raise NotADirectoryError(f"Image path is not a directory: {directory}")
    if "original" not in directory.parts:
        raise ValueError(
            "directory_path must point to `original` or one of its "
            "subdirectories so it can be mapped to `upscaled`"
        )

    image_paths = _list_images(directory)
    if not image_paths:
        raise ValueError(f"No supported image files found recursively in: {directory}")
    if not 1 <= counter <= len(image_paths):
        raise ValueError(
            f"counter {counter} is outside 1..{len(image_paths)} for {directory}"
        )

    selected_path = image_paths[counter - 1]
    return {
        "image": _load_image(selected_path),
        "file_name": selected_path.stem,
        "upscaled_folder_path": (
            f"{_upscaled_folder(selected_path).as_posix()}/"
        ),
    }
