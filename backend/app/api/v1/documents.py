"""Document upload, listing, retrieval, and deletion endpoints.

NOTE (Development-Only Auth Scaffolding):
Endpoints currently resolve identity via `CurrentUser` (`X-User-Id` header in
`backend.app.api.deps`) pending JWT authentication and RBAC in AL-22. These
unauthenticated endpoints provide no cryptographic caller verification and must
NOT be exposed in a public or production deployment before AL-22 is integrated.
"""
import codecs
import os
import uuid
from pathlib import Path
import logging

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, Request, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.app.api.deps import CurrentUser, DbSession
from backend.app.core.config import settings
from backend.app.models.document import Document
from backend.app.schemas.document import DocumentResponse, Visibility
from backend.app.services.document_ingest import build_storage_path, index_document
from backend.app.services.hash_service import (
    FileSizeLimitExceededError,
    compute_sha256_and_size_stream,
)
from backend.app.services.vector_store import delete_document_chunks

logger = logging.getLogger(__name__)

async def log_security_event(user_id: str, filename: str, reason: str, ip: str):
    """Non-blocking security audit logger. Never raises exceptions."""
    try:
        log_msg = f"[SECURITY AUDIT] User: {user_id} | File: {filename} | Reason: {reason} | IP: {ip}"
        logger.warning(log_msg)
    except Exception:
        pass

router = APIRouter(prefix="/documents", tags=["Documents"])

ALLOWED_MIME_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
}

STREAM_CHUNK_SIZE = 65536


def _max_size_message() -> str:
    max_mb = settings.MAX_UPLOAD_BYTES / (1024 * 1024)
    label = f"{int(max_mb)}MB" if max_mb.is_integer() else f"{max_mb:.1f}MB"
    return f"File size exceeds maximum limit of {label}."


def _resolve_mime(filename: str | None, declared: str | None) -> tuple[str, str]:
    if not filename or not Path(filename).name:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file must have a valid filename.",
        )
    suffix = Path(filename).suffix.lower()
    if suffix not in ALLOWED_MIME_TYPES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid file type. Only PDF, DOCX, and TXT files are allowed.",
        )
    return suffix, ALLOWED_MIME_TYPES[suffix]


UNIQUE_VIOLATION_SQLSTATE = "23505"
DUPLICATE_INDEX_NAMES = {"uq_documents_private_user_hash", "uq_documents_public_hash"}


def _is_duplicate_conflict(exc: IntegrityError) -> bool:
    """True only for SQLSTATE 23505 on the approved private/public unique indexes."""
    orig = getattr(exc, "orig", None)
    if orig is None:
        return False
    causes = [orig, getattr(orig, "__cause__", None), getattr(orig, "_asyncpg_error", None)]
    sqlstate = next(
        (s for s in (getattr(c, "sqlstate", None) or getattr(c, "pgcode", None) for c in causes) if s),
        None,
    )
    constraint = next(
        (n for n in (getattr(c, "constraint_name", None) for c in causes) if n),
        None,
    )
    return sqlstate == UNIQUE_VIOLATION_SQLSTATE and constraint in DUPLICATE_INDEX_NAMES


async def _validate_file_magic_bytes(file: UploadFile, suffix: str) -> None:
    """Validate header signature bytes to reject trivially spoofed extensions."""
    header = await file.read(2048)
    await file.seek(0)

    if not header:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty (0 bytes).",
        )

    if suffix == ".pdf" and not header.startswith(b"%PDF-"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File content signature does not match a valid PDF.",
        )
    if suffix == ".docx" and not header.startswith(b"PK\x03\x04"):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="File content signature does not match a valid DOCX document.",
        )
    if suffix == ".txt":
        if b"\x00" in header:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="File content is not valid UTF-8 plain text.",
            )
        try:
            codecs.getincrementaldecoder("utf-8")().decode(header, final=False)
        except UnicodeDecodeError as exc:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="File content is not valid UTF-8 plain text.",
            ) from exc


def _best_effort_remove(path: str | None) -> None:
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


async def _safe_rollback(db: DbSession) -> None:
    try:
        await db.rollback()
    except Exception:
        pass


def _duplicate_conflict_detail(visibility: Visibility) -> str:
    if visibility == "private":
        return "A private document with this content hash already exists in your workspace."
    return "A public document with this content hash already exists in the shared knowledge pool."


@router.post("", response_model=DocumentResponse, status_code=status.HTTP_201_CREATED)
async def upload_document(
    request: Request,
    db: DbSession,
    current_user: CurrentUser,
    file: UploadFile = File(...),
    visibility: Visibility = Form("private"),
) -> Document:
    """Upload a document with streaming SHA-256 hashing and scoped duplicate detection."""
    client_ip = request.client.host if request.client else "unknown"
    user_id_str = str(current_user.id)
    filename_str = file.filename or "unknown"

    if visibility not in ("private", "public"):
        await log_security_event(user_id_str, filename_str, "Invalid visibility parameter", client_ip)
        raise HTTPException(status_code=400, detail="visibility must be private or public")

    size_header = file.size or 0
    if size_header and size_header > settings.MAX_UPLOAD_BYTES:
        await log_security_event(user_id_str, filename_str, "File size limit exceeded (header)", client_ip)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_max_size_message(),
        )

    try:
        suffix, mime_type = _resolve_mime(file.filename, file.content_type)
    except HTTPException as exc:
        await log_security_event(user_id_str, filename_str, f"MIME/Filename validation failed: {exc.detail}", client_ip)
        raise

    try:
        await _validate_file_magic_bytes(file, suffix)
    except HTTPException as exc:
        await log_security_event(user_id_str, filename_str, f"Magic bytes validation failed: {exc.detail}", client_ip)
        raise

    try:
        file_hash, file_size = await compute_sha256_and_size_stream(
            file,
            chunk_size=STREAM_CHUNK_SIZE,
            max_bytes=settings.MAX_UPLOAD_BYTES,
        )
    except FileSizeLimitExceededError as exc:
        await log_security_event(user_id_str, filename_str, "File size limit exceeded during stream", client_ip)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_max_size_message(),
        ) from exc

    if file_size == 0:
        await log_security_event(user_id_str, filename_str, "Uploaded file is empty (0 bytes)", client_ip)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty (0 bytes).",
        )

    # Scoped duplicate pre-check
    if visibility == "private":
        duplicate_stmt = select(Document.id).where(
            Document.visibility == "private",
            Document.user_id == current_user.id,
            Document.file_hash == file_hash,
        )
    else:
        duplicate_stmt = select(Document.id).where(
            Document.visibility == "public",
            Document.file_hash == file_hash,
        )

    existing = await db.execute(duplicate_stmt)
    if existing.scalar_one_or_none() is not None:
        await log_security_event(user_id_str, filename_str, f"Duplicate {visibility} document conflict", client_ip)
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_duplicate_conflict_detail(visibility),
        )

    document_id = uuid.uuid4()
    safe_filename = Path(file.filename or f"{document_id}{suffix}").name
    file_path = build_storage_path(current_user.id, document_id, safe_filename)
    tmp_path = f"{file_path}.{uuid.uuid4().hex}.tmp"
    committed = False

    try:
        await file.seek(0)
        written_bytes = 0
        with open(tmp_path, "wb") as buffer:
            while chunk := await file.read(STREAM_CHUNK_SIZE):
                written_bytes += len(chunk)
                if written_bytes > settings.MAX_UPLOAD_BYTES:
                    raise FileSizeLimitExceededError(settings.MAX_UPLOAD_BYTES)
                buffer.write(chunk)
            buffer.flush()

        os.replace(tmp_path, file_path)

        document = Document(
            id=document_id,
            user_id=current_user.id,
            filename=safe_filename,
            file_path=file_path,
            mime_type=mime_type,
            file_size=written_bytes,
            file_hash=file_hash,
            status="pending",
            visibility=visibility,
        )
        db.add(document)
        await db.commit()
        committed = True
    except FileSizeLimitExceededError as exc:
        _best_effort_remove(tmp_path)
        if not committed:
            _best_effort_remove(file_path)
        await _safe_rollback(db)
        await log_security_event(user_id_str, filename_str, "File size limit exceeded during disk write", client_ip)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_max_size_message(),
        ) from exc
    except IntegrityError as exc:
        _best_effort_remove(tmp_path)
        if not committed:
            _best_effort_remove(file_path)
        await _safe_rollback(db)
        if _is_duplicate_conflict(exc):
            await log_security_event(user_id_str, filename_str, "Database unique constraint duplicate conflict", client_ip)
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_duplicate_conflict_detail(visibility),
            ) from exc
        await log_security_event(user_id_str, filename_str, "Database integrity constraint violation", client_ip)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Database integrity constraint violated during document registration.",
        ) from exc
    except OSError as exc:
        _best_effort_remove(tmp_path)
        if not committed:
            _best_effort_remove(file_path)
        await _safe_rollback(db)
        await log_security_event(user_id_str, filename_str, "Filesystem storage error", client_ip)
        raise HTTPException(
            status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
            detail="Failed to persist uploaded document to storage.",
        ) from exc
    except BaseException:
        if not committed:
            _best_effort_remove(tmp_path)
            _best_effort_remove(file_path)
            await _safe_rollback(db)
        raise

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