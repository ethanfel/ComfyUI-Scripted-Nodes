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
}


def _natural_sort_key(path):
    folded_name = path.name.casefold()
    parts = re.split(r"([0-9]+)", folded_name)
    return (
        tuple(
            (1, int(part), len(part)) if part.isdigit() else (0, part)
            for part in parts
        ),
        folded_name,
        path.name,
    )


def _list_images(directory):
    return sorted(
        (
            path
            for path in directory.iterdir()
            if path.is_file() and path.suffix.casefold() in IMAGE_EXTENSIONS
        ),
        key=_natural_sort_key,
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
        raise ValueError(f"Could not load image `{path.name}`: {exc}") from exc

    pixels = np.ascontiguousarray(pixels)
    return torch.from_numpy(pixels).unsqueeze(0)


def run(directory_path, counter):
    if not isinstance(directory_path, str) or not directory_path.strip():
        raise ValueError("directory_path cannot be empty")

    directory = Path(directory_path).expanduser()
    if not directory.exists():
        raise FileNotFoundError(f"Image directory does not exist: {directory}")
    if not directory.is_dir():
        raise NotADirectoryError(f"Image path is not a directory: {directory}")

    image_paths = _list_images(directory)
    if not image_paths:
        raise ValueError(f"No supported image files found in: {directory}")
    if not 1 <= counter <= len(image_paths):
        raise ValueError(
            f"counter {counter} is outside 1..{len(image_paths)} for {directory}"
        )

    selected_path = image_paths[counter - 1]
    return {
        "image": _load_image(selected_path),
        "file_name": selected_path.stem,
    }
