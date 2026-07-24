#!/usr/bin/env python
"""Exercise the pack loader against a real ComfyUI checkout.

The unit suite runs the load/unload transaction against a stand-in for ComfyUI's
registries.  This script runs it against the real thing: it boots ComfyUI in
process the way ``main.py`` does, loads real node packs, and asserts that
unloading leaves every registry exactly as it was found.

    python tools/verify_pack_loader.py --comfy-root /path/to/ComfyUI
    python tools/verify_pack_loader.py --comfy-root ... --pack enabled:ComfyUI-KJNodes
    python tools/verify_pack_loader.py --comfy-root ... --repository owner/repo

Nothing is written to the ComfyUI tree.  Pack code *is* executed -- that is the
point -- so a pip firewall is installed first: several popular packs run
``pip install`` from their own ``__init__.py`` and would otherwise modify the
environment simply by being imported.  The firewall is harm reduction for a
developer's machine, not a sandbox.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path


# --------------------------------------------------------------------------- #
# pip firewall -- installed before any pack code can run
# --------------------------------------------------------------------------- #

BLOCKED: list[str] = []


def _looks_like_package_install(command: object) -> bool:
    try:
        parts = command if isinstance(command, (list, tuple)) else [command]
        text = " ".join(str(part) for part in parts).lower()
    except Exception:
        return False
    if "pip" not in text and "uv " not in text:
        return False
    return any(word in text for word in ("install", "uninstall", "freeze"))


def install_pip_firewall() -> None:
    real_popen = subprocess.Popen

    class GuardedPopen(real_popen):  # type: ignore[misc,valid-type]
        def __init__(self, args, *rest, **kwargs):
            if _looks_like_package_install(args):
                BLOCKED.append(str(args))
                raise PermissionError(f"blocked package install: {args}")
            super().__init__(args, *rest, **kwargs)

    subprocess.Popen = GuardedPopen  # type: ignore[misc]

    for name in ("call", "check_call", "check_output", "run"):
        original = getattr(subprocess, name)

        def guarded(*args, _original=original, **kwargs):
            if args and _looks_like_package_install(args[0]):
                BLOCKED.append(str(args[0]))
                raise PermissionError(f"blocked package install: {args[0]}")
            return _original(*args, **kwargs)

        setattr(subprocess, name, guarded)

    real_system = os.system

    def guarded_system(command: str) -> int:
        if _looks_like_package_install(command):
            BLOCKED.append(command)
            return 1
        return real_system(command)

    os.system = guarded_system


# --------------------------------------------------------------------------- #
# ComfyUI bootstrap
# --------------------------------------------------------------------------- #


def bootstrap(comfy_root: Path):
    """Boot ComfyUI in this process, in the order main.py uses.

    The order is load-bearing: ``<root>/comfy`` must not go on ``sys.path`` until
    core nodes are loaded (it shadows ``utils``), and ``init_extra_nodes`` must
    run or several packs fail on a circular import inside ``comfy_extras``.
    """

    os.chdir(comfy_root)
    sys.path.insert(0, os.fspath(comfy_root))

    import comfy.options

    comfy.options.enable_args_parsing(False)

    import nodes
    import server

    loop = asyncio.new_event_loop()
    asyncio.set_event_loop(loop)
    prompt_server = server.PromptServer(loop)
    loop.run_until_complete(
        nodes.init_extra_nodes(init_custom_nodes=False, init_api_nodes=False)
    )
    sys.path.insert(0, os.fspath(comfy_root / "comfy"))

    # main.py registers the core routes and then AppRunner.setup() freezes the
    # router; the loader has to work in that post-boot state.
    prompt_server.add_routes()
    prompt_server.app.router._frozen = True
    return nodes, server, prompt_server, loop


# --------------------------------------------------------------------------- #
# Checks
# --------------------------------------------------------------------------- #


class Report:
    def __init__(self) -> None:
        self.failures = 0

    def check(self, label: str, ok: bool, detail: str = "") -> bool:
        print(f"  [{'PASS' if ok else 'FAIL'}] {label}" + (f" :: {detail}" if detail else ""))
        self.failures += not ok
        return ok


def snapshot(nodes, prompt_server) -> dict:
    folder_paths = sys.modules.get("folder_paths")
    return {
        "classes": set(nodes.NODE_CLASS_MAPPINGS),
        "display": dict(nodes.NODE_DISPLAY_NAME_MAPPINGS),
        "web": dict(nodes.EXTENSION_WEB_DIRS),
        "dirs": dict(nodes.LOADED_MODULE_DIRS),
        "resources": len(prompt_server.app.router._resources),
        "sys_path": list(sys.path),
        "folders": set(getattr(folder_paths, "folder_names_and_paths", {}) or {}),
    }


def verify_pack(pack_id, registry, nodes, prompt_server, loop, report) -> None:
    print(f"\n=== {pack_id} ===")
    before = snapshot(nodes, prompt_server)

    try:
        record = loop.run_until_complete(registry.load(pack_id))
    except Exception as exc:  # noqa: BLE001 - the loader's own errors are the result
        print(f"  load refused: {type(exc).__name__}: {exc}")
        return

    added = set(nodes.NODE_CLASS_MAPPINGS) - before["classes"]
    report.check("registered nodes", bool(added), f"{len(added)} classes")
    report.check("class ids recorded", set(record.undo.class_ids) == added)
    print(f"  web mount: {record.web_mount}")
    print(
        f"  routes: {len(record.routes)} served, {len(record.refused_routes)} refused"
    )
    for refused in record.refused_routes[:5]:
        print(f"    refused {refused['path']}: {refused['reason']}")
    for note in record.dirty:
        print(f"  ! {note}")

    # Every registered class must survive the same treatment /object_info gives it.
    unserializable = []
    for class_id in sorted(added):
        node_class = nodes.NODE_CLASS_MAPPINGS[class_id]
        try:
            json.dumps(
                {
                    "input": node_class.INPUT_TYPES(),
                    "output": list(getattr(node_class, "RETURN_TYPES", [])),
                },
                default=str,
            )
        except Exception as exc:  # noqa: BLE001
            unserializable.append((class_id, f"{type(exc).__name__}: {exc}"))
    report.check(
        "node definitions serialize",
        not unserializable,
        f"{len(unserializable)} failed {unserializable[:2]}",
    )

    loop.run_until_complete(registry.unload(pack_id))
    after = snapshot(nodes, prompt_server)

    report.check(
        "node classes restored",
        after["classes"] == before["classes"],
        f"delta={sorted(after['classes'] ^ before['classes'])[:5]}",
    )
    report.check(
        "display names restored",
        after["display"] == before["display"],
        f"delta={sorted(set(after['display']) ^ set(before['display']))[:5]}",
    )
    report.check("web directories restored", after["web"] == before["web"])
    report.check("module directories restored", after["dirs"] == before["dirs"])
    report.check(
        "router resources restored",
        after["resources"] == before["resources"],
        f"{before['resources']} -> {after['resources']}",
    )
    report.check("sys.path restored", after["sys_path"] == before["sys_path"])
    report.check("folder_paths restored", after["folders"] == before["folders"])
    report.check(
        "pack modules purged",
        not [key for key in sys.modules if key.startswith(record.undo.module_key)],
    )


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--comfy-root",
        type=Path,
        required=True,
        help="Path to the ComfyUI checkout (the directory containing nodes.py)",
    )
    parser.add_argument(
        "--pack",
        action="append",
        default=[],
        metavar="SCOPE:NAME",
        help="Pack id to verify, e.g. disabled:ComfyUI-KJNodes (repeatable)",
    )
    parser.add_argument(
        "--repository",
        action="append",
        default=[],
        metavar="OWNER/REPO",
        help="Fetch this GitHub repository into a temporary cache and verify it",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=0,
        help="Verify this many discovered packs when none are named explicitly",
    )
    arguments = parser.parse_args()

    comfy_root = arguments.comfy_root.expanduser().resolve()
    if not (comfy_root / "nodes.py").is_file():
        parser.error(f"{comfy_root} does not look like a ComfyUI checkout")

    install_pip_firewall()
    nodes, _server, prompt_server, loop = bootstrap(comfy_root)

    sys.path.insert(0, os.fspath(Path(__file__).resolve().parents[1]))
    import pack_cache
    import pack_http
    import pack_loader

    report = Report()
    report.check(
        "aiohttp exposes the router internals the loader needs",
        pack_http.router_surgery_supported(prompt_server.app),
    )

    registry = pack_loader.get_pack_registry()
    pack_ids = list(arguments.pack)

    temporary_cache = None
    if arguments.repository:
        temporary_cache = tempfile.TemporaryDirectory(prefix="pack-cache-verify-")
        pack_cache.cache_root = lambda: Path(temporary_cache.name)
        for repository in arguments.repository:
            print(f"\nfetching {repository}…")
            fetched = pack_cache.fetch_pack(repository)
            print(f"  {fetched.file_count} files at {fetched.commit[:12]}")
            pack_ids.append(fetched.pack_id)

    if not pack_ids:
        discovered = pack_loader.discover_packs()
        print(f"discovered {len(discovered)} packs")
        pack_ids = [
            candidate.pack_id
            for candidate in discovered
            if not candidate.installed_at_boot
        ][: arguments.limit or 5]

    try:
        for pack_id in pack_ids:
            verify_pack(pack_id, registry, nodes, prompt_server, loop, report)
    finally:
        if temporary_cache is not None:
            temporary_cache.cleanup()

    if BLOCKED:
        print(f"\nblocked package installs attempted by packs: {BLOCKED}")
    print(f"\n{'ALL PASS' if not report.failures else f'{report.failures} FAILURES'}")
    return 1 if report.failures else 0


if __name__ == "__main__":
    sys.exit(main())
