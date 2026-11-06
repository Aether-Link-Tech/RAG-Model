from __future__ import annotations

import asyncio
import logging
import os
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
    extract_text,
)
from backend.app.services.embedding import EmbeddingError, embed_texts
from backend.app.services.vector_store import Visibility, upsert_chunks

logger = logging.getLogger(__name__)


class IngestError(RuntimeError):
    pass


async def index_document(db: AsyncSession, document: Document) -> Document:
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
        chunks = split_text(text)
        if not chunks:
            raise IngestError("No extractable text found in document")

        embeddings = await embed_texts(chunks)
        visibility: Visibility = "public" if document.visibility == "public" else "private"
        ids = [f"{document.id}:{index}" for index in range(len(chunks))]
        metadatas = [
            {
                "document_id": str(document.id),
                "filename": document.filename,
                "chunk_index": index,
            }
            for index in range(len(chunks))
        ]
        upsert_chunks(
            user_id=document.user_id,
            ids=ids,
            embeddings=embeddings,
            documents=chunks,
            metadatas=metadatas,
            visibility=visibility,
        )
        document.status = "indexed"
        document.error_message = None
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


def build_storage_path(user_id: uuid.UUID, document_id: uuid.UUID, filename: str) -> str:
    extension = Path(filename).suffix.lower()
    directory = Path(settings.UPLOAD_DIR) / str(user_id)
    os.makedirs(directory, exist_ok=True)
    return str(directory / f"{document_id}{extension}")


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
            await index_document(db, document)
        finally:
            clear_rls_context(db)
