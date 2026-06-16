"""Human Guidance API — HG-P1a endpoints."""

from __future__ import annotations

from datetime import datetime
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, status
from pydantic import BaseModel, Field
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_active_user
from app.models import GuidanceStatus, HumanGuidance
from app.services.authz import get_project_for_user
from app.services.human_guidance_service import (
    archive_guidance,
    create_guidance,
    get_guidance,
    list_guidance,
    update_guidance,
    _UNSET,
)

router = APIRouter()


# ── request / response schemas ────────────────────────────────────────────────


class CreateGuidanceRequest(BaseModel):
    message: str = Field(..., max_length=500)
    scope: str = "project"
    priority: int = Field(0, ge=0, le=100)
    expires_at: Optional[datetime] = None


class PatchGuidanceRequest(BaseModel):
    message: Optional[str] = Field(None, max_length=500)
    status: Optional[str] = None
    priority: Optional[int] = Field(None, ge=0, le=100)
    expires_at: Optional[datetime] = None
    change_reason: Optional[str] = None


def _serialize(g: HumanGuidance, *, full: bool = False) -> dict:
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

    entry, created = create_guidance(
        db,
        user_id=current_user.id,
        project_id=project_id,
        scope=body.scope,
        message=body.message,
        priority=body.priority,
        expires_at=body.expires_at,
        created_by=getattr(current_user, "email", None),
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
