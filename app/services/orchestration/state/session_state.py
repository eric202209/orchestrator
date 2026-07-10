"""Session-state transitions for orchestration flows."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any

from app.services.orchestration.state.persistence import set_session_alert


class SessionStatus(str, Enum):
    PENDING = "pending"
    RUNNING = "running"
    PAUSED = "paused"
    AWAITING_INPUT = "awaiting_input"
    STOPPED = "stopped"
    COMPLETED = "completed"
    FAILED = "failed"


_LEGACY_STATUS_ALIASES = {"active": SessionStatus.RUNNING.value}


def normalize_session_status(status: str | SessionStatus | None) -> str:
    value = str(status.value if isinstance(status, SessionStatus) else status or "")
    return _LEGACY_STATUS_ALIASES.get(value.strip().lower(), value.strip().lower())


@dataclass(frozen=True)
class SessionTransition:
    allowed: bool
    result_status: str
    is_active: bool
    timestamp_policy: str | None = None


_SESSION_TRANSITION_POLICY: dict[tuple[str, str], SessionTransition] = {
    ("pending", "start"): SessionTransition(
        allowed=True,
        result_status="running",
        is_active=True,
        timestamp_policy="started_at",
    ),
    ("stopped", "start"): SessionTransition(
        allowed=True,
        result_status="running",
        is_active=True,
        timestamp_policy="started_at",
    ),
    ("running", "pause"): SessionTransition(
        allowed=True,
        result_status="paused",
        is_active=False,
        timestamp_policy="paused_at",
    ),
    ("paused", "resume"): SessionTransition(
        allowed=True,
        result_status="running",
        is_active=True,
        timestamp_policy="resumed_at",
    ),
    ("awaiting_input", "resume"): SessionTransition(
        allowed=True,
        result_status="running",
        is_active=True,
        timestamp_policy="resumed_at",
    ),
    ("running", "await_input"): SessionTransition(
        allowed=True,
        result_status="awaiting_input",
        is_active=True,
    ),
    ("running", "stop"): SessionTransition(
        allowed=True,
        result_status="stopped",
        is_active=False,
        timestamp_policy="stopped_at",
    ),
    ("paused", "stop"): SessionTransition(
        allowed=True,
        result_status="stopped",
        is_active=False,
        timestamp_policy="stopped_at",
    ),
    ("awaiting_input", "stop"): SessionTransition(
        allowed=True,
        result_status="stopped",
        is_active=False,
        timestamp_policy="stopped_at",
    ),
    ("running", "complete"): SessionTransition(
        allowed=True,
        result_status="completed",
        is_active=False,
        timestamp_policy="stopped_at",
    ),
}


def resolve_session_transition(current_status: str, action: str) -> SessionTransition:
    """Resolve session transition policy without mutating a session row."""

    normalized_status = normalize_session_status(current_status)
    normalized_action = str(action or "").strip().lower()
    transition = _SESSION_TRANSITION_POLICY.get((normalized_status, normalized_action))
    if transition:
        return transition
    return SessionTransition(
        allowed=False,
        result_status=normalized_status,
        is_active=normalized_status
        in {SessionStatus.RUNNING.value, SessionStatus.AWAITING_INPUT.value},
    )


def mark_session_running(
    session: Any | None,
    *,
    alert_level: str | None = None,
    alert_message: str | None = None,
    started_at: datetime | None = None,
) -> None:
    if not session:
        return
    session.status = "running"
    session.is_active = True
    if started_at is not None:
        session.started_at = started_at
    set_session_alert(session, alert_level, alert_message)


def mark_session_awaiting_input(session: Any | None) -> None:
    if not session:
        return
    session.status = "awaiting_input"
    session.is_active = True


def mark_session_paused(
    session: Any | None,
    *,
    alert_level: str | None = None,
    alert_message: str | None = None,
    paused_at: datetime | None = None,
    is_active: bool = False,
) -> None:
    if not session:
        return
    session.status = "paused"
    session.is_active = is_active
    if paused_at is not None:
        session.paused_at = paused_at
    set_session_alert(session, alert_level, alert_message)


def mark_session_stopped(
    session: Any | None,
    *,
    stopped_at: datetime | None = None,
) -> None:
    if not session:
        return
    session.status = "stopped"
    session.is_active = False
    if stopped_at is not None:
        session.stopped_at = stopped_at


def mark_session_completed(
    session: Any | None,
    *,
    completed_at: datetime | None = None,
) -> None:
    if not session:
        return
    session.status = "completed"
    session.is_active = False
    if completed_at is not None:
        session.stopped_at = completed_at
    set_session_alert(session, None, None)


def mark_session_failed(
    session: Any | None,
    *,
    failed_at: datetime | None = None,
    alert_level: str | None = "error",
    alert_message: str | None = None,
) -> None:
    if not session:
        return
    session.status = "failed"
    session.is_active = False
    if failed_at is not None:
        session.stopped_at = failed_at
    set_session_alert(session, alert_level, alert_message)


def clear_session_alert(session: Any | None) -> None:
    set_session_alert(session, None, None)


def mark_session_pending(session: Any | None) -> None:
    if not session:
        return
    session.status = "pending"
    session.is_active = False


def mark_session_deleted(
    session: Any | None,
    *,
    deleted_at: datetime | None = None,
) -> None:
    if not session:
        return
    session.status = "deleted"
    session.is_active = False
    session.deleted_at = deleted_at or datetime.now(timezone.utc)


def mark_session_resumed(
    session: Any | None,
    *,
    resumed_at: datetime | None = None,
) -> None:
    if not session:
        return
    session.status = "running"
    session.is_active = True
    session.resumed_at = resumed_at or datetime.now(timezone.utc)
    if getattr(session, "paused_at", None):
        session.paused_at = None
