"""HTTP API and graph node for fetching and loading third-party node packs.

Routes are registered at import time, while ComfyUI is still building its route
table, which is the only moment a custom node can add endpoints without touching
aiohttp internals (``server.py`` consumes ``PromptServer.routes`` exactly once,
after custom nodes have loaded).

Fetching and loading are HTTP actions driven by explicit button presses, never
side effects of executing a graph.  :class:`ComfyPackLoaderNode` reports state
and nothing else: a workflow that arrives from someone else must not be able to
download and run a repository just because it was queued.  This preserves the
invariant the README already states for scripted nodes -- opening or running a
workflow never executes code the user has not chosen to run.
"""

from __future__ import annotations

import asyncio
import json
import sys
from collections.abc import Mapping
from typing import Any

try:  # pragma: no cover - exercised implicitly by both import styles
    from . import node_pack_tester, pack_cache, pack_loader
except ImportError:  # pytest may collect this file as a top-level module
    import node_pack_tester  # type: ignore[no-redef]
    import pack_cache  # type: ignore[no-redef]
    import pack_loader  # type: ignore[no-redef]

_REF_KINDS = tuple(node_pack_tester.SUPPORTED_REF_KINDS)


PACK_ROUTE_PREFIX = "/scripted_nodes/packs"
MAX_REPOSITORY_LENGTH = 512


def _error_status(error: Exception) -> int:
    if isinstance(error, pack_loader.PackNotFoundError):
        return 404
    if isinstance(error, pack_loader.PackConflictError):
        return 409
    if isinstance(error, pack_loader.PackBusyError):
        return 409
    if isinstance(error, pack_loader.PackImportError):
        return 422
    if isinstance(error, pack_loader.PackRuntimeError):
        return 503
    if isinstance(error, pack_cache.PackCacheUnavailableError):
        return 503
    if isinstance(error, pack_cache.PackCacheVerificationError):
        return 409
    if isinstance(error, TimeoutError):
        return 504
    return 400


def _error_payload(error: Exception) -> dict[str, Any]:
    payload: dict[str, Any] = {
        "ok": False,
        "error": str(error),
        "code": type(error).__name__,
    }
    missing = getattr(error, "missing_module", None)
    if missing:
        payload["missing_module"] = missing
    return payload


def state_payload() -> dict[str, Any]:
    """Everything the pack browser needs in one response."""

    registry = pack_loader.get_pack_registry()
    try:
        cached = [pack.to_dict() for pack in pack_cache.list_cached_packs()]
    except pack_cache.PackCacheError:
        cached = []
    return {
        "ok": True,
        "packs": registry.catalog(),
        "cached": cached,
        "loaded": [record.candidate.pack_id for record in registry.loaded_packs()],
    }


def _register_pack_routes() -> bool:
    """Register the pack endpoints when imported by a running ComfyUI."""

    server_module = sys.modules.get("server")
    if server_module is None:
        return False
    try:
        from aiohttp import web
    except ImportError:
        return False

    PromptServer = getattr(server_module, "PromptServer", None)
    prompt_server = (
        getattr(PromptServer, "instance", None) if PromptServer is not None else None
    )
    if prompt_server is None or not hasattr(prompt_server, "routes"):
        return False

    def failure(error: Exception) -> Any:
        return web.json_response(_error_payload(error), status=_error_status(error))

    async def json_payload(request: Any) -> Mapping[str, Any] | None:
        try:
            payload = await request.json()
        except Exception:
            return None
        return payload if isinstance(payload, Mapping) else None

    def required_string(payload: Mapping[str, Any], key: str) -> str:
        value = payload.get(key)
        if not isinstance(value, str) or not value.strip():
            raise pack_loader.PackNameError(
                f"Request JSON must contain a non-empty string `{key}`"
            )
        return value.strip()

    def optional_string(payload: Mapping[str, Any], key: str) -> str:
        value = payload.get(key, "")
        if value is None:
            return ""
        if not isinstance(value, str):
            raise pack_loader.PackNameError(f"`{key}` must be a string")
        return value.strip()

    @prompt_server.routes.get(PACK_ROUTE_PREFIX)
    async def list_packs(request: Any) -> Any:
        try:
            return web.json_response(state_payload())
        except pack_loader.PackLoaderError as exc:
            return failure(exc)

    @prompt_server.routes.post(f"{PACK_ROUTE_PREFIX}/fetch")
    async def fetch_pack_route(request: Any) -> Any:
        payload = await json_payload(request)
        if payload is None:
            return failure(pack_loader.PackNameError("Request body must be JSON"))
        try:
            repository = required_string(payload, "repository")
            if len(repository) > MAX_REPOSITORY_LENGTH:
                raise pack_loader.PackNameError("Repository reference is too long")
            ref_kind = optional_string(payload, "ref_kind") or "default"
            ref = optional_string(payload, "ref")
            subdirectory = optional_string(payload, "subdirectory")
            overwrite = payload.get("overwrite", False)
            if not isinstance(overwrite, bool):
                raise pack_loader.PackNameError("`overwrite` must be a boolean")
        except pack_loader.PackLoaderError as exc:
            return failure(exc)

        try:
            # Git subprocesses would otherwise block the server's event loop for
            # the whole fetch.
            record = await asyncio.to_thread(
                pack_cache.fetch_pack,
                repository,
                ref_kind,
                ref,
                subdirectory,
                overwrite=overwrite,
            )
        except (pack_cache.PackCacheError, ValueError) as exc:
            return failure(exc)
        return web.json_response({"ok": True, "pack": record.to_dict()})

    async def transaction(request: Any, action: str) -> Any:
        payload = await json_payload(request)
        if payload is None:
            return failure(pack_loader.PackNameError("Request body must be JSON"))
        try:
            pack_id = required_string(payload, "id")
        except pack_loader.PackLoaderError as exc:
            return failure(exc)

        registry = pack_loader.get_pack_registry()
        method = getattr(registry, action)
        try:
            record = await method(pack_id)
        except pack_loader.PackLoaderError as exc:
            return failure(exc)
        return web.json_response(
            {"ok": True, "action": action, "pack": record.to_dict(), **state_payload()}
        )

    @prompt_server.routes.post(f"{PACK_ROUTE_PREFIX}/load")
    async def load_pack(request: Any) -> Any:
        return await transaction(request, "load")

    @prompt_server.routes.post(f"{PACK_ROUTE_PREFIX}/unload")
    async def unload_pack(request: Any) -> Any:
        return await transaction(request, "unload")

    @prompt_server.routes.post(f"{PACK_ROUTE_PREFIX}/reload")
    async def reload_pack(request: Any) -> Any:
        return await transaction(request, "reload")

    @prompt_server.routes.post(f"{PACK_ROUTE_PREFIX}/verify")
    async def verify_cached(request: Any) -> Any:
        payload = await json_payload(request)
        if payload is None:
            return failure(pack_loader.PackNameError("Request body must be JSON"))
        try:
            pack_id = required_string(payload, "id")
            candidate = pack_loader.find_pack(pack_id)
        except pack_loader.PackLoaderError as exc:
            return failure(exc)
        if candidate.scope != "cache":
            return web.json_response(
                {"ok": True, "verified": False, "changed": [], "added": []}
            )
        try:
            changed, added = await asyncio.to_thread(
                pack_cache.verify_pack, candidate.path
            )
        except pack_cache.PackCacheError as exc:
            return failure(exc)
        return web.json_response(
            {"ok": True, "verified": not changed, "changed": changed, "added": added}
        )

    @prompt_server.routes.delete(f"{PACK_ROUTE_PREFIX}/cache")
    async def delete_cached(request: Any) -> Any:
        payload = await json_payload(request)
        if payload is None:
            return failure(pack_loader.PackNameError("Request body must be JSON"))
        try:
            name = pack_loader.normalize_pack_name(required_string(payload, "name"))
            commit = required_string(payload, "commit")
        except pack_loader.PackLoaderError as exc:
            return failure(exc)
        if pack_loader.get_pack_registry().get(f"cache:{name}") is not None:
            return failure(
                pack_loader.PackConflictError(
                    f"`{name}` is currently loaded; unload it before deleting"
                )
            )
        try:
            deleted = await asyncio.to_thread(
                pack_cache.delete_cached_pack, name, commit
            )
        except pack_cache.PackCacheError as exc:
            return failure(exc)
        if not deleted:
            return failure(
                pack_loader.PackNotFoundError(
                    f"No cached copy of `{name}` at `{commit}`"
                )
            )
        return web.json_response({"ok": True, **state_payload()})

    return True


class ComfyPackLoaderNode:
    """Reports which node packs are loaded; never loads one when queued.

    Fetching and loading happen through this node's buttons, which call the HTTP
    API directly.  Keeping them out of ``execute`` is deliberate: a workflow
    someone else built would otherwise be able to download and run an arbitrary
    repository the moment it was queued.
    """

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "repository": (
                    "STRING",
                    {
                        "default": "https://github.com/owner/repository",
                        "multiline": False,
                        "tooltip": "Public HTTPS GitHub URL or `owner/repository`",
                    },
                ),
                "ref_kind": (list(_REF_KINDS),),
                "ref": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "tooltip": "Branch, tag or full commit id; empty for default",
                    },
                ),
                "subdirectory": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "tooltip": "Optional pack folder inside a monorepo",
                    },
                ),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("status", "status_json")
    FUNCTION = "report"
    CATEGORY = "utils/scripted"
    DESCRIPTION = (
        "Fetch a GitHub node pack and load its real nodes into the running "
        "ComfyUI without installing it. Queueing this node only reports state; "
        "use its buttons to fetch, load and unload."
    )

    @classmethod
    def IS_CHANGED(cls, **kwargs: Any) -> float:
        return float("nan")

    def report(self, **kwargs: Any) -> dict[str, Any]:
        registry = pack_loader.get_pack_registry()
        loaded = registry.loaded_packs()
        if loaded:
            lines = [f"{len(loaded)} node pack(s) loaded without installing:"]
            for record in loaded:
                lines.append(
                    f"  {record.candidate.name} "
                    f"({len(record.undo.class_ids)} nodes, {record.candidate.scope})"
                )
                for note in record.dirty:
                    lines.append(f"    ! {note}")
        else:
            lines = ["No node packs are loaded by the Scripted Nodes pack loader."]
        status = "\n".join(lines)
        payload = json.dumps(
            {"loaded": [record.to_dict() for record in loaded]},
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return {
            "ui": {"pack_status": [status]},
            "result": (status, payload),
        }


PACK_ROUTES_REGISTERED = _register_pack_routes()


__all__ = [
    "PACK_ROUTES_REGISTERED",
    "PACK_ROUTE_PREFIX",
    "ComfyPackLoaderNode",
    "state_payload",
]
