"""Dispatch claiming and rejection helpers for orchestration workers."""

from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.models import Session as SessionModel, SessionTask, Task, TaskStatus
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.persistence import (
    append_orchestration_event as _append_orchestration_event,
    find_latest_orchestration_event as _find_latest_orchestration_event,
    read_orchestration_events as _read_orchestration_events,
)
from app.services.workspace.system_settings import (
    get_effective_agent_backend,
    get_effective_agent_model_family,
)

from .common import _parse_event_timestamp
from .execution_state import _clear_orphaned_running_state_without_active_execution

_STALE_QUEUE_CLAIM_SLA_SECONDS = 900


def _get_latest_session_task_link(
    db: Session, session_id: int, task_id: int
) -> Optional[SessionTask]:
    return (
        db.query(SessionTask)
        .filter(
            SessionTask.session_id == session_id,
            SessionTask.task_id == task_id,
        )
        .order_by(SessionTask.id.desc())
        .first()
    )


def _runtime_selection_details(db: Session) -> Dict[str, Optional[str]]:
    return {
        "backend": get_effective_agent_backend(
            settings.ORCHESTRATOR_AGENT_BACKEND, db=db
        ),
        "model_family": get_effective_agent_model_family(
            settings.ORCHESTRATOR_AGENT_MODEL_FAMILY, db=db
        ),
    }


def _should_reject_stale_dispatch_claim(
    *,
    dispatch_project_dir: Optional[Path],
    session_id: int,
    task_id: int,
    queued_event: Optional[Dict[str, Any]],
    queue_latency_seconds: Optional[float],
    resume_checkpoint_name: Optional[str] = None,
) -> Optional[str]:
    if resume_checkpoint_name:
        return None
    if not dispatch_project_dir or not queued_event:
        return None
    if (
        queue_latency_seconds is None
        or queue_latency_seconds <= _STALE_QUEUE_CLAIM_SLA_SECONDS
    ):
        return None

    queued_at = _parse_event_timestamp((queued_event or {}).get("timestamp"))
    if queued_at is None:
        return "stale_queue_dispatch"

    latest_post_queue_event = _find_latest_orchestration_event(
        dispatch_project_dir,
        session_id,
        task_id,
        event_types={
            EventType.TASK_CLAIMED,
            EventType.TASK_STARTED,
            EventType.PHASE_STARTED,
            EventType.PHASE_FINISHED,
        },
    )
    latest_post_queue_at = _parse_event_timestamp(
        (latest_post_queue_event or {}).get("timestamp")
    )
    if latest_post_queue_at is not None and latest_post_queue_at >= queued_at:
        return "stale_queue_dispatch_already_progressed"
    return "stale_queue_dispatch"


def _find_queued_event_for_dispatch(
    *,
    dispatch_project_dir: Optional[Path],
    session_id: int,
    task_id: int,
    queued_event_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    if not dispatch_project_dir:
        return None
    if queued_event_id:
        for event in _read_orchestration_events(
            dispatch_project_dir,
            session_id,
            task_id,
            event_type_filter=EventType.TASK_QUEUED,
        ):
            if event.get("event_id") == queued_event_id:
                return event
        return None
    return _find_latest_orchestration_event(
        dispatch_project_dir,
        session_id,
        task_id,
        event_types={EventType.TASK_QUEUED},
    )


def _claim_queued_task_for_worker(
    *,
    db: Session,
    session: SessionModel,
    task: Task,
    session_task_link: Optional[SessionTask],
    expected_session_instance_id: Optional[str],
) -> tuple[bool, str, Optional[datetime], Optional[SessionTask]]:
    """Claim a queued task exactly once for the current worker dispatch."""

    if (
        expected_session_instance_id
        and session.instance_id != expected_session_instance_id
    ):
        db.rollback()
        return False, "session_instance_changed", None, session_task_link

    if session.status not in {"pending", "running", "paused", "awaiting_input"}:
        db.rollback()
        return False, f"session_not_runnable:{session.status}", None, session_task_link

    claim_started_at = datetime.now(timezone.utc)
    updated_task_rows = (
        db.query(Task)
        .filter(Task.id == task.id, Task.status == TaskStatus.PENDING)
        .update(
            {
                Task.status: TaskStatus.RUNNING,
                Task.started_at: claim_started_at,
                Task.completed_at: None,
                Task.error_message: None,
            },
            synchronize_session=False,
        )
    )
    if updated_task_rows != 1:
        db.rollback()
        return False, f"task_not_claimable:{task.status.value}", None, session_task_link

    latest_link = session_task_link or _get_latest_session_task_link(
        db, session.id, task.id
    )
    if latest_link is None:
        db.rollback()
        return False, "missing_session_task_link", None, None

    updated_link_rows = (
        db.query(SessionTask)
        .filter(
            SessionTask.id == latest_link.id, SessionTask.status == TaskStatus.PENDING
        )
        .update(
            {
                SessionTask.status: TaskStatus.RUNNING,
                SessionTask.started_at: claim_started_at,
                SessionTask.completed_at: None,
            },
            synchronize_session=False,
        )
    )
    if updated_link_rows != 1:
        db.rollback()
        return (
            False,
            f"session_task_not_claimable:{latest_link.status.value}",
            None,
            latest_link,
        )

    db.commit()
    db.refresh(session)
    db.refresh(task)
    latest_link = _get_latest_session_task_link(db, session.id, task.id)
    return True, "claimed", claim_started_at, latest_link


def _emit_dispatch_rejected(
    *,
    reason: str,
    log_message: str,
    db: Session,
    session: SessionModel,
    session_id: int,
    task_id: int,
    task_execution_id: Optional[int],
    dispatch_project_dir: Optional[Path],
    expected_session_instance_id: Optional[str],
    celery_task_id: Optional[str],
    queue_latency_seconds: Optional[float],
    queued_event: Optional[Dict[str, Any]],
    emit_live: Any,
) -> Dict[str, Any]:
    reject_details = {
        "reason": reason,
        "session_instance_id": session.instance_id,
        "expected_session_instance_id": expected_session_instance_id,
        "project_dir": str(dispatch_project_dir) if dispatch_project_dir else None,
        "celery_task_id": celery_task_id,
        "task_execution_id": task_execution_id,
        "queue_latency_seconds": queue_latency_seconds,
        "queued_event_id": (queued_event or {}).get("event_id"),
        **_runtime_selection_details(db),
    }
    if dispatch_project_dir:
        _append_orchestration_event(
            project_dir=dispatch_project_dir,
            session_id=session_id,
            task_id=task_id,
            event_type=EventType.TASK_DISPATCH_REJECTED,
            details=reject_details,
        )
    if task_execution_id:
        _clear_orphaned_running_state_without_active_execution(
            db,
            session_id=session_id,
            task_id=task_id,
        )
    emit_live("WARN", log_message, metadata=reject_details)
    return {"status": "ignored", "reason": reason}
