"""Session lifecycle control helpers."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timezone
from pathlib import Path
from typing import Any, Dict

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import (
    LogEntry,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.agents.agent_runtime import create_agent_runtime
from app.services.workspace.checkpoint_service import CheckpointError, CheckpointService
from app.services.workspace.project_isolation_service import (
    resolve_project_workspace_path,
)
from app.services.session.session_runtime_service import (
    DEFAULT_ORCHESTRATION_TIMEOUT_SECONDS,
    queue_task_for_session,
    reopen_failed_ordered_task_if_needed,
    revoke_session_celery_tasks,
    set_session_alert,
)
from app.services.orchestration.task_rules import (
    should_execute_in_canonical_project_root,
)
from app.services.orchestration.run_state import (
    mark_task_attempt_pending,
    reset_active_attempts_for_session_stop,
)
from app.services.orchestration.session_state import (
    clear_session_alert,
    mark_session_paused,
    mark_session_running,
    mark_session_stopped,
)
from app.services.task_service import TaskService
from app.services.task_execution_service import create_task_execution

logger = logging.getLogger(__name__)

_ORPHANED_PLANNING_RECOVERY_SECONDS = 120
_STALE_RUNNING_SESSION_SWEEP_SECONDS = DEFAULT_ORCHESTRATION_TIMEOUT_SECONDS + 300


def _coerce_naive_utc_datetime(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value
    return value.astimezone(UTC).replace(tzinfo=None)


def _reset_running_session_tasks(
    db: Session,
    *,
    session_id: int,
    next_status: TaskStatus = TaskStatus.PENDING,
) -> int:
    """Compatibility wrapper for the run-state transition module."""

    return reset_active_attempts_for_session_stop(
        db,
        session_id=session_id,
        next_status=next_status,
    )


def _recover_orphaned_running_session_if_needed(
    db: Session,
    *,
    session: SessionModel,
) -> bool:
    """Recover a session stranded in RUNNING after planning/repair crashed.

    We keep this heuristic narrow on purpose: only recover when the latest task
    is still at step 0, the latest log shows the run reached planning-response
    handling, and then no additional logs arrived for a short grace period.
    """

    if session.status != "running" or not session.is_active:
        return False

    latest_link = (
        db.query(SessionTask)
        .filter(
            SessionTask.session_id == session.id,
            SessionTask.status == TaskStatus.RUNNING,
        )
        .order_by(SessionTask.id.desc())
        .first()
    )
    if not latest_link:
        return False

    task = db.query(Task).filter(Task.id == latest_link.task_id).first()
    if not task or task.status != TaskStatus.RUNNING:
        return False
    if int(task.current_step or 0) != 0:
        return False

    latest_log = (
        db.query(LogEntry)
        .filter(
            LogEntry.session_id == session.id,
            LogEntry.task_id == task.id,
        )
        .order_by(LogEntry.id.desc())
        .first()
    )
    if not latest_log or not latest_log.created_at:
        return False

    terminal_planning_messages = {
        "[ORCHESTRATION] Planning response received; parsing and validating plan",
        "[OPENCLAW] Request returned output; awaiting orchestration validation",
        "[OPENCLAW] stdout was empty; recovered structured response from stderr",
    }
    if str(latest_log.message or "") not in terminal_planning_messages:
        return False

    age_seconds = (
        datetime.now(UTC).replace(tzinfo=None)
        - _coerce_naive_utc_datetime(latest_log.created_at)
    ).total_seconds()
    if age_seconds < _ORPHANED_PLANNING_RECOVERY_SECONDS:
        return False

    _stop_running_session_for_recovery(
        db,
        session=session,
        task=task,
        session_task=latest_link,
        stop_reason="orphaned_running_session",
        task_error_message=(
            "Recovered an orphaned planning run after no progress logs arrived "
            "following planning-response validation."
        ),
        alert_message=(
            "Recovered an orphaned planning run so the session can be started again."
        ),
        recovery_log_message=(
            "Recovered orphaned running task after planning-response handling "
            "stalled without further progress"
        ),
    )
    return True


def _record_failure_knowledge_for_recovery(
    db: Session,
    *,
    session_id: int,
    task_id: int,
    stop_reason: str,
) -> bool:
    try:
        from app.services.orchestration.phases.failure_flow import (
            record_failure_knowledge_for_stopped_session,
        )

        return bool(
            record_failure_knowledge_for_stopped_session(
                db=db,
                session_id=session_id,
                task_id=task_id,
                failure_reason=stop_reason,
                logger=logger,
            )
        )
    except Exception as knowledge_exc:
        logger.warning(
            "[STOP] session=%s task_id=%s stop_reason=%s knowledge_recorded=False error=%s",
            session_id,
            task_id,
            stop_reason,
            knowledge_exc,
        )
        return False


def _stop_running_session_for_recovery(
    db: Session,
    *,
    session: SessionModel,
    task: Task,
    session_task: SessionTask,
    stop_reason: str,
    task_error_message: str,
    alert_message: str,
    recovery_log_message: str,
) -> bool:
    mark_task_attempt_pending(
        task=task,
        session_task_link=session_task,
        reset_started_at=True,
        error_message=task_error_message,
    )
    mark_session_stopped(session, stopped_at=datetime.now(timezone.utc))
    session.last_alert_level = "warn"
    session.last_alert_message = alert_message
    session.last_alert_at = datetime.now(timezone.utc)
    db.add(
        LogEntry(
            session_id=session.id,
            task_id=task.id,
            session_instance_id=session.instance_id,
            level="WARN",
            message=recovery_log_message,
        )
    )
    db.commit()
    knowledge_recorded = _record_failure_knowledge_for_recovery(
        db,
        session_id=session.id,
        task_id=task.id,
        stop_reason=stop_reason,
    )
    logger.warning(
        "[STOP] session=%s task_id=%s stop_reason=%s knowledge_recorded=%s",
        session.id,
        task.id,
        stop_reason,
        knowledge_recorded,
    )
    return knowledge_recorded


def recover_stale_running_sessions(
    db: Session,
    *,
    stale_after_seconds: int = _STALE_RUNNING_SESSION_SWEEP_SECONDS,
) -> list[dict[str, Any]]:
    now = datetime.now(UTC).replace(tzinfo=None)
    running_sessions = (
        db.query(SessionModel)
        .filter(
            SessionModel.status == "running",
            SessionModel.is_active.is_(True),
            SessionModel.deleted_at.is_(None),
        )
        .all()
    )
    recovered_sessions: list[dict[str, Any]] = []

    for session in running_sessions:
        latest_link = (
            db.query(SessionTask)
            .filter(
                SessionTask.session_id == session.id,
                SessionTask.status == TaskStatus.RUNNING,
            )
            .order_by(SessionTask.id.desc())
            .first()
        )
        if not latest_link:
            continue

        task = db.query(Task).filter(Task.id == latest_link.task_id).first()
        if not task or task.status != TaskStatus.RUNNING:
            continue

        latest_log = (
            db.query(LogEntry)
            .filter(
                LogEntry.session_id == session.id,
                LogEntry.task_id == task.id,
            )
            .order_by(LogEntry.id.desc())
            .first()
        )

        last_progress_at = next(
            (
                candidate
                for candidate in (
                    latest_log.created_at if latest_log else None,
                    latest_link.started_at,
                    task.started_at,
                    session.updated_at,
                    session.started_at,
                    session.created_at,
                )
                if candidate is not None
            ),
            None,
        )
        if last_progress_at is None:
            continue

        age_seconds = (
            now - _coerce_naive_utc_datetime(last_progress_at)
        ).total_seconds()
        if age_seconds < stale_after_seconds:
            continue

        stop_reason = (
            "no_progress_timeout"
            if latest_log and latest_log.created_at
            else "hard_time_limit_or_worker_killed"
        )
        knowledge_recorded = _stop_running_session_for_recovery(
            db,
            session=session,
            task=task,
            session_task=latest_link,
            stop_reason=stop_reason,
            task_error_message=(
                "Recovered stale running session after runtime stopped making progress."
            ),
            alert_message=(
                "Recovered stale running session after runtime stopped making progress."
            ),
            recovery_log_message=(
                "Recovered stale running session after no progress timeout."
            ),
        )
        recovered_sessions.append(
            {
                "session_id": session.id,
                "task_id": task.id,
                "stop_reason": stop_reason,
                "knowledge_recorded": knowledge_recorded,
            }
        )

    return recovered_sessions


def reconcile_terminal_running_sessions(
    db: Session,
    sessions: list[SessionModel] | None = None,
) -> list[dict[str, Any]]:
    """Pause/stop sessions that still say running after terminal task execution.

    This catches persisted state drift where the task execution and session-task
    link reached a terminal state, but the session row was not updated. The UI
    should not show such sessions as actively running.
    """

    query = db.query(SessionModel).filter(
        SessionModel.status == "running",
        SessionModel.deleted_at.is_(None),
    )
    if sessions is not None:
        session_ids = [
            session.id for session in sessions if session.status == "running"
        ]
        if not session_ids:
            return []
        query = query.filter(SessionModel.id.in_(session_ids))

    reconciled: list[dict[str, Any]] = []
    for session in query.all():
        running_execution_exists = (
            db.query(TaskExecution.id)
            .filter(
                TaskExecution.session_id == session.id,
                TaskExecution.status == TaskStatus.RUNNING,
            )
            .first()
            is not None
        )
        if running_execution_exists:
            continue

        latest_execution = (
            db.query(TaskExecution)
            .filter(TaskExecution.session_id == session.id)
            .order_by(
                TaskExecution.completed_at.desc().nullslast(),
                TaskExecution.started_at.desc().nullslast(),
                TaskExecution.created_at.desc().nullslast(),
                TaskExecution.id.desc(),
            )
            .first()
        )
        if latest_execution is None:
            continue

        if latest_execution.status == TaskStatus.FAILED:
            next_status = "paused"
            alert_level = "error"
            alert_message = (
                "Recovered session status after its latest task execution failed."
            )
        elif latest_execution.status in (TaskStatus.CANCELLED, TaskStatus.DONE):
            next_status = "stopped"
            alert_level = None
            alert_message = None
        else:
            continue

        if next_status == "paused":
            mark_session_paused(
                session,
                alert_level=alert_level,
                alert_message=alert_message,
                paused_at=session.paused_at or datetime.now(timezone.utc),
            )
        else:
            mark_session_stopped(
                session,
                stopped_at=session.stopped_at or datetime.now(timezone.utc),
            )
            session.last_alert_level = alert_level
            session.last_alert_message = alert_message
            session.last_alert_at = (
                datetime.now(timezone.utc) if alert_message else None
            )
        db.add(
            LogEntry(
                session_id=session.id,
                task_id=latest_execution.task_id,
                task_execution_id=latest_execution.id,
                session_instance_id=session.instance_id,
                level="WARN",
                message=(
                    "Reconciled stale running session after terminal task execution"
                ),
            )
        )
        reconciled.append(
            {
                "session_id": session.id,
                "task_execution_id": latest_execution.id,
                "previous_status": "running",
                "next_status": next_status,
                "terminal_task_status": latest_execution.status.value,
            }
        )

    if reconciled:
        db.commit()
    return reconciled


def _ensure_session_task_ready_for_resume(
    db: Session,
    *,
    session: SessionModel,
    task: Task,
    resumed_at: datetime,
) -> SessionTask:
    """Restore the task/session link so resumed runs look active to the UI."""

    session_task_link = (
        db.query(SessionTask)
        .filter(SessionTask.session_id == session.id, SessionTask.task_id == task.id)
        .order_by(SessionTask.id.desc())
        .first()
    )
    if not session_task_link:
        session_task_link = SessionTask(
            session_id=session.id,
            task_id=task.id,
            status=TaskStatus.PENDING,
            started_at=None,
        )
        db.add(session_task_link)
    else:
        mark_task_attempt_pending(
            task=None,
            session_task_link=session_task_link,
            reset_started_at=True,
        )

    mark_task_attempt_pending(
        task=task,
        reset_started_at=True,
        error_message=None,
    )
    return session_task_link


def _checkpoint_has_resume_state(payload: Dict[str, Any] | None) -> bool:
    if not payload:
        return False
    context = payload.get("context", {}) or {}
    orchestration_state = payload.get("orchestration_state", {}) or {}
    step_results = payload.get("step_results", []) or []
    return bool(
        context.get("task_id")
        or context.get("task_subfolder")
        or context.get("project_dir_override")
        or context.get("task_description")
        or orchestration_state.get("plan")
        or orchestration_state.get("status")
        or step_results
    )


def _checkpoint_has_execution_progress(payload: Dict[str, Any] | None) -> bool:
    """Return True only when the checkpoint can replay meaningful execution state."""

    if not payload:
        return False

    orchestration_state = payload.get("orchestration_state", {}) or {}
    step_results = payload.get("step_results", []) or []
    execution_results = orchestration_state.get("execution_results", []) or []
    current_step_index = (
        orchestration_state.get("current_step_index")
        or payload.get("current_step_index")
        or 0
    )

    return bool(
        orchestration_state.get("plan")
        or step_results
        or execution_results
        or int(current_step_index or 0) > 0
    )


def _select_checkpoint_payload_for_pause(
    checkpoint_service: CheckpointService, session_id: int
) -> Dict[str, Any] | None:
    for candidate_name in ("autosave_latest", "autosave_error", None):
        try:
            payload = checkpoint_service.load_checkpoint(
                session_id, checkpoint_name=candidate_name
            )
        except Exception:
            continue
        if _checkpoint_has_execution_progress(payload):
            return payload
    return None


def _decode_task_steps(task: Task) -> list[dict[str, Any]]:
    raw_steps = task.steps
    if not raw_steps:
        return []
    if isinstance(raw_steps, list):
        return raw_steps
    if not isinstance(raw_steps, str):
        return []
    try:
        decoded = json.loads(raw_steps)
    except Exception:
        return []
    return decoded if isinstance(decoded, list) else []


def _build_checkpoint_payload_from_session_state(
    db: Session,
    *,
    session: SessionModel,
) -> Dict[str, Any] | None:
    latest_session_task = (
        db.query(SessionTask)
        .filter(SessionTask.session_id == session.id)
        .order_by(SessionTask.started_at.desc().nullslast(), SessionTask.id.desc())
        .first()
    )
    if not latest_session_task:
        return None

    task = db.query(Task).filter(Task.id == latest_session_task.task_id).first()
    if not task:
        return None

    project = session.project or task.project
    workspace_path_override: str | None = None
    project_dir_override: str | None = None
    if project and project.workspace_path:
        try:
            workspace_root = resolve_project_workspace_path(
                project.workspace_path, project.name, db=db
            )
            workspace_path_override = str(workspace_root)
            if should_execute_in_canonical_project_root(
                task,
                getattr(task, "execution_profile", None),
                task.title,
                task.description,
            ):
                project_dir_override = str(workspace_root)
            else:
                project_dir_override = str(
                    workspace_root / task.task_subfolder
                    if task.task_subfolder
                    else workspace_root
                )
        except Exception:
            workspace_path_override = project.workspace_path
            if should_execute_in_canonical_project_root(
                task,
                getattr(task, "execution_profile", None),
                task.title,
                task.description,
            ):
                project_dir_override = str(project.workspace_path)
            elif task.task_subfolder and project.workspace_path:
                project_dir_override = str(
                    Path(project.workspace_path) / task.task_subfolder
                )

    plan = _decode_task_steps(task)
    current_step_index = int(task.current_step or 0)
    orchestration_status = (
        task.status.value if hasattr(task.status, "value") else str(task.status)
    )
    payload = {
        "context": {
            "task_id": task.id,
            "task_description": task.description or task.title,
            "project_name": project.name if project else None,
            "project_context": project.description if project else None,
            "project_rules": project.project_rules if project else None,
            "task_subfolder": task.task_subfolder,
            "workspace_path_override": workspace_path_override,
            "project_dir_override": project_dir_override,
        },
        "orchestration_state": {
            "status": orchestration_status,
            "plan": plan,
            "current_step_index": current_step_index,
            "execution_results": [],
            "debug_attempts": [],
            "changed_files": [],
            "validation_history": [],
            "phase_history": [],
            "last_plan_validation": None,
            "last_completion_validation": None,
            "relaxed_mode": False,
            "completion_repair_attempts": 0,
            "debug_repair_task_execution_ids": [],
        },
        "current_step_index": current_step_index,
        "step_results": [],
    }
    if not _checkpoint_has_resume_state(payload):
        return None
    return payload


def _latest_session_task_link(db: Session, session_id: int) -> SessionTask | None:
    return (
        db.query(SessionTask)
        .filter(SessionTask.session_id == session_id)
        .order_by(SessionTask.started_at.desc().nullslast(), SessionTask.id.desc())
        .first()
    )


def _maybe_resume_manual_session_work(
    db: Session,
    *,
    session: SessionModel,
    session_instance_id: str,
    allow_checkpoint_resume: bool = True,
) -> list[dict[str, Any]]:
    """For manual-mode restart, continue the last selected task when possible."""

    from app.tasks.worker import execute_orchestration_task

    latest_link = _latest_session_task_link(db, session.id)
    if not latest_link:
        return []

    task = db.query(Task).filter(Task.id == latest_link.task_id).first()
    if not task:
        return []

    checkpoint_service = CheckpointService(db)
    try:
        checkpoint_data = _load_replayable_resume_checkpoint(
            checkpoint_service, session.id
        )
    except CheckpointError:
        checkpoint_data = None
    resolved_checkpoint_name = None
    resume_has_progress = _checkpoint_has_execution_progress(checkpoint_data)
    if checkpoint_data is not None:
        resolved_checkpoint_name = checkpoint_data.get(
            "_resolved_checkpoint_name"
        ) or checkpoint_data.get("checkpoint_name")
        checkpoint_task_id = (checkpoint_data.get("context", {}) or {}).get("task_id")
        if checkpoint_task_id:
            checkpoint_task = (
                db.query(Task).filter(Task.id == checkpoint_task_id).first()
            )
            if checkpoint_task:
                task = checkpoint_task

    _ensure_session_task_ready_for_resume(
        db,
        session=session,
        task=task,
        resumed_at=datetime.now(timezone.utc),
    )

    if allow_checkpoint_resume and resume_has_progress and resolved_checkpoint_name:
        prompt = task.description or task.title
        task_execution = create_task_execution(
            db,
            session_id=session.id,
            task_id=task.id,
        )
        result = execute_orchestration_task.delay(
            session_id=session.id,
            task_id=task.id,
            prompt=prompt,
            timeout_seconds=DEFAULT_ORCHESTRATION_TIMEOUT_SECONDS,
            resume_checkpoint_name=resolved_checkpoint_name,
            expected_session_instance_id=session_instance_id,
            task_execution_id=task_execution.id,
        )
        db.add(
            LogEntry(
                session_id=session.id,
                session_instance_id=session_instance_id,
                task_id=task.id,
                task_execution_id=task_execution.id,
                level="INFO",
                message=(
                    f"Manual session restart resumed task {task.id} from checkpoint "
                    f"{resolved_checkpoint_name}"
                ),
                log_metadata=json.dumps(
                    {
                        "celery_task_id": result.id,
                        "task_execution_id": task_execution.id,
                        "dispatch_mode": "checkpoint_resume",
                        "checkpoint_name": resolved_checkpoint_name,
                    }
                ),
            )
        )
        db.commit()
        return [
            {
                "task_id": task.id,
                "task_name": task.title,
                "task_execution_id": task_execution.id,
                "celery_id": result.id,
                "dispatch_mode": "checkpoint_resume",
                "checkpoint_name": resolved_checkpoint_name,
            }
        ]

    queued = queue_task_for_session(
        db=db,
        session=session,
        task_id=task.id,
        timeout_seconds=DEFAULT_ORCHESTRATION_TIMEOUT_SECONDS,
    )
    queued["dispatch_mode"] = "fresh_requeue"
    return [queued]


def _load_replayable_resume_checkpoint(
    checkpoint_service: CheckpointService,
    session_id: int,
    *,
    checkpoint_name: str | None = None,
) -> Dict[str, Any] | None:
    """Return a checkpoint only when it has real execution progress to replay.

    Explicit caller choices are still honored and may return a low-fidelity
    checkpoint so the worker can decide how to recover. Automatic resumes are
    stricter: they only reuse checkpoints that contain a saved plan, step
    results, or a non-zero execution cursor.
    """

    if checkpoint_name:
        return checkpoint_service.load_resume_checkpoint(
            session_id, checkpoint_name=checkpoint_name
        )

    for candidate_name in ("autosave_latest", "autosave_error", None):
        try:
            payload = checkpoint_service.load_resume_checkpoint(
                session_id, checkpoint_name=candidate_name
            )
        except CheckpointError:
            continue
        if _checkpoint_has_execution_progress(payload):
            return payload
    return None


async def start_session_lifecycle(db: Session, session_id: int) -> Dict[str, Any]:
    """Start a session and queue work if needed."""
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    recovered_orphaned_run = _recover_orphaned_running_session_if_needed(
        db, session=session
    )
    if recovered_orphaned_run:
        db.refresh(session)

    if session.status in ["running", "paused", "active"]:
        raise HTTPException(
            status_code=400,
            detail=f"Session is already {session.status}. Use stop or resume instead.",
        )

    if session.status == "pending" and session.is_active:
        logger.warning(
            "Session %s is stuck in pending state with is_active=True. Resetting...",
            session_id,
        )
        mark_session_stopped(session)
        db.commit()

    if session.status == "active":
        logger.warning(
            "Session %s has 'active' status. Treating as stopped and resetting...",
            session_id,
        )
        mark_session_stopped(session)
        db.commit()

    try:
        session_instance_id = str(uuid.uuid4())
        session.instance_id = session_instance_id
        db.commit()

        runtime = create_agent_runtime(db, session_id, use_demo_mode=False)
        task_description = session.description or session.name
        logger.info(
            "Starting session %s with description: %s, instance: %s",
            session_id,
            task_description[:50],
            session_instance_id,
        )
        session_key = await runtime.create_session(task_description)

        clear_session_alert(session)

        if session.project_id:
            task_service = TaskService(db)
            project_tasks = task_service.get_project_tasks(session.project_id)

            stale_running_links = (
                db.query(SessionTask)
                .filter(
                    SessionTask.session_id == session_id,
                    SessionTask.status == TaskStatus.RUNNING,
                )
                .all()
            )
            stale_running_tasks = []
            for link in stale_running_links:
                task = next(
                    (
                        candidate
                        for candidate in project_tasks
                        if candidate.id == link.task_id
                    ),
                    None,
                )
                if not task:
                    continue

                other_active_link = (
                    db.query(SessionTask)
                    .join(SessionModel, SessionTask.session_id == SessionModel.id)
                    .filter(
                        SessionTask.task_id == task.id,
                        SessionTask.session_id != session_id,
                        SessionTask.status == TaskStatus.RUNNING,
                        SessionModel.deleted_at.is_(None),
                        SessionModel.status.in_(["pending", "running", "active"]),
                    )
                    .first()
                )
                if other_active_link:
                    continue

                mark_task_attempt_pending(
                    task=task,
                    session_task_link=link,
                    reset_started_at=True,
                    error_message=None,
                )
                stale_running_tasks.append(task)

            if stale_running_tasks:
                db.add(
                    LogEntry(
                        session_id=session_id,
                        session_instance_id=session_instance_id,
                        level="INFO",
                        message=f"Recovered {len(stale_running_tasks)} stale running task(s) for restart",
                    )
                )
                db.commit()

            pending_tasks = task_service.get_project_tasks(session.project_id)
            reopen_failed_ordered_task_if_needed(
                db,
                session,
                ignore_recovery_budget=(session.execution_mode == "automatic"),
            )
            pending_tasks = task_service.get_project_tasks(session.project_id)

            queued_tasks = []
            if session.execution_mode == "automatic":
                next_task = task_service.get_next_pending_task(session.project_id)
                if next_task:
                    queued_tasks.append(
                        queue_task_for_session(
                            db=db,
                            session=session,
                            task_id=next_task.id,
                            timeout_seconds=DEFAULT_ORCHESTRATION_TIMEOUT_SECONDS,
                        )
                    )
                else:
                    mark_session_stopped(session)
                    db.add(
                        LogEntry(
                            session_id=session_id,
                            session_instance_id=session_instance_id,
                            level="WARN",
                            message="No pending tasks were available for automatic execution",
                        )
                    )
                    db.commit()
            else:
                queued_tasks = _maybe_resume_manual_session_work(
                    db,
                    session=session,
                    session_instance_id=session_instance_id,
                    allow_checkpoint_resume=not recovered_orphaned_run,
                )
                mark_session_running(session)
                db.add(
                    LogEntry(
                        session_id=session_id,
                        session_instance_id=session_instance_id,
                        level="INFO",
                        message=(
                            "Session started in manual mode."
                            + (
                                " Reused the last selected task automatically."
                                if queued_tasks
                                else " Use the session task list to choose the next task to run."
                            )
                        ),
                    )
                )
                db.commit()

            if not queued_tasks:
                task_status_summary = {
                    str(
                        task.status.value
                        if hasattr(task.status, "value")
                        else task.status
                    ): 0
                    for task in pending_tasks
                }
                for task in pending_tasks:
                    key = str(
                        task.status.value
                        if hasattr(task.status, "value")
                        else task.status
                    )
                    task_status_summary[key] = task_status_summary.get(key, 0) + 1

                db.add(
                    LogEntry(
                        session_id=session_id,
                        session_instance_id=session_instance_id,
                        level="WARN",
                        message="No tasks were queued for this session start",
                        log_metadata=json.dumps(
                            {"task_status_summary": task_status_summary}
                        ),
                    )
                )
                db.commit()

            session_key = (
                f"{session_key}:tasks={','.join([str(t['task_id']) for t in queued_tasks])}"
                if queued_tasks
                else session_key
            )

        session.started_at = datetime.now(timezone.utc)
        if session.execution_mode == "manual":
            mark_session_running(session)
        elif session.status != "stopped":
            mark_session_running(session)
        db.commit()

        db.add(
            LogEntry(
                session_id=session_id,
                session_instance_id=session_instance_id,
                level="INFO",
                message=f"Session started: {session.name}",
                log_metadata=json.dumps(
                    {
                        "event_type": "session_started",
                        "session_id": session_id,
                        "instance_id": session_instance_id,
                        "session_key": session_key,
                        "task_description": task_description,
                        "execution_mode": session.execution_mode,
                    }
                ),
            )
        )
        db.commit()

        return {
            "status": "started",
            "session_key": session_key,
            "session_id": session_id,
            "message": f"Session '{session.name}' started successfully",
        }
    except HTTPException:
        raise
    except Exception as exc:
        db.add(
            LogEntry(
                session_id=session_id,
                level="ERROR",
                message=f"Failed to start session: {str(exc)}",
                log_metadata=json.dumps(
                    {
                        "event_type": "session_start_failed",
                        "session_id": session_id,
                        "error": str(exc),
                    }
                ),
            )
        )
        db.commit()
        raise HTTPException(status_code=500, detail=str(exc))


async def stop_session_lifecycle(
    db: Session,
    session_id: int,
    *,
    force: bool = False,
    initiated_by: str | None = None,
    source: str | None = None,
) -> Dict[str, Any]:
    """Stop a running or paused session."""
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.status not in ["running", "paused", "active"]:
        raise HTTPException(status_code=400, detail="Session is not running")

    try:
        checkpoint_name = None
        try:
            checkpoint_service = CheckpointService(db)
            try:
                latest_checkpoint = checkpoint_service.load_checkpoint(session_id)
            except Exception:
                latest_checkpoint = None
            if not _checkpoint_has_execution_progress(latest_checkpoint):
                latest_checkpoint = (
                    _build_checkpoint_payload_from_session_state(db, session=session)
                    or latest_checkpoint
                )
            if not latest_checkpoint:
                raise CheckpointError(
                    f"No replayable checkpoint state found for session {session_id}"
                )
            checkpoint_name = f"stopped_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"
            checkpoint_service.save_checkpoint(
                session_id=session_id,
                checkpoint_name=checkpoint_name,
                context_data=latest_checkpoint.get("context", {}),
                orchestration_state=latest_checkpoint.get("orchestration_state", {}),
                current_step_index=latest_checkpoint.get("current_step_index"),
                step_results=latest_checkpoint.get("step_results", []),
            )
        except Exception:
            checkpoint_name = None

        revoked_ids = revoke_session_celery_tasks(db, session_id, terminate=True)
        runtime = create_agent_runtime(db, session_id, use_demo_mode=False)
        if not force:
            await runtime.stop_session()

        mark_session_stopped(session, stopped_at=datetime.now(timezone.utc))
        _running_link = (
            db.query(SessionTask)
            .filter(
                SessionTask.session_id == session_id,
                SessionTask.status == TaskStatus.RUNNING,
            )
            .first()
        )
        logger.info(
            "[STOP] session=%s task_id=%s initiated_by=%s source=%s "
            "handle_task_failure=False knowledge_failure_recording=skipped_manual_stop",
            session_id,
            _running_link.task_id if _running_link else None,
            initiated_by,
            source,
        )
        reset_count = _reset_running_session_tasks(
            db,
            session_id=session_id,
            next_status=TaskStatus.PENDING,
        )
        db.commit()

        db.add(
            LogEntry(
                session_id=session_id,
                level="INFO",
                message=f"Session stopped: {session.name}",
                log_metadata=json.dumps(
                    {
                        "event_type": "session_stopped",
                        "session_id": session_id,
                        "force": force,
                        "initiated_by": initiated_by or "unknown",
                        "source": source or "unspecified",
                        "revoked_task_ids": revoked_ids,
                        "checkpoint_name": checkpoint_name,
                        "reset_running_tasks": reset_count,
                    }
                ),
            )
        )
        db.commit()

        return {
            "status": "stopped",
            "session_id": session_id,
            "initiated_by": initiated_by or "unknown",
            "source": source or "unspecified",
            "message": f"Session '{session.name}' stopped successfully",
        }
    except HTTPException:
        raise
    except Exception as exc:
        db.add(
            LogEntry(
                session_id=session_id,
                level="ERROR",
                message=f"Failed to stop session: {str(exc)}",
                log_metadata=json.dumps(
                    {
                        "event_type": "session_stop_failed",
                        "session_id": session_id,
                        "error": str(exc),
                    }
                ),
            )
        )
        db.commit()
        raise HTTPException(status_code=500, detail=str(exc))


async def pause_session_lifecycle(db: Session, session_id: int) -> Dict[str, Any]:
    """Pause a running session and save checkpoint state."""
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.status not in ["running", "paused", "active"]:
        raise HTTPException(status_code=400, detail="Session is not running")

    try:
        revoked_ids = revoke_session_celery_tasks(db, session_id, terminate=True)
        checkpoint_name = None
        checkpoint_service = CheckpointService(db)
        try:
            latest_checkpoint = _select_checkpoint_payload_for_pause(
                checkpoint_service, session_id
            )
            if not latest_checkpoint:
                latest_checkpoint = _build_checkpoint_payload_from_session_state(
                    db, session=session
                )
            if not latest_checkpoint:
                raise CheckpointError(
                    f"No replayable checkpoint state found for session {session_id}"
                )
            checkpoint_name = f"paused_{datetime.now(UTC).strftime('%Y%m%d_%H%M%S')}"
            checkpoint_service.save_checkpoint(
                session_id=session_id,
                checkpoint_name=checkpoint_name,
                context_data=latest_checkpoint.get("context", {}),
                orchestration_state=latest_checkpoint.get("orchestration_state", {}),
                current_step_index=latest_checkpoint.get("current_step_index"),
                step_results=latest_checkpoint.get("step_results", []),
            )
        except Exception:
            latest_session_task = (
                db.query(SessionTask)
                .filter(SessionTask.session_id == session_id)
                .order_by(
                    SessionTask.started_at.desc().nullslast(), SessionTask.id.desc()
                )
                .first()
            )
            runtime = create_agent_runtime(
                db,
                session_id,
                latest_session_task.task_id if latest_session_task else None,
                use_demo_mode=False,
            )
            await runtime.pause_session()

        mark_session_paused(
            session,
            paused_at=datetime.now(timezone.utc),
            is_active=True,
        )
        reset_count = _reset_running_session_tasks(
            db,
            session_id=session_id,
            next_status=TaskStatus.PENDING,
        )
        db.commit()

        db.add(
            LogEntry(
                session_id=session_id,
                level="INFO",
                message=f"Session paused: {session.name}",
                log_metadata=json.dumps(
                    {
                        "event_type": "session_paused",
                        "session_id": session_id,
                        "revoked_task_ids": revoked_ids,
                        "checkpoint_name": checkpoint_name,
                        "reset_running_tasks": reset_count,
                    }
                ),
            )
        )
        db.commit()

        return {
            "status": "paused",
            "session_id": session_id,
            "message": f"Session '{session.name}' paused successfully",
        }
    except HTTPException:
        raise
    except Exception as exc:
        db.add(
            LogEntry(
                session_id=session_id,
                level="ERROR",
                message=f"Failed to pause session: {str(exc)}",
                log_metadata=json.dumps(
                    {
                        "event_type": "session_pause_failed",
                        "session_id": session_id,
                        "error": str(exc),
                    }
                ),
            )
        )
        db.commit()
        raise HTTPException(status_code=500, detail=str(exc))


async def request_human_intervention_lifecycle(
    db: Session,
    session_id: int,
    *,
    intervention_type: str,
    prompt: str,
    task_id: int | None = None,
    context_snapshot: Dict[str, Any] | None = None,
    expires_in_minutes: int = 120,
    initiated_by: str = "human",
) -> Dict[str, Any]:
    """Pause execution and create a HITL intervention request."""
    from app.services.session.intervention_service import create_intervention_request

    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    req = create_intervention_request(
        db,
        session_id=session_id,
        project_id=session.project_id,
        intervention_type=intervention_type,
        prompt=prompt,
        task_id=task_id,
        context_snapshot=context_snapshot,
        expires_in_minutes=expires_in_minutes,
        initiated_by=initiated_by,
    )

    return {
        "status": "awaiting_input",
        "session_id": session_id,
        "intervention_id": req.id,
        "intervention_type": req.intervention_type,
        "message": f"Session '{session.name}' is now waiting for human input",
    }


async def resume_session_lifecycle(
    db: Session,
    session_id: int,
    *,
    checkpoint_name: str | None = None,
) -> Dict[str, Any]:
    """Resume a paused, stopped, or awaiting_input session from checkpoint."""
    from app.tasks.worker import execute_orchestration_task

    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.status == "running":
        _recover_orphaned_running_session_if_needed(db, session=session)
        db.refresh(session)
    if session.status not in ["paused", "stopped", "awaiting_input"]:
        raise HTTPException(status_code=400, detail="Session is not resumable")

    try:
        checkpoint_service = CheckpointService(db)
        latest_session_task = (
            db.query(SessionTask)
            .filter(SessionTask.session_id == session_id)
            .order_by(SessionTask.started_at.desc().nullslast(), SessionTask.id.desc())
            .first()
        )
        probed_checkpoints: list[Dict[str, Any]] = []
        if checkpoint_name:
            checkpoint_data = _load_replayable_resume_checkpoint(
                checkpoint_service,
                session_id,
                checkpoint_name=checkpoint_name,
            )
        else:
            checkpoint_data = None
            for candidate_name in ("autosave_latest", "autosave_error", None):
                try:
                    payload = checkpoint_service.load_resume_checkpoint(
                        session_id, checkpoint_name=candidate_name
                    )
                except CheckpointError:
                    continue
                probed_checkpoints.append(payload)
                if _checkpoint_has_execution_progress(payload):
                    checkpoint_data = payload
                    break
        requested_checkpoint_name = None
        resolved_checkpoint_name = None
        restore_fidelity: Dict[str, Any] = {
            "score": 0,
            "status": "none",
            "summary": "No replayable checkpoint was selected; session will restart from the current workspace",
            "present_signals": [],
            "warnings": ["missing execution progress"],
        }
        context_data: Dict[str, Any] = {}
        task_id = latest_session_task.task_id if latest_session_task else None

        if checkpoint_data is not None:
            requested_checkpoint_name = checkpoint_data.get(
                "_requested_checkpoint_name"
            )
            resolved_checkpoint_name = checkpoint_data.get(
                "_resolved_checkpoint_name"
            ) or checkpoint_data.get("checkpoint_name")
            restore_fidelity = checkpoint_service._checkpoint_restore_fidelity(
                checkpoint_data
            )
            context_data = checkpoint_data.get("context", {})
            task_id = context_data.get("task_id") or task_id
        elif checkpoint_name:
            raise HTTPException(
                status_code=404,
                detail=(
                    f"No replayable checkpoint found for session {session_id}: "
                    f"'{checkpoint_name}'"
                ),
            )

        if not task_id:
            if checkpoint_name:
                try:
                    fallback_checkpoint = checkpoint_service.load_resume_checkpoint(
                        session_id, checkpoint_name=checkpoint_name
                    )
                except CheckpointError as checkpoint_error:
                    raise HTTPException(
                        status_code=404,
                        detail=(
                            f"No usable checkpoint found for session {session_id}: "
                            f"{checkpoint_error}"
                        ),
                    ) from checkpoint_error
                requested_checkpoint_name = fallback_checkpoint.get(
                    "_requested_checkpoint_name"
                )
                resolved_checkpoint_name = fallback_checkpoint.get(
                    "_resolved_checkpoint_name"
                ) or fallback_checkpoint.get("checkpoint_name")
                restore_fidelity = checkpoint_service._checkpoint_restore_fidelity(
                    fallback_checkpoint
                )
                context_data = fallback_checkpoint.get("context", {})
                task_id = context_data.get("task_id")
            else:
                for fallback_checkpoint in probed_checkpoints:
                    context_data = fallback_checkpoint.get("context", {}) or {}
                    task_id = context_data.get("task_id")
                    if task_id:
                        restore_fidelity = (
                            checkpoint_service._checkpoint_restore_fidelity(
                                fallback_checkpoint
                            )
                        )
                        break

        task = db.query(Task).filter(Task.id == task_id).first() if task_id else None
        if not task:
            raise HTTPException(
                status_code=404, detail="No task found to resume from checkpoint"
            )

        resumed_at = datetime.now(timezone.utc)
        session.instance_id = str(uuid.uuid4())
        db.flush()
        _ensure_session_task_ready_for_resume(
            db,
            session=session,
            task=task,
            resumed_at=resumed_at,
        )
        prompt = context_data.get("task_description") or task.description or task.title
        resume_has_progress = _checkpoint_has_execution_progress(checkpoint_data)

        if resume_has_progress:
            task_execution = create_task_execution(
                db,
                session_id=session_id,
                task_id=task.id,
            )
            result = execute_orchestration_task.delay(
                session_id=session_id,
                task_id=task.id,
                prompt=prompt,
                timeout_seconds=DEFAULT_ORCHESTRATION_TIMEOUT_SECONDS,
                resume_checkpoint_name=resolved_checkpoint_name,
                expected_session_instance_id=session.instance_id,
                task_execution_id=task_execution.id,
            )
            dispatch_mode = "checkpoint_resume"
            task_execution_id = task_execution.id
        else:
            queued = queue_task_for_session(
                db=db,
                session=session,
                task_id=task.id,
                timeout_seconds=DEFAULT_ORCHESTRATION_TIMEOUT_SECONDS,
            )

            class _QueuedResult:
                id = queued["celery_id"]

            result = _QueuedResult()
            dispatch_mode = "fresh_requeue"
            task_execution_id = queued.get("task_execution_id")

        mark_session_running(session)
        session.resumed_at = resumed_at
        db.commit()

        db.add(
            LogEntry(
                session_id=session_id,
                task_execution_id=task_execution_id,
                level="INFO",
                message=(
                    f"Session resumed: {session.name}"
                    + (
                        f" (requested checkpoint: {requested_checkpoint_name}, resolved checkpoint: {resolved_checkpoint_name})"
                        if requested_checkpoint_name
                        and requested_checkpoint_name != resolved_checkpoint_name
                        else (
                            f" (checkpoint: {resolved_checkpoint_name})"
                            if resume_has_progress and resolved_checkpoint_name
                            else " (fresh run from current workspace)"
                        )
                    )
                ),
                log_metadata=json.dumps(
                    {
                        "event_type": "session_resumed",
                        "session_id": session_id,
                        "requested_checkpoint_name": requested_checkpoint_name,
                        "checkpoint_name": resolved_checkpoint_name,
                        "resolved_checkpoint_name": resolved_checkpoint_name,
                        "celery_task_id": result.id,
                        "task_execution_id": task_execution_id,
                        "task_id": task.id,
                        "restore_fidelity": restore_fidelity,
                        "dispatch_mode": dispatch_mode,
                    }
                ),
            )
        )
        db.commit()

        return {
            "status": "resumed",
            "session_id": session_id,
            "requested_checkpoint_name": requested_checkpoint_name,
            "resolved_checkpoint_name": resolved_checkpoint_name,
            "restore_fidelity": restore_fidelity,
            "message": (
                (
                    f"Session '{session.name}' resumed successfully"
                    + (
                        f" using resolved checkpoint '{resolved_checkpoint_name}' instead of '{requested_checkpoint_name}'"
                        if requested_checkpoint_name
                        and requested_checkpoint_name != resolved_checkpoint_name
                        else (
                            f" using checkpoint '{resolved_checkpoint_name}'"
                            if resolved_checkpoint_name
                            else " from the current workspace"
                        )
                    )
                )
                if resume_has_progress
                else (
                    f"Session '{session.name}' resumed by queueing a fresh run from the current workspace because "
                    f"no execution progress to replay was available"
                )
            ),
        }
    except HTTPException:
        raise
    except Exception as exc:
        db.add(
            LogEntry(
                session_id=session_id,
                level="ERROR",
                message=f"Failed to resume session: {str(exc)}",
                log_metadata=json.dumps(
                    {
                        "event_type": "session_resume_failed",
                        "session_id": session_id,
                        "error": str(exc),
                    }
                ),
            )
        )
        db.commit()
        raise HTTPException(status_code=500, detail=str(exc))
