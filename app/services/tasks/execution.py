"""Helpers for task execution attempts and their immutable identity evidence."""

from contextlib import contextmanager
import json
from datetime import datetime
from typing import Any, Iterator

from sqlalchemy import func
from sqlalchemy.orm import Session

from app.models import Project, PlanningSession, Task, TaskExecution, TaskStatus
from app.services.observability.planning_identity import active_execution_identity
from app.services.workspace.project_isolation_service import (
    resolve_project_workspace_path,
)
from app.services.workspace.project_mutation_lock import (
    ProjectMutationLockError,
    project_mutation_lock,
)


class ProjectExecutionSerializationConflict(RuntimeError):
    """A canonical project cannot admit another incompatible execution."""

    reason = "project_execution_serialization_conflict"

    def __init__(
        self,
        *,
        project_id: int,
        active_execution_id: int | None = None,
        lock_path: str | None = None,
    ):
        self.project_id = project_id
        self.active_execution_id = active_execution_id
        self.lock_path = lock_path
        details = [self.reason, f"project_id={project_id}"]
        if active_execution_id is not None:
            details.append(f"active_task_execution_id={active_execution_id}")
        if lock_path:
            details.append(f"lock_path={lock_path}")
        super().__init__(" ".join(details))


@contextmanager
def project_execution_serialization_admission(
    db: Session,
    *,
    session_id: int,
    task_id: int,
) -> Iterator[None]:
    """Atomically admit one canonical project execution before row creation.

    The existing project mutation lock is the admission mutex. The active
    TaskExecution query runs while that lock is held, and callers must commit
    the newly-created row before leaving this context. This keeps concurrent
    queue callers from both observing an empty active set while retaining the
    worker's later lock acquisition as defense in depth.
    """

    task = db.query(Task).filter(Task.id == task_id).first()
    project = (
        db.query(Project).filter(Project.id == task.project_id).first()
        if task is not None
        else None
    )
    if task is None or project is None:
        yield
        return

    # Keep the domain decision at the existing helper so a future profile
    # change does not globalize the gate.
    from app.services.orchestration.task_rules import (
        should_execute_in_canonical_project_root,
    )

    if not should_execute_in_canonical_project_root(
        task,
        getattr(task, "execution_profile", None),
        task.title,
        task.description,
    ):
        yield
        return

    project_root = resolve_project_workspace_path(
        project.workspace_path,
        project.name,
        db=db,
    )
    owner = f"session:{session_id}:task:{task_id}:execution:pending"
    try:
        with project_mutation_lock(
            project_id=project.id,
            project_root=project_root,
            operation="pre_dispatch_execution_admission",
            owner=owner,
        ):
            active_execution = (
                db.query(TaskExecution)
                .join(Task, Task.id == TaskExecution.task_id)
                .filter(
                    Task.project_id == project.id,
                    TaskExecution.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]),
                )
                .order_by(TaskExecution.id.desc())
                .first()
            )
            if active_execution is not None:
                raise ProjectExecutionSerializationConflict(
                    project_id=project.id,
                    active_execution_id=active_execution.id,
                )
            yield
    except ProjectMutationLockError as exc:
        raise ProjectExecutionSerializationConflict(
            project_id=project.id,
            lock_path=str(exc.lock_path),
        ) from exc


def create_task_execution(
    db: Session,
    *,
    session_id: int,
    task_id: int,
    status: TaskStatus = TaskStatus.PENDING,
    started_at: datetime | None = None,
) -> TaskExecution:
    identity = active_execution_identity(db)
    identity.pop("execution_adaptation_profile", None)
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
