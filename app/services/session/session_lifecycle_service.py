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

from app.models import LogEntry, Session as SessionModel, SessionTask, Task, TaskStatus
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
from app.services.task_service import TaskService


logger = logging.getLogger(__name__)

_ORPHANED_PLANNING_RECOVERY_SECONDS = 120


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
    """Normalize tasks/links after pause/stop so resume does not inherit RUNNING state."""

    running_links = (
        db.query(SessionTask)
        .filter(
            SessionTask.session_id == session_id,
            SessionTask.status == TaskStatus.RUNNING,
        )
        .all()
    )
    updated = 0
    seen_task_ids: set[int] = set()

    for link in running_links:
        link.status = next_status
        link.completed_at = None
        updated += 1
        if link.task_id in seen_task_ids:
            continue
        seen_task_ids.add(link.task_id)
        task = db.query(Task).filter(Task.id == link.task_id).first()
        if not task:
            continue
        if task.status == TaskStatus.RUNNING:
            task.status = next_status
            task.completed_at = None
            task.error_message = None

    return updated


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

    latest_link.status = TaskStatus.PENDING
    latest_link.started_at = None
    latest_link.completed_at = None
    task.status = TaskStatus.PENDING
    task.started_at = None
    task.completed_at = None
    task.error_message = (
        "Recovered an orphaned planning run after no progress logs arrived "
        "following planning-response validation."
    )
    task.current_step = 0
    session.status = "stopped"
    session.is_active = False
    set_session_alert(
        db,
        session,
        "warn",
        "Recovered an orphaned planning run so the session can be started again.",
    )
    db.add(
        LogEntry(
            session_id=session.id,
            task_id=task.id,
            session_instance_id=session.instance_id,
            level="WARN",
            message=(
                "Recovered orphaned running task after planning-response handling "
                "stalled without further progress"
            ),
        )
    )
    db.commit()
    return True


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
        session_task_link.status = TaskStatus.PENDING
        session_task_link.started_at = None
        session_task_link.completed_at = None

    task.status = TaskStatus.PENDING
    task.started_at = None
    task.completed_at = None
    task.error_message = None
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

    if resume_has_progress and resolved_checkpoint_name:
        prompt = task.description or task.title
        result = execute_orchestration_task.delay(
            session_id=session.id,
            task_id=task.id,
            prompt=prompt,
            timeout_seconds=DEFAULT_ORCHESTRATION_TIMEOUT_SECONDS,
            resume_checkpoint_name=resolved_checkpoint_name,
            expected_session_instance_id=session_instance_id,
        )
        db.add(
            LogEntry(
                session_id=session.id,
                session_instance_id=session_instance_id,
                task_id=task.id,
                level="INFO",
                message=(
                    f"Manual session restart resumed task {task.id} from checkpoint "
                    f"{resolved_checkpoint_name}"
                ),
                log_metadata=json.dumps(
                    {
                        "celery_task_id": result.id,
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
        session.is_active = False
        session.status = "stopped"
        db.commit()

    if session.status == "active":
        logger.warning(
            "Session %s has 'active' status. Treating as stopped and resetting...",
            session_id,
        )
        session.is_active = False
        session.status = "stopped"
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

        set_session_alert(db, session, None, None)

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

                task.status = TaskStatus.PENDING
                task.error_message = None
                task.started_at = None
                task.completed_at = None
                task.current_step = 0
                link.status = TaskStatus.PENDING
                link.started_at = None
                link.completed_at = None
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
                    session.status = "stopped"
                    session.is_active = False
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
                )
                session.status = "running"
                session.is_active = True
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
            session.is_active = True
            session.status = "running"
        elif session.status != "stopped":
            session.is_active = True
            session.status = "running"
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

        session.is_active = False
        session.stopped_at = datetime.now(timezone.utc)
        session.status = "stopped"
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

        session.is_active = True
        session.status = "paused"
        session.paused_at = datetime.now(timezone.utc)
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
        "status": "waiting_for_human",
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
    """Resume a paused, stopped, or waiting_for_human session from checkpoint."""
    from app.tasks.worker import execute_orchestration_task

    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.status == "running":
        _recover_orphaned_running_session_if_needed(db, session=session)
        db.refresh(session)
    if session.status not in ["paused", "stopped", "waiting_for_human"]:
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
            result = execute_orchestration_task.delay(
                session_id=session_id,
                task_id=task.id,
                prompt=prompt,
                timeout_seconds=DEFAULT_ORCHESTRATION_TIMEOUT_SECONDS,
                resume_checkpoint_name=resolved_checkpoint_name,
                expected_session_instance_id=session.instance_id,
            )
            dispatch_mode = "checkpoint_resume"
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

        session.status = "running"
        session.is_active = True
        session.resumed_at = resumed_at
        db.commit()

        db.add(
            LogEntry(
                session_id=session_id,
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
