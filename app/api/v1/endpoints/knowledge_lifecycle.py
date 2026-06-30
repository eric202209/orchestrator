"""Knowledge lifecycle endpoints — GET, PATCH, retire, restore, revisions, events, usage drilldown."""

from __future__ import annotations

from datetime import datetime
from typing import Any, Dict, List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy.orm import Session

from app.database import get_db
from app.dependencies import get_current_active_user
from app.models import User
from app.config import settings
from app.services.knowledge.knowledge_lifecycle_service import (
    ImmutableFieldError,
    KnowledgeLifecycleService,
    KnowledgeNotFoundError,
    UnknownFieldError,
)
from app.services.knowledge.knowledge_service import KnowledgeService
from app.services.knowledge.knowledge_sync_service import (
    KnowledgeSyncError,
    KnowledgeSyncService,
)
from app.services.knowledge.knowledge_usage_drilldown_service import (
    KnowledgeUsageDrilldownService,
)

router = APIRouter()

_service = KnowledgeLifecycleService()
_drilldown = KnowledgeUsageDrilldownService()


def _build_sync_service() -> KnowledgeSyncService:
    ksvc = KnowledgeService(
        qdrant_url=settings.QDRANT_URL,
        collection_name=settings.QDRANT_COLLECTION_NAME,
    )
    return KnowledgeSyncService(ksvc)


# ---------------------------------------------------------------------------
# Response schemas
# ---------------------------------------------------------------------------


class KnowledgeLifecycleItemResponse(BaseModel):
    id: str
    title: str
    content: str
    source_path: Optional[str]
    knowledge_type: str
    tags: Optional[list]
    applies_to: Optional[list]
    tool_name: Optional[str]
    failure_signature: Optional[str]
    priority: int
    project_scope: Optional[str]
    is_active: bool
    version: int
    checksum: str
    sync_status: str
    sync_required_at: Optional[datetime]
    last_synced_at: Optional[datetime]
    last_sync_error: Optional[str]
    created_at: Optional[datetime]
    updated_at: Optional[datetime]

    model_config = ConfigDict(from_attributes=True)


class KnowledgeRevisionResponse(BaseModel):
    id: int
    knowledge_item_id: str
    version: int
    previous_version: int
    changed_fields: list
    before_snapshot: dict
    after_snapshot: dict
    change_reason: Optional[str]
    created_by: Optional[str]
    created_at: Optional[datetime]

    model_config = ConfigDict(from_attributes=True)


class KnowledgeLifecycleEventResponse(BaseModel):
    id: int
    knowledge_item_id: str
    event_type: str
    payload: Optional[Any]
    actor: Optional[str]
    reason: Optional[str]
    created_at: Optional[datetime]

    model_config = ConfigDict(from_attributes=True)


class PaginatedRevisions(BaseModel):
    items: List[KnowledgeRevisionResponse]
    total: int
    page: int
    page_size: int


class PaginatedEvents(BaseModel):
    items: List[KnowledgeLifecycleEventResponse]
    total: int
    page: int
    page_size: int


class KnowledgeUsageLogResponse(BaseModel):
    id: str
    session_id: int
    task_id: Optional[int]
    trigger_phase: str
    retrieval_reason: str
    retrieval_query: Optional[str]
    confidence: float
    rank: int
    used_in_prompt: bool
    was_effective: Optional[bool]
    created_at: Optional[datetime]

    model_config = ConfigDict(from_attributes=True)


class PaginatedUsageLogs(BaseModel):
    items: List[KnowledgeUsageLogResponse]
    total: int
    page: int
    page_size: int


class KnowledgeUsageSummaryResponse(BaseModel):
    knowledge_item_id: str
    retrieval_count: int
    used_in_prompt_count: int
    effective_count: int
    knowledge_hit_rate: Optional[float]
    effectiveness_rate: Optional[float]
    avg_confidence: Optional[float]
    phase_distribution: Dict[str, int]
    recent_sessions: List[int]
    recent_tasks: List[int]


# ---------------------------------------------------------------------------
# Request schemas
# ---------------------------------------------------------------------------


class KnowledgeItemUpdateRequest(BaseModel):
    title: Optional[str] = None
    content: Optional[str] = None
    tags: Optional[list] = None
    priority: Optional[int] = None
    applies_to: Optional[list] = None
    tool_name: Optional[str] = None
    failure_signature: Optional[str] = None
    knowledge_type: Optional[str] = None
    reason: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


class KnowledgeActionRequest(BaseModel):
    reason: Optional[str] = None

    model_config = ConfigDict(extra="forbid")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _not_found(knowledge_id: str) -> HTTPException:
    return HTTPException(
        status_code=status.HTTP_404_NOT_FOUND,
        detail=f"Knowledge item {knowledge_id!r} not found",
    )


def _actor(user: User) -> Optional[str]:
    return getattr(user, "email", None)


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.get(
    "/knowledge/{knowledge_id}",
    response_model=KnowledgeLifecycleItemResponse,
    tags=["knowledge-lifecycle"],
)
def get_knowledge_item(
    knowledge_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    try:
        item = _service.get(db, knowledge_id)
    except KnowledgeNotFoundError:
        raise _not_found(knowledge_id)
    return item


@router.patch(
    "/knowledge/{knowledge_id}",
    response_model=KnowledgeLifecycleItemResponse,
    tags=["knowledge-lifecycle"],
)
def update_knowledge_item(
    knowledge_id: str,
    body: KnowledgeItemUpdateRequest,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    dumped = body.model_dump(exclude_unset=True)
    reason = dumped.pop("reason", None)
    fields: dict[str, Any] = dumped
    try:
        item = _service.update(
            db, knowledge_id, fields, reason=reason, actor=_actor(current_user)
        )
    except KnowledgeNotFoundError:
        raise _not_found(knowledge_id)
    except (ImmutableFieldError, UnknownFieldError) as exc:
        raise HTTPException(
            status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
        )
    return item


@router.post(
    "/knowledge/{knowledge_id}/retire",
    response_model=KnowledgeLifecycleItemResponse,
    tags=["knowledge-lifecycle"],
)
def retire_knowledge_item(
    knowledge_id: str,
    body: KnowledgeActionRequest = KnowledgeActionRequest(),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    try:
        item = _service.retire(
            db, knowledge_id, reason=body.reason, actor=_actor(current_user)
        )
    except KnowledgeNotFoundError:
        raise _not_found(knowledge_id)
    return item


@router.post(
    "/knowledge/{knowledge_id}/restore",
    response_model=KnowledgeLifecycleItemResponse,
    tags=["knowledge-lifecycle"],
)
def restore_knowledge_item(
    knowledge_id: str,
    body: KnowledgeActionRequest = KnowledgeActionRequest(),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    try:
        item = _service.restore(
            db, knowledge_id, reason=body.reason, actor=_actor(current_user)
        )
    except KnowledgeNotFoundError:
        raise _not_found(knowledge_id)
    return item


@router.post(
    "/knowledge/{knowledge_id}/sync",
    response_model=KnowledgeLifecycleItemResponse,
    tags=["knowledge-lifecycle"],
)
def sync_knowledge_item(
    knowledge_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    svc = _build_sync_service()
    try:
        item = svc.sync(db, knowledge_id, actor=_actor(current_user))
    except KnowledgeNotFoundError:
        raise _not_found(knowledge_id)
    except KnowledgeSyncError as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Sync failed: {exc}",
        )
    return item


@router.get(
    "/knowledge/{knowledge_id}/revisions",
    response_model=PaginatedRevisions,
    tags=["knowledge-lifecycle"],
)
def list_knowledge_revisions(
    knowledge_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    try:
        items, total = _service.get_revisions(
            db, knowledge_id, page=page, page_size=page_size
        )
    except KnowledgeNotFoundError:
        raise _not_found(knowledge_id)
    return PaginatedRevisions(items=items, total=total, page=page, page_size=page_size)


@router.get(
    "/knowledge/{knowledge_id}/events",
    response_model=PaginatedEvents,
    tags=["knowledge-lifecycle"],
)
def list_knowledge_events(
    knowledge_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    try:
        items, total = _service.get_events(
            db, knowledge_id, page=page, page_size=page_size
        )
    except KnowledgeNotFoundError:
        raise _not_found(knowledge_id)
    return PaginatedEvents(items=items, total=total, page=page, page_size=page_size)


@router.get(
    "/knowledge/{knowledge_id}/usage",
    response_model=PaginatedUsageLogs,
    tags=["knowledge-lifecycle"],
)
def list_knowledge_usage(
    knowledge_id: str,
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    trigger_phase: Optional[str] = Query(None),
    used_in_prompt: Optional[bool] = Query(None),
    was_effective: Optional[bool] = Query(None),
    session_id: Optional[int] = Query(None),
    task_id: Optional[int] = Query(None),
    created_after: Optional[datetime] = Query(None),
    created_before: Optional[datetime] = Query(None),
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    try:
        items, total = _drilldown.get_usage_list(
            db,
            knowledge_id,
            page=page,
            page_size=page_size,
            trigger_phase=trigger_phase,
            used_in_prompt=used_in_prompt,
            was_effective=was_effective,
            session_id=session_id,
            task_id=task_id,
            created_after=created_after,
            created_before=created_before,
        )
    except KnowledgeNotFoundError:
        raise _not_found(knowledge_id)
    return PaginatedUsageLogs(items=items, total=total, page=page, page_size=page_size)


@router.get(
    "/knowledge/{knowledge_id}/usage/summary",
    response_model=KnowledgeUsageSummaryResponse,
    tags=["knowledge-lifecycle"],
)
def get_knowledge_usage_summary(
    knowledge_id: str,
    db: Session = Depends(get_db),
    current_user: User = Depends(get_current_active_user),
):
    try:
        summary = _drilldown.get_usage_summary(db, knowledge_id)
    except KnowledgeNotFoundError:
        raise _not_found(knowledge_id)
    return summary
