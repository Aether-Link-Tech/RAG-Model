from fastapi import APIRouter, HTTPException, status
from sqlalchemy.exc import IntegrityError

from backend.app.api.deps import CurrentUser, DbSession
from backend.app.core.security import hash_password
from backend.app.models.user import User
from backend.app.schemas.user import UserCreate, UserResponse
from backend.app.services.vector_store import delete_user_private_knowledge

router = APIRouter(prefix="/users", tags=["Users"])


@router.post("", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def create_user(payload: UserCreate, db: DbSession) -> User:
    user = User(
        email=payload.email.lower().strip(),
        hashed_password=hash_password(payload.password),
        role=payload.role,
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
    delete_user_private_knowledge(current_user.id)
    current_user.is_deleted = True
    await db.commit()
    await db.refresh(current_user)
    return current_user
