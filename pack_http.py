"""Live aiohttp router surgery for temporarily loaded node packs.

ComfyUI consumes ``PromptServer.routes`` exactly once at startup and then
freezes the aiohttp router, so a pack imported after boot has both of its HTTP
surfaces silently disabled: the routes it registers during ``__init__`` land in
a table nobody reads, and its ``web/`` directory is never mounted.

This module restores both, reversibly:

* :class:`RouteCapture` swaps ``PromptServer.routes`` for an empty table so a
  pack's import-time registrations are collected instead of discarded.
* :func:`filter_pack_routes` refuses the collected routes that could shadow a
  core endpoint.  A pack loaded from an untrusted source must not be able to
  answer for ``/prompt`` or ``/userdata``.
* :func:`unfrozen_router` reopens the router only for the duration of a load and
  always refreezes, and :func:`remove_resources` deletes every resource a load
  created so unloading leaves the routing table as it was found.

Every private aiohttp attribute this module touches is probed first through
:func:`router_surgery_supported`; when a future aiohttp changes them the loader
degrades to "pack loaded, HTTP surface unavailable" instead of raising.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass
from typing import Any, Iterator, Sequence


# Path segments owned by ComfyUI core or by this extension.  A pack route whose
# first segment matches one of these is refused rather than replayed: aiohttp
# resolves the most specific registered prefix, so a late route really can take
# traffic away from core.
RESERVED_ROUTE_SEGMENTS = frozenset(
    {
        "api",
        "customnode",
        "download",
        "embeddings",
        "experiment",
        "extensions",
        "features",
        "free",
        "history",
        "index.html",
        "interrupt",
        "internal",
        "models",
        "object_info",
        "prompt",
        "queue",
        "scripted_nodes",
        "settings",
        "system_stats",
        "upload",
        "user",
        "userdata",
        "users",
        "view",
        "view_metadata",
        "ws",
    }
)

# Patterns that can match outside the prefix they appear to claim, such as
# ``/{tail:.*}``.  A pack asking for one of these is asking for every path.
_GREEDY_PATTERN = re.compile(r"\{[^{}]*:[^{}]*(?:\.\*|\.\+)[^{}]*\}")

MAX_PACK_ROUTES = 200


class PackRouteError(ValueError):
    """Raised when a pack's HTTP surface cannot be served safely."""


@dataclass
class RouteDecision:
    """One captured route and whether it may be replayed."""

    method: str
    path: str
    allowed: bool
    reason: str = ""

    def to_dict(self) -> dict[str, Any]:
        return {
            "method": self.method,
            "path": self.path,
            "allowed": self.allowed,
            "reason": self.reason,
        }


def router_surgery_supported(app: Any) -> bool:
    """Return whether this aiohttp exposes the internals the loader needs."""

    router = getattr(app, "router", None)
    if router is None:
        return False
    return (
        isinstance(getattr(router, "_frozen", None), bool)
        and isinstance(getattr(router, "_resources", None), list)
        and callable(getattr(router, "unindex_resource", None))
        and callable(getattr(app, "add_routes", None))
    )


@contextmanager
def unfrozen_router(app: Any) -> Iterator[None]:
    """Temporarily reopen a frozen router, always restoring its prior state.

    The window spans a pack's whole import because packs call ``add_routes``
    from inside their own ``__init__``; a narrower window would make those calls
    raise inside third-party code where they cannot be handled.
    """

    router = app.router
    previous = router._frozen
    router._frozen = False
    try:
        yield
    finally:
        router._frozen = previous


@contextmanager
def capture_routes(prompt_server: Any) -> Iterator[Any]:
    """Collect the routes a pack registers on ``PromptServer.routes``.

    ``server.py`` reads ``self.routes`` once at boot, so anything a pack appends
    afterwards is dead.  Swapping in a fresh table for the duration of the load
    turns those dead registrations into a list we can vet and replay.
    """

    from aiohttp import web

    captured = web.RouteTableDef()
    original = prompt_server.routes
    prompt_server.routes = captured
    try:
        yield captured
    finally:
        prompt_server.routes = original


def _first_segment(path: str) -> str:
    return path.lstrip("/").split("/", 1)[0].split("{", 1)[0].lower()


def _existing_canonicals(app: Any) -> set[str]:
    canonicals: set[str] = set()
    for resource in app.router._resources:
        canonical = getattr(resource, "canonical", None)
        if isinstance(canonical, str):
            canonicals.add(canonical)
    return canonicals


def filter_pack_routes(
    captured: Sequence[Any],
    *,
    app: Any,
    pack_name: str,
) -> list[RouteDecision]:
    """Decide which captured routes may be replayed on the live router.

    A route is refused when it claims a reserved core segment, uses a greedy
    pattern that can match outside its own prefix, or duplicates a path that is
    already registered.  Refusal is per route: a pack that wants one hostile
    endpoint still gets its benign ones.
    """

    from aiohttp import web

    existing = _existing_canonicals(app)
    reserved = RESERVED_ROUTE_SEGMENTS
    decisions: list[RouteDecision] = []
    seen: set[tuple[str, str]] = set()
    for route in captured:
        if not isinstance(route, web.RouteDef):
            # StaticDef and friends are handled by the web-directory mount.
            continue
        method = str(route.method)
        path = str(route.path)
        key = (method, path)

        if len(decisions) >= MAX_PACK_ROUTES:
            decisions.append(
                RouteDecision(method, path, False, "pack route limit reached")
            )
            continue
        if key in seen:
            decisions.append(
                RouteDecision(method, path, False, "duplicate route declaration")
            )
            continue
        seen.add(key)

        if not path.startswith("/"):
            decisions.append(
                RouteDecision(method, path, False, "route path is not absolute")
            )
            continue
        segment = _first_segment(path)
        if not segment:
            decisions.append(
                RouteDecision(method, path, False, "route claims the site root")
            )
            continue
        if segment in reserved:
            decisions.append(
                RouteDecision(
                    method,
                    path,
                    False,
                    f"`/{segment}` is reserved by ComfyUI",
                )
            )
            continue
        if _GREEDY_PATTERN.search(path):
            decisions.append(
                RouteDecision(
                    method,
                    path,
                    False,
                    "wildcard pattern can match outside its prefix",
                )
            )
            continue
        if path in existing:
            decisions.append(
                RouteDecision(
                    method,
                    path,
                    False,
                    "path is already served by another route",
                )
            )
            continue

        decisions.append(RouteDecision(method, path, True))
    return decisions


def snapshot_resources(app: Any) -> list[Any]:
    """Record the router's resources so direct registrations can be spotted."""

    return list(app.router._resources)


def audit_direct_resources(
    app: Any,
    before: Sequence[Any],
    *,
    pack_name: str,
) -> tuple[list[Any], list[RouteDecision]]:
    """Vet resources a pack registered on the router itself during import.

    Packs do not only append to ``PromptServer.routes``; several call
    ``app.add_routes`` directly, guarded by ``if not router.frozen``.  Unfreezing
    for the import makes those calls succeed, which is what the pack wants, but
    the resources bypass :func:`filter_pack_routes` and are unknown to the undo
    record.  Approved ones are returned for the caller to track; the rest are
    detached immediately.
    """

    known = {id(resource) for resource in before}
    kept: list[Any] = []
    rejected: list[Any] = []
    decisions: list[RouteDecision] = []

    for resource in app.router._resources:
        if id(resource) in known:
            continue
        canonical = getattr(resource, "canonical", "") or ""
        segment = _first_segment(canonical)
        if not canonical.startswith("/") or not segment:
            reason = "route claims the site root"
        elif segment in RESERVED_ROUTE_SEGMENTS:
            reason = f"`/{segment}` is reserved by ComfyUI"
        elif _GREEDY_PATTERN.search(canonical):
            reason = "wildcard pattern can match outside its prefix"
        else:
            reason = ""

        decisions.append(
            RouteDecision("*", canonical, not reason, reason or "registered directly")
        )
        (kept if not reason else rejected).append(resource)

    if rejected:
        remove_resources(app, rejected)
    return kept, decisions


def replay_routes(
    app: Any,
    captured: Sequence[Any],
    decisions: Sequence[RouteDecision],
) -> list[Any]:
    """Register the approved routes and return the resources they created.

    ComfyUI mints an ``/api``-prefixed twin for its own routes so a frontend dev
    server can proxy them.  Pack routes deliberately get no twin: ``/api`` is
    core's delegation namespace, and auto-promoting a pack path into it is how a
    route named ``/userdata/{file}`` would end up answering for core's.
    """

    from aiohttp import web

    approved = {
        (decision.method, decision.path)
        for decision in decisions
        if decision.allowed
    }
    if not approved:
        return []

    table = web.RouteTableDef()
    for route in captured:
        if not isinstance(route, web.RouteDef):
            continue
        if (str(route.method), str(route.path)) not in approved:
            continue
        table.route(route.method, route.path)(route.handler, **route.kwargs)

    return _add_and_collect(app, table)


def mount_web_directory(app: Any, name: str, directory: str) -> list[Any]:
    """Serve a pack's ``web/`` folder at the same URL a real install would use.

    Packs hardcode ``extensions/<PackName>/...`` inside their own JavaScript, so
    a proxy prefix would break them.  *name* is the loader's validated pack name,
    never a value the pack supplied.
    """

    from aiohttp import web

    return _add_and_collect(app, [web.static("/extensions/" + name, directory)])


def _add_and_collect(app: Any, routes: Any) -> list[Any]:
    """Add *routes* and return only the resources this call created."""

    router = app.router
    before = list(router._resources)
    try:
        app.add_routes(routes)
    except Exception as exc:  # pragma: no cover - aiohttp raises many types
        created = [
            resource for resource in router._resources if resource not in before
        ]
        remove_resources(app, created)
        raise PackRouteError(f"Route registration failed: {exc}") from exc
    return [resource for resource in router._resources if resource not in before]


def remove_resources(app: Any, resources: Sequence[Any]) -> int:
    """Detach resources created for a pack; return how many were removed.

    Re-registering a prefix does not replace an existing resource in aiohttp --
    the first registration keeps winning -- so unloading has to remove the
    resource objects themselves or a later load of the same pack would serve
    stale files.
    """

    router = app.router
    removed = 0
    for resource in resources:
        try:
            router._resources.remove(resource)
        except ValueError:
            continue
        try:
            router.unindex_resource(resource)
        except Exception:
            # The resource is already detached from the routing table; leaving
            # the index entry is preferable to aborting the rest of the unload.
            pass
        removed += 1
    return removed


__all__ = [
    "MAX_PACK_ROUTES",
    "RESERVED_ROUTE_SEGMENTS",
    "PackRouteError",
    "RouteDecision",
    "audit_direct_resources",
    "capture_routes",
    "filter_pack_routes",
    "mount_web_directory",
    "remove_resources",
    "replay_routes",
    "router_surgery_supported",
    "snapshot_resources",
    "unfrozen_router",
]
