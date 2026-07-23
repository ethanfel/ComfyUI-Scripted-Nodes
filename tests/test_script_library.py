from __future__ import annotations

import asyncio
import importlib.util
import math
import os
from pathlib import Path
import stat
import sys
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]
MODULE_NAME = "_script_library_tests"


def _load_module():
    existing = sys.modules.get(MODULE_NAME)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(
        MODULE_NAME,
        ROOT / "script_library.py",
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[MODULE_NAME] = module
    spec.loader.exec_module(module)
    return module


library_module = _load_module()


VALID_CODE = """INPUTS = {"value": "INT"}
OUTPUTS = {"result": "INT"}
def run(value):
    return value
"""


@pytest.fixture
def collection(tmp_path):
    bundled = tmp_path / "bundled"
    user = tmp_path / "models" / "scripted_nodes"
    (bundled / "text").mkdir(parents=True)
    (bundled / "text" / "bundled.py").write_text(
        VALID_CODE,
        encoding="utf-8",
    )
    return library_module.ScriptLibrary(
        bundled_root=bundled,
        user_root=user,
    )


def test_collection_lists_and_loads_bundled_and_user_scripts(collection):
    saved = collection.save("personal/example", VALID_CODE)

    records = collection.list_scripts()

    assert [record.id for record in records] == [
        "user:personal/example.py",
        "bundled:text/bundled.py",
    ]
    assert saved.to_dict() == {
        "id": "user:personal/example.py",
        "name": "personal/example.py",
        "source": "user",
        "deletable": True,
    }
    bundled_record, bundled_code = collection.load(
        "bundled:text/bundled.py"
    )
    user_record, user_code = collection.load("user:personal/example.py")
    assert bundled_record.deletable is False
    assert bundled_code == VALID_CODE
    assert user_record.deletable is True
    assert user_code == VALID_CODE


def test_save_adds_py_and_requires_explicit_overwrite(
    collection,
):
    first = collection.save("nested/demo", "first")

    assert first.name == "nested/demo.py"
    with pytest.raises(
        library_module.ScriptConflictError,
        match="already exists",
    ):
        collection.save("nested/demo.py", "second")

    collection.save("nested/demo.py", "second", overwrite=True)
    _, code = collection.load("user:nested/demo.py")

    assert code == "second"
    user_root = collection.user_root
    assert not list(user_root.rglob(".scripted-node-*.tmp"))


def test_first_save_publishes_a_completed_temporary_file(
    collection,
    monkeypatch,
):
    publications = []

    def publish(source, target):
        assert source.read_text(encoding="utf-8") == VALID_CODE
        assert not target.exists()
        os.rename(source, target)
        publications.append(target)
        return True

    monkeypatch.setattr(
        library_module,
        "_try_rename_noreplace",
        publish,
    )
    monkeypatch.setattr(
        library_module,
        "_try_hardlink_publish",
        lambda source, target: pytest.fail("hard-link fallback was used"),
    )

    collection.save("published", VALID_CODE)

    assert publications == [collection.user_root / "published.py"]
    assert collection.load("user:published.py")[1] == VALID_CODE


def test_overwrite_preserves_existing_file_permissions(collection):
    if os.name == "nt":
        pytest.skip("POSIX file modes are not available on Windows")

    collection.save("private", "first")
    path = collection.user_root / "private.py"
    path.chmod(0o600)

    collection.save("private", "second", overwrite=True)

    assert stat.S_IMODE(path.stat().st_mode) == 0o600
    assert path.read_text(encoding="utf-8") == "second"


@pytest.mark.parametrize(
    "name",
    [
        "",
        "   ",
        "/absolute",
        "../escape",
        "folder/../escape",
        "folder/./script",
        "folder//script",
        r"folder\script",
        ".hidden",
        "folder/.hidden",
        "bad.txt",
        "prefix:name",
        "control\x00name",
        "surrogate\ud800name",
    ],
)
def test_unsafe_names_are_rejected_without_writing(collection, name):
    with pytest.raises(library_module.ScriptNameError):
        collection.save(name, VALID_CODE)

    assert collection.list_scripts() == [
        library_module.ScriptRecord(
            id="bundled:text/bundled.py",
            name="text/bundled.py",
            source="bundled",
            deletable=False,
        )
    ]


def test_invalid_unicode_script_source_is_a_library_error(collection):
    with pytest.raises(
        library_module.ScriptLibraryError,
        match="valid Unicode",
    ):
        collection.save("invalid_unicode", "bad surrogate: \ud800")


def test_symbolic_links_are_never_loaded_or_overwritten(
    collection,
    tmp_path,
):
    collection.list_scripts()
    outside_file = tmp_path / "outside.py"
    outside_file.write_text("outside", encoding="utf-8")
    linked_file = collection.user_root / "linked.py"
    try:
        linked_file.symlink_to(outside_file)
    except (NotImplementedError, OSError):
        pytest.skip("Symbolic links are unavailable on this platform")

    with pytest.raises(library_module.ScriptStorageError, match="Symbolic"):
        collection.load("user:linked.py")
    with pytest.raises(library_module.ScriptStorageError, match="Symbolic"):
        collection.save("linked.py", "replacement", overwrite=True)
    assert outside_file.read_text(encoding="utf-8") == "outside"

    outside_directory = tmp_path / "outside-directory"
    outside_directory.mkdir()
    linked_directory = collection.user_root / "linked-directory"
    linked_directory.symlink_to(outside_directory, target_is_directory=True)
    with pytest.raises(library_module.ScriptStorageError, match="symbolic"):
        collection.save("linked-directory/escape", VALID_CODE)
    assert list(outside_directory.iterdir()) == []


def test_delete_is_limited_to_user_scripts_and_cleans_empty_folders(
    collection,
):
    collection.save("nested/remove_me", VALID_CODE)

    with pytest.raises(
        library_module.ProtectedScriptError,
        match="read-only",
    ):
        collection.delete("bundled:text/bundled.py")

    deleted = collection.delete("user:nested/remove_me.py")
    assert deleted.id == "user:nested/remove_me.py"
    assert not (collection.user_root / "nested").exists()
    with pytest.raises(library_module.ScriptNotFoundError):
        collection.load("user:nested/remove_me.py")


def test_default_storage_is_models_scripted_nodes(tmp_path, monkeypatch):
    models_dir = tmp_path / "models"
    models_dir.mkdir()
    fake_folder_paths = SimpleNamespace(models_dir=str(models_dir))
    monkeypatch.setitem(sys.modules, "folder_paths", fake_folder_paths)

    assert library_module._default_user_scripts_root() == (
        models_dir / "scripted_nodes"
    )


def test_browser_and_save_nodes_use_the_managed_collection(
    collection,
    monkeypatch,
):
    monkeypatch.setattr(library_module, "_DEFAULT_LIBRARY", collection)
    collection.save("loaded", VALID_CODE)

    choices = library_module.ComfyScriptBrowserNode.INPUT_TYPES()["required"][
        "script_name"
    ][0]
    assert choices == [
        "user:loaded.py",
        "bundled:text/bundled.py",
    ]
    assert library_module.ComfyScriptBrowserNode().load_script(
        "user:loaded.py"
    ) == (VALID_CODE,)

    output = library_module.ComfySaveScriptNode().save_script(
        "saved/from_node",
        "node code",
    )
    assert output == ("node code", "saved/from_node.py")
    assert library_module.ComfySaveScriptNode.OUTPUT_NODE is True
    assert collection.load("user:saved/from_node.py")[1] == "node code"


def test_browser_hash_tracks_source_and_save_node_is_never_cached(
    collection,
    monkeypatch,
):
    monkeypatch.setattr(library_module, "_DEFAULT_LIBRARY", collection)
    collection.save("changing", "first")

    first_hash = library_module.ComfyScriptBrowserNode.IS_CHANGED(
        "user:changing.py"
    )
    collection.save("changing", "second", overwrite=True)
    second_hash = library_module.ComfyScriptBrowserNode.IS_CHANGED(
        "user:changing.py"
    )

    assert first_hash != second_hash
    assert math.isnan(library_module.ComfySaveScriptNode.IS_CHANGED())


def test_script_size_and_non_utf8_files_are_rejected(collection):
    with pytest.raises(library_module.ScriptLibraryError, match="exceed"):
        collection.save(
            "too_large",
            "x" * (library_module.MAX_SCRIPT_BYTES + 1),
        )

    collection.list_scripts()
    invalid = collection.user_root / "invalid.py"
    invalid.write_bytes(b"\xff\xfe")
    with pytest.raises(library_module.ScriptStorageError, match="UTF-8"):
        collection.load("user:invalid.py")


class _CapturedRoutes:
    def __init__(self):
        self.handlers = {}

    def _register(self, method, path):
        def decorator(handler):
            self.handlers[(method, path)] = handler
            return handler

        return decorator

    def get(self, path):
        return self._register("GET", path)

    def post(self, path):
        return self._register("POST", path)

    def delete(self, path):
        return self._register("DELETE", path)


class _Request:
    def __init__(self, payload=None):
        self.payload = payload

    async def json(self):
        return self.payload


def test_rest_endpoints_list_load_save_and_delete(
    collection,
    monkeypatch,
):
    routes = _CapturedRoutes()
    fake_server = ModuleType("server")
    fake_server.PromptServer = SimpleNamespace(
        instance=SimpleNamespace(routes=routes)
    )

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

    route_module_name = "_script_library_route_tests"
    spec = importlib.util.spec_from_file_location(
        route_module_name,
        ROOT / "script_library.py",
    )
    assert spec is not None and spec.loader is not None
    route_module = importlib.util.module_from_spec(spec)
    monkeypatch.setitem(sys.modules, route_module_name, route_module)
    spec.loader.exec_module(route_module)
    route_module._DEFAULT_LIBRARY = route_module.ScriptLibrary(
        bundled_root=collection.bundled_root,
        user_root=collection.user_root,
    )

    assert route_module.SCRIPT_LIBRARY_ROUTES_REGISTERED is True
    save = routes.handlers[("POST", "/scripted_nodes/scripts")]
    response = asyncio.run(
        save(
            _Request(
                {
                    "name": "api/saved",
                    "code": VALID_CODE,
                    "overwrite": False,
                }
            )
        )
    )
    assert response.status == 201
    script_id = response.payload["script"]["id"]
    assert script_id == "user:api/saved.py"

    conflict = asyncio.run(
        save(
            _Request(
                {
                    "name": "api/saved",
                    "code": "replacement",
                    "overwrite": False,
                }
            )
        )
    )
    assert conflict.status == 409
    assert "already exists" in conflict.payload["error"]

    malformed = asyncio.run(save(_Request(None)))
    assert malformed.status == 400

    list_scripts = routes.handlers[("GET", "/scripted_nodes/scripts")]
    response = asyncio.run(list_scripts(_Request()))
    assert response.status == 200
    assert script_id in {
        record["id"] for record in response.payload["scripts"]
    }

    load = routes.handlers[("POST", "/scripted_nodes/scripts/load")]
    response = asyncio.run(load(_Request({"id": script_id})))
    assert response.status == 200
    assert response.payload["code"] == VALID_CODE
    assert response.payload["schema"]["outputs"] == [
        {"name": "result", "type": "INT"}
    ]
    assert response.payload["schema_json"]

    missing = asyncio.run(
        load(_Request({"id": "user:api/missing.py"}))
    )
    assert missing.status == 404

    invalid_save = asyncio.run(
        save(
            _Request(
                {
                    "name": "api/draft",
                    "code": "not valid Python (",
                    "overwrite": False,
                }
            )
        )
    )
    invalid_id = invalid_save.payload["script"]["id"]
    invalid_load = asyncio.run(load(_Request({"id": invalid_id})))
    assert invalid_load.status == 200
    assert invalid_load.payload["schema"] is None
    assert invalid_load.payload["schema_error"]

    delete = routes.handlers[("DELETE", "/scripted_nodes/scripts")]
    response = asyncio.run(delete(_Request({"id": script_id})))
    assert response.status == 200
    assert response.payload["deleted"]["id"] == script_id

    protected = asyncio.run(
        delete(_Request({"id": "bundled:text/bundled.py"}))
    )
    assert protected.status == 403

    missing_delete = asyncio.run(
        delete(_Request({"id": "user:api/missing.py"}))
    )
    assert missing_delete.status == 404

    draft_delete = asyncio.run(delete(_Request({"id": invalid_id})))
    assert draft_delete.status == 200
