"""Publish a private document as a public one (FR-082 / AL-185).

Publishing is OWNER ONLY. It is a copy-then-delete performed by the
`publish_document` SECURITY DEFINER function (AL-184), never an UPDATE of
`visibility` (the AL-109 trigger forbids that). Non-owners, admins included,
get the same 404 as for a document that does not exist.

Order of operations:
  0. owner-scoped preflight (no file is touched for non-owners)
  1. new id and path     2. copy file     3. publish_document() + commit
  4. copy private chunks to the public collection (no re-embedding)
  5. delete the old private chunks     6. remove the old private file

Failure rules after step 3 never try to restore the private row: the function
committed the insert and the delete in one transaction.
"""
from __future__ import annotations

import asyncio
import logging
import os
import shutil
import uuid
from dataclasses import dataclass
from pathlib import Path

from fastapi import status
from sqlalchemy import select, text
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.models.document import Document
from backend.app.models.document_image import DocumentImage
from backend.app.models.fingerprint import DocumentFingerprint
from backend.app.services.document_images import image_folder
from backend.app.services import audit_log, near_duplicate
from backend.app.services.document_ingest import index_document
from backend.app.services.storage_paths import (
    UnsafeStoragePathError,
    build_storage_path,
    create_exclusive_staging_file,
    open_user_file_no_follow,
    resolve_user_file_path,
)
from backend.app.services.vector_store import (
    copy_private_chunks_to_public,
    delete_document_chunks,
)

logger = logging.getLogger(__name__)

NOT_FOUND_DETAIL = "Document not found"
NOT_INDEXED_DETAIL = "Document must be indexed before it can be published"
ALREADY_PUBLISHED_DETAIL = "This document is already published."
COPY_FAILED_DETAIL = "Failed to copy the document file."
PUBLISH_FAILED_DETAIL = "The document could not be published."

_UNIQUE_VIOLATION = "23505"


class PublishError(Exception):
    """A publish failure that maps directly to an HTTP response."""

    def __init__(self, status_code: int, detail: str) -> None:
        super().__init__(detail)
        self.status_code = status_code
        self.detail = detail


@dataclass
class PublishResult:
    document: Document
    warning: str | None = None


def _sqlstate(exc: DBAPIError) -> str | None:
    orig = getattr(exc, "orig", None)
    if orig is None:
        return None
    causes = [orig, getattr(orig, "__cause__", None), getattr(orig, "_asyncpg_error", None)]
    return next(
        (s for s in (getattr(c, "sqlstate", None) or getattr(c, "pgcode", None) for c in causes) if s),
        None,
    )


def _remove_file(path: str | Path | None) -> bool:
    """Best-effort removal; returns False when an existing file could not be removed."""
    if not path:
        return True
    try:
        os.remove(path)
    except FileNotFoundError:
        return True
    except OSError:
        return False
    return True


def _copy_file(owner_id: uuid.UUID, new_id: uuid.UUID, source: Path, destination: str) -> None:
    """Copy via an exclusive staging file inside ``owner_id``'s directory."""
    tmp_path, dst_file = create_exclusive_staging_file(
        owner_id, new_id, source.name, purpose="publish"
    )
    try:
        with dst_file, open_user_file_no_follow(owner_id, source) as src_file:
            shutil.copyfileobj(src_file, dst_file)
            dst_file.flush()
        os.replace(tmp_path, destination)
    except BaseException:
        _remove_file(tmp_path)
        raise


async def _safe_rollback(db: AsyncSession) -> None:
    try:
        await db.rollback()
    except Exception:
        pass


async def _audit_denied(caller_id: uuid.UUID, filename: str | None, reason: str, ip: str) -> None:
    await audit_log.record_security_event(
        event_type=audit_log.ACCESS_DENIED,
        user_id=caller_id,
        filename=filename,
        reason=reason,
        ip_address=None if ip == "unknown" else ip,
    )


async def publish_private_document(
    db: AsyncSession,
    *,
    caller_id: uuid.UUID,
    document_id: uuid.UUID,
    client_ip: str = "unknown",
) -> PublishResult:
    """Publish `document_id` for `caller_id`. Raises `PublishError` on 404/409/500."""
    # --- 0. Owner-scoped preflight (defence in depth; the function re-checks under FOR UPDATE).
    found = (
        await db.execute(select(Document).where(Document.id == document_id))
    ).scalar_one_or_none()
    if found is None or found.user_id != caller_id:
        # Unknown id, someone else's document, or an admin: indistinguishable 404.
        await _audit_denied(
            caller_id, None, "Publish denied: not the owner or document not found", client_ip
        )
        raise PublishError(status.HTTP_404_NOT_FOUND, NOT_FOUND_DETAIL)

    old_id = found.id
    owner_id = found.user_id
    filename = found.filename
    old_path = found.file_path
    visibility = found.visibility
    doc_status = found.status

    if visibility == "public":
        await _audit_denied(caller_id, filename, "Publish denied: already public", client_ip)
        raise PublishError(status.HTTP_409_CONFLICT, ALREADY_PUBLISHED_DETAIL)
    if doc_status != "indexed":
        await _audit_denied(caller_id, filename, "Publish denied: not indexed", client_ip)
        raise PublishError(status.HTTP_409_CONFLICT, NOT_INDEXED_DETAIL)

    # --- 1 + 2. New id/path, then copy the file. Nothing in the DB has changed yet.
    new_id = uuid.uuid4()
    new_path: str | None = None
    try:
        resolved_old_path = resolve_user_file_path(owner_id, old_path, must_exist=True)
        new_path = build_storage_path(owner_id, new_id, resolved_old_path.name)
        await asyncio.to_thread(_copy_file, owner_id, new_id, resolved_old_path, new_path)
    except (OSError, UnsafeStoragePathError, ValueError) as exc:
        logger.exception("Publish: could not copy file for document %s", old_id)
        _remove_file(new_path)
        raise PublishError(status.HTTP_500_INTERNAL_SERVER_ERROR, COPY_FAILED_DETAIL) from exc

    # FR-081: keep the near-duplicate fingerprint. Deleting the private row deletes its
    # fingerprint too, and copied vectors are not re-indexed, so re-attach it below.
    fingerprint = (
        await db.execute(
            select(DocumentFingerprint.signature).where(DocumentFingerprint.document_id == old_id)
        )
    ).scalar_one_or_none()

    # FR-058: extracted images (rows cascade-delete with the private row; files stay).
    images = (
        await db.execute(select(DocumentImage).where(DocumentImage.document_id == old_id))
    ).scalars().all()
    image_rows = [
        {column.name: getattr(image, column.key) for column in DocumentImage.__table__.columns}
        for image in images
    ]

    # --- 3. publish_document(): inserts the public row and deletes the private row.
    try:
        await db.execute(
            text("SELECT * FROM publish_document(:old_id, :new_id, :new_path)"),
            {"old_id": old_id, "new_id": new_id, "new_path": new_path},
        )
        await db.commit()
    except DBAPIError as exc:
        await _safe_rollback(db)
        _remove_file(new_path)
        sqlstate = _sqlstate(exc)
        if sqlstate in ("42501", "P0002"):
            await _audit_denied(
                caller_id, filename, "Publish denied: not the owner or document not found", client_ip
            )
            raise PublishError(status.HTTP_404_NOT_FOUND, NOT_FOUND_DETAIL) from exc
        if sqlstate == "P0001":
            raise PublishError(status.HTTP_409_CONFLICT, NOT_INDEXED_DETAIL) from exc
        if sqlstate == _UNIQUE_VIOLATION:
            await _audit_denied(caller_id, filename, "Publish denied: duplicate public content", client_ip)
            raise PublishError(status.HTTP_409_CONFLICT, ALREADY_PUBLISHED_DETAIL) from exc
        logger.exception("Publish: publish_document failed for document %s", old_id)
        raise PublishError(status.HTTP_500_INTERNAL_SERVER_ERROR, PUBLISH_FAILED_DETAIL) from exc
    except BaseException:
        await _safe_rollback(db)
        _remove_file(new_path)
        raise

    new_document = (
        await db.execute(select(Document).where(Document.id == new_id))
    ).scalar_one()

    # --- 4. Move the vectors into the public collection.
    indexed = True
    try:
        copied = await asyncio.to_thread(
            copy_private_chunks_to_public,
            user_id=owner_id,
            old_document_id=old_id,
            new_document_id=new_id,
        )
        if copied == 0:
            # Nothing to copy (e.g. the private index was lost): index from the new file.
            new_document = await index_document(db, new_document)
            indexed = new_document.status == "indexed"
        else:
            if fingerprint is not None:
                await _keep_fingerprint(db, new_document, fingerprint)
            if image_rows:
                await _keep_images(db, owner_id, old_id, new_id, image_rows)
    except Exception:
        logger.exception(
            "Publish: public indexing failed for new document %s (old document %s; "
            "orphaned old file left at %s)",
            new_id,
            old_id,
            old_path,
        )
        indexed = False
        try:
            delete_document_chunks(user_id=owner_id, document_id=new_id, visibility="public")
        except Exception:
            logger.exception("Publish: could not clear partial public chunks for %s", new_id)
        await _safe_rollback(db)
        new_document = (
            await db.execute(select(Document).where(Document.id == new_id))
        ).scalar_one()
        new_document.status = "failed"
        new_document.error_message = "Public indexing failed after publishing; indexing must be retried."
        await db.commit()
        await db.refresh(new_document)

    audit_filename = filename
    if not indexed:
        # Keep the copied file and the new public row so indexing can be retried.
        await _audit_published(
            owner_id, audit_filename, old_id, new_id, client_ip, indexing_failed=True
        )
        return PublishResult(document=new_document)

    # --- 5. Remove the old private chunks.
    warning: str | None = None
    try:
        delete_document_chunks(user_id=owner_id, document_id=old_id, visibility="private")
    except Exception:
        logger.exception(
            "Publish: old private chunks for document %s could not be deleted", old_id
        )
        warning = "The document was published, but its old private index entries could not be removed."

    # --- 6. Remove the old private file (the publish already succeeded).
    if not _remove_file(resolved_old_path):
        logger.error("Publish: old private file for document %s could not be removed", old_id)

    await _audit_published(owner_id, audit_filename, old_id, new_id, client_ip)
    return PublishResult(document=new_document, warning=warning)


async def _keep_images(db: AsyncSession, owner_id, old_id, new_id, rows: list[dict]) -> None:
    """Move extracted images to the public copy: folder and document_images rows (FR-058)."""
    old_folder, new_folder = image_folder(owner_id, old_id), image_folder(owner_id, new_id)
    try:
        if old_folder.is_dir():
            new_folder.parent.mkdir(parents=True, exist_ok=True)
            old_folder.rename(new_folder)
        for row in rows:
            row = dict(row, id=uuid.uuid4(), document_id=new_id,
                       file_path=str(new_folder / Path(row["file_path"]).name))
            row.pop("created_at", None)
            db.add(DocumentImage(**row))
        await db.commit()
    except Exception:  # never fail a publish over image metadata
        logger.exception("Publish: could not move extracted images for %s", new_id)
        await _safe_rollback(db)


async def _keep_fingerprint(db: AsyncSession, document: Document, signature: bytes) -> None:
    """Store the published copy's fingerprint (now public) so later uploads are compared with it."""
    try:
        await near_duplicate.store_fingerprint(db, document, near_duplicate.from_bytes(signature))
        await db.commit()
    except Exception:  # never fail a publish over the duplicate index
        logger.exception("Publish: could not keep the near-duplicate fingerprint for %s", document.id)
        await _safe_rollback(db)


async def _audit_published(
    owner_id: uuid.UUID,
    filename: str | None,
    old_id: uuid.UUID,
    new_id: uuid.UUID,
    ip: str,
    *,
    indexing_failed: bool = False,
) -> None:
    if indexing_failed:
        reason = (
            f"Publish completed with failed indexing: private document {old_id} "
            f"is now public document {new_id} with status failed"
        )
    else:
        reason = f"Published private document {old_id} as public document {new_id}"
    await audit_log.record_security_event(
        event_type=audit_log.DOCUMENT_PUBLISHED,
        user_id=owner_id,
        filename=filename,
        reason=reason,
        ip_address=None if ip == "unknown" else ip,
    )
