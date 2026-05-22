"""DB-backed execution retry, failure classification, and stale attempt resolution."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import TaskExecution, TaskStatus

_RETRY_EXEMPT_CATEGORIES = {
    "planning_failure",
    "governance_hold",
    "backend_transport_error",
}

_MAX_RETRIES = 2


def classify_failure(exit_reason: str, backend_id: str, context: dict) -> str:
    """Map backend/provider outcome strings to a stable failure_category value."""
    reason = (exit_reason or "").lower()
    if any(k in reason for k in ("capacity", "lock", "slot", "busy")):
        return "backend_capacity_limit"
    if any(k in reason for k in ("time limit", "timeout", "timed out")):
        return "backend_timeout"
    if any(
        k in reason
        for k in ("transport", "connect", "unavailable", "config", "cli_not_found")
    ):
        return "backend_transport_error"
    if "planning" in reason and any(k in reason for k in ("fail", "invalid", "error")):
        return "planning_failure"
    if any(k in reason for k in ("validation", "validator", "contract")):
        return "validation_failure"
    if any(k in reason for k in ("governance", "review", "hold", "permission")):
        return "governance_hold"
    return "execution_failure"


def timeout_terminal_state_blocks_late_success(execution: TaskExecution | None) -> bool:
    """Return True when timeout already owns the terminal state for this execution."""

    if execution is None:
        return False
    return execution.failure_category == "backend_timeout" and execution.status in {
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    }


def should_retry(db: Session, task_execution_id: int, failure_category: str) -> bool:
    """Return True if a new attempt is warranted, based on persisted attempt history.

    Does not write state. Caller decides whether to create a new TaskExecution.
    """
    if failure_category in _RETRY_EXEMPT_CATEGORIES:
        return False
    execution = (
        db.query(TaskExecution).filter(TaskExecution.id == task_execution_id).first()
    )
    if not execution:
        return False
    existing_count = (
        db.query(TaskExecution)
        .filter(
            TaskExecution.session_id == execution.session_id,
            TaskExecution.task_id == execution.task_id,
        )
        .count()
    )
    return existing_count <= _MAX_RETRIES


def resolve_ambiguous_execution(
    db: Session, task_execution_id: int, runtime: object
) -> str:
    """Return the resolved TaskStatus string for a stale RUNNING execution row.

    Called on worker boot recovery to resolve orphaned active attempts.
    Does not write state — caller is responsible for committing the resolved status.
    """
    execution = (
        db.query(TaskExecution).filter(TaskExecution.id == task_execution_id).first()
    )
    if not execution:
        return TaskStatus.FAILED.value
    if execution.status != TaskStatus.RUNNING:
        return execution.status.value
    if hasattr(execution, "failure_category") and execution.failure_category is None:
        execution.failure_category = "lifecycle_inconsistency"
    return TaskStatus.CANCELLED.value
