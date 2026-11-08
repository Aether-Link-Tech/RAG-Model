"""Authentication endpoints (FR-001 registration, FR-002 login, FR-003 logout)."""
from __future__ import annotations

import hashlib
import secrets
from datetime import datetime, timedelta, timezone
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Response, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.app.api.deps import DbSession, bearer_scheme
from backend.app.core.config import settings
from backend.app.core.crypto import email_index, normalize_email
from backend.app.core.redis import RedisUnavailableError
from backend.app.core.roles import UserRole
from backend.app.core.security import (
    TokenValidationError,
    create_access_token,
    create_refresh_token,
    decode_access_token,
    decode_refresh_token,
    hash_password,
    password_needs_rehash,
    verify_password,
)
from backend.app.models.user import User, UserStatus
from backend.app.schemas.auth import (
    CaptchaChallenge,
    LoginRequest,
    LogoutRequest,
    MessageResponse,
    RefreshRequest,
    RegisterRequest,
    ResendVerificationRequest,
    TokenResponse,
    VerifyEmailRequest,
)
from backend.app.schemas.user import UserResponse
from backend.app.services import auth_security, captcha
from backend.app.services.email_service import send_verification_email

router = APIRouter(prefix="/auth", tags=["Authentication"])

CAPTCHA_REQUIRED_HEADER = "X-Captcha-Required"
INVALID_CREDENTIALS = "Invalid email or password."
# Compared against when the email is unknown, so response time does not reveal
# whether an account exists.
_DUMMY_HASH = hash_password("dummy-password-for-timing-only!A1")


def _service_unavailable() -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Authentication service temporarily unavailable. Please try again.",
    )


def _new_verification_token() -> tuple[str, str, datetime]:
    token = secrets.token_urlsafe(32)
    token_hash = hashlib.sha256(token.encode("ascii")).hexdigest()
    expires_at = datetime.now(timezone.utc) + timedelta(hours=settings.EMAIL_VERIFICATION_TTL_HOURS)
    return token, token_hash, expires_at


def _token_pair(user: User) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token(user.id, role=user.role),
        refresh_token=create_refresh_token(user.id, role=user.role),
        token_type="bearer",
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=UserResponse.model_validate(user),
    )


async def _require_captcha(solution: str | None) -> None:
    try:
        await captcha.verify_solution(solution)
    except captcha.CaptchaError as exc:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail=str(exc),
            headers={CAPTCHA_REQUIRED_HEADER: "true"},
        ) from exc
    except RedisUnavailableError as exc:
        raise _service_unavailable() from exc


@router.get("/captcha", response_model=CaptchaChallenge)
async def get_captcha_challenge(response: Response) -> dict:
    """Issue a proof-of-work challenge for register/login/resend (FR-001 / FR-002)."""
    response.headers["Cache-Control"] = "no-store"
    return captcha.create_challenge()


@router.post("/register", response_model=UserResponse, status_code=status.HTTP_201_CREATED)
async def register(payload: RegisterRequest, background: BackgroundTasks, db: DbSession) -> User:
    """Self-service sign-up. The account stays 'pending' until the email link is used."""
    await _require_captcha(payload.captcha)

    existing = await db.execute(select(User.id).where(User.email_hash == email_index(payload.email)))
    if existing.scalar_one_or_none() is not None:
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        )

    token, token_hash, expires_at = _new_verification_token()
    user = User(
        email=payload.email,
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        role=UserRole.USER.value,
        status=UserStatus.PENDING,
        email_verification_hash=token_hash,
        email_verification_expires_at=expires_at,
    )
    db.add(user)
    try:
        await db.commit()
    except IntegrityError as exc:
        await db.rollback()
        raise HTTPException(
            status_code=status.HTTP_409_CONFLICT,
            detail="An account with this email already exists.",
        ) from exc
    await db.refresh(user)
    background.add_task(send_verification_email, payload.email, payload.full_name, token)
    return user


@router.post("/verify-email", response_model=MessageResponse)
async def verify_email(payload: VerifyEmailRequest, db: DbSession) -> MessageResponse:
    token_hash = hashlib.sha256(payload.token.encode("ascii", errors="ignore")).hexdigest()
    result = await db.execute(
        select(User).where(User.email_verification_hash == token_hash, User.is_deleted.is_(False))
    )
    user = result.scalar_one_or_none()
    if user is None:
        raise HTTPException(
            status_code=status.HTTP_400_BAD_REQUEST,
            detail="This verification link is invalid or has already been used.",
        )
    expires_at = user.email_verification_expires_at
    if expires_at is None or expires_at < datetime.now(timezone.utc):
        raise HTTPException(
            status_code=status.HTTP_410_GONE,
            detail="This verification link has expired. Request a new one.",
        )
    user.status = UserStatus.ACTIVE
    user.email_verified_at = datetime.now(timezone.utc)
    user.email_verification_hash = None
    user.email_verification_expires_at = None
    await db.commit()
    return MessageResponse(detail="Email verified. You can sign in now.")


@router.post(
    "/resend-verification",
    response_model=MessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def resend_verification(
    payload: ResendVerificationRequest, background: BackgroundTasks, db: DbSession
) -> MessageResponse:
    """Always answers the same way, so it cannot be used to discover accounts."""
    await _require_captcha(payload.captcha)
    result = await db.execute(
        select(User).where(
            User.email_hash == email_index(payload.email),
            User.is_deleted.is_(False),
            User.status == UserStatus.PENDING,
        )
    )
    user = result.scalar_one_or_none()
    if user is not None:
        token, token_hash, expires_at = _new_verification_token()
        user.email_verification_hash = token_hash
        user.email_verification_expires_at = expires_at
        await db.commit()
        background.add_task(send_verification_email, user.email, user.full_name, token)
    return MessageResponse(
        detail="If an unverified account exists for this email, a new link is on its way."
    )


@router.post("/login", response_model=TokenResponse, status_code=status.HTTP_200_OK)
async def login(payload: LoginRequest, db: DbSession) -> TokenResponse:
    """Authenticate and issue an access + refresh token pair (FR-002).

    Failed attempts are counted per email (whether or not the account exists):
    a captcha is required after LOGIN_CAPTCHA_AFTER_FAILURES and the email is locked
    for LOGIN_LOCKOUT_MINUTES after LOGIN_MAX_FAILURES.
    """
    email = normalize_email(payload.email)
    account_key = email_index(email)
    try:
        locked_for = await auth_security.lockout_remaining_seconds(account_key)
        if locked_for:
            raise HTTPException(
                status_code=status.HTTP_423_LOCKED,
                detail="Too many failed sign-in attempts. Try again in 15 minutes.",
                headers={"Retry-After": str(locked_for)},
            )
        if await auth_security.failed_attempts(account_key) >= settings.LOGIN_CAPTCHA_AFTER_FAILURES:
            await _require_captcha(payload.captcha)

        result = await db.execute(
            select(User).where(User.email_hash == account_key, User.is_deleted.is_(False))
        )
        user = result.scalar_one_or_none()
        password_ok = verify_password(payload.password, user.hashed_password if user else _DUMMY_HASH)

        if user is None or not password_ok:
            failures = await auth_security.record_failed_login(account_key)
            if failures >= settings.LOGIN_MAX_FAILURES:
                raise HTTPException(
                    status_code=status.HTTP_423_LOCKED,
                    detail="Too many failed sign-in attempts. Try again in 15 minutes.",
                    headers={"Retry-After": str(settings.LOGIN_LOCKOUT_MINUTES * 60)},
                )
            headers = {"WWW-Authenticate": "Bearer"}
            if failures >= settings.LOGIN_CAPTCHA_AFTER_FAILURES:
                headers[CAPTCHA_REQUIRED_HEADER] = "true"
            raise HTTPException(status_code=status.HTTP_401_UNAUTHORIZED, detail=INVALID_CREDENTIALS, headers=headers)

        await auth_security.reset_failed_logins(account_key)
    except RedisUnavailableError as exc:
        raise _service_unavailable() from exc

    if not user.is_active:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Please verify your email address first. Check your inbox for the link.",
        )

    if password_needs_rehash(user.hashed_password):
        user.hashed_password = hash_password(payload.password)
        await db.commit()
        await db.refresh(user)

    return _token_pair(user)


@router.post("/refresh", response_model=TokenResponse)
async def refresh(payload: RefreshRequest, db: DbSession) -> TokenResponse:
    """Exchange a refresh token for a new pair. The old refresh token is revoked (rotation)."""
    unauthorized = HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail="Session has ended. Please sign in again.",
        headers={"WWW-Authenticate": "Bearer"},
    )
    try:
        claims = decode_refresh_token(payload.refresh_token)
    except TokenValidationError as exc:
        raise unauthorized from exc
    try:
        if await auth_security.is_token_revoked(claims.get("jti")):
            raise unauthorized
        result = await db.execute(
            select(User).where(User.id == claims["user_id"], User.is_deleted.is_(False))
        )
        user = result.scalar_one_or_none()
        # A role change invalidates the session: the next sign-in carries the new role.
        if user is None or not user.is_active or user.role != claims["role"]:
            raise unauthorized
        await auth_security.revoke_token(claims.get("jti"), claims.get("exp"))
    except RedisUnavailableError as exc:
        raise _service_unavailable() from exc
    return _token_pair(user)


@router.post("/logout", status_code=status.HTTP_204_NO_CONTENT)
async def logout(
    payload: LogoutRequest | None = None,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)] = None,
) -> Response:
    """Revoke the access token and, if sent, the refresh token (FR-003).

    Works even when the access token already expired, so the refresh token can
    always be revoked.
    """
    to_revoke: list[dict] = []
    subject = None
    if credentials and credentials.credentials:
        try:
            access = decode_access_token(credentials.credentials)
            to_revoke.append(access)
            subject = access["user_id"]
        except TokenValidationError:
            pass
    if payload and payload.refresh_token:
        try:
            refresh_claims = decode_refresh_token(payload.refresh_token)
            if subject is None or refresh_claims["user_id"] == subject:
                to_revoke.append(refresh_claims)
        except TokenValidationError:
            pass
    if not to_revoke:
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Not authenticated",
            headers={"WWW-Authenticate": "Bearer"},
        )
    try:
        for claims in to_revoke:
            await auth_security.revoke_token(claims.get("jti"), claims.get("exp"))
    except RedisUnavailableError as exc:
        raise _service_unavailable() from exc
    return Response(status_code=status.HTTP_204_NO_CONTENT)
