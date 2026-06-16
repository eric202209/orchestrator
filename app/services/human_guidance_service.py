"""Human Guidance service — CRUD for HG-P1a."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy.orm import Session as DBSession

from app.models import GuidanceStatus, HumanGuidance, HumanGuidanceRevision

_UNSET = object()


def _get_or_404(db: DBSession, guidance_id: int) -> HumanGuidance:
    g = db.query(HumanGuidance).filter(HumanGuidance.id == guidance_id).first()
    if not g:
        raise HTTPException(status_code=404, detail="guidance_not_found")
    return g


def create_guidance(
    db: DBSession,
    *,
    user_id: Optional[int],
    project_id: Optional[int] = None,
    session_id: Optional[int] = None,
    task_id: Optional[int] = None,
    scope: str,
    message: str,
    priority: int = 0,
    expires_at: Optional[datetime] = None,
    created_by: Optional[str] = None,
) -> Tuple[HumanGuidance, bool]:
    """Create a guidance entry. Returns (entry, created); created=False on dedup."""
    message = (message or "").strip()
    if not message:
        raise HTTPException(status_code=400, detail="guidance_empty")
    if len(message) > 500:
        raise HTTPException(status_code=400, detail="message_too_long")
    if priority < 0 or priority > 100:
        raise HTTPException(status_code=400, detail="invalid_priority")

    existing = (
        db.query(HumanGuidance)
        .filter(
            HumanGuidance.user_id == user_id,
            HumanGuidance.message == message,
            HumanGuidance.scope == scope,
            HumanGuidance.project_id == project_id,
            HumanGuidance.session_id == session_id,
            HumanGuidance.task_id == task_id,
            HumanGuidance.status == GuidanceStatus.ACTIVE,
        )
        .first()
    )
    if existing:
        return existing, False

    entry = HumanGuidance(
        user_id=user_id,
        project_id=project_id,
        session_id=session_id,
        task_id=task_id,
        scope=scope,
        message=message,
        status=GuidanceStatus.ACTIVE,
        priority=priority,
        expires_at=expires_at,
        created_by=created_by,
        revision=1,
    )
    db.add(entry)
    db.commit()
    db.refresh(entry)
    return entry, True


def get_guidance(db: DBSession, guidance_id: int) -> HumanGuidance:
    return _get_or_404(db, guidance_id)


def list_guidance(
    db: DBSession,
    *,
    project_id: int,
    status: str = "active",
    scope: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
) -> Tuple[List[HumanGuidance], int]:
    query = db.query(HumanGuidance).filter(HumanGuidance.project_id == project_id)
    if status != "all":
        query = query.filter(HumanGuidance.status == status)
    if scope:
        query = query.filter(HumanGuidance.scope == scope)
    total = query.count()
    items = (
        query.order_by(
            HumanGuidance.priority.desc(),
            HumanGuidance.created_at.asc(),
        )
        .offset(offset)
        .limit(limit)
        .all()
    )
    return items, total


def update_guidance(
    db: DBSession,
    guidance_id: int,
    *,
    message=_UNSET,
    status=_UNSET,
    priority=_UNSET,
    expires_at=_UNSET,
    change_reason: Optional[str] = None,
    changed_by: Optional[str] = None,
) -> HumanGuidance:
    entry = _get_or_404(db, guidance_id)
    if entry.status == GuidanceStatus.ARCHIVED:
        raise HTTPException(status_code=400, detail="Cannot update archived guidance")

    now = datetime.now(timezone.utc)

    if message is not _UNSET:
        msg = (message or "").strip()
        if not msg:
            raise HTTPException(status_code=400, detail="guidance_empty")
        if len(msg) > 500:
            raise HTTPException(status_code=400, detail="message_too_long")
        if msg != entry.message:
            rev = HumanGuidanceRevision(
                guidance_id=entry.id,
                revision=entry.revision,
                message=entry.message,
                changed_by=changed_by,
                change_reason=change_reason,
            )
            db.add(rev)
            entry.message = msg
            entry.revision += 1

    if status is not _UNSET:
        allowed = {GuidanceStatus.ACTIVE, GuidanceStatus.DISABLED, "active", "disabled"}
        if status not in allowed:
            raise HTTPException(status_code=422, detail="immutable_field")
        entry.status = status
        if status in (GuidanceStatus.DISABLED, "disabled"):
            entry.disabled_at = now
        else:
            entry.disabled_at = None

    if priority is not _UNSET:
        if priority < 0 or priority > 100:
            raise HTTPException(status_code=400, detail="invalid_priority")
        entry.priority = priority

    if expires_at is not _UNSET:
        entry.expires_at = expires_at

    entry.updated_at = now
    db.commit()
    db.refresh(entry)
    return entry


def archive_guidance(db: DBSession, guidance_id: int) -> HumanGuidance:
    entry = _get_or_404(db, guidance_id)
    if entry.status == GuidanceStatus.ARCHIVED:
        return entry
    entry.status = GuidanceStatus.ARCHIVED
    entry.archived_at = datetime.now(timezone.utc)
    db.commit()
    db.refresh(entry)
    return entry
