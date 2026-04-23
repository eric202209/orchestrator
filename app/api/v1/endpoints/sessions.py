"""Sessions API endpoints with OpenClaw integration"""

from fastapi import APIRouter, Depends, HTTPException, status, WebSocket
from fastapi.requests import Request
from pydantic import BaseModel
from sqlalchemy.orm import Session
from typing import List, Optional, Any, Dict
from datetime import datetime, timezone
import json
import logging
import uuid

from app.database import get_db

logger = logging.getLogger(__name__)
from app.models import Session as SessionModel, SessionTask, TaskStatus, LogEntry
from app.schemas import (
    SessionCreate,
    SessionUpdate,
    SessionResponse,
    TaskExecuteRequest,
)
from app.services import (
    PromptTemplates,
    check_session_overwrites_payload as _check_session_overwrites_payload,
    cleanup_orphaned_checkpoints_payload as _cleanup_orphaned_checkpoints_payload,
    cleanup_session_checkpoints_payload as _cleanup_session_checkpoints_payload,
    create_session_backup_payload as _create_session_backup_payload,
    delete_session_checkpoint_payload as _delete_session_checkpoint_payload,
    ensure_unique_session_name as _ensure_unique_session_name,
    get_session_logs_payload as _get_session_logs_payload,
    get_session_workspace_info_payload as _get_session_workspace_info_payload,
    get_session_statistics_payload as _get_session_statistics_payload,
    get_sorted_logs_payload as _get_sorted_logs_payload,
    get_tool_execution_history_payload as _get_tool_execution_history_payload,
    inspect_session_checkpoint_payload as _inspect_session_checkpoint_payload,
    list_session_checkpoints_payload as _list_session_checkpoints_payload,
    load_session_checkpoint_payload as _load_session_checkpoint_payload,
    maybe_queue_next_automatic_task as _maybe_queue_next_automatic_task,
    queue_task_for_session as _queue_task_for_session,
    replay_session_checkpoint_payload as _replay_session_checkpoint_payload,
    save_session_checkpoint_payload as _save_session_checkpoint_payload,
    pause_session_lifecycle as _pause_session_lifecycle,
    resume_session_lifecycle as _resume_session_lifecycle,
    set_session_alert as _set_session_alert,
    start_session_payload as _start_session_payload,
    start_session_lifecycle as _start_session_lifecycle,
    stop_session_lifecycle as _stop_session_lifecycle,
    stream_session_logs as _stream_session_logs,
    stream_session_status as _stream_session_status,
    track_tool_execution_payload as _track_tool_execution_payload,
    execute_task_payload as _execute_task_payload,
)
from app.services.name_formatter import humanize_display_name
from app.dependencies import get_current_active_user, get_current_user

router = APIRouter()

DEFAULT_ORCHESTRATION_TIMEOUT_SECONDS = 1800


@router.post(
    "/sessions", response_model=SessionResponse, status_code=status.HTTP_201_CREATED
)
def create_session(
    session: SessionCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Create a new OpenClaw orchestration session"""
    # Verify project exists
    from app.models import Project

    project = db.query(Project).filter(Project.id == session.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    session_data = session.model_dump()
    session_data["name"] = _ensure_unique_session_name(
        db,
        session.project_id,
        humanize_display_name(session_data.get("name") or "session"),
    )
    db_session = SessionModel(**session_data)
    db_session.status = "pending"
    db_session.is_active = False
    db_session.instance_id = str(
        uuid.uuid4()
    )  # Generate unique instance ID immediately
    db.add(db_session)
    db.flush()

    # Log session creation (single commit with session)
    db.add(
        LogEntry(
            session_id=db_session.id,
            level="INFO",
            message=f"Session created: {db_session.name}",
            log_metadata=json.dumps({"project_id": session.project_id}),
        )
    )
    db.commit()
    db.refresh(db_session)

    return db_session


@router.get("/sessions", response_model=List[SessionResponse])
def list_sessions(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    skip: int = 0,
    limit: int = 100,
    status: Optional[str] = None,
    is_active: Optional[bool] = None,
    project_id: Optional[int] = None,
):
    """List sessions across projects for authenticated dashboard use."""
    query = db.query(SessionModel).filter(SessionModel.deleted_at.is_(None))

    if project_id is not None:
        query = query.filter(SessionModel.project_id == project_id)
    if is_active is not None:
        query = query.filter(SessionModel.is_active == is_active)
    if status:
        query = query.filter(SessionModel.status == status)

    return (
        query.order_by(SessionModel.created_at.desc()).offset(skip).limit(limit).all()
    )


@router.get("/projects/{project_id}/sessions", response_model=List[SessionResponse])
def get_project_sessions(
    project_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    skip: int = 0,
    limit: int = 100,
    is_active: Optional[bool] = None,
):
    """Get all sessions for a project with filtering"""
    from app.models import Project

    project = (
        db.query(Project)
        .filter(Project.id == project_id, Project.deleted_at.is_(None))
        .first()
    )
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    query = db.query(SessionModel).filter(
        SessionModel.project_id == project_id,
        SessionModel.deleted_at.is_(None),
    )
    if is_active is not None:
        query = query.filter(SessionModel.is_active == is_active)

    sessions = query.offset(skip).limit(limit).all()
    return sessions


@router.get("/sessions/{session_id}", response_model=SessionResponse)
def get_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Get a specific session with detailed information"""
    session = (
        db.query(SessionModel)
        .filter(SessionModel.id == session_id, SessionModel.deleted_at.is_(None))
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Get associated logs
    log_count = db.query(LogEntry).filter(LogEntry.session_id == session_id).count()

    # Get associated tasks
    from app.models import SessionTask as SessionTaskModel

    session_tasks = (
        db.query(SessionTaskModel)
        .filter(SessionTaskModel.session_id == session_id)
        .all()
    )

    # Add metadata
    response = session
    response.log_count = log_count
    response.task_count = len(session_tasks)

    return response


@router.patch("/sessions/{session_id}", response_model=SessionResponse)
def update_session(
    session_id: int,
    session_update: SessionUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Update a session"""
    db_session = (
        db.query(SessionModel)
        .filter(SessionModel.id == session_id, SessionModel.deleted_at.is_(None))
        .first()
    )
    if not db_session:
        raise HTTPException(status_code=404, detail="Session not found")

    update_data = session_update.model_dump(exclude_unset=True)
    if "execution_mode" in update_data and update_data["execution_mode"] not in {
        "automatic",
        "manual",
    }:
        raise HTTPException(
            status_code=400, detail="execution_mode must be 'automatic' or 'manual'"
        )
    for field, value in update_data.items():
        setattr(db_session, field, value)

    db.commit()
    db.refresh(db_session)

    # Log update
    db.add(
        LogEntry(
            session_id=session_id,
            level="INFO",
            message=f"Session updated: {session_id}",
            log_metadata=json.dumps({"updates": update_data}),
        )
    )
    db.commit()

    return db_session


@router.post("/sessions/{session_id}/refresh-tasks")
def refresh_session_tasks(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Refresh a session against the latest project task list and queue the next task if applicable."""
    from app.services.task_service import TaskService

    session = (
        db.query(SessionModel)
        .filter(SessionModel.id == session_id, SessionModel.deleted_at.is_(None))
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if not session.project_id:
        raise HTTPException(
            status_code=400, detail="Session is not linked to a project"
        )

    task_service = TaskService(db)
    ordered_tasks = task_service.get_project_tasks(session.project_id)
    counts = {
        "total": len(ordered_tasks),
        "pending": len(
            [task for task in ordered_tasks if task.status == TaskStatus.PENDING]
        ),
        "running": len(
            [task for task in ordered_tasks if task.status == TaskStatus.RUNNING]
        ),
        "done": len([task for task in ordered_tasks if task.status == TaskStatus.DONE]),
        "failed": len(
            [task for task in ordered_tasks if task.status == TaskStatus.FAILED]
        ),
    }

    queued_task = None
    if session.status == "running" and session.execution_mode == "automatic":
        queued_task = _maybe_queue_next_automatic_task(db, session)

    db.add(
        LogEntry(
            session_id=session.id,
            session_instance_id=session.instance_id,
            level="INFO",
            message="Session tasks refreshed from project state",
            log_metadata=json.dumps(
                {
                    "execution_mode": session.execution_mode,
                    "counts": counts,
                    "queued_task": queued_task,
                }
            ),
        )
    )
    db.commit()

    return {
        "session_id": session.id,
        "execution_mode": session.execution_mode,
        "counts": counts,
        "queued_task": queued_task,
    }


@router.post("/sessions/{session_id}/tasks/{task_id}/run")
def run_session_task(
    session_id: int,
    task_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Queue a specific task for manual execution inside a session."""
    session = (
        db.query(SessionModel)
        .filter(SessionModel.id == session_id, SessionModel.deleted_at.is_(None))
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    if not session.instance_id:
        session.instance_id = str(uuid.uuid4())
        db.commit()
        db.refresh(session)

    queued = _queue_task_for_session(db=db, session=session, task_id=task_id)
    return {
        "status": "queued",
        "session_id": session.id,
        "execution_mode": session.execution_mode,
        "queued_task": queued,
    }


@router.delete(
    "/sessions/{session_id}",
    response_model=None,
    status_code=status.HTTP_204_NO_CONTENT,
)
def delete_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Delete a session"""
    import json
    from app.models import Session as SessionModel, LogEntry, TaskCheckpoint
    from app.services.workspace.checkpoint_service import CheckpointService

    logger.info(f"DELETE /sessions/{session_id} - Starting deletion")

    db_session = (
        db.query(SessionModel)
        .filter(SessionModel.id == session_id, SessionModel.deleted_at.is_(None))
        .first()
    )
    if not db_session:
        logger.warning(f"DELETE /sessions/{session_id} - Session not found")
        raise HTTPException(status_code=404, detail="Session not found")

    deleted_at = datetime.now(timezone.utc)
    db_session.deleted_at = deleted_at
    db_session.is_active = False
    db_session.status = "deleted"
    if "__deleted__" not in db_session.name:
        db_session.name = f"{db_session.name}__deleted__{db_session.id}"

    checkpoint_service = CheckpointService(db)
    deleted_checkpoints = checkpoint_service.delete_all_checkpoints(session_id)
    orphan_cleanup = checkpoint_service.cleanup_orphaned_checkpoints()

    deleted_session_tasks = (
        db.query(SessionTask).filter(SessionTask.session_id == session_id).delete()
    )
    deleted_task_checkpoints = (
        db.query(TaskCheckpoint)
        .filter(TaskCheckpoint.session_id == session_id)
        .delete(synchronize_session=False)
    )

    # Delete all logs for this session to prevent ID reuse issues
    deleted_logs = db.query(LogEntry).filter(LogEntry.session_id == session_id).delete()

    db.commit()
    logger.info(f"Deleted {deleted_logs} logs for session {session_id}")
    logger.info(
        "Deleted session %s artifacts: checkpoints=%s session_tasks=%s task_checkpoints=%s orphan_cleanup=%s",
        session_id,
        deleted_checkpoints,
        deleted_session_tasks,
        deleted_task_checkpoints,
        orphan_cleanup,
    )

    # Optional: Actually delete the session row if you want hard delete behavior
    # db.delete(db_session)
    # db.commit()

    logger.info(f"DELETE /sessions/{session_id} - Session deleted successfully")
    return None


class StartOpenClawRequest(BaseModel):
    task_description: str


@router.post("/sessions/{session_id}/start")
async def start_session(
    session_id: int,
    request: StartOpenClawRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Start a backend-neutral orchestration session for a task."""

    return await _start_session_payload(
        db, session_id, task_description=request.task_description
    )


@router.post("/sessions/{session_id}/start-openclaw")
async def start_openclaw_session(
    session_id: int,
    request: StartOpenClawRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Start an OpenClaw session for a given task

    Creates an OpenClaw session and begins task execution
    """
    return await _start_session_payload(
        db, session_id, task_description=request.task_description
    )


@router.post("/sessions/{session_id}/execute")
async def execute_task(
    session_id: int,
    task_request: TaskExecuteRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Execute a task via OpenClaw with multi-step orchestration

    Args:
        session_id: Session ID
        task_request: Task execution request with task description and timeout
    """
    return await _execute_task_payload(db, session_id, task_request)


@router.websocket("/sessions/{session_id}/logs/stream")
async def websocket_log_stream(
    websocket: WebSocket, session_id: int, db: Session = Depends(get_db)
):
    """
    WebSocket endpoint for real-time log streaming with heartbeat mechanism

    Clients can connect to receive live logs from an OpenClaw session.
    Implements heartbeat (ping/pong) every 30 seconds to prevent timeout disconnects.

    TIMEOUT FIX: Prevents 5-minute disconnect by maintaining connection activity
    """
    await _stream_session_logs(websocket, session_id, db)


@router.websocket("/sessions/{session_id}/status")
async def websocket_session_status(
    websocket: WebSocket, session_id: int, db: Session = Depends(get_db)
):
    await _stream_session_status(websocket, session_id, db)


@router.get("/sessions/{session_id}/tools")
def get_tool_execution_history(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    task_id: Optional[int] = None,
    limit: int = 50,
    tool_name: Optional[str] = None,
):
    """Get tool execution history for a session"""
    return _get_tool_execution_history_payload(
        db, session_id, task_id=task_id, limit=limit, tool_name=tool_name
    )


@router.get("/sessions/{session_id}/statistics")
def get_session_statistics(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    days: int = 7,
):
    """Get statistics for a session"""
    return _get_session_statistics_payload(db, session_id, days=days)


@router.post("/sessions/{session_id}/tools/track")
def track_tool_execution(
    session_id: int,
    execution_id: str,
    tool_name: str,
    params: dict,
    result: Any,
    success: bool,
    db: Session = Depends(get_db),
    task_id: Optional[int] = None,
    session_instance_id: Optional[str] = None,  # NEW: For log isolation
):
    """Manually track a tool execution with instance tracking

    Args:
        session_id: Session ID
        execution_id: Unique execution identifier
        tool_name: Name of the tool
        params: Tool parameters
        result: Tool execution result
        success: Whether execution was successful
        db: Database session
        task_id: Optional task ID
        session_instance_id: Instance UUID for log isolation (NEW)

    Returns:
        Tracked execution result
    """
    return _track_tool_execution_payload(
        db,
        session_id=session_id,
        execution_id=execution_id,
        tool_name=tool_name,
        params=params,
        result=result,
        success=success,
        task_id=task_id,
        session_instance_id=session_instance_id,
    )


@router.post("/sessions/{session_id}/start")
async def start_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Start an OpenClaw session for a given session

    Creates an OpenClaw session and begins task execution
    """
    return await _start_session_lifecycle(db, session_id)


@router.post("/sessions/{session_id}/stop")
async def stop_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    force: bool = False,
):
    """
    Stop an OpenClaw session gracefully

    Args:
        session_id: Session ID
        force: If True, force stop without cleanup
    """
    return await _stop_session_lifecycle(db, session_id, force=force)


@router.post("/sessions/{session_id}/pause")
async def pause_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Pause a running session

    Saves current state and pauses execution
    """
    return await _pause_session_lifecycle(db, session_id)


@router.post("/sessions/{session_id}/resume")
async def resume_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Resume a paused session

    Restores saved state and continues execution
    """
    return await _resume_session_lifecycle(db, session_id)


@router.get("/sessions/{session_id}/prompts/{template_name}")
def get_prompt_template(
    session_id: int,
    template_name: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Get a prompt template for the session"""
    template = PromptTemplates.get_template(template_name)
    if not template:
        raise HTTPException(
            status_code=404, detail=f"Template '{template_name}' not found"
        )

    return {
        "template_name": template_name,
        "template": template,
        "session_id": session_id,
    }


@router.get("/sessions/{session_id}/logs")
def get_session_logs(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    limit: Optional[int] = 100,
    offset: int = 0,
):
    """
    Get logs for a session (simple endpoint for stopped sessions)

    This endpoint fetches all logs for a session, optionally filtered by instance_id.
    It's designed to work for stopped sessions where WebSocket streaming isn't available.

    Args:
        session_id: Session ID
        limit: Maximum number of logs to return (default: 100)
        offset: Offset for pagination (default: 0)

    Returns:
        List of log entries
    """
    return _get_session_logs_payload(db, session_id, limit=limit, offset=offset)


@router.get("/sessions/{session_id}/logs/sorted")
def get_sorted_logs(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
    order: str = "asc",  # "asc" for oldest first, "desc" for newest first
    deduplicate: bool = True,  # Remove duplicate entries
    level: Optional[str] = None,  # Optional filter by log level
    limit: Optional[int] = None,  # Optional limit on number of logs
    offset: int = 0,  # NEW: For pagination
):
    """
    Get sorted and optionally deduplicated logs for a session

    OPTIMIZED: Uses database-level sorting and pagination to avoid timeout issues

    Args:
        session_id: Session ID
        order: Sort order - "asc" (oldest first) or "desc" (newest first)
        deduplicate: Remove duplicate log entries (note: expensive for large datasets)
        level: Optional log level filter (INFO, WARNING, ERROR)
        limit: Optional limit on number of logs to return (default: 100)
        offset: Offset for pagination (default: 0)

    Returns:
        Sorted list of log entries
    """
    return _get_sorted_logs_payload(
        db,
        session_id,
        order=order,
        deduplicate=deduplicate,
        level=level,
        limit=limit,
        offset=offset,
    )


@router.post("/generate-steps")
async def generate_steps_from_description(
    request: Request,
    body: dict,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Generate task steps using OpenClaw AI"""
    task_name = body.get("task_name", "Task")
    description = body.get("description", "")

    # Create prompt for step generation
    prompt_template = PromptTemplates.get_template("generate_steps")
    if not prompt_template:
        prompt_template = """You are a task planning assistant. Given a task name and description, break it down into clear, actionable steps. Return the steps as a JSON array.

Task Name: {task_name}
Description: {description}

Return ONLY a JSON array of step objects with 'title' and 'description' fields. Example:
[
  {{ "title": "Step 1", "description": "First thing to do" }},
  {{ "title": "Step 2", "description": "Second thing to do" }}
]"""

    formatted_prompt = prompt_template.format(
        task_name=task_name, description=description
    )

    # Use a simple heuristic-based step generator as fallback
    # This creates basic steps based on common patterns
    import re

    # Common patterns for different task types
    patterns = {
        "authentication|login|register": [
            {
                "title": "Create Authentication Routes",
                "description": "Set up /login and /register endpoints",
            },
            {
                "title": "Implement Password Hashing",
                "description": "Add bcrypt or similar for secure password storage",
            },
            {
                "title": "Create JWT Token Generation",
                "description": "Generate and validate JWT tokens for authentication",
            },
            {
                "title": "Build Login/Register Forms",
                "description": "Create frontend forms with validation",
            },
            {
                "title": "Add Protected Routes",
                "description": "Implement route guards for authenticated users",
            },
        ],
        "database|sql|model": [
            {
                "title": "Design Database Schema",
                "description": "Create tables and relationships",
            },
            {
                "title": "Implement ORM Models",
                "description": "Define SQLAlchemy models",
            },
            {
                "title": "Create Migrations",
                "description": "Set up Alembic for database migrations",
            },
            {
                "title": "Add Database Connection",
                "description": "Configure database connection pooling",
            },
        ],
        "frontend|ui|react": [
            {
                "title": "Setup Component Structure",
                "description": "Organize React components folder",
            },
            {
                "title": "Create UI Components",
                "description": "Build reusable UI components",
            },
            {"title": "Add Styling", "description": "Implement CSS/Tailwind styling"},
            {
                "title": "Add State Management",
                "description": "Setup React Context or Redux",
            },
        ],
    }

    desc_lower = description.lower()
    detected_pattern = None
    for pattern, steps in patterns.items():
        if re.search(pattern, desc_lower):
            detected_pattern = pattern
            break

    if detected_pattern:
        return {"steps": patterns[detected_pattern], "task_name": task_name}

    # Default generic steps
    default_steps = [
        {
            "title": "Analyze Requirements",
            "description": "Understand the task scope and requirements",
        },
        {
            "title": "Plan Implementation",
            "description": "Create a step-by-step implementation plan",
        },
        {
            "title": "Write Code",
            "description": "Implement the feature following best practices",
        },
        {
            "title": "Test Implementation",
            "description": "Write and run tests to verify functionality",
        },
        {"title": "Document Changes", "description": "Update documentation and README"},
    ]

    return {"steps": default_steps, "task_name": task_name}


class OverwriteCheckRequest(BaseModel):
    """Request model for overwrite check"""

    project_id: int
    task_subfolder: str
    planned_files: Optional[List[str]] = []


@router.post("/sessions/{session_id}/check-overwrites")
async def check_session_overwrites(
    session_id: int,
    request: OverwriteCheckRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Check for potential overwrites before executing a task in this session

    Args:
        session_id: Session ID to check
        request: Overwrite check request with project info and planned files
        db: Database session

    Returns:
        Overwrite protection result with safety status and warnings
    """
    return _check_session_overwrites_payload(
        db,
        session_id,
        project_id=request.project_id,
        task_subfolder=request.task_subfolder,
        planned_files=request.planned_files,
    )


@router.post("/sessions/{session_id}/create-backup")
async def create_session_backup(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Create a backup of existing workspace before proceeding

    Args:
        session_id: Session ID to backup
        db: Database session

    Returns:
        Backup result with path and file count
    """
    try:
        return _create_session_backup_payload(db, session_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backup failed: {str(e)}")


@router.get("/sessions/{session_id}/workspace-info")
async def get_session_workspace_info(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Get workspace information for a session

    Args:
        session_id: Session ID to check
        db: Database session

    Returns:
        Workspace details including file count and last modified date
    """
    try:
        return _get_session_workspace_info_payload(db, session_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Workspace info failed: {str(e)}")


@router.post("/sessions/{session_id}/checkpoint/save")
async def save_session_checkpoint(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Save a manual checkpoint for a paused session

    Args:
        session_id: Session ID to checkpoint
        db: Database session

    Returns:
        Checkpoint metadata including path and timestamp
    """
    return await _save_session_checkpoint_payload(db, session_id)


@router.get("/sessions/{session_id}/checkpoints")
async def list_session_checkpoints(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    List all checkpoints for a session

    Args:
        session_id: Session ID to check
        db: Database session

    Returns:
        List of checkpoint metadata (oldest first)
    """
    try:
        return _list_session_checkpoints_payload(db, session_id)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to list checkpoints: {str(e)}"
        )


@router.get("/sessions/{session_id}/checkpoints/{checkpoint_name}")
async def inspect_session_checkpoint(
    session_id: int,
    checkpoint_name: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Return operator-friendly metadata for one checkpoint."""
    try:
        return _inspect_session_checkpoint_payload(db, session_id, checkpoint_name)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to inspect checkpoint: {str(e)}"
        )


@router.post("/sessions/{session_id}/checkpoint/load")
async def load_session_checkpoint(
    session_id: int,
    checkpoint_name: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Load a specific checkpoint for resuming execution

    Args:
        session_id: Session ID to resume
        checkpoint_name: Name of the checkpoint to load
        db: Database session

    Returns:
        Resume result with new session key
    """
    return await _load_session_checkpoint_payload(db, session_id, checkpoint_name)


@router.post("/sessions/{session_id}/checkpoints/{checkpoint_name}/replay")
async def replay_session_checkpoint(
    session_id: int,
    checkpoint_name: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """Resume execution from a selected checkpoint for operator replay."""
    return await _replay_session_checkpoint_payload(db, session_id, checkpoint_name)


@router.delete("/sessions/{session_id}/checkpoints/{checkpoint_name}")
async def delete_session_checkpoint(
    session_id: int,
    checkpoint_name: str,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Delete a specific checkpoint

    Args:
        session_id: Session ID
        checkpoint_name: Name of the checkpoint to delete
        db: Database session

    Returns:
        Deletion result
    """
    try:
        return _delete_session_checkpoint_payload(db, session_id, checkpoint_name)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to delete checkpoint: {str(e)}"
        )


@router.post("/sessions/{session_id}/checkpoint/cleanup")
async def cleanup_session_checkpoints(
    session_id: int,
    keep_latest: int = 3,
    max_age_hours: int = 24,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_user),
):
    """
    Clean up old checkpoints, keeping only the latest N

    Args:
        session_id: Session ID to cleanup
        keep_latest: Number of most recent checkpoints to keep
        max_age_hours: Delete checkpoints older than this (hours)
        db: Database session

    Returns:
        Cleanup statistics
    """
    try:
        return _cleanup_session_checkpoints_payload(
            db, session_id, keep_latest=keep_latest, max_age_hours=max_age_hours
        )
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to cleanup checkpoints: {str(e)}"
        )


@router.post("/checkpoints/orphaned/cleanup")
async def cleanup_orphaned_checkpoints(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """
    Clean checkpoint artifacts for sessions that are missing or soft-deleted.
    """
    try:
        return _cleanup_orphaned_checkpoints_payload(db)
    except HTTPException:
        raise
    except Exception as e:
        raise HTTPException(
            status_code=500,
            detail=f"Failed to cleanup orphaned checkpoints: {str(e)}",
        )
