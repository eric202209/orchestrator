"""Tasks API endpoints"""

import logging
import json
import time
from datetime import UTC, datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status, Request
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Task, TaskStatus, Project, LogEntry, SessionTask
from app.schemas import TaskCreate, TaskUpdate, TaskResponse, TaskPromotionRequest
from app.dependencies import get_current_active_user
from app.services.agents.agent_runtime import create_agent_runtime
from app.services.error_handler import EnhancedErrorHandler
from app.services.log_utils import sort_logs
from app.services.name_formatter import humanize_display_name
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.persistence import append_orchestration_event
from app.services.orchestration.context_assembly import render_adapted_runtime_prompt
from app.services.session.session_runtime_service import ensure_task_workspace
from app.services.task_service import TaskService
from app.services.workspace.system_settings import (
    get_effective_agent_backend,
    get_effective_agent_model_family,
)
from app.config import settings

logger = logging.getLogger(__name__)


# Pydantic models for overwrite protection
class OverwriteCheckRequest(BaseModel):
    """Request model for overwrite check"""

    project_id: int
    task_subfolder: str
    planned_files: List[str] = Field(default_factory=list)


class BackupResponse(BaseModel):
    """Response model for backup creation"""

    success: bool
    backup_path: Optional[str] = None
    files_backed_up: Optional[int] = None
    error: Optional[str] = None


router = APIRouter()

# Constants
MAX_PROMPT_LENGTH = 50000  # Max prompt length to avoid context window overflow
DEFAULT_TASK_RETRY_TIMEOUT_SECONDS = 1800


def _resolve_task_subfolder_name(task: Task) -> str:
    if getattr(task, "task_subfolder", None):
        return str(task.task_subfolder)

    title = (task.title or "").strip().lower()
    slug = "".join(char if char.isalnum() else "-" for char in title)
    slug = "-".join(part for part in slug.split("-") if part)
    return f"task-{slug}" if slug else f"task-{task.id}"


def _prepare_task_for_fresh_execution(
    task: Task, clear_saved_plan: bool = False
) -> None:
    """Reset task execution state before a fresh run."""
    task.status = TaskStatus.PENDING
    task.error_message = None
    task.started_at = None
    task.completed_at = None
    task.current_step = 0
    if clear_saved_plan:
        task.steps = None


def _get_active_task_session(db: Session, task_id: int) -> Optional[int]:
    from app.models import Session as SessionModel

    active_session = (
        db.query(SessionTask)
        .join(SessionTask.session)
        .filter(
            SessionTask.task_id == task_id,
            SessionTask.status == TaskStatus.RUNNING,
            SessionModel.deleted_at.is_(None),
            SessionModel.status.in_(["pending", "running", "active"]),
        )
        .order_by(SessionTask.started_at.desc().nullslast(), SessionTask.id.desc())
        .first()
    )
    return active_session.session_id if active_session else None


def _queue_task_retry(
    db: Session,
    task: Task,
    timeout_seconds: int = DEFAULT_TASK_RETRY_TIMEOUT_SECONDS,
) -> dict:
    from app.models import Session as SessionModel
    from app.api.v1.endpoints.sessions import _ensure_unique_session_name
    from app.tasks.worker import execute_orchestration_task
    from app.services.task_service import TaskService

    active_session_id = _get_active_task_session(db, task.id)
    if active_session_id:
        raise HTTPException(
            status_code=409,
            detail=f"Task already has an active session ({active_session_id}). Open the session to resume or stop it first.",
        )

    blocking_tasks = TaskService(db).get_blocking_prior_tasks(task)
    if blocking_tasks:
        blocking_summary = ", ".join(
            f"#{item.plan_position} {item.title} ({item.status.value})"
            for item in blocking_tasks[:3]
        )
        raise HTTPException(
            status_code=409,
            detail=(
                "Earlier ordered tasks must finish before retrying this one. "
                f"Blocked by: {blocking_summary}"
            ),
        )

    prompt = (task.description or task.title or "").strip()
    if not prompt:
        raise HTTPException(
            status_code=400, detail="Task is missing a description or title to execute"
        )

    new_session = SessionModel(
        name=_ensure_unique_session_name(
            db,
            task.project_id,
            humanize_display_name(f"{task.title} session"),
        ),
        description=prompt[:500],
        project_id=task.project_id,
        status="pending",
        default_execution_profile=getattr(task, "execution_profile", "full_lifecycle"),
        is_active=False,
        instance_id=f"orchestrator-task-{task.id}-{int(time.time())}",
    )
    db.add(new_session)
    db.flush()

    session_task = SessionTask(
        session_id=new_session.id,
        task_id=task.id,
        status=TaskStatus.PENDING,
        started_at=None,
    )
    db.add(session_task)

    should_clear_saved_plan = task.status in (
        TaskStatus.DONE,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    )
    _prepare_task_for_fresh_execution(task, clear_saved_plan=should_clear_saved_plan)
    task_workspace = ensure_task_workspace(db, new_session, task.id)

    try:
        result = execute_orchestration_task.delay(
            session_id=new_session.id,
            task_id=task.id,
            prompt=prompt,
            timeout_seconds=timeout_seconds,
            expected_session_instance_id=new_session.instance_id,
        )
    except Exception:
        db.rollback()
        raise

    new_session.status = "running"
    new_session.is_active = True
    new_session.started_at = datetime.now(UTC)

    db.add(
        LogEntry(
            session_id=new_session.id,
            session_instance_id=new_session.instance_id,
            task_id=task.id,
            level="INFO",
            message=f"Task queued: {task.title}",
            log_metadata=json.dumps(
                {
                    "celery_task_id": result.id,
                    "retry": True,
                    "cleared_saved_plan": should_clear_saved_plan,
                }
            ),
        )
    )
    db.add(
        LogEntry(
            session_id=new_session.id,
            session_instance_id=new_session.instance_id,
            task_id=task.id,
            level="INFO",
            message=f"Session started: {new_session.name}",
        )
    )
    db.commit()
    append_orchestration_event(
        project_dir=task_workspace["workspace_path"],
        session_id=new_session.id,
        task_id=task.id,
        event_type=EventType.TASK_QUEUED,
        details={
            "session_instance_id": new_session.instance_id,
            "celery_task_id": result.id,
            "project_dir": task_workspace["workspace_path"],
            "backend": get_effective_agent_backend(
                settings.ORCHESTRATOR_AGENT_BACKEND, db=db
            ),
            "model_family": get_effective_agent_model_family(
                settings.ORCHESTRATOR_AGENT_MODEL_FAMILY, db=db
            ),
        },
    )

    return {
        "status": "started",
        "task_id": task.id,
        "session_id": new_session.id,
        "celery_task_id": result.id,
        "message": f"Task '{task.title}' restarted successfully",
    }


@router.get("/tasks", response_model=List[TaskResponse])
def get_all_tasks(
    skip: int = 0,
    limit: int = 100,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
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
    task_service = TaskService(db)
    changed = False
    for task in tasks:
        changed = task_service.sync_workspace_status(task, commit=False) or changed
    if changed:
        db.commit()
    return tasks


@router.post("/tasks", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
def create_task(
    task: TaskCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Create a new task"""
    # Verify project exists
    project = db.query(Project).filter(Project.id == task.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    task_data = task.model_dump()
    task_data["title"] = humanize_display_name(task_data.get("title", ""))
    db_task = Task(**task_data, status=TaskStatus.PENDING)
    db.add(db_task)
    db.commit()
    db.refresh(db_task)
    return db_task


@router.get("/projects/{project_id}/tasks", response_model=List[TaskResponse])
def get_project_tasks(
    project_id: int,
    skip: int = 0,
    limit: int = 100,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Get all tasks for a project"""
    # Verify project exists
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    task_service = TaskService(db)
    tasks = task_service.get_project_tasks(project_id)[skip : skip + limit]
    task_ids = [task.id for task in tasks]
    latest_session_links: dict[int, int] = {}
    if task_ids:
        session_links = (
            db.query(SessionTask)
            .filter(SessionTask.task_id.in_(task_ids))
            .order_by(
                SessionTask.task_id.asc(),
                SessionTask.started_at.desc().nullslast(),
                SessionTask.completed_at.desc().nullslast(),
                SessionTask.id.desc(),
            )
            .all()
        )
        for link in session_links:
            latest_session_links.setdefault(link.task_id, link.session_id)

    response_tasks = []
    for task in tasks:
        task_dict = task.__dict__.copy()
        task_dict["session_id"] = latest_session_links.get(task.id)
        response_tasks.append(task_dict)
    return response_tasks


@router.post("/tasks/{task_id}/execute")
async def execute_task_with_runtime(
    task_id: int,
    request: Request,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """
    Execute a task through the active runtime with real-time log streaming

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
        from app.services.workspace.overwrite_protection_service import (
            OverwriteProtectionService,
            OverwriteProtectionError,
        )

        protection = OverwriteProtectionService(db)

        if not task.project:
            raise HTTPException(status_code=404, detail="Project not found")

        overwrite_warning = ""
        try:
            overwrite_result = protection.check_and_warn(
                project_id=task.project.id,
                task_subfolder=_resolve_task_subfolder_name(task),
                planned_files=[],
                action="warn",
            )
            if not overwrite_result["safe_to_proceed"]:
                warning_message = overwrite_result.get("warning_message") or ""
                logger.warning(
                    "Overwrite warning for task %s: %s",
                    task.id,
                    warning_message[:200],
                )
                if warning_message:
                    overwrite_warning = (
                        "\n\n### EXISTING WORKSPACE WARNING:\n" + warning_message
                    )
        except OverwriteProtectionError as exc:
            logger.warning(
                "Overwrite check failed for task %s (continuing): %s",
                task.id,
                str(exc)[:200],
            )

        from app.models import Session as SessionModel

        new_session = SessionModel(
            name=f"Task {task_id} Execution",
            description=prompt[:500],
            project_id=task.project_id if task.project else None,
            status="pending",
            is_active=False,
            instance_id=f"orchestrator-task-{task_id}-{int(time.time())}",
        )
        db.add(new_session)
        db.flush()

        session_task = SessionTask(
            session_id=new_session.id,
            task_id=task.id,
        )
        db.add(session_task)

        runtime = create_agent_runtime(db, new_session.id, task_id)
        try:
            await runtime.create_session(prompt)
        except Exception:
            db.rollback()
            raise

        new_session.status = "running"
        new_session.is_active = True
        new_session.started_at = datetime.now(UTC)
        db.commit()
        db.refresh(new_session)

        logger.info("Created session %s for task %s", new_session.id, task.id)

        from app.services.prompt_templates import PromptTemplates

        prompt_text = PromptTemplates.build_task_prompt(
            task_description=prompt + overwrite_warning,
            project_context=f"Project: {task.project.name if task.project else 'Unknown'} at {task.project.workspace_path if task.project and task.project.workspace_path else '/workspace'}",
        )
        prompt_text = render_adapted_runtime_prompt(
            db,
            objective="Execute the requested task through the active runtime.",
            execution_mode="direct_task_execution",
            prompt_body=prompt_text,
            instructions=[
                "Use the current workspace as the source of truth.",
                "Return a direct execution result for the requested task.",
            ],
            context={
                "Task ID": task.id,
                "Project ID": task.project_id,
            },
            expected_output="Execution result text or structured completion payload.",
        )

        actual_timeout = max(timeout_seconds, 600)

        result = await runtime.execute_task(
            prompt=prompt_text,
            timeout_seconds=actual_timeout,
        )

        # Update task status
        if result["status"] == "completed":
            task.status = TaskStatus.DONE
            task.error_message = None
        else:
            task.status = TaskStatus.FAILED
            task.error_message = result.get("error", "Unknown error")

        db.commit()
        db.refresh(task)

        return result

    except Exception as e:
        error_handler = EnhancedErrorHandler()
        recovery_plan = error_handler.create_error_recovery_plan(e, "task_execution")

        error_msg = f"Task execution failed: {str(e)}"
        import traceback

        error_details = traceback.format_exc()
        logger.exception(
            "Error executing task %s: %s\n%s", task_id, error_msg, error_details
        )

        if task:
            task.status = TaskStatus.FAILED
            task.error_message = f"{error_msg}\nRecommended action: {recovery_plan.get('recommended_action', 'manual_intervention')}"
            db.commit()
        raise HTTPException(status_code=500, detail=error_msg)


# Backward-compatible alias for older imports/tests during the rename period.
execute_task_with_openclaw = execute_task_with_runtime


@router.get("/tasks/{task_id}", response_model=TaskResponse)
def get_task(
    task_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Get a task by ID"""
    task_service = TaskService(db)
    task = task_service.get_task(task_id)
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


@router.get("/projects/{project_id}/workspace-overview")
def get_project_workspace_overview(project_id: int, db: Session = Depends(get_db)):
    """Summarize task workspace promotion state for a project."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    task_service = TaskService(db)
    tasks = task_service.get_project_tasks(project_id)
    baseline = task_service.get_project_baseline_overview(project)
    counts: Dict[str, int] = {}
    for task in tasks:
        key = getattr(task, "workspace_status", None) or "not_created"
        counts[key] = counts.get(key, 0) + 1

    promoted_tasks = [
        {
            "id": task.id,
            "title": task.title,
            "plan_position": task.plan_position,
            "workspace_status": task.workspace_status,
            "task_subfolder": task.task_subfolder,
            "promoted_at": (
                task.promoted_at.isoformat()
                if getattr(task, "promoted_at", None)
                else None
            ),
        }
        for task in tasks
        if getattr(task, "workspace_status", None) == "promoted"
    ]

    return {
        "project_id": project_id,
        "project_name": project.name,
        "counts": counts,
        "baseline": baseline,
        "promoted_tasks": promoted_tasks,
        "ready_task_ids": [
            task.id
            for task in tasks
            if getattr(task, "workspace_status", None) == "ready"
        ],
    }


@router.post("/tasks/{task_id}/retry")
def retry_task(task_id: int, db: Session = Depends(get_db)):
    """Queue a fresh execution for a failed or timed-out task."""
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.status == TaskStatus.RUNNING:
        raise HTTPException(
            status_code=409,
            detail="Task is already running. Open the linked session to monitor it.",
        )

    return _queue_task_retry(db, task)


@router.post("/tasks/{task_id}/promote", response_model=TaskResponse)
def promote_task_workspace(
    task_id: int,
    payload: TaskPromotionRequest,
    db: Session = Depends(get_db),
):
    """Mark a task workspace as reviewed and promoted into the project baseline."""
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if task.status != TaskStatus.DONE:
        raise HTTPException(
            status_code=409, detail="Only completed tasks can be promoted"
        )
    if not task.task_subfolder:
        raise HTTPException(
            status_code=409, detail="Task has no workspace folder to promote"
        )

    project = db.query(Project).filter(Project.id == task.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    task.workspace_status = "promoted"
    task.promoted_at = datetime.utcnow()
    task.promotion_note = (payload.note or "").strip() or None
    task.updated_at = datetime.utcnow()
    baseline_result = TaskService(db).promote_task_into_baseline(project, task)
    db.commit()
    db.refresh(task)
    db.add(
        LogEntry(
            task_id=task.id,
            level="INFO",
            message=(
                f"Workspace promoted into project baseline ({baseline_result['files_copied']} files copied)"
            ),
        )
    )
    db.commit()
    return task


@router.post("/tasks/{task_id}/request-changes", response_model=TaskResponse)
def request_task_workspace_changes(
    task_id: int,
    payload: TaskPromotionRequest,
    db: Session = Depends(get_db),
):
    """Mark a completed task workspace as needing follow-up before promotion."""
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if not task.task_subfolder:
        raise HTTPException(
            status_code=409, detail="Task has no workspace folder to review"
        )

    note = (payload.note or "").strip()
    if not note:
        raise HTTPException(
            status_code=400, detail="A review note is required when requesting changes"
        )

    task.workspace_status = "changes_requested"
    task.promoted_at = None
    task.promotion_note = note
    task.updated_at = datetime.utcnow()
    db.commit()
    db.refresh(task)
    return task


@router.put("/tasks/{task_id}", response_model=TaskResponse)
def update_task(
    task_id: int,
    task_update: TaskUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Update a task"""
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    update_data = task_update.model_dump(exclude_unset=True)
    if not update_data:
        raise HTTPException(status_code=400, detail="No task fields provided")
    if "title" in update_data and update_data["title"] is not None:
        update_data["title"] = humanize_display_name(update_data["title"])

    editable_fields = {
        "title",
        "description",
        "status",
        "priority",
        "steps",
        "current_step",
        "error_message",
    }

    unsupported_fields = sorted(set(update_data) - editable_fields)
    if unsupported_fields:
        raise HTTPException(
            status_code=400,
            detail=f"Unsupported fields: {unsupported_fields}",
        )

    for field, value in update_data.items():
        if field in {"description", "steps"} and value == "":
            value = None
        setattr(task, field, value)

    task.updated_at = datetime.utcnow()

    db.commit()
    db.refresh(task)
    return task


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(
    task_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
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
    current_user=Depends(get_current_active_user),
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
        from app.services.workspace.overwrite_protection_service import (
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
        from app.services.workspace.overwrite_protection_service import (
            OverwriteProtectionService,
        )

        protection = OverwriteProtectionService(db)

        backup_result = protection.create_backup_of_existing(
            project_id=task.project.id if task.project else 1,
            task_subfolder=_resolve_task_subfolder_name(task),
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
        from app.services.workspace.overwrite_protection_service import (
            OverwriteProtectionService,
        )

        protection = OverwriteProtectionService(db)

        workspace_info = protection.check_workspace_exists(
            project_id=task.project.id if task.project else 1,
            task_subfolder=_resolve_task_subfolder_name(task),
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
