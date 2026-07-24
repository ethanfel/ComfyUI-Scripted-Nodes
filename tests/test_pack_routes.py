from __future__ import annotations

import asyncio
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
pack_loader = _load("pack_loader", "pack_loader.py")
_load("node_pack_tester", "node_pack_tester.py")
pack_cache = _load("pack_cache", "pack_cache.py")
pack_routes = _load("pack_routes", "pack_routes.py")


class _CapturedRoutes:
    """Stands in for PromptServer.routes and records what was registered."""

    def __init__(self):
        self.handlers: dict[tuple[str, str], object] = {}

    def _record(self, method):
        def outer(path):
            def inner(handler):
                self.handlers[(method, path)] = handler
                return handler

            return inner

        return outer

    def __getattr__(self, name):
        if name in {"get", "post", "delete", "put", "patch"}:
            return self._record(name.upper())
        raise AttributeError(name)


class _Request:
    def __init__(self, payload=None, *, malformed=False):
        self._payload = payload
        self._malformed = malformed

    async def json(self):
        if self._malformed:
            raise ValueError("not json")
        return self._payload


def _body(response):
    return json.loads(response.body.decode("utf-8"))


@pytest.fixture
def routes(monkeypatch):
    """Register the pack routes against a fake, non-running PromptServer."""

    captured = _CapturedRoutes()
    prompt_server = SimpleNamespace(routes=captured)
    server_module = ModuleType("server")
    server_module.PromptServer = SimpleNamespace(instance=prompt_server)
    monkeypatch.setitem(sys.modules, "server", server_module)

    assert pack_routes._register_pack_routes() is True
    return captured


def _call(routes, method, path, payload=None, **kwargs):
    handler = routes.handlers[(method, path)]
    return asyncio.run(handler(_Request(payload, **kwargs)))


# --------------------------------------------------------------------------- #
# Registration
# --------------------------------------------------------------------------- #


def test_routes_are_not_registered_without_a_running_server(monkeypatch):
    monkeypatch.delitem(sys.modules, "server", raising=False)
    assert pack_routes._register_pack_routes() is False


def test_every_documented_endpoint_is_registered(routes):
    prefix = pack_routes.PACK_ROUTE_PREFIX
    assert set(routes.handlers) == {
        ("GET", prefix),
        ("POST", f"{prefix}/fetch"),
        ("POST", f"{prefix}/load"),
        ("POST", f"{prefix}/unload"),
        ("POST", f"{prefix}/reload"),
        ("POST", f"{prefix}/verify"),
        ("DELETE", f"{prefix}/cache"),
    }


# --------------------------------------------------------------------------- #
# Error mapping
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("error", "status"),
    [
        (pack_loader.PackNotFoundError("x"), 404),
        (pack_loader.PackConflictError("x"), 409),
        (pack_loader.PackBusyError("x"), 409),
        (pack_loader.PackImportError("x"), 422),
        (pack_loader.PackRuntimeError("x"), 503),
        (pack_cache.PackCacheUnavailableError("x"), 503),
        (pack_cache.PackCacheVerificationError("x"), 409),
        (pack_loader.PackNameError("x"), 400),
        (ValueError("x"), 400),
        (TimeoutError("x"), 504),
    ],
)
def test_errors_map_to_meaningful_statuses(error, status):
    assert pack_routes._error_status(error) == status


def test_missing_module_is_surfaced_to_the_client():
    payload = pack_routes._error_payload(
        pack_loader.PackImportError("needs it", missing_module="segment_anything")
    )
    assert payload["ok"] is False
    assert payload["missing_module"] == "segment_anything"
    assert payload["code"] == "PackImportError"


# --------------------------------------------------------------------------- #
# Request validation
# --------------------------------------------------------------------------- #


@pytest.mark.parametrize(
    ("method", "suffix"),
    [
        ("POST", "/fetch"),
        ("POST", "/load"),
        ("POST", "/unload"),
        ("POST", "/reload"),
        ("POST", "/verify"),
        ("DELETE", "/cache"),
    ],
)
def test_non_json_bodies_are_rejected(routes, method, suffix):
    response = _call(
        routes,
        method,
        f"{pack_routes.PACK_ROUTE_PREFIX}{suffix}",
        malformed=True,
    )
    assert response.status == 400
    assert _body(response)["ok"] is False


def test_fetch_requires_a_repository(routes):
    response = _call(routes, "POST", f"{pack_routes.PACK_ROUTE_PREFIX}/fetch", {})
    assert response.status == 400
    assert "repository" in _body(response)["error"]


def test_fetch_rejects_an_over_long_repository(routes, monkeypatch):
    monkeypatch.setattr(pack_routes, "MAX_REPOSITORY_LENGTH", 8)
    response = _call(
        routes,
        "POST",
        f"{pack_routes.PACK_ROUTE_PREFIX}/fetch",
        {"repository": "owner/" + "x" * 40},
    )
    assert response.status == 400
    assert "too long" in _body(response)["error"]


def test_fetch_rejects_a_non_boolean_overwrite(routes):
    response = _call(
        routes,
        "POST",
        f"{pack_routes.PACK_ROUTE_PREFIX}/fetch",
        {"repository": "owner/repo", "overwrite": "yes"},
    )
    assert response.status == 400


def test_transactions_require_an_id(routes):
    for suffix in ("/load", "/unload", "/reload"):
        response = _call(
            routes, "POST", f"{pack_routes.PACK_ROUTE_PREFIX}{suffix}", {"id": ""}
        )
        assert response.status == 400


def test_unknown_pack_ids_are_reported_as_missing(routes, monkeypatch):
    folder_paths = ModuleType("folder_paths")
    folder_paths.get_folder_paths = lambda name: []
    monkeypatch.setitem(sys.modules, "folder_paths", folder_paths)

    response = _call(
        routes,
        "POST",
        f"{pack_routes.PACK_ROUTE_PREFIX}/load",
        {"id": "disabled:Nope"},
    )
    assert response.status == 404


# --------------------------------------------------------------------------- #
# Behaviour
# --------------------------------------------------------------------------- #


def test_fetch_delegates_to_the_cache_and_returns_the_record(routes, monkeypatch):
    calls = {}

    def fake_fetch(repository, ref_kind, ref, subdirectory, *, overwrite):
        calls.update(
            repository=repository,
            ref_kind=ref_kind,
            ref=ref,
            subdirectory=subdirectory,
            overwrite=overwrite,
        )
        return pack_cache.CachedPack(
            name="DemoPack",
            slug="owner/DemoPack",
            host="github.com",
            owner="owner",
            repository="DemoPack",
            commit="d" * 40,
            requested_ref="main",
            subdirectory="",
            path=Path("/tmp/DemoPack"),
            file_count=3,
            total_bytes=99,
            fetched_at=1.0,
            has_submodules=False,
            refused_entries=(),
        )

    monkeypatch.setattr(pack_cache, "fetch_pack", fake_fetch)
    response = _call(
        routes,
        "POST",
        f"{pack_routes.PACK_ROUTE_PREFIX}/fetch",
        {"repository": "owner/DemoPack", "ref_kind": "branch", "ref": "main"},
    )

    assert response.status == 200
    assert calls == {
        "repository": "owner/DemoPack",
        "ref_kind": "branch",
        "ref": "main",
        "subdirectory": "",
        "overwrite": False,
    }
    pack = _body(response)["pack"]
    assert pack["id"] == "cache:DemoPack"
    assert pack["short_commit"] == "d" * 12


def test_fetch_reports_cache_errors(routes, monkeypatch):
    def failing(*args, **kwargs):
        raise pack_cache.PackCacheUnavailableError("no user directory")

    monkeypatch.setattr(pack_cache, "fetch_pack", failing)
    response = _call(
        routes,
        "POST",
        f"{pack_routes.PACK_ROUTE_PREFIX}/fetch",
        {"repository": "owner/repo"},
    )
    assert response.status == 503


def test_deleting_a_loaded_pack_is_refused(routes, monkeypatch):
    registry = pack_loader.PackRegistry()
    registry._loaded["cache:DemoPack"] = SimpleNamespace()
    monkeypatch.setattr(pack_loader, "get_pack_registry", lambda: registry)

    response = _call(
        routes,
        "DELETE",
        f"{pack_routes.PACK_ROUTE_PREFIX}/cache",
        {"name": "DemoPack", "commit": "d" * 40},
    )
    assert response.status == 409
    assert "unload" in _body(response)["error"]


def test_deleting_an_unknown_cache_entry_is_a_404(routes, monkeypatch):
    monkeypatch.setattr(pack_loader, "get_pack_registry", pack_loader.PackRegistry)
    monkeypatch.setattr(pack_cache, "delete_cached_pack", lambda name, commit: False)
    response = _call(
        routes,
        "DELETE",
        f"{pack_routes.PACK_ROUTE_PREFIX}/cache",
        {"name": "DemoPack", "commit": "d" * 40},
    )
    assert response.status == 404


def test_state_payload_lists_packs_and_cache(monkeypatch):
    monkeypatch.setattr(pack_loader, "get_pack_registry", pack_loader.PackRegistry)
    monkeypatch.setattr(pack_cache, "list_cached_packs", list)
    folder_paths = ModuleType("folder_paths")
    folder_paths.get_folder_paths = lambda name: []
    monkeypatch.setitem(sys.modules, "folder_paths", folder_paths)

    payload = pack_routes.state_payload()
    assert payload["ok"] is True
    assert payload["packs"] == []
    assert payload["cached"] == []
    assert payload["loaded"] == []


def test_state_payload_survives_an_unavailable_cache(monkeypatch):
    monkeypatch.setattr(pack_loader, "get_pack_registry", pack_loader.PackRegistry)

    def failing():
        raise pack_cache.PackCacheUnavailableError("no user directory")

    monkeypatch.setattr(pack_cache, "list_cached_packs", failing)
    folder_paths = ModuleType("folder_paths")
    folder_paths.get_folder_paths = lambda name: []
    monkeypatch.setitem(sys.modules, "folder_paths", folder_paths)

    assert pack_routes.state_payload()["cached"] == []


# --------------------------------------------------------------------------- #
# The node itself
# --------------------------------------------------------------------------- #


def test_loader_node_declares_editable_widgets():
    required = pack_routes.ComfyPackLoaderNode.INPUT_TYPES()["required"]
    assert list(required) == ["repository", "ref_kind", "ref", "subdirectory"]
    # Everything the user types lives in `required`, so the frontend renders
    # widgets rather than input sockets.
    assert required["ref_kind"][0] == list(pack_routes._REF_KINDS)
    assert required["ref_kind"][0], "a combo must never be empty"


def test_queueing_the_node_reports_state_without_loading_anything(monkeypatch):
    registry = pack_loader.PackRegistry()
    monkeypatch.setattr(pack_loader, "get_pack_registry", lambda: registry)

    def explode(*args, **kwargs):
        raise AssertionError("queueing must never fetch or load")

    monkeypatch.setattr(pack_cache, "fetch_pack", explode)
    monkeypatch.setattr(pack_loader.PackRegistry, "load", explode)

    result = pack_routes.ComfyPackLoaderNode().report(
        repository="owner/repo", ref_kind="default", ref="", subdirectory=""
    )
    status, payload = result["result"]
    assert "No node packs are loaded" in status
    assert json.loads(payload) == {"loaded": []}
    assert result["ui"]["pack_status"] == [status]


def test_node_report_describes_loaded_packs(monkeypatch, tmp_path):
    registry = pack_loader.PackRegistry()
    candidate = pack_loader.PackCandidate(
        pack_id="cache:DemoPack",
        name="DemoPack",
        scope="cache",
        path=tmp_path,
        has_pyproject=False,
        has_requirements=False,
        has_web_directory=False,
        installed_at_boot=False,
    )
    undo = pack_loader.UndoRecord(module_key="k", class_ids=["A", "B"])
    registry._loaded["cache:DemoPack"] = pack_loader.LoadedPack(
        candidate=candidate, undo=undo, dirty=["started 1 background thread(s)"]
    )
    monkeypatch.setattr(pack_loader, "get_pack_registry", lambda: registry)

    status, payload = pack_routes.ComfyPackLoaderNode().report()["result"]
    assert "DemoPack (2 nodes, cache)" in status
    assert "! started 1 background thread(s)" in status
    assert json.loads(payload)["loaded"][0]["class_ids"] == ["A", "B"]
