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
from app.services.agents.agent_runtime import create_agent_runtime
from app.services.orchestration.run_state import (
    mark_task_attempt_cancelled,
    mark_task_attempt_done,
    mark_task_attempt_failed,
    mark_task_attempt_pending,
    mark_task_attempt_running,
)
from app.services.orchestration.state.session_state import (
    normalize_session_status,
    SessionStatus,
)
from app.services.session.execution_policy import (
    timeout_terminal_state_blocks_late_success,
)
from app.services.tasks.tool_tracking import ToolTrackingService

logger = logging.getLogger(__name__)


async def start_session_payload(
    db: Session, session_id: int, *, task_description: str
) -> Dict[str, Any]:
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    if normalize_session_status(session.status) == SessionStatus.RUNNING.value:
        raise HTTPException(
            status_code=409,
            detail=(
                "Session already has active execution in progress. "
                "Stop it before starting another direct execution."
            ),
        )

    try:
        runtime = create_agent_runtime(db, session_id, use_demo_mode=False)
        session_key = await runtime.create_session(task_description)

        db.add(
            LogEntry(
                session_id=session_id,
                level="INFO",
                message=f"Agent session started: {task_description[:100]}",
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
            "message": f"Agent session created for task: {task_description[:50]}...",
        }
    except Exception as exc:
        db.add(
            LogEntry(
                session_id=session_id,
                level="ERROR",
                message=f"Failed to start agent session: {str(exc)}",
            )
        )
        db.commit()
        raise HTTPException(status_code=500, detail=str(exc))


async def start_agent_session_payload(
    db: Session, session_id: int, *, task_description: str
) -> Dict[str, Any]:
    """Primary backend-neutral alias for starting a runtime-backed session."""

    return await start_session_payload(
        db, session_id, task_description=task_description
    )


# BACKEND_COUPLING: function name encodes OpenClaw as the transport; kept for backward-compat
async def start_openclaw_session_payload(
    db: Session, session_id: int, *, task_description: str
) -> Dict[str, Any]:
    """Backward-compatible alias for callers still using the old name."""

    return await start_session_payload(
        db, session_id, task_description=task_description
    )


def update_execution_failure_metadata(
    db: Session,
    task_execution_id: int,
    *,
    failure_category: str,
    backend_id: str | None = None,
) -> None:
    """Write failure_category and optional backend_id to a TaskExecution row."""
    from app.models import TaskExecution

    execution = (
        db.query(TaskExecution).filter(TaskExecution.id == task_execution_id).first()
    )
    if not execution:
        return
    if hasattr(execution, "failure_category"):
        execution.failure_category = failure_category
    if backend_id is not None and hasattr(execution, "backend_id"):
        execution.backend_id = backend_id


def mark_execution_running(
    *,
    task: object | None,
    session_task_link: object | None = None,
    task_execution: object | None = None,
    started_at: datetime | None = None,
) -> datetime:
    """Mark an attempt running through the session execution service boundary."""

    return mark_task_attempt_running(
        task=task,
        session_task_link=session_task_link,
        task_execution=task_execution,
        started_at=started_at,
    )


def mark_execution_pending(
    *,
    task: object | None,
    session_task_link: object | None = None,
    task_execution: object | None = None,
    reset_started_at: bool = False,
    reset_steps: bool = False,
    workspace_status: str | None = None,
    error_message: str | None = None,
) -> None:
    """Reset an attempt through the session execution service boundary."""

    mark_task_attempt_pending(
        task=task,
        session_task_link=session_task_link,
        task_execution=task_execution,
        reset_started_at=reset_started_at,
        reset_steps=reset_steps,
        workspace_status=workspace_status,
        error_message=error_message,
    )


def mark_execution_failed(
    *,
    task: object | None,
    session_task_link: object | None = None,
    task_execution: object | None = None,
    error_message: str | None = None,
    completed_at: datetime | None = None,
    workspace_status: str | None = None,
) -> datetime:
    """Mark an attempt failed through the session execution service boundary."""

    return mark_task_attempt_failed(
        task=task,
        session_task_link=session_task_link,
        task_execution=task_execution,
        error_message=error_message,
        completed_at=completed_at,
        workspace_status=workspace_status,
    )


def mark_execution_done(
    *,
    task: object | None,
    session_task_link: object | None = None,
    task_execution: object | None = None,
    completed_at: datetime | None = None,
) -> datetime:
    """Mark an attempt complete through the session execution service boundary."""

    if timeout_terminal_state_blocks_late_success(task_execution):
        return task_execution.completed_at or completed_at or datetime.now(timezone.utc)

    return mark_task_attempt_done(
        task=task,
        session_task_link=session_task_link,
        task_execution=task_execution,
        completed_at=completed_at,
    )


def mark_execution_cancelled(
    *,
    task: object | None,
    session_task_link: object | None = None,
    task_execution: object | None = None,
    completed_at: datetime | None = None,
) -> datetime:
    """Mark an attempt cancelled through the session execution service boundary."""

    return mark_task_attempt_cancelled(
        task=task,
        session_task_link=session_task_link,
        task_execution=task_execution,
        completed_at=completed_at,
    )


def mark_execution_cancelled_for_session_stop(
    db: Session,
    *,
    session_id: int,
    task: object | None = None,
    session_task_link: object | None = None,
    task_execution: object | None = None,
) -> None:
    """Cancel an in-flight attempt when a session is stopped or paused.

    Delegates to the shared transition helper; exists so worker/endpoint code
    does not call transitions directly for this lifecycle scenario.
    """
    mark_execution_cancelled(
        task=task,
        session_task_link=session_task_link,
        task_execution=task_execution,
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
