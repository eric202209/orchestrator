"""Tests for GET /knowledge/{id}/usage and /knowledge/{id}/usage/summary."""

from __future__ import annotations

import hashlib
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import (
    Base,
    KnowledgeItem,
    KnowledgeUsageLog,
    Project,
    Session as SessionModel,
    Task,
    TaskStatus,
)
from app.schemas.knowledge import KnowledgeType
from app.services.knowledge.knowledge_lifecycle_service import KnowledgeNotFoundError
from app.services.knowledge.knowledge_usage_drilldown_service import (
    KnowledgeUsageDrilldownService,
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
    return KnowledgeUsageDrilldownService()


def _make_item(db, *, title="Test Item", knowledge_type=KnowledgeType.format_guide):
    content = f"{title} content"
    item = KnowledgeItem(
        id=str(uuid.uuid4()),
        title=title,
        content=content,
        knowledge_type=knowledge_type,
        checksum=hashlib.sha256(content.encode()).hexdigest(),
    )
    db.add(item)
    db.commit()
    db.refresh(item)
    return item


def _make_project(db):
    project = Project(name="Test Project", workspace_path="/tmp/test")
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _make_session(db, project):
    count = db.query(SessionModel).count()
    sess = SessionModel(
        project_id=project.id,
        name=f"Session {count + 1}",
        description="test",
        status="stopped",
        is_active=False,
        execution_mode="manual",
    )
    db.add(sess)
    db.commit()
    db.refresh(sess)
    return sess


def _make_task(db, project):
    task = Task(
        project_id=project.id,
        title="Test Task",
        status=TaskStatus.DONE,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _make_log(
    db,
    session,
    item,
    *,
    trigger_phase="planning",
    task_id=None,
    retrieval_reason="test reason",
    retrieval_query="test query",
    confidence=0.9,
    rank=0,
    used_in_prompt=True,
    was_effective=None,
    created_at=None,
):
    log = KnowledgeUsageLog(
        session_id=session.id,
        task_id=task_id,
        knowledge_item_id=item.id,
        trigger_phase=trigger_phase,
        retrieval_reason=retrieval_reason,
        retrieval_query=retrieval_query,
        confidence=confidence,
        rank=rank,
        used_in_prompt=used_in_prompt,
        was_effective=was_effective,
        created_at=created_at,
    )
    db.add(log)
    db.commit()
    db.refresh(log)
    return log


# ---------------------------------------------------------------------------
# Usage list tests
# ---------------------------------------------------------------------------


def test_usage_list_missing_item_raises(db, svc):
    with pytest.raises(KnowledgeNotFoundError):
        svc.get_usage_list(db, "nonexistent-id")


def test_usage_list_empty(db, svc):
    item = _make_item(db)
    items, total = svc.get_usage_list(db, item.id)
    assert items == []
    assert total == 0


def test_usage_list_populated(db, svc):
    project = _make_project(db)
    session = _make_session(db, project)
    item = _make_item(db)
    _make_log(db, session, item)

    items, total = svc.get_usage_list(db, item.id)
    assert total == 1
    assert len(items) == 1
    assert items[0].knowledge_item_id == item.id
    assert items[0].session_id == session.id
    assert items[0].trigger_phase == "planning"
    assert items[0].used_in_prompt is True


def test_usage_list_pagination(db, svc):
    project = _make_project(db)
    session = _make_session(db, project)
    item = _make_item(db)
    for i in range(5):
        _make_log(db, session, item, rank=i)

    page1, total = svc.get_usage_list(db, item.id, page=1, page_size=3)
    assert total == 5
    assert len(page1) == 3

    page2, _ = svc.get_usage_list(db, item.id, page=2, page_size=3)
    assert len(page2) == 2


def test_usage_list_filter_trigger_phase(db, svc):
    project = _make_project(db)
    session = _make_session(db, project)
    item = _make_item(db)
    _make_log(db, session, item, trigger_phase="planning")
    _make_log(db, session, item, trigger_phase="execution")

    items, total = svc.get_usage_list(db, item.id, trigger_phase="planning")
    assert total == 1
    assert items[0].trigger_phase == "planning"


def test_usage_list_filter_used_in_prompt(db, svc):
    project = _make_project(db)
    session = _make_session(db, project)
    item = _make_item(db)
    _make_log(db, session, item, used_in_prompt=True)
    _make_log(db, session, item, used_in_prompt=False)

    items, total = svc.get_usage_list(db, item.id, used_in_prompt=True)
    assert total == 1
    assert items[0].used_in_prompt is True


def test_usage_list_filter_was_effective(db, svc):
    project = _make_project(db)
    session = _make_session(db, project)
    item = _make_item(db)
    _make_log(db, session, item, was_effective=True)
    _make_log(db, session, item, was_effective=False)
    _make_log(db, session, item, was_effective=None)

    items, total = svc.get_usage_list(db, item.id, was_effective=True)
    assert total == 1
    assert items[0].was_effective is True


def test_usage_list_filter_session_id(db, svc):
    project = _make_project(db)
    sess_a = _make_session(db, project)
    sess_b = _make_session(db, project)
    item = _make_item(db)
    _make_log(db, sess_a, item)
    _make_log(db, sess_b, item)

    items, total = svc.get_usage_list(db, item.id, session_id=sess_a.id)
    assert total == 1
    assert items[0].session_id == sess_a.id


def test_usage_list_filter_task_id(db, svc):
    project = _make_project(db)
    session = _make_session(db, project)
    task = _make_task(db, project)
    item = _make_item(db)
    _make_log(db, session, item, task_id=task.id)
    _make_log(db, session, item, task_id=None)

    items, total = svc.get_usage_list(db, item.id, task_id=task.id)
    assert total == 1
    assert items[0].task_id == task.id


def test_usage_list_filter_created_after(db, svc):
    project = _make_project(db)
    session = _make_session(db, project)
    item = _make_item(db)
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    t2 = datetime(2026, 6, 1, tzinfo=UTC)
    _make_log(db, session, item, created_at=t1)
    _make_log(db, session, item, created_at=t2)

    items, total = svc.get_usage_list(
        db, item.id, created_after=datetime(2026, 3, 1, tzinfo=UTC)
    )
    assert total == 1
    # SQLite drops tzinfo; compare naive
    assert items[0].created_at.replace(tzinfo=None) == t2.replace(tzinfo=None)


def test_usage_list_filter_created_before(db, svc):
    project = _make_project(db)
    session = _make_session(db, project)
    item = _make_item(db)
    t1 = datetime(2026, 1, 1, tzinfo=UTC)
    t2 = datetime(2026, 6, 1, tzinfo=UTC)
    _make_log(db, session, item, created_at=t1)
    _make_log(db, session, item, created_at=t2)

    items, total = svc.get_usage_list(
        db, item.id, created_before=datetime(2026, 3, 1, tzinfo=UTC)
    )
    assert total == 1
    # SQLite drops tzinfo; compare naive
    assert items[0].created_at.replace(tzinfo=None) == t1.replace(tzinfo=None)


# ---------------------------------------------------------------------------
# Usage summary tests
# ---------------------------------------------------------------------------


def test_summary_missing_item_raises(db, svc):
    with pytest.raises(KnowledgeNotFoundError):
        svc.get_usage_summary(db, "nonexistent-id")


def test_summary_empty(db, svc):
    item = _make_item(db)
    s = svc.get_usage_summary(db, item.id)

    assert s["knowledge_item_id"] == item.id
    assert s["retrieval_count"] == 0
    assert s["used_in_prompt_count"] == 0
    assert s["effective_count"] == 0
    assert s["knowledge_hit_rate"] is None
    assert s["effectiveness_rate"] is None
    assert s["avg_confidence"] is None
    assert s["phase_distribution"] == {}
    assert s["recent_sessions"] == []
    assert s["recent_tasks"] == []


def test_summary_rates(db, svc):
    project = _make_project(db)
    session = _make_session(db, project)
    item = _make_item(db)

    # 4 retrievals, 3 used in prompt, 2 effective
    _make_log(db, session, item, used_in_prompt=True, was_effective=True)
    _make_log(db, session, item, used_in_prompt=True, was_effective=True)
    _make_log(db, session, item, used_in_prompt=True, was_effective=False)
    _make_log(db, session, item, used_in_prompt=False, was_effective=None)

    s = svc.get_usage_summary(db, item.id)
    assert s["retrieval_count"] == 4
    assert s["used_in_prompt_count"] == 3
    assert s["effective_count"] == 2
    assert s["knowledge_hit_rate"] == pytest.approx(3 / 4)
    assert s["effectiveness_rate"] == pytest.approx(2 / 3)


def test_summary_avg_confidence(db, svc):
    project = _make_project(db)
    session = _make_session(db, project)
    item = _make_item(db)
    _make_log(db, session, item, confidence=0.8)
    _make_log(db, session, item, confidence=1.0)

    s = svc.get_usage_summary(db, item.id)
    assert s["avg_confidence"] == pytest.approx(0.9)


def test_summary_phase_distribution(db, svc):
    project = _make_project(db)
    session = _make_session(db, project)
    item = _make_item(db)
    _make_log(db, session, item, trigger_phase="planning")
    _make_log(db, session, item, trigger_phase="planning")
    _make_log(db, session, item, trigger_phase="execution")

    s = svc.get_usage_summary(db, item.id)
    assert s["phase_distribution"] == {"planning": 2, "execution": 1}


def test_summary_recent_sessions(db, svc):
    project = _make_project(db)
    sess1 = _make_session(db, project)
    sess2 = _make_session(db, project)
    item = _make_item(db)
    _make_log(db, sess1, item, created_at=datetime(2026, 1, 1, tzinfo=UTC))
    _make_log(db, sess2, item, created_at=datetime(2026, 6, 1, tzinfo=UTC))

    s = svc.get_usage_summary(db, item.id)
    # Most recent first
    assert s["recent_sessions"][0] == sess2.id
    assert sess1.id in s["recent_sessions"]


def test_summary_recent_tasks(db, svc):
    project = _make_project(db)
    session = _make_session(db, project)
    task = _make_task(db, project)
    item = _make_item(db)
    _make_log(db, session, item, task_id=task.id)
    _make_log(db, session, item, task_id=None)

    s = svc.get_usage_summary(db, item.id)
    assert task.id in s["recent_tasks"]


def test_summary_hit_rate_null_when_no_retrievals(db, svc):
    item = _make_item(db)
    s = svc.get_usage_summary(db, item.id)
    assert s["knowledge_hit_rate"] is None


def test_summary_effectiveness_rate_null_when_not_used_in_prompt(db, svc):
    project = _make_project(db)
    session = _make_session(db, project)
    item = _make_item(db)
    _make_log(db, session, item, used_in_prompt=False, was_effective=None)

    s = svc.get_usage_summary(db, item.id)
    assert s["retrieval_count"] == 1
    assert s["used_in_prompt_count"] == 0
    assert s["effectiveness_rate"] is None


def test_summary_avg_confidence_null_when_no_rows(db, svc):
    item = _make_item(db)
    s = svc.get_usage_summary(db, item.id)
    assert s["avg_confidence"] is None


def test_usage_log_json_serialization(db, svc):
    """Verify returned rows have the expected fields populated."""
    project = _make_project(db)
    session = _make_session(db, project)
    item = _make_item(db)
    _make_log(
        db,
        session,
        item,
        trigger_phase="completion_repair",
        retrieval_reason="semantic",
        retrieval_query="how to fix X",
        confidence=0.95,
        rank=2,
        used_in_prompt=True,
        was_effective=True,
    )

    items, _ = svc.get_usage_list(db, item.id)
    row = items[0]
    assert row.trigger_phase == "completion_repair"
    assert row.retrieval_reason == "semantic"
    assert row.retrieval_query == "how to fix X"
    assert row.confidence == pytest.approx(0.95)
    assert row.rank == 2
    assert row.used_in_prompt is True
    assert row.was_effective is True
    assert row.session_id == session.id
