from __future__ import annotations

import importlib.util
from pathlib import Path
import sys

import pytest


ROOT = Path(__file__).resolve().parents[1]
BACKEND_MODULE_NAME = "_scripted_node_collection_tests"
LINE_FROM_FILE = ROOT / "scripts" / "text" / "line_from_file.py"
LOAD_TEXT_FILE = ROOT / "scripts" / "text" / "load_text_file.py"
LOAD_IMAGE_FROM_FOLDER = ROOT / "scripts" / "image" / "load_image_from_folder.py"
RESIZE_TO_RECOMMENDED_SIZE = (
    ROOT / "scripts" / "image" / "resize_to_recommended_size.py"
)


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
LINE_CODE = LINE_FROM_FILE.read_text(encoding="utf-8")
LOAD_CODE = LOAD_TEXT_FILE.read_text(encoding="utf-8")
FOLDER_IMAGE_CODE = LOAD_IMAGE_FROM_FOLDER.read_text(encoding="utf-8")
RESIZE_CODE = RESIZE_TO_RECOMMENDED_SIZE.read_text(encoding="utf-8")


def _run(file_path: Path | str, line_number: int) -> str:
    schema = backend.parse_script_schema(LINE_CODE)
    result = backend.ComfyScriptedNode().execute(
        code=LINE_CODE,
        schema_json=backend.schema_to_json(schema),
        file_path=str(file_path),
        line_number=line_number,
    )
    return result[0]


def test_line_from_file_declares_expected_schema():
    schema = backend.parse_script_schema(LINE_CODE)

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


def _load_file(file_path: Path | str) -> str:
    schema = backend.parse_script_schema(LOAD_CODE)
    result = backend.ComfyScriptedNode().execute(
        code=LOAD_CODE,
        schema_json=backend.schema_to_json(schema),
        file_path=str(file_path),
    )
    return result[0]


def test_load_text_file_declares_expected_schema():
    schema = backend.parse_script_schema(LOAD_CODE)

    assert schema.inputs == [
        {"name": "file_path", "type": "STRING", "options": {}},
    ]
    assert schema.outputs == [
        {"name": "content", "type": "STRING"},
    ]


def test_load_text_file_returns_complete_content_with_line_numbers(tmp_path):
    text_file = tmp_path / "document.txt"
    text_file.write_bytes(
        b"\xef\xbb\xbfalpha\r\ncaf\xc3\xa9\r\n\r\nomega\r\n"
    )

    assert _load_file(text_file) == "1: alpha\n2: café\n3: \n4: omega"


def test_load_text_file_returns_an_empty_string_for_an_empty_file(tmp_path):
    text_file = tmp_path / "empty.txt"
    text_file.write_bytes(b"")

    assert _load_file(text_file) == ""


def test_load_text_file_rejects_an_empty_path():
    with pytest.raises(
        backend.ScriptExecutionError,
        match="file_path cannot be empty",
    ):
        _load_file("   ")


def test_load_text_file_reports_a_missing_file(tmp_path):
    missing = tmp_path / "missing.txt"

    with pytest.raises(
        backend.ScriptExecutionError,
        match="FileNotFoundError",
    ):
        _load_file(missing)


class _FakeImage:
    def __init__(self, shape):
        self.shape = tuple(shape)

    def movedim(self, source, destination):
        dimensions = list(self.shape)
        source %= len(dimensions)
        destination %= len(dimensions)
        dimension = dimensions.pop(source)
        dimensions.insert(destination, dimension)
        return _FakeImage(dimensions)


def _run_resize(monkeypatch, shape):
    import types

    calls = []
    comfy_module = types.ModuleType("comfy")
    utils_module = types.ModuleType("comfy.utils")

    def common_upscale(samples, width, height, method, crop):
        calls.append(
            {
                "shape": samples.shape,
                "width": width,
                "height": height,
                "method": method,
                "crop": crop,
            }
        )
        return _FakeImage((samples.shape[0], samples.shape[1], height, width))

    utils_module.common_upscale = common_upscale
    comfy_module.utils = utils_module
    monkeypatch.setitem(sys.modules, "comfy", comfy_module)
    monkeypatch.setitem(sys.modules, "comfy.utils", utils_module)

    schema = backend.parse_script_schema(RESIZE_CODE)
    image = _FakeImage(shape)
    result = backend.ComfyScriptedNode().execute(
        code=RESIZE_CODE,
        schema_json=backend.schema_to_json(schema),
        image=image,
    )
    return image, result, calls


def test_resize_to_recommended_size_declares_expected_schema():
    schema = backend.parse_script_schema(RESIZE_CODE)

    assert schema.inputs == [
        {"name": "image", "type": "IMAGE", "options": {}},
    ]
    assert schema.outputs == [
        {"name": "resized_image", "type": "IMAGE"},
        {"name": "target_width", "type": "INT"},
        {"name": "target_height", "type": "INT"},
    ]


@pytest.mark.parametrize(
    ("height", "width", "expected"),
    [
        (1800, 1800, (1024, 1024)),
        (1600, 1200, (896, 1152)),
        (1800, 1000, (832, 1216)),
        (1200, 1600, (1152, 896)),
        (1000, 1800, (1216, 832)),
    ],
)
def test_resize_to_recommended_size_selects_closest_aspect_ratio(
    monkeypatch,
    height,
    width,
    expected,
):
    _, result, calls = _run_resize(monkeypatch, (2, height, width, 3))

    target_width, target_height = expected
    assert result[0].shape == (2, target_height, target_width, 3)
    assert result[1:3] == expected
    assert calls == [
        {
            "shape": (2, 3, height, width),
            "width": target_width,
            "height": target_height,
            "method": "area",
            "crop": "center",
        }
    ]


def test_resize_to_recommended_size_uses_bicubic_for_small_inputs(monkeypatch):
    _, result, calls = _run_resize(monkeypatch, (1, 512, 512, 4))

    assert result[0].shape == (1, 1024, 1024, 4)
    assert result[1:3] == (1024, 1024)
    assert calls[0]["method"] == "bicubic"


def test_resize_to_recommended_size_uses_cover_scale_for_mixed_dimensions(
    monkeypatch,
):
    _, result, calls = _run_resize(monkeypatch, (1, 1400, 700, 3))

    assert result[0].shape == (1, 1216, 832, 3)
    assert result[1:3] == (832, 1216)
    assert calls[0]["method"] == "bicubic"


@pytest.mark.parametrize(
    ("aspect", "offset", "expected"),
    [
        ((832 / 1216 * 896 / 1152) ** 0.5, -2, (832, 1216)),
        ((832 / 1216 * 896 / 1152) ** 0.5, 2, (896, 1152)),
        ((896 / 1152) ** 0.5, -2, (896, 1152)),
        ((896 / 1152) ** 0.5, 2, (1024, 1024)),
        ((1152 / 896) ** 0.5, -2, (1024, 1024)),
        ((1152 / 896) ** 0.5, 2, (1152, 896)),
        ((1152 / 896 * 1216 / 832) ** 0.5, -2, (1152, 896)),
        ((1152 / 896 * 1216 / 832) ** 0.5, 2, (1216, 832)),
    ],
)
def test_resize_to_recommended_size_has_stable_aspect_boundaries(
    monkeypatch,
    aspect,
    offset,
    expected,
):
    height = 1_000_000
    width = round(aspect * height) + offset

    _, result, _ = _run_resize(monkeypatch, (1, height, width, 3))

    assert result[1:3] == expected


def test_resize_to_recommended_size_preserves_an_exact_target(monkeypatch):
    image, result, calls = _run_resize(monkeypatch, (3, 1152, 896, 3))

    assert result[0] is image
    assert result[1:3] == (896, 1152)
    assert calls == []


@pytest.mark.parametrize(
    "shape",
    [
        (1024, 1024, 3),
        (0, 1024, 1024, 3),
        (1, 0, 1024, 3),
        (1, 1024, 0, 3),
        (1, 1024, 1024, 0),
    ],
)
def test_resize_to_recommended_size_rejects_invalid_images(monkeypatch, shape):
    with pytest.raises(
        backend.ScriptExecutionError,
        match="ComfyUI IMAGE tensor|dimensions must all be greater than zero",
    ):
        _run_resize(monkeypatch, shape)


def _require_image_dependencies():
    pytest.importorskip("numpy")
    pytest.importorskip("torch")
    return pytest.importorskip("PIL.Image")


def _write_test_image(path, color=(255, 0, 0), size=(3, 2), **save_options):
    image_module = _require_image_dependencies()
    image = image_module.new("RGB", size, color)
    image.save(path, **save_options)


def _load_folder_image(directory_path, counter):
    schema = backend.parse_script_schema(FOLDER_IMAGE_CODE)
    result = backend.ComfyScriptedNode().execute(
        code=FOLDER_IMAGE_CODE,
        schema_json=backend.schema_to_json(schema),
        directory_path=str(directory_path),
        counter=counter,
    )
    return result[0], result[1]


def test_load_image_from_folder_declares_expected_schema():
    schema = backend.parse_script_schema(FOLDER_IMAGE_CODE)

    assert schema.inputs == [
        {"name": "directory_path", "type": "STRING", "options": {}},
        {
            "name": "counter",
            "type": "INT",
            "options": {"default": 1, "min": 1, "step": 1},
        },
    ]
    assert schema.outputs == [
        {"name": "image", "type": "IMAGE"},
        {"name": "file_name", "type": "STRING"},
    ]


def test_load_image_from_folder_uses_natural_order_and_omits_extension(tmp_path):
    folder = tmp_path / "images"
    folder.mkdir()
    _write_test_image(folder / "image10.png", (0, 0, 255))
    _write_test_image(folder / "image2.PNG", (0, 255, 0), format="PNG")
    _write_test_image(folder / "image01.png", (255, 255, 0))
    _write_test_image(folder / "image1.png", (255, 0, 0))
    (folder / "notes.txt").write_text("not an image", encoding="utf-8")
    nested = folder / "nested"
    nested.mkdir()
    _write_test_image(nested / "image0.png")

    image, file_name = _load_folder_image(f"{folder}/", 3)

    assert tuple(image.shape) == (1, 2, 3, 3)
    assert image[0, 0, 0].tolist() == pytest.approx([0.0, 1.0, 0.0])
    assert file_name == "image2"


def test_load_image_from_folder_applies_exif_orientation(tmp_path):
    image_module = _require_image_dependencies()
    folder = tmp_path / "images"
    folder.mkdir()
    image = image_module.new("RGB", (2, 3), (30, 60, 90))
    exif = image_module.Exif()
    exif[274] = 6
    image.save(folder / "rotated.jpg", exif=exif)

    loaded, file_name = _load_folder_image(folder, 1)

    assert tuple(loaded.shape) == (1, 2, 3, 3)
    assert file_name == "rotated"


def test_load_image_from_folder_uses_first_animated_frame(tmp_path):
    image_module = _require_image_dependencies()
    folder = tmp_path / "images"
    folder.mkdir()
    first = image_module.new("RGB", (2, 2), (255, 0, 0))
    second = image_module.new("RGB", (2, 2), (0, 0, 255))
    first.save(
        folder / "animated.gif",
        save_all=True,
        append_images=[second],
        duration=100,
        loop=0,
    )

    loaded, file_name = _load_folder_image(folder, 1)

    assert tuple(loaded.shape) == (1, 2, 2, 3)
    assert loaded[0, 0, 0].tolist() == pytest.approx([1.0, 0.0, 0.0])
    assert file_name == "animated"


def test_load_image_from_folder_converts_alpha_image_to_rgb(tmp_path):
    image_module = _require_image_dependencies()
    folder = tmp_path / "images"
    folder.mkdir()
    image_module.new("RGBA", (2, 1), (64, 128, 255, 0)).save(
        folder / "alpha.png"
    )

    loaded, file_name = _load_folder_image(folder, 1)

    assert tuple(loaded.shape) == (1, 1, 2, 3)
    assert loaded[0, 0, 0].tolist() == pytest.approx(
        [64 / 255, 128 / 255, 1.0]
    )
    assert file_name == "alpha"


@pytest.mark.parametrize("counter", [0, -1, 3])
def test_load_image_from_folder_rejects_out_of_range_counter(
    tmp_path,
    counter,
):
    folder = tmp_path / "images"
    folder.mkdir()
    (folder / "first.png").write_bytes(b"not opened")
    (folder / "second.png").write_bytes(b"not opened")

    with pytest.raises(
        backend.ScriptExecutionError,
        match=rf"counter {counter} is outside 1\.\.2",
    ):
        _load_folder_image(folder, counter)


def test_load_image_from_folder_rejects_an_empty_path():
    with pytest.raises(
        backend.ScriptExecutionError,
        match="directory_path cannot be empty",
    ):
        _load_folder_image("   ", 1)


def test_load_image_from_folder_reports_missing_and_non_directory_paths(
    tmp_path,
):
    missing = tmp_path / "missing"
    regular_file = tmp_path / "file.txt"
    regular_file.write_text("content", encoding="utf-8")

    with pytest.raises(
        backend.ScriptExecutionError,
        match="Image directory does not exist",
    ):
        _load_folder_image(missing, 1)
    with pytest.raises(
        backend.ScriptExecutionError,
        match="Image path is not a directory",
    ):
        _load_folder_image(regular_file, 1)


def test_load_image_from_folder_rejects_directory_without_images(tmp_path):
    folder = tmp_path / "empty"
    folder.mkdir()
    (folder / "readme.txt").write_text("no images", encoding="utf-8")

    with pytest.raises(
        backend.ScriptExecutionError,
        match="No supported image files found",
    ):
        _load_folder_image(folder, 1)


def test_load_image_from_folder_reports_a_corrupt_image(tmp_path):
    _require_image_dependencies()
    folder = tmp_path / "images"
    folder.mkdir()
    (folder / "broken.png").write_bytes(b"not a png")

    with pytest.raises(
        backend.ScriptExecutionError,
        match=r"Could not load image `broken\.png`",
    ):
        _load_folder_image(folder, 1)
