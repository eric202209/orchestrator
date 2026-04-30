"""Celery Worker Tasks

Background task processing for the orchestrator.
Implements multi-step orchestration workflow:
PLANNING → EXECUTING (step-by-step) → DEBUGGING (on failure) → PLAN_REVISION → DONE
"""

import os
import logging
import json
import asyncio
import time
from pathlib import Path
from typing import Optional, Dict, Any, List
from datetime import datetime, timezone
from sqlalchemy.orm import Session
from app.celery_app import celery_app
from app.models import (
    Session as SessionModel,
    SessionTask,
    Task,
    TaskStatus,
    LogEntry,
    Project,
)
from app.database import get_db_session
from app.services import (
    create_agent_runtime,
    build_task_subfolder_name,
)
from app.services.orchestration import (
    STALE_RUN_GUARD_SECONDS,
    OrchestrationRunContext,
    TaskWorkspaceViolationError,
    ValidatorService,
    build_task_report_payload as _build_task_report_payload,
    execute_planning_phase,
    execute_step_loop,
    extract_plan_steps as _extract_plan_steps,
    extract_structured_text as _extract_structured_text,
    handle_task_failure,
    looks_like_truncated_multistep_plan as _looks_like_truncated_multistep_plan,
    normalize_plan_with_live_logging as _normalize_plan_with_live_logging,
    normalize_step as _normalize_step,
    render_task_report as _render_task_report,
    run_virtual_merge_gate as _run_virtual_merge_gate,
    should_execute_in_canonical_project_root as _should_execute_in_canonical_project_root,
    should_restore_workspace_on_failure,
    should_force_review_execution_profile as _should_force_review_execution_profile,
)
from app.services.orchestration.context_assembly import (
    collect_workspace_inventory_paths,
    sanitize_progress_notes_for_workspace,
)
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.persistence import (
    append_orchestration_event as _append_orchestration_event,
    find_latest_orchestration_event as _find_latest_orchestration_event,
    record_live_log as _record_live_log,
    record_validation_verdict as _record_validation_verdict,
    restore_step_result as _restore_step_result,
    save_orchestration_checkpoint as _save_orchestration_checkpoint,
    set_session_alert as _set_session_alert,
)
from app.services.orchestration.execution.runtime import (
    get_state_manager_path as _get_state_manager_path,
    restore_workspace_after_abort as _restore_workspace_after_abort,
    snapshot_workspace_before_run as _snapshot_workspace_before_run,
    write_project_state_snapshot as _write_project_state_snapshot,
)
from app.services.error_handler import error_handler
from app.services.workspace.checkpoint_service import CheckpointService
from app.services.workspace.project_isolation_service import (
    resolve_project_workspace_path,
)
from app.services.task_service import TaskService
from app.services.prompt_templates import (
    OrchestrationStatus,
    OrchestrationState,
)
from app.services.session.session_runtime_service import (
    queue_task_for_session as _queue_task_for_session,
)
from app.services.orchestration.policy import get_policy_profile
from app.services.orchestration.policy import (
    ORCHESTRATION_TASK_SOFT_TIME_LIMIT_SECONDS,
    ORCHESTRATION_TASK_TIME_LIMIT_SECONDS,
)
from app.services.workspace.system_settings import get_effective_policy_profile
from app.services.workspace.system_settings import (
    get_effective_agent_backend,
    get_effective_agent_model_family,
)
from app.config import settings
from app.services.orchestration.validation.workspace_guard import (
    verify_workspace_contract,
)

logger = logging.getLogger(__name__)

_PROGRESS_NOTES_MAX_BYTES = 8000
_STALE_QUEUE_CLAIM_SLA_SECONDS = 900


def _parse_event_timestamp(raw_timestamp: Optional[str]) -> Optional[datetime]:
    text = str(raw_timestamp or "").strip()
    if not text:
        return None
    try:
        parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        return parsed.replace(tzinfo=timezone.utc)
    return parsed


def _coerce_utc_datetime(value: Optional[datetime]) -> Optional[datetime]:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _inject_progress_notes_into_context(
    *,
    orchestration_state: Any,
    logger: Any,
) -> None:
    """Read .openclaw/progress_notes.md and prepend it to project_context.

    This is the orient phase from best-practices: give the planner full history
    of what previous task runs already accomplished so it does not repeat work
    or contradict earlier decisions.
    """
    from pathlib import Path

    project_dir = getattr(orchestration_state, "project_dir", None)
    if not project_dir:
        return
    notes_path = Path(project_dir) / ".openclaw" / "progress_notes.md"
    if not notes_path.exists():
        return
    try:
        notes_text = notes_path.read_text(encoding="utf-8", errors="replace")
        sanitized_notes = sanitize_progress_notes_for_workspace(
            notes_text,
            Path(project_dir),
        )
        workspace_inventory = collect_workspace_inventory_paths(
            Path(project_dir),
            max_files=40,
        )
        # Keep only the tail so large histories don't flood the context window.
        if len(sanitized_notes) > _PROGRESS_NOTES_MAX_BYTES:
            sanitized_notes = (
                "...(truncated)\n" + sanitized_notes[-_PROGRESS_NOTES_MAX_BYTES:]
            )
        workspace_truth = ["=== CURRENT WORKSPACE TRUTH ==="]
        if workspace_inventory:
            workspace_truth.extend(f"- {path}" for path in workspace_inventory[:40])
        else:
            workspace_truth.append("- No tracked files detected yet.")
        prefix = (
            "=== PRIOR SESSION PROGRESS NOTES ===\n"
            + sanitized_notes.strip()
            + "\n=== END PRIOR SESSION PROGRESS NOTES ===\n\n"
            + "\n".join(workspace_truth)
            + "\n=== END CURRENT WORKSPACE TRUTH ===\n\n"
        )
        current = orchestration_state.project_context or ""
        orchestration_state.project_context = (prefix + current)[:8000]
        logger.info("[ORIENT] Injected progress notes from %s", notes_path)
    except Exception as e:
        logger.warning("[ORIENT] Failed to read progress notes: %s", e)


def _get_next_pending_project_task(
    db: Session, project_id: Optional[int]
) -> Optional[Task]:
    if not project_id:
        return None
    return TaskService(db).get_next_pending_task(project_id)


def _get_latest_session_task_link(
    db: Session, session_id: int, task_id: int
) -> Optional[SessionTask]:
    return (
        db.query(SessionTask)
        .filter(
            SessionTask.session_id == session_id,
            SessionTask.task_id == task_id,
        )
        .order_by(SessionTask.id.desc())
        .first()
    )


def _runtime_selection_details(db: Session) -> Dict[str, Optional[str]]:
    return {
        "backend": get_effective_agent_backend(
            settings.ORCHESTRATOR_AGENT_BACKEND, db=db
        ),
        "model_family": get_effective_agent_model_family(
            settings.ORCHESTRATOR_AGENT_MODEL_FAMILY, db=db
        ),
    }


def _should_reject_stale_dispatch_claim(
    *,
    dispatch_project_dir: Optional[Path],
    session_id: int,
    task_id: int,
    queued_event: Optional[Dict[str, Any]],
    queue_latency_seconds: Optional[float],
) -> Optional[str]:
    if not dispatch_project_dir or not queued_event:
        return None
    if (
        queue_latency_seconds is None
        or queue_latency_seconds <= _STALE_QUEUE_CLAIM_SLA_SECONDS
    ):
        return None

    queued_at = _parse_event_timestamp((queued_event or {}).get("timestamp"))
    if queued_at is None:
        return "stale_queue_dispatch"

    latest_post_queue_event = _find_latest_orchestration_event(
        dispatch_project_dir,
        session_id,
        task_id,
        event_types={
            EventType.TASK_CLAIMED,
            EventType.TASK_STARTED,
            EventType.PHASE_STARTED,
            EventType.PHASE_FINISHED,
        },
    )
    latest_post_queue_at = _parse_event_timestamp(
        (latest_post_queue_event or {}).get("timestamp")
    )
    if latest_post_queue_at is not None and latest_post_queue_at >= queued_at:
        return "stale_queue_dispatch_already_progressed"
    return "stale_queue_dispatch"


def _claim_queued_task_for_worker(
    *,
    db: Session,
    session: SessionModel,
    task: Task,
    session_task_link: Optional[SessionTask],
    expected_session_instance_id: Optional[str],
) -> tuple[bool, str, Optional[datetime], Optional[SessionTask]]:
    """Claim a queued task exactly once for the current worker dispatch."""

    if (
        expected_session_instance_id
        and session.instance_id != expected_session_instance_id
    ):
        db.rollback()
        return False, "session_instance_changed", None, session_task_link

    if session.status not in {"pending", "running", "paused", "waiting_for_human"}:
        db.rollback()
        return False, f"session_not_runnable:{session.status}", None, session_task_link

    claim_started_at = datetime.now(timezone.utc)
    updated_task_rows = (
        db.query(Task)
        .filter(Task.id == task.id, Task.status == TaskStatus.PENDING)
        .update(
            {
                Task.status: TaskStatus.RUNNING,
                Task.started_at: claim_started_at,
                Task.completed_at: None,
                Task.error_message: None,
            },
            synchronize_session=False,
        )
    )
    if updated_task_rows != 1:
        db.rollback()
        return False, f"task_not_claimable:{task.status.value}", None, session_task_link

    latest_link = session_task_link or _get_latest_session_task_link(
        db, session.id, task.id
    )
    if latest_link is None:
        db.rollback()
        return False, "missing_session_task_link", None, None

    updated_link_rows = (
        db.query(SessionTask)
        .filter(
            SessionTask.id == latest_link.id, SessionTask.status == TaskStatus.PENDING
        )
        .update(
            {
                SessionTask.status: TaskStatus.RUNNING,
                SessionTask.started_at: claim_started_at,
                SessionTask.completed_at: None,
            },
            synchronize_session=False,
        )
    )
    if updated_link_rows != 1:
        db.rollback()
        return (
            False,
            f"session_task_not_claimable:{latest_link.status.value}",
            None,
            latest_link,
        )

    db.commit()
    db.refresh(session)
    db.refresh(task)
    latest_link = _get_latest_session_task_link(db, session.id, task.id)
    return True, "claimed", claim_started_at, latest_link


@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    time_limit=ORCHESTRATION_TASK_TIME_LIMIT_SECONDS,
    soft_time_limit=ORCHESTRATION_TASK_SOFT_TIME_LIMIT_SECONDS,
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

    try:
        # Get session and task
        session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
        task = db.query(Task).filter(Task.id == task_id).first()

        if not session or not task:
            raise ValueError("Session or task not found")

        def emit_live(
            level: str, message: str, metadata: Optional[Dict[str, Any]] = None
        ) -> None:
            _record_live_log(
                db,
                session_id,
                task_id,
                level,
                message,
                session_instance_id=session.instance_id,
                metadata=metadata,
            )

        execution_profile = (
            getattr(task, "execution_profile", None)
            or getattr(session, "default_execution_profile", None)
            or "full_lifecycle"
        )
        if _should_force_review_execution_profile(
            execution_profile,
            prompt,
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
        queue_latency_seconds = None
        if dispatch_project_dir:
            queued_event = _find_latest_orchestration_event(
                dispatch_project_dir,
                session_id,
                task_id,
                event_types={EventType.TASK_QUEUED},
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
        )
        if stale_dispatch_reason:
            reject_details = {
                "reason": stale_dispatch_reason,
                "session_instance_id": session.instance_id,
                "expected_session_instance_id": expected_session_instance_id,
                "project_dir": (
                    str(dispatch_project_dir) if dispatch_project_dir else None
                ),
                "celery_task_id": getattr(getattr(self, "request", None), "id", None),
                "queue_latency_seconds": queue_latency_seconds,
                "queued_event_id": (queued_event or {}).get("event_id"),
                **_runtime_selection_details(db),
            }
            if dispatch_project_dir:
                _append_orchestration_event(
                    project_dir=dispatch_project_dir,
                    session_id=session_id,
                    task_id=task_id,
                    event_type=EventType.TASK_DISPATCH_REJECTED,
                    details=reject_details,
                )
            emit_live(
                "WARN",
                f"[ORCHESTRATION] Rejected stale queued dispatch before claim: {stale_dispatch_reason}",
                metadata=reject_details,
            )
            return {"status": "ignored", "reason": stale_dispatch_reason}

        session_task_link = _get_latest_session_task_link(db, session_id, task_id)
        claim_ok = False
        claim_reason = "unclaimed"
        claim_started_at = None
        for claim_attempt in range(5):
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
            time.sleep(0.2)
        if not claim_ok:
            reject_details = {
                "reason": claim_reason,
                "session_instance_id": session.instance_id,
                "expected_session_instance_id": expected_session_instance_id,
                "project_dir": (
                    str(dispatch_project_dir) if dispatch_project_dir else None
                ),
                "celery_task_id": getattr(getattr(self, "request", None), "id", None),
                "queue_latency_seconds": queue_latency_seconds,
                "queued_event_id": (queued_event or {}).get("event_id"),
                **_runtime_selection_details(db),
            }
            if dispatch_project_dir:
                _append_orchestration_event(
                    project_dir=dispatch_project_dir,
                    session_id=session_id,
                    task_id=task_id,
                    event_type=EventType.TASK_DISPATCH_REJECTED,
                    details=reject_details,
                )
            emit_live(
                "WARN",
                f"[ORCHESTRATION] Rejected stale or duplicate task dispatch: {claim_reason}",
                metadata=reject_details,
            )
            return {"status": "ignored", "reason": claim_reason}

        claimed_details = {
            "session_instance_id": session.instance_id,
            "expected_session_instance_id": expected_session_instance_id,
            "celery_task_id": getattr(getattr(self, "request", None), "id", None),
            "project_dir": str(dispatch_project_dir) if dispatch_project_dir else None,
            "queue_latency_seconds": queue_latency_seconds,
            "queued_event_id": (queued_event or {}).get("event_id"),
            **_runtime_selection_details(db),
        }
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

            # Keep a stable task subfolder identifier for metadata/reporting, even
            # when execution itself happens in the canonical project root.
            if task.task_subfolder:
                # ✅ Subfolder already locked from a previous run — use it
                # unconditionally so all cycles land in the same directory.
                orchestration_state._task_subfolder_override = task.task_subfolder
                logger.info(
                    "[ORCHESTRATION] Reusing locked task subfolder '%s' for task %s",
                    task.task_subfolder,
                    task_id,
                )
            else:
                # First run: generate a stable slug and persist it immediately.
                task_title_slug = build_task_subfolder_name(task.title, task_id)

                # Resolve collisions once, then freeze.
                counter = 1
                subfolder_name = task_title_slug
                while True:
                    existing_tasks = (
                        db.query(Task)
                        .filter(
                            Task.project_id == session.project_id,
                            Task.task_subfolder == subfolder_name,
                            Task.id != task_id,  # exclude self
                        )
                        .all()
                    )
                    if not existing_tasks:
                        break
                    subfolder_name = f"{task_title_slug}-{counter}"
                    counter += 1

                task.task_subfolder = subfolder_name
                db.commit()
                orchestration_state._task_subfolder_override = subfolder_name
                logger.info(
                    "[ORCHESTRATION] Locked new task subfolder '%s' for task %s",
                    subfolder_name,
                    task_id,
                )
        else:
            # Fallback: use slugified project name
            pass

        is_resume_execution = bool(resume_checkpoint_name)
        task_service = TaskService(db)
        if runs_in_canonical_baseline and project and not is_resume_execution:
            canonical_baseline_dir = task_service.get_project_baseline_dir(project)
            canonical_baseline_dir.mkdir(parents=True, exist_ok=True)
            orchestration_state._project_dir_override = str(canonical_baseline_dir)
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
            canonical_baseline_dir = task_service.get_project_baseline_dir(project)
            canonical_baseline_dir.mkdir(parents=True, exist_ok=True)
            orchestration_state._project_dir_override = str(canonical_baseline_dir)
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
            logger.info(f"Created task workspace: {task_workspace}")

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

        def restore_workspace_snapshot_if_needed(
            reason: str,
        ) -> Optional[Dict[str, Any]]:
            if not project:
                return None
            should_restore = should_restore_workspace_on_failure(
                reason, policy_profile=active_policy.name
            )
            if not should_restore:
                logger.warning(
                    "[ORCHESTRATION] Preserved workspace for task %s after %s; automatic restore is limited to isolation-violation failures",
                    task_id,
                    reason,
                )
                emit_live(
                    "WARN",
                    (
                        "[ORCHESTRATION] Preserved the current workspace after "
                        f"{reason}; automatic restore is only applied for "
                        "workspace-isolation violations"
                    ),
                    metadata={
                        "phase": "workspace_restore",
                        "reason": reason,
                        "restore_skipped": True,
                        "policy": active_policy.name,
                    },
                )
                _append_orchestration_event(
                    project_dir=orchestration_state.project_dir,
                    session_id=session_id,
                    task_id=task_id,
                    event_type=EventType.WORKSPACE_RESTORE_SKIPPED,
                    details={
                        "reason": reason,
                        "policy": "preserve_on_non_isolation_failures",
                    },
                )
                return {
                    "restored": False,
                    "reason": "preserved_non_isolation_failure",
                    "target_path": str(orchestration_state.project_dir),
                }
            restore_result = _restore_workspace_after_abort(
                task_service,
                project,
                task_id,
                orchestration_state.project_dir,
                preserve_project_root_rules=runs_in_canonical_baseline,
            )
            if restore_result and restore_result.get("restored"):
                logger.warning(
                    "[ORCHESTRATION] Restored workspace snapshot for task %s after %s (%s files)",
                    task_id,
                    reason,
                    restore_result.get("files_restored", 0),
                )
                emit_live(
                    "WARN",
                    (
                        "[ORCHESTRATION] Restored workspace to the pre-run snapshot "
                        f"after {reason} ({restore_result.get('files_restored', 0)} files)"
                    ),
                    metadata={
                        "phase": "workspace_restore",
                        "reason": reason,
                        "snapshot_path": restore_result.get("snapshot_path"),
                        "target_path": restore_result.get("target_path"),
                    },
                )
            elif (
                restore_result
                and restore_result.get("reason")
                == "empty_snapshot_preserved_existing_workspace"
            ):
                logger.warning(
                    "[ORCHESTRATION] Skipped destructive restore for task %s after %s because snapshot was empty and workspace already had files",
                    task_id,
                    reason,
                )
                emit_live(
                    "WARN",
                    (
                        "[ORCHESTRATION] Skipped restoring an empty pre-run snapshot "
                        f"after {reason} to preserve the current workspace "
                        f"({restore_result.get('current_workspace_files', 0)} files)"
                    ),
                    metadata={
                        "phase": "workspace_restore",
                        "reason": reason,
                        "snapshot_path": restore_result.get("snapshot_path"),
                        "target_path": restore_result.get("target_path"),
                        "current_workspace_files": restore_result.get(
                            "current_workspace_files", 0
                        ),
                        "restore_skipped": True,
                    },
                )
                _append_orchestration_event(
                    project_dir=orchestration_state.project_dir,
                    session_id=session_id,
                    task_id=task_id,
                    event_type=EventType.WORKSPACE_RESTORE_SKIPPED,
                    details={
                        "reason": reason,
                        "policy": "empty_snapshot_preserved_existing_workspace",
                    },
                )
            elif restore_result and restore_result.get("restored"):
                _append_orchestration_event(
                    project_dir=orchestration_state.project_dir,
                    session_id=session_id,
                    task_id=task_id,
                    event_type=EventType.WORKSPACE_PRESERVED,
                    details={
                        "reason": reason,
                        "restored_files": restore_result.get("files_restored", 0),
                    },
                )
            return restore_result

        project_context_summary = task_service.build_project_execution_context(
            project=project,
            current_task=task,
        )
        if hydration_result.get("hydrated"):
            hydrated_sources = ", ".join(
                f"#{item.get('task_id')} {item.get('title')}"
                for item in hydration_result.get("source_tasks", [])[:6]
            )
            project_context_summary = (
                f"{project_context_summary}\n"
                f"Hydrated baseline sources available directly in this workspace: {hydrated_sources}"
            )[:5000]
        orchestration_state.project_context = project_context_summary

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
                    f"[ORCHESTRATION] Task {task_id} already running for {time_since_start}, marking as failed"
                )
                task.status = TaskStatus.FAILED
                task.error_message = f"Task already running for {time_since_start}, possible duplicate execution"
                db.commit()
                raise Exception("Task timeout - already running too long")
        elif task.started_at and is_resume_execution:
            logger.info(
                "[ORCHESTRATION] Skipping stale-run timeout guard for task %s because resume checkpoint '%s' was requested",
                task_id,
                resume_checkpoint_name,
            )

        # Update task status
        task.status = TaskStatus.RUNNING
        task.started_at = (
            claim_started_at or task.started_at or datetime.now(timezone.utc)
        )
        task.completed_at = None
        task.error_message = None
        task.current_step = 0
        task.workspace_status = "in_progress" if task.task_subfolder else "not_created"
        session_task_link = _get_latest_session_task_link(db, session_id, task_id)
        if session_task_link:
            session_task_link.status = TaskStatus.RUNNING
            session_task_link.started_at = claim_started_at or task.started_at
            session_task_link.completed_at = None
        db.commit()
        _write_project_state_snapshot(db, project, task, session_id)

        logger.info(f"[ORCHESTRATION] Starting multi-step execution for task {task_id}")
        emit_live(
            "INFO",
            f"[ORCHESTRATION] Starting multi-step execution for task {task_id}",
            metadata={"phase": "start"},
        )
        try:
            _append_orchestration_event(
                project_dir=orchestration_state.project_dir,
                session_id=session_id,
                task_id=task_id,
                event_type=EventType.TASK_STARTED,
                details={"execution_profile": execution_profile},
            )
        except Exception as exc:
            logger.debug(
                "[ORCHESTRATION] Failed to record task-start event for task %s: %s",
                task_id,
                exc,
            )

        # Initialize the active runtime service
        runtime_service = create_agent_runtime(db, session_id, task_id)

        # Get session context
        session_context = asyncio.run(runtime_service.get_session_context())

        if project and project.workspace_path:
            expected_root = Path(
                resolve_project_workspace_path(project.workspace_path, project.name)
            )
            workspace_contract = verify_workspace_contract(
                expected_root=expected_root,
                task_dir=Path(orchestration_state.project_dir),
                expected_task_subfolder=getattr(task, "task_subfolder", None),
                allow_project_root_task_dir=runs_in_canonical_baseline,
                runtime_session_context=session_context,
            )
            if not workspace_contract.get("ok"):
                contract_error = "Workspace contract failed before execution: " + str(
                    workspace_contract.get("reason") or "unknown mismatch"
                )
                task.status = TaskStatus.FAILED
                task.completed_at = datetime.now(timezone.utc)
                task.error_message = contract_error
                task.workspace_status = "blocked"
                if session_task_link:
                    session_task_link.status = TaskStatus.FAILED
                    session_task_link.completed_at = task.completed_at
                session.status = "paused"
                session.is_active = False
                _set_session_alert(session, "error", contract_error[:2000])
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

        def _apply_checkpoint_payload(checkpoint_payload: Dict[str, Any]) -> str:
            checkpoint_context = checkpoint_payload.get("context", {}) or {}
            checkpoint_state = checkpoint_payload.get("orchestration_state", {}) or {}

            nonlocal prompt
            prompt = checkpoint_context.get("task_description", prompt) or prompt
            orchestration_state.project_name = checkpoint_context.get(
                "project_name", orchestration_state.project_name
            )
            orchestration_state.project_context = checkpoint_context.get(
                "project_context", orchestration_state.project_context
            )
            if checkpoint_context.get("task_subfolder"):
                orchestration_state._task_subfolder_override = checkpoint_context.get(
                    "task_subfolder"
                )
            # Do NOT restore _project_dir_override from checkpoint — it is always
            # recomputed from current project settings (canonical baseline logic
            # runs after checkpoint load). Restoring a stale saved path causes
            # the wrong workspace (old host path, or path with task subfolder) to
            # be used in prompts and tool calls.

            orchestration_state.plan = checkpoint_state.get("plan", []) or []
            orchestration_state.current_step_index = (
                checkpoint_state.get(
                    "current_step_index",
                    checkpoint_payload.get("current_step_index", 0) or 0,
                )
                or 0
            )
            orchestration_state.debug_attempts = (
                checkpoint_state.get("debug_attempts", []) or []
            )
            orchestration_state.changed_files = (
                checkpoint_state.get("changed_files", []) or []
            )
            orchestration_state.validation_history = (
                checkpoint_state.get("validation_history", []) or []
            )
            orchestration_state.phase_history = (
                checkpoint_state.get("phase_history", []) or []
            )
            orchestration_state.last_plan_validation = checkpoint_state.get(
                "last_plan_validation"
            )
            orchestration_state.last_completion_validation = checkpoint_state.get(
                "last_completion_validation"
            )
            orchestration_state.relaxed_mode = bool(
                checkpoint_state.get("relaxed_mode", False)
            )
            orchestration_state.completion_repair_attempts = int(
                checkpoint_state.get("completion_repair_attempts", 0) or 0
            )
            orchestration_state.execution_results = [
                _restore_step_result(item)
                for item in checkpoint_state.get(
                    "execution_results", checkpoint_payload.get("step_results", [])
                )
            ]
            # Restore execution status: if a plan exists with pending steps, mark
            # as EXECUTING so downstream checkpoint saves reflect the real phase.
            # Never restore ABORTED/DONE — those are terminal states from a prior
            # run and we are actively resuming.
            raw_status = checkpoint_state.get("status", "")
            if raw_status in ("executing", "debugging", "revising_plan"):
                try:
                    orchestration_state.status = OrchestrationStatus(raw_status)
                except ValueError:
                    pass
            elif (
                orchestration_state.plan
                and orchestration_state.current_step_index
                < len(orchestration_state.plan)
            ):
                orchestration_state.status = OrchestrationStatus.EXECUTING
            _append_orchestration_event(
                project_dir=orchestration_state.project_dir,
                session_id=session_id,
                task_id=task_id,
                event_type=EventType.CHECKPOINT_LOADED,
                details={
                    "checkpoint_name": checkpoint_payload.get(
                        "_resolved_checkpoint_name"
                    )
                    or checkpoint_payload.get("checkpoint_name"),
                    "requested_checkpoint_name": checkpoint_payload.get(
                        "_requested_checkpoint_name"
                    ),
                },
            )
            if (
                orchestration_state.completion_repair_attempts > 0
                and orchestration_state.plan
                and len(orchestration_state.execution_results)
                == int(orchestration_state.current_step_index or 0)
                and int(orchestration_state.current_step_index or 0)
                == len(orchestration_state.plan) - 1
            ):
                stale_repair_step = orchestration_state.plan[-1]
                orchestration_state.plan = orchestration_state.plan[
                    : orchestration_state.current_step_index
                ]
                orchestration_state.completion_repair_attempts = 0
                task.steps = json.dumps(orchestration_state.plan)
                emit_live(
                    "WARN",
                    "[ORCHESTRATION] Dropped a stale pending completion-repair step from the resume checkpoint and reset the repair budget",
                    metadata={
                        "phase": "resume",
                        "stale_completion_repair_step": stale_repair_step.get(
                            "description", ""
                        ),
                    },
                )
            completed_step_count = max(
                len(orchestration_state.execution_results),
                int(orchestration_state.current_step_index or 0),
            )
            compatibility = (
                ValidatorService.assess_plan_workspace_compatibility(
                    project_dir=orchestration_state.project_dir,
                    plan=orchestration_state.plan,
                    completed_step_count=completed_step_count,
                )
                if orchestration_state.plan
                else {"compatible": True}
            )
            if (
                orchestration_state.plan
                and orchestration_state.current_step_index
                >= len(orchestration_state.plan)
            ):
                orchestration_state.completion_repair_attempts = 0
            return compatibility

        def _clear_resume_execution_state(error_message: str) -> None:
            orchestration_state.plan = []
            orchestration_state.current_step_index = 0
            orchestration_state.debug_attempts = []
            orchestration_state.execution_results = []
            orchestration_state.changed_files = []
            orchestration_state.completion_repair_attempts = 0
            orchestration_state.status = OrchestrationStatus.PLANNING
            orchestration_state.abort_reason = ""
            task.steps = None
            task.current_step = 0
            task.error_message = None
            if session_task_link:
                session_task_link.status = TaskStatus.RUNNING
                session_task_link.started_at = task.started_at
                session_task_link.completed_at = None
            session.status = "running"
            session.is_active = True
            _set_session_alert(session, "warn", error_message[:2000])
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
            resume_workspace_compatibility = _apply_checkpoint_payload(checkpoint_data)
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
                        fallback_compatibility = _apply_checkpoint_payload(
                            fallback_data
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
                )
                _record_validation_verdict(
                    db,
                    session_id,
                    task_id,
                    orchestration_state,
                    resume_plan_verdict,
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
                    orchestration_state.plan = []
                    orchestration_state.current_step_index = 0
                    orchestration_state.debug_attempts = []
                    orchestration_state.execution_results = []
                    orchestration_state.changed_files = []
                    orchestration_state.status = OrchestrationStatus.PLANNING
                    orchestration_state.abort_reason = ""
                    task.steps = None
                    task.current_step = 0
                    task.error_message = None
                    if session_task_link:
                        session_task_link.status = TaskStatus.RUNNING
                        session_task_link.started_at = task.started_at
                        session_task_link.completed_at = None
                    session.status = "running"
                    session.is_active = True
                    _set_session_alert(session, "warn", resume_error[:2000])
                    db.commit()

            resumed_from_checkpoint = bool(orchestration_state.plan)
            if resumed_from_checkpoint:
                logger.info(
                    f"[ORCHESTRATION] Resuming from checkpoint '{resolved_resume_checkpoint_name}' at step index {orchestration_state.current_step_index}"
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

        refreshed_project_context = task_service.build_project_execution_context(
            project=project,
            current_task=task,
        )
        if hydration_result.get("hydrated"):
            hydrated_sources = ", ".join(
                f"#{item.get('task_id')} {item.get('title')}"
                for item in hydration_result.get("source_tasks", [])[:6]
            )
            refreshed_project_context = (
                f"{refreshed_project_context}\n"
                f"Hydrated baseline sources available directly in this workspace: {hydrated_sources}"
            )[:5000]
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
            restore_workspace_snapshot_if_needed=restore_workspace_snapshot_if_needed,
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
            task.status = TaskStatus.FAILED
            task.error_message = gate_error
            if session_task_link:
                session_task_link.status = TaskStatus.FAILED
                session_task_link.completed_at = datetime.now(timezone.utc)
            session.status = "paused"
            session.is_active = False
            _set_session_alert(session, "error", gate_error[:2000])
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
                    )
                    _record_validation_verdict(
                        db,
                        session_id,
                        task_id,
                        orchestration_state,
                        stored_plan_verdict,
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
            planning_phase_result = execute_planning_phase(
                ctx=run_ctx,
                workspace_review=workspace_review,
                extract_structured_text=_extract_structured_text,
                extract_plan_steps=_extract_plan_steps,
                looks_like_truncated_multistep_plan=_looks_like_truncated_multistep_plan,
                normalize_plan_with_live_logging=_normalize_plan_with_live_logging,
                workspace_violation_error_cls=TaskWorkspaceViolationError,
            )
            if planning_phase_result.get("status") != "completed":
                return planning_phase_result

        _save_orchestration_checkpoint(
            db, session_id, task_id, prompt, orchestration_state
        )

        return execute_step_loop(
            ctx=run_ctx,
            extract_structured_text=_extract_structured_text,
            normalize_step=_normalize_step,
            normalize_plan_with_live_logging=_normalize_plan_with_live_logging,
            workspace_violation_error_cls=TaskWorkspaceViolationError,
            write_project_state_snapshot_fn=_write_project_state_snapshot,
            get_next_pending_project_task_fn=_get_next_pending_project_task,
            get_latest_session_task_link_fn=_get_latest_session_task_link,
            execute_orchestration_task_delay_fn=execute_orchestration_task.delay,
            build_task_report_payload_fn=_build_task_report_payload,
            render_task_report_fn=_render_task_report,
            record_live_log_fn=_record_live_log,
        )

    except Exception as exc:
        handle_task_failure(
            self_task=self,
            ctx=(
                run_ctx
                if "run_ctx" in locals()
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
                        execution_profile=(
                            execution_profile
                            if "execution_profile" in locals()
                            else "full_lifecycle"
                        ),
                        validation_profile=(
                            validation_profile
                            if "validation_profile" in locals()
                            else "implementation"
                        ),
                        runs_in_canonical_baseline=(
                            runs_in_canonical_baseline
                            if "runs_in_canonical_baseline" in locals()
                            else False
                        ),
                        orchestration_state=orchestration_state,
                        runtime_service=None,
                        task_service=None,
                        logger=logger,
                        emit_live=lambda *_args, **_kwargs: None,
                        error_handler=error_handler,
                        policy_profile_name=(
                            active_policy.name
                            if "active_policy" in locals()
                            else "balanced"
                        ),
                        validation_severity=(
                            active_policy.validation_severity
                            if "active_policy" in locals()
                            else "standard"
                        ),
                        completion_repair_budget=(
                            active_policy.completion_repair_budget
                            if "active_policy" in locals()
                            else 1
                        ),
                        restore_workspace_snapshot_if_needed=(
                            restore_workspace_snapshot_if_needed
                            if "restore_workspace_snapshot_if_needed" in locals()
                            else None
                        ),
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
        db.close()


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def process_github_webhook(
    self, webhook_data: Dict[str, Any], repo_owner: str, repo_name: str
):
    """
    Process GitHub webhook in the background

    Args:
        webhook_data: GitHub webhook payload
        repo_owner: Repository owner
        repo_name: Repository name
    """
    try:
        # Get or create project
        db = get_db_session()

        project = (
            db.query(Project)
            .filter(Project.github_url.ilike(f"%{repo_owner}/{repo_name}%"))
            .first()
        )

        if not project:
            # Create new project from webhook
            project = Project(
                name=f"{repo_owner}/{repo_name}",
                github_url=f"https://github.com/{repo_owner}/{repo_name}",
                description="Auto-created from GitHub webhook",
            )
            db.add(project)
            db.commit()
            db.refresh(project)

        # Process webhook based on type
        webhook_type = webhook_data.get("type", "Unknown")

        if webhook_type == "PushEvent":
            # Handle push events
            logger.info(f"Processing push event for {repo_owner}/{repo_name}")
            # TODO: Create task for code analysis

        elif webhook_type == "PullRequestEvent":
            # Handle PR events
            logger.info(f"Processing PR event for {repo_owner}/{repo_name}")
            # TODO: Create task for PR review

        elif webhook_type == "IssueEvent":
            # Handle issue events
            logger.info(f"Processing issue event for {repo_owner}/{repo_name}")
            # TODO: Create task from issue

        db.close()

        return {
            "status": "processed",
            "webhook_type": webhook_type,
            "project_id": project.id if project else None,
        }

    except Exception as exc:
        logger.error(f"Webhook processing failed: {str(exc)}")
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=5, default_retry_delay=30)
def scheduled_task_execution(self, task_id: int, scheduled_time: str, prompt: str):
    """
    Execute a task at a scheduled time

    Args:
        task_id: Task ID
        scheduled_time: ISO format scheduled time
        prompt: Task prompt
    """
    from datetime import datetime as dt

    try:
        # Check if it's time to execute
        now = dt.utcnow()
        schedule_dt = dt.fromisoformat(scheduled_time.replace("Z", "+00:00"))

        if now < schedule_dt:
            # Not time yet, reschedule
            delay_seconds = (schedule_dt - now).total_seconds()
            logger.info(
                f"Task {task_id} scheduled for later, retrying in {delay_seconds}s"
            )
            raise self.retry(countdown=delay_seconds)

        # Execute the task
        db = get_db_session()

        task = db.query(Task).filter(Task.id == task_id).first()
        if task:
            task.status = TaskStatus.RUNNING
            task.started_at = dt.utcnow()
            db.commit()

        # TODO: Implement actual scheduled execution
        # This will integrate with the active runtime session

        if task:
            task.status = TaskStatus.DONE
            task.completed_at = dt.utcnow()
            db.commit()

        db.close()

        return {
            "status": "completed",
            "task_id": task_id,
            "executed_at": dt.utcnow().isoformat(),
        }

    except Exception as exc:
        logger.error(f"Scheduled task {task_id} failed: {str(exc)}")
        raise self.retry(exc=exc, max_retries=3)


@celery_app.task(bind=True)
def cleanup_old_logs(self, days: int = 30, session_id: Optional[int] = None):
    """
    Clean up old log entries

    Args:
        days: Delete logs older than this many days
        session_id: Optional session filter
    """
    try:
        db = get_db_session()

        from datetime import datetime, timedelta

        cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)

        query = db.query(LogEntry).filter(LogEntry.created_at < cutoff_date)
        if session_id:
            query = query.filter(LogEntry.session_id == session_id)

        deleted_count = query.delete(synchronize_session=False)
        db.commit()

        logger.info(f"Deleted {deleted_count} old log entries")

        db.close()

        return {
            "status": "completed",
            "deleted_count": deleted_count,
            "days": days,
            "session_id": session_id,
        }

    except Exception as exc:
        logger.error(f"Log cleanup failed: {str(exc)}")
        raise self.retry(exc=exc, max_retries=3)


# Backward-compatible alias for older imports and serialized task references.
execute_openclaw_task = execute_orchestration_task


@celery_app.task(bind=True)
def generate_task_report(self, task_id: int, output_format: str = "json"):
    """
    Generate a report for a completed task

    Args:
        task_id: Task ID
        output_format: Output format (json, markdown, html)
    """
    try:
        db = get_db_session()
        report = _build_task_report_payload(db, task_id)
        return _render_task_report(report, output_format=output_format)

    except Exception as exc:
        logger.error(f"Report generation failed: {str(exc)}")
        raise self.retry(exc=exc, max_retries=3)
    finally:
        db.close()


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
    import json as _json

    db = get_db_session()
    try:
        from app.services.session.intervention_service import _get_session_or_404

        session = _get_session_or_404(db, session_id)
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
            f"[OPERATOR-QUERY] Processing operator question…",
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

        # Store AI answer in context_snapshot as JSON
        existing = {}
        try:
            if req.context_snapshot:
                existing = _json.loads(req.context_snapshot)
        except Exception:
            pass
        existing["ai_response"] = ai_answer
        req.context_snapshot = _json.dumps(existing)
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
