"""Document upload, listing, retrieval, and deletion endpoints.

Protected by Bearer JWT authentication (`CurrentUser`) and PostgreSQL Row-Level
Security (AL-127 / AL-129) via transaction-local `app.user_id` and `app.user_role`.
"""
import asyncio
import codecs
import ctypes.util
import logging
import os
import uuid
import zipfile
from pathlib import Path
from urllib.parse import quote

from fastapi import APIRouter, BackgroundTasks, File, Form, HTTPException, Request, UploadFile, status
from fastapi.responses import StreamingResponse
from sqlalchemy import or_, select
from sqlalchemy.exc import IntegrityError
from starlette.background import BackgroundTask

from backend.app.api.deps import CurrentUser, DbSession
from backend.app.api.v1.collections import get_owned_collection
from backend.app.core.config import settings
from backend.app.core.roles import UserRole
from backend.app.models.document import Document
from backend.app.schemas.document import DocumentResponse, PublishResponse, Visibility
from backend.app.services import audit_log
from backend.app.services.near_duplicate import NearDuplicateError
from backend.app.services.document_images import remove_image_files
from backend.app.services.clamav import (
    ClamAVUnavailableError,
    InfectedFileError,
    scan_and_quarantine,
)
from backend.app.services.document_ingest import (
    index_document,
    reindex_document_in_background,
)
from backend.app.services.document_publish import PublishError, publish_private_document
from backend.app.services.storage_paths import (
    UnsafeStoragePathError,
    build_staging_path,
    build_storage_path,
    create_exclusive_staging_file,
    open_user_file_no_follow,
    resolve_user_file_path,
)
from backend.app.services.hash_service import (
    FileSizeLimitExceededError,
    compute_sha256_and_size_stream,
)
from backend.app.services.vector_store import delete_document_chunks, rename_document_chunks

logger = logging.getLogger(__name__)

async def log_security_event(
    user_id: str,
    filename: str | None,
    reason: str,
    ip: str,
    event_type: str = audit_log.UPLOAD_REJECTED,
) -> None:
    """Persist a security event to `security_audit_log` (FR-069). Never raises."""
    await audit_log.record_security_event(
        event_type=event_type,
        user_id=user_id,
        filename=filename,
        reason=reason,
        ip_address=None if ip == "unknown" else ip,
    )

router = APIRouter(prefix="/documents", tags=["Documents"])


def _load_python_magic():
    """Return the python-magic module only when the libmagic library is installed.

    On hosts without libmagic (e.g. Windows dev machines) `import magic` can hang
    while searching for the DLL, so it is never imported there; validation then
    uses the explicit signature checks below.
    """
    if ctypes.util.find_library("magic") is None:
        return None
    try:
        import magic  # type: ignore[import-not-found]
    except Exception:  # pragma: no cover - environment dependent
        return None
    return magic


_MAGIC = _load_python_magic()

ALLOWED_MIME_TYPES = {
    ".pdf": "application/pdf",
    ".docx": "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
    ".txt": "text/plain",
}

STREAM_CHUNK_SIZE = 65536
DANGEROUS_INTERMEDIATE_EXTENSIONS = {
    ".exe",
    ".js",
    ".xml",
    ".bat",
    ".cmd",
    ".vbs",
    ".ps1",
    ".scr",
    ".jar",
}
# Archive formats hidden before the final extension (e.g. archive.tar.gz.pdf)
# are flagged for manual review. Ordinary dotted names (Annual.Report.2024.pdf)
# are not affected.
ARCHIVE_INTERMEDIATE_EXTENSIONS = {".tar", ".gz", ".tgz", ".zip", ".7z", ".rar", ".bz2", ".xz"}


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


def _validate_extension_chain(filename: str | None) -> None:
    """Reject executable/script suffixes hidden before an allowed extension."""
    safe_name = Path(filename or "").name
    suffixes = [suffix.lower() for suffix in Path(safe_name).suffixes]
    if any(suffix in DANGEROUS_INTERMEDIATE_EXTENSIONS for suffix in suffixes[:-1]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "Suspicious double extension. Remove the dangerous extension "
                "before the file type (for example, rename report.xml.pdf)."
            ),
        )
    if any(suffix in ARCHIVE_INTERMEDIATE_EXTENSIONS for suffix in suffixes[:-1]):
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                "This filename hides an archive extension before the file type "
                "and requires manual review before upload."
            ),
        )


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

    expected_mime = ALLOWED_MIME_TYPES[suffix]
    try:
        if _MAGIC is None:
            raise RuntimeError("libmagic not available")
        detected_mime = _MAGIC.from_buffer(header, mime=True)
    except Exception:
        # Keep validation available in developer environments without libmagic;
        # production installs use python-magic for fuller signature detection.
        if header.startswith(b"%PDF-"):
            detected_mime = "application/pdf"
        elif header.startswith(b"PK\x03\x04"):
            detected_mime = "application/zip"
        elif b"\x00" in header:
            detected_mime = "application/octet-stream"
        else:
            try:
                header.decode("utf-8")
                detected_mime = "text/plain"
            except UnicodeDecodeError:
                detected_mime = "application/octet-stream"

    if suffix == ".docx":
        try:
            await file.seek(0)
            with zipfile.ZipFile(file.file) as archive:
                entries = set(archive.namelist())
            is_docx = "[Content_Types].xml" in entries and "word/document.xml" in entries
        except (OSError, zipfile.BadZipFile):
            is_docx = False
        finally:
            await file.seek(0)
        if detected_mime in {"application/zip", expected_mime} and is_docx:
            detected_mime = expected_mime

    if suffix == ".txt":
        if b"\x00" in header:
            detected_mime = "application/octet-stream"
        try:
            codecs.getincrementaldecoder("utf-8")().decode(header, final=False)
        except UnicodeDecodeError:
            detected_mime = "application/octet-stream"
        if detected_mime.startswith("text/"):
            detected_mime = expected_mime

    if detected_mime != expected_mime:
        logger.warning(
            "Rejected uploaded file with MIME mismatch: filename=%r extension=%s detected=%s expected=%s",
            file.filename,
            suffix,
            detected_mime,
            expected_mime,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=(
                f"File content does not match the .{suffix.lstrip('.')} extension. "
                "Choose the correct file type and upload again."
            ),
        )


def _best_effort_remove(path: str | Path | None) -> None:
    if path and os.path.exists(path):
        try:
            os.remove(path)
        except OSError:
            pass


def _best_effort_remove_user_file(user_id: uuid.UUID, path: str | Path | None) -> None:
    """Remove ``path`` only if it resolves safely inside ``user_id``'s storage directory."""
    if not path:
        return
    try:
        resolved = resolve_user_file_path(user_id, path, must_exist=False)
    except UnsafeStoragePathError:
        logger.warning("Refused to remove unsafe storage path for user %s", user_id)
        return
    _best_effort_remove(resolved)


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
    collection_id: str | None = Form(default=None),
) -> DocumentResponse:
    """Upload a document with streaming SHA-256 hashing and scoped duplicate detection."""
    client_ip = request.client.host if request.client else "unknown"
    user_id_str = str(current_user.id)
    filename_str = file.filename or "unknown"
    _, target_collection_id = await _resolve_collection_field(db, current_user.id, collection_id)

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
        _validate_extension_chain(file.filename)
    except HTTPException as exc:
        await log_security_event(user_id_str, filename_str, f"Extension chain validation failed: {exc.detail}", client_ip)
        raise

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
    try:
        file_path = build_storage_path(current_user.id, document_id, safe_filename)
        tmp_path, buffer = create_exclusive_staging_file(
            current_user.id, document_id, safe_filename, purpose="upload"
        )
    except UnsafeStoragePathError as exc:
        await log_security_event(user_id_str, filename_str, f"Upload rejected: {exc}", client_ip)
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Invalid filename.",
        ) from exc
    committed = False

    try:
        written_bytes = 0
        with buffer:
            await file.seek(0)
            while chunk := await file.read(STREAM_CHUNK_SIZE):
                written_bytes += len(chunk)
                if written_bytes > settings.MAX_UPLOAD_BYTES:
                    raise FileSizeLimitExceededError(settings.MAX_UPLOAD_BYTES)
                buffer.write(chunk)
            buffer.flush()

        try:
            await asyncio.to_thread(
                scan_and_quarantine,
                str(tmp_path),
                current_user.id,
                document_id,
            )
        except InfectedFileError as exc:
            client_ip = request.client.host if request.client else "unknown"
            await log_security_event(
                user_id_str,
                filename_str,
                f"Malware detected: {exc.signature}",
                client_ip,
            )
            logger.warning(
                "Quarantined infected upload: user_id=%s ip=%s filename=%r signature=%s",
                current_user.id,
                client_ip,
                safe_filename,
                exc.signature,
            )
            raise

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
            collection_id=target_collection_id,
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
    except InfectedFileError as exc:
        _best_effort_remove(tmp_path)
        await _safe_rollback(db)
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Malware was detected. The file was quarantined and the upload was blocked.",
        ) from exc
    except ClamAVUnavailableError as exc:
        _best_effort_remove(tmp_path)
        await _safe_rollback(db)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Malware scanning is temporarily unavailable. Please retry the upload.",
        ) from exc
    except BaseException:
        if not committed:
            _best_effort_remove(tmp_path)
            _best_effort_remove(file_path)
            await _safe_rollback(db)
        raise

    await db.refresh(document)
    try:
        indexed = await index_document(db, document)
    except NearDuplicateError as exc:
        # FR-081: the upload is rejected and removed cleanly (row, file; no vectors were written).
        await _discard_rejected_upload(db, document)
        await log_security_event(
            user_id_str, filename_str, "Near-duplicate of an existing document", client_ip,
        )
        raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
    return _to_response(indexed, current_user.id)


async def _discard_rejected_upload(db: DbSession, document: Document) -> None:
    file_path, owner_id, document_id = document.file_path, document.user_id, document.id
    remove_image_files(owner_id, document_id)
    try:
        await db.delete(document)
        await db.commit()
    except Exception:
        await _safe_rollback(db)
        logger.exception("Could not remove rejected near-duplicate document %s", document.id)
    _best_effort_remove_user_file(owner_id, file_path)


def _to_response(document: Document, caller_id: uuid.UUID) -> DocumentResponse:
    """Serialize a document for `caller_id`, hiding the uploader of documents they don't own."""
    owned = document.user_id == caller_id
    response = DocumentResponse.model_validate(document)
    return response.model_copy(
        update={
            "is_owner": owned,
            "user_id": document.user_id if owned else None,
            # Collections are private to their owner.
            "collection_id": document.collection_id if owned else None,
        }
    )


_UNCATEGORIZED_VALUES = {"", "none", "null", "uncategorized"}


async def _resolve_collection_field(
    db: DbSession, owner_id: uuid.UUID, raw: str | None
) -> tuple[bool, uuid.UUID | None]:
    """Parse a `collection_id` form/query value (FR-012).

    Returns (provided, collection_id). An empty value or "uncategorized" means
    "no collection". A collection that is not the caller's is 404, like any
    other resource the caller cannot see.
    """
    if raw is None:
        return False, None
    value = raw.strip()
    if value.lower() in _UNCATEGORIZED_VALUES:
        return True, None
    try:
        collection_id = uuid.UUID(value)
    except ValueError as exc:
        raise HTTPException(
            status_code=422,
            detail="collection_id must be a UUID or 'uncategorized'.",
        ) from exc
    collection = await get_owned_collection(db, owner_id, collection_id)
    return True, collection.id


@router.get("", response_model=list[DocumentResponse])
async def list_documents(
    db: DbSession,
    current_user: CurrentUser,
    collection_id: str | None = None,
) -> list[DocumentResponse]:
    """List own and public documents. With `collection_id` (a UUID or
    "uncategorized"), list only the caller's own documents in that collection."""
    query = select(Document).where(
        or_(
            Document.user_id == current_user.id,
            Document.visibility == "public",
        )
    )
    if collection_id is not None:
        _, target = await _resolve_collection_field(db, current_user.id, collection_id)
        query = query.where(
            Document.user_id == current_user.id,
            Document.collection_id.is_(None) if target is None else Document.collection_id == target,
        )
    result = await db.execute(query.order_by(Document.created_at.desc()))
    return [_to_response(doc, current_user.id) for doc in result.scalars().all()]


@router.get("/{document_id}", response_model=DocumentResponse)
async def get_document(
    document_id: uuid.UUID,
    db: DbSession,
    current_user: CurrentUser,
) -> DocumentResponse:
    return _to_response(
        await _accessible_document(db, current_user.id, document_id), current_user.id
    )


def _content_disposition(filename: str) -> str:
    """Build an attachment Content-Disposition header matching Starlette FileResponse."""
    clean = filename.replace("\r", "").replace("\n", "")
    try:
        clean.encode("latin-1")
    except UnicodeEncodeError:
        return f"attachment; filename*=utf-8''{quote(clean)}"
    escaped = clean.replace("\\", "\\\\").replace('"', '\\"')
    return f'attachment; filename="{escaped}"'


async def _stream_user_file(file_obj):
    """Stream an already-opened file descriptor and close it on completion or disconnect."""
    try:
        while chunk := await asyncio.to_thread(file_obj.read, STREAM_CHUNK_SIZE):
            yield chunk
    finally:
        file_obj.close()


@router.get("/{document_id}/download")
async def download_document(
    request: Request,
    document_id: uuid.UUID,
    db: DbSession,
    current_user: CurrentUser,
) -> StreamingResponse:
    """Download an accessible document file (AL-182).

    Own documents and public documents only; another user's private document
    is 404 (same rule as GET /documents/{id}). The stored path is never exposed.
    """
    document = await _accessible_document(db, current_user.id, document_id)

    try:
        file_obj = open_user_file_no_follow(document.user_id, document.file_path)
    except (UnsafeStoragePathError, FileNotFoundError) as exc:
        # Missing file, symlink, or stored path outside the owner's namespace: never serve it.
        reason = (
            "Download blocked: stored path outside the owner's namespace"
            if isinstance(exc, UnsafeStoragePathError)
            else "Download blocked: file missing from storage"
        )
        await log_security_event(
            str(current_user.id), document.filename, reason,
            request.client.host if request.client else "unknown", audit_log.DOWNLOAD_BLOCKED,
        )
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="File not found on storage server.",
        ) from exc

    try:
        file_size = os.fstat(file_obj.fileno()).st_size
        headers = {
            "Content-Disposition": _content_disposition(document.filename),
            "Content-Length": str(file_size),
        }
    except BaseException:
        file_obj.close()
        raise

    logger.info("Document %s downloaded by user %s", document.id, current_user.id)

    return StreamingResponse(
        _stream_user_file(file_obj),
        media_type=document.mime_type,
        headers=headers,
        background=BackgroundTask(file_obj.close),
    )


@router.post(
    "/{document_id}/publish",
    response_model=PublishResponse,
    status_code=status.HTTP_201_CREATED,
)
async def publish_document_endpoint(
    request: Request,
    document_id: uuid.UUID,
    db: DbSession,
    current_user: CurrentUser,
) -> PublishResponse:
    """Publish one of the caller's own indexed private documents (AL-185).

    Owner only, no request body. Anyone else (admins included) and unknown ids get
    404; already published, duplicate public content and not-indexed documents get
    409. Returns the NEW public document.
    """
    caller_id = current_user.id
    try:
        result = await publish_private_document(
            db,
            caller_id=caller_id,
            document_id=document_id,
            client_ip=request.client.host if request.client else "unknown",
        )
    except PublishError as exc:
        raise HTTPException(status_code=exc.status_code, detail=exc.detail) from exc
    base = _to_response(result.document, caller_id)
    return PublishResponse(**base.model_dump(), warning=result.warning)


@router.put(
    "/{document_id}",
    response_model=DocumentResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def update_document(
    request: Request,
    document_id: uuid.UUID,
    db: DbSession,
    current_user: CurrentUser,
    background_tasks: BackgroundTasks,
    file: UploadFile | None = File(default=None),
    filename: str | None = Form(default=None),
    collection_id: str | None = Form(default=None),
) -> DocumentResponse:
    """Rename a document, move it to another collection, or replace its contents
    and queue a fresh index. `collection_id` may be a UUID of one of the caller's
    collections, or empty / "uncategorized" to take it out of its collection.

    Only the owner may update. Another user's private document -> 404 (never
    revealed); another user's public document -> 403.
    """
    document = await _accessible_document(db, current_user.id, document_id)
    client_ip = request.client.host if request.client else "unknown"
    if document.user_id != current_user.id:
        if document.visibility == "private":
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail="Document not found")
        await log_security_event(
            str(current_user.id), document.filename, "Update denied: not the owner", client_ip,
            audit_log.ACCESS_DENIED,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the owner can update this document.",
        )
    # Captured up front: after a rollback ORM attributes expire and must not be
    # lazy-loaded inside the async error handlers.
    actor_id = current_user.id
    actor_role = current_user.role
    doc_id = document.id
    if file is None and filename is None and collection_id is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Provide a new filename, a replacement file, or a collection_id.",
        )
    move_collection, target_collection_id = await _resolve_collection_field(
        db, actor_id, collection_id
    )

    if filename is not None:
        filename = Path(filename).name.strip()
        if not filename or len(filename) > 255:
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Filename must contain 1 to 255 characters.",
            )
        if file is None and Path(filename).suffix.lower() != Path(document.filename).suffix.lower():
            raise HTTPException(
                status_code=status.HTTP_400_BAD_REQUEST,
                detail="Renaming cannot change the file type. Upload a replacement file to change formats.",
            )

    if file is None:
        if move_collection:
            document.collection_id = target_collection_id
        if filename is None:
            await db.commit()
            await db.refresh(document)
            return _to_response(document, current_user.id)
        document.filename = filename
        await db.commit()
        try:
            rename_document_chunks(
                user_id=document.user_id,
                document_id=document.id,
                filename=document.filename,
                visibility="public" if document.visibility == "public" else "private",
            )
        except Exception:
            # The database name is authoritative; a later re-index refreshes vector metadata.
            background_tasks.add_task(
                reindex_document_in_background, document.id, current_user.id, current_user.role
            )
        await db.refresh(document)
        return _to_response(document, current_user.id)

    replacement_name = filename or Path(file.filename or document.filename).name
    replacement_name = Path(replacement_name).name
    # A replacement goes through the same validation chain as a new upload.
    try:
        _validate_extension_chain(replacement_name)
        suffix, mime_type = _resolve_mime(replacement_name, file.content_type)
        await _validate_file_magic_bytes(file, suffix)
    except HTTPException as exc:
        await log_security_event(
            str(current_user.id), replacement_name, f"Replacement rejected: {exc.detail}", client_ip,
            audit_log.REPLACE_REJECTED,
        )
        raise
    try:
        file_hash, file_size = await compute_sha256_and_size_stream(
            file,
            chunk_size=STREAM_CHUNK_SIZE,
            max_bytes=settings.MAX_UPLOAD_BYTES,
        )
    except FileSizeLimitExceededError as exc:
        await log_security_event(
            str(actor_id), replacement_name, "Replacement exceeds the size limit", client_ip,
            audit_log.REPLACE_REJECTED,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=_max_size_message(),
        ) from exc
    if file_size == 0:
        await log_security_event(
            str(actor_id), replacement_name, "Replacement is empty (0 bytes)", client_ip,
            audit_log.REPLACE_REJECTED,
        )
        raise HTTPException(status_code=400, detail="Uploaded file is empty (0 bytes).")

    duplicate_query = select(Document.id).where(
        Document.id != document.id,
        Document.visibility == document.visibility,
        Document.file_hash == file_hash,
    )
    if document.visibility == "private":
        duplicate_query = duplicate_query.where(Document.user_id == current_user.id)
    existing = await db.execute(duplicate_query)
    if existing.scalar_one_or_none() is not None:
        await log_security_event(
            str(actor_id), replacement_name, f"Duplicate {document.visibility} document conflict",
            client_ip, audit_log.REPLACE_REJECTED,
        )
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail=_duplicate_conflict_detail(
                "public" if document.visibility == "public" else "private"
            ),
        )

    visibility: Visibility = "public" if document.visibility == "public" else "private"
    old_path = document.file_path
    try:
        resolved_old_path = resolve_user_file_path(document.user_id, old_path, must_exist=False)
        replacement_path = Path(
            build_storage_path(
                document.user_id,
                document.id,
                replacement_name,
                variant=uuid.uuid4().hex,
            )
        )
        temporary_path, buffer = create_exclusive_staging_file(
            document.user_id, document.id, replacement_name, purpose="replace"
        )
    except UnsafeStoragePathError as exc:
        await log_security_event(
            str(actor_id), replacement_name, f"Replacement rejected: {exc}", client_ip,
            audit_log.REPLACE_REJECTED,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Document storage path is unsafe; replacement rejected.",
        ) from exc

    try:
        written_bytes = 0
        with buffer:
            await file.seek(0)
            while chunk := await file.read(STREAM_CHUNK_SIZE):
                written_bytes += len(chunk)
                if written_bytes > settings.MAX_UPLOAD_BYTES:
                    raise FileSizeLimitExceededError(settings.MAX_UPLOAD_BYTES)
                buffer.write(chunk)
            buffer.flush()

        await asyncio.to_thread(scan_and_quarantine, str(temporary_path), actor_id, doc_id)
        os.replace(temporary_path, replacement_path)

        document.filename = replacement_name
        document.file_path = str(replacement_path)
        document.mime_type = mime_type
        document.file_size = written_bytes
        document.file_hash = file_hash
        document.status = "processing"
        document.error_message = None
        if move_collection:
            document.collection_id = target_collection_id
        await db.commit()
    except FileSizeLimitExceededError as exc:
        _best_effort_remove(str(temporary_path))
        _best_effort_remove(str(replacement_path))
        await _safe_rollback(db)
        raise HTTPException(400, detail=_max_size_message()) from exc
    except IntegrityError as exc:
        _best_effort_remove(str(temporary_path))
        _best_effort_remove(str(replacement_path))
        await _safe_rollback(db)
        if _is_duplicate_conflict(exc):
            raise HTTPException(
                status_code=409,
                detail=_duplicate_conflict_detail(
                    visibility
                ),
            ) from exc
        raise
    except InfectedFileError as exc:
        _best_effort_remove(str(temporary_path))
        await _safe_rollback(db)
        await log_security_event(
            str(actor_id), replacement_name, f"Malware detected in replacement: {exc.signature}", client_ip,
            audit_log.REPLACE_REJECTED,
        )
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
            detail="Malware was detected. The file was quarantined and the update was blocked.",
        ) from exc
    except ClamAVUnavailableError as exc:
        _best_effort_remove(str(temporary_path))
        await _safe_rollback(db)
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Malware scanning is temporarily unavailable. Please retry the update.",
        ) from exc
    except BaseException:
        _best_effort_remove(str(temporary_path))
        _best_effort_remove(str(replacement_path))
        await _safe_rollback(db)
        raise

    try:
        delete_document_chunks(
            user_id=document.user_id,
            document_id=document.id,
            visibility=visibility,
        )
    except Exception as exc:
        document.status = "failed"
        document.error_message = "Old document index could not be cleared before re-indexing."
        await db.commit()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="The document was replaced, but its old index could not be cleared. Retry the update.",
        ) from exc

    if resolved_old_path != replacement_path.resolve():
        _best_effort_remove_user_file(document.user_id, resolved_old_path)
    background_tasks.add_task(reindex_document_in_background, doc_id, actor_id, actor_role)
    await db.refresh(document)
    return _to_response(document, actor_id)


@router.delete("/{document_id}", status_code=status.HTTP_204_NO_CONTENT)
async def delete_document(
    request: Request,
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
        and current_user.role != UserRole.ADMIN
    ):
        await log_security_event(
            str(current_user.id), document.filename, "Delete denied: not the owner or an admin",
            request.client.host if request.client else "unknown", audit_log.ACCESS_DENIED,
        )
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Only the original uploader or an admin can delete a public document.",
        )

    visibility = "public" if document.visibility == "public" else "private"
    document_id = document.id
    owner_id = document.user_id
    try:
        original_path = resolve_user_file_path(owner_id, document.file_path, must_exist=False)
        staged_path = build_staging_path(
            owner_id, document_id, purpose="delete", existing_path=original_path
        )
    except UnsafeStoragePathError as exc:
        await log_security_event(
            str(current_user.id), document.filename, f"Delete blocked: {exc}",
            request.client.host if request.client else "unknown", audit_log.ACCESS_DENIED,
        )
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="Document storage path is unsafe; deletion rejected.",
        ) from exc
    staged_file = False

    # Move the file aside first so a failed database delete can restore it.
    if original_path.exists():
        try:
            os.replace(original_path, staged_path)
            staged_file = True
        except OSError as exc:
            raise HTTPException(
                status_code=status.HTTP_500_INTERNAL_SERVER_ERROR,
                detail="Document file could not be staged for deletion.",
            ) from exc

    try:
        # ChromaDB is outside the PostgreSQL transaction. Repair its index if
        # the SQL delete fails after vector cleanup succeeds.
        delete_document_chunks(
            user_id=document.user_id,
            document_id=document_id,
            visibility=visibility,
        )
        await db.delete(document)
        await db.commit()
    except Exception as exc:
        await _safe_rollback(db)
        if staged_file and staged_path.exists():
            try:
                os.replace(staged_path, original_path)
            except OSError:
                logger.exception("Could not restore document file after delete failure")
        try:
            restored_document = await db.get(Document, document_id)
            if restored_document is not None:
                await index_document(db, restored_document)
        except Exception:
            logger.exception("Could not restore document index after delete failure")
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Document deletion could not be completed. Please retry.",
        ) from exc

    remove_image_files(owner_id, document_id)  # extracted images (FR-058); rows cascade
    if staged_file:
        try:
            staged_path.unlink()
        except OSError:
            # Keep the tombstone name so it cannot be mistaken for a live upload.
            logger.exception("Document was deleted but its staged file could not be removed")


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
