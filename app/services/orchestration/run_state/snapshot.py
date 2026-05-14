"""Read-only run-state snapshots for diagnostics and invariant tests."""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from typing import Optional

from sqlalchemy.orm import Session

from app.models import Session as SessionModel
from app.models import SessionTask, Task, TaskExecution, TaskStatus


@dataclass(frozen=True)
class RunStateSnapshot:
    session_id: int
    task_id: Optional[int]
    task_execution_id: Optional[int]
    session_status: Optional[str]
    session_is_active: Optional[bool]
    task_status: Optional[str]
    session_task_status: Optional[str]
    task_execution_status: Optional[str]
    task_completed_at: Optional[datetime]
    session_task_completed_at: Optional[datetime]
    task_execution_completed_at: Optional[datetime]

    @property
    def has_active_execution(self) -> bool:
        return self.task_execution_status in {
            TaskStatus.PENDING.value,
            TaskStatus.RUNNING.value,
        }

    @property
    def stopped_with_active_execution(self) -> bool:
        return (
            self.session_status in {"stopped", "deleted"}
            or self.session_is_active is False
        ) and self.has_active_execution


def _status_value(value: object) -> Optional[str]:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def read_run_state_snapshot(
    db: Session,
    *,
    session_id: int,
    task_id: Optional[int] = None,
    task_execution_id: Optional[int] = None,
) -> RunStateSnapshot:
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()

    task_execution = None
    if task_execution_id is not None:
        task_execution = (
            db.query(TaskExecution)
            .filter(TaskExecution.id == task_execution_id)
            .first()
        )
    elif task_id is not None:
        task_execution = (
            db.query(TaskExecution)
            .filter(
                TaskExecution.session_id == session_id,
                TaskExecution.task_id == task_id,
            )
            .order_by(
                TaskExecution.completed_at.desc().nullslast(),
                TaskExecution.started_at.desc().nullslast(),
                TaskExecution.created_at.desc().nullslast(),
                TaskExecution.id.desc(),
            )
            .first()
        )

    resolved_task_id = task_id or getattr(task_execution, "task_id", None)
    task = (
        db.query(Task).filter(Task.id == resolved_task_id).first()
        if resolved_task_id is not None
        else None
    )
    session_task_link = (
        db.query(SessionTask)
        .filter(
            SessionTask.session_id == session_id,
            SessionTask.task_id == resolved_task_id,
        )
        .order_by(SessionTask.id.desc())
        .first()
        if resolved_task_id is not None
        else None
    )

    return RunStateSnapshot(
        session_id=session_id,
        task_id=resolved_task_id,
        task_execution_id=getattr(task_execution, "id", None),
        session_status=str(getattr(session, "status", None) or "") or None,
        session_is_active=(
            bool(getattr(session, "is_active"))
            if session is not None and getattr(session, "is_active", None) is not None
            else None
        ),
        task_status=_status_value(getattr(task, "status", None)),
        session_task_status=_status_value(getattr(session_task_link, "status", None)),
        task_execution_status=_status_value(getattr(task_execution, "status", None)),
        task_completed_at=getattr(task, "completed_at", None),
        session_task_completed_at=getattr(session_task_link, "completed_at", None),
        task_execution_completed_at=getattr(task_execution, "completed_at", None),
    )
