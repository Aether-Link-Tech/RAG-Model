"""FR-069 (AL-99 / AL-166) — Security Audit Log write-path and RLS tests."""
from __future__ import annotations

import io
import logging
import os
import uuid

import pytest
from sqlalchemy import delete, select, update
from sqlalchemy.exc import DBAPIError
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.config import settings
from backend.app.core.security import create_access_token
from backend.app.db.rls import clear_rls_context, configure_rls_context
from backend.app.models.document import Document
from backend.app.models.security_audit_log import SecurityAuditLog
from backend.app.models.user import User
from backend.app.services import security_audit
from backend.tests.conftest import (
    TestingSessionLocal,
    _CleanupSessionLocal,
    purge_test_users,
)

VALID_TXT_BYTES = b"Valid plain text content for FR-069 security audit testing."


def _headers(user: User) -> dict[str, str]:
    return {"Authorization": f"Bearer {create_access_token(user.id, role=user.role)}"}


async def _fetch_audit_rows_for_user(user_id: uuid.UUID) -> list[SecurityAuditLog]:
    """Query security_audit_log via a separate superuser session."""
    async with _CleanupSessionLocal() as super_session:
        result = await super_session.execute(
            select(SecurityAuditLog)
            .where(SecurityAuditLog.user_id == user_id)
            .order_by(SecurityAuditLog.timestamp.asc())
        )
        return list(result.scalars().all())


# --- 1. Rejected upload writes one row with user_id, filename, reason, ip ----


@pytest.mark.asyncio
async def test_rejected_upload_writes_single_audit_row(async_client, test_user: User):
    response = await async_client.post(
        "/api/v1/documents",
        headers=_headers(test_user),
        files={"file": ("report.xml.pdf", io.BytesIO(b"%PDF-1.4 fake"), "application/pdf")},
    )
    assert response.status_code == 400

    rows = await _fetch_audit_rows_for_user(test_user.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id == test_user.id
    assert row.filename == "report.xml.pdf"
    assert "Extension chain validation failed" in row.reason
    assert row.ip_address == "127.0.0.1"
    assert row.timestamp is not None


# --- 2. Audit row survives when the request transaction fails/rolls back -----


@pytest.mark.asyncio
async def test_audit_row_survives_failed_request_rollback(
    async_client, test_user: User, db_session: AsyncSession, monkeypatch
):
    """Force a rejection (`InfectedFileError`) where `log_security_event` runs BEFORE
    `await _safe_rollback(db)` in `upload_document`, proving the audit row is committed
    in its own independent session and survives the request rollback.
    """
    from backend.app.api.v1 import documents as documents_api
    from backend.app.services import clamav

    def _raise_infected(path, user_id, document_id):
        raise clamav.InfectedFileError("Eicar-Test-Signature")

    monkeypatch.setattr(documents_api, "scan_and_quarantine", _raise_infected)

    response = await async_client.post(
        "/api/v1/documents",
        headers=_headers(test_user),
        files={"file": ("infected_rollback.txt", io.BytesIO(VALID_TXT_BYTES), "text/plain")},
    )
    assert response.status_code == 422

    # No document row was persisted
    docs = (
        await db_session.execute(select(Document).where(Document.user_id == test_user.id))
    ).scalars().all()
    assert docs == []

    # The security audit row survived `await _safe_rollback(db)` on the request session
    rows = await _fetch_audit_rows_for_user(test_user.id)
    assert len(rows) == 1
    assert rows[0].filename == "infected_rollback.txt"
    assert rows[0].reason == "Malware detected: Eicar-Test-Signature"


# --- 3. Replacement rejections in update_document write audit rows -----------


@pytest.mark.asyncio
async def test_update_document_replacement_rejections_write_audit_rows(
    async_client, test_user: User, monkeypatch
):
    up_res = await async_client.post(
        "/api/v1/documents",
        headers=_headers(test_user),
        files={"file": ("clean.txt", io.BytesIO(VALID_TXT_BYTES), "text/plain")},
    )
    assert up_res.status_code == 201
    doc_id = up_res.json()["id"]

    # 3a. Double-extension replacement rejection
    r_ext = await async_client.put(
        f"/api/v1/documents/{doc_id}",
        headers=_headers(test_user),
        files={"file": ("evil.exe.txt", io.BytesIO(b"hello"), "text/plain")},
    )
    assert r_ext.status_code == 400

    # 3b. Oversized stream replacement rejection (newly added call site in Step 5a)
    monkeypatch.setattr(settings, "MAX_UPLOAD_BYTES", 64)
    r_size = await async_client.put(
        f"/api/v1/documents/{doc_id}",
        headers=_headers(test_user),
        files={"file": ("huge_replace.txt", io.BytesIO(b"A" * 256), "text/plain")},
    )
    assert r_size.status_code == 400

    rows = await _fetch_audit_rows_for_user(test_user.id)
    assert len(rows) == 2
    assert rows[0].filename == "evil.exe.txt"
    assert "Replacement rejected" in rows[0].reason
    assert rows[1].filename == "huge_replace.txt"
    assert rows[1].reason == "File size limit exceeded during stream"


# --- 4. Ownership denial in delete_document writes an audit row --------------


@pytest.mark.asyncio
async def test_delete_document_ownership_denial_writes_audit_row(
    async_client, test_user: User, test_user_2: User
):
    pub_res = await async_client.post(
        "/api/v1/documents",
        headers=_headers(test_user),
        data={"visibility": "public"},
        files={"file": ("company_policy.txt", io.BytesIO(VALID_TXT_BYTES), "text/plain")},
    )
    assert pub_res.status_code == 201
    pub_doc_id = pub_res.json()["id"]

    # test_user_2 (non-owner, non-admin) attempts to delete test_user's public doc -> 403
    del_res = await async_client.delete(
        f"/api/v1/documents/{pub_doc_id}",
        headers=_headers(test_user_2),
    )
    assert del_res.status_code == 403

    rows = await _fetch_audit_rows_for_user(test_user_2.id)
    assert len(rows) == 1
    assert rows[0].user_id == test_user_2.id
    assert rows[0].filename == "company_policy.txt"
    assert rows[0].reason == "Unauthorized public document delete attempt"
    assert rows[0].ip_address == "127.0.0.1"


# --- 4b. Denial on another user's PRIVATE document stores document ID instead of filename ---


@pytest.mark.asyncio
async def test_delete_another_user_private_document_denial_records_document_id(
    async_client, test_user: User, test_user_2: User
):
    """User B attempts to delete User A's private document: audit row must store

    document ID as filename to avoid leaking private document names (AL-99).
    """
    secret_filename = "classified_financial_records.pdf"
    priv_res = await async_client.post(
        "/api/v1/documents",
        headers=_headers(test_user),
        data={"visibility": "private"},
        files={"file": (secret_filename, io.BytesIO(b"%PDF-1.4 sample"), "application/pdf")},
    )
    assert priv_res.status_code == 201
    priv_doc_id = priv_res.json()["id"]

    # test_user_2 attempts to delete test_user's private doc -> 404
    del_res = await async_client.delete(
        f"/api/v1/documents/{priv_doc_id}",
        headers=_headers(test_user_2),
    )
    assert del_res.status_code == 404

    rows = await _fetch_audit_rows_for_user(test_user_2.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id == test_user_2.id
    assert row.filename == str(priv_doc_id)
    assert secret_filename not in (row.filename or "")
    assert secret_filename not in (row.reason or "")
    assert "Unauthorized" in row.reason
    assert row.ip_address == "127.0.0.1"


# --- 4c. Non-owner denial on a PUBLIC document retains the real filename ---


@pytest.mark.asyncio
async def test_delete_another_user_public_document_denial_retains_real_filename(
    async_client, test_user: User, test_user_2: User
):
    """User B attempts to delete User A's public document: audit row keeps real filename."""
    public_filename = "public_terms_of_service.txt"
    pub_res = await async_client.post(
        "/api/v1/documents",
        headers=_headers(test_user),
        data={"visibility": "public"},
        files={"file": (public_filename, io.BytesIO(VALID_TXT_BYTES), "text/plain")},
    )
    assert pub_res.status_code == 201
    pub_doc_id = pub_res.json()["id"]

    # test_user_2 attempts to delete test_user's public doc -> 403
    del_res = await async_client.delete(
        f"/api/v1/documents/{pub_doc_id}",
        headers=_headers(test_user_2),
    )
    assert del_res.status_code == 403

    rows = await _fetch_audit_rows_for_user(test_user_2.id)
    assert len(rows) == 1
    row = rows[0]
    assert row.user_id == test_user_2.id
    assert row.filename == public_filename
    assert row.reason == "Unauthorized public document delete attempt"
    assert row.ip_address == "127.0.0.1"


# --- 5. Path-escape attempt in download_document writes an audit row ---------


@pytest.mark.asyncio
async def test_download_document_path_escape_writes_audit_row_and_missing_file_does_not(
    async_client, test_user: User, tmp_path
):
    up_res = await async_client.post(
        "/api/v1/documents",
        headers=_headers(test_user),
        files={"file": ("notes.txt", io.BytesIO(VALID_TXT_BYTES), "text/plain")},
    )
    assert up_res.status_code == 201
    doc_id = uuid.UUID(up_res.json()["id"])

    # 5a. Plain missing file inside UPLOAD_DIR -> 404, must NOT write a security audit row
    async with _CleanupSessionLocal() as admin_session:
        doc_obj = await admin_session.get(Document, doc_id)
        inside_path = doc_obj.file_path
    os.remove(inside_path)

    r_missing = await async_client.get(
        f"/api/v1/documents/{doc_id}/download",
        headers=_headers(test_user),
    )
    assert r_missing.status_code == 404
    assert await _fetch_audit_rows_for_user(test_user.id) == []

    # 5b. Stored file_path pointing outside UPLOAD_DIR -> 404, writes "Path traversal attempt blocked"
    outside_file = tmp_path / "outside_secret.txt"
    outside_file.write_text("secret host data", encoding="utf-8")

    async with _CleanupSessionLocal() as admin_session:
        await admin_session.execute(
            update(Document)
            .where(Document.id == doc_id)
            .values(file_path=str(outside_file))
        )
        await admin_session.commit()

    r_escape = await async_client.get(
        f"/api/v1/documents/{doc_id}/download",
        headers=_headers(test_user),
    )
    assert r_escape.status_code == 404

    rows = await _fetch_audit_rows_for_user(test_user.id)
    assert len(rows) == 1
    assert rows[0].user_id == test_user.id
    assert rows[0].filename == "notes.txt"
    assert rows[0].reason == "Path traversal attempt blocked"
    assert rows[0].ip_address == "127.0.0.1"


# --- 6. RLS SELECT access: 0 rows for normal user, visible to admin ----------


@pytest.mark.asyncio
async def test_rls_select_blocks_regular_user_and_allows_admin(
    async_client, test_user: User
):
    response = await async_client.post(
        "/api/v1/documents",
        headers=_headers(test_user),
        files={"file": ("bad.bat.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")},
    )
    assert response.status_code == 400

    # Regular user (`app.user_role = 'user'`) sees 0 rows
    async with TestingSessionLocal() as user_session:
        await configure_rls_context(user_session, test_user.id, "user")
        user_rows = (
            await user_session.execute(
                select(SecurityAuditLog).where(SecurityAuditLog.user_id == test_user.id)
            )
        ).scalars().all()
        assert user_rows == []
        clear_rls_context(user_session)

    # Admin (`app.user_role = 'admin'`) sees the audit row
    async with TestingSessionLocal() as admin_session:
        await configure_rls_context(admin_session, test_user.id, "admin")
        admin_rows = (
            await admin_session.execute(
                select(SecurityAuditLog).where(SecurityAuditLog.user_id == test_user.id)
            )
        ).scalars().all()
        assert len(admin_rows) == 1
        assert admin_rows[0].filename == "bad.bat.pdf"
        clear_rls_context(admin_session)


# --- 7. rag_app cannot UPDATE or DELETE rows in security_audit_log -----------


@pytest.mark.asyncio
async def test_rag_app_cannot_update_or_delete_audit_rows(
    async_client, test_user: User
):
    response = await async_client.post(
        "/api/v1/documents",
        headers=_headers(test_user),
        files={"file": ("immutable.cmd.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")},
    )
    assert response.status_code == 400

    async with TestingSessionLocal() as app_session:
        await configure_rls_context(app_session, test_user.id, "admin")

        with pytest.raises(DBAPIError, match="permission denied for table security_audit_log"):
            await app_session.execute(
                update(SecurityAuditLog)
                .where(SecurityAuditLog.user_id == test_user.id)
                .values(reason="tampered")
            )
        await app_session.rollback()

        await configure_rls_context(app_session, test_user.id, "admin")
        with pytest.raises(DBAPIError, match="permission denied for table security_audit_log"):
            await app_session.execute(
                delete(SecurityAuditLog).where(SecurityAuditLog.user_id == test_user.id)
            )
        await app_session.rollback()
        clear_rls_context(app_session)


# --- 8. Deleted user's audit rows survive user hard deletion -----------------


@pytest.mark.asyncio
async def test_deleted_users_audit_rows_survive(async_client, db_session: AsyncSession):
    temp_user_id = uuid.uuid4()
    temp_user = User(
        id=temp_user_id,
        email=f"ephemeral-{temp_user_id.hex[:8]}@example.com",
        hashed_password="hashed_password",
        role="user",
        is_deleted=False,
    )
    db_session.add(temp_user)
    await db_session.commit()

    response = await async_client.post(
        "/api/v1/documents",
        headers=_headers(temp_user),
        files={"file": ("survive_user_delete.exe.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")},
    )
    assert response.status_code == 400

    rows_before = await _fetch_audit_rows_for_user(temp_user_id)
    assert len(rows_before) == 1

    # Hard-delete the user as superuser
    await purge_test_users(temp_user_id)

    async with _CleanupSessionLocal() as super_session:
        assert await super_session.get(User, temp_user_id) is None

    # Audit row still exists with the same user_id
    rows_after = await _fetch_audit_rows_for_user(temp_user_id)
    assert len(rows_after) == 1
    assert rows_after[0].id == rows_before[0].id
    assert rows_after[0].user_id == temp_user_id
    assert rows_after[0].filename == "survive_user_delete.exe.pdf"


# --- 9. Forced DB failure in record_security_event preserves HTTP 400 --------


@pytest.mark.asyncio
async def test_forced_db_failure_in_audit_service_does_not_alter_http_response(
    async_client, test_user: User, monkeypatch, caplog
):
    class _BrokenSessionContext:
        async def __aenter__(self):
            raise RuntimeError("Simulated database outage during audit insert")

        async def __aexit__(self, exc_type, exc, tb):
            return False

    monkeypatch.setattr(
        security_audit,
        "AsyncSessionLocal",
        lambda: _BrokenSessionContext(),
    )

    with caplog.at_level(logging.ERROR, logger="backend.app.services.security_audit"):
        response = await async_client.post(
            "/api/v1/documents",
            headers=_headers(test_user),
            files={"file": ("broken_audit.js.pdf", io.BytesIO(b"%PDF-1.4"), "application/pdf")},
        )

    assert response.status_code == 400
    assert "suspicious double extension" in response.json()["detail"].lower()
    assert any(
        "Failed to write security audit row" in record.getMessage()
        for record in caplog.records
    )
