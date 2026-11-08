"""Shared async Redis client (token blacklist, login lockout, captcha replay protection).

Security checks that depend on Redis fail closed: if Redis is unreachable the caller
gets 503 instead of skipping the check (see `RedisUnavailableError`).
"""
from __future__ import annotations

import redis.asyncio as aioredis
from redis.exceptions import RedisError

from backend.app.core.config import settings

_client: aioredis.Redis | None = None


class RedisUnavailableError(RuntimeError):
    """Raised when a security check cannot reach Redis."""


def get_redis() -> aioredis.Redis:
    global _client
    if _client is None:
        _client = aioredis.from_url(
            settings.REDIS_URL,
            decode_responses=True,
            socket_connect_timeout=2,
            socket_timeout=2,
        )
    return _client


def set_redis_client(client: aioredis.Redis | None) -> None:
    """Swap the client (tests use fakeredis)."""
    global _client
    _client = client


__all__ = ["RedisError", "RedisUnavailableError", "get_redis", "set_redis_client"]
