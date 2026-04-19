"""Task failure and abort handling flow."""

import logging
from datetime import datetime
from typing import Any, Callable, Optional

from app.models import TaskStatus
from app.services.orchestration.persistence import (
    record_live_log,
    save_orchestration_checkpoint,
    set_session_alert,
)
from app.services.orchestration.runtime import write_project_state_snapshot
from app.services.prompt_templates import OrchestrationStatus


def handle_task_failure(
    *,
    self_task: Any,
    db: Any,
    session: Any,
    project: Any,
    task: Any,
    session_task_link: Any,
    session_id: int,
    task_id: int,
    prompt: str,
    exc: Exception,
    orchestration_state: Any,
    restore_workspace_snapshot_if_needed: Optional[Callable[[str], None]],
    logger: logging.Logger,
    error_handler: Any,
    get_latest_session_task_link_fn: Callable[..., Any],
    write_project_state_snapshot_fn: Callable[..., None] = write_project_state_snapshot,
    save_orchestration_checkpoint_fn: Callable[
        ..., None
    ] = save_orchestration_checkpoint,
    record_live_log_fn: Callable[..., None] = record_live_log,
) -> None:
    should_retry = error_handler.should_retry(exc, "task_execution")
    is_timeout = "time limit" in str(exc).lower() or "timeout" in str(exc).lower()

    if task:
        task.status = TaskStatus.FAILED
        task.error_message = str(exc)
        task.completed_at = datetime.utcnow()
        task.workspace_status = "blocked" if task.task_subfolder else "not_created"

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

    try:
        if project and orchestration_state and restore_workspace_snapshot_if_needed:
            restore_workspace_snapshot_if_needed("task exception")
    except Exception as restore_error:
        logger.error(
            "[ORCHESTRATION] Failed to restore pre-run workspace snapshot for task %s: %s",
            task_id,
            str(restore_error),
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

    raise self_task.retry(exc=exc)
