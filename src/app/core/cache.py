"""Redis-backed read cache.

Nothing in this module is load-bearing for correctness. Postgres stays the sole
source of truth for seat state (docs/decisions/0001-conditional-update-over-locks.md);
Redis only spares the database repeated seat-map reads. Every entry carries a
short TTL so that a missed invalidation self-heals in seconds instead of
serving a stale seat map indefinitely — the TTL is the safety net, the explicit
invalidation is the optimisation on top of it.
"""

import json
import uuid
from typing import Any

from redis.asyncio import Redis

from app.core.config import get_settings

_client: Redis | None = None


def get_redis() -> Redis:
    """FastAPI dependency returning a shared Redis client.

    redis-py's async client is connection-pooled and safe to share across
    concurrent requests, so one per process is correct.
    """
    global _client
    if _client is None:
        _client = Redis.from_url(get_settings().redis_url, decode_responses=True)
    return _client


class SeatMapCache:
    """Caches the rendered seat map for a show."""

    def __init__(self, redis: Redis, ttl_seconds: int) -> None:
        self._redis = redis
        self._ttl_seconds = ttl_seconds

    @staticmethod
    def _key(show_id: uuid.UUID) -> str:
        return f"seatmap:{show_id}"

    async def get(self, show_id: uuid.UUID) -> dict[str, Any] | None:
        cached = await self._redis.get(self._key(show_id))
        if cached is None:
            return None
        decoded: dict[str, Any] = json.loads(cached)
        return decoded

    async def set(self, show_id: uuid.UUID, payload: dict[str, Any]) -> None:
        await self._redis.set(self._key(show_id), json.dumps(payload), ex=self._ttl_seconds)

    async def invalidate(self, show_id: uuid.UUID) -> None:
        await self._redis.delete(self._key(show_id))
