from datetime import datetime, timedelta, timezone
import base64
import hashlib
import hmac
from typing import Any
import uuid

import bcrypt
import jwt
from jwt import PyJWTError

from backend.app.core.config import settings
from backend.app.core.roles import UserRole

# Token types carried in the `typ` claim. Access tokens minted before this
# claim existed have no `typ` and are still treated as access tokens.
ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"
EMAIL_VERIFICATION_TOKEN_TYPE = "email_verification"

_LEGACY_PBKDF2_ITERATIONS = 120_000


class TokenValidationError(ValueError):
    """Raised when a JWT access token is missing required claims, expired, or invalid."""


# --- Passwords (FR-001 / AL-116, FR-007 / AL-135) --------------------------------


def _bcrypt_input(password: str) -> bytes:
    """bcrypt only reads the first 72 bytes, and bcrypt>=5 rejects longer input.

    Passwords may be up to 128 characters (more bytes in UTF-8), so every password
    is first reduced to a fixed 44-byte SHA-256 digest, base64-encoded so it has no
    NUL bytes. Every character of the password therefore counts.
    """
    digest = hashlib.sha256(password.encode("utf-8")).digest()
    return base64.b64encode(digest)


def hash_password(password: str) -> str:
    """Hash a password with bcrypt using `settings.BCRYPT_ROUNDS` (at least 12)."""
    salt = bcrypt.gensalt(rounds=settings.BCRYPT_ROUNDS)
    return bcrypt.hashpw(_bcrypt_input(password), salt).decode("ascii")


def _is_bcrypt_hash(hashed_password: str) -> bool:
    return hashed_password.startswith(("$2a$", "$2b$", "$2y$"))


def _verify_legacy_pbkdf2(password: str, hashed_password: str) -> bool:
    """Accounts created before bcrypt used PBKDF2-SHA256 stored as `salt_hex:hash_hex`."""
    salt_hex, derived_hex = hashed_password.split(":", 1)
    expected = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        _LEGACY_PBKDF2_ITERATIONS,
    )
    return hmac.compare_digest(expected.hex(), derived_hex)


def verify_password(password: str, hashed_password: str) -> bool:
    try:
        if _is_bcrypt_hash(hashed_password):
            return bcrypt.checkpw(_bcrypt_input(password), hashed_password.encode("ascii"))
        return _verify_legacy_pbkdf2(password, hashed_password)
    except (ValueError, TypeError):
        return False


def bcrypt_cost(hashed_password: str) -> int | None:
    """Return the work factor of a bcrypt hash, or None for any other format."""
    if not _is_bcrypt_hash(hashed_password):
        return None
    try:
        return int(hashed_password.split("$")[2])
    except (IndexError, ValueError):
        return None


def password_needs_rehash(hashed_password: str) -> bool:
    """True for legacy PBKDF2 hashes and bcrypt hashes below the configured cost."""
    cost = bcrypt_cost(hashed_password)
    return cost is None or cost < settings.BCRYPT_ROUNDS


# --- JWTs ------------------------------------------------------------------------


def _encode(
    payload: dict[str, Any],
    *,
    secret_key: str | None = None,
    algorithm: str | None = None,
) -> str:
    return jwt.encode(
        payload,
        secret_key if secret_key is not None else settings.SECRET_KEY,
        algorithm=algorithm if algorithm is not None else settings.ALGORITHM,
    )


def _base_claims(subject: uuid.UUID | str, token_type: str, lifetime: timedelta) -> dict[str, Any]:
    now = datetime.now(timezone.utc)
    return {
        "sub": str(subject),
        "typ": token_type,
        # Unique per token, so two tokens minted in the same second never collide
        # in the logout blacklist.
        "jti": uuid.uuid4().hex,
        "iat": int(now.timestamp()),
        "exp": int((now + lifetime).timestamp()),
        "iss": settings.JWT_ISSUER,
        "aud": settings.JWT_AUDIENCE,
    }


def create_access_token(
    subject: uuid.UUID | str,
    *,
    role: UserRole | str = UserRole.USER,
    expires_delta: timedelta | None = None,
    issuer: str | None = None,
    audience: str | None = None,
    secret_key: str | None = None,
    algorithm: str | None = None,
) -> str:
    """Create a signed JWT access token bound to the caller's UUID (`sub`) and role (`role`).

    Raises ValueError if `role` is not a valid `UserRole`.
    """
    role_claim = UserRole(role).value
    delta = (
        expires_delta
        if expires_delta is not None
        else timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    payload = _base_claims(subject, ACCESS_TOKEN_TYPE, delta)
    payload["role"] = role_claim
    if issuer is not None:
        payload["iss"] = issuer
    if audience is not None:
        payload["aud"] = audience
    return _encode(payload, secret_key=secret_key, algorithm=algorithm)


def create_refresh_token(subject: uuid.UUID | str, *, expires_delta: timedelta | None = None) -> str:
    """Refresh tokens carry no role: `/auth/refresh` re-reads the role from the database."""
    delta = (
        expires_delta
        if expires_delta is not None
        else timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS)
    )
    return _encode(_base_claims(subject, REFRESH_TOKEN_TYPE, delta))


def create_email_verification_token(
    subject: uuid.UUID | str,
    email: str,
    *,
    expires_delta: timedelta | None = None,
) -> str:
    """Signed, 24-hour link token. Bound to the email so it dies if the email changes."""
    delta = (
        expires_delta
        if expires_delta is not None
        else timedelta(hours=settings.EMAIL_VERIFICATION_EXPIRE_HOURS)
    )
    payload = _base_claims(subject, EMAIL_VERIFICATION_TOKEN_TYPE, delta)
    payload["email"] = email
    return _encode(payload)


def _decode(token: str, required: list[str]) -> dict[str, Any]:
    if not token or not isinstance(token, str):
        raise TokenValidationError("Missing authentication token.")
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
            issuer=settings.JWT_ISSUER,
            audience=settings.JWT_AUDIENCE,
            options={"require": required},
        )
    except PyJWTError as exc:
        raise TokenValidationError("Invalid or expired authentication token.") from exc

    raw_sub = payload.get("sub")
    if not raw_sub or not isinstance(raw_sub, str):
        raise TokenValidationError("Token subject claim is missing.")
    try:
        payload["user_id"] = uuid.UUID(raw_sub)
    except (ValueError, TypeError) as exc:
        raise TokenValidationError("Token subject claim is not a valid UUID.") from exc
    return payload


def decode_access_token(token: str) -> dict[str, Any]:
    """Validate JWT signature, expiration, issuer, audience, UUID subject, and role claim.

    On success `payload["user_id"]` is a UUID and `payload["role"]` is a `UserRole`.
    Refresh and email-verification tokens are rejected.
    """
    payload = _decode(token, ["sub", "role", "exp", "iat", "iss", "aud"])
    if payload.get("typ", ACCESS_TOKEN_TYPE) != ACCESS_TOKEN_TYPE:
        raise TokenValidationError("Token is not an access token.")

    raw_role = payload.get("role")
    try:
        payload["role"] = UserRole(raw_role)
    except ValueError as exc:
        raise TokenValidationError("Token role claim is invalid.") from exc

    return payload


def decode_refresh_token(token: str) -> dict[str, Any]:
    payload = _decode(token, ["sub", "typ", "jti", "exp", "iat", "iss", "aud"])
    if payload.get("typ") != REFRESH_TOKEN_TYPE:
        raise TokenValidationError("Token is not a refresh token.")
    return payload


def decode_email_verification_token(token: str) -> dict[str, Any]:
    payload = _decode(token, ["sub", "typ", "email", "exp", "iat", "iss", "aud"])
    if payload.get("typ") != EMAIL_VERIFICATION_TOKEN_TYPE:
        raise TokenValidationError("Token is not an email verification token.")
    return payload
