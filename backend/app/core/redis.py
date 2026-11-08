"""Shared async Redis client (login lockout, token blacklist)."""
from __future__ import annotations

import redis.asyncio as aioredis

from backend.app.core.config import settings

_client: aioredis.Redis | None = None


def get_redis() -> aioredis.Redis:
    """Return the process-wide Redis client, created on first use."""
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
