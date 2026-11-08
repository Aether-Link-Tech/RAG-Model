"""Authentication routes: FR-001 register, FR-002 login, FR-003 logout."""
import logging
import math
from typing import Annotated

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request, status
from fastapi.security import HTTPAuthorizationCredentials
from sqlalchemy import select
from sqlalchemy.exc import IntegrityError

from backend.app.api.deps import CurrentUser, DbSession, bearer_scheme
from backend.app.core.config import settings
from backend.app.core.roles import UserRole
from backend.app.core.security import (
    TokenValidationError,
    create_access_token,
    create_email_verification_token,
    create_refresh_token,
    decode_access_token,
    decode_email_verification_token,
    decode_refresh_token,
    hash_password,
    password_needs_rehash,
    verify_password,
)
from backend.app.models.user import User
from backend.app.schemas.auth import (
    LoginRequest,
    LogoutRequest,
    MessageResponse,
    RefreshRequest,
    RegisterRequest,
    RegisterResponse,
    ResendVerificationRequest,
    TokenResponse,
    VerifyEmailResponse,
)
from backend.app.services.auth_state import (
    AuthStateUnavailableError,
    blacklist_token,
    claim_resend_slot,
    failed_login_count,
    is_token_blacklisted,
    lockout_seconds_remaining,
    record_failed_login,
    reset_failed_logins,
)
from backend.app.services.captcha import verify_captcha
from backend.app.services.email import send_verification_email

logger = logging.getLogger(__name__)

router = APIRouter(prefix="/auth", tags=["Authentication"])

INVALID_CREDENTIALS = "Invalid email or password"
CAPTCHA_FAILED = "Captcha verification failed."
CAPTCHA_REQUIRED = "Captcha verification is required."
RESEND_MESSAGE = (
    "If an unverified account exists for this email, a new verification link has been sent."
)


def _client_ip(request: Request) -> str | None:
    return request.client.host if request.client else None


def _service_unavailable(exc: Exception) -> HTTPException:
    logger.error("Auth state store unavailable: %s", exc)
    return HTTPException(
        status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
        detail="Authentication service is temporarily unavailable.",
    )


def _locked(seconds: int) -> HTTPException:
    minutes = max(1, math.ceil(seconds / 60))
    return HTTPException(
        status_code=status.HTTP_423_LOCKED,
        detail=f"Too many failed login attempts. Try again in {minutes} minute(s).",
        headers={"Retry-After": str(max(seconds, 1))},
    )


def _unauthorized(detail: str = INVALID_CREDENTIALS) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_401_UNAUTHORIZED,
        detail=detail,
        headers={"WWW-Authenticate": "Bearer"},
    )


def _issue_tokens(user: User) -> TokenResponse:
    return TokenResponse(
        access_token=create_access_token(user.id, role=user.role),
        refresh_token=create_refresh_token(user.id),
        token_type="bearer",
        expires_in=settings.ACCESS_TOKEN_EXPIRE_MINUTES * 60,
        user=user,
    )


async def _active_user_by_email(db: DbSession, email: str) -> User | None:
    result = await db.execute(
        select(User).where(User.email == email, User.is_deleted.is_(False))
    )
    return result.scalar_one_or_none()


# --- FR-001: registration and email verification -----------------------------------


@router.post("/register", response_model=RegisterResponse, status_code=status.HTTP_201_CREATED)
async def register(
    payload: RegisterRequest,
    request: Request,
    db: DbSession,
    background_tasks: BackgroundTasks,
) -> RegisterResponse:
    """Create a pending account and email a 24-hour verification link (AL-115, AL-117, AL-118)."""
    if not await verify_captcha(payload.captcha_token, _client_ip(request)):
        raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=CAPTCHA_FAILED)

    user = User(
        email=payload.email,
        full_name=payload.full_name,
        hashed_password=hash_password(payload.password),
        role=UserRole.USER.value,
        is_verified=False,
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

    token = create_email_verification_token(user.id, user.email)
    background_tasks.add_task(send_verification_email, user.email, token)
    return RegisterResponse(user_id=user.id)


@router.get("/verify-email", response_model=VerifyEmailResponse)
async def verify_email(
    db: DbSession,
    token: Annotated[str, Query(min_length=1, max_length=4096)],
) -> VerifyEmailResponse:
    invalid = HTTPException(
        status_code=status.HTTP_400_BAD_REQUEST,
        detail="This verification link is invalid or has expired.",
    )
    try:
        claims = decode_email_verification_token(token)
    except TokenValidationError as exc:
        raise invalid from exc

    result = await db.execute(
        select(User).where(User.id == claims["user_id"], User.is_deleted.is_(False))
    )
    user = result.scalar_one_or_none()
    # The token is bound to the address it was sent to.
    if user is None or user.email != claims["email"]:
        raise invalid

    if not user.is_verified:
        user.is_verified = True
        await db.commit()
    return VerifyEmailResponse()


@router.post(
    "/resend-verification",
    response_model=MessageResponse,
    status_code=status.HTTP_202_ACCEPTED,
)
async def resend_verification(
    payload: ResendVerificationRequest,
    db: DbSession,
    background_tasks: BackgroundTasks,
) -> MessageResponse:
    """Always answers the same way, so it cannot be used to discover accounts."""
    email = payload.email.strip().lower()
    user = await _active_user_by_email(db, email)
    if user is not None and not user.is_verified:
        try:
            allowed = await claim_resend_slot(email)
        except AuthStateUnavailableError as exc:
            raise _service_unavailable(exc) from exc
        if allowed:
            token = create_email_verification_token(user.id, user.email)
            background_tasks.add_task(send_verification_email, user.email, token)
    return MessageResponse(message=RESEND_MESSAGE)


# --- FR-002: login with lockout ------------------------------------------------------


@router.post("/login", response_model=TokenResponse, status_code=status.HTTP_200_OK)
async def login(payload: LoginRequest, request: Request, db: DbSession) -> TokenResponse:
    """Authenticate and issue an access + refresh token pair (AL-120, AL-121).

    The failed-attempt counter is keyed by email whether or not the account
    exists, so responses never reveal which emails are registered.
    """
    email = payload.email.lower().strip()
    try:
        failures = await failed_login_count(email)
        if failures >= settings.LOGIN_MAX_FAILED_ATTEMPTS:
            raise _locked(await lockout_seconds_remaining(email))
    except AuthStateUnavailableError as exc:
        raise _service_unavailable(exc) from exc

    if failures >= settings.LOGIN_CAPTCHA_AFTER_FAILURES:
        if not payload.captcha_token:
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=CAPTCHA_REQUIRED)
        if not await verify_captcha(payload.captcha_token, _client_ip(request)):
            raise HTTPException(status_code=status.HTTP_400_BAD_REQUEST, detail=CAPTCHA_FAILED)

    user = await _active_user_by_email(db, email)
    if user is None or not verify_password(payload.password, user.hashed_password):
        try:
            failures = await record_failed_login(email)
            if failures >= settings.LOGIN_MAX_FAILED_ATTEMPTS:
                raise _locked(await lockout_seconds_remaining(email))
        except AuthStateUnavailableError as exc:
            raise _service_unavailable(exc) from exc
        raise _unauthorized()

    try:
        await reset_failed_logins(email)
    except AuthStateUnavailableError as exc:
        raise _service_unavailable(exc) from exc

    # Only reached with the right password, so this does not leak account existence.
    if not user.is_verified:
        raise HTTPException(
            status_code=status.HTTP_403_FORBIDDEN,
            detail="Please verify your email address before signing in.",
        )

    # Upgrade legacy PBKDF2 (or low-cost bcrypt) hashes on successful login.
    if password_needs_rehash(user.hashed_password):
        user.hashed_password = hash_password(payload.password)
        await db.commit()
        await db.refresh(user)

    return _issue_tokens(user)


@router.post("/refresh", response_model=TokenResponse)
async def refresh_tokens(payload: RefreshRequest, db: DbSession) -> TokenResponse:
    """Swap a refresh token for a new pair. The role is re-read from the database,
    so a role change takes effect here (FR-004). The old refresh token is revoked."""
    try:
        claims = decode_refresh_token(payload.refresh_token)
    except TokenValidationError as exc:
        raise _unauthorized("Invalid or expired refresh token.") from exc

    try:
        if await is_token_blacklisted(payload.refresh_token):
            raise _unauthorized("Refresh token has been revoked.")
    except AuthStateUnavailableError as exc:
        raise _service_unavailable(exc) from exc

    result = await db.execute(
        select(User).where(User.id == claims["user_id"], User.is_deleted.is_(False))
    )
    user = result.scalar_one_or_none()
    if user is None or not user.is_verified:
        raise _unauthorized("Invalid or expired refresh token.")

    try:
        await blacklist_token(payload.refresh_token, claims["exp"])
    except AuthStateUnavailableError as exc:
        raise _service_unavailable(exc) from exc
    return _issue_tokens(user)


# --- FR-003: logout ------------------------------------------------------------------


@router.post("/logout", response_model=MessageResponse)
async def logout(
    current_user: CurrentUser,
    credentials: Annotated[HTTPAuthorizationCredentials | None, Depends(bearer_scheme)],
    payload: LogoutRequest | None = None,
) -> MessageResponse:
    """Revoke the caller's access token (and refresh token, if sent) until they expire."""
    # get_current_user has already validated this token.
    access_token = credentials.credentials
    access_claims = decode_access_token(access_token)

    refresh_exp: int | None = None
    if payload is not None and payload.refresh_token:
        try:
            refresh_claims = decode_refresh_token(payload.refresh_token)
        except TokenValidationError:
            refresh_claims = None
        # Only revoke a refresh token that belongs to the caller.
        if refresh_claims is not None and refresh_claims["user_id"] == current_user.id:
            refresh_exp = refresh_claims["exp"]

    try:
        await blacklist_token(access_token, access_claims["exp"])
        if refresh_exp is not None:
            await blacklist_token(payload.refresh_token, refresh_exp)
    except AuthStateUnavailableError as exc:
        raise _service_unavailable(exc) from exc
    return MessageResponse(message="Logged out.")
