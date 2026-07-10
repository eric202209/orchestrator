"""Sync Redis slot governor for backend concurrency control."""

from __future__ import annotations

from typing import Any
from urllib.parse import urlparse

from app.config import settings


def make_redis_client() -> Any:
    """Build a sync Redis client from CELERY_BROKER_URL, matching ops.py health pattern."""
    import redis

    url = urlparse(settings.CELERY_BROKER_URL)
    return redis.Redis(
        host=url.hostname or "localhost",
        port=url.port or 6379,
        db=int((url.path or "/0").lstrip("/") or "0"),
        password=url.password,
        socket_connect_timeout=2,
        socket_timeout=2,
    )


def _slot_key(backend_id: str) -> str:
    return f"orchestrator:backend_slots:{backend_id}"


_ACQUIRE_SLOT_SCRIPT = """
local key = KEYS[1]
local session_id = ARGV[1]
local max_slots = tonumber(ARGV[2])
local lease_seconds = tonumber(ARGV[3])

if redis.call('SISMEMBER', key, session_id) == 0 and redis.call('SCARD', key) >= max_slots then
    return 0
end

redis.call('SADD', key, session_id)
redis.call('EXPIRE', key, lease_seconds)
return 1
"""


def acquire_backend_slot(
    redis_client: Any,
    backend_id: str,
    session_id: int,
    max_slots: int,
    timeout_s: int = 3900,
) -> bool:
    """Atomically claim a backend slot for session_id. Returns False when at capacity.

    Redis operational errors propagate to the caller so the worker can apply its
    availability policy. Contention is represented only by a False return.
    """
    key = _slot_key(backend_id)
    return bool(
        redis_client.eval(
            _ACQUIRE_SLOT_SCRIPT,
            1,
            key,
            str(session_id),
            max_slots,
            timeout_s,
        )
    )


def release_backend_slot(redis_client: Any, backend_id: str, session_id: int) -> None:
    """Release the slot held by session_id for backend."""
    try:
        redis_client.srem(_slot_key(backend_id), str(session_id))
    except Exception:
        pass


def get_concurrency_snapshot(redis_client: Any, backend_id: str) -> dict:
    """Return active slot count and active session IDs for backend."""
    key = _slot_key(backend_id)
    try:
        members = redis_client.smembers(key) or set()
    except Exception:
        members = set()
    active_session_ids = sorted(int(m) for m in members)
    return {
        "backend_id": backend_id,
        "active_count": len(active_session_ids),
        "active_session_ids": active_session_ids,
    }
