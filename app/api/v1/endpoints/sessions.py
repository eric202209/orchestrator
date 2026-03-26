"""Sessions API endpoints with OpenClaw integration"""

from fastapi import APIRouter, Depends, HTTPException, status, WebSocket, WebSocketDisconnect
from fastapi.requests import Request
from sqlalchemy.orm import Session
from typing import List, Optional, Any
from datetime import datetime, timezone
import json
import asyncio
import logging

from app.database import get_db

logger = logging.getLogger(__name__)
from app.models import Session as SessionModel, SessionTask, TaskStatus, LogEntry
from app.schemas import SessionCreate, SessionUpdate, SessionResponse, TaskExecuteRequest
from app.services import (
    OpenClawSessionService,
    LogStreamService,
    ToolTrackingService,
    PromptTemplates
)
from app.services.log_utils import sort_logs, deduplicate_logs, format_logs_batch
from app.dependencies import get_current_user
from app.auth import verify_token

router = APIRouter()


# In-memory WebSocket connections for log streaming
active_websockets: dict = {}


@router.post("/sessions", response_model=SessionResponse, status_code=status.HTTP_201_CREATED)
def create_session(
    session: SessionCreate, 
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Create a new OpenClaw orchestration session"""
    # Verify project exists
    from app.models import Project
    project = db.query(Project).filter(Project.id == session.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    
    db_session = SessionModel(**session.model_dump())
    db.add(db_session)
    db.commit()
    db.refresh(db_session)
    
    # Log session creation
    db.add(LogEntry(
        session_id=db_session.id,
        level="INFO",
        message=f"Session created: {db_session.name}",
        metadata=json.dumps({"project_id": session.project_id})
    ))
    db.commit()
    
    return db_session


@router.get("/projects/{project_id}/sessions", response_model=List[SessionResponse])
def get_project_sessions(
    project_id: int, 
    db: Session = Depends(get_db),
    skip: int = 0,
    limit: int = 100,
    is_active: Optional[bool] = None
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
    current_user = Depends(get_current_user)
):
    """Get a specific session with detailed information"""
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    # Get associated logs
    log_count = db.query(LogEntry).filter(LogEntry.session_id == session_id).count()
    
    # Get associated tasks
    from app.models import SessionTask as SessionTaskModel
    session_tasks = db.query(SessionTaskModel).filter(SessionTaskModel.session_id == session_id).all()
    
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
    current_user = Depends(get_current_user)
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
    db.add(LogEntry(
        session_id=session_id,
        level="INFO",
        message=f"Session updated: {session_id}",
        metadata=json.dumps({"updates": update_data})
    ))
    db.commit()
    
    return db_session


@router.delete("/sessions/{session_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_session(
    session_id: int, 
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """Delete a session"""
    db_session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not db_session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    db.delete(db_session)
    db.commit()
    return None


from pydantic import BaseModel

class StartOpenClawRequest(BaseModel):
    task_description: str

@router.post("/sessions/{session_id}/start-openclaw")
async def start_openclaw_session(
    session_id: int,
    request: StartOpenClawRequest,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
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
        db.add(LogEntry(
            session_id=session_id,
            level="INFO",
            message=f"OpenClaw session started: {task_description[:100]}",
            metadata=json.dumps({
                "session_key": session_key,
                "task_description": task_description
            })
        ))
        db.commit()
        
        return {
            "status": "started",
            "session_key": session_key,
            "session_id": session_id,
            "message": f"OpenClaw session created for task: {task_description[:50]}..."
        }
        
    except Exception as e:
        db.add(LogEntry(
            session_id=session_id,
            level="ERROR",
            message=f"Failed to start OpenClaw session: {str(e)}"
        ))
        db.commit()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sessions/{session_id}/execute")
async def execute_task(
    session_id: int,
    task_request: TaskExecuteRequest,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Execute a task via OpenClaw
    
    Args:
        session_id: Session ID
        task_request: Task execution request with task description and timeout
    """
    prompt = task_request.task
    timeout_seconds = task_request.timeout_seconds
    use_demo_mode = task_request.use_demo_mode if hasattr(task_request, 'use_demo_mode') else True
    
    if not prompt:
        raise HTTPException(status_code=422, detail="Task prompt is required")
    # Verify session exists
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    try:
        # Initialize services with demo mode flag
        openclaw_service = OpenClawSessionService(db, session_id, use_demo_mode=use_demo_mode)
        
        # Create OpenClaw session (generates session key)
        task_description = session.description or session.name
        await openclaw_service.create_openclaw_session(task_description)
        
        # Get session context
        context = await openclaw_service.get_session_context()
        
        # Build prompt using templates
        prompt_text = PromptTemplates.build_task_prompt(
            task_description=prompt,
            project_context=context.get("project_context"),
            recent_logs=context.get("recent_logs", [])
        )
        
        # Execute task
        result = await openclaw_service.execute_task(prompt_text, timeout_seconds)
        
        return {
            "status": "completed",
            "result": result,
            "execution_id": f"exec_{session_id}_{datetime.utcnow().timestamp()}"
        }
        
    except Exception as e:
        db.add(LogEntry(
            session_id=session_id,
            level="ERROR",
            message=f"Task execution failed: {str(e)}"
        ))
        db.commit()
        raise HTTPException(status_code=500, detail=str(e))


@router.websocket("/sessions/{session_id}/logs/stream")
async def websocket_log_stream(
    websocket: WebSocket,
    session_id: int,
    db: Session = Depends(get_db)
):
    """
    WebSocket endpoint for real-time log streaming
    
    Clients can connect to receive live logs from an OpenClaw session
    """
    # Verify session exists
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        await websocket.close(code=1008, reason="Session not found")
        return
    
    # Accept connection
    await websocket.accept()
    
    # Register WebSocket
    if session_id not in active_websockets:
        active_websockets[session_id] = []
    active_websockets[session_id].append(websocket)
    
    # Send initial connection confirmation
    await websocket.send_json({
        "type": "connected",
        "session_id": session_id,
        "timestamp": datetime.utcnow().isoformat()
    })
    
    # Send recent logs
    log_service = LogStreamService(db)
    recent_logs = log_service.get_recent_logs(session_id, limit=20)
    
    for log in recent_logs:
        await websocket.send_json({
            "type": "log",
            **log
        })
    
    try:
        # Keep connection alive
        while True:
            data = await websocket.receive_text()
            # Handle client messages (could be commands, heartbeats, etc.)
            if data == "ping":
                await websocket.send_text("pong")
    
    except WebSocketDisconnect:
        active_websockets[session_id].remove(websocket)
        if not active_websockets[session_id]:
            del active_websockets[session_id]
        logger.info(f"WebSocket disconnected for session {session_id}")


@router.get("/sessions/{session_id}/logs")
def get_session_logs(
    session_id: int,
    db: Session = Depends(get_db),
    level: Optional[str] = None,
    limit: int = 100,
    offset: int = 0
):
    """Get log entries for a session"""
    query = db.query(LogEntry).filter(LogEntry.session_id == session_id)
    
    if level:
        query = query.filter(LogEntry.level == level.upper())
    
    logs = query.order_by(LogEntry.created_at.desc()).offset(offset).limit(limit).all()
    
    return {
        "total": query.count(),
        "logs": [
            {
                "id": log.id,
                "level": log.level,
                "message": log.message,
                "timestamp": log.created_at.isoformat(),
                "metadata": json.loads(log.log_metadata) if log.log_metadata else {}
            }
            for log in logs
        ]
    }


@router.get("/sessions/{session_id}/tools")
def get_tool_execution_history(
    session_id: int,
    db: Session = Depends(get_db),
    task_id: Optional[int] = None,
    limit: int = 50,
    tool_name: Optional[str] = None
):
    """Get tool execution history for a session"""
    tool_service = ToolTrackingService(db)
    
    executions = tool_service.get_execution_history(
        session_id=session_id,
        task_id=task_id,
        limit=limit,
        tool_name=tool_name
    )
    
    return {
        "total": len(executions),
        "executions": executions
    }


@router.get("/sessions/{session_id}/statistics")
def get_session_statistics(
    session_id: int,
    db: Session = Depends(get_db),
    days: int = 7
):
    """Get statistics for a session"""
    tool_service = ToolTrackingService(db)
    
    # Get log statistics
    total_logs = db.query(LogEntry).filter(LogEntry.session_id == session_id).count()
    info_logs = db.query(LogEntry).filter(
        LogEntry.session_id == session_id,
        LogEntry.level == "INFO"
    ).count()
    error_logs = db.query(LogEntry).filter(
        LogEntry.session_id == session_id,
        LogEntry.level == "ERROR"
    ).count()
    
    # Get tool statistics
    tool_stats = tool_service.get_tool_statistics(session_id, days)
    
    return {
        "session_id": session_id,
        "period_days": days,
        "logs": {
            "total": total_logs,
            "info": info_logs,
            "errors": error_logs
        },
        "tools": tool_stats
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
    task_id: Optional[int] = None
):
    """Manually track a tool execution"""
    tool_service = ToolTrackingService(db)
    
    execution = tool_service.track(
        execution_id=execution_id,
        tool_name=tool_name,
        params=params,
        result=result,
        success=success,
        session_id=session_id,
        task_id=task_id
    )
    
    return {
        "status": "tracked",
        "execution_id": execution_id,
        "tool": tool_name
    }


@router.post("/sessions/{session_id}/start")
async def start_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
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
    
    # Check if already running
    if session.is_active:
        raise HTTPException(status_code=400, detail="Session is already running")
    
    try:
        # Initialize OpenClaw service
        openclaw_service = OpenClawSessionService(db, session_id, use_demo_mode=False)
        
        # Create OpenClaw session using session description or name
        task_description = session.description or session.name
        session_key = await openclaw_service.create_openclaw_session(task_description)
        
        # If session is linked to a project, queue all pending tasks
        print(f"DEBUG: session.project_id = {session.project_id}")
        if session.project_id:
            print(f"DEBUG: Found project_id {session.project_id}, queuing tasks...")
            from app.services.task_service import TaskService
            from app.models import TaskStatus
            
            task_service = TaskService(db)
            pending_tasks = task_service.get_project_tasks(session.project_id)
            print(f"DEBUG: Found {len(pending_tasks)} tasks for project")
            
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
                        timeout_seconds=300
                    )
                    print(f"DEBUG: Celery task queued with ID: {result.id}")
                    queued_tasks.append({
                        "task_id": task.id,
                        "task_name": task.title,
                        "celery_id": result.id
                    })
                    print(f"DEBUG: Task queued successfully")
                    
                    # Log the task queue
                    db.add(LogEntry(
                        session_id=session_id,
                        task_id=task.id,
                        level="INFO",
                        message=f"Task queued: {task.title}",
                        metadata=json.dumps({
                            "celery_task_id": result.id
                        })
                    ))
            
            # Update session metadata with queued tasks
            session_key = f"{session_key}:tasks={','.join([str(t['task_id']) for t in queued_tasks])}" if queued_tasks else session_key
        
        # Update database
        session.is_active = True
        session.started_at = datetime.now(timezone.utc)
        session.status = "running"
        db.commit()
        
        # Log the start
        db.add(LogEntry(
            session_id=session_id,
            level="INFO",
            message=f"Session started: {session.name}",
            metadata=json.dumps({
                "session_key": session_key,
                "task_description": task_description
            })
        ))
        db.commit()
        
        return {
            "status": "started",
            "session_key": session_key,
            "session_id": session_id,
            "message": f"Session '{session.name}' started successfully"
        }
        
    except Exception as e:
        db.add(LogEntry(
            session_id=session_id,
            level="ERROR",
            message=f"Failed to start session: {str(e)}"
        ))
        db.commit()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sessions/{session_id}/stop")
async def stop_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user),
    force: bool = False
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
    
    # Check if not running
    if not session.is_active:
        raise HTTPException(status_code=400, detail="Session is not running")
    
    try:
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
        db.add(LogEntry(
            session_id=session_id,
            level="INFO",
            message=f"Session stopped: {session.name}",
            metadata=json.dumps({"force": force})
        ))
        db.commit()
        
        return {
            "status": "stopped",
            "session_id": session_id,
            "message": f"Session '{session.name}' stopped successfully"
        }
        
    except Exception as e:
        db.add(LogEntry(
            session_id=session_id,
            level="ERROR",
            message=f"Failed to stop session: {str(e)}"
        ))
        db.commit()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sessions/{session_id}/pause")
async def pause_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Pause a running session
    
    Saves current state and pauses execution
    """
    # Verify session exists
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    # Check if running
    if not session.is_active:
        raise HTTPException(status_code=400, detail="Session is not running")
    
    try:
        # Initialize OpenClaw service
        openclaw_service = OpenClawSessionService(db, session_id, use_demo_mode=False)
        
        # Pause the OpenClaw session
        await openclaw_service.pause_session()
        
        # Update database
        session.status = "paused"
        session.paused_at = datetime.now(timezone.utc)
        db.commit()
        
        # Log the pause
        db.add(LogEntry(
            session_id=session_id,
            level="INFO",
            message=f"Session paused: {session.name}",
            metadata={}
        ))
        db.commit()
        
        return {
            "status": "paused",
            "session_id": session_id,
            "message": f"Session '{session.name}' paused successfully"
        }
        
    except Exception as e:
        db.add(LogEntry(
            session_id=session_id,
            level="ERROR",
            message=f"Failed to pause session: {str(e)}"
        ))
        db.commit()
        raise HTTPException(status_code=500, detail=str(e))


@router.post("/sessions/{session_id}/resume")
async def resume_session(
    session_id: int,
    db: Session = Depends(get_db),
    current_user = Depends(get_current_user)
):
    """
    Resume a paused session
    
    Restores saved state and continues execution
    """
    # Verify session exists
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    # Check if paused
    if session.status != "paused":
        raise HTTPException(status_code=400, detail="Session is not paused")
    
    try:
        # Initialize OpenClaw service
        openclaw_service = OpenClawSessionService(db, session_id, use_demo_mode=False)
        
        # Resume the OpenClaw session
        await openclaw_service.resume_session()
        
        # Update database
        session.status = "running"
        session.is_active = True
        session.resumed_at = datetime.now(timezone.utc)
        db.commit()
        
        # Log the resume
        db.add(LogEntry(
            session_id=session_id,
            level="INFO",
            message=f"Session resumed: {session.name}",
            metadata={}
        ))
        db.commit()
        
        return {
            "status": "resumed",
            "session_id": session_id,
            "message": f"Session '{session.name}' resumed successfully"
        }
        
    except Exception as e:
        db.add(LogEntry(
            session_id=session_id,
            level="ERROR",
            message=f"Failed to resume session: {str(e)}"
        ))
        db.commit()
        raise HTTPException(status_code=500, detail=str(e))


@router.websocket("/sessions/{session_id}/status")
async def websocket_session_status(
    websocket: WebSocket,
    session_id: int,
    db: Session = Depends(get_db)
):
    """
    WebSocket endpoint for real-time session status updates
    
    Clients can connect to receive live status updates from a session
    """
    # Extract token from WebSocket query params if provided
    # For now, accept all connections (auth is handled by frontend routing)
    
    # Verify session exists
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        await websocket.close(code=1008, reason="Session not found")
        return
    
    # Accept connection
    await websocket.accept()
    
    # Register WebSocket
    if session_id not in active_websockets:
        active_websockets[session_id] = []
    active_websockets[session_id].append(websocket)
    
    # Send initial status
    await websocket.send_json({
        "type": "status_update",
        "session_id": session_id,
        "status": {
            "is_active": session.is_active,
            "status": session.status,
            "started_at": session.started_at.isoformat() if session.started_at else None,
            "stopped_at": session.stopped_at.isoformat() if session.stopped_at else None,
            "paused_at": session.paused_at.isoformat() if session.paused_at else None,
            "resumed_at": session.resumed_at.isoformat() if session.resumed_at else None
        },
        "timestamp": datetime.utcnow().isoformat()
    })
    
    try:
        # Keep connection alive
        while True:
            data = await websocket.receive_text()
            
            # Handle client messages
            if data == "ping":
                await websocket.send_text("pong")
            elif data == "status":
                # Send current status on demand
                session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
                await websocket.send_json({
                    "type": "status_update",
                    "session_id": session_id,
                    "status": {
                        "is_active": session.is_active,
                        "status": session.status,
                        "started_at": session.started_at.isoformat() if session.started_at else None,
                        "stopped_at": session.stopped_at.isoformat() if session.stopped_at else None,
                        "paused_at": session.paused_at.isoformat() if session.paused_at else None,
                        "resumed_at": session.resumed_at.isoformat() if session.resumed_at else None
                    },
                    "timestamp": datetime.utcnow().isoformat()
                })
    
    except WebSocketDisconnect:
        active_websockets[session_id].remove(websocket)
        if not active_websockets[session_id]:
            del active_websockets[session_id]
        logger.info(f"WebSocket disconnected for session {session_id}")


@router.get("/sessions/{session_id}/prompts/{template_name}")
def get_prompt_template(
    session_id: int,
    template_name: str,
    db: Session = Depends(get_db)
):
    """Get a prompt template for the session"""
    template = PromptTemplates.get_template(template_name)
    if not template:
        raise HTTPException(status_code=404, detail=f"Template '{template_name}' not found")
    
    return {
        "template_name": template_name,
        "template": template,
        "session_id": session_id
    }


@router.get("/sessions/{session_id}/logs/sorted")
def get_sorted_logs(
    session_id: int,
    db: Session = Depends(get_db),
    order: str = "asc",  # "asc" for oldest first, "desc" for newest first
    deduplicate: bool = True,  # Remove duplicate entries
    level: Optional[str] = None,  # Optional filter by log level
    limit: Optional[int] = None,  # Optional limit on number of logs
):
    """
    Get sorted and optionally deduplicated logs for a session
    
    Args:
        session_id: Session ID
        order: Sort order - "asc" (oldest first) or "desc" (newest first)
        deduplicate: Remove duplicate log entries
        level: Optional log level filter (INFO, WARNING, ERROR)
        limit: Optional limit on number of logs to return
        
    Returns:
        Sorted list of log entries
    """
    # Verify session exists
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    
    # Get all logs for the session
    logs_query = db.query(LogEntry).filter(LogEntry.session_id == session_id)
    
    # Apply level filter if specified
    if level:
        logs_query = logs_query.filter(LogEntry.level == level)
    
    # Get logs
    logs_entries = logs_query.all()
    
    # Convert to list of dicts
    logs = [
        {
            "id": log.id,
            "session_id": log.session_id,
            "task_id": log.task_id,
            "level": log.level,
            "message": log.message,
            "timestamp": log.created_at.isoformat(),
            "metadata": json.loads(log.log_metadata) if log.log_metadata else {}
        }
        for log in logs_entries
    ]
    
    # Sort and deduplicate
    sorted_logs = sort_logs(logs, order=order, deduplicate=deduplicate)
    
    # Apply limit if specified
    if limit:
        sorted_logs = sorted_logs[:limit]
    
    return {
        "session_id": session_id,
        "total_logs": len(logs),
        "returned_logs": len(sorted_logs),
        "sort_order": order,
        "deduplicated": deduplicate,
        "logs": sorted_logs
    }
