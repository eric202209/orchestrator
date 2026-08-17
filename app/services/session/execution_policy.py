"""DB-backed execution retry, failure classification, and stale attempt resolution."""

from __future__ import annotations

from sqlalchemy.orm import Session

from app.models import TaskExecution, TaskStatus

_RETRY_EXEMPT_CATEGORIES = {
    "planning_failure",
    "governance_hold",
    "backend_transport_error",
    "planning_contract_violation",
}

_MAX_RETRIES = 2

# One automatic recovery rerun per failure episode. Every rerun dispatches a
# fresh TaskExecution, so the persisted attempt rows are the durable episode
# generation counter.
_MAX_AUTOMATIC_RECOVERY_RERUNS = 1

# Deterministic planning/plan contract rejections. The provider response is
# refused before normalization, Plan Repair, APA, or Execution, so an identical
# request is refused identically — a retry only re-spends provider time.
_DETERMINISTIC_PLANNING_CONTRACT_MARKERS = (
    "planning_semantic_target_contract_violation",
    "planning_semantic_target_inventory_invalid",
    "planning_repair_missing_source_materialization",
    "planning_validation_failed_after_repair",
    "repair_output_contract_violation",
    "op_contract_violation",
    "unknown_target_id",
    "target_id_path_mismatch",
    "target_id_operation_forbidden",
    "target_path_invalid",
    "provider_plan_shape_invalid",
    "provider_semantic_shape_invalid",
    "provider_semantic_new_invalid",
    "provider_mixed_old_target_id",
    "provider_selector_internals_forbidden",
    "semantic_target_construction_",
)


def is_deterministic_planning_contract_failure(reason: str) -> bool:
    """True when the failure is a deterministic planning/plan contract rejection."""

    text = (reason or "").lower()
    return any(marker in text for marker in _DETERMINISTIC_PLANNING_CONTRACT_MARKERS)


def is_retry_exempt_category(failure_category: str) -> bool:
    """True when persisted policy forbids creating a new attempt for this category."""

    return failure_category in _RETRY_EXEMPT_CATEGORIES


def automatic_recovery_rerun_allowed(
    db: Session, *, session_id: int | None, task_id: int | None
) -> bool:
    """True while this failure episode still has its one automatic recovery rerun.

    The rerun dispatches a fresh TaskExecution, so the persisted attempt rows are
    the durable episode ceiling. Celery retries reuse the same TaskExecution and
    reset the mutable ``task.workspace_status``, so neither can re-arm this guard.
    """

    if db is None or session_id is None or task_id is None:
        return True
    attempts = (
        db.query(TaskExecution)
        .filter(
            TaskExecution.session_id == session_id,
            TaskExecution.task_id == task_id,
        )
        .count()
    )
    return attempts <= _MAX_AUTOMATIC_RECOVERY_RERUNS


def classify_failure(exit_reason: str, backend_id: str, context: dict) -> str:
    """Map backend/provider outcome strings to a stable failure_category value."""
    reason = (exit_reason or "").lower()
    context = context or {}
    provider_classification = str(
        context.get("provider_failure_classification") or ""
    ).lower()
    failure_category = str(context.get("failure_category") or "").lower()
    if failure_category == "runtime_safety_stop" or any(
        marker in reason
        for marker in (
            "runtime pollution",
            "canonical workspace pollution",
            "provider initialization escaped",
            "workspace binding mismatch",
        )
    ):
        return "runtime_safety_stop"
    if is_deterministic_planning_contract_failure(reason):
        return "planning_contract_violation"
    if provider_classification == "provider_timeout":
        return "provider_timeout"
    if context.get("failure_phase") == "planning" and (
        "repair" in reason and "timeout" in reason
    ):
        return "planning_repair_timeout"
    if "planning repair" in reason and any(
        marker in reason for marker in ("timeout", "timed out")
    ):
        return "planning_repair_timeout"
    if context.get("failure_phase") == "planning" and any(
        marker in reason
        for marker in (
            "planning validation",
            "plan validation",
            "planning json",
            "malformed planning",
        )
    ):
        return "planning_validation_failed"
    if "infrastructure" in reason or "worker lost" in reason:
        return "infrastructure_failure"
    if any(k in reason for k in ("context window too small", "blocked model")):
        # Deterministic backend capability rejection: the same request fails
        # identically until the deployment configuration changes, so a new
        # attempt is never warranted (backend_transport_error is retry-exempt).
        return "backend_transport_error"
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
