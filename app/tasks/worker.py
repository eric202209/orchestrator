"""Celery Worker Tasks

Background task processing for the orchestrator.
Implements multi-step orchestration workflow:
PLANNING → EXECUTING (step-by-step) → DEBUGGING (on failure) → PLAN_REVISION → DONE
"""

import os
import logging
import json
import asyncio
from pathlib import Path
from typing import Optional, Dict, Any
from datetime import datetime, timezone
from app.celery_app import celery_app
from app.models import (
    Session as SessionModel,
    SessionTask,
    Task,
    TaskStatus,
    Project,
)
from app.database import get_db_session
from app.services import (
    create_agent_runtime,
    build_task_subfolder_name,
    get_session_or_404,
)
from app.config import settings
from app.services.agents.agent_runtime import BackendRole, resolve_backend_name_for_role
from app.services.agents.agent_backends import get_backend_descriptor
from app.services.session.execution_policy import classify_failure
from app.services.session.session_execution_service import (
    update_execution_failure_metadata,
)
from app.services.orchestration import (
    STALE_RUN_GUARD_SECONDS,
    OrchestrationRunContext,
    TaskWorkspaceViolationError,
    ValidatorService,
    build_task_report_payload as _build_task_report_payload,
    extract_plan_steps as _extract_plan_steps,
    extract_structured_text as _extract_structured_text,
    looks_like_truncated_multistep_plan as _looks_like_truncated_multistep_plan,
    normalize_plan_with_live_logging as _normalize_plan_with_live_logging,
    normalize_step as _normalize_step,
    render_task_report as _render_task_report,
    run_virtual_merge_gate as _run_virtual_merge_gate,
    should_execute_in_canonical_project_root as _should_execute_in_canonical_project_root,
    should_force_review_execution_profile as _should_force_review_execution_profile,
)
from app.services.orchestration.lifecycle.worker_bootstrap import (
    build_claimed_details as _build_claimed_details,
    run_start_runtime_identity as _run_start_runtime_identity,
    should_use_configured_planning_runtime,
)
from app.services.orchestration.lifecycle.worker_capacity import (
    BACKEND_CAPACITY_RETRY_MAX_RETRIES,
    backend_capacity_retry_state,
    prepare_backend_capacity_retry,
)
from app.services.orchestration.coordinators.completion_coordinator import (
    CompletionCoordinator as _CompletionCoordinator,
)
from app.services.orchestration.coordinators.failure_coordinator import (
    FailureCoordinator as _FailureCoordinator,
)
from app.services.orchestration.coordinators.execution_coordinator import (
    ExecutionCoordinator as _ExecutionCoordinator,
)
from app.services.orchestration.coordinators.planning_coordinator import (
    PlanningCoordinator as _PlanningCoordinator,
)
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.state.persistence import (
    append_orchestration_event as _append_orchestration_event,
    record_live_log as _record_live_log,
    record_validation_verdict as _record_validation_verdict,
    save_orchestration_checkpoint as _save_orchestration_checkpoint,
)
from app.services.orchestration.execution.runtime import (
    build_runtime_executor_context as _build_runtime_executor_context,
    dispose_runtime_workspace_safely as _dispose_runtime_workspace_safely,
    get_state_manager_path as _get_state_manager_path,
    maybe_allocate_runtime_workspace as _maybe_allocate_runtime_workspace,
    maybe_bind_runtime_cwd_override as _maybe_bind_runtime_cwd_override,
    resolve_workspace_contract_args as _resolve_workspace_contract_args,
    snapshot_workspace_before_run as _snapshot_workspace_before_run,
    workspace_snapshot_key as _workspace_snapshot_key,
    write_project_state_snapshot as _write_project_state_snapshot,
)
from app.services.orchestration.execution.runtime_context import (
    RuntimeExecutorContext,
)
from app.services.orchestration.error_handler import error_handler
from app.services.workspace.checkpoint_service import CheckpointService
from app.services.workspace.project_isolation_service import (
    resolve_project_workspace_path,
)
from app.services.workspace.task_sandbox_allocator import (
    RUNTIME_SCHEMA_VERSION as _RUNTIME_SCHEMA_VERSION,
    TaskSandbox,
)
from app.services.workspace.system_settings import get_effective_runtime_root
from app.services.tasks.service import TaskService
from app.services.orchestration.prompt_templates import (
    OrchestrationStatus,
    OrchestrationState,
)
from app.services.session.session_runtime_service import (
    queue_task_for_session as _queue_task_for_session,
)
from app.services.tasks.execution import (
    create_task_execution,
    get_task_execution,
)
from app.services.human_guidance.service import resolve_guidance_runtime_target
from app.services.orchestration.policy import get_policy_profile
from app.services.orchestration.policy import (
    ORCHESTRATION_TASK_SOFT_TIME_LIMIT_SECONDS,
    ORCHESTRATION_TASK_TIME_LIMIT_SECONDS,
)
from app.services.orchestration.state.execution_states import TerminalReason
from app.services.workspace.system_settings import get_effective_policy_profile
from app.services.orchestration.validation.workspace_guard import (
    verify_workspace_contract,
)
from app.services.session.session_execution_service import (
    mark_execution_cancelled,
    mark_execution_failed,
    mark_execution_running,
)
from app.services.orchestration.state.session_state import (
    mark_session_failed,
    mark_session_paused,
    mark_session_running,
)
from app.services.workspace.project_mutation_lock import project_mutation_lock
from app.services.observability import (
    build_text_trace_payload,
    flush_langfuse,
    langfuse_tracing_enabled,
    start_langfuse_observation,
    update_langfuse_observation,
)

logger = logging.getLogger(__name__)

MAX_SUBFOLDER_COLLISION_ATTEMPTS = 999


from celery.signals import worker_ready

from app.tasks.worker_support.worker_helpers import (
    _apply_checkpoint_payload,
    _build_base_project_context,
    _claim_queued_task_for_worker,
    _coerce_utc_datetime,
    _decode_context_snapshot_object,
    _emit_dispatch_rejected,
    _find_queued_event_for_dispatch,
    _get_latest_session_task_link,
    _get_next_pending_project_task,
    _inject_progress_notes_into_context,
    _parse_event_timestamp,
    _restore_workspace_snapshot_if_needed,
    _runtime_selection_details,
    _should_reject_stale_dispatch_claim,
    _sync_task_execution_from_task_state,
    _sync_task_execution_state,
)


@worker_ready.connect
def on_worker_ready(sender, **kwargs):
    """On cold boot, immediately recover sessions orphaned by a previous worker crash.

    The periodic beat sweep fires every 15 min; this closes the cold-start gap.
    stale_after_seconds=60 gives a brief grace window for rolling restarts.
    """
    try:
        db = get_db_session()
        try:
            from app.services.session.session_lifecycle_service import (
                recover_stale_running_sessions,
            )

            recovered = recover_stale_running_sessions(db, stale_after_seconds=60)
            if recovered:
                logger.warning(
                    "Worker boot recovery: recovered %d orphaned session(s): %s",
                    len(recovered),
                    [r.get("session_id") for r in recovered],
                )
            else:
                logger.info("Worker boot recovery: no orphaned sessions found.")
        finally:
            db.close()
    except Exception as exc:
        logger.error("Worker boot recovery scan failed: %s", exc)


@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    time_limit=ORCHESTRATION_TASK_TIME_LIMIT_SECONDS,
    soft_time_limit=ORCHESTRATION_TASK_SOFT_TIME_LIMIT_SECONDS,
    acks_late=True,
    reject_on_worker_lost=True,
    acks_on_failure_or_timeout=True,
    queue="celery",
)
def execute_orchestration_task(
    self,
    session_id: int,
    task_id: int,
    prompt: str,
    timeout_seconds: int = 300,
    context: Optional[Dict[str, Any]] = None,
    resume_checkpoint_name: Optional[str] = None,
    expected_session_instance_id: Optional[str] = None,
    task_execution_id: Optional[int] = None,
    queued_event_id: Optional[str] = None,
    planning_backend_override: Optional[str] = None,
    planning_escalation_metadata: Optional[Dict[str, Any]] = None,
):
    """
    Execute an orchestration task with multi-step runtime coordination

    Workflow:
    1. PLANNING → Generate step plan
    2. EXECUTING → Execute each step
    3. DEBUGGING → Fix failed steps
    4. PLAN_REVISION → Revise plan if needed
    5. DONE → Summarize completion

    Args:
        session_id: Session ID
        task_id: Task ID
        prompt: Task prompt to execute
        timeout_seconds: Maximum execution time
        context: Additional context
    """
    db = get_db_session()
    session: Optional[SessionModel] = None
    task: Optional[Task] = None
    orchestration_state: Optional[OrchestrationState] = None
    session_task_link: Optional[SessionTask] = None
    trace_context_manager = None
    trace_observation = None
    run_ctx: Optional[OrchestrationRunContext] = None
    execution_profile: str = "full_lifecycle"
    validation_profile: str = "implementation"
    runs_in_canonical_baseline: bool = False
    active_policy = None
    restore_workspace_snapshot_if_needed = None
    project_mutation_lock_context = None
    _backend_slot_acquired: bool = False
    _backend_slot_backend_id: Optional[str] = None
    _backend_slot_redis = None
    _resolved_execution_backend: str = settings.AGENT_BACKEND
    planning_runtime_service = None
    # Phase 23C: set only when RUNTIME_WORKSPACE_ENABLED redirects execution
    # into an allocated Task Execution Sandbox. `finally` disposes it
    # unconditionally (success, failure, cancellation, timeout, exception)
    # whenever it is not None.
    _runtime_sandbox: Optional[TaskSandbox] = None
    _runtime_sandbox_project_root: Optional[Path] = None
    # Phase 23D: the single Runtime Executor Context for this dispatch, built
    # alongside _runtime_sandbox above. None for resume/non-canonical-baseline
    # dispatch (unchanged, out of scope -- see Phase 23C's own scope note).
    _runtime_context: Optional[RuntimeExecutorContext] = None
    runtime_service = None

    try:
        # Get session and task
        session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
        task = db.query(Task).filter(Task.id == task_id).first()

        if not session or not task:
            raise ValueError("Session or task not found")

        _resolved_execution_backend = resolve_backend_name_for_role(
            db, BackendRole.EXECUTION
        )
        resolved_planning_backend = resolve_backend_name_for_role(
            db, BackendRole.PLANNING
        )

        if task_execution_id is None:
            task_execution = create_task_execution(
                db,
                session_id=session_id,
                task_id=task_id,
            )
            task_execution_id = task_execution.id

        def emit_live(
            level: str, message: str, metadata: Optional[Dict[str, Any]] = None
        ) -> None:
            metadata_with_execution = dict(metadata or {})
            metadata_with_execution.setdefault("task_execution_id", task_execution_id)
            _record_live_log(
                db,
                session_id,
                task_id,
                level,
                message,
                session_instance_id=session.instance_id,
                metadata=metadata_with_execution,
                task_execution_id=task_execution_id,
            )

        def _task_is_first_ordered() -> bool:
            return getattr(task, "plan_position", None) == 1

        def _project_has_blocked_after_task1() -> bool:
            if not _task_is_first_ordered() or not project:
                return False
            return (
                db.query(Task)
                .filter(
                    Task.project_id == project.id,
                    Task.plan_position.isnot(None),
                    Task.plan_position > 1,
                    Task.status.notin_([TaskStatus.DONE, TaskStatus.CANCELLED]),
                )
                .first()
                is not None
            )

        def _emit_task1_product_event(
            event_type: str,
            *,
            reason: Optional[str] = None,
            level: str = "INFO",
        ) -> None:
            if not _task_is_first_ordered():
                return
            emit_live(
                level,
                f"[ORCHESTRATION] Task 1 product metric: {event_type}",
                metadata={
                    "event_type": event_type,
                    "phase": "task1_product_metrics",
                    "reason": reason,
                    "plan_position": getattr(task, "plan_position", None),
                },
            )
            if (
                event_type == "task1_execution_failed"
                and _project_has_blocked_after_task1()
            ):
                emit_live(
                    "WARN",
                    "[ORCHESTRATION] Project is blocked after Task 1 failure",
                    metadata={
                        "event_type": "project_blocked_after_task1",
                        "phase": "task1_product_metrics",
                        "reason": reason,
                    },
                )

        execution_profile = (
            getattr(task, "execution_profile", None)
            or getattr(session, "default_execution_profile", None)
            or "full_lifecycle"
        )
        if _should_force_review_execution_profile(
            execution_profile,
            task.description if task else None,
            task.title if task else None,
            task.description if task else None,
        ):
            execution_profile = "review_only"
            emit_live(
                "INFO",
                "[ORCHESTRATION] Task intent is inspection/review-oriented; using review_only execution profile",
                metadata={
                    "phase": "profile_selection",
                    "execution_profile": execution_profile,
                },
            )

        # Get the project associated with this session
        project = (
            db.query(Project).filter(Project.id == session.project_id).first()
            if session.project_id
            else None
        )
        runs_in_canonical_baseline = bool(
            project
            and task
            and _should_execute_in_canonical_project_root(
                task,
                execution_profile,
                task.title if task else None,
                task.description if task else None,
            )
        )
        dispatch_project_dir = None
        if project and project.workspace_path:
            dispatch_project_dir = Path(
                resolve_project_workspace_path(project.workspace_path, project.name)
            )
            if task.task_subfolder and not runs_in_canonical_baseline:
                dispatch_project_dir = (
                    dispatch_project_dir / str(task.task_subfolder)
                ).resolve()

        queued_event = None
        queued_at = None
        queue_latency_seconds = None
        if dispatch_project_dir:
            queued_event = _find_queued_event_for_dispatch(
                dispatch_project_dir=dispatch_project_dir,
                session_id=session_id,
                task_id=task_id,
                queued_event_id=queued_event_id,
            )
            queued_at = _parse_event_timestamp((queued_event or {}).get("timestamp"))
            if queued_at is not None:
                queue_latency_seconds = round(
                    (
                        datetime.now(timezone.utc) - _coerce_utc_datetime(queued_at)
                    ).total_seconds(),
                    3,
                )
        stale_dispatch_reason = _should_reject_stale_dispatch_claim(
            dispatch_project_dir=dispatch_project_dir,
            session_id=session_id,
            task_id=task_id,
            queued_event=queued_event,
            queue_latency_seconds=queue_latency_seconds,
            resume_checkpoint_name=resume_checkpoint_name,
        )
        if stale_dispatch_reason:
            task_execution = get_task_execution(db, task_execution_id)
            mark_execution_cancelled(
                task=None,
                task_execution=task_execution,
                completed_at=datetime.now(timezone.utc),
            )
            db.commit()
            return _emit_dispatch_rejected(
                reason=stale_dispatch_reason,
                log_message=f"[ORCHESTRATION] Rejected stale queued dispatch before claim: {stale_dispatch_reason}",
                db=db,
                session=session,
                session_id=session_id,
                task_id=task_id,
                task_execution_id=task_execution_id,
                dispatch_project_dir=dispatch_project_dir,
                expected_session_instance_id=expected_session_instance_id,
                celery_task_id=getattr(getattr(self, "request", None), "id", None),
                queue_latency_seconds=queue_latency_seconds,
                queued_event=queued_event,
                emit_live=emit_live,
            )

        session_task_link = _get_latest_session_task_link(db, session_id, task_id)
        claim_ok = False
        claim_reason = "unclaimed"
        claim_started_at = None
        for claim_attempt in range(1):
            session = (
                db.query(SessionModel).filter(SessionModel.id == session_id).first()
            )
            task = db.query(Task).filter(Task.id == task_id).first()
            session_task_link = _get_latest_session_task_link(db, session_id, task_id)
            if not session or not task:
                claim_reason = "session_or_task_not_found"
                break
            claim_ok, claim_reason, claim_started_at, session_task_link = (
                _claim_queued_task_for_worker(
                    db=db,
                    session=session,
                    task=task,
                    session_task_link=session_task_link,
                    expected_session_instance_id=expected_session_instance_id,
                )
            )
            if claim_ok or claim_reason == "session_instance_changed":
                break
        if not claim_ok:
            task_execution = get_task_execution(db, task_execution_id)
            mark_execution_cancelled(
                task=None,
                task_execution=task_execution,
                completed_at=datetime.now(timezone.utc),
            )
            db.commit()
            return _emit_dispatch_rejected(
                reason=claim_reason,
                log_message=f"[ORCHESTRATION] Rejected stale or duplicate task dispatch: {claim_reason}",
                db=db,
                session=session,
                session_id=session_id,
                task_id=task_id,
                task_execution_id=task_execution_id,
                dispatch_project_dir=dispatch_project_dir,
                expected_session_instance_id=expected_session_instance_id,
                celery_task_id=getattr(getattr(self, "request", None), "id", None),
                queue_latency_seconds=queue_latency_seconds,
                queued_event=queued_event,
                emit_live=emit_live,
            )

        runtime_selection = _runtime_selection_details(db)
        claimed_details = _build_claimed_details(
            session_instance_id=session.instance_id,
            expected_session_instance_id=expected_session_instance_id,
            celery_task_id=getattr(getattr(self, "request", None), "id", None),
            task_execution_id=task_execution_id,
            dispatch_project_dir=dispatch_project_dir,
            queue_latency_seconds=queue_latency_seconds,
            queued_event=queued_event,
            runtime_selection=runtime_selection,
        )
        if dispatch_project_dir:
            _append_orchestration_event(
                project_dir=dispatch_project_dir,
                session_id=session_id,
                task_id=task_id,
                event_type=EventType.TASK_CLAIMED,
                details=claimed_details,
            )
        emit_live(
            "INFO",
            "[ORCHESTRATION] Worker claimed queued task dispatch",
            metadata=claimed_details,
        )

        # Initialize orchestration state with new workspace architecture
        # Structure: workspace_root / project_workspace / task_subfolder

        orchestration_state = OrchestrationState(
            session_id=str(session_id),
            task_description=prompt,
            project_name=project.name if project else "",
            project_context=context.get("project_context", "") if context else "",
            task_id=task_id,  # Pass task ID for subfolder generation
        )

        # If project has workspace_path configured, use it
        if project and project.workspace_path:
            workspace_path = str(
                resolve_project_workspace_path(project.workspace_path, project.name)
            )

            orchestration_state._workspace_path_override = workspace_path

            if not runs_in_canonical_baseline and task.task_subfolder:
                # ✅ Subfolder already locked from a previous run — use it
                # unconditionally so all cycles land in the same directory.
                orchestration_state._task_subfolder_override = task.task_subfolder
                logger.info(
                    "[ORCHESTRATION] Reusing locked task subfolder '%s' for task %s",
                    task.task_subfolder,
                    task_id,
                )
            elif not runs_in_canonical_baseline:
                # First run: generate a stable slug and persist it immediately.
                task_title_slug = build_task_subfolder_name(task.title, task_id)

                # Resolve collisions once, then freeze.
                subfolder_name = task_title_slug
                for counter in range(1, MAX_SUBFOLDER_COLLISION_ATTEMPTS + 1):
                    existing_task = (
                        db.query(Task)
                        .filter(
                            Task.project_id == session.project_id,
                            Task.task_subfolder == subfolder_name,
                            Task.id != task_id,  # exclude self
                        )
                        .first()
                    )
                    if not existing_task:
                        break
                    subfolder_name = f"{task_title_slug}-{counter}"
                else:
                    subfolder_name = f"{task_title_slug}-{task_id}"

                task.task_subfolder = subfolder_name
                db.commit()
                orchestration_state._task_subfolder_override = subfolder_name
                logger.info(
                    "[ORCHESTRATION] Locked new task subfolder '%s' for task %s",
                    subfolder_name,
                    task_id,
                )
        is_resume_execution = bool(resume_checkpoint_name)
        task_service = TaskService(db)
        if runs_in_canonical_baseline and project:
            # Phase 23D-2: build the one RuntimeExecutorContext (non-sandboxed
            # here; the sandboxed branch below rebuilds and overwrites it once
            # RUNTIME_WORKSPACE_ENABLED redirection is known) and set
            # orchestration_state._project_dir_override *before* the mutation
            # lock is attempted. Without this, a failure at lock acquisition
            # (e.g. a concurrent auto-advance/auto-recovery dispatch for the
            # same project) leaves _project_dir_override unset, and
            # OrchestrationState.project_dir's own fallback
            # (project_workspace_path / f"task-{task_id}") then silently
            # creates a real task-N subfolder directly in the tracked Project
            # Workspace on the failure path -- a RuntimeExecutorContext
            # bypass, not a redesign of what happens on success.
            _early_baseline_dir = task_service.get_project_root(project)
            _runtime_context = _build_runtime_executor_context(
                sandbox=None,
                project_workspace=_early_baseline_dir,
                executor=_resolved_execution_backend,
                project_id=project.id,
                task_execution_id=task_execution_id,
            )
            orchestration_state._project_dir_override = str(
                _runtime_context.runtime_workspace
            )
            mutation_lock_context = project_mutation_lock(
                project_id=project.id,
                project_root=_early_baseline_dir,
                operation="execute_canonical_root_task",
                owner=f"session:{session_id}:task:{task_id}:execution:{task_execution_id}",
            )
            mutation_lock_context.__enter__()
            project_mutation_lock_context = mutation_lock_context
        if project:
            task_service.ensure_project_gitignore_guard(project)
        if runs_in_canonical_baseline and project and not is_resume_execution:
            canonical_baseline_dir = task_service.get_project_baseline_dir(project)
            canonical_baseline_dir.mkdir(parents=True, exist_ok=True)
            if settings.RUNTIME_WORKSPACE_ENABLED:
                # Phase 23C: redirect execution into an allocated Task
                # Execution Sandbox instead of the Project Workspace itself.
                # maybe_allocate_runtime_workspace raises TaskSandboxError on
                # failure, which propagates to the existing outer exception
                # handler -- no fallback execution in the Project Workspace
                # on a failed allocation.
                runtime_root = get_effective_runtime_root(db)
                _runtime_sandbox = _maybe_allocate_runtime_workspace(
                    enabled=True,
                    project_id=project.id,
                    task_execution_id=task_execution_id,
                    canonical_baseline_dir=canonical_baseline_dir,
                    executor=_resolved_execution_backend,
                    runtime_root=runtime_root,
                )
                _runtime_sandbox_project_root = canonical_baseline_dir
                _runtime_context = _build_runtime_executor_context(
                    sandbox=_runtime_sandbox,
                    project_workspace=canonical_baseline_dir,
                    executor=_resolved_execution_backend,
                    project_id=project.id,
                    task_execution_id=task_execution_id,
                    runtime_root=runtime_root,
                )
                orchestration_state._project_dir_override = str(
                    _runtime_context.runtime_workspace
                )
                logger.info(
                    "[ORCHESTRATION] Redirecting task %s from Project Workspace %s "
                    "to Runtime Workspace %s",
                    task_id,
                    canonical_baseline_dir,
                    _runtime_sandbox.path,
                )
                runtime_metadata_fields = {
                    "project_workspace": str(canonical_baseline_dir),
                    "runtime_workspace": str(_runtime_sandbox.path),
                    "runtime_root": str(runtime_root),
                    "runtime_state": _runtime_sandbox.read_metadata().get(
                        "runtime_state"
                    ),
                    "runtime_schema_version": _RUNTIME_SCHEMA_VERSION,
                }
                emit_live(
                    "INFO",
                    (
                        "[ORCHESTRATION] Project Workspace "
                        f"{canonical_baseline_dir} -> Runtime Workspace "
                        f"{_runtime_sandbox.path}"
                    ),
                    metadata={
                        "phase": "runtime_workspace_allocated",
                        "workspace_mutation_skipped": True,
                        "reason": "runtime_workspace_is_execution_surface",
                        **runtime_metadata_fields,
                    },
                )
                _append_orchestration_event(
                    project_dir=_runtime_sandbox.path,
                    session_id=session_id,
                    task_id=task_id,
                    event_type=EventType.RUNTIME_WORKSPACE_ALLOCATED,
                    details=runtime_metadata_fields,
                )
            else:
                _runtime_context = _build_runtime_executor_context(
                    sandbox=None,
                    project_workspace=canonical_baseline_dir,
                    executor=_resolved_execution_backend,
                    project_id=project.id,
                    task_execution_id=task_execution_id,
                )
                orchestration_state._project_dir_override = str(
                    _runtime_context.runtime_workspace
                )
                logger.info(
                    "[ORCHESTRATION] Using canonical project root for task %s at %s",
                    task_id,
                    canonical_baseline_dir,
                )
                emit_live(
                    "INFO",
                    (
                        "[ORCHESTRATION] Using the canonical project root as the live "
                        f"workspace and will execute in {canonical_baseline_dir}"
                    ),
                    metadata={
                        "phase": "canonical_workspace",
                        "workspace_path": str(canonical_baseline_dir),
                        "workspace_mutation_skipped": True,
                        "reason": "project_root_is_source_of_truth",
                    },
                )
        elif runs_in_canonical_baseline and project and is_resume_execution:
            # Phase 23C scope note: resume executions are not redirected into
            # a Task Execution Sandbox even when RUNTIME_WORKSPACE_ENABLED is
            # True. Resuming a runtime-workspace execution would require
            # re-attaching to (or re-allocating against) a prior sandbox --
            # not specified by this phase -- so resumes keep executing
            # directly in the canonical project root, unchanged.
            canonical_baseline_dir = task_service.get_project_baseline_dir(project)
            canonical_baseline_dir.mkdir(parents=True, exist_ok=True)
            # Phase 23D-2: same RuntimeExecutorContext authority as every
            # other branch (still non-sandboxed for resumes -- Phase 23C's
            # unchanged scope decision -- but no longer a raw
            # _project_dir_override assignment bypassing the context object).
            _runtime_context = _build_runtime_executor_context(
                sandbox=None,
                project_workspace=canonical_baseline_dir,
                executor=_resolved_execution_backend,
                project_id=project.id if project else None,
                task_execution_id=task_execution_id,
            )
            orchestration_state._project_dir_override = str(
                _runtime_context.runtime_workspace
            )
            emit_live(
                "INFO",
                "[ORCHESTRATION] Resume requested; using the canonical project root and skipping pre-run baseline rebuild to preserve the current workspace",
                metadata={
                    "phase": "resume",
                    "baseline_path": str(canonical_baseline_dir),
                    "workspace_mutation_skipped": True,
                    "reason": "resume_preserve_workspace",
                },
            )

        # Create the task workspace directory if it doesn't exist
        task_workspace = orchestration_state.project_dir
        if is_resume_execution and project and task and task.task_subfolder:
            task_workspace_review = task_service.review_existing_workspace(
                project=project,
                current_task=task,
                target_dir=task_workspace,
            )
            if not task_workspace_review.get("has_existing_files"):
                project_root = task_service.get_project_root(project)
                project_root_review = task_service.review_existing_workspace(
                    project=project,
                    current_task=task,
                    target_dir=project_root,
                )
                if project_root_review.get("has_existing_files"):
                    orchestration_state._project_dir_override = str(project_root)
                    task_workspace = orchestration_state.project_dir
                    logger.warning(
                        "[ORCHESTRATION] Resume for task %s found an empty task workspace at %s; using populated project root %s instead",
                        task_id,
                        task.task_subfolder,
                        project_root,
                    )
                    emit_live(
                        "WARN",
                        (
                            "[ORCHESTRATION] Resume found no files in the task workspace "
                            f"`{task.task_subfolder}`; using the populated project root "
                            f"{project_root} instead"
                        ),
                        metadata={
                            "phase": "resume",
                            "empty_task_workspace": task.task_subfolder,
                            "fallback_project_root": str(project_root),
                        },
                    )
        if not os.path.exists(task_workspace):
            os.makedirs(task_workspace, exist_ok=True)
            logger.info("Created task workspace: %s", task_workspace)

        hydration_result = (
            task_service.hydrate_task_workspace(
                project, task, orchestration_state.project_dir
            )
            if project and task and not is_resume_execution
            else {"hydrated": False, "source_tasks": [], "files_copied": 0}
        )
        if hydration_result.get("hydrated"):
            logger.info(
                "[ORCHESTRATION] Hydrated workspace for task %s with %s files from prior tasks",
                task_id,
                hydration_result.get("files_copied", 0),
            )
            emit_live(
                "INFO",
                (
                    f"[ORCHESTRATION] Hydrated current workspace with {hydration_result.get('files_copied', 0)} "
                    "files from completed/promoted prior tasks"
                ),
                metadata={
                    "phase": "workspace_hydration",
                    "sources": hydration_result.get("source_tasks", []),
                },
            )
        elif is_resume_execution:
            emit_live(
                "INFO",
                "[ORCHESTRATION] Resume requested; skipped pre-run workspace hydration to preserve existing task files",
                metadata={
                    "phase": "resume",
                    "workspace_mutation_skipped": True,
                    "reason": "resume_preserve_workspace",
                },
            )

        workspace_snapshot_result: Optional[Dict[str, Any]] = None
        active_policy_name = get_effective_policy_profile(db=db)
        active_policy = get_policy_profile(active_policy_name)
        if project:
            workspace_snapshot_result = _snapshot_workspace_before_run(
                task_service,
                project,
                task_id,
                orchestration_state.project_dir,
                task_execution_id=task_execution_id,
                preserve_project_root_rules=runs_in_canonical_baseline,
            )
            if workspace_snapshot_result is not None:
                emit_live(
                    "INFO",
                    (
                        "[ORCHESTRATION] Captured workspace snapshot before execution "
                        f"({workspace_snapshot_result.get('files_copied', 0)} files)"
                    ),
                    metadata={
                        "phase": "workspace_snapshot",
                        "snapshot_path": workspace_snapshot_result.get("snapshot_path"),
                        "source_exists": workspace_snapshot_result.get("source_exists"),
                        "is_resume_execution": is_resume_execution,
                    },
                )

        restore_workspace_snapshot_if_needed = (
            lambda reason, force_restore=False: _restore_workspace_snapshot_if_needed(
                reason,
                project=project,
                session_id=session_id,
                task_id=task_id,
                task_execution_id=task_execution_id,
                orchestration_state=orchestration_state,
                policy_profile_name=active_policy.name,
                runs_in_canonical_baseline=runs_in_canonical_baseline,
                task_service=task_service,
                emit_live=emit_live,
                force_restore=force_restore,
                lock_already_held=runs_in_canonical_baseline,
            )
        )

        # Check if task has been running too long (safety check).
        # Skip this stale-run guard for explicit checkpoint resumes, otherwise a
        # legitimate resume after several minutes gets rejected before we even
        # load the saved orchestration state.
        if task.started_at and not is_resume_execution:
            time_since_start = datetime.now(timezone.utc) - _coerce_utc_datetime(
                task.started_at
            )
            if time_since_start.total_seconds() > STALE_RUN_GUARD_SECONDS:
                logger.warning(
                    "[ORCHESTRATION] Task %s already running for %s, marking as failed",
                    task_id,
                    time_since_start,
                )
                task_execution = get_task_execution(db, task_execution_id)
                mark_execution_failed(
                    task=task,
                    session_task_link=session_task_link,
                    task_execution=task_execution,
                    error_message=(
                        f"Task already running for {time_since_start}, "
                        "possible duplicate execution"
                    ),
                    completed_at=datetime.now(timezone.utc),
                )
                if task_execution is not None:
                    update_execution_failure_metadata(
                        db,
                        task_execution.id,
                        failure_category="lifecycle_inconsistency",
                        backend_id=_resolved_execution_backend,
                    )
                db.commit()
                raise Exception("Task timeout - already running too long")
        elif task.started_at and is_resume_execution:
            logger.info(
                "[ORCHESTRATION] Skipping stale-run timeout guard for task %s because resume checkpoint '%s' was requested",
                task_id,
                resume_checkpoint_name,
            )

        session_task_link = _get_latest_session_task_link(db, session_id, task_id)
        task_execution = get_task_execution(db, task_execution_id)

        # --- Backend concurrency slot acquisition ---
        _eff_backend = _resolved_execution_backend
        _bd = get_backend_descriptor(_eff_backend)
        if _bd.capabilities.max_parallel_sessions is not None:
            try:
                from app.services.agents.backend_concurrency import (
                    acquire_backend_slot,
                    make_redis_client,
                )

                _backend_slot_redis = make_redis_client()
                _backend_slot_backend_id = _bd.name
                _backend_slot_acquired = acquire_backend_slot(
                    _backend_slot_redis,
                    _bd.name,
                    session_id,
                    max_slots=settings.LOCAL_OPENCLAW_MAX_PARALLEL_SESSIONS,
                )
            except Exception as _redis_exc:
                logger.warning(
                    "[ORCHESTRATION] Redis slot acquisition error (non-fatal, proceeding): %s",
                    _redis_exc,
                )
                _backend_slot_acquired = True  # fail open on Redis unavailability
            if not _backend_slot_acquired:
                if task_execution is not None:
                    update_execution_failure_metadata(
                        db,
                        task_execution.id,
                        failure_category="backend_capacity_limit",
                        backend_id=_bd.name,
                    )
                capacity_retry_count, capacity_retry_exhausted = (
                    backend_capacity_retry_state(
                        getattr(self, "request", None),
                        BACKEND_CAPACITY_RETRY_MAX_RETRIES,
                    )
                )
                emit_live(
                    "ERROR" if capacity_retry_exhausted else "WARN",
                    (
                        f"[ORCHESTRATION] Backend '{_bd.name}' at capacity; "
                        + (
                            "retry budget exhausted"
                            if capacity_retry_exhausted
                            else "retrying dispatch"
                        )
                    ),
                    metadata={
                        "phase": "slot_acquisition",
                        "reason": "backend_capacity_limit",
                        "backend_id": _bd.name,
                        "failure_category": "backend_capacity_limit",
                        "retry_count": capacity_retry_count,
                        "max_retries": BACKEND_CAPACITY_RETRY_MAX_RETRIES,
                        "retry_budget_exhausted": capacity_retry_exhausted,
                    },
                )
                if capacity_retry_exhausted:
                    mark_execution_failed(
                        task=task,
                        session_task_link=session_task_link,
                        task_execution=task_execution,
                        error_message=(
                            f"Backend '{_bd.name}' remained at capacity after "
                            f"{BACKEND_CAPACITY_RETRY_MAX_RETRIES} retries"
                        ),
                        completed_at=datetime.now(timezone.utc),
                        workspace_status=(
                            "in_progress" if task.task_subfolder else "not_created"
                        ),
                    )
                    if session is not None:
                        mark_session_paused(
                            session,
                            alert_level="error",
                            alert_message=(
                                f"Backend '{_bd.name}' is at capacity and retry "
                                "budget was exhausted"
                            )[:2000],
                        )
                    db.commit()
                    return {
                        "status": "failed",
                        "reason": "backend_capacity_limit",
                        "retry_budget_exhausted": True,
                    }
                prepare_backend_capacity_retry(
                    task=task,
                    session_task_link=session_task_link,
                    task_execution=task_execution,
                    backend_id=_bd.name,
                )
                db.commit()
                raise self.retry(
                    countdown=15, max_retries=BACKEND_CAPACITY_RETRY_MAX_RETRIES
                )

        mark_execution_running(
            task=task,
            session_task_link=session_task_link,
            task_execution=task_execution,
            started_at=(
                claim_started_at or task.started_at or datetime.now(timezone.utc)
            ),
        )
        if task_execution is not None and hasattr(task_execution, "backend_id"):
            task_execution.backend_id = _resolved_execution_backend
        if task_execution is not None:
            try:
                if queued_at is not None:
                    task_execution.queued_at = queued_at
                if queue_latency_seconds is not None:
                    task_execution.queue_latency_seconds = queue_latency_seconds
            except Exception:
                pass
        db.commit()
        _write_project_state_snapshot(db, project, task, session_id)

        logger.info(
            "[ORCHESTRATION] Starting multi-step execution for task %s", task_id
        )
        emit_live(
            "INFO",
            f"[ORCHESTRATION] Starting multi-step execution for task {task_id}",
            metadata={"phase": "start"},
        )
        try:
            run_start_runtime_identity = _run_start_runtime_identity(
                db,
                runtime_selection,
            )
            _append_orchestration_event(
                project_dir=orchestration_state.project_dir,
                session_id=session_id,
                task_id=task_id,
                event_type=EventType.TASK_STARTED,
                details={
                    "execution_profile": execution_profile,
                    "task_execution_id": task_execution_id,
                    "run_start_runtime_identity": run_start_runtime_identity,
                    "run_start_config_snapshot": run_start_runtime_identity["config"],
                },
            )
        except Exception as exc:
            logger.debug(
                "[ORCHESTRATION] Failed to record task-start event for task %s: %s",
                task_id,
                exc,
            )

        # Initialize the active runtime service
        runtime_service = create_agent_runtime(
            db, session_id, task_id, role=BackendRole.EXECUTION
        )
        if hasattr(runtime_service, "task_execution_id"):
            runtime_service.task_execution_id = task_execution_id
        # Phase 23C/23D (F15): single resolved runtime path -- the execution
        # runtime must use the same Runtime Executor Context path already
        # set on orchestration_state.project_dir, not re-derive its own.
        _maybe_bind_runtime_cwd_override(runtime_service, _runtime_context)
        # Phase 23D (Goal 3): dynamically bind the executor's workspace to
        # the Runtime Executor Context. No-op for a non-sandboxed context
        # (Model A); raises OpenClawAgentSelectionError (fail closed) if a
        # sandboxed context cannot be bound -- never falls back to the
        # Project Workspace or a default agent.
        if hasattr(runtime_service, "bind_runtime_workspace"):
            runtime_service.bind_runtime_workspace(_runtime_context)
        runtime_metadata = (
            runtime_service.get_backend_metadata()
            if hasattr(runtime_service, "get_backend_metadata")
            else {}
        )
        planning_runtime_metadata = None
        use_configured_planning_runtime = should_use_configured_planning_runtime(
            planning_backend_override=planning_backend_override,
            resolved_planning_backend=resolved_planning_backend,
            resolved_execution_backend=_resolved_execution_backend,
        )
        if use_configured_planning_runtime:
            planning_runtime_service = create_agent_runtime(
                db,
                session_id,
                task_id,
                role=BackendRole.PLANNING,
                backend_override=planning_backend_override,
            )
            if hasattr(planning_runtime_service, "task_execution_id"):
                planning_runtime_service.task_execution_id = task_execution_id
            if hasattr(planning_runtime_service, "__dict__"):
                planning_runtime_service._disable_direct_planning = True
            planning_runtime_metadata = (
                planning_runtime_service.get_backend_metadata()
                if hasattr(planning_runtime_service, "get_backend_metadata")
                else {}
            )
            if planning_backend_override and session is not None:
                session.escalation_backend_id = planning_backend_override
            if planning_backend_override:
                emit_live(
                    "INFO",
                    "[ORCHESTRATION] Using operator-selected stronger planning lane for this task",
                    metadata={
                        "event_type": EventType.LANE_ESCALATION_TRIGGERED,
                        "phase": "planning",
                        "planning_backend_override": planning_backend_override,
                        **(planning_escalation_metadata or {}),
                    },
                )
            else:
                emit_live(
                    "INFO",
                    "[ORCHESTRATION] Using configured planning backend for this task",
                    metadata={
                        "phase": "planning",
                        "planning_backend": resolved_planning_backend,
                        "execution_backend": _resolved_execution_backend,
                        "planning_runtime": planning_runtime_metadata,
                    },
                )
        effective_guidance_planning_backend = (
            planning_backend_override or resolved_planning_backend
        )
        guidance_runtime_metadata = (
            planning_runtime_metadata
            if planning_runtime_metadata is not None
            else runtime_metadata
        )
        guidance_target = resolve_guidance_runtime_target(
            backend=effective_guidance_planning_backend,
            runtime_metadata=guidance_runtime_metadata,
            planning_backend=effective_guidance_planning_backend,
            execution_backend=_resolved_execution_backend,
        )
        guidance_backend = guidance_target["backend"]
        guidance_model_name = guidance_target["model_name"]
        guidance_model_family = guidance_target["model_family"]
        logger.info(
            "[HG_BACKEND] planning_backend=%s execution_backend=%s guidance_backend=%s model_name=%s model_family=%s",
            effective_guidance_planning_backend,
            _resolved_execution_backend,
            guidance_backend,
            guidance_model_name,
            guidance_model_family,
        )
        trace_context_manager = start_langfuse_observation(
            name="orchestrator-task-run",
            as_type="agent",
            input=build_text_trace_payload(prompt),
            metadata={
                "project_id": project.id if project else None,
                "project_name": project.name if project else None,
                "session_id": session_id,
                "task_id": task_id,
                "task_execution_id": task_execution_id,
                "execution_profile": execution_profile,
                "resume_checkpoint_name": resume_checkpoint_name,
                "backend": runtime_metadata.get("backend"),
                "model_family": runtime_metadata.get("model_family"),
                "adaptation_profile": runtime_metadata.get("adaptation_profile"),
            },
        )
        trace_observation = trace_context_manager.__enter__()
        if trace_observation is not None:
            logger.info(
                "[LANGFUSE] Started orchestration trace for session=%s task=%s backend=%s",
                session_id,
                task_id,
                runtime_metadata.get("backend") or "unknown",
            )
        elif langfuse_tracing_enabled():
            logger.warning(
                "[LANGFUSE] Tracing enabled but orchestration trace did not start for session=%s task=%s",
                session_id,
                task_id,
            )

        # Get session context
        session_context = asyncio.run(runtime_service.get_session_context())

        if project and project.workspace_path:
            # Phase 23C (Goal 4): when a Task Execution Sandbox is active,
            # validate against the Runtime Workspace only -- execution no
            # longer happens in the Project Workspace at all, so comparing
            # task_dir against it would be a false-positive contract failure.
            _contract_args = _resolve_workspace_contract_args(
                runtime_context=_runtime_context,
                project_workspace_path=Path(
                    resolve_project_workspace_path(project.workspace_path, project.name)
                ),
                task_subfolder=getattr(task, "task_subfolder", None),
                runs_in_canonical_baseline=runs_in_canonical_baseline,
            )
            workspace_contract = verify_workspace_contract(
                expected_root=_contract_args["expected_root"],
                task_dir=Path(orchestration_state.project_dir),
                expected_task_subfolder=_contract_args["expected_task_subfolder"],
                allow_project_root_task_dir=_contract_args[
                    "allow_project_root_task_dir"
                ],
                runtime_session_context=session_context,
            )
            if not workspace_contract.get("ok"):
                contract_error = "Workspace contract failed before execution: " + str(
                    workspace_contract.get("reason") or "unknown mismatch"
                )
                mark_execution_failed(
                    task=task,
                    session_task_link=session_task_link,
                    task_execution=task_execution,
                    error_message=contract_error,
                    completed_at=datetime.now(timezone.utc),
                    workspace_status="blocked",
                )
                if task_execution is not None:
                    update_execution_failure_metadata(
                        db,
                        task_execution.id,
                        failure_category="governance_hold",
                        backend_id=_resolved_execution_backend,
                    )
                mark_session_paused(
                    session,
                    alert_level="error",
                    alert_message=contract_error[:2000],
                )
                db.commit()
                contract_details = {
                    **workspace_contract,
                    "session_instance_id": session.instance_id,
                    **runtime_service.get_backend_metadata(),
                }
                _append_orchestration_event(
                    project_dir=orchestration_state.project_dir,
                    session_id=session_id,
                    task_id=task_id,
                    event_type=EventType.WORKSPACE_CONTRACT_FAILED,
                    details=contract_details,
                )
                emit_live(
                    "ERROR",
                    f"[ORCHESTRATION] {contract_error}",
                    metadata=contract_details,
                )
                _write_project_state_snapshot(db, project, task, session_id)
                return {"status": "failed", "reason": "workspace_contract_failed"}

        checkpoint_service = CheckpointService(db)
        resumed_from_checkpoint = False

        def _clear_resume_execution_state(error_message: str) -> None:
            orchestration_state.plan = []
            orchestration_state.current_step_index = 0
            orchestration_state.debug_attempts = []
            orchestration_state.execution_results = []
            orchestration_state.changed_files = []
            orchestration_state.completion_repair_attempts = 0
            orchestration_state.execution_recovery_attempts = 0
            orchestration_state.execution_recovery_signature_hashes = []
            orchestration_state.status = OrchestrationStatus.PLANNING
            orchestration_state.abort_reason = ""
            task.steps = None
            task.current_step = 0
            mark_execution_running(
                task=task,
                session_task_link=session_task_link,
                task_execution=task_execution,
                started_at=task.started_at or datetime.now(timezone.utc),
            )
            mark_session_running(
                session,
                alert_level="warn",
                alert_message=error_message[:2000],
            )
            db.commit()

        if resume_checkpoint_name:
            checkpoint_data = checkpoint_service.load_resume_checkpoint(
                session_id=session_id, checkpoint_name=resume_checkpoint_name
            )
            requested_resume_checkpoint_name = (
                checkpoint_data.get("_requested_checkpoint_name")
                or resume_checkpoint_name
            )
            resolved_resume_checkpoint_name = (
                checkpoint_data.get("_resolved_checkpoint_name")
                or resume_checkpoint_name
            )
            if resolved_resume_checkpoint_name != requested_resume_checkpoint_name:
                _append_orchestration_event(
                    project_dir=orchestration_state.project_dir,
                    session_id=session_id,
                    task_id=task_id,
                    event_type=EventType.CHECKPOINT_REDIRECTED,
                    details={
                        "requested_checkpoint_name": requested_resume_checkpoint_name,
                        "resolved_checkpoint_name": resolved_resume_checkpoint_name,
                    },
                )
            explicit_resume_request = bool(requested_resume_checkpoint_name)
            prompt, resume_workspace_compatibility = _apply_checkpoint_payload(
                checkpoint_data,
                orchestration_state=orchestration_state,
                task=task,
                session_id=session_id,
                task_id=task_id,
                prompt=prompt,
                emit_live=emit_live,
            )
            if orchestration_state.plan and not resume_workspace_compatibility.get(
                "compatible", True
            ):
                if not explicit_resume_request:
                    fallback_checkpoint_names = [
                        name
                        for name in ("autosave_latest", "autosave_error")
                        if name != resolved_resume_checkpoint_name
                    ]
                    fallback_applied = False
                    for fallback_name in fallback_checkpoint_names:
                        try:
                            fallback_data = checkpoint_service.load_checkpoint(
                                session_id, fallback_name
                            )
                        except Exception:
                            continue
                        prompt, fallback_compatibility = _apply_checkpoint_payload(
                            fallback_data,
                            orchestration_state=orchestration_state,
                            task=task,
                            session_id=session_id,
                            task_id=task_id,
                            prompt=prompt,
                            emit_live=emit_live,
                        )
                        if fallback_compatibility.get("compatible", True):
                            emit_live(
                                "WARN",
                                f"[ORCHESTRATION] Resume checkpoint drift detected; switching to compatible fallback checkpoint '{fallback_name}'",
                                metadata={
                                    "phase": "resume",
                                    "reason": "resume_workspace_drift_fallback",
                                    "requested_checkpoint_name": requested_resume_checkpoint_name,
                                    "resolved_checkpoint_name": resolved_resume_checkpoint_name,
                                    "fallback_checkpoint_name": fallback_name,
                                    "compatibility": resume_workspace_compatibility,
                                },
                            )
                            _append_orchestration_event(
                                project_dir=orchestration_state.project_dir,
                                session_id=session_id,
                                task_id=task_id,
                                event_type=EventType.RESUME_WORKSPACE_DRIFT,
                                details={
                                    "requested_checkpoint_name": requested_resume_checkpoint_name,
                                    "resolved_checkpoint_name": resolved_resume_checkpoint_name,
                                    "fallback_checkpoint_name": fallback_name,
                                },
                            )
                            logger.warning(
                                "[ORCHESTRATION] Resume checkpoint drift detected for task %s; switched from %s to compatible fallback %s",
                                task_id,
                                resolved_resume_checkpoint_name,
                                fallback_name,
                            )
                            resolved_resume_checkpoint_name = fallback_name
                            resume_workspace_compatibility = fallback_compatibility
                            fallback_applied = True
                            break
                    if fallback_applied:
                        pass
                    else:
                        compatibility_error = (
                            "Checkpoint plan does not match the current workspace; "
                            "discarding saved execution state and replanning from existing files"
                        )
                        logger.warning(
                            "[ORCHESTRATION] %s task=%s checkpoint=%s details=%s",
                            compatibility_error,
                            task_id,
                            resolved_resume_checkpoint_name,
                            resume_workspace_compatibility,
                        )
                        emit_live(
                            "WARN",
                            "[ORCHESTRATION] Resume checkpoint plan no longer matches the current workspace; falling back to a fresh replan from existing files",
                            metadata={
                                "phase": "resume",
                                "reason": "resume_workspace_drift",
                                "requested_checkpoint_name": requested_resume_checkpoint_name,
                                "resolved_checkpoint_name": resolved_resume_checkpoint_name,
                                "compatibility": resume_workspace_compatibility,
                            },
                        )
                        _append_orchestration_event(
                            project_dir=orchestration_state.project_dir,
                            session_id=session_id,
                            task_id=task_id,
                            event_type=EventType.RESUME_WORKSPACE_DRIFT,
                            details={
                                "requested_checkpoint_name": requested_resume_checkpoint_name,
                                "resolved_checkpoint_name": resolved_resume_checkpoint_name,
                                "compatibility": resume_workspace_compatibility,
                                "action": "replan",
                            },
                        )
                        _clear_resume_execution_state(compatibility_error)
                else:
                    compatibility_error = (
                        "Checkpoint plan does not match the current workspace; "
                        "honouring the requested checkpoint by discarding its saved execution state "
                        "and replanning from the current workspace"
                    )
                    logger.warning(
                        "[ORCHESTRATION] %s task=%s checkpoint=%s details=%s",
                        compatibility_error,
                        task_id,
                        resolved_resume_checkpoint_name,
                        resume_workspace_compatibility,
                    )
                    emit_live(
                        "WARN",
                        "[ORCHESTRATION] Requested resume checkpoint no longer matches the current workspace; "
                        "keeping the requested checkpoint context but starting a fresh replan",
                        metadata={
                            "phase": "resume",
                            "reason": "resume_workspace_drift",
                            "requested_checkpoint_name": requested_resume_checkpoint_name,
                            "resolved_checkpoint_name": resolved_resume_checkpoint_name,
                            "compatibility": resume_workspace_compatibility,
                        },
                    )
                    _append_orchestration_event(
                        project_dir=orchestration_state.project_dir,
                        session_id=session_id,
                        task_id=task_id,
                        event_type=EventType.RESUME_WORKSPACE_DRIFT,
                        details={
                            "requested_checkpoint_name": requested_resume_checkpoint_name,
                            "resolved_checkpoint_name": resolved_resume_checkpoint_name,
                            "compatibility": resume_workspace_compatibility,
                            "action": "replan_requested_checkpoint",
                        },
                    )
                    _clear_resume_execution_state(compatibility_error)

            if orchestration_state.plan:
                orchestration_state.plan = _normalize_plan_with_live_logging(
                    db,
                    session_id,
                    task_id,
                    orchestration_state.plan,
                    orchestration_state.project_dir,
                    logger,
                    session.instance_id,
                    "Checkpoint restore",
                )
                resume_plan_verdict = ValidatorService.validate_plan(
                    orchestration_state.plan,
                    output_text=json.dumps(orchestration_state.plan),
                    task_prompt=prompt,
                    execution_profile=execution_profile,
                    project_dir=orchestration_state.project_dir,
                    title=task.title if task else None,
                    description=task.description if task else None,
                    validation_severity=active_policy.validation_severity,
                    workflow_stage=getattr(task, "workflow_stage", None),
                    is_first_ordered_task=getattr(task, "plan_position", None) == 1,
                )
                _record_validation_verdict(
                    db,
                    session_id,
                    task_id,
                    orchestration_state,
                    resume_plan_verdict.verdict,
                )
                db.commit()
                if not resume_plan_verdict.accepted:
                    resume_error = (
                        "Checkpoint plan failed validation on resume: "
                        + "; ".join(resume_plan_verdict.reasons[:3])
                    )
                    logger.warning(
                        "[ORCHESTRATION] %s Falling back to a fresh replan from the current workspace.",
                        resume_error,
                    )
                    emit_live(
                        "WARN",
                        "[ORCHESTRATION] Resume checkpoint plan failed validation; falling back to a fresh replan from the current workspace",
                        metadata={
                            "phase": "resume",
                            "reason": "invalid_resume_plan",
                            "validation_status": resume_plan_verdict.status,
                            "reasons": resume_plan_verdict.reasons[:10],
                        },
                    )
                    _clear_resume_execution_state(resume_error)

            resumed_from_checkpoint = bool(orchestration_state.plan)
            if resumed_from_checkpoint:
                logger.info(
                    "[ORCHESTRATION] Resuming from checkpoint '%s' at step index %s",
                    resolved_resume_checkpoint_name,
                    orchestration_state.current_step_index,
                )
                emit_live(
                    "INFO",
                    f"[ORCHESTRATION] Resuming from checkpoint '{resolved_resume_checkpoint_name}' at step index {orchestration_state.current_step_index}",
                    metadata={
                        "phase": "resume",
                        "requested_checkpoint_name": requested_resume_checkpoint_name,
                        "resolved_checkpoint_name": resolved_resume_checkpoint_name,
                    },
                )

        refreshed_project_context = _build_base_project_context(
            task_service, project, task, hydration_result
        )
        workspace_review = task_service.review_existing_workspace(
            project=project,
            current_task=task,
            target_dir=orchestration_state.project_dir,
        )
        if workspace_review.get("has_existing_files"):
            refreshed_project_context = (
                f"{refreshed_project_context}\n"
                f"{workspace_review.get('summary', '')}"
            )[:7000]
            emit_live(
                "INFO",
                "[ORCHESTRATION] Reviewed existing task workspace before planning",
                metadata={
                    "phase": "workspace_review",
                    "file_count": workspace_review.get("file_count", 0),
                    "source_file_count": workspace_review.get("source_file_count", 0),
                    "placeholder_issue_count": workspace_review.get(
                        "placeholder_issue_count", 0
                    ),
                },
            )
        orchestration_state.project_context = refreshed_project_context
        validation_profile = ValidatorService.infer_validation_profile(
            prompt,
            execution_profile,
            title=task.title if task else None,
            description=task.description if task else None,
        )

        run_ctx = OrchestrationRunContext(
            db=db,
            session=session,
            project=project,
            task=task,
            session_task_link=session_task_link,
            session_id=session_id,
            task_id=task_id,
            prompt=prompt,
            timeout_seconds=timeout_seconds,
            execution_profile=execution_profile,
            validation_profile=validation_profile,
            runs_in_canonical_baseline=runs_in_canonical_baseline,
            orchestration_state=orchestration_state,
            runtime_service=runtime_service,
            task_service=task_service,
            logger=logger,
            emit_live=emit_live,
            error_handler=error_handler,
            policy_profile_name=active_policy.name,
            validation_severity=active_policy.validation_severity,
            completion_repair_budget=active_policy.completion_repair_budget,
            workflow_stage=getattr(task, "workflow_stage", None),
            task_execution_id=task_execution_id,
            restore_workspace_snapshot_if_needed=restore_workspace_snapshot_if_needed,
            planning_backend=effective_guidance_planning_backend,
            execution_backend=_resolved_execution_backend,
            guidance_backend=guidance_backend,
            guidance_model_name=guidance_model_name,
            guidance_model_family=guidance_model_family,
        )

        gate_error = _run_virtual_merge_gate(
            db=db,
            project=project,
            current_task=task,
            execution_profile=execution_profile,
            get_state_manager_path_fn=_get_state_manager_path,
        )
        if gate_error:
            orchestration_state.status = OrchestrationStatus.ABORTED
            orchestration_state.abort_reason = gate_error
            task_execution = get_task_execution(db, task_execution_id)
            mark_execution_failed(
                task=task,
                session_task_link=session_task_link,
                task_execution=task_execution,
                error_message=gate_error,
                completed_at=datetime.now(timezone.utc),
            )
            if task_execution is not None:
                update_execution_failure_metadata(
                    db,
                    task_execution.id,
                    failure_category="governance_hold",
                    backend_id=_resolved_execution_backend,
                )
            mark_session_paused(
                session,
                alert_level="error",
                alert_message=gate_error[:2000],
            )
            db.commit()
            restore_workspace_snapshot_if_needed("virtual merge gate failure")
            _write_project_state_snapshot(db, project, task, session_id)
            return {"status": "failed", "reason": "virtual_merge_gate_failed"}

        if (
            not resumed_from_checkpoint
            and task
            and task.steps
            and not orchestration_state.plan
        ):
            try:
                stored_plan_payload = json.loads(task.steps)
                stored_plan = _extract_plan_steps(stored_plan_payload)
                if stored_plan:
                    orchestration_state.plan = _normalize_plan_with_live_logging(
                        db,
                        session_id,
                        task_id,
                        stored_plan,
                        orchestration_state.project_dir,
                        logger,
                        session.instance_id,
                        "Stored task plan",
                    )
                    stored_plan_verdict = ValidatorService.validate_plan(
                        orchestration_state.plan,
                        output_text=json.dumps(orchestration_state.plan),
                        task_prompt=prompt,
                        execution_profile=execution_profile,
                        project_dir=orchestration_state.project_dir,
                        title=task.title if task else None,
                        description=task.description if task else None,
                        validation_severity=active_policy.validation_severity,
                        workflow_stage=getattr(task, "workflow_stage", None),
                        is_first_ordered_task=getattr(task, "plan_position", None) == 1,
                    )
                    _record_validation_verdict(
                        db,
                        session_id,
                        task_id,
                        orchestration_state,
                        stored_plan_verdict.verdict,
                    )
                    db.commit()
                    if not stored_plan_verdict.accepted:
                        logger.warning(
                            "[ORCHESTRATION] Stored plan for task %s failed validation: %s",
                            task_id,
                            "; ".join(stored_plan_verdict.reasons[:3]),
                        )
                        emit_live(
                            "WARN",
                            "[ORCHESTRATION] Saved plan failed validation; discarding and replanning",
                            metadata={
                                "phase": "planning",
                                "source": "stored_task_plan",
                                "validation_status": stored_plan_verdict.status,
                                "reasons": stored_plan_verdict.reasons[:10],
                            },
                        )
                        orchestration_state.plan = []
                        task.steps = None
                        db.commit()
                    else:
                        logger.info(
                            "[ORCHESTRATION] Reusing stored plan for task %s with %s steps",
                            task_id,
                            len(orchestration_state.plan),
                        )
                        emit_live(
                            "INFO",
                            f"[ORCHESTRATION] Reusing saved plan with {len(orchestration_state.plan)} steps",
                            metadata={
                                "phase": "planning",
                                "source": "stored_task_plan",
                            },
                        )
            except Exception as stored_plan_error:
                logger.warning(
                    "[ORCHESTRATION] Failed to reuse stored plan for task %s: %s",
                    task_id,
                    stored_plan_error,
                )

        if not resumed_from_checkpoint and not orchestration_state.plan:
            _inject_progress_notes_into_context(
                orchestration_state=orchestration_state,
                logger=logger,
            )
            if settings.PSS_CONTINUATION_INJECTION_ENABLED and project:
                from app.services.project.state_summary import (
                    _inject_project_state_summary_into_context,
                )

                _inject_project_state_summary_into_context(
                    orchestration_state=orchestration_state,
                    db=db,
                    project_id=project.id,
                    logger=logger,
                    task_position=getattr(task, "plan_position", None),
                    include_artifacts=not settings.ARTIFACT_CONTINUATION_ENABLED,
                )
            if settings.ARTIFACT_CONTINUATION_ENABLED and project:
                from app.services.project.state_summary import (
                    _inject_project_artifacts_into_context,
                )

                _inject_project_artifacts_into_context(
                    orchestration_state=orchestration_state,
                    db=db,
                    project_id=project.id,
                    logger=logger,
                    task_position=getattr(task, "plan_position", None),
                )
            if settings.WORKING_MEMORY_INJECTION_ENABLED:
                from app.services.orchestration.working_memory import (
                    inject_working_memory_into_context,
                )

                # P1f: check per-project/session injection activation (non-fatal)
                _inject_ok = True
                if project:
                    try:
                        from app.services.human_guidance.activation import (
                            check_activation_flag as _check_act,
                        )

                        _inject_ok = _check_act(
                            db,
                            project_id=getattr(project, "id", None),
                            session_id=session_id,
                            flag="injection_enabled",
                        )
                    except Exception:
                        pass

                if _inject_ok:
                    inject_working_memory_into_context(
                        orchestration_state=orchestration_state,
                        task=task,
                        logger=logger,
                    )
                else:
                    logger.info(
                        "[HG_RUNTIME_PATH] wm_injection=off (activation disabled"
                        " for project=%s session=%s)",
                        getattr(project, "id", None),
                        session_id,
                    )
            if settings.REPO_MEMORY_INJECTION_ENABLED:
                from app.services.orchestration.repo_memory import (
                    inject_repo_memory_into_context,
                )

                inject_repo_memory_into_context(
                    orchestration_state=orchestration_state,
                    logger=logger,
                )
            # HG-P1c-2: guidance conflict detection (warning-only, non-blocking).
            if project and task:
                from app.services.human_guidance.conflicts import (
                    run_conflict_detection_if_enabled,
                )

                run_conflict_detection_if_enabled(
                    db=db,
                    project_id=project.id,
                    session_id=session_id,
                    task_id=task_id,
                    user_id=getattr(project, "user_id", None),
                    task_title=getattr(task, "title", "") or "",
                    task_description=getattr(task, "description", "") or "",
                    backend=run_ctx.guidance_backend,
                    model_family=run_ctx.guidance_model_family,
                )
            # Slice J: incremental execution path (creation-only prototype, flag-gated).
            # Runs after context injection; before execute_planning_phase.
            # Falls back to full planning on any failure.
            _incremental_route_taken = False
            if settings.INCREMENTAL_EXECUTION_ENABLED:
                _inc_task_desc = getattr(task, "description", None) or prompt or ""
                from app.services.orchestration.planning.incremental_classifier import (
                    is_incremental_candidate as _is_incremental_candidate,
                )

                if _is_incremental_candidate(_inc_task_desc):
                    from app.services.orchestration.phases.incremental_flow import (
                        attempt_incremental_execution,
                    )

                    _inc_result = attempt_incremental_execution(
                        ctx=run_ctx,
                        task_description=_inc_task_desc,
                    )
                    if _inc_result.get("status") == "completed":
                        _incremental_route_taken = True
                        planning_phase_result = {"status": "completed"}
            with start_langfuse_observation(
                name="planning-phase",
                as_type="span",
                input={
                    "prompt_chars": len(prompt or ""),
                    "project_context_chars": len(
                        orchestration_state.project_context or ""
                    ),
                },
                metadata={
                    "session_id": session_id,
                    "task_id": task_id,
                    "execution_profile": execution_profile,
                    "phase": "planning",
                },
            ) as planning_phase_observation:
                # HG-P2c: emit backend/HG coverage diagnostic before planning.
                try:
                    from app.services.orchestration.phases.planning_guidance_enforcement import (
                        emit_hg_p2b_worker_coverage as _emit_hg_coverage,
                    )
                    from app.config import settings as _cfg

                    _emit_hg_coverage(
                        execution_backend=_resolved_execution_backend,
                        resolved_planning_backend=resolved_planning_backend,
                        use_configured_planning_runtime=use_configured_planning_runtime,
                        hg_table_enabled=_cfg.HUMAN_GUIDANCE_TABLE_ENABLED,
                        logger=logger,
                    )
                except Exception:
                    pass  # diagnostic only — never block planning

                if not _incremental_route_taken:
                    planning_phase_result = _PlanningCoordinator().run_planning(
                        ctx=run_ctx,
                        workspace_review=workspace_review,
                        extract_structured_text=_extract_structured_text,
                        extract_plan_steps=_extract_plan_steps,
                        looks_like_truncated_multistep_plan=_looks_like_truncated_multistep_plan,
                        normalize_plan_with_live_logging=_normalize_plan_with_live_logging,
                        workspace_violation_error_cls=TaskWorkspaceViolationError,
                        planning_runtime_service=planning_runtime_service,
                    )
                if not _incremental_route_taken:
                    update_langfuse_observation(
                        planning_phase_observation,
                        output=planning_phase_result,
                        metadata={
                            "phase": "planning",
                            "plan_steps": len(orchestration_state.plan or []),
                        },
                        level=(
                            "ERROR"
                            if planning_phase_result.get("status") == "failed"
                            else None
                        ),
                        status_message=(
                            str(planning_phase_result.get("reason") or "")[:500] or None
                        ),
                    )
                if not _incremental_route_taken and planning_backend_override:
                    escalation_result_metadata = {
                        "event_type": EventType.LANE_ESCALATION_RESULT,
                        "phase": "planning",
                        "planning_backend_override": planning_backend_override,
                        "planning_runtime": planning_runtime_metadata,
                        "validation_result": planning_phase_result.get("status"),
                        "result_reason": planning_phase_result.get("reason"),
                        "plan_steps": len(orchestration_state.plan or []),
                        **(planning_escalation_metadata or {}),
                    }
                    emit_live(
                        "INFO",
                        "[ORCHESTRATION] Stronger planning lane returned to the normal acceptance gate",
                        metadata=escalation_result_metadata,
                    )
                    try:
                        _append_orchestration_event(
                            project_dir=orchestration_state.project_dir,
                            session_id=session_id,
                            task_id=task_id,
                            event_type=EventType.LANE_ESCALATION_RESULT,
                            details=escalation_result_metadata,
                        )
                    except Exception as exc:
                        logger.debug(
                            "[ORCHESTRATION] Failed to record lane escalation result: %s",
                            exc,
                        )
                    if session is not None:
                        session.escalation_backend_id = None
                        db.add(session)
                        db.commit()
            if planning_phase_result.get("status") != "completed":
                mark_session_paused(
                    session,
                    alert_level="error",
                    alert_message=str(
                        planning_phase_result.get("reason") or "planning_failed"
                    )[:2000],
                )
                db.commit()
                update_langfuse_observation(
                    trace_observation,
                    output=planning_phase_result,
                    level=(
                        "ERROR"
                        if planning_phase_result.get("status") == "failed"
                        else None
                    ),
                    status_message=str(planning_phase_result.get("reason") or "")[:500]
                    or None,
                )
                _emit_task1_product_event(
                    "task1_execution_failed",
                    reason=str(
                        planning_phase_result.get("reason") or "planning_failed"
                    ),
                    level="WARN",
                )
                return planning_phase_result

        _save_orchestration_checkpoint(
            db, session_id, task_id, prompt, orchestration_state
        )

        with start_langfuse_observation(
            name="execution-phase",
            as_type="span",
            input={
                "planned_steps": len(orchestration_state.plan or []),
                "current_step_index": orchestration_state.current_step_index,
            },
            metadata={
                "session_id": session_id,
                "task_id": task_id,
                "execution_profile": execution_profile,
                "phase": "executing",
            },
        ) as execution_phase_observation:
            step_loop_result = _ExecutionCoordinator().run_execution(
                ctx=run_ctx,
                extract_structured_text=_extract_structured_text,
                normalize_step=_normalize_step,
                normalize_plan_with_live_logging=_normalize_plan_with_live_logging,
                workspace_violation_error_cls=TaskWorkspaceViolationError,
                write_project_state_snapshot_fn=_write_project_state_snapshot,
                record_live_log_fn=_record_live_log,
            )
            update_langfuse_observation(
                execution_phase_observation,
                output=step_loop_result,
                metadata={
                    "phase": "executing",
                    "completed_steps": len(
                        getattr(orchestration_state, "completed_steps", []) or []
                    ),
                },
                level=("ERROR" if step_loop_result.get("status") == "failed" else None),
                status_message=str(step_loop_result.get("reason") or "")[:500] or None,
            )
        if step_loop_result.get("status") == "completed":
            with start_langfuse_observation(
                name="task-summary-phase",
                as_type="span",
                input={
                    "completed_steps": len(
                        getattr(orchestration_state, "completed_steps", []) or []
                    ),
                    "execution_results": len(
                        orchestration_state.execution_results or []
                    ),
                },
                metadata={
                    "session_id": session_id,
                    "task_id": task_id,
                    "phase": "task_summary",
                    "execution_profile": execution_profile,
                },
            ) as task_summary_observation:
                step_loop_result = _CompletionCoordinator().complete_task(
                    ctx=run_ctx,
                    write_project_state_snapshot_fn=_write_project_state_snapshot,
                    get_next_pending_project_task_fn=_get_next_pending_project_task,
                    get_latest_session_task_link_fn=_get_latest_session_task_link,
                    execute_orchestration_task_delay_fn=execute_orchestration_task.delay,
                    build_task_report_payload_fn=_build_task_report_payload,
                    render_task_report_fn=_render_task_report,
                )
                update_langfuse_observation(
                    task_summary_observation,
                    output=step_loop_result,
                    metadata={"phase": "task_summary"},
                    level=(
                        "ERROR" if step_loop_result.get("status") == "failed" else None
                    ),
                    status_message=str(step_loop_result.get("reason") or "")[:500]
                    or None,
                )
        update_langfuse_observation(
            trace_observation,
            output=step_loop_result,
            level="ERROR" if step_loop_result.get("status") == "failed" else None,
            status_message=str(step_loop_result.get("reason") or "")[:500] or None,
        )
        if step_loop_result.get("status") == "failed":
            _emit_task1_product_event(
                "task1_execution_failed",
                reason=str(step_loop_result.get("reason") or "task_failed"),
                level="WARN",
            )
            failure_reason = str(step_loop_result.get("reason") or "task_failed")
            if failure_reason == TerminalReason.VERIFICATION_FAILED:
                mark_session_failed(
                    session,
                    failed_at=datetime.now(timezone.utc),
                    alert_level="error",
                    alert_message=failure_reason[:2000],
                )
            else:
                mark_session_paused(
                    session,
                    alert_level="error",
                    alert_message=failure_reason[:2000],
                )
            db.commit()
        elif step_loop_result.get("status") == "completed":
            _emit_task1_product_event(
                "task1_execution_succeeded",
                reason=str(step_loop_result.get("reason") or "completed"),
            )
        return step_loop_result

    except Exception as exc:
        from celery.exceptions import Retry as _CeleryRetry

        if isinstance(exc, _CeleryRetry):
            raise
        try:
            _fail_cat = classify_failure(str(exc), _resolved_execution_backend, {})
            if task_execution_id is not None:
                update_execution_failure_metadata(
                    db,
                    task_execution_id,
                    failure_category=_fail_cat,
                    backend_id=_resolved_execution_backend,
                )
            if _fail_cat == "backend_timeout":
                try:
                    emit_live(
                        "ERROR",
                        "[ORCHESTRATION] Backend timeout reached before backend completion; timeout remains authoritative",
                        metadata={
                            "phase": "execution_timeout",
                            "reason": "backend_timeout",
                            "backend_id": _resolved_execution_backend,
                            "failure_category": "backend_timeout",
                            "terminal_reason": "timeout_before_backend_completion",
                        },
                    )
                except Exception:
                    pass
        except Exception:
            pass
        update_langfuse_observation(
            trace_observation,
            output={"status": "failed", "reason": "exception"},
            metadata={"exception_type": exc.__class__.__name__},
            level="ERROR",
            status_message=str(exc)[:500],
        )
        _FailureCoordinator().handle_failure(
            self_task=self,
            ctx=(
                run_ctx
                if run_ctx is not None
                else (
                    OrchestrationRunContext(
                        db=db,
                        session=session,
                        project=project,
                        task=task,
                        session_task_link=session_task_link,
                        session_id=session_id,
                        task_id=task_id,
                        prompt=prompt,
                        timeout_seconds=timeout_seconds,
                        execution_profile=execution_profile,
                        validation_profile=validation_profile,
                        runs_in_canonical_baseline=runs_in_canonical_baseline,
                        orchestration_state=orchestration_state,
                        runtime_service=None,
                        task_service=None,
                        logger=logger,
                        emit_live=lambda *_args, **_kwargs: None,
                        error_handler=error_handler,
                        policy_profile_name=(
                            active_policy.name
                            if active_policy is not None
                            else "balanced"
                        ),
                        validation_severity=(
                            active_policy.validation_severity
                            if active_policy is not None
                            else "standard"
                        ),
                        completion_repair_budget=(
                            active_policy.completion_repair_budget
                            if active_policy is not None
                            else 1
                        ),
                        workflow_stage=getattr(task, "workflow_stage", None),
                        task_execution_id=task_execution_id,
                        restore_workspace_snapshot_if_needed=restore_workspace_snapshot_if_needed,
                    )
                    if session and task
                    else None
                )
            ),
            exc=exc,
            get_latest_session_task_link_fn=_get_latest_session_task_link,
            queue_task_for_session_fn=_queue_task_for_session,
        )

    finally:
        try:
            if planning_backend_override and session is not None:
                session.escalation_backend_id = None
                db.add(session)
            if project and task and task_execution_id and orchestration_state:
                task_service_for_change_set = TaskService(db)
                task_service_for_change_set.persist_task_execution_change_set(
                    project,
                    task,
                    session_id=session_id,
                    task_execution_id=task_execution_id,
                    snapshot_key=_workspace_snapshot_key(task_id, task_execution_id),
                    target_dir=Path(orchestration_state.project_dir),
                    preserve_project_root_rules=runs_in_canonical_baseline,
                    status=getattr(getattr(task, "status", None), "value", None),
                    commit=False,
                )
            _sync_task_execution_from_task_state(
                db,
                task_execution_id,
                task=task,
                session_task_link=session_task_link,
            )
        except Exception as sync_exc:
            logger.warning(
                "[ORCHESTRATION] Failed to sync task execution %s: %s",
                task_execution_id,
                sync_exc,
            )
        if (
            _backend_slot_acquired
            and _backend_slot_redis is not None
            and _backend_slot_backend_id
        ):
            try:
                from app.services.agents.backend_concurrency import release_backend_slot

                release_backend_slot(
                    _backend_slot_redis, _backend_slot_backend_id, session_id
                )
            except Exception as _rel_exc:
                logger.warning(
                    "[ORCHESTRATION] Failed to release backend slot for %s: %s",
                    _backend_slot_backend_id,
                    _rel_exc,
                )
        if runtime_service is not None and hasattr(
            runtime_service, "release_runtime_workspace_binding"
        ):
            # Phase 23D: discard the ephemeral executor workspace binding (if
            # any was established) on every terminal path, mirroring the
            # sandbox disposal below. Never raises.
            runtime_service.release_runtime_workspace_binding()
        if _runtime_sandbox is not None:
            # Phase 23C (Goal 5): dispose the Task Execution Sandbox on every
            # terminal path -- success, failure, cancellation, timeout, or
            # exception all reach this `finally`. No promotion of sandbox
            # content back to the Project Workspace happens here or anywhere
            # else in this phase; disposal is unconditional and never raises.
            _disposed_path = _runtime_sandbox.path
            _dispose_runtime_workspace_safely(
                _runtime_sandbox,
                project_root=_runtime_sandbox_project_root,
                logger_obj=logger,
            )
            logger.info(
                "[ORCHESTRATION] Disposed Runtime Workspace %s for task %s "
                "(task_execution_id=%s)",
                _disposed_path,
                task_id,
                task_execution_id,
            )
            if _runtime_sandbox_project_root is not None:
                try:
                    _append_orchestration_event(
                        project_dir=_runtime_sandbox_project_root,
                        session_id=session_id,
                        task_id=task_id,
                        event_type=EventType.RUNTIME_WORKSPACE_DISPOSED,
                        details={
                            "runtime_workspace": str(_disposed_path),
                            "project_workspace": str(_runtime_sandbox_project_root),
                        },
                    )
                except Exception:
                    pass
        if project_mutation_lock_context is not None:
            project_mutation_lock_context.__exit__(None, None, None)
        if trace_context_manager is not None:
            trace_context_manager.__exit__(None, None, None)
        flush_langfuse()
        db.close()


# Backward-compatible alias for older imports and serialized task references.
execute_openclaw_task = execute_orchestration_task

# Maintenance tasks re-exported for backward compatibility with older imports.
from app.tasks.maintenance import (  # noqa: E402
    cleanup_old_logs,
    generate_task_report,
    process_github_webhook,
    scheduled_task_execution,
    sweep_orphaned_running_sessions,
)

__all__ = [
    "answer_human_intervention_query",
    "cleanup_old_logs",
    "execute_openclaw_task",
    "execute_orchestration_task",
    "generate_task_report",
    "process_github_webhook",
    "scheduled_task_execution",
    "sweep_orphaned_running_sessions",
]


@celery_app.task(bind=True, max_retries=1, default_retry_delay=5)
def answer_human_intervention_query(
    self, intervention_id: int, session_id: int
) -> None:
    """Ask the AI to respond to an operator query submitted via 'Request Review'.

    Stores the AI's response in InterventionRequest.context_snapshot and emits
    live logs so the frontend shows progress: one "thinking" log immediately,
    then the answer when the LLM call completes.
    """
    from app.models import InterventionRequest
    from app.services.agents.agent_runtime import invoke_runtime_prompt

    db = get_db_session()
    try:
        session = get_session_or_404(db, session_id)
        req = (
            db.query(InterventionRequest)
            .filter(InterventionRequest.id == intervention_id)
            .first()
        )
        if not req or req.status != "pending":
            return

        project = db.query(Project).filter(Project.id == req.project_id).first()
        project_name = project.name if project else f"project-{req.project_id}"

        human_question = req.prompt or ""

        # Emit immediately so the frontend shows "AI is working" rather than a
        # silent spinner for the full duration of the LLM call.
        _record_live_log(
            db,
            session_id,
            req.task_id,
            "INFO",
            "[OPERATOR-QUERY] Processing operator question…",
            session_instance_id=session.instance_id,
            metadata={
                "phase": "human_intervention",
                "intervention_id": intervention_id,
                "status": "processing",
                "human_question": human_question,
            },
        )
        db.commit()

        ai_prompt = (
            f"An operator has submitted a question mid-session. "
            f"Project: {project_name}. "
            f"Operator question: {human_question}\n\n"
            f"Answer concisely and helpfully. If you need workspace context to answer, "
            f"say so and describe what you would need. Do NOT execute any commands."
        )

        result = invoke_runtime_prompt(
            db,
            ai_prompt,
            session_id=session_id,
            task_id=req.task_id,
            timeout_seconds=90,
            session_prefix="human_intervention",
        )
        ai_answer = str((result or {}).get("output", "")).strip() or "(No response)"

        # Store AI answer in context_snapshot as JSON while preserving non-object snapshots.
        existing = _decode_context_snapshot_object(req.context_snapshot)
        existing["ai_response"] = ai_answer
        req.context_snapshot = json.dumps(existing)
        db.commit()

        # Emit the completed answer so WebSocket picks it up
        _record_live_log(
            db,
            session_id,
            req.task_id,
            "INFO",
            f"[OPERATOR-QUERY] AI response to operator question: {ai_answer[:500]}",
            session_instance_id=session.instance_id,
            metadata={
                "phase": "human_intervention",
                "intervention_id": intervention_id,
                "status": "answered",
                "ai_response": ai_answer,
                "human_question": human_question,
            },
        )
        db.commit()
        logger.info(
            "AI answered operator intervention %s for session %s",
            intervention_id,
            session_id,
        )
    except Exception as exc:
        logger.error("answer_human_intervention_query failed: %s", exc)
    finally:
        db.close()
