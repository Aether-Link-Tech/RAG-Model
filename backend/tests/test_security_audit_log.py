"""FR-069 security audit log (AL-166, AL-167, AL-168)."""
import io
import uuid
from datetime import datetime, timedelta, timezone

import pytest
from sqlalchemy import select, text

from backend.app.db.rls import configure_rls_context
from backend.app.models.audit import SecurityAuditLog
from backend.app.services import audit_log
from backend.tests.conftest import TestingSessionLocal, _CleanupSessionLocal, bearer


async def _entries_for(user_id) -> list:
    async with _CleanupSessionLocal() as session:
        rows = await session.execute(
            text(
                "SELECT event_type, filename, reason, ip_address FROM security_audit_log "
                "WHERE user_id = :u ORDER BY created_at"
            ),
            {"u": user_id},
        )
        return rows.all()


async def _clear(user_id) -> None:
    async with _CleanupSessionLocal() as session:
        await session.execute(text("DELETE FROM security_audit_log WHERE user_id = :u"), {"u": user_id})
        await session.commit()


@pytest.mark.asyncio
async def test_rejected_upload_is_logged_with_required_fields(async_client, test_user):
    try:
        res = await async_client.post(
            "/api/v1/documents",
            headers=bearer(test_user),
            data={"visibility": "private"},
            files={"file": ("payload.exe", io.BytesIO(b"MZ" + b"\0" * 64), "application/octet-stream")},
        )
        assert res.status_code == 400
        rows = await _entries_for(test_user.id)
        assert len(rows) == 1
        row = rows[0]
        assert row.event_type == audit_log.UPLOAD_REJECTED
        assert row.filename == "payload.exe"
        assert row.reason
        # Client IP is stored encrypted (FR-007)
        assert row.ip_address is None or row.ip_address.startswith("v1:")
    finally:
        await _clear(test_user.id)


@pytest.mark.asyncio
async def test_rejected_replacement_and_ownership_denials_are_logged(async_client, test_user, test_user_2):
    try:
        upload = await async_client.post(
            "/api/v1/documents",
            headers=bearer(test_user),
            data={"visibility": "public"},
            files={"file": ("shared.txt", io.BytesIO(f"shared {uuid.uuid4()}".encode()), "text/plain")},
        )
        doc_id = upload.json()["id"]

        bad_replace = await async_client.put(
            f"/api/v1/documents/{doc_id}",
            headers=bearer(test_user),
            files={"file": ("evil.pdf.exe", io.BytesIO(b"MZ"), "application/octet-stream")},
        )
        assert bad_replace.status_code == 400

        denied_update = await async_client.put(
            f"/api/v1/documents/{doc_id}", headers=bearer(test_user_2), data={"filename": "mine.txt"}
        )
        assert denied_update.status_code == 403
        denied_delete = await async_client.delete(f"/api/v1/documents/{doc_id}", headers=bearer(test_user_2))
        assert denied_delete.status_code == 403

        owner_events = {r.event_type for r in await _entries_for(test_user.id)}
        assert audit_log.REPLACE_REJECTED in owner_events
        other_events = [r.event_type for r in await _entries_for(test_user_2.id)]
        assert other_events.count(audit_log.ACCESS_DENIED) == 2
    finally:
        await _clear(test_user.id)
        await _clear(test_user_2.id)


@pytest.mark.asyncio
async def test_audit_log_is_admin_only_via_api_and_rls(async_client, test_user, admin_user):
    try:
        await audit_log.record_security_event(
            event_type=audit_log.UPLOAD_REJECTED,
            user_id=test_user.id,
            filename="report_final.pdf",
            reason="Magic bytes validation failed",
            ip_address="203.0.113.7",
        )
        assert (await async_client.get("/api/v1/admin/audit-log")).status_code == 401
        assert (await async_client.get("/api/v1/admin/audit-log", headers=bearer(test_user))).status_code == 403

        page = await async_client.get(
            "/api/v1/admin/audit-log",
            params={"user_id": str(test_user.id), "q": "final", "event_type": "upload_rejected"},
            headers=bearer(admin_user),
        )
        assert page.status_code == 200
        body = page.json()
        assert body["total"] == 1
        assert body["items"][0]["ip_address"] == "203.0.113.7"  # decrypted for the admin
        assert body["items"][0]["filename"] == "report_final.pdf"

        # Wildcards in the search are literal
        none = await async_client.get(
            "/api/v1/admin/audit-log", params={"user_id": str(test_user.id), "q": "%"}, headers=bearer(admin_user)
        )
        assert none.json()["total"] == 0

        # Even with direct SQL, a non-admin role sees nothing (RLS) and cannot change rows
        async with TestingSessionLocal() as session:
            await configure_rls_context(session, test_user.id, "user")
            assert (await session.execute(select(SecurityAuditLog))).scalars().all() == []
            await session.rollback()
    finally:
        await _clear(test_user.id)


@pytest.mark.asyncio
async def test_entries_older_than_90_days_are_purged(test_user):
    try:
        await audit_log.record_security_event(
            event_type=audit_log.UPLOAD_REJECTED, user_id=test_user.id, filename="old.txt", reason="old", ip_address=None
        )
        await audit_log.record_security_event(
            event_type=audit_log.UPLOAD_REJECTED, user_id=test_user.id, filename="new.txt", reason="new", ip_address=None
        )
        async with _CleanupSessionLocal() as session:
            await session.execute(
                text(
                    "UPDATE security_audit_log SET created_at = :t WHERE user_id = :u AND filename = 'old.txt'"
                ),
                {"t": datetime.now(timezone.utc) - timedelta(days=91), "u": test_user.id},
            )
            await session.commit()

        removed = await audit_log.purge_expired_entries()
        assert removed >= 1
        assert [r.filename for r in await _entries_for(test_user.id)] == ["new.txt"]
    finally:
        await _clear(test_user.id)


@pytest.mark.asyncio
async def test_audit_log_is_append_only_for_the_app_role(test_user):
    try:
        await audit_log.record_security_event(
            event_type=audit_log.UPLOAD_REJECTED, user_id=test_user.id, filename="a.txt", reason="r", ip_address=None
        )
        async with TestingSessionLocal() as session:
            await configure_rls_context(session, test_user.id, "admin")
            with pytest.raises(Exception, match="permission denied"):
                await session.execute(text("UPDATE security_audit_log SET reason = 'tampered'"))
            await session.rollback()
    finally:
        await _clear(test_user.id)
