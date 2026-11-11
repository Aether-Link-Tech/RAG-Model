from __future__ import annotations

import asyncio
import logging
import os
import re
import threading
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.db.session import AsyncSessionLocal
from backend.app.models.document import Document
from backend.app.services.chunking import split_text
from backend.app.services.document_parser import (
    DocumentParserError,
    UnsupportedDocumentError,
    extract_layout,
    extract_text,
)
from backend.app.services.embedding import EmbeddingError, embed_texts
from backend.app.services import near_duplicate
from backend.app.services.near_duplicate import NearDuplicateError
from backend.app.services.document_images import clear_document_images, process_document_images, remove_image_files
from backend.app.services.ocr_merge import chunks_from_segments, merge_layout, merged_text
from backend.app.services.vector_store import Visibility, upsert_chunks

logger = logging.getLogger(__name__)


class IngestError(RuntimeError):
    pass


_OCR_SUFFIXES = (".pdf", ".docx")


def _may_contain_images(document: Document) -> bool:
    return Path(document.file_path or document.filename).suffix.lower() in _OCR_SUFFIXES


async def index_document(db: AsyncSession, document: Document) -> Document:
    # Captured up front: a rollback expires ORM attributes, which must not lazy-load later.
    owner_id, document_id = document.user_id, document.id
    document.status = "processing"
    document.error_message = None
    await db.commit()
    await db.refresh(document)

    cancel_event = threading.Event()
    try:
        text = await asyncio.to_thread(
            extract_text,
            document.file_path,
            document.mime_type,
            document.filename,
            cancel_event=cancel_event,
        )
        # FR-058 – FR-061: OCR embedded images and merge their text in reading order.
        segments = None
        if settings.OCR_ENABLED and _may_contain_images(document):
            await clear_document_images(db, document)
            layout = await asyncio.to_thread(
                extract_layout,
                document.file_path,
                document.mime_type,
                document.filename,
                cancel_event=cancel_event,
            )
            if layout.get("images"):
                ocr_results = await process_document_images(db, document, layout)
                segments = merge_layout(layout, ocr_results)
        full_text = merged_text(segments) if segments else text

        # FR-081: compare with the caller's own and public documents. Documents without any
        # text (e.g. a scan whose images could not be read) are not fingerprinted, otherwise
        # every such document would look identical.
        fingerprint = None
        if near_duplicate.shingles(full_text):
            fingerprint = near_duplicate.signature(full_text)
            match = await near_duplicate.find_near_duplicate(db, document, fingerprint)
            if match is not None:
                raise NearDuplicateError(match)

        if segments:
            chunk_items = chunks_from_segments(segments)
        else:
            chunk_items = [(piece, {"source_type": "text"}) for piece in split_text(text)]
        if not chunk_items:
            raise IngestError("No extractable text found in document")
        chunks = [piece for piece, _ in chunk_items]

        embeddings = await embed_texts(chunks)
        visibility: Visibility = "public" if document.visibility == "public" else "private"
        ids = [f"{document.id}:{index}" for index in range(len(chunks))]
        metadatas = [
            {
                **extra,
                "document_id": str(document.id),
                "filename": document.filename,
                "chunk_index": index,
                "embedding_model": settings.EMBEDDING_MODEL_VERSION,
            }
            for index, (_, extra) in enumerate(chunk_items)
        ]
        upsert_chunks(
            user_id=document.user_id,
            ids=ids,
            embeddings=embeddings,
            documents=chunks,
            metadatas=metadatas,
            visibility=visibility,
        )
        if fingerprint is not None:
            await near_duplicate.store_fingerprint(db, document, fingerprint)
        document.status = "indexed"
        document.error_message = None
    except NearDuplicateError as exc:
        # Recorded on the document, then re-raised so an upload can be rejected cleanly.
        await db.rollback()
        remove_image_files(owner_id, document_id)
        document.status = "failed"
        document.error_message = str(exc)
        await db.commit()
        await db.refresh(document)
        raise
    except asyncio.CancelledError:
        cancel_event.set()
        document.status = "failed"
        document.error_message = "Document indexing was cancelled"
        try:
            await db.commit()
        except Exception:
            pass
        raise
    except (
        DocumentParserError,
        UnsupportedDocumentError,
        EmbeddingError,
        IngestError,
        OSError,
        ValueError,
    ) as exc:
        document.status = "failed"
        document.error_message = str(exc)

    await db.commit()
    await db.refresh(document)
    return document


_SAFE_EXTENSION_RE = re.compile(r"^\.[a-z0-9]{1,10}$")


class UnsafeStoragePathError(ValueError):
    """A storage path would leave the owner's namespace."""


def user_storage_dir(user_id: uuid.UUID) -> Path:
    """`<UPLOAD_DIR>/users/<user_id>/documents` (AL-139)."""
    return Path(settings.UPLOAD_DIR) / "users" / str(uuid.UUID(str(user_id))) / "documents"


def build_storage_path(user_id: uuid.UUID, document_id: uuid.UUID, filename: str) -> str:
    """Return `<UPLOAD_DIR>/users/<user_id>/documents/<document_id><.ext>` (AL-139).

    Only the extension of the client filename is used, and only if it is a
    plain `.abc123` suffix, so names like `../../x` or `a.pdf/..` can never
    steer the write outside the owner's directory. The resolved path is
    checked against that directory as a second guard.
    """
    owner_id = uuid.UUID(str(user_id))
    doc_id = uuid.UUID(str(document_id))
    extension = Path(filename).suffix.lower()
    if extension and not _SAFE_EXTENSION_RE.match(extension):
        raise UnsafeStoragePathError("Unsupported file extension.")

    directory = user_storage_dir(owner_id)
    target = directory / f"{doc_id}{extension}"
    if not target.resolve().is_relative_to(directory.resolve()):
        raise UnsafeStoragePathError("Storage path escapes the user namespace.")
    os.makedirs(directory, exist_ok=True)
    return str(target)


async def reindex_document_in_background(
    document_id: uuid.UUID,
    user_id: uuid.UUID,
    user_role: str = "user",
) -> None:
    """Reload and re-index a document in a session owned by the background task.

    The session runs as the non-superuser rag_app role under RLS, so it must be
    bound to the acting user's identity; without it the document is invisible
    and would stay 'processing' forever.
    """
    from backend.app.db.rls import clear_rls_context, configure_rls_context

    async with AsyncSessionLocal() as db:
        try:
            await configure_rls_context(db, user_id, user_role)
            document = await db.get(Document, document_id)
            if document is None:
                logger.warning("Re-index skipped: document %s not visible to user %s", document_id, user_id)
                return
            try:
                await index_document(db, document)
            except NearDuplicateError:
                # A replacement that duplicates another document stays failed with the reason.
                logger.info("Re-index of %s rejected as near-duplicate", document_id)
        finally:
            clear_rls_context(db)
