"""Isolated security tests for Phase 2 storage-path integration (AL-253)."""

from __future__ import annotations

import io
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi import BackgroundTasks, HTTPException, UploadFile
from fastapi.responses import StreamingResponse
from starlette.datastructures import Headers

from backend.app.api.v1 import documents as documents_api
from backend.app.models.user import UserRole
from backend.app.services import document_ingest, document_publish
from backend.app.services.storage_paths import (
    UnsafeStoragePathError,
    build_storage_path,
    user_storage_dir,
)


def _make_request() -> SimpleNamespace:
    return SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"))


def _make_user(user_id: uuid.UUID | None = None, role: UserRole = UserRole.USER) -> SimpleNamespace:
    return SimpleNamespace(id=user_id or uuid.uuid4(), role=role)


def _make_doc(
    *,
    owner_id: uuid.UUID,
    file_path: str,
    filename: str = "report.txt",
    visibility: str = "private",
    status: str = "indexed",
) -> SimpleNamespace:
    return SimpleNamespace(
        id=uuid.uuid4(),
        user_id=owner_id,
        filename=filename,
        file_path=file_path,
        mime_type="text/plain",
        file_size=12,
        file_hash="a" * 64,
        status=status,
        error_message=None,
        visibility=visibility,
        collection_id=None,
    )


def _make_upload(filename: str, content: bytes, content_type: str = "text/plain") -> UploadFile:
    return UploadFile(
        file=io.BytesIO(content),
        filename=filename,
        headers=Headers({"content-type": content_type}),
    )


@pytest.mark.asyncio
async def test_download_blocks_tampered_cross_user_path(isolate_storage, monkeypatch):
    user_a = _make_user()
    user_b = _make_user()
    path_b = Path(build_storage_path(user_b.id, uuid.uuid4(), "secret.txt"))
    path_b.write_text("user b secret", encoding="utf-8")

    # User A's DB row has been tampered to point to User B's file.
    doc_a = _make_doc(owner_id=user_a.id, file_path=str(path_b), filename="secret.txt")
    monkeypatch.setattr(documents_api, "_accessible_document", AsyncMock(return_value=doc_a))
    audit_mock = AsyncMock()
    monkeypatch.setattr(documents_api, "log_security_event", audit_mock)

    with pytest.raises(HTTPException) as exc_info:
        await documents_api.download_document(_make_request(), doc_a.id, AsyncMock(), user_a)

    assert exc_info.value.status_code == 404
    audit_mock.assert_awaited_once()
    assert path_b.read_text(encoding="utf-8") == "user b secret"


@pytest.mark.asyncio
async def test_public_download_validates_against_original_owner(isolate_storage, monkeypatch):
    user_a = _make_user()
    user_b = _make_user()

    # Public document owned by User B, stored inside User B's namespace.
    doc_id = uuid.uuid4()
    path_b = Path(build_storage_path(user_b.id, doc_id, "public.txt"))
    path_b.write_text("public content", encoding="utf-8")
    public_doc = _make_doc(
        owner_id=user_b.id, file_path=str(path_b), filename="public.txt", visibility="public"
    )

    monkeypatch.setattr(documents_api, "_accessible_document", AsyncMock(return_value=public_doc))
    response = await documents_api.download_document(_make_request(), public_doc.id, AsyncMock(), user_a)
    assert isinstance(response, StreamingResponse)
    chunks = [chunk async for chunk in response.body_iterator]
    assert b"".join(chunks) == b"public content"
    assert response.headers["content-length"] == str(len(b"public content"))
    assert 'attachment; filename="public.txt"' == response.headers["content-disposition"]

    # If the public document's path is tampered to point into User A's directory, it must fail.
    path_a = Path(build_storage_path(user_a.id, uuid.uuid4(), "trap.txt"))
    path_a.write_text("trap", encoding="utf-8")
    public_doc.file_path = str(path_a)
    monkeypatch.setattr(documents_api, "log_security_event", AsyncMock())

    with pytest.raises(HTTPException) as exc_info:
        await documents_api.download_document(_make_request(), public_doc.id, AsyncMock(), user_a)
    assert exc_info.value.status_code == 404


@pytest.mark.asyncio
async def test_replace_rejects_unsafe_existing_path_before_any_mutation(isolate_storage, monkeypatch):
    user_a = _make_user()
    user_b = _make_user()

    path_b = Path(build_storage_path(user_b.id, uuid.uuid4(), "victim.txt"))
    path_b.write_text("do not touch", encoding="utf-8")

    doc_a = _make_doc(owner_id=user_a.id, file_path=str(path_b), filename="victim.txt")
    db = AsyncMock()
    db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))
    monkeypatch.setattr(documents_api, "_accessible_document", AsyncMock(return_value=doc_a))
    monkeypatch.setattr(documents_api, "log_security_event", AsyncMock())

    upload = _make_upload("new.txt", b"replacement bytes")
    with pytest.raises(HTTPException) as exc_info:
        await documents_api.update_document(
            request=_make_request(),
            document_id=doc_a.id,
            background_tasks=BackgroundTasks(),
            db=db,
            current_user=user_a,
            file=upload,
            filename="new.txt",
            collection_id=None,
        )

    assert exc_info.value.status_code == 400
    assert doc_a.file_path == str(path_b)
    assert doc_a.filename == "victim.txt"
    db.commit.assert_not_called()
    assert path_b.read_text(encoding="utf-8") == "do not touch"
    assert not user_storage_dir(user_a.id, create=False).exists()


@pytest.mark.asyncio
async def test_failed_replacement_keeps_db_record_and_original_file_unchanged(isolate_storage, monkeypatch):
    user_a = _make_user()
    doc_id = uuid.uuid4()
    orig_path = Path(build_storage_path(user_a.id, doc_id, "orig.txt"))
    orig_path.write_text("original content", encoding="utf-8")

    doc_a = _make_doc(owner_id=user_a.id, file_path=str(orig_path), filename="orig.txt")
    doc_a.id = doc_id

    db = AsyncMock()
    db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: None))
    monkeypatch.setattr(documents_api, "_accessible_document", AsyncMock(return_value=doc_a))
    monkeypatch.setattr(documents_api, "log_security_event", AsyncMock())
    monkeypatch.setattr(
        documents_api,
        "scan_and_quarantine",
        MagicMock(side_effect=documents_api.InfectedFileError("EICAR")),
    )

    upload = _make_upload("infected.txt", b"X5O!P%@AP[4\\PZX54(P^)7CC)7}$EICAR")
    with pytest.raises(HTTPException) as exc_info:
        await documents_api.update_document(
            request=_make_request(),
            document_id=doc_a.id,
            background_tasks=BackgroundTasks(),
            db=db,
            current_user=user_a,
            file=upload,
            filename="infected.txt",
            collection_id=None,
        )

    assert exc_info.value.status_code == 422
    assert doc_a.file_path == str(orig_path)
    assert doc_a.filename == "orig.txt"
    db.commit.assert_not_called()
    assert orig_path.read_text(encoding="utf-8") == "original content"


@pytest.mark.asyncio
async def test_delete_rejects_tampered_cross_user_path_before_mutation(isolate_storage, monkeypatch):
    user_a = _make_user()
    user_b = _make_user()

    path_b = Path(build_storage_path(user_b.id, uuid.uuid4(), "victim.txt"))
    path_b.write_text("user b data", encoding="utf-8")

    doc_a = _make_doc(owner_id=user_a.id, file_path=str(path_b), filename="victim.txt")
    db = AsyncMock()
    monkeypatch.setattr(documents_api, "_accessible_document", AsyncMock(return_value=doc_a))
    monkeypatch.setattr(documents_api, "log_security_event", AsyncMock())
    delete_chunks_mock = MagicMock()
    monkeypatch.setattr(documents_api, "delete_document_chunks", delete_chunks_mock)

    with pytest.raises(HTTPException) as exc_info:
        await documents_api.delete_document(_make_request(), doc_a.id, db, user_a)

    assert exc_info.value.status_code == 400
    delete_chunks_mock.assert_not_called()
    db.delete.assert_not_called()
    db.commit.assert_not_called()
    assert path_b.read_text(encoding="utf-8") == "user b data"


@pytest.mark.asyncio
async def test_publish_rejects_tampered_cross_user_source_path(isolate_storage, monkeypatch):
    user_a = _make_user()
    user_b = _make_user()

    path_b = Path(build_storage_path(user_b.id, uuid.uuid4(), "secret.txt"))
    path_b.write_text("user b secret", encoding="utf-8")

    doc_a = _make_doc(
        owner_id=user_a.id, file_path=str(path_b), filename="secret.txt", visibility="private", status="indexed"
    )
    db = AsyncMock()
    db.execute = AsyncMock(return_value=SimpleNamespace(scalar_one_or_none=lambda: doc_a))
    monkeypatch.setattr(document_publish, "_audit_denied", AsyncMock())

    with pytest.raises(document_publish.PublishError) as exc_info:
        await document_publish.publish_private_document(
            db, caller_id=user_a.id, document_id=doc_a.id
        )

    assert exc_info.value.status_code == 500
    assert db.execute.await_count == 1
    assert not user_storage_dir(user_a.id, create=False).exists()


@pytest.mark.asyncio
async def test_index_document_rejects_unsafe_stored_path_before_db_or_parser(isolate_storage, monkeypatch):
    user_a = _make_user()
    user_b = _make_user()
    path_b = Path(build_storage_path(user_b.id, uuid.uuid4(), "secret.txt"))
    path_b.write_text("user b secret", encoding="utf-8")

    doc_a = _make_doc(owner_id=user_a.id, file_path=str(path_b), status="pending")
    db = AsyncMock()
    extract_mock = MagicMock()
    monkeypatch.setattr(document_ingest, "extract_text", extract_mock)

    with pytest.raises(UnsafeStoragePathError):
        await document_ingest.index_document(db, doc_a)

    assert doc_a.status == "pending"
    db.commit.assert_not_called()
    extract_mock.assert_not_called()


@pytest.mark.asyncio
async def test_discard_rejected_upload_never_deletes_foreign_file(isolate_storage):
    user_a = _make_user()
    user_b = _make_user()
    path_b = Path(build_storage_path(user_b.id, uuid.uuid4(), "victim.txt"))
    path_b.write_text("keep me", encoding="utf-8")

    doc_a = _make_doc(owner_id=user_a.id, file_path=str(path_b))
    db = AsyncMock()

    await documents_api._discard_rejected_upload(db, doc_a)
    assert path_b.read_text(encoding="utf-8") == "keep me"


def test_exclusive_staging_file_cannot_overwrite_existing_file(isolate_storage, monkeypatch):
    from backend.app.services import storage_paths

    user_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    existing = user_storage_dir(user_id, create=True) / f".upload.{doc_id}.collision.txt.tmp"
    existing.write_bytes(b"existing-sentinel")

    monkeypatch.setattr(storage_paths, "build_staging_path", lambda *a, **kw: existing)
    with pytest.raises(FileExistsError):
        storage_paths.create_exclusive_staging_file(user_id, doc_id, "doc.txt", purpose="upload")

    assert existing.read_bytes() == b"existing-sentinel"


@pytest.mark.asyncio
async def test_download_uses_open_user_file_no_follow_and_closes_fd_on_disconnect(
    isolate_storage, monkeypatch
):
    from backend.app.services import storage_paths

    user_a = _make_user()
    doc_id = uuid.uuid4()
    path_a = Path(build_storage_path(user_a.id, doc_id, "stream.txt"))
    payload = b"A" * (documents_api.STREAM_CHUNK_SIZE * 2 + 17)
    path_a.write_bytes(payload)

    doc_a = _make_doc(owner_id=user_a.id, file_path=str(path_a), filename="stream.txt")
    monkeypatch.setattr(documents_api, "_accessible_document", AsyncMock(return_value=doc_a))

    calls = []
    opened_handles = []
    real_open = storage_paths.open_user_file_no_follow

    def spy_open(owner_id, stored_path):
        calls.append((owner_id, stored_path))
        handle = real_open(owner_id, stored_path)
        opened_handles.append(handle)
        return handle

    monkeypatch.setattr(documents_api, "open_user_file_no_follow", spy_open)

    response = await documents_api.download_document(_make_request(), doc_a.id, AsyncMock(), user_a)
    assert isinstance(response, StreamingResponse)
    assert calls == [(user_a.id, str(path_a))]
    assert len(opened_handles) == 1
    assert not opened_handles[0].closed

    # Simulate client disconnect after reading only the first chunk.
    first_chunk = await anext(response.body_iterator)
    assert len(first_chunk) == documents_api.STREAM_CHUNK_SIZE
    await response.body_iterator.aclose()
    assert opened_handles[0].closed


# ---------------------------------------------------------------------------
# Phase 3: document_images, clamav, document_parser, and integrity_check
# ---------------------------------------------------------------------------


@pytest.mark.asyncio
async def test_document_images_rejects_symlink_escapes_and_stays_owner_scoped(
    isolate_storage, monkeypatch
):
    import base64

    from backend.app.services import document_images

    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    doc_a = uuid.uuid4()
    doc_b = uuid.uuid4()

    # Legitimate image folder is strictly inside User A's namespace.
    folder_a = document_images.image_folder(user_a, doc_a)
    assert folder_a.resolve().is_relative_to(user_storage_dir(user_a).resolve())

    # Symlinked images/ directory pointing to User B's storage must fail closed.
    dir_a = user_storage_dir(user_a, create=True)
    dir_b = user_storage_dir(user_b, create=True)
    secret_b = dir_b / f"{doc_b}.txt"
    secret_b.write_text("user b secret", encoding="utf-8")
    (dir_a / "images").symlink_to(dir_b)

    with pytest.raises(UnsafeStoragePathError):
        document_images.image_folder(user_a, doc_a)

    with pytest.raises(UnsafeStoragePathError):
        document_images.remove_image_files(user_a, doc_a)

    # User B's file must remain untouched.
    assert secret_b.read_text(encoding="utf-8") == "user b secret"

    # Also verify process_document_images rejects symlinked image target file.
    (dir_a / "images").unlink()
    valid_folder = document_images.image_folder(user_a, doc_a)
    valid_folder.mkdir(parents=True, exist_ok=True)
    (valid_folder / "0.png").symlink_to(secret_b)

    doc_obj = _make_doc(owner_id=user_a, file_path=str(dir_a / f"{doc_a}.pdf"))
    doc_obj.id = doc_a
    payload = base64.b64encode(b"\x89PNG\r\n\x1a\n").decode("ascii")
    monkeypatch.setattr(document_images, "get_ocr_client", MagicMock(return_value=None))

    with pytest.raises(UnsafeStoragePathError):
        await document_images.process_document_images(
            AsyncMock(), doc_obj, {"images": [{"index": 0, "ext": "png", "data": payload}]}
        )
    assert secret_b.read_text(encoding="utf-8") == "user b secret"


def test_clamav_rejects_cross_user_source_and_unsafe_quarantine_paths(
    isolate_storage, tmp_path, monkeypatch
):
    from backend.app.core.config import settings
    from backend.app.services import clamav

    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    doc_id = uuid.uuid4()

    q_root = tmp_path / "quarantine"
    q_root.mkdir()
    monkeypatch.setattr(settings, "CLAMAV_QUARANTINE_DIR", str(q_root))

    # Cross-user source path must be rejected before touching ClamAV or moving anything.
    path_b = Path(build_storage_path(user_b, doc_id, "secret.txt"))
    path_b.write_text("user b file", encoding="utf-8")
    with pytest.raises(UnsafeStoragePathError):
        clamav.scan_and_quarantine(str(path_b), user_a, doc_id)
    assert path_b.exists()

    # Non-UUID user_id / document_id traversal must be rejected.
    path_a = Path(build_storage_path(user_a, doc_id, "own.txt"))
    path_a.write_text("own file", encoding="utf-8")
    with pytest.raises(ValueError):
        clamav.scan_and_quarantine(str(path_a), "../escape", doc_id)

    # Symlinked quarantine directory for user_a must be rejected before scanning.
    (q_root / str(user_a)).symlink_to(tmp_path)
    with pytest.raises(UnsafeStoragePathError):
        clamav.scan_and_quarantine(str(path_a), user_a, doc_id)


def test_parser_rejects_paths_outside_owner_directory_and_symlinks(
    isolate_storage, monkeypatch
):
    from backend.app.core.config import settings
    from backend.app.services import document_parser

    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    doc_b = uuid.uuid4()
    path_b = Path(build_storage_path(user_b, doc_b, "secret.txt"))
    path_b.write_text("user b text", encoding="utf-8")

    monkeypatch.setattr(settings, "PARSER_SANDBOX_ENABLED", False)

    # Cross-user path rejected for extract_text, extract_layout, and extract_text_sandboxed.
    with pytest.raises(UnsafeStoragePathError):
        document_parser.extract_text(str(path_b), "text/plain", "secret.txt", owner_id=user_a)

    with pytest.raises(UnsafeStoragePathError):
        document_parser.extract_layout(str(path_b), "text/plain", "secret.txt", owner_id=user_a)

    with pytest.raises(UnsafeStoragePathError):
        document_parser.extract_text_sandboxed(
            str(path_b), "text/plain", "secret.txt", owner_id=user_a
        )

    # Symlink inside User A's folder pointing to User B's file is also rejected.
    symlink_a = user_storage_dir(user_a, create=True) / f"{uuid.uuid4()}.txt"
    symlink_a.symlink_to(path_b)
    with pytest.raises(UnsafeStoragePathError):
        document_parser.extract_text(str(symlink_a), "text/plain", "link.txt", owner_id=user_a)


def test_integrity_check_detects_tampered_and_symlink_paths(isolate_storage):
    from backend.app.services.integrity_check import DocumentRecord, check_integrity

    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    doc_cross = uuid.uuid4()
    doc_sym = uuid.uuid4()

    path_b = Path(build_storage_path(user_b, doc_cross, "b.txt"))
    path_b.write_text("b", encoding="utf-8")

    sym_a = user_storage_dir(user_a, create=True) / f"{doc_sym}.txt"
    sym_a.symlink_to(path_b)

    fake_chroma = SimpleNamespace(list_collections=lambda: [])
    records = [
        DocumentRecord(
            id=doc_cross, user_id=user_a, visibility="private", status="pending", file_path=str(path_b)
        ),
        DocumentRecord(
            id=doc_sym, user_id=user_a, visibility="private", status="pending", file_path=str(sym_a)
        ),
    ]
    report = check_integrity(records, chroma_client=fake_chroma, upload_dir=isolate_storage)
    outside = {issue.document_id for issue in report.by_kind("file_outside_owner_folder")}
    assert outside == {str(doc_cross), str(doc_sym)}


# ---------------------------------------------------------------------------
# Phase 3 Security Audit: parser owner enforcement, quarantine safety,
# descriptor-relative image deletion
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "env_value",
    ["production", "staging", "test", "testing", "unknown", "", None, "   "],
)
def test_parser_requires_owner_id_in_non_local_environments(
    isolate_storage, tmp_path, monkeypatch, env_value
):
    from backend.app.core.config import settings
    from backend.app.services import document_parser

    sample = tmp_path / "doc.txt"
    sample.write_text("hello", encoding="utf-8")
    monkeypatch.setattr(settings, "ENV", env_value)
    monkeypatch.setattr(settings, "PARSER_SANDBOX_ENABLED", False)

    with pytest.raises(UnsafeStoragePathError):
        document_parser.extract_text(str(sample), "text/plain", "doc.txt")

    with pytest.raises(UnsafeStoragePathError):
        document_parser.extract_layout(str(sample), "text/plain", "doc.txt")

    with pytest.raises(UnsafeStoragePathError):
        document_parser.extract_text_sandboxed(str(sample), "text/plain", "doc.txt")


@pytest.mark.parametrize("env_value", ["development", "dev", "local", " DEV "])
def test_parser_allows_omitted_owner_id_only_in_approved_local_envs(
    isolate_storage, tmp_path, monkeypatch, env_value
):
    from backend.app.core.config import settings
    from backend.app.services import document_parser

    sample = tmp_path / "doc.txt"
    sample.write_text("hello local", encoding="utf-8")
    monkeypatch.setattr(settings, "ENV", env_value)
    monkeypatch.setattr(settings, "PARSER_SANDBOX_ENABLED", False)

    assert document_parser.extract_text(str(sample), "text/plain", "doc.txt") == "hello local"


def test_clamav_never_overwrites_existing_quarantine_or_leaves_partial_file(
    isolate_storage, tmp_path, monkeypatch
):
    import errno
    import os

    from backend.app.core.config import settings
    from backend.app.services import clamav

    user_id = uuid.uuid4()
    doc_id = uuid.uuid4()
    q_root = tmp_path / "quarantine"
    q_root.mkdir()
    monkeypatch.setattr(settings, "CLAMAV_QUARANTINE_DIR", str(q_root))

    staged = user_storage_dir(user_id, create=True) / "infected.tmp"
    staged.write_bytes(b"new-infected-payload")

    class _InfectedClamd:
        def __init__(self, *args, **kwargs):
            pass

        def ping(self):
            return "PONG"

        def instream(self, stream):
            stream.read()
            return {"stream": ("FOUND", "EICAR-Test-Signature")}

    monkeypatch.setattr(clamav.clamd, "ClamdNetworkSocket", _InfectedClamd)

    # 1. Existing quarantine destination must not be overwritten.
    existing_q = q_root / str(user_id) / str(doc_id)
    existing_q.parent.mkdir(parents=True, exist_ok=True)
    existing_q.write_bytes(b"original-quarantined-evidence")

    with pytest.raises(clamav.ClamAVUnavailableError):
        clamav.scan_and_quarantine(str(staged), user_id, doc_id)

    assert existing_q.read_bytes() == b"original-quarantined-evidence"

    # 2. Failed cross-device quarantine copy must not leave a partial quarantine file.
    existing_q.unlink()

    def _exdev_link(src, dst):
        raise OSError(errno.EXDEV, "Cross-device link")

    with monkeypatch.context() as m:
        m.setattr(os, "link", _exdev_link)
        m.setattr(clamav.shutil, "copyfileobj", MagicMock(side_effect=OSError("disk full")))

        with pytest.raises(clamav.ClamAVUnavailableError):
            clamav.scan_and_quarantine(str(staged), user_id, doc_id)

    assert not existing_q.exists()
    assert staged.exists()

    # 3. Existing symlink at quarantine destination must be rejected without following it.
    outside_target = tmp_path / "outside_target.txt"
    outside_target.write_text("do-not-overwrite", encoding="utf-8")
    existing_q.symlink_to(outside_target)

    with pytest.raises(UnsafeStoragePathError):
        clamav.scan_and_quarantine(str(staged), user_id, doc_id)
    assert outside_target.read_text(encoding="utf-8") == "do-not-overwrite"
    assert staged.exists()

    # 4. Successful EXDEV cross-device move copies, fsyncs, and unlinks source.
    existing_q.unlink()
    with monkeypatch.context() as m:
        m.setattr(os, "link", _exdev_link)
        with pytest.raises(clamav.InfectedFileError):
            clamav.scan_and_quarantine(str(staged), user_id, doc_id)
    assert existing_q.read_bytes() == b"new-infected-payload"
    assert not staged.exists()


def test_remove_image_files_cannot_redirect_deletion_via_symlink(isolate_storage, monkeypatch):
    from backend.app.services import document_images

    user_a = uuid.uuid4()
    user_b = uuid.uuid4()
    doc_a = uuid.uuid4()

    dir_b = user_storage_dir(user_b, create=True)
    victim_file = dir_b / "keep.txt"
    victim_file.write_text("must survive", encoding="utf-8")

    # Case 1: Nested directories + symlinked dir + symlinked file inside User A's image folder.
    folder_a = document_images.image_folder(user_a, doc_a)
    nested = folder_a / "nested" / "deep"
    nested.mkdir(parents=True, exist_ok=True)
    (nested / "1.png").write_bytes(b"nested-png")
    (folder_a / "nested_dir_link").symlink_to(dir_b)
    (folder_a / "file_link").symlink_to(victim_file)
    (folder_a / "0.png").write_bytes(b"png")

    document_images.remove_image_files(user_a, doc_a)
    assert not folder_a.exists()
    assert victim_file.read_text(encoding="utf-8") == "must survive"

    # Case 2: PermissionError during unlink stays inside owner folder and leaves victim untouched.
    import os

    folder_a.mkdir(parents=True, exist_ok=True)
    (folder_a / "0.png").write_bytes(b"png")
    real_unlink = os.unlink

    def _deny_unlink(path, *, dir_fd=None):
        raise PermissionError("denied")

    monkeypatch.setattr(os, "unlink", _deny_unlink)
    with pytest.raises(PermissionError):
        document_images.remove_image_files(user_a, doc_a)
    monkeypatch.setattr(os, "unlink", real_unlink)
    document_images.remove_image_files(user_a, doc_a)
    assert victim_file.read_text(encoding="utf-8") == "must survive"

    # Case 3: Symlink substitution of folder_a itself between image_folder() and os.open().
    sym_folder = user_storage_dir(user_a, create=True) / "images" / str(doc_a)
    sym_folder.parent.mkdir(parents=True, exist_ok=True)
    sym_folder.symlink_to(dir_b)
    monkeypatch.setattr(document_images, "image_folder", lambda u, d: sym_folder)

    with pytest.raises(UnsafeStoragePathError):
        document_images.remove_image_files(user_a, doc_a)
    assert victim_file.read_text(encoding="utf-8") == "must survive"
