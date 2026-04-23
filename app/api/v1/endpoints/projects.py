"""Projects API endpoints"""

from fastapi import APIRouter, Depends, HTTPException, status
from sqlalchemy import false, or_
from sqlalchemy.orm import Session
from typing import List
from datetime import datetime, timedelta, timezone
from app.database import get_db
from app.models import (
    Project,
    Session as SessionModel,
    LogEntry,
    Task,
    SessionTask,
    Plan,
    PlanningArtifact,
    PlanningMessage,
    PlanningSession,
    PermissionRequest,
    SessionState,
    ConversationHistory,
    TaskCheckpoint,
)
from app.schemas import ProjectCreate, ProjectUpdate, ProjectResponse
from app.services.workspace.project_isolation_service import (
    normalize_project_workspace_path,
)
from app.services.workspace.checkpoint_service import CheckpointService
from app.services.name_formatter import humanize_display_name
from app.services.task_service import TaskService
from app.config import settings
from app.dependencies import get_current_active_user

router = APIRouter()


@router.post(
    "/projects", response_model=ProjectResponse, status_code=status.HTTP_201_CREATED
)
def create_project(
    project: ProjectCreate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Create a new project"""
    project_data = project.model_dump()
    project_data["name"] = humanize_display_name(project_data.get("name", ""))
    project_data["workspace_path"] = normalize_project_workspace_path(
        project.workspace_path, project.name, db=db
    )
    db_project = Project(**project_data)
    db.add(db_project)
    db.commit()
    db.refresh(db_project)
    return db_project


@router.get("/projects", response_model=List[ProjectResponse])
def get_projects(skip: int = 0, limit: int = 100, db: Session = Depends(get_db)):
    """Get all active (non-deleted) projects"""
    projects = (
        db.query(Project)
        .filter(Project.deleted_at.is_(None))
        .offset(skip)
        .limit(limit)
        .all()
    )
    return projects


@router.delete("/projects/purge-soft-deleted")
def purge_soft_deleted_projects(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
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
        checkpoint_service = CheckpointService(db)
        session_ids = [
            session_id
            for (session_id,) in db.query(SessionModel.id)
            .filter(SessionModel.project_id == project_id)
            .all()
        ]
        for session_id in session_ids:
            checkpoint_service.delete_all_checkpoints(session_id)
        checkpoint_service.cleanup_orphaned_checkpoints()

        task_ids = [
            task_id
            for (task_id,) in db.query(Task.id)
            .filter(Task.project_id == project_id)
            .all()
        ]

        if session_ids:
            db.query(LogEntry).filter(LogEntry.session_id.in_(session_ids)).delete(
                synchronize_session=False
            )
            db.query(SessionState).filter(
                SessionState.session_id.in_(session_ids)
            ).delete(synchronize_session=False)
            db.query(ConversationHistory).filter(
                ConversationHistory.session_id.in_(session_ids)
            ).delete(synchronize_session=False)

        if task_ids:
            db.query(LogEntry).filter(LogEntry.task_id.in_(task_ids)).delete(
                synchronize_session=False
            )

        if session_ids or task_ids:
            db.query(TaskCheckpoint).filter(
                or_(
                    (
                        TaskCheckpoint.session_id.in_(session_ids)
                        if session_ids
                        else false()
                    ),
                    TaskCheckpoint.task_id.in_(task_ids) if task_ids else false(),
                )
            ).delete(synchronize_session=False)

        db.query(PermissionRequest).filter(
            PermissionRequest.project_id == project_id
        ).delete(synchronize_session=False)

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
    project = (
        db.query(Project)
        .filter(Project.id == project_id, Project.deleted_at.is_(None))
        .first()
    )
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


@router.post("/projects/{project_id}/baseline/rebuild")
def rebuild_project_baseline(
    project_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Rebuild the canonical project baseline from promoted task workspaces."""
    project = (
        db.query(Project)
        .filter(Project.id == project_id, Project.deleted_at.is_(None))
        .first()
    )
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    result = TaskService(db).rebuild_project_baseline(project)
    return {
        "project_id": project.id,
        "project_name": project.name,
        **result,
    }


@router.put("/projects/{project_id}", response_model=ProjectResponse)
def update_project(
    project_id: int,
    project_update: ProjectUpdate,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Update a project"""
    db_project = (
        db.query(Project)
        .filter(Project.id == project_id, Project.deleted_at.is_(None))
        .first()
    )
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")

    update_data = project_update.model_dump(exclude_unset=True)
    if "name" in update_data and update_data["name"] is not None:
        update_data["name"] = humanize_display_name(update_data["name"])
    if "workspace_path" in update_data or "name" in update_data:
        update_data["workspace_path"] = normalize_project_workspace_path(
            update_data.get("workspace_path", db_project.workspace_path),
            update_data.get("name", db_project.name),
            db=db,
        )
    for field, value in update_data.items():
        setattr(db_project, field, value)

    db.commit()
    db.refresh(db_project)
    return db_project


@router.delete("/projects/{project_id}")
def delete_project(
    project_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Delete a project (soft delete to prevent ID reuse issues)"""
    from app.models import Session, TaskCheckpoint
    from app.services.workspace.checkpoint_service import CheckpointService

    db_project = (
        db.query(Project)
        .filter(Project.id == project_id, Project.deleted_at.is_(None))
        .first()
    )
    if not db_project:
        raise HTTPException(status_code=404, detail="Project not found")

    session_ids = [
        session_id
        for (session_id,) in db.query(SessionModel.id)
        .filter(SessionModel.project_id == project_id)
        .all()
    ]
    task_ids = [
        task_id
        for (task_id,) in db.query(Task.id).filter(Task.project_id == project_id).all()
    ]

    # Soft delete: mark as deleted instead of hard delete
    # This prevents database ID reuse issues that cause stale logs
    db_project.deleted_at = datetime.now(timezone.utc)

    # Also soft delete all sessions for this project
    db.query(Session).filter(Session.project_id == project_id).update(
        {
            "deleted_at": datetime.now(timezone.utc),
            "is_active": False,
            "status": "deleted",
        }
    )

    db.query(PlanningSession).filter(PlanningSession.project_id == project_id).update(
        {
            "status": "cancelled",
            "current_prompt_id": None,
            "updated_at": datetime.now(timezone.utc),
        },
        synchronize_session=False,
    )

    if session_ids:
        checkpoint_service = CheckpointService(db)
        for session_id in session_ids:
            checkpoint_service.delete_all_checkpoints(session_id)
        checkpoint_service.cleanup_orphaned_checkpoints()

        db.query(LogEntry).filter(LogEntry.session_id.in_(session_ids)).delete(
            synchronize_session=False
        )
        db.query(SessionTask).filter(SessionTask.session_id.in_(session_ids)).delete(
            synchronize_session=False
        )
        db.query(TaskCheckpoint).filter(
            TaskCheckpoint.session_id.in_(session_ids)
        ).delete(synchronize_session=False)

    if task_ids:
        db.query(LogEntry).filter(LogEntry.task_id.in_(task_ids)).delete(
            synchronize_session=False
        )
        db.query(TaskCheckpoint).filter(TaskCheckpoint.task_id.in_(task_ids)).delete(
            synchronize_session=False
        )
        db.query(Task).filter(Task.id.in_(task_ids)).delete(synchronize_session=False)

    db.commit()

    return {"message": "Project and associated artifacts deleted successfully"}
