from datetime import datetime, timedelta, timezone
import hashlib
import hmac
import os
from typing import Any
import uuid

import jwt
from jwt import PyJWTError

from backend.app.core.config import settings
from backend.app.core.roles import UserRole


class TokenValidationError(ValueError):
    """Raised when a JWT access token is missing required claims, expired, or invalid."""


def hash_password(password: str) -> str:
    salt = os.urandom(16)
    derived = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, 120_000)
    return f"{salt.hex()}:{derived.hex()}"


def verify_password(password: str, hashed_password: str) -> bool:
    try:
        salt_hex, derived_hex = hashed_password.split(":", 1)
        expected = hashlib.pbkdf2_hmac(
            "sha256",
            password.encode("utf-8"),
            bytes.fromhex(salt_hex),
            120_000,
        )
        return hmac.compare_digest(expected.hex(), derived_hex)
    except (ValueError, TypeError):
        return False


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
    }
    return jwt.encode(
        payload,
        secret_key if secret_key is not None else settings.SECRET_KEY,
        algorithm=algorithm if algorithm is not None else settings.ALGORITHM,
    )


def decode_access_token(token: str) -> dict[str, Any]:
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

    return payload
