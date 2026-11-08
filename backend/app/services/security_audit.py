"""Security audit log persistence service (FR-069 / AL-99)."""
from __future__ import annotations

import logging
import uuid

from backend.app.db.session import AsyncSessionLocal
from backend.app.models.security_audit_log import SecurityAuditLog

logger = logging.getLogger(__name__)


async def record_security_event(
    user_id: uuid.UUID | str | None,
    filename: str | None,
    reason: str,
    ip: str | None,
) -> None:
    """Persist a security rejection event in its own committed transaction.

    Connects via `AsyncSessionLocal` as `rag_app` without setting RLS user context
    (the table's INSERT policy is `WITH CHECK (true)`). Never raises to callers so
    that audit failures cannot alter the HTTP rejection response.
    """
    try:
        parsed_user_id: uuid.UUID | None
        if user_id is None or isinstance(user_id, uuid.UUID):
            parsed_user_id = user_id
        else:
            parsed_user_id = uuid.UUID(str(user_id))

        safe_filename = filename[:255] if filename is not None else None
        safe_reason = (reason or "")[:255]
        safe_ip = ip[:45] if ip is not None else None

        entry = SecurityAuditLog(
            user_id=parsed_user_id,
            filename=safe_filename,
            reason=safe_reason,
            ip_address=safe_ip,
        )
        async with AsyncSessionLocal() as session:
            session.add(entry)
            await session.commit()
    except Exception:
        logger.exception("Failed to write security audit row")
