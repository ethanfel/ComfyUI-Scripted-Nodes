from __future__ import annotations

import asyncio
import importlib.util
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


# pack_loader imports pack_http by name, so register it under the name it uses.
pack_http = _load("pack_http", "pack_http.py")
pack_loader = _load("pack_loader", "pack_loader.py")


# --------------------------------------------------------------------------- #
# Name and id validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    "value",
    ["ComfyUI-KJNodes", "comfy_mtb", "a", "pack.v2", "A-1_2.3"],
)
def test_valid_pack_names_are_returned(value):
    assert pack_loader.normalize_pack_name(value) == value


@pytest.mark.parametrize(
    "value",
    [
        "",
        "   ",
        ".",
        "..",
        ".hidden",
        "-leading",
        "with space",
        "with/slash",
        "with\\backslash",
        "with:colon",
        "x" * 65,
    ],
)
def test_unsafe_pack_names_are_rejected(value):
    with pytest.raises(pack_loader.PackNameError):
        pack_loader.normalize_pack_name(value)


def test_pack_name_must_be_a_string():
    with pytest.raises(pack_loader.PackNameError):
        pack_loader.normalize_pack_name(None)


@pytest.mark.parametrize(
    ("pack_id", "expected"),
    [
        ("enabled:Pack", ("enabled", "Pack")),
        ("disabled:Pack", ("disabled", "Pack")),
        ("cache:Pack", ("cache", "Pack")),
    ],
)
def test_pack_ids_parse_for_every_scope(pack_id, expected):
    assert pack_loader._parse_pack_id(pack_id) == expected


@pytest.mark.parametrize(
    "pack_id",
    ["Pack", "installed:Pack", ":Pack", "enabled:../escape", "enabled:"],
)
def test_malformed_pack_ids_are_rejected(pack_id):
    with pytest.raises(pack_loader.PackNameError):
        pack_loader._parse_pack_id(pack_id)


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


@pytest.fixture
def comfy_tree(tmp_path, monkeypatch):
    """A custom_nodes root with an enabled pack, a disabled pack and decoys."""

    root = tmp_path / "custom_nodes"
    (root / "GoodPack").mkdir(parents=True)
    (root / "GoodPack" / "__init__.py").write_text("NODE_CLASS_MAPPINGS = {}\n")
    (root / "GoodPack" / "web").mkdir()
    (root / "GoodPack" / "pyproject.toml").write_text("[project]\nname='good'\n")

    (root / ".disabled" / "OffPack").mkdir(parents=True)
    (root / ".disabled" / "OffPack" / "__init__.py").write_text("")
    (root / ".disabled" / "OffPack" / "requirements.txt").write_text("torch\n")

    (root / "__pycache__").mkdir()
    (root / "NotAPack").mkdir()  # no __init__.py
    (root / "NotAPack" / "readme.md").write_text("")
    (root / "loose.py").write_text("")

    folder_paths = ModuleType("folder_paths")
    folder_paths.get_folder_paths = lambda name: [str(root)]
    folder_paths.folder_names_and_paths = {}
    monkeypatch.setitem(sys.modules, "folder_paths", folder_paths)
    monkeypatch.delitem(sys.modules, "nodes", raising=False)
    return root


def test_discovery_finds_enabled_and_disabled_packs(comfy_tree):
    found = {candidate.pack_id: candidate for candidate in pack_loader.discover_packs()}
    assert set(found) == {"enabled:GoodPack", "disabled:OffPack"}

    good = found["enabled:GoodPack"]
    assert good.has_web_directory and good.has_pyproject
    assert not good.has_requirements
    assert not good.installed_at_boot

    off = found["disabled:OffPack"]
    assert off.scope == "disabled" and off.has_requirements


def test_discovery_skips_symlinked_directories(comfy_tree):
    link = comfy_tree / "LinkedPack"
    try:
        link.symlink_to(comfy_tree / "GoodPack", target_is_directory=True)
    except (OSError, NotImplementedError):  # pragma: no cover - platform dependent
        pytest.skip("symlinks are unavailable")
    ids = {candidate.pack_id for candidate in pack_loader.discover_packs()}
    assert "enabled:LinkedPack" not in ids


def test_find_pack_rejects_unknown_ids(comfy_tree):
    with pytest.raises(pack_loader.PackNotFoundError):
        pack_loader.find_pack("enabled:Missing")


def test_packs_loaded_at_boot_are_marked_installed(comfy_tree, monkeypatch):
    nodes = ModuleType("nodes")
    nodes.LOADED_MODULE_DIRS = {"GoodPack": str(comfy_tree / "GoodPack")}
    monkeypatch.setitem(sys.modules, "nodes", nodes)

    found = {c.pack_id: c for c in pack_loader.discover_packs()}
    assert found["enabled:GoodPack"].installed_at_boot
    assert not found["disabled:OffPack"].installed_at_boot


# --------------------------------------------------------------------------- #
# Route filtering
# --------------------------------------------------------------------------- #


class _Resource:
    def __init__(self, canonical: str):
        self.canonical = canonical


class _Router:
    def __init__(self, canonicals=()):
        self._resources = [_Resource(path) for path in canonicals]
        self._frozen = True
        self.unindexed: list[object] = []

    def unindex_resource(self, resource):
        self.unindexed.append(resource)


class _App:
    def __init__(self, canonicals=()):
        self.router = _Router(canonicals)
        self.added: list[object] = []

    def add_routes(self, routes):
        for route in routes:
            path = getattr(route, "path", None) or getattr(route, "prefix", "")
            self.router._resources.append(_Resource(path))
        self.added.append(routes)


def _routes(*pairs):
    from aiohttp import web

    table = web.RouteTableDef()
    for method, path in pairs:
        table.route(method, path)(lambda request: None)
    return table


def test_reserved_core_paths_are_refused():
    app = _App(["/prompt"])
    decisions = pack_http.filter_pack_routes(
        _routes(
            ("GET", "/mypack/config"),
            ("GET", "/userdata/steal"),
            ("POST", "/prompt"),
            ("GET", "/api/queue"),
        ),
        app=app,
        pack_name="mypack",
    )
    allowed = [d.path for d in decisions if d.allowed]
    refused = {d.path: d.reason for d in decisions if not d.allowed}

    assert allowed == ["/mypack/config"]
    assert "reserved" in refused["/userdata/steal"]
    assert "reserved" in refused["/prompt"]
    assert "reserved" in refused["/api/queue"]


def test_greedy_patterns_are_refused():
    decisions = pack_http.filter_pack_routes(
        _routes(("GET", "/mypack/{tail:.*}"), ("GET", "/mypack/{name}")),
        app=_App(),
        pack_name="mypack",
    )
    refused = [d for d in decisions if not d.allowed]
    assert len(refused) == 1
    assert refused[0].path == "/mypack/{tail:.*}"
    assert "wildcard" in refused[0].reason


def test_duplicate_and_existing_paths_are_refused():
    app = _App(["/taken"])
    decisions = pack_http.filter_pack_routes(
        _routes(("GET", "/taken"), ("GET", "/fine"), ("GET", "/fine")),
        app=app,
        pack_name="mypack",
    )
    refused = {d.path: d.reason for d in decisions if not d.allowed}
    assert "already served" in refused["/taken"]
    assert sum(1 for d in decisions if d.allowed) == 1


def test_route_count_is_capped(monkeypatch):
    monkeypatch.setattr(pack_http, "MAX_PACK_ROUTES", 2)
    decisions = pack_http.filter_pack_routes(
        _routes(*[("GET", f"/mypack/{index}") for index in range(5)]),
        app=_App(),
        pack_name="mypack",
    )
    assert sum(1 for d in decisions if d.allowed) == 2


def test_direct_resources_are_audited_and_hostile_ones_detached():
    app = _App(["/existing"])
    before = pack_http.snapshot_resources(app)
    app.router._resources.append(_Resource("/kjweb_async"))
    hostile = _Resource("/api/{tail:.*}")
    app.router._resources.append(hostile)

    kept, decisions = pack_http.audit_direct_resources(
        app, before, pack_name="mypack"
    )

    assert [resource.canonical for resource in kept] == ["/kjweb_async"]
    assert hostile not in app.router._resources
    assert hostile in app.router.unindexed
    assert {d.path: d.allowed for d in decisions} == {
        "/kjweb_async": True,
        "/api/{tail:.*}": False,
    }


def test_remove_resources_reports_how_many_were_detached():
    app = _App(["/a", "/b"])
    resources = list(app.router._resources)
    assert pack_http.remove_resources(app, resources) == 2
    assert app.router._resources == []
    assert pack_http.remove_resources(app, resources) == 0


def test_router_surgery_support_is_probed():
    assert pack_http.router_surgery_supported(_App())
    assert not pack_http.router_surgery_supported(SimpleNamespace(router=None))
    assert not pack_http.router_surgery_supported(
        SimpleNamespace(router=SimpleNamespace())
    )


def test_unfrozen_router_restores_the_previous_state():
    app = _App()
    with pack_http.unfrozen_router(app):
        assert app.router._frozen is False
    assert app.router._frozen is True

    with pytest.raises(RuntimeError):
        with pack_http.unfrozen_router(app):
            raise RuntimeError("boom")
    assert app.router._frozen is True


# --------------------------------------------------------------------------- #
# The load transaction, against a faithful stand-in for ComfyUI
# --------------------------------------------------------------------------- #


class _FakeNodes(ModuleType):
    """Reproduces the parts of nodes.load_custom_node the loader depends on.

    Including its two hazards: NODE_DISPLAY_NAME_MAPPINGS.update ignores the
    `ignore` set, and EXTENSION_WEB_DIRS keys come from the pack itself.
    """

    def __init__(self, behaviour):
        super().__init__("nodes")
        self.NODE_CLASS_MAPPINGS = {"CoreNode": object}
        self.NODE_DISPLAY_NAME_MAPPINGS = {"CoreNode": "Core Node"}
        self.EXTENSION_WEB_DIRS = {}
        self.LOADED_MODULE_DIRS = {}
        self._behaviour = behaviour

    async def load_custom_node(self, module_path, ignore=frozenset(), module_parent=""):
        return self._behaviour(self, module_path, ignore, module_parent)


@pytest.fixture
def fake_comfy(tmp_path, monkeypatch):
    root = tmp_path / "custom_nodes"
    pack = root / ".disabled" / "DemoPack"
    pack.mkdir(parents=True)
    (pack / "__init__.py").write_text("")
    (pack / "web").mkdir()

    folder_paths = ModuleType("folder_paths")
    folder_paths.get_folder_paths = lambda name: [str(root)]
    folder_paths.folder_names_and_paths = {"checkpoints": ([], set())}
    monkeypatch.setitem(sys.modules, "folder_paths", folder_paths)

    app = _App(["/existing"])
    prompt_server = SimpleNamespace(
        app=app,
        routes=None,
        prompt_queue=SimpleNamespace(get_tasks_remaining=lambda: 0),
        sent=[],
    )
    prompt_server.send_sync = lambda event, data: prompt_server.sent.append(
        (event, data)
    )
    server_module = ModuleType("server")
    server_module.PromptServer = SimpleNamespace(instance=prompt_server)
    monkeypatch.setitem(sys.modules, "server", server_module)

    from aiohttp import web

    prompt_server.routes = web.RouteTableDef()
    return SimpleNamespace(
        root=root, pack=pack, app=app, prompt_server=prompt_server, folder_paths=folder_paths
    )


def _install_nodes(monkeypatch, behaviour) -> _FakeNodes:
    nodes = _FakeNodes(behaviour)
    monkeypatch.setitem(sys.modules, "nodes", nodes)
    return nodes


def _well_behaved(nodes, module_path, ignore, module_parent):
    for name in ("DemoA", "DemoB"):
        if name not in ignore:
            nodes.NODE_CLASS_MAPPINGS[name] = type(name, (), {})
    nodes.NODE_DISPLAY_NAME_MAPPINGS.update({"DemoA": "Demo A", "DemoB": "Demo B"})
    nodes.LOADED_MODULE_DIRS["DemoPack"] = module_path
    nodes.EXTENSION_WEB_DIRS["DemoPack"] = str(Path(module_path) / "web")
    return True


def test_load_registers_nodes_and_unload_restores_everything(fake_comfy, monkeypatch):
    nodes = _install_nodes(monkeypatch, _well_behaved)
    registry = pack_loader.PackRegistry()

    record = asyncio.run(registry.load("disabled:DemoPack"))

    assert record.undo.class_ids == ["DemoA", "DemoB"]
    assert set(nodes.NODE_CLASS_MAPPINGS) == {"CoreNode", "DemoA", "DemoB"}
    assert nodes.EXTENSION_WEB_DIRS["DemoPack"].endswith("web")
    assert record.web_mount == "/extensions/DemoPack"
    assert fake_comfy.prompt_server.sent[0][0] == "scripted_nodes.packs_changed"

    asyncio.run(registry.unload("disabled:DemoPack"))

    assert set(nodes.NODE_CLASS_MAPPINGS) == {"CoreNode"}
    assert nodes.NODE_DISPLAY_NAME_MAPPINGS == {"CoreNode": "Core Node"}
    assert nodes.EXTENSION_WEB_DIRS == {}
    assert nodes.LOADED_MODULE_DIRS == {}
    assert registry.get("disabled:DemoPack") is None


def test_pack_cannot_retitle_nodes_it_does_not_own(fake_comfy, monkeypatch):
    def hostile(nodes, module_path, ignore, module_parent):
        nodes.NODE_CLASS_MAPPINGS["DemoA"] = type("DemoA", (), {})
        # Retitle a core node and name one it never registered.
        nodes.NODE_DISPLAY_NAME_MAPPINGS.update(
            {"DemoA": "Demo A", "CoreNode": "Totally Safe Node", "Ghost": "Ghost"}
        )
        return True

    nodes = _install_nodes(monkeypatch, hostile)
    registry = pack_loader.PackRegistry()
    record = asyncio.run(registry.load("disabled:DemoPack"))

    assert nodes.NODE_DISPLAY_NAME_MAPPINGS["CoreNode"] == "Core Node"
    assert "Ghost" not in nodes.NODE_DISPLAY_NAME_MAPPINGS
    assert set(record.collisions) == {"CoreNode", "Ghost"}


def test_pack_cannot_claim_another_packs_web_mount(fake_comfy, monkeypatch):
    def hostile(nodes, module_path, ignore, module_parent):
        nodes.NODE_CLASS_MAPPINGS["DemoA"] = type("DemoA", (), {})
        # This is what a malicious pyproject `project.name` would do.
        nodes.EXTENSION_WEB_DIRS["comfyui-kjnodes"] = str(Path(module_path) / "web")
        return True

    nodes = _install_nodes(monkeypatch, hostile)
    registry = pack_loader.PackRegistry()
    record = asyncio.run(registry.load("disabled:DemoPack"))

    assert "comfyui-kjnodes" not in nodes.EXTENSION_WEB_DIRS
    assert set(nodes.EXTENSION_WEB_DIRS) == {"DemoPack"}
    assert record.web_mount == "/extensions/DemoPack"


def test_load_refuses_when_the_name_is_already_registered(fake_comfy, monkeypatch):
    nodes = _install_nodes(monkeypatch, _well_behaved)
    nodes.LOADED_MODULE_DIRS["DemoPack"] = "/somewhere/else"
    registry = pack_loader.PackRegistry()

    with pytest.raises(pack_loader.PackConflictError):
        asyncio.run(registry.load("disabled:DemoPack"))


def test_failed_import_reports_the_missing_module_and_rolls_back(
    fake_comfy, monkeypatch
):
    import logging

    def failing(nodes, module_path, ignore, module_parent):
        nodes.LOADED_MODULE_DIRS["DemoPack"] = module_path
        logging.getLogger("nodes").warning(
            "Traceback...\nModuleNotFoundError: No module named 'segment_anything'"
        )
        return False

    nodes = _install_nodes(monkeypatch, failing)
    registry = pack_loader.PackRegistry()

    with pytest.raises(pack_loader.PackImportError) as excinfo:
        asyncio.run(registry.load("disabled:DemoPack"))

    assert excinfo.value.missing_module == "segment_anything"
    assert nodes.LOADED_MODULE_DIRS == {}
    assert registry.get("disabled:DemoPack") is None


def test_import_that_registers_nothing_is_an_error(fake_comfy, monkeypatch):
    _install_nodes(monkeypatch, lambda *args: True)
    registry = pack_loader.PackRegistry()

    with pytest.raises(pack_loader.PackImportError):
        asyncio.run(registry.load("disabled:DemoPack"))


def test_loading_twice_is_refused_and_reload_replaces_the_classes(
    fake_comfy, monkeypatch
):
    nodes = _install_nodes(monkeypatch, _well_behaved)
    registry = pack_loader.PackRegistry()

    asyncio.run(registry.load("disabled:DemoPack"))
    first = nodes.NODE_CLASS_MAPPINGS["DemoA"]

    with pytest.raises(pack_loader.PackConflictError):
        asyncio.run(registry.load("disabled:DemoPack"))

    asyncio.run(registry.reload("disabled:DemoPack"))
    # Reload unloads first, so `ignore` cannot mask the pack's own ids and the
    # registry ends up pointing at freshly created classes.
    assert nodes.NODE_CLASS_MAPPINGS["DemoA"] is not first


def test_load_is_refused_while_the_queue_is_busy(fake_comfy, monkeypatch):
    _install_nodes(monkeypatch, _well_behaved)
    fake_comfy.prompt_server.prompt_queue.get_tasks_remaining = lambda: 2
    registry = pack_loader.PackRegistry()

    with pytest.raises(pack_loader.PackBusyError):
        asyncio.run(registry.load("disabled:DemoPack"))


def test_installed_packs_are_not_loaded_again(fake_comfy, monkeypatch):
    nodes = _install_nodes(monkeypatch, _well_behaved)
    enabled = fake_comfy.root / "DemoPack"
    enabled.mkdir()
    (enabled / "__init__.py").write_text("")
    nodes.LOADED_MODULE_DIRS["DemoPack"] = str(enabled)

    registry = pack_loader.PackRegistry()
    with pytest.raises(pack_loader.PackConflictError):
        asyncio.run(registry.load("enabled:DemoPack"))


def test_folder_path_registrations_are_reverted(fake_comfy, monkeypatch):
    def registers_folder(nodes, module_path, ignore, module_parent):
        fake_comfy.folder_paths.folder_names_and_paths["demo_models"] = ([], set())
        return _well_behaved(nodes, module_path, ignore, module_parent)

    _install_nodes(monkeypatch, registers_folder)
    registry = pack_loader.PackRegistry()

    record = asyncio.run(registry.load("disabled:DemoPack"))
    assert record.undo.folder_path_keys_added == ["demo_models"]
    assert "demo_models" in fake_comfy.folder_paths.folder_names_and_paths

    asyncio.run(registry.unload("disabled:DemoPack"))
    assert "demo_models" not in fake_comfy.folder_paths.folder_names_and_paths


def test_sys_path_additions_are_reported_and_removed(fake_comfy, monkeypatch):
    added = str(fake_comfy.pack / "modules")

    def mutates_sys_path(nodes, module_path, ignore, module_parent):
        sys.path.insert(0, added)
        return _well_behaved(nodes, module_path, ignore, module_parent)

    _install_nodes(monkeypatch, mutates_sys_path)
    registry = pack_loader.PackRegistry()

    record = asyncio.run(registry.load("disabled:DemoPack"))
    assert added in record.undo.sys_path_added
    assert any("sys.path" in note for note in record.dirty)

    asyncio.run(registry.unload("disabled:DemoPack"))
    assert added not in sys.path


def test_catalog_reports_state_for_every_pack(fake_comfy, monkeypatch):
    _install_nodes(monkeypatch, _well_behaved)
    registry = pack_loader.PackRegistry()

    states = {entry["id"]: entry["state"] for entry in registry.catalog()}
    assert states == {"disabled:DemoPack": "available"}

    asyncio.run(registry.load("disabled:DemoPack"))
    entry = next(item for item in registry.catalog() if item["id"] == "disabled:DemoPack")
    assert entry["state"] == "loaded"
    assert entry["node_count"] == 2
