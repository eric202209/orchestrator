"""Tasks API endpoints"""

import logging
import json
import time
import uuid
from datetime import UTC, datetime
from typing import Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, status, Request
from pydantic import BaseModel, Field
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.database import get_db
from app.models import Task, TaskStatus, Project, LogEntry, SessionTask, TaskExecution
from app.schemas import TaskCreate, TaskUpdate, TaskResponse, TaskPromotionRequest
from app.dependencies import get_current_active_user
from app.services.agents.agent_runtime import create_agent_runtime
from app.services.error_handler import EnhancedErrorHandler
from app.services.log_utils import sort_logs
from app.services.name_formatter import humanize_display_name
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.execution.runtime import workspace_snapshot_key
from app.services.orchestration.review_policy import build_operator_override_metadata
from app.services.orchestration.state.persistence import append_orchestration_event
from app.services.orchestration.context.assembly import render_adapted_runtime_prompt
from app.services.orchestration.run_state import (
    mark_task_attempt_done,
    mark_task_attempt_failed,
    mark_task_attempt_pending,
    mark_task_attempt_running,
)
from app.services.orchestration.state.session_state import (
    mark_session_running,
    mark_session_stopped,
)
from app.services.authz import project_access_filter
from app.services.session.session_runtime_service import ensure_task_workspace
from app.services.task_execution_service import (
    create_task_execution,
)
from app.services.task_service import TaskService
from app.services.workspace.system_settings import (
    get_effective_agent_backend,
    get_effective_agent_model_family,
    get_effective_workspace_review_policy,
)
from app.services.workspace.project_mutation_lock import ProjectMutationLockError
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


class TaskRetryRequest(BaseModel):
    session_id: Optional[int] = None
    execution_scope: Optional[str] = None
    create_new_session: bool = False


class TaskChangeSetRejectRequest(BaseModel):
    task_execution_id: Optional[int] = None
    note: Optional[str] = None


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
    mark_task_attempt_pending(
        task=task,
        reset_started_at=True,
        reset_steps=clear_saved_plan,
        error_message=None,
    )


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


def _operator_identifier(current_user) -> Optional[str]:
    if not current_user:
        return None
    email = (getattr(current_user, "email", None) or "").strip()
    if email:
        return email
    user_id = getattr(current_user, "id", None)
    return f"user:{user_id}" if user_id is not None else None


def _build_request_changes_repair_prompt(
    *,
    task: Task,
    base_prompt: str,
    change_set: Optional[dict],
) -> str:
    note = (getattr(task, "promotion_note", None) or "").strip()
    if not note:
        return base_prompt

    sections = [
        base_prompt.strip(),
        "",
        "Operator requested changes before this task can be accepted.",
        f"Request changes note: {note}",
    ]
    payload = (change_set or {}).get("change_set") or {}
    if payload:
        sections.append("")
        sections.append("Latest deterministic workspace change set:")
        sections.append(
            "- counts: "
            f"added={int(payload.get('added_count') or 0)}, "
            f"modified={int(payload.get('modified_count') or 0)}, "
            f"deleted={int(payload.get('deleted_count') or 0)}"
        )
        warning_flags = payload.get("warning_flags") or []
        if warning_flags:
            sections.append(
                "- warning flags: " + ", ".join(map(str, warning_flags[:8]))
            )
        for label, key in (
            ("added", "added_files"),
            ("modified", "modified_files"),
            ("deleted", "deleted_files"),
        ):
            files = [str(item) for item in payload.get(key, [])[:12]]
            if files:
                sections.append(f"- {label} files: " + ", ".join(files))
    sections.extend(
        [
            "",
            "Repair instructions:",
            "- Address the operator note directly.",
            "- Preserve accepted existing project files unless the note requires a change.",
            "- Do not recreate unrelated scaffolding or parallel task folders.",
            "- Verify the repaired result before finishing.",
        ]
    )
    return "\n".join(sections).strip()


def _change_set_has_changes(change_set: Optional[dict]) -> bool:
    payload = (change_set or {}).get("change_set") or change_set or {}
    return int(payload.get("changed_count") or 0) > 0


def _validate_task_execution_for_change_set(
    db: Session,
    *,
    task: Task,
    task_execution_id: int,
) -> TaskExecution:
    task_execution = (
        db.query(TaskExecution).filter(TaskExecution.id == task_execution_id).first()
    )
    if not task_execution:
        raise HTTPException(status_code=404, detail="Task execution not found")
    if task_execution.task_id != task.id:
        raise HTTPException(
            status_code=409,
            detail="Task execution belongs to a different task",
        )
    return task_execution


def _queue_task_retry(
    db: Session,
    task: Task,
    retry_request: Optional[TaskRetryRequest] = None,
    timeout_seconds: int = DEFAULT_TASK_RETRY_TIMEOUT_SECONDS,
) -> dict:
    from app.models import Session as SessionModel
    from app.api.v1.endpoints.sessions import _ensure_unique_session_name
    from app.tasks.worker import execute_orchestration_task
    from app.services.task_service import TaskService

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

    retry_request = retry_request or TaskRetryRequest()
    explicit_new_session = (
        retry_request.create_new_session
        or str(retry_request.execution_scope or "").lower() == "new_session"
    )

    if explicit_new_session:
        selected_session = SessionModel(
            name=_ensure_unique_session_name(
                db,
                task.project_id,
                humanize_display_name(f"{task.title} session"),
            ),
            description=prompt[:500],
            project_id=task.project_id,
            status="pending",
            default_execution_profile=getattr(
                task, "execution_profile", "full_lifecycle"
            ),
            is_active=False,
            instance_id=f"orchestrator-task-{task.id}-{int(time.time())}",
        )
        db.add(selected_session)
    elif retry_request.session_id is not None:
        selected_session = (
            db.query(SessionModel)
            .filter(
                SessionModel.id == retry_request.session_id,
                SessionModel.project_id == task.project_id,
                SessionModel.deleted_at.is_(None),
            )
            .first()
        )
        if not selected_session:
            raise HTTPException(
                status_code=404,
                detail="Requested retry session was not found for this task project",
            )
    else:
        selected_session = (
            db.query(SessionModel)
            .join(SessionTask, SessionTask.session_id == SessionModel.id)
            .filter(
                SessionTask.task_id == task.id,
                SessionModel.project_id == task.project_id,
                SessionModel.deleted_at.is_(None),
                SessionModel.is_active.is_(True),
            )
            .order_by(
                SessionTask.started_at.desc().nullslast(),
                SessionTask.completed_at.desc().nullslast(),
                SessionTask.id.desc(),
            )
            .first()
        )

        if not selected_session:
            selected_session = (
                db.query(SessionModel)
                .filter(
                    SessionModel.project_id == task.project_id,
                    SessionModel.deleted_at.is_(None),
                    or_(
                        SessionModel.instance_id.is_(None),
                        ~SessionModel.instance_id.like("orchestrator-task-%"),
                    ),
                )
                .order_by(
                    SessionModel.started_at.desc().nullslast(),
                    SessionModel.created_at.desc().nullslast(),
                    SessionModel.id.desc(),
                )
                .first()
            )

        if not selected_session:
            selected_session = SessionModel(
                name=_ensure_unique_session_name(
                    db,
                    task.project_id,
                    "Project workflow",
                ),
                description="Default project workflow session",
                project_id=task.project_id,
                status="pending",
                default_execution_profile=getattr(
                    task, "execution_profile", "full_lifecycle"
                ),
                is_active=False,
                instance_id=str(uuid.uuid4()),
            )
            db.add(selected_session)

    if not selected_session.instance_id:
        selected_session.instance_id = str(uuid.uuid4())

    db.flush()

    session_task = (
        db.query(SessionTask)
        .filter(
            SessionTask.session_id == selected_session.id,
            SessionTask.task_id == task.id,
        )
        .order_by(SessionTask.id.desc())
        .first()
    )
    if not session_task:
        session_task = SessionTask(
            session_id=selected_session.id,
            task_id=task.id,
            status=TaskStatus.PENDING,
            started_at=None,
        )
        db.add(session_task)
    else:
        mark_task_attempt_pending(
            task=None,
            session_task_link=session_task,
            reset_started_at=True,
        )

    task_execution = create_task_execution(
        db,
        session_id=selected_session.id,
        task_id=task.id,
    )

    should_clear_saved_plan = task.status in (
        TaskStatus.DONE,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    )
    latest_change_set = None
    if getattr(task, "workspace_status", None) == "changes_requested":
        latest_change_set = TaskService(db).get_latest_task_change_set_for_task(task.id)
        prompt = _build_request_changes_repair_prompt(
            task=task,
            base_prompt=prompt,
            change_set=latest_change_set,
        )
    _prepare_task_for_fresh_execution(task, clear_saved_plan=should_clear_saved_plan)
    repair_archive_result = None
    if (
        explicit_new_session
        and getattr(task, "workspace_status", None) == "changes_requested"
    ):
        project = db.query(Project).filter(Project.id == task.project_id).first()
        if project:
            repair_archive_result = TaskService(
                db
            ).archive_task_workspace_for_repair_rerun(
                project,
                task,
            )
    task_workspace = ensure_task_workspace(db, selected_session, task.id)

    queued_event = append_orchestration_event(
        project_dir=task_workspace["workspace_path"],
        session_id=selected_session.id,
        task_id=task.id,
        event_type=EventType.TASK_QUEUED,
        details={
            "session_instance_id": selected_session.instance_id,
            "celery_task_id": None,
            "task_execution_id": task_execution.id,
            "project_dir": task_workspace["workspace_path"],
            "backend": get_effective_agent_backend(settings.AGENT_BACKEND, db=db),
            "model_family": get_effective_agent_model_family(
                settings.AGENT_MODEL, db=db
            ),
        },
    )

    mark_session_running(
        selected_session,
        started_at=selected_session.started_at or datetime.now(UTC),
    )

    db.add(
        LogEntry(
            session_id=selected_session.id,
            session_instance_id=selected_session.instance_id,
            task_id=task.id,
            task_execution_id=task_execution.id,
            level="INFO",
            message=f"Task queued: {task.title}",
            log_metadata=json.dumps(
                {
                    "celery_task_id": None,
                    "task_execution_id": task_execution.id,
                    "retry": True,
                    "execution_scope": (
                        "isolated_session"
                        if explicit_new_session
                        else "workflow_session"
                    ),
                    "isolated_session": explicit_new_session,
                    "legacy_isolated_session": explicit_new_session,
                    "cleared_saved_plan": should_clear_saved_plan,
                    "repair_archive_result": repair_archive_result,
                    "request_changes_repair_context": bool(latest_change_set)
                    or bool(getattr(task, "promotion_note", None)),
                }
            ),
        )
    )
    db.add(
        LogEntry(
            session_id=selected_session.id,
            session_instance_id=selected_session.instance_id,
            task_id=task.id,
            task_execution_id=task_execution.id,
            level="INFO",
            message=f"Session started: {selected_session.name}",
        )
    )
    db.commit()

    try:
        result = execute_orchestration_task.delay(
            session_id=selected_session.id,
            task_id=task.id,
            prompt=prompt,
            timeout_seconds=timeout_seconds,
            expected_session_instance_id=selected_session.instance_id,
            task_execution_id=task_execution.id,
            queued_event_id=(queued_event or {}).get("event_id"),
        )
    except Exception:
        mark_session_stopped(selected_session, stopped_at=datetime.now(UTC))
        mark_task_attempt_failed(
            task=task,
            session_task_link=session_task,
            task_execution=task_execution,
            error_message="Failed to dispatch task to worker",
            completed_at=datetime.now(UTC),
        )
        db.add(
            LogEntry(
                session_id=selected_session.id,
                session_instance_id=selected_session.instance_id,
                task_id=task.id,
                task_execution_id=task_execution.id,
                level="ERROR",
                message=f"Failed to dispatch task to worker: {task.title}",
                log_metadata=json.dumps(
                    {
                        "task_execution_id": task_execution.id,
                        "dispatch_failed": True,
                    }
                ),
            )
        )
        db.commit()
        raise

    db.add(
        LogEntry(
            session_id=selected_session.id,
            session_instance_id=selected_session.instance_id,
            task_id=task.id,
            task_execution_id=task_execution.id,
            level="INFO",
            message=f"Celery task dispatched: {task.title}",
            log_metadata=json.dumps(
                {
                    "celery_task_id": result.id,
                    "task_execution_id": task_execution.id,
                    "dispatch_after_commit": True,
                }
            ),
        )
    )
    db.commit()
    return {
        "status": "started",
        "task_id": task.id,
        "session_id": selected_session.id,
        "task_execution_id": task_execution.id,
        "celery_task_id": result.id,
        "execution_scope": (
            "isolated_session" if explicit_new_session else "workflow_session"
        ),
        "isolated_session": explicit_new_session,
        "repair_archive_result": repair_archive_result,
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
    query = (
        db.query(Task)
        .join(Project, Project.id == Task.project_id)
        .filter(Project.deleted_at.is_(None), project_access_filter(db, current_user))
    )

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
        task_execution = create_task_execution(
            db,
            session_id=new_session.id,
            task_id=task.id,
        )

        runtime = create_agent_runtime(db, new_session.id, task_id)
        if hasattr(runtime, "task_execution_id"):
            runtime.task_execution_id = task_execution.id
        try:
            await runtime.create_session(prompt)
        except Exception:
            db.rollback()
            raise

        mark_session_running(new_session, started_at=datetime.now(UTC))
        mark_task_attempt_running(
            task=task,
            session_task_link=session_task,
            task_execution=task_execution,
            started_at=new_session.started_at,
        )
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

        if result["status"] == "completed":
            mark_task_attempt_done(
                task=task,
                session_task_link=session_task,
                task_execution=task_execution,
                completed_at=datetime.now(UTC),
            )
        else:
            mark_task_attempt_failed(
                task=task,
                session_task_link=session_task,
                task_execution=task_execution,
                error_message=result.get("error", "Unknown error"),
                completed_at=datetime.now(UTC),
            )

        db.commit()
        db.refresh(task)

        result["task_execution_id"] = task_execution.id
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
            task_execution = locals().get("task_execution")
            mark_task_attempt_failed(
                task=task,
                session_task_link=locals().get("session_task"),
                task_execution=task_execution,
                error_message=(
                    f"{error_msg}\nRecommended action: "
                    f"{recovery_plan.get('recommended_action', 'manual_intervention')}"
                ),
                completed_at=datetime.now(UTC),
            )
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


@router.get("/tasks/{task_id}/change-set")
def get_latest_task_change_set(task_id: int, db: Session = Depends(get_db)):
    """Return the latest deterministic workspace change set for a task."""
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    task_service = TaskService(db)
    latest_change_set = task_service.get_latest_task_change_set_for_task(task_id)
    if not latest_change_set:
        raise HTTPException(status_code=404, detail="No change set recorded for task")
    change_set = latest_change_set.get("change_set") or {}
    review_decision = latest_change_set.get("review_decision")
    if not review_decision:
        review_decision = task_service.change_set_review_decision(
            change_set,
            workspace_review_policy=get_effective_workspace_review_policy(
                settings.WORKSPACE_REVIEW_POLICY,
                db=db,
            ),
        )
    return {
        "task_id": task_id,
        "task_execution_id": latest_change_set.get("task_execution_id"),
        "change_set": change_set,
        "review_decision": review_decision,
        "recorded_at": latest_change_set.get("recorded_at"),
    }


@router.post("/tasks/{task_id}/change-set/reject")
def reject_latest_task_change_set(
    task_id: int,
    payload: Optional[TaskChangeSetRejectRequest] = None,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Archive candidate files and restore the pre-run snapshot for a task execution."""
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    project = db.query(Project).filter(Project.id == task.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    payload = payload or TaskChangeSetRejectRequest()
    task_execution_id = payload.task_execution_id
    if task_execution_id is None:
        raise HTTPException(
            status_code=400,
            detail="task_execution_id is required to reject and restore a change set",
        )

    _validate_task_execution_for_change_set(
        db,
        task=task,
        task_execution_id=task_execution_id,
    )
    task_service = TaskService(db)
    change_set = task_service.get_task_execution_change_set(
        task_execution_id=task_execution_id
    )
    if not change_set:
        raise HTTPException(
            status_code=404,
            detail="No change set recorded for task_execution_id",
        )
    if change_set.get("task_id") not in {None, task_id}:
        raise HTTPException(
            status_code=409,
            detail="Change set belongs to a different task",
        )
    snapshot_key = (
        str(change_set.get("snapshot_key"))
        if change_set and change_set.get("snapshot_key")
        else workspace_snapshot_key(task_id, task_execution_id)
    )
    try:
        result = task_service.reject_task_execution_change_set(
            project,
            task,
            task_execution_id=task_execution_id,
            snapshot_key=snapshot_key,
            reason=(payload.note or "operator_rejected_change_set").strip()
            or "operator_rejected_change_set",
            operator=_operator_identifier(current_user),
        )
    except ProjectMutationLockError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return result


@router.get("/projects/{project_id}/workspace-overview")
def get_project_workspace_overview(project_id: int, db: Session = Depends(get_db)):
    """Summarize task workspace promotion state for a project."""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    task_service = TaskService(db)
    tasks = task_service.get_project_tasks(project_id)
    baseline = task_service.get_project_baseline_overview(project)
    audit = task_service.audit_project_workspace_shape(project)
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
    pending_change_sets = []
    workspace_review_policy = get_effective_workspace_review_policy(
        settings.WORKSPACE_REVIEW_POLICY,
        db=db,
    )
    for task in tasks:
        latest_change_set = task_service.get_latest_task_change_set_for_task(task.id)
        if not latest_change_set:
            continue
        change_set_payload = latest_change_set.get("change_set") or {}
        if int(change_set_payload.get("changed_count") or 0) <= 0:
            continue
        pending_change_sets.append(
            {
                "task_id": task.id,
                "title": task.title,
                "workspace_status": getattr(task, "workspace_status", None),
                "task_execution_id": latest_change_set.get("task_execution_id"),
                "recorded_at": latest_change_set.get("recorded_at"),
                "change_set": change_set_payload,
                "review_decision": latest_change_set.get("review_decision")
                or task_service.change_set_review_decision(
                    change_set_payload,
                    workspace_review_policy=workspace_review_policy,
                ),
            }
        )

    return {
        "project_id": project_id,
        "project_name": project.name,
        "counts": counts,
        "baseline": baseline,
        "audit": audit,
        "promoted_tasks": promoted_tasks,
        "pending_change_sets": pending_change_sets,
        "ready_task_ids": [
            task.id
            for task in tasks
            if getattr(task, "workspace_status", None) == "ready"
        ],
    }


@router.post("/tasks/{task_id}/retry")
def retry_task(
    task_id: int,
    retry_request: Optional[TaskRetryRequest] = None,
    db: Session = Depends(get_db),
):
    """Queue a fresh execution for a failed or timed-out task."""
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    if task.status == TaskStatus.RUNNING:
        raise HTTPException(
            status_code=409,
            detail="Task is already running. Open the linked session to monitor it.",
        )

    return _queue_task_retry(db, task, retry_request=retry_request)


@router.post("/tasks/{task_id}/promote", response_model=TaskResponse)
def promote_task_workspace(
    task_id: int,
    payload: TaskPromotionRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
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

    task_service = TaskService(db)
    latest_change_set = task_service.get_latest_task_change_set_for_task(task.id)
    if payload.task_execution_id is None and _change_set_has_changes(latest_change_set):
        raise HTTPException(
            status_code=400,
            detail="task_execution_id is required to promote a recorded change set",
        )

    accepted_change_set = None
    if payload.task_execution_id is not None:
        _validate_task_execution_for_change_set(
            db,
            task=task,
            task_execution_id=payload.task_execution_id,
        )
        if (
            _change_set_has_changes(latest_change_set)
            and latest_change_set.get("task_execution_id") != payload.task_execution_id
        ):
            raise HTTPException(
                status_code=409,
                detail="task_execution_id does not match the latest pending change set",
            )
        accepted_change_set = task_service.get_task_execution_change_set(
            task_execution_id=payload.task_execution_id
        )
        if not accepted_change_set:
            raise HTTPException(
                status_code=404,
                detail="No change set recorded for task_execution_id",
            )
        if accepted_change_set.get("task_id") not in {None, task.id}:
            raise HTTPException(
                status_code=409,
                detail="Change set belongs to a different task",
            )
        if accepted_change_set.get("task_execution_id") not in {
            None,
            payload.task_execution_id,
        }:
            raise HTTPException(
                status_code=409,
                detail="Change set task_execution_id does not match request",
            )

    task.workspace_status = "promoted"
    task.promoted_at = datetime.now(UTC)
    task.promotion_note = (payload.note or "").strip() or None
    task.updated_at = datetime.now(UTC)
    try:
        baseline_result = task_service.promote_task_into_baseline(project, task)
    except ProjectMutationLockError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    if accepted_change_set:
        baseline_result["accepted_change_set"] = accepted_change_set
        disposition_record = task_service.mark_task_execution_change_set_disposition(
            task_execution_id=payload.task_execution_id,
            disposition="promoted",
            reason=(payload.note or "operator_promoted_change_set").strip()
            or "operator_promoted_change_set",
            metadata=build_operator_override_metadata(
                action="promote",
                reason=(payload.note or "operator_promoted_change_set").strip()
                or "operator_promoted_change_set",
                task_execution_id=payload.task_execution_id,
                change_set=accepted_change_set,
                operator=_operator_identifier(current_user),
                extra={
                    "files_copied": baseline_result.get("files_copied"),
                    "baseline_path": baseline_result.get("baseline_path"),
                },
            ),
            commit=False,
        )
        if disposition_record:
            baseline_result["accepted_change_set"] = (
                task_service.get_task_execution_change_set(
                    task_execution_id=disposition_record.task_execution_id
                )
            )
    promoted_workspace_archive_result = task_service.archive_promoted_task_workspace(
        project, task, reason="manual_promotion"
    )
    db.commit()
    db.refresh(task)
    db.add(
        LogEntry(
            task_id=task.id,
            level="INFO",
            message=(
                f"Workspace promoted into project baseline ({baseline_result['files_copied']} files copied)"
            ),
            log_metadata=json.dumps(
                {
                    "baseline_result": baseline_result,
                    "promoted_workspace_archive_result": (
                        promoted_workspace_archive_result
                    ),
                }
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
    task.updated_at = datetime.now(UTC)
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

    if "current_step" in update_data and update_data["current_step"] is not None:
        requested_step = int(update_data["current_step"])
        if requested_step < 0:
            raise HTTPException(
                status_code=400,
                detail="current_step must be zero or greater",
            )
        existing_step = int(task.current_step or 0)
        if (
            requested_step < existing_step
            and getattr(task, "workspace_status", None) == "promoted"
        ):
            raise HTTPException(
                status_code=409,
                detail=(
                    "Cannot lower current_step on a promoted task workspace. "
                    "Request changes or rerun in a new isolated session instead."
                ),
            )

    for field, value in update_data.items():
        if field in {"description", "steps"} and value == "":
            value = None
        setattr(task, field, value)

    task.updated_at = datetime.now(UTC)

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
