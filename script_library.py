"""Managed script collection and ComfyUI nodes for loading/saving scripts.

Bundled examples are read-only. User scripts live under ComfyUI's
``models/scripted_nodes`` directory and are addressed by validated relative
names, never by caller-provided filesystem paths.
"""

from __future__ import annotations

import ctypes
import errno
import hashlib
import importlib
import os
import stat
import sys
import tempfile
import threading
import unicodedata
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any


SCRIPT_EXTENSION = ".py"
USER_SCRIPT_DIRECTORY = "scripted_nodes"
MAX_SCRIPT_BYTES = 2 * 1024 * 1024
MAX_SCRIPT_NAME_LENGTH = 240
_AT_FDCWD = -100
_RENAME_NOREPLACE = 1
_PUBLISH_FALLBACK_ERRORS = {
    errno.EACCES,
    errno.EINVAL,
    errno.EPERM,
    errno.EXDEV,
    getattr(errno, "ENOSYS", errno.EINVAL),
    getattr(errno, "ENOTSUP", errno.EINVAL),
    getattr(errno, "EOPNOTSUPP", errno.EINVAL),
}


class ScriptLibraryError(ValueError):
    """Base error for managed script collection operations."""


class ScriptNameError(ScriptLibraryError):
    """Raised when a script name is unsafe or malformed."""


class ScriptNotFoundError(ScriptLibraryError):
    """Raised when a managed script does not exist."""


class ScriptConflictError(ScriptLibraryError):
    """Raised when saving would replace a script without permission."""


class ProtectedScriptError(ScriptLibraryError):
    """Raised when an operation would modify a bundled script."""


class ScriptStorageError(ScriptLibraryError):
    """Raised when managed storage cannot be accessed safely."""


@dataclass(frozen=True)
class ScriptRecord:
    """Public metadata for a script without exposing its filesystem path."""

    id: str
    name: str
    source: str
    deletable: bool

    def to_dict(self) -> dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "source": self.source,
            "deletable": self.deletable,
        }


def normalize_script_name(value: str) -> str:
    """Return a safe POSIX-style relative ``.py`` name.

    A missing extension is added for convenience. Absolute paths, traversal,
    hidden path components, alternate separators, and control characters are
    rejected rather than rewritten.
    """

    if not isinstance(value, str):
        raise ScriptNameError("Script name must be a string")

    name = unicodedata.normalize("NFC", value.strip())
    if not name:
        raise ScriptNameError("Script name cannot be empty")
    if len(name) > MAX_SCRIPT_NAME_LENGTH:
        raise ScriptNameError(
            f"Script name cannot exceed {MAX_SCRIPT_NAME_LENGTH} characters"
        )
    if "\\" in name:
        raise ScriptNameError("Script names must use `/` between folders")
    if name.startswith("/"):
        raise ScriptNameError("Script name must be relative")
    if any(
        unicodedata.category(character) in {"Cc", "Cs"}
        for character in name
    ):
        raise ScriptNameError("Script name cannot contain control characters")
    if ":" in name:
        raise ScriptNameError("Script name cannot contain `:`")

    parts = name.split("/")
    if any(part in {"", ".", ".."} for part in parts):
        raise ScriptNameError(
            "Script name cannot contain empty, `.` or `..` path components"
        )
    if any(part.startswith(".") for part in parts):
        raise ScriptNameError("Script name cannot contain hidden path components")

    suffix = Path(parts[-1]).suffix
    if not suffix:
        parts[-1] += SCRIPT_EXTENSION
    elif suffix != SCRIPT_EXTENSION:
        raise ScriptNameError(f"Script name must end in `{SCRIPT_EXTENSION}`")

    normalized = "/".join(parts)
    if len(normalized) > MAX_SCRIPT_NAME_LENGTH:
        raise ScriptNameError(
            f"Script name cannot exceed {MAX_SCRIPT_NAME_LENGTH} characters"
        )
    return normalized


def _default_user_scripts_root() -> Path:
    """Resolve ComfyUI's model directory lazily.

    ``folder_paths`` is intentionally imported only when storage is used so
    this module remains importable by lightweight tooling and unit tests.
    """

    try:
        import folder_paths
    except ImportError as exc:
        raise ScriptStorageError(
            "ComfyUI's `folder_paths.models_dir` is unavailable"
        ) from exc

    models_dir = getattr(folder_paths, "models_dir", None)
    if not isinstance(models_dir, (str, os.PathLike)):
        raise ScriptStorageError(
            "ComfyUI's `folder_paths.models_dir` is unavailable"
        )

    try:
        models_root = Path(models_dir).expanduser().resolve(strict=True)
    except OSError as exc:
        raise ScriptStorageError(
            "ComfyUI's models directory cannot be resolved"
        ) from exc
    if not models_root.is_dir():
        raise ScriptStorageError("ComfyUI's models path is not a directory")
    return models_root / USER_SCRIPT_DIRECTORY


def _script_id(source: str, name: str) -> str:
    return f"{source}:{name}"


def _parse_script_id(script_id: str) -> tuple[str, str]:
    if not isinstance(script_id, str):
        raise ScriptNameError("Script id must be a string")
    source, separator, raw_name = script_id.partition(":")
    if not separator or source not in {"bundled", "user"}:
        raise ScriptNameError(
            "Script id must start with `bundled:` or `user:`"
        )
    name = normalize_script_name(raw_name)
    if name != raw_name:
        raise ScriptNameError("Script id must contain a canonical `.py` name")
    return source, name


def _validate_script_code(code: str) -> bytes:
    if not isinstance(code, str):
        raise ScriptLibraryError("Script code must be a string")
    if "\x00" in code:
        raise ScriptLibraryError("Script code cannot contain NUL characters")
    try:
        encoded = code.encode("utf-8")
    except UnicodeEncodeError as exc:
        raise ScriptLibraryError(
            "Script code must be valid Unicode text"
        ) from exc
    if len(encoded) > MAX_SCRIPT_BYTES:
        raise ScriptLibraryError(
            f"Script code cannot exceed {MAX_SCRIPT_BYTES} UTF-8 bytes"
        )
    return encoded


def _is_link_like(path: Path) -> bool:
    """Return whether a path is a symlink or Windows reparse point."""

    try:
        if path.is_symlink():
            return True
        if os.name == "nt":
            attributes = getattr(os.lstat(path), "st_file_attributes", 0)
            reparse_flag = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
            return bool(reparse_flag and attributes & reparse_flag)
    except OSError:
        return False
    return False


def _try_rename_noreplace(source: Path, target: Path) -> bool:
    """Atomically publish *source* on Linux without replacing *target*.

    Return ``False`` when the platform or backing filesystem does not support
    ``renameat2(RENAME_NOREPLACE)``. A destination conflict is always raised.
    """

    if not sys.platform.startswith("linux"):
        return False
    try:
        renameat2 = ctypes.CDLL(None, use_errno=True).renameat2
    except (AttributeError, OSError):
        return False

    renameat2.argtypes = [
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_int,
        ctypes.c_char_p,
        ctypes.c_uint,
    ]
    renameat2.restype = ctypes.c_int
    result = renameat2(
        _AT_FDCWD,
        os.fsencode(source),
        _AT_FDCWD,
        os.fsencode(target),
        _RENAME_NOREPLACE,
    )
    if result == 0:
        return True

    error_number = ctypes.get_errno()
    if error_number == errno.EEXIST:
        raise FileExistsError(
            error_number,
            os.strerror(error_number),
            os.fspath(target),
        )
    if error_number in _PUBLISH_FALLBACK_ERRORS:
        return False
    raise OSError(
        error_number,
        os.strerror(error_number),
        os.fspath(target),
    )


def _try_hardlink_publish(source: Path, target: Path) -> bool:
    """Publish a completed temporary file with no replacement when supported."""

    try:
        try:
            os.link(source, target, follow_symlinks=False)
        except (NotImplementedError, TypeError):
            os.link(source, target)
    except FileExistsError:
        raise
    except OSError as exc:
        if exc.errno in _PUBLISH_FALLBACK_ERRORS:
            return False
        raise
    os.unlink(source)
    return True


def _fsync_directory(directory: Path) -> None:
    """Best-effort durability barrier for a completed directory mutation."""

    flags = os.O_RDONLY
    if hasattr(os, "O_DIRECTORY"):
        flags |= os.O_DIRECTORY
    try:
        descriptor = os.open(directory, flags)
    except OSError:
        return
    try:
        os.fsync(descriptor)
    except OSError:
        pass
    finally:
        os.close(descriptor)


class ScriptLibrary:
    """Two-tier collection of bundled and user-owned Python scripts."""

    def __init__(
        self,
        *,
        bundled_root: str | os.PathLike[str] | None = None,
        user_root: str | os.PathLike[str] | None = None,
    ):
        self.bundled_root = Path(
            bundled_root
            if bundled_root is not None
            else Path(__file__).resolve().parent / "scripts"
        )
        self._configured_user_root = (
            Path(user_root) if user_root is not None else None
        )
        self._lock = threading.RLock()

    @property
    def user_root(self) -> Path:
        if self._configured_user_root is not None:
            return self._configured_user_root
        return _default_user_scripts_root()

    def _ensure_user_root(self) -> Path:
        root = self.user_root
        try:
            if _is_link_like(root):
                raise ScriptStorageError(
                    "User script directory cannot be a symbolic link"
                )
            root.mkdir(parents=True, exist_ok=True)
            if _is_link_like(root) or not root.is_dir():
                raise ScriptStorageError(
                    "User script storage is not a regular directory"
                )
            return root.resolve(strict=True)
        except ScriptLibraryError:
            raise
        except OSError as exc:
            raise ScriptStorageError(
                "User script directory could not be created"
            ) from exc

    @staticmethod
    def _ensure_safe_parent(root: Path, name: str) -> Path:
        current = root
        for component in name.split("/")[:-1]:
            current = current / component
            try:
                if _is_link_like(current):
                    raise ScriptStorageError(
                        "Script folders cannot be symbolic links"
                    )
                current.mkdir(exist_ok=True)
                if _is_link_like(current) or not current.is_dir():
                    raise ScriptStorageError(
                        "A script folder is not a regular directory"
                    )
            except ScriptLibraryError:
                raise
            except OSError as exc:
                raise ScriptStorageError(
                    "A script folder could not be created"
                ) from exc
        return current

    @staticmethod
    def _checked_existing_path(root: Path, name: str) -> Path:
        try:
            canonical_root = root.resolve(strict=True)
        except OSError as exc:
            raise ScriptStorageError("Script collection is unavailable") from exc

        current = canonical_root
        for component in name.split("/"):
            current = current / component
            if _is_link_like(current):
                raise ScriptStorageError("Symbolic links are not valid scripts")

        try:
            resolved = current.resolve(strict=True)
        except FileNotFoundError as exc:
            raise ScriptNotFoundError(f"Script `{name}` was not found") from exc
        except OSError as exc:
            raise ScriptStorageError(f"Script `{name}` cannot be resolved") from exc

        try:
            resolved.relative_to(canonical_root)
        except ValueError as exc:
            raise ScriptStorageError(
                "Script resolved outside the managed collection"
            ) from exc
        if not resolved.is_file():
            raise ScriptNotFoundError(f"Script `{name}` was not found")
        return resolved

    @staticmethod
    def _read_script(path: Path, name: str) -> str:
        flags = os.O_RDONLY
        if hasattr(os, "O_NOFOLLOW"):
            flags |= os.O_NOFOLLOW
        try:
            descriptor = os.open(path, flags)
        except OSError as exc:
            raise ScriptStorageError(f"Script `{name}` could not be opened") from exc

        try:
            file_stat = os.fstat(descriptor)
            if not stat.S_ISREG(file_stat.st_mode):
                raise ScriptStorageError(f"Script `{name}` is not a regular file")
            if file_stat.st_size > MAX_SCRIPT_BYTES:
                raise ScriptStorageError(
                    f"Script `{name}` exceeds the {MAX_SCRIPT_BYTES}-byte limit"
                )
            with os.fdopen(descriptor, "rb", closefd=True) as script_file:
                descriptor = -1
                data = script_file.read(MAX_SCRIPT_BYTES + 1)
        finally:
            if descriptor >= 0:
                os.close(descriptor)

        if len(data) > MAX_SCRIPT_BYTES:
            raise ScriptStorageError(
                f"Script `{name}` exceeds the {MAX_SCRIPT_BYTES}-byte limit"
            )
        try:
            return data.decode("utf-8")
        except UnicodeDecodeError as exc:
            raise ScriptStorageError(
                f"Script `{name}` is not valid UTF-8"
            ) from exc

    @staticmethod
    def _records_in(root: Path, source: str) -> list[ScriptRecord]:
        if not root.exists() or not root.is_dir() or _is_link_like(root):
            return []

        records: list[ScriptRecord] = []
        for directory, subdirectories, filenames in os.walk(
            root, followlinks=False
        ):
            directory_path = Path(directory)
            subdirectories[:] = [
                child
                for child in subdirectories
                if not child.startswith(".")
                and not _is_link_like(directory_path / child)
            ]
            for filename in filenames:
                path = directory_path / filename
                if (
                    _is_link_like(path)
                    or not path.is_file()
                    or path.suffix != SCRIPT_EXTENSION
                ):
                    continue
                try:
                    name = path.relative_to(root).as_posix()
                    if normalize_script_name(name) != name:
                        continue
                except (ValueError, ScriptNameError):
                    continue
                records.append(
                    ScriptRecord(
                        id=_script_id(source, name),
                        name=name,
                        source=source,
                        deletable=source == "user",
                    )
                )
        return records

    def list_scripts(self) -> list[ScriptRecord]:
        with self._lock:
            user_root = self._ensure_user_root()
            records = self._records_in(self.bundled_root, "bundled")
            records.extend(self._records_in(user_root, "user"))
            return sorted(
                records,
                key=lambda item: (
                    item.name.casefold(),
                    0 if item.source == "user" else 1,
                    item.id,
                ),
            )

    def load(self, script_id: str) -> tuple[ScriptRecord, str]:
        source, name = _parse_script_id(script_id)
        with self._lock:
            root = (
                self.bundled_root
                if source == "bundled"
                else self._ensure_user_root()
            )
            path = self._checked_existing_path(root, name)
            code = self._read_script(path, name)
            record = ScriptRecord(
                id=_script_id(source, name),
                name=name,
                source=source,
                deletable=source == "user",
            )
            return record, code

    def save(
        self,
        name: str,
        code: str,
        *,
        overwrite: bool = False,
    ) -> ScriptRecord:
        canonical_name = normalize_script_name(name)
        encoded = _validate_script_code(code)
        if not isinstance(overwrite, bool):
            raise ScriptLibraryError("`overwrite` must be a boolean")

        with self._lock:
            root = self._ensure_user_root()
            parent = self._ensure_safe_parent(root, canonical_name)
            target = parent / canonical_name.rsplit("/", 1)[-1]
            if _is_link_like(target):
                raise ScriptStorageError(
                    "Symbolic links cannot be overwritten as scripts"
                )
            target_mode = 0o644
            if target.exists():
                if not target.is_file():
                    raise ScriptStorageError(
                        "The requested script name is not a regular file"
                    )
                if not overwrite:
                    raise ScriptConflictError(
                        f"Script `{canonical_name}` already exists"
                    )
                target_mode = stat.S_IMODE(
                    os.stat(target, follow_symlinks=False).st_mode
                )

            descriptor = -1
            temporary_path: str | None = None
            remove_incomplete_target = False
            incomplete_identity: tuple[int, int] | None = None
            try:
                descriptor, temporary_path = tempfile.mkstemp(
                    prefix=".scripted-node-",
                    suffix=".tmp",
                    dir=parent,
                )
                if hasattr(os, "fchmod"):
                    os.fchmod(descriptor, target_mode)
                with os.fdopen(
                    descriptor, "wb", closefd=True
                ) as script_file:
                    descriptor = -1
                    script_file.write(encoded)
                    script_file.flush()
                    os.fsync(script_file.fileno())

                temporary = Path(temporary_path)
                if overwrite:
                    os.replace(temporary_path, target)
                    temporary_path = None
                else:
                    try:
                        published = _try_rename_noreplace(
                            temporary,
                            target,
                        )
                        if not published:
                            published = _try_hardlink_publish(
                                temporary,
                                target,
                            )
                    except FileExistsError as exc:
                        raise ScriptConflictError(
                            f"Script `{canonical_name}` already exists"
                        ) from exc
                    if published:
                        temporary_path = None
                    else:
                        # Last-resort portable publication. O_EXCL preserves
                        # the no-overwrite guarantee, but a process crash
                        # during this short write can leave a partial file.
                        flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL
                        if hasattr(os, "O_NOFOLLOW"):
                            flags |= os.O_NOFOLLOW
                        try:
                            descriptor = os.open(target, flags, target_mode)
                        except FileExistsError as exc:
                            raise ScriptConflictError(
                                f"Script `{canonical_name}` already exists"
                            ) from exc
                        created_stat = os.fstat(descriptor)
                        incomplete_identity = (
                            created_stat.st_dev,
                            created_stat.st_ino,
                        )
                        remove_incomplete_target = True
                        with os.fdopen(
                            descriptor, "wb", closefd=True
                        ) as script_file:
                            descriptor = -1
                            script_file.write(encoded)
                            script_file.flush()
                            os.fsync(script_file.fileno())
                        remove_incomplete_target = False

                _fsync_directory(parent)
            except ScriptLibraryError:
                raise
            except OSError as exc:
                raise ScriptStorageError(
                    f"Script `{canonical_name}` could not be saved"
                ) from exc
            finally:
                if descriptor >= 0:
                    os.close(descriptor)
                if temporary_path is not None:
                    try:
                        os.unlink(temporary_path)
                    except FileNotFoundError:
                        pass
                if (
                    remove_incomplete_target
                    and incomplete_identity is not None
                ):
                    try:
                        current_stat = os.lstat(target)
                        current_identity = (
                            current_stat.st_dev,
                            current_stat.st_ino,
                        )
                        if current_identity == incomplete_identity:
                            target.unlink()
                    except OSError:
                        pass

            return ScriptRecord(
                id=_script_id("user", canonical_name),
                name=canonical_name,
                source="user",
                deletable=True,
            )

    def delete(self, script_id: str) -> ScriptRecord:
        source, name = _parse_script_id(script_id)
        if source != "user":
            raise ProtectedScriptError("Bundled scripts are read-only")

        with self._lock:
            root = self._ensure_user_root()
            target = self._checked_existing_path(root, name)
            record = ScriptRecord(
                id=_script_id("user", name),
                name=name,
                source="user",
                deletable=True,
            )
            try:
                target.unlink()
            except OSError as exc:
                raise ScriptStorageError(
                    f"Script `{name}` could not be deleted"
                ) from exc

            # Remove only empty collection folders, stopping at the managed
            # root. Failure is harmless because the script itself is gone.
            current = target.parent
            while current != root:
                try:
                    current.rmdir()
                except OSError:
                    break
                current = current.parent
            return record


_DEFAULT_LIBRARY: ScriptLibrary | None = None
_DEFAULT_LIBRARY_LOCK = threading.Lock()


def get_script_library() -> ScriptLibrary:
    global _DEFAULT_LIBRARY
    if _DEFAULT_LIBRARY is None:
        with _DEFAULT_LIBRARY_LOCK:
            if _DEFAULT_LIBRARY is None:
                _DEFAULT_LIBRARY = ScriptLibrary()
    return _DEFAULT_LIBRARY


class ComfyScriptBrowserNode:
    """Load a managed script and emit its source for a Scripted Node."""

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        try:
            choices = [
                record.id for record in get_script_library().list_scripts()
            ]
        except ScriptLibraryError:
            choices = []
        return {
            "required": {
                "script_name": (
                    choices or [""],
                    {"tooltip": "Saved or bundled script to load"},
                ),
            },
        }

    RETURN_TYPES = ("STRING",)
    RETURN_NAMES = ("script",)
    FUNCTION = "load_script"
    CATEGORY = "utils/scripted"
    DESCRIPTION = (
        "Loads a managed Python script. Connect its output to a Scripted Node."
    )

    def load_script(self, script_name: str) -> tuple[str]:
        _, code = get_script_library().load(script_name)
        return (code,)

    @classmethod
    def IS_CHANGED(cls, script_name: str) -> str:
        try:
            _, code = get_script_library().load(script_name)
        except ScriptLibraryError:
            return "missing"
        return hashlib.sha256(code.encode("utf-8")).hexdigest()


class ComfySaveScriptNode:
    """Save trusted source into ComfyUI's managed user script collection."""

    @classmethod
    def INPUT_TYPES(cls) -> dict[str, Any]:
        return {
            "required": {
                "script_name": (
                    "STRING",
                    {"default": "my_script", "multiline": False},
                ),
                "code": (
                    "STRING",
                    {
                        "default": "",
                        "multiline": True,
                        "dynamicPrompts": False,
                    },
                ),
                "overwrite": ("BOOLEAN", {"default": False}),
            },
        }

    RETURN_TYPES = ("STRING", "STRING")
    RETURN_NAMES = ("script", "saved_name")
    FUNCTION = "save_script"
    CATEGORY = "utils/scripted"
    OUTPUT_NODE = True
    DESCRIPTION = (
        "Saves Python source under ComfyUI models/scripted_nodes. "
        "Existing scripts require Overwrite."
    )

    @classmethod
    def IS_CHANGED(cls, **kwargs: Any) -> float:
        # Saving is an intentional side effect and must never be satisfied by
        # ComfyUI's execution cache.
        return float("nan")

    def save_script(
        self,
        script_name: str,
        code: str,
        overwrite: bool = False,
    ) -> tuple[str, str]:
        record = get_script_library().save(
            script_name,
            code,
            overwrite=overwrite,
        )
        return code, record.name


def _error_status(error: ScriptLibraryError) -> int:
    if isinstance(error, ScriptNotFoundError):
        return 404
    if isinstance(error, ScriptConflictError):
        return 409
    if isinstance(error, ProtectedScriptError):
        return 403
    if isinstance(error, ScriptStorageError):
        return 500
    return 400


def _register_script_library_routes() -> bool:
    """Register collection endpoints when loaded by a running ComfyUI."""

    server_module = sys.modules.get("server")
    if server_module is None:
        return False
    try:
        from aiohttp import web
    except ImportError:
        return False

    PromptServer = getattr(server_module, "PromptServer", None)
    prompt_server = (
        getattr(PromptServer, "instance", None)
        if PromptServer is not None
        else None
    )
    if prompt_server is None or not hasattr(prompt_server, "routes"):
        return False

    def error_response(error: ScriptLibraryError) -> Any:
        return web.json_response(
            {"ok": False, "error": str(error)},
            status=_error_status(error),
        )

    async def json_payload(request: Any) -> Mapping[str, Any] | None:
        try:
            payload = await request.json()
        except Exception:
            return None
        return payload if isinstance(payload, Mapping) else None

    def schema_payload(code: str) -> dict[str, Any]:
        """Analyze loaded code without making invalid drafts unloadable."""

        try:
            module_name = (
                f"{__package__}.scripted_node"
                if __package__
                else "scripted_node"
            )
            scripted_node = importlib.import_module(module_name)
            schema = scripted_node.parse_script_schema(code)
            return {
                "schema": schema.to_dict(),
                "schema_json": scripted_node.schema_to_json(schema),
            }
        except Exception as exc:
            return {
                "schema": None,
                "schema_json": "",
                "schema_error": str(exc),
            }

    @prompt_server.routes.get("/scripted_nodes/scripts")
    async def list_scripts(request: Any) -> Any:
        try:
            scripts = [
                record.to_dict()
                for record in get_script_library().list_scripts()
            ]
        except ScriptLibraryError as exc:
            return error_response(exc)
        return web.json_response({"ok": True, "scripts": scripts})

    @prompt_server.routes.post("/scripted_nodes/scripts/load")
    async def load_script(request: Any) -> Any:
        payload = await json_payload(request)
        if payload is None or not isinstance(payload.get("id"), str):
            return web.json_response(
                {"ok": False, "error": "Request JSON must contain string `id`"},
                status=400,
            )
        try:
            record, code = get_script_library().load(payload["id"])
        except ScriptLibraryError as exc:
            return error_response(exc)
        return web.json_response(
            {
                "ok": True,
                "script": record.to_dict(),
                "code": code,
                **schema_payload(code),
            }
        )

    @prompt_server.routes.post("/scripted_nodes/scripts")
    async def save_script(request: Any) -> Any:
        payload = await json_payload(request)
        if (
            payload is None
            or not isinstance(payload.get("name"), str)
            or not isinstance(payload.get("code"), str)
        ):
            return web.json_response(
                {
                    "ok": False,
                    "error": (
                        "Request JSON must contain string `name` and `code`"
                    ),
                },
                status=400,
            )
        overwrite = payload.get("overwrite", False)
        if not isinstance(overwrite, bool):
            return web.json_response(
                {"ok": False, "error": "`overwrite` must be a boolean"},
                status=400,
            )
        try:
            record = get_script_library().save(
                payload["name"],
                payload["code"],
                overwrite=overwrite,
            )
        except ScriptLibraryError as exc:
            return error_response(exc)
        return web.json_response(
            {
                "ok": True,
                "script": record.to_dict(),
                "code": payload["code"],
            },
            status=201,
        )

    @prompt_server.routes.delete("/scripted_nodes/scripts")
    async def delete_script(request: Any) -> Any:
        payload = await json_payload(request)
        if payload is None or not isinstance(payload.get("id"), str):
            return web.json_response(
                {"ok": False, "error": "Request JSON must contain string `id`"},
                status=400,
            )
        try:
            record = get_script_library().delete(payload["id"])
        except ScriptLibraryError as exc:
            return error_response(exc)
        return web.json_response(
            {"ok": True, "deleted": record.to_dict()}
        )

    return True


SCRIPT_LIBRARY_ROUTES_REGISTERED = _register_script_library_routes()


__all__ = [
    "MAX_SCRIPT_BYTES",
    "MAX_SCRIPT_NAME_LENGTH",
    "SCRIPT_EXTENSION",
    "SCRIPT_LIBRARY_ROUTES_REGISTERED",
    "USER_SCRIPT_DIRECTORY",
    "ComfySaveScriptNode",
    "ComfyScriptBrowserNode",
    "ProtectedScriptError",
    "ScriptConflictError",
    "ScriptLibrary",
    "ScriptLibraryError",
    "ScriptNameError",
    "ScriptNotFoundError",
    "ScriptRecord",
    "ScriptStorageError",
    "get_script_library",
    "normalize_script_name",
]
