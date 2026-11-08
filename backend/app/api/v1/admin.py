"""Admin-only routes (FR-004 / AL-124, AL-126).

The role guard is applied once at router level, so every route added under
/admin is admin-only by default: regular users get 403, anonymous callers 401.
"""
import uuid

from fastapi import APIRouter, Depends
from sqlalchemy import select

from backend.app.api.deps import AdminUser, DbSession, require_roles
from backend.app.api.v1 import users as users_api
from backend.app.core.roles import UserRole
from backend.app.models.role import Role
from backend.app.models.user import User
from backend.app.schemas.user import RoleResponse, UserResponse, UserRoleUpdate

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
