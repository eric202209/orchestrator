"""Canonical read-side lifecycle authority for Orchestrator Sessions.

This module is intentionally read-only.  E1 establishes durable representation
and a single projection owner; writer transitions remain in their existing
runtime paths until later Phase 36 slices.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Any

from sqlalchemy.exc import SQLAlchemyError
from sqlalchemy.orm import Session as DbSession

from app.models import (
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.orchestration.state.session_state import normalize_session_status

_STATUS_TO_PHASE: dict[str, str | None] = {
    "pending": None,
    "running": "step_executing",
    "recovering": "recovering",
    "retry_pending": "retry_pending",
    "paused": "awaiting_input",
    "awaiting_input": "awaiting_input",
    "stopped": "cancelled",
    "cancelled": "cancelled",
    "canceled": "cancelled",
    "failed": "failed",
    "done": "done",
    "completed": "done",
}

_KNOWN_STATUSES = frozenset(_STATUS_TO_PHASE)
_TERMINAL_STATUSES = frozenset(
    {"stopped", "cancelled", "canceled", "failed", "done", "completed"}
)
_CONTINUATION_STATUSES = frozenset({"recovering", "retry_pending"})

_PHASE_TO_COORDINATOR = {
    "step_executing": "ExecutionCoordinator",
    "awaiting_input": "ExecutionCoordinator",
    "recovering": "FailureCoordinator",
    "retry_pending": "FailureCoordinator",
}

_STATUS_TO_ALLOWED_ACTIONS: dict[str, list[str]] = {
    "pending": ["view_logs", "view_timeline", "start_session"],
    "running": ["view_logs", "view_timeline", "pause_session", "stop_session"],
    "recovering": ["view_logs", "view_timeline"],
    "retry_pending": ["view_logs", "view_timeline"],
    "paused": ["view_logs", "view_timeline", "resume_session", "stop_session"],
    "awaiting_input": [
        "view_logs",
        "view_timeline",
        "submit_guidance",
        "stop_session",
    ],
    "stopped": ["view_logs", "view_timeline", "resume_session", "start_session"],
    "cancelled": [
        "view_logs",
        "view_timeline",
        "resume_session",
        "start_session",
    ],
    "canceled": ["view_logs", "view_timeline", "resume_session", "start_session"],
    "failed": [
        "view_logs",
        "view_timeline",
        "resume_session",
        "retry_task",
        "start_session",
    ],
    "done": ["view_logs", "view_timeline", "start_session"],
    "completed": ["view_logs", "view_timeline", "start_session"],
}


@dataclass(frozen=True)
class _ContinuationMetadata:
    has_marker: bool
    valid: bool
    kind: str | None
    retry_count: int | None
    retry_eta: datetime | None


@dataclass(frozen=True)
class LifecycleAuthority:
    """Typed, read-only lifecycle facts and compatibility projection."""

    current_phase: str | None
    terminal_reason: str | None
    attempt_status: str | None
    attempt_failure_reason: str | None
    continuation_pending: bool
    continuation_kind: str | None
    retry_count: int | None
    retry_eta: datetime | None
    logical_terminal: bool
    quiescent: bool
    last_transition_at: datetime | None
    coordinator: str | None
    allowed_actions: tuple[str, ...]

    @property
    def is_terminal(self) -> bool:
        """Compatibility alias; there is only one terminal predicate."""

        return self.logical_terminal

    def as_projection(self) -> dict[str, Any]:
        return {
            "current_phase": self.current_phase,
            "terminal_reason": self.terminal_reason,
            "attempt_status": self.attempt_status,
            "attempt_failure_reason": self.attempt_failure_reason,
            "continuation_pending": self.continuation_pending,
            "continuation_kind": self.continuation_kind,
            "retry_count": self.retry_count,
            "retry_eta": self.retry_eta,
            "logical_terminal": self.logical_terminal,
            "quiescent": self.quiescent,
            "last_transition_at": self.last_transition_at,
            "coordinator": self.coordinator,
            "allowed_actions": list(self.allowed_actions),
            "is_terminal": self.is_terminal,
        }


def _status(session: SessionModel) -> str:
    return normalize_session_status(getattr(session, "status", None))


def _continuation_metadata(session: SessionModel) -> _ContinuationMetadata:
    raw_task_id = getattr(session, "continuation_task_id", None)
    raw_kind = getattr(session, "continuation_kind", None)
    raw_retry_count = getattr(session, "continuation_retry_count", 0)
    raw_retry_eta = getattr(session, "continuation_retry_eta", None)
    if (
        raw_retry_count is None
        and raw_task_id is None
        and raw_kind is None
        and raw_retry_eta is None
    ):
        # SQLAlchemy applies Python defaults on flush.  Keep an unflushed,
        # otherwise empty Session deterministic as well.
        raw_retry_count = 0

    kind = raw_kind.strip() if isinstance(raw_kind, str) and raw_kind.strip() else None
    retry_count = (
        raw_retry_count
        if isinstance(raw_retry_count, int) and not isinstance(raw_retry_count, bool)
        else None
    )
    task_id_valid = raw_task_id is None or (
        isinstance(raw_task_id, int)
        and not isinstance(raw_task_id, bool)
        and raw_task_id > 0
    )
    retry_count_valid = retry_count is not None and retry_count >= 0
    eta_valid = raw_retry_eta is None or isinstance(raw_retry_eta, datetime)
    has_marker = any(
        (
            raw_task_id is not None,
            raw_kind is not None,
            raw_retry_eta is not None,
            raw_retry_count not in (None, 0),
        )
    )
    valid = (
        task_id_valid
        and retry_count_valid
        and eta_valid
        and (not has_marker or kind is not None)
    )
    return _ContinuationMetadata(
        has_marker=has_marker,
        valid=valid,
        kind=kind,
        retry_count=retry_count,
        retry_eta=raw_retry_eta if eta_valid else None,
    )


def _continuation_state(
    session: SessionModel,
) -> tuple[bool, bool, _ContinuationMetadata]:
    status = _status(session)
    metadata = _continuation_metadata(session)
    continuation_pending = metadata.has_marker and metadata.valid
    malformed_phase = status in _CONTINUATION_STATUSES and not continuation_pending
    malformed_metadata = metadata.has_marker and not metadata.valid
    return continuation_pending, malformed_phase or malformed_metadata, metadata


def derive_continuation_pending(session: SessionModel) -> bool:
    """Return true only for a valid durable continuation marker."""

    return _continuation_state(session)[0]


def derive_logical_terminal(session: SessionModel) -> bool:
    """Return stable terminality without inferring it from unknown status."""

    status = _status(session)
    continuation_pending, malformed, _ = _continuation_state(session)
    return status in _TERMINAL_STATUSES and not continuation_pending and not malformed


def _resolve_latest_execution(
    db: DbSession,
    session: SessionModel,
    *,
    task_id: int | None,
    latest_task_execution: TaskExecution | None,
) -> tuple[TaskExecution | None, bool]:
    session_id = getattr(session, "id", None)
    if (
        latest_task_execution is not None
        and latest_task_execution.session_id == session_id
        and (task_id is None or latest_task_execution.task_id == task_id)
    ):
        return latest_task_execution, False
    if not isinstance(session_id, int):
        return None, True
    try:
        query = db.query(TaskExecution).filter(TaskExecution.session_id == session_id)
        if task_id is not None:
            query = query.filter(TaskExecution.task_id == task_id)
        return query.order_by(TaskExecution.id.desc()).first(), False
    except (SQLAlchemyError, AttributeError, TypeError):
        return None, True


def _active_work(db: DbSession, session: SessionModel) -> tuple[bool, bool]:
    """Return durable active-work residue and whether the read was ambiguous."""

    session_id = getattr(session, "id", None)
    if not isinstance(session_id, int):
        return False, True
    try:
        execution = (
            db.query(TaskExecution.id)
            .filter(
                TaskExecution.session_id == session_id,
                TaskExecution.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]),
            )
            .first()
        )
        if execution is not None:
            return True, False

        session_task = (
            db.query(SessionTask.id)
            .filter(
                SessionTask.session_id == session_id,
                SessionTask.status == TaskStatus.RUNNING,
            )
            .first()
        )
        if session_task is not None:
            return True, False

        linked_task = (
            db.query(Task.id)
            .join(SessionTask, SessionTask.task_id == Task.id)
            .filter(
                SessionTask.session_id == session_id,
                Task.status == TaskStatus.RUNNING,
            )
            .first()
        )
        return linked_task is not None, False
    except (SQLAlchemyError, AttributeError, TypeError):
        return False, True


def derive_quiescent(db: DbSession, session: SessionModel) -> bool:
    """Return logical-work quiescence from one durable DB read context."""

    status = _status(session)
    continuation_pending, malformed, _ = _continuation_state(session)
    if status not in _KNOWN_STATUSES or continuation_pending or malformed:
        return False
    active_work, ambiguous = _active_work(db, session)
    return not active_work and not ambiguous


def _failure_reason(
    db: DbSession,
    session: SessionModel,
    latest: TaskExecution | None,
) -> tuple[str | None, bool]:
    if latest is not None and latest.failure_category:
        return str(latest.failure_category).strip() or None, False
    try:
        query = db.query(TaskExecution).filter(
            TaskExecution.session_id == session.id,
            TaskExecution.failure_category.isnot(None),
        )
        failed_execution = query.order_by(
            TaskExecution.completed_at.desc().nullslast(),
            TaskExecution.id.desc(),
        ).first()
        if failed_execution is None or not failed_execution.failure_category:
            return None, False
        return str(failed_execution.failure_category).strip() or None, False
    except (SQLAlchemyError, AttributeError, TypeError):
        return None, True


def _last_transition_at(session: SessionModel) -> datetime | None:
    return (
        getattr(session, "lifecycle_updated_at", None)
        or getattr(session, "updated_at", None)
        or getattr(session, "created_at", None)
    )


def derive_lifecycle_authority(
    db: DbSession,
    session: SessionModel,
    *,
    task_id: int | None = None,
    latest_task_execution: TaskExecution | None = None,
) -> LifecycleAuthority:
    """Derive all operator lifecycle facts from one SQLAlchemy read context.

    The caller's SQLAlchemy ``Session`` supplies the available transaction
    boundary.  This function does not claim snapshot isolation: under the
    configured database isolation, its sequential reads share that context as
    far as the existing persistence layer permits.  Query/identity ambiguity
    fails closed for quiescence.
    """

    status = _status(session)
    current_phase = _STATUS_TO_PHASE.get(status)
    continuation_pending, malformed, metadata = _continuation_state(session)
    logical_terminal = derive_logical_terminal(session)
    active_work, active_query_ambiguous = _active_work(db, session)
    quiescent = (
        status in _KNOWN_STATUSES
        and not continuation_pending
        and not malformed
        and not active_work
        and not active_query_ambiguous
    )

    latest, latest_query_ambiguous = _resolve_latest_execution(
        db,
        session,
        task_id=task_id,
        latest_task_execution=latest_task_execution,
    )
    attempt_status = None
    attempt_failure_reason = None
    if latest is not None:
        raw_status = getattr(latest, "status", None)
        attempt_status = getattr(raw_status, "value", raw_status)
        attempt_status = str(attempt_status) if attempt_status else None
        attempt_failure_reason = (
            str(latest.failure_category).strip() if latest.failure_category else None
        )

    terminal_reason = None
    reason_query_ambiguous = False
    if status in _TERMINAL_STATUSES and logical_terminal:
        terminal_reason, reason_query_ambiguous = _failure_reason(db, session, latest)
    elif (
        status == "paused"
        and not continuation_pending
        and not malformed
        and attempt_status in {"failed", "cancelled", "canceled"}
    ):
        # A manual pause is non-terminal, but it can preserve a failed task
        # attempt.  Keep that natural failure cause available to projections;
        # the pause cause is surfaced independently by stop-reason extraction.
        terminal_reason = attempt_failure_reason

    # A query failure does not manufacture terminality, but it does make the
    # physical/logical quiescence answer unsafe.
    if latest_query_ambiguous or reason_query_ambiguous:
        quiescent = False

    return LifecycleAuthority(
        current_phase=current_phase,
        terminal_reason=terminal_reason,
        attempt_status=attempt_status,
        attempt_failure_reason=attempt_failure_reason,
        continuation_pending=continuation_pending,
        continuation_kind=metadata.kind,
        retry_count=metadata.retry_count,
        retry_eta=metadata.retry_eta,
        logical_terminal=logical_terminal,
        quiescent=quiescent,
        last_transition_at=_last_transition_at(session),
        coordinator=_PHASE_TO_COORDINATOR.get(current_phase),
        allowed_actions=tuple(
            _STATUS_TO_ALLOWED_ACTIONS.get(status, ["view_logs", "view_timeline"])
        ),
    )
