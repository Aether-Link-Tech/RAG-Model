import os
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.app.api.deps import CurrentUser, DbSession
from backend.app.core.config import settings
from backend.app.models.document import Document
from backend.app.schemas.document import DocumentResponse, Visibility
from backend.app.services.document_ingest import build_storage_path, index_document
from backend.app.services.hash_service import compute_sha256_stream
from backend.app.services.vector_store import delete_document_chunks

router = APIRouter(prefix="/documents", tags=["Documents"])

ALLOWED_MIME_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
}


def _resolve_mime(filename: str | None, declared: str | None) -> str:
    suffix = Path(filename or "").suffix.lower()
    if suffix not in ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file type. Only PDF, DOCX, and TXT files are allowed.",
        )
    return declared if declared in ALLOWED_MIME_TYPES.values() else ALLOWED_MIME_TYPES[suffix]


@router.post("", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
async def upload_document(
    db: DbSession,
    current_user: CurrentUser,
    file: UploadFile = File(...),
    visibility: Visibility = Form("private"),
) -> Document:
    if visibility not in ("private", "public"):
        raise HTTPException(status_code=400, detail="visibility must be private or public")

    size_header = file.size or 0
    if size_header and size_header > settings.MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File size exceeds maximum limit of 25MB.",
        )

    mime_type = _resolve_mime(file.filename, file.content_type)
    file_hash = await compute_sha256_stream(file)

    existing = await db.execute(select(Document).where(Document.file_hash == file_hash))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A document with this content hash already exists",
        )

    document_id = uuid.uuid4()
    filename = file.filename or f"{document_id}.bin"
    file_path = build_storage_path(current_user.id, document_id, filename)

    contents = await file.read()
    if len(contents) > settings.MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File size exceeds maximum limit of 25MB.",
        )
    with open(file_path, "wb") as buffer:
        buffer.write(contents)

    document = Document(
        id=document_id,
        user_id=current_user.id,
        filename=filename,
        file_path=file_path,
        mime_type=mime_type,
        file_size=len(contents),
        file_hash=file_hash,
        status="pending",
        visibility=visibility,
    )
    db.add(document)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        if os.path.exists(file_path):
            os.remove(file_path)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A document with this content hash already exists",
        ) from exc
    await db.refresh(document)
    return await index_document(db, document)


@router.get("", response_model=list[DocumentResponse])
async def list_documents(db: DbSession, current_user: CurrentUser) -> list[Document]:
    result = await db.execute(
        select(Document)
        .where(Document.user_id == current_user.id)
        .order_by(Document.created_at.desc())
    )
    return list(result.scalars().all())


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: uuid.UUID,
    db: DbSession,
    current_user: CurrentUser,
) -> Document:
    document = await _owned_document(db, current_user.id, document_id)
    return document


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID,
    db: DbSession,
    current_user: CurrentUser,
) -> None:
    document = await _owned_document(db, current_user.id, document_id)
    visibility = "public" if document.visibility == "public" else "private"
    delete_document_chunks(
        user_id=current_user.id,
        document_id=document.id,
        visibility=visibility,
    )
    if os.path.exists(document.file_path):
        os.remove(document.file_path)
    await db.delete(document)
    await db.commit()


async def _owned_document(db: DbSession, user_id: uuid.UUID, document_id: uuid.UUID) -> Document:
    result = await db.execute(
        select(Document).where(
            Document.id == document_id,
            Document.user_id == user_id,
        )
    )
    document = result.scalar_one_or_none()
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return document
