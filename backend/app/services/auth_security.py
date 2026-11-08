"""Redis-backed authentication safeguards.

- Token blacklist (FR-003): revoked token ids are kept until the token would expire anyway.
- Login throttling (FR-002): failed attempts per account; captcha after N failures,
  15-minute lockout after LOGIN_MAX_FAILURES. A successful login resets the counter.
"""
from __future__ import annotations

import time

from backend.app.core.config import settings
from backend.app.core.redis import RedisError, RedisUnavailableError, get_redis

_BLACKLIST_PREFIX = "auth:revoked:"
_FAILURES_PREFIX = "auth:login-failures:"
_LOCK_PREFIX = "auth:login-lock:"


async def revoke_token(jti: str | None, exp: int | None) -> None:
    """Blacklist a token id with TTL = remaining lifetime of the token."""
    if not jti:
        return
    ttl = max(1, int((exp or 0) - time.time()))
    try:
        await get_redis().set(_BLACKLIST_PREFIX + jti, "1", ex=ttl)
    except RedisError as exc:
        raise RedisUnavailableError("Token revocation store unavailable") from exc


async def is_token_revoked(jti: str | None) -> bool:
    if not jti:
        return False
    try:
        return bool(await get_redis().exists(_BLACKLIST_PREFIX + jti))
    except RedisError as exc:
        raise RedisUnavailableError("Token revocation store unavailable") from exc


async def lockout_remaining_seconds(account_key: str) -> int:
    """Seconds left on an active lockout, or 0."""
    try:
        ttl = await get_redis().ttl(_LOCK_PREFIX + account_key)
    except RedisError as exc:
        raise RedisUnavailableError("Login protection store unavailable") from exc
    return max(0, int(ttl or 0))


async def failed_attempts(account_key: str) -> int:
    try:
        value = await get_redis().get(_FAILURES_PREFIX + account_key)
    except RedisError as exc:
        raise RedisUnavailableError("Login protection store unavailable") from exc
    return int(value or 0)


async def record_failed_login(account_key: str) -> int:
    """Increment the failure counter; lock the account when the limit is reached.

    Returns the number of failures in the current window.
    """
    window = settings.LOGIN_LOCKOUT_MINUTES * 60
    try:
        redis = get_redis()
        count = int(await redis.incr(_FAILURES_PREFIX + account_key))
        if count == 1:
            await redis.expire(_FAILURES_PREFIX + account_key, window)
        if count >= settings.LOGIN_MAX_FAILURES:
            await redis.set(_LOCK_PREFIX + account_key, "1", ex=window)
            await redis.delete(_FAILURES_PREFIX + account_key)
        return count
    except RedisError as exc:
        raise RedisUnavailableError("Login protection store unavailable") from exc


async def reset_failed_logins(account_key: str) -> None:
    try:
        await get_redis().delete(_FAILURES_PREFIX + account_key, _LOCK_PREFIX + account_key)
    except RedisError as exc:
        raise RedisUnavailableError("Login protection store unavailable") from exc
