"""Human Guidance service — CRUD (HG-P1a) + active collection and usage telemetry (HG-P1b)."""

from __future__ import annotations

import hashlib
import logging
from datetime import UTC, datetime, timezone
from typing import Any, Dict, List, Optional, Tuple

from fastapi import HTTPException
from sqlalchemy import and_, case, or_
from sqlalchemy.orm import Session as DBSession

from app.models import (
    GuidanceScope,
    GuidanceStatus,
    HumanGuidance,
    HumanGuidanceRevision,
    HumanGuidanceUsage,
)

logger = logging.getLogger(__name__)

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


# ── HG-P1b: active-guidance collection ───────────────────────────────────────

_SCOPE_ORDER = case(
    (HumanGuidance.scope == GuidanceScope.TASK, 0),
    (HumanGuidance.scope == GuidanceScope.SESSION, 1),
    (HumanGuidance.scope == GuidanceScope.PROJECT, 2),
    (HumanGuidance.scope == GuidanceScope.GLOBAL, 3),
    else_=4,
)


def collect_active_guidance(
    db: DBSession,
    *,
    user_id: Optional[int],
    project_id: Optional[int],
    session_id: Optional[int],
    task_id: Optional[int],
    now: Optional[datetime] = None,
) -> List[Dict[str, Any]]:
    """Return active guidance applicable to this execution context in merge order.

    Scope order (narrowest first): task > session > project > global.
    Within scope: priority DESC, created_at ASC.
    Excludes disabled, archived, expired entries.
    Returns normalized dicts compatible with working_memory.json human_guidance entries.
    """
    if db is None:
        return []
    if now is None:
        now = datetime.now(UTC)

    scope_conditions = [HumanGuidance.scope == GuidanceScope.GLOBAL]
    if project_id is not None:
        scope_conditions.append(
            and_(
                HumanGuidance.scope == GuidanceScope.PROJECT,
                HumanGuidance.project_id == project_id,
            )
        )
    if session_id is not None:
        scope_conditions.append(
            and_(
                HumanGuidance.scope == GuidanceScope.SESSION,
                HumanGuidance.session_id == session_id,
            )
        )
    if task_id is not None:
        scope_conditions.append(
            and_(
                HumanGuidance.scope == GuidanceScope.TASK,
                HumanGuidance.task_id == task_id,
            )
        )

    try:
        rows = (
            db.query(HumanGuidance)
            .filter(
                HumanGuidance.user_id == user_id,
                HumanGuidance.status == GuidanceStatus.ACTIVE,
                or_(
                    HumanGuidance.expires_at.is_(None),
                    HumanGuidance.expires_at > now,
                ),
                or_(*scope_conditions),
            )
            .order_by(
                _SCOPE_ORDER,
                HumanGuidance.priority.desc(),
                HumanGuidance.created_at.asc(),
            )
            .all()
        )
    except Exception as exc:
        logger.warning("collect_active_guidance query failed: %s", exc)
        return []

    out: List[Dict[str, Any]] = []
    for row in rows:
        scope_val = row.scope.value if hasattr(row.scope, "value") else str(row.scope)
        status_val = (
            row.status.value if hasattr(row.status, "value") else str(row.status)
        )
        created_at = ""
        try:
            ts = getattr(row, "created_at", None)
            if ts is not None:
                created_at = ts.isoformat()
        except Exception:
            pass
        out.append(
            {
                "id": row.id,
                "task_id": row.task_id,
                "message": row.message,
                "created_at": created_at,
                "source": "operator_guidance",
                "scope": scope_val,
                "status": status_val,
                "priority": row.priority,
            }
        )
    return out


def record_guidance_usage(
    db: DBSession,
    *,
    entries: List[Dict[str, Any]],
    project_id: Optional[int],
    session_id: Optional[int],
    task_id: Optional[int],
) -> None:
    """Write HumanGuidanceUsage rows for each guidance entry rendered into WM.

    Never raises — telemetry failures must not fail task completion.
    """
    try:
        for position, entry in enumerate(entries):
            guidance_id = entry.get("id")
            message = entry.get("message", "")
            message_hash = (
                hashlib.md5(message.encode("utf-8")).hexdigest() if message else None
            )
            row = HumanGuidanceUsage(
                guidance_id=guidance_id,
                project_id=project_id,
                session_id=session_id,
                task_id=task_id,
                rendered=True,
                trimmed=False,
                source="human_guidance_table",
                render_position=position,
                rendered_chars=len(message),
                message_hash=message_hash,
            )
            db.add(row)
        db.commit()
    except Exception as exc:
        logger.warning("record_guidance_usage failed (non-fatal): %s", exc)
        try:
            db.rollback()
        except Exception:
            pass
