"""Store embedded images, OCR them within a time budget, skip failures (FR-058 – FR-063).

Images are written to the owner's own folder (<UPLOAD_DIR>/users/<id>/documents/images/
<document_id>/) and recorded in document_images (owner-only RLS). OCR runs in batches
against the OCR worker; each document gets OCR_DOCUMENT_BUDGET_SECONDS in total. An image
that fails, times out, or is left when the budget runs out is skipped, logged and written
to the security audit log for admins; it never fails the document.
"""
from __future__ import annotations

import base64
import binascii
import errno
import logging
import os
import time
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

from sqlalchemy import delete
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.models.document import Document
from backend.app.models.document_image import DocumentImage
from backend.app.services import audit_log
from backend.app.services.ocr import OCRFailure, get_ocr_client
from backend.app.services.storage_paths import (
    UnsafeStoragePathError,
    _assert_contained,
    _check_no_symlinks,
    user_storage_dir,
)

logger = logging.getLogger(__name__)


@dataclass
class ImageOCR:
    index: int
    image_id: uuid.UUID
    page: Optional[int]
    bbox: Optional[list]
    status: str  # "done" | "skipped"
    text: str = ""
    confidence: Optional[float] = None
    engine: Optional[str] = None
    error: Optional[str] = None


def image_folder(user_id: uuid.UUID, document_id: uuid.UUID) -> Path:
    """Inside the owner's storage namespace (AL-139), one folder per document."""
    owner_id = uuid.UUID(str(user_id))
    doc_id = uuid.UUID(str(document_id))
    base = user_storage_dir(owner_id)
    _check_no_symlinks(base, ("images", str(doc_id)))
    folder = base / "images" / str(doc_id)
    _assert_contained(folder, base)
    return folder


def _safe_rmtree_fd(dir_fd: int) -> None:
    """Recursively clear a directory via an already-opened ``O_NOFOLLOW`` descriptor."""
    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    with os.scandir(dir_fd) as entries:
        for entry in entries:
            if entry.is_symlink() or not entry.is_dir(follow_symlinks=False):
                os.unlink(entry.name, dir_fd=dir_fd)
            else:
                sub_fd = os.open(entry.name, dir_flags, dir_fd=dir_fd)
                try:
                    _safe_rmtree_fd(sub_fd)
                finally:
                    os.close(sub_fd)
                os.rmdir(entry.name, dir_fd=dir_fd)


def remove_image_files(user_id: uuid.UUID, document_id: uuid.UUID) -> None:
    folder = image_folder(user_id, document_id)
    dir_flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0) | getattr(os, "O_NOFOLLOW", 0)
    try:
        dir_fd = os.open(folder, dir_flags)
    except FileNotFoundError:
        return
    except OSError as exc:
        if exc.errno in (errno.ELOOP, errno.ENOTDIR):
            raise UnsafeStoragePathError("Image folder must not be a symlink.") from exc
        raise

    try:
        _safe_rmtree_fd(dir_fd)
    finally:
        os.close(dir_fd)

    parent_fd = os.open(folder.parent, dir_flags)
    try:
        os.rmdir(folder.name, dir_fd=parent_fd)
    except FileNotFoundError:
        pass
    finally:
        os.close(parent_fd)


async def clear_document_images(db: AsyncSession, document: Document) -> None:
    """Remove a document's previous images (rows and files), e.g. before a re-index."""
    await db.execute(delete(DocumentImage).where(DocumentImage.document_id == document.id))
    remove_image_files(document.user_id, document.id)


async def _audit_skip(document: Document, image: ImageOCR) -> None:
    where = f"page {image.page}" if image.page else "document body"
    await audit_log.record_security_event(
        event_type=audit_log.OCR_IMAGE_SKIPPED,
        user_id=document.user_id,
        filename=document.filename,
        reason=(f"OCR skipped image {image.index + 1} ({where}) of document {document.id}, "
                f"image {image.image_id}: {image.error}"),
        ip_address=None,
    )


async def process_document_images(db: AsyncSession, document: Document, layout: dict) -> dict[int, ImageOCR]:
    """Save every layout image, OCR them and return results keyed by image index."""
    folder = image_folder(document.user_id, document.id)
    folder.mkdir(parents=True, exist_ok=True)
    folder = image_folder(document.user_id, document.id)
    resolved_folder = folder.resolve()
    results: dict[int, ImageOCR] = {}
    rows: dict[int, DocumentImage] = {}
    payloads: dict[int, bytes] = {}

    for item in layout.get("images", []):
        index = int(item["index"])
        image_id = uuid.uuid4()
        try:
            data = base64.b64decode(item["data"], validate=True)
        except (binascii.Error, KeyError, ValueError):
            data = b""
        ext = str(item.get("ext") or "png").lower()
        if not ext.isalnum() or len(ext) > 5:
            ext = "img"
        leaf = f"{index}.{ext}"
        _check_no_symlinks(folder, (leaf,))
        path = folder / leaf
        if path.resolve().parent != resolved_folder:
            raise UnsafeStoragePathError("Image path escapes document image folder.")
        if data:
            flags = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_NOFOLLOW", 0)
            fd = os.open(path, flags, 0o600)
            try:
                handle = os.fdopen(fd, "wb")
            except BaseException:
                os.close(fd)
                raise
            with handle:
                handle.write(data)
        rows[index] = DocumentImage(
            id=image_id, document_id=document.id, user_id=document.user_id, image_index=index,
            page=item.get("page"), bbox=item.get("bbox"), width=item.get("width"), height=item.get("height"),
            size_bytes=len(data), file_path=str(path), ocr_status="pending",
        )
        results[index] = ImageOCR(index, image_id, item.get("page"), item.get("bbox"), "pending")
        if data:
            payloads[index] = data
        else:
            results[index].status, results[index].error = "skipped", "Image data is missing or corrupt"
    db.add_all(rows.values())

    try:
        client = get_ocr_client()
    except Exception as exc:  # e.g. model files missing: skip images, never fail the document
        logger.error("OCR client unavailable: %s", exc)
        client = None
    deadline = time.monotonic() + settings.OCR_DOCUMENT_BUDGET_SECONDS
    pending = list(payloads)
    batch = max(1, settings.OCR_BATCH_SIZE)
    for start in range(0, len(pending), batch):
        indexes = pending[start:start + batch]
        remaining = deadline - time.monotonic()
        if client is None:
            for index in indexes:
                results[index].status, results[index].error = "skipped", "OCR is not configured correctly"
            continue
        if remaining <= 0:
            for index in indexes:
                results[index].status, results[index].error = "skipped", "OCR time budget for the document was used up"
            continue
        try:
            outcomes = await client.recognize([payloads[i] for i in indexes], timeout=remaining)
        except Exception as exc:  # a broken client must never fail the document
            logger.exception("OCR batch failed")
            outcomes = [OCRFailure(f"OCR failed: {type(exc).__name__}")] * len(indexes)
        for index, outcome in zip(indexes, outcomes):
            target = results[index]
            if isinstance(outcome, OCRFailure):
                target.status, target.error = "skipped", outcome.reason
            else:
                target.status, target.text = "done", outcome.text.strip()
                target.confidence, target.engine = round(outcome.confidence, 4), outcome.engine

    for index, result in results.items():
        row = rows[index]
        row.ocr_status = result.status
        row.ocr_text = result.text or None
        row.ocr_confidence = result.confidence
        row.ocr_engine = result.engine
        row.ocr_error = (result.error or "")[:500] or None
        if result.status == "skipped":
            logger.warning("OCR skipped image %s of document %s: %s", result.image_id, document.id, result.error)
            await _audit_skip(document, result)
    return results
