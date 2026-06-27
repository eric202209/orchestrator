"""Authorization helpers for project resources."""

from __future__ import annotations

from fastapi import HTTPException
from sqlalchemy import false, or_, true
from sqlalchemy.orm import Session

from app.config import settings
from app.models import Project, Session as SessionModel, User


def _is_admin_user(user: User) -> bool:
    """Return True if the user has admin privileges."""
    admin_emails = {
        email.strip().lower()
        for email in (settings.ADMIN_EMAILS or "").split(",")
        if email.strip()
    }
    return bool(user.email and user.email.lower() in admin_emails)


def project_access_filter(db: Session, user: User | None):
    """Return the project visibility predicate for authenticated local users.

    Admin users (listed in ADMIN_EMAILS) see all projects.
    Single-user deployments see their own projects plus unowned ones.
    Multi-user deployments see only their own projects.
    """
    user_id = getattr(user, "id", None)
    if user_id is None:
        return false()

    if user is not None and _is_admin_user(user):
        return true()

    active_user_ids = db.query(User.id).filter(User.is_active.is_(True)).limit(2).all()
    if len(active_user_ids) <= 1:
        return or_(Project.user_id == user_id, Project.user_id.is_(None))
    return Project.user_id == user_id


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
