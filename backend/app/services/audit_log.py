"""Security audit log writer (FR-069 / AL-167).

Every rejected upload/replacement, ownership denial and path-escape attempt is written
to `security_audit_log` in its own session and transaction, so the record survives
even when the request itself is rolled back. Writing never raises: a logging failure
must not turn a clean 4xx rejection into a 500.
"""
from __future__ import annotations

import logging
import uuid
from datetime import datetime, timedelta, timezone

from sqlalchemy import delete, insert, text
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker

from backend.app.core.config import settings
from backend.app.models.audit import SecurityAuditLog

logger = logging.getLogger(__name__)

# Event types (stable values; the admin viewer filters on them)
UPLOAD_REJECTED = "upload_rejected"
REPLACE_REJECTED = "replace_rejected"
ACCESS_DENIED = "access_denied"
DOWNLOAD_BLOCKED = "download_blocked"
OCR_IMAGE_SKIPPED = "ocr_image_skipped"  # FR-063: an image could not be OCR-ed and was skipped
EVENT_TYPES = (UPLOAD_REJECTED, REPLACE_REJECTED, ACCESS_DENIED, DOWNLOAD_BLOCKED, OCR_IMAGE_SKIPPED)

_session_factory: async_sessionmaker[AsyncSession] | None = None


def set_audit_session_factory(factory: async_sessionmaker[AsyncSession] | None) -> None:
    """Tests point the writer at their NullPool session factory."""
    global _session_factory
    _session_factory = factory


def _factory() -> async_sessionmaker[AsyncSession]:
    if _session_factory is not None:
        return _session_factory
    from backend.app.db.session import AsyncSessionLocal

    return AsyncSessionLocal


def _as_uuid(value: uuid.UUID | str | None) -> uuid.UUID | None:
    if value is None or isinstance(value, uuid.UUID):
        return value
    try:
        return uuid.UUID(str(value))
    except ValueError:
        return None


async def record_security_event(
    *,
    event_type: str,
    user_id: uuid.UUID | str | None,
    filename: str | None,
    reason: str,
    ip_address: str | None,
) -> None:
    logger.warning(
        "[SECURITY AUDIT] event=%s user=%s file=%s reason=%s", event_type, user_id, filename, reason
    )
    try:
        # Core INSERT without RETURNING: writers must not need read access to the log,
        # which RLS reserves for admins.
        async with _factory()() as session:
            await session.execute(
                insert(SecurityAuditLog).values(
                    id=uuid.uuid4(),
                    user_id=_as_uuid(user_id),
                    event_type=event_type,
                    filename=(filename or None) and filename[:255],
                    reason=reason[:500],
                    ip_address=ip_address or None,
                )
            )
            await session.commit()
    except Exception:
        logger.exception("Security audit event could not be persisted")


async def purge_expired_entries() -> int:
    """Delete entries older than the retention period (FR-069: 90 days)."""
    cutoff = datetime.now(timezone.utc) - timedelta(days=settings.AUDIT_LOG_RETENTION_DAYS)
    async with _factory()() as session:
        # A DELETE must also see the rows it filters on, and RLS shows the log only to
        # admins. This system job runs as admin for this transaction only; the delete
        # policy still restricts it to rows past the retention period.
        await session.execute(text("SELECT set_config('app.user_role', 'admin', true)"))
        result = await session.execute(
            delete(SecurityAuditLog).where(SecurityAuditLog.created_at < cutoff)
        )
        await session.commit()
        return result.rowcount or 0
