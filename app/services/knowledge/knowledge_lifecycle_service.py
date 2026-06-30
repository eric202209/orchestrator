"""Lifecycle operations for KnowledgeItem records."""

from __future__ import annotations

from datetime import datetime, timezone
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.models import KnowledgeItem, KnowledgeItemRevision, KnowledgeLifecycleEvent


def _utcnow() -> datetime:
    return datetime.now(timezone.utc)


_EDITABLE_FIELDS = frozenset(
    {
        "title",
        "content",
        "tags",
        "priority",
        "applies_to",
        "tool_name",
        "failure_signature",
        "knowledge_type",
    }
)

_IMMUTABLE_FIELDS = frozenset({"id", "created_at", "checksum", "version"})

# Fields captured in before/after snapshots
_SNAPSHOT_FIELDS = (
    "title",
    "content",
    "knowledge_type",
    "tags",
    "applies_to",
    "tool_name",
    "failure_signature",
    "priority",
    "is_active",
)


class KnowledgeLifecycleError(Exception):
    pass


class KnowledgeNotFoundError(KnowledgeLifecycleError):
    pass


class ImmutableFieldError(KnowledgeLifecycleError):
    pass


class UnknownFieldError(KnowledgeLifecycleError):
    pass


def _snapshot(item: KnowledgeItem) -> dict[str, Any]:
    return {f: getattr(item, f) for f in _SNAPSHOT_FIELDS}


class KnowledgeLifecycleService:
    def get(self, db: Session, knowledge_id: str) -> KnowledgeItem:
        item = db.query(KnowledgeItem).filter(KnowledgeItem.id == knowledge_id).first()
        if item is None:
            raise KnowledgeNotFoundError(f"Knowledge item {knowledge_id!r} not found")
        return item

    def update(
        self,
        db: Session,
        knowledge_id: str,
        fields: dict[str, Any],
        *,
        reason: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> KnowledgeItem:
        item = self.get(db, knowledge_id)

        immutable_attempted = _IMMUTABLE_FIELDS & set(fields)
        if immutable_attempted:
            raise ImmutableFieldError(
                f"Fields are immutable and cannot be updated: {sorted(immutable_attempted)}"
            )

        unknown = set(fields) - _EDITABLE_FIELDS
        if unknown:
            raise UnknownFieldError(
                f"Unknown or non-editable fields: {sorted(unknown)}"
            )

        before = _snapshot(item)

        changed: list[str] = []
        for field, value in fields.items():
            if getattr(item, field) != value:
                changed.append(field)
                setattr(item, field, value)

        if changed:
            previous_version = item.version
            item.version = previous_version + 1
            item.sync_status = "dirty"
            item.sync_required_at = _utcnow()
            item.last_sync_error = None
            after = _snapshot(item)

            revision = KnowledgeItemRevision(
                knowledge_item_id=item.id,
                version=item.version,
                previous_version=previous_version,
                changed_fields=sorted(changed),
                before_snapshot=before,
                after_snapshot=after,
                change_reason=reason,
                created_by=actor,
            )
            db.add(revision)

            event = KnowledgeLifecycleEvent(
                knowledge_item_id=item.id,
                event_type="updated",
                payload={"changed_fields": sorted(changed), "version": item.version},
                actor=actor,
                reason=reason,
            )
            db.add(event)

        db.commit()
        db.refresh(item)
        return item

    def retire(
        self,
        db: Session,
        knowledge_id: str,
        *,
        reason: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> KnowledgeItem:
        item = self.get(db, knowledge_id)
        if not item.is_active:
            return item
        item.is_active = False
        event = KnowledgeLifecycleEvent(
            knowledge_item_id=item.id,
            event_type="retired",
            payload=None,
            actor=actor,
            reason=reason,
        )
        db.add(event)
        db.commit()
        db.refresh(item)
        return item

    def restore(
        self,
        db: Session,
        knowledge_id: str,
        *,
        reason: Optional[str] = None,
        actor: Optional[str] = None,
    ) -> KnowledgeItem:
        item = self.get(db, knowledge_id)
        if item.is_active:
            return item
        item.is_active = True
        event = KnowledgeLifecycleEvent(
            knowledge_item_id=item.id,
            event_type="restored",
            payload=None,
            actor=actor,
            reason=reason,
        )
        db.add(event)
        db.commit()
        db.refresh(item)
        return item

    def get_revisions(
        self,
        db: Session,
        knowledge_id: str,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[KnowledgeItemRevision], int]:
        self.get(db, knowledge_id)  # raises if missing
        q = (
            db.query(KnowledgeItemRevision)
            .filter(KnowledgeItemRevision.knowledge_item_id == knowledge_id)
            .order_by(KnowledgeItemRevision.version.desc())
        )
        total = q.count()
        items = q.offset((page - 1) * page_size).limit(page_size).all()
        return items, total

    def get_events(
        self,
        db: Session,
        knowledge_id: str,
        *,
        page: int = 1,
        page_size: int = 20,
    ) -> tuple[list[KnowledgeLifecycleEvent], int]:
        self.get(db, knowledge_id)  # raises if missing
        q = (
            db.query(KnowledgeLifecycleEvent)
            .filter(KnowledgeLifecycleEvent.knowledge_item_id == knowledge_id)
            .order_by(KnowledgeLifecycleEvent.created_at.desc())
        )
        total = q.count()
        items = q.offset((page - 1) * page_size).limit(page_size).all()
        return items, total
