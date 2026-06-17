"""Human Guidance API — HG-P1a/P1c/P1d/P1e endpoints."""

from __future__ import annotations

import json
import logging
from datetime import datetime, timezone
from typing import List, Optional

logger = logging.getLogger(__name__)

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_active_user
from app.models import GuidanceStatus, HumanGuidance
from app.services.authz import get_project_for_user
from app.services.human_guidance_service import (
    VALID_BACKENDS,
    _UNSET,
    archive_guidance,
    collect_active_guidance,
    create_guidance,
    get_guidance,
    get_guidance_history,
    list_global_guidance,
    list_guidance,
    update_guidance,
)

router = APIRouter()


# ── request / response schemas ────────────────────────────────────────────────


class CreateGuidanceRequest(BaseModel):
    message: str = Field(..., max_length=500)
    scope: str = "project"
    priority: int = Field(0, ge=0, le=100)
    expires_at: Optional[datetime] = None
    backend_targets: Optional[List[str]] = None


class PatchGuidanceRequest(BaseModel):
    message: Optional[str] = Field(None, max_length=500)
    status: Optional[str] = None
    priority: Optional[int] = Field(None, ge=0, le=100)
    expires_at: Optional[datetime] = None
    change_reason: Optional[str] = None


def _serialize(g: HumanGuidance, *, full: bool = False) -> dict:
    from app.services.human_guidance_service import _parse_backend_targets

    out = {
        "id": g.id,
        "project_id": g.project_id,
        "session_id": g.session_id,
        "task_id": g.task_id,
        "scope": g.scope.value if hasattr(g.scope, "value") else g.scope,
        "message": g.message,
        "status": g.status.value if hasattr(g.status, "value") else g.status,
        "priority": g.priority,
        "created_at": g.created_at.isoformat() if g.created_at else None,
        "updated_at": g.updated_at.isoformat() if g.updated_at else None,
        "expires_at": g.expires_at.isoformat() if g.expires_at else None,
        "created_by": g.created_by,
        "revision": g.revision,
        "backend_targets": _parse_backend_targets(getattr(g, "backend_targets", None)),
    }
    if full:
        out["disabled_at"] = g.disabled_at.isoformat() if g.disabled_at else None
        out["archived_at"] = g.archived_at.isoformat() if g.archived_at else None
        out["conflict_warnings"] = []
    return out


_VALID_SCOPES = {"global", "project", "session", "task"}


# ── endpoints ─────────────────────────────────────────────────────────────────


@router.post(
    "/projects/{project_id}/guidance",
    status_code=status.HTTP_201_CREATED,
)
def create_project_guidance(
    project_id: int,
    body: CreateGuidanceRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Create project-scoped (or global) guidance entry."""
    get_project_for_user(db, project_id, current_user)

    if body.scope not in _VALID_SCOPES:
        raise HTTPException(status_code=400, detail="invalid_scope")

    backend_targets = body.backend_targets or ["all"]
    invalid_backends = [b for b in backend_targets if b not in VALID_BACKENDS]
    if invalid_backends:
        raise HTTPException(status_code=400, detail="invalid_backend_target")

    entry, created = create_guidance(
        db,
        user_id=current_user.id,
        project_id=project_id,
        scope=body.scope,
        message=body.message,
        priority=body.priority,
        expires_at=body.expires_at,
        created_by=getattr(current_user, "email", None),
        backend_targets=backend_targets,
    )
    if not created:
        from fastapi.responses import JSONResponse

        return JSONResponse(
            status_code=200,
            content=_serialize(entry),
        )
    return _serialize(entry)


@router.get("/projects/{project_id}/guidance")
def list_project_guidance(
    project_id: int,
    status: str = "active",
    scope: Optional[str] = None,
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """List guidance entries for a project."""
    get_project_for_user(db, project_id, current_user)

    valid_statuses = {"active", "disabled", "archived", "expired", "all"}
    if status not in valid_statuses:
        raise HTTPException(status_code=400, detail="invalid_status")
    if scope and scope not in _VALID_SCOPES:
        raise HTTPException(status_code=400, detail="invalid_scope")

    items, total = list_guidance(
        db,
        project_id=project_id,
        status=status,
        scope=scope,
        limit=limit,
        offset=offset,
    )
    return {
        "project_id": project_id,
        "total": total,
        "items": [_serialize(g) for g in items],
    }


class PatchConflictRequest(BaseModel):
    status: str
    resolution_note: Optional[str] = None


class ActivationPatchRequest(BaseModel):
    table_enabled: bool = False
    persistence_enabled: bool = False
    render_enabled: bool = False
    injection_enabled: bool = False
    conflict_detection_enabled: bool = False


def _serialize_conflict_row(row: object) -> dict:
    try:
        patterns = json.loads(getattr(row, "conflict_patterns", None) or "[]")
    except Exception:
        patterns = []
    detected = getattr(row, "detected_at", None)
    resolved = getattr(row, "resolved_at", None)
    row_status = getattr(row, "status", "open")
    return {
        "id": getattr(row, "id", None),
        "guidance_id": getattr(row, "guidance_id", None),
        "guidance_message": getattr(row, "guidance_message", ""),
        "task_id": getattr(row, "task_id", None),
        "task_title": getattr(row, "task_title", "") or "",
        "conflict_excerpt": getattr(row, "conflict_excerpt", "") or "",
        "conflict_patterns": patterns,
        "severity": getattr(row, "severity", "warning"),
        "status": row_status,
        "detected_at": detected.isoformat() if detected else None,
        "resolved": row_status in ("resolved", "ignored"),
    }


@router.get("/projects/{project_id}/guidance/conflicts")
def list_guidance_conflicts(
    project_id: int,
    status: str = "open",
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Return guidance conflict records for a project, read from HumanGuidanceConflict table."""
    from app.models import HumanGuidanceConflict

    get_project_for_user(db, project_id, current_user)

    valid_statuses = {"open", "resolved", "ignored", "all"}
    if status not in valid_statuses:
        raise HTTPException(status_code=400, detail="invalid_status")

    try:
        query = db.query(HumanGuidanceConflict).filter(
            HumanGuidanceConflict.project_id == project_id
        )
        if status != "all":
            query = query.filter(HumanGuidanceConflict.status == status)

        total = query.count()
        rows = (
            query.order_by(HumanGuidanceConflict.detected_at.desc())
            .offset(offset)
            .limit(limit)
            .all()
        )
        return {
            "project_id": project_id,
            "total": total,
            "items": [_serialize_conflict_row(r) for r in rows],
        }
    except Exception as exc:
        logger.warning("[GUIDANCE_CONFLICTS] Failed to read conflict table: %s", exc)
        return {"project_id": project_id, "total": 0, "items": []}


@router.patch("/projects/{project_id}/guidance/conflicts/{conflict_id}")
def patch_guidance_conflict(
    project_id: int,
    conflict_id: int,
    body: PatchConflictRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Resolve, ignore, or reopen a guidance conflict."""
    from app.models import HumanGuidanceConflict

    get_project_for_user(db, project_id, current_user)

    valid_statuses = {"open", "resolved", "ignored"}
    if body.status not in valid_statuses:
        raise HTTPException(status_code=422, detail="invalid_status")

    row = (
        db.query(HumanGuidanceConflict)
        .filter(
            HumanGuidanceConflict.id == conflict_id,
            HumanGuidanceConflict.project_id == project_id,
        )
        .first()
    )
    if row is None:
        raise HTTPException(status_code=404, detail="conflict_not_found")

    row.status = body.status
    if body.resolution_note is not None:
        row.resolution_note = body.resolution_note

    if body.status in ("resolved", "ignored"):
        row.resolved_at = datetime.now(timezone.utc)
        row.resolved_by = getattr(current_user, "email", None)
    elif body.status == "open":
        row.resolved_at = None
        row.resolved_by = None

    db.commit()
    db.refresh(row)

    out = _serialize_conflict_row(row)
    out["resolved_at"] = row.resolved_at.isoformat() if row.resolved_at else None
    out["resolved_by"] = row.resolved_by
    out["resolution_note"] = row.resolution_note
    return out


@router.get("/guidance/global")
def list_global_guidance_endpoint(
    status: str = "active",
    limit: int = 50,
    offset: int = 0,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """List global-scope guidance for the authenticated user."""
    valid_statuses = {"active", "disabled", "archived", "expired", "all"}
    if status not in valid_statuses:
        raise HTTPException(status_code=400, detail="invalid_status")

    items, total = list_global_guidance(
        db,
        user_id=current_user.id,
        status=status,
        limit=limit,
        offset=offset,
    )
    return {"total": total, "items": [_serialize(g) for g in items]}


@router.get("/guidance/{guidance_id}/history")
def get_guidance_history_endpoint(
    guidance_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Return revision history for a guidance entry in ascending revision order."""
    entry, revisions = get_guidance_history(db, guidance_id)
    if entry.project_id:
        get_project_for_user(db, entry.project_id, current_user)
    return {
        "id": entry.id,
        "revisions": [
            {
                "revision": r.revision,
                "message": r.message,
                "changed_by": r.changed_by,
                "changed_at": r.changed_at.isoformat() if r.changed_at else None,
                "change_reason": r.change_reason,
            }
            for r in revisions
        ],
    }


@router.get("/projects/{project_id}/guidance/rendered")
def get_rendered_guidance(
    project_id: int,
    session_id: Optional[int] = None,
    task_id: Optional[int] = None,
    backend: str = "all",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Preview the Operator Guidance block without writing WM or recording telemetry."""
    from app.services.orchestration.working_memory import (
        _INJECTION_BUDGET,
        render_guidance_block,
    )
    from app.services.human_guidance_selection_service import (
        select_guidance_for_injection,
    )

    get_project_for_user(db, project_id, current_user)

    if backend not in VALID_BACKENDS:
        raise HTTPException(status_code=400, detail="invalid_backend")

    all_entries = collect_active_guidance(
        db,
        user_id=current_user.id,
        project_id=project_id,
        session_id=session_id,
        task_id=task_id,
        backend="all",
    )
    if backend == "all":
        entries = all_entries
        filtered_backend_ids: List[int] = []
    else:
        entries = collect_active_guidance(
            db,
            user_id=current_user.id,
            project_id=project_id,
            session_id=session_id,
            task_id=task_id,
            backend=backend,
        )
        all_ids = {e.get("id") for e in all_entries}
        matched_ids = {e.get("id") for e in entries}
        filtered_backend_ids = sorted(all_ids - matched_ids)

    selection = select_guidance_for_injection(entries, _INJECTION_BUDGET)
    selected_entries = selection["selected"]
    trimmed_entries = selection["trimmed"]

    body_lines = render_guidance_block(selected_entries)
    if body_lines:
        block = "Operator Guidance\n" + "\n".join(body_lines)
    else:
        block = ""

    rendered_chars = len(block)
    max_chars = _INJECTION_BUDGET
    trimmed = bool(trimmed_entries) or rendered_chars > max_chars
    if trimmed:
        block = block[:max_chars]

    return {
        "project_id": project_id,
        "rendered_chars": len(block),
        "max_chars": max_chars,
        "trimmed": trimmed,
        "selected_count": len(selected_entries),
        "trimmed_count": len(trimmed_entries),
        "selected_ids": [entry.get("id") for entry in selected_entries],
        "trimmed_ids": [entry.get("id") for entry in trimmed_entries],
        "selection_metadata": selection["selection_metadata"],
        "block": block,
        "backend": backend,
        "filtered_backend_ids": filtered_backend_ids,
    }


@router.get("/guidance/{guidance_id}")
def get_guidance_entry(
    guidance_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Get a single guidance entry by ID."""
    entry = get_guidance(db, guidance_id)
    if entry.project_id:
        get_project_for_user(db, entry.project_id, current_user)
    return _serialize(entry, full=True)


@router.patch("/guidance/{guidance_id}")
def patch_guidance_entry(
    guidance_id: int,
    body: PatchGuidanceRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Update message, status, priority, or expiry of a guidance entry."""
    entry = get_guidance(db, guidance_id)
    if entry.project_id:
        get_project_for_user(db, entry.project_id, current_user)

    provided = body.model_fields_set

    kwargs: dict = {}
    if "message" in provided:
        kwargs["message"] = body.message
    if "status" in provided:
        if body.status not in ("active", "disabled"):
            raise HTTPException(status_code=422, detail="immutable_field")
        kwargs["status"] = body.status
    if "priority" in provided:
        kwargs["priority"] = body.priority
    if "expires_at" in provided:
        kwargs["expires_at"] = body.expires_at
    if "change_reason" in provided:
        kwargs["change_reason"] = body.change_reason

    updated = update_guidance(
        db,
        guidance_id,
        changed_by=getattr(current_user, "email", None),
        **kwargs,
    )
    return _serialize(updated, full=True)


@router.delete("/guidance/{guidance_id}")
def archive_guidance_entry(
    guidance_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Soft-delete (archive) a guidance entry."""
    entry = get_guidance(db, guidance_id)
    if entry.project_id:
        get_project_for_user(db, entry.project_id, current_user)

    archived = archive_guidance(db, guidance_id)
    return {
        "id": archived.id,
        "status": (
            archived.status.value
            if hasattr(archived.status, "value")
            else archived.status
        ),
        "archived_at": (
            archived.archived_at.isoformat() if archived.archived_at else None
        ),
        "message": "Archived. Guidance will no longer affect planning.",
    }


# ── HG-P1e: activation controls ───────────────────────────────────────────────


def _serialize_activation_row(row: object) -> dict:
    created = getattr(row, "created_at", None)
    updated = getattr(row, "updated_at", None)
    disabled = getattr(row, "disabled_at", None)
    return {
        "id": getattr(row, "id", None),
        "scope": getattr(row, "scope", None),
        "project_id": getattr(row, "project_id", None),
        "session_id": getattr(row, "session_id", None),
        "table_enabled": getattr(row, "table_enabled", False),
        "persistence_enabled": getattr(row, "persistence_enabled", False),
        "render_enabled": getattr(row, "render_enabled", False),
        "injection_enabled": getattr(row, "injection_enabled", False),
        "conflict_detection_enabled": getattr(row, "conflict_detection_enabled", False),
        "status": getattr(row, "status", "disabled"),
        "enabled_by": getattr(row, "enabled_by", None),
        "disabled_at": disabled.isoformat() if disabled else None,
        "disabled_by": getattr(row, "disabled_by", None),
        "created_at": created.isoformat() if created else None,
        "updated_at": updated.isoformat() if updated else None,
    }


@router.get("/projects/{project_id}/guidance/readiness")
def get_project_guidance_readiness(
    project_id: int,
    session_id: Optional[int] = None,
    backend: str = "all",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Return Human Guidance readiness status for a project (optionally session-scoped)."""
    from app.services.human_guidance_activation_service import readiness_status

    get_project_for_user(db, project_id, current_user)
    if backend not in VALID_BACKENDS:
        raise HTTPException(status_code=400, detail="invalid_backend")
    return readiness_status(
        db, project_id=project_id, session_id=session_id, backend=backend
    )


@router.patch("/projects/{project_id}/guidance/activation")
def patch_project_activation(
    project_id: int,
    body: ActivationPatchRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Set project-level Human Guidance activation flags."""
    from app.services.human_guidance_activation_service import set_project_activation

    get_project_for_user(db, project_id, current_user)
    row = set_project_activation(
        db,
        project_id,
        body.model_dump(),
        enabled_by=getattr(current_user, "email", None),
    )
    return _serialize_activation_row(row)


@router.post("/projects/{project_id}/guidance/activation/disable")
def disable_project_activation(
    project_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Disable project-level Human Guidance activation."""
    from app.services.human_guidance_activation_service import disable_activation

    get_project_for_user(db, project_id, current_user)
    row = disable_activation(
        db, "project", project_id, disabled_by=getattr(current_user, "email", None)
    )
    return _serialize_activation_row(row)


@router.get("/sessions/{session_id}/guidance/readiness")
def get_session_guidance_readiness(
    session_id: int,
    backend: str = "all",
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Return Human Guidance readiness status for a session."""
    from app.services.authz import get_session_for_user
    from app.services.human_guidance_activation_service import readiness_status

    session = get_session_for_user(db, session_id, current_user)
    if backend not in VALID_BACKENDS:
        raise HTTPException(status_code=400, detail="invalid_backend")
    return readiness_status(
        db, project_id=session.project_id, session_id=session_id, backend=backend
    )


@router.patch("/sessions/{session_id}/guidance/activation")
def patch_session_activation(
    session_id: int,
    body: ActivationPatchRequest,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Set session-level Human Guidance activation flags (overrides project)."""
    from app.services.authz import get_session_for_user
    from app.services.human_guidance_activation_service import set_session_activation

    session = get_session_for_user(db, session_id, current_user)
    row = set_session_activation(
        db,
        session_id,
        session.project_id,
        body.model_dump(),
        enabled_by=getattr(current_user, "email", None),
    )
    return _serialize_activation_row(row)


@router.post("/sessions/{session_id}/guidance/activation/disable")
def disable_session_activation(
    session_id: int,
    db: Session = Depends(get_db),
    current_user=Depends(get_current_active_user),
):
    """Disable session-level Human Guidance activation."""
    from app.services.authz import get_session_for_user
    from app.services.human_guidance_activation_service import disable_activation

    session = get_session_for_user(db, session_id, current_user)
    row = disable_activation(
        db, "session", session_id, disabled_by=getattr(current_user, "email", None)
    )
    return _serialize_activation_row(row)
