from fastapi import APIRouter, HTTPException, status
from sqlalchemy import select

from backend.app.api.deps import DbSession
from backend.app.core.config import settings
from backend.app.core.security import create_access_token, verify_password
from backend.app.models.user import User
from backend.app.schemas.auth import LoginRequest, TokenResponse

router = APIRouter(prefix="/auth", tags=["Authentication"])


@router.post("/login", response_model=TokenResponse, status_code=status.HTTP_200_OK)
async def login(payload: LoginRequest, db: DbSession) -> TokenResponse:
    """Authenticate user credentials and issue a signed Bearer JWT access token."""
    normalized_email = payload.email.lower().strip()
    result = await db.execute(
        select(User).where(
            User.email == normalized_email,
            User.is_deleted.is_(False),
        )
    )
    user = result.scalar_one_or_none()
    if user is None or not verify_password(payload.password, user.hashed_password):
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid email or password",
            headers={"WWW-Authenticate": "Bearer"},
        )

    access_token = create_access_token(user.id, role=user.role)
    return TokenResponse(
        access_token=access_token,
        token_type="bearer",
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=user,
    )
