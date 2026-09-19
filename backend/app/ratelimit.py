"""Fixed-window rate limiter backed by Redis (or memory for tests / single-process dev)."""

from __future__ import annotations

import threading
import time
from collections import defaultdict
from functools import lru_cache
from typing import Protocol

from fastapi import HTTPException

from .config import get_settings, parse_rate


class Backend(Protocol):
    def incr(self, key: str, window: int) -> tuple[int, int]:
        """Increment ``key``; return (count, seconds_until_reset)."""


class MemoryBackend:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._data: dict[str, tuple[int, float]] = defaultdict(lambda: (0, 0.0))

    def incr(self, key: str, window: int) -> tuple[int, int]:
        now = time.monotonic()
        with self._lock:
            count, reset_at = self._data[key]
            if now >= reset_at:
                count, reset_at = 0, now + window
            count += 1
            self._data[key] = (count, reset_at)
            return count, max(1, int(reset_at - now))

    def clear(self) -> None:
        with self._lock:
            self._data.clear()


_LUA = """
local c = redis.call('INCR', KEYS[1])
if c == 1 then redis.call('EXPIRE', KEYS[1], ARGV[1]) end
local ttl = redis.call('TTL', KEYS[1])
if ttl < 0 then redis.call('EXPIRE', KEYS[1], ARGV[1]); ttl = tonumber(ARGV[1]) end
return {c, ttl}
"""


class RedisBackend:
    def __init__(self, url: str) -> None:
        import redis

        self._client = redis.Redis.from_url(url, socket_timeout=2, socket_connect_timeout=2)
        self._script = self._client.register_script(_LUA)

    def incr(self, key: str, window: int) -> tuple[int, int]:
        count, ttl = self._script(keys=[f"rl:{key}"], args=[window])
        return int(count), max(1, int(ttl))


@lru_cache
def get_backend() -> Backend:
    url = get_settings().effective_ratelimit_url
    if url.startswith("memory://"):
        return MemoryBackend()
    return RedisBackend(url)


def check(scope: str, identifier: str, spec: str, backend: Backend | None = None) -> None:
    """Raise 429 once ``identifier`` exceeds ``spec`` ("count/seconds") within ``scope``.

    Fails *closed*: if the limiter backend is unreachable the request is refused with 503 rather than
    silently running unthrottled (login brute-forcing must not become possible during a Redis outage).
    """
    limit, window = parse_rate(spec)
    backend = backend or get_backend()
    try:
        count, retry_after = backend.incr(f"{scope}:{identifier}", window)
    except Exception:
        raise HTTPException(status_code=503, detail={"code": "rate_limiter_unavailable", "message": "Service temporarily unavailable. Try again shortly."}) from None
    if count > limit:
        raise HTTPException(
            status_code=429,
            detail={"code": "rate_limited", "message": "Too many requests. Please slow down."},
            headers={"Retry-After": str(retry_after)},
        )
