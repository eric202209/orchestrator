"""Best-effort in-memory streaming health registry for websocket diagnostics."""

from __future__ import annotations

from collections import deque
from dataclasses import dataclass
from datetime import UTC, datetime, timedelta
from threading import Lock
from typing import Any, Dict

_STREAM_KINDS = (
    "session_logs",
    "session_status",
    "project_logs",
    "mobile_session_logs",
)
_RECENT_ERROR_WINDOW = timedelta(minutes=15)
_MAX_RECENT_ERRORS = 25


@dataclass
class _StreamCounters:
    active_connections: int = 0
    total_connections: int = 0
    disconnects: int = 0
    errors: int = 0


_lock = Lock()
_counters: dict[str, _StreamCounters] = {
    kind: _StreamCounters() for kind in _STREAM_KINDS
}
_recent_errors: dict[str, deque[dict[str, Any]]] = {
    kind: deque(maxlen=_MAX_RECENT_ERRORS) for kind in _STREAM_KINDS
}


def _normalize_kind(stream_kind: str) -> str:
    normalized = (stream_kind or "").strip()
    if normalized not in _counters:
        raise ValueError(f"Unknown stream kind: {stream_kind}")
    return normalized


def register_stream_connection(stream_kind: str) -> None:
    kind = _normalize_kind(stream_kind)
    with _lock:
        counters = _counters[kind]
        counters.active_connections += 1
        counters.total_connections += 1


def unregister_stream_connection(stream_kind: str) -> None:
    kind = _normalize_kind(stream_kind)
    with _lock:
        counters = _counters[kind]
        counters.active_connections = max(0, counters.active_connections - 1)
        counters.disconnects += 1


def record_stream_error(stream_kind: str, error: Any) -> None:
    kind = _normalize_kind(stream_kind)
    with _lock:
        counters = _counters[kind]
        counters.errors += 1
        _recent_errors[kind].append(
            {
                "timestamp": datetime.now(UTC).isoformat(),
                "message": str(error)[:500],
            }
        )


def clear_streaming_health() -> None:
    """Reset counters for tests."""

    with _lock:
        for kind in _STREAM_KINDS:
            _counters[kind] = _StreamCounters()
            _recent_errors[kind].clear()


def get_streaming_health_snapshot() -> Dict[str, Any]:
    cutoff = datetime.now(UTC) - _RECENT_ERROR_WINDOW
    with _lock:
        streams: dict[str, Any] = {}
        total_active = 0
        total_errors = 0
        recent_error_total = 0

        for kind in _STREAM_KINDS:
            counters = _counters[kind]
            recent_errors = [
                item
                for item in _recent_errors[kind]
                if datetime.fromisoformat(item["timestamp"]) >= cutoff
            ]
            total_active += counters.active_connections
            total_errors += counters.errors
            recent_error_total += len(recent_errors)
            streams[kind] = {
                "active_connections": counters.active_connections,
                "total_connections": counters.total_connections,
                "disconnects": counters.disconnects,
                "errors": counters.errors,
                "recent_errors": recent_errors,
            }

    overall_status = "healthy"
    if recent_error_total > 0:
        overall_status = "warning"

    return {
        "status": overall_status,
        "active_connections": total_active,
        "error_count": total_errors,
        "recent_error_count": recent_error_total,
        "streams": streams,
    }
