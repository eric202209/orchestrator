"""Helpers for task execution attempts and their immutable identity evidence."""

import json
from datetime import datetime
from typing import Any

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import PlanningSession, Task, TaskExecution, TaskStatus
from app.services.observability.planning_identity import active_execution_identity


def create_task_execution(
    db: Session,
    *,
    session_id: int,
    task_id: int,
    status: TaskStatus = TaskStatus.PENDING,
    started_at: datetime | None = None,
) -> TaskExecution:
    identity = active_execution_identity(db)
    planning_session = _originating_planning_session(db, task_id)
    if planning_session is not None:
        identity.update(
            {
                "planning_session_id": planning_session.id,
                "planning_backend": planning_session.planning_backend,
                "planner_model": planning_session.planner_model,
                "reasoning_profile": planning_session.reasoning_profile,
                "configuration_fingerprint": (
                    planning_session.configuration_fingerprint
                ),
            }
        )
    execution = TaskExecution(
        session_id=session_id,
        task_id=task_id,
        attempt_number=next_attempt_number(db, session_id, task_id),
        status=status,
        started_at=started_at,
        **identity,
    )
    db.add(execution)
    db.flush()
    return execution


def task_execution_identity_payload(
    execution: TaskExecution | None,
) -> dict[str, Any] | None:
    if execution is None:
        return None
    return {
        "task_execution_id": execution.id,
        "planning_session_id": execution.planning_session_id,
        "planning_backend": execution.planning_backend,
        "execution_backend": execution.execution_backend,
        "planner_model": execution.planner_model,
        "executor_model": execution.executor_model,
        "reasoning_profile": execution.reasoning_profile,
        "configuration_fingerprint": execution.configuration_fingerprint,
    }


def _originating_planning_session(db: Session, task_id: int) -> PlanningSession | None:
    task = db.query(Task).filter(Task.id == task_id).first()
    if task is None or task.plan_id is None:
        return None
    candidates = (
        db.query(PlanningSession)
        .filter(PlanningSession.finalized_plan_id == task.plan_id)
        .order_by(PlanningSession.id.desc())
        .all()
    )
    explicit_matches = [
        candidate
        for candidate in candidates
        if task_id in _committed_task_ids(candidate.committed_task_ids)
    ]
    if len(explicit_matches) == 1:
        return explicit_matches[0]
    if not explicit_matches and len(candidates) == 1:
        return candidates[0]
    return None


def originating_planning_session_for_task(
    db: Session, task_id: int
) -> PlanningSession | None:
    """Return the uniquely attributable immutable planning session, if any."""

    return _originating_planning_session(db, task_id)


def _committed_task_ids(raw_value: str | None) -> set[int]:
    if not raw_value:
        return set()
    try:
        values = json.loads(raw_value)
    except (TypeError, json.JSONDecodeError):
        return set()
    if not isinstance(values, list):
        return set()
    return {
        int(value)
        for value in values
        if isinstance(value, int) or (isinstance(value, str) and value.isdigit())
    }


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
