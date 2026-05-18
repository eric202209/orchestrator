"""Session runtime and task queue helpers."""

from __future__ import annotations

import json
import re
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from fastapi import HTTPException
from sqlalchemy.orm import Session

from app.models import LogEntry, Session as SessionModel, SessionTask, Task, TaskStatus
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.state.persistence import append_orchestration_event
from app.services.orchestration.task_rules import (
    should_execute_in_canonical_project_root,
)
from app.services.orchestration.run_state import mark_task_attempt_pending
from app.services.orchestration.state.session_state import (
    clear_session_alert,
    mark_session_paused,
    mark_session_running,
    mark_session_stopped,
)
from app.config import settings
from app.services.workspace.project_isolation_service import (
    resolve_project_workspace_path,
)
from app.services.prompt_templates import OrchestrationState
from app.services.task_service import TaskService
from app.services.workspace.system_settings import (
    get_effective_agent_backend,
    get_effective_agent_model_family,
)
from app.services.task_execution_service import create_task_execution

DEFAULT_ORCHESTRATION_TIMEOUT_SECONDS = 1800
MAX_AUTOMATIC_TASK_RECOVERY_ATTEMPTS = 1


def _utc_now_iso() -> str:
    return datetime.now(UTC).isoformat()


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


def _resolve_task_workspace_path(project_workspace: Path, task_subfolder: str) -> Path:
    """Resolve a task workspace and reject subfolders outside the project."""
    project_root = project_workspace.resolve()
    task_workspace = (project_root / task_subfolder).resolve()
    try:
        task_workspace.relative_to(project_root)
    except ValueError as exc:
        raise HTTPException(
            status_code=400,
            detail="Task subfolder must stay within the project workspace",
        ) from exc
    if task_workspace == project_root:
        raise HTTPException(
            status_code=400,
            detail="Task subfolder must stay within the project workspace",
        )
    return task_workspace


def prepare_task_for_fresh_execution(
    task: Task, clear_saved_plan: bool = False
) -> None:
    """Reset task execution state before a fresh manual/automatic rerun."""
    mark_task_attempt_pending(
        task=task,
        reset_started_at=True,
        reset_steps=clear_saved_plan,
        error_message=None,
    )


def _task_failure_requires_operator_review(task: Task) -> bool:
    failure_text = str(getattr(task, "error_message", "") or "").strip().lower()
    if not failure_text:
        return False

    hard_stop_markers = (
        "completion validation failed",
        "completion repair failed",
        "planning failed",
        "planning circuit breaker opened",
        "workspace contract failed",
        "max step attempts reached",
        "step failed after",
        "repeat_completion_failure_signature",
        "root cause",
    )
    return any(marker in failure_text for marker in hard_stop_markers)


def _count_automatic_recovery_attempts(
    db: Session, session_id: int, task_id: int
) -> int:
    return (
        db.query(LogEntry)
        .filter(
            LogEntry.session_id == session_id,
            LogEntry.task_id == task_id,
            LogEntry.message.like(
                "Recovered earliest failed/cancelled ordered task for automatic retry:%"
            ),
        )
        .count()
    )


def _runtime_selection_details(db: Session) -> Dict[str, Optional[str]]:
    return {
        "backend": get_effective_agent_backend(settings.AGENT_BACKEND, db=db),
        "model_family": get_effective_agent_model_family(settings.AGENT_MODEL, db=db),
    }


def ensure_task_workspace(
    db: Session, session: SessionModel, task_id: int
) -> Dict[str, Optional[str]]:
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

    project_workspace_path = Path(
        resolve_project_workspace_path(project.workspace_path, project.name)
    )
    TaskService(db).ensure_project_gitignore_guard(project)
    orchestration_state._workspace_path_override = str(project_workspace_path)

    if task.task_subfolder:
        _resolve_task_workspace_path(project_workspace_path, task.task_subfolder)
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
        _resolve_task_workspace_path(project_workspace_path, candidate)
        orchestration_state._task_subfolder_override = candidate
        db.flush()

    runs_in_canonical_workspace = should_execute_in_canonical_project_root(
        task,
        getattr(task, "execution_profile", None),
        task.title,
        task.description,
    )
    if runs_in_canonical_workspace:
        workspace_path = project_workspace_path
        orchestration_state._project_dir_override = str(workspace_path)
    else:
        workspace_path = _resolve_task_workspace_path(
            project_workspace_path, task.task_subfolder
        )
    workspace_path.mkdir(parents=True, exist_ok=True)

    return {
        "task_subfolder": None if runs_in_canonical_workspace else task.task_subfolder,
        "stored_task_subfolder": task.task_subfolder,
        "workspace_path": str(workspace_path),
        "workspace_scope": (
            "canonical_project_root"
            if runs_in_canonical_workspace
            else "isolated_task_workspace"
        ),
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
            return str(
                workspace.get("stored_task_subfolder")
                or workspace.get("task_subfolder")
                or f"task_{session.id}"
            )

    return f"task_{session.id}"


def set_session_alert(
    db: Session,
    session: SessionModel,
    level: Optional[str] = None,
    message: Optional[str] = None,
) -> None:
    session.last_alert_level = level
    session.last_alert_message = message
    session.last_alert_at = datetime.now(UTC) if message else None
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

    if task.status == TaskStatus.RUNNING:
        raise HTTPException(
            status_code=409,
            detail=(
                "Task is already running; active execution is in progress. "
                "Open the linked session to monitor it."
            ),
        )

    active_link = (
        db.query(SessionTask)
        .join(SessionModel, SessionTask.session_id == SessionModel.id)
        .filter(
            SessionTask.task_id == task.id,
            SessionTask.status == TaskStatus.RUNNING,
            SessionModel.deleted_at.is_(None),
            SessionModel.status.in_(["pending", "running", "active"]),
        )
        .first()
    )
    if active_link:
        raise HTTPException(
            status_code=409,
            detail=(
                "Task already has active execution in progress. "
                f"Open session {active_link.session_id} to monitor it."
            ),
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
            status=TaskStatus.PENDING,
            started_at=None,
        )
        db.add(session_task_link)
    else:
        mark_task_attempt_pending(
            task=None,
            session_task_link=session_task_link,
            reset_started_at=True,
        )
    task_execution = create_task_execution(
        db,
        session_id=session.id,
        task_id=task.id,
    )

    prior_status = task.status
    should_clear_saved_plan = prior_status in (
        TaskStatus.DONE,
        TaskStatus.FAILED,
        TaskStatus.CANCELLED,
    )
    task_prompt = build_task_execution_prompt(task)
    prepare_task_for_fresh_execution(task, clear_saved_plan=should_clear_saved_plan)

    mark_session_running(session, started_at=session.started_at or datetime.now(UTC))
    clear_session_alert(session)

    event_project_dir = Path(task_workspace["workspace_path"])
    if task_workspace.get("task_subfolder"):
        event_project_dir = event_project_dir / str(task_workspace["task_subfolder"])

    # Write TASK_QUEUED to disk before dispatching so the worker's stale-dispatch
    # check always finds this fresh event instead of a stale one from a prior run.
    queued_event = append_orchestration_event(
        project_dir=event_project_dir,
        session_id=session.id,
        task_id=task.id,
        event_type=EventType.TASK_QUEUED,
        details={
            "session_instance_id": session.instance_id,
            "project_dir": task_workspace["workspace_path"],
            "task_title": task.title,
            "execution_profile": task.execution_profile,
            **_runtime_selection_details(db),
        },
    )
    db.commit()

    result = execute_orchestration_task.delay(
        session_id=session.id,
        task_id=task.id,
        prompt=task_prompt,
        timeout_seconds=timeout_seconds,
        expected_session_instance_id=session.instance_id,
        task_execution_id=task_execution.id,
        queued_event_id=(queued_event or {}).get("event_id"),
    )

    db.add(
        LogEntry(
            session_id=session.id,
            session_instance_id=session.instance_id,
            task_id=task.id,
            task_execution_id=task_execution.id,
            level="INFO",
            message=f"Queued task {task.id}: {task.title}",
            log_metadata=json.dumps(
                {
                    "celery_task_id": result.id,
                    "task_execution_id": task_execution.id,
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
        "task_execution_id": task_execution.id,
        "celery_id": result.id,
        "plan_position": getattr(task, "plan_position", None),
    }


def reopen_failed_ordered_task_if_needed(
    db: Session, session: SessionModel, *, ignore_recovery_budget: bool = False
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

    if _task_failure_requires_operator_review(retryable_task):
        mark_session_paused(
            session,
            alert_level="warning",
            alert_message=(
                "Automatic execution paused because the next failed task needs "
                f"operator review before retry: #{getattr(retryable_task, 'plan_position', None)} "
                f"{retryable_task.title}"
            )[:2000],
        )
        db.commit()
        return None

    prior_recovery_attempts = _count_automatic_recovery_attempts(
        db, session.id, retryable_task.id
    )
    if (
        prior_recovery_attempts >= MAX_AUTOMATIC_TASK_RECOVERY_ATTEMPTS
        and not ignore_recovery_budget
    ):
        mark_session_paused(
            session,
            alert_level="warning",
            alert_message=(
                "Automatic execution paused because the next failed task has "
                "already been retried automatically once and still needs a real fix: "
                f"#{getattr(retryable_task, 'plan_position', None)} {retryable_task.title}"
            )[:2000],
        )
        db.add(
            LogEntry(
                session_id=session.id,
                session_instance_id=session.instance_id,
                task_id=retryable_task.id,
                level="WARN",
                message=(
                    "Skipped automatic retry for failed ordered task because the "
                    "automatic recovery budget was exhausted"
                ),
            )
        )
        db.commit()
        return None

    session_link = (
        db.query(SessionTask)
        .filter(
            SessionTask.session_id == session.id,
            SessionTask.task_id == retryable_task.id,
        )
        .order_by(SessionTask.id.desc())
        .first()
    )
    mark_task_attempt_pending(
        task=retryable_task,
        session_task_link=session_link,
        reset_started_at=True,
        reset_steps=True,
        error_message=None,
    )

    db.add(
        LogEntry(
            session_id=session.id,
            session_instance_id=session.instance_id,
            task_id=retryable_task.id,
            level="INFO",
            message=(
                "Recovered earliest failed/cancelled ordered task for "
                + (
                    "explicit session start retry: "
                    if ignore_recovery_budget
                    else "automatic retry: "
                )
                + f"#{getattr(retryable_task, 'plan_position', None)} {retryable_task.title}"
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
            mark_session_paused(session)
            blocked_by = task_service.get_blocking_prior_tasks(pending_blocked_task)
            if blocked_by:
                blocking_summary = ", ".join(
                    f"#{item.plan_position} {item.title} ({item.status.value})"
                    for item in blocked_by[:3]
                )
                mark_session_paused(
                    session,
                    alert_level="warning",
                    alert_message=(
                        "Automatic execution is paused because an earlier ordered task "
                        f"is incomplete: {blocking_summary}"
                    )[:2000],
                )
        else:
            mark_session_stopped(session)
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
