"""Tests for usage_log_service — row count, field values, rank order."""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, KnowledgeItem, KnowledgeUsageLog
from app.schemas.knowledge import (
    KnowledgeContext,
    KnowledgeItemRef,
    KnowledgeType,
    RecommendedAction,
)
from app.services.knowledge.usage_log_service import log_usage


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


def _make_knowledge_item(db, *, title: str = "Item") -> KnowledgeItem:
    item = KnowledgeItem(
        title=title,
        content="content",
        knowledge_type=KnowledgeType.format_guide,
        applies_to=["planning"],
        tags=[],
        priority=0,
        checksum=hashlib.sha256(title.encode()).hexdigest(),
    )
    db.add(item)
    db.flush()
    return item


def _make_ref(item: KnowledgeItem, rank: int) -> KnowledgeItemRef:
    return KnowledgeItemRef(
        id=item.id,
        title=item.title,
        knowledge_type=item.knowledge_type,
        content=item.content,
        priority=item.priority,
        confidence=0.8,
    )


def _make_ctx(refs: list[KnowledgeItemRef]) -> KnowledgeContext:
    return KnowledgeContext(
        retrieved_items=refs,
        query="test",
        trigger_phase="planning",
        retrieval_reason="test",
        confidence=0.8,
        matched_failure_memory=False,
        recommended_action=RecommendedAction.none,
    )


def test_n_items_produce_n_log_rows(db):
    items = [_make_knowledge_item(db, title=f"Item {i}") for i in range(3)]
    refs = [_make_ref(item, i) for i, item in enumerate(items)]
    ctx = _make_ctx(refs)

    log_usage(ctx, session_id=1, task_id=None, used_in_prompt=True, db=db)

    rows = db.query(KnowledgeUsageLog).all()
    assert len(rows) == 3


def test_all_rows_have_was_effective_none(db):
    items = [_make_knowledge_item(db, title=f"Item {i}") for i in range(2)]
    refs = [_make_ref(item, i) for i, item in enumerate(items)]
    ctx = _make_ctx(refs)

    log_usage(ctx, session_id=1, task_id=None, used_in_prompt=True, db=db)

    rows = db.query(KnowledgeUsageLog).all()
    assert all(row.was_effective is None for row in rows)


def test_rank_matches_item_order_in_context(db):
    items = [_make_knowledge_item(db, title=f"Item {i}") for i in range(3)]
    refs = [_make_ref(item, i) for i, item in enumerate(items)]
    ctx = _make_ctx(refs)

    log_usage(ctx, session_id=1, task_id=None, used_in_prompt=True, db=db)

    rows = sorted(db.query(KnowledgeUsageLog).all(), key=lambda r: r.rank)
    for expected_rank, (row, ref) in enumerate(zip(rows, refs)):
        assert row.rank == expected_rank
        assert row.knowledge_item_id == ref.id
