"""TaskExecution synchronization helpers for orchestration workers."""

from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models import SessionTask, Task, TaskExecution, TaskStatus
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
    task_execution.status = status
    if started_at is not None:
        task_execution.started_at = started_at
    if completed_at is not None:
        task_execution.completed_at = completed_at
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
        task.status = TaskStatus.PENDING
        task.started_at = None
        task.completed_at = None

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
        link.status = TaskStatus.PENDING
        link.started_at = None
        link.completed_at = None

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

    current_status = (
        getattr(session_task_link, "status", None)
        or getattr(task, "status", None)
        or task_execution.status
    )
    if task_execution.status in {
        TaskStatus.DONE,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    } and current_status not in {
        TaskStatus.DONE,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }:
        return
    task_execution.status = current_status
    if getattr(session_task_link, "started_at", None) or getattr(
        task, "started_at", None
    ):
        task_execution.started_at = getattr(
            session_task_link, "started_at", None
        ) or getattr(task, "started_at", None)
    if current_status in {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}:
        task_execution.completed_at = (
            getattr(session_task_link, "completed_at", None)
            or getattr(task, "completed_at", None)
            or datetime.now(timezone.utc)
        )
    db.commit()
