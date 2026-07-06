"""FailureCoordinator — owns the task failure lifecycle.

Phase 14B-2: Extracts handle_task_failure from failure_flow.py into a single,
owned orchestration surface.

Orchestration decisions live here. Algorithm helpers remain in failure_flow.py.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Callable, Optional

from app.models import InterventionRequest, LogEntry, TaskExecution, TaskStatus
from app.runtime_naming import (
    BOUNDED_DEBUG_REPAIR_TIMEOUT_REASON,
    LEGACY_BOUNDED_DEBUG_REPAIR_TIMEOUT_REASON,
    bounded_debug_repair_timeout_alias_details,
)
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.events.telemetry import record_phase_event
from app.services.orchestration.execution.runtime import write_project_state_snapshot
from app.services.orchestration.run_state import (
    mark_task_attempt_failed,
    mark_task_attempt_pending,
)
from app.services.orchestration.state.persistence import (
    record_live_log,
    save_orchestration_checkpoint,
)
from app.services.orchestration.state.session_state import (
    mark_session_paused,
    mark_session_running,
)
from app.services.orchestration.types import OrchestrationRunContext
from app.services.orchestration.prompt_templates import OrchestrationStatus
from app.services.workspace.project_mutation_lock import ProjectMutationLockError


class FailureCoordinator:
    """Single orchestration boundary for task failure handling.

    Owns: failure classification, retry routing, workspace restore preparation,
    knowledge halt, auto recovery queuing, session pause/terminal finalization.
    Delegates: append_orchestration_event, _apply_knowledge_halt,
    _prepare_retry_workspace, and other helpers to failure_flow.py.
    """

    def handle_failure(
        self,
        *,
        self_task: Any,
        ctx: Optional[OrchestrationRunContext],
        exc: Exception,
        get_latest_session_task_link_fn: Callable[..., Any],
        write_project_state_snapshot_fn: Callable[
            ..., None
        ] = write_project_state_snapshot,
        save_orchestration_checkpoint_fn: Callable[
            ..., None
        ] = save_orchestration_checkpoint,
        record_live_log_fn: Callable[..., None] = record_live_log,
        queue_task_for_session_fn: Optional[Callable[..., Any]] = None,
    ) -> None:
        # Deferred imports from failure_flow so that test patches on
        # failure_flow.* are respected at call time.
        from app.services.orchestration.phases.failure_flow import (
            DIRTY_RETRY_CHECKPOINT_NAME,
            _apply_knowledge_halt,
            _is_bounded_debug_repair_timeout,
            _prepare_retry_workspace,
            _session_has_other_active_execution,
            _task_execution_for_context,
            append_orchestration_event,
        )

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

        # ── Phase 17A/17B: classify failure + route through recovery registry ───
        try:
            import asyncio
            import concurrent.futures

            from app.services.orchestration.recovery.failure_classifier import (
                FailureClassifier,
            )
            from app.services.orchestration.recovery.recovery_strategy_registry import (
                RecoveryStrategyRegistry,
            )

            _failure_event = FailureClassifier.classify(
                exc,
                orchestration_state,
                session_id=session_id,
                task_id=task_id,
            )

            # 17B: build a sync LLM callable for reflection retry when runtime is available.
            _llm_callable = None
            _runtime = getattr(ctx, "runtime_service", None) if ctx else None
            if _runtime is not None:

                def _reflection_llm_callable(_prompt: str) -> str:
                    with concurrent.futures.ThreadPoolExecutor(max_workers=1) as _ex:
                        _res = _ex.submit(
                            asyncio.run,
                            _runtime.invoke_prompt(
                                _prompt,
                                timeout_seconds=60,
                                source_brain="local",
                                session_prefix="reflection",
                            ),
                        ).result()
                    return str(_res.get("output", ""))

                _llm_callable = _reflection_llm_callable

            _recovery_decision = RecoveryStrategyRegistry.route(
                _failure_event,
                project_dir=getattr(orchestration_state, "project_dir", None),
                session_id=session_id,
                task_id=task_id,
                orchestration_state=orchestration_state,
                llm_callable=_llm_callable,
            )

            # 17A-6: wrapper_timeout_noise → annotate_and_continue
            # Timeout fired after the task already reached terminal state (DONE).
            # Treat as watchdog noise — do not mark task failed, do not re-raise.
            if _recovery_decision.strategy == "annotate_and_continue":
                logger.info(
                    "[17A] wrapper_timeout_noise annotated; not propagating as task "
                    "failure (session_id=%s task_id=%s)",
                    session_id,
                    task_id,
                )
                return
        except Exception as _17a_exc:
            logger.debug("[17A/17B] classifier/registry raised: %s", _17a_exc)

        should_retry = (
            error_handler.should_retry(exc, "task_execution")
            if error_handler
            else False
        )
        retry_count = int(
            getattr(getattr(self_task, "request", None), "retries", 0) or 0
        )
        max_retries = int(getattr(self_task, "max_retries", 0) or 0)
        runtime_diagnostics = getattr(exc, "runtime_diagnostics", None) or {}
        is_bounded_debug_repair_timeout = _is_bounded_debug_repair_timeout(
            exc, runtime_diagnostics
        )

        is_planning_lock_wait_timeout = runtime_diagnostics.get(
            "timeout_boundary"
        ) == "planning_lock_wait" or "OpenClaw planning lock wait timed out" in str(exc)
        is_project_mutation_lock_conflict = isinstance(exc, ProjectMutationLockError)
        has_retry_capacity = (
            should_retry
            and retry_count < max_retries
            and not is_bounded_debug_repair_timeout
            and not is_planning_lock_wait_timeout
            and not is_project_mutation_lock_conflict
        )
        is_timeout = (
            "time limit" in str(exc).lower()
            or "timeout" in str(exc).lower()
            or "timed out" in str(exc).lower()
        )
        diagnostic_reason = None
        if is_project_mutation_lock_conflict:
            diagnostic_reason = "project_mutation_lock_conflict"
        elif is_bounded_debug_repair_timeout:
            diagnostic_reason = BOUNDED_DEBUG_REPAIR_TIMEOUT_REASON
        elif is_planning_lock_wait_timeout:
            diagnostic_reason = "planning_openclaw_lock_contention"
        elif is_timeout:
            diagnostic_reason = "openclaw_timeout"
        elif "parse" in str(exc).lower():
            diagnostic_reason = "debug_parse_error"

        non_restoring_failure_markers = (
            "completion validation failed",
            "baseline publish validation failed",
            "completion repair failed",
        )
        should_restore_workspace = (
            not any(
                marker in str(exc).lower() for marker in non_restoring_failure_markers
            )
            and not is_bounded_debug_repair_timeout
        )

        auto_recovery_eligible = bool(
            session
            and task
            and session.execution_mode == "automatic"
            and getattr(task, "plan_position", None) is not None
            and not is_timeout
            and getattr(task, "workspace_status", None) != "changes_requested"
        )

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
        completed_at = datetime.now(UTC)
        task_execution = _task_execution_for_context(db, ctx)
        task_execution_id = task_execution.id if task_execution else None
        mark_task_attempt_failed(
            task=task,
            session_task_link=session_task_link,
            task_execution=task_execution,
            error_message=str(exc),
            completed_at=completed_at,
            workspace_status=(
                "blocked" if task and task.task_subfolder else "not_created"
            ),
        )

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

        other_active_execution = _session_has_other_active_execution(
            db,
            session_id=session_id,
            current_task_execution_id=task_execution_id,
        )
        if session:
            if other_active_execution:
                mark_session_running(
                    session, alert_level="warning", alert_message=alert_message[:2000]
                )
            else:
                mark_session_paused(
                    session, alert_level="error", alert_message=alert_message[:2000]
                )

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
                    details={
                        "retryable": has_retry_capacity,
                        "error_handler_retryable": should_retry,
                        "is_timeout": is_timeout,
                        **bounded_debug_repair_timeout_alias_details(
                            is_bounded_debug_repair_timeout
                        ),
                        "planning_lock_wait_timeout": is_planning_lock_wait_timeout,
                        "project_mutation_lock_conflict": is_project_mutation_lock_conflict,
                        "reason": diagnostic_reason,
                        "reason_architecture": (
                            BOUNDED_DEBUG_REPAIR_TIMEOUT_REASON
                            if diagnostic_reason
                            in {
                                LEGACY_BOUNDED_DEBUG_REPAIR_TIMEOUT_REASON,
                                BOUNDED_DEBUG_REPAIR_TIMEOUT_REASON,
                            }
                            else diagnostic_reason
                        ),
                    },
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

        knowledge_halted = _apply_knowledge_halt(
            ctx=ctx,
            exc=exc,
            retry_count=retry_count,
            session_id=session_id,
            task_id=task_id,
            logger=logger,
        )

        if not knowledge_halted and has_retry_capacity and session and task:
            retry_workspace_restored = False
            retry_kwargs = None
            retry_restore_blocked = False
            if ctx is not None:
                (
                    retry_workspace_restored,
                    retry_kwargs,
                    retry_restore_blocked,
                ) = _prepare_retry_workspace(
                    ctx=ctx,
                    exc=exc,
                    restore_workspace_snapshot_if_needed=restore_workspace_snapshot_if_needed,
                    record_live_log_fn=record_live_log_fn,
                    logger=logger,
                    self_task=self_task,
                )
            if retry_restore_blocked:
                retry_blocked_message = (
                    "Retry requires checkpoint resume because workspace restore failed "
                    "or the workspace remained dirty after failure."
                )
                mark_task_attempt_failed(
                    task=task,
                    session_task_link=session_task_link,
                    task_execution=task_execution,
                    error_message=retry_blocked_message,
                    completed_at=completed_at,
                    workspace_status=(
                        "blocked" if task and task.task_subfolder else "not_created"
                    ),
                )
                mark_session_paused(
                    session,
                    alert_level="error",
                    alert_message=retry_blocked_message[:2000],
                )
                db.commit()
                write_project_state_snapshot_fn(db, project, task, session_id)
                return
            mark_task_attempt_pending(
                task=task,
                session_task_link=session_task_link,
                workspace_status=(
                    "in_progress" if task.task_subfolder else "not_created"
                ),
                error_message=(
                    None
                    if retry_workspace_restored
                    else (
                        "Retry requires checkpoint resume because the workspace could "
                        "not be restored cleanly after failure."
                    )
                ),
            )
            mark_session_running(
                session,
                alert_level="warning",
                alert_message=(
                    f"Retrying task {task_id} automatically after failure "
                    f"({retry_count + 1}/{max_retries + 1})"
                )[:2000],
            )
            db.commit()
            if retry_kwargs is not None:
                raise self_task.retry(exc=exc, kwargs=retry_kwargs)
            raise self_task.retry(exc=exc)

        if (
            not knowledge_halted
            and auto_recovery_eligible
            and queue_task_for_session_fn
            and session
            and task
        ):
            recovery_message = (
                "Automatic recovery queued for failed ordered task. "
                "The next run will inspect the real workspace first and fix the underlying issue."
            )
            recovery_error_message = (
                f"{str(exc)}\n\n"
                "Automatic recovery requested: inspect the real workspace and repair the bug "
                "instead of repeating the previous assumptions."
            )[:4000]
            mark_task_attempt_pending(
                task=task,
                session_task_link=session_task_link,
                reset_started_at=True,
                reset_steps=True,
                workspace_status="changes_requested",
                error_message=recovery_error_message,
            )
            mark_session_running(
                session, alert_level="warning", alert_message=recovery_message[:2000]
            )
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
                mark_task_attempt_failed(
                    task=task,
                    session_task_link=session_task_link,
                    task_execution=task_execution,
                    error_message=f"{str(exc)} | recovery queue error: {str(recovery_queue_error)}",
                    completed_at=datetime.now(UTC),
                    workspace_status=(
                        "blocked" if task.task_subfolder else "not_created"
                    ),
                )
                mark_session_paused(
                    session,
                    alert_level="error",
                    alert_message=(
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
                            "reason": diagnostic_reason,
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
