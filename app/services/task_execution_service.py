"""Read-only helpers for task execution attempts."""

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import TaskExecution


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
