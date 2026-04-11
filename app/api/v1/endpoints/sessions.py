"""Sessions API endpoints with OpenClaw integration"""

from fastapi import (
    APIRouter,
    Depends,
    HTTPException,
    status,
    WebSocket,
    WebSocketDisconnect,
)
from fastapi.requests import Request
from sqlalchemy.orm import Session
from typing import List, Optional, Any, Dict
from datetime import datetime, timezone
import json
import asyncio
import logging
import uuid
import re
from pathlib import Path

from app.database import get_db, get_db_session as create_db_session

logger = logging.getLogger(__name__)
from app.models import Session as SessionModel, SessionTask, TaskStatus, LogEntry
from app.schemas import (
    SessionCreate,
    SessionUpdate,
    SessionResponse,
    TaskExecuteRequest,
)
from app.services import (
    OpenClawSessionService,
    LogStreamService,
    ToolTrackingService,
    PromptTemplates,
)
from app.services.openclaw_service import OpenClawSessionError
from app.services.prompt_templates import OrchestrationState, OPENCLAW_WORKSPACE_ROOT
from app.services.project_isolation_service import resolve_project_workspace_path
from app.services.log_utils import sort_logs, deduplicate_logs, format_logs_batch
from app.services.log_stream_service import stream_logs, get_project_logs_summary
from app.dependencies import get_current_active_user, get_current_user
from app.auth import verify_token

router = APIRouter()


# In-memory WebSocket connections for log streaming
active_websockets: dict = {}


def _slugify_task_name(name: str) -> str:
    """Convert task titles into stable folder names."""
    if not name:
        return "task"

    slug = name.lower()
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "task"


def _build_task_subfolder_name(title: str, task_id: int) -> str:
    slug = _slugify_task_name(title)
    return f"task-{slug}" if slug else f"task-{task_id}"


def _ensure_task_workspace(
    db: Session, session: SessionModel, task_id: int
) -> Dict[str, str]:
    """Ensure a selected task has a subfolder and workspace on disk."""
    from app.models import Project, Task

    # OrchestrationState and OPENCLAW_WORKSPACE_ROOT already imported at top of file

    task = (
        db.query(Task)
        .filter(Task.id == task_id, Task.project_id == session.project_id)
        .first()
    )
    if not task:
        raise HTTPException(status_code=404, detail="Task not found for this session")

    project = db.query(Project).filter(Project.id == session.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    orchestration_state = OrchestrationState(
        session_id=str(session.id),
        task_description=task.description or task.title,
        project_name=project.name or "",
        task_id=task.id,
    )

    if project.workspace_path:
        workspace_path = str(
            resolve_project_workspace_path(project.workspace_path, project.name)
        )
        orchestration_state._workspace_path_override = workspace_path

    if task.task_subfolder:
        orchestration_state._task_subfolder_override = task.task_subfolder
    else:
        base_subfolder = _build_task_subfolder_name(task.title, task.id)
        candidate = base_subfolder
        suffix = 2

        while True:
            existing = (
                db.query(Task)
                .filter(
                    Task.project_id == session.project_id,
                    Task.task_subfolder == candidate,
                    Task.id != task.id,
                )
                .first()
            )
            if not existing:
                break
            candidate = f"{base_subfolder}-{suffix}"
            suffix += 1

        task.task_subfolder = candidate
        orchestration_state._task_subfolder_override = candidate
        db.flush()

    workspace_path = Path(orchestration_state.project_dir)
    workspace_path.mkdir(parents=True, exist_ok=True)

    return {
        "task_subfolder": task.task_subfolder,
        "workspace_path": str(workspace_path),
    }


def _get_session_celery_task_ids(db: Session, session_id: int) -> List[str]:
    """Collect queued/running Celery task ids recorded for a session."""
    task_ids: List[str] = []
    log_entries = (
        db.query(LogEntry)
        .filter(LogEntry.session_id == session_id)
        .order_by(LogEntry.created_at.desc())
        .all()
    )
    for log in log_entries:
        if not log.log_metadata:
            continue
        try:
            metadata = json.loads(log.log_metadata)
        except Exception:
            continue
        celery_task_id = metadata.get("celery_task_id")
        if celery_task_id and celery_task_id not in task_ids:
            task_ids.append(celery_task_id)
    return task_ids


def _get_session_task_subfolder(db: Session, session: SessionModel) -> str:
    """Resolve the active task subfolder for a session."""
    from app.models import Task

    session_task = (
        db.query(SessionTask)
        .filter(SessionTask.session_id == session.id)
        .order_by(SessionTask.started_at.desc().nullslast(), SessionTask.id.desc())
        .first()
    )

    if session_task:
        task = db.query(Task).filter(Task.id == session_task.task_id).first()
        if task:
            workspace = _ensure_task_workspace(db, session, task.id)
            return workspace["task_subfolder"]

    return f"task_{session.id}"


def _revoke_session_celery_tasks(
    db: Session, session_id: int, terminate: bool = True
) -> List[str]:
    """Revoke all known Celery tasks for a session."""
    from app.celery_app import celery_app

    revoked_ids: List[str] = []
    for celery_task_id in _get_session_celery_task_ids(db, session_id):
        celery_app.control.revoke(
            celery_task_id,
            terminate=terminate,
            signal="SIGTERM",
        )
        revoked_ids.append(celery_task_id)
    return revoked_ids


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

    db_session = SessionModel(**session.model_dump())
    db_session.is_active = True  # Session is active when created
    db_session.instance_id = str(uuid.uuid4())  # Generate unique instance ID immediately
    db.add(db_session)

    # Commit session creation
    db.commit()
    db.refresh(db_session)

    # Log session creation (single commit with session)
    db.add(
        LogEntry(
            session_id=db_session.id,
            level="INFO",
            message=f"Session created: {db_session.name}",
            log_metadata=json.dumps({"project_id": session.project_id}),
        )
    )
    # Don't commit here - let the next operation handle it
    # This prevents double-commit delays

    return db_session


@router.get("/projects/{project_id}/sessions", response_model=List[SessionResponse])
def get_project_sessions(
    project_id: int,
    db: Session = Depends(get_db),
    skip: int = 0,
    limit: int = 100,
    is_active: Optional[bool] = None,
):
    """Get all sessions for a project with filtering"""
    from app.models import Project

    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    query = db.query(SessionModel).filter(SessionModel.project_id == project_id)
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
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
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
    db_session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not db_session:
        raise HTTPException(status_code=404, detail="Session not found")

    update_data = session_update.model_dump(exclude_unset=True)
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
    from app.models import Session as SessionModel, LogEntry

    logger.info(f"DELETE /sessions/{session_id} - Starting deletion")

    db_session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not db_session:
        logger.warning(f"DELETE /sessions/{session_id} - Session not found")
        raise HTTPException(status_code=404, detail="Session not found")

    # Log deletion before actually deleting
    db.add(
        LogEntry(
            session_id=session_id,
            level="INFO",
            message=f"Session deletion requested: {db_session.name}",
            log_metadata=json.dumps({"requested_by": current_user.email}),
        )
    )
    db.commit()

    # Soft delete: mark as deleted and delete all logs
    # This prevents database ID reuse issues that cause stale logs
    db_session.deleted_at = datetime.now(timezone.utc)
    db_session.is_active = False
    db_session.status = "deleted"
    
    # Delete all logs for this session to prevent ID reuse issues
    from app.models import LogEntry
    deleted_logs = db.query(LogEntry).filter(
        LogEntry.session_id == session_id
    ).delete()
    
    db.commit()
    logger.info(f"Deleted {deleted_logs} logs for session {session_id}")

    # Optional: Actually delete the session row if you want hard delete behavior
    # db.delete(db_session)
    # db.commit()

    logger.info(f"DELETE /sessions/{session_id} - Session deleted successfully")
    return None


from pydantic import BaseModel


class StartOpenClawRequest(BaseModel):
    task_description: str


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
    # Verify session exists
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    task_description = request.task_description

    try:
        # Initialize OpenClaw service
        openclaw_service = OpenClawSessionService(db, session_id, use_demo_mode=False)

        # Create OpenClaw session
        session_key = await openclaw_service.create_openclaw_session(task_description)

        # Log the start
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

    except Exception as e:
        db.add(
            LogEntry(
                session_id=session_id,
                level="ERROR",
                message=f"Failed to start OpenClaw session: {str(e)}",
            )
        )
        db.commit()
        raise HTTPException(status_code=500, detail=str(e))


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
    prompt = task_request.task
    timeout_seconds = task_request.timeout_seconds

    if not prompt:
        raise HTTPException(status_code=422, detail="Task prompt is required")

    # Verify session exists
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    selected_task = None
    task_workspace = None

    try:

        if task_request.task_id:
            from app.models import Task, SessionTask

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

            task_workspace = _ensure_task_workspace(db, session, selected_task.id)

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

        # Always use real execution mode (no demo mode)
        openclaw_service = OpenClawSessionService(
            db,
            session_id,
            task_id=selected_task.id if selected_task else None,
            use_demo_mode=False,
        )

        # Create OpenClaw session (generates session key)
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

        # Execute task with multi-step orchestration (PLANNING -> EXECUTING -> DEBUGGING)
        # Pass raw task description - orchestration handles prompt building internally
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

    except Exception as e:
        import traceback

        if selected_task:
            selected_task.status = TaskStatus.FAILED
            selected_task.error_message = str(e)
            selected_task.completed_at = datetime.utcnow()

        session.is_active = False
        session.status = "stopped"
        session.stopped_at = datetime.now(timezone.utc)

        traceback_text = traceback.format_exc()
        logger.error(
            "Task execution failed for session %s: %s\n%s",
            session_id,
            str(e),
            traceback_text,
        )
        error_detail = str(e)
        db.add(
            LogEntry(
                session_id=session_id,
                task_id=selected_task.id if selected_task else None,
                level="ERROR",
                message=(
                    f"Task execution failed: {error_detail}"
                    if isinstance(e, OpenClawSessionError)
                    else f"Task execution failed: {str(e)}"
                ),
                log_metadata=json.dumps({"traceback": traceback_text}),
            )
        )
        db.commit()
        raise HTTPException(
            status_code=500,
            detail=(
                error_detail
                if isinstance(e, OpenClawSessionError)
                else "Task execution failed. Check session logs for details."
            ),
        )


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
    # Verify session exists
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        logger.warning(f"WebSocket connection rejected: session {session_id} not found")
        await websocket.close(code=1008, reason="Session not found")
        return

    # Accept connection
    await websocket.accept()
    logger.info(
        f"WebSocket connected for session {session_id}, instance: {session.instance_id}"
    )

    # Register WebSocket with heartbeat tracking
    if session_id not in active_websockets:
        active_websockets[session_id] = []
    active_websockets[session_id].append(
        {"websocket": websocket, "last_activity": datetime.utcnow()}
    )

    # Send initial connection confirmation with heartbeat interval info
    await websocket.send_json(
        {
            "type": "connected",
            "session_id": session_id,
            "session_instance_id": session.instance_id,
            "timestamp": datetime.utcnow().isoformat(),
            "heartbeat_interval": 30,  # Ping every 30 seconds to prevent timeout
        }
    )

    # Send recent logs filtered by instance_id
    log_service = LogStreamService(db)
    recent_logs = log_service.get_recent_logs(
        session_id, instance_id=session.instance_id, limit=20
    )
    logger.info(
        f"Sending {len(recent_logs)} recent logs to WebSocket (filtered by instance: {session.instance_id})"
    )
    if not recent_logs and session.instance_id:
        logger.warning(
            f"No logs found for session {session_id} with instance_id {session.instance_id}"
        )
        # Try to get any logs for this session (without instance filter)
        fallback_logs = log_service.get_recent_logs(session_id, instance_id=None, limit=20)
        logger.info(f"Fallback: Found {len(fallback_logs)} logs without instance filter")
        recent_logs = fallback_logs

    for log in recent_logs:
        await websocket.send_json({"type": "log", **log})
    
    logger.info(f"Sent {len(recent_logs)} initial logs, starting main loop...")

    # Background task to send periodic heartbeats (prevents 5-minute timeout)
    async def heartbeat_sender():
        """Send ping every 30 seconds to keep WebSocket connection alive"""
        try:
            while True:
                await asyncio.sleep(30)  # Ping every 30 seconds
                await websocket.send_text("ping")
                logger.debug(f"Sent heartbeat ping to session {session_id}")
        except Exception as e:
            logger.error(f"Heartbeat sender error for session {session_id}: {str(e)}")

    last_log_id = recent_logs[-1]["id"] if recent_logs else 0

    try:
        # Start heartbeat task
        heartbeat_task = asyncio.create_task(heartbeat_sender())

        # Keep connection alive and handle client messages
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
            except asyncio.TimeoutError:
                data = None

            poll_db = create_db_session()
            try:
                current_session = (
                    poll_db.query(SessionModel)
                    .filter(SessionModel.id == session_id)
                    .first()
                )
                if current_session:
                    query = poll_db.query(LogEntry).filter(
                        LogEntry.session_id == session_id,
                        LogEntry.id > last_log_id,
                    )
                    if current_session.instance_id:
                        query = query.filter(
                            LogEntry.session_instance_id == current_session.instance_id
                        )
                    else:
                        query = query.filter(LogEntry.session_instance_id.is_(None))

                    new_logs = (
                        query.order_by(LogEntry.created_at.asc()).limit(100).all()
                    )
                    for log in new_logs:
                        last_log_id = max(last_log_id, log.id)
                        await websocket.send_json(
                            {
                                "type": "log",
                                "id": log.id,
                                "session_id": log.session_id,
                                "task_id": log.task_id,
                                "message": log.message,
                                "level": log.level,
                                "timestamp": (
                                    log.created_at.isoformat()
                                    if log.created_at
                                    else None
                                ),
                                "metadata": (
                                    json.loads(log.log_metadata)
                                    if log.log_metadata
                                    else {}
                                ),
                                "session_instance_id": log.session_instance_id,
                            }
                        )
            finally:
                poll_db.close()

            if data is None:
                continue

            # Update last activity timestamp
            for ws_info in active_websockets.get(session_id, []):
                if ws_info.get("websocket") == websocket:
                    ws_info["last_activity"] = datetime.utcnow()
                    break

            # Handle heartbeat responses and client messages
            if data == "ping":
                await websocket.send_text("pong")
                logger.debug(f"Received ping from session {session_id}, sent pong")
            elif data.lower() == "pong":
                # Client responded to our ping, connection is alive
                logger.debug(f"Client pong received for session {session_id}")
            else:
                logger.debug(f"Received message from WebSocket: {data[:100]}...")

    except WebSocketDisconnect:
        logger.info(f"WebSocket disconnected gracefully for session {session_id}")
        heartbeat_task.cancel()
        try:
            active_websockets[session_id] = [
                w
                for w in active_websockets.get(session_id, [])
                if w.get("websocket") != websocket
            ]
            if not active_websockets[session_id]:
                del active_websockets[session_id]
        except Exception:
            pass
    except Exception as e:
        logger.error(f"WebSocket error for session {session_id}: {str(e)}")
        heartbeat_task.cancel()
        try:
            active_websockets[session_id] = [
                w
                for w in active_websockets.get(session_id, [])
                if w.get("websocket") != websocket
            ]
            if not active_websockets[session_id]:
                del active_websockets[session_id]
        except Exception:
            pass


@router.websocket("/sessions/{session_id}/status")
async def websocket_session_status(
    websocket: WebSocket, session_id: int, db: Session = Depends(get_db)
):
    # Validate session before accepting
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        await websocket.close(code=1008, reason="Session not found")
        return

    await websocket.accept()
    await websocket.send_json(
        {
            "type": "connected",
            "session_id": session_id,
            "timestamp": datetime.utcnow().isoformat(),
            "heartbeat_interval": 30,
            "status_interval": 2,
        }
    )

    async def status_sender():
        """Push live session status snapshots to the client."""
        last_snapshot: Optional[Dict[str, Any]] = None
        while True:
            await asyncio.sleep(2)
            poll_db = create_db_session()
            try:
                current = (
                    poll_db.query(SessionModel)
                    .filter(SessionModel.id == session_id)
                    .first()
                )
                if not current:
                    await websocket.send_json(
                        {
                            "type": "error",
                            "session_id": session_id,
                            "message": "Session not found",
                            "timestamp": datetime.utcnow().isoformat(),
                        }
                    )
                    break

                snapshot = {
                    "id": current.id,
                    "status": current.status,
                    "is_active": current.is_active,
                    "started_at": (
                        current.started_at.isoformat() if current.started_at else None
                    ),
                    "stopped_at": (
                        current.stopped_at.isoformat() if current.stopped_at else None
                    ),
                    "paused_at": (
                        current.paused_at.isoformat() if current.paused_at else None
                    ),
                    "resumed_at": (
                        current.resumed_at.isoformat() if current.resumed_at else None
                    ),
                    "updated_at": (
                        current.updated_at.isoformat() if current.updated_at else None
                    ),
                }

                if snapshot != last_snapshot:
                    await websocket.send_json(
                        {
                            "type": "status_update",
                            "session_id": session_id,
                            "status": snapshot,
                            "timestamp": datetime.utcnow().isoformat(),
                        }
                    )
                    last_snapshot = snapshot
            finally:
                poll_db.close()

    async def heartbeat_sender():
        """Keep idle connections alive through intermediary proxies."""
        while True:
            await asyncio.sleep(30)
            await websocket.send_text("ping")

    status_task = asyncio.create_task(status_sender())
    heartbeat_task = asyncio.create_task(heartbeat_sender())

    try:
        while True:
            try:
                data = await asyncio.wait_for(websocket.receive_text(), timeout=1.0)
            except asyncio.TimeoutError:
                continue

            if data == "ping":
                await websocket.send_text("pong")
            elif data.lower() == "pong":
                continue
            else:
                logger.debug(
                    "Status websocket received message for session %s: %s",
                    session_id,
                    data[:100],
                )
    except WebSocketDisconnect:
        logger.info("Status websocket disconnected for session %s", session_id)
    except Exception as e:
        logger.error("Status websocket error for session %s: %s", session_id, str(e))
    finally:
        status_task.cancel()
        heartbeat_task.cancel()


def get_session_logs(
    session_id: int,
    db: Session = Depends(get_db),
    level: Optional[str] = None,
    limit: int = 100,
    offset: int = 0,
):
    """Get log entries for a session (filtered by instance_id)"""
    # Verify session exists to get instance_id
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Filter logs by both session_id and instance_id to prevent ID reuse issues
    query = db.query(LogEntry).filter(
        LogEntry.session_id == session_id,
        LogEntry.session_instance_id == session.instance_id,
    )

    if level:
        query = query.filter(LogEntry.level == level.upper())

    logs = query.order_by(LogEntry.created_at.desc()).offset(offset).limit(limit).all()

    return {
        "total": query.count(),
        "session_instance_id": session.instance_id,
        "logs": [
            {
                "id": log.id,
                "level": log.level,
                "message": log.message,
                "timestamp": log.created_at.isoformat(),
                "metadata": json.loads(log.log_metadata) if log.log_metadata else {},
            }
            for log in logs
        ],
    }


@router.get("/sessions/{session_id}/tools")
def get_tool_execution_history(
    session_id: int,
    db: Session = Depends(get_db),
    task_id: Optional[int] = None,
    limit: int = 50,
    tool_name: Optional[str] = None,
):
    """Get tool execution history for a session"""
    tool_service = ToolTrackingService(db)

    executions = tool_service.get_execution_history(
        session_id=session_id, task_id=task_id, limit=limit, tool_name=tool_name
    )

    return {"total": len(executions), "executions": executions}


@router.get("/sessions/{session_id}/statistics")
def get_session_statistics(
    session_id: int, db: Session = Depends(get_db), days: int = 7
):
    """Get statistics for a session"""
    tool_service = ToolTrackingService(db)

    # Get log statistics
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

    # Get tool statistics
    tool_stats = tool_service.get_tool_statistics(session_id, days)

    return {
        "session_id": session_id,
        "period_days": days,
        "logs": {"total": total_logs, "info": info_logs, "errors": error_logs},
        "tools": tool_stats,
    }


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
    tool_service = ToolTrackingService(db)

    execution = tool_service.track(
        execution_id=execution_id,
        tool_name=tool_name,
        params=params,
        result=result,
        success=success,
        session_id=session_id,
        task_id=task_id,
        session_instance_id=session_instance_id,  # NEW: For isolation
    )

    return {"status": "tracked", "execution_id": execution_id, "tool": tool_name}


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
    from app.tasks.worker import execute_openclaw_task

    # Verify session exists
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Check if already running (includes 'active' as equivalent to 'running')
    if session.status in ["running", "paused", "active"]:
        raise HTTPException(
            status_code=400,
            detail=f"Session is already {session.status}. Use stop or resume instead.",
        )

    # Handle sessions stuck in "pending" or other non-running states
    if session.status == "pending" and session.is_active:
        logger.warning(
            f"Session {session_id} is stuck in pending state with is_active=True. Resetting..."
        )
        # Reset the session state to allow starting
        session.is_active = False
        session.status = "stopped"
        db.commit()

    # Handle sessions stuck in "active" status
    if session.status == "active":
        logger.warning(
            f"Session {session_id} has 'active' status. Treating as stopped and resetting..."
        )
        session.is_active = False
        session.status = "stopped"
        db.commit()

    try:
        # Every fresh start gets a new instance id so logs from prior attempts
        # do not mix into the latest run.
        session_instance_id = str(uuid.uuid4())
        session.instance_id = session_instance_id
        db.commit()

        # Initialize OpenClaw service
        openclaw_service = OpenClawSessionService(db, session_id, use_demo_mode=False)

        # Create OpenClaw session using session description or name
        task_description = session.description or session.name
        logger.info(
            f"Starting session {session_id} with description: {task_description[:50]}, instance: {session_instance_id}"
        )
        session_key = await openclaw_service.create_openclaw_session(task_description)

        # If session is linked to a project, queue all pending tasks
        print(f"DEBUG: session.project_id = {session.project_id}")
        if session.project_id:
            print(f"DEBUG: Found project_id {session.project_id}, queuing tasks...")
            from app.services.task_service import TaskService
            from app.models import Task

            task_service = TaskService(db)
            project_tasks = task_service.get_project_tasks(session.project_id)
            print(f"DEBUG: Found {len(project_tasks)} tasks for project")

            # Recover stale task states from an older interrupted session run.
            # If the session is being restarted and no session is currently active,
            # tasks left in RUNNING should become PENDING so they can be re-queued.
            stale_running_tasks = [
                task for task in project_tasks if task.status == TaskStatus.RUNNING
            ]
            for task in stale_running_tasks:
                task.status = TaskStatus.PENDING
                task.error_message = None
                task.started_at = None
                task.completed_at = None
                task.current_step = 0
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

            # Queue all pending tasks for execution
            queued_tasks = []
            for task in pending_tasks:
                if task.status == TaskStatus.PENDING:
                    # Update task status to running
                    task_service.update_task_status(task.id, TaskStatus.RUNNING)

                    # Queue the task for execution via Celery
                    print(f"DEBUG: Queuing task {task.id}: {task.title}")
                    result = execute_openclaw_task.delay(
                        session_id=session_id,
                        task_id=task.id,
                        prompt=task.description or task.title,
                        timeout_seconds=300,
                    )
                    print(f"DEBUG: Celery task queued with ID: {result.id}")
                    queued_tasks.append(
                        {
                            "task_id": task.id,
                            "task_name": task.title,
                            "celery_id": result.id,
                        }
                    )
                    print(f"DEBUG: Task queued successfully")

                    # Log the task queue
                    db.add(
                        LogEntry(
                            session_id=session_id,
                            session_instance_id=session_instance_id,
                            task_id=task.id,
                            level="INFO",
                            message=f"Task queued: {task.title}",
                            log_metadata=json.dumps({"celery_task_id": result.id}),
                        )
                    )

            if not queued_tasks:
                task_status_summary = {
                    str(task.status.value if hasattr(task.status, "value") else task.status): 0
                    for task in pending_tasks
                }
                for task in pending_tasks:
                    key = str(task.status.value if hasattr(task.status, "value") else task.status)
                    task_status_summary[key] = task_status_summary.get(key, 0) + 1

                db.add(
                    LogEntry(
                        session_id=session_id,
                        session_instance_id=session_instance_id,
                        level="WARN",
                        message="No tasks were queued for this session start",
                        log_metadata=json.dumps({"task_status_summary": task_status_summary}),
                    )
                )
                db.commit()

            # Update session metadata with queued tasks
            session_key = (
                f"{session_key}:tasks={','.join([str(t['task_id']) for t in queued_tasks])}"
                if queued_tasks
                else session_key
            )

        # Update session state
        session.is_active = True
        session.started_at = datetime.now(timezone.utc)
        session.status = "running"
        db.commit()

        # Log the start with instance tracking
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

    except Exception as e:
        db.add(
            LogEntry(
                session_id=session_id,
                level="ERROR",
                message=f"Failed to start session: {str(e)}",
            )
        )
        db.commit()
        raise HTTPException(status_code=500, detail=str(e))


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
    # Verify session exists
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Check if running (includes 'active' as equivalent to 'running')
    if session.status not in ["running", "paused", "active"]:
        raise HTTPException(status_code=400, detail="Session is not running")

    try:
        checkpoint_name = None
        try:
            from app.services.checkpoint_service import CheckpointService

            checkpoint_service = CheckpointService(db)
            latest_checkpoint = checkpoint_service.load_checkpoint(session_id)
            checkpoint_name = f"stopped_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}"
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

        revoked_ids = _revoke_session_celery_tasks(db, session_id, terminate=True)

        # Initialize OpenClaw service
        openclaw_service = OpenClawSessionService(db, session_id, use_demo_mode=False)

        # Stop the OpenClaw session
        if not force:
            await openclaw_service.stop_session()

        # Update database
        session.is_active = False
        session.stopped_at = datetime.now(timezone.utc)
        session.status = "stopped"
        db.commit()

        # Log the stop
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

    except Exception as e:
        db.add(
            LogEntry(
                session_id=session_id,
                level="ERROR",
                message=f"Failed to stop session: {str(e)}",
            )
        )
        db.commit()
        raise HTTPException(status_code=500, detail=str(e))


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
    # Verify session exists
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Check if running (includes 'active' as equivalent to 'running')
    if session.status not in ["running", "paused", "active"]:
        raise HTTPException(status_code=400, detail="Session is not running")

    try:
        from app.services.checkpoint_service import CheckpointService

        revoked_ids = _revoke_session_celery_tasks(db, session_id, terminate=True)

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
            # Fallback for direct/non-worker executions that don't have an autosave yet.
            openclaw_service = OpenClawSessionService(
                db, session_id, use_demo_mode=False
            )
            await openclaw_service.pause_session()

        # Update database
        session.is_active = True  # Keep session active when paused
        session.status = "paused"
        session.paused_at = datetime.now(timezone.utc)
        db.commit()

        # Log the pause
        db.add(
            LogEntry(
                session_id=session_id,
                level="INFO",
                message=f"Session paused: {session.name}",
                log_metadata=json.dumps(
                    {
                        "revoked_task_ids": revoked_ids,
                        "checkpoint_name": checkpoint_name,
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

    except Exception as e:
        db.add(
            LogEntry(
                session_id=session_id,
                level="ERROR",
                message=f"Failed to pause session: {str(e)}",
            )
        )
        db.commit()
        raise HTTPException(status_code=500, detail=str(e))


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
    # Verify session exists
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Check if resumable
    if session.status not in ["paused", "stopped"]:
        raise HTTPException(status_code=400, detail="Session is not resumable")

    try:
        from app.services.checkpoint_service import CheckpointService
        from app.tasks.worker import execute_openclaw_task
        from app.models import Task

        checkpoint_service = CheckpointService(db)
        checkpoint_data = checkpoint_service.load_checkpoint(session_id)
        checkpoint_name = checkpoint_data.get("checkpoint_name")
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

        result = execute_openclaw_task.delay(
            session_id=session_id,
            task_id=task.id,
            prompt=prompt,
            timeout_seconds=300,
            resume_checkpoint_name=checkpoint_name,
        )

        # Update database
        session.status = "running"
        session.is_active = True
        session.resumed_at = datetime.now(timezone.utc)
        db.commit()

        # Log the resume
        db.add(
            LogEntry(
                session_id=session_id,
                level="INFO",
                message=f"Session resumed: {session.name}",
                log_metadata=json.dumps(
                    {
                        "checkpoint_name": checkpoint_name,
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
            "message": f"Session '{session.name}' resumed successfully",
        }

    except Exception as e:
        db.add(
            LogEntry(
                session_id=session_id,
                level="ERROR",
                message=f"Failed to resume session: {str(e)}",
            )
        )
        db.commit()
        raise HTTPException(status_code=500, detail=str(e))


@router.get("/sessions/{session_id}/prompts/{template_name}")
def get_prompt_template(
    session_id: int, template_name: str, db: Session = Depends(get_db)
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
    # Verify session exists
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Default limit to prevent timeouts
    default_limit = 100
    effective_limit = limit if limit else default_limit

    # Cap limit at 1000 to prevent abuse
    if effective_limit > 1000:
        effective_limit = 1000

    # Build query - filter by instance_id if available
    logs_query = db.query(LogEntry).filter(
        LogEntry.session_id == session_id
    )

    if session.instance_id:
        logs_query = logs_query.filter(LogEntry.session_instance_id == session.instance_id)

    # Apply pagination
    logs = logs_query.order_by(LogEntry.created_at.desc()).offset(offset).limit(effective_limit).all()

    return {"logs": logs, "total": logs_query.count()}


@router.get("/sessions/{session_id}/logs/sorted")
def get_sorted_logs(
    session_id: int,
    db: Session = Depends(get_db),
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
    # Verify session exists
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Default limit to prevent timeouts
    default_limit = 100
    effective_limit = limit if limit else default_limit

    # Cap limit at 1000 to prevent abuse
    if effective_limit > 1000:
        effective_limit = 1000

    # OPTIMIZATION: Use database-level sorting instead of Python sorting
    # This is MUCH faster for large datasets
    logs_query = db.query(LogEntry).filter(
        LogEntry.session_id == session_id,
        LogEntry.session_instance_id == session.instance_id,
    )

    # Apply level filter if specified
    if level:
        logs_query = logs_query.filter(LogEntry.level == level)

    # Get total count BEFORE pagination
    total_logs = logs_query.count()

    # Apply database-level sorting (ORDER BY) - this is fast!
    if order == "desc":
        logs_query = logs_query.order_by(LogEntry.created_at.desc())
    else:
        logs_query = logs_query.order_by(LogEntry.created_at.asc())

    # Apply pagination (LIMIT + OFFSET) - this is fast!
    logs_entries = logs_query.offset(offset).limit(effective_limit).all()

    # Convert to list of dicts
    logs = [
        {
            "id": log.id,
            "session_id": log.session_id,
            "task_id": log.task_id,
            "level": log.level,
            "message": log.message,
            "timestamp": log.created_at.isoformat(),
            "metadata": json.loads(log.log_metadata) if log.log_metadata else {},
        }
        for log in logs_entries
    ]

    # Only deduplicate if requested (this is expensive, so make it optional)
    if deduplicate:
        logs = deduplicate_logs(logs)

    return {
        "session_id": session_id,
        "session_instance_id": session.instance_id,
        "total_logs": total_logs,
        "returned_logs": len(logs),
        "offset": offset,
        "limit": effective_limit,
        "sort_order": order,
        "deduplicated": deduplicate,
        "logs": logs,
        "has_more": (offset + len(logs)) < total_logs,
    }


@router.post("/generate-steps")
async def generate_steps_from_description(
    request: Request,
    body: dict,
    db: Session = Depends(get_db),
):
    """Generate task steps using OpenClaw AI"""
    from app.services.openclaw_service import OpenClawSessionService

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


# ============================================================================
# OVERWRITE PROTECTION ENDPOINTS (Session-scoped)
# ============================================================================

from pydantic import BaseModel
from typing import List, Optional


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
    # Verify session exists
    from app.models import Session as SessionModel

    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        from app.services.overwrite_protection_service import (
            OverwriteProtectionService,
            OverwriteProtectionError,
        )

        protection = OverwriteProtectionService(db)

        result = protection.check_and_warn(
            project_id=request.project_id,
            task_subfolder=request.task_subfolder,
            planned_files=request.planned_files,
            action="warn",  # Show warning but allow proceed
        )

        return {
            "safe_to_proceed": result["safe_to_proceed"],
            "workspace_exists": result.get("workspace_exists", False),
            "file_count": result.get("file_count", 0),
            "would_overwrite": result.get("has_conflicts", False),
            "warning_message": result.get("warning_message"),
            "conflicting_files": result.get("conflict_info", {}).get(
                "conflicting_files", []
            ),
        }

    except OverwriteProtectionError as e:
        raise HTTPException(status_code=409, detail=str(e))


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
    # Verify session exists
    from app.models import Session as SessionModel, Project

    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        from app.services.overwrite_protection_service import OverwriteProtectionService

        protection = OverwriteProtectionService(db)

        project_id = session.project_id or 1

        backup_result = protection.create_backup_of_existing(
            project_id=project_id,
            task_subfolder=_get_session_task_subfolder(db, session),
        )

        return {
            "success": backup_result["success"],
            "backup_path": backup_result.get("backup_path"),
            "files_backed_up": backup_result.get("file_count", 0),
            "error": backup_result.get("error"),
        }

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
    # Verify session exists
    from app.models import Session as SessionModel, Project

    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        from app.services.overwrite_protection_service import OverwriteProtectionService

        protection = OverwriteProtectionService(db)

        project_id = session.project_id or 1

        workspace_info = protection.check_workspace_exists(
            project_id=project_id,
            task_subfolder=_get_session_task_subfolder(db, session),
        )

        return {
            "exists": workspace_info.get("exists", False),
            "path": workspace_info.get("path"),
            "file_count": workspace_info.get("file_count", 0),
            "last_modified": workspace_info.get("last_modified"),
            "would_overwrite": workspace_info.get("would_overwrite", False),
        }

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Workspace info failed: {str(e)}")


# ============================================================================
# CHECKPOINT MANAGEMENT ENDPOINTS (For Pause/Resume Functionality)
# ============================================================================

from pydantic import BaseModel
from typing import List, Optional


class CheckpointListResponse(BaseModel):
    """Response model for checkpoint listing"""

    checkpoints: List[Dict[str, Any]]
    total_count: int


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
    # Verify session exists
    from app.models import Session as SessionModel

    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        openclaw_service = OpenClawSessionService(db, session_id)

        # Save checkpoint using the service
        await openclaw_service.pause_session()  # This includes checkpoint saving

        return {
            "success": True,
            "message": "Checkpoint saved successfully",
            "session_id": session_id,
        }

    except OpenClawSessionError as e:
        raise HTTPException(status_code=409, detail=str(e))


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
    # Verify session exists
    from app.models import Session as SessionModel

    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        from app.services.checkpoint_service import CheckpointService

        checkpoint_service = CheckpointService(db)

        checkpoints = checkpoint_service.list_checkpoints(session_id)

        return {
            "session_id": session_id,
            "total_count": len(checkpoints),
            "checkpoints": checkpoints,
        }

    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to list checkpoints: {str(e)}"
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
    # Verify session exists
    from app.models import Session as SessionModel

    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        openclaw_service = OpenClawSessionService(db, session_id)

        # Resume from checkpoint
        session_key = await openclaw_service.resume_session(
            checkpoint_name=checkpoint_name
        )

        return {
            "success": True,
            "session_key": session_key,
            "message": f"Session resumed from checkpoint: {checkpoint_name}",
            "session_id": session_id,
        }

    except OpenClawSessionError as e:
        raise HTTPException(status_code=409, detail=str(e))


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
    # Verify session exists
    from app.models import Session as SessionModel

    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        from app.services.checkpoint_service import CheckpointService

        checkpoint_service = CheckpointService(db)

        deleted = checkpoint_service.delete_checkpoint(session_id, checkpoint_name)

        if not deleted:
            raise HTTPException(status_code=404, detail="Checkpoint not found")

        return {
            "success": True,
            "message": f"Checkpoint '{checkpoint_name}' deleted successfully",
            "session_id": session_id,
            "checkpoint_name": checkpoint_name,
        }

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
    # Verify session exists
    from app.models import Session as SessionModel

    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    try:
        from app.services.checkpoint_service import CheckpointService

        checkpoint_service = CheckpointService(db)

        result = checkpoint_service.cleanup_old_checkpoints(
            session_id=session_id, keep_latest=keep_latest, max_age_hours=max_age_hours
        )

        return {
            "success": True,
            "deleted_count": result.get("deleted", 0),
            "kept_count": result.get("kept", 0),
            "error": result.get("error"),
        }

    except Exception as e:
        raise HTTPException(
            status_code=500, detail=f"Failed to cleanup checkpoints: {str(e)}"
        )
