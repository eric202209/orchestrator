"""Projects API endpoints"""

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel
from sqlalchemy import false, or_
from sqlalchemy.orm import Session
from typing import List, Optional
from datetime import datetime, timedelta, timezone
from app.schemas.pagination import paginate
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
from app.services.project.name_formatter import humanize_display_name
from app.services.tasks.service import TaskService
from app.services.workspace.project_mutation_lock import ProjectMutationLockError
from app.config import settings
from app.dependencies import get_current_active_user
from app.services.auth.authorization import get_project_for_user, project_access_filter

router = APIRouter()


class WorkspaceCleanupRequest(BaseModel):
    dry_run: bool = True
    include_ready: bool = False
    include_changes_requested: bool = False
    include_blocked: bool = True


class WorkspaceArchiveRestoreRequest(BaseModel):
    task_id: int
    archive_path: str


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
    project_data["user_id"] = current_user.id
    db_project = Project(**project_data)
    db.add(db_project)
    db.commit()
    db.refresh(db_project)
    TaskService(db).ensure_project_gitignore_guard(db_project)
    return db_project


_PROJECT_ORDER_COLUMNS = {
    "created_at": Project.created_at,
    "updated_at": Project.updated_at,
    "name": Project.name,
}


@router.get("/projects")
def get_projects(
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
    # Legacy — TODO(Phase15E-4): remove legacy skip/limit mode
    skip: int = 0,
    limit: int = 100,
    # Paginated mode
    page: Optional[int] = None,
    per_page: int = 25,
    # Filters
    search: Optional[str] = None,
    # Ordering
    order_by: str = "created_at",
    order_dir: str = "desc",
):
    """Get all active (non-deleted) projects.

    Legacy mode (no page): returns List[ProjectResponse] with skip/limit.
    Paginated mode (page param): returns Page[ProjectResponse].
    """
    if page is not None and page < 1:
        raise HTTPException(status_code=422, detail="page must be >= 1")
    if per_page < 1 or per_page > 200:
        raise HTTPException(
            status_code=422, detail="per_page must be between 1 and 200"
        )

    query = db.query(Project).filter(
        Project.deleted_at.is_(None), project_access_filter(db, current_user)
    )
    if search:
        query = query.filter(Project.name.ilike(f"%{search}%"))

    if page is None:
        # Legacy mode
        projects = query.offset(skip).limit(limit).all()
        return projects

    # Paginated mode
    col = _PROJECT_ORDER_COLUMNS.get(order_by, Project.created_at)
    if order_dir.lower() == "asc":
        query = query.order_by(col.asc().nullslast())
    else:
        query = query.order_by(col.desc().nullslast())
    page_data = paginate(query, page, per_page)
    from app.schemas import ProjectResponse as _ProjectResponse

    page_data["items"] = [
        _ProjectResponse.model_validate(p) for p in page_data["items"]
    ]
    return page_data


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
        .filter(project_access_filter(db, current_user))
        .all()
    )

    if not old_deleted_projects:
        return {"message": "No projects to purge", "purged_count": 0}

    project_ids = [project.id for project in old_deleted_projects]
    session_ids_by_project: dict[int, list[int]] = {
        project_id: [] for project_id in project_ids
    }
    task_ids_by_project: dict[int, list[int]] = {
        project_id: [] for project_id in project_ids
    }
    planning_session_ids_by_project: dict[int, list[int]] = {
        project_id: [] for project_id in project_ids
    }

    for project_id, session_id in (
        db.query(SessionModel.project_id, SessionModel.id)
        .filter(SessionModel.project_id.in_(project_ids))
        .all()
    ):
        session_ids_by_project.setdefault(project_id, []).append(session_id)

    for project_id, task_id in (
        db.query(Task.project_id, Task.id)
        .filter(Task.project_id.in_(project_ids))
        .all()
    ):
        task_ids_by_project.setdefault(project_id, []).append(task_id)

    for project_id, planning_session_id in (
        db.query(PlanningSession.project_id, PlanningSession.id)
        .filter(PlanningSession.project_id.in_(project_ids))
        .all()
    ):
        planning_session_ids_by_project.setdefault(project_id, []).append(
            planning_session_id
        )

    purged_count = 0

    try:
        checkpoint_service = CheckpointService(db)
        for project_id in project_ids:
            session_ids = session_ids_by_project.get(project_id, [])
            task_ids = task_ids_by_project.get(project_id, [])
            planning_session_ids = planning_session_ids_by_project.get(project_id, [])

            for session_id in session_ids:
                checkpoint_service.delete_all_checkpoints(session_id)

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

            if planning_session_ids:
                db.query(PlanningMessage).filter(
                    PlanningMessage.planning_session_id.in_(planning_session_ids)
                ).delete(synchronize_session=False)
                db.query(PlanningArtifact).filter(
                    PlanningArtifact.planning_session_id.in_(planning_session_ids)
                ).delete(synchronize_session=False)
                db.query(PlanningSession).filter(
                    PlanningSession.id.in_(planning_session_ids)
                ).delete(synchronize_session=False)

            db.query(PermissionRequest).filter(
                PermissionRequest.project_id == project_id
            ).delete(synchronize_session=False)

            db.query(Project).filter(Project.id == project_id).delete(
                synchronize_session=False
            )
            purged_count += 1

        checkpoint_service.cleanup_orphaned_checkpoints()
        db.commit()
    except Exception:
        db.rollback()
        raise

    return {
        "message": f"Purged {purged_count} soft-deleted projects permanently",
        "purged_count": purged_count,
        "retention_days": settings.SOFT_DELETE_RETENTION_DAYS,
    }


@router.get("/projects/{project_id}", response_model=ProjectResponse)
def get_project(
    project_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Get a specific project"""
    return get_project_for_user(db, project_id, current_user)


@router.post("/projects/{project_id}/baseline/rebuild")
def rebuild_project_baseline(
    project_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Rebuild the canonical project baseline from promoted task workspaces."""
    project = get_project_for_user(db, project_id, current_user)

    try:
        result = TaskService(db).rebuild_project_baseline(project)
    except ProjectMutationLockError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
    return {
        "project_id": project.id,
        "project_name": project.name,
        **result,
    }


@router.post("/projects/{project_id}/workspace-cleanup")
def cleanup_project_task_workspaces(
    project_id: int,
    payload: WorkspaceCleanupRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Preview or delete retained disposable task workspace folders."""
    project = get_project_for_user(db, project_id, current_user)

    result = TaskService(db).cleanup_retained_task_workspaces(
        project,
        dry_run=payload.dry_run,
        include_ready=payload.include_ready,
        include_changes_requested=payload.include_changes_requested,
        include_blocked=payload.include_blocked,
    )
    return {
        "project_id": project.id,
        "project_name": project.name,
        **result,
    }


@router.post("/projects/{project_id}/workspace-archive/restore")
def restore_project_task_workspace_archive(
    project_id: int,
    payload: WorkspaceArchiveRestoreRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Restore an archived task workspace after operator review."""
    project = get_project_for_user(db, project_id, current_user)
    task = (
        db.query(Task)
        .filter(Task.id == payload.task_id, Task.project_id == project.id)
        .first()
    )
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    try:
        result = TaskService(db).restore_archived_task_workspace(
            project,
            task,
            archive_path=payload.archive_path,
        )
    except ValueError as exc:
        raise HTTPException(status_code=409, detail=str(exc)) from exc
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
    db_project = get_project_for_user(db, project_id, current_user)

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
    TaskService(db).ensure_project_gitignore_guard(db_project)
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

    db_project = get_project_for_user(db, project_id, current_user)

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
