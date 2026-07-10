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
from app.services.agents.interfaces import AgentRuntimeError
from app.services.workspace.project_isolation_service import (
    resolve_project_workspace_path,
)
from app.services.orchestration.prompt_templates import OrchestrationState
from app.services.orchestration.run_state import (
    mark_task_attempt_cancelled,
    mark_task_attempt_done,
    mark_task_attempt_failed,
    mark_task_attempt_pending,
    mark_task_attempt_running,
)
from app.services.orchestration.state.session_state import (
    mark_session_running,
    mark_session_stopped,
    normalize_session_status,
    SessionStatus,
)
from app.services.session.session_runtime_service import ensure_task_workspace
from app.services.session.execution_policy import (
    timeout_terminal_state_blocks_late_success,
)
from app.services.tasks.execution import create_task_execution
from app.services.tasks.service import TaskService
from app.services.tasks.tool_tracking import ToolTrackingService
from app.services.workspace.system_settings import (
    get_effective_agent_backend,
    get_effective_runtime_root,
)
from app.services.orchestration.execution.runtime import (
    build_runtime_executor_context,
    maybe_allocate_runtime_workspace,
    maybe_bind_runtime_cwd_override,
    dispose_runtime_workspace_safely,
)
from app.config import settings

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
    if normalize_session_status(session.status) == SessionStatus.RUNNING.value:
        raise HTTPException(
            status_code=409,
            detail=(
                "Session already has active execution in progress. "
                "Stop it before queueing another task."
            ),
        )

    selected_task = None
    session_task_link = None
    task_workspace = None
    task_execution = None

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
            if existing_link:
                session_task_link = existing_link
            else:
                session_task_link = SessionTask(
                    session_id=session_id,
                    task_id=selected_task.id,
                )
                db.add(session_task_link)
            task_execution = create_task_execution(
                db,
                session_id=session_id,
                task_id=selected_task.id,
                status=TaskStatus.PENDING,
            )

            mark_task_attempt_running(
                task=selected_task,
                session_task_link=session_task_link,
                task_execution=task_execution,
                started_at=datetime.now(timezone.utc),
            )
            if session.status not in ("running", "paused"):
                mark_session_running(session)
            db.add(
                LogEntry(
                    session_id=session_id,
                    session_instance_id=session.instance_id,
                    task_id=selected_task.id,
                    task_execution_id=task_execution.id,
                    level="INFO",
                    message=f"Prepared task workspace: {task_workspace['workspace_path']}",
                    log_metadata=json.dumps(
                        {
                            **task_workspace,
                            "task_execution_id": task_execution.id,
                        }
                    ),
                )
            )
            db.commit()

        runtime = create_agent_runtime(
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
        await runtime.create_session(task_description)

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

        # Phase 23D-2: this direct-execution endpoint (POST
        # /sessions/{id}/execute) previously never built a
        # RuntimeExecutorContext -- it always ran against the real Project
        # Workspace, ignoring RUNTIME_WORKSPACE_ENABLED, mirroring the same
        # bypass the POST /tasks/{id}/execute endpoint had. Reuse the same
        # allocate/bind/dispose primitives worker.py's canonical dispatch
        # uses. Only applies when a task (and therefore a task_execution_id
        # to key sandbox allocation by) is selected AND the task executes in
        # the canonical project root -- task-subfolder-scoped executions are
        # not redirected, matching worker.py's canonical-baseline-only scope
        # (Phase 23C). A raw session prompt with no task_id has no Project
        # Workspace redirect target.
        _runtime_sandbox = None
        _runtime_context = None
        _project_root = None
        if (
            selected_task
            and task_workspace
            and session.project
            and task_execution
            and task_workspace.get("workspace_scope") == "canonical_project_root"
        ):
            _project_root = TaskService(db).get_project_root(session.project)
            _runtime_root = get_effective_runtime_root(db)
            _execution_backend = get_effective_agent_backend(
                settings.AGENT_BACKEND, db=db
            )
            _runtime_sandbox = maybe_allocate_runtime_workspace(
                enabled=settings.RUNTIME_WORKSPACE_ENABLED,
                project_id=session.project_id,
                task_execution_id=task_execution.id,
                canonical_baseline_dir=_project_root,
                executor=_execution_backend,
                runtime_root=_runtime_root,
            )
            _runtime_context = build_runtime_executor_context(
                sandbox=_runtime_sandbox,
                project_workspace=_project_root,
                executor=_execution_backend,
                project_id=session.project_id,
                task_execution_id=task_execution.id,
                runtime_root=_runtime_root,
            )
            orchestration_state._project_dir_override = str(
                _runtime_context.runtime_workspace
            )
            maybe_bind_runtime_cwd_override(runtime, _runtime_context)
            if hasattr(runtime, "bind_runtime_workspace"):
                runtime.bind_runtime_workspace(_runtime_context)

        try:
            result = await runtime.execute_task_with_orchestration(
                prompt, timeout_seconds, orchestration_state=orchestration_state
            )
        finally:
            if hasattr(runtime, "release_runtime_workspace_binding"):
                runtime.release_runtime_workspace_binding()
            dispose_runtime_workspace_safely(
                _runtime_sandbox,
                project_root=_project_root,
                logger_obj=logger,
            )
        if task_execution:
            mark_task_attempt_done(
                task=selected_task,
                session_task_link=session_task_link,
                task_execution=task_execution,
                completed_at=datetime.now(timezone.utc),
            )
            db.commit()

        return {
            "status": "completed",
            "result": result,
            "execution_id": f"exec_{session_id}_{datetime.utcnow().timestamp()}",
            "task_execution_id": task_execution.id if task_execution else None,
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
            mark_task_attempt_failed(
                task=selected_task,
                session_task_link=session_task_link,
                task_execution=task_execution,
                error_message=str(exc),
                completed_at=datetime.now(timezone.utc),
            )

        mark_session_stopped(session, stopped_at=datetime.now(timezone.utc))

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
                task_execution_id=task_execution.id if task_execution else None,
                level="ERROR",
                message=(
                    f"Task execution failed: {error_detail}"
                    if isinstance(exc, AgentRuntimeError)
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
                if isinstance(exc, AgentRuntimeError)
                else "Task execution failed. Check session logs for details."
            ),
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
