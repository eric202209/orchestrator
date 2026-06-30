"""Tests for Phase 16C-2 — Manual Knowledge Synchronization."""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Optional
from unittest.mock import MagicMock, patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, KnowledgeItem, KnowledgeLifecycleEvent
from app.schemas.knowledge import KnowledgeType
from app.services.knowledge.knowledge_lifecycle_service import KnowledgeNotFoundError
from app.services.knowledge.knowledge_sync_service import (
    KnowledgeSyncError,
    KnowledgeSyncService,
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


def _mock_knowledge_svc(
    *, fail: bool = False, error: str = "Qdrant error"
) -> MagicMock:
    ksvc = MagicMock()
    if fail:
        ksvc.ingest.side_effect = Exception(error)
    else:
        ksvc.ingest.return_value = None
    return ksvc


def _make_item(
    db,
    *,
    content: str = "Original content.",
    sync_status: str = "dirty",
    last_sync_error: Optional[str] = None,
    sync_required_at: Optional[datetime] = None,
) -> KnowledgeItem:
    item = KnowledgeItem(
        title="Test Item",
        content=content,
        knowledge_type=KnowledgeType.format_guide,
        applies_to=["planning"],
        tags=[],
        priority=0,
        is_active=True,
        checksum=hashlib.sha256(b"stale-checksum").hexdigest(),
        sync_status=sync_status,
        last_sync_error=last_sync_error,
        sync_required_at=sync_required_at,
    )
    db.add(item)
    db.flush()
    return item


# ---------------------------------------------------------------------------
# 404 — nonexistent item
# ---------------------------------------------------------------------------


def test_sync_nonexistent_item_raises(db):
    svc = KnowledgeSyncService(_mock_knowledge_svc())
    with pytest.raises(KnowledgeNotFoundError):
        svc.sync(db, "does-not-exist")


# ---------------------------------------------------------------------------
# Idempotency — already synced
# ---------------------------------------------------------------------------


def test_sync_already_synced_is_idempotent(db):
    item = _make_item(db, sync_status="synced")
    db.commit()
    ksvc = _mock_knowledge_svc()
    svc = KnowledgeSyncService(ksvc)

    result = svc.sync(db, item.id)

    assert result.sync_status == "synced"
    ksvc.ingest.assert_not_called()


def test_sync_already_synced_creates_no_event(db):
    item = _make_item(db, sync_status="synced")
    db.commit()
    svc = KnowledgeSyncService(_mock_knowledge_svc())

    svc.sync(db, item.id)

    events = (
        db.query(KnowledgeLifecycleEvent)
        .filter(KnowledgeLifecycleEvent.knowledge_item_id == item.id)
        .all()
    )
    assert events == []


# ---------------------------------------------------------------------------
# Success path — dirty item
# ---------------------------------------------------------------------------


def test_sync_dirty_item_calls_ingest(db):
    item = _make_item(db, sync_status="dirty")
    db.commit()
    ksvc = _mock_knowledge_svc()
    svc = KnowledgeSyncService(ksvc)

    svc.sync(db, item.id)

    ksvc.ingest.assert_called_once()


def test_sync_success_recomputes_checksum(db):
    content = "Current knowledge content."
    item = _make_item(db, content=content, sync_status="dirty")
    db.commit()
    svc = KnowledgeSyncService(_mock_knowledge_svc())

    result = svc.sync(db, item.id)

    expected = hashlib.sha256(content.encode()).hexdigest()
    assert result.checksum == expected


def test_sync_success_sets_synced_status(db):
    item = _make_item(db, sync_status="dirty")
    db.commit()
    svc = KnowledgeSyncService(_mock_knowledge_svc())

    result = svc.sync(db, item.id)

    assert result.sync_status == "synced"


def test_sync_success_populates_last_synced_at(db):
    item = _make_item(db, sync_status="dirty")
    db.commit()
    svc = KnowledgeSyncService(_mock_knowledge_svc())

    result = svc.sync(db, item.id)

    assert result.last_synced_at is not None


def test_sync_success_clears_last_sync_error(db):
    item = _make_item(db, sync_status="dirty", last_sync_error="previous error")
    db.commit()
    svc = KnowledgeSyncService(_mock_knowledge_svc())

    result = svc.sync(db, item.id)

    assert result.last_sync_error is None


def test_sync_success_clears_sync_required_at(db):
    item = _make_item(
        db,
        sync_status="dirty",
        sync_required_at=datetime.now(timezone.utc),
    )
    db.commit()
    svc = KnowledgeSyncService(_mock_knowledge_svc())

    result = svc.sync(db, item.id)

    assert result.sync_required_at is None


def test_sync_success_creates_synced_event(db):
    item = _make_item(db, sync_status="dirty")
    db.commit()
    svc = KnowledgeSyncService(_mock_knowledge_svc())

    svc.sync(db, item.id)

    event = (
        db.query(KnowledgeLifecycleEvent)
        .filter(
            KnowledgeLifecycleEvent.knowledge_item_id == item.id,
            KnowledgeLifecycleEvent.event_type == "synced",
        )
        .first()
    )
    assert event is not None
    assert event.payload["previous_status"] == "dirty"
    assert event.payload["new_status"] == "synced"
    assert "checksum" in event.payload


def test_sync_success_event_records_actor(db):
    item = _make_item(db, sync_status="dirty")
    db.commit()
    svc = KnowledgeSyncService(_mock_knowledge_svc())

    svc.sync(db, item.id, actor="admin@example.com")

    event = (
        db.query(KnowledgeLifecycleEvent)
        .filter(KnowledgeLifecycleEvent.knowledge_item_id == item.id)
        .first()
    )
    assert event.actor == "admin@example.com"


# ---------------------------------------------------------------------------
# Failed item retry — same success path
# ---------------------------------------------------------------------------


def test_sync_failed_item_can_retry(db):
    item = _make_item(db, sync_status="failed", last_sync_error="prev error")
    db.commit()
    svc = KnowledgeSyncService(_mock_knowledge_svc())

    result = svc.sync(db, item.id)

    assert result.sync_status == "synced"
    assert result.last_sync_error is None


def test_sync_failed_item_event_records_previous_status(db):
    item = _make_item(db, sync_status="failed")
    db.commit()
    svc = KnowledgeSyncService(_mock_knowledge_svc())

    svc.sync(db, item.id)

    event = (
        db.query(KnowledgeLifecycleEvent)
        .filter(KnowledgeLifecycleEvent.event_type == "synced")
        .first()
    )
    assert event.payload["previous_status"] == "failed"


# ---------------------------------------------------------------------------
# Failure path
# ---------------------------------------------------------------------------


def test_sync_failure_raises_knowledge_sync_error(db):
    item = _make_item(db, sync_status="dirty")
    db.commit()
    svc = KnowledgeSyncService(
        _mock_knowledge_svc(fail=True, error="embedding timeout")
    )

    with pytest.raises(KnowledgeSyncError):
        svc.sync(db, item.id)


def test_sync_failure_sets_failed_status(db):
    item = _make_item(db, sync_status="dirty")
    db.commit()
    svc = KnowledgeSyncService(_mock_knowledge_svc(fail=True))

    with pytest.raises(KnowledgeSyncError):
        svc.sync(db, item.id)

    db.refresh(item)
    assert item.sync_status == "failed"


def test_sync_failure_records_error_message(db):
    item = _make_item(db, sync_status="dirty")
    db.commit()
    svc = KnowledgeSyncService(_mock_knowledge_svc(fail=True, error="Qdrant timeout"))

    with pytest.raises(KnowledgeSyncError):
        svc.sync(db, item.id)

    db.refresh(item)
    assert "Qdrant timeout" in item.last_sync_error


def test_sync_failure_creates_sync_failed_event(db):
    item = _make_item(db, sync_status="dirty")
    db.commit()
    svc = KnowledgeSyncService(_mock_knowledge_svc(fail=True, error="network error"))

    with pytest.raises(KnowledgeSyncError):
        svc.sync(db, item.id)

    event = (
        db.query(KnowledgeLifecycleEvent)
        .filter(
            KnowledgeLifecycleEvent.knowledge_item_id == item.id,
            KnowledgeLifecycleEvent.event_type == "sync_failed",
        )
        .first()
    )
    assert event is not None
    assert "network error" in event.payload["error"]
    assert event.payload["new_status"] == "failed"


# ---------------------------------------------------------------------------
# Sync state transitions via syncing intermediate state
# ---------------------------------------------------------------------------


def test_sync_passes_through_syncing_state(db):
    """Verify syncing is the intermediate state by checking ingest is called exactly once."""
    item = _make_item(db, sync_status="dirty")
    db.commit()

    captured_status: list[str] = []
    original_id = item.id

    real_flush = db.flush

    def capturing_ingest(_item):
        # At call time, sync_status should be "syncing"
        db_item = (
            db.query(KnowledgeItem).filter(KnowledgeItem.id == original_id).first()
        )
        captured_status.append(db_item.sync_status)

    ksvc = MagicMock()
    ksvc.ingest.side_effect = capturing_ingest
    svc = KnowledgeSyncService(ksvc)

    svc.sync(db, item.id)

    assert "syncing" in captured_status


# ---------------------------------------------------------------------------
# Endpoint smoke tests
# ---------------------------------------------------------------------------


def test_sync_endpoint_404_for_missing_item(authenticated_client):
    resp = authenticated_client.post("/api/v1/knowledge/nonexistent-id/sync")
    assert resp.status_code == 404


def test_sync_endpoint_200_for_synced_item(authenticated_client, db_session):
    content = "Synced item content."
    item = KnowledgeItem(
        title="Already Synced",
        content=content,
        knowledge_type=KnowledgeType.format_guide,
        applies_to=["planning"],
        tags=[],
        priority=0,
        is_active=True,
        checksum=hashlib.sha256(content.encode()).hexdigest(),
        sync_status="synced",
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)

    with patch(
        "app.api.v1.endpoints.knowledge_lifecycle._build_sync_service"
    ) as mock_factory:
        mock_svc = MagicMock()
        mock_svc.sync.return_value = item
        mock_factory.return_value = mock_svc
        resp = authenticated_client.post(f"/api/v1/knowledge/{item.id}/sync")

    assert resp.status_code == 200
    data = resp.json()
    assert data["sync_status"] == "synced"


def test_sync_endpoint_503_on_sync_failure(authenticated_client, db_session):
    content = "Dirty item content."
    item = KnowledgeItem(
        title="Dirty Item",
        content=content,
        knowledge_type=KnowledgeType.format_guide,
        applies_to=["planning"],
        tags=[],
        priority=0,
        is_active=True,
        checksum=hashlib.sha256(b"stale").hexdigest(),
        sync_status="dirty",
    )
    db_session.add(item)
    db_session.commit()
    db_session.refresh(item)

    with patch(
        "app.api.v1.endpoints.knowledge_lifecycle._build_sync_service"
    ) as mock_factory:
        mock_svc = MagicMock()
        mock_svc.sync.side_effect = KnowledgeSyncError("embedding timeout")
        mock_factory.return_value = mock_svc
        resp = authenticated_client.post(f"/api/v1/knowledge/{item.id}/sync")

    assert resp.status_code == 503
    assert "Sync failed" in resp.json()["detail"]
