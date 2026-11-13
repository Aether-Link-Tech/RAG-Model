"""Centralized, fail-closed per-user storage path builder and validator (AL-253)."""

from __future__ import annotations

import os
import re
import stat
import uuid
from pathlib import Path
from typing import BinaryIO, Literal
from urllib.parse import unquote

from backend.app.core.config import settings

_SAFE_EXTENSION_RE = re.compile(r"^\.[a-z0-9]{1,10}$")
_SAFE_VARIANT_RE = re.compile(r"^[a-f0-9]{16,64}$")
_WINDOWS_DRIVE_RE = re.compile(r"^[A-Za-z]:")
_MAX_FILENAME_LEN = 255
_UNQUOTE_PASSES = 3
_STAGING_PURPOSES = frozenset({"upload", "replace", "publish", "delete"})


class UnsafeStoragePathError(ValueError):
    """A storage path would leave the owner's namespace or is otherwise unsafe."""


def _check_no_symlinks(root: Path, relative_parts: tuple[str, ...]) -> None:
    """Reject symlinks at ``root`` or any descendant component in ``relative_parts``.

    Ancestors above ``root`` (such as ``/var`` -> ``/private/var`` on macOS) are
    intentionally not checked so system temp directories work normally.
    """
    if root.is_symlink():
        raise UnsafeStoragePathError("Storage root must not be a symlink.")
    current = root
    for part in relative_parts:
        current = current / part
        if current.is_symlink():
            raise UnsafeStoragePathError("Symlinks are not permitted inside user storage.")


def _assert_contained(target: Path, directory: Path) -> Path:
    """Resolve ``target`` and verify it stays strictly inside ``directory``."""
    resolved_dir = directory.resolve()
    resolved_target = target.resolve()
    if resolved_target == resolved_dir or not resolved_target.is_relative_to(resolved_dir):
        raise UnsafeStoragePathError("Storage path escapes the user namespace.")
    return resolved_target


def validate_safe_filename(filename: str | None) -> str:
    """Validate that a client-supplied filename cannot trigger path traversal.

    Rejects empty names, NUL bytes, ASCII control characters, path separators,
    ``..`` traversal segments, Windows drive/UNC prefixes, and URL-encoded or
    double-encoded equivalents, while allowing ordinary percent characters such
    as ``"100% summary.pdf"``.
    """
    if not isinstance(filename, str) or not filename or not filename.strip():
        raise UnsafeStoragePathError("Filename must be a non-empty string.")
    if len(filename) > _MAX_FILENAME_LEN:
        raise UnsafeStoragePathError("Filename exceeds maximum allowed length.")

    candidate = filename
    for _ in range(_UNQUOTE_PASSES + 1):
        if "\x00" in candidate or any(ord(ch) < 32 or ord(ch) == 127 for ch in candidate):
            raise UnsafeStoragePathError("Filename contains illegal control characters.")
        if "/" in candidate or "\\" in candidate:
            raise UnsafeStoragePathError("Filename must not contain path separators.")
        if candidate in {".", ".."} or ".." in candidate:
            raise UnsafeStoragePathError("Filename must not contain traversal sequences.")
        if _WINDOWS_DRIVE_RE.match(candidate):
            raise UnsafeStoragePathError("Filename must not contain a drive prefix.")

        decoded = unquote(candidate)
        if decoded == candidate:
            break
        candidate = decoded

    return filename


def user_storage_dir(
    user_id: uuid.UUID | str,
    *,
    base_dir: str | Path | None = None,
    create: bool = False,
) -> Path:
    """Return ``<UPLOAD_DIR>/users/<user_id>/documents`` with symlink checks."""
    owner_id = uuid.UUID(str(user_id))
    root = Path(base_dir) if base_dir is not None else Path(settings.UPLOAD_DIR)
    rel_parts = ("users", str(owner_id), "documents")
    _check_no_symlinks(root, rel_parts)

    directory = root.joinpath(*rel_parts)
    expected_resolved = root.resolve().joinpath(*rel_parts)
    if directory.resolve() != expected_resolved:
        raise UnsafeStoragePathError("User storage directory escapes upload root.")

    if create:
        os.makedirs(directory, exist_ok=True)
    return directory


def build_storage_path(
    user_id: uuid.UUID | str,
    document_id: uuid.UUID | str,
    filename: str,
    *,
    variant: str | None = None,
    create_dir: bool = True,
) -> str:
    """Return ``<UPLOAD_DIR>/users/<user_id>/documents/<document_id>[_<variant>][<.ext>]``.

    Validates the owner and document UUIDs, sanitizes the filename and its
    extension, rejects symlinks, and verifies canonical containment inside the
    owner's directory.
    """
    owner_id = uuid.UUID(str(user_id))
    doc_id = uuid.UUID(str(document_id))
    safe_name = validate_safe_filename(filename)

    extension = Path(safe_name).suffix.lower()
    if extension and not _SAFE_EXTENSION_RE.match(extension):
        raise UnsafeStoragePathError("Unsupported file extension.")

    if variant is not None:
        if not isinstance(variant, str) or not _SAFE_VARIANT_RE.match(variant):
            raise UnsafeStoragePathError("Invalid storage path variant.")
        leaf = f"{doc_id}_{variant}{extension}"
    else:
        leaf = f"{doc_id}{extension}"

    directory = user_storage_dir(owner_id, create=False)
    _check_no_symlinks(directory, (leaf,))
    target = directory / leaf
    _assert_contained(target, directory)

    if create_dir:
        os.makedirs(directory, exist_ok=True)
    return str(target)


def build_staging_path(
    user_id: uuid.UUID | str,
    document_id: uuid.UUID | str,
    filename: str = "",
    *,
    purpose: Literal["upload", "replace", "publish", "delete"] = "upload",
    existing_path: str | Path | None = None,
    create_dir: bool = True,
) -> Path:
    """Generate a unique temporary or deletion-staging path inside ``user_storage_dir``."""
    if purpose not in _STAGING_PURPOSES:
        raise UnsafeStoragePathError(f"Unsupported staging purpose: {purpose!r}")

    owner_id = uuid.UUID(str(user_id))
    doc_id = uuid.UUID(str(document_id))
    directory = user_storage_dir(owner_id, create=False)

    if purpose == "delete":
        if existing_path is not None:
            resolve_user_file_path(owner_id, existing_path, must_exist=False)
        leaf = f"{doc_id}.{uuid.uuid4().hex}.deleting"
    else:
        extension = ""
        if filename:
            safe_name = validate_safe_filename(filename)
            extension = Path(safe_name).suffix.lower()
            if extension and not _SAFE_EXTENSION_RE.match(extension):
                raise UnsafeStoragePathError("Unsupported file extension.")
        leaf = f".{purpose}.{doc_id}.{uuid.uuid4().hex}{extension}.tmp"

    _check_no_symlinks(directory, (leaf,))
    target = directory / leaf
    _assert_contained(target, directory)

    if create_dir:
        os.makedirs(directory, exist_ok=True)
    return target


def resolve_user_file_path(
    user_id: uuid.UUID | str,
    stored_path: str | Path | None,
    *,
    base_dir: str | Path | None = None,
    must_exist: bool = False,
) -> Path:
    """Resolve ``stored_path`` and fail closed if it leaves ``user_storage_dir(user_id)``."""
    if stored_path is None:
        raise UnsafeStoragePathError("Storage path is missing.")
    raw = str(stored_path)
    if not raw or not raw.strip() or "\x00" in raw:
        raise UnsafeStoragePathError("Invalid storage path.")

    owner_id = uuid.UUID(str(user_id))
    directory = user_storage_dir(owner_id, base_dir=base_dir, create=False)
    resolved_dir = directory.resolve()

    candidate = Path(raw)
    if candidate.is_symlink():
        raise UnsafeStoragePathError("Symlinks are not permitted in user storage.")

    # Check intermediate components if the candidate is lexically under directory or resolved_dir.
    for base in (directory, resolved_dir):
        if candidate.is_relative_to(base):
            rel = candidate.relative_to(base)
            if rel.parts:
                _check_no_symlinks(base, rel.parts)
            break

    resolved_target = _assert_contained(candidate, directory)
    if resolved_target.is_symlink():
        raise UnsafeStoragePathError("Symlinks are not permitted in user storage.")

    if must_exist and not resolved_target.is_file():
        raise FileNotFoundError(f"Storage file not found: {resolved_target.name}")

    return resolved_target


def open_user_file_no_follow(
    user_id: uuid.UUID | str,
    stored_path: str | Path | None,
) -> BinaryIO:
    """Open a user's stored file for reading with ``O_NOFOLLOW`` where supported."""
    resolved = resolve_user_file_path(user_id, stored_path, must_exist=True)
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(resolved, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise UnsafeStoragePathError("Target is not a regular file.")
        return os.fdopen(fd, "rb")
    except Exception:
        os.close(fd)
        raise


def create_exclusive_staging_file(
    user_id: uuid.UUID | str,
    document_id: uuid.UUID | str,
    filename: str = "",
    *,
    purpose: Literal["upload", "replace", "publish", "delete"] = "upload",
) -> tuple[Path, BinaryIO]:
    """Create a new staging file exclusively (``O_CREAT | O_EXCL | O_NOFOLLOW``)."""
    staging_path = build_staging_path(
        user_id,
        document_id,
        filename,
        purpose=purpose,
        create_dir=True,
    )
    flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
    fd = os.open(staging_path, flags, 0o600)
    try:
        return staging_path, os.fdopen(fd, "wb")
    except Exception:
        os.close(fd)
        raise



def build_quarantine_path(
    user_id: uuid.UUID | str,
    document_id: uuid.UUID | str,
    *,
    create_dir: bool = True,
) -> Path:
    """Return ``<CLAMAV_QUARANTINE_DIR>/<user_id>/<document_id>`` with symlink checks."""
    owner_id = uuid.UUID(str(user_id))
    doc_id = uuid.UUID(str(document_id))
    q_root = Path(settings.CLAMAV_QUARANTINE_DIR)
    _check_no_symlinks(q_root, (str(owner_id), str(doc_id)))

    q_dir = q_root / str(owner_id)
    target = q_dir / str(doc_id)
    _assert_contained(target, q_dir)
    if q_dir.resolve() != q_root.resolve() / str(owner_id):
        raise UnsafeStoragePathError("Quarantine directory escapes quarantine root.")

    if create_dir:
        os.makedirs(q_dir, exist_ok=True)
    return target
