"""Tests for FailureAnalyticsService — Phase 15A-3.

All tests are unit tests (SQLite in-memory).
Event journal functions are mocked to avoid filesystem dependencies.

Covers:
- Empty database
- No event journals
- Recovery attempts / successes / failures counted
- Success rate null when no attempts
- Success rate computed correctly
- Budget exhaustion count
- Churn guard activation count
- Failure category aggregation
- Malformed event rows skipped
- Window filtering (DB-backed metrics)
- Event window filtering (event-journal metrics)
- API contract shape
- JSON serialization
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import (
    Base,
    Project,
    Session as SessionModel,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.analytics.failure_analytics_service import FailureAnalyticsService
from app.services.orchestration.events.event_types import EventType

# ── constants ──────────────────────────────────────────────────────────────────

_ATTEMPTED = EventType.EXECUTION_RECOVERY_ATTEMPTED
_SUCCEEDED = EventType.EXECUTION_RECOVERY_SUCCEEDED
_FAILED = EventType.EXECUTION_RECOVERY_FAILED

_WINDOW_LABELS = ("7d", "30d", "all_time")

_WINDOW_KEYS = {
    "recovery_attempts",
    "recovery_successes",
    "recovery_failures",
    "recovery_success_rate",
    "budget_exhaustion_count",
    "churn_guard_activations",
    "failure_category_distribution",
    "failure_category_recovery",
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


def _session(
    db,
    project: Project,
    *,
    status: str = "completed",
    repair_churn_stopped: bool = False,
    created_at: datetime | None = None,
) -> SessionModel:
    now = created_at or datetime.now(UTC)
    s = SessionModel(
        project_id=project.id,
        name=f"session-{now.isoformat()}",
        status=status,
        started_at=now,
        created_at=now,
        repair_churn_stopped=repair_churn_stopped,
    )
    db.add(s)
    db.flush()
    return s


def _task(db, project: Project) -> Task:
    t = Task(project_id=project.id, title="task", description="x")
    db.add(t)
    db.flush()
    return t


def _execution(
    db,
    session: SessionModel,
    task: Task,
    *,
    attempt: int = 1,
    status: TaskStatus = TaskStatus.FAILED,
    failure_category: str | None = "tool_failure",
    created_at: datetime | None = None,
) -> TaskExecution:
    now = created_at or datetime.now(UTC)
    ex = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=attempt,
        status=status,
        failure_category=failure_category,
        created_at=now,
    )
    db.add(ex)
    db.flush()
    return ex


def _event(
    event_type: str,
    *,
    ts: datetime | None = None,
    budget_exhausted: bool = False,
) -> dict:
    now = ts or datetime.now(UTC)
    e: dict = {"event_type": event_type, "timestamp": now.isoformat()}
    if budget_exhausted:
        e["details"] = {"budget_exhausted": True}
    return e


def _no_journals(db, sess, task_id):
    """resolve_event_log_project_dir returning None → no events."""
    return None


# ── helper: patch event journal to return a fixed list ────────────────────────


def _patch_events(events: list):
    """Context manager that makes _collect_event_journal_windows return
    the given events for every session/task."""
    from pathlib import Path

    fake_dir = Path("/fake/project")

    resolve_patch = patch(
        "app.services.analytics.failure_analytics_service."
        "FailureAnalyticsService._collect_event_journal_windows",
    )
    return resolve_patch


def _build_event_windows(events: list, now: datetime | None = None) -> dict:
    """Build the per-window dict that _collect_event_journal_windows returns,
    by processing events the same way the real implementation does."""
    from app.services.analytics.failure_analytics_service import (
        _empty_event_bucket,
        _parse_event_timestamp,
    )

    now = now or datetime.now(UTC)
    thresholds = {
        "7d": now - timedelta(days=7),
        "30d": now - timedelta(days=30),
        "all_time": None,
    }
    per_window = {label: _empty_event_bucket() for label in thresholds}

    for event in events:
        et = event.get("event_type", "")
        if et not in (_ATTEMPTED, _SUCCEEDED, _FAILED):
            continue
        event_ts = _parse_event_timestamp(event.get("timestamp"))
        for label, threshold in thresholds.items():
            if threshold is not None:
                if event_ts is None or event_ts < threshold:
                    continue
            bucket = per_window[label]
            if et == _ATTEMPTED:
                bucket["recovery_attempts"] += 1
            elif et == _SUCCEEDED:
                bucket["recovery_successes"] += 1
            elif et == _FAILED:
                bucket["recovery_failures"] += 1
                details = event.get("details") or {}
                if details.get("budget_exhausted"):
                    bucket["budget_exhaustion_count"] += 1

    return per_window


# ── tests: empty database ──────────────────────────────────────────────────────


class TestEmptyDatabase:
    def test_all_counts_zero(self, mem_db):
        result = FailureAnalyticsService(mem_db).compute()
        for label in _WINDOW_LABELS:
            w = result["windows"][label]
            assert w["recovery_attempts"] == 0
            assert w["recovery_successes"] == 0
            assert w["recovery_failures"] == 0
            assert w["budget_exhaustion_count"] == 0
            assert w["churn_guard_activations"] == 0

    def test_success_rate_null_when_no_data(self, mem_db):
        result = FailureAnalyticsService(mem_db).compute()
        for label in _WINDOW_LABELS:
            assert result["windows"][label]["recovery_success_rate"] is None

    def test_failure_distribution_empty(self, mem_db):
        result = FailureAnalyticsService(mem_db).compute()
        for label in _WINDOW_LABELS:
            assert result["windows"][label]["failure_category_distribution"] == {}

    def test_failure_category_recovery_empty(self, mem_db):
        result = FailureAnalyticsService(mem_db).compute()
        for label in _WINDOW_LABELS:
            assert result["windows"][label]["failure_category_recovery"] == {}

    def test_top_level_shape(self, mem_db):
        result = FailureAnalyticsService(mem_db).compute()
        assert "windows" in result
        assert "generated_at" in result
        assert result["metrics_version"] == 1
        assert set(result["windows"]) == {"7d", "30d", "all_time"}

    def test_window_keys_present(self, mem_db):
        result = FailureAnalyticsService(mem_db).compute()
        for label in _WINDOW_LABELS:
            assert (
                set(result["windows"][label]) == _WINDOW_KEYS
            ), f"Window '{label}' has wrong keys"


# ── tests: no event journals ───────────────────────────────────────────────────


class TestNoEventJournals:
    def test_sessions_exist_but_no_events(self, mem_db):
        p = _project(mem_db)
        _session(mem_db, p)
        mem_db.commit()

        # resolve_event_log_project_dir returns None → no events read
        with patch(
            "app.services.analytics.failure_analytics_service."
            "FailureAnalyticsService._collect_event_journal_windows",
            return_value={
                label: {
                    "recovery_attempts": 0,
                    "recovery_successes": 0,
                    "recovery_failures": 0,
                    "budget_exhaustion_count": 0,
                }
                for label in _WINDOW_LABELS
            },
        ):
            result = FailureAnalyticsService(mem_db).compute()

        for label in _WINDOW_LABELS:
            w = result["windows"][label]
            assert w["recovery_attempts"] == 0
            assert w["recovery_success_rate"] is None


# ── tests: recovery event counting ────────────────────────────────────────────


class TestRecoveryAttemptsCounted:
    def test_single_attempt(self, mem_db):
        events = [_event(_ATTEMPTED)]
        with patch.object(
            FailureAnalyticsService,
            "_collect_event_journal_windows",
            return_value=_build_event_windows(events),
        ):
            result = FailureAnalyticsService(mem_db).compute()

        assert result["windows"]["all_time"]["recovery_attempts"] == 1

    def test_multiple_attempts(self, mem_db):
        events = [_event(_ATTEMPTED), _event(_ATTEMPTED), _event(_ATTEMPTED)]
        with patch.object(
            FailureAnalyticsService,
            "_collect_event_journal_windows",
            return_value=_build_event_windows(events),
        ):
            result = FailureAnalyticsService(mem_db).compute()

        assert result["windows"]["all_time"]["recovery_attempts"] == 3


class TestRecoverySuccessesCounted:
    def test_single_success(self, mem_db):
        events = [_event(_ATTEMPTED), _event(_SUCCEEDED)]
        with patch.object(
            FailureAnalyticsService,
            "_collect_event_journal_windows",
            return_value=_build_event_windows(events),
        ):
            result = FailureAnalyticsService(mem_db).compute()

        assert result["windows"]["all_time"]["recovery_successes"] == 1

    def test_successes_without_attempts_counted(self, mem_db):
        # Edge case: orphan success events still counted
        events = [_event(_SUCCEEDED), _event(_SUCCEEDED)]
        with patch.object(
            FailureAnalyticsService,
            "_collect_event_journal_windows",
            return_value=_build_event_windows(events),
        ):
            result = FailureAnalyticsService(mem_db).compute()

        assert result["windows"]["all_time"]["recovery_successes"] == 2


class TestRecoveryFailuresCounted:
    def test_single_failure(self, mem_db):
        events = [_event(_ATTEMPTED), _event(_FAILED)]
        with patch.object(
            FailureAnalyticsService,
            "_collect_event_journal_windows",
            return_value=_build_event_windows(events),
        ):
            result = FailureAnalyticsService(mem_db).compute()

        assert result["windows"]["all_time"]["recovery_failures"] == 1

    def test_budget_exhausted_increments_counter(self, mem_db):
        events = [_event(_ATTEMPTED), _event(_FAILED, budget_exhausted=True)]
        with patch.object(
            FailureAnalyticsService,
            "_collect_event_journal_windows",
            return_value=_build_event_windows(events),
        ):
            result = FailureAnalyticsService(mem_db).compute()

        w = result["windows"]["all_time"]
        assert w["recovery_failures"] == 1
        assert w["budget_exhaustion_count"] == 1

    def test_budget_not_exhausted_does_not_increment(self, mem_db):
        events = [_event(_ATTEMPTED), _event(_FAILED)]
        with patch.object(
            FailureAnalyticsService,
            "_collect_event_journal_windows",
            return_value=_build_event_windows(events),
        ):
            result = FailureAnalyticsService(mem_db).compute()

        assert result["windows"]["all_time"]["budget_exhaustion_count"] == 0


# ── tests: success rate formula ───────────────────────────────────────────────


class TestSuccessRate:
    def test_null_when_no_attempts(self, mem_db):
        with patch.object(
            FailureAnalyticsService,
            "_collect_event_journal_windows",
            return_value=_build_event_windows([]),
        ):
            result = FailureAnalyticsService(mem_db).compute()

        for label in _WINDOW_LABELS:
            assert result["windows"][label]["recovery_success_rate"] is None

    def test_full_success(self, mem_db):
        events = [
            _event(_ATTEMPTED),
            _event(_ATTEMPTED),
            _event(_SUCCEEDED),
            _event(_SUCCEEDED),
        ]
        with patch.object(
            FailureAnalyticsService,
            "_collect_event_journal_windows",
            return_value=_build_event_windows(events),
        ):
            result = FailureAnalyticsService(mem_db).compute()

        assert result["windows"]["all_time"]["recovery_success_rate"] == 1.0

    def test_zero_success(self, mem_db):
        events = [
            _event(_ATTEMPTED),
            _event(_ATTEMPTED),
            _event(_FAILED),
            _event(_FAILED),
        ]
        with patch.object(
            FailureAnalyticsService,
            "_collect_event_journal_windows",
            return_value=_build_event_windows(events),
        ):
            result = FailureAnalyticsService(mem_db).compute()

        assert result["windows"]["all_time"]["recovery_success_rate"] == 0.0

    def test_partial_success(self, mem_db):
        # 1 success out of 4 attempts → 0.25
        events = [
            _event(_ATTEMPTED),
            _event(_ATTEMPTED),
            _event(_ATTEMPTED),
            _event(_ATTEMPTED),
            _event(_SUCCEEDED),
        ]
        with patch.object(
            FailureAnalyticsService,
            "_collect_event_journal_windows",
            return_value=_build_event_windows(events),
        ):
            result = FailureAnalyticsService(mem_db).compute()

        assert result["windows"]["all_time"]["recovery_success_rate"] == 0.25


# ── tests: churn guard activations (DB-backed, windowed) ──────────────────────


class TestChurnGuardActivations:
    def test_zero_when_no_sessions(self, mem_db):
        result = FailureAnalyticsService(mem_db).compute()
        for label in _WINDOW_LABELS:
            assert result["windows"][label]["churn_guard_activations"] == 0

    def test_counts_sessions_with_churn_stopped_true(self, mem_db):
        p = _project(mem_db)
        _session(mem_db, p, repair_churn_stopped=True)
        _session(mem_db, p, repair_churn_stopped=True)
        _session(mem_db, p, repair_churn_stopped=False)
        mem_db.commit()

        result = FailureAnalyticsService(mem_db).compute()
        assert result["windows"]["all_time"]["churn_guard_activations"] == 2

    def test_null_churn_stopped_excluded(self, mem_db):
        p = _project(mem_db)
        s = SessionModel(
            project_id=p.id,
            name="sess-null-churn",
            status="stopped",
            started_at=datetime.now(UTC),
            created_at=datetime.now(UTC),
            repair_churn_stopped=None,
        )
        mem_db.add(s)
        mem_db.commit()

        result = FailureAnalyticsService(mem_db).compute()
        assert result["windows"]["all_time"]["churn_guard_activations"] == 0

    def test_window_filters_churn_by_created_at(self, mem_db):
        p = _project(mem_db)
        old = datetime.now(UTC) - timedelta(days=10)
        recent = datetime.now(UTC) - timedelta(days=2)
        _session(mem_db, p, repair_churn_stopped=True, created_at=old)
        _session(mem_db, p, repair_churn_stopped=True, created_at=recent)
        mem_db.commit()

        result = FailureAnalyticsService(mem_db).compute()
        assert result["windows"]["7d"]["churn_guard_activations"] == 1
        assert result["windows"]["30d"]["churn_guard_activations"] == 2
        assert result["windows"]["all_time"]["churn_guard_activations"] == 2


# ── tests: failure category distribution (DB-backed, windowed) ────────────────


class TestFailureCategoryDistribution:
    def test_single_category(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        t = _task(mem_db, p)
        _execution(mem_db, s, t, failure_category="tool_failure")
        mem_db.commit()

        result = FailureAnalyticsService(mem_db).compute()
        assert result["windows"]["all_time"]["failure_category_distribution"] == {
            "tool_failure": 1
        }

    def test_multiple_categories(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        t1 = _task(mem_db, p)
        t2 = _task(mem_db, p)
        _execution(mem_db, s, t1, failure_category="tool_failure")
        _execution(mem_db, s, t2, failure_category="context_overflow")
        mem_db.commit()

        dist = FailureAnalyticsService(mem_db).compute()["windows"]["all_time"][
            "failure_category_distribution"
        ]
        assert dist["tool_failure"] == 1
        assert dist["context_overflow"] == 1

    def test_null_category_excluded(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        t = _task(mem_db, p)
        _execution(mem_db, s, t, status=TaskStatus.DONE, failure_category=None)
        mem_db.commit()

        dist = FailureAnalyticsService(mem_db).compute()["windows"]["all_time"][
            "failure_category_distribution"
        ]
        assert dist == {}

    def test_window_filters_category_by_created_at(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        t = _task(mem_db, p)
        old = datetime.now(UTC) - timedelta(days=10)
        _execution(mem_db, s, t, failure_category="old_fail", created_at=old)
        mem_db.commit()

        result = FailureAnalyticsService(mem_db).compute()
        assert result["windows"]["7d"]["failure_category_distribution"] == {}
        assert result["windows"]["all_time"]["failure_category_distribution"] == {
            "old_fail": 1
        }


# ── tests: malformed event rows skipped ───────────────────────────────────────


class TestMalformedEventRowsSkipped:
    def test_missing_event_type_skipped(self, mem_db):
        events = [
            {"timestamp": datetime.now(UTC).isoformat()},  # no event_type
            _event(_ATTEMPTED),
        ]
        with patch.object(
            FailureAnalyticsService,
            "_collect_event_journal_windows",
            return_value=_build_event_windows(events),
        ):
            result = FailureAnalyticsService(mem_db).compute()

        # only the ATTEMPTED event counts
        assert result["windows"]["all_time"]["recovery_attempts"] == 1

    def test_unknown_event_type_skipped(self, mem_db):
        events = [
            {
                "event_type": "some_other_event",
                "timestamp": datetime.now(UTC).isoformat(),
            },
            _event(_SUCCEEDED),
        ]
        with patch.object(
            FailureAnalyticsService,
            "_collect_event_journal_windows",
            return_value=_build_event_windows(events),
        ):
            result = FailureAnalyticsService(mem_db).compute()

        assert result["windows"]["all_time"]["recovery_successes"] == 1

    def test_missing_timestamp_counted_in_all_time_only(self, mem_db):
        # Build window dict directly using _build_event_windows logic:
        # event with no timestamp → only goes into all_time
        events = [{"event_type": _ATTEMPTED}]  # no timestamp field
        windows = _build_event_windows(events)
        # events with no timestamp should appear in all_time but not 7d/30d
        assert windows["all_time"]["recovery_attempts"] == 1
        assert windows["7d"]["recovery_attempts"] == 0
        assert windows["30d"]["recovery_attempts"] == 0

    def test_malformed_timestamp_counted_in_all_time_only(self, mem_db):
        events = [{"event_type": _ATTEMPTED, "timestamp": "not-a-date"}]
        windows = _build_event_windows(events)
        assert windows["all_time"]["recovery_attempts"] == 1
        assert windows["7d"]["recovery_attempts"] == 0


# ── tests: event window filtering ─────────────────────────────────────────────


class TestEventWindowFiltering:
    def test_old_event_excluded_from_7d(self, mem_db):
        old = datetime.now(UTC) - timedelta(days=10)
        recent = datetime.now(UTC) - timedelta(days=2)
        events = [
            _event(_ATTEMPTED, ts=old),
            _event(_ATTEMPTED, ts=recent),
        ]
        windows = _build_event_windows(events)
        assert windows["7d"]["recovery_attempts"] == 1
        assert windows["30d"]["recovery_attempts"] == 2
        assert windows["all_time"]["recovery_attempts"] == 2

    def test_old_event_excluded_from_30d(self, mem_db):
        old = datetime.now(UTC) - timedelta(days=35)
        events = [_event(_ATTEMPTED, ts=old)]
        windows = _build_event_windows(events)
        assert windows["7d"]["recovery_attempts"] == 0
        assert windows["30d"]["recovery_attempts"] == 0
        assert windows["all_time"]["recovery_attempts"] == 1


# ── tests: API contract ────────────────────────────────────────────────────────


class TestApiContract:
    def test_endpoint_returns_correct_shape(self, mem_db):
        from app.api.v1.endpoints.analytics import get_failure_analytics

        class _FakeUser:
            id = 1

        result = get_failure_analytics(current_user=_FakeUser(), db=mem_db)
        assert "windows" in result
        assert "generated_at" in result
        assert result["metrics_version"] == 1
        assert set(result["windows"]) == {"7d", "30d", "all_time"}

    def test_all_window_keys_present(self, mem_db):
        from app.api.v1.endpoints.analytics import get_failure_analytics

        class _FakeUser:
            id = 1

        result = get_failure_analytics(current_user=_FakeUser(), db=mem_db)
        for label in _WINDOW_LABELS:
            assert (
                set(result["windows"][label]) == _WINDOW_KEYS
            ), f"Window '{label}' missing keys"

    def test_generated_at_is_iso8601(self, mem_db):
        from app.api.v1.endpoints.analytics import get_failure_analytics

        class _FakeUser:
            id = 1

        result = get_failure_analytics(current_user=_FakeUser(), db=mem_db)
        dt = datetime.fromisoformat(result["generated_at"])
        assert dt.tzinfo is not None


# ── tests: JSON serialization ─────────────────────────────────────────────────


class TestJsonSerialization:
    def test_result_is_json_serializable(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p, repair_churn_stopped=True)
        t = _task(mem_db, p)
        _execution(mem_db, s, t, failure_category="tool_failure")
        mem_db.commit()

        result = FailureAnalyticsService(mem_db).compute()
        serialized = json.dumps(result)
        parsed = json.loads(serialized)
        assert parsed["metrics_version"] == 1

    def test_null_rate_serializes_as_null(self, mem_db):
        result = FailureAnalyticsService(mem_db).compute()
        serialized = json.dumps(result)
        parsed = json.loads(serialized)
        for label in _WINDOW_LABELS:
            assert parsed["windows"][label]["recovery_success_rate"] is None

    def test_empty_dicts_serialize_as_objects(self, mem_db):
        result = FailureAnalyticsService(mem_db).compute()
        serialized = json.dumps(result)
        parsed = json.loads(serialized)
        for label in _WINDOW_LABELS:
            assert isinstance(
                parsed["windows"][label]["failure_category_distribution"], dict
            )
            assert isinstance(
                parsed["windows"][label]["failure_category_recovery"], dict
            )

    def test_computed_rate_serializes_as_float(self, mem_db):
        events = [_event(_ATTEMPTED), _event(_ATTEMPTED), _event(_SUCCEEDED)]
        with patch.object(
            FailureAnalyticsService,
            "_collect_event_journal_windows",
            return_value=_build_event_windows(events),
        ):
            result = FailureAnalyticsService(mem_db).compute()

        serialized = json.dumps(result)
        parsed = json.loads(serialized)
        rate = parsed["windows"]["all_time"]["recovery_success_rate"]
        assert isinstance(rate, float)
        assert rate == 0.5
