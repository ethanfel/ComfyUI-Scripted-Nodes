import math

import comfy.utils


RECOMMENDED_SIZES = (
    (1024, 1024),
    (896, 1152),
    (832, 1216),
    (1152, 896),
    (1216, 832),
)

INPUTS = {
    "image": "IMAGE",
}

OUTPUTS = {
    "resized_image": "IMAGE",
    "target_width": "INT",
    "target_height": "INT",
}


def _closest_size(width, height):
    source_aspect = width / height
    return min(
        RECOMMENDED_SIZES,
        key=lambda size: (
            abs(math.log(source_aspect / (size[0] / size[1]))),
            -(size[0] * size[1]),
        ),
    )


def run(image):
    shape = getattr(image, "shape", None)
    if shape is None or len(shape) != 4:
        raise ValueError(
            "image must be a ComfyUI IMAGE tensor with shape "
            "[batch, height, width, channels]"
        )

    batch, height, width, channels = (int(dimension) for dimension in shape)
    if batch < 1 or height < 1 or width < 1 or channels < 1:
        raise ValueError("image dimensions must all be greater than zero")

    target_width, target_height = _closest_size(width, height)

    if (width, height) == (target_width, target_height):
        resized_image = image
    else:
        cover_scale = max(target_width / width, target_height / height)
        upscale_method = "area" if cover_scale <= 1.0 else "bicubic"
        samples = image.movedim(-1, 1)
        resized_image = comfy.utils.common_upscale(
            samples,
            target_width,
            target_height,
            upscale_method,
            "center",
        ).movedim(1, -1)

    return {
        "resized_image": resized_image,
        "target_width": target_width,
        "target_height": target_height,
    }
