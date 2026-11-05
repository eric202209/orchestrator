"""Shared run-state transitions for Execution Session task attempts."""

from __future__ import annotations

import os
import sqlite3
import socket
from datetime import datetime, timezone

from sqlalchemy import update
from sqlalchemy.orm import Session, object_session
from sqlalchemy.orm.attributes import set_committed_value
from sqlalchemy.orm.exc import UnmappedInstanceError

from app.models import SessionTask, Task, TaskExecution, TaskStatus
from app.services.execution.process_identity import current_process_start_identity


class TaskCompletionRejected(RuntimeError):
    """A persisted Task or related attempt row is no longer completable."""

    def __init__(self, reason: str):
        self.reason = reason
        super().__init__(reason)


class TaskCompletionTransactionAborted(TaskCompletionRejected):
    """A CAS database failure requires the caller to roll back its transaction."""

    def __init__(self, database_reason: str, *, sqlite_error_code: int | None = None):
        self.database_reason = database_reason
        self.sqlite_error_code = sqlite_error_code
        super().__init__("task_completion_transaction_aborted")


def _task_completion_transaction_aborted(
    db: Session, error: Exception
) -> TaskCompletionTransactionAborted:
    sqlite_error_code = getattr(getattr(error, "orig", None), "sqlite_errorcode", None)
    if db.get_bind().dialect.name != "sqlite":
        reason = "database_error"
    elif sqlite_error_code == getattr(sqlite3, "SQLITE_BUSY_SNAPSHOT", 517):
        reason = "sqlite_stale_snapshot"
    elif isinstance(sqlite_error_code, int) and sqlite_error_code & 0xFF in {
        sqlite3.SQLITE_BUSY,
        sqlite3.SQLITE_LOCKED,
    }:
        reason = "sqlite_write_lock_conflict"
    else:
        reason = "sqlite_database_error"
    return TaskCompletionTransactionAborted(
        reason,
        sqlite_error_code=(
            sqlite_error_code if isinstance(sqlite_error_code, int) else None
        ),
    )


def _begin_task_completion_savepoint(db: Session):
    connection = db.connection()
    if connection.dialect.name == "sqlite":
        # SQLAlchemy's Session.begin_nested() flushes all pending ORM state
        # before SAVEPOINT. Use the Connection API so caller-owned changes stay
        # outside the CAS savepoint. Under sqlite3 legacy transaction control,
        # SELECT does not start a physical transaction and RELEASE of a top-level
        # SAVEPOINT commits; explicitly open the outer transaction first.
        raw_connection = connection.connection.driver_connection
        if not raw_connection.in_transaction:
            connection.exec_driver_sql("BEGIN")
    return connection.begin_nested()


def _completion_session_for_rows(*rows: object | None) -> Session | None:
    attached_sessions: list[Session] = []
    for row in rows:
        if row is None:
            continue
        try:
            attached = object_session(row)
        except UnmappedInstanceError as exc:
            raise TaskCompletionRejected("completion_row_not_persisted") from exc
        if attached is None:
            raise TaskCompletionRejected("completion_row_not_persisted")
        attached_sessions.append(attached)
    if not attached_sessions:
        return None
    db = attached_sessions[0]
    if any(attached is not db for attached in attached_sessions[1:]):
        raise TaskCompletionRejected("completion_rows_use_different_transactions")
    return db


def _conditionally_mark_done(db: Session, row: object) -> bool:
    row_type = type(row)
    result = db.execute(
        update(row_type)
        .where(
            row_type.id == row.id,
            row_type.status.in_(
                [TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.DONE]
            ),
        )
        .values(status=TaskStatus.DONE)
        .execution_options(synchronize_session=False)
    )
    return result.rowcount == 1


def reserve_task_completion(db: Session, task_id: int) -> None:
    """Acquire a conditional Task write lock before completion publication.

    The status is unchanged.  Holding this transaction through finalization
    serializes a concurrent operator cancellation with baseline promotion and
    terminal completion commit. An ordinary predicate miss changes no row and
    leaves the caller transaction available. A database failure raises
    TaskCompletionTransactionAborted; the caller must roll back the full
    transaction before doing more database work.
    """

    try:
        with db.no_autoflush:
            result = db.execute(
                update(Task)
                .where(
                    Task.id == task_id,
                    Task.status.in_(
                        [TaskStatus.PENDING, TaskStatus.RUNNING, TaskStatus.DONE]
                    ),
                )
                .values(status=Task.status, updated_at=Task.updated_at)
                .execution_options(synchronize_session=False)
            )
    except Exception as exc:
        raise _task_completion_transaction_aborted(db, exc) from exc

    if result.rowcount != 1:
        raise TaskCompletionRejected("task_completion_not_active")


def _release_task_execution_runtime_ownership(
    task_execution: TaskExecution | None,
    *,
    released_at: datetime | None = None,
    reason: str,
) -> None:
    """End current runtime ownership while retaining immutable lease history."""

    if task_execution is None:
        return
    released_at = released_at or datetime.now(timezone.utc)
    task_execution.worker_pid = None
    task_execution.worker_hostname = None
    if hasattr(task_execution, "worker_process_start_identity"):
        task_execution.worker_process_start_identity = None
    task_execution.heartbeat_at = None

    lease = getattr(task_execution, "runtime_lease", None)
    if lease is not None and getattr(lease, "lease_status", None) == "active":
        lease.lease_status = "released"
        lease.released_at = released_at
        lease.release_reason = reason
        lease.closed_at = released_at
        lease.closure_reason = reason
        if hasattr(lease, "closed_worker_instance_id"):
            lease.closed_worker_instance_id = lease.worker_instance_id
        if hasattr(lease, "closed_ownership_fencing_token"):
            lease.closed_ownership_fencing_token = lease.ownership_fencing_token
        if hasattr(lease, "updated_at"):
            lease.updated_at = released_at


def _workspace_status_for_attempt_status(
    task: Task,
    status: TaskStatus,
) -> str:
    if not task.task_subfolder:
        return "not_created"
    if status == TaskStatus.RUNNING:
        return "in_progress"
    if status == TaskStatus.DONE:
        return "ready"
    if status in {TaskStatus.FAILED, TaskStatus.CANCELLED}:
        return "blocked"
    if status == TaskStatus.PENDING:
        return "isolated"
    return task.workspace_status or "isolated"


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
        task.workspace_status = _workspace_status_for_attempt_status(
            task, TaskStatus.RUNNING
        )
    if session_task_link:
        session_task_link.status = TaskStatus.RUNNING
        session_task_link.started_at = started_at
        session_task_link.completed_at = None
    if task_execution:
        task_execution.status = TaskStatus.RUNNING
        task_execution.started_at = started_at
        task_execution.completed_at = None
        task_execution.worker_pid = os.getpid()
        task_execution.worker_hostname = socket.gethostname()
        if hasattr(task_execution, "worker_process_start_identity"):
            task_execution.worker_process_start_identity = (
                current_process_start_identity(task_execution.worker_pid)
            )
        task_execution.heartbeat_at = started_at
    return started_at


def refresh_task_execution_lease(
    task_execution: TaskExecution | None,
    *,
    heartbeat_at: datetime | None = None,
) -> datetime | None:
    """Refresh the liveness lease for a running execution at a work boundary."""

    if task_execution is None or task_execution.status != TaskStatus.RUNNING:
        return None
    heartbeat_at = heartbeat_at or datetime.now(timezone.utc)
    task_execution.heartbeat_at = heartbeat_at
    return heartbeat_at


def mark_task_attempt_pending(
    *,
    task: Task | None,
    session_task_link: SessionTask | None = None,
    task_execution: TaskExecution | None = None,
    reset_started_at: bool = False,
    reset_steps: bool = False,
    workspace_status: str | None = None,
    error_message: str | None = None,
) -> None:
    """Reset a task attempt so it can be picked up by a later run."""

    if task:
        task.status = TaskStatus.PENDING
        if reset_started_at:
            task.started_at = None
        task.completed_at = None
        task.current_step = 0
        if reset_steps:
            task.steps = None
        if workspace_status is not None:
            task.workspace_status = workspace_status
        task.error_message = error_message
    if session_task_link:
        session_task_link.status = TaskStatus.PENDING
        if reset_started_at:
            session_task_link.started_at = None
        session_task_link.completed_at = None
    if task_execution:
        task_execution.status = TaskStatus.PENDING
        if reset_started_at:
            task_execution.started_at = None
        task_execution.completed_at = None
        _release_task_execution_runtime_ownership(
            task_execution,
            reason="attempt_pending_handoff",
        )


def mark_task_attempt_failed(
    *,
    task: Task | None,
    session_task_link: SessionTask | None = None,
    task_execution: TaskExecution | None = None,
    error_message: str | None = None,
    completed_at: datetime | None = None,
    workspace_status: str | None = None,
    failure_metadata_authoritative: bool = False,
) -> datetime:
    """Mark a task attempt as failed across domain rows."""

    task_was_failed = task is not None and task.status == TaskStatus.FAILED
    session_task_was_failed = (
        session_task_link is not None and session_task_link.status == TaskStatus.FAILED
    )
    existing_completed_at = (
        getattr(task, "completed_at", None)
        if task_was_failed and getattr(task, "completed_at", None) is not None
        else (
            getattr(session_task_link, "completed_at", None)
            if session_task_was_failed
            and getattr(session_task_link, "completed_at", None) is not None
            else None
        )
    )
    completed_at = existing_completed_at or completed_at or datetime.now(timezone.utc)
    if task:
        task.status = TaskStatus.FAILED
        if error_message is not None and (
            not task_was_failed
            or getattr(task, "error_message", None) is None
            or failure_metadata_authoritative
        ):
            task.error_message = error_message
        if getattr(task, "completed_at", None) is None or not task_was_failed:
            task.completed_at = completed_at
        if workspace_status is not None and (
            not task_was_failed or getattr(task, "workspace_status", None) is None
        ):
            task.workspace_status = workspace_status
    if session_task_link:
        session_task_link.status = TaskStatus.FAILED
        if (
            getattr(session_task_link, "completed_at", None) is None
            or not session_task_was_failed
        ):
            session_task_link.completed_at = completed_at
    if task_execution:
        task_execution.status = TaskStatus.FAILED
        task_execution.completed_at = task_execution.completed_at or completed_at
        if task_execution.dispatch_state is not None:
            task_execution.dispatch_state = "TERMINAL"
            task_execution.dispatch_lease_owner = None
            task_execution.dispatch_lease_expires_at = None
        _release_task_execution_runtime_ownership(
            task_execution,
            released_at=completed_at,
            reason="attempt_failed",
        )
    return completed_at


def mark_task_attempt_cancelled(
    *,
    task: Task | None,
    session_task_link: SessionTask | None = None,
    task_execution: TaskExecution | None = None,
    completed_at: datetime | None = None,
    error_message: str | None = None,
) -> datetime:
    """Mark a task attempt as cancelled across domain rows."""

    completed_at = completed_at or datetime.now(timezone.utc)
    if task:
        task.status = TaskStatus.CANCELLED
        if error_message is not None:
            task.error_message = error_message
        task.completed_at = completed_at
    if session_task_link:
        session_task_link.status = TaskStatus.CANCELLED
        session_task_link.completed_at = completed_at
    if task_execution:
        task_execution.status = TaskStatus.CANCELLED
        task_execution.completed_at = task_execution.completed_at or completed_at
        if task_execution.dispatch_state is not None:
            task_execution.dispatch_state = "TERMINAL"
            task_execution.dispatch_lease_owner = None
            task_execution.dispatch_lease_expires_at = None
        _release_task_execution_runtime_ownership(
            task_execution,
            released_at=completed_at,
            reason="attempt_cancelled",
        )
    return completed_at


def mark_task_attempt_done(
    *,
    task: Task | None,
    session_task_link: SessionTask | None = None,
    task_execution: TaskExecution | None = None,
    completed_at: datetime | None = None,
) -> datetime:
    """Conditionally mark supplied attempt rows done in one CAS savepoint.

    This helper participates in, but never commits or rolls back, the caller's
    transaction. Predicate rejection rolls back only the CAS savepoint. A
    database failure raises TaskCompletionTransactionAborted and requires the
    caller to roll back the full transaction.
    """

    completed_at = completed_at or datetime.now(timezone.utc)

    # The Task row is the cancellation authority.  Conditional writes on every
    # supplied row keep a stale completion from overwriting a committed
    # cancellation and make the related terminal changes one DB transaction.
    db = _completion_session_for_rows(task, session_task_link, task_execution)
    if db is not None:
        savepoint = None
        try:
            savepoint = _begin_task_completion_savepoint(db)
            with db.no_autoflush:
                for row in (task, session_task_link, task_execution):
                    if row is not None and not _conditionally_mark_done(db, row):
                        raise TaskCompletionRejected("task_completion_not_active")
            savepoint.commit()
        except TaskCompletionTransactionAborted:
            raise
        except TaskCompletionRejected:
            if savepoint is not None and savepoint.is_active:
                try:
                    savepoint.rollback()
                except Exception as rollback_error:
                    raise _task_completion_transaction_aborted(
                        db, rollback_error
                    ) from rollback_error
            raise
        except Exception as exc:
            if savepoint is not None and savepoint.is_active:
                try:
                    savepoint.rollback()
                except Exception as rollback_error:
                    exc = rollback_error
            raise _task_completion_transaction_aborted(db, exc) from exc

    if task:
        set_committed_value(task, "status", TaskStatus.DONE)
        task.completed_at = completed_at
        task.error_message = None
    if session_task_link:
        set_committed_value(session_task_link, "status", TaskStatus.DONE)
        session_task_link.completed_at = completed_at
    if task_execution:
        set_committed_value(task_execution, "status", TaskStatus.DONE)
        task_execution.completed_at = task_execution.completed_at or completed_at
        if task_execution.dispatch_state is not None:
            task_execution.dispatch_state = "TERMINAL"
            task_execution.dispatch_lease_owner = None
            task_execution.dispatch_lease_expires_at = None
        _release_task_execution_runtime_ownership(
            task_execution,
            released_at=completed_at,
            reason="attempt_completed",
        )
    return completed_at


def finalize_attempt_planning_failure(
    *,
    task: Task | None,
    session_task_link: SessionTask | None = None,
    task_execution: TaskExecution | None = None,
    error_message: str | None = None,
    completed_at: datetime | None = None,
    workspace_status: str = "blocked",
) -> datetime:
    """Finalize an attempt that failed before execution due to planning."""

    return mark_task_attempt_failed(
        task=task,
        session_task_link=session_task_link,
        task_execution=task_execution,
        error_message=error_message,
        completed_at=completed_at,
        workspace_status=workspace_status,
    )


def finalize_attempt_execution_failure(
    *,
    task: Task | None,
    session_task_link: SessionTask | None = None,
    task_execution: TaskExecution | None = None,
    error_message: str | None = None,
    completed_at: datetime | None = None,
    workspace_status: str = "blocked",
) -> datetime:
    """Finalize an attempt that failed while executing or verifying steps."""

    return mark_task_attempt_failed(
        task=task,
        session_task_link=session_task_link,
        task_execution=task_execution,
        error_message=error_message,
        completed_at=completed_at,
        workspace_status=workspace_status,
    )


def finalize_attempt_completion_validation_failure(
    *,
    task: Task | None,
    session_task_link: SessionTask | None = None,
    task_execution: TaskExecution | None = None,
    error_message: str | None = None,
    completed_at: datetime | None = None,
    workspace_status: str = "blocked",
) -> datetime:
    """Finalize an attempt whose final completion validation failed."""

    return mark_task_attempt_failed(
        task=task,
        session_task_link=session_task_link,
        task_execution=task_execution,
        error_message=error_message,
        completed_at=completed_at,
        workspace_status=workspace_status,
    )


def cancel_attempt_for_session_pause_stop(
    *,
    task: Task | None,
    session_task_link: SessionTask | None = None,
    task_execution: TaskExecution | None = None,
    completed_at: datetime | None = None,
) -> datetime:
    """Finalize an active attempt that is cancelled by pause or stop."""

    return mark_task_attempt_cancelled(
        task=task,
        session_task_link=session_task_link,
        task_execution=task_execution,
        completed_at=completed_at,
    )


def finalize_attempt_successful_completion(
    *,
    task: Task | None,
    session_task_link: SessionTask | None = None,
    task_execution: TaskExecution | None = None,
    completed_at: datetime | None = None,
) -> datetime:
    """Finalize a successfully completed attempt."""

    return mark_task_attempt_done(
        task=task,
        session_task_link=session_task_link,
        task_execution=task_execution,
        completed_at=completed_at,
    )


def reset_active_attempts_for_session_stop(
    db: Session,
    *,
    session_id: int,
    next_status: TaskStatus = TaskStatus.PENDING,
    terminalize: bool = False,
    stop_reason: str | None = None,
) -> int:
    """Normalize active attempts after pause, or terminalize an explicit stop."""

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
        task = db.query(Task).filter(Task.id == execution.task_id).first()
        link = (
            db.query(SessionTask)
            .filter(
                SessionTask.session_id == session_id,
                SessionTask.task_id == execution.task_id,
            )
            .order_by(SessionTask.id.desc())
            .first()
        )
        if terminalize:
            mark_task_attempt_cancelled(
                task=task,
                session_task_link=link,
                task_execution=execution,
                completed_at=now,
                error_message=stop_reason,
            )
        else:
            execution.status = TaskStatus.CANCELLED
            execution.completed_at = execution.completed_at or now
            _release_task_execution_runtime_ownership(
                execution,
                released_at=now,
                reason="session_pause",
            )
        if getattr(execution, "failure_category", None) is None:
            execution.failure_category = "manual_stop"

    if terminalize:
        terminalized_task_ids = set(execution_task_ids)
        running_links = (
            db.query(SessionTask)
            .filter(
                SessionTask.session_id == session_id,
                SessionTask.status == TaskStatus.RUNNING,
            )
            .all()
        )
        for link in running_links:
            if link.task_id in terminalized_task_ids:
                continue
            task = db.query(Task).filter(Task.id == link.task_id).first()
            mark_task_attempt_cancelled(
                task=task,
                session_task_link=link,
                completed_at=now,
                error_message=stop_reason,
            )
            terminalized_task_ids.add(link.task_id)
        return len(terminalized_task_ids)

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
            task.workspace_status = _workspace_status_for_attempt_status(
                task, next_status
            )

    for task_id in execution_task_ids - seen_task_ids:
        task = db.query(Task).filter(Task.id == task_id).first()
        if task and task.status == TaskStatus.RUNNING:
            task.status = next_status
            task.completed_at = None
            task.error_message = None
            task.workspace_status = _workspace_status_for_attempt_status(
                task, next_status
            )
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
