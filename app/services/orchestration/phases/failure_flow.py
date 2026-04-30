"""Task failure and abort handling flow."""

import logging
import json
from datetime import UTC, datetime
from typing import Any, Callable, Optional

from app.models import LogEntry, TaskStatus
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.events.telemetry import record_phase_event
from app.services.orchestration.execution.runtime import write_project_state_snapshot
from app.services.orchestration.persistence import (
    append_orchestration_event,
    record_live_log,
    save_orchestration_checkpoint,
    set_session_alert,
)
from app.services.orchestration.types import OrchestrationRunContext
from app.services.prompt_templates import OrchestrationStatus


def handle_task_failure(
    *,
    self_task: Any,
    ctx: Optional[OrchestrationRunContext],
    exc: Exception,
    get_latest_session_task_link_fn: Callable[..., Any],
    write_project_state_snapshot_fn: Callable[..., None] = write_project_state_snapshot,
    save_orchestration_checkpoint_fn: Callable[
        ..., None
    ] = save_orchestration_checkpoint,
    record_live_log_fn: Callable[..., None] = record_live_log,
    queue_task_for_session_fn: Optional[Callable[..., Any]] = None,
) -> None:
    db = ctx.db if ctx else None
    session = ctx.session if ctx else None
    project = ctx.project if ctx else None
    task = ctx.task if ctx else None
    session_task_link = ctx.session_task_link if ctx else None
    session_id = ctx.session_id if ctx else None
    task_id = ctx.task_id if ctx else None
    prompt = ctx.prompt if ctx else ""
    orchestration_state = ctx.orchestration_state if ctx else None
    restore_workspace_snapshot_if_needed = (
        ctx.restore_workspace_snapshot_if_needed if ctx else None
    )
    logger = ctx.logger if ctx else logging.getLogger(__name__)
    error_handler = ctx.error_handler if ctx else None

    should_retry = (
        error_handler.should_retry(exc, "task_execution") if error_handler else False
    )
    retry_count = int(getattr(getattr(self_task, "request", None), "retries", 0) or 0)
    max_retries = int(getattr(self_task, "max_retries", 0) or 0)
    has_retry_capacity = should_retry and retry_count < max_retries
    is_timeout = (
        "time limit" in str(exc).lower()
        or "timeout" in str(exc).lower()
        or "timed out" in str(exc).lower()
    )
    non_restoring_failure_markers = (
        "completion validation failed",
        "baseline publish validation failed",
        "completion repair failed",
    )
    should_restore_workspace = not any(
        marker in str(exc).lower() for marker in non_restoring_failure_markers
    )

    auto_recovery_eligible = bool(
        session
        and task
        and session.execution_mode == "automatic"
        and getattr(task, "plan_position", None) is not None
        and not is_timeout
        and getattr(task, "workspace_status", None) != "changes_requested"
    )

    if task:
        task.status = TaskStatus.FAILED
        task.error_message = str(exc)
        task.completed_at = datetime.now(UTC)
        task.workspace_status = "blocked" if task.task_subfolder else "not_created"

    if orchestration_state and session_id and task_id:
        try:
            append_orchestration_event(
                project_dir=orchestration_state.project_dir,
                session_id=session_id,
                task_id=task_id,
                event_type=EventType.TASK_FAILED,
                details={"error": str(exc)},
            )
        except Exception:
            pass

    if not session_task_link:
        session_task_link = get_latest_session_task_link_fn(db, session_id, task_id)
    if session_task_link and task:
        session_task_link.status = TaskStatus.FAILED
        session_task_link.completed_at = task.completed_at

    error_str = str(exc).lower()
    if "json" in error_str or "parse" in error_str:
        if task:
            task.error_message += "\nDiagnosis: JSON parsing error detected"
            task.error_message += "\nSuggested fix: Check AI agent response format"
    elif "empty" in error_str:
        if task:
            task.error_message += "\nDiagnosis: Empty response from AI agent"
            task.error_message += "\nSuggested fix: Retry with more specific prompt"

    alert_message = (
        f"Task {task_id} failed in {session.execution_mode if session else 'session'} mode: {str(exc)}"
        if session
        else f"Task {task_id} failed: {str(exc)}"
    )

    if session:
        session.status = "paused"
        session.is_active = False
        set_session_alert(session, "error", alert_message[:2000])

    if is_timeout and task:
        task.error_message += " (Task timed out after 5 minutes)"
        task.error_message += "\nSuggested fix: Break task into smaller steps"

    try:
        if orchestration_state:
            orchestration_state.status = OrchestrationStatus.ABORTED
            orchestration_state.abort_reason = str(exc)
            record_phase_event(
                orchestration_state,
                phase="failure",
                status="error",
                message=f"[ORCHESTRATION] Task {task_id} failed: {exc}",
                details={"retryable": should_retry, "is_timeout": is_timeout},
            )
            save_orchestration_checkpoint_fn(
                db,
                session_id,
                task_id,
                prompt,
                orchestration_state,
                checkpoint_name="autosave_error",
            )
            record_live_log_fn(
                db,
                session_id,
                task_id,
                "WARN",
                "[CHECKPOINT] Error checkpoint saved for resume",
                session_instance_id=session.instance_id if session else None,
                metadata={"checkpoint_name": "autosave_error"},
            )
    except Exception as checkpoint_error:
        logger.error(
            "[CHECKPOINT] Failed to save error checkpoint for task %s: %s",
            task_id,
            str(checkpoint_error),
        )

    if has_retry_capacity and session and task:
        task.status = TaskStatus.RUNNING
        task.completed_at = None
        task.workspace_status = "in_progress" if task.task_subfolder else "not_created"
        if session_task_link:
            session_task_link.status = TaskStatus.RUNNING
            session_task_link.completed_at = None
        session.status = "running"
        session.is_active = True
        set_session_alert(
            session,
            "warning",
            (
                f"Retrying task {task_id} automatically after failure "
                f"({retry_count + 1}/{max_retries + 1})"
            )[:2000],
        )
        db.commit()
        raise self_task.retry(exc=exc)

    if auto_recovery_eligible and queue_task_for_session_fn and session and task:
        recovery_message = (
            "Automatic recovery queued for failed ordered task. "
            "The next run will inspect the real workspace first and fix the underlying issue."
        )
        task.status = TaskStatus.PENDING
        task.started_at = None
        task.completed_at = None
        task.current_step = 0
        task.steps = None
        task.workspace_status = "changes_requested"
        task.error_message = (
            f"{str(exc)}\n\n"
            "Automatic recovery requested: inspect the real workspace and repair the bug "
            "instead of repeating the previous assumptions."
        )[:4000]
        if session_task_link:
            session_task_link.status = TaskStatus.PENDING
            session_task_link.started_at = None
            session_task_link.completed_at = None
        session.status = "running"
        session.is_active = True
        set_session_alert(session, "warning", recovery_message[:2000])
        db.commit()
        try:
            queue_task_for_session_fn(db=db, session=session, task_id=task.id)
            record_live_log_fn(
                db,
                session_id,
                task_id,
                "WARN",
                "[ORCHESTRATION] Ordered task failed; queued one automatic recovery rerun with repair context",
                session_instance_id=session.instance_id if session else None,
                metadata={
                    "phase": "failure",
                    "automatic_recovery": True,
                    "retry_count": retry_count,
                },
            )
            db.commit()
            write_project_state_snapshot_fn(db, project, task, session_id)
            return
        except Exception as recovery_queue_error:
            logger.error(
                "[ORCHESTRATION] Failed to queue automatic recovery for task %s: %s",
                task_id,
                recovery_queue_error,
            )
            task.status = TaskStatus.FAILED
            task.workspace_status = "blocked" if task.task_subfolder else "not_created"
            session.status = "paused"
            session.is_active = False
            set_session_alert(
                session,
                "error",
                (
                    f"{alert_message}. Automatic recovery could not be queued: "
                    f"{str(recovery_queue_error)}"
                )[:2000],
            )
            db.commit()

    try:
        if (
            project
            and orchestration_state
            and restore_workspace_snapshot_if_needed
            and should_restore_workspace
        ):
            restore_workspace_snapshot_if_needed("task exception")
    except Exception as restore_error:
        logger.error(
            "[ORCHESTRATION] Failed to restore pre-run workspace snapshot for task %s: %s",
            task_id,
            str(restore_error),
        )

    if not should_restore_workspace:
        logger.warning(
            "[ORCHESTRATION] Skipped workspace restore for task %s because the failure was a completion/baseline validation issue",
            task_id,
        )

    db.commit()
    write_project_state_snapshot_fn(db, project, task, session_id)

    if session:
        db.add(
            LogEntry(
                session_id=session_id,
                session_instance_id=session.instance_id,
                task_id=task_id,
                level="ERROR",
                message=alert_message[:2000],
                log_metadata=json.dumps(
                    {
                        "alarm": True,
                        "execution_mode": session.execution_mode,
                        "task_id": task_id,
                    }
                ),
            )
        )
        db.commit()

    logger.error("[ORCHESTRATION] Task %s failed: %s", task_id, str(exc))
    if is_timeout:
        logger.warning(
            "[ORCHESTRATION] Task exceeded time limit - this prevents hanging tasks"
        )

    if is_timeout:
        raise exc

    raise exc
