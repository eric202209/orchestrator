"""Shared run-state transitions for Execution Session task attempts."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy.orm import Session

from app.models import SessionTask, Task, TaskExecution, TaskStatus


def mark_task_attempt_running(
    *,
    task: Task | None,
    session_task_link: SessionTask | None = None,
    task_execution: TaskExecution | None = None,
    started_at: datetime | None = None,
) -> datetime:
    """Mark a task attempt as actively running across domain rows."""

    started_at = started_at or datetime.now(timezone.utc)
    if task:
        task.status = TaskStatus.RUNNING
        task.started_at = started_at
        task.completed_at = None
        task.error_message = None
        task.current_step = 0
        task.workspace_status = "in_progress" if task.task_subfolder else "not_created"
    if session_task_link:
        session_task_link.status = TaskStatus.RUNNING
        session_task_link.started_at = started_at
        session_task_link.completed_at = None
    if task_execution:
        task_execution.status = TaskStatus.RUNNING
        task_execution.started_at = started_at
        task_execution.completed_at = None
    return started_at


def mark_task_attempt_failed(
    *,
    task: Task | None,
    session_task_link: SessionTask | None = None,
    task_execution: TaskExecution | None = None,
    error_message: str | None = None,
    completed_at: datetime | None = None,
    workspace_status: str | None = None,
) -> datetime:
    """Mark a task attempt as failed across domain rows."""

    completed_at = completed_at or datetime.now(timezone.utc)
    if task:
        task.status = TaskStatus.FAILED
        if error_message is not None:
            task.error_message = error_message
        task.completed_at = completed_at
        if workspace_status is not None:
            task.workspace_status = workspace_status
    if session_task_link:
        session_task_link.status = TaskStatus.FAILED
        session_task_link.completed_at = completed_at
    if task_execution:
        task_execution.status = TaskStatus.FAILED
        task_execution.completed_at = task_execution.completed_at or completed_at
    return completed_at


def mark_task_attempt_cancelled(
    *,
    task: Task | None,
    session_task_link: SessionTask | None = None,
    task_execution: TaskExecution | None = None,
    completed_at: datetime | None = None,
) -> datetime:
    """Mark a task attempt as cancelled across domain rows."""

    completed_at = completed_at or datetime.now(timezone.utc)
    if task:
        task.status = TaskStatus.CANCELLED
        task.completed_at = completed_at
    if session_task_link:
        session_task_link.status = TaskStatus.CANCELLED
        session_task_link.completed_at = completed_at
    if task_execution:
        task_execution.status = TaskStatus.CANCELLED
        task_execution.completed_at = task_execution.completed_at or completed_at
    return completed_at


def mark_task_attempt_done(
    *,
    task: Task | None,
    session_task_link: SessionTask | None = None,
    task_execution: TaskExecution | None = None,
    completed_at: datetime | None = None,
) -> datetime:
    """Mark a task attempt as done across domain rows."""

    completed_at = completed_at or datetime.now(timezone.utc)
    if task:
        task.status = TaskStatus.DONE
        task.completed_at = completed_at
        task.error_message = None
    if session_task_link:
        session_task_link.status = TaskStatus.DONE
        session_task_link.completed_at = completed_at
    if task_execution:
        task_execution.status = TaskStatus.DONE
        task_execution.completed_at = task_execution.completed_at or completed_at
    return completed_at


def reset_active_attempts_for_session_stop(
    db: Session,
    *,
    session_id: int,
    next_status: TaskStatus = TaskStatus.PENDING,
) -> int:
    """Normalize active attempts after pause/stop so resume starts cleanly."""

    now = datetime.now(timezone.utc)
    active_executions = (
        db.query(TaskExecution)
        .filter(
            TaskExecution.session_id == session_id,
            TaskExecution.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]),
        )
        .all()
    )
    execution_task_ids = {execution.task_id for execution in active_executions}
    for execution in active_executions:
        execution.status = TaskStatus.CANCELLED
        execution.completed_at = execution.completed_at or now

    running_links = (
        db.query(SessionTask)
        .filter(
            SessionTask.session_id == session_id,
            SessionTask.status == TaskStatus.RUNNING,
        )
        .all()
    )
    updated = 0
    seen_task_ids: set[int] = set()

    for link in running_links:
        link.status = next_status
        link.completed_at = None
        updated += 1
        if link.task_id in seen_task_ids:
            continue
        seen_task_ids.add(link.task_id)
        task = db.query(Task).filter(Task.id == link.task_id).first()
        if not task:
            continue
        if task.status == TaskStatus.RUNNING:
            task.status = next_status
            task.completed_at = None
            task.error_message = None

    for task_id in execution_task_ids - seen_task_ids:
        task = db.query(Task).filter(Task.id == task_id).first()
        if task and task.status == TaskStatus.RUNNING:
            task.status = next_status
            task.completed_at = None
            task.error_message = None
        latest_link = (
            db.query(SessionTask)
            .filter(
                SessionTask.session_id == session_id,
                SessionTask.task_id == task_id,
            )
            .order_by(SessionTask.id.desc())
            .first()
        )
        if latest_link and latest_link.status == TaskStatus.RUNNING:
            latest_link.status = next_status
            latest_link.completed_at = None
            updated += 1

    return updated
