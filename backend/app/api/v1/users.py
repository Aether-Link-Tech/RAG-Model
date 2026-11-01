import logging
import uuid

from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.app.api.deps import CurrentUser, DbSession
from backend.app.core.security import hash_password
from backend.app.models.user import User
from backend.app.schemas.user import UserCreate, UserResponse
from backend.app.services.vector_store import delete_user_private_knowledge

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/users", tags=["Users"])


async def _soft_delete_user(db: DbSession, target_user: User) -> User:
    """Soft-delete a user account and purge only their private vector collection.

    Public documents uploaded by the user remain intact in PostgreSQL, disk
    storage, and the shared public Chroma collection.
    """
    try:
        delete_user_private_knowledge(target_user.id)
    except Exception:
        logger.warning(
            "Best-effort private vector collection cleanup failed for user %s",
            target_user.id,
            exc_info=True,
        )
    target_user.is_deleted = True
    await db.commit()
    await db.refresh(target_user)
    return target_user


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(payload: UserCreate, db: DbSession) -> User:
    user = User(
        email=payload.email.lower().strip(),
        hashed_password=hash_password(payload.password),
        role="user",
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="A user with this email already exists",
        ) from exc
    await db.refresh(user)
    return user


@router.get("", response_model=list[UserResponse])
async def list_users(current_user: CurrentUser, db: DbSession) -> list[User]:
    if current_user.role != "admin":
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Administrator privileges required to list users.",
        )
    result = await db.execute(
        select(User)
        .where(User.is_deleted.is_(False))
        .order_by(User.created_at.desc())
    )
    return list(result.scalars().all())


@router.get("/me", response_model=UserResponse)
async def read_me(current_user: CurrentUser) -> User:
    return current_user


@router.delete("/me/private-collection", status_code=status.HTTP_200_OK)
async def delete_my_private_collection(current_user: CurrentUser) -> dict:
    deleted = delete_user_private_knowledge(current_user.id)
    return {
        "user_id": str(current_user.id),
        "private_collection_deleted": deleted,
        "public_collection_untouched": True,
    }


@router.delete("/me", response_model=UserResponse)
async def delete_me(current_user: CurrentUser, db: DbSession) -> User:
    return await _soft_delete_user(db, current_user)


@router.delete("/{user_id}", response_model=UserResponse)
async def delete_user(
    user_id: uuid.UUID,
    current_user: CurrentUser,
    db: DbSession,
) -> User:
    if current_user.role != "admin" and current_user.id != user_id:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Not authorized to delete this user account.",
        )
    result = await db.execute(
        select(User).where(User.id == user_id, User.is_deleted.is_(False))
    )
    target_user = result.scalar_one_or_none()
    if target_user is None:
        raise HTTPException(
            status_code=status.HTTP_404_NOT_FOUND,
            detail="User not found",
        )
    return await _soft_delete_user(db, target_user)
