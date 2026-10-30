"""Document upload, listing, retrieval, and deletion endpoints.

Protected by Bearer JWT authentication (`CurrentUser`) and PostgreSQL Row-Level
Security (AL-127 / AL-129) via transaction-local `app.user_id` and `app.user_role`.
"""
import codecs
import os
import uuid
from pathlib import Path

from fastapi import APIRouter, File, Form, HTTPException, UploadFile, status
from sqlalchemy import or_, select
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
    # The validated extension is authoritative; a declared Content-Type never overrides it.
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
    header = await file.read(1024)
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
    db: DbSession,
    current_user: CurrentUser,
    file: UploadFile = File(...),
    visibility: Visibility = Form("private"),
) -> Document:
    """Upload a document with streaming SHA-256 hashing and scoped duplicate detection.

    Consistency & Crash-Recovery Note:
    Local filesystem storage and PostgreSQL do not share a two-phase commit
    protocol. Uploads stream to a `.tmp` staging file, atomically rename via
    `os.replace()`, and perform best-effort unlink + DB rollback on pre-commit
    errors, unique-constraint races, or async cancellation. Once `db.commit()`
    succeeds, the persisted file is never unlinked by post-commit refresh/index
    failures. Hard process crashes (`SIGKILL` / power loss) between `os.replace`
    and `db.commit()`, or OS unlink failures, may leave unreferenced files on
    disk requiring periodic reconciliation.
    """
    if visibility not in ("private", "public"):
        raise HTTPException(status_code=400, detail="visibility must be private or public")

    size_header = file.size or 0
    if size_header and size_header > settings.MAX_UPLOAD_BYTES:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_max_size_message(),
        )

    suffix, mime_type = _resolve_mime(file.filename, file.content_type)
    await _validate_file_magic_bytes(file, suffix)

    try:
        file_hash, file_size = await compute_sha256_and_size_stream(
            file,
            chunk_size=STREAM_CHUNK_SIZE,
            max_bytes=settings.MAX_UPLOAD_BYTES,
        )
    except FileSizeLimitExceededError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_max_size_message(),
        ) from exc

    if file_size == 0:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Uploaded file is empty (0 bytes).",
        )

    # Scoped duplicate pre-check:
    # - private: scoped strictly to current_user.id (never probes other users' private docs)
    # - public: scoped globally across public documents only (never probes private docs)
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
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail=_duplicate_conflict_detail(visibility),
            ) from exc
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Database integrity constraint violated during document registration.",
        ) from exc
    except OSError as exc:
        _best_effort_remove(tmp_path)
        if not committed:
            _best_effort_remove(file_path)
        await _safe_rollback(db)
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

    # Post-commit operations: any failure here MUST NOT unlink `file_path`
    # because the PostgreSQL record referencing `file_path` is already committed.
    await db.refresh(document)
    return await index_document(db, document)


@router.get("", response_model=list[DocumentResponse])
async def list_documents(db: DbSession, current_user: CurrentUser) -> list[Document]:
    result = await db.execute(
        select(Document)
        .where(
            or_(
                Document.user_id == current_user.id,
                Document.visibility == "public",
            )
        )
        .order_by(Document.created_at.desc())
    )
    return list(result.scalars().all())


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: uuid.UUID,
    db: DbSession,
    current_user: CurrentUser,
) -> Document:
    return await _accessible_document(db, current_user.id, document_id)


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    document_id: uuid.UUID,
    db: DbSession,
    current_user: CurrentUser,
) -> None:
    document = await _accessible_document(db, current_user.id, document_id)
    if document.visibility == "private" and document.user_id != current_user.id:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    if (
        document.visibility == "public"
        and document.user_id != current_user.id
        and current_user.role != "admin"
    ):
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the original uploader or an admin can delete a public document.",
        )

    visibility = "public" if document.visibility == "public" else "private"
    delete_document_chunks(
        user_id=document.user_id,
        document_id=document.id,
        visibility=visibility,
    )
    if os.path.exists(document.file_path):
        os.remove(document.file_path)
    await db.delete(document)
    await db.commit()


async def _accessible_document(
    db: DbSession,
    user_id: uuid.UUID,
    document_id: uuid.UUID,
) -> Document:
    result = await db.execute(
        select(Document).where(
            Document.id == document_id,
            or_(
                Document.user_id == user_id,
                Document.visibility == "public",
            ),
        )
    )
    document = result.scalar_one_or_none()
    if document is None:
        raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
    return document
