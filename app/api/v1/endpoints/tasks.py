"""Tasks API endpoints"""

from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import time
import json
import asyncio
from pydantic import BaseModel
from app.database import get_db
from app.models import Task, TaskStatus, Project, LogEntry, SessionTask
from app.schemas import TaskCreate, TaskUpdate, TaskResponse
from app.services.openclaw_service import OpenClawSessionService
from app.services.error_handler import EnhancedErrorHandler
from app.services.log_utils import sort_logs


# Pydantic models for overwrite protection
class OverwriteCheckRequest(BaseModel):
    """Request model for overwrite check"""

    project_id: int
    task_subfolder: str
    planned_files: Optional[List[str]] = []


class BackupResponse(BaseModel):
    """Response model for backup creation"""

    success: bool
    backup_path: Optional[str] = None
    files_backed_up: Optional[int] = None
    error: Optional[str] = None


router = APIRouter()

# Constants
MAX_PROMPT_LENGTH = 50000  # Max prompt length to avoid context window overflow


def _latest_session_success_for_task(
    db: Session, task_id: int, session_id: Optional[int]
) -> bool:
    if not session_id:
        return False

    success_log = (
        db.query(LogEntry)
        .filter(
            LogEntry.task_id == task_id,
            LogEntry.session_id == session_id,
            LogEntry.message.ilike(
                f"[ORCHESTRATION] Task {task_id} completed successfully%"
            ),
        )
        .order_by(LogEntry.created_at.desc())
        .first()
    )
    return success_log is not None


@router.get("/tasks", response_model=List[TaskResponse])
def get_all_tasks(
    skip: int = 0,
    limit: int = 100,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """Get all tasks across all projects"""
    query = db.query(Task)

    if status:
        try:
            task_status = TaskStatus[status.upper()]
            query = query.filter(Task.status == task_status)
        except KeyError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    tasks = query.order_by(Task.created_at.desc()).offset(skip).limit(limit).all()
    return tasks


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
        # NEW: Check for potential overwrites BEFORE executing task
        from app.services.overwrite_protection_service import (
            OverwriteProtectionService,
            OverwriteProtectionError,
        )

        protection = OverwriteProtectionService(db)

        # Get project info to check workspace
        if not task.project:
            raise HTTPException(status_code=404, detail="Project not found")

        try:
            overwrite_result = protection.check_and_warn(
                project_id=task.project.id,
                task_subfolder=f"task_{task_id}",  # Task subfolder format
                planned_files=[],  # We don't know planned files yet
                action="warn",  # Show warning but allow proceed
            )

            if not overwrite_result["safe_to_proceed"]:
                # Log the warning for audit trail
                print(
                    f"⚠️  Overwrite detected: {overwrite_result.get('warning_message', '')[:200]}"
                )
        except Exception as e:
            # If check fails, log warning but don't block execution
            print(f"⚠️  Overwrite check failed (continuing anyway): {str(e)[:100]}")

        # Start OpenClaw session and create database session record
        from app.models import Session as SessionModel

        # Create a new session record for this task execution
        new_session = SessionModel(
            name=f"Task {task_id} Execution",
            description=prompt[:500],
            project_id=task.project_id if task.project else None,
            status="running",
            instance_id=f"orchestrator-task-{task_id}-{int(time.time())}",
        )
        db.add(new_session)
        db.commit()
        db.refresh(new_session)

        # Create SessionTask relationship
        session_task = SessionTask(
            session_id=new_session.id,
            task_id=task.id,
        )
        db.add(session_task)
        db.commit()

        print(f"✅ Created session {new_session.id} for task {task.id}")

        # Start OpenClaw session with the new session ID
        session_service = OpenClawSessionService(db, new_session.id, task_id)
        openclaw_key = await session_service.start_session(prompt)

        # Build prompt using templates
        from app.services.prompt_templates import PromptTemplates

        # Add overwrite warning to prompt if detected
        overwrite_warning = ""
        try:
            protection = OverwriteProtectionService(db)
            result = protection.check_and_warn(
                project_id=task.project.id,
                task_subfolder=f"task_{task_id}",
                action="warn",
            )

            if not result["safe_to_proceed"] and result.get("warning_message"):
                overwrite_warning = f"\n\n### ⚠️  EXISTING WORKSPACE WARNING:\n{result['warning_message']}"
        except:
            pass

        # Build enhanced prompt - use build_task_prompt for simple task execution
        prompt_text = PromptTemplates.build_task_prompt(
            task_description=prompt + overwrite_warning,
            project_context=f"Project: {task.project.name if task.project else 'Unknown'} at {task.project.workspace_path if task.project and task.project.workspace_path else '/workspace'}",
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
            task.status = TaskStatus.DONE
            task.output = result.get("output", "")[:10000]  # Limit output size
        else:
            task.status = TaskStatus.FAILED
            task.output = result.get("output", "")[:10000]
            task.error = result.get("error", "Unknown error")

        db.commit()
        db.refresh(task)

        return result

    except Exception as e:
        # Use enhanced error handler
        error_handler = EnhancedErrorHandler()
        recovery_plan = error_handler.create_error_recovery_plan(e, "task_execution")

        error_msg = f"Task execution failed: {str(e)}"
        import traceback

        error_details = traceback.format_exc()
        print(f"Error executing task: {error_msg}")
        print(f"Details: {error_details}")

        # Update task with enhanced error information
        if task:
            task.status = TaskStatus.FAILED
            task.error = error_msg
            task.error_message = f"{error_msg}\nRecommended action: {recovery_plan.get('recommended_action', 'manual_intervention')}"
            db.commit()
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

    # Prefer the most recent task/session relationship so Task Detail reflects
    # the latest execution context instead of an arbitrary historical row.
    session_task = (
        db.query(SessionTask)
        .filter(SessionTask.task_id == task_id)
        .order_by(
            SessionTask.started_at.desc().nullslast(),
            SessionTask.completed_at.desc().nullslast(),
            SessionTask.id.desc(),
        )
        .first()
    )
    session_id = session_task.session_id if session_task else None

    # Reconcile stale task state when the latest linked session already logged
    # successful completion but the task record is still marked failed.
    if task.status == TaskStatus.FAILED and _latest_session_success_for_task(
        db, task_id, session_id
    ):
        task.status = TaskStatus.DONE
        task.error_message = None
        task.completed_at = task.completed_at or datetime.utcnow()
        if session_task:
            session_task.status = TaskStatus.DONE
            session_task.completed_at = session_task.completed_at or task.completed_at
        db.commit()
        db.refresh(task)

    # Add session_id to task response
    task_dict = task.__dict__.copy()
    task_dict["session_id"] = session_id

    # If no session found but task is running/done, try to get from recent logs
    if not session_id and task.status in [TaskStatus.RUNNING, TaskStatus.DONE]:
        from app.models import LogEntry

        recent_log = (
            db.query(LogEntry)
            .filter(LogEntry.task_id == task_id)
            .order_by(LogEntry.created_at.desc())
            .first()
        )
        if recent_log and recent_log.session_id:
            session_id = recent_log.session_id
            task_dict["session_id"] = session_id

    return task_dict


@router.put("/tasks/{task_id}", response_model=TaskResponse)
def update_task(task_id: int, task_update: TaskUpdate, db: Session = Depends(get_db)):
    """Update a task"""
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    update_data = task_update.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No task fields provided")

    editable_fields = {
        "title",
        "description",
        "status",
        "priority",
        "steps",
        "current_step",
        "error_message",
    }

    for field, value in update_data.items():
        if field not in editable_fields:
            continue
        if field in {"description", "steps"} and value == "":
            value = None
        setattr(task, field, value)

    task.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(task)
    return task


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(task_id: int, db: Session = Depends(get_db)):
    """Delete a task"""
    from app.models import TaskCheckpoint

    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    db.query(LogEntry).filter(LogEntry.task_id == task_id).delete(
        synchronize_session=False
    )
    db.query(SessionTask).filter(SessionTask.task_id == task_id).delete(
        synchronize_session=False
    )
    db.query(TaskCheckpoint).filter(TaskCheckpoint.task_id == task_id).delete(
        synchronize_session=False
    )
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


# ============================================================================
# OVERWRITE PROTECTION ENDPOINTS
# ============================================================================


@router.post("/tasks/{task_id}/check-overwrites")
async def check_task_overwrites(
    task_id: int, request: OverwriteCheckRequest, db: Session = Depends(get_db)
):
    """
    Check for potential overwrites before executing a task

    Args:
        task_id: Task ID to check
        request: Overwrite check request with project info and planned files
        db: Database session

    Returns:
        Overwrite protection result with safety status and warnings
    """
    # Verify task exists
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

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


@router.post("/tasks/{task_id}/create-backup")
async def create_task_backup(task_id: int, db: Session = Depends(get_db)):
    """
    Create a backup of existing workspace before proceeding

    Args:
        task_id: Task ID to backup
        db: Database session

    Returns:
        Backup result with path and file count
    """
    # Verify task exists
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    try:
        from app.services.overwrite_protection_service import OverwriteProtectionService

        protection = OverwriteProtectionService(db)

        backup_result = protection.create_backup_of_existing(
            project_id=task.project.id if task.project else 1,
            task_subfolder=f"task_{task_id}",
        )

        return BackupResponse(**backup_result).model_dump()

    except Exception as e:
        raise HTTPException(status_code=500, detail=f"Backup failed: {str(e)}")


@router.get("/tasks/{task_id}/workspace-info")
async def get_workspace_info(task_id: int, db: Session = Depends(get_db)):
    """
    Get workspace information for a task

    Args:
        task_id: Task ID to check
        db: Database session

    Returns:
        Workspace details including file count and last modified date
    """
    # Verify task exists
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    try:
        from app.services.overwrite_protection_service import OverwriteProtectionService

        protection = OverwriteProtectionService(db)

        workspace_info = protection.check_workspace_exists(
            project_id=task.project.id if task.project else 1,
            task_subfolder=f"task_{task_id}",
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
