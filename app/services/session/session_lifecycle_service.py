"""Session lifecycle control helpers."""

from __future__ import annotations

import json
import logging
import uuid
from datetime import UTC, datetime, timezone
from typing import Any, Dict

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import LogEntry, Session as SessionModel, SessionTask, Task, TaskStatus
from app.services.agents.agent_runtime import create_agent_runtime
from app.services.workspace.checkpoint_service import CheckpointError, CheckpointService
from app.services.session.session_runtime_service import (
    DEFAULT_ORCHESTRATION_TIMEOUT_SECONDS,
    queue_task_for_session,
    reopen_failed_ordered_task_if_needed,
    revoke_session_celery_tasks,
    set_session_alert,
)
from app.services.task_service import TaskService


logger = logging.getLogger(__name__)


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


async def start_session_lifecycle(db: Session, session_id: int) -> Dict[str, Any]:
    """Start a session and queue work if needed."""
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

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
            reopen_failed_ordered_task_if_needed(db, session)
            pending_tasks = task_service.get_project_tasks(session.project_id)

            if not any(task.status == TaskStatus.PENDING for task in pending_tasks):
                retryable_failed_tasks = [
                    task
                    for task in pending_tasks
                    if task.status in [TaskStatus.FAILED, TaskStatus.CANCELLED]
                ]
                for task in retryable_failed_tasks:
                    task.status = TaskStatus.PENDING
                    task.error_message = None
                    task.started_at = None
                    task.completed_at = None
                    task.current_step = 0
                    task.steps = None

                if retryable_failed_tasks:
                    db.add(
                        LogEntry(
                            session_id=session_id,
                            session_instance_id=session_instance_id,
                            level="INFO",
                            message=(
                                f"Recovered {len(retryable_failed_tasks)} failed/cancelled "
                                "task(s) for retry"
                            ),
                        )
                    )
                    db.commit()
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
                session.status = "running"
                session.is_active = True
                db.add(
                    LogEntry(
                        session_id=session_id,
                        session_instance_id=session_instance_id,
                        level="INFO",
                        message="Session started in manual mode. Use the session task list to choose the next task to run.",
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
                        "session_key": session_key,
                        "task_description": task_description,
                        "instance_id": session_instance_id,
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
            )
        )
        db.commit()
        raise HTTPException(status_code=500, detail=str(exc))


async def stop_session_lifecycle(
    db: Session, session_id: int, *, force: bool = False
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
            latest_checkpoint = checkpoint_service.load_checkpoint(session_id)
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
                        "force": force,
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
            latest_checkpoint = checkpoint_service.load_checkpoint(session_id)
            checkpoint_name = f"paused_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
            checkpoint_service.save_checkpoint(
                session_id=session_id,
                checkpoint_name=checkpoint_name,
                context_data=latest_checkpoint.get("context", {}),
                orchestration_state=latest_checkpoint.get("orchestration_state", {}),
                current_step_index=latest_checkpoint.get("current_step_index"),
                step_results=latest_checkpoint.get("step_results", []),
            )
        except Exception:
            runtime = create_agent_runtime(db, session_id, use_demo_mode=False)
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
            )
        )
        db.commit()
        raise HTTPException(status_code=500, detail=str(exc))


async def resume_session_lifecycle(db: Session, session_id: int) -> Dict[str, Any]:
    """Resume a paused or stopped session from checkpoint."""
    from app.tasks.worker import execute_orchestration_task

    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if session.status not in ["paused", "stopped"]:
        raise HTTPException(status_code=400, detail="Session is not resumable")

    try:
        checkpoint_service = CheckpointService(db)
        try:
            checkpoint_data = checkpoint_service.load_resume_checkpoint(session_id)
        except CheckpointError as checkpoint_error:
            raise HTTPException(
                status_code=404,
                detail=f"No usable checkpoint found for session {session_id}: {checkpoint_error}",
            ) from checkpoint_error

        requested_checkpoint_name = checkpoint_data.get("_requested_checkpoint_name")
        checkpoint_name = checkpoint_data.get(
            "_resolved_checkpoint_name"
        ) or checkpoint_data.get("checkpoint_name")
        context_data = checkpoint_data.get("context", {})
        task_id = context_data.get("task_id")
        if not task_id:
            latest_session_task = (
                db.query(SessionTask)
                .filter(SessionTask.session_id == session_id)
                .order_by(
                    SessionTask.started_at.desc().nullslast(), SessionTask.id.desc()
                )
                .first()
            )
            task_id = latest_session_task.task_id if latest_session_task else None

        task = db.query(Task).filter(Task.id == task_id).first() if task_id else None
        if not task:
            raise HTTPException(
                status_code=404, detail="No task found to resume from checkpoint"
            )

        prompt = context_data.get("task_description") or task.description or task.title
        result = execute_orchestration_task.delay(
            session_id=session_id,
            task_id=task.id,
            prompt=prompt,
            timeout_seconds=DEFAULT_ORCHESTRATION_TIMEOUT_SECONDS,
            resume_checkpoint_name=checkpoint_name,
        )

        session.status = "running"
        session.is_active = True
        session.resumed_at = datetime.now(timezone.utc)
        db.commit()

        db.add(
            LogEntry(
                session_id=session_id,
                level="INFO",
                message=(
                    f"Session resumed: {session.name}"
                    + (
                        f" (requested checkpoint: {requested_checkpoint_name}, resolved checkpoint: {checkpoint_name})"
                        if requested_checkpoint_name
                        and requested_checkpoint_name != checkpoint_name
                        else f" (checkpoint: {checkpoint_name})"
                    )
                ),
                log_metadata=json.dumps(
                    {
                        "requested_checkpoint_name": requested_checkpoint_name,
                        "checkpoint_name": checkpoint_name,
                        "resolved_checkpoint_name": checkpoint_name,
                        "celery_task_id": result.id,
                        "task_id": task.id,
                    }
                ),
            )
        )
        db.commit()

        return {
            "status": "resumed",
            "session_id": session_id,
            "requested_checkpoint_name": requested_checkpoint_name,
            "resolved_checkpoint_name": checkpoint_name,
            "message": (
                f"Session '{session.name}' resumed successfully"
                + (
                    f" using resolved checkpoint '{checkpoint_name}' instead of '{requested_checkpoint_name}'"
                    if requested_checkpoint_name
                    and requested_checkpoint_name != checkpoint_name
                    else f" using checkpoint '{checkpoint_name}'"
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
            )
        )
        db.commit()
        raise HTTPException(status_code=500, detail=str(exc))
