"""Manual synchronization service for knowledge items (Phase 16C-2)."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy.orm import Session

from app.models import KnowledgeItem, KnowledgeLifecycleEvent
from app.services.knowledge.knowledge_lifecycle_service import KnowledgeNotFoundError
from app.services.knowledge.knowledge_service import KnowledgeService


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


def _sha256(content: str) -> str:
    return hashlib.sha256(content.encode()).hexdigest()


class KnowledgeSyncError(Exception):
    pass


class KnowledgeSyncService:
    def __init__(self, knowledge_service: KnowledgeService) -> None:
        self._ksvc = knowledge_service

    def sync(
        self,
        db: Session,
        knowledge_id: str,
        *,
        actor: Optional[str] = None,
    ) -> KnowledgeItem:
        item = db.query(KnowledgeItem).filter(KnowledgeItem.id == knowledge_id).first()
        if item is None:
            raise KnowledgeNotFoundError(f"Knowledge item {knowledge_id!r} not found")

        if item.sync_status == "synced":
            return item

        previous_status = item.sync_status
        item.sync_status = "syncing"
        db.flush()

        try:
            new_checksum = _sha256(item.content)
            self._ksvc.ingest(item)
            item.checksum = new_checksum
            item.sync_status = "synced"
            item.sync_required_at = None
            item.last_synced_at = _utcnow()
            item.last_sync_error = None
            event = KnowledgeLifecycleEvent(
                knowledge_item_id=item.id,
                event_type="synced",
                payload={
                    "previous_status": previous_status,
                    "new_status": "synced",
                    "checksum": new_checksum,
                },
                actor=actor,
                reason=None,
            )
        except KnowledgeSyncError:
            raise
        except Exception as exc:
            item.sync_status = "failed"
            item.last_sync_error = str(exc)
            event = KnowledgeLifecycleEvent(
                knowledge_item_id=item.id,
                event_type="sync_failed",
                payload={
                    "previous_status": previous_status,
                    "new_status": "failed",
                    "error": str(exc),
                },
                actor=actor,
                reason=None,
            )
            db.add(event)
            db.commit()
            db.refresh(item)
            raise KnowledgeSyncError(str(exc)) from exc

        db.add(event)
        db.commit()
        db.refresh(item)
        return item
