"""Admin-only routes (FR-004 / AL-124, AL-126).

The role guard is applied once at router level, so every route added under
/admin is admin-only by default: regular users get 403, anonymous callers 401.
"""
import uuid
from datetime import datetime
from typing import Annotated, Literal

from fastapi import APIRouter, Depends, Query
from pydantic import BaseModel, ConfigDict
from sqlalchemy import func, select

from backend.app.api.deps import AdminUser, DbSession, require_roles
from backend.app.api.v1 import users as users_api
from backend.app.core.roles import UserRole
from backend.app.models.audit import SecurityAuditLog
from backend.app.models.role import Role
from backend.app.models.user import User
from backend.app.schemas.user import RoleResponse, UserResponse, UserRoleUpdate
from backend.app.services.audit_log import EVENT_TYPES

router = APIRouter(
    prefix="/admin",
    tags=["Admin"],
    dependencies=[Depends(require_roles(UserRole.ADMIN))],
)


@router.get("/users", response_model=list[UserResponse])
async def admin_list_users(current_admin: AdminUser, db: DbSession) -> list[User]:
    return await users_api.list_users(current_admin, db)


@router.patch("/users/{user_id}/role", response_model=UserResponse)
async def admin_update_user_role(
    user_id: uuid.UUID,
    payload: UserRoleUpdate,
    current_admin: AdminUser,
    db: DbSession,
) -> User:
    return await users_api.update_user_role(user_id, payload, current_admin, db)


@router.get("/roles", response_model=list[RoleResponse])
async def admin_list_roles(db: DbSession) -> list[Role]:
    result = await db.execute(select(Role).order_by(Role.id))
    return list(result.scalars().all())


# --- FR-069 / AL-168: security audit log ------------------------------------------


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
    db: DbSession,
    event_type: Annotated[Literal[EVENT_TYPES] | None, Query()] = None,  # type: ignore[valid-type]
    user_id: uuid.UUID | None = None,
    since: datetime | None = None,
    until: datetime | None = None,
    q: Annotated[str | None, Query(max_length=100, description="Search filename or reason")] = None,
    limit: Annotated[int, Query(ge=1, le=200)] = 50,
    offset: Annotated[int, Query(ge=0)] = 0,
) -> AuditLogPage:
    """Security audit log, newest first. PostgreSQL RLS also limits reads to admins."""
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
        escaped = q.replace("\\", "\\\\").replace("%", "\%").replace("_", "\_")
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
