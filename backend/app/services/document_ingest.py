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
from backend.app.services.document_images import clear_document_images, process_document_images
from backend.app.services.ocr_merge import chunks_from_segments, merge_layout, merged_text
from backend.app.services.vector_store import (
    Visibility,
    cleanup_stale_document_chunks,
    upsert_chunks,
)

logger = logging.getLogger(__name__)


class IngestError(RuntimeError):
    pass


_OCR_SUFFIXES = (".pdf", ".docx")


def _may_contain_images(document: Document) -> bool:
    return Path(document.file_path or document.filename).suffix.lower() in _OCR_SUFFIXES


def document_error_message(exc: Exception) -> str:
    if isinstance(exc, NearDuplicateError):
        return "A similar document already exists."
    if isinstance(exc, EmbeddingError):
        return "Embedding service is unavailable. Please retry."
    if isinstance(exc, (DocumentParserError, UnsupportedDocumentError, OSError)):
        return "The file could not be read."
    if isinstance(exc, IngestError):
        return "No extractable text was found in the document."
    return "Processing failed. Please retry or contact support."


async def _extract_document_content(
    db: AsyncSession,
    document: Document,
    cancel_event: threading.Event,
) -> tuple[str, list | None]:
    doc_visibility: Visibility = "public" if document.visibility == "public" else "private"
    safe_file_path = str(
        resolve_document_file_path(
            document.user_id,
            document.file_path,
            visibility=doc_visibility,
            must_exist=True,
        )
    )
    text = await asyncio.to_thread(
        extract_text,
        safe_file_path,
        document.mime_type,
        document.filename,
        owner_id=document.user_id,
        visibility=doc_visibility,
        cancel_event=cancel_event,
    )
    segments = None
    if settings.OCR_ENABLED and _may_contain_images(document):
        await clear_document_images(db, document)
        layout = await asyncio.to_thread(
            extract_layout,
            safe_file_path,
            document.mime_type,
            document.filename,
            owner_id=document.user_id,
            visibility=doc_visibility,
            cancel_event=cancel_event,
        )
        if layout.get("images"):
            ocr_results = await process_document_images(db, document, layout)
            segments = merge_layout(layout, ocr_results)
    return text, segments


async def check_document_for_near_duplicate(
    db: AsyncSession, document: Document
) -> None:
    """Run the same parsing and duplicate comparison used by indexing before upload dispatch."""
    cancel_event = threading.Event()
    try:
        text, segments = await _extract_document_content(db, document, cancel_event)
        content = merged_text(segments) if segments else text
        await near_duplicate.check_text_for_near_duplicate(db, document, content)
    finally:
        if _may_contain_images(document):
            await clear_document_images(db, document)


async def index_document(db: AsyncSession, document: Document) -> Document:
    # Captured up front: a rollback expires ORM attributes, which must not lazy-load later.
    owner_id, document_id = document.user_id, document.id
    doc_visibility: Visibility = "public" if document.visibility == "public" else "private"
    # Fail closed before any DB or file operation if the stored path leaves its allowed namespace.
    safe_file_path = str(
        resolve_document_file_path(
            owner_id, document.file_path, visibility=doc_visibility, must_exist=False
        )
    )
    document.status = "processing"
    document.error_message = None
    await db.commit()
    await db.refresh(document)

    cancel_event = threading.Event()
    try:
        text, segments = await _extract_document_content(db, document, cancel_event)
        full_text = merged_text(segments) if segments else text

        # FR-081: compare with the caller's own and public documents. Documents without any
        # text (e.g. a scan whose images could not be read) are not fingerprinted, otherwise
        # every such document would look identical.
        fingerprint = await near_duplicate.check_text_for_near_duplicate(
            db, document, full_text
        )

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
                "chunk_id": ids[index],
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
        cleanup_stale_document_chunks(
            user_id=document.user_id,
            document_id=document.id,
            chunk_ids=ids,
            visibility=visibility,
        )
        if fingerprint is not None:
            await near_duplicate.store_fingerprint(db, document, fingerprint)
        document.status = "indexed"
        document.error_message = None
    except NearDuplicateError as exc:
        logger.exception("Document indexing rejected as near-duplicate")
        await db.rollback()
        document.status = "failed"
        document.error_message = document_error_message(exc)
        await db.commit()
        await db.refresh(document)
        raise
    except (EmbeddingError, TimeoutError, ConnectionError) as exc:
        logger.exception("Temporary indexing failure for document %s", document_id)
        document.status = "failed"
        document.error_message = document_error_message(exc)
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
        IngestError,
        OSError,
        ValueError,
    ) as exc:
        logger.exception("Document indexing failed for document %s", document_id)
        document.status = "failed"
        document.error_message = document_error_message(exc)

    await db.commit()
    await db.refresh(document)
    return document


from backend.app.services.storage_paths import (
    UnsafeStoragePathError,
    build_public_storage_path,
    build_staging_path,
    build_storage_path,
    public_storage_dir,
    resolve_document_file_path,
    resolve_public_file_path,
    user_storage_dir,
    validate_safe_filename,
)


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
