"""Static compatibility tester for GitHub-hosted ComfyUI node packs.

The tester deliberately never imports repository Python. Git fetches into a
temporary bare repository, regular Python blobs are read directly from the
object database, and compatibility is estimated from the abstract syntax tree.
"""

from __future__ import annotations

import ast
import asyncio
import io
import json
import keyword
import os
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import tokenize
from collections import defaultdict
from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any
from urllib.parse import unquote, urlsplit


NODE_PACK_TEST_ROUTE = "/scripted_nodes/node-packs/test"
MAX_REPOSITORY_INPUT_LENGTH = 512
MAX_REF_LENGTH = 240
MAX_SUBDIRECTORY_LENGTH = 320
MAX_TREE_ENTRIES = 50_000
MAX_PYTHON_FILES = 1_000
MAX_SOURCE_FILE_BYTES = 2 * 1024 * 1024
MAX_TOTAL_SOURCE_BYTES = 16 * 1024 * 1024
MAX_GIT_OUTPUT_BYTES = 40 * 1024 * 1024
MAX_TEMPORARY_REPOSITORY_BYTES = 256 * 1024 * 1024
MAX_CONCURRENT_SCANS = 2
GIT_FETCH_TIMEOUT_SECONDS = 60
GIT_COMMAND_TIMEOUT_SECONDS = 20
TOTAL_FETCH_TIMEOUT_SECONDS = 120
PROCESS_READER_JOIN_TIMEOUT_SECONDS = 1
MAX_ADAPTER_OUTPUTS = 32
MAX_REPORTED_NODES = 1_000
MAX_REASONS_PER_NODE = 24
MAX_REASON_CHARACTERS = 500
MAX_REPORT_TEXT_CHARACTERS = 200_000
MAX_STRUCTURED_NODE_CHARACTERS = 3_000_000
MAX_RESULT_FIELD_CHARACTERS = 600
MAX_REPORTED_FILE_PATHS = 200
MAX_SOURCE_TOKENS_PER_FILE = 50_000
MAX_TOTAL_SOURCE_TOKENS = 250_000
MAX_AST_NODES_PER_FILE = 100_000
MAX_TOTAL_AST_NODES = 250_000
MAX_DISCOVERED_CLASSES = 1_000
MAX_DISCOVERED_MAPPINGS = 1_000
MAX_ANALYZED_RESULTS = 2_000
GIT_BLOB_SIZE_BATCH = 128
SUPPORTED_REF_KINDS = ("default", "branch", "tag", "commit")
RESERVED_ADAPTER_INPUTS = {"code", "schema_json"}
SKIPPED_DIRECTORIES = {
    "__pycache__",
    ".git",
    ".github",
    ".venv",
    "docs",
    "env",
    "examples",
    "node_modules",
    "site-packages",
    "test",
    "tests",
    "venv",
}
_OWNER_PATTERN = re.compile(r"[A-Za-z0-9](?:[A-Za-z0-9-]{0,38})\Z")
_REPOSITORY_PATTERN = re.compile(r"[A-Za-z0-9._-]{1,100}\Z")
_COMMIT_PATTERN = re.compile(r"(?:[0-9a-fA-F]{40}|[0-9a-fA-F]{64})\Z")
_RESOLVED_COMMIT_PATTERN = re.compile(r"(?:[0-9a-f]{40}|[0-9a-f]{64})\Z")
_REF_FORBIDDEN = set(" ~^:?*[\\")
_IS_WINDOWS = sys.platform == "win32"


class NodePackTestError(ValueError):
    """Base error with a stable REST code and status."""

    code = "node_pack_test_error"
    status = 400


class RepositoryValidationError(NodePackTestError):
    code = "invalid_repository"
    status = 400


class RevisionValidationError(NodePackTestError):
    code = "invalid_revision"
    status = 400


class SubdirectoryValidationError(NodePackTestError):
    code = "invalid_subdirectory"
    status = 400


class GitUnavailableError(NodePackTestError):
    code = "git_not_available"
    status = 503


class NodePackFetchError(NodePackTestError):
    code = "fetch_failed"
    status = 502


class NodePackFetchTimeout(NodePackFetchError):
    code = "fetch_timeout"
    status = 504


class NodePackTooLargeError(NodePackTestError):
    code = "repository_too_large"
    status = 413


class NodePackBusyError(NodePackTestError):
    code = "scanner_busy"
    status = 429


class NodePackScanError(NodePackTestError):
    code = "scan_failed"
    status = 422


@dataclass(frozen=True)
class GitHubSource:
    slug: str
    url: str


@dataclass(frozen=True)
class FetchedNodePack:
    source: GitHubSource
    requested_ref: str
    resolved_commit: str
    subdirectory: str
    python_sources: dict[str, str]
    metadata_files: tuple[str, ...]
    skipped_files: tuple[str, ...]


@dataclass
class _ClassInfo:
    name: str
    path: str
    line: int
    node: ast.ClassDef
    assignments: dict[str, ast.AST]
    methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef]
    bases: tuple[str, ...]
    resolved_bases: tuple[_ClassInfo, ...] = ()
    has_unresolved_base: bool = False
    decorator_bindings_uncertain: bool = False
    dynamic_class_body: bool = False
    class_body_terminates: bool = False
    conditional: bool = False


@dataclass(frozen=True)
class _MappingEntry:
    class_id: str
    class_reference: str
    path: str
    line: int
    mapped: bool = True
    conditional: bool = False
    reachability_unverified: bool = False


@dataclass(frozen=True)
class _ImportAlias:
    target: str
    module: str
    level: int
    conditional: bool
    line: int


@dataclass(frozen=True)
class _InputSchema:
    required: frozenset[str]
    optional: frozenset[str]


@dataclass
class _FileInfo:
    path: str
    tree: ast.Module
    classes: dict[str, _ClassInfo]
    imports: dict[str, list[_ImportAlias]]
    other_bindings: dict[str, list[int]]
    wildcard_import_lines: list[int]


_SCAN_SEMAPHORE = threading.BoundedSemaphore(MAX_CONCURRENT_SCANS)


def normalize_github_source(value: str) -> GitHubSource:
    """Validate and canonicalize a public ``github.com`` repository."""

    if not isinstance(value, str):
        raise RepositoryValidationError("Repository must be a string")
    raw = value.strip()
    if not raw:
        raise RepositoryValidationError("Repository cannot be empty")
    if len(raw) > MAX_REPOSITORY_INPUT_LENGTH:
        raise RepositoryValidationError(
            f"Repository cannot exceed {MAX_REPOSITORY_INPUT_LENGTH} characters"
        )
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise RepositoryValidationError("Repository cannot contain control characters")

    if "://" in raw:
        parsed = urlsplit(raw)
        if parsed.scheme != "https" or parsed.hostname != "github.com":
            raise RepositoryValidationError(
                "Only HTTPS repositories on github.com are supported"
            )
        try:
            port = parsed.port
        except ValueError as exc:
            raise RepositoryValidationError(
                "Repository URL contains an invalid port"
            ) from exc
        if (
            parsed.username is not None
            or parsed.password is not None
            or port is not None
            or parsed.query
            or parsed.fragment
        ):
            raise RepositoryValidationError(
                "Repository URLs cannot contain credentials, ports, queries, or fragments"
            )
        decoded_path = unquote(parsed.path)
        if decoded_path != parsed.path:
            raise RepositoryValidationError(
                "Percent-encoded repository paths are not supported"
            )
        raw_path = decoded_path.strip("/")
    else:
        if raw.startswith(("git@", "ssh:", "file:", "git:")):
            raise RepositoryValidationError(
                "Only HTTPS repositories on github.com are supported"
            )
        raw_path = raw.strip("/")

    parts = raw_path.split("/")
    if len(parts) != 2:
        raise RepositoryValidationError(
            "Repository must be `owner/repository` or an HTTPS GitHub URL"
        )
    owner, repository = parts
    if repository.endswith(".git"):
        repository = repository[:-4]
    if not _OWNER_PATTERN.fullmatch(owner):
        raise RepositoryValidationError("GitHub owner name is invalid")
    if (
        not _REPOSITORY_PATTERN.fullmatch(repository)
        or repository in {".", ".."}
        or repository.startswith(".")
    ):
        raise RepositoryValidationError("GitHub repository name is invalid")

    slug = f"{owner}/{repository}"
    return GitHubSource(slug=slug, url=f"https://github.com/{slug}.git")


def normalize_revision(ref_kind: str, ref: str) -> tuple[str, str]:
    """Return the canonical ref kind and fully-qualified fetch ref."""

    if not isinstance(ref_kind, str) or ref_kind not in SUPPORTED_REF_KINDS:
        raise RevisionValidationError(
            "`ref_kind` must be default, branch, tag, or commit"
        )
    if not isinstance(ref, str):
        raise RevisionValidationError("Revision must be a string")
    value = ref.strip()
    if len(value) > MAX_REF_LENGTH:
        raise RevisionValidationError(
            f"Revision cannot exceed {MAX_REF_LENGTH} characters"
        )

    if ref_kind == "default":
        if value:
            raise RevisionValidationError(
                "Revision must be empty when `ref_kind` is default"
            )
        return ref_kind, "HEAD"
    if not value:
        raise RevisionValidationError(f"A {ref_kind} name is required")
    if ref_kind == "commit":
        if not _COMMIT_PATTERN.fullmatch(value):
            raise RevisionValidationError(
                "A commit revision must be a full 40- or 64-character hexadecimal id"
            )
        return ref_kind, value.lower()

    if (
        value == "@"
        or value.startswith(("-", "/", "."))
        or value.endswith(("/", "."))
        or any(character in _REF_FORBIDDEN for character in value)
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
        or ".." in value
        or "//" in value
        or "@{" in value
        or any(
            component.startswith(".") or component.endswith(".lock")
            for component in value.split("/")
        )
    ):
        raise RevisionValidationError(f"{ref_kind.title()} name is unsafe")
    prefix = "refs/heads" if ref_kind == "branch" else "refs/tags"
    return ref_kind, f"{prefix}/{value}"


def normalize_subdirectory(value: str) -> str:
    """Validate an optional POSIX-style relative pack subdirectory."""

    if not isinstance(value, str):
        raise SubdirectoryValidationError("Subdirectory must be a string")
    stripped = value.strip()
    if not stripped or stripped == ".":
        return ""
    if stripped.startswith("/"):
        raise SubdirectoryValidationError("Subdirectory must be relative")
    raw = stripped.rstrip("/")
    if len(raw) > MAX_SUBDIRECTORY_LENGTH:
        raise SubdirectoryValidationError(
            f"Subdirectory cannot exceed {MAX_SUBDIRECTORY_LENGTH} characters"
        )
    if "\\" in raw or ":" in raw:
        raise SubdirectoryValidationError("Subdirectory must use `/` separators")
    if any(ord(character) < 32 or ord(character) == 127 for character in raw):
        raise SubdirectoryValidationError(
            "Subdirectory cannot contain control characters"
        )
    parts = raw.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise SubdirectoryValidationError(
            "Subdirectory cannot contain empty, `.` or `..` components"
        )
    return "/".join(parts)


def _git_environment() -> dict[str, str]:
    environment = {
        key: value for key, value in os.environ.items() if not key.startswith("GIT_")
    }
    environment.update(
        {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": os.devnull,
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_LFS_SKIP_SMUDGE": "1",
        }
    )
    return environment


def _run_process(
    arguments: Sequence[str],
    *,
    timeout: float,
    input_bytes: bytes | None = None,
    max_output_bytes: int = MAX_GIT_OUTPUT_BYTES,
    disk_watch_path: Path | None = None,
    max_disk_bytes: int = MAX_TEMPORARY_REPOSITORY_BYTES,
) -> bytes:
    """Run a bounded Git subprocess and terminate its process group on timeout."""

    process = subprocess.Popen(
        list(arguments),
        stdin=subprocess.PIPE if input_bytes is not None else subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        shell=False,
        env=_git_environment(),
        start_new_session=not _IS_WINDOWS,
        creationflags=(
            getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0) if _IS_WINDOWS else 0
        ),
    )
    assert process.stdout is not None
    assert process.stderr is not None
    stdout_chunks: list[bytes] = []
    stderr_chunks: list[bytes] = []
    output_exceeded = threading.Event()

    def read_bounded(
        stream: Any,
        chunks: list[bytes],
        limit: int,
    ) -> None:
        retained = 0
        while True:
            chunk = stream.read(64 * 1024)
            if not chunk:
                break
            remaining = max(0, limit - retained)
            if remaining:
                chunks.append(chunk[:remaining])
                retained += min(len(chunk), remaining)
            if len(chunk) > remaining:
                output_exceeded.set()

    stdout_reader = threading.Thread(
        target=read_bounded,
        args=(process.stdout, stdout_chunks, max_output_bytes),
        daemon=True,
    )
    stderr_reader = threading.Thread(
        target=read_bounded,
        args=(process.stderr, stderr_chunks, 64 * 1024),
        daemon=True,
    )
    stdout_reader.start()
    stderr_reader.start()

    if input_bytes is not None and process.stdin is not None:
        try:
            process.stdin.write(input_bytes)
            process.stdin.close()
        except BrokenPipeError:
            pass

    deadline = time.monotonic() + timeout
    next_disk_check = time.monotonic()
    timed_out = False
    disk_limit_exceeded = False
    while process.poll() is None:
        if output_exceeded.is_set():
            _terminate_process(process)
            break
        now = time.monotonic()
        if disk_watch_path is not None and now >= next_disk_check:
            if _directory_size(disk_watch_path) > max_disk_bytes:
                disk_limit_exceeded = True
                _terminate_process(process)
                break
            next_disk_check = now + 0.05
        if now >= deadline:
            timed_out = True
            _terminate_process(process)
            break
        time.sleep(0.01)
    process.wait()
    reader_deadline = time.monotonic() + PROCESS_READER_JOIN_TIMEOUT_SECONDS
    for reader in (stdout_reader, stderr_reader):
        reader.join(max(0.0, reader_deadline - time.monotonic()))
    reader_incomplete = stdout_reader.is_alive() or stderr_reader.is_alive()
    stdout = b"".join(stdout_chunks)
    stderr = b"".join(stderr_chunks)

    if timed_out:
        raise NodePackFetchTimeout(
            "The repository did not respond before the fetch timeout"
        )

    if output_exceeded.is_set():
        raise NodePackTooLargeError(
            "Git produced more metadata than the tester can safely inspect"
        )
    if disk_watch_path is not None and (
        disk_limit_exceeded or _directory_size(disk_watch_path) > max_disk_bytes
    ):
        raise NodePackTooLargeError(
            "Temporary Git data exceeded the repository safety limit"
        )
    if reader_incomplete:
        raise NodePackScanError("Git output could not be read within the safety limit")
    if process.returncode != 0:
        diagnostic = stderr.decode("utf-8", errors="replace").lower()
        if (
            "repository not found" in diagnostic
            or "authentication failed" in diagnostic
            or "could not read username" in diagnostic
        ):
            message = (
                "Repository was not found or is private. "
                "The static tester currently supports public GitHub repositories."
            )
        elif "couldn't find remote ref" in diagnostic:
            message = "The requested branch, tag, or commit was not found"
        else:
            message = "GitHub repository fetch failed"
        raise NodePackFetchError(message)
    return stdout


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    if process.poll() is not None:
        return
    if not _IS_WINDOWS:
        try:
            os.killpg(process.pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    else:
        try:
            subprocess.run(
                [
                    "taskkill",
                    "/PID",
                    str(process.pid),
                    "/T",
                    "/F",
                ],
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                check=False,
                timeout=2,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
            )
        except (OSError, subprocess.SubprocessError):
            pass
        if process.poll() is None:
            process.kill()


def _directory_size(root: Path) -> int:
    total = 0
    for directory, _, filenames in os.walk(root, followlinks=False):
        for filename in filenames:
            path = Path(directory) / filename
            try:
                total += path.stat(follow_symlinks=False).st_size
            except OSError:
                continue
            if total > MAX_TEMPORARY_REPOSITORY_BYTES:
                return total
    return total


def _git_command(
    git: str,
    repository: Path,
    hooks_directory: Path,
    *arguments: str,
) -> list[str]:
    return [
        git,
        "-C",
        os.fspath(repository),
        "-c",
        "protocol.allow=never",
        "-c",
        "protocol.https.allow=always",
        "-c",
        "credential.helper=",
        "-c",
        f"core.hooksPath={hooks_directory}",
        "-c",
        "fetch.recurseSubmodules=false",
        "-c",
        "gc.auto=0",
        *arguments,
    ]


def _parse_tree(
    tree_output: bytes,
    subdirectory: str,
) -> tuple[list[tuple[str, str]], tuple[str, ...]]:
    entries = tree_output.split(b"\0")
    if entries and not entries[-1]:
        entries.pop()
    if len(entries) > MAX_TREE_ENTRIES:
        raise NodePackTooLargeError(
            f"Repository contains more than {MAX_TREE_ENTRIES} tree entries"
        )

    prefix = f"{subdirectory}/" if subdirectory else ""
    candidates: list[tuple[str, str]] = []
    metadata: list[str] = []
    found_subdirectory = not subdirectory
    for raw_entry in entries:
        try:
            header, raw_path = raw_entry.split(b"\t", 1)
            mode, object_type, object_id = header.split()
            path = raw_path.decode("utf-8")
        except (ValueError, UnicodeDecodeError) as exc:
            raise NodePackScanError(
                "Repository tree contains an unreadable entry"
            ) from exc
        if prefix and not path.startswith(prefix):
            continue
        found_subdirectory = True
        relative = path[len(prefix) :] if prefix else path
        pure_path = PurePosixPath(relative)
        if (
            not relative
            or pure_path.is_absolute()
            or any(part in {"", ".", ".."} for part in pure_path.parts)
        ):
            continue
        if any(part in SKIPPED_DIRECTORIES for part in pure_path.parts[:-1]):
            continue
        lower_name = pure_path.name.lower()
        if lower_name.startswith("requirements") or lower_name in {
            "install.py",
            "pyproject.toml",
            "setup.cfg",
            "setup.py",
        }:
            metadata.append(relative)
        if (
            mode not in {b"100644", b"100755"}
            or object_type != b"blob"
            or pure_path.suffix.lower() != ".py"
        ):
            continue
        candidates.append((relative, object_id.decode("ascii")))

    if not found_subdirectory:
        raise SubdirectoryValidationError(
            f"Subdirectory `{subdirectory}` does not exist in the fetched revision"
        )
    if len(candidates) > MAX_PYTHON_FILES:
        raise NodePackTooLargeError(
            f"Pack contains more than {MAX_PYTHON_FILES} Python files"
        )
    return candidates, tuple(sorted(set(metadata)))


def _parse_blob_sizes(
    size_output: bytes,
    candidates: Sequence[tuple[str, str]],
) -> list[tuple[str, str, int]]:
    lines = size_output.splitlines()
    if len(lines) != len(candidates):
        raise NodePackScanError("Git returned incomplete Python blob sizes")
    sized: list[tuple[str, str, int]] = []
    total_size = 0
    for line, (path, expected_id) in zip(lines, candidates):
        fields = line.split()
        if len(fields) != 3:
            raise NodePackScanError("Git returned invalid Python blob metadata")
        object_id, object_type, raw_size = fields
        try:
            size = int(raw_size)
        except ValueError as exc:
            raise NodePackScanError(
                f"Could not determine the size of `{path}`"
            ) from exc
        if object_id.decode("ascii") != expected_id or object_type != b"blob":
            raise NodePackScanError("Git blob metadata did not match the tree")
        if size > MAX_SOURCE_FILE_BYTES:
            raise NodePackTooLargeError(
                f"Python file `{path}` exceeds {MAX_SOURCE_FILE_BYTES} bytes"
            )
        total_size += size
        sized.append((path, expected_id, size))
    if total_size > MAX_TOTAL_SOURCE_BYTES:
        raise NodePackTooLargeError(
            f"Python source exceeds {MAX_TOTAL_SOURCE_BYTES} bytes in total"
        )
    return sized


def _decode_batch_blobs(
    output: bytes,
    candidates: Sequence[tuple[str, str, int]],
) -> dict[str, bytes]:
    sources: dict[str, bytes] = {}
    offset = 0
    for path, expected_id, expected_size in candidates:
        header_end = output.find(b"\n", offset)
        if header_end < 0:
            raise NodePackScanError("Git returned an incomplete Python blob")
        header = output[offset:header_end].split()
        if len(header) != 3:
            raise NodePackScanError("Git returned invalid blob metadata")
        object_id, object_type, raw_size = header
        try:
            size = int(raw_size)
        except ValueError as exc:
            raise NodePackScanError("Git returned an invalid blob size") from exc
        if (
            object_id.decode("ascii") != expected_id
            or object_type != b"blob"
            or size != expected_size
        ):
            raise NodePackScanError("Git blob metadata did not match the tree")
        data_start = header_end + 1
        data_end = data_start + size
        if data_end >= len(output) or output[data_end : data_end + 1] != b"\n":
            raise NodePackScanError("Git returned a truncated Python blob")
        sources[path] = output[data_start:data_end]
        offset = data_end + 1
    if output[offset:]:
        raise NodePackScanError("Git returned unexpected data after Python blobs")
    return sources


def _decode_python_source(path: str, data: bytes) -> str:
    try:
        encoding, _ = tokenize.detect_encoding(io.BytesIO(data).readline)
        return data.decode(encoding)
    except (LookupError, SyntaxError, UnicodeDecodeError) as exc:
        raise NodePackScanError(
            f"Python file `{path}` is not valid encoded source"
        ) from exc


def fetch_repository(
    repository: str,
    ref_kind: str = "default",
    ref: str = "",
    subdirectory: str = "",
) -> FetchedNodePack:
    """Fetch a GitHub revision without checkout and return bounded source blobs."""

    source = normalize_github_source(repository)
    normalized_kind, requested_ref = normalize_revision(ref_kind, ref)
    normalized_subdirectory = normalize_subdirectory(subdirectory)
    git = shutil.which("git")
    if git is None:
        raise GitUnavailableError("Git is required to test a node pack")
    fetch_deadline = time.monotonic() + TOTAL_FETCH_TIMEOUT_SECONDS

    def remaining_timeout(command_limit: float) -> float:
        remaining = fetch_deadline - time.monotonic()
        if remaining <= 0:
            raise NodePackFetchTimeout(
                "The node-pack fetch exceeded its overall time limit"
            )
        return min(command_limit, remaining)

    with tempfile.TemporaryDirectory(prefix="comfy-scripted-pack-") as temporary:
        temporary_root = Path(temporary)
        repository_path = temporary_root / "repository.git"
        hooks_directory = temporary_root / "empty-hooks"
        template_directory = temporary_root / "empty-template"
        hooks_directory.mkdir(mode=0o700)
        template_directory.mkdir(mode=0o700)

        _run_process(
            [
                git,
                "-c",
                f"init.templateDir={template_directory}",
                "init",
                "--bare",
                "--quiet",
                os.fspath(repository_path),
            ],
            timeout=remaining_timeout(GIT_COMMAND_TIMEOUT_SECONDS),
        )
        _run_process(
            _git_command(
                git,
                repository_path,
                hooks_directory,
                "remote",
                "add",
                "origin",
                source.url,
            ),
            timeout=remaining_timeout(GIT_COMMAND_TIMEOUT_SECONDS),
        )
        for key, value in (
            ("remote.origin.promisor", "true"),
            ("remote.origin.partialclonefilter", "blob:none"),
        ):
            _run_process(
                _git_command(
                    git,
                    repository_path,
                    hooks_directory,
                    "config",
                    key,
                    value,
                ),
                timeout=remaining_timeout(GIT_COMMAND_TIMEOUT_SECONDS),
            )
        _run_process(
            _git_command(
                git,
                repository_path,
                hooks_directory,
                "fetch",
                "--quiet",
                "--depth=1",
                "--no-tags",
                "--no-recurse-submodules",
                "--filter=blob:none",
                "--",
                "origin",
                requested_ref,
            ),
            timeout=remaining_timeout(GIT_FETCH_TIMEOUT_SECONDS),
            disk_watch_path=repository_path,
        )
        if _directory_size(repository_path) > MAX_TEMPORARY_REPOSITORY_BYTES:
            raise NodePackTooLargeError(
                "Temporary Git data exceeded the repository safety limit"
            )
        resolved_commit = (
            _run_process(
                _git_command(
                    git,
                    repository_path,
                    hooks_directory,
                    "rev-parse",
                    "--verify",
                    "FETCH_HEAD^{commit}",
                ),
                timeout=remaining_timeout(GIT_COMMAND_TIMEOUT_SECONDS),
            )
            .decode("ascii")
            .strip()
        )
        if not _RESOLVED_COMMIT_PATTERN.fullmatch(resolved_commit):
            raise NodePackScanError("Git returned an invalid resolved commit id")

        tree_output = _run_process(
            _git_command(
                git,
                repository_path,
                hooks_directory,
                "ls-tree",
                "-r",
                "-z",
                "FETCH_HEAD",
            ),
            timeout=remaining_timeout(GIT_COMMAND_TIMEOUT_SECONDS),
        )
        candidates, metadata_files = _parse_tree(
            tree_output,
            normalized_subdirectory,
        )
        if candidates:
            sized_candidates: list[tuple[str, str, int]] = []
            total_source_size = 0
            for offset in range(0, len(candidates), GIT_BLOB_SIZE_BATCH):
                candidate_batch = candidates[offset : offset + GIT_BLOB_SIZE_BATCH]
                batch_input = "".join(
                    f"{object_id}\n" for _, object_id in candidate_batch
                ).encode("ascii")
                size_output = _run_process(
                    _git_command(
                        git,
                        repository_path,
                        hooks_directory,
                        "cat-file",
                        "--batch-check=%(objectname) %(objecttype) %(objectsize)",
                    ),
                    timeout=remaining_timeout(GIT_FETCH_TIMEOUT_SECONDS),
                    input_bytes=batch_input,
                    max_output_bytes=len(candidate_batch) * 128,
                    disk_watch_path=repository_path,
                )
                sized_batch = _parse_blob_sizes(
                    size_output,
                    candidate_batch,
                )
                total_source_size += sum(size for _, _, size in sized_batch)
                if total_source_size > MAX_TOTAL_SOURCE_BYTES:
                    raise NodePackTooLargeError(
                        f"Python source exceeds {MAX_TOTAL_SOURCE_BYTES} bytes in total"
                    )
                sized_candidates.extend(sized_batch)
            object_input = "".join(
                f"{object_id}\n" for _, object_id, _ in sized_candidates
            ).encode("ascii")
            blob_output = _run_process(
                _git_command(
                    git,
                    repository_path,
                    hooks_directory,
                    "cat-file",
                    "--batch",
                ),
                timeout=remaining_timeout(GIT_FETCH_TIMEOUT_SECONDS),
                input_bytes=object_input,
                max_output_bytes=MAX_TOTAL_SOURCE_BYTES + MAX_PYTHON_FILES * 128,
                disk_watch_path=repository_path,
            )
            raw_sources = _decode_batch_blobs(
                blob_output,
                sized_candidates,
            )
            if _directory_size(repository_path) > MAX_TEMPORARY_REPOSITORY_BYTES:
                raise NodePackTooLargeError(
                    "Temporary Git data exceeded the repository safety limit"
                )
        else:
            raw_sources = {}

        decoded_sources: dict[str, str] = {}
        skipped_files: list[str] = []
        for path, data in raw_sources.items():
            try:
                decoded_sources[path] = _decode_python_source(path, data)
            except NodePackScanError:
                skipped_files.append(path)

        return FetchedNodePack(
            source=source,
            requested_ref=(
                "default branch" if normalized_kind == "default" else requested_ref
            ),
            resolved_commit=resolved_commit,
            subdirectory=normalized_subdirectory,
            python_sources=decoded_sources,
            metadata_files=metadata_files,
            skipped_files=tuple(sorted(skipped_files)),
        )


def _expression_name(expression: ast.AST) -> str | None:
    if isinstance(expression, ast.Name):
        return expression.id
    if isinstance(expression, ast.Attribute):
        parent = _expression_name(expression.value)
        return f"{parent}.{expression.attr}" if parent else expression.attr
    return None


def _assignment_target_name(statement: ast.AST) -> str | None:
    if isinstance(statement, ast.Assign):
        for target in statement.targets:
            if isinstance(target, ast.Name):
                return target.id
    if isinstance(statement, ast.AnnAssign) and isinstance(statement.target, ast.Name):
        return statement.target.id
    return None


def _binding_target_names(target: ast.AST | None) -> list[str]:
    if isinstance(target, ast.Name):
        return [target.id]
    if isinstance(target, ast.Starred):
        return _binding_target_names(target.value)
    if isinstance(target, (ast.Tuple, ast.List)):
        return [
            name for element in target.elts for name in _binding_target_names(element)
        ]
    return []


def _assignment_value(statement: ast.AST) -> ast.AST | None:
    if isinstance(statement, ast.Assign):
        return statement.value
    if isinstance(statement, ast.AnnAssign):
        return statement.value
    return None


def _literal_string(expression: ast.AST | None) -> str | None:
    if isinstance(expression, ast.Constant) and isinstance(expression.value, str):
        return expression.value
    return None


def _literal_value(expression: ast.AST | None) -> Any:
    if expression is None:
        return None
    try:
        return ast.literal_eval(expression)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return None


def _literal_value_known(expression: ast.AST | None) -> tuple[bool, Any]:
    if expression is None:
        return False, None
    try:
        return True, ast.literal_eval(expression)
    except (ValueError, TypeError, SyntaxError, MemoryError, RecursionError):
        return False, None


def _literal_truth(expression: ast.AST | None) -> bool | None:
    known, value = _literal_value_known(expression)
    if not known:
        return None
    return bool(value)


def _statement_guaranteed_to_raise(statement: ast.stmt) -> bool:
    if isinstance(statement, ast.Raise):
        return True
    if isinstance(statement, ast.Assert):
        return _literal_truth(statement.test) is False
    if not isinstance(statement, ast.If):
        return False
    truth = _literal_truth(statement.test)
    if truth is True:
        return _sequence_guaranteed_to_raise(statement.body)
    if truth is False:
        return _sequence_guaranteed_to_raise(statement.orelse)
    return (
        bool(statement.orelse)
        and _sequence_guaranteed_to_raise(statement.body)
        and _sequence_guaranteed_to_raise(statement.orelse)
    )


def _sequence_guaranteed_to_raise(statements: Sequence[ast.stmt]) -> bool:
    return any(_statement_guaranteed_to_raise(statement) for statement in statements)


def _class_info(
    path: str,
    node: ast.ClassDef,
    *,
    conditional: bool = False,
) -> _ClassInfo:
    assignments: dict[str, ast.AST] = {}
    methods: dict[str, ast.FunctionDef | ast.AsyncFunctionDef] = {}
    dynamic_class_body = False
    class_bound_names: set[str] = set()
    decorator_bindings_uncertain = False
    for statement in node.body:
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            if any(
                isinstance(decorator, ast.Name)
                and decorator.id in {"classmethod", "staticmethod"}
                and decorator.id in class_bound_names
                for decorator in statement.decorator_list
            ):
                decorator_bindings_uncertain = True
            methods[statement.name] = statement
            assignments.pop(statement.name, None)
            class_bound_names.add(statement.name)
            continue
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            value = _assignment_value(statement)
            targets = (
                statement.targets
                if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            names = [
                name for target in targets for name in _binding_target_names(target)
            ]
            if value is not None:
                for name in names:
                    assignments[name] = value
                    methods.pop(name, None)
                    class_bound_names.add(name)
            if any(not isinstance(target, ast.Name) for target in targets):
                dynamic_class_body = True
            continue
        if isinstance(statement, ast.AugAssign):
            names = _binding_target_names(statement.target)
            for name in names:
                assignments[name] = statement
                methods.pop(name, None)
                class_bound_names.add(name)
            dynamic_class_body = True
            continue
        if isinstance(statement, ast.Delete):
            for target in statement.targets:
                for name in _binding_target_names(target):
                    assignments.pop(name, None)
                    methods.pop(name, None)
                    class_bound_names.discard(name)
            dynamic_class_body = True
            continue
        if isinstance(statement, (ast.Pass, ast.Raise)):
            continue
        if (
            isinstance(statement, ast.Expr)
            and isinstance(statement.value, ast.Constant)
            and isinstance(statement.value.value, str)
        ):
            continue
        dynamic_class_body = True
    resolved_base_names = tuple(
        name for base in node.bases if (name := _expression_name(base)) is not None
    )
    return _ClassInfo(
        name=node.name,
        path=path,
        line=node.lineno,
        node=node,
        assignments=assignments,
        methods=methods,
        bases=resolved_base_names,
        has_unresolved_base=len(resolved_base_names) != len(node.bases),
        decorator_bindings_uncertain=decorator_bindings_uncertain,
        dynamic_class_body=dynamic_class_body,
        class_body_terminates=_sequence_guaranteed_to_raise(node.body),
        conditional=conditional,
    )


def _iter_module_statements_with_context(
    statements: Sequence[ast.stmt],
    *,
    conditional: bool = False,
) -> list[tuple[ast.stmt, bool]]:
    """Flatten module control-flow without entering classes or functions."""

    flattened: list[tuple[ast.stmt, bool]] = []
    for statement in statements:
        flattened.append((statement, conditional))
        nested_groups: list[tuple[Sequence[ast.stmt], bool]] = []
        if isinstance(statement, ast.If):
            literal_test = _literal_truth(statement.test)
            if literal_test is True:
                nested_groups.append((statement.body, conditional))
            elif literal_test is False:
                nested_groups.append((statement.orelse, conditional))
            else:
                nested_groups.extend(
                    (
                        (statement.body, True),
                        (statement.orelse, True),
                    )
                )
        elif isinstance(statement, (ast.For, ast.AsyncFor, ast.While)):
            nested_groups.extend(
                (
                    (statement.body, True),
                    (statement.orelse, True),
                )
            )
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            nested_groups.append((statement.body, True))
        elif isinstance(
            statement,
            (ast.Try, getattr(ast, "TryStar", ast.Try)),
        ):
            nested_groups.extend(
                (
                    (statement.body, True),
                    (statement.orelse, True),
                    (statement.finalbody, conditional),
                    *((handler.body, True) for handler in statement.handlers),
                )
            )
        elif isinstance(statement, ast.Match):
            nested_groups.extend((case.body, True) for case in statement.cases)
        for nested, nested_conditional in nested_groups:
            flattened.extend(
                _iter_module_statements_with_context(
                    nested,
                    conditional=conditional or nested_conditional,
                )
            )
        if _statement_guaranteed_to_raise(statement):
            break
    return flattened


def _iter_module_statements(statements: Sequence[ast.stmt]) -> list[ast.stmt]:
    return [
        statement for statement, _ in _iter_module_statements_with_context(statements)
    ]


def _import_aliases(tree: ast.Module) -> dict[str, list[_ImportAlias]]:
    aliases: defaultdict[str, list[_ImportAlias]] = defaultdict(list)
    for statement, conditional in _iter_module_statements_with_context(tree.body):
        if isinstance(statement, ast.ImportFrom):
            for alias in statement.names:
                aliases[alias.asname or alias.name].append(
                    _ImportAlias(
                        target=alias.name,
                        module=statement.module or "",
                        level=statement.level,
                        conditional=conditional,
                        line=statement.lineno,
                    )
                )
        elif isinstance(statement, ast.Import):
            for alias in statement.names:
                aliases[alias.asname or alias.name.split(".")[0]].append(
                    _ImportAlias(
                        target=alias.name,
                        module=alias.name,
                        level=0,
                        conditional=conditional,
                        line=statement.lineno,
                    )
                )
    return dict(aliases)


def _wildcard_import_lines(tree: ast.Module) -> list[int]:
    return [
        statement.lineno
        for statement, _ in _iter_module_statements_with_context(tree.body)
        if isinstance(statement, ast.ImportFrom)
        and any(alias.name == "*" for alias in statement.names)
    ]


def _statement_expression_bindings(statement: ast.stmt) -> set[str]:
    names: set[str] = set()
    pending = list(ast.iter_child_nodes(statement))
    while pending:
        node = pending.pop()
        if isinstance(node, ast.stmt):
            continue
        if isinstance(node, ast.Lambda):
            continue
        if isinstance(node, ast.NamedExpr):
            names.update(_binding_target_names(node.target))
        elif isinstance(node, ast.ExceptHandler) and node.name:
            names.add(node.name)
        elif isinstance(node, (ast.MatchAs, ast.MatchStar)) and node.name:
            names.add(node.name)
        elif isinstance(node, ast.MatchMapping) and node.rest:
            names.add(node.rest)
        pending.extend(ast.iter_child_nodes(node))
    return names


def _module_other_bindings(tree: ast.Module) -> dict[str, list[int]]:
    bindings: defaultdict[str, list[int]] = defaultdict(list)
    for statement, _ in _iter_module_statements_with_context(tree.body):
        names: list[str] = []
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            names.append(statement.name)
        elif isinstance(statement, ast.Assign):
            for target in statement.targets:
                names.extend(_binding_target_names(target))
        elif isinstance(statement, (ast.AnnAssign, ast.AugAssign)):
            names.extend(_binding_target_names(statement.target))
        elif isinstance(statement, (ast.For, ast.AsyncFor)):
            names.extend(_binding_target_names(statement.target))
        elif isinstance(statement, (ast.With, ast.AsyncWith)):
            for item in statement.items:
                names.extend(_binding_target_names(item.optional_vars))
        elif isinstance(statement, ast.Delete):
            for target in statement.targets:
                names.extend(_binding_target_names(target))
        names.extend(_statement_expression_bindings(statement))
        for name in set(names):
            bindings[name].append(statement.lineno)
    return dict(bindings)


def _link_class_bases(
    file_infos: Sequence[_FileInfo],
    classes_by_name: Mapping[str, list[_ClassInfo]],
) -> None:
    """Resolve only bases whose active module binding is statically certain."""

    files_by_path = {file_info.path: file_info for file_info in file_infos}
    for candidates in classes_by_name.values():
        for class_info in candidates:
            file_info = files_by_path[class_info.path]
            resolved: list[_ClassInfo] = []
            unresolved = (
                class_info.has_unresolved_base or len(class_info.node.bases) > 1
            )
            trusted_decorators = {
                decorator.id
                for method in class_info.methods.values()
                for decorator in method.decorator_list
                if isinstance(decorator, ast.Name)
                and decorator.id in {"classmethod", "staticmethod"}
            }
            for decorator_name in trusted_decorators:
                if (
                    decorator_name in class_info.assignments
                    or decorator_name in class_info.methods
                    or any(
                        candidate.path == class_info.path
                        and candidate.line < class_info.line
                        for candidate in classes_by_name.get(decorator_name, [])
                    )
                    or any(
                        imported.line < class_info.line
                        for imported in file_info.imports.get(decorator_name, [])
                    )
                    or any(
                        line < class_info.line
                        for line in file_info.other_bindings.get(
                            decorator_name,
                            [],
                        )
                    )
                    or any(
                        line < class_info.line
                        for line in file_info.wildcard_import_lines
                    )
                ):
                    class_info.decorator_bindings_uncertain = True
            for base in class_info.bases:
                if "." in base:
                    unresolved = True
                    continue
                bindings: list[tuple[int, str, Any]] = [
                    (candidate.line, "class", candidate)
                    for candidate in classes_by_name.get(base, [])
                    if candidate.path == class_info.path
                    and candidate.line < class_info.line
                ]
                bindings.extend(
                    (imported.line, "import", imported)
                    for imported in file_info.imports.get(base, [])
                    if imported.line < class_info.line
                )
                bindings.extend(
                    (line, "other", None)
                    for line in file_info.other_bindings.get(base, [])
                    if line < class_info.line
                )
                bindings.extend(
                    (line, "wildcard", None)
                    for line in file_info.wildcard_import_lines
                    if line < class_info.line
                )
                if not bindings:
                    if base != "object":
                        unresolved = True
                    continue
                latest_line = max(line for line, _, _ in bindings)
                latest = [
                    (kind, value)
                    for line, kind, value in bindings
                    if line == latest_line
                ]
                if len(latest) != 1:
                    unresolved = True
                    continue
                kind, value = latest[0]
                if kind == "class":
                    resolved.append(value)
                    if value.conditional:
                        unresolved = True
                    continue
                if kind == "import":
                    imported = value
                    is_framework_import = (
                        not imported.conditional
                        and base == "ABC"
                        and imported.level == 0
                        and imported.module == "abc"
                        and imported.target == "ABC"
                    ) or (
                        not imported.conditional
                        and base == "ComfyNodeABC"
                        and imported.level == 0
                        and imported.target == "ComfyNodeABC"
                        and imported.module.startswith("comfy")
                    )
                    if is_framework_import:
                        continue
                unresolved = True
            class_info.resolved_bases = tuple(resolved)
            class_info.has_unresolved_base = unresolved


def _mapping_key(
    expression: ast.AST | None,
    classes: Mapping[str, _ClassInfo],
) -> str | None:
    del classes
    literal = _literal_string(expression)
    return literal.strip() if literal else None


def _mapping_entries_from_dict(
    value: ast.Dict,
    *,
    path: str,
    line: int,
    classes: Mapping[str, _ClassInfo],
    conditional: bool = False,
) -> list[_MappingEntry]:
    entries: list[_MappingEntry] = []
    for key, mapped_value in zip(value.keys, value.values):
        if key is None:
            continue
        class_id = _mapping_key(key, classes)
        class_reference = _expression_name(mapped_value)
        if class_id and class_reference:
            entries.append(
                _MappingEntry(
                    class_id=class_id,
                    class_reference=class_reference,
                    path=path,
                    line=getattr(key, "lineno", line),
                    conditional=conditional,
                    reachability_unverified=path != "__init__.py",
                )
            )
            if len(entries) > MAX_DISCOVERED_MAPPINGS:
                raise NodePackTooLargeError(
                    "Pack declares too many literal node mappings"
                )
    return entries


def _extract_file_metadata(
    file_info: _FileInfo,
) -> tuple[
    list[_MappingEntry],
    dict[str, str],
    set[str],
]:
    mappings: list[_MappingEntry] = []
    display_names: dict[str, str] = {}
    features: set[str] = set()
    mapping_assignment_count = 0
    tree = file_info.tree
    for statement, conditional in _iter_module_statements_with_context(tree.body):
        if (
            isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef))
            and statement.name == "comfy_entrypoint"
        ):
            features.add("v3_entrypoint")
        if isinstance(statement, (ast.FunctionDef, ast.AsyncFunctionDef)):
            for decorator in statement.decorator_list:
                expression = (
                    decorator.func if isinstance(decorator, ast.Call) else decorator
                )
                decorator_name = _expression_name(expression) or ""
                if ".routes." in decorator_name:
                    features.add("server_routes")
        if isinstance(statement, (ast.Assign, ast.AnnAssign)):
            value = _assignment_value(statement)
            targets = (
                statement.targets
                if isinstance(statement, ast.Assign)
                else [statement.target]
            )
            target_names = {
                name for target in targets for name in _binding_target_names(target)
            }
            if "WEB_DIRECTORY" in target_names:
                features.add("web_directory")
            if _expression_name(value) == "NODE_CLASS_MAPPINGS" and target_names - {
                "NODE_CLASS_MAPPINGS"
            }:
                features.add("dynamic_registration")
            if "NODE_CLASS_MAPPINGS" in target_names:
                mapping_assignment_count += 1
                if mapping_assignment_count > 1 or conditional:
                    features.add("dynamic_registration")
                if not conditional:
                    mappings.clear()
                if isinstance(value, ast.Dict):
                    if any(
                        key is None
                        or _mapping_key(key, file_info.classes) is None
                        or _expression_name(mapped_value) is None
                        for key, mapped_value in zip(value.keys, value.values)
                    ):
                        features.add("dynamic_registration")
                    mappings.extend(
                        _mapping_entries_from_dict(
                            value,
                            path=file_info.path,
                            line=getattr(statement, "lineno", 0),
                            classes=file_info.classes,
                            conditional=conditional,
                        )
                    )
                else:
                    features.add("dynamic_registration")
            if "NODE_DISPLAY_NAME_MAPPINGS" in target_names and isinstance(
                value, ast.Dict
            ):
                for key, display in zip(value.keys, value.values):
                    class_id = _literal_string(key)
                    display_name = _literal_string(display)
                    if class_id and display_name:
                        display_names[class_id] = display_name
                        if len(display_names) > MAX_DISCOVERED_MAPPINGS:
                            raise NodePackTooLargeError(
                                "Pack declares too many display-name mappings"
                            )

            for target in targets:
                if (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "NODE_CLASS_MAPPINGS"
                ):
                    if mapping_assignment_count == 0:
                        features.add("dynamic_registration")
                    class_id = _literal_string(target.slice)
                    class_reference = _expression_name(value) if value else None
                    if class_id and class_reference:
                        mappings.append(
                            _MappingEntry(
                                class_id=class_id,
                                class_reference=class_reference,
                                path=file_info.path,
                                line=getattr(statement, "lineno", 0),
                                conditional=conditional,
                                reachability_unverified=(
                                    file_info.path != "__init__.py"
                                ),
                            )
                        )
                    else:
                        features.add("dynamic_registration")
        if isinstance(statement, ast.AugAssign):
            target_name = (
                statement.target.id if isinstance(statement.target, ast.Name) else ""
            )
            if target_name == "NODE_CLASS_MAPPINGS":
                features.add("dynamic_registration")
        if isinstance(statement, ast.Delete):
            deletes_mapping = any(
                isinstance(target, ast.Name)
                and target.id == "NODE_CLASS_MAPPINGS"
                or (
                    isinstance(target, ast.Subscript)
                    and isinstance(target.value, ast.Name)
                    and target.value.id == "NODE_CLASS_MAPPINGS"
                )
                for target in statement.targets
            )
            if deletes_mapping:
                features.add("dynamic_registration")
                if not conditional and any(
                    isinstance(target, ast.Name) and target.id == "NODE_CLASS_MAPPINGS"
                    for target in statement.targets
                ):
                    mappings.clear()
        call = (
            statement.value
            if isinstance(statement, ast.Expr) and isinstance(statement.value, ast.Call)
            else None
        )
        if call is not None:
            function_name = _expression_name(call.func) or ""
            if "PromptServer.instance.routes" in function_name:
                features.add("server_routes")
            call_values = [*call.args, *(keyword.value for keyword in call.keywords)]
            if any(
                isinstance(node, ast.Name) and node.id == "NODE_CLASS_MAPPINGS"
                for value in call_values
                for node in ast.walk(value)
            ):
                features.add("dynamic_registration")
            if function_name.startswith("NODE_CLASS_MAPPINGS."):
                if (
                    function_name == "NODE_CLASS_MAPPINGS.update"
                    and len(call.args) == 1
                    and not call.keywords
                    and isinstance(call.args[0], ast.Dict)
                ):
                    if mapping_assignment_count == 0:
                        features.add("dynamic_registration")
                    mapping_dict = call.args[0]
                    if any(
                        key is None
                        or _mapping_key(key, file_info.classes) is None
                        or _expression_name(mapped_value) is None
                        for key, mapped_value in zip(
                            mapping_dict.keys,
                            mapping_dict.values,
                        )
                    ):
                        features.add("dynamic_registration")
                    mappings.extend(
                        _mapping_entries_from_dict(
                            mapping_dict,
                            path=file_info.path,
                            line=getattr(statement, "lineno", 0),
                            classes=file_info.classes,
                            conditional=conditional,
                        )
                    )
                else:
                    features.add("dynamic_registration")
        if len(mappings) > MAX_DISCOVERED_MAPPINGS:
            raise NodePackTooLargeError("Pack declares too many literal node mappings")
    return mappings, display_names, features


def _class_member(
    class_info: _ClassInfo,
    name: str,
    classes_by_name: Mapping[str, list[_ClassInfo]],
    *,
    methods: bool,
    visited: set[tuple[str, str, int]] | None = None,
) -> ast.AST | None:
    del classes_by_name
    seen = set(visited or ())
    pending = [class_info]
    while pending:
        current = pending.pop()
        identity = (current.path, current.name, current.line)
        if identity in seen:
            continue
        seen.add(identity)
        primary = current.methods if methods else current.assignments
        shadowing = current.assignments if methods else current.methods
        if name in primary:
            return primary[name]
        if name in shadowing:
            return shadowing[name]
        pending.extend(reversed(current.resolved_bases))
    return None


def _has_unknown_base(
    class_info: _ClassInfo,
    classes_by_name: Mapping[str, list[_ClassInfo]],
) -> bool:
    del classes_by_name
    seen: set[tuple[str, str, int]] = set()
    pending = [class_info]
    while pending:
        current = pending.pop()
        identity = (current.path, current.name, current.line)
        if identity in seen:
            continue
        seen.add(identity)
        if current.has_unresolved_base:
            return True
        if current is not class_info and current.conditional:
            return True
        if current is not class_info and (
            current.node.decorator_list
            or any(keyword.arg == "metaclass" for keyword in current.node.keywords)
            or current.decorator_bindings_uncertain
            or current.dynamic_class_body
            or current.class_body_terminates
        ):
            return True
        pending.extend(current.resolved_bases)
    return False


def _hierarchy_declares_abstract_methods(class_info: _ClassInfo) -> bool:
    seen: set[tuple[str, str, int]] = set()
    pending = [class_info]
    while pending:
        current = pending.pop()
        identity = (current.path, current.name, current.line)
        if identity in seen:
            continue
        seen.add(identity)
        if any(
            (_expression_name(decorator) or "")
            in {
                "abstractmethod",
                "abc.abstractmethod",
            }
            for method in current.methods.values()
            for decorator in method.decorator_list
        ):
            return True
        pending.extend(current.resolved_bases)
    return False


def _inspect_input_options(
    options_node: ast.AST,
    hard: list[str],
    partial: list[str],
) -> None:
    options = _literal_value(options_node)
    if not isinstance(options, Mapping):
        partial.append("input options are computed dynamically")
        return
    if bool(options.get("lazy")):
        hard.append("uses lazy input evaluation")
    if bool(options.get("rawLink")):
        hard.append("uses rawLink input semantics")
    try:
        json.dumps(options)
    except (TypeError, ValueError, OverflowError):
        hard.append("input options are not JSON-serializable")


def _inspect_input_mapping(
    mapping_node: ast.Dict,
    hard: list[str],
    partial: list[str],
) -> tuple[set[str], bool]:
    names: set[str] = set()
    complete = True
    for raw_name, descriptor in zip(mapping_node.keys, mapping_node.values):
        if raw_name is None:
            hard.append(
                "input mapping uses dictionary unpacking; its schema cannot be verified"
            )
            complete = False
            continue
        name = _literal_string(raw_name)
        if not name:
            partial.append("contains a dynamically named input")
            complete = False
            continue
        names.add(name)
        if not name.isidentifier() or keyword.iskeyword(name):
            hard.append(f"input `{name}` is not a valid Python identifier")
        if name in RESERVED_ADAPTER_INPUTS:
            hard.append(f"input `{name}` conflicts with the adapter")

        if isinstance(descriptor, ast.Constant) and isinstance(descriptor.value, str):
            if not descriptor.value.strip():
                hard.append(f"input `{name}` has an empty socket type")
            continue
        if isinstance(descriptor, (ast.Tuple, ast.List)) and descriptor.elts:
            if len(descriptor.elts) > 2:
                hard.append(f"input `{name}` descriptor has more than type and options")
            type_value = descriptor.elts[0]
            if isinstance(type_value, ast.Constant) and isinstance(
                type_value.value, str
            ):
                if not type_value.value.strip():
                    hard.append(f"input `{name}` has an empty socket type")
            elif isinstance(type_value, (ast.Tuple, ast.List)):
                partial.append(f"input `{name}` uses a combo/widget descriptor")
            else:
                partial.append(f"input `{name}` has a dynamic socket type")
            if len(descriptor.elts) > 1:
                _inspect_input_options(descriptor.elts[1], hard, partial)
            continue
        partial.append(f"input `{name}` has a dynamic descriptor")
    return names, complete


def _function_scope_nodes(
    method: ast.FunctionDef | ast.AsyncFunctionDef,
) -> list[ast.AST]:
    """Walk one function body without entering nested scopes."""

    nodes: list[ast.AST] = []
    pending: list[ast.AST] = list(reversed(method.body))
    scope_boundaries = (
        ast.FunctionDef,
        ast.AsyncFunctionDef,
        ast.ClassDef,
        ast.Lambda,
    )
    while pending:
        node = pending.pop()
        if isinstance(node, scope_boundaries):
            continue
        nodes.append(node)
        children = [
            child
            for child in ast.iter_child_nodes(node)
            if not isinstance(child, scope_boundaries)
        ]
        pending.extend(reversed(children))
    return nodes


def _inspect_input_types(
    method: ast.FunctionDef | ast.AsyncFunctionDef,
    hard: list[str],
    partial: list[str],
) -> _InputSchema | None:
    if isinstance(method, ast.AsyncFunctionDef):
        hard.append("INPUT_TYPES is asynchronous")
        return None
    scoped_nodes = _function_scope_nodes(method)
    if any(isinstance(node, (ast.Yield, ast.YieldFrom)) for node in scoped_nodes):
        hard.append("INPUT_TYPES is a generator")
    decorator_names = [
        _expression_name(decorator) for decorator in method.decorator_list
    ]
    trusted_decorators = {
        "classmethod",
        "staticmethod",
        "property",
        "cached_property",
    }
    decorators = {
        name
        for name in decorator_names
        if name is not None and name in trusted_decorators
    }
    if any(name is None for name in decorator_names):
        partial.append("INPUT_TYPES uses called or unresolved decorators")
    if "property" in decorators or "cached_property" in decorators:
        hard.append("INPUT_TYPES is declared as a property")
    if any(
        name is not None and name not in trusted_decorators for name in decorator_names
    ):
        partial.append("INPUT_TYPES uses custom decorators")
    positional = list(method.args.posonlyargs) + list(method.args.args)
    defaults = len(method.args.defaults)
    required = max(0, len(positional) - defaults)
    if "classmethod" in decorators:
        if not positional and method.args.vararg is None:
            hard.append("classmethod INPUT_TYPES cannot accept its bound class")
        required = max(0, required - 1)
    if required:
        hard.append("INPUT_TYPES requires instance or positional arguments")
    if any(default is None for default in method.args.kw_defaults):
        hard.append("INPUT_TYPES requires keyword-only arguments")
    returns = [node for node in scoped_nodes if isinstance(node, ast.Return)]
    literal_returns = [
        node.value for node in returns if isinstance(node.value, ast.Dict)
    ]
    if not returns or len(literal_returns) != len(returns):
        partial.append(
            "INPUT_TYPES is computed dynamically; runtime schema is unverified"
        )
    required_by_return: list[set[str]] = []
    optional_by_return: list[set[str]] = []
    names_complete = bool(returns) and len(literal_returns) == len(returns)
    if not method.body or not isinstance(method.body[-1], ast.Return):
        partial.append("INPUT_TYPES may exit without returning a schema")
        names_complete = False
    for returned in literal_returns:
        returned_required: set[str] = set()
        returned_optional: set[str] = set()
        if any(key is None for key in returned.keys):
            hard.append(
                "INPUT_TYPES uses dictionary unpacking; hidden sections cannot be excluded"
            )
            names_complete = False
        sections: dict[str, ast.AST] = {}
        for key, value in zip(returned.keys, returned.values):
            section = _literal_string(key)
            if section:
                sections[section] = value
            elif key is not None:
                hard.append(
                    "INPUT_TYPES has a dynamic section name; hidden inputs cannot be excluded"
                )
                names_complete = False
        for section in sections:
            if section not in {"required", "optional", "hidden"}:
                partial.append(f"uses nonstandard `{section}` input section")
        hidden = sections.get("hidden")
        if isinstance(hidden, ast.Dict) and hidden.keys:
            hard.append("uses hidden ComfyUI inputs")
        elif hidden is not None and not isinstance(hidden, ast.Dict):
            hard.append("hidden inputs are computed dynamically")
        for section_name in ("required", "optional"):
            inputs = sections.get(section_name)
            if isinstance(inputs, ast.Dict):
                names, complete = _inspect_input_mapping(
                    inputs,
                    hard,
                    partial,
                )
                if section_name == "required":
                    returned_required.update(names)
                else:
                    returned_optional.update(names)
                names_complete = names_complete and complete
            elif inputs is not None:
                partial.append(f"`{section_name}` inputs are computed dynamically")
                names_complete = False
        duplicate_names = returned_required & returned_optional
        for name in sorted(duplicate_names):
            hard.append(f"input `{name}` is declared as both required and optional")
        required_by_return.append(returned_required)
        optional_by_return.append(returned_optional)
    if not names_complete:
        return None
    all_names = set().union(*required_by_return, *optional_by_return)
    always_required = (
        set.intersection(*required_by_return) if required_by_return else set()
    )
    return _InputSchema(
        required=frozenset(always_required),
        optional=frozenset(all_names - always_required),
    )


def _inspect_execution_signature(
    method: ast.FunctionDef,
    declared_inputs: _InputSchema | None,
    hard: list[str],
    partial: list[str],
) -> None:
    decorator_names = [
        _expression_name(decorator) for decorator in method.decorator_list
    ]
    trusted_decorators = {
        "classmethod",
        "staticmethod",
        "property",
        "cached_property",
    }
    decorators = {
        name
        for name in decorator_names
        if name is not None and name in trusted_decorators
    }
    if any(name is None for name in decorator_names):
        partial.append("execution method uses called or unresolved decorators")
    if "property" in decorators or "cached_property" in decorators:
        hard.append("execution method is declared as a property")
    if any(
        name is not None and name not in trusted_decorators for name in decorator_names
    ):
        partial.append("execution method uses custom decorators")
    positional = list(method.args.posonlyargs) + list(method.args.args)
    default_start = len(positional) - len(method.args.defaults)
    positional_parameters = [
        {
            "name": argument.arg,
            "positional_only": index < len(method.args.posonlyargs),
            "required": index < default_start,
        }
        for index, argument in enumerate(positional)
    ]
    bound_keyword_name: str | None = None
    if "staticmethod" not in decorators:
        if positional_parameters:
            receiver = positional_parameters.pop(0)
            if not receiver["positional_only"]:
                bound_keyword_name = str(receiver["name"])
        elif method.args.vararg is None:
            hard.append("execution method cannot accept its bound class instance")

    accepted_keywords = {
        parameter["name"]
        for parameter in positional_parameters
        if not parameter["positional_only"]
    }
    accepted_keywords.update(argument.arg for argument in method.args.kwonlyargs)
    required_keywords = {
        parameter["name"]
        for parameter in positional_parameters
        if parameter["required"]
    }
    required_keywords.update(
        argument.arg
        for argument, default in zip(
            method.args.kwonlyargs,
            method.args.kw_defaults,
        )
        if default is None
    )
    for parameter in positional_parameters:
        if parameter["positional_only"] and parameter["required"]:
            hard.append(f"execution parameter `{parameter['name']}` is positional-only")

    if declared_inputs is None:
        return
    all_declared_inputs = set(declared_inputs.required | declared_inputs.optional)
    if bound_keyword_name in all_declared_inputs:
        hard.append(
            f"input `{bound_keyword_name}` conflicts with the bound method receiver"
        )
    if method.args.kwarg is None:
        for name in sorted(all_declared_inputs - accepted_keywords):
            hard.append(f"execution method does not accept input `{name}`")
    for name in sorted(required_keywords - set(declared_inputs.required)):
        if name in declared_inputs.optional:
            hard.append(f"execution method requires optional input `{name}`")
        else:
            hard.append(f"execution method requires undeclared input `{name}`")


def _inspect_execution_return(
    method: ast.FunctionDef | ast.AsyncFunctionDef,
    expected_outputs: int | None,
    hard: list[str],
    partial: list[str],
) -> None:
    scoped_nodes = _function_scope_nodes(method)
    if any(isinstance(node, (ast.Yield, ast.YieldFrom)) for node in scoped_nodes):
        hard.append("execution method is a generator")
    returns = [node for node in scoped_nodes if isinstance(node, ast.Return)]
    if not returns:
        hard.append("execution method does not return node outputs")
        return
    if not method.body or not isinstance(method.body[-1], ast.Return):
        partial.append("execution method may exit without returning outputs")

    for returned in returns:
        value = returned.value
        if value is None or (isinstance(value, ast.Constant) and value.value is None):
            hard.append("execution method returns no node outputs")
            continue
        if isinstance(value, (ast.Tuple, ast.List)):
            if any(isinstance(element, ast.Starred) for element in value.elts):
                partial.append("execution return count uses iterable unpacking")
            elif expected_outputs is not None and len(value.elts) != expected_outputs:
                hard.append("execution return count does not match RETURN_TYPES")
            continue
        if isinstance(value, ast.Dict):
            entries = {
                key: item
                for raw_key, item in zip(value.keys, value.values)
                if (key := _literal_string(raw_key)) is not None
            }
            if len(entries) != len(value.values):
                partial.append("execution result dictionary is computed dynamically")
            if "expand" in entries:
                hard.append("returns dynamic graph expansion data")
            if "ui" in entries:
                partial.append("returns custom UI data that an adapter would discard")
            result_value = entries.get("result")
            if isinstance(result_value, (ast.Tuple, ast.List)):
                if any(
                    isinstance(element, ast.Starred) for element in result_value.elts
                ):
                    partial.append("execution result count uses iterable unpacking")
                elif (
                    expected_outputs is not None
                    and len(result_value.elts) != expected_outputs
                ):
                    hard.append("execution result count does not match RETURN_TYPES")
            elif result_value is not None:
                partial.append("execution result shape is computed dynamically")
            elif expected_outputs:
                hard.append("execution result dictionary has no `result` outputs")
            continue
        partial.append("execution return shape is computed dynamically")


def _constructor_requires_arguments(
    constructor: ast.FunctionDef | ast.AsyncFunctionDef,
) -> bool:
    positional = list(constructor.args.posonlyargs) + list(constructor.args.args)
    if positional:
        positional = positional[1:]
    elif constructor.args.vararg is None:
        return True
    defaults = len(constructor.args.defaults)
    required_positional = max(0, len(positional) - defaults)
    required_keyword = sum(default is None for default in constructor.args.kw_defaults)
    return required_positional > 0 or required_keyword > 0


def _inspect_constructor_behavior(
    constructor: ast.FunctionDef | ast.AsyncFunctionDef,
    hard: list[str],
    partial: list[str],
) -> None:
    if constructor.decorator_list:
        partial.append("constructor uses decorators that may alter its call contract")
    scoped_nodes = _function_scope_nodes(constructor)
    if any(isinstance(node, (ast.Yield, ast.YieldFrom)) for node in scoped_nodes):
        hard.append("constructor is a generator")
    for returned in (node for node in scoped_nodes if isinstance(node, ast.Return)):
        if returned.value is None:
            continue
        known, value = _literal_value_known(returned.value)
        if known and value is None:
            continue
        if known:
            hard.append("constructor returns a non-None value")
        else:
            partial.append("constructor may return a non-None value")


def _deduplicate_reasons(reasons: Sequence[str]) -> list[str]:
    unique: list[str] = []
    seen: set[str] = set()
    for reason in reasons:
        normalized = str(reason)
        if len(normalized) > MAX_REASON_CHARACTERS:
            normalized = normalized[: MAX_REASON_CHARACTERS - 1] + "…"
        if normalized in seen:
            continue
        if len(unique) == MAX_REASONS_PER_NODE:
            unique.append("additional compatibility reasons were omitted")
            break
        seen.add(normalized)
        unique.append(normalized)
    return unique


def _analyze_class(
    mapping: _MappingEntry,
    class_info: _ClassInfo | None,
    classes_by_name: Mapping[str, list[_ClassInfo]],
    display_names: Mapping[str, str],
) -> dict[str, Any]:
    if class_info is None:
        return {
            "class_id": mapping.class_id,
            "display_name": display_names.get(mapping.class_id, mapping.class_id),
            "class_name": mapping.class_reference,
            "status": "partial",
            "reasons": [
                "mapping target could not be resolved statically; runtime inspection is required"
            ],
            "source_file": mapping.path,
            "line": mapping.line,
            "mapped": mapping.mapped,
            "confidence": "static",
            "function": None,
            "return_types": None,
        }

    hard: list[str] = []
    partial: list[str] = []
    if mapping.conditional:
        partial.append("node registration is conditional at module load time")
    if mapping.reachability_unverified:
        partial.append(
            "registration is outside the root entrypoint; import reachability is unverified"
        )
    if class_info.conditional:
        partial.append("node class definition is conditional at module load time")
    if class_info.class_body_terminates:
        hard.append("class body raises before the node can be created")
    elif class_info.dynamic_class_body:
        partial.append("class body contains dynamic statements")
    if class_info.decorator_bindings_uncertain:
        partial.append("method decorator names may be shadowed at runtime")
    if class_info.node.decorator_list:
        partial.append("node class uses decorators that may alter runtime behavior")
    if any(keyword.arg == "metaclass" for keyword in class_info.node.keywords):
        partial.append("node class uses a custom metaclass")
    unknown_base = _has_unknown_base(class_info, classes_by_name)
    if unknown_base:
        partial.append(
            "inherits from an unresolved base; constructor and scheduler hooks are unverified"
        )
    if _hierarchy_declares_abstract_methods(class_info):
        partial.append(
            "class hierarchy declares abstract methods; instantiation is unverified"
        )

    input_method = _class_member(
        class_info,
        "INPUT_TYPES",
        classes_by_name,
        methods=True,
    )
    declared_inputs: _InputSchema | None = None
    if isinstance(input_method, (ast.FunctionDef, ast.AsyncFunctionDef)):
        declared_inputs = _inspect_input_types(input_method, hard, partial)
    elif unknown_base:
        partial.append("INPUT_TYPES may be inherited from an unresolved base class")
    else:
        hard.append("does not declare INPUT_TYPES")

    function_node = _class_member(
        class_info,
        "FUNCTION",
        classes_by_name,
        methods=False,
    )
    function_name = _literal_string(function_node)
    execution: ast.FunctionDef | ast.AsyncFunctionDef | None = None
    if function_name:
        execution_member = _class_member(
            class_info,
            function_name,
            classes_by_name,
            methods=True,
        )
        if isinstance(execution_member, ast.AsyncFunctionDef):
            execution = execution_member
            hard.append(f"execution method `{function_name}` is asynchronous")
        elif isinstance(execution_member, ast.FunctionDef):
            execution = execution_member
        elif unknown_base:
            partial.append(
                f"execution method `{function_name}` may be inherited dynamically"
            )
        else:
            hard.append(f"FUNCTION names missing method `{function_name}`")
    elif function_node is not None:
        partial.append("FUNCTION is computed dynamically")
    elif unknown_base:
        partial.append("FUNCTION may be inherited from an unresolved base class")
    else:
        hard.append("does not declare a literal FUNCTION name")

    return_types_node = _class_member(
        class_info,
        "RETURN_TYPES",
        classes_by_name,
        methods=False,
    )
    return_types = _literal_value(return_types_node)
    normalized_return_types: list[str] | None = None
    if isinstance(return_types, (tuple, list)):
        normalized_return_types = list(return_types)
        if not 1 <= len(normalized_return_types) <= MAX_ADAPTER_OUTPUTS:
            hard.append(
                f"declares {len(normalized_return_types)} outputs; adapter supports 1..{MAX_ADAPTER_OUTPUTS}"
            )
        if not all(isinstance(item, str) and item.strip() for item in return_types):
            hard.append("RETURN_TYPES contains a non-string socket type")
    elif return_types_node is not None:
        partial.append("RETURN_TYPES is computed dynamically")
    elif unknown_base:
        partial.append("RETURN_TYPES may be inherited from an unresolved base class")
    else:
        hard.append("does not declare RETURN_TYPES")

    if isinstance(execution, ast.FunctionDef):
        _inspect_execution_signature(
            execution,
            declared_inputs,
            hard,
            partial,
        )
        _inspect_execution_return(
            execution,
            (
                len(normalized_return_types)
                if normalized_return_types is not None
                else None
            ),
            hard,
            partial,
        )

    return_names_node = _class_member(
        class_info,
        "RETURN_NAMES",
        classes_by_name,
        methods=False,
    )
    return_names = _literal_value(return_names_node)
    if (
        isinstance(return_names, (tuple, list))
        and normalized_return_types is not None
        and len(return_names) != len(normalized_return_types)
    ):
        hard.append("RETURN_NAMES length does not match RETURN_TYPES")
    if isinstance(return_names, (tuple, list)) and not all(
        isinstance(item, str) and item.strip() for item in return_names
    ):
        hard.append("RETURN_NAMES contains an invalid name")

    constructor = _class_member(
        class_info,
        "__init__",
        classes_by_name,
        methods=True,
    )
    if isinstance(constructor, (ast.FunctionDef, ast.AsyncFunctionDef)):
        _inspect_constructor_behavior(constructor, hard, partial)
        if isinstance(constructor, ast.AsyncFunctionDef):
            hard.append("constructor is asynchronous")
        if _constructor_requires_arguments(constructor):
            hard.append("constructor requires arguments")
    elif constructor is not None:
        known, value = _literal_value_known(constructor)
        if known and not callable(value):
            hard.append("constructor is assigned a non-callable value")
        else:
            partial.append("constructor is assigned dynamically")

    allocator = _class_member(
        class_info,
        "__new__",
        classes_by_name,
        methods=True,
    )
    if isinstance(allocator, ast.AsyncFunctionDef):
        hard.append("allocator is asynchronous")
    elif isinstance(allocator, ast.FunctionDef):
        if allocator.decorator_list:
            partial.append("allocator uses decorators that may alter its call contract")
        if _constructor_requires_arguments(allocator):
            hard.append("allocator requires arguments")
    elif allocator is not None:
        known, value = _literal_value_known(allocator)
        if known and not callable(value):
            hard.append("allocator is assigned a non-callable value")
        else:
            partial.append("allocator is assigned dynamically")

    input_is_list_node = _class_member(
        class_info,
        "INPUT_IS_LIST",
        classes_by_name,
        methods=False,
    )
    input_is_list_known, input_is_list = _literal_value_known(input_is_list_node)
    if input_is_list_node is not None and input_is_list_known and bool(input_is_list):
        hard.append("uses INPUT_IS_LIST scheduler semantics")
    elif input_is_list_node is not None and (
        not input_is_list_known
        or (input_is_list is not False and input_is_list is not None)
    ):
        partial.append("INPUT_IS_LIST is computed dynamically")

    output_is_list_node = _class_member(
        class_info,
        "OUTPUT_IS_LIST",
        classes_by_name,
        methods=False,
    )
    output_is_list = _literal_value(output_is_list_node)
    if isinstance(output_is_list, (tuple, list)) and any(output_is_list):
        hard.append("uses list-output scheduler semantics")
    if (
        isinstance(output_is_list, (tuple, list))
        and normalized_return_types is not None
        and len(output_is_list) != len(normalized_return_types)
    ):
        hard.append("OUTPUT_IS_LIST length does not match RETURN_TYPES")
    elif output_is_list_node is not None and not isinstance(
        output_is_list, (tuple, list)
    ):
        partial.append("OUTPUT_IS_LIST is computed dynamically")

    output_node_member = _class_member(
        class_info,
        "OUTPUT_NODE",
        classes_by_name,
        methods=False,
    )
    output_node_known, output_node = _literal_value_known(output_node_member)
    if output_node_member is not None and output_node_known and bool(output_node):
        partial.append("uses OUTPUT_NODE sink behavior")
    elif output_node_member is not None and not output_node_known:
        partial.append("OUTPUT_NODE behavior is computed dynamically")

    not_idempotent_member = _class_member(
        class_info,
        "NOT_IDEMPOTENT",
        classes_by_name,
        methods=False,
    )
    not_idempotent_known, not_idempotent = _literal_value_known(not_idempotent_member)
    if (
        not_idempotent_member is not None
        and not_idempotent_known
        and bool(not_idempotent)
    ):
        partial.append("declares non-idempotent cache behavior")
    elif not_idempotent_member is not None and not not_idempotent_known:
        partial.append("non-idempotent cache behavior is computed dynamically")

    for method_name, reason, severity in (
        ("check_lazy_status", "uses lazy scheduler callbacks", "hard"),
        ("VALIDATE_INPUTS", "uses custom prompt validation", "partial"),
        ("IS_CHANGED", "uses custom cache invalidation", "partial"),
    ):
        hook = _class_member(
            class_info,
            method_name,
            classes_by_name,
            methods=True,
        )
        if hook is not None:
            (hard if severity == "hard" else partial).append(reason)
            continue
        assigned_hook = _class_member(
            class_info,
            method_name,
            classes_by_name,
            methods=False,
        )
        if assigned_hook is not None:
            (hard if severity == "hard" else partial).append(
                f"{reason} through an assigned callback"
            )

    hard = _deduplicate_reasons(hard)
    partial = _deduplicate_reasons(partial)
    if hard:
        status = "unsupported"
        reasons = [*hard, *partial]
    elif partial or not mapping.mapped:
        status = "partial"
        reasons = partial
        if not mapping.mapped:
            reasons.insert(
                0,
                "node-like class was not found in a literal NODE_CLASS_MAPPINGS entry",
            )
    else:
        status = "compatible"
        reasons = ["matches the basic static V1 adapter contract"]

    return {
        "class_id": mapping.class_id,
        "display_name": display_names.get(mapping.class_id, mapping.class_id),
        "class_name": class_info.name,
        "status": status,
        "reasons": reasons,
        "source_file": class_info.path,
        "line": class_info.line,
        "mapped": mapping.mapped,
        "confidence": "static",
        "function": function_name,
        "return_types": normalized_return_types,
    }


def _relative_import_paths(
    importing_path: str,
    imported: _ImportAlias,
    reference: str,
) -> set[str]:
    if imported.level <= 0 or imported.conditional:
        return set()
    package_parts = list(PurePosixPath(importing_path).parent.parts)
    parents_to_drop = imported.level - 1
    if parents_to_drop > len(package_parts):
        return set()
    if parents_to_drop:
        package_parts = package_parts[:-parents_to_drop]
    module_parts = [part for part in imported.module.split(".") if part]
    if not module_parts and "." in reference:
        module_parts.append(imported.target.split(".")[0])
    path_parts = [*package_parts, *module_parts]
    if not path_parts:
        return {
            os.fspath(PurePosixPath(*package_parts, "__init__.py")),
        }
    module_path = PurePosixPath(*path_parts)
    return {
        os.fspath(module_path.with_suffix(".py")),
        os.fspath(module_path / "__init__.py"),
    }


def _resolve_mapping_class(
    mapping: _MappingEntry,
    file_infos: Sequence[_FileInfo],
    classes_by_name: Mapping[str, list[_ClassInfo]],
) -> _ClassInfo | None:
    class_name = mapping.class_reference.split(".")[-1]
    file_info = next(
        (item for item in file_infos if item.path == mapping.path),
        None,
    )
    if file_info is None:
        return None

    root_name = mapping.class_reference.split(".")[0]
    bindings: list[tuple[int, str, Any]] = []
    if "." not in mapping.class_reference:
        bindings.extend(
            (candidate.line, "class", candidate)
            for candidate in classes_by_name.get(class_name, [])
            if candidate.path == mapping.path and candidate.line < mapping.line
        )
    bindings.extend(
        (imported.line, "import", imported)
        for imported in file_info.imports.get(root_name, [])
        if imported.line < mapping.line
    )
    bindings.extend(
        (line, "other", None)
        for line in file_info.other_bindings.get(root_name, [])
        if line < mapping.line
    )
    bindings.extend(
        (line, "wildcard", None)
        for line in file_info.wildcard_import_lines
        if line < mapping.line
    )
    if not bindings:
        return None
    latest_line = max(line for line, _, _ in bindings)
    latest = [(kind, value) for line, kind, value in bindings if line == latest_line]
    if len(latest) != 1:
        return None
    kind, value = latest[0]
    if kind == "class":
        return value
    if kind != "import" or "." in mapping.class_reference:
        return None
    imported = value
    imported_class_name = imported.target.split(".")[-1]
    expected_paths = _relative_import_paths(
        mapping.path,
        imported,
        mapping.class_reference,
    )
    if not expected_paths:
        return None
    imported_candidates = [
        candidate
        for candidate in classes_by_name.get(imported_class_name, [])
        if candidate.path in expected_paths
    ]
    resolved: list[_ClassInfo] = []
    for candidate in imported_candidates:
        target_file = next(
            (item for item in file_infos if item.path == candidate.path),
            None,
        )
        if target_file is None:
            continue
        target_bindings: list[tuple[int, str, Any]] = [
            (
                local.line,
                "class",
                local,
            )
            for local in classes_by_name.get(imported_class_name, [])
            if local.path == target_file.path
        ]
        target_bindings.extend(
            (item.line, "import", item)
            for item in target_file.imports.get(imported_class_name, [])
        )
        target_bindings.extend(
            (line, "other", None)
            for line in target_file.other_bindings.get(
                imported_class_name,
                [],
            )
        )
        target_bindings.extend(
            (line, "wildcard", None) for line in target_file.wildcard_import_lines
        )
        if not target_bindings:
            continue
        target_latest_line = max(line for line, _, _ in target_bindings)
        target_latest = [
            (binding_kind, binding)
            for line, binding_kind, binding in target_bindings
            if line == target_latest_line
        ]
        if (
            len(target_latest) == 1
            and target_latest[0][0] == "class"
            and target_latest[0][1] is candidate
        ):
            resolved.append(candidate)
    return resolved[0] if len(resolved) == 1 else None


def analyze_python_sources(
    python_sources: Mapping[str, str],
    *,
    source: Mapping[str, Any] | None = None,
    metadata_files: Sequence[str] = (),
    skipped_files: Sequence[str] = (),
) -> dict[str, Any]:
    """Statically estimate adapter compatibility without executing source."""

    file_infos: list[_FileInfo] = []
    parse_warnings: list[str] = []
    pack_features: set[str] = set()
    if "__init__.py" not in python_sources:
        pack_features.add("missing_root_entrypoint")
    if "__init__.py" in skipped_files:
        pack_features.add("invalid_root_entrypoint")
    classes_by_name: defaultdict[str, list[_ClassInfo]] = defaultdict(list)
    total_source_tokens = 0
    total_ast_nodes = 0
    total_classes = 0
    for path in sorted(python_sources):
        code = python_sources[path]
        source_tokens = 0
        token_limit_exceeded = False
        try:
            for _ in tokenize.generate_tokens(io.StringIO(code).readline):
                source_tokens += 1
                if source_tokens > MAX_SOURCE_TOKENS_PER_FILE:
                    token_limit_exceeded = True
                    break
        except (tokenize.TokenError, IndentationError, SyntaxError):
            # The parser below provides the canonical syntax diagnostic.
            pass
        if token_limit_exceeded:
            parse_warnings.append(
                f"`{path}` contains too many lexical tokens and was skipped"
            )
            if path == "__init__.py":
                pack_features.add("invalid_root_entrypoint")
            continue
        total_source_tokens += source_tokens
        if total_source_tokens > MAX_TOTAL_SOURCE_TOKENS:
            raise NodePackTooLargeError(
                "Pack source exceeds the cumulative token safety limit"
            )
        try:
            tree = ast.parse(code, filename=path)
        except (SyntaxError, ValueError, MemoryError, RecursionError) as exc:
            detail = getattr(exc, "msg", str(exc))
            parse_warnings.append(f"`{path}` could not be parsed: {detail}")
            if path == "__init__.py":
                pack_features.add("invalid_root_entrypoint")
            continue
        try:
            ast_node_count = sum(1 for _ in ast.walk(tree))
        except RecursionError:
            parse_warnings.append(f"`{path}` exceeded the AST traversal safety limit")
            continue
        if ast_node_count > MAX_AST_NODES_PER_FILE:
            parse_warnings.append(
                f"`{path}` contains too many syntax nodes and was skipped"
            )
            if path == "__init__.py":
                pack_features.add("invalid_root_entrypoint")
            continue
        total_ast_nodes += ast_node_count
        if total_ast_nodes > MAX_TOTAL_AST_NODES:
            raise NodePackTooLargeError(
                "Pack syntax exceeds the cumulative analysis safety limit"
            )
        try:
            compile(tree, path, "exec")
        except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError) as exc:
            detail = getattr(exc, "msg", str(exc))
            parse_warnings.append(f"`{path}` is not loadable Python syntax: {detail}")
            if path == "__init__.py":
                pack_features.add("invalid_root_entrypoint")
            continue
        classes: dict[str, _ClassInfo] = {}
        for node, conditional in _iter_module_statements_with_context(tree.body):
            if not isinstance(node, ast.ClassDef):
                continue
            class_info = _class_info(
                path,
                node,
                conditional=conditional or node.name in classes,
            )
            classes[node.name] = class_info
            classes_by_name[class_info.name].append(class_info)
            total_classes += 1
            if total_classes > MAX_DISCOVERED_CLASSES:
                raise NodePackTooLargeError(
                    "Pack declares too many classes for static analysis"
                )
        file_infos.append(
            _FileInfo(
                path=path,
                tree=tree,
                classes=classes,
                imports=_import_aliases(tree),
                other_bindings=_module_other_bindings(tree),
                wildcard_import_lines=_wildcard_import_lines(tree),
            )
        )

    _link_class_bases(file_infos, classes_by_name)

    mappings: list[_MappingEntry] = []
    display_names: dict[str, str] = {}
    for file_info in file_infos:
        file_mappings, file_display_names, features = _extract_file_metadata(file_info)
        mappings.extend(file_mappings)
        if len(mappings) > MAX_DISCOVERED_MAPPINGS:
            raise NodePackTooLargeError(
                "Pack declares too many node mappings for static analysis"
            )
        display_names.update(file_display_names)
        pack_features.update(features)

    unique_mappings: dict[tuple[str, str, str, int], _MappingEntry] = {}
    for mapping in mappings:
        unique_mappings.setdefault(
            (
                mapping.class_id,
                mapping.class_reference,
                mapping.path,
                mapping.line,
            ),
            mapping,
        )
    mappings = list(unique_mappings.values())
    mapping_targets: defaultdict[str, set[tuple[str, str, int]]] = defaultdict(set)
    for mapping in mappings:
        mapping_targets[mapping.class_id].add(
            (mapping.class_reference, mapping.path, mapping.line)
        )
    if any(len(targets) > 1 for targets in mapping_targets.values()):
        pack_features.add("ambiguous_registration")

    resolved_classes: set[tuple[str, str, int]] = set()
    results: list[dict[str, Any]] = []
    for mapping in mappings:
        class_info = _resolve_mapping_class(
            mapping,
            file_infos,
            classes_by_name,
        )
        if class_info:
            resolved_classes.add((class_info.path, class_info.name, class_info.line))
        results.append(
            _analyze_class(
                mapping,
                class_info,
                classes_by_name,
                display_names,
            )
        )
        if len(results) > MAX_ANALYZED_RESULTS:
            raise NodePackTooLargeError(
                "Pack exposes too many node candidates for static analysis"
            )

    # Dynamic mapping construction is common. Include strong node-like class
    # candidates that were not reachable through a literal mapping, but keep
    # them explicitly partial.
    for candidates in classes_by_name.values():
        for class_info in candidates:
            if (
                class_info.path,
                class_info.name,
                class_info.line,
            ) in resolved_classes:
                continue
            has_inputs = "INPUT_TYPES" in class_info.methods
            has_function = "FUNCTION" in class_info.assignments
            has_outputs = "RETURN_TYPES" in class_info.assignments
            if not (has_inputs and has_function and has_outputs):
                continue
            mapping = _MappingEntry(
                class_id=class_info.name,
                class_reference=class_info.name,
                path=class_info.path,
                line=class_info.line,
                mapped=False,
            )
            results.append(
                _analyze_class(
                    mapping,
                    class_info,
                    classes_by_name,
                    display_names,
                )
            )
            if len(results) > MAX_ANALYZED_RESULTS:
                raise NodePackTooLargeError(
                    "Pack exposes too many node candidates for static analysis"
                )

    pack_limitations: list[str] = []
    if "missing_root_entrypoint" in pack_features:
        pack_limitations.append(
            "pack has no root __init__.py entrypoint, so node registration is not loadable as a pack"
        )
    if "invalid_root_entrypoint" in pack_features:
        pack_limitations.append(
            "pack root __init__.py could not be validated as loadable Python"
        )
    if metadata_files:
        pack_limitations.append(
            "pack declares dependencies that the temporary adapter would not install"
        )
    if "web_directory" in pack_features:
        pack_limitations.append(
            "pack declares custom frontend code that the adapter would not load"
        )
    if "server_routes" in pack_features:
        pack_limitations.append(
            "pack declares server routes that the adapter would not register"
        )
    if "v3_entrypoint" in pack_features:
        pack_limitations.append(
            "pack mixes in a V3 entrypoint that the first adapter would not load"
        )
    if "dynamic_registration" in pack_features:
        pack_limitations.append(
            "pack mutates node registrations dynamically, so literal mappings may be overridden"
        )
    if "ambiguous_registration" in pack_features:
        pack_limitations.append(
            "pack assigns at least one node id to multiple possible classes"
        )
    for result in results:
        if result["status"] == "unsupported":
            continue
        if pack_limitations:
            result["status"] = "partial"
            result["reasons"] = _deduplicate_reasons(
                [*result["reasons"], *pack_limitations]
            )

    if "v3_entrypoint" in pack_features and not results:
        results.append(
            {
                "class_id": "comfy_entrypoint",
                "display_name": "V3 extension entrypoint",
                "class_name": "comfy_entrypoint",
                "status": "unsupported",
                "reasons": [
                    "V3-only node discovery is unsupported by the first adapter"
                ],
                "source_file": "",
                "line": 0,
                "mapped": False,
                "confidence": "static",
                "function": None,
                "return_types": None,
            }
        )

    for result in results:
        for field in (
            "class_id",
            "display_name",
            "class_name",
            "source_file",
        ):
            result[field] = _safe_report_fragment(
                result.get(field, ""),
                MAX_RESULT_FIELD_CHARACTERS,
            )
        function_name = result.get("function")
        if function_name is not None:
            result["function"] = _safe_report_fragment(
                function_name,
                MAX_RESULT_FIELD_CHARACTERS,
            )
        return_types = result.get("return_types")
        if isinstance(return_types, list):
            result["return_types"] = [
                _safe_report_fragment(
                    socket_type,
                    MAX_RESULT_FIELD_CHARACTERS,
                )
                for socket_type in return_types[:MAX_ADAPTER_OUTPUTS]
            ]
        result["reasons"] = [
            _safe_report_fragment(reason, MAX_REASON_CHARACTERS)
            for reason in result.get("reasons", [])[: MAX_REASONS_PER_NODE + 1]
        ]

    status_order = {"compatible": 0, "partial": 1, "unsupported": 2}
    results.sort(
        key=lambda result: (
            status_order.get(result["status"], 9),
            result["display_name"].casefold(),
            result["class_id"].casefold(),
        )
    )
    full_summary = {
        status: sum(result["status"] == status for result in results)
        for status in ("compatible", "partial", "unsupported")
    }
    full_summary["total"] = len(results)
    bounded_results: list[dict[str, Any]] = []
    structured_characters = 0
    for result in results[:MAX_REPORTED_NODES]:
        result_characters = len(
            json.dumps(
                result,
                ensure_ascii=False,
                separators=(",", ":"),
            )
        )
        if (
            bounded_results
            and structured_characters + result_characters
            > MAX_STRUCTURED_NODE_CHARACTERS
        ):
            break
        bounded_results.append(result)
        structured_characters += result_characters
    omitted_node_results = len(results) - len(bounded_results)
    results = bounded_results
    summary = {
        status: full_summary[status]
        for status in ("compatible", "partial", "unsupported")
    }
    summary["total"] = full_summary["total"]

    warnings = [
        (
            "Static source analysis only: repository Python was not imported "
            "or executed, so dependencies and runtime-computed schemas remain unverified."
        )
    ]
    if "missing_root_entrypoint" in pack_features:
        warnings.append(
            "No root __init__.py entrypoint was found; non-root registrations are not directly loadable."
        )
    if "invalid_root_entrypoint" in pack_features:
        warnings.append(
            "The root __init__.py entrypoint could not be validated, so no class is marked compatible."
        )
    if metadata_files:
        warnings.append(
            "Pack declares installation/dependency metadata; the tester did not install it."
        )
    if "web_directory" in pack_features:
        warnings.append(
            "Pack declares a custom web directory; classes needing its JavaScript UI may not adapt."
        )
    if "server_routes" in pack_features:
        warnings.append(
            "Pack appears to declare server routes; adapter execution would not register them."
        )
    if "v3_entrypoint" in pack_features:
        warnings.append(
            "Pack declares a V3 comfy_entrypoint; V3-only nodes are not supported by the first adapter."
        )
    if "dynamic_registration" in pack_features:
        warnings.append(
            "Pack mutates NODE_CLASS_MAPPINGS dynamically; literal registrations may not match runtime."
        )
    if "ambiguous_registration" in pack_features:
        warnings.append(
            "Pack assigns a node id to multiple classes; runtime control flow determines the winner."
        )
    if skipped_files:
        warnings.append(
            f"{len(skipped_files)} Python file(s) used an unsupported source encoding and were skipped."
        )
    if omitted_node_results:
        warnings.append(
            f"{omitted_node_results} additional node result(s) were omitted from the bounded report."
        )
    warnings.extend(parse_warnings)
    warnings = [
        _safe_report_fragment(warning, MAX_REASON_CHARACTERS) for warning in warnings
    ]
    reported_metadata = [
        _safe_report_fragment(path, MAX_RESULT_FIELD_CHARACTERS)
        for path in metadata_files[:MAX_REPORTED_FILE_PATHS]
    ]
    reported_skipped = [
        _safe_report_fragment(path, MAX_RESULT_FIELD_CHARACTERS)
        for path in skipped_files[:MAX_REPORTED_FILE_PATHS]
    ]
    report = {
        "source": dict(source or {}),
        "summary": summary,
        "nodes": results,
        "files": {
            "python_discovered": len(python_sources),
            "python_parsed": len(file_infos),
            "metadata": reported_metadata,
            "metadata_total": len(metadata_files),
            "skipped": reported_skipped,
            "skipped_total": len(skipped_files),
        },
        "warnings": warnings,
        "confidence": "static",
    }
    report["report_text"] = format_report(report)
    return report


def _safe_report_fragment(value: Any, max_length: int = 600) -> str:
    text = str(value)
    escaped: list[str] = []
    for character in text:
        codepoint = ord(character)
        if character == "\n":
            escaped.append(r"\n")
        elif character == "\r":
            escaped.append(r"\r")
        elif character == "\t":
            escaped.append(r"\t")
        elif codepoint < 32 or codepoint == 127:
            escaped.append(f"\\x{codepoint:02x}")
        else:
            escaped.append(character)
        if sum(len(part) for part in escaped) >= max_length:
            escaped.append("…")
            break
    return "".join(escaped)


def format_report(report: Mapping[str, Any]) -> str:
    """Render a compact human-readable compatibility report."""

    source = report.get("source", {})
    slug = _safe_report_fragment(source.get("repository", "Node pack"))
    commit = source.get("resolved_commit", "")
    revision = _safe_report_fragment(source.get("requested_ref", ""))
    subdirectory = _safe_report_fragment(source.get("subdirectory", ""))
    title = str(slug)
    if commit:
        title += f" @ {str(commit)[:12]}"
    if subdirectory:
        title += f" / {subdirectory}"
    if revision and revision != "default branch":
        title += f" ({revision})"

    summary = report.get("summary", {})
    lines = [
        "STATIC NODE PACK COMPATIBILITY ESTIMATE",
        title,
        "",
        (
            f"Compatible {summary.get('compatible', 0)}  |  "
            f"Partial {summary.get('partial', 0)}  |  "
            f"Unsupported {summary.get('unsupported', 0)}"
        ),
    ]
    groups = (
        ("compatible", "COMPATIBLE", "✓"),
        ("partial", "PARTIAL / NEEDS REVIEW", "⚠"),
        ("unsupported", "UNSUPPORTED", "✗"),
    )
    nodes = report.get("nodes", [])
    for status, heading, marker in groups:
        matching = [node for node in nodes if node.get("status") == status]
        if not matching:
            continue
        lines.extend(["", heading])
        for node in matching:
            class_id = _safe_report_fragment(node.get("class_id", "unknown"))
            display_name = _safe_report_fragment(node.get("display_name", class_id))
            class_name = _safe_report_fragment(node.get("class_name", "unresolved"))
            location = _safe_report_fragment(node.get("source_file", ""))
            lines.append(
                f"{marker} {display_name} [{class_id}] — {class_name} ({location})"
            )
            for reason in node.get("reasons", []):
                lines.append(f"    - {_safe_report_fragment(reason)}")

    warnings = report.get("warnings", [])
    if warnings:
        lines.extend(["", "PACK NOTES"])
        lines.extend(f"- {_safe_report_fragment(warning)}" for warning in warnings)
    if not nodes:
        lines.extend(
            [
                "",
                "No legacy V1 node classes were discovered statically.",
            ]
        )
    text = "\n".join(lines)
    if len(text) > MAX_REPORT_TEXT_CHARACTERS:
        text = (
            text[: MAX_REPORT_TEXT_CHARACTERS - 80]
            + "\n\n[Report text truncated at the configured safety limit.]"
        )
    return text


def test_node_pack(
    repository: str,
    ref_kind: str = "default",
    ref: str = "",
    subdirectory: str = "",
) -> dict[str, Any]:
    """Fetch and statically inspect a GitHub node pack."""

    if not _SCAN_SEMAPHORE.acquire(blocking=False):
        raise NodePackBusyError(
            "The compatibility tester is already handling its maximum number of scans"
        )
    try:
        fetched = fetch_repository(repository, ref_kind, ref, subdirectory)
        source = {
            "repository": fetched.source.slug,
            "url": fetched.source.url.removesuffix(".git"),
            "requested_ref": fetched.requested_ref,
            "resolved_commit": fetched.resolved_commit,
            "subdirectory": fetched.subdirectory,
        }
        return analyze_python_sources(
            fetched.python_sources,
            source=source,
            metadata_files=fetched.metadata_files,
            skipped_files=fetched.skipped_files,
        )
    finally:
        _SCAN_SEMAPHORE.release()


class ComfyNodePackTesterNode:
    """Estimate compatibility of classes in a public GitHub node pack."""

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "repository": (
                    "STRING",
                    {
                        "default": "https://github.com/owner/repository",
                        "multiline": False,
                    },
                ),
                "ref_kind": (list(SUPPORTED_REF_KINDS),),
                "ref": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "tooltip": (
                            "Branch, tag, or full commit id; empty for default"
                        ),
                    },
                ),
                "subdirectory": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": False,
                        "tooltip": "Optional node-pack folder inside a monorepo",
                    },
                ),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("report", "report_json")
    FUNCTION = "inspect_pack"
    CATEGORY = "utils/scripted"
    DESCRIPTION = (
        "Statically estimates which legacy node classes a temporary adapter "
        "could support. Repository Python is never imported or executed."
    )

    @classmethod
    def IS_CHANGED(cls, **kwargs: Any) -> float:
        return float("nan")

    def inspect_pack(
        self,
        repository: str,
        ref_kind: str = "default",
        ref: str = "",
        subdirectory: str = "",
    ) -> dict[str, Any]:
        report = test_node_pack(repository, ref_kind, ref, subdirectory)
        request_source = {
            "repository": repository.strip(),
            "ref_kind": ref_kind,
            "ref": ref.strip(),
            "subdirectory": subdirectory.strip(),
        }
        report_text = report["report_text"]
        report_json = json.dumps(
            report,
            ensure_ascii=False,
            separators=(",", ":"),
        )
        return {
            "ui": {
                "compatibility_report": [report_text],
                "compatibility_json": [report_json],
                "compatibility_source": [request_source],
            },
            "result": (report_text, report_json),
        }


def _register_node_pack_test_route() -> bool:
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

    @prompt_server.routes.post(NODE_PACK_TEST_ROUTE)
    async def inspect_node_pack(request: Any) -> Any:
        try:
            payload = await request.json()
        except Exception:
            payload = None
        if not isinstance(payload, Mapping):
            return web.json_response(
                {
                    "ok": False,
                    "code": "invalid_request",
                    "error": "Request body must be valid JSON",
                },
                status=400,
            )
        required = {
            "repository": payload.get("repository", ""),
            "ref_kind": payload.get("ref_kind", "default"),
            "ref": payload.get("ref", ""),
            "subdirectory": payload.get("subdirectory", ""),
        }
        if not all(isinstance(value, str) for value in required.values()):
            return web.json_response(
                {
                    "ok": False,
                    "code": "invalid_request",
                    "error": "Repository, ref_kind, ref, and subdirectory must be strings",
                },
                status=400,
            )
        try:
            report = await asyncio.to_thread(test_node_pack, **required)
        except NodePackTestError as exc:
            return web.json_response(
                {"ok": False, "code": exc.code, "error": str(exc)},
                status=exc.status,
            )
        except Exception:
            return web.json_response(
                {
                    "ok": False,
                    "code": "scan_failed",
                    "error": "Unexpected node-pack compatibility failure",
                },
                status=500,
            )
        return web.json_response(
            {
                "ok": True,
                "report": report,
                "report_text": report["report_text"],
            }
        )

    return True


NODE_PACK_TEST_ROUTE_REGISTERED = _register_node_pack_test_route()


__all__ = [
    "GIT_FETCH_TIMEOUT_SECONDS",
    "MAX_PYTHON_FILES",
    "MAX_SOURCE_FILE_BYTES",
    "MAX_TOTAL_SOURCE_BYTES",
    "NODE_PACK_TEST_ROUTE",
    "NODE_PACK_TEST_ROUTE_REGISTERED",
    "ComfyNodePackTesterNode",
    "FetchedNodePack",
    "GitHubSource",
    "GitUnavailableError",
    "NodePackFetchError",
    "NodePackFetchTimeout",
    "NodePackBusyError",
    "NodePackScanError",
    "NodePackTestError",
    "NodePackTooLargeError",
    "RepositoryValidationError",
    "RevisionValidationError",
    "SubdirectoryValidationError",
    "analyze_python_sources",
    "fetch_repository",
    "format_report",
    "normalize_github_source",
    "normalize_revision",
    "normalize_subdirectory",
    "test_node_pack",
]
