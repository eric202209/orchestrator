"""Bounded observability for task outcome and failure reconstruction."""

from __future__ import annotations

import json
import re
import traceback
from typing import Any, Callable, Optional

from app.services.orchestration.events.event_types import EventType

_MAX_EXCEPTION_MESSAGE_CHARS = 4000
_MAX_TRACEBACK_CHARS = 12000
_SENSITIVE_VALUE_PATTERN = re.compile(
    r"(?i)(\b(?:authorization|bearer|api[_-]?key|password|passwd|secret|token|"
    r"credential|cookie)\b\s*[:=]\s*)([^\s,;]+)"
)


def _redact_sensitive_values(value: str) -> str:
    value = _SENSITIVE_VALUE_PATTERN.sub(r"\1<redacted>", value)
    return re.sub(r"(?i)\bbearer\s+[A-Za-z0-9._~+/=-]+", "Bearer <redacted>", value)


def bounded_exception_message(exc: BaseException) -> str:
    """Return a bounded exception message with common credential forms redacted."""

    return _redact_sensitive_values(str(exc))[:_MAX_EXCEPTION_MESSAGE_CHARS]


def bounded_exception_traceback(exc: BaseException) -> str:
    """Return a bounded traceback without locals, prompts, or model responses."""

    rendered = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    return _redact_sensitive_values(rendered)[:_MAX_TRACEBACK_CHARS]


def status_value(value: Any) -> Optional[str]:
    if value is None:
        return None
    return str(getattr(value, "value", value))


def _durable_project_dir(ctx: Any, state: Any) -> Any:
    """Resolve the project event root instead of a disposable runtime root."""

    project = getattr(ctx, "project", None)
    if project is not None:
        try:
            from app.services.workspace.project_isolation_service import (
                resolve_project_workspace_path,
            )

            return resolve_project_workspace_path(
                getattr(project, "workspace_path", None),
                getattr(project, "name", None),
                db=getattr(ctx, "db", None),
            )
        except Exception:
            pass
    return getattr(state, "project_dir", None)


def build_failure_evidence(
    *,
    exc: BaseException,
    session_id: Optional[int],
    task_id: Optional[int],
    task_execution_id: Optional[int],
    orchestration_phase: Any,
    task_status_before_failure: Any,
    session_task_status_before_failure: Any,
    task_execution_status_before_failure: Any,
    orchestration_status_before_failure: Any,
    failure_category: Optional[str],
    failure_class: Optional[str],
    retry_capacity: bool,
    automatic_recovery_eligible: bool,
    project_mutation_lock_classification: bool,
    planning_lock_classification: bool,
    timeout_classification: bool,
    authoritative_success_recorded: bool,
) -> dict[str, Any]:
    return {
        "exception_type": type(exc).__name__,
        "exception_module": type(exc).__module__,
        "exception_message": bounded_exception_message(exc),
        "traceback": bounded_exception_traceback(exc),
        "session_id": session_id,
        "task_id": task_id,
        "task_execution_id": task_execution_id,
        "orchestration_phase": status_value(orchestration_phase) or "failure",
        "task_status_before_failure": status_value(task_status_before_failure),
        "session_task_status_before_failure": status_value(
            session_task_status_before_failure
        ),
        "task_execution_status_before_failure": status_value(
            task_execution_status_before_failure
        ),
        "orchestration_status_before_failure": status_value(
            orchestration_status_before_failure
        ),
        "failure_category": failure_category,
        "failure_class": failure_class,
        "retry_capacity": bool(retry_capacity),
        "automatic_recovery_eligible": bool(automatic_recovery_eligible),
        "project_mutation_lock_classification": bool(
            project_mutation_lock_classification
        ),
        "planning_lock_classification": bool(planning_lock_classification),
        "timeout_classification": bool(timeout_classification),
        "authoritative_success_recorded": bool(authoritative_success_recorded),
    }


def persist_failure_evidence(
    *,
    evidence: dict[str, Any],
    project_dir: Any,
    session_id: Optional[int],
    task_id: Optional[int],
    task_execution_id: Optional[int],
    session_instance_id: Optional[str],
    append_orchestration_event_fn: Callable[..., Any],
    record_live_log_fn: Callable[..., Any],
    db: Any,
    logger: Any,
) -> None:
    """Persist/emit failure evidence, reporting secondary persistence failures."""

    try:
        append_orchestration_event_fn(
            project_dir=project_dir,
            session_id=session_id,
            task_id=task_id,
            event_type=EventType.FAILURE_EVIDENCE_CAPTURED,
            details=evidence,
            phase=evidence.get("orchestration_phase"),
            coordinator="FailureCoordinator",
        )
    except Exception as evidence_error:
        logger.error(
            "[R4_FAILURE_OBSERVABILITY] event persistence failed; "
            "original_exception_type=%s task_execution_id=%s secondary_error=%s",
            evidence.get("exception_type"),
            task_execution_id,
            bounded_exception_message(evidence_error),
        )

    try:
        record_live_log_fn(
            db,
            session_id,
            task_id,
            "ERROR",
            "[ORCHESTRATION] Failure evidence captured before recovery/terminal handling",
            session_instance_id=session_instance_id,
            task_execution_id=task_execution_id,
            metadata=evidence,
        )
    except Exception as evidence_error:
        logger.error(
            "[R4_FAILURE_OBSERVABILITY] durable LogEntry persistence failed; "
            "original_exception_type=%s task_execution_id=%s secondary_error=%s",
            evidence.get("exception_type"),
            task_execution_id,
            bounded_exception_message(evidence_error),
        )


def record_outcome_checkpoint(
    *,
    ctx: Any,
    operation: str,
    status: str,
    append_orchestration_event_fn: Callable[..., Any],
    logger: Any,
    details: Optional[dict[str, Any]] = None,
) -> None:
    """Record a compact entry/success/failure checkpoint in the event journal."""

    state = getattr(ctx, "orchestration_state", None)
    task_execution_status = getattr(
        getattr(ctx, "task_execution", None), "status", None
    )
    if task_execution_status is None:
        task_execution_id = getattr(ctx, "task_execution_id", None)
        db = getattr(ctx, "db", None)
        if isinstance(task_execution_id, int) and db is not None:
            try:
                from app.models import TaskExecution

                task_execution = (
                    db.query(TaskExecution)
                    .filter(TaskExecution.id == task_execution_id)
                    .first()
                )
                task_execution_status = getattr(task_execution, "status", None)
            except Exception:
                task_execution_status = None

    orchestration_phase = (
        status_value(
            getattr(state, "current_phase", None) if state is not None else None
        )
        or "task_summary"
    )
    checkpoint = {
        "operation": operation,
        "status": status,
        "task_execution_id": getattr(ctx, "task_execution_id", None),
        "task_status": status_value(
            getattr(getattr(ctx, "task", None), "status", None)
        ),
        "session_task_status": status_value(
            getattr(getattr(ctx, "session_task_link", None), "status", None)
        ),
        "task_execution_status": status_value(task_execution_status),
        "orchestration_phase": orchestration_phase,
    }
    if details:
        checkpoint.update(details)
    if state is not None:
        phase_history = getattr(state, "phase_history", None)
        if isinstance(phase_history, list):
            phase_history.append(
                {
                    "phase": checkpoint["orchestration_phase"] or "task_summary",
                    "status": status,
                    "message": f"outcome checkpoint: {operation}",
                    "details": checkpoint,
                }
            )
    try:
        append_orchestration_event_fn(
            project_dir=_durable_project_dir(ctx, state),
            session_id=getattr(ctx, "session_id", None),
            task_id=getattr(ctx, "task_id", None),
            event_type=EventType.OUTCOME_CHECKPOINT,
            details=checkpoint,
            phase=checkpoint["orchestration_phase"] or "task_summary",
            coordinator="CompletionCoordinator",
        )
    except Exception as checkpoint_error:
        logger.warning(
            "[R4_OUTCOME_OBSERVABILITY] checkpoint persistence failed operation=%s "
            "status=%s task_execution_id=%s error=%s",
            operation,
            status,
            checkpoint["task_execution_id"],
            bounded_exception_message(checkpoint_error),
        )
    finally:
        logger.info(
            "[R4_OUTCOME_CHECKPOINT] %s",
            json.dumps(checkpoint, sort_keys=True, default=str),
        )
