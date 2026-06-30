"""Knowledge layer API endpoints."""

from __future__ import annotations

import hashlib
from typing import List, Optional

from fastapi import APIRouter, Depends, HTTPException, Query, status
from pydantic import BaseModel, ConfigDict
from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.config import settings
from app.database import get_db
from app.dependencies import get_current_active_user
from app.models import KnowledgeItem
from app.schemas.knowledge import KnowledgeContext, KnowledgeType
from app.services.knowledge.knowledge_service import KnowledgeService

router = APIRouter()


# ---------------------------------------------------------------------------
# Request / response schemas (local — not in app/schemas/knowledge.py because
# they are API-boundary types, not domain types)
# ---------------------------------------------------------------------------


class KnowledgeItemCreate(BaseModel):
    title: str
    content: str
    source_path: Optional[str] = None
    knowledge_type: str
    tags: Optional[list] = None
    applies_to: Optional[list] = None
    tool_name: Optional[str] = None
    failure_signature: Optional[str] = None
    priority: int = 0
    project_scope: Optional[str] = None


class KnowledgeItemResponse(BaseModel):
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

    model_config = ConfigDict(from_attributes=True)


class KnowledgeIngestResponse(BaseModel):
    id: str
    checksum: str


class KnowledgeQueryRequest(BaseModel):
    query: str
    trigger_phase: str
    knowledge_types: List[str] = []
    top_k: int = 3


class PaginatedKnowledgeItems(BaseModel):
    items: List[KnowledgeItemResponse]
    total: int
    page: int
    page_size: int


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


def _get_knowledge_service() -> KnowledgeService:
    return KnowledgeService(
        qdrant_url=settings.QDRANT_URL,
        collection_name=settings.QDRANT_COLLECTION_NAME,
    )


# ---------------------------------------------------------------------------
# Endpoints
# ---------------------------------------------------------------------------


@router.post(
    "/ingest",
    response_model=KnowledgeIngestResponse,
    status_code=status.HTTP_200_OK,
)
def ingest_knowledge_item(
    body: KnowledgeItemCreate,
    db: Session = Depends(get_db),
    _current_user=Depends(get_current_active_user),
):
    checksum = _sha256(body.content)

    existing = (
        db.query(KnowledgeItem)
        .filter(
            KnowledgeItem.source_path == body.source_path,
            KnowledgeItem.checksum == checksum,
        )
        .first()
        if body.source_path
        else None
    )
    if existing:
        return KnowledgeIngestResponse(id=existing.id, checksum=existing.checksum)

    item = KnowledgeItem(
        title=body.title,
        content=body.content,
        source_path=body.source_path,
        knowledge_type=body.knowledge_type,
        tags=body.tags or [],
        applies_to=body.applies_to or [],
        tool_name=body.tool_name,
        failure_signature=body.failure_signature,
        priority=body.priority,
        project_scope=body.project_scope,
        checksum=checksum,
    )
    db.add(item)
    db.flush()

    try:
        svc = _get_knowledge_service()
        svc.ingest(item)
    except Exception as exc:
        db.rollback()
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Qdrant unavailable: {exc}",
        )

    db.commit()
    return KnowledgeIngestResponse(id=item.id, checksum=item.checksum)


@router.get(
    "/items",
    response_model=PaginatedKnowledgeItems,
)
def list_knowledge_items(
    knowledge_type: Optional[str] = Query(None),
    search: Optional[str] = Query(None),
    include_retired: bool = Query(True),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
    db: Session = Depends(get_db),
    _current_user=Depends(get_current_active_user),
):
    q = db.query(KnowledgeItem)
    if not include_retired:
        q = q.filter(KnowledgeItem.is_active.is_(True))
    if knowledge_type:
        q = q.filter(KnowledgeItem.knowledge_type == knowledge_type)
    if search:
        pattern = f"%{search.strip()}%"
        q = q.filter(
            or_(
                KnowledgeItem.title.ilike(pattern),
                KnowledgeItem.content.ilike(pattern),
            )
        )
    total = q.count()
    items = q.offset((page - 1) * page_size).limit(page_size).all()
    return PaginatedKnowledgeItems(
        items=items,
        total=total,
        page=page,
        page_size=page_size,
    )


@router.post(
    "/query",
    response_model=KnowledgeContext,
)
def query_knowledge(
    body: KnowledgeQueryRequest,
    db: Session = Depends(get_db),
    _current_user=Depends(get_current_active_user),
):
    try:
        svc = _get_knowledge_service()
        return svc.retrieve(
            query=body.query,
            trigger_phase=body.trigger_phase,
            knowledge_types=body.knowledge_types,
            top_k=body.top_k,
            db=db,
        )
    except Exception as exc:
        raise HTTPException(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            detail=f"Qdrant unavailable: {exc}",
        )
