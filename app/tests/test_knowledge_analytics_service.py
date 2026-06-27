"""Tests for KnowledgeAnalyticsService — Phase 15A-4.

All tests are unit tests (SQLite in-memory, no HTTP, no event journal).
Covers:
- Empty database
- Retrieval count
- Used-in-prompt count
- Hit rate null when no retrievals / computed correctly
- Effectiveness rate null / ignores null was_effective / computed correctly
- Phase utilization grouping
- Top items ordered by retrieval count
- Missing knowledge item handled safely (orphaned FK)
- Low effectiveness items when enough data exists
- Rolling window filtering
- API contract shape
- JSON serialization
"""

from __future__ import annotations

import json
import uuid
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine, text
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import (
    Base,
    KnowledgeItem,
    KnowledgeUsageLog,
    Project,
    Session as SessionModel,
    Task,
)
from app.services.analytics.knowledge_analytics_service import (
    KnowledgeAnalyticsService,
    _LOW_EFFECTIVENESS_THRESHOLD,
    _MIN_RETRIEVAL_THRESHOLD,
    _TOP_ITEMS_LIMIT,
)

_WINDOW_LABELS = ("7d", "30d", "all_time")

_WINDOW_KEYS = {
    "retrieval_count",
    "used_in_prompt_count",
    "knowledge_hit_rate",
    "effectiveness_rate",
    "phase_utilization",
    "top_items",
    "low_effectiveness_items",
}

# ── fixtures ───────────────────────────────────────────────────────────────────


@pytest.fixture()
def mem_db():
    engine = create_engine(
        "sqlite://",
        connect_args={"check_same_thread": False},
        poolclass=StaticPool,
    )
    Base.metadata.create_all(bind=engine)
    SessionLocal = sessionmaker(bind=engine)
    db = SessionLocal()
    try:
        yield db
    finally:
        db.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def _project(db) -> Project:
    p = Project(name="test-project", workspace_path="/tmp/test")
    db.add(p)
    db.flush()
    return p


def _session(db, project: Project) -> SessionModel:
    s = SessionModel(
        project_id=project.id,
        name="test-session",
        status="completed",
        started_at=datetime.now(UTC),
        created_at=datetime.now(UTC),
    )
    db.add(s)
    db.flush()
    return s


def _knowledge_item(
    db,
    *,
    title: str = "Test Item",
    knowledge_type: str = "pattern",
    checksum: str | None = None,
) -> KnowledgeItem:
    item = KnowledgeItem(
        id=str(uuid.uuid4()),
        title=title,
        content="Some content",
        knowledge_type=knowledge_type,
        checksum=checksum or str(uuid.uuid4()),
    )
    db.add(item)
    db.flush()
    return item


def _usage_log(
    db,
    session: SessionModel,
    item: KnowledgeItem,
    *,
    used_in_prompt: bool = True,
    was_effective: bool | None = None,
    trigger_phase: str = "planning",
    confidence: float = 0.9,
    created_at: datetime | None = None,
) -> KnowledgeUsageLog:
    log = KnowledgeUsageLog(
        id=str(uuid.uuid4()),
        session_id=session.id,
        knowledge_item_id=item.id,
        trigger_phase=trigger_phase,
        retrieval_reason="test reason",
        confidence=confidence,
        rank=1,
        used_in_prompt=used_in_prompt,
        was_effective=was_effective,
        created_at=created_at or datetime.now(UTC),
    )
    db.add(log)
    db.flush()
    return log


# ── tests: empty database ──────────────────────────────────────────────────────


class TestEmptyDatabase:
    def test_all_counts_zero(self, mem_db):
        result = KnowledgeAnalyticsService(mem_db).compute()
        for label in _WINDOW_LABELS:
            w = result["windows"][label]
            assert w["retrieval_count"] == 0
            assert w["used_in_prompt_count"] == 0

    def test_rates_null(self, mem_db):
        result = KnowledgeAnalyticsService(mem_db).compute()
        for label in _WINDOW_LABELS:
            w = result["windows"][label]
            assert w["knowledge_hit_rate"] is None
            assert w["effectiveness_rate"] is None

    def test_collections_empty(self, mem_db):
        result = KnowledgeAnalyticsService(mem_db).compute()
        for label in _WINDOW_LABELS:
            w = result["windows"][label]
            assert w["phase_utilization"] == {}
            assert w["top_items"] == []
            assert w["low_effectiveness_items"] == []

    def test_top_level_shape(self, mem_db):
        result = KnowledgeAnalyticsService(mem_db).compute()
        assert "windows" in result
        assert "generated_at" in result
        assert result["metrics_version"] == 1
        assert set(result["windows"]) == {"7d", "30d", "all_time"}

    def test_window_keys_present(self, mem_db):
        result = KnowledgeAnalyticsService(mem_db).compute()
        for label in _WINDOW_LABELS:
            assert (
                set(result["windows"][label]) == _WINDOW_KEYS
            ), f"Window '{label}' has wrong keys"


# ── tests: retrieval count ─────────────────────────────────────────────────────


class TestRetrievalCount:
    def test_single_retrieval(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item = _knowledge_item(mem_db)
        _usage_log(mem_db, s, item)
        mem_db.commit()

        w = KnowledgeAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["retrieval_count"] == 1

    def test_multiple_retrievals(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item = _knowledge_item(mem_db)
        for _ in range(5):
            _usage_log(mem_db, s, item)
        mem_db.commit()

        w = KnowledgeAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["retrieval_count"] == 5

    def test_counts_all_items(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item1 = _knowledge_item(mem_db, title="A")
        item2 = _knowledge_item(mem_db, title="B")
        _usage_log(mem_db, s, item1)
        _usage_log(mem_db, s, item2)
        _usage_log(mem_db, s, item2)
        mem_db.commit()

        w = KnowledgeAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["retrieval_count"] == 3


# ── tests: used_in_prompt count ───────────────────────────────────────────────


class TestUsedInPromptCount:
    def test_only_used_rows_counted(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item = _knowledge_item(mem_db)
        _usage_log(mem_db, s, item, used_in_prompt=True)
        _usage_log(mem_db, s, item, used_in_prompt=False)
        _usage_log(mem_db, s, item, used_in_prompt=True)
        mem_db.commit()

        w = KnowledgeAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["retrieval_count"] == 3
        assert w["used_in_prompt_count"] == 2

    def test_zero_when_none_used(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item = _knowledge_item(mem_db)
        _usage_log(mem_db, s, item, used_in_prompt=False)
        mem_db.commit()

        w = KnowledgeAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["used_in_prompt_count"] == 0


# ── tests: knowledge_hit_rate ─────────────────────────────────────────────────


class TestKnowledgeHitRate:
    def test_null_when_no_retrievals(self, mem_db):
        result = KnowledgeAnalyticsService(mem_db).compute()
        for label in _WINDOW_LABELS:
            assert result["windows"][label]["knowledge_hit_rate"] is None

    def test_one_when_all_used(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item = _knowledge_item(mem_db)
        _usage_log(mem_db, s, item, used_in_prompt=True)
        _usage_log(mem_db, s, item, used_in_prompt=True)
        mem_db.commit()

        w = KnowledgeAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["knowledge_hit_rate"] == 1.0

    def test_zero_when_none_used(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item = _knowledge_item(mem_db)
        _usage_log(mem_db, s, item, used_in_prompt=False)
        mem_db.commit()

        w = KnowledgeAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["knowledge_hit_rate"] == 0.0

    def test_partial_hit_rate(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item = _knowledge_item(mem_db)
        _usage_log(mem_db, s, item, used_in_prompt=True)
        _usage_log(mem_db, s, item, used_in_prompt=True)
        _usage_log(mem_db, s, item, used_in_prompt=False)
        _usage_log(mem_db, s, item, used_in_prompt=False)
        mem_db.commit()

        w = KnowledgeAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["knowledge_hit_rate"] == 0.5


# ── tests: effectiveness_rate ─────────────────────────────────────────────────


class TestEffectivenessRate:
    def test_null_when_no_prompt_used_rows(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item = _knowledge_item(mem_db)
        _usage_log(mem_db, s, item, used_in_prompt=False, was_effective=True)
        mem_db.commit()

        w = KnowledgeAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["effectiveness_rate"] is None

    def test_null_was_effective_not_counted_as_false(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item = _knowledge_item(mem_db)
        # All used_in_prompt=True, but was_effective is NULL → 0 effective / 2 used
        _usage_log(mem_db, s, item, used_in_prompt=True, was_effective=None)
        _usage_log(mem_db, s, item, used_in_prompt=True, was_effective=None)
        mem_db.commit()

        w = KnowledgeAnalyticsService(mem_db).compute()["windows"]["all_time"]
        # Numerator is 0 (no was_effective=TRUE), denominator is 2 → 0.0
        assert w["effectiveness_rate"] == 0.0

    def test_full_effectiveness(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item = _knowledge_item(mem_db)
        _usage_log(mem_db, s, item, used_in_prompt=True, was_effective=True)
        _usage_log(mem_db, s, item, used_in_prompt=True, was_effective=True)
        mem_db.commit()

        w = KnowledgeAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["effectiveness_rate"] == 1.0

    def test_partial_effectiveness(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item = _knowledge_item(mem_db)
        _usage_log(mem_db, s, item, used_in_prompt=True, was_effective=True)
        _usage_log(mem_db, s, item, used_in_prompt=True, was_effective=False)
        _usage_log(mem_db, s, item, used_in_prompt=True, was_effective=None)
        _usage_log(mem_db, s, item, used_in_prompt=True, was_effective=None)
        mem_db.commit()

        w = KnowledgeAnalyticsService(mem_db).compute()["windows"]["all_time"]
        # 1 effective / 4 used_in_prompt = 0.25
        assert w["effectiveness_rate"] == 0.25

    def test_not_used_in_prompt_excluded_from_denominator(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item = _knowledge_item(mem_db)
        _usage_log(mem_db, s, item, used_in_prompt=True, was_effective=True)
        _usage_log(mem_db, s, item, used_in_prompt=False, was_effective=True)
        mem_db.commit()

        w = KnowledgeAnalyticsService(mem_db).compute()["windows"]["all_time"]
        # Only 1 used_in_prompt row, which is effective → 1.0
        assert w["effectiveness_rate"] == 1.0


# ── tests: phase_utilization ──────────────────────────────────────────────────


class TestPhaseUtilization:
    def test_single_phase(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item = _knowledge_item(mem_db)
        _usage_log(mem_db, s, item, trigger_phase="planning")
        _usage_log(mem_db, s, item, trigger_phase="planning")
        mem_db.commit()

        w = KnowledgeAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["phase_utilization"] == {"planning": 2}

    def test_multiple_phases(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item = _knowledge_item(mem_db)
        _usage_log(mem_db, s, item, trigger_phase="planning")
        _usage_log(mem_db, s, item, trigger_phase="debug_repair")
        _usage_log(mem_db, s, item, trigger_phase="debug_repair")
        _usage_log(mem_db, s, item, trigger_phase="completion_repair")
        mem_db.commit()

        w = KnowledgeAnalyticsService(mem_db).compute()["windows"]["all_time"]
        pu = w["phase_utilization"]
        assert pu["planning"] == 1
        assert pu["debug_repair"] == 2
        assert pu["completion_repair"] == 1

    def test_empty_when_no_logs(self, mem_db):
        w = KnowledgeAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["phase_utilization"] == {}


# ── tests: top_items ──────────────────────────────────────────────────────────


class TestTopItems:
    def test_ordered_by_retrieval_count_desc(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item_a = _knowledge_item(mem_db, title="A")
        item_b = _knowledge_item(mem_db, title="B")
        item_c = _knowledge_item(mem_db, title="C")
        # B is most retrieved (3), C is second (2), A is last (1)
        _usage_log(mem_db, s, item_a)
        _usage_log(mem_db, s, item_b)
        _usage_log(mem_db, s, item_b)
        _usage_log(mem_db, s, item_b)
        _usage_log(mem_db, s, item_c)
        _usage_log(mem_db, s, item_c)
        mem_db.commit()

        w = KnowledgeAnalyticsService(mem_db).compute()["windows"]["all_time"]
        tops = w["top_items"]
        assert len(tops) == 3
        assert tops[0]["title"] == "B"
        assert tops[0]["retrieval_count"] == 3
        assert tops[1]["title"] == "C"
        assert tops[2]["title"] == "A"

    def test_item_fields_present(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item = _knowledge_item(mem_db, title="My Item")
        _usage_log(
            mem_db, s, item, used_in_prompt=True, was_effective=True, confidence=0.8
        )
        mem_db.commit()

        tops = KnowledgeAnalyticsService(mem_db).compute()["windows"]["all_time"][
            "top_items"
        ]
        assert len(tops) == 1
        t = tops[0]
        assert t["knowledge_item_id"] == item.id
        assert t["title"] == "My Item"
        assert t["retrieval_count"] == 1
        assert t["used_in_prompt_count"] == 1
        assert t["hit_rate"] == 1.0
        assert t["effectiveness_rate"] == 1.0
        assert t["avg_confidence"] == 0.8

    def test_hit_rate_null_when_no_retrievals_per_item(self, mem_db):
        # Can't happen (an item with 0 retrievals won't appear), but test 0-used case:
        p = _project(mem_db)
        s = _session(mem_db, p)
        item = _knowledge_item(mem_db)
        _usage_log(mem_db, s, item, used_in_prompt=False, was_effective=None)
        mem_db.commit()

        tops = KnowledgeAnalyticsService(mem_db).compute()["windows"]["all_time"][
            "top_items"
        ]
        assert tops[0]["hit_rate"] == 0.0

    def test_effectiveness_rate_null_when_no_prompt_use(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item = _knowledge_item(mem_db)
        _usage_log(mem_db, s, item, used_in_prompt=False)
        mem_db.commit()

        tops = KnowledgeAnalyticsService(mem_db).compute()["windows"]["all_time"][
            "top_items"
        ]
        assert tops[0]["effectiveness_rate"] is None

    def test_limited_to_top_n(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        # Create more items than _TOP_ITEMS_LIMIT
        items = [
            _knowledge_item(mem_db, title=f"Item {i}")
            for i in range(_TOP_ITEMS_LIMIT + 3)
        ]
        for item in items:
            _usage_log(mem_db, s, item)
        mem_db.commit()

        tops = KnowledgeAnalyticsService(mem_db).compute()["windows"]["all_time"][
            "top_items"
        ]
        assert len(tops) == _TOP_ITEMS_LIMIT


# ── tests: missing knowledge item handled safely ───────────────────────────────


class TestMissingKnowledgeItemHandledSafely:
    def test_orphaned_log_returns_none_title(self, mem_db):
        """A usage log referencing a non-existent knowledge_item should not crash."""
        p = _project(mem_db)
        s = _session(mem_db, p)
        orphan_id = str(uuid.uuid4())
        # Insert directly without the KnowledgeItem to simulate orphaned FK
        # (SQLite doesn't enforce FKs by default)
        log = KnowledgeUsageLog(
            id=str(uuid.uuid4()),
            session_id=s.id,
            knowledge_item_id=orphan_id,
            trigger_phase="planning",
            retrieval_reason="test",
            confidence=0.5,
            rank=1,
            used_in_prompt=True,
            was_effective=None,
            created_at=datetime.now(UTC),
        )
        mem_db.add(log)
        mem_db.commit()

        result = KnowledgeAnalyticsService(mem_db).compute()
        w = result["windows"]["all_time"]
        # Should not crash; retrieval counted
        assert w["retrieval_count"] == 1
        # top_items should include the item with title=None
        tops = w["top_items"]
        assert len(tops) == 1
        assert tops[0]["title"] is None
        assert tops[0]["knowledge_item_id"] == orphan_id


# ── tests: low_effectiveness_items ────────────────────────────────────────────


class TestLowEffectivenessItems:
    def test_empty_when_no_logs(self, mem_db):
        w = KnowledgeAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["low_effectiveness_items"] == []

    def test_empty_when_no_was_effective_data(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item = _knowledge_item(mem_db)
        # Many retrievals but all was_effective=None
        for _ in range(_MIN_RETRIEVAL_THRESHOLD + 2):
            _usage_log(mem_db, s, item, used_in_prompt=True, was_effective=None)
        mem_db.commit()

        w = KnowledgeAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["low_effectiveness_items"] == []

    def test_empty_when_below_min_retrieval_threshold(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item = _knowledge_item(mem_db)
        # Fewer than threshold retrievals with was_effective=False
        for _ in range(_MIN_RETRIEVAL_THRESHOLD - 1):
            _usage_log(mem_db, s, item, used_in_prompt=True, was_effective=False)
        mem_db.commit()

        w = KnowledgeAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["low_effectiveness_items"] == []

    def test_item_included_when_low_effectiveness(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item = _knowledge_item(mem_db, title="Bad Item")
        # Enough retrievals, was_effective data present, all ineffective
        for _ in range(_MIN_RETRIEVAL_THRESHOLD):
            _usage_log(mem_db, s, item, used_in_prompt=True, was_effective=False)
        mem_db.commit()

        w = KnowledgeAnalyticsService(mem_db).compute()["windows"]["all_time"]
        low = w["low_effectiveness_items"]
        assert len(low) == 1
        assert low[0]["title"] == "Bad Item"
        assert low[0]["effectiveness_rate"] == 0.0

    def test_item_excluded_when_high_effectiveness(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item = _knowledge_item(mem_db)
        for _ in range(_MIN_RETRIEVAL_THRESHOLD):
            _usage_log(mem_db, s, item, used_in_prompt=True, was_effective=True)
        mem_db.commit()

        w = KnowledgeAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["low_effectiveness_items"] == []

    def test_sorted_worst_first(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item_a = _knowledge_item(mem_db, title="A")
        item_b = _knowledge_item(mem_db, title="B")
        # item_a: 0/3 effective (rate=0.0), item_b: 1/4 effective (rate=0.25 — at boundary)
        for _ in range(_MIN_RETRIEVAL_THRESHOLD):
            _usage_log(mem_db, s, item_a, used_in_prompt=True, was_effective=False)
        _usage_log(mem_db, s, item_b, used_in_prompt=True, was_effective=True)
        for _ in range(3):
            _usage_log(mem_db, s, item_b, used_in_prompt=True, was_effective=False)
        mem_db.commit()

        w = KnowledgeAnalyticsService(mem_db).compute()["windows"]["all_time"]
        low = w["low_effectiveness_items"]
        # item_b has rate=0.25 which is AT the threshold, not below — should be excluded
        # item_a has rate=0.0 — included
        titles = [x["title"] for x in low]
        assert "A" in titles
        # 0.25 is NOT < 0.25, so item_b is excluded
        assert "B" not in titles


# ── tests: rolling window filtering ───────────────────────────────────────────


class TestRollingWindowFiltering:
    def test_old_log_excluded_from_7d(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item = _knowledge_item(mem_db)
        old = datetime.now(UTC) - timedelta(days=10)
        _usage_log(mem_db, s, item, created_at=old)
        mem_db.commit()

        result = KnowledgeAnalyticsService(mem_db).compute()
        assert result["windows"]["7d"]["retrieval_count"] == 0
        assert result["windows"]["all_time"]["retrieval_count"] == 1

    def test_old_log_excluded_from_30d(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item = _knowledge_item(mem_db)
        old = datetime.now(UTC) - timedelta(days=35)
        _usage_log(mem_db, s, item, created_at=old)
        mem_db.commit()

        result = KnowledgeAnalyticsService(mem_db).compute()
        assert result["windows"]["30d"]["retrieval_count"] == 0
        assert result["windows"]["all_time"]["retrieval_count"] == 1

    def test_recent_log_appears_in_7d(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item = _knowledge_item(mem_db)
        recent = datetime.now(UTC) - timedelta(days=2)
        _usage_log(mem_db, s, item, created_at=recent)
        mem_db.commit()

        result = KnowledgeAnalyticsService(mem_db).compute()
        assert result["windows"]["7d"]["retrieval_count"] == 1
        assert result["windows"]["30d"]["retrieval_count"] == 1
        assert result["windows"]["all_time"]["retrieval_count"] == 1

    def test_window_filters_phase_utilization(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item = _knowledge_item(mem_db)
        old = datetime.now(UTC) - timedelta(days=10)
        _usage_log(mem_db, s, item, trigger_phase="old_phase", created_at=old)
        _usage_log(mem_db, s, item, trigger_phase="new_phase")
        mem_db.commit()

        result = KnowledgeAnalyticsService(mem_db).compute()
        assert "old_phase" not in result["windows"]["7d"]["phase_utilization"]
        assert "old_phase" in result["windows"]["all_time"]["phase_utilization"]

    def test_window_filters_top_items(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item = _knowledge_item(mem_db)
        old = datetime.now(UTC) - timedelta(days=10)
        _usage_log(mem_db, s, item, created_at=old)
        mem_db.commit()

        result = KnowledgeAnalyticsService(mem_db).compute()
        assert result["windows"]["7d"]["top_items"] == []
        assert len(result["windows"]["all_time"]["top_items"]) == 1


# ── tests: API contract ────────────────────────────────────────────────────────


class TestApiContract:
    def test_endpoint_returns_correct_shape(self, mem_db):
        from app.api.v1.endpoints.analytics import get_knowledge_analytics

        class _FakeUser:
            id = 1

        result = get_knowledge_analytics(current_user=_FakeUser(), db=mem_db)
        assert "windows" in result
        assert "generated_at" in result
        assert result["metrics_version"] == 1
        assert set(result["windows"]) == {"7d", "30d", "all_time"}

    def test_all_window_keys_present(self, mem_db):
        from app.api.v1.endpoints.analytics import get_knowledge_analytics

        class _FakeUser:
            id = 1

        result = get_knowledge_analytics(current_user=_FakeUser(), db=mem_db)
        for label in _WINDOW_LABELS:
            assert (
                set(result["windows"][label]) == _WINDOW_KEYS
            ), f"Window '{label}' missing keys"

    def test_generated_at_is_iso8601(self, mem_db):
        from app.api.v1.endpoints.analytics import get_knowledge_analytics

        class _FakeUser:
            id = 1

        result = get_knowledge_analytics(current_user=_FakeUser(), db=mem_db)
        dt = datetime.fromisoformat(result["generated_at"])
        assert dt.tzinfo is not None


# ── tests: JSON serialization ─────────────────────────────────────────────────


class TestJsonSerialization:
    def test_empty_result_serializable(self, mem_db):
        result = KnowledgeAnalyticsService(mem_db).compute()
        serialized = json.dumps(result)
        parsed = json.loads(serialized)
        assert parsed["metrics_version"] == 1

    def test_null_rates_serialize_as_null(self, mem_db):
        result = KnowledgeAnalyticsService(mem_db).compute()
        serialized = json.dumps(result)
        parsed = json.loads(serialized)
        for label in _WINDOW_LABELS:
            assert parsed["windows"][label]["knowledge_hit_rate"] is None
            assert parsed["windows"][label]["effectiveness_rate"] is None

    def test_top_items_serialize_as_list(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item = _knowledge_item(mem_db, title="Serialize Test")
        _usage_log(
            mem_db, s, item, used_in_prompt=True, was_effective=True, confidence=0.95
        )
        mem_db.commit()

        result = KnowledgeAnalyticsService(mem_db).compute()
        serialized = json.dumps(result)
        parsed = json.loads(serialized)
        tops = parsed["windows"]["all_time"]["top_items"]
        assert isinstance(tops, list)
        assert len(tops) == 1
        assert tops[0]["title"] == "Serialize Test"
        assert isinstance(tops[0]["avg_confidence"], float)

    def test_phase_utilization_serializes_as_object(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        item = _knowledge_item(mem_db)
        _usage_log(mem_db, s, item, trigger_phase="planning")
        mem_db.commit()

        result = KnowledgeAnalyticsService(mem_db).compute()
        serialized = json.dumps(result)
        parsed = json.loads(serialized)
        pu = parsed["windows"]["all_time"]["phase_utilization"]
        assert isinstance(pu, dict)
        assert pu["planning"] == 1
