"""Task failure and abort handling flow."""

import logging
import json
from datetime import UTC, datetime
from typing import Any, Callable, Optional

from app.models import InterventionRequest, LogEntry, TaskExecution, TaskStatus
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.events.telemetry import record_phase_event
from app.services.orchestration.execution.runtime import write_project_state_snapshot
from app.services.orchestration.state.persistence import (
    append_orchestration_event,
    read_orchestration_events,
    record_live_log,
    save_orchestration_checkpoint,
)
from app.services.orchestration.run_state import (
    mark_task_attempt_failed,
    mark_task_attempt_pending,
    task_execution_id_from_context,
)
from app.runtime_naming import (
    BOUNDED_DEBUG_REPAIR_TIMEOUT_REASON,
    LEGACY_BOUNDED_DEBUG_REPAIR_TIMEOUT_REASON,
    bounded_debug_repair_timeout_alias_details,
    is_bounded_debug_repair_mode,
)
from app.services.orchestration.state.execution_states import OrchestrationPhase
from app.services.orchestration.state.session_state import (
    mark_session_paused,
    mark_session_running,
)
from app.services.orchestration.types import OrchestrationRunContext
from app.services.workspace.project_mutation_lock import ProjectMutationLockError
from app.services.orchestration.prompt_templates import OrchestrationStatus

DIRTY_RETRY_CHECKPOINT_NAME = "autosave_error"
_KNOWLEDGE_HALT_MIN_CONFIDENCE = 0.95

# Administrative events written by handle_task_failure before _prepare_retry_workspace
# is called. They do not indicate planning-phase entry or source mutation.
_HANDLE_TASK_FAILURE_ADMIN_EVENTS = frozenset(
    {
        EventType.TASK_FAILED,
        EventType.CHECKPOINT_SAVED,
        EventType.HEALTH_SCORE_UPDATED,
    }
)


def _task_execution_for_context(
    db: Any,
    ctx: Optional[Any],
) -> Optional[TaskExecution]:
    task_execution_id = task_execution_id_from_context(ctx)
    if task_execution_id is None:
        return None
    return db.query(TaskExecution).filter(TaskExecution.id == task_execution_id).first()


def _session_has_other_active_execution(
    db: Any,
    *,
    session_id: Optional[int],
    current_task_execution_id: Optional[int],
) -> bool:
    if session_id is None:
        return False
    query = db.query(TaskExecution).filter(
        TaskExecution.session_id == session_id,
        TaskExecution.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]),
    )
    if current_task_execution_id is not None:
        query = query.filter(TaskExecution.id != current_task_execution_id)
    return query.first() is not None


def _retry_request_kwargs(self_task: Any) -> Optional[dict[str, Any]]:
    request = getattr(self_task, "request", None)
    kwargs = getattr(request, "kwargs", None)
    if not isinstance(kwargs, dict):
        return None
    return dict(kwargs)


def _has_no_orchestration_events_for_retry(
    *,
    orchestration_state: Any,
    session_id: Optional[int],
    task_id: Optional[int],
) -> bool:
    """Return True when no meaningful (planning/execution) events exist for this task.

    snapshot_missing + no meaningful events = pre-planning setup failure, no source
    mutation evidence. Admin events written by handle_task_failure itself
    (TASK_FAILED, CHECKPOINT_SAVED, HEALTH_SCORE_UPDATED) are excluded because they
    are always present before this check runs and carry no signal about source mutations.
    Returns False on any read error so the existing blocking behavior is preserved.
    """
    if orchestration_state is None or session_id is None or task_id is None:
        return False
    try:
        events = read_orchestration_events(
            orchestration_state.project_dir,
            session_id,
            task_id,
        )
        meaningful_events = [
            event
            for event in events
            if event.get("event_type") not in _HANDLE_TASK_FAILURE_ADMIN_EVENTS
        ]
        return len(meaningful_events) == 0
    except Exception:
        return False


def _prepare_retry_workspace(
    *,
    ctx: OrchestrationRunContext,
    exc: Exception,
    restore_workspace_snapshot_if_needed: Optional[Callable[..., Any]],
    record_live_log_fn: Callable[..., None],
    logger: logging.Logger,
    self_task: Any,
) -> tuple[bool, Optional[dict[str, Any]], bool]:
    """Prepare workspace before Celery retry.

    Returns (workspace_restored, retry_kwargs, restore_blocked_retry).
    restore_blocked_retry is true when an attempted restore failed or left the
    workspace dirty enough that immediate retry would be unsafe.
    """

    db = ctx.db
    session = ctx.session
    session_id = ctx.session_id
    task_id = ctx.task_id
    orchestration_state = ctx.orchestration_state
    retry_details = {
        "phase": "failure",
        "reason": "retryable_task_failure",
        "error": str(exc)[:500],
        "checkpoint_name": DIRTY_RETRY_CHECKPOINT_NAME,
    }
    restore_result = None

    if restore_workspace_snapshot_if_needed:
        try:
            restore_result = restore_workspace_snapshot_if_needed(
                "retryable task failure",
                force_restore=True,
            )
        except TypeError:
            restore_result = restore_workspace_snapshot_if_needed(
                "retryable task failure"
            )
        except Exception as restore_exc:
            restore_result = {
                "restored": False,
                "reason": f"restore_failed:{str(restore_exc)[:200]}",
            }
            logger.warning(
                "[ORCHESTRATION] Workspace restore before retry failed for task %s: %s",
                task_id,
                restore_exc,
            )

    if restore_result and restore_result.get("restored"):
        record_live_log_fn(
            db,
            session_id,
            task_id,
            "WARN",
            "[ORCHESTRATION] Restored workspace snapshot before retrying failed task",
            session_instance_id=session.instance_id if session else None,
            metadata={**retry_details, "restore_result": restore_result},
        )
        return True, None, False

    # snapshot_missing + zero events = pre-planning setup failure, no source mutation evidence.
    # The workspace is identical to its pre-run state; direct retry is safe without restore.
    if (
        restore_result is not None
        and not restore_result.get("restored")
        and restore_result.get("reason") == "snapshot_missing"
        and _has_no_orchestration_events_for_retry(
            orchestration_state=orchestration_state,
            session_id=session_id,
            task_id=task_id,
        )
    ):
        record_live_log_fn(
            db,
            session_id,
            task_id,
            "WARN",
            "[ORCHESTRATION] snapshot_missing with no prior events; workspace unmodified — allowing direct retry",
            session_instance_id=session.instance_id if session else None,
            metadata={
                **retry_details,
                "restore_result": restore_result,
                "retry_exemption": "snapshot_missing_no_events",
            },
        )
        return False, None, False

    dirty_details = {
        **retry_details,
        "restore_result": restore_result,
        "retry_mode": "checkpoint_resume_required",
    }
    if orchestration_state is not None:
        try:
            append_orchestration_event(
                project_dir=orchestration_state.project_dir,
                session_id=session_id,
                task_id=task_id,
                event_type=EventType.WORKSPACE_RETRY_DIRTY,
                details=dirty_details,
            )
        except Exception:
            pass
    record_live_log_fn(
        db,
        session_id,
        task_id,
        "WARN",
        "[ORCHESTRATION] Retryable failure left workspace un-restored; retry will resume from autosave_error checkpoint",
        session_instance_id=session.instance_id if session else None,
        metadata=dirty_details,
    )

    retry_kwargs = _retry_request_kwargs(self_task)
    if retry_kwargs is not None:
        retry_kwargs["resume_checkpoint_name"] = DIRTY_RETRY_CHECKPOINT_NAME
        retry_kwargs["queued_event_id"] = None
    return False, retry_kwargs, restore_result is not None


def _is_bounded_debug_repair_timeout(
    exc: Exception, runtime_diagnostics: dict[str, Any]
) -> bool:
    """Return true only for bounded source-step debug repair timeouts."""

    debug_prompt_mode = runtime_diagnostics.get("debug_prompt_mode")
    debug_prompt_mode_architecture = runtime_diagnostics.get(
        "debug_prompt_mode_architecture"
    )
    if debug_prompt_mode_architecture is not None:
        is_bounded_debug_repair = is_bounded_debug_repair_mode(
            debug_prompt_mode_architecture
        )
    else:
        is_bounded_debug_repair = is_bounded_debug_repair_mode(debug_prompt_mode)
    if not is_bounded_debug_repair:
        return False
    if runtime_diagnostics.get("failure_phase") != OrchestrationPhase.DEBUG_REPAIR:
        return False
    if runtime_diagnostics.get("debug_failure_class") != "source_step_validation":
        return False
    if runtime_diagnostics.get("timed_out") is True:
        return True
    return "timed out" in str(exc).lower() or "timeout" in str(exc).lower()


def _knowledge_context_can_halt(knowledge_ctx: Any) -> bool:
    """Return True only for high-confidence failure memory halt signals."""

    retrieved_items = list(getattr(knowledge_ctx, "retrieved_items", []) or [])
    if not retrieved_items:
        return False

    top_item = retrieved_items[0]
    if str(getattr(top_item, "knowledge_type", "")) != "failure_memory":
        return False

    recommended_action = getattr(knowledge_ctx, "recommended_action", "")
    if str(getattr(recommended_action, "value", recommended_action)) != "stop_retry":
        return False

    try:
        confidence = float(getattr(top_item, "confidence", 0.0) or 0.0)
    except (TypeError, ValueError):
        return False
    return confidence >= _KNOWLEDGE_HALT_MIN_CONFIDENCE


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
    """Shim — failure lifecycle is owned by FailureCoordinator."""
    from app.services.orchestration.coordinators.failure_coordinator import (
        FailureCoordinator,
    )

    return FailureCoordinator().handle_failure(
        self_task=self_task,
        ctx=ctx,
        exc=exc,
        get_latest_session_task_link_fn=get_latest_session_task_link_fn,
        write_project_state_snapshot_fn=write_project_state_snapshot_fn,
        save_orchestration_checkpoint_fn=save_orchestration_checkpoint_fn,
        record_live_log_fn=record_live_log_fn,
        queue_task_for_session_fn=queue_task_for_session_fn,
    )


def _apply_knowledge_halt(
    *,
    ctx: Optional[Any],
    exc: Exception,
    retry_count: int,
    session_id: Optional[int],
    task_id: Optional[int],
    logger: logging.Logger,
) -> bool:
    """Return True and create InterventionRequest when a known failure memory says stop.

    Wraps all knowledge calls in try/except so failures here never break the normal
    retry path.
    """
    if ctx is None:
        return False
    db = getattr(ctx, "db", None)
    task = getattr(ctx, "task", None)
    project = getattr(ctx, "project", None)
    orchestration_state = getattr(ctx, "orchestration_state", None)

    if db is None or task is None or project is None:
        return False

    try:
        from app.config import settings
        from app.services.knowledge import failure_signature_service, usage_log_service
        from app.services.knowledge.knowledge_service import KnowledgeService

        phase = getattr(orchestration_state, "current_phase", None) or "execution"
        sig = failure_signature_service.extract(
            exc=exc,
            phase=phase,
            tool_name=None,
            retry_count=retry_count,
        )

        svc = KnowledgeService(
            qdrant_url=settings.QDRANT_URL,
            collection_name=settings.QDRANT_COLLECTION_NAME,
        )
        knowledge_ctx = svc.retrieve(
            query=sig.normalized_message,
            trigger_phase="failure",
            knowledge_types=["failure_memory", "debug_case"],
            failure_signature=sig.signature_hash(),
            db=db,
        )
        usage_log_service.log_usage(
            context=knowledge_ctx,
            session_id=session_id,
            task_id=task_id,
            used_in_prompt=False,
            db=db,
        )

        if _knowledge_context_can_halt(knowledge_ctx) and retry_count >= 2:
            top_title = (
                knowledge_ctx.retrieved_items[0].title
                if knowledge_ctx.retrieved_items
                else "known failure"
            )
            prompt_body = (
                f"Task halted after {retry_count} retries: matched known failure memory "
                f"'{top_title}'. Recommended action: {knowledge_ctx.recommended_action.value}."
            )
            db.add(
                InterventionRequest(
                    session_id=session_id,
                    task_id=task_id,
                    project_id=project.id,
                    intervention_type="guidance",
                    initiated_by="ai",
                    prompt=prompt_body,
                )
            )
            task_execution = None
            task_execution_id = task_execution_id_from_context(ctx)
            if task_execution_id:
                task_execution = (
                    db.query(TaskExecution)
                    .filter(TaskExecution.id == task_execution_id)
                    .first()
                )
            mark_task_attempt_failed(
                task=task,
                session_task_link=getattr(ctx, "session_task_link", None),
                task_execution=task_execution,
                error_message=prompt_body,
                completed_at=datetime.now(UTC),
            )
            db.commit()
            logger.warning(
                "[KNOWLEDGE] Halt: matched failure memory '%s' at retry_count=%d; "
                "InterventionRequest created",
                top_title,
                retry_count,
            )
            return True

    except Exception as knowledge_exc:
        logger.warning(
            "[KNOWLEDGE] Halt check skipped session=%s task=%s: %s",
            session_id,
            task_id,
            knowledge_exc,
        )

    return False


def record_failure_knowledge_for_stopped_session(
    *,
    db: Any,
    session_id: int,
    task_id: int,
    failure_reason: str,
    logger: logging.Logger,
) -> bool:
    """Record KnowledgeUsageLog for a session stopped by a runtime failure.

    Called from stop paths that bypass handle_task_failure() (orphan recovery,
    hard time-limit kill). Never modifies task or session status.
    """
    try:
        from app.config import settings
        from app.services.knowledge import failure_signature_service, usage_log_service
        from app.services.knowledge.knowledge_service import KnowledgeService

        sig = failure_signature_service.extract(
            exc=RuntimeError(failure_reason),
            phase="execution",
            tool_name=None,
            retry_count=0,
        )
        svc = KnowledgeService(
            qdrant_url=settings.QDRANT_URL,
            collection_name=settings.QDRANT_COLLECTION_NAME,
        )
        knowledge_ctx = svc.retrieve(
            query=sig.normalized_message,
            trigger_phase="failure",
            knowledge_types=["failure_memory", "debug_case"],
            failure_signature=sig.signature_hash(),
            db=db,
        )
        usage_log_service.log_usage(
            context=knowledge_ctx,
            session_id=session_id,
            task_id=task_id,
            used_in_prompt=False,
            db=db,
        )
        logger.info(
            "[KNOWLEDGE] Recorded failure knowledge for stopped session=%s task=%s "
            "items=%d retrieval_reason=%s",
            session_id,
            task_id,
            len(knowledge_ctx.retrieved_items),
            knowledge_ctx.retrieval_reason,
        )
        return True
    except Exception as record_exc:
        logger.warning(
            "[KNOWLEDGE] record_failure_knowledge_for_stopped_session failed "
            "session=%s task=%s: %s",
            session_id,
            task_id,
            record_exc,
        )
        return False
