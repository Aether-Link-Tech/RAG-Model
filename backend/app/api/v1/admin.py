"""Admin-only endpoints (FR-004 / FR-069)."""
from __future__ import annotations

import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from backend.app.api.deps import AdminUser, DbSession
from backend.app.models.audit import SecurityAuditLog
from backend.app.services.audit_log import EVENT_TYPES

router = APIRouter(prefix="/admin", tags=["Admin"])


class AuditLogEntry(BaseModel):
    id: uuid.UUID
    user_id: uuid.UUID | None
    event_type: str
    filename: str | None
    reason: str
    ip_address: str | None
    created_at: datetime

    model_config = ConfigDict(from_attributes=True)


class AuditLogPage(BaseModel):
    items: list[AuditLogEntry]
    total: int
    limit: int
    offset: int


@router.get("/audit-log", response_model=AuditLogPage)
async def list_audit_log(
    _admin: AdminUser,
    db: DbSession,
    event_type: Annotated[Literal[EVENT_TYPES] | None, Query()] = None,  # type: ignore[valid-type]
    user_id: uuid.UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    q: Annotated[str | None, Query(max_length=100, description="Search filename or reason")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AuditLogPage:
    """Security audit log, newest first (FR-069 / AL-168). PostgreSQL RLS also limits
    reads to admins, so this stays safe even if the role guard were removed."""
    filters = []
    if event_type:
        filters.append(SecurityAuditLog.event_type == event_type)
    if user_id:
        filters.append(SecurityAuditLog.user_id == user_id)
    if since:
        filters.append(SecurityAuditLog.created_at >= since)
    if until:
        filters.append(SecurityAuditLog.created_at <= until)
    if q:
        escaped = q.replace("\\", "\\\\").replace("%", "\\%").replace("_", "\\_")
        pattern = "%" + escaped + "%"
        filters.append(
            SecurityAuditLog.filename.ilike(pattern, escape="\\")
            | SecurityAuditLog.reason.ilike(pattern, escape="\\")
        )

    total = (await db.execute(select(func.count(SecurityAuditLog.id)).where(*filters))).scalar_one()
    rows = await db.execute(
        select(SecurityAuditLog)
        .where(*filters)
        .order_by(SecurityAuditLog.created_at.desc())
        .limit(limit)
        .offset(offset)
    )
    return AuditLogPage(
        items=[AuditLogEntry.model_validate(row) for row in rows.scalars().all()],
        total=int(total),
        limit=limit,
        offset=offset,
    )
