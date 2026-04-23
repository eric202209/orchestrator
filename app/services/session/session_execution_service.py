"""Session execution and tool-tracking helpers."""

from __future__ import annotations

import json
import logging
import traceback
from datetime import datetime, timezone
from typing import Any, Dict, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import LogEntry, Session as SessionModel, SessionTask, TaskStatus
from app.schemas import TaskExecuteRequest
from app.services.agents.agent_runtime import create_agent_runtime
from app.services.agents.openclaw_service import OpenClawSessionError
from app.services.workspace.project_isolation_service import (
    resolve_project_workspace_path,
)
from app.services.prompt_templates import OrchestrationState
from app.services.session.session_runtime_service import ensure_task_workspace
from app.services.tool_tracking_service import ToolTrackingService


logger = logging.getLogger(__name__)


async def start_openclaw_session_payload(
    db: Session, session_id: int, *, task_description: str
) -> Dict[str, Any]:
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        openclaw_service = create_agent_runtime(db, session_id, use_demo_mode=False)
        session_key = await openclaw_service.create_openclaw_session(task_description)

        db.add(
            LogEntry(
                session_id=session_id,
                level="INFO",
                message=f"OpenClaw session started: {task_description[:100]}",
                log_metadata=json.dumps(
                    {"session_key": session_key, "task_description": task_description}
                ),
            )
        )
        db.commit()

        return {
            "status": "started",
            "session_key": session_key,
            "session_id": session_id,
            "message": f"OpenClaw session created for task: {task_description[:50]}...",
        }
    except Exception as exc:
        db.add(
            LogEntry(
                session_id=session_id,
                level="ERROR",
                message=f"Failed to start OpenClaw session: {str(exc)}",
            )
        )
        db.commit()
        raise HTTPException(status_code=500, detail=str(exc))


async def execute_task_payload(
    db: Session, session_id: int, task_request: TaskExecuteRequest
) -> Dict[str, Any]:
    prompt = task_request.task
    timeout_seconds = task_request.timeout_seconds
    if not prompt:
        raise HTTPException(status_code=422, detail="Task prompt is required")

    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    selected_task = None
    task_workspace = None

    try:
        if task_request.task_id:
            from app.models import Task

            selected_task = (
                db.query(Task)
                .filter(
                    Task.id == task_request.task_id,
                    Task.project_id == session.project_id,
                )
                .first()
            )
            if not selected_task:
                raise HTTPException(
                    status_code=404, detail="Selected task not found for this session"
                )

            task_workspace = ensure_task_workspace(db, session, selected_task.id)

            existing_link = (
                db.query(SessionTask)
                .filter(
                    SessionTask.session_id == session_id,
                    SessionTask.task_id == selected_task.id,
                )
                .first()
            )
            if not existing_link:
                db.add(
                    SessionTask(
                        session_id=session_id,
                        task_id=selected_task.id,
                        status=TaskStatus.RUNNING,
                        started_at=datetime.utcnow(),
                    )
                )

            selected_task.status = TaskStatus.RUNNING
            selected_task.started_at = datetime.utcnow()
            db.add(
                LogEntry(
                    session_id=session_id,
                    session_instance_id=session.instance_id,
                    task_id=selected_task.id,
                    level="INFO",
                    message=f"Prepared task workspace: {task_workspace['workspace_path']}",
                    log_metadata=json.dumps(task_workspace),
                )
            )
            db.commit()

        openclaw_service = create_agent_runtime(
            db,
            session_id,
            task_id=selected_task.id if selected_task else None,
            use_demo_mode=False,
        )

        task_description = (
            selected_task.description
            if selected_task and selected_task.description
            else session.description or session.name
        )
        await openclaw_service.create_openclaw_session(task_description)

        orchestration_state = None
        if selected_task and task_workspace:
            project_name = session.project.name if session.project else ""
            orchestration_state = OrchestrationState(
                session_id=str(session_id),
                task_description=prompt,
                project_name=project_name,
                project_context=session.description or "",
                task_id=selected_task.id,
            )

            if session.project and session.project.workspace_path:
                workspace_path = str(
                    resolve_project_workspace_path(
                        session.project.workspace_path, session.project.name
                    )
                )
                orchestration_state._workspace_path_override = workspace_path

            if selected_task.task_subfolder:
                orchestration_state._task_subfolder_override = (
                    selected_task.task_subfolder
                )
            if task_workspace.get("workspace_path"):
                orchestration_state._project_dir_override = task_workspace[
                    "workspace_path"
                ]

        result = await openclaw_service.execute_task_with_orchestration(
            prompt, timeout_seconds, orchestration_state=orchestration_state
        )

        return {
            "status": "completed",
            "result": result,
            "execution_id": f"exec_{session_id}_{datetime.utcnow().timestamp()}",
            "task_id": selected_task.id if selected_task else None,
            "task_subfolder": (
                task_workspace["task_subfolder"] if task_workspace else None
            ),
            "workspace_path": (
                task_workspace["workspace_path"] if task_workspace else None
            ),
        }
    except HTTPException:
        raise
    except Exception as exc:
        if selected_task:
            selected_task.status = TaskStatus.FAILED
            selected_task.error_message = str(exc)
            selected_task.completed_at = datetime.utcnow()

        session.is_active = False
        session.status = "stopped"
        session.stopped_at = datetime.now(timezone.utc)

        traceback_text = traceback.format_exc()
        logger.error(
            "Task execution failed for session %s: %s\n%s",
            session_id,
            str(exc),
            traceback_text,
        )
        error_detail = str(exc)
        db.add(
            LogEntry(
                session_id=session_id,
                task_id=selected_task.id if selected_task else None,
                level="ERROR",
                message=(
                    f"Task execution failed: {error_detail}"
                    if isinstance(exc, OpenClawSessionError)
                    else f"Task execution failed: {str(exc)}"
                ),
                log_metadata=json.dumps({"traceback": traceback_text}),
            )
        )
        db.commit()
        raise HTTPException(
            status_code=500,
            detail=(
                error_detail
                if isinstance(exc, OpenClawSessionError)
                else "Task execution failed. Check session logs for details."
            ),
        )


def get_tool_execution_history_payload(
    db: Session,
    session_id: int,
    *,
    task_id: Optional[int] = None,
    limit: int = 50,
    tool_name: Optional[str] = None,
) -> Dict[str, Any]:
    tool_service = ToolTrackingService(db)
    executions = tool_service.get_execution_history(
        session_id=session_id, task_id=task_id, limit=limit, tool_name=tool_name
    )
    return {"total": len(executions), "executions": executions}


def get_session_statistics_payload(
    db: Session, session_id: int, *, days: int = 7
) -> Dict[str, Any]:
    tool_service = ToolTrackingService(db)
    total_logs = db.query(LogEntry).filter(LogEntry.session_id == session_id).count()
    info_logs = (
        db.query(LogEntry)
        .filter(LogEntry.session_id == session_id, LogEntry.level == "INFO")
        .count()
    )
    error_logs = (
        db.query(LogEntry)
        .filter(LogEntry.session_id == session_id, LogEntry.level == "ERROR")
        .count()
    )
    tool_stats = tool_service.get_tool_statistics(session_id, days)
    return {
        "session_id": session_id,
        "period_days": days,
        "logs": {"total": total_logs, "info": info_logs, "errors": error_logs},
        "tools": tool_stats,
    }


def track_tool_execution_payload(
    db: Session,
    *,
    session_id: int,
    execution_id: str,
    tool_name: str,
    params: dict,
    result: Any,
    success: bool,
    task_id: Optional[int] = None,
    session_instance_id: Optional[str] = None,
) -> Dict[str, Any]:
    tool_service = ToolTrackingService(db)
    execution = tool_service.track(
        execution_id=execution_id,
        tool_name=tool_name,
        params=params,
        result=result,
        success=success,
        session_id=session_id,
        task_id=task_id,
        session_instance_id=session_instance_id,
    )
    return {"status": "tracked", "execution_id": execution_id, "tool": tool_name}
