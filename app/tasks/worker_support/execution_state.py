"""TaskExecution synchronization helpers for orchestration workers."""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models import SessionTask, Task, TaskExecution, TaskStatus
from app.services.orchestration.run_state import (
    mark_task_attempt_cancelled,
    mark_task_attempt_done,
    mark_task_attempt_failed,
    mark_task_attempt_pending,
    mark_task_attempt_running,
)
from app.services.task_execution_service import get_task_execution


def _sync_task_execution_state(
    db: Session,
    task_execution_id: Optional[int],
    *,
    status: TaskStatus,
    started_at: Optional[datetime] = None,
    completed_at: Optional[datetime] = None,
) -> None:
    task_execution = get_task_execution(db, task_execution_id)
    if not task_execution:
        return
    if status == TaskStatus.RUNNING:
        mark_task_attempt_running(
            task=None,
            task_execution=task_execution,
            started_at=started_at,
        )
    elif status == TaskStatus.FAILED:
        mark_task_attempt_failed(
            task=None,
            task_execution=task_execution,
            completed_at=completed_at,
        )
    elif status == TaskStatus.CANCELLED:
        mark_task_attempt_cancelled(
            task=None,
            task_execution=task_execution,
            completed_at=completed_at,
        )
    elif status == TaskStatus.DONE:
        mark_task_attempt_done(
            task=None,
            task_execution=task_execution,
            completed_at=completed_at,
        )
    elif status == TaskStatus.PENDING:
        mark_task_attempt_pending(
            task=None,
            task_execution=task_execution,
            reset_started_at=started_at is None,
        )
        if started_at is not None:
            task_execution.started_at = started_at
    db.commit()


def _clear_orphaned_running_state_without_active_execution(
    db: Session,
    *,
    session_id: int,
    task_id: int,
) -> None:
    """A task/link cannot remain RUNNING after its only active execution is terminal."""

    active_execution = (
        db.query(TaskExecution)
        .filter(
            TaskExecution.session_id == session_id,
            TaskExecution.task_id == task_id,
            TaskExecution.status == TaskStatus.RUNNING,
        )
        .first()
    )
    if active_execution:
        return

    task = db.query(Task).filter(Task.id == task_id).first()
    if task and task.status == TaskStatus.RUNNING:
        mark_task_attempt_pending(task=task, reset_started_at=True)

    running_links = (
        db.query(SessionTask)
        .filter(
            SessionTask.session_id == session_id,
            SessionTask.task_id == task_id,
            SessionTask.status == TaskStatus.RUNNING,
        )
        .all()
    )
    for link in running_links:
        mark_task_attempt_pending(
            task=None,
            session_task_link=link,
            reset_started_at=True,
        )

    db.commit()


def _sync_task_execution_from_task_state(
    db: Session,
    task_execution_id: Optional[int],
    *,
    task: Optional[Task],
    session_task_link: Optional[SessionTask],
) -> None:
    task_execution = get_task_execution(db, task_execution_id)
    if not task_execution:
        return

    task_status = getattr(task, "status", None)
    link_status = getattr(session_task_link, "status", None)
    terminal_statuses = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
    if task_status in terminal_statuses:
        current_status = task_status
    else:
        current_status = link_status or task_status or task_execution.status
    if (
        task_execution.status
        in {
            TaskStatus.DONE,
            TaskStatus.FAILED,
            TaskStatus.CANCELLED,
        }
        and current_status not in terminal_statuses
    ):
        return
    started_at = getattr(session_task_link, "started_at", None) or getattr(
        task, "started_at", None
    )
    if current_status in {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}:
        completed_at = (
            getattr(session_task_link, "completed_at", None)
            or getattr(task, "completed_at", None)
            or datetime.now(timezone.utc)
        )
        if current_status == TaskStatus.DONE:
            mark_task_attempt_done(
                task=None,
                task_execution=task_execution,
                completed_at=completed_at,
            )
        elif current_status == TaskStatus.FAILED:
            mark_task_attempt_failed(
                task=None,
                task_execution=task_execution,
                completed_at=completed_at,
            )
        else:
            mark_task_attempt_cancelled(
                task=None,
                task_execution=task_execution,
                completed_at=completed_at,
            )
    elif current_status == TaskStatus.RUNNING:
        mark_task_attempt_running(
            task=None,
            task_execution=task_execution,
            started_at=started_at,
        )
    elif current_status == TaskStatus.PENDING:
        mark_task_attempt_pending(
            task=None,
            task_execution=task_execution,
            reset_started_at=started_at is None,
        )
        if started_at is not None:
            task_execution.started_at = started_at
    db.commit()
