import asyncio
import io
import os
import uuid
from pathlib import Path

import pytest
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.models.document import Document
from backend.app.models.user import User

VALID_PDF_BYTES = b"%PDF-1.4\n1 0 obj\n<<\n>>\nendobj\ntrailer\n<<\n>>\n%%EOF"
VALID_TXT_BYTES = b"Hello, this is a valid plain text document for RAG indexing."
VALID_DOCX_BYTES = b"PK\x03\x04\x14\x00\x00\x00\x08\x00Test docx mock content"


def _auth_headers(user: User) -> dict[str, str]:
    return {"X-User-Id": str(user.id)}


def _list_all_files(root_dir: str) -> list[str]:
    if not os.path.exists(root_dir):
        return []
    return [str(p) for p in Path(root_dir).rglob("*") if p.is_file()]


@pytest.mark.asyncio
async def test_first_private_upload_succeeds(
    async_client, test_user: User, db_session: AsyncSession
):
    """Verify that a valid first-time private document upload succeeds with HTTP 201."""
    files = {"file": ("annual_report.pdf", io.BytesIO(VALID_PDF_BYTES), "application/pdf")}

    response = await async_client.post(
        "/api/v1/documents",
        headers=_auth_headers(test_user),
        files=files,
    )
    assert response.status_code == 201
    res_data = response.json()

    assert res_data["filename"] == "annual_report.pdf"
    assert res_data["mime_type"] == "application/pdf"
    assert res_data["file_size"] == len(VALID_PDF_BYTES)
    assert len(res_data["file_hash"]) == 64
    assert res_data["visibility"] == "private"
    assert res_data["status"] == "pending"
    assert res_data["user_id"] == str(test_user.id)

    doc_id = uuid.UUID(res_data["id"])
    doc_in_db = await db_session.get(Document, doc_id)
    assert doc_in_db is not None
    assert doc_in_db.file_hash == res_data["file_hash"]
    assert os.path.exists(doc_in_db.file_path)
    assert Path(doc_in_db.file_path).read_bytes() == VALID_PDF_BYTES
    # Ensure no leftover .tmp staging files remain
    assert not [p for p in _list_all_files(settings.UPLOAD_DIR) if p.endswith(".tmp")]


@pytest.mark.asyncio
async def test_private_duplicate_same_user_rejected_with_409(
    async_client, test_user: User, db_session: AsyncSession
):
    """Verify duplicate private upload for the same user is rejected with HTTP 409."""
    res_1 = await async_client.post(
        "/api/v1/documents",
        headers=_auth_headers(test_user),
        data={"visibility": "private"},
        files={"file": ("contract.txt", io.BytesIO(VALID_TXT_BYTES), "text/plain")},
    )
    assert res_1.status_code == 201

    res_2 = await async_client.post(
        "/api/v1/documents",
        headers=_auth_headers(test_user),
        data={"visibility": "private"},
        files={"file": ("renamed_copy_contract.txt", io.BytesIO(VALID_TXT_BYTES), "text/plain")},
    )
    assert res_2.status_code == 409
    assert "private document" in res_2.json()["detail"].lower()

    stmt = select(Document).where(Document.user_id == test_user.id)
    docs = (await db_session.execute(stmt)).scalars().all()
    assert len(docs) == 1
    assert len(_list_all_files(settings.UPLOAD_DIR)) == 1


@pytest.mark.asyncio
async def test_private_duplicate_across_different_users_allowed(
    async_client, test_user: User, test_user_2: User
):
    """Verify different users may independently upload identical private documents."""
    shared_private_bytes = b"%PDF-1.4 Confidential Multi-Tenant Private Document"

    res_u1 = await async_client.post(
        "/api/v1/documents",
        headers=_auth_headers(test_user),
        data={"visibility": "private"},
        files={"file": ("u1_private.pdf", io.BytesIO(shared_private_bytes), "application/pdf")},
    )
    assert res_u1.status_code == 201

    res_u2 = await async_client.post(
        "/api/v1/documents",
        headers=_auth_headers(test_user_2),
        data={"visibility": "private"},
        files={"file": ("u2_private.pdf", io.BytesIO(shared_private_bytes), "application/pdf")},
    )
    assert res_u2.status_code == 201

    doc_u1 = res_u1.json()
    doc_u2 = res_u2.json()
    assert doc_u1["file_hash"] == doc_u2["file_hash"]
    assert doc_u1["id"] != doc_u2["id"]
    assert doc_u1["user_id"] != doc_u2["user_id"]


@pytest.mark.asyncio
async def test_public_duplicate_rejected_globally_across_users(
    async_client, test_user: User, test_user_2: User, db_session: AsyncSession
):
    """Verify public knowledge pool deduplication is global across all uploaders."""
    public_bytes = b"%PDF-1.4 Shared Company Policy Handbook"

    res_u1 = await async_client.post(
        "/api/v1/documents",
        headers=_auth_headers(test_user),
        data={"visibility": "public"},
        files={"file": ("handbook_v1.pdf", io.BytesIO(public_bytes), "application/pdf")},
    )
    assert res_u1.status_code == 201
    assert res_u1.json()["visibility"] == "public"

    # Different user attempts to upload identical bytes to the public pool -> 409
    res_u2 = await async_client.post(
        "/api/v1/documents",
        headers=_auth_headers(test_user_2),
        data={"visibility": "public"},
        files={"file": ("handbook_copy.pdf", io.BytesIO(public_bytes), "application/pdf")},
    )
    assert res_u2.status_code == 409
    detail = res_u2.json()["detail"]
    assert "public document" in detail.lower()
    # Must not leak uploader user_id or filename
    assert str(test_user.id) not in detail
    assert "handbook_v1.pdf" not in detail

    stmt = select(Document).where(
        Document.visibility == "public",
        Document.file_hash == res_u1.json()["file_hash"],
    )
    docs = (await db_session.execute(stmt)).scalars().all()
    assert len(docs) == 1


@pytest.mark.asyncio
async def test_cross_scope_independence_between_private_and_public(
    async_client, test_user: User, test_user_2: User
):
    """
    Verify private and public scopes do not collide or act as an existence oracle:
    - A private document uploaded by User 1 does NOT block User 2 (or User 1) from
      uploading the same content to `public`.
    - A public document does NOT block User 2 from uploading a private copy.
    """
    cross_scope_bytes = b"Cross-scope independence verification payload."

    # 1. User 1 uploads privately
    res_priv_u1 = await async_client.post(
        "/api/v1/documents",
        headers=_auth_headers(test_user),
        data={"visibility": "private"},
        files={"file": ("secret_notes.txt", io.BytesIO(cross_scope_bytes), "text/plain")},
    )
    assert res_priv_u1.status_code == 201

    # 2. User 2 uploads identical bytes to public pool -> succeeds (does not reveal User 1's private doc)
    res_pub_u2 = await async_client.post(
        "/api/v1/documents",
        headers=_auth_headers(test_user_2),
        data={"visibility": "public"},
        files={"file": ("published_notes.txt", io.BytesIO(cross_scope_bytes), "text/plain")},
    )
    assert res_pub_u2.status_code == 201

    # 3. User 2 also uploads identical bytes to their own private workspace -> succeeds
    res_priv_u2 = await async_client.post(
        "/api/v1/documents",
        headers=_auth_headers(test_user_2),
        data={"visibility": "private"},
        files={"file": ("my_private_notes.txt", io.BytesIO(cross_scope_bytes), "text/plain")},
    )
    assert res_priv_u2.status_code == 201


@pytest.mark.asyncio
async def test_explicit_integrity_error_branch_deletes_failed_file_and_keeps_winner(
    async_client, test_user: User, db_session: AsyncSession, monkeypatch
):
    """
    Explicitly exercise the PostgreSQL `IntegrityError` branch (bypassing the application
    SELECT pre-check) and verify:
    1. Winner's DB record and disk file remain intact.
    2. Loser hits PostgreSQL `uq_documents_private_user_hash` IntegrityError -> HTTP 409.
    3. Loser's staged/persisted file is deleted from disk.
    """
    payload = b"%PDF-1.4 Deterministic PostgreSQL IntegrityError Branch Test"

    # Step 1: Persist the winning document normally
    res_winner = await async_client.post(
        "/api/v1/documents",
        headers=_auth_headers(test_user),
        data={"visibility": "private"},
        files={"file": ("winner.pdf", io.BytesIO(payload), "application/pdf")},
    )
    assert res_winner.status_code == 201
    winner_id = uuid.UUID(res_winner.json()["id"])
    winner_doc = await db_session.get(Document, winner_id)
    assert winner_doc is not None
    assert os.path.exists(winner_doc.file_path)

    # Step 2: Simulate a race condition where Request #2's pre-check SELECT saw no row
    # so Request #2 writes its own file to disk and hits PostgreSQL's unique index on commit.
    original_execute = AsyncSession.execute

    class _EmptyDuplicateCheckResult:
        def scalar_one_or_none(self):
            return None

    async def _bypass_duplicate_precheck(self, statement, *args, **kwargs):
        sql_str = str(statement)
        if "FROM documents" in sql_str and "documents.file_hash" in sql_str:
            return _EmptyDuplicateCheckResult()
        return await original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", _bypass_duplicate_precheck)

    res_loser = await async_client.post(
        "/api/v1/documents",
        headers=_auth_headers(test_user),
        data={"visibility": "private"},
        files={"file": ("loser_race.pdf", io.BytesIO(payload), "application/pdf")},
    )
    assert res_loser.status_code == 409

    # Restore original AsyncSession.execute without undoing autouse UPLOAD_DIR patch
    monkeypatch.setattr(AsyncSession, "execute", original_execute)

    # Verify winner's file is completely intact and loser's file was removed
    assert os.path.exists(winner_doc.file_path)
    assert Path(winner_doc.file_path).read_bytes() == payload
    stored_files = _list_all_files(settings.UPLOAD_DIR)
    assert stored_files == [winner_doc.file_path]

    stmt = select(Document).where(Document.user_id == test_user.id)
    docs = (await db_session.execute(stmt)).scalars().all()
    assert len(docs) == 1
    assert docs[0].id == winner_id


@pytest.mark.asyncio
async def test_explicit_public_integrity_error_branch_across_users(
    async_client, test_user: User, test_user_2: User, db_session: AsyncSession, monkeypatch
):
    """
    Explicitly exercise PostgreSQL `uq_documents_public_hash` IntegrityError across
    two distinct uploaders when the application pre-check is bypassed.
    """
    payload = b"%PDF-1.4 Deterministic Public Pool IntegrityError Branch Test"

    res_winner = await async_client.post(
        "/api/v1/documents",
        headers=_auth_headers(test_user),
        data={"visibility": "public"},
        files={"file": ("public_winner.pdf", io.BytesIO(payload), "application/pdf")},
    )
    assert res_winner.status_code == 201
    winner_id = uuid.UUID(res_winner.json()["id"])
    winner_doc = await db_session.get(Document, winner_id)
    assert winner_doc is not None

    original_execute = AsyncSession.execute

    class _EmptyDuplicateCheckResult:
        def scalar_one_or_none(self):
            return None

    async def _bypass_duplicate_precheck(self, statement, *args, **kwargs):
        sql_str = str(statement)
        if "FROM documents" in sql_str and "documents.file_hash" in sql_str:
            return _EmptyDuplicateCheckResult()
        return await original_execute(self, statement, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "execute", _bypass_duplicate_precheck)
    try:
        res_loser = await async_client.post(
            "/api/v1/documents",
            headers=_auth_headers(test_user_2),
            data={"visibility": "public"},
            files={"file": ("public_loser.pdf", io.BytesIO(payload), "application/pdf")},
        )
        assert res_loser.status_code == 409
        assert "public document" in res_loser.json()["detail"].lower()
    finally:
        monkeypatch.setattr(AsyncSession, "execute", original_execute)

    assert os.path.exists(winner_doc.file_path)
    assert Path(winner_doc.file_path).read_bytes() == payload
    assert _list_all_files(settings.UPLOAD_DIR) == [winner_doc.file_path]


@pytest.mark.asyncio
async def test_concurrent_identical_uploads_private_and_public(
    async_client, test_user: User, test_user_2: User, db_session: AsyncSession
):
    """Verify concurrent uploads cannot bypass private or public partial unique indexes."""
    priv_content = b"%PDF-1.4 Concurrent Private Race Payload"
    pub_content = b"%PDF-1.4 Concurrent Public Pool Race Payload"

    async def _upload(user: User, filename: str, body: bytes, visibility: str):
        return await async_client.post(
            "/api/v1/documents",
            headers=_auth_headers(user),
            data={"visibility": visibility},
            files={"file": (filename, io.BytesIO(body), "application/pdf")},
        )

    # 1. Concurrent private uploads by same user
    priv_responses = await asyncio.gather(
        _upload(test_user, "priv_1.pdf", priv_content, "private"),
        _upload(test_user, "priv_2.pdf", priv_content, "private"),
    )
    assert sorted(r.status_code for r in priv_responses) == [201, 409]

    # 2. Concurrent public uploads by two DIFFERENT users
    pub_responses = await asyncio.gather(
        _upload(test_user, "pub_u1.pdf", pub_content, "public"),
        _upload(test_user_2, "pub_u2.pdf", pub_content, "public"),
    )
    assert sorted(r.status_code for r in pub_responses) == [201, 409]

    # Exactly 2 committed files on disk (1 private winner + 1 public winner), zero .tmp files
    all_files = _list_all_files(settings.UPLOAD_DIR)
    assert len(all_files) == 2
    assert not [p for p in all_files if p.endswith(".tmp")]

    all_docs = (
        await db_session.execute(
            select(Document).where(Document.user_id.in_([test_user.id, test_user_2.id]))
        )
    ).scalars().all()
    assert len(all_docs) == 2


@pytest.mark.asyncio
async def test_post_commit_refresh_failure_preserves_committed_file(
    async_client, test_user: User, db_session: AsyncSession, monkeypatch
):
    """
    Verify Finding 3 / Rule 8: If `db.commit()` succeeds and `db.refresh()` subsequently
    raises an error, the committed document's stored file on disk MUST NOT be deleted.
    """
    payload = b"Committed document content that must survive a post-commit refresh failure."
    original_refresh = AsyncSession.refresh

    async def _fail_on_document_refresh(self, instance, *args, **kwargs):
        if isinstance(instance, Document):
            raise RuntimeError("Simulated post-commit refresh failure")
        return await original_refresh(self, instance, *args, **kwargs)

    monkeypatch.setattr(AsyncSession, "refresh", _fail_on_document_refresh)

    with pytest.raises(RuntimeError, match="Simulated post-commit refresh failure"):
        await async_client.post(
            "/api/v1/documents",
            headers=_auth_headers(test_user),
            data={"visibility": "private"},
            files={"file": ("survive_refresh.txt", io.BytesIO(payload), "text/plain")},
        )

    # Verify the DB record WAS committed and its referenced file STILL exists intact on disk
    stmt = select(Document).where(Document.user_id == test_user.id)
    committed_doc = (await db_session.execute(stmt)).scalar_one_or_none()
    assert committed_doc is not None
    assert os.path.exists(committed_doc.file_path)
    assert Path(committed_doc.file_path).read_bytes() == payload


@pytest.mark.asyncio
async def test_pre_commit_database_failure_rolls_back_and_cleans_up_files(
    async_client, test_user: User, db_session: AsyncSession, monkeypatch
):
    """Verify pre-commit failure triggers rollback and deletes staged/uncommitted files."""
    original_commit = AsyncSession.commit

    async def _mock_commit_failure(self, *args, **kwargs):
        raise RuntimeError("Simulated database failure during commit")

    monkeypatch.setattr(AsyncSession, "commit", _mock_commit_failure)
    try:
        with pytest.raises(RuntimeError, match="Simulated database failure"):
            await async_client.post(
                "/api/v1/documents",
                headers=_auth_headers(test_user),
                files={"file": ("uncommitted.txt", io.BytesIO(VALID_TXT_BYTES), "text/plain")},
            )
    finally:
        monkeypatch.setattr(AsyncSession, "commit", original_commit)

    assert _list_all_files(settings.UPLOAD_DIR) == []
    stmt = select(Document).where(Document.user_id == test_user.id)
    assert (await db_session.execute(stmt)).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_cancelled_error_before_commit_cleans_up_staged_file(
    async_client, test_user: User, db_session: AsyncSession, monkeypatch
):
    """Verify `asyncio.CancelledError` (BaseException) cleans up staged/uncommitted files."""
    original_commit = AsyncSession.commit

    async def _cancelled_commit(self, *args, **kwargs):
        raise asyncio.CancelledError("Simulated client disconnect during commit")

    monkeypatch.setattr(AsyncSession, "commit", _cancelled_commit)
    try:
        upload_task = asyncio.create_task(
            async_client.post(
                "/api/v1/documents",
                headers=_auth_headers(test_user),
                files={"file": ("cancelled.txt", io.BytesIO(VALID_TXT_BYTES), "text/plain")},
            )
        )
        with pytest.raises(asyncio.CancelledError):
            await upload_task
    finally:
        monkeypatch.setattr(AsyncSession, "commit", original_commit)

    assert _list_all_files(settings.UPLOAD_DIR) == []
    stmt = select(Document).where(Document.user_id == test_user.id)
    assert (await db_session.execute(stmt)).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_oversized_upload_rejected_during_streaming_with_400(
    async_client, test_user: User, db_session: AsyncSession, monkeypatch
):
    """Verify oversized uploads are rejected during streaming without leaving files or DB rows."""
    monkeypatch.setattr(settings, "MAX_UPLOAD_BYTES", 1024)  # 1 KB limit
    oversized_txt = b"A" * 8192  # 8 KB payload

    response = await async_client.post(
        "/api/v1/documents",
        headers=_auth_headers(test_user),
        files={"file": ("too_big.txt", io.BytesIO(oversized_txt), "text/plain")},
    )
    assert response.status_code == 400
    assert "exceeds maximum limit" in response.json()["detail"].lower()
    assert _list_all_files(settings.UPLOAD_DIR) == []

    stmt = select(Document).where(Document.user_id == test_user.id)
    assert (await db_session.execute(stmt)).scalar_one_or_none() is None


@pytest.mark.asyncio
async def test_validation_rejects_empty_unsupported_and_spoofed_files(
    async_client, test_user: User
):
    """Verify empty files, unsupported extensions, and spoofed magic bytes return HTTP 400."""
    # Empty file
    r_empty = await async_client.post(
        "/api/v1/documents",
        headers=_auth_headers(test_user),
        files={"file": ("empty.txt", io.BytesIO(b""), "text/plain")},
    )
    assert r_empty.status_code == 400

    # Unsupported extension
    r_ext = await async_client.post(
        "/api/v1/documents",
        headers=_auth_headers(test_user),
        files={"file": ("payload.exe", io.BytesIO(b"MZ..."), "application/octet-stream")},
    )
    assert r_ext.status_code == 400

    # Spoofed PDF magic bytes
    r_spoof = await async_client.post(
        "/api/v1/documents",
        headers=_auth_headers(test_user),
        files={"file": ("fake.pdf", io.BytesIO(b"Not a real PDF header"), "application/pdf")},
    )
    assert r_spoof.status_code == 400
    assert "signature" in r_spoof.json()["detail"].lower()


@pytest.mark.asyncio
async def test_unknown_user_returns_401(async_client):
    """Verify upload with unknown X-User-Id returns 401 Unauthorized."""
    response = await async_client.post(
        "/api/v1/documents",
        headers={"X-User-Id": str(uuid.uuid4())},
        files={"file": ("doc.txt", io.BytesIO(VALID_TXT_BYTES), "text/plain")},
    )
    assert response.status_code == 401


@pytest.mark.asyncio
async def test_declared_content_type_cannot_override_extension_mime(
    async_client, test_user: User, db_session: AsyncSession
):
    """A declared Content-Type for a different supported type must not override the extension's MIME."""
    response = await async_client.post(
        "/api/v1/documents",
        headers=_auth_headers(test_user),
        files={"file": ("notes.txt", io.BytesIO(VALID_TXT_BYTES), "application/pdf")},
    )
    assert response.status_code == 201
    assert response.json()["mime_type"] == "text/plain"

    doc = await db_session.get(Document, uuid.UUID(response.json()["id"]))
    assert doc.mime_type == "text/plain"


class _FakePgError(Exception):
    def __init__(self, sqlstate, constraint_name, message="fake"):
        super().__init__(message)
        self.sqlstate = sqlstate
        self.constraint_name = constraint_name


def _integrity_error(sqlstate, constraint_name, message="duplicate key ... file_hash"):
    from sqlalchemy.exc import IntegrityError

    return IntegrityError("INSERT ...", {}, _FakePgError(sqlstate, constraint_name, message))


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "constraint", ["uq_documents_private_user_hash", "uq_documents_public_hash"]
)
async def test_integrity_error_23505_on_approved_index_returns_409(
    async_client, test_user: User, db_session: AsyncSession, monkeypatch, constraint
):
    """SQLSTATE 23505 on an approved unique index maps to 409 and removes the file."""
    original_commit = AsyncSession.commit

    async def _raise_duplicate(self, *args, **kwargs):
        raise _integrity_error("23505", constraint)

    monkeypatch.setattr(AsyncSession, "commit", _raise_duplicate)
    try:
        response = await async_client.post(
            "/api/v1/documents",
            headers=_auth_headers(test_user),
            files={"file": ("dup.txt", io.BytesIO(VALID_TXT_BYTES), "text/plain")},
        )
    finally:
        monkeypatch.setattr(AsyncSession, "commit", original_commit)

    assert response.status_code == 409
    assert _list_all_files(settings.UPLOAD_DIR) == []


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "sqlstate,constraint",
    [
        ("23505", "ix_some_other_unique_index"),  # unique violation, unapproved index
        ("23503", "uq_documents_private_user_hash"),  # FK violation, even with approved name
        ("23502", None),  # not-null violation
    ],
)
async def test_unrelated_integrity_error_returns_400_not_409(
    async_client, test_user: User, monkeypatch, sqlstate, constraint
):
    """Unrelated integrity violations (even mentioning file_hash) must not be reported as duplicates."""
    original_commit = AsyncSession.commit

    async def _raise_other(self, *args, **kwargs):
        raise _integrity_error(sqlstate, constraint, "violation mentioning file_hash column")

    monkeypatch.setattr(AsyncSession, "commit", _raise_other)
    try:
        response = await async_client.post(
            "/api/v1/documents",
            headers=_auth_headers(test_user),
            files={"file": ("other.txt", io.BytesIO(VALID_TXT_BYTES), "text/plain")},
        )
    finally:
        monkeypatch.setattr(AsyncSession, "commit", original_commit)

    assert response.status_code == 400
    assert "integrity constraint" in response.json()["detail"].lower()
    assert _list_all_files(settings.UPLOAD_DIR) == []
