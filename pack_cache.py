"""Materialize a pinned GitHub node pack into a verified local cache.

``node_pack_tester.fetch_repository`` already performs the hardened part of this
job -- a bare, hooks-free, HTTPS-only partial fetch that resolves a ref to a
40-hex commit without ever checking it out -- but it keeps only ``.py`` blobs in
memory, which cannot be imported and omits the ``web/`` assets and data files a
pack needs.  This module reuses that plumbing and writes a real directory tree.

Two properties matter and are deliberately not obtained with ``git archive``:

* **The tree is the commit's tree.**  ``git archive`` applies in-tree
  ``.gitattributes``: ``export-subst`` substitutes commit metadata (including the
  attacker-controlled commit *message*) into file contents, and ``export-ignore``
  omits files entirely.  A commit id would therefore not identify the bytes that
  end up executing.  Blobs are read with ``cat-file`` and written here instead,
  so the content is exactly what the commit names.
* **The tree stays what was approved.**  Every file's mode and blob id are
  recorded in a manifest at fetch time and re-verified before each load, so a
  later edit to the cache -- by another process, or by a previously loaded pack
  writing into its own directory -- is detected rather than silently executed.

Tree entries that are not ordinary files or directories are refused: mode
``120000`` is a symlink (which could point anywhere on the filesystem) and mode
``160000`` is a submodule, whose contents this fetch cannot supply.
"""

from __future__ import annotations

import hashlib
import json
import os
import shutil
import tempfile
import threading
import time
import unicodedata
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Sequence

try:  # pragma: no cover - exercised implicitly by both import styles
    from . import node_pack_tester as _tester
except ImportError:  # pytest may collect this file as a top-level module
    import node_pack_tester as _tester  # type: ignore[no-redef]


CACHE_DIRECTORY_NAME = "scripted_node_packs"
MANIFEST_NAME = "pack_manifest.json"
MANIFEST_VERSION = 1

MAX_PACK_FILES = 4_000
MAX_PACK_FILE_BYTES = 32 * 1024 * 1024
MAX_PACK_TREE_BYTES = 128 * 1024 * 1024
BLOB_BATCH = 128

# Ordinary file and directory modes.  Anything else is refused by name below.
_FILE_MODES = {b"100644", b"100755"}
_DIRECTORY_MODE = b"040000"
_SYMLINK_MODE = b"120000"
_SUBMODULE_MODE = b"160000"

_CACHE_LOCK = threading.RLock()


class PackCacheError(ValueError):
    """Base error for fetching or verifying a cached pack."""


class PackCacheUnavailableError(PackCacheError):
    """Raised when the cache directory cannot be resolved or created."""


class PackCacheConflictError(PackCacheError):
    """Raised when a cache entry already exists and was not requested."""


class PackCacheVerificationError(PackCacheError):
    """Raised when a cached tree no longer matches its recorded manifest."""


@dataclass(frozen=True)
class CachedPack:
    """One materialized commit of one repository."""

    name: str
    slug: str
    host: str
    owner: str
    repository: str
    commit: str
    requested_ref: str
    subdirectory: str
    path: Path
    file_count: int
    total_bytes: int
    fetched_at: float
    has_submodules: bool
    refused_entries: tuple[str, ...]

    @property
    def pack_id(self) -> str:
        return f"cache:{self.name}"

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.pack_id,
            "name": self.name,
            "slug": self.slug,
            "host": self.host,
            "owner": self.owner,
            "repository": self.repository,
            "commit": self.commit,
            "short_commit": self.commit[:12],
            "requested_ref": self.requested_ref,
            "subdirectory": self.subdirectory,
            "path": os.fspath(self.path),
            "file_count": self.file_count,
            "total_bytes": self.total_bytes,
            "fetched_at": self.fetched_at,
            "has_submodules": self.has_submodules,
            "refused_entries": list(self.refused_entries),
        }


# --------------------------------------------------------------------------- #
# Cache location
# --------------------------------------------------------------------------- #


def cache_root() -> Path:
    """Return the directory holding fetched packs, creating it on demand.

    ComfyUI's user directory is used rather than ``models/``: ``folder_paths``
    scans the model tree, and a pack full of ``.py`` and ``.js`` files would then
    surface in model pickers and ``get_filename_list`` results.
    """

    try:
        import folder_paths
    except ImportError as exc:
        raise PackCacheUnavailableError(
            "ComfyUI's `folder_paths` module is unavailable"
        ) from exc

    get_user_directory = getattr(folder_paths, "get_user_directory", None)
    if not callable(get_user_directory):
        raise PackCacheUnavailableError(
            "This ComfyUI version does not expose a user directory"
        )
    try:
        user_directory = Path(get_user_directory()).expanduser().resolve(strict=True)
    except OSError as exc:
        raise PackCacheUnavailableError(
            "ComfyUI's user directory cannot be resolved"
        ) from exc

    root = user_directory / CACHE_DIRECTORY_NAME
    try:
        root.mkdir(mode=0o700, parents=True, exist_ok=True)
    except OSError as exc:
        raise PackCacheUnavailableError(
            f"The pack cache directory cannot be created: {exc}"
        ) from exc
    return root


def _split_source(source: Any) -> tuple[str, str, str]:
    """Return the sanitised (host, owner, repository) of a fetched source."""

    host, _, slug = source.url.partition("://")[2].partition("/")
    owner, _, repository = slug.removesuffix(".git").partition("/")
    return (
        _safe_component(host),
        _safe_component(owner),
        _safe_component(repository),
    )


def _commit_directory(root: Path, source: Any, commit: str) -> Path:
    host, owner, repository = _split_source(source)
    return root / host / owner / repository / commit


def _safe_component(value: str) -> str:
    """Reduce a URL component to something safe as one directory name."""

    normalized = unicodedata.normalize("NFC", value).strip().strip(".")
    cleaned = "".join(
        character if character.isalnum() or character in "._-" else "_"
        for character in normalized
    )
    cleaned = cleaned.strip(".") or "unknown"
    return cleaned[:64]


# --------------------------------------------------------------------------- #
# Tree parsing
# --------------------------------------------------------------------------- #


@dataclass(frozen=True)
class _TreeEntry:
    path: str
    mode: str
    object_id: str


def _parse_full_tree(
    tree_output: bytes,
    subdirectory: str,
) -> tuple[list[_TreeEntry], list[str], bool]:
    """Return every ordinary file in the tree, plus what was refused."""

    entries = tree_output.split(b"\0")
    if entries and not entries[-1]:
        entries.pop()
    if len(entries) > _tester.MAX_TREE_ENTRIES:
        raise _tester.NodePackTooLargeError(
            f"Repository contains more than {_tester.MAX_TREE_ENTRIES} tree entries"
        )

    prefix = f"{subdirectory}/" if subdirectory else ""
    files: list[_TreeEntry] = []
    refused: list[str] = []
    has_submodules = False
    found_subdirectory = not subdirectory

    for raw_entry in entries:
        try:
            header, raw_path = raw_entry.split(b"\t", 1)
            mode, object_type, object_id = header.split()
            path = raw_path.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise _tester.NodePackScanError(
                "Repository tree contains an unreadable entry"
            ) from exc

        if prefix and not path.startswith(prefix):
            continue
        found_subdirectory = True
        relative = path[len(prefix):] if prefix else path
        if not _safe_relative_path(relative):
            refused.append(relative or path)
            continue

        if mode == _SUBMODULE_MODE or object_type == b"commit":
            has_submodules = True
            refused.append(f"{relative} (submodule)")
            continue
        if mode == _SYMLINK_MODE:
            refused.append(f"{relative} (symlink)")
            continue
        if mode == _DIRECTORY_MODE or object_type == b"tree":
            continue
        if mode not in _FILE_MODES or object_type != b"blob":
            refused.append(f"{relative} (mode {mode.decode('ascii', 'replace')})")
            continue

        files.append(
            _TreeEntry(
                path=relative,
                mode=mode.decode("ascii"),
                object_id=object_id.decode("ascii"),
            )
        )

    if not found_subdirectory:
        raise _tester.SubdirectoryValidationError(
            f"Subdirectory `{subdirectory}` does not exist in the fetched revision"
        )
    if len(files) > MAX_PACK_FILES:
        raise _tester.NodePackTooLargeError(
            f"Pack contains more than {MAX_PACK_FILES} files"
        )
    return files, refused, has_submodules


def _safe_relative_path(relative: str) -> bool:
    """Reject anything that would escape, hide inside or confuse the cache dir."""

    if not relative or relative.startswith("/") or "\\" in relative:
        return False
    if "\x00" in relative:
        return False
    # Split the raw string rather than using PurePosixPath.parts, which silently
    # normalises away a `.` component and would let `a/./b` through.
    parts = relative.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        return False
    if any(part.endswith((" ", ".")) for part in parts):
        # Trailing dots and spaces are silently stripped by Windows.
        return False
    return True


# --------------------------------------------------------------------------- #
# Fetch
# --------------------------------------------------------------------------- #


def fetch_pack(
    repository: str,
    ref_kind: str = "default",
    ref: str = "",
    subdirectory: str = "",
    *,
    name: str | None = None,
    overwrite: bool = False,
) -> CachedPack:
    """Fetch a pinned revision of *repository* into the local pack cache.

    The returned tree is ready for :func:`pack_loader.PackRegistry.load`; nothing
    in the pack has been imported or executed at this point.
    """

    source = _tester.normalize_github_source(repository)
    normalized_kind, requested_ref = _tester.normalize_revision(ref_kind, ref)
    normalized_subdirectory = _tester.normalize_subdirectory(subdirectory)

    git = shutil.which("git")
    if git is None:
        raise _tester.GitUnavailableError("Git is required to fetch a node pack")

    pack_name = _pack_name(source, normalized_subdirectory, name)
    root = cache_root()
    deadline = time.monotonic() + _tester.TOTAL_FETCH_TIMEOUT_SECONDS

    def remaining(command_limit: float) -> float:
        left = deadline - time.monotonic()
        if left <= 0:
            raise _tester.NodePackFetchTimeout(
                "The node-pack fetch exceeded its overall time limit"
            )
        return min(command_limit, left)

    with _CACHE_LOCK, tempfile.TemporaryDirectory(
        prefix="comfy-scripted-pack-"
    ) as temporary:
        temporary_root = Path(temporary)
        repository_path = temporary_root / "repository.git"
        hooks_directory = temporary_root / "empty-hooks"
        template_directory = temporary_root / "empty-template"
        hooks_directory.mkdir(mode=0o700)
        template_directory.mkdir(mode=0o700)

        commit = _fetch_bare(
            git,
            source,
            requested_ref,
            repository_path,
            hooks_directory,
            template_directory,
            remaining,
        )

        destination = _commit_directory(root, source, commit) / pack_name
        if destination.exists():
            if not overwrite:
                try:
                    return read_manifest(destination)
                except PackCacheError:
                    pass
            _remove_tree(destination)

        tree_output = _tester._run_process(
            _tester._git_command(
                git, repository_path, hooks_directory, "ls-tree", "-r", "-z", commit
            ),
            timeout=remaining(_tester.GIT_COMMAND_TIMEOUT_SECONDS),
        )
        files, refused, has_submodules = _parse_full_tree(
            tree_output, normalized_subdirectory
        )
        if not files:
            raise _tester.NodePackScanError(
                "The fetched revision contains no files to materialize"
            )

        staging = temporary_root / "tree"
        staging.mkdir(mode=0o700)
        total_bytes = _write_blobs(
            git, repository_path, hooks_directory, files, staging, remaining
        )

        if not (staging / "__init__.py").is_file():
            raise _tester.NodePackScanError(
                "The fetched revision has no `__init__.py`; it is not a node pack"
                + (
                    " (try setting a subdirectory)"
                    if not normalized_subdirectory
                    else ""
                )
            )

        host, owner, repository = _split_source(source)
        record = CachedPack(
            name=pack_name,
            slug=source.slug,
            host=host,
            owner=owner,
            repository=repository,
            commit=commit,
            requested_ref=(
                "default branch" if normalized_kind == "default" else requested_ref
            ),
            subdirectory=normalized_subdirectory,
            path=destination,
            file_count=len(files),
            total_bytes=total_bytes,
            fetched_at=time.time(),
            has_submodules=has_submodules,
            refused_entries=tuple(refused[:100]),
        )
        _write_manifest(staging, record, files)

        destination.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        shutil.move(os.fspath(staging), os.fspath(destination))
        return record


def _pack_name(source: Any, subdirectory: str, override: str | None) -> str:
    """Name the leaf directory the way a normal install would.

    ``load_custom_node`` derives the module name, ``LOADED_MODULE_DIRS`` key,
    ``EXTENSION_WEB_DIRS`` key and ``/extensions`` URL from this directory's
    basename, and packs hardcode ``extensions/<RepoName>/...`` inside their own
    JavaScript.  Naming the leaf after the commit would break all of it, so the
    commit lives in the parent chain and the leaf keeps the repository name.
    """

    if override is not None:
        candidate = override
    elif subdirectory:
        candidate = PurePosixPath(subdirectory).name
    else:
        candidate = source.slug.partition("/")[2]

    try:
        from . import pack_loader
    except ImportError:  # pragma: no cover - top-level import style
        import pack_loader  # type: ignore[no-redef]
    return pack_loader.normalize_pack_name(candidate)


def _fetch_bare(
    git: str,
    source: Any,
    requested_ref: str,
    repository_path: Path,
    hooks_directory: Path,
    template_directory: Path,
    remaining: Any,
) -> str:
    """Resolve *requested_ref* to a commit in a throwaway bare repository."""

    _tester._run_process(
        [
            git,
            "-c",
            f"init.templateDir={template_directory}",
            "init",
            "--bare",
            "--quiet",
            os.fspath(repository_path),
        ],
        timeout=remaining(_tester.GIT_COMMAND_TIMEOUT_SECONDS),
    )
    _tester._run_process(
        _tester._git_command(
            git, repository_path, hooks_directory, "remote", "add", "origin", source.url
        ),
        timeout=remaining(_tester.GIT_COMMAND_TIMEOUT_SECONDS),
    )
    _tester._run_process(
        _tester._git_command(
            git,
            repository_path,
            hooks_directory,
            "fetch",
            "--quiet",
            "--depth=1",
            "--no-tags",
            "--no-recurse-submodules",
            "--",
            "origin",
            requested_ref,
        ),
        timeout=remaining(_tester.GIT_FETCH_TIMEOUT_SECONDS),
        disk_watch_path=repository_path,
    )
    commit = (
        _tester._run_process(
            _tester._git_command(
                git,
                repository_path,
                hooks_directory,
                "rev-parse",
                "--verify",
                "FETCH_HEAD^{commit}",
            ),
            timeout=remaining(_tester.GIT_COMMAND_TIMEOUT_SECONDS),
        )
        .decode("ascii")
        .strip()
    )
    if not _tester._RESOLVED_COMMIT_PATTERN.fullmatch(commit):
        raise _tester.NodePackScanError("Git returned an invalid resolved commit id")
    return commit


def _write_blobs(
    git: str,
    repository_path: Path,
    hooks_directory: Path,
    files: Sequence[_TreeEntry],
    staging: Path,
    remaining: Any,
) -> int:
    """Write every blob to *staging*, enforcing per-file and total size caps."""

    total = 0
    for offset in range(0, len(files), BLOB_BATCH):
        batch = files[offset: offset + BLOB_BATCH]
        request = "".join(f"{entry.object_id}\n" for entry in batch).encode("ascii")
        output = _tester._run_process(
            _tester._git_command(
                git, repository_path, hooks_directory, "cat-file", "--batch"
            ),
            timeout=remaining(_tester.GIT_FETCH_TIMEOUT_SECONDS),
            input_bytes=request,
            max_output_bytes=MAX_PACK_TREE_BYTES + len(batch) * 128,
            disk_watch_path=repository_path,
        )
        total += _write_batch(output, batch, staging, total)
    return total


def _write_batch(
    output: bytes,
    batch: Sequence[_TreeEntry],
    staging: Path,
    written_so_far: int,
) -> int:
    """Split one ``cat-file --batch`` stream into files on disk."""

    total = 0
    position = 0
    for entry in batch:
        newline = output.find(b"\n", position)
        if newline == -1:
            raise _tester.NodePackScanError("Git returned a truncated blob stream")
        header = output[position:newline].split()
        if len(header) != 3 or header[1] != b"blob":
            raise _tester.NodePackScanError("Git returned invalid blob metadata")
        if header[0].decode("ascii") != entry.object_id:
            raise _tester.NodePackScanError("Git blob metadata did not match the tree")
        try:
            size = int(header[2])
        except ValueError as exc:
            raise _tester.NodePackScanError(
                f"Could not determine the size of `{entry.path}`"
            ) from exc
        if size > MAX_PACK_FILE_BYTES:
            raise _tester.NodePackTooLargeError(
                f"`{entry.path}` exceeds {MAX_PACK_FILE_BYTES} bytes"
            )
        start = newline + 1
        data = output[start: start + size]
        if len(data) != size:
            raise _tester.NodePackScanError("Git returned a truncated blob")
        position = start + size + 1  # trailing newline

        total += size
        if written_so_far + total > MAX_PACK_TREE_BYTES:
            raise _tester.NodePackTooLargeError(
                f"Pack tree exceeds {MAX_PACK_TREE_BYTES} bytes"
            )

        target = staging / entry.path
        target.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
        # O_NOFOLLOW and O_EXCL: nothing should exist here yet, and a component
        # replaced with a link between mkdir and open must not be followed.
        descriptor = os.open(
            target,
            os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0),
            0o755 if entry.mode == "100755" else 0o644,
        )
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
    return total


# --------------------------------------------------------------------------- #
# Manifest
# --------------------------------------------------------------------------- #


def _write_manifest(
    directory: Path, record: CachedPack, files: Sequence[_TreeEntry]
) -> None:
    payload = {
        "version": MANIFEST_VERSION,
        "pack": {
            key: value
            for key, value in record.to_dict().items()
            if key not in {"path", "id"}
        },
        "files": [
            {"path": entry.path, "mode": entry.mode, "blob": entry.object_id}
            for entry in files
        ],
    }
    (directory / MANIFEST_NAME).write_text(
        json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8"
    )


def read_manifest(directory: Path) -> CachedPack:
    """Reconstruct the cache record written beside a materialized tree."""

    try:
        payload = json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackCacheVerificationError(
            f"`{directory.name}` has no readable pack manifest"
        ) from exc
    if not isinstance(payload, dict) or payload.get("version") != MANIFEST_VERSION:
        raise PackCacheVerificationError(
            f"`{directory.name}` has an unsupported pack manifest"
        )
    pack = payload.get("pack")
    if not isinstance(pack, dict):
        raise PackCacheVerificationError(
            f"`{directory.name}` has a malformed pack manifest"
        )
    return CachedPack(
        name=str(pack.get("name", directory.name)),
        slug=str(pack.get("slug", "")),
        host=str(pack.get("host", "")),
        owner=str(pack.get("owner", "")),
        repository=str(pack.get("repository", "")),
        commit=str(pack.get("commit", "")),
        requested_ref=str(pack.get("requested_ref", "")),
        subdirectory=str(pack.get("subdirectory", "")),
        path=directory,
        file_count=int(pack.get("file_count", 0)),
        total_bytes=int(pack.get("total_bytes", 0)),
        fetched_at=float(pack.get("fetched_at", 0.0)),
        has_submodules=bool(pack.get("has_submodules", False)),
        refused_entries=tuple(pack.get("refused_entries", []) or []),
    )


def _git_blob_id(path: Path, algorithm: str = "sha1") -> str:
    """Recompute Git's object id for a file.

    Repositories may use either object format, so the algorithm is chosen from
    the length of the id recorded in the manifest rather than assumed.
    """

    data = path.read_bytes()
    digest = hashlib.new(algorithm)
    digest.update(b"blob %d\0" % len(data))
    digest.update(data)
    return digest.hexdigest()


def verify_pack(directory: Path) -> tuple[list[str], list[str]]:
    """Compare a cached tree against the manifest written when it was fetched.

    Returns ``(changed, added)``.  *changed* lists files the fetched commit named
    that are now missing or no longer hash to the recorded blob id -- that is a
    swap of approved code and callers should refuse to load.  *added* lists files
    that appeared afterwards, which many packs do legitimately by writing their
    own configuration, so it is reported rather than treated as tampering.
    """

    try:
        payload = json.loads((directory / MANIFEST_NAME).read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        raise PackCacheVerificationError(
            f"`{directory.name}` has no readable pack manifest"
        ) from exc

    entries = payload.get("files")
    if not isinstance(entries, list):
        raise PackCacheVerificationError(
            f"`{directory.name}` has a malformed pack manifest"
        )

    differences: list[str] = []
    added: list[str] = []
    recorded: set[str] = set()
    for entry in entries:
        if not isinstance(entry, dict):
            continue
        relative = str(entry.get("path", ""))
        if not _safe_relative_path(relative):
            differences.append(f"{relative}: unsafe path in manifest")
            continue
        recorded.add(relative)
        target = directory / relative
        blob = str(entry.get("blob", ""))
        algorithm = "sha256" if len(blob) == 64 else "sha1"
        try:
            if not target.is_file() or target.is_symlink():
                differences.append(f"{relative}: missing")
                continue
            if _git_blob_id(target, algorithm) != blob:
                differences.append(f"{relative}: modified")
        except OSError as exc:
            differences.append(f"{relative}: unreadable ({exc.strerror})")

    for path in directory.rglob("*"):
        if not path.is_file() or path.is_symlink():
            continue
        relative = path.relative_to(directory).as_posix()
        if relative == MANIFEST_NAME or relative in recorded:
            continue
        if any(part == "__pycache__" for part in path.relative_to(directory).parts):
            continue
        added.append(relative)

    return differences, added


# --------------------------------------------------------------------------- #
# Listing and removal
# --------------------------------------------------------------------------- #


def list_cached_packs() -> list[CachedPack]:
    """Return every materialized pack in the cache, newest fetch first."""

    try:
        root = cache_root()
    except PackCacheUnavailableError:
        return []
    packs: list[CachedPack] = []
    for manifest in root.glob(f"*/*/*/*/*/{MANIFEST_NAME}"):
        try:
            packs.append(read_manifest(manifest.parent))
        except PackCacheError:
            continue
    return sorted(packs, key=lambda pack: pack.fetched_at, reverse=True)


def find_cached_pack(name: str) -> CachedPack | None:
    """Return the most recently fetched cache entry called *name*."""

    for pack in list_cached_packs():
        if pack.name == name:
            return pack
    return None


def _remove_tree(directory: Path) -> None:
    if directory.is_symlink() or not directory.exists():
        return
    shutil.rmtree(directory, ignore_errors=True)


def delete_cached_pack(name: str, commit: str) -> bool:
    """Remove one cached commit; return whether anything was deleted."""

    with _CACHE_LOCK:
        for pack in list_cached_packs():
            if pack.name == name and pack.commit == commit:
                _remove_tree(pack.path)
                # Drop the now-empty <commit>/ parent as well.
                try:
                    pack.path.parent.rmdir()
                except OSError:
                    pass
                return True
    return False


__all__ = [
    "CACHE_DIRECTORY_NAME",
    "MANIFEST_NAME",
    "MAX_PACK_FILES",
    "MAX_PACK_FILE_BYTES",
    "MAX_PACK_TREE_BYTES",
    "CachedPack",
    "PackCacheConflictError",
    "PackCacheError",
    "PackCacheUnavailableError",
    "PackCacheVerificationError",
    "cache_root",
    "delete_cached_pack",
    "fetch_pack",
    "find_cached_pack",
    "list_cached_packs",
    "read_manifest",
    "verify_pack",
]
