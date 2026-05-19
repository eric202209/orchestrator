"""Session-state transitions for orchestration flows."""

from __future__ import annotations

from datetime import datetime
from typing import Any

from app.services.orchestration.state.persistence import set_session_alert


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


def clear_session_alert(session: Any | None) -> None:
    set_session_alert(session, None, None)
