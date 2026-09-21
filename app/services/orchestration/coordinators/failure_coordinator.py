"""FailureCoordinator — owns the task failure lifecycle.

Phase 14B-2: Extracts handle_task_failure from failure_flow.py into a single,
owned orchestration surface.

Orchestration decisions live here. Algorithm helpers remain in failure_flow.py.
"""

from __future__ import annotations

import asyncio
import concurrent.futures
import json
import logging
from datetime import UTC, datetime, timedelta
from typing import Any, Callable, Optional

from app.models import InterventionRequest, LogEntry, TaskExecution, TaskStatus
from app.runtime_naming import (
    BOUNDED_DEBUG_REPAIR_TIMEOUT_REASON,
    LEGACY_BOUNDED_DEBUG_REPAIR_TIMEOUT_REASON,
    bounded_debug_repair_timeout_alias_details,
)
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.events.telemetry import record_phase_event
from app.services.orchestration.diagnostics.outcome_observability import (
    build_failure_evidence,
    persist_failure_evidence,
    status_value,
)
from app.services.orchestration.execution.runtime import write_project_state_snapshot
from app.services.orchestration.run_state import (
    mark_task_attempt_failed,
    mark_task_attempt_pending,
)
from app.services.orchestration.lifecycle.transitions import (
    ContinuationIdentity,
    LifecycleTransitionError,
    enter_recovering,
    finalize_logical_failure,
    revoke_autonomous_continuation,
    schedule_continuation,
)
from app.services.tasks.execution import create_task_execution
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
from app.services.workspace.control_state_paths import control_state_of
from app.services.agents.provider_deadline import (
    ProviderDeadline,
    invoke_with_provider_deadline,
)

# A session that already reached a terminal state must never be re-armed by an
# automatic recovery rerun queued from a late failure of its last execution.
_TERMINAL_SESSION_STATUSES = frozenset(
    {"stopped", "completed", "cancelled", "failed", "archived"}
)
REFLECTION_PROVIDER_TIMEOUT_SECONDS = 60


def _invoke_reflection_prompt(
    runtime: Any,
    prompt: str,
    *,
    timeout_seconds: float = REFLECTION_PROVIDER_TIMEOUT_SECONDS,
) -> dict[str, Any]:
    """Invoke failure-only reflection under one total logical deadline."""

    deadline = ProviderDeadline.start(timeout_seconds)
    return asyncio.run(
        invoke_with_provider_deadline(
            lambda: runtime.invoke_prompt(
                prompt,
                timeout_seconds=timeout_seconds,
                source_brain="local",
                session_prefix="reflection",
            ),
            deadline=deadline,
        )
    )


# Session states an operator or an already-completed generation owns.  A
# fenced terminal handoff from an older owner never overwrites one of these.
_AUTHORITATIVE_CONCURRENT_STATUSES = frozenset(
    {"paused", "stopped", "cancelled", "canceled", "completed", "failed"}
)


def _finalize_logical_failure_fenced(
    db,
    session,
    *,
    task_execution,
    failure_reason: str,
    expected_instance_id: Optional[str],
    expected_task_execution_id: Optional[int],
    commit: bool,
    logger,
) -> bool:
    """Terminalize only while this owner still holds the claimed identity.

    ER4: the canonical lifecycle transition performs the authoritative
    expected-generation/expected-attempt comparison inside its own
    transaction.  When it rejects, an authoritative concurrent winner (an
    operator action or a successor generation) owns the Session and this old
    owner must not overwrite it.
    """

    try:
        finalize_logical_failure(
            db,
            session,
            task_execution=task_execution,
            failure_reason=failure_reason,
            expected_instance_id=expected_instance_id,
            expected_task_execution_id=expected_task_execution_id,
            commit=commit,
        )
        return True
    except LifecycleTransitionError as fence_error:
        db.rollback()
        logger.warning(
            "[ER4] Stale terminal handoff refused by lifecycle fence: %s",
            fence_error.reason,
        )
        return False


class FailureCoordinator:
    """Single orchestration boundary for task failure handling.

    Owns: failure classification, retry routing, workspace restore preparation,
    knowledge halt, auto recovery queuing, session pause/terminal finalization.
    Delegates: append_orchestration_event, _apply_knowledge_halt,
    _prepare_retry_workspace, and other helpers to failure_flow.py.
    """

    @staticmethod
    def _stamp_retry_dispatch_provenance(
        *,
        self_task: Any,
        retry_kwargs: Optional[dict[str, Any]],
        orchestration_state: Any,
        session: Any,
        task: Any,
        task_execution_id: Optional[int],
        retry_count: int,
        append_orchestration_event_fn: Callable[..., Any],
        continuation_identity: Optional[ContinuationIdentity] = None,
    ) -> Optional[dict[str, Any]]:
        """Give each architecture-owned Celery retry its own queue event.

        ``TaskExecution`` remains the logical execution identity and Celery's
        task id remains the delivery chain identity.  The persisted queued
        event is the dispatch-attempt provenance consumed by the stale guard.
        """

        request = getattr(self_task, "request", None)
        if retry_kwargs is None:
            request_kwargs = getattr(request, "kwargs", None)
            if isinstance(request_kwargs, dict):
                retry_kwargs = dict(request_kwargs)
            elif continuation_identity is not None:
                # A provider-free/unit Celery request may not carry kwargs.
                # E3 still has to pass the strict identity to the delivery.
                retry_kwargs = {}
            else:
                return None
        if continuation_identity is not None:
            retry_kwargs.update(
                {
                    "expected_session_instance_id": continuation_identity.instance_id,
                    "continuation_task_id": continuation_identity.continuation_task_id,
                    "continuation_kind": continuation_identity.continuation_kind,
                    "continuation_retry_count": continuation_identity.retry_count,
                    "task_execution_id": continuation_identity.task_execution_id,
                }
            )
        if orchestration_state is None or session is None or task is None:
            return retry_kwargs

        previous_queued_event_id = retry_kwargs.get("queued_event_id")
        retry_event = append_orchestration_event_fn(
            project_dir=orchestration_state.project_dir,
            session_id=session.id,
            task_id=task.id,
            event_type=EventType.TASK_QUEUED,
            parent_event_id=previous_queued_event_id,
            details={
                "dispatch_kind": (
                    "e3_continuation"
                    if continuation_identity is not None
                    else "architecture_owned_retry"
                ),
                "task_execution_id": task_execution_id,
                "celery_task_id": getattr(request, "id", None),
                "retry_count": retry_count + 1,
                "previous_queued_event_id": previous_queued_event_id,
                "session_instance_id": getattr(session, "instance_id", None),
                **(
                    {
                        "continuation_task_id": continuation_identity.continuation_task_id,
                        "continuation_kind": continuation_identity.continuation_kind,
                        "continuation_retry_count": continuation_identity.retry_count,
                    }
                    if continuation_identity is not None
                    else {}
                ),
            },
        )
        retry_kwargs["queued_event_id"] = retry_event["event_id"]
        return retry_kwargs

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

        # Phase 36 E3 deliberately delays reflection/classification until the
        # attempt has entered the nonterminal recovery state below.  This
        # closes the former interval in which reflection could run while the
        # Session was already exposed as paused/aborted.
        _failure_event = None
        _recovery_decision = None

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
        # ER4: a typed terminal-attempt handoff carries the exact generation
        # and attempt its worker claimed.  Ordinary failures carry neither, so
        # their existing behaviour is unchanged.
        expected_instance_id = getattr(exc, "expected_session_instance_id", None)
        expected_task_execution_id = getattr(exc, "expected_task_execution_id", None)
        # The Session state this coordinator *entered* with, read before any
        # transition below mutates it.  A fenced handoff that arrives to find
        # an already authoritative operator/terminal state must preserve it
        # rather than pause, reopen, or terminalize it (ER4 sections 11-12).
        entry_session_status = (
            status_value(getattr(session, "status", None)) if session else None
        )
        fenced_authoritative_state = bool(
            expected_instance_id is not None
            and entry_session_status in _AUTHORITATIVE_CONCURRENT_STATUSES
        )
        is_discovery_terminal_failure = bool(
            getattr(exc, "failure_category", None) == "discovery_terminal_failure"
            or "canonical_workspace_pollution_detected" in str(exc)
            or "read_only_discovery_failed_closed" in str(exc)
        )
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
            and not is_discovery_terminal_failure
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

        # Retry ownership is a single authority: the automatic-recovery rerun
        # obeys the same exclusions as the Celery retry path. A canonical-root
        # mutation-lock conflict must never queue a new attempt (the rerun
        # races the still-held lock of the failing dispatch), and categories
        # the persisted execution policy marks retry-exempt (deterministic
        # backend capability rejections, planning failures, governance holds)
        # must not be re-executed either.
        from app.services.session.execution_policy import (
            automatic_recovery_rerun_allowed as _automatic_recovery_rerun_allowed,
            classify_failure as _classify_failure_category,
            is_retry_exempt_category as _is_retry_exempt_category,
        )

        failure_category_for_retry = _classify_failure_category(
            str(exc),
            "",
            {
                **runtime_diagnostics,
                "failure_phase": (
                    "planning" if "planning" in str(exc).lower() else "execution"
                ),
                "failure_category": getattr(exc, "failure_category", None),
                "provider_failure_classification": getattr(
                    exc, "provider_failure_classification", None
                ),
            },
        )
        # The episode ceiling is derived from the persisted TaskExecution rows,
        # not from ``task.workspace_status``. Every automatic recovery rerun
        # dispatches a fresh TaskExecution, while the Celery retry path reuses
        # the current one and resets workspace_status back to
        # not_created/in_progress — so workspace_status alone cannot bound the
        # episode (POST33-D1: executions 281 → 282 → 283 → 284).
        auto_recovery_eligible = bool(
            session
            and task
            and session.execution_mode == "automatic"
            and status_value(getattr(session, "status", None))
            not in _TERMINAL_SESSION_STATUSES
            and status_value(getattr(task, "status", None))
            != TaskStatus.CANCELLED.value
            and getattr(task, "plan_position", None) is not None
            and not is_timeout
            and not is_project_mutation_lock_conflict
            and not is_discovery_terminal_failure
            and not _is_retry_exempt_category(failure_category_for_retry)
            and getattr(task, "workspace_status", None) != "changes_requested"
            and _automatic_recovery_rerun_allowed(
                db, session_id=session_id, task_id=task_id
            )
        )

        # Capture the current attempt before any failure transition or early
        # recovery/annotation return can mutate durable state.
        task_execution = _task_execution_for_context(db, ctx)
        task_status_before_failure = getattr(task, "status", None)
        session_task_status_before_failure = getattr(session_task_link, "status", None)
        task_execution_status_before_failure = getattr(task_execution, "status", None)
        orchestration_status_before_failure = getattr(
            orchestration_state, "status", None
        )
        authoritative_success_recorded = any(
            status_value(status) in {TaskStatus.DONE.value, "done", "completed"}
            for status in (
                task_status_before_failure,
                session_task_status_before_failure,
                task_execution_status_before_failure,
                orchestration_status_before_failure,
            )
        )
        if (
            task_execution is None
            and session is not None
            and task is not None
            and (has_retry_capacity or auto_recovery_eligible)
            and not authoritative_success_recorded
        ):
            # Worker production dispatches always provide this identity. The
            # fallback keeps the coordinator's direct recovery entry point
            # equally strict by creating the attempt fact before scheduling.
            task_execution = create_task_execution(
                db,
                session_id=session.id,
                task_id=task.id,
                status=TaskStatus.RUNNING,
            )
        task_execution_id = task_execution.id if task_execution else None
        if task_execution is not None and hasattr(task_execution, "failure_category"):
            task_execution.failure_category = failure_category_for_retry

        # E3: make the failed attempt visible without making the logical
        # generation terminal.  This commit is intentionally before the
        # classifier/reflection call below, so a held recovery decision reads
        # ``recovering`` through the normal lifecycle projection.
        recovery_identity: Optional[ContinuationIdentity] = None
        recovery_started = False
        e3_continuation_kind = (
            "celery_retry" if has_retry_capacity else "automatic_recovery"
        )
        other_active_before_recovery = _session_has_other_active_execution(
            db,
            session_id=session_id,
            current_task_execution_id=task_execution_id,
        )
        if (
            session is not None
            and task_execution is not None
            and not authoritative_success_recorded
            and not other_active_before_recovery
            and (has_retry_capacity or auto_recovery_eligible)
        ):
            try:
                recovery_identity = enter_recovering(
                    db,
                    session,
                    task_execution=task_execution,
                    continuation_kind=e3_continuation_kind,
                    retry_count=retry_count,
                    failure_reason=str(exc),
                    expected_instance_id=expected_instance_id,
                    expected_task_execution_id=expected_task_execution_id,
                    commit=True,
                )
                recovery_started = True
            except LifecycleTransitionError as transition_error:
                logger.warning(
                    "[E3] Could not enter recovering for session %s task %s: %s",
                    session_id,
                    task_id,
                    transition_error.reason,
                )
                # Recovery admission is a branch-owning lifecycle decision.
                # Never continue into retry preparation after it rejects: that
                # was the CA1 path that reset the attempt to PENDING while the
                # Execution Session remained incompatible with continuation.
                db.rollback()
                current_session = (
                    db.query(type(session)).filter(type(session).id == session_id).one()
                )
                current_execution = (
                    db.query(TaskExecution)
                    .filter(TaskExecution.id == task_execution_id)
                    .one()
                )
                current_status = status_value(current_session.status)
                if current_status not in {
                    "paused",
                    "stopped",
                    "cancelled",
                    "canceled",
                    "completed",
                    "failed",
                }:
                    _finalize_logical_failure_fenced(
                        db,
                        current_session,
                        task_execution=current_execution,
                        failure_reason=(
                            f"recovery admission failed: {transition_error.reason}; "
                            f"original failure: {exc}"
                        ),
                        expected_instance_id=expected_instance_id,
                        expected_task_execution_id=expected_task_execution_id,
                        commit=True,
                        logger=logger,
                    )
                raise exc

        # ── Phase 17A/17B: classify failure + route through recovery registry ───
        try:
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
                            _invoke_reflection_prompt,
                            _runtime,
                            _prompt,
                            timeout_seconds=REFLECTION_PROVIDER_TIMEOUT_SECONDS,
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
        except Exception as _17a_exc:
            logger.debug("[17A/17B] classifier/registry raised: %s", _17a_exc)

        persist_failure_evidence(
            evidence=build_failure_evidence(
                exc=exc,
                session_id=session_id,
                task_id=task_id,
                task_execution_id=task_execution_id,
                orchestration_phase=getattr(orchestration_state, "current_phase", None),
                task_status_before_failure=task_status_before_failure,
                session_task_status_before_failure=session_task_status_before_failure,
                task_execution_status_before_failure=task_execution_status_before_failure,
                orchestration_status_before_failure=orchestration_status_before_failure,
                failure_category=failure_category_for_retry,
                failure_class=getattr(_failure_event, "failure_class", None),
                retry_capacity=has_retry_capacity,
                automatic_recovery_eligible=auto_recovery_eligible,
                project_mutation_lock_classification=is_project_mutation_lock_conflict,
                planning_lock_classification=is_planning_lock_wait_timeout,
                timeout_classification=is_timeout,
                authoritative_success_recorded=authoritative_success_recorded,
            ),
            project_dir=getattr(orchestration_state, "project_dir", None),
            session_id=session_id,
            task_id=task_id,
            task_execution_id=task_execution_id,
            session_instance_id=getattr(session, "instance_id", None),
            append_orchestration_event_fn=append_orchestration_event,
            record_live_log_fn=record_live_log_fn,
            db=db,
            logger=logger,
        )

        if getattr(_recovery_decision, "strategy", None) == "annotate_and_continue":
            logger.info(
                "[17A] wrapper_timeout_noise annotated; not propagating as task "
                "failure (session_id=%s task_id=%s)",
                session_id,
                task_id,
            )
            return

        if orchestration_state and session_id and task_id:
            try:
                append_orchestration_event(
                    project_dir=control_state_of(orchestration_state),
                    session_id=session_id,
                    task_id=task_id,
                    event_type=EventType.TASK_FAILED,
                    details={
                        "error": str(exc),
                        "scope": "attempt",
                        "logical_terminal": not recovery_started,
                        "continuation_pending": recovery_started,
                        "continuation_kind": (
                            recovery_identity.continuation_kind
                            if recovery_identity is not None
                            else None
                        ),
                    },
                )
            except Exception:
                pass

        if not session_task_link:
            session_task_link = get_latest_session_task_link_fn(db, session_id, task_id)
        completed_at = datetime.now(UTC)
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

        other_active_execution = other_active_before_recovery
        if session and not fenced_authoritative_state:
            if recovery_started:
                # E3 has already committed the authoritative recovering state.
                # In particular, do not translate an attempt failure into the
                # legacy operator-paused state while reflection is live.
                pass
            elif other_active_execution:
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
                if not recovery_started:
                    orchestration_state.status = OrchestrationStatus.ABORTED
                    orchestration_state.abort_reason = str(exc)
                else:
                    # Checkpoint evidence may describe the failed attempt,
                    # but ABORTED is a logical stop for replay/operator
                    # consumers and is forbidden during E3 recovery.
                    orchestration_state.abort_reason = None
                record_phase_event(
                    orchestration_state,
                    phase="failure",
                    status="recovering" if recovery_started else "error",
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
                        "failure_category": failure_category_for_retry,
                        "terminal_cause": (
                            None if recovery_started else failure_category_for_retry
                        ),
                        "scope": "attempt",
                        "logical_terminal": not recovery_started,
                        "continuation_pending": recovery_started,
                        "operator_pause_requested": False,
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
        operator_pause_requested = bool(knowledge_halted)
        if knowledge_halted and recovery_started and session is not None:
            # Knowledge-backed halts are an intentional operator outcome, not
            # a hidden retry. Revoke the autonomous marker and fence the old
            # generation before the caller observes the pause.
            revoke_autonomous_continuation(
                db,
                session,
                resulting_status="paused",
                reason="knowledge_halt",
                commit=True,
            )
            operator_pause_requested = True

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
                    failure_metadata_authoritative=True,
                )
                if recovery_started:
                    revoke_autonomous_continuation(
                        db,
                        session,
                        resulting_status="paused",
                        reason=retry_blocked_message,
                    )
                else:
                    mark_session_paused(
                        session,
                        alert_level="error",
                        alert_message=retry_blocked_message[:2000],
                    )
                db.commit()
                write_project_state_snapshot_fn(db, project, task, session_id)
                return
            retry_delay = getattr(self_task, "default_retry_delay", None)
            retry_eta = None
            if isinstance(retry_delay, (int, float)) and retry_delay >= 0:
                retry_eta = datetime.now(UTC) + timedelta(seconds=retry_delay)
            try:
                # The pending reset and durable continuation marker share the
                # schedule_continuation transaction.  A pre-marker exception
                # is rolled back and compensated before this worker exits.
                retry_identity = schedule_continuation(
                    db,
                    session,
                    task_execution=task_execution,
                    continuation_task_id=task.id,
                    continuation_kind="celery_retry",
                    retry_count=retry_count + 1,
                    retry_eta=retry_eta,
                    commit=True,
                )
            except Exception as scheduling_error:
                db.rollback()
                current_session = (
                    db.query(type(session)).filter(type(session).id == session_id).one()
                )
                current_execution = (
                    db.query(TaskExecution)
                    .filter(TaskExecution.id == task_execution_id)
                    .one()
                )
                marker_committed = bool(
                    status_value(current_session.status) == "retry_pending"
                    and getattr(current_session, "continuation_task_id", None)
                    == task_id
                    and getattr(current_session, "continuation_kind", None)
                )
                if not marker_committed and status_value(
                    current_session.status
                ) not in {
                    "paused",
                    "stopped",
                    "cancelled",
                    "canceled",
                    "completed",
                    "failed",
                }:
                    _finalize_logical_failure_fenced(
                        db,
                        current_session,
                        task_execution=current_execution,
                        failure_reason=(
                            f"continuation scheduling failed before durable intent: "
                            f"{scheduling_error}"
                        ),
                        expected_instance_id=expected_instance_id,
                        expected_task_execution_id=expected_task_execution_id,
                        commit=True,
                        logger=logger,
                    )
                raise
            retry_kwargs = self._stamp_retry_dispatch_provenance(
                self_task=self_task,
                retry_kwargs=retry_kwargs,
                orchestration_state=orchestration_state,
                session=session,
                task=task,
                task_execution_id=task_execution_id,
                retry_count=retry_count,
                append_orchestration_event_fn=append_orchestration_event,
                continuation_identity=retry_identity,
            )
            try:
                if retry_kwargs is not None:
                    raise self_task.retry(exc=exc, kwargs=retry_kwargs)
                raise self_task.retry(exc=exc)
            except Exception as publication_error:
                # Celery's Retry exception is the normal handoff. Any other
                # exception means publication failed after the durable marker
                # was committed; retain retry_pending for reconciliation.
                from celery.exceptions import Retry as CeleryRetry

                if not isinstance(publication_error, CeleryRetry):
                    logger.error(
                        "[E3] Ordinary retry publication failed for session %s task %s: %s",
                        session_id,
                        task_id,
                        publication_error,
                    )
                    db.add(
                        LogEntry(
                            session_id=session_id,
                            session_instance_id=session.instance_id,
                            task_id=task_id,
                            task_execution_id=task_execution_id,
                            level="ERROR",
                            message="E3 retry publication failed after retry_pending commit",
                            log_metadata=json.dumps(
                                {
                                    "dispatch_kind": "e3_continuation",
                                    "continuation_kind": retry_identity.continuation_kind,
                                    "continuation_retry_count": retry_identity.retry_count,
                                    "task_execution_id": retry_identity.task_execution_id,
                                    "error": str(publication_error),
                                }
                            ),
                        )
                    )
                    db.commit()
                raise

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
            task.workspace_status = "changes_requested"
            task.error_message = recovery_error_message
            try:
                # The queue writer creates the fresh recovery execution and
                # calls schedule_continuation in the same transaction. The
                # explicit discriminator keeps all unmigrated callers on
                # their legacy dispatch contract.
                _queue_result = queue_task_for_session_fn(
                    db=db,
                    session=session,
                    task_id=task.id,
                    continuation_kind="automatic_recovery",
                    continuation_retry_count=retry_count + 1,
                    recovery_error_message=recovery_error_message,
                )
                if (
                    recovery_started
                    and getattr(session, "status", None) != "retry_pending"
                ):
                    raise LifecycleTransitionError(
                        "automatic_recovery_writer_did_not_schedule_continuation"
                    )
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
                        "dispatch_kind": "e3_continuation",
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
                if (
                    recovery_started
                    and getattr(session, "status", None) == "retry_pending"
                    and getattr(session, "continuation_kind", None)
                    == "automatic_recovery"
                ):
                    # queue_task_for_session commits before .delay(). Preserve
                    # the inspectable continuation intent for E8 reconciliation.
                    db.add(
                        LogEntry(
                            session_id=session_id,
                            session_instance_id=session.instance_id,
                            task_id=task_id,
                            task_execution_id=task_execution_id,
                            level="ERROR",
                            message="E3 automatic recovery publication failed after retry_pending commit",
                            log_metadata=json.dumps(
                                {
                                    "dispatch_kind": "e3_continuation",
                                    "continuation_kind": "automatic_recovery",
                                    "continuation_retry_count": getattr(
                                        session, "continuation_retry_count", None
                                    ),
                                    "error": str(recovery_queue_error),
                                }
                            ),
                        )
                    )
                    db.commit()
                else:
                    # Preparation/claim failure occurred before a durable
                    # continuation existed. The current product policy is an
                    # operator pause, with the autonomous generation fenced.
                    if recovery_started:
                        revoke_autonomous_continuation(
                            db,
                            session,
                            resulting_status="paused",
                            reason=(
                                f"{alert_message}. Automatic recovery could not be queued: "
                                f"{str(recovery_queue_error)}"
                            )[:2000],
                        )
                        operator_pause_requested = True
                    else:
                        mark_session_paused(
                            session,
                            alert_level="error",
                            alert_message=(
                                f"{alert_message}. Automatic recovery could not be queued: "
                                f"{str(recovery_queue_error)}"
                            )[:2000],
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
                        failure_metadata_authoritative=True,
                    )
                    db.commit()

        workspace_restore_failed = False
        try:
            if (
                project
                and orchestration_state
                and restore_workspace_snapshot_if_needed
                and should_restore_workspace
            ):
                restore_workspace_snapshot_if_needed("task exception")
        except Exception as restore_error:
            workspace_restore_failed = True
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

        if session and not operator_pause_requested:
            current_session_status = status_value(getattr(session, "status", None))
            continuation_is_pending = (
                current_session_status == "retry_pending"
                and getattr(session, "continuation_task_id", None) is not None
            )
            if continuation_is_pending:
                # A publication failure after schedule_continuation committed
                # remains inspectable and nonterminal for E8 reconciliation.
                pass
            elif workspace_restore_failed:
                if recovery_started:
                    revoke_autonomous_continuation(
                        db,
                        session,
                        resulting_status="paused",
                        reason="workspace_restore_failed",
                    )
                else:
                    mark_session_paused(
                        session,
                        alert_level="error",
                        alert_message="Workspace restore failed; operator review required",
                    )
            elif other_active_execution:
                mark_session_running(
                    session,
                    alert_level="warning",
                    alert_message=alert_message[:2000],
                )
            elif current_session_status not in {
                "paused",
                "stopped",
                "cancelled",
                "completed",
                "failed",
            } or (
                current_session_status == "paused"
                and not recovery_started
                and not knowledge_halted
                and not fenced_authoritative_state
            ):
                _finalize_logical_failure_fenced(
                    db,
                    session,
                    task_execution=task_execution,
                    failure_reason=str(exc),
                    expected_instance_id=expected_instance_id,
                    expected_task_execution_id=expected_task_execution_id,
                    commit=False,
                    logger=logger,
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
