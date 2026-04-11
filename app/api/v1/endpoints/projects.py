"""Projects API endpoints"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime, timedelta, timezone
from app.database import get_db
from app.models import Project, Session, LogEntry, Task, SessionTask
from app.schemas import ProjectCreate, ProjectUpdate, ProjectResponse
from app.services.project_isolation_service import normalize_project_workspace_path
from app.config import settings

router = APIRouter()


@router.post(
    "/projects", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED
)
def create_project(project: ProjectCreate, db: Session = Depends(get_db)):
    """Create a new project"""
    project_data = project.model_dump()
    project_data["workspace_path"] = normalize_project_workspace_path(
        project.workspace_path, project.name
    )
    db_project = Project(**project_data)
    db.add(db_project)
    db.commit()
    db.refresh(db_project)
    return db_project


@router.get("/projects", response_model=List[ProjectResponse])
def get_projects(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    """Get all active (non-deleted) projects"""
    projects = db.query(Project).filter(
        Project.deleted_at.is_(None)
    ).offset(skip).limit(limit).all()
    return projects


@router.delete("/projects/purge-soft-deleted")
def purge_soft_deleted_projects(db: Session = Depends(get_db)):
    """
    Permanently delete all soft-deleted projects older than retention period.

    This endpoint is typically called by a scheduled task (Celery beat)
    to clean up old soft-deleted data and prevent database bloat.
    """
    cutoff_date = datetime.now(timezone.utc) - timedelta(
        days=settings.SOFT_DELETE_RETENTION_DAYS
    )

    # Find soft-deleted projects older than retention period
    old_deleted_projects = (
        db.query(Project)
        .filter(Project.deleted_at.isnot(None))
        .filter(Project.deleted_at < cutoff_date)
        .all()
    )

    if not old_deleted_projects:
        return {"message": "No projects to purge", "purged_count": 0}

    purged_count = 0

    for project in old_deleted_projects:
        project_id = project.id

        # Delete all related data (cascade)
        # Delete session tasks first (foreign key to session)
        db.query(SessionTask).filter(
            SessionTask.session_id.in_(
                db.query(Session.id).filter(Session.project_id == project_id)
            )
        ).delete(synchronize_session=False)

        # Delete all logs for sessions in this project
        db.query(LogEntry).filter(
            LogEntry.session_id.in_(
                db.query(Session.id).filter(Session.project_id == project_id)
            )
        ).delete(synchronize_session=False)

        # Delete all tasks in this project
        db.query(Task).filter(Task.project_id == project_id).delete(
            synchronize_session=False
        )

        # Delete all sessions in this project
        db.query(Session).filter(Session.project_id == project_id).delete(
            synchronize_session=False
        )

        # Delete the project itself
        db.delete(project)
        purged_count += 1

    db.commit()

    return {
        "message": f"Purged {purged_count} soft-deleted projects permanently",
        "purged_count": purged_count,
        "retention_days": settings.SOFT_DELETE_RETENTION_DAYS,
    }


@router.get("/projects/{project_id}", response_model=ProjectResponse)
def get_project(project_id: int, db: Session = Depends(get_db)):
    """Get a specific project"""
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.put("/projects/{project_id}", response_model=ProjectResponse)
def update_project(
    project_id: int, project_update: ProjectUpdate, db: Session = Depends(get_db)
):
    """Update a project"""
    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")

    update_data = project_update.model_dump(exclude_unset=True)
    if "workspace_path" in update_data or "name" in update_data:
        update_data["workspace_path"] = normalize_project_workspace_path(
            update_data.get("workspace_path", db_project.workspace_path),
            update_data.get("name", db_project.name),
        )
    for field, value in update_data.items():
        setattr(db_project, field, value)

    db.commit()
    db.refresh(db_project)
    return db_project


@router.delete("/projects/{project_id}")
def delete_project(project_id: int, db: Session = Depends(get_db)):
    """Delete a project (soft delete to prevent ID reuse issues)"""
    from app.models import Session, LogEntry
    from app.schemas import ProjectResponse

    db_project = db.query(Project).filter(Project.id == project_id).first()
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")

    # Soft delete: mark as deleted instead of hard delete
    # This prevents database ID reuse issues that cause stale logs
    db_project.deleted_at = datetime.now(timezone.utc)
    db.commit()

    # Also soft delete all sessions for this project
    deleted_sessions = db.query(Session).filter(
        Session.project_id == project_id
    ).update({
        "deleted_at": datetime.now(timezone.utc),
        "is_active": False,
        "status": "deleted"
    })
    db.commit()

    return {"message": "Project and associated sessions deleted successfully"}
