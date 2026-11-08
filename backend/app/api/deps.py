from collections.abc import Awaitable, Callable
from typing import Annotated

from fastapi import Depends, HTTPException, status
from fastapi.security import HTTPAuthorizationCredentials, HTTPBearer
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from backend.app.core.roles import UserRole
from backend.app.core.security import TokenValidationError, decode_access_token
from backend.app.db.rls import configure_rls_context
from backend.app.db.session import get_db
from backend.app.models.user import User

DbSession = Annotated[AsyncSession, Depends(get_db)]

bearer_scheme = HTTPBearer(auto_error=False)


async def get_current_user(
    db: DbSession,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> User:
    """Authenticate caller via Bearer JWT and bind `app.user_id` to the DB transaction (AL-129).

    Never accepts client-supplied user ID headers (`X-User-Id`) or parameters as proof of identity.
    """
    if credentials is None or credentials.scheme.lower() != "bearer" or not credentials.credentials:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )

    try:
        token_payload = decode_access_token(credentials.credentials)
    except TokenValidationError as exc:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail=str(exc),
            headers={"WWW-Authenticate": "Bearer"},
        ) from exc

    user_id = token_payload["user_id"]
    result = await db.execute(
        select(User).where(User.id == user_id, User.is_deleted.is_(False))
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Unknown or deleted user",
            headers={"WWW-Authenticate": "Bearer"},
        )

    # The database is the source of truth for roles. A token minted before a role
    # change (e.g. an admin was demoted) must not keep its old privileges.
    if token_payload["role"] != user.role:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Token role is out of date. Please sign in again.",
            headers={"WWW-Authenticate": "Bearer"},
        )

    await configure_rls_context(db, user.id, user.role)
    return user


CurrentUser = Annotated[User, Depends(get_current_user)]


def require_roles(*allowed_roles: UserRole) -> Callable[[User], Awaitable[User]]:
    """Build a dependency that only lets users holding one of `allowed_roles` through (AL-21).

    Unauthenticated callers get 401 (from `get_current_user`); authenticated callers
    without a permitted role get 403.
    """
    if not allowed_roles:
        raise ValueError("require_roles() needs at least one role.")
    permitted = frozenset(UserRole(role) for role in allowed_roles)

    async def _role_guard(current_user: CurrentUser) -> User:
        if current_user.role not in permitted:
            raise HTTPException(
                status_code=status.HTTP_403_FORBIDDEN,
                detail="You do not have permission to perform this action.",
            )
        return current_user

    return _role_guard


AdminUser = Annotated[User, Depends(require_roles(UserRole.ADMIN))]
