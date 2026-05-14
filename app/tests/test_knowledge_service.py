"""Tests for KnowledgeService — ingest, retrieve, budget enforcement."""

from __future__ import annotations

from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, KnowledgeItem
from app.schemas.knowledge import KnowledgeType
from app.services.knowledge.knowledge_service import KnowledgeService

# Fixed fake embedding vector (1536 dims, all zeros)
_FAKE_VECTOR = [0.0] * 1536


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
    with patch.object(KnowledgeService, "_embed", return_value=_FAKE_VECTOR):
        yield KnowledgeService(qdrant_url=":memory:")


def _make_item(
    db,
    *,
    title: str = "Test Item",
    content: str = "Some content.",
    knowledge_type: str = KnowledgeType.format_guide,
    applies_to: list | None = None,
    tags: list | None = None,
    failure_signature: str | None = None,
    priority: int = 0,
) -> KnowledgeItem:
    import hashlib

    item = KnowledgeItem(
        title=title,
        content=content,
        knowledge_type=knowledge_type,
        applies_to=applies_to or ["planning"],
        tags=tags or [],
        failure_signature=failure_signature,
        priority=priority,
        checksum=hashlib.sha256(content.encode()).hexdigest(),
    )
    db.add(item)
    db.flush()
    return item


# ---------------------------------------------------------------------------


def test_ingest_and_retrieve_by_knowledge_type(svc, db):
    item = _make_item(db, title="JSON Guide", knowledge_type=KnowledgeType.format_guide)
    with patch.object(svc, "_embed", return_value=_FAKE_VECTOR):
        svc.ingest(item)
        ctx = svc.retrieve(
            query="output format",
            trigger_phase="planning",
            knowledge_types=[KnowledgeType.format_guide],
            db=db,
        )
    assert any(ref.title == "JSON Guide" for ref in ctx.retrieved_items)


def test_ingest_idempotent_same_item_twice(svc, db):
    item = _make_item(db, title="Idempotent Item")
    with patch.object(svc, "_embed", return_value=_FAKE_VECTOR):
        svc.ingest(item)
        svc.ingest(item)  # second call — same id, upsert
        points = svc._client.count(collection_name=svc._collection).count
    assert points == 1


def test_applies_to_planning_not_returned_for_failure(svc, db):
    item = _make_item(
        db,
        title="Planning Only",
        applies_to=["planning"],
        knowledge_type=KnowledgeType.format_guide,
    )
    with patch.object(svc, "_embed", return_value=_FAKE_VECTOR):
        svc.ingest(item)
        ctx = svc.retrieve(
            query="error",
            trigger_phase="failure",
            knowledge_types=[KnowledgeType.failure_memory, KnowledgeType.debug_case],
            db=db,
        )
    assert not any(ref.title == "Planning Only" for ref in ctx.retrieved_items)


def test_validation_retrieval_can_use_failure_memory_from_sqlite_fallback(svc, db):
    item = _make_item(
        db,
        title="Package Metadata Planning Repair Failure",
        content="Prior repair failed because the final verification step had no command.",
        applies_to=["failure"],
        knowledge_type=KnowledgeType.failure_memory,
        priority=10,
    )
    with patch.object(svc, "_has_indexed_points", return_value=False):
        ctx = svc.retrieve(
            query="plan validation failed after repair",
            trigger_phase="validation",
            knowledge_types=[KnowledgeType.failure_memory, KnowledgeType.debug_case],
            db=db,
        )
    assert any(ref.id == item.id for ref in ctx.retrieved_items)
    assert ctx.trigger_phase == "validation"
    assert ctx.matched_failure_memory is True


def test_sqlite_fallback_ranks_exact_failure_memory_before_generic_guides(svc, db):
    signature = (
        "Verification/review plan references source files that do not exist in the "
        "current workspace"
    )
    _make_item(
        db,
        title="Shell-Safe Command Format Guide",
        content="Use shell-safe commands and avoid unsupported command syntax.",
        applies_to=["planning", "validation"],
        knowledge_type=KnowledgeType.format_guide,
        priority=50,
    )
    _make_item(
        db,
        title="OpenAI 401 Missing Embedding Key",
        content="Embedding calls can fail when the API key is missing or invalid.",
        applies_to=["failure", "validation"],
        knowledge_type=KnowledgeType.failure_memory,
        failure_signature="OpenAI 401",
        priority=40,
    )
    specific = _make_item(
        db,
        title="Static Verification Missing Workspace Files",
        content=(
            "When validating a static site, inspect the current workspace before "
            "referencing or creating conventional asset paths like styles.css."
        ),
        applies_to=["planning", "validation", "failure"],
        knowledge_type=KnowledgeType.failure_memory,
        failure_signature=signature,
        priority=5,
    )

    with patch.object(svc, "_has_indexed_points", return_value=False):
        ctx = svc.retrieve(
            query=(
                "Plan validation failed after repair: Verification/review plan "
                "references source files that do not exist in the current workspace "
                "(files: ['styles.css'])"
            ),
            trigger_phase="validation",
            knowledge_types=[
                KnowledgeType.failure_memory,
                KnowledgeType.format_guide,
                KnowledgeType.debug_case,
            ],
            failure_signature=signature,
            db=db,
        )

    assert ctx.retrieved_items[0].id == specific.id
    assert ctx.retrieved_items[0].confidence == 1.0
    assert ctx.query is not None
    assert ctx.retrieval_reason == "sqlite_fallback_qdrant_or_embedding_unavailable"


def test_sqlite_fallback_tolerates_legacy_non_string_tags(svc, db):
    item = _make_item(
        db,
        title="Legacy Tags Failure Memory",
        content="Legacy rows may contain non-string tag values.",
        applies_to=["failure"],
        knowledge_type=KnowledgeType.failure_memory,
        tags=["legacy", 120],
        priority=5,
    )

    with patch.object(svc, "_has_indexed_points", return_value=False):
        ctx = svc.retrieve(
            query="legacy tags failure",
            trigger_phase="failure",
            knowledge_types=[KnowledgeType.failure_memory],
            db=db,
        )

    assert any(ref.id == item.id for ref in ctx.retrieved_items)


def test_max_items_budget_enforced(svc, db):
    items = []
    for i in range(5):
        item = _make_item(
            db,
            title=f"Item {i}",
            content=f"Content for item {i}.",
            applies_to=["planning", "all"],
            knowledge_type=KnowledgeType.format_guide,
        )
        items.append(item)
    with patch.object(svc, "_embed", return_value=_FAKE_VECTOR):
        for item in items:
            svc.ingest(item)
        ctx = svc.retrieve(
            query="format guide",
            trigger_phase="planning",
            knowledge_types=[KnowledgeType.format_guide],
            top_k=10,
            db=db,
        )
    assert len(ctx.retrieved_items) <= 3


def test_qdrant_unavailable_returns_sqlite_fallback(svc, db):
    _make_item(db, title="Fallback Qdrant", knowledge_type=KnowledgeType.format_guide)
    with patch.object(svc, "_search", side_effect=Exception("Qdrant down")):
        with patch.object(svc, "_embed", return_value=_FAKE_VECTOR):
            ctx = svc.retrieve(
                query="format guide",
                trigger_phase="planning",
                knowledge_types=[KnowledgeType.format_guide],
                db=db,
            )
    assert any(ref.title == "Fallback Qdrant" for ref in ctx.retrieved_items)
    assert ctx.retrieval_reason == "sqlite_fallback_qdrant_or_embedding_unavailable"
    assert ctx.confidence == 0.3


def test_embedding_failure_returns_sqlite_fallback(svc, db):
    _make_item(db, title="Fallback Embed", knowledge_type=KnowledgeType.format_guide)
    with patch.object(svc, "_embed", side_effect=Exception("OpenAI down")):
        ctx = svc.retrieve(
            query="format guide",
            trigger_phase="planning",
            knowledge_types=[KnowledgeType.format_guide],
            db=db,
        )
    assert any(ref.title == "Fallback Embed" for ref in ctx.retrieved_items)
    assert ctx.retrieval_reason == "sqlite_fallback_qdrant_or_embedding_unavailable"
    assert ctx.confidence == 0.3


def test_empty_qdrant_skips_embedding_and_uses_sqlite_fallback(svc, db):
    _make_item(db, title="Empty Qdrant", knowledge_type=KnowledgeType.format_guide)
    with patch.object(svc, "_embed", side_effect=AssertionError("should not embed")):
        ctx = svc.retrieve(
            query="format guide",
            trigger_phase="planning",
            knowledge_types=[KnowledgeType.format_guide],
            db=db,
        )

    assert any(ref.title == "Empty Qdrant" for ref in ctx.retrieved_items)
    assert ctx.retrieval_reason == "sqlite_fallback_qdrant_or_embedding_unavailable"


def test_max_total_chars_budget_enforced(svc, db):
    # Each item has 800 chars of content; 3 × 800 = 2400 > 2000 limit
    long_content = "x" * 800
    items = []
    for i in range(3):
        item = _make_item(
            db,
            title=f"Big Item {i}",
            content=long_content,
            applies_to=["planning", "all"],
            knowledge_type=KnowledgeType.format_guide,
        )
        items.append(item)
    with patch.object(svc, "_embed", return_value=_FAKE_VECTOR):
        for item in items:
            svc.ingest(item)
        ctx = svc.retrieve(
            query="format",
            trigger_phase="planning",
            knowledge_types=[KnowledgeType.format_guide],
            top_k=10,
            db=db,
        )
    total_chars = sum(len(ref.content) for ref in ctx.retrieved_items)
    assert total_chars <= 2000
