"""Load a third-party ComfyUI node pack into the running process.

ComfyUI already ships the primitive this needs: ``nodes.load_custom_node`` is a
coroutine that accepts any absolute directory and derives its ``sys.modules``
key from that path, so a pack does not have to live in ``custom_nodes`` and the
server does not have to restart.  Because the pack's real classes land in
``nodes.NODE_CLASS_MAPPINGS``, the real scheduler runs them: hidden inputs,
``INPUT_IS_LIST``, list outputs, lazy evaluation, ``IS_CHANGED``,
``VALIDATE_INPUTS``, async execution and V3 schemas all work with no adapter
code, and ``/object_info`` renders the pack's real widgets because it is rebuilt
from the mappings on every request.

What core does *not* do is take any of it back, so this module wraps the load in
a transaction.  Every global the loader or the pack touches is snapshotted
first, and the differences are recorded in an :class:`UndoRecord` that
:meth:`PackRegistry.unload` replays in reverse.

Loading a pack executes its code with ComfyUI's permissions.  Nothing here is a
sandbox, and :attr:`LoadedPack.dirty` exists precisely because some of what a
pack does at import time -- background threads, C extensions, monkeypatches --
cannot be undone at all.
"""

from __future__ import annotations

import logging
import os
import re
import sys
import threading
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

try:  # pragma: no cover - exercised implicitly by both import styles
    from . import pack_http
except ImportError:  # pytest may collect this file as a top-level module
    import pack_http  # type: ignore[no-redef]


#: ``load_custom_node`` stamps ``RELATIVE_PYTHON_MODULE`` with this prefix, which
#: surfaces in ``/object_info`` as the node's provenance.
PACK_MODULE_PARENT = "scripted_node_packs"

DISABLED_DIRECTORY_NAME = ".disabled"
MAX_PACK_NAME_LENGTH = 64

#: Where a loadable pack directory came from.  ``enabled`` and ``disabled`` are
#: the two ``custom_nodes`` locations ComfyUI already understands; ``cache`` is
#: reserved for packs fetched from a remote repository and materialized under
#: the user directory, which load through exactly the same transaction.
PACK_SCOPES = ("enabled", "disabled", "cache")
SKIPPED_PACK_NAMES = frozenset({"__pycache__", "ComfyUI-Scripted-Nodes"})

# A pack name becomes a filesystem component, a ``sys.modules`` key fragment and
# a URL segment under /extensions, so it is validated once, strictly.
_PACK_NAME_PATTERN = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\Z")
_MISSING_MODULE_PATTERN = re.compile(r"No module named ['\"]([^'\"]+)['\"]")


class PackLoaderError(ValueError):
    """Base error for node-pack loading."""


class PackNameError(PackLoaderError):
    """Raised when a pack name or id is unsafe or malformed."""


class PackNotFoundError(PackLoaderError):
    """Raised when no loadable pack matches an id."""


class PackConflictError(PackLoaderError):
    """Raised when loading would collide with an already-registered pack."""


class PackBusyError(PackLoaderError):
    """Raised when the server is executing a prompt or another load is running."""


class PackRuntimeError(PackLoaderError):
    """Raised when the ComfyUI runtime this loader needs is unavailable."""


class PackImportError(PackLoaderError):
    """Raised when a pack's own code failed to import."""

    def __init__(self, message: str, *, missing_module: str | None = None):
        super().__init__(message)
        self.missing_module = missing_module


def normalize_pack_name(value: str) -> str:
    """Return *value* if it is safe as a directory, module and URL component."""

    if not isinstance(value, str):
        raise PackNameError("Pack name must be a string")
    name = unicodedata.normalize("NFC", value.strip())
    if not name:
        raise PackNameError("Pack name cannot be empty")
    if len(name) > MAX_PACK_NAME_LENGTH:
        raise PackNameError(
            f"Pack name cannot exceed {MAX_PACK_NAME_LENGTH} characters"
        )
    if name in {".", ".."}:
        raise PackNameError("Pack name cannot be a path component")
    if not _PACK_NAME_PATTERN.fullmatch(name):
        raise PackNameError(
            "Pack name may only contain letters, digits, `.`, `-` and `_` "
            "and must not start with a separator"
        )
    return name


def _parse_pack_id(pack_id: str) -> tuple[str, str]:
    if not isinstance(pack_id, str):
        raise PackNameError("Pack id must be a string")
    scope, separator, raw_name = pack_id.partition(":")
    if not separator or scope not in PACK_SCOPES:
        raise PackNameError(
            "Pack id must start with " + " or ".join(f"`{s}:`" for s in PACK_SCOPES)
        )
    return scope, normalize_pack_name(raw_name)


# --------------------------------------------------------------------------- #
# ComfyUI runtime accessors
#
# Imported lazily so this module stays importable by tests and tooling with no
# ComfyUI present, matching script_library.py's treatment of folder_paths.
# --------------------------------------------------------------------------- #


def _require_module(name: str, purpose: str) -> Any:
    module = sys.modules.get(name)
    if module is not None:
        return module
    try:
        return __import__(name, fromlist=["__name__"])
    except ImportError as exc:
        raise PackRuntimeError(
            f"ComfyUI's `{name}` module is unavailable; {purpose} requires a "
            "running ComfyUI"
        ) from exc


def _nodes_module() -> Any:
    return _require_module("nodes", "loading a node pack")


def _prompt_server() -> Any:
    server_module = _require_module("server", "loading a node pack")
    prompt_server = getattr(
        getattr(server_module, "PromptServer", None), "instance", None
    )
    if prompt_server is None:
        raise PackRuntimeError("ComfyUI's PromptServer is not running")
    return prompt_server


def _folder_paths() -> Any | None:
    return sys.modules.get("folder_paths")


def _custom_node_roots() -> list[Path]:
    folder_paths = _folder_paths()
    roots: list[Path] = []
    if folder_paths is not None:
        try:
            raw_roots = folder_paths.get_folder_paths("custom_nodes")
        except Exception:
            raw_roots = []
        for raw in raw_roots:
            try:
                roots.append(Path(raw).resolve(strict=True))
            except OSError:
                continue
    return roots


# --------------------------------------------------------------------------- #
# Discovery
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class PackCandidate:
    """A directory that looks like a loadable node pack."""

    pack_id: str
    name: str
    scope: str
    path: Path
    has_pyproject: bool
    has_requirements: bool
    has_web_directory: bool
    installed_at_boot: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.pack_id,
            "name": self.name,
            "scope": self.scope,
            "path": os.fspath(self.path),
            "has_pyproject": self.has_pyproject,
            "has_requirements": self.has_requirements,
            "has_web_directory": self.has_web_directory,
            "installed_at_boot": self.installed_at_boot,
        }


def _looks_like_pack(directory: Path) -> bool:
    try:
        return (directory / "__init__.py").is_file()
    except OSError:
        return False


def _boot_loaded_directory(name: str) -> str | None:
    nodes = sys.modules.get("nodes")
    if nodes is None:
        return None
    loaded = getattr(nodes, "LOADED_MODULE_DIRS", None)
    if not isinstance(loaded, dict):
        return None
    directory = loaded.get(name)
    return directory if isinstance(directory, str) else None


def _candidate(directory: Path, scope: str) -> PackCandidate | None:
    try:
        name = normalize_pack_name(directory.name)
    except PackNameError:
        return None
    if name in SKIPPED_PACK_NAMES or not _looks_like_pack(directory):
        return None

    boot_directory = _boot_loaded_directory(name)
    installed_at_boot = boot_directory is not None and Path(
        boot_directory
    ) == directory

    web_directory = any(
        (directory / candidate).is_dir() for candidate in ("web", "js", "dist")
    )
    return PackCandidate(
        pack_id=f"{scope}:{name}",
        name=name,
        scope=scope,
        path=directory,
        has_pyproject=(directory / "pyproject.toml").is_file(),
        has_requirements=(directory / "requirements.txt").is_file(),
        has_web_directory=web_directory,
        installed_at_boot=installed_at_boot,
    )


def _cache_module() -> Any | None:
    try:
        from . import pack_cache
    except ImportError:  # pragma: no cover - top-level import style
        try:
            import pack_cache  # type: ignore[no-redef]
        except ImportError:
            return None
    return pack_cache


def _cached_candidates() -> list[PackCandidate]:
    """Return packs fetched from a remote repository into the local cache."""

    pack_cache = _cache_module()
    if pack_cache is None:
        return []
    try:
        cached = pack_cache.list_cached_packs()
    except Exception:
        return []

    candidates: list[PackCandidate] = []
    for entry in cached:
        candidate = _candidate(entry.path, "cache")
        if candidate is not None:
            candidates.append(candidate)
    return candidates


def discover_packs() -> list[PackCandidate]:
    """Return every pack directory this loader could load.

    That is ComfyUI's ``custom_nodes`` roots, the ``.disabled`` folder that
    ComfyUI-Manager uses, and anything fetched into the local pack cache.  The
    first match wins when a name appears more than once.
    """

    found: dict[str, PackCandidate] = {}
    for candidate in _cached_candidates():
        found.setdefault(candidate.pack_id, candidate)
    for root in _custom_node_roots():
        for scope, base in (
            ("enabled", root),
            ("disabled", root / DISABLED_DIRECTORY_NAME),
        ):
            try:
                entries = sorted(base.iterdir())
            except OSError:
                continue
            for entry in entries:
                if not entry.is_dir() or entry.is_symlink():
                    continue
                candidate = _candidate(entry, scope)
                if candidate is not None:
                    found.setdefault(candidate.pack_id, candidate)
    return sorted(found.values(), key=lambda item: item.pack_id)


def find_pack(pack_id: str) -> PackCandidate:
    """Resolve a pack id to a candidate, refusing caller-supplied paths."""

    scope, name = _parse_pack_id(pack_id)
    normalized = f"{scope}:{name}"
    for candidate in discover_packs():
        if candidate.pack_id == normalized:
            return candidate
    raise PackNotFoundError(f"No loadable node pack matches `{normalized}`")


# --------------------------------------------------------------------------- #
# Transaction bookkeeping
# --------------------------------------------------------------------------- #


@dataclass
class UndoRecord:
    """Everything one load changed, in the form unloading needs to reverse it."""

    module_key: str = ""
    class_ids: list[str] = field(default_factory=list)
    display_names_added: list[str] = field(default_factory=list)
    web_dir_keys: list[str] = field(default_factory=list)
    loaded_module_dir_key: str | None = None
    sys_path_added: list[str] = field(default_factory=list)
    sys_modules_added: list[str] = field(default_factory=list)
    folder_path_keys_added: list[str] = field(default_factory=list)
    route_resources: list[Any] = field(default_factory=list)


@dataclass
class LoadedPack:
    """A pack currently registered by this loader."""

    candidate: PackCandidate
    undo: UndoRecord
    routes: list[dict[str, Any]] = field(default_factory=list)
    refused_routes: list[dict[str, Any]] = field(default_factory=list)
    web_directory: str | None = None
    collisions: list[str] = field(default_factory=list)
    dirty: list[str] = field(default_factory=list)

    @property
    def web_mount(self) -> str | None:
        """The URL the pack's own JavaScript expects to be served from."""

        if self.web_directory is None:
            return None
        return "/extensions/" + self.candidate.name

    def to_dict(self) -> dict[str, Any]:
        return {
            **self.candidate.to_dict(),
            "class_ids": list(self.undo.class_ids),
            "node_count": len(self.undo.class_ids),
            "routes": list(self.routes),
            "refused_routes": list(self.refused_routes),
            "web_mount": self.web_mount,
            "collisions": list(self.collisions),
            "dirty": list(self.dirty),
        }


class _LogCapture(logging.Handler):
    """Collect the traceback ``load_custom_node`` logs instead of raising."""

    def __init__(self) -> None:
        super().__init__(level=logging.WARNING)
        self.records: list[str] = []

    def emit(self, record: logging.LogRecord) -> None:
        try:
            self.records.append(record.getMessage())
        except Exception:  # pragma: no cover - defensive
            pass

    @property
    def text(self) -> str:
        return "\n".join(self.records)


def _dirty_notes(added_sys_path: list[str], new_threads: int) -> list[str]:
    """Describe what this load did that unloading cannot reverse.

    Unloading restores the node registries, not the process.  A pack that spawns
    a thread, imports a C extension or patches a core module stays in memory
    whatever the registry says, so the loader reports that rather than claiming
    a clean removal.
    """

    notes: list[str] = []
    if added_sys_path:
        notes.append(
            "added "
            + ", ".join(f"`{entry}`" for entry in added_sys_path)
            + " to sys.path"
        )
    if new_threads > 0:
        notes.append(
            f"started {new_threads} background thread(s) that keep running "
            "after unload"
        )
    return notes


# --------------------------------------------------------------------------- #
# Execution-cache invalidation
# --------------------------------------------------------------------------- #

_PACK_GENERATION = 0
_CACHE_PATCH_INSTALLED = False


def _reset_cache_instance(cache: Any) -> None:
    for attribute in ("cache", "subcaches", "used_generation", "children"):
        value = getattr(cache, attribute, None)
        if isinstance(value, dict):
            value.clear()


def _install_cache_invalidator() -> bool:
    """Make ComfyUI drop cached node outputs after a pack transaction.

    The ``PromptExecutor`` is a local inside ``prompt_worker`` and unreachable
    from any global, so the only place to hook is the cache itself.  Patching
    ``BasicCache.set_prompt`` covers every cache subclass through ``super()``;
    clearing is idempotent, so being reached twice for one prompt is harmless.
    """

    global _CACHE_PATCH_INSTALLED
    if _CACHE_PATCH_INSTALLED:
        return True
    caching = sys.modules.get("comfy_execution.caching")
    if caching is None:
        return False
    basic_cache = getattr(caching, "BasicCache", None)
    original = getattr(basic_cache, "set_prompt", None)
    if original is None:
        return False
    if getattr(original, "_scripted_nodes_patched", False):
        _CACHE_PATCH_INSTALLED = True
        return True

    async def set_prompt(self: Any, dynprompt: Any, node_ids: Any, is_changed: Any):
        if getattr(self, "_scripted_nodes_generation", None) != _PACK_GENERATION:
            self._scripted_nodes_generation = _PACK_GENERATION
            _reset_cache_instance(self)
        return await original(self, dynprompt, node_ids, is_changed)

    set_prompt._scripted_nodes_patched = True  # type: ignore[attr-defined]
    basic_cache.set_prompt = set_prompt
    _CACHE_PATCH_INSTALLED = True
    return True


def _invalidate_execution_cache(class_ids: list[str]) -> None:
    global _PACK_GENERATION
    _PACK_GENERATION += 1
    _install_cache_invalidator()

    caching = sys.modules.get("comfy_execution.caching")
    memo = getattr(caching, "NODE_CLASS_CONTAINS_UNIQUE_ID", None)
    if isinstance(memo, dict):
        # Memoised per class_type and never invalidated by core, so a reloaded
        # class would otherwise inherit the previous class's hidden-input answer.
        for class_id in class_ids:
            memo.pop(class_id, None)

    folder_paths = _folder_paths()
    for attribute in ("filename_list_cache", "cache_helper"):
        cache = getattr(folder_paths, attribute, None)
        if isinstance(cache, dict):
            cache.clear()
        else:
            clear = getattr(cache, "clear", None)
            if callable(clear):
                try:
                    clear()
                except Exception:
                    pass


# --------------------------------------------------------------------------- #
# The loader
# --------------------------------------------------------------------------- #


class PackRegistry:
    """Loads and unloads node packs against the live ComfyUI registries."""

    def __init__(self) -> None:
        self._lock = threading.RLock()
        self._transaction = threading.Lock()
        self._loaded: dict[str, LoadedPack] = {}

    # -- state ---------------------------------------------------------- #

    def loaded_packs(self) -> list[LoadedPack]:
        with self._lock:
            return list(self._loaded.values())

    def get(self, pack_id: str) -> LoadedPack | None:
        with self._lock:
            return self._loaded.get(pack_id)

    def catalog(self) -> list[dict[str, Any]]:
        """Describe every discovered pack and whether this loader owns it."""

        with self._lock:
            loaded = dict(self._loaded)
        entries: list[dict[str, Any]] = []
        for candidate in discover_packs():
            record = loaded.get(candidate.pack_id)
            if record is not None:
                entry = record.to_dict()
                entry["state"] = "loaded"
            else:
                entry = candidate.to_dict()
                entry["state"] = (
                    "installed" if candidate.installed_at_boot else "available"
                )
                entry["node_count"] = 0
            entries.append(entry)
        return entries

    # -- guards --------------------------------------------------------- #

    def _require_idle(self) -> None:
        """Refuse to mutate the registry while the executor may be reading it.

        The prompt worker runs in its own thread and re-reads
        ``NODE_CLASS_MAPPINGS`` per node, so this check is necessary.  It is not
        atomic: a prompt queued between the check and the mutation still races,
        which is why loads are a deliberate user action rather than automatic.
        """

        try:
            prompt_server = _prompt_server()
        except PackRuntimeError:
            return
        queue = getattr(prompt_server, "prompt_queue", None)
        remaining = getattr(queue, "get_tasks_remaining", None)
        if callable(remaining):
            try:
                pending = remaining()
            except Exception:
                return
            if pending:
                raise PackBusyError(
                    "ComfyUI is executing or has queued prompts; wait for the "
                    "queue to drain before changing loaded packs"
                )

    # -- load ----------------------------------------------------------- #

    async def load(self, pack_id: str) -> LoadedPack:
        """Register a pack's real node classes into the running server."""

        candidate = find_pack(pack_id)
        with self._lock:
            if candidate.pack_id in self._loaded:
                raise PackConflictError(
                    f"`{candidate.pack_id}` is already loaded; unload it first"
                )
        if candidate.installed_at_boot:
            raise PackConflictError(
                f"`{candidate.name}` is installed and was already loaded by "
                "ComfyUI at startup"
            )
        added_files = self._verify_cached(candidate)

        if not self._transaction.acquire(blocking=False):
            raise PackBusyError("Another node-pack transaction is in progress")
        try:
            self._require_idle()
            record = await self._load_locked(candidate)
        finally:
            self._transaction.release()

        if added_files:
            record.dirty.append(
                f"{len(added_files)} file(s) present in the cached tree were not "
                "part of the fetched commit"
            )
        with self._lock:
            self._loaded[candidate.pack_id] = record
        _invalidate_execution_cache(record.undo.class_ids)
        self._notify("loaded", record)
        return record

    def _verify_cached(self, candidate: PackCandidate) -> list[str]:
        """Refuse a fetched pack whose files no longer match its commit.

        Content-addressing is only meaningful if it is checked.  Files added
        after the fetch are tolerated -- packs routinely write their own config
        into their directory -- but a file the commit named having *changed* means
        the code about to run is not the code that was fetched.
        """

        if candidate.scope != "cache":
            return []
        pack_cache = _cache_module()
        if pack_cache is None:
            return []
        try:
            changed, added = pack_cache.verify_pack(candidate.path)
        except pack_cache.PackCacheError as exc:
            raise PackConflictError(str(exc)) from exc
        if changed:
            raise PackConflictError(
                f"`{candidate.name}` no longer matches the commit it was fetched "
                "from ("
                + ", ".join(changed[:5])
                + (f", and {len(changed) - 5} more" if len(changed) > 5 else "")
                + "); re-fetch it before loading"
            )
        return added

    async def _load_locked(self, candidate: PackCandidate) -> LoadedPack:
        nodes = _nodes_module()
        prompt_server = _prompt_server()
        app = getattr(prompt_server, "app", None)

        module_path = os.fspath(candidate.path)
        module_key = module_path.replace(".", "_x_")
        undo = UndoRecord(module_key=module_key)

        node_classes = nodes.NODE_CLASS_MAPPINGS
        display_names = nodes.NODE_DISPLAY_NAME_MAPPINGS
        web_dirs = nodes.EXTENSION_WEB_DIRS
        module_dirs = nodes.LOADED_MODULE_DIRS

        # A pack whose directory name is already claimed would silently take
        # over the installed pack's web-dir mapping and module registration.
        if candidate.name in module_dirs:
            raise PackConflictError(
                f"`{candidate.name}` is already registered by another pack"
            )
        if candidate.name in web_dirs:
            raise PackConflictError(
                f"`{candidate.name}` already owns an /extensions mount"
            )

        before_classes = set(node_classes)
        before_display = dict(display_names)
        before_web = dict(web_dirs)
        before_sys_path = list(sys.path)
        before_threads = threading.active_count()
        folder_paths = _folder_paths()
        before_folders = (
            set(getattr(folder_paths, "folder_names_and_paths", {}) or {})
            if folder_paths is not None
            else set()
        )

        # A previous load of the same directory leaves its submodules behind;
        # without this purge a reload silently re-uses the stale ones.
        for key in [
            key
            for key in sys.modules
            if key == module_key or key.startswith(module_key + ".")
        ]:
            sys.modules.pop(key, None)

        hook_breaker = sys.modules.get("hook_breaker_ac10a0")
        if hook_breaker is not None:
            try:
                hook_breaker.save_functions()
            except Exception:
                hook_breaker = None

        capture = _LogCapture()
        root_logger = logging.getLogger()
        root_logger.addHandler(capture)

        surgery = app is not None and pack_http.router_surgery_supported(app)
        captured_routes: list[Any] = []
        before_resources = pack_http.snapshot_resources(app) if surgery else []
        try:
            if surgery:
                with pack_http.capture_routes(prompt_server) as table:
                    with pack_http.unfrozen_router(app):
                        loaded = await nodes.load_custom_node(
                            module_path,
                            ignore=set(before_classes),
                            module_parent=PACK_MODULE_PARENT,
                        )
                    captured_routes = list(table)
            else:
                loaded = await nodes.load_custom_node(
                    module_path,
                    ignore=set(before_classes),
                    module_parent=PACK_MODULE_PARENT,
                )
        finally:
            root_logger.removeHandler(capture)
            if hook_breaker is not None:
                try:
                    hook_breaker.restore_functions()
                except Exception:
                    pass

        # Only the pack's own modules are recorded: purging an unrelated
        # dependency it happened to import first would break whoever else is
        # using it.
        undo.sys_modules_added = sorted(
            key
            for key in sys.modules
            if key == module_key or key.startswith(module_key + ".")
        )
        undo.sys_path_added = [
            entry for entry in sys.path if entry not in before_sys_path
        ]
        if candidate.name in module_dirs:
            undo.loaded_module_dir_key = candidate.name

        # Resources the pack registered on the router itself during its import,
        # which the unfrozen window let through.  Tracked before the failure
        # check so a pack that raises half-way still leaves the router clean.
        direct_decisions: list[Any] = []
        if surgery:
            direct, direct_decisions = pack_http.audit_direct_resources(
                app, before_resources, pack_name=candidate.name
            )
            undo.route_resources.extend(direct)

        if not loaded:
            self._rollback(undo, app, before_display, before_web)
            missing = _MISSING_MODULE_PATTERN.search(capture.text)
            module_name = missing.group(1) if missing else None
            if module_name:
                raise PackImportError(
                    f"`{candidate.name}` needs the Python module "
                    f"`{module_name}`, which is not installed",
                    missing_module=module_name,
                )
            detail = capture.text.strip().splitlines()
            raise PackImportError(
                f"`{candidate.name}` failed to import"
                + (f": {detail[-1]}" if detail else "")
            )

        record = LoadedPack(candidate=candidate, undo=undo)
        undo.class_ids = sorted(set(node_classes) - before_classes)
        if folder_paths is not None:
            after_folders = set(
                getattr(folder_paths, "folder_names_and_paths", {}) or {}
            )
            undo.folder_path_keys_added = sorted(after_folders - before_folders)

        record.collisions = self._settle_display_names(
            display_names, before_display, set(undo.class_ids), undo
        )
        record.web_directory = self._settle_web_directory(
            web_dirs, before_web, candidate, undo
        )
        record.dirty = _dirty_notes(
            undo.sys_path_added,
            max(0, threading.active_count() - before_threads),
        )

        if not undo.class_ids:
            self._rollback(undo, app, before_display, before_web)
            raise PackImportError(
                f"`{candidate.name}` imported but registered no nodes"
                + (
                    "; every node id it declares is already taken"
                    if record.collisions
                    else ""
                )
            )

        if surgery:
            self._serve_pack_http(
                app, candidate, record, captured_routes, direct_decisions, undo
            )
        elif captured_routes or record.web_directory:
            record.dirty.append(
                "this aiohttp version does not expose the router internals, so "
                "the pack's web assets and endpoints are not served"
            )
        return record

    # -- post-load hygiene ---------------------------------------------- #

    def _settle_display_names(
        self,
        display_names: dict[str, Any],
        before: dict[str, Any],
        class_ids: set[str],
        undo: UndoRecord,
    ) -> list[str]:
        """Keep the pack's display names for its own nodes and nothing else.

        ``load_custom_node`` applies ``NODE_DISPLAY_NAME_MAPPINGS.update`` with
        no regard for its own ``ignore`` set, on both the V1 and V3 paths, so a
        pack can retitle core nodes or nodes belonging to another pack.
        """

        collisions: list[str] = []
        for key in list(display_names):
            if key in class_ids:
                undo.display_names_added.append(key)
                continue
            if key not in before:
                del display_names[key]
                collisions.append(key)
            elif display_names[key] != before[key]:
                display_names[key] = before[key]
                collisions.append(key)
        return collisions

    def _settle_web_directory(
        self,
        web_dirs: dict[str, str],
        before: dict[str, str],
        candidate: PackCandidate,
        undo: UndoRecord,
    ) -> str | None:
        """Publish the pack's web assets under a key the loader chose.

        ``EXTENSION_WEB_DIRS`` keys come from ``WEB_DIRECTORY`` or, worse, from
        ``project.name`` in the pack's own ``pyproject.toml``, which ComfyUI does
        not validate.  A hostile pack could therefore claim an installed pack's
        key and have its JavaScript served in that pack's place.  Anything the
        pack added is dropped and replaced with a single key equal to its
        validated directory name.
        """

        chosen: str | None = None
        for key in list(web_dirs):
            if key in before:
                if web_dirs[key] != before[key]:
                    web_dirs[key] = before[key]
                continue
            directory = web_dirs.pop(key)
            if chosen is None and _within(candidate.path, directory):
                chosen = directory
        if chosen is None:
            return None
        web_dirs[candidate.name] = chosen
        undo.web_dir_keys.append(candidate.name)
        return chosen

    def _serve_pack_http(
        self,
        app: Any,
        candidate: PackCandidate,
        record: LoadedPack,
        captured_routes: list[Any],
        direct_decisions: list[Any],
        undo: UndoRecord,
    ) -> None:
        decisions = pack_http.filter_pack_routes(
            captured_routes, app=app, pack_name=candidate.name
        )
        decisions.extend(direct_decisions)
        record.routes = [d.to_dict() for d in decisions if d.allowed]
        record.refused_routes = [d.to_dict() for d in decisions if not d.allowed]
        try:
            with pack_http.unfrozen_router(app):
                if decisions:
                    undo.route_resources.extend(
                        pack_http.replay_routes(app, captured_routes, decisions)
                    )
                if record.web_directory:
                    undo.route_resources.extend(
                        pack_http.mount_web_directory(
                            app, candidate.name, record.web_directory
                        )
                    )
        except pack_http.PackRouteError as exc:
            record.dirty.append(f"HTTP surface unavailable: {exc}")

    # -- unload --------------------------------------------------------- #

    async def unload(self, pack_id: str) -> LoadedPack:
        """Remove a loaded pack from every registry it was added to."""

        with self._lock:
            record = self._loaded.get(pack_id)
        if record is None:
            raise PackNotFoundError(f"`{pack_id}` is not loaded")

        if not self._transaction.acquire(blocking=False):
            raise PackBusyError("Another node-pack transaction is in progress")
        try:
            self._require_idle()
            prompt_server = _prompt_server()
            self._reverse(record.undo, getattr(prompt_server, "app", None))
        finally:
            self._transaction.release()

        with self._lock:
            self._loaded.pop(pack_id, None)
        _invalidate_execution_cache(record.undo.class_ids)
        self._notify("unloaded", record)
        return record

    async def reload(self, pack_id: str) -> LoadedPack:
        """Unload then load, so edited pack source takes effect.

        This ordering matters: ``load_custom_node`` skips every id already in
        ``NODE_CLASS_MAPPINGS``, so loading over a live pack would leave the
        registry pointing at the previous class objects while still reporting
        success.
        """

        if self.get(pack_id) is not None:
            await self.unload(pack_id)
        return await self.load(pack_id)

    def _reverse(self, undo: UndoRecord, app: Any) -> None:
        nodes = _nodes_module()
        for class_id in undo.class_ids:
            nodes.NODE_CLASS_MAPPINGS.pop(class_id, None)
        for name in undo.display_names_added:
            nodes.NODE_DISPLAY_NAME_MAPPINGS.pop(name, None)
        for key in undo.web_dir_keys:
            nodes.EXTENSION_WEB_DIRS.pop(key, None)
        if undo.loaded_module_dir_key:
            nodes.LOADED_MODULE_DIRS.pop(undo.loaded_module_dir_key, None)

        if app is not None and undo.route_resources:
            pack_http.remove_resources(app, undo.route_resources)
            undo.route_resources.clear()

        folder_paths = _folder_paths()
        folders = getattr(folder_paths, "folder_names_and_paths", None)
        if isinstance(folders, dict):
            for key in undo.folder_path_keys_added:
                folders.pop(key, None)

        retained = {
            entry
            for record in self.loaded_packs()
            if record.undo is not undo
            for entry in record.undo.sys_path_added
        }
        for entry in undo.sys_path_added:
            if entry in retained:
                continue
            while entry in sys.path:
                sys.path.remove(entry)

        for key in undo.sys_modules_added:
            if key == undo.module_key or key.startswith(undo.module_key + "."):
                sys.modules.pop(key, None)

    def _rollback(
        self,
        undo: UndoRecord,
        app: Any,
        before_display: dict[str, Any],
        before_web: dict[str, str],
    ) -> None:
        """Undo a partial load whose pack raised part-way through its import."""

        nodes = _nodes_module()
        nodes.NODE_DISPLAY_NAME_MAPPINGS.clear()
        nodes.NODE_DISPLAY_NAME_MAPPINGS.update(before_display)
        nodes.EXTENSION_WEB_DIRS.clear()
        nodes.EXTENSION_WEB_DIRS.update(before_web)
        self._reverse(undo, app)

    # -- notification --------------------------------------------------- #

    def _notify(self, action: str, record: LoadedPack) -> None:
        try:
            prompt_server = _prompt_server()
        except PackRuntimeError:
            return
        send = getattr(prompt_server, "send_sync", None)
        if not callable(send):
            return
        try:
            send(
                "scripted_nodes.packs_changed",
                {
                    "action": action,
                    "pack": record.to_dict(),
                    "loaded": [item.candidate.pack_id for item in self.loaded_packs()],
                },
            )
        except Exception:
            pass


def _within(root: Path, candidate: str) -> bool:
    try:
        Path(candidate).resolve().relative_to(root.resolve())
    except (OSError, ValueError):
        return False
    return True


_REGISTRY: PackRegistry | None = None
_REGISTRY_LOCK = threading.Lock()


def get_pack_registry() -> PackRegistry:
    """Return the process-wide registry, creating it on first use."""

    global _REGISTRY
    with _REGISTRY_LOCK:
        if _REGISTRY is None:
            _REGISTRY = PackRegistry()
        return _REGISTRY


__all__ = [
    "DISABLED_DIRECTORY_NAME",
    "MAX_PACK_NAME_LENGTH",
    "PACK_MODULE_PARENT",
    "LoadedPack",
    "PackBusyError",
    "PackCandidate",
    "PackConflictError",
    "PackImportError",
    "PackLoaderError",
    "PackNameError",
    "PackNotFoundError",
    "PackRegistry",
    "PackRuntimeError",
    "UndoRecord",
    "discover_packs",
    "find_pack",
    "get_pack_registry",
    "normalize_pack_name",
]
