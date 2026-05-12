"""Authorization helpers for project resources."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import true
from sqlalchemy.orm import Session

from app.models import Project, Session as SessionModel, User


def project_access_filter(db: Session, user: User):
    """Return the project visibility predicate for authenticated local users."""
    del db, user
    return true()


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
