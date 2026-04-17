"""Mobile API — clawmobile integration via OpenClaw Gateway

These endpoints are called by OpenClaw as tools, not directly by the mobile app.
Access is restricted to OpenClaw/Gateway using a shared API key.

Flow:
  clawmobile → OpenClaw Gateway → OpenClaw agent → these endpoints
"""

import secrets
import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, Request, status
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_active_user
from app.models import LogEntry, Project, Session as SessionModel, SessionTask, Task, TaskStatus, User
from app.services.project_isolation_service import resolve_project_workspace_path
from app.services.system_settings import get_effective_mobile_gateway_key

logger = logging.getLogger(__name__)
TREE_MAX_DEPTH = 3
TREE_MAX_ENTRIES = 120
TREE_EXCLUDED_NAMES = {
    ".git",
    ".openclaw",
    "node_modules",
    "__pycache__",
    ".venv",
    "venv",
    ".idea",
    ".pytest_cache",
}


def _status_value(value: object) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _build_task_counts(tasks: list[Task]) -> dict[str, int]:
    counts = {
        "total": len(tasks),
        "pending": 0,
        "running": 0,
        "done": 0,
        "failed": 0,
    }
    for task in tasks:
        status_value = _status_value(task.status).lower()
        if status_value == TaskStatus.RUNNING.value:
            counts["running"] += 1
        elif status_value == TaskStatus.DONE.value:
            counts["done"] += 1
        elif status_value == TaskStatus.FAILED.value:
            counts["failed"] += 1
        else:
            counts["pending"] += 1
    return counts


def require_mobile_gateway_key(
    request: Request,
    x_openclaw_api_key: str | None = Header(default=None, alias="X-OpenClaw-API-Key"),
    authorization: str | None = Header(default=None, alias="Authorization"),
):
    """
    Require a shared key from OpenClaw/Gateway before exposing orchestration data.

    Accepted headers:
    - X-OpenClaw-API-Key: <key>
    - Authorization: Bearer <key>
    """
    configured_key = settings.MOBILE_GATEWAY_API_KEY or settings.OPENCLAW_API_KEY

    if not configured_key:
        logger.warning(
            "Mobile API request rejected: key not configured path=%s client=%s",
            request.url.path,
            request.client.host if request.client else "unknown",
        )
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Mobile gateway API key is not configured",
        )

    presented_key = x_openclaw_api_key
    if not presented_key and authorization:
        scheme, _, token = authorization.partition(" ")
        if scheme.lower() == "bearer" and token:
            presented_key = token

    if not presented_key or not secrets.compare_digest(presented_key, configured_key):
        logger.warning(
            "Mobile API auth failed path=%s client=%s auth_header=%s api_key_header=%s",
            request.url.path,
            request.client.host if request.client else "unknown",
            bool(authorization),
            bool(x_openclaw_api_key),
        )
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing mobile gateway API key",
            headers={"WWW-Authenticate": "Bearer"},
        )


def _log_mobile_request(request: Request, action: str, **extra: object) -> None:
    client_host = request.client.host if request.client else "unknown"
    details = " ".join(f"{key}={value}" for key, value in extra.items() if value is not None)
    suffix = f" {details}" if details else ""
    logger.info(
        "Mobile API request action=%s path=%s client=%s%s",
        action,
        request.url.path,
        client_host,
        suffix,
    )


def _build_project_tree_lines(
    root: Path, max_depth: int = TREE_MAX_DEPTH, max_entries: int = TREE_MAX_ENTRIES
) -> tuple[list[str], bool]:
    lines: list[str] = []
    truncated = False
    entries_seen = 0

    def walk(path: Path, prefix: str, depth: int) -> None:
        nonlocal truncated, entries_seen
        if truncated or depth >= max_depth:
            return

        try:
            children = [
                child
                for child in sorted(
                    path.iterdir(), key=lambda item: (not item.is_dir(), item.name.lower())
                )
                if child.name not in TREE_EXCLUDED_NAMES
            ]
        except OSError:
            lines.append(f"{prefix}[unreadable]")
            return

        for index, child in enumerate(children):
            if entries_seen >= max_entries:
                truncated = True
                return

            is_last = index == len(children) - 1
            branch = "└── " if is_last else "├── "
            label = f"{child.name}/" if child.is_dir() else child.name
            lines.append(f"{prefix}{branch}{label}")
            entries_seen += 1

            if child.is_dir():
                next_prefix = f"{prefix}{'    ' if is_last else '│   '}"
                walk(child, next_prefix, depth + 1)
                if truncated:
                    return

    walk(root, "", 0)
    return lines, truncated


router = APIRouter(dependencies=[Depends(require_mobile_gateway_key)])
admin_router = APIRouter(prefix="/mobile-admin", tags=["mobile-admin"])


def _get_mobile_shared_key() -> tuple[str, str] | tuple[None, None]:
    return get_effective_mobile_gateway_key(
        settings.MOBILE_GATEWAY_API_KEY, settings.OPENCLAW_API_KEY
    )


def _mask_secret(secret: str | None) -> str | None:
    if not secret:
        return None
    if len(secret) <= 8:
        return "*" * len(secret)
    return f"{secret[:8]}...{secret[-4:]}"


def _derive_mobile_base_url(request: Request) -> str:
    configured = (settings.ORCHESTRATOR_MOBILE_BASE_URL or "").strip().rstrip("/")
    if configured:
        if configured.endswith("/api/v1"):
            return f"{configured}/mobile"
        if configured.endswith("/api/v1/mobile"):
            return configured
        if configured.endswith("/mobile"):
            return configured
        return f"{configured}/api/v1/mobile"

    base_url = str(request.base_url).rstrip("/")
    return f"{base_url}{settings.API_V1_STR}/mobile"


@admin_router.get("/connection-info")
def get_mobile_connection_info(
    request: Request,
    current_user: User = Depends(get_current_active_user),
):
    """Return recommended mobile connection details for authenticated users."""
    shared_key, key_source = _get_mobile_shared_key()
    mobile_base_url = _derive_mobile_base_url(request)
    return {
        "user_email": current_user.email,
        "mobile_base_url": mobile_base_url,
        "dashboard_url": mobile_base_url.removesuffix("/api/v1/mobile"),
        "required_header": "X-OpenClaw-API-Key",
        "authorization_header_supported": True,
        "api_key_configured": bool(shared_key),
        "api_key_preview": _mask_secret(shared_key),
        "api_key_source": key_source,
        "available_endpoints": [
            f"{mobile_base_url}/dashboard",
            f"{mobile_base_url}/projects",
            f"{mobile_base_url}/projects/{{project_id}}/status",
            f"{mobile_base_url}/projects/{{project_id}}/tasks",
            f"{mobile_base_url}/sessions",
            f"{mobile_base_url}/sessions/{{session_id}}/summary",
            f"{mobile_base_url}/sessions/{{session_id}}/checkpoints",
            f"{mobile_base_url}/sessions/{{session_id}}/resume",
            f"{mobile_base_url}/sessions/{{session_id}}/stop",
            f"{mobile_base_url}/tasks/{{task_id}}/retry",
        ],
    }


@admin_router.get("/connection-secret")
def reveal_mobile_connection_secret(
    current_user: User = Depends(get_current_active_user),
):
    """Reveal the configured mobile shared key to an authenticated user."""
    shared_key, key_source = _get_mobile_shared_key()
    if not shared_key:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail="Mobile gateway API key is not configured",
        )

    return {
        "user_email": current_user.email,
        "header_name": "X-OpenClaw-API-Key",
        "api_key": shared_key,
        "api_key_preview": _mask_secret(shared_key),
        "api_key_source": key_source,
    }


# ── Projects ─────────────────────────────────────────────────


@router.get("/mobile/projects")
def list_projects(request: Request, db: Session = Depends(get_db)):
    """List all projects — called by OpenClaw as a tool"""
    _log_mobile_request(request, "list_projects")
    projects = (
        db.query(Project)
        .filter(Project.deleted_at.is_(None))
        .order_by(Project.created_at.desc())
        .all()
    )
    return {
        "projects": [
            {
                "id": p.id,
                "name": p.name,
                "description": p.description,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
            for p in projects
        ],
        "total": len(projects),
    }


@router.get("/mobile/projects/{project_id}/status")
def get_project_status(
    project_id: int, request: Request, db: Session = Depends(get_db)
):
    """Get project status including active and recent sessions plus tasks."""
    _log_mobile_request(request, "project_status", project_id=project_id)
    project = (
        db.query(Project)
        .filter(Project.id == project_id, Project.deleted_at.is_(None))
        .first()
    )
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Get active sessions
    active_sessions = (
        db.query(SessionModel)
        .filter(
            SessionModel.project_id == project_id,
            SessionModel.is_active,
            SessionModel.deleted_at.is_(None),
        )
        .all()
    )

    recent_sessions = (
        db.query(SessionModel)
        .filter(
            SessionModel.project_id == project_id,
            SessionModel.deleted_at.is_(None),
        )
        .order_by(SessionModel.started_at.desc().nullslast(), SessionModel.id.desc())
        .limit(8)
        .all()
    )

    # Get task stats
    tasks = db.query(Task).filter(Task.project_id == project_id).all()
    task_stats = _build_task_counts(tasks)

    return {
        "project_id": project_id,
        "project_name": project.name,
        "description": project.description,
        "active_sessions": len(active_sessions),
        "recent_sessions": len(recent_sessions),
        "tasks": task_stats,
        "sessions": [
            {
                "id": s.id,
                "name": s.name,
                "status": s.status,
                "is_active": bool(s.is_active),
                "started_at": s.started_at.isoformat() if s.started_at else None,
            }
            for s in recent_sessions
        ],
    }


@router.get("/mobile/projects/{project_id}/tree")
def get_project_tree(
    project_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Return a compact, mobile-friendly project file tree."""
    _log_mobile_request(request, "project_tree", project_id=project_id)
    project = (
        db.query(Project)
        .filter(Project.id == project_id, Project.deleted_at.is_(None))
        .first()
    )
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    project_root = resolve_project_workspace_path(project.workspace_path, project.name)
    if not project_root.exists():
        return {
            "project_id": project_id,
            "project_name": project.name,
            "root": str(project_root),
            "exists": False,
            "tree_lines": [],
            "total_entries_shown": 0,
            "truncated": False,
        }

    tree_lines, truncated = _build_project_tree_lines(project_root)
    return {
        "project_id": project_id,
        "project_name": project.name,
        "root": str(project_root),
        "exists": True,
        "tree_lines": tree_lines,
        "total_entries_shown": len(tree_lines),
        "truncated": truncated,
    }

# ── Sessions ─────────────────────────────────────────────────


@router.get("/mobile/sessions")
def list_sessions(
    request: Request,
    project_id: Optional[int] = None,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """List sessions, optionally filtered by project or status"""
    _log_mobile_request(
        request, "list_sessions", project_id=project_id, status=status
    )
    query = db.query(SessionModel).filter(SessionModel.deleted_at.is_(None))

    if project_id:
        query = query.filter(SessionModel.project_id == project_id)
    if status:
        query = query.filter(SessionModel.status == status)

    sessions = query.order_by(SessionModel.id.desc()).limit(20).all()

    return {
        "sessions": [
            {
                "id": s.id,
                "name": s.name,
                "status": s.status,
                "is_active": s.is_active,
                "project_id": s.project_id,
                "started_at": s.started_at.isoformat() if s.started_at else None,
                "stopped_at": s.stopped_at.isoformat() if s.stopped_at else None,
            }
            for s in sessions
        ]
    }


@router.get("/mobile/sessions/{session_id}/summary")
def get_session_summary(
    session_id: int, request: Request, db: Session = Depends(get_db)
):
    """Get a concise session summary for mobile display"""
    _log_mobile_request(request, "session_summary", session_id=session_id)
    session = (
        db.query(SessionModel)
        .filter(SessionModel.id == session_id, SessionModel.deleted_at.is_(None))
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")

    # Get recent logs (last 10)
    recent_logs = (
        db.query(LogEntry)
        .filter(LogEntry.session_id == session_id)
        .order_by(LogEntry.created_at.desc())
        .limit(10)
        .all()
    )

    # Get task progress
    tasks = db.query(Task).filter(Task.project_id == session.project_id).all()
    task_counts = _build_task_counts(tasks)

    return {
        "session_id": session_id,
        "name": session.name,
        "status": session.status,
        "execution_mode": getattr(session, "execution_mode", "automatic"),
        "is_active": session.is_active,
        "started_at": session.started_at.isoformat() if session.started_at else None,
        "active_alert": (
            {
                "level": getattr(session, "last_alert_level", None),
                "message": getattr(session, "last_alert_message", None),
                "at": (
                    session.last_alert_at.isoformat()
                    if getattr(session, "last_alert_at", None)
                    else None
                ),
            }
            if getattr(session, "last_alert_message", None)
            else None
        ),
        "task_progress": {
            "total": task_counts["total"],
            "pending": task_counts["pending"],
            "done": task_counts["done"],
            "running": task_counts["running"],
            "failed": task_counts["failed"],
        },
        "recent_logs": [
            {
                "level": log.level,
                "message": log.message,
                "timestamp": log.created_at.isoformat(),
            }
            for log in reversed(recent_logs)
        ],
    }


@router.get("/mobile/sessions/{session_id}/checkpoints")
async def list_mobile_session_checkpoints(
    session_id: int, request: Request, db: Session = Depends(get_db)
):
    """List checkpoints for a session using mobile shared-key auth."""
    _log_mobile_request(request, "session_checkpoints", session_id=session_id)
    from app.api.v1.endpoints.sessions import list_session_checkpoints

    return await list_session_checkpoints(session_id=session_id, db=db, current_user=None)


@router.post("/mobile/sessions/{session_id}/stop")
async def stop_mobile_session(
    session_id: int,
    request: Request,
    force: bool = False,
    db: Session = Depends(get_db),
):
    """Stop a session through the mobile API."""
    _log_mobile_request(request, "stop_session", session_id=session_id, force=force)
    from app.api.v1.endpoints.sessions import stop_session

    return await stop_session(
        session_id=session_id,
        db=db,
        current_user=None,
        force=force,
    )


@router.post("/mobile/sessions/{session_id}/resume")
async def resume_mobile_session(
    session_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Resume a stopped or paused session through the mobile API."""
    _log_mobile_request(request, "resume_session", session_id=session_id)
    from app.api.v1.endpoints.sessions import resume_session

    return await resume_session(
        session_id=session_id,
        db=db,
        current_user=None,
    )


@router.post("/mobile/tasks/{task_id}/retry")
def retry_mobile_task(
    task_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Retry a failed or timed-out task through the mobile API."""
    _log_mobile_request(request, "retry_task", task_id=task_id)
    from app.api.v1.endpoints.tasks import retry_task

    return retry_task(task_id=task_id, db=db)


# ── Tasks ─────────────────────────────────────────────────────


@router.get("/mobile/tasks/{task_id}")
def get_mobile_task(
    task_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Get task detail including linked session context."""
    _log_mobile_request(request, "task_detail", task_id=task_id)

    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    from app.api.v1.endpoints.tasks import _get_active_task_session

    session_task = (
        db.query(SessionModel.id, SessionModel.name)
        .join(SessionTask, SessionTask.session_id == SessionModel.id)
        .filter(SessionTask.task_id == task_id, SessionModel.deleted_at.is_(None))
        .order_by(
            SessionTask.started_at.desc().nullslast(),
            SessionTask.completed_at.desc().nullslast(),
            SessionTask.id.desc(),
        )
        .first()
    )
    active_session_id = _get_active_task_session(db, task_id)

    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "status": task.status.value if hasattr(task.status, "value") else str(task.status),
        "project_id": task.project_id,
        "priority": task.priority or 0,
        "plan_position": getattr(task, "plan_position", None),
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "error_message": task.error_message,
        "session_id": session_task.id if session_task else None,
        "session_name": session_task.name if session_task else None,
        "has_active_session": bool(active_session_id),
    }


@router.get("/mobile/projects/{project_id}/tasks")
def list_project_tasks(
    project_id: int,
    request: Request,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """List tasks for a project"""
    _log_mobile_request(request, "project_tasks", project_id=project_id, status=status)
    project = (
        db.query(Project)
        .filter(Project.id == project_id, Project.deleted_at.is_(None))
        .first()
    )
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    query = db.query(Task).filter(Task.project_id == project_id)

    if status:
        try:
            task_status = TaskStatus[status.upper()]
            query = query.filter(Task.status == task_status)
        except KeyError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    tasks = (
        query.order_by(
            Task.plan_position.asc().nullslast(),
            Task.priority.desc(),
            Task.created_at.asc().nullslast(),
            Task.id.asc(),
        ).all()
    )

    task_payload = []
    total_tasks = len(tasks)
    for index, t in enumerate(tasks, start=1):
        latest_session = (
            db.query(
                SessionModel.id,
                SessionModel.name,
                SessionModel.status,
                SessionModel.is_active,
            )
            .join(SessionTask, SessionTask.session_id == SessionModel.id)
            .filter(SessionTask.task_id == t.id, SessionModel.deleted_at.is_(None))
            .order_by(
                SessionTask.started_at.desc().nullslast(),
                SessionTask.completed_at.desc().nullslast(),
                SessionTask.id.desc(),
            )
            .first()
        )

        task_payload.append(
            {
                "id": t.id,
                "title": t.title,
                "description": t.description,
                "status": (
                    t.status.value if hasattr(t.status, "value") else str(t.status)
                ),
                "priority": getattr(t, "priority", None),
                "plan_position": getattr(t, "plan_position", None),
                "error_message": getattr(t, "error_message", None),
                "created_at": (
                    t.created_at.isoformat()
                    if hasattr(t, "created_at") and t.created_at
                    else None
                ),
                "updated_at": (
                    t.updated_at.isoformat()
                    if hasattr(t, "updated_at") and t.updated_at
                    else None
                ),
                "sequence_index": index,
                "sequence_total": total_tasks,
                "latest_session_id": latest_session.id if latest_session else None,
                "latest_session_name": latest_session.name if latest_session else None,
                "latest_session_status": latest_session.status if latest_session else None,
                "has_active_session": bool(
                    latest_session.is_active if latest_session else False
                ),
            }
        )

    return {
        "project_id": project_id,
        "tasks": task_payload,
        "total": total_tasks,
    }


# ── Quick actions ─────────────────────────────────────────────


@router.get("/mobile/dashboard")
def get_dashboard(request: Request, db: Session = Depends(get_db)):
    """
    Get overall system status for mobile dashboard.
    Called by OpenClaw when user asks for system overview.
    """
    _log_mobile_request(request, "dashboard")
    # Count all entities
    total_projects = db.query(Project).filter(Project.deleted_at.is_(None)).count()
    total_sessions = (
        db.query(SessionModel).filter(SessionModel.deleted_at.is_(None)).count()
    )
    active_sessions = (
        db.query(SessionModel)
        .filter(SessionModel.is_active, SessionModel.deleted_at.is_(None))
        .count()
    )
    running_sessions = (
        db.query(SessionModel)
        .filter(
            SessionModel.status == "running",
            SessionModel.deleted_at.is_(None),
        )
        .count()
    )

    active_project_ids = (
        db.query(Project.id).filter(Project.deleted_at.is_(None)).subquery()
    )

    # Task stats across active projects only
    task_query = db.query(Task).filter(Task.project_id.in_(active_project_ids))
    total_tasks = task_query.count()
    all_tasks = task_query.all()
    task_counts = _build_task_counts(all_tasks)
    pending_tasks = task_counts["pending"]
    done_tasks = task_counts["done"]
    failed_tasks = task_counts["failed"]
    running_tasks = task_counts["running"]

    # Recent activity (last 5 log entries)
    recent_logs = db.query(LogEntry).order_by(LogEntry.created_at.desc()).limit(5).all()
    alerted_sessions = (
        db.query(SessionModel)
        .filter(
            SessionModel.deleted_at.is_(None),
            SessionModel.last_alert_message.isnot(None),
        )
        .order_by(SessionModel.last_alert_at.desc().nullslast(), SessionModel.id.desc())
        .limit(5)
        .all()
    )

    return {
        "timestamp": datetime.utcnow().isoformat(),
        "summary": {
            "projects": total_projects,
            "sessions": {
                "total": total_sessions,
                "active": active_sessions,
                "running": running_sessions,
            },
            "tasks": {
                "total": total_tasks,
                "pending": pending_tasks,
                "done": done_tasks,
                "running": running_tasks,
                "failed": failed_tasks,
                "completion_rate": (
                    f"{(done_tasks / total_tasks * 100):.1f}%"
                    if total_tasks > 0
                    else "N/A"
                ),
            },
        },
        "alerts": [
            {
                "session_id": session.id,
                "project_id": session.project_id,
                "session_name": session.name,
                "level": session.last_alert_level,
                "message": session.last_alert_message,
                "at": (
                    session.last_alert_at.isoformat()
                    if session.last_alert_at
                    else None
                ),
            }
            for session in alerted_sessions
        ],
        "recent_activity": [
            {
                "level": log.level,
                "message": log.message,
                "timestamp": log.created_at.isoformat(),
                "session_id": log.session_id,
            }
            for log in recent_logs
        ],
    }
