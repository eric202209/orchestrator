"""Tasks API endpoints"""

from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import json
import asyncio
from app.database import get_db
from app.models import Task, TaskStatus, Project, LogEntry
from app.schemas import TaskCreate, TaskUpdate, TaskResponse
from app.services.openclaw_service import OpenClawSessionService
from app.services.log_utils import sort_logs, deduplicate_logs

router = APIRouter()

# Constants
MAX_PROMPT_LENGTH = 50000  # Max prompt length to avoid context window overflow


@router.post("/tasks", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
def create_task(task: TaskCreate, db: Session = Depends(get_db)):
    """Create a new task"""
    # Verify project exists
    project = db.query(Project).filter(Project.id == task.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    db_task = Task(**task.model_dump(), status=TaskStatus.PENDING)
    db.add(db_task)
    db.commit()
    db.refresh(db_task)
    return db_task


@router.get("/projects/{project_id}/tasks", response_model=List[TaskResponse])
def get_project_tasks(
    project_id: int, skip: int = 0, limit: int = 100, db: Session = Depends(get_db)
):
    """Get all tasks for a project"""
    # Verify project exists
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    tasks = (
        db.query(Task)
        .filter(Task.project_id == project_id)
        .offset(skip)
        .limit(limit)
        .all()
    )
    return tasks


@router.post("/tasks/{task_id}/execute")
async def execute_task_with_openclaw(
    task_id: int, request: Request, db: Session = Depends(get_db)
):
    """
    Execute a task using OpenClaw AI agent with real-time log streaming

    Args:
        task_id: Task ID to execute
        request: HTTP request with prompt data
        db: Database session

    Returns:
        Execution result with logs
    """
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Get prompt from request body or use task description
    try:
        prompt_data = await request.json()
        prompt = prompt_data.get("prompt") if prompt_data else task.description
        # Get timeout settings from request
        timeout_seconds = prompt_data.get("timeout_seconds", 600)  # Default 10 minutes
    except json.JSONDecodeError:
        prompt = task.description
        timeout_seconds = 600

    try:
        # Start OpenClaw session
        session_service = OpenClawSessionService(db, None, task_id)
        openclaw_key = await session_service.start_session(prompt)

        # Build prompt using templates
        from app.services import PromptTemplates

        # Get session context
        session_context = await session_service.get_session_context()

        # Build enhanced prompt with templates
        prompt_text = PromptTemplates.format_task_execution(
            task=task, prompt=prompt, session_context=session_context
        )

        # Increase timeout for complex tasks - default to 600 seconds (10 minutes)
        # This prevents premature timeouts on medium/large tasks
        actual_timeout = max(timeout_seconds, 600)

        # Execute with streaming enabled for real-time logs
        result = await session_service.execute_task_with_streaming(
            prompt=prompt_text,
            timeout_seconds=actual_timeout,
        )

        # Update task status
        if result["status"] == "completed":
            task.status = TaskStatus.COMPLETED
            task.output = result.get("output", "")[:10000]  # Limit output size
        else:
            task.status = TaskStatus.FAILED
            task.output = result.get("output", "")[:10000]
            task.error = result.get("error", "Unknown error")

        db.commit()
        db.refresh(task)

        return result

    except Exception as e:
        error_msg = f"Task execution failed: {str(e)}"
        task.status = TaskStatus.FAILED
        task.error = error_msg
        db.commit()
        raise HTTPException(status_code=500, detail=error_msg)


@router.get("/tasks/{task_id}")
def get_task(task_id: int, db: Session = Depends(get_db)):
    """Get a task by ID"""
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.put("/tasks/{task_id}", response_model=TaskResponse)
def update_task(task_id: int, task_update: TaskUpdate, db: Session = Depends(get_db)):
    """Update a task"""
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    update_data = task_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(task, field, value)

    db.commit()
    db.refresh(task)
    return task


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(task_id: int, db: Session = Depends(get_db)):
    """Delete a task"""
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    db.delete(task)
    db.commit()
    return None


@router.get("/tasks/{task_id}/logs/sorted")
def get_sorted_task_logs(
    task_id: int,
    db: Session = Depends(get_db),
    order: str = "asc",
    deduplicate: bool = True,
    level: Optional[str] = None,
    limit: Optional[int] = None,
):
    """
    Get sorted and optionally deduplicated logs for a task

    Args:
        task_id: Task ID
        order: "asc" for oldest first, "desc" for newest first
        deduplicate: Remove duplicate log entries
        level: Optional log level filter
        limit: Optional limit on number of logs

    Returns:
        Sorted list of log entries
    """
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    logs_entries = db.query(LogEntry).filter(LogEntry.task_id == task_id).all()

    if level:
        logs_entries = [log for log in logs_entries if log.level == level]

    logs = [
        {
            "id": log.id,
            "task_id": log.task_id,
            "session_id": log.session_id,
            "level": log.level,
            "message": log.message,
            "timestamp": log.created_at.isoformat(),
            "metadata": json.loads(log.log_metadata) if log.log_metadata else {},
        }
        for log in logs_entries
    ]

    sorted_logs = sort_logs(logs, order=order, deduplicate=deduplicate)

    if limit:
        sorted_logs = sorted_logs[:limit]

    return {
        "task_id": task_id,
        "total_logs": len(logs),
        "returned_logs": len(sorted_logs),
        "sort_order": order,
        "deduplicated": deduplicate,
        "logs": sorted_logs,
    }
