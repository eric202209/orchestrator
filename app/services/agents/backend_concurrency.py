"""Sync Redis slot governor for backend concurrency control.

Phase 22B-1X1: a slot member alone ("the session id") carries no proof that
its owner is still alive, so a force-terminated worker leaves the member in
the set until the whole-key TTL expires -- and that TTL is refreshed by every
later acquisition, so a stale member can block capacity indefinitely.

Each acquisition therefore also records *owner evidence* in a companion hash
keyed by the same member. The evidence is what
``backend_slot_reconciliation`` resolves against the execution graph, and its
``owner_token`` fences reconciliation removals so one worker can never drop
another live worker's freshly acquired slot.
"""

from __future__ import annotations

import json
import uuid
from typing import Any, Dict, Optional
from urllib.parse import urlparse

from app.config import settings

OWNER_EVIDENCE_SCHEMA_VERSION = 1


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


def _owner_key(backend_id: str) -> str:
    return f"orchestrator:backend_slot_owners:{backend_id}"


def _decode(value: Any) -> Optional[str]:
    if value is None:
        return None
    if isinstance(value, bytes):
        return value.decode(errors="replace")
    return str(value)


_ACQUIRE_SLOT_SCRIPT = """
local key = KEYS[1]
local owner_key = KEYS[2]
local session_id = ARGV[1]
local max_slots = tonumber(ARGV[2])
local lease_seconds = tonumber(ARGV[3])
local owner_evidence = ARGV[4]

if redis.call('SISMEMBER', key, session_id) == 0 and redis.call('SCARD', key) >= max_slots then
    return 0
end

redis.call('SADD', key, session_id)
redis.call('EXPIRE', key, lease_seconds)
if owner_evidence ~= '' then
    redis.call('HSET', owner_key, session_id, owner_evidence)
    redis.call('EXPIRE', owner_key, lease_seconds)
end
return 1
"""


# Removes a member only while the recorded owner evidence still matches what
# the caller observed. ``expected`` of '' means "the caller observed a member
# with no evidence at all"; any evidence written since then proves a newer
# owner and blocks the removal.
_FENCED_RELEASE_SCRIPT = """
local key = KEYS[1]
local owner_key = KEYS[2]
local session_id = ARGV[1]
local expected = ARGV[2]
local current = redis.call('HGET', owner_key, session_id)

if expected == '' then
    if current then
        return 0
    end
elseif current ~= expected then
    return 0
end

redis.call('SREM', key, session_id)
redis.call('HDEL', owner_key, session_id)
return 1
"""


def build_owner_evidence(
    *,
    session_id: int,
    task_execution_id: Optional[int] = None,
    worker_hostname: Optional[str] = None,
    worker_pid: Optional[int] = None,
    worker_process_start_identity: Optional[str] = None,
    acquired_at: Optional[str] = None,
    owner_token: Optional[str] = None,
) -> Dict[str, Any]:
    """Build the owner-evidence record stored alongside a slot member."""
    from datetime import UTC, datetime

    return {
        "schema_version": OWNER_EVIDENCE_SCHEMA_VERSION,
        "session_id": int(session_id),
        "task_execution_id": (
            int(task_execution_id) if task_execution_id is not None else None
        ),
        "worker_hostname": worker_hostname,
        "worker_pid": int(worker_pid) if worker_pid is not None else None,
        "worker_process_start_identity": worker_process_start_identity,
        "acquired_at": acquired_at or datetime.now(UTC).isoformat(),
        "owner_token": owner_token or uuid.uuid4().hex,
    }


def acquire_backend_slot(
    redis_client: Any,
    backend_id: str,
    session_id: int,
    max_slots: int,
    timeout_s: int = 3900,
    owner_evidence: Optional[Dict[str, Any]] = None,
) -> bool:
    """Atomically claim a backend slot for session_id. Returns False when at capacity.

    ``owner_evidence`` (see :func:`build_owner_evidence`) is written in the
    same atomic step so a slot never exists without the facts reconciliation
    needs. Redis operational errors propagate to the caller so the worker can
    apply its availability policy. Contention is represented only by a False
    return.
    """
    key = _slot_key(backend_id)
    evidence_payload = (
        json.dumps(owner_evidence, sort_keys=True) if owner_evidence else ""
    )
    return bool(
        redis_client.eval(
            _ACQUIRE_SLOT_SCRIPT,
            2,
            key,
            _owner_key(backend_id),
            str(session_id),
            max_slots,
            timeout_s,
            evidence_payload,
        )
    )


def release_backend_slot(redis_client: Any, backend_id: str, session_id: int) -> bool:
    """Release the slot held by session_id for backend, idempotently."""
    try:
        redis_client.srem(_slot_key(backend_id), str(session_id))
    except Exception:
        return False
    try:
        redis_client.hdel(_owner_key(backend_id), str(session_id))
    except Exception:
        # The member is already gone, which is what "released" means. Orphan
        # evidence is reclaimed by reconciliation and by the key TTL.
        pass
    return True


def release_backend_slot_if_owner(
    redis_client: Any,
    backend_id: str,
    session_id: int,
    expected_owner_evidence: Optional[str],
) -> bool:
    """Release a slot only while its owner evidence still matches.

    ``expected_owner_evidence`` is the raw stored string observed by the
    caller, or None when the caller observed a member with no evidence.
    Returns False when a newer owner has claimed the member since -- the
    fence that keeps reconciliation from releasing a live worker's slot.
    """
    return bool(
        redis_client.eval(
            _FENCED_RELEASE_SCRIPT,
            2,
            _slot_key(backend_id),
            _owner_key(backend_id),
            str(session_id),
            expected_owner_evidence if expected_owner_evidence is not None else "",
        )
    )


def backend_slot_owned_by(redis_client: Any, backend_id: str, session_id: int) -> bool:
    """Return whether Redis still records session_id as the slot owner."""
    members = redis_client.smembers(_slot_key(backend_id))
    owner = str(session_id)
    return owner in members or owner.encode() in members


def get_slot_owner_evidence(redis_client: Any, backend_id: str) -> Dict[str, str]:
    """Return the raw owner-evidence strings by slot member for backend."""
    try:
        recorded = redis_client.hgetall(_owner_key(backend_id)) or {}
    except Exception:
        return {}
    evidence: Dict[str, str] = {}
    for member, payload in recorded.items():
        decoded_member = _decode(member)
        decoded_payload = _decode(payload)
        if decoded_member is not None and decoded_payload is not None:
            evidence[decoded_member] = decoded_payload
    return evidence


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
