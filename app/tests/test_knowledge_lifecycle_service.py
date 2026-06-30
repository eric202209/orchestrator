"""Tests for KnowledgeLifecycleService — Phase 16A-1 + 16A-2."""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import (
    Base,
    KnowledgeItem,
    KnowledgeItemRevision,
    KnowledgeLifecycleEvent,
)
from app.schemas.knowledge import KnowledgeType
from app.services.knowledge.knowledge_lifecycle_service import (
    ImmutableFieldError,
    KnowledgeLifecycleService,
    KnowledgeNotFoundError,
    UnknownFieldError,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


@pytest.fixture()
def svc():
    return KnowledgeLifecycleService()


def _make_item(
    db,
    *,
    title: str = "Test Item",
    content: str = "Some content.",
    knowledge_type: str = KnowledgeType.format_guide,
    applies_to: list | None = None,
    tags: list | None = None,
    failure_signature: str | None = None,
    tool_name: str | None = None,
    priority: int = 0,
    is_active: bool = True,
) -> KnowledgeItem:
    item = KnowledgeItem(
        title=title,
        content=content,
        knowledge_type=knowledge_type,
        applies_to=applies_to or ["planning"],
        tags=tags or [],
        failure_signature=failure_signature,
        tool_name=tool_name,
        priority=priority,
        is_active=is_active,
        checksum=hashlib.sha256(content.encode()).hexdigest(),
    )
    db.add(item)
    db.flush()
    return item


# ---------------------------------------------------------------------------
# Get
# ---------------------------------------------------------------------------


class TestGet:
    def test_get_existing(self, svc, db):
        item = _make_item(db, title="My Guide")
        result = svc.get(db, item.id)
        assert result.id == item.id
        assert result.title == "My Guide"

    def test_get_missing_raises(self, svc, db):
        with pytest.raises(KnowledgeNotFoundError):
            svc.get(db, "nonexistent-id")


# ---------------------------------------------------------------------------
# Update — editable fields
# ---------------------------------------------------------------------------


class TestUpdateEditableFields:
    def test_update_title(self, svc, db):
        item = _make_item(db, title="Old Title")
        updated = svc.update(db, item.id, {"title": "New Title"})
        assert updated.title == "New Title"

    def test_update_content(self, svc, db):
        item = _make_item(db, content="old content")
        updated = svc.update(db, item.id, {"content": "new content"})
        assert updated.content == "new content"

    def test_update_tags(self, svc, db):
        item = _make_item(db)
        updated = svc.update(db, item.id, {"tags": ["python", "fastapi"]})
        assert updated.tags == ["python", "fastapi"]

    def test_update_priority(self, svc, db):
        item = _make_item(db)
        updated = svc.update(db, item.id, {"priority": 10})
        assert updated.priority == 10

    def test_update_applies_to(self, svc, db):
        item = _make_item(db)
        updated = svc.update(db, item.id, {"applies_to": ["validation", "all"]})
        assert updated.applies_to == ["validation", "all"]

    def test_update_tool_name(self, svc, db):
        item = _make_item(db)
        updated = svc.update(db, item.id, {"tool_name": "bash"})
        assert updated.tool_name == "bash"

    def test_update_failure_signature(self, svc, db):
        item = _make_item(db)
        updated = svc.update(db, item.id, {"failure_signature": "json_parse_error"})
        assert updated.failure_signature == "json_parse_error"

    def test_update_knowledge_type(self, svc, db):
        item = _make_item(db, knowledge_type=KnowledgeType.format_guide)
        updated = svc.update(db, item.id, {"knowledge_type": KnowledgeType.debug_case})
        assert updated.knowledge_type == KnowledgeType.debug_case

    def test_update_multiple_fields(self, svc, db):
        item = _make_item(db, title="Old", priority=0)
        updated = svc.update(db, item.id, {"title": "New", "priority": 5})
        assert updated.title == "New"
        assert updated.priority == 5


# ---------------------------------------------------------------------------
# Update — rejection rules
# ---------------------------------------------------------------------------


class TestUpdateRejection:
    def test_immutable_id_rejected(self, svc, db):
        item = _make_item(db)
        with pytest.raises(ImmutableFieldError):
            svc.update(db, item.id, {"id": "new-id"})

    def test_immutable_created_at_rejected(self, svc, db):
        item = _make_item(db)
        with pytest.raises(ImmutableFieldError):
            svc.update(db, item.id, {"created_at": "2024-01-01"})

    def test_immutable_checksum_rejected(self, svc, db):
        item = _make_item(db)
        with pytest.raises(ImmutableFieldError):
            svc.update(db, item.id, {"checksum": "abc123"})

    def test_immutable_version_rejected(self, svc, db):
        item = _make_item(db)
        with pytest.raises(ImmutableFieldError):
            svc.update(db, item.id, {"version": 99})

    def test_unknown_field_rejected(self, svc, db):
        item = _make_item(db)
        with pytest.raises(UnknownFieldError):
            svc.update(db, item.id, {"source_path": "/some/path"})

    def test_project_scope_rejected(self, svc, db):
        item = _make_item(db)
        with pytest.raises(UnknownFieldError):
            svc.update(db, item.id, {"project_scope": "my-project"})

    def test_nonexistent_raises(self, svc, db):
        with pytest.raises(KnowledgeNotFoundError):
            svc.update(db, "bad-id", {"title": "X"})


# ---------------------------------------------------------------------------
# Update — version increment
# ---------------------------------------------------------------------------


class TestUpdateVersionIncrement:
    def test_version_incremented_on_change(self, svc, db):
        item = _make_item(db)
        assert item.version == 1
        updated = svc.update(db, item.id, {"title": "Changed"})
        assert updated.version == 2

    def test_version_not_incremented_on_noop(self, svc, db):
        item = _make_item(db, title="Same")
        svc.update(db, item.id, {"title": "Same"})
        result = svc.get(db, item.id)
        assert result.version == 1

    def test_version_not_incremented_on_empty_dict(self, svc, db):
        item = _make_item(db)
        svc.update(db, item.id, {})
        result = svc.get(db, item.id)
        assert result.version == 1

    def test_version_increments_sequentially(self, svc, db):
        item = _make_item(db)
        svc.update(db, item.id, {"title": "v2"})
        svc.update(db, item.id, {"title": "v3"})
        result = svc.get(db, item.id)
        assert result.version == 3

    def test_checksum_unchanged_after_content_update(self, svc, db):
        item = _make_item(db, content="original")
        original_checksum = item.checksum
        svc.update(db, item.id, {"content": "changed"})
        result = svc.get(db, item.id)
        assert result.checksum == original_checksum


# ---------------------------------------------------------------------------
# Update — revision rows
# ---------------------------------------------------------------------------


class TestUpdateRevisionRows:
    def test_revision_created_on_change(self, svc, db):
        item = _make_item(db, title="Before")
        svc.update(db, item.id, {"title": "After"})
        revs = (
            db.query(KnowledgeItemRevision)
            .filter(KnowledgeItemRevision.knowledge_item_id == item.id)
            .all()
        )
        assert len(revs) == 1

    def test_no_revision_on_noop(self, svc, db):
        item = _make_item(db, title="Same")
        svc.update(db, item.id, {"title": "Same"})
        revs = (
            db.query(KnowledgeItemRevision)
            .filter(KnowledgeItemRevision.knowledge_item_id == item.id)
            .all()
        )
        assert len(revs) == 0

    def test_revision_version_fields(self, svc, db):
        item = _make_item(db)
        svc.update(db, item.id, {"title": "New"})
        rev = (
            db.query(KnowledgeItemRevision)
            .filter(KnowledgeItemRevision.knowledge_item_id == item.id)
            .first()
        )
        assert rev.version == 2
        assert rev.previous_version == 1

    def test_revision_changed_fields(self, svc, db):
        item = _make_item(db)
        svc.update(db, item.id, {"title": "New", "priority": 5})
        rev = (
            db.query(KnowledgeItemRevision)
            .filter(KnowledgeItemRevision.knowledge_item_id == item.id)
            .first()
        )
        assert "title" in rev.changed_fields
        assert "priority" in rev.changed_fields

    def test_revision_before_after_snapshot(self, svc, db):
        item = _make_item(db, title="Before", priority=0)
        svc.update(db, item.id, {"title": "After", "priority": 7})
        rev = (
            db.query(KnowledgeItemRevision)
            .filter(KnowledgeItemRevision.knowledge_item_id == item.id)
            .first()
        )
        assert rev.before_snapshot["title"] == "Before"
        assert rev.before_snapshot["priority"] == 0
        assert rev.after_snapshot["title"] == "After"
        assert rev.after_snapshot["priority"] == 7

    def test_revision_reason_and_actor(self, svc, db):
        item = _make_item(db)
        svc.update(
            db,
            item.id,
            {"title": "New"},
            reason="fixing outdated info",
            actor="ops@example.com",
        )
        rev = (
            db.query(KnowledgeItemRevision)
            .filter(KnowledgeItemRevision.knowledge_item_id == item.id)
            .first()
        )
        assert rev.change_reason == "fixing outdated info"
        assert rev.created_by == "ops@example.com"

    def test_multiple_updates_create_multiple_revisions(self, svc, db):
        item = _make_item(db)
        svc.update(db, item.id, {"title": "v2"})
        svc.update(db, item.id, {"title": "v3"})
        revs = (
            db.query(KnowledgeItemRevision)
            .filter(KnowledgeItemRevision.knowledge_item_id == item.id)
            .all()
        )
        assert len(revs) == 2


# ---------------------------------------------------------------------------
# Retire
# ---------------------------------------------------------------------------


class TestRetire:
    def test_retire_active_item(self, svc, db):
        item = _make_item(db, is_active=True)
        retired = svc.retire(db, item.id)
        assert retired.is_active is False

    def test_retire_already_retired_is_idempotent(self, svc, db):
        item = _make_item(db, is_active=False)
        result = svc.retire(db, item.id)
        assert result.is_active is False

    def test_retire_nonexistent_raises(self, svc, db):
        with pytest.raises(KnowledgeNotFoundError):
            svc.retire(db, "no-such-item")

    def test_retire_does_not_delete(self, svc, db):
        item = _make_item(db)
        svc.retire(db, item.id)
        found = db.query(KnowledgeItem).filter(KnowledgeItem.id == item.id).first()
        assert found is not None
        assert found.is_active is False

    def test_retire_creates_event(self, svc, db):
        item = _make_item(db, is_active=True)
        svc.retire(db, item.id)
        events = (
            db.query(KnowledgeLifecycleEvent)
            .filter(KnowledgeLifecycleEvent.knowledge_item_id == item.id)
            .all()
        )
        assert len(events) == 1
        assert events[0].event_type == "retired"

    def test_retire_idempotent_no_duplicate_event(self, svc, db):
        item = _make_item(db, is_active=False)
        svc.retire(db, item.id)
        events = (
            db.query(KnowledgeLifecycleEvent)
            .filter(KnowledgeLifecycleEvent.knowledge_item_id == item.id)
            .all()
        )
        assert len(events) == 0

    def test_retire_event_stores_reason_and_actor(self, svc, db):
        item = _make_item(db)
        svc.retire(
            db, item.id, reason="deprecated by new version", actor="admin@example.com"
        )
        ev = (
            db.query(KnowledgeLifecycleEvent)
            .filter(KnowledgeLifecycleEvent.knowledge_item_id == item.id)
            .first()
        )
        assert ev.reason == "deprecated by new version"
        assert ev.actor == "admin@example.com"


# ---------------------------------------------------------------------------
# Restore
# ---------------------------------------------------------------------------


class TestRestore:
    def test_restore_retired_item(self, svc, db):
        item = _make_item(db, is_active=False)
        restored = svc.restore(db, item.id)
        assert restored.is_active is True

    def test_restore_already_active_is_idempotent(self, svc, db):
        item = _make_item(db, is_active=True)
        result = svc.restore(db, item.id)
        assert result.is_active is True

    def test_restore_nonexistent_raises(self, svc, db):
        with pytest.raises(KnowledgeNotFoundError):
            svc.restore(db, "no-such-item")

    def test_restore_creates_event(self, svc, db):
        item = _make_item(db, is_active=False)
        svc.restore(db, item.id)
        events = (
            db.query(KnowledgeLifecycleEvent)
            .filter(KnowledgeLifecycleEvent.knowledge_item_id == item.id)
            .all()
        )
        assert len(events) == 1
        assert events[0].event_type == "restored"

    def test_restore_idempotent_no_duplicate_event(self, svc, db):
        item = _make_item(db, is_active=True)
        svc.restore(db, item.id)
        events = (
            db.query(KnowledgeLifecycleEvent)
            .filter(KnowledgeLifecycleEvent.knowledge_item_id == item.id)
            .all()
        )
        assert len(events) == 0

    def test_restore_event_stores_reason_and_actor(self, svc, db):
        item = _make_item(db, is_active=False)
        svc.restore(
            db, item.id, reason="re-enabling after review", actor="ops@example.com"
        )
        ev = (
            db.query(KnowledgeLifecycleEvent)
            .filter(KnowledgeLifecycleEvent.knowledge_item_id == item.id)
            .first()
        )
        assert ev.reason == "re-enabling after review"
        assert ev.actor == "ops@example.com"


# ---------------------------------------------------------------------------
# Retire + Restore roundtrip
# ---------------------------------------------------------------------------


class TestRetireRestoreRoundtrip:
    def test_retire_then_restore(self, svc, db):
        item = _make_item(db, is_active=True)
        svc.retire(db, item.id)
        svc.restore(db, item.id)
        result = svc.get(db, item.id)
        assert result.is_active is True

    def test_retire_restore_creates_two_events(self, svc, db):
        item = _make_item(db, is_active=True)
        svc.retire(db, item.id)
        svc.restore(db, item.id)
        events = (
            db.query(KnowledgeLifecycleEvent)
            .filter(KnowledgeLifecycleEvent.knowledge_item_id == item.id)
            .order_by(KnowledgeLifecycleEvent.created_at)
            .all()
        )
        assert len(events) == 2
        assert events[0].event_type == "retired"
        assert events[1].event_type == "restored"


# ---------------------------------------------------------------------------
# Revisions endpoint
# ---------------------------------------------------------------------------


class TestGetRevisions:
    def test_empty_revisions_for_new_item(self, svc, db):
        item = _make_item(db)
        revisions, total = svc.get_revisions(db, item.id)
        assert total == 0
        assert revisions == []

    def test_revisions_returned_after_update(self, svc, db):
        item = _make_item(db)
        svc.update(db, item.id, {"title": "New"})
        revisions, total = svc.get_revisions(db, item.id)
        assert total == 1
        assert len(revisions) == 1

    def test_revisions_nonexistent_raises(self, svc, db):
        with pytest.raises(KnowledgeNotFoundError):
            svc.get_revisions(db, "no-such-id")

    def test_revisions_pagination(self, svc, db):
        item = _make_item(db)
        for i in range(5):
            svc.update(db, item.id, {"title": f"v{i + 2}"})
        page1, total = svc.get_revisions(db, item.id, page=1, page_size=3)
        assert total == 5
        assert len(page1) == 3
        page2, _ = svc.get_revisions(db, item.id, page=2, page_size=3)
        assert len(page2) == 2

    def test_revisions_ordered_desc_by_version(self, svc, db):
        item = _make_item(db)
        svc.update(db, item.id, {"title": "v2"})
        svc.update(db, item.id, {"title": "v3"})
        revisions, _ = svc.get_revisions(db, item.id)
        assert revisions[0].version > revisions[1].version


# ---------------------------------------------------------------------------
# Events endpoint
# ---------------------------------------------------------------------------


class TestGetEvents:
    def test_empty_events_for_new_item(self, svc, db):
        item = _make_item(db)
        events, total = svc.get_events(db, item.id)
        assert total == 0
        assert events == []

    def test_events_after_retire(self, svc, db):
        item = _make_item(db)
        svc.retire(db, item.id)
        events, total = svc.get_events(db, item.id)
        assert total == 1
        assert events[0].event_type == "retired"

    def test_events_after_update(self, svc, db):
        item = _make_item(db)
        svc.update(db, item.id, {"title": "New"})
        events, total = svc.get_events(db, item.id)
        assert total == 1
        assert events[0].event_type == "updated"

    def test_events_update_payload_contains_changed_fields(self, svc, db):
        item = _make_item(db)
        svc.update(db, item.id, {"title": "New"})
        events, _ = svc.get_events(db, item.id)
        assert "title" in events[0].payload["changed_fields"]

    def test_events_nonexistent_raises(self, svc, db):
        with pytest.raises(KnowledgeNotFoundError):
            svc.get_events(db, "no-such-id")

    def test_events_pagination(self, svc, db):
        item = _make_item(db)
        for i in range(4):
            svc.update(db, item.id, {"title": f"v{i + 2}"})
        page1, total = svc.get_events(db, item.id, page=1, page_size=3)
        assert total == 4
        assert len(page1) == 3

    def test_full_lifecycle_event_sequence(self, svc, db):
        item = _make_item(db)
        svc.update(db, item.id, {"title": "Changed"})
        svc.retire(db, item.id)
        svc.restore(db, item.id)
        events, total = svc.get_events(db, item.id)
        assert total == 3
        types = {ev.event_type for ev in events}
        assert types == {"updated", "retired", "restored"}


# ---------------------------------------------------------------------------
# Serialization
# ---------------------------------------------------------------------------


class TestSerialization:
    def test_get_returns_all_expected_fields(self, svc, db):
        item = _make_item(
            db,
            title="Shape Test",
            content="body text",
            knowledge_type=KnowledgeType.debug_case,
            tags=["x"],
            applies_to=["planning"],
            failure_signature="sig",
            tool_name="bash",
            priority=3,
        )
        result = svc.get(db, item.id)
        assert result.id is not None
        assert result.title == "Shape Test"
        assert result.content == "body text"
        assert result.knowledge_type == KnowledgeType.debug_case
        assert result.tags == ["x"]
        assert result.applies_to == ["planning"]
        assert result.failure_signature == "sig"
        assert result.tool_name == "bash"
        assert result.priority == 3
        assert result.is_active is True
        assert result.version == 1
        assert result.checksum is not None
        assert result.created_at is not None

    def test_null_optional_fields(self, svc, db):
        item = _make_item(db)
        result = svc.get(db, item.id)
        assert result.tool_name is None
        assert result.failure_signature is None

    def test_updated_at_field_exists(self, svc, db):
        item = _make_item(db)
        result = svc.get(db, item.id)
        assert hasattr(result, "updated_at")

    def test_enum_knowledge_type_as_string(self, svc, db):
        item = _make_item(db, knowledge_type=KnowledgeType.failure_memory)
        result = svc.get(db, item.id)
        assert result.knowledge_type == KnowledgeType.failure_memory

    def test_revision_snapshot_is_dict(self, svc, db):
        item = _make_item(db)
        svc.update(db, item.id, {"priority": 5})
        rev = (
            db.query(KnowledgeItemRevision)
            .filter(KnowledgeItemRevision.knowledge_item_id == item.id)
            .first()
        )
        assert isinstance(rev.before_snapshot, dict)
        assert isinstance(rev.after_snapshot, dict)

    def test_event_payload_is_dict_for_update(self, svc, db):
        item = _make_item(db)
        svc.update(db, item.id, {"title": "X"})
        ev = (
            db.query(KnowledgeLifecycleEvent)
            .filter(KnowledgeLifecycleEvent.knowledge_item_id == item.id)
            .first()
        )
        assert isinstance(ev.payload, dict)
        assert "changed_fields" in ev.payload
        assert "version" in ev.payload

    def test_event_payload_is_none_for_retire(self, svc, db):
        item = _make_item(db)
        svc.retire(db, item.id)
        ev = (
            db.query(KnowledgeLifecycleEvent)
            .filter(KnowledgeLifecycleEvent.knowledge_item_id == item.id)
            .first()
        )
        assert ev.payload is None
