"""Authorization helpers for user-owned project resources."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import Project, Session as SessionModel, User


def _active_user_count(db: Session) -> int:
    return db.query(User).filter(User.is_active.is_(True)).count()


def _allow_legacy_ownerless_resources(db: Session) -> bool:
    # Existing local deployments may have projects created before user ownership.
    # Keep those visible only when the database is effectively single-user.
    return _active_user_count(db) <= 1


def project_access_filter(db: Session, user: User):
    if _allow_legacy_ownerless_resources(db):
        return or_(Project.user_id == user.id, Project.user_id.is_(None))
    return Project.user_id == user.id


def get_project_for_user(db: Session, project_id: int, user: User) -> Project:
    project = (
        db.query(Project)
        .filter(
            Project.id == project_id,
            Project.deleted_at.is_(None),
            project_access_filter(db, user),
        )
        .first()
    )
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")
    return project


def get_session_for_user(db: Session, session_id: int, user: User) -> SessionModel:
    session = (
        db.query(SessionModel)
        .join(Project, Project.id == SessionModel.project_id)
        .filter(
            SessionModel.id == session_id,
            SessionModel.deleted_at.is_(None),
            Project.deleted_at.is_(None),
            project_access_filter(db, user),
        )
        .first()
    )
    if not session:
        raise HTTPException(status_code=404, detail="Session not found")
    return session
