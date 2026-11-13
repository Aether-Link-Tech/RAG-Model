"""Legacy per-user storage layout migration service (AL-253 Phase 4).

Canonical layout:
    ``<UPLOAD_DIR>/users/<user_id>/documents/<document_id>[.<ext>]``

Legacy layout:
    ``<UPLOAD_DIR>/<user_id>/...``

This module plans and optionally executes a safe, repeatable migration of
legitimate legacy document files into the canonical owner-scoped layout while
updating ``documents.file_path`` transactionally. Dry-run is the default.
"""

from __future__ import annotations

import hashlib
import logging
import os
import stat
import uuid
from collections import Counter
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Awaitable, Callable, Literal, Sequence

from sqlalchemy import select, update
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from backend.app.core.config import settings
from backend.app.models.document import Document
from backend.app.services.storage_paths import (
    UnsafeStoragePathError,
    resolve_user_file_path,
    user_storage_dir,
    validate_safe_filename,
)

logger = logging.getLogger(__name__)

_SAFE_EXTENSION_RE_CHARS = set("abcdefghijklmnopqrstuvwxyz0123456789")
_COPY_CHUNK_SIZE = 1024 * 1024  # 1 MiB

MigrationStatus = Literal[
    "already_canonical",
    "orphaned_legacy_source",
    "recovered_legacy_cleanup",
    "planned",
    "migrated",
    "invalid_uuid",
    "unsafe_path",
    "symlink_source",
    "cross_user_path",
    "outside_owner_dir",
    "missing_source",
    "duplicate_destination",
    "destination_exists",
    "db_update_failed",
    "io_error",
]


@dataclass(frozen=True)
class DocumentMigrationRecord:
    """Minimal document metadata required for storage layout migration."""

    id: uuid.UUID | str
    user_id: uuid.UUID | str
    filename: str
    file_path: str | None


@dataclass
class MigrationItemResult:
    """Per-document migration plan or execution outcome."""

    document_id: str
    user_id: str
    status: MigrationStatus
    source_path: str | None = None
    destination_path: str | None = None
    detail: str = ""
    warning: str | None = None


@dataclass
class MigrationReport:
    """Aggregate report returned by ``migrate_legacy_storage``."""

    mode: Literal["dry-run", "apply"]
    upload_dir: str
    total_documents: int = 0
    already_canonical: int = 0
    orphaned_legacy_source: int = 0
    planned: int = 0
    migrated: int = 0
    recovered: int = 0
    skipped_or_failed: int = 0
    items: list[MigrationItemResult] = field(default_factory=list)

    def to_dict(self) -> dict:
        return {
            "mode": self.mode,
            "upload_dir": self.upload_dir,
            "total_documents": self.total_documents,
            "already_canonical": self.already_canonical,
            "orphaned_legacy_source": self.orphaned_legacy_source,
            "planned": self.planned,
            "migrated": self.migrated,
            "recovered": self.recovered,
            "skipped_or_failed": self.skipped_or_failed,
            "items": [asdict(item) for item in self.items],
        }


def _parse_uuid(value: uuid.UUID | str) -> uuid.UUID | None:
    try:
        return uuid.UUID(str(value))
    except (ValueError, TypeError, AttributeError):
        return None


def _has_symlink_component(path: Path, stop_at: Path) -> bool:
    """Return True if ``path`` or any ancestor below ``stop_at`` is a symlink."""
    resolved_stop = stop_at.resolve()
    current = path
    while True:
        if current.is_symlink():
            return True
        if current == stop_at or current.resolve() == resolved_stop:
            break
        parent = current.parent
        if parent == current:
            break
        current = parent
    return False


def _detect_other_user_path(candidate: Path, upload_root: Path, owner_id: uuid.UUID) -> uuid.UUID | None:
    """If ``candidate`` is inside another user's legacy or canonical directory, return that user's UUID."""
    resolved_root = upload_root.resolve()
    try:
        rel = candidate.resolve().relative_to(resolved_root)
    except ValueError:
        return None
    parts = rel.parts
    if not parts:
        return None
    if parts[0] == "users" and len(parts) >= 2:
        other = _parse_uuid(parts[1])
        if other is not None and other != owner_id:
            return other
    else:
        other = _parse_uuid(parts[0])
        if other is not None and other != owner_id:
            return other
    return None


def _build_canonical_destination(
    owner_id: uuid.UUID,
    doc_id: uuid.UUID,
    filename: str,
    source_path: Path,
    upload_root: Path,
) -> Path:
    """Compute and validate the canonical destination path without creating directories."""
    raw_name = filename.strip() if isinstance(filename, str) and filename.strip() else source_path.name
    safe_name = validate_safe_filename(raw_name)
    extension = Path(safe_name).suffix.lower()
    if extension and (
        len(extension) > 16
        or not extension.startswith(".")
        or not all(ch in _SAFE_EXTENSION_RE_CHARS for ch in extension[1:])
    ):
        raise UnsafeStoragePathError("Unsupported file extension.")
    canonical_dir = user_storage_dir(owner_id, base_dir=upload_root, create=False)
    candidate = canonical_dir / f"{doc_id}{extension}"
    return resolve_user_file_path(owner_id, candidate, base_dir=upload_root, must_exist=False)


def _sha256_file(path: Path) -> tuple[str, int]:
    """Compute (hex_sha256, byte_size) of a regular file without following symlinks."""
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    digest = hashlib.sha256()
    total_bytes = 0
    fd = os.open(path, flags)
    try:
        st = os.fstat(fd)
        if not stat.S_ISREG(st.st_mode):
            raise UnsafeStoragePathError(f"Path is not a regular file: {path}")
        while True:
            chunk = os.read(fd, _COPY_CHUNK_SIZE)
            if not chunk:
                break
            digest.update(chunk)
            total_bytes += len(chunk)
    finally:
        os.close(fd)
    return digest.hexdigest(), total_bytes


def _find_orphaned_legacy_source(
    owner_uuid: uuid.UUID,
    doc_uuid: uuid.UUID,
    filename: str,
    canonical_path: Path,
    upload_root: Path,
) -> Path | None:
    """Detect a leftover legacy source file matching ``canonical_path`` after an interrupted cleanup.

    Only inspects direct candidate files inside ``<upload_root>/<owner_uuid>/`` matching
    ``<doc_uuid><ext>`` or the sanitized document filename, and only returns a match when
    the legacy file is a non-symlink regular file with identical SHA-256 and byte size.
    """
    legacy_dir = upload_root / str(owner_uuid)
    if legacy_dir.is_symlink() or not legacy_dir.is_dir():
        return None
    if canonical_path.is_symlink() or not canonical_path.is_file():
        return None

    candidate_names: list[str] = [f"{doc_uuid}{canonical_path.suffix}"]
    try:
        safe_name = validate_safe_filename(filename)
        if safe_name not in candidate_names:
            candidate_names.append(safe_name)
    except UnsafeStoragePathError:
        pass

    resolved_legacy = legacy_dir.resolve()
    try:
        canonical_hash, canonical_size = _sha256_file(canonical_path)
    except (OSError, UnsafeStoragePathError):
        return None

    for name in candidate_names:
        candidate = legacy_dir / name
        if _has_symlink_component(candidate, upload_root):
            continue
        if not candidate.exists() or not candidate.is_file():
            continue
        resolved_candidate = candidate.resolve()
        if resolved_candidate == resolved_legacy or not resolved_candidate.is_relative_to(resolved_legacy):
            continue
        try:
            cand_hash, cand_size = _sha256_file(resolved_candidate)
        except (OSError, UnsafeStoragePathError):
            continue
        if cand_hash == canonical_hash and cand_size == canonical_size:
            return resolved_candidate
    return None


def _plan_single_document(
    doc: DocumentMigrationRecord,
    upload_root: Path,
) -> MigrationItemResult:
    doc_uuid = _parse_uuid(doc.id)
    owner_uuid = _parse_uuid(doc.user_id)
    if doc_uuid is None or owner_uuid is None:
        return MigrationItemResult(
            document_id=str(doc.id),
            user_id=str(doc.user_id),
            status="invalid_uuid",
            source_path=doc.file_path,
            detail="Document ID or user ID is not a valid UUID.",
        )

    raw_path = doc.file_path
    if not raw_path or not isinstance(raw_path, str) or not raw_path.strip() or "\x00" in raw_path:
        return MigrationItemResult(
            document_id=str(doc_uuid),
            user_id=str(owner_uuid),
            status="unsafe_path",
            source_path=raw_path,
            detail="Stored file_path is empty or contains NUL bytes.",
        )

    # 1. Check whether the document already uses the canonical owner-scoped layout.
    try:
        canonical_path = resolve_user_file_path(
            owner_uuid, raw_path, base_dir=upload_root, must_exist=False
        )
        orphaned_legacy = _find_orphaned_legacy_source(
            owner_uuid, doc_uuid, doc.filename, canonical_path, upload_root
        )
        if orphaned_legacy is not None:
            return MigrationItemResult(
                document_id=str(doc_uuid),
                user_id=str(owner_uuid),
                status="orphaned_legacy_source",
                source_path=str(orphaned_legacy),
                destination_path=str(canonical_path),
                detail=(
                    "Database already points to canonical file, but identical legacy "
                    "source file remains after an interrupted cleanup."
                ),
            )
        return MigrationItemResult(
            document_id=str(doc_uuid),
            user_id=str(owner_uuid),
            status="already_canonical",
            source_path=raw_path,
            destination_path=raw_path,
            detail="Already stored in canonical owner directory.",
        )
    except UnsafeStoragePathError:
        pass

    candidate = Path(raw_path)
    legacy_dir = upload_root / str(owner_uuid)

    # 2. Reject symlinks in the legacy directory or source path before resolving.
    if _has_symlink_component(candidate, upload_root) or legacy_dir.is_symlink():
        return MigrationItemResult(
            document_id=str(doc_uuid),
            user_id=str(owner_uuid),
            status="symlink_source",
            source_path=raw_path,
            detail="Source path or legacy owner directory contains a symlink.",
        )

    # 3. Check cross-user paths explicitly before generic outside-owner checks.
    other_user = _detect_other_user_path(candidate, upload_root, owner_uuid)
    if other_user is not None:
        return MigrationItemResult(
            document_id=str(doc_uuid),
            user_id=str(owner_uuid),
            status="cross_user_path",
            source_path=raw_path,
            detail=f"Path belongs to a different user ({other_user}).",
        )

    # 4. Enforce containment inside the expected legacy owner directory <upload_root>/<owner_id>/.
    resolved_legacy = legacy_dir.resolve()
    resolved_candidate = candidate.resolve()
    if resolved_candidate == resolved_legacy or not resolved_candidate.is_relative_to(resolved_legacy):
        return MigrationItemResult(
            document_id=str(doc_uuid),
            user_id=str(owner_uuid),
            status="outside_owner_dir",
            source_path=raw_path,
            detail=f"Path is outside legacy owner directory {legacy_dir}.",
        )

    # 5. Verify the legacy source file exists and is a regular file.
    if not resolved_candidate.exists() or not resolved_candidate.is_file():
        return MigrationItemResult(
            document_id=str(doc_uuid),
            user_id=str(owner_uuid),
            status="missing_source",
            source_path=str(resolved_candidate),
            detail="Legacy source file is missing or not a regular file.",
        )

    # 6. Compute and validate canonical destination path.
    try:
        dest_path = _build_canonical_destination(
            owner_uuid, doc_uuid, doc.filename, resolved_candidate, upload_root
        )
    except UnsafeStoragePathError as exc:
        return MigrationItemResult(
            document_id=str(doc_uuid),
            user_id=str(owner_uuid),
            status="unsafe_path",
            source_path=str(resolved_candidate),
            detail=f"Cannot build safe canonical destination: {exc}",
        )

    # 7. Never overwrite an existing destination file.
    if dest_path.exists() or dest_path.is_symlink():
        return MigrationItemResult(
            document_id=str(doc_uuid),
            user_id=str(owner_uuid),
            status="destination_exists",
            source_path=str(resolved_candidate),
            destination_path=str(dest_path),
            detail="Canonical destination already exists; refusing to overwrite.",
        )

    return MigrationItemResult(
        document_id=str(doc_uuid),
        user_id=str(owner_uuid),
        status="planned",
        source_path=str(resolved_candidate),
        destination_path=str(dest_path),
        detail="Ready to migrate to canonical layout.",
    )


def _copy_to_new_destination(source_path: Path, dest_path: Path) -> None:
    """Copy ``source_path`` to ``dest_path`` without following symlinks or overwriting.

    Writes to an exclusive temporary staging file in ``dest_path.parent`` first,
    fsyncs it, atomically hard-links it to ``dest_path`` via ``os.link`` so an
    existing destination can never be overwritten, and verifies the destination
    bytes match the source SHA-256 and size before returning.
    """
    os.makedirs(dest_path.parent, exist_ok=True)
    tmp_path = dest_path.parent / f".{dest_path.name}.{uuid.uuid4().hex}.migrating"
    read_flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    write_flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)

    src_hasher = hashlib.sha256()
    src_size = 0
    src_fd = os.open(source_path, read_flags)
    try:
        src_stat = os.fstat(src_fd)
        if not stat.S_ISREG(src_stat.st_mode):
            raise UnsafeStoragePathError("Source path is not a regular file.")

        tmp_fd = os.open(tmp_path, write_flags, 0o600)
        try:
            while True:
                chunk = os.read(src_fd, _COPY_CHUNK_SIZE)
                if not chunk:
                    break
                src_hasher.update(chunk)
                src_size += len(chunk)
                view = memoryview(chunk)
                written = 0
                while written < len(view):
                    written += os.write(tmp_fd, view[written:])
            os.fsync(tmp_fd)
        finally:
            os.close(tmp_fd)

        os.link(tmp_path, dest_path)
    finally:
        os.close(src_fd)
        if tmp_path.exists() or tmp_path.is_symlink():
            try:
                os.unlink(tmp_path)
            except OSError:
                pass

    dest_hash, dest_size = _sha256_file(dest_path)
    if dest_hash != src_hasher.hexdigest() or dest_size != src_size:
        try:
            if dest_path.exists() and not dest_path.is_symlink():
                os.unlink(dest_path)
        except OSError:
            pass
        raise OSError(
            f"Integrity verification failed after copy for {dest_path}: "
            f"expected {src_size}B/{src_hasher.hexdigest()}, got {dest_size}B/{dest_hash}."
        )


def plan_legacy_storage_migration(
    documents: Sequence[DocumentMigrationRecord],
    *,
    upload_dir: str | Path | None = None,
) -> MigrationReport:
    """Validate and plan legacy-to-canonical storage migration without modifying anything."""
    upload_root = Path(upload_dir or settings.UPLOAD_DIR).resolve()
    report = MigrationReport(
        mode="dry-run",
        upload_dir=str(upload_root),
        total_documents=len(documents),
    )

    items = [_plan_single_document(doc, upload_root) for doc in documents]

    # Detect duplicate canonical destinations across all planned items.
    dest_counts = Counter(
        item.destination_path
        for item in items
        if item.status == "planned" and item.destination_path is not None
    )
    for item in items:
        if (
            item.status == "planned"
            and item.destination_path is not None
            and dest_counts[item.destination_path] > 1
        ):
            item.status = "duplicate_destination"
            item.detail = (
                f"Multiple documents ({dest_counts[item.destination_path]}) map to "
                f"destination {item.destination_path}."
            )

    report.items = items
    for item in items:
        if item.status == "already_canonical":
            report.already_canonical += 1
        elif item.status == "orphaned_legacy_source":
            report.orphaned_legacy_source += 1
        elif item.status == "planned":
            report.planned += 1
        else:
            report.skipped_or_failed += 1
    return report


UpdatePathFn = Callable[[uuid.UUID, str], Awaitable[None]]


async def migrate_legacy_storage(
    documents: Sequence[DocumentMigrationRecord],
    *,
    update_document_path: UpdatePathFn | None = None,
    upload_dir: str | Path | None = None,
    apply: bool = False,
) -> MigrationReport:
    """Plan and (when ``apply=True``) execute legacy storage migration.

    When ``apply=False`` (the default), no filesystem or database changes are made.
    When ``apply=True``:
    - Each planned file is copied to a staging file and hard-linked into its canonical
      destination (never overwriting an existing destination).
    - ``update_document_path(document_id, destination_path)`` is awaited to persist the
      database change transactionally.
    - If the database update fails, the newly created destination file is removed and
      the original legacy file is preserved untouched so the migration can be retried.
    - The original legacy file is unlinked only after the database update succeeds.
    """
    report = plan_legacy_storage_migration(documents, upload_dir=upload_dir)
    if not apply:
        return report

    if update_document_path is None:
        raise ValueError("update_document_path callback is required when apply=True.")

    report.mode = "apply"
    upload_root = Path(report.upload_dir)
    report.planned = 0
    report.orphaned_legacy_source = 0

    for item in report.items:
        if item.status == "orphaned_legacy_source":
            assert item.source_path is not None and item.destination_path is not None
            source_path = Path(item.source_path)
            dest_path = Path(item.destination_path)
            owner_uuid = uuid.UUID(item.user_id)
            try:
                resolve_user_file_path(owner_uuid, dest_path, base_dir=upload_root, must_exist=True)
                if _has_symlink_component(source_path, upload_root):
                    raise UnsafeStoragePathError("Legacy source became a symlink.")
                src_hash, src_size = _sha256_file(source_path)
                dst_hash, dst_size = _sha256_file(dest_path)
                if src_hash != dst_hash or src_size != dst_size:
                    raise UnsafeStoragePathError("Legacy source no longer matches canonical file.")
                os.unlink(source_path)
                item.status = "recovered_legacy_cleanup"
                item.detail = "Removed leftover legacy source file matching canonical destination."
                report.recovered += 1
            except (OSError, UnsafeStoragePathError) as exc:
                item.status = "io_error"
                item.detail = f"Failed to clean up orphaned legacy source: {exc}"
                report.skipped_or_failed += 1
            continue

        if item.status != "planned":
            continue

        assert item.source_path is not None and item.destination_path is not None
        source_path = Path(item.source_path)
        dest_path = Path(item.destination_path)
        owner_uuid = uuid.UUID(item.user_id)
        doc_uuid = uuid.UUID(item.document_id)

        # Re-validate destination containment and absence immediately before copying.
        try:
            resolve_user_file_path(owner_uuid, dest_path, base_dir=upload_root, must_exist=False)
        except UnsafeStoragePathError as exc:
            item.status = "unsafe_path"
            item.detail = f"Destination failed containment check before apply: {exc}"
            report.skipped_or_failed += 1
            continue

        if _has_symlink_component(source_path, upload_root):
            item.status = "symlink_source"
            item.detail = "Source path became a symlink before apply."
            report.skipped_or_failed += 1
            continue

        try:
            _copy_to_new_destination(source_path, dest_path)
        except FileExistsError:
            item.status = "destination_exists"
            item.detail = "Canonical destination already exists; refusing to overwrite."
            report.skipped_or_failed += 1
            continue
        except (OSError, UnsafeStoragePathError) as exc:
            item.status = "io_error"
            item.detail = f"Failed to copy legacy file to canonical destination: {exc}"
            report.skipped_or_failed += 1
            continue

        # Persist DB update; roll back the new destination file if DB commit fails.
        try:
            await update_document_path(doc_uuid, str(dest_path))
        except Exception as exc:
            try:
                if dest_path.exists() and not dest_path.is_symlink():
                    os.unlink(dest_path)
            except OSError as cleanup_exc:
                logger.error(
                    "Failed to roll back destination %s after DB error: %s",
                    dest_path,
                    cleanup_exc,
                )
            item.status = "db_update_failed"
            item.detail = f"Database update failed; destination rolled back and source preserved: {exc}"
            report.skipped_or_failed += 1
            continue

        # DB update is confirmed; remove legacy source file.
        try:
            os.unlink(source_path)
        except OSError as exc:
            item.warning = f"Database updated, but failed to remove legacy source {source_path}: {exc}"
            logger.warning("%s", item.warning)

        item.status = "migrated"
        item.detail = "Migrated to canonical layout and updated database path."
        report.migrated += 1

    return report


async def run_database_migration(
    *,
    database_url: str | None = None,
    upload_dir: str | Path | None = None,
    apply: bool = False,
) -> MigrationReport:
    """Load all documents from PostgreSQL and run the legacy storage migration."""
    url = database_url or settings.DATABASE_URL
    engine = create_async_engine(url, future=True, pool_pre_ping=True)
    session_factory = async_sessionmaker(
        bind=engine,
        class_=AsyncSession,
        expire_on_commit=False,
        autocommit=False,
        autoflush=False,
    )

    try:
        async with session_factory() as session:
            result = await session.execute(
                select(Document.id, Document.user_id, Document.filename, Document.file_path)
            )
            records = [
                DocumentMigrationRecord(
                    id=row.id,
                    user_id=row.user_id,
                    filename=row.filename,
                    file_path=row.file_path,
                )
                for row in result.all()
            ]

        async def _update_path(doc_id: uuid.UUID, new_path: str) -> None:
            async with session_factory() as session:
                async with session.begin():
                    await session.execute(
                        update(Document)
                        .where(Document.id == doc_id)
                        .values(file_path=new_path)
                    )

        return await migrate_legacy_storage(
            records,
            update_document_path=_update_path if apply else None,
            upload_dir=upload_dir,
            apply=apply,
        )
    finally:
        await engine.dispose()
