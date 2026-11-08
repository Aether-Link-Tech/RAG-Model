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


class TokenValidationError(ValueError):
    """Raised when a JWT access token is missing required claims, expired, or invalid."""


ACCESS_TOKEN_TYPE = "access"
REFRESH_TOKEN_TYPE = "refresh"


LEGACY_PBKDF2_ITERATIONS = 120_000
# bcrypt only uses the first 72 bytes of a password; pre-hash longer inputs so
# no part of a long passphrase is silently ignored.
_BCRYPT_MAX_BYTES = 72


def _bcrypt_input(password: str) -> bytes:
    raw = password.encode("utf-8")
    if len(raw) > _BCRYPT_MAX_BYTES:
        raw = base64.b64encode(hashlib.sha256(raw).digest())
    return raw


def hash_password(password: str) -> str:
    """Hash a password with bcrypt (cost = BCRYPT_ROUNDS, at least 12 outside tests)."""
    salt = bcrypt.gensalt(rounds=settings.BCRYPT_ROUNDS)
    return bcrypt.hashpw(_bcrypt_input(password), salt).decode("ascii")


def _verify_legacy_pbkdf2(password: str, hashed_password: str) -> bool:
    salt_hex, derived_hex = hashed_password.split(":", 1)
    expected = hashlib.pbkdf2_hmac(
        "sha256",
        password.encode("utf-8"),
        bytes.fromhex(salt_hex),
        LEGACY_PBKDF2_ITERATIONS,
    )
    return hmac.compare_digest(expected.hex(), derived_hex)


def verify_password(password: str, hashed_password: str) -> bool:
    """Verify bcrypt hashes, and legacy PBKDF2 hashes created before FR-007."""
    try:
        if hashed_password.startswith("$2"):
            return bcrypt.checkpw(_bcrypt_input(password), hashed_password.encode("ascii"))
        return _verify_legacy_pbkdf2(password, hashed_password)
    except (ValueError, TypeError):
        return False


def password_needs_rehash(hashed_password: str) -> bool:
    """True for legacy PBKDF2 hashes or bcrypt hashes below the configured cost."""
    if not hashed_password.startswith("$2"):
        return True
    try:
        return int(hashed_password.split("$")[2]) < settings.BCRYPT_ROUNDS
    except (IndexError, ValueError):
        return True


def create_access_token(
    subject: uuid.UUID | str,
    *,
    role: UserRole | str = UserRole.USER,
    expires_delta: timedelta | None = None,
    issuer: str | None = None,
    audience: str | None = None,
    secret_key: str | None = None,
    algorithm: str | None = None,
    token_type: str = ACCESS_TOKEN_TYPE,
) -> str:
    """Create a signed JWT access token bound to the caller's UUID (`sub`) and role (`role`).

    Every token carries a unique `jti`, so logout can revoke it (FR-003).

    Raises ValueError if `role` is not a valid `UserRole`.
    """
    role_claim = UserRole(role).value
    now = datetime.now(timezone.utc)
    delta = (
        expires_delta
        if expires_delta is not None
        else timedelta(minutes=settings.ACCESS_TOKEN_EXPIRE_MINUTES)
    )
    expire = now + delta
    payload: dict[str, Any] = {
        "sub": str(subject),
        "role": role_claim,
        "iat": int(now.timestamp()),
        "exp": int(expire.timestamp()),
        "iss": issuer if issuer is not None else settings.JWT_ISSUER,
        "aud": audience if audience is not None else settings.JWT_AUDIENCE,
        "jti": uuid.uuid4().hex,
        "typ": token_type,
    }
    return jwt.encode(
        payload,
        secret_key if secret_key is not None else settings.SECRET_KEY,
        algorithm=algorithm if algorithm is not None else settings.ALGORITHM,
    )


def create_refresh_token(subject: uuid.UUID | str, *, role: UserRole | str) -> str:
    """Long-lived token used only at POST /auth/refresh to obtain a new token pair (FR-002)."""
    return create_access_token(
        subject,
        role=role,
        expires_delta=timedelta(days=settings.REFRESH_TOKEN_EXPIRE_DAYS),
        token_type=REFRESH_TOKEN_TYPE,
    )


def decode_refresh_token(token: str) -> dict[str, Any]:
    return decode_access_token(token, expected_type=REFRESH_TOKEN_TYPE)


def decode_access_token(token: str, *, expected_type: str = ACCESS_TOKEN_TYPE) -> dict[str, Any]:
    """Validate JWT signature, expiration, issuer, audience, UUID subject, and role claim.

    On success `payload["user_id"]` is a UUID and `payload["role"]` is a `UserRole`.
    """
    if not token or not isinstance(token, str):
        raise TokenValidationError("Missing authentication token.")
    try:
        payload = jwt.decode(
            token,
            settings.SECRET_KEY,
            algorithms=[settings.ALGORITHM],
            issuer=settings.JWT_ISSUER,
            audience=settings.JWT_AUDIENCE,
            options={"require": ["sub", "role", "exp", "iat", "iss", "aud"]},
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

    raw_role = payload.get("role")
    try:
        payload["role"] = UserRole(raw_role)
    except ValueError as exc:
        raise TokenValidationError("Token role claim is invalid.") from exc

    # Tokens minted before FR-003 have no `typ`/`jti`; treat them as access tokens.
    if payload.get("typ", ACCESS_TOKEN_TYPE) != expected_type:
        raise TokenValidationError("Wrong token type.")

    return payload
