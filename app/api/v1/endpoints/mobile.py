"""Mobile API — clawmobile integration via OpenClaw Gateway

These endpoints are called by OpenClaw as tools, not directly by the mobile app.
Access is restricted to OpenClaw/Gateway using a shared API key.

Flow:
  clawmobile → OpenClaw Gateway → OpenClaw agent → these endpoints
"""

import secrets
from datetime import datetime
from typing import Optional

from fastapi import APIRouter, Depends, Header, HTTPException, status
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.models import Project, Session as SessionModel, Task, TaskStatus, LogEntry


def require_mobile_gateway_key(
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
        raise HTTPException(
            status_code=status.HTTP_401_UNAUTHORIZED,
            detail="Invalid or missing mobile gateway API key",
            headers={"WWW-Authenticate": "Bearer"},
        )


router = APIRouter(dependencies=[Depends(require_mobile_gateway_key)])


# ── Projects ─────────────────────────────────────────────────


@router.get("/mobile/projects")
def list_projects(db: Session = Depends(get_db)):
    """List all projects — called by OpenClaw as a tool"""
    projects = db.query(Project).all()
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
def get_project_status(project_id: int, db: Session = Depends(get_db)):
    """Get project status including active sessions and tasks"""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Get active sessions
    active_sessions = (
        db.query(SessionModel)
        .filter(SessionModel.project_id == project_id, SessionModel.is_active)
        .all()
    )

    # Get task stats
    tasks = db.query(Task).filter(Task.project_id == project_id).all()
    task_stats = {
        "total": len(tasks),
        "pending": sum(1 for t in tasks if t.status == TaskStatus.PENDING),
        "running": sum(1 for t in tasks if t.status == TaskStatus.RUNNING),
        "done": sum(1 for t in tasks if t.status == TaskStatus.DONE),
        "failed": sum(1 for t in tasks if t.status == TaskStatus.FAILED),
    }

    return {
        "project_id": project_id,
        "project_name": project.name,
        "description": project.description,
        "active_sessions": len(active_sessions),
        "tasks": task_stats,
        "sessions": [
            {
                "id": s.id,
                "name": s.name,
                "status": s.status,
                "started_at": s.started_at.isoformat() if s.started_at else None,
            }
            for s in active_sessions
        ],
    }


# ── Sessions ─────────────────────────────────────────────────


@router.get("/mobile/sessions")
def list_sessions(
    project_id: Optional[int] = None,
    status: Optional[str] = None,
    db: Session = Depends(get_db),
):
    """List sessions, optionally filtered by project or status"""
    query = db.query(SessionModel)

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
def get_session_summary(session_id: int, db: Session = Depends(get_db)):
    """Get a concise session summary for mobile display"""
    session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
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

    return {
        "session_id": session_id,
        "name": session.name,
        "status": session.status,
        "is_active": session.is_active,
        "started_at": session.started_at.isoformat() if session.started_at else None,
        "task_progress": {
            "total": len(tasks),
            "done": sum(1 for t in tasks if t.status == TaskStatus.DONE),
            "running": sum(1 for t in tasks if t.status == TaskStatus.RUNNING),
            "failed": sum(1 for t in tasks if t.status == TaskStatus.FAILED),
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


# ── Tasks ─────────────────────────────────────────────────────


@router.get("/mobile/projects/{project_id}/tasks")
def list_project_tasks(
    project_id: int, status: Optional[str] = None, db: Session = Depends(get_db)
):
    """List tasks for a project"""
    query = db.query(Task).filter(Task.project_id == project_id)

    if status:
        try:
            task_status = TaskStatus[status.upper()]
            query = query.filter(Task.status == task_status)
        except KeyError:
            raise HTTPException(status_code=400, detail=f"Invalid status: {status}")

    tasks = query.all()

    return {
        "project_id": project_id,
        "tasks": [
            {
                "id": t.id,
                "title": t.title,
                "description": t.description,
                "status": (
                    t.status.value if hasattr(t.status, "value") else str(t.status)
                ),
                "priority": getattr(t, "priority", None),
                "created_at": (
                    t.created_at.isoformat()
                    if hasattr(t, "created_at") and t.created_at
                    else None
                ),
            }
            for t in tasks
        ],
        "total": len(tasks),
    }


# ── Quick actions ─────────────────────────────────────────────


@router.get("/mobile/dashboard")
def get_dashboard(db: Session = Depends(get_db)):
    """
    Get overall system status for mobile dashboard.
    Called by OpenClaw when user asks for system overview.
    """
    # Count all entities
    total_projects = db.query(Project).count()
    total_sessions = db.query(SessionModel).count()
    active_sessions = db.query(SessionModel).filter(SessionModel.is_active).count()
    running_sessions = (
        db.query(SessionModel).filter(SessionModel.status == "running").count()
    )

    # Task stats across all projects
    total_tasks = db.query(Task).count()
    done_tasks = db.query(Task).filter(Task.status == TaskStatus.DONE).count()
    failed_tasks = db.query(Task).filter(Task.status == TaskStatus.FAILED).count()
    running_tasks = db.query(Task).filter(Task.status == TaskStatus.RUNNING).count()

    # Recent activity (last 5 log entries)
    recent_logs = db.query(LogEntry).order_by(LogEntry.created_at.desc()).limit(5).all()

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
