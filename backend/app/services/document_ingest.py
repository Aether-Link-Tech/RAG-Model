from __future__ import annotations

import os
import uuid
from pathlib import Path

from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.db.session import AsyncSessionLocal
from backend.app.models.document import Document
from backend.app.services.chunking import split_text
from backend.app.services.document_parser import (
    UnsupportedDocumentError,
    extract_text,
)
from backend.app.services.embedding import EmbeddingError, embed_texts
from backend.app.services.vector_store import Visibility, upsert_chunks


class IngestError(RuntimeError):
    pass


async def index_document(db: AsyncSession, document: Document) -> Document:
    document.status = "processing"
    document.error_message = None
    await db.commit()
    await db.refresh(document)

    try:
        text = extract_text(document.file_path, document.mime_type, document.filename)
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
    except (UnsupportedDocumentError, EmbeddingError, IngestError, OSError, ValueError) as exc:
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


async def reindex_document_in_background(document_id: uuid.UUID) -> None:
    """Reload and re-index a document in a session owned by the background task."""
    async with AsyncSessionLocal() as db:
        document = await db.get(Document, document_id)
        if document is not None:
            await index_document(db, document)
