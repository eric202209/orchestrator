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
        yield KnowledgeService(qdrant_url=":memory:", embedding_dim=len(_FAKE_VECTOR))


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


def test_static_site_knowledge_is_gated_from_ordinary_backend_tasks(svc, db):
    _make_item(
        db,
        title="Static Site Materialization Contract",
        content="Use typed ops for index.html and css/style.css.",
        applies_to=["planning", "validation"],
        knowledge_type=KnowledgeType.format_guide,
        tags=["static-site", "html", "css"],
        priority=20,
    )
    backend_item = _make_item(
        db,
        title="Backend API Guide",
        content="Use FastAPI routers and service functions.",
        applies_to=["planning"],
        knowledge_type=KnowledgeType.format_guide,
        tags=["backend", "api"],
        priority=1,
    )

    with patch.object(svc, "_has_indexed_points", return_value=False):
        ctx = svc.retrieve(
            query="Add a FastAPI endpoint and service method",
            trigger_phase="planning",
            knowledge_types=[KnowledgeType.format_guide],
            db=db,
        )

    titles = {ref.title for ref in ctx.retrieved_items}
    assert "Backend API Guide" in titles
    assert "Static Site Materialization Contract" not in titles
    assert any(ref.id == backend_item.id for ref in ctx.retrieved_items)


def test_negated_static_site_mentions_do_not_open_static_site_gate(svc, db):
    _make_item(
        db,
        title="Static Site Verification Contract",
        content="Verify index.html and css/style.css for plain static sites.",
        applies_to=["planning", "validation"],
        knowledge_type=KnowledgeType.format_guide,
        tags=["static-site", "verification", "html", "css"],
        priority=20,
    )
    backend_item = _make_item(
        db,
        title="Backend Repair Guide",
        content="Keep backend bug fixes scoped to the target module and tests.",
        applies_to=["planning"],
        knowledge_type=KnowledgeType.format_guide,
        tags=["backend"],
        priority=1,
    )

    with patch.object(svc, "_has_indexed_points", return_value=False):
        ctx = svc.retrieve(
            query=(
                "Fix the backend tax calculation bug. Do not create frontend or "
                "static-site files. Do not create index.html."
            ),
            trigger_phase="planning",
            knowledge_types=[KnowledgeType.format_guide],
            db=db,
        )

    titles = {ref.title for ref in ctx.retrieved_items}
    assert "Backend Repair Guide" in titles
    assert "Static Site Verification Contract" not in titles
    assert any(ref.id == backend_item.id for ref in ctx.retrieved_items)


def test_static_site_knowledge_is_returned_for_matching_static_site_task(svc, db):
    item = _make_item(
        db,
        title="Static Site Materialization Contract",
        content="Use typed ops for index.html and css/style.css.",
        applies_to=["planning", "validation"],
        knowledge_type=KnowledgeType.format_guide,
        tags=["static-site", "html", "css"],
        priority=20,
    )

    with patch.object(svc, "_has_indexed_points", return_value=False):
        ctx = svc.retrieve(
            query="Create a plain static site with index.html and css/style.css",
            trigger_phase="planning",
            knowledge_types=[KnowledgeType.format_guide],
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


def test_qdrant_unavailable_during_startup_still_returns_sqlite_fallback(db):
    _make_item(db, title="Startup Fallback", knowledge_type=KnowledgeType.format_guide)

    with patch.object(
        KnowledgeService, "_ensure_collection", side_effect=Exception("Qdrant down")
    ):
        svc = KnowledgeService(qdrant_url="http://127.0.0.1:1")

    ctx = svc.retrieve(
        query="format guide",
        trigger_phase="planning",
        knowledge_types=[KnowledgeType.format_guide],
        db=db,
    )

    assert any(ref.title == "Startup Fallback" for ref in ctx.retrieved_items)
    assert ctx.retrieval_reason == "sqlite_fallback_qdrant_or_embedding_unavailable"


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


# ---------------------------------------------------------------------------
# Budget fix: truncated-length accounting and continue-not-break
# ---------------------------------------------------------------------------


def test_budget_uses_truncated_length_not_raw_length(svc, db):
    """Item whose raw length exceeds remaining budget but truncated length fits is included."""
    # guide1 fills 1006 raw chars (≤ 800 truncated → 800 effective)
    guide1 = _make_item(
        db,
        title="Guide First",
        content="A" * 1006,
        applies_to=["planning"],
        knowledge_type=KnowledgeType.format_guide,
        priority=10,
    )
    # guide2: raw=1795, truncated=800 — previously dropped by raw-length check (800+1795>2000)
    # With fix: 800+800=1600 ≤ 2000 → must be included
    guide2 = _make_item(
        db,
        title="Guide Second Large Raw",
        content="B" * 1795,
        applies_to=["planning"],
        knowledge_type=KnowledgeType.format_guide,
        priority=9,
    )
    with patch.object(svc, "_embed", return_value=_FAKE_VECTOR):
        svc.ingest(guide1)
        svc.ingest(guide2)
        ctx = svc.retrieve(
            query="format",
            trigger_phase="planning",
            knowledge_types=[KnowledgeType.format_guide],
            top_k=10,
            db=db,
        )
    titles = {ref.title for ref in ctx.retrieved_items}
    assert "Guide Second Large Raw" in titles, (
        "Item with raw_len=1795 (truncated=800) must fit after an 1006-char item "
        "when budget=2000; was incorrectly dropped by raw-length accounting"
    )


def test_oversized_item_after_truncation_is_still_excluded(svc, db):
    """Item whose truncated length still overflows the remaining budget is excluded."""
    # guide1: raw=1300, truncated=800 → effective=800
    _make_item(
        db,
        title="Guide Fills Most Budget",
        content="A" * 1300,
        applies_to=["planning"],
        knowledge_type=KnowledgeType.format_guide,
        priority=10,
    )
    # guide2: raw=1500, truncated=800 → 800+800=1600 ≤ 2000 → fits
    _make_item(
        db,
        title="Guide Also Fits",
        content="B" * 1500,
        applies_to=["planning"],
        knowledge_type=KnowledgeType.format_guide,
        priority=9,
    )
    # guide3: raw=900, truncated=800 → 1600+800=2400 > 2000 → must be excluded
    excluded = _make_item(
        db,
        title="Guide Too Large After Two",
        content="C" * 900,
        applies_to=["planning"],
        knowledge_type=KnowledgeType.format_guide,
        priority=8,
    )
    with patch.object(svc, "_has_indexed_points", return_value=False):
        ctx = svc.retrieve(
            query="format",
            trigger_phase="planning",
            knowledge_types=[KnowledgeType.format_guide],
            top_k=10,
            db=db,
        )
    titles = {ref.title for ref in ctx.retrieved_items}
    assert excluded.title not in titles


def test_later_small_item_included_after_oversized_item_skipped(svc, db):
    """A small item after a too-large item is still included (continue, not break)."""
    # guide1: raw=1006, truncated=800 → effective=800, total=800
    _make_item(
        db,
        title="Guide One",
        content="A" * 1006,
        applies_to=["planning"],
        knowledge_type=KnowledgeType.format_guide,
        priority=10,
    )
    # guide2: raw=1795, truncated=800 → total would be 1600 ≤ 2000 → added, total=1600
    _make_item(
        db,
        title="Guide Two Large",
        content="B" * 1795,
        applies_to=["planning"],
        knowledge_type=KnowledgeType.format_guide,
        priority=9,
    )
    # guide3: raw=276 → total would be 1876 ≤ 2000 → must be included
    small = _make_item(
        db,
        title="Guide Three Small",
        content="C" * 276,
        applies_to=["planning"],
        knowledge_type=KnowledgeType.format_guide,
        priority=8,
    )
    with patch.object(svc, "_embed", return_value=_FAKE_VECTOR):
        svc.ingest(
            db.query(KnowledgeItem).filter(KnowledgeItem.title == "Guide One").first()
        )
        svc.ingest(
            db.query(KnowledgeItem)
            .filter(KnowledgeItem.title == "Guide Two Large")
            .first()
        )
        svc.ingest(small)
        ctx = svc.retrieve(
            query="format",
            trigger_phase="planning",
            knowledge_types=[KnowledgeType.format_guide],
            top_k=10,
            db=db,
        )
    titles = {ref.title for ref in ctx.retrieved_items}
    assert "Guide Three Small" in titles, (
        "Small item after oversized item must not be discarded; "
        "budget loop must use continue not break"
    )


def test_total_budget_still_enforced_after_fix(svc, db):
    """Total injected chars must not exceed KNOWLEDGE_MAX_TOTAL_CHARS after fix."""
    for i in range(3):
        _make_item(
            db,
            title=f"Budget Item {i}",
            content="X" * 900,
            applies_to=["planning"],
            knowledge_type=KnowledgeType.format_guide,
            priority=10 - i,
        )
    with patch.object(svc, "_has_indexed_points", return_value=False):
        ctx = svc.retrieve(
            query="format",
            trigger_phase="planning",
            knowledge_types=[KnowledgeType.format_guide],
            top_k=10,
            db=db,
        )
    total_injected = sum(len(ref.content) for ref in ctx.retrieved_items)
    assert total_injected <= 2000


def test_static_site_guide_fits_after_format_guide_in_2000_char_budget(svc, db):
    """Reproduces the Garden Story production scenario.

    A 1006-char format guide is sorted first (format_guide rank=2 < task_example rank=4).
    The Static Site Task Planning Guide has raw_len=1795 but truncated_len=800.
    With the fix, both fit in the 2000-char budget: 800+800=1600.
    """
    format_guide = _make_item(
        db,
        title="Workspace Root Never Nested",
        content="W" * 1006,
        applies_to=["planning"],
        knowledge_type=KnowledgeType.format_guide,
        priority=10,
    )
    static_site_guide = _make_item(
        db,
        title="Static Site Task Planning Guide",
        content="S" * 1795,
        applies_to=["planning"],
        knowledge_type=KnowledgeType.task_example,
        tags=["static-site", "html", "css"],
        priority=8,
    )
    with patch.object(svc, "_embed", return_value=_FAKE_VECTOR):
        svc.ingest(format_guide)
        svc.ingest(static_site_guide)
        ctx = svc.retrieve(
            query="Build a plain static site with index.html and css/style.css",
            trigger_phase="planning",
            knowledge_types=[KnowledgeType.format_guide, KnowledgeType.task_example],
            top_k=10,
            db=db,
        )
    titles = {ref.title for ref in ctx.retrieved_items}
    assert "Static Site Task Planning Guide" in titles, (
        "Static Site Guide (raw=1795, truncated=800) must be included alongside "
        "the 1006-char format guide; total effective size 1600 ≤ 2000"
    )
    assert "Workspace Root Never Nested" in titles


def test_budget_ordering_unchanged_type_rank_still_applies(svc, db):
    """format_guide (rank=2) still sorts before task_example (rank=4) at equal priority."""
    task_ex = _make_item(
        db,
        title="Task Example Item",
        content="T" * 100,
        applies_to=["planning"],
        knowledge_type=KnowledgeType.task_example,
        priority=5,
    )
    fmt_guide = _make_item(
        db,
        title="Format Guide Item",
        content="F" * 100,
        applies_to=["planning"],
        knowledge_type=KnowledgeType.format_guide,
        priority=5,
    )
    with patch.object(svc, "_has_indexed_points", return_value=False):
        ctx = svc.retrieve(
            query="format guide",
            trigger_phase="planning",
            knowledge_types=[KnowledgeType.format_guide, KnowledgeType.task_example],
            top_k=10,
            db=db,
        )
    # format_guide must appear before task_example
    types_in_order = [ref.knowledge_type for ref in ctx.retrieved_items]
    fmt_pos = next(
        (i for i, t in enumerate(types_in_order) if t == KnowledgeType.format_guide),
        None,
    )
    task_pos = next(
        (i for i, t in enumerate(types_in_order) if t == KnowledgeType.task_example),
        None,
    )
    assert fmt_pos is not None
    assert task_pos is not None
    assert fmt_pos < task_pos, "format_guide must still rank before task_example"
