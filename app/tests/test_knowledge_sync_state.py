"""Tests for Phase 16C-1 — Knowledge Synchronization State Foundation."""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.db_migrations import _migration_022_knowledge_sync_state, run_schema_migrations
from app.models import Base, KnowledgeItem
from app.schemas.knowledge import KnowledgeType
from app.services.knowledge.knowledge_lifecycle_service import KnowledgeLifecycleService


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def engine():
    eng = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=eng)
    yield eng
    Base.metadata.drop_all(bind=eng)
    eng.dispose()


@pytest.fixture()
def db(engine):
    Session = sessionmaker(bind=engine)
    session = Session()
    try:
        yield session
    finally:
        session.close()


@pytest.fixture()
def svc():
    return KnowledgeLifecycleService()


def _make_item(db, *, content: str = "Original content.") -> KnowledgeItem:
    item = KnowledgeItem(
        title="Test Item",
        content=content,
        knowledge_type=KnowledgeType.format_guide,
        applies_to=["planning"],
        tags=[],
        priority=0,
        is_active=True,
        checksum=hashlib.sha256(content.encode()).hexdigest(),
    )
    db.add(item)
    db.flush()
    return item


# ---------------------------------------------------------------------------
# Migration defaults
# ---------------------------------------------------------------------------


def test_migration_adds_sync_columns(engine):
    columns = {c["name"] for c in inspect(engine).get_columns("knowledge_items")}
    assert "sync_status" in columns
    assert "sync_required_at" in columns
    assert "last_synced_at" in columns
    assert "last_sync_error" in columns


def test_migration_default_synced_for_existing_rows():
    """Simulate pre-migration state: insert without sync_status, run migration, verify default."""
    pre_migration_engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    with pre_migration_engine.begin() as conn:
        conn.execute(
            text(
                """
                CREATE TABLE knowledge_items (
                    id VARCHAR(36) PRIMARY KEY,
                    title VARCHAR(255) NOT NULL,
                    content TEXT NOT NULL,
                    knowledge_type VARCHAR(50) NOT NULL,
                    applies_to JSON,
                    tags JSON,
                    priority INTEGER DEFAULT 0,
                    is_active BOOLEAN DEFAULT 1,
                    version INTEGER DEFAULT 1,
                    checksum VARCHAR(64) NOT NULL
                )
                """
            )
        )
        conn.execute(
            text(
                """
                INSERT INTO knowledge_items
                    (id, title, content, knowledge_type, applies_to, tags,
                     priority, is_active, version, checksum)
                VALUES
                    ('abc-123', 'Old Item', 'content', 'format_guide', '[]', '[]',
                     0, 1, 1, 'abc')
                """
            )
        )
    _migration_022_knowledge_sync_state(pre_migration_engine)
    with pre_migration_engine.connect() as conn:
        row = conn.execute(
            text("SELECT sync_status FROM knowledge_items WHERE id = 'abc-123'")
        ).fetchone()
    pre_migration_engine.dispose()
    assert row[0] == "synced"


# ---------------------------------------------------------------------------
# New item defaults
# ---------------------------------------------------------------------------


def test_new_item_defaults_to_synced(db):
    item = _make_item(db)
    db.commit()
    db.refresh(item)
    assert item.sync_status == "synced"
    assert item.sync_required_at is None
    assert item.last_synced_at is None
    assert item.last_sync_error is None


# ---------------------------------------------------------------------------
# PATCH — real change marks dirty
# ---------------------------------------------------------------------------


def test_patch_real_change_marks_dirty(db, svc):
    item = _make_item(db)
    db.commit()

    updated = svc.update(db, item.id, {"title": "New Title"}, reason="test")

    assert updated.sync_status == "dirty"
    assert updated.sync_required_at is not None
    assert updated.last_sync_error is None


def test_patch_content_change_marks_dirty(db, svc):
    item = _make_item(db)
    db.commit()

    updated = svc.update(db, item.id, {"content": "Updated content."}, reason="fix")

    assert updated.sync_status == "dirty"
    assert updated.sync_required_at is not None


def test_patch_noop_stays_synced(db, svc):
    item = _make_item(db, content="Same content.")
    db.commit()

    updated = svc.update(db, item.id, {"content": "Same content."}, reason="no change")

    assert updated.sync_status == "synced"
    assert updated.sync_required_at is None


def test_patch_clears_last_sync_error(db, svc):
    item = _make_item(db)
    item.last_sync_error = "previous embedding failure"
    db.commit()

    updated = svc.update(db, item.id, {"title": "Clean Title"}, reason="retry")

    assert updated.sync_status == "dirty"
    assert updated.last_sync_error is None


def test_patch_dirty_timestamp_populated(db, svc):
    item = _make_item(db)
    db.commit()

    updated = svc.update(db, item.id, {"priority": 5}, reason="bump priority")

    assert updated.sync_required_at is not None


# ---------------------------------------------------------------------------
# Retire / restore preserve sync state
# ---------------------------------------------------------------------------


def test_retire_preserves_sync_status(db, svc):
    item = _make_item(db)
    db.commit()

    retired = svc.retire(db, item.id, reason="pruning")

    assert retired.sync_status == "synced"
    assert retired.sync_required_at is None


def test_restore_preserves_sync_status(db, svc):
    item = _make_item(db)
    item.is_active = False
    db.commit()

    restored = svc.restore(db, item.id, reason="reinstating")

    assert restored.sync_status == "synced"
    assert restored.sync_required_at is None


def test_retire_preserves_dirty_sync_status(db, svc):
    item = _make_item(db)
    db.commit()
    svc.update(db, item.id, {"title": "Edited"}, reason="edit")

    retired = svc.retire(db, item.id, reason="retiring edited item")

    assert retired.sync_status == "dirty"


def test_restore_preserves_dirty_sync_status(db, svc):
    item = _make_item(db)
    item.is_active = False
    item.sync_status = "dirty"
    db.commit()

    restored = svc.restore(db, item.id, reason="bring back dirty item")

    assert restored.sync_status == "dirty"


# ---------------------------------------------------------------------------
# Serialization — sync fields appear in GET /knowledge/{id} response
# ---------------------------------------------------------------------------


def test_lifecycle_item_response_includes_sync_fields():
    from datetime import datetime, timezone

    from app.api.v1.endpoints.knowledge_lifecycle import KnowledgeLifecycleItemResponse

    now = datetime.now(timezone.utc)

    response = KnowledgeLifecycleItemResponse(
        id="x",
        title="T",
        content="C",
        source_path=None,
        knowledge_type="format_guide",
        tags=[],
        applies_to=["planning"],
        tool_name=None,
        failure_signature=None,
        priority=0,
        project_scope=None,
        is_active=True,
        version=2,
        checksum="abc",
        sync_status="dirty",
        sync_required_at=now,
        last_synced_at=None,
        last_sync_error="embedding timeout",
        created_at=now,
        updated_at=None,
    )

    assert response.sync_status == "dirty"
    assert response.sync_required_at == now
    assert response.last_synced_at is None
    assert response.last_sync_error == "embedding timeout"
