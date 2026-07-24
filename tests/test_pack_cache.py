from __future__ import annotations

import hashlib
import importlib.util
import json
import sys
from pathlib import Path
from types import ModuleType, SimpleNamespace

import pytest


ROOT = Path(__file__).resolve().parents[1]


def _load(name: str, filename: str) -> ModuleType:
    existing = sys.modules.get(name)
    if existing is not None:
        return existing
    spec = importlib.util.spec_from_file_location(name, ROOT / filename)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    spec.loader.exec_module(module)
    return module


_load("pack_http", "pack_http.py")
_load("pack_loader", "pack_loader.py")
_load("node_pack_tester", "node_pack_tester.py")
pack_cache = _load("pack_cache", "pack_cache.py")
tester = sys.modules["node_pack_tester"]


def _blob_id(data: bytes) -> str:
    digest = hashlib.sha1()
    digest.update(b"blob %d\0" % len(data))
    digest.update(data)
    return digest.hexdigest()


def _tree(*entries: tuple[str, str, str]) -> bytes:
    """Build `git ls-tree -r -z` output from (mode, type, path) triples."""

    chunks = []
    for mode, object_type, path in entries:
        object_id = _blob_id(path.encode())
        chunks.append(
            f"{mode} {object_type} {object_id}\t{path}".encode() + b"\0"
        )
    return b"".join(chunks)


# --------------------------------------------------------------------------- #
# Path safety
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "relative",
    ["a.py", "pkg/mod.py", "web/js/app.min.js", "a-b/c_d.txt"],
)
def test_ordinary_relative_paths_are_accepted(relative):
    assert pack_cache._safe_relative_path(relative)


@pytest.mark.parametrize(
    "relative",
    [
        "",
        "/absolute.py",
        "../escape.py",
        "a/../../etc/passwd",
        "a/./b.py",
        "a//b.py",
        "a\\b.py",
        "trailing./x.py",
        "trailing /x.py",
        "nul\x00.py",
    ],
)
def test_escaping_or_ambiguous_paths_are_rejected(relative):
    assert not pack_cache._safe_relative_path(relative)


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        ("github.com", "github.com"),
        ("owner", "owner"),
        ("..", "unknown"),
        ("", "unknown"),
        ("a/b", "a_b"),
        ("weird name!", "weird_name_"),
    ],
)
def test_url_components_are_reduced_to_safe_directory_names(value, expected):
    assert pack_cache._safe_component(value) == expected


# --------------------------------------------------------------------------- #
# Tree parsing
# --------------------------------------------------------------------------- #


def test_ordinary_files_are_collected():
    files, refused, submodules = pack_cache._parse_full_tree(
        _tree(
            ("100644", "blob", "__init__.py"),
            ("100755", "blob", "scripts/run.sh"),
            ("040000", "tree", "web"),
            ("100644", "blob", "web/app.js"),
        ),
        "",
    )
    assert [entry.path for entry in files] == [
        "__init__.py",
        "scripts/run.sh",
        "web/app.js",
    ]
    assert refused == []
    assert submodules is False


def test_symlinks_and_submodules_are_refused():
    files, refused, submodules = pack_cache._parse_full_tree(
        _tree(
            ("100644", "blob", "__init__.py"),
            ("120000", "blob", "evil_link"),
            ("160000", "commit", "vendor/dep"),
        ),
        "",
    )
    assert [entry.path for entry in files] == ["__init__.py"]
    assert "evil_link (symlink)" in refused
    assert "vendor/dep (submodule)" in refused
    assert submodules is True


def test_unsafe_paths_in_the_tree_are_refused():
    files, refused, _ = pack_cache._parse_full_tree(
        _tree(("100644", "blob", "ok.py"), ("100644", "blob", "../escape.py")),
        "",
    )
    assert [entry.path for entry in files] == ["ok.py"]
    assert refused == ["../escape.py"]


def test_subdirectory_selects_and_reroots_entries():
    files, _, _ = pack_cache._parse_full_tree(
        _tree(
            ("100644", "blob", "readme.md"),
            ("100644", "blob", "nodes/__init__.py"),
            ("100644", "blob", "nodes/web/app.js"),
        ),
        "nodes",
    )
    assert [entry.path for entry in files] == ["__init__.py", "web/app.js"]


def test_missing_subdirectory_is_an_error():
    with pytest.raises(tester.SubdirectoryValidationError):
        pack_cache._parse_full_tree(_tree(("100644", "blob", "a.py")), "nodes")


def test_file_count_is_capped(monkeypatch):
    monkeypatch.setattr(pack_cache, "MAX_PACK_FILES", 2)
    with pytest.raises(tester.NodePackTooLargeError):
        pack_cache._parse_full_tree(
            _tree(*[("100644", "blob", f"f{i}.py") for i in range(3)]), ""
        )


def test_unreadable_tree_entries_are_an_error():
    with pytest.raises(tester.NodePackScanError):
        pack_cache._parse_full_tree(b"not-a-tree-entry\0", "")


# --------------------------------------------------------------------------- #
# Blob writing
# --------------------------------------------------------------------------- #


def _entry(path: str, data: bytes, mode: str = "100644"):
    return pack_cache._TreeEntry(path=path, mode=mode, object_id=_blob_id(data))


def _batch(*items: tuple[str, bytes]) -> bytes:
    chunks = []
    for path, data in items:
        chunks.append(
            f"{_blob_id(data)} blob {len(data)}\n".encode() + data + b"\n"
        )
    return b"".join(chunks)


def test_blobs_are_written_with_their_modes(tmp_path):
    entries = [_entry("a.py", b"print(1)"), _entry("bin/run", b"#!/bin/sh", "100755")]
    written = pack_cache._write_batch(
        _batch(("a.py", b"print(1)"), ("bin/run", b"#!/bin/sh")),
        entries,
        tmp_path,
        0,
    )
    assert written == len(b"print(1)") + len(b"#!/bin/sh")
    assert (tmp_path / "a.py").read_bytes() == b"print(1)"
    assert (tmp_path / "bin" / "run").read_bytes() == b"#!/bin/sh"
    if sys.platform != "win32":
        assert (tmp_path / "bin" / "run").stat().st_mode & 0o111


def test_mismatched_blob_ids_are_rejected(tmp_path):
    with pytest.raises(tester.NodePackScanError):
        pack_cache._write_batch(
            _batch(("a.py", b"other")), [_entry("a.py", b"expected")], tmp_path, 0
        )


def test_truncated_blob_streams_are_rejected(tmp_path):
    truncated = _batch(("a.py", b"print(1)"))[:-5]
    with pytest.raises(tester.NodePackScanError):
        pack_cache._write_batch(
            truncated, [_entry("a.py", b"print(1)")], tmp_path, 0
        )


def test_per_file_and_total_size_caps_are_enforced(tmp_path, monkeypatch):
    monkeypatch.setattr(pack_cache, "MAX_PACK_FILE_BYTES", 4)
    with pytest.raises(tester.NodePackTooLargeError):
        pack_cache._write_batch(
            _batch(("a.py", b"too long")), [_entry("a.py", b"too long")], tmp_path, 0
        )

    monkeypatch.setattr(pack_cache, "MAX_PACK_FILE_BYTES", 1024)
    monkeypatch.setattr(pack_cache, "MAX_PACK_TREE_BYTES", 4)
    with pytest.raises(tester.NodePackTooLargeError):
        pack_cache._write_batch(
            _batch(("a.py", b"12345")), [_entry("a.py", b"12345")], tmp_path, 0
        )


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #


@pytest.fixture
def cached_tree(tmp_path):
    directory = tmp_path / "DemoPack"
    directory.mkdir()
    files = {"__init__.py": b"NODE_CLASS_MAPPINGS = {}\n", "web/app.js": b"//\n"}
    entries = []
    for relative, data in files.items():
        target = directory / relative
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(data)
        entries.append(_entry(relative, data))

    record = pack_cache.CachedPack(
        name="DemoPack",
        slug="owner/DemoPack",
        host="github.com",
        owner="owner",
        repository="DemoPack",
        commit="a" * 40,
        requested_ref="default branch",
        subdirectory="",
        path=directory,
        file_count=len(entries),
        total_bytes=sum(len(data) for data in files.values()),
        fetched_at=1234.0,
        has_submodules=False,
        refused_entries=(),
    )
    pack_cache._write_manifest(directory, record, entries)
    return directory


def test_manifest_round_trips(cached_tree):
    record = pack_cache.read_manifest(cached_tree)
    assert record.name == "DemoPack"
    assert record.commit == "a" * 40
    assert record.pack_id == "cache:DemoPack"
    assert record.path == cached_tree


def test_untouched_tree_verifies_clean(cached_tree):
    assert pack_cache.verify_pack(cached_tree) == ([], [])


def test_modified_file_is_reported_as_changed(cached_tree):
    (cached_tree / "__init__.py").write_bytes(b"import os\n")
    changed, added = pack_cache.verify_pack(cached_tree)
    assert changed == ["__init__.py: modified"]
    assert added == []


def test_deleted_file_is_reported_as_changed(cached_tree):
    (cached_tree / "web" / "app.js").unlink()
    changed, _ = pack_cache.verify_pack(cached_tree)
    assert changed == ["web/app.js: missing"]


def test_new_file_is_reported_separately_from_tampering(cached_tree):
    (cached_tree / "settings.json").write_text("{}")
    changed, added = pack_cache.verify_pack(cached_tree)
    assert changed == []
    assert added == ["settings.json"]


def test_bytecode_caches_are_ignored(cached_tree):
    (cached_tree / "__pycache__").mkdir()
    (cached_tree / "__pycache__" / "x.pyc").write_bytes(b"\x00")
    assert pack_cache.verify_pack(cached_tree) == ([], [])


def test_missing_or_malformed_manifests_are_errors(tmp_path):
    empty = tmp_path / "NoManifest"
    empty.mkdir()
    with pytest.raises(pack_cache.PackCacheVerificationError):
        pack_cache.read_manifest(empty)
    with pytest.raises(pack_cache.PackCacheVerificationError):
        pack_cache.verify_pack(empty)

    (empty / pack_cache.MANIFEST_NAME).write_text(json.dumps({"version": 99}))
    with pytest.raises(pack_cache.PackCacheVerificationError):
        pack_cache.read_manifest(empty)


def test_sha256_manifests_are_verified_with_sha256(cached_tree):
    payload = json.loads(
        (cached_tree / pack_cache.MANIFEST_NAME).read_text(encoding="utf-8")
    )
    data = (cached_tree / "__init__.py").read_bytes()
    digest = hashlib.sha256()
    digest.update(b"blob %d\0" % len(data))
    digest.update(data)
    for entry in payload["files"]:
        if entry["path"] == "__init__.py":
            entry["blob"] = digest.hexdigest()
    (cached_tree / pack_cache.MANIFEST_NAME).write_text(json.dumps(payload))

    changed, _ = pack_cache.verify_pack(cached_tree)
    assert changed == []


# --------------------------------------------------------------------------- #
# Cache root and listing
# --------------------------------------------------------------------------- #


def test_cache_root_requires_comfyui(monkeypatch):
    monkeypatch.delitem(sys.modules, "folder_paths", raising=False)
    monkeypatch.setattr(
        pack_cache, "cache_root", pack_cache.cache_root
    )  # ensure the real function
    import builtins

    real_import = builtins.__import__

    def blocked(name, *args, **kwargs):
        if name == "folder_paths":
            raise ImportError("no ComfyUI")
        return real_import(name, *args, **kwargs)

    monkeypatch.setattr(builtins, "__import__", blocked)
    with pytest.raises(pack_cache.PackCacheUnavailableError):
        pack_cache.cache_root()


def test_cache_root_is_created_under_the_user_directory(tmp_path, monkeypatch):
    user_directory = tmp_path / "user"
    user_directory.mkdir()
    folder_paths = ModuleType("folder_paths")
    folder_paths.get_user_directory = lambda: str(user_directory)
    monkeypatch.setitem(sys.modules, "folder_paths", folder_paths)

    root = pack_cache.cache_root()
    assert root == user_directory / pack_cache.CACHE_DIRECTORY_NAME
    assert root.is_dir()


def test_cache_root_rejects_a_comfyui_without_a_user_directory(monkeypatch):
    folder_paths = ModuleType("folder_paths")
    monkeypatch.setitem(sys.modules, "folder_paths", folder_paths)
    with pytest.raises(pack_cache.PackCacheUnavailableError):
        pack_cache.cache_root()


def test_listing_and_deleting_cached_packs(tmp_path, monkeypatch, cached_tree):
    root = tmp_path / "cache"
    destination = root / "github.com" / "owner" / "DemoPack" / ("a" * 40) / "DemoPack"
    destination.parent.mkdir(parents=True)
    cached_tree.rename(destination)
    monkeypatch.setattr(pack_cache, "cache_root", lambda: root)

    packs = pack_cache.list_cached_packs()
    assert [pack.name for pack in packs] == ["DemoPack"]
    assert pack_cache.find_cached_pack("DemoPack").commit == "a" * 40
    assert pack_cache.find_cached_pack("Missing") is None

    assert pack_cache.delete_cached_pack("DemoPack", "a" * 40)
    assert pack_cache.list_cached_packs() == []
    assert not pack_cache.delete_cached_pack("DemoPack", "a" * 40)


def test_listing_survives_an_unreadable_entry(tmp_path, monkeypatch):
    root = tmp_path / "cache"
    broken = root / "github.com" / "owner" / "Broken" / ("b" * 40) / "Broken"
    broken.mkdir(parents=True)
    (broken / pack_cache.MANIFEST_NAME).write_text("not json")
    monkeypatch.setattr(pack_cache, "cache_root", lambda: root)
    assert pack_cache.list_cached_packs() == []


# --------------------------------------------------------------------------- #
# Naming
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("slug", "subdirectory", "override", "expected"),
    [
        ("owner/ComfyUI-KJNodes", "", None, "ComfyUI-KJNodes"),
        ("owner/monorepo", "packs/MyPack", None, "MyPack"),
        ("owner/repo", "", "Explicit", "Explicit"),
    ],
)
def test_pack_names_come_from_the_repository_not_the_commit(
    slug, subdirectory, override, expected
):
    source = SimpleNamespace(slug=slug, url=f"https://github.com/{slug}")
    assert pack_cache._pack_name(source, subdirectory, override) == expected


def test_unsafe_pack_names_are_rejected():
    source = SimpleNamespace(slug="owner/..", url="https://github.com/owner/..")
    with pytest.raises(sys.modules["pack_loader"].PackNameError):
        pack_cache._pack_name(source, "", None)


def test_commit_directory_layout_keeps_the_repository_name_as_the_leaf(tmp_path):
    source = SimpleNamespace(
        slug="owner/repo", url="https://github.com/owner/repo.git"
    )
    directory = pack_cache._commit_directory(tmp_path, source, "c" * 40)
    assert directory == tmp_path / "github.com" / "owner" / "repo" / ("c" * 40)
