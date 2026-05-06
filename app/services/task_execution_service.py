"""Read-only helpers for task execution attempts."""

from datetime import datetime

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import TaskExecution, TaskStatus


def create_task_execution(
    db: Session,
    *,
    session_id: int,
    task_id: int,
    status: TaskStatus = TaskStatus.PENDING,
    started_at: datetime | None = None,
) -> TaskExecution:
    execution = TaskExecution(
        session_id=session_id,
        task_id=task_id,
        attempt_number=next_attempt_number(db, session_id, task_id),
        status=status,
        started_at=started_at,
    )
    db.add(execution)
    db.flush()
    return execution


def get_task_execution(
    db: Session, task_execution_id: int | None
) -> TaskExecution | None:
    if not task_execution_id:
        return None
    return db.query(TaskExecution).filter(TaskExecution.id == task_execution_id).first()


def next_attempt_number(db: Session, session_id: int, task_id: int) -> int:
    """Return the next attempt number without creating an execution row."""
    latest_attempt = (
        db.query(func.max(TaskExecution.attempt_number))
        .filter(
            TaskExecution.session_id == session_id,
            TaskExecution.task_id == task_id,
        )
        .scalar()
    )
    return int(latest_attempt or 0) + 1


def latest_execution_for_session_task(
    db: Session, session_id: int, task_id: int
) -> TaskExecution | None:
    return (
        db.query(TaskExecution)
        .filter(
            TaskExecution.session_id == session_id,
            TaskExecution.task_id == task_id,
        )
        .order_by(TaskExecution.attempt_number.desc(), TaskExecution.id.desc())
        .first()
    )


def executions_for_session(db: Session, session_id: int) -> list[TaskExecution]:
    return (
        db.query(TaskExecution)
        .filter(TaskExecution.session_id == session_id)
        .order_by(
            TaskExecution.task_id.asc(),
            TaskExecution.attempt_number.asc(),
            TaskExecution.id.asc(),
        )
        .all()
    )


def executions_for_task(db: Session, task_id: int) -> list[TaskExecution]:
    return (
        db.query(TaskExecution)
        .filter(TaskExecution.task_id == task_id)
        .order_by(
            TaskExecution.session_id.asc(),
            TaskExecution.attempt_number.asc(),
            TaskExecution.id.asc(),
        )
        .all()
    )
