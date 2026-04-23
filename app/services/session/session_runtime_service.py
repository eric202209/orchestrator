"""Session runtime and task queue helpers."""

from __future__ import annotations

import json
import re
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import LogEntry, Session as SessionModel, SessionTask, Task, TaskStatus
from app.services.orchestration.task_rules import (
    should_execute_in_canonical_project_root,
)
from app.services.workspace.project_isolation_service import (
    resolve_project_workspace_path,
)
from app.services.prompt_templates import OrchestrationState
from app.services.task_service import TaskService


DEFAULT_ORCHESTRATION_TIMEOUT_SECONDS = 1800


def slugify_task_name(name: str) -> str:
    """Convert task titles into stable folder names."""
    if not name:
        return "task"

    slug = name.lower()
    slug = re.sub(r"[\s_]+", "-", slug)
    slug = re.sub(r"[^a-z0-9-]", "", slug)
    slug = re.sub(r"-+", "-", slug).strip("-")
    return slug or "task"


def ensure_unique_session_name(
    db: Session, project_id: int, desired_name: str, session_id: Optional[int] = None
) -> str:
    """Generate a session name that stays unique even with soft-deleted rows."""
    base_name = (desired_name or "session").strip() or "session"
    candidate = base_name
    suffix = 2
    while True:
        query = db.query(SessionModel).filter(
            SessionModel.project_id == project_id,
            SessionModel.name == candidate,
        )
        if session_id is not None:
            query = query.filter(SessionModel.id != session_id)
        existing = query.first()
        if not existing:
            return candidate
        candidate = f"{base_name}-{suffix}"
        suffix += 1


def build_task_subfolder_name(title: str, task_id: int) -> str:
    slug = slugify_task_name(title)
    return f"task-{slug}" if slug else f"task-{task_id}"


def prepare_task_for_fresh_execution(
    task: Task, clear_saved_plan: bool = False
) -> None:
    """Reset task execution state before a fresh manual/automatic rerun."""
    task.status = TaskStatus.RUNNING
    task.started_at = datetime.utcnow()
    task.completed_at = None
    task.error_message = None
    task.current_step = 0
    if clear_saved_plan:
        task.steps = None


def ensure_task_workspace(
    db: Session, session: SessionModel, task_id: int
) -> Dict[str, str]:
    """Ensure a selected task has a subfolder and workspace on disk."""
    from app.models import Project

    task = (
        db.query(Task)
        .filter(Task.id == task_id, Task.project_id == session.project_id)
        .first()
    )
    if not task:
        raise HTTPException(status_code=404, detail="Task not found for this session")

    project = (
        db.query(Project)
        .filter(Project.id == session.project_id, Project.deleted_at.is_(None))
        .first()
    )
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
        base_subfolder = build_task_subfolder_name(task.title, task.id)
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

    if should_execute_in_canonical_project_root(
        task,
        getattr(task, "execution_profile", None),
        task.title,
        task.description,
    ):
        workspace_path = Path(
            resolve_project_workspace_path(project.workspace_path, project.name)
        )
        orchestration_state._project_dir_override = str(workspace_path)
    else:
        workspace_path = Path(orchestration_state.project_dir)
    workspace_path.mkdir(parents=True, exist_ok=True)

    return {
        "task_subfolder": task.task_subfolder,
        "workspace_path": str(workspace_path),
    }


def get_session_celery_task_ids(db: Session, session_id: int) -> List[str]:
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


def get_session_task_subfolder(db: Session, session: SessionModel) -> str:
    """Resolve the active task subfolder for a session."""
    session_task = (
        db.query(SessionTask)
        .filter(SessionTask.session_id == session.id)
        .order_by(SessionTask.started_at.desc().nullslast(), SessionTask.id.desc())
        .first()
    )

    if session_task:
        task = db.query(Task).filter(Task.id == session_task.task_id).first()
        if task:
            workspace = ensure_task_workspace(db, session, task.id)
            return workspace["task_subfolder"]

    return f"task_{session.id}"


def set_session_alert(
    db: Session,
    session: SessionModel,
    level: Optional[str] = None,
    message: Optional[str] = None,
) -> None:
    session.last_alert_level = level
    session.last_alert_message = message
    session.last_alert_at = datetime.utcnow() if message else None
    db.flush()


def build_task_execution_prompt(task: Task) -> str:
    """Build the runtime prompt for a task, preserving recovery context when needed."""
    base_prompt = (task.description or task.title or "").strip()
    if not base_prompt:
        return ""

    workspace_status = getattr(task, "workspace_status", None)
    prior_error = (getattr(task, "error_message", None) or "").strip()
    if workspace_status != "changes_requested" or not prior_error:
        return base_prompt

    return (
        f"{base_prompt}\n\n"
        "Recovery instructions:\n"
        "- The previous execution did not complete successfully.\n"
        "- First inspect the real current workspace, tests, fixtures, and configs before proposing new structure.\n"
        "- Diagnose and fix the underlying mistake or bug instead of repeating the same plan.\n"
        "- Reuse existing files when present and treat them as the source of truth.\n"
        f"- Previous failure details: {prior_error[:1800]}"
    )


def queue_task_for_session(
    db: Session,
    session: SessionModel,
    task_id: int,
    timeout_seconds: int = DEFAULT_ORCHESTRATION_TIMEOUT_SECONDS,
) -> Dict[str, Any]:
    from app.tasks.worker import execute_orchestration_task

    task = (
        db.query(Task)
        .filter(Task.id == task_id, Task.project_id == session.project_id)
        .first()
    )
    if not task:
        raise HTTPException(status_code=404, detail="Task not found for this session")

    blocking_tasks = TaskService(db).get_blocking_prior_tasks(task)
    if blocking_tasks:
        blocking_summary = ", ".join(
            f"#{item.plan_position} {item.title} ({item.status.value})"
            for item in blocking_tasks[:3]
        )
        raise HTTPException(
            status_code=409,
            detail=(
                "This task is blocked by earlier ordered work. "
                f"Finish these first: {blocking_summary}"
            ),
        )

    task_workspace = ensure_task_workspace(db, session, task.id)
    session_task_link = (
        db.query(SessionTask)
        .filter(SessionTask.session_id == session.id, SessionTask.task_id == task.id)
        .first()
    )
    if not session_task_link:
        session_task_link = SessionTask(
            session_id=session.id,
            task_id=task.id,
            status=TaskStatus.RUNNING,
            started_at=datetime.utcnow(),
        )
        db.add(session_task_link)
    else:
        session_task_link.status = TaskStatus.RUNNING
        session_task_link.started_at = datetime.utcnow()
        session_task_link.completed_at = None

    prior_status = task.status
    should_clear_saved_plan = prior_status in (
        TaskStatus.DONE,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    )
    task_prompt = build_task_execution_prompt(task)
    prepare_task_for_fresh_execution(task, clear_saved_plan=should_clear_saved_plan)

    session.status = "running"
    session.is_active = True
    if not session.started_at:
        session.started_at = datetime.utcnow()
    set_session_alert(db, session, None, None)

    result = execute_orchestration_task.delay(
        session_id=session.id,
        task_id=task.id,
        prompt=task_prompt,
        timeout_seconds=timeout_seconds,
    )

    db.add(
        LogEntry(
            session_id=session.id,
            session_instance_id=session.instance_id,
            task_id=task.id,
            level="INFO",
            message=f"Queued task {task.id}: {task.title}",
            log_metadata=json.dumps(
                {
                    "celery_task_id": result.id,
                    "task_workspace": task_workspace["workspace_path"],
                    "plan_position": getattr(task, "plan_position", None),
                    "execution_mode": session.execution_mode,
                    "cleared_saved_plan": should_clear_saved_plan,
                }
            ),
        )
    )
    db.commit()

    return {
        "task_id": task.id,
        "task_name": task.title,
        "celery_id": result.id,
        "plan_position": getattr(task, "plan_position", None),
    }


def reopen_failed_ordered_task_if_needed(
    db: Session, session: SessionModel
) -> Optional[Dict[str, Any]]:
    """Reopen the earliest failed/cancelled ordered task when automatic flow is blocked."""
    if session.execution_mode != "automatic" or not session.project_id:
        return None

    task_service = TaskService(db)
    if task_service.get_next_pending_task(session.project_id):
        return None

    retryable_task = (
        db.query(Task)
        .filter(
            Task.project_id == session.project_id,
            Task.status.in_([TaskStatus.FAILED, TaskStatus.CANCELLED]),
        )
        .order_by(
            Task.plan_position.asc().nullslast(),
            Task.priority.desc(),
            Task.created_at.asc().nullslast(),
            Task.id.asc(),
        )
        .first()
    )
    if not retryable_task:
        return None

    retryable_task.status = TaskStatus.PENDING
    retryable_task.error_message = None
    retryable_task.started_at = None
    retryable_task.completed_at = None
    retryable_task.current_step = 0
    retryable_task.steps = None

    session_link = (
        db.query(SessionTask)
        .filter(
            SessionTask.session_id == session.id,
            SessionTask.task_id == retryable_task.id,
        )
        .order_by(SessionTask.id.desc())
        .first()
    )
    if session_link:
        session_link.status = TaskStatus.PENDING
        session_link.started_at = None
        session_link.completed_at = None

    db.add(
        LogEntry(
            session_id=session.id,
            session_instance_id=session.instance_id,
            task_id=retryable_task.id,
            level="INFO",
            message=(
                "Recovered earliest failed/cancelled ordered task for automatic retry: "
                f"#{getattr(retryable_task, 'plan_position', None)} {retryable_task.title}"
            ),
        )
    )
    db.commit()

    return {
        "task_id": retryable_task.id,
        "task_name": retryable_task.title,
        "plan_position": getattr(retryable_task, "plan_position", None),
    }


def maybe_queue_next_automatic_task(
    db: Session,
    session: SessionModel,
    timeout_seconds: int = DEFAULT_ORCHESTRATION_TIMEOUT_SECONDS,
) -> Optional[Dict[str, Any]]:
    if session.execution_mode != "automatic" or not session.project_id:
        return None

    running_task = (
        db.query(Task)
        .join(SessionTask, SessionTask.task_id == Task.id)
        .filter(
            SessionTask.session_id == session.id,
            SessionTask.status == TaskStatus.RUNNING,
            Task.status == TaskStatus.RUNNING,
        )
        .first()
    )
    if running_task:
        return None

    task_service = TaskService(db)
    next_task = task_service.get_next_pending_task(session.project_id)
    if not next_task:
        recovered_task = reopen_failed_ordered_task_if_needed(db, session)
        if recovered_task:
            next_task = task_service.get_next_pending_task(session.project_id)

    if not next_task:
        pending_blocked_task = (
            db.query(Task)
            .filter(
                Task.project_id == session.project_id, Task.status == TaskStatus.PENDING
            )
            .order_by(
                Task.plan_position.asc().nullslast(),
                Task.priority.desc(),
                Task.created_at.asc().nullslast(),
                Task.id.asc(),
            )
            .first()
        )
        if pending_blocked_task:
            session.status = "paused"
            session.is_active = False
            blocked_by = task_service.get_blocking_prior_tasks(pending_blocked_task)
            if blocked_by:
                blocking_summary = ", ".join(
                    f"#{item.plan_position} {item.title} ({item.status.value})"
                    for item in blocked_by[:3]
                )
                set_session_alert(
                    db,
                    session,
                    "warning",
                    (
                        "Automatic execution is paused because an earlier ordered task "
                        f"is incomplete: {blocking_summary}"
                    )[:2000],
                )
        else:
            session.status = "stopped"
            session.is_active = False
        db.commit()
        return None

    return queue_task_for_session(
        db=db,
        session=session,
        task_id=next_task.id,
        timeout_seconds=timeout_seconds,
    )


def revoke_session_celery_tasks(
    db: Session, session_id: int, terminate: bool = True
) -> List[str]:
    """Revoke all known Celery tasks for a session."""
    from app.celery_app import celery_app

    revoked_ids: List[str] = []
    for celery_task_id in get_session_celery_task_ids(db, session_id):
        celery_app.control.revoke(
            celery_task_id,
            terminate=terminate,
            signal="SIGTERM",
        )
        revoked_ids.append(celery_task_id)
    return revoked_ids
