"""Mobile API — clawmobile integration via OpenClaw Gateway

These endpoints are called by OpenClaw as tools, not directly by the mobile app.
Access is restricted to OpenClaw/Gateway using a shared API key.

Flow:
  clawmobile → OpenClaw Gateway → OpenClaw agent → these endpoints
"""

import secrets
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

from fastapi import (
    APIRouter,
    Depends,
    Header,
    HTTPException,
    Query,
    Request,
    WebSocket,
    WebSocketDisconnect,
    status,
)
from sqlalchemy.orm import Session
from pydantic import BaseModel
from typing import Any

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_active_user
from app.models import (
    LogEntry,
    PermissionRequest,
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskStatus,
    User,
)

# ── Request body schemas for new mobile endpoints ─────────────


class MobileCreateSessionBody(BaseModel):
    project_id: int
    name: str
    task_id: Optional[int] = None


class MobileCheckpointLoadBody(BaseModel):
    checkpoint_name: str


class MobileTaskPositionBody(BaseModel):
    plan_position: int


class MobileWorkspaceReviewBody(BaseModel):
    action: str  # "promote" or "request_changes"
    note: Optional[str] = None


class MobilePermissionApproveBody(BaseModel):
    auto_approve_same: bool = False


from app.services.workspace.project_isolation_service import (
    resolve_project_workspace_path,
)
from app.services.workspace.system_settings import get_effective_mobile_gateway_key
from app.services.task_service import TaskService

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
TASK_REPORT_RE = re.compile(r"^task_report_\d+\.md$", re.IGNORECASE)


def _status_value(value: object) -> str:
    return value.value if hasattr(value, "value") else str(value)


def _normalize_permission_status(value: object) -> str:
    normalized = _status_value(value).lower()
    return "rejected" if normalized == "denied" else normalized


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
    configured_key, _ = get_effective_mobile_gateway_key(
        settings.MOBILE_GATEWAY_API_KEY,
        settings.OPENCLAW_API_KEY,
    )

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
    details = " ".join(
        f"{key}={value}" for key, value in extra.items() if value is not None
    )
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
                    path.iterdir(),
                    key=lambda item: (not item.is_dir(), item.name.lower()),
                )
                if child.name not in TREE_EXCLUDED_NAMES
                and not TASK_REPORT_RE.match(child.name)
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

            if child.is_dir() and not (depth == 0 and child.name.startswith("task-")):
                next_prefix = f"{prefix}{'    ' if is_last else '│   '}"
                walk(child, next_prefix, depth + 1)
                if truncated:
                    return

    walk(root, "", 0)
    return lines, truncated


def _get_latest_task_attempt(db: Session, task_id: int):
    return (
        db.query(
            SessionTask.status,
            SessionTask.started_at,
            SessionTask.completed_at,
            SessionModel.id.label("session_id"),
            SessionModel.name.label("session_name"),
            SessionModel.status.label("session_status"),
            SessionModel.is_active.label("session_is_active"),
        )
        .join(SessionModel, SessionTask.session_id == SessionModel.id)
        .filter(SessionTask.task_id == task_id, SessionModel.deleted_at.is_(None))
        .order_by(
            SessionTask.started_at.desc().nullslast(),
            SessionTask.completed_at.desc().nullslast(),
            SessionTask.id.desc(),
        )
        .first()
    )


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
    """Return setup metadata without exposing the raw mobile shared key."""
    shared_key, key_source = _get_mobile_shared_key()
    return {
        "user_email": current_user.email,
        "header_name": "X-OpenClaw-API-Key",
        "api_key": None,
        "api_key_configured": bool(shared_key),
        "api_key_preview": _mask_secret(shared_key),
        "api_key_source": key_source,
        "detail": "Raw mobile gateway secrets are not returned by the API",
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
    baseline = TaskService(db).get_project_baseline_overview(project)
    if not project_root.exists():
        return {
            "project_id": project_id,
            "project_name": project.name,
            "root": str(project_root),
            "exists": False,
            "baseline": baseline,
            "tree_lines": [],
            "task_workspaces": [],
            "total_entries_shown": 0,
            "truncated": False,
        }

    tree_lines, truncated = _build_project_tree_lines(project_root)
    task_workspaces = (
        db.query(Task)
        .filter(
            Task.project_id == project_id,
            Task.task_subfolder.isnot(None),
        )
        .order_by(
            Task.plan_position.asc().nullslast(),
            Task.created_at.asc().nullslast(),
            Task.id.asc(),
        )
        .all()
    )
    return {
        "project_id": project_id,
        "project_name": project.name,
        "root": str(project_root),
        "exists": True,
        "baseline": baseline,
        "tree_lines": tree_lines,
        "task_workspaces": [
            {
                "task_id": task.id,
                "title": task.title,
                "status": _status_value(task.status),
                "plan_position": getattr(task, "plan_position", None),
                "subfolder": task.task_subfolder,
            }
            for task in task_workspaces
        ],
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
    _log_mobile_request(request, "list_sessions", project_id=project_id, status=status)
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

    return await list_session_checkpoints(
        session_id=session_id, db=db, current_user=None
    )


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
    TaskService(db).sync_workspace_status(task)

    from app.api.v1.endpoints.tasks import _get_active_task_session

    latest_attempt = _get_latest_task_attempt(db, task_id)
    active_session_id = _get_active_task_session(db, task_id)
    latest_attempt_status = (
        _status_value(latest_attempt.status) if latest_attempt else None
    )
    is_live_attempt = (
        bool(latest_attempt.session_is_active)
        if latest_attempt and latest_attempt_status == TaskStatus.RUNNING.value
        else False
    )

    return {
        "id": task.id,
        "title": task.title,
        "description": task.description,
        "status": (
            task.status.value if hasattr(task.status, "value") else str(task.status)
        ),
        "project_id": task.project_id,
        "priority": task.priority or 0,
        "plan_position": getattr(task, "plan_position", None),
        "created_at": task.created_at.isoformat() if task.created_at else None,
        "updated_at": task.updated_at.isoformat() if task.updated_at else None,
        "error_message": task.error_message,
        "workspace_status": getattr(task, "workspace_status", None),
        "promotion_note": getattr(task, "promotion_note", None),
        "promoted_at": (
            task.promoted_at.isoformat() if getattr(task, "promoted_at", None) else None
        ),
        "session_id": latest_attempt.session_id if latest_attempt else None,
        "session_name": latest_attempt.session_name if latest_attempt else None,
        "latest_attempt_status": latest_attempt_status,
        "latest_session_status": latest_attempt_status,
        "has_active_session": bool(active_session_id and is_live_attempt),
        "is_live_attempt": is_live_attempt,
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

    tasks = query.order_by(
        Task.plan_position.asc().nullslast(),
        Task.priority.desc(),
        Task.created_at.asc().nullslast(),
        Task.id.asc(),
    ).all()
    task_service = TaskService(db)
    changed = False
    for task in tasks:
        changed = task_service.sync_workspace_status(task, commit=False) or changed
    if changed:
        db.commit()

    task_payload = []
    total_tasks = len(tasks)
    for index, t in enumerate(tasks, start=1):
        latest_attempt = _get_latest_task_attempt(db, t.id)
        latest_attempt_status = (
            _status_value(latest_attempt.status) if latest_attempt else None
        )
        is_live_attempt = (
            bool(latest_attempt.session_is_active)
            if latest_attempt and latest_attempt_status == TaskStatus.RUNNING.value
            else False
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
                "workspace_status": getattr(t, "workspace_status", None),
                "promotion_note": getattr(t, "promotion_note", None),
                "promoted_at": (
                    t.promoted_at.isoformat()
                    if getattr(t, "promoted_at", None)
                    else None
                ),
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
                "latest_session_id": (
                    latest_attempt.session_id if latest_attempt else None
                ),
                "latest_session_name": (
                    latest_attempt.session_name if latest_attempt else None
                ),
                "latest_attempt_status": latest_attempt_status,
                "latest_session_status": latest_attempt_status,
                "has_active_session": is_live_attempt,
                "is_live_attempt": is_live_attempt,
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
                    session.last_alert_at.isoformat() if session.last_alert_at else None
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


# ── US2: Session Start / Pause / Live Log Stream ──────────────


@router.post("/mobile/sessions")
async def create_mobile_session(
    body: MobileCreateSessionBody,
    request: Request,
    db: Session = Depends(get_db),
):
    """Create a new session via mobile — proxies to SessionCreate service (T022)."""
    _log_mobile_request(request, "create_session", project_id=body.project_id)
    from app.schemas import SessionCreate
    from app.api.v1.endpoints.sessions import create_session

    session_create = SessionCreate(
        project_id=body.project_id,
        name=body.name,
        task_id=body.task_id,
    )
    result = create_session(session=session_create, db=db, current_user=None)
    return {
        "session_id": result["id"] if isinstance(result, dict) else result.id,
        "status": (
            result.get("status", "pending")
            if isinstance(result, dict)
            else getattr(result, "status", "pending")
        ),
        "name": (
            result.get("name", body.name)
            if isinstance(result, dict)
            else getattr(result, "name", body.name)
        ),
    }


@router.post("/mobile/sessions/{session_id}/pause")
async def pause_mobile_session(
    session_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Pause a running session via mobile (T023)."""
    _log_mobile_request(request, "pause_session", session_id=session_id)
    from app.api.v1.endpoints.sessions import pause_session

    return await pause_session(session_id=session_id, db=db, current_user=None)


@router.websocket("/mobile/sessions/{session_id}/logs/stream")
async def mobile_log_stream(
    session_id: int,
    websocket: WebSocket,
    api_key: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    """Stream live log entries for a session via WebSocket (T024).

    Auth: X-OpenClaw-API-Key header or ?api_key= query param (fallback for WebSocket clients).
    Closes on terminal session state (stopped/failed/done).
    """
    configured_key, _ = get_effective_mobile_gateway_key(
        settings.MOBILE_GATEWAY_API_KEY, settings.OPENCLAW_API_KEY
    )
    presented_key = api_key or websocket.headers.get("X-OpenClaw-API-Key")
    if (
        not configured_key
        or not presented_key
        or not secrets.compare_digest(presented_key, configured_key)
    ):
        await websocket.close(code=4001)
        return

    await websocket.accept()
    TERMINAL_STATES = {"stopped", "failed", "done", "completed", "error"}
    last_log_id = 0
    try:
        while True:
            session = (
                db.query(SessionModel).filter(SessionModel.id == session_id).first()
            )
            if not session:
                await websocket.send_json(
                    {
                        "level": "ERROR",
                        "message": "Session not found",
                        "timestamp": datetime.utcnow().isoformat(),
                    }
                )
                break
            if _status_value(session.status).lower() in TERMINAL_STATES:
                break

            new_logs = (
                db.query(LogEntry)
                .filter(LogEntry.session_id == session_id, LogEntry.id > last_log_id)
                .order_by(LogEntry.id.asc())
                .limit(50)
                .all()
            )
            for log in new_logs:
                await websocket.send_json(
                    {
                        "level": log.level,
                        "message": log.message,
                        "timestamp": log.created_at.isoformat(),
                    }
                )
                last_log_id = log.id

            db.expire_all()
            import asyncio

            await asyncio.sleep(1)
    except WebSocketDisconnect:
        pass
    finally:
        try:
            await websocket.close()
        except Exception:
            pass


# ── US3: Task Position / Workspace Review ─────────────────────


@router.patch("/mobile/tasks/{task_id}/position")
def update_mobile_task_position(
    task_id: int,
    body: MobileTaskPositionBody,
    request: Request,
    db: Session = Depends(get_db),
):
    """Update task plan_position for drag-and-drop reorder (T036)."""
    _log_mobile_request(request, "update_task_position", task_id=task_id)
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    task.plan_position = body.plan_position
    db.commit()
    return {"task_id": task_id, "plan_position": body.plan_position}


@router.post("/mobile/tasks/{task_id}/review")
def submit_mobile_workspace_review(
    task_id: int,
    body: MobileWorkspaceReviewBody,
    request: Request,
    db: Session = Depends(get_db),
):
    """Submit workspace review (promote / request_changes) for a task (T037)."""
    _log_mobile_request(
        request, "workspace_review", task_id=task_id, action=body.action
    )
    from app.services.task_service import TaskService

    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    if body.action not in ("promote", "request_changes"):
        raise HTTPException(
            status_code=400, detail="action must be 'promote' or 'request_changes'"
        )

    task_service = TaskService(db)
    if body.action == "promote":
        task_service.promote_task(task, note=body.note)
    else:
        task_service.request_changes(task, note=body.note)
    db.commit()

    return {
        "task_id": task_id,
        "workspace_status": getattr(task, "workspace_status", body.action),
        "promoted_at": (
            task.promoted_at.isoformat() if getattr(task, "promoted_at", None) else None
        ),
    }


# ── US4: Checkpoint Load / Delete ─────────────────────────────


@router.post("/mobile/sessions/{session_id}/checkpoint/load")
async def load_mobile_checkpoint(
    session_id: int,
    body: MobileCheckpointLoadBody,
    request: Request,
    db: Session = Depends(get_db),
):
    """Load a session checkpoint via mobile (T048)."""
    _log_mobile_request(
        request, "load_checkpoint", session_id=session_id, name=body.checkpoint_name
    )
    from app.api.v1.endpoints.sessions import load_session_checkpoint

    return await load_session_checkpoint(
        session_id=session_id,
        checkpoint_name=body.checkpoint_name,
        db=db,
        current_user=None,
    )


@router.delete("/mobile/sessions/{session_id}/checkpoints/{checkpoint_name}")
async def delete_mobile_checkpoint(
    session_id: int,
    checkpoint_name: str,
    request: Request,
    db: Session = Depends(get_db),
):
    """Delete a session checkpoint via mobile (T049)."""
    _log_mobile_request(
        request, "delete_checkpoint", session_id=session_id, name=checkpoint_name
    )
    from app.api.v1.endpoints.sessions import delete_session_checkpoint

    return await delete_session_checkpoint(
        session_id=session_id,
        checkpoint_name=checkpoint_name,
        db=db,
        current_user=None,
    )


# ── US5: Permission List / Approve / Reject ───────────────────


@router.get("/mobile/permissions")
def list_mobile_permissions(
    request: Request,
    status: Optional[str] = Query(default=None),
    db: Session = Depends(get_db),
):
    """List permission requests, optionally filtered by status (T057)."""
    _log_mobile_request(request, "list_permissions", status=status)
    query = db.query(PermissionRequest).order_by(
        PermissionRequest.created_at.desc().nullslast(),
        PermissionRequest.id.desc(),
    )
    if status:
        normalized_status = status.lower()
        internal_status = (
            "denied" if normalized_status == "rejected" else normalized_status
        )
        if internal_status not in {"pending", "approved", "denied", "expired"}:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")
        query = query.filter(PermissionRequest.status == internal_status)

    permissions = query.limit(100).all()
    return {
        "permissions": [
            {
                "id": p.id,
                "operation_type": p.operation_type,
                "description": p.description,
                "status": _normalize_permission_status(p.status),
                "session_id": p.session_id,
                "session_name": p.session.name if p.session else None,
                "expires_at": p.expires_at.isoformat() if p.expires_at else None,
                "created_at": p.created_at.isoformat() if p.created_at else None,
            }
            for p in permissions
        ],
        "total": len(permissions),
    }


@router.post("/mobile/permissions/{request_id}/approve")
async def approve_mobile_permission(
    request_id: int,
    body: MobilePermissionApproveBody,
    request: Request,
    db: Session = Depends(get_db),
):
    """Approve a permission request via mobile (T058)."""
    _log_mobile_request(request, "approve_permission", request_id=request_id)
    from app.services.permission_service import PermissionApprovalService

    service = PermissionApprovalService(db)
    try:
        permission = service.approve_permission(
            request_id=request_id,
            approved_by="mobile",
            auto_approve_same=body.auto_approve_same,
        )
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "request_id": permission.id,
        "status": permission.status,
        "message": "Permission approved",
    }


@router.post("/mobile/permissions/{request_id}/reject")
async def reject_mobile_permission(
    request_id: int,
    request: Request,
    db: Session = Depends(get_db),
):
    """Reject a permission request via mobile (T059)."""
    _log_mobile_request(request, "reject_permission", request_id=request_id)
    from app.services.permission_service import PermissionApprovalService

    service = PermissionApprovalService(db)
    try:
        permission = service.deny_permission(request_id=request_id)
    except ValueError as e:
        raise HTTPException(status_code=400, detail=str(e))

    return {
        "request_id": permission.id,
        "status": "rejected",
        "message": "Permission rejected",
    }
