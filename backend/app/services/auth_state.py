"""Redis-backed auth state: token blacklist (FR-003) and failed-login counter (FR-002).

Both fail closed. If Redis cannot be reached, callers get `AuthStateUnavailableError`
and must refuse the request (503) instead of skipping the check.
"""
from __future__ import annotations

import hashlib

from backend.app.core.crypto import email_index
import time

from redis.exceptions import RedisError

from backend.app.core.config import settings
from backend.app.core.redis import get_redis

BLACKLIST_PREFIX = "auth:blacklist:"
LOGIN_FAIL_PREFIX = "login_fail:"
RESEND_COOLDOWN_PREFIX = "auth:resend:"


class AuthStateUnavailableError(RuntimeError):
    """Redis is unreachable, so auth state cannot be checked."""


def _token_key(token: str) -> str:
    # Store a digest, never the token itself.
    return BLACKLIST_PREFIX + hashlib.sha256(token.encode("utf-8")).hexdigest()


def _login_key(email: str) -> str:
    # Keyed HMAC of the email, so Redis never holds the plaintext address (FR-007).
    return LOGIN_FAIL_PREFIX + email_index(email)


# --- Token blacklist (FR-003 / AL-20) ---------------------------------------------


async def blacklist_token(token: str, expires_at: int) -> None:
    """Blacklist `token` until its own expiry (`exp`, epoch seconds)."""
    ttl = int(expires_at - time.time())
    if ttl <= 0:
        return  # already expired, nothing to revoke
    try:
        await get_redis().set(_token_key(token), "1", ex=ttl)
    except RedisError as exc:
        raise AuthStateUnavailableError("Token blacklist is unavailable.") from exc


async def is_token_blacklisted(token: str) -> bool:
    try:
        return bool(await get_redis().exists(_token_key(token)))
    except RedisError as exc:
        raise AuthStateUnavailableError("Token blacklist is unavailable.") from exc


# --- Failed-login counter (FR-002 / AL-121) ----------------------------------------


async def failed_login_count(email: str) -> int:
    try:
        value = await get_redis().get(_login_key(email))
    except RedisError as exc:
        raise AuthStateUnavailableError("Login attempt counter is unavailable.") from exc
    return int(value or 0)


async def record_failed_login(email: str) -> int:
    """Increment the counter and (re)start its lockout window. Returns the new count."""
    key = _login_key(email)
    try:
        pipe = get_redis().pipeline()
        pipe.incr(key)
        pipe.expire(key, settings.LOGIN_LOCKOUT_MINUTES * 60)
        count, _ = await pipe.execute()
    except RedisError as exc:
        raise AuthStateUnavailableError("Login attempt counter is unavailable.") from exc
    return int(count)


async def lockout_seconds_remaining(email: str) -> int:
    try:
        ttl = await get_redis().ttl(_login_key(email))
    except RedisError as exc:
        raise AuthStateUnavailableError("Login attempt counter is unavailable.") from exc
    return max(int(ttl), 0)


async def reset_failed_logins(email: str) -> None:
    try:
        await get_redis().delete(_login_key(email))
    except RedisError as exc:
        raise AuthStateUnavailableError("Login attempt counter is unavailable.") from exc


# --- Resend-verification cooldown (FR-001 / AL-117) --------------------------------


async def claim_resend_slot(email: str, cooldown_seconds: int = 60) -> bool:
    """True if a verification email may be re-sent now (at most one per cooldown)."""
    try:
        return bool(
            await get_redis().set(
                RESEND_COOLDOWN_PREFIX + email_index(email), "1", ex=cooldown_seconds, nx=True
            )
        )
    except RedisError as exc:
        raise AuthStateUnavailableError("Rate limiter is unavailable.") from exc
