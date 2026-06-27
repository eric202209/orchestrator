"""Tests for ExecutionAnalyticsService — Phase 15A-5.

All tests are unit tests (SQLite in-memory).
Phase-duration tests exercise tally_phase_events() directly and mock
_collect_phase_duration_windows where integration is not needed.

Covers:
- Empty database
- Execution count
- Mean execution duration / null timestamps / negative durations
- Queue latency p50 / p95
- Token totals with nulls
- Backend distribution / unknown fallback
- Phase duration from matched events
- Unmatched phase events ignored
- Malformed event rows skipped
- Rolling window filtering
- API contract
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
from app.services.analytics.execution_analytics_service import (
    ExecutionAnalyticsService,
    tally_phase_events,
    _percentile,
)
from app.services.orchestration.events.event_types import EventType

_WINDOW_LABELS = ("7d", "30d", "all_time")

_WINDOW_KEYS = {
    "execution_count",
    "mean_execution_duration_seconds",
    "queue_latency_p50_seconds",
    "queue_latency_p95_seconds",
    "tokens_in_total",
    "tokens_out_total",
    "backend_distribution",
    "phase_duration_seconds",
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
    status: TaskStatus = TaskStatus.DONE,
    backend_id: str | None = "local",
    queue_latency_seconds: float | None = None,
    tokens_in: int | None = None,
    tokens_out: int | None = None,
    started_at: datetime | None = None,
    completed_at: datetime | None = None,
    attempt: int = 1,
    created_at: datetime | None = None,
) -> TaskExecution:
    now = created_at or datetime.now(UTC)
    ex = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=attempt,
        status=status,
        backend_id=backend_id,
        queue_latency_seconds=queue_latency_seconds,
        tokens_in=tokens_in,
        tokens_out=tokens_out,
        started_at=started_at,
        completed_at=completed_at,
        created_at=now,
    )
    db.add(ex)
    db.flush()
    return ex


def _phase_event(event_type: str, phase: str, ts: datetime | None = None) -> dict:
    return {
        "event_type": event_type,
        "phase": phase,
        "timestamp": (ts or datetime.now(UTC)).isoformat(),
    }


def _empty_phase_windows():
    return {label: {} for label in _WINDOW_LABELS}


# ── tests: empty database ──────────────────────────────────────────────────────


class TestEmptyDatabase:
    def test_counts_and_totals_zero(self, mem_db):
        result = ExecutionAnalyticsService(mem_db).compute()
        for label in _WINDOW_LABELS:
            w = result["windows"][label]
            assert w["execution_count"] == 0
            assert w["tokens_in_total"] == 0
            assert w["tokens_out_total"] == 0

    def test_nullable_metrics_are_null(self, mem_db):
        result = ExecutionAnalyticsService(mem_db).compute()
        for label in _WINDOW_LABELS:
            w = result["windows"][label]
            assert w["mean_execution_duration_seconds"] is None
            assert w["queue_latency_p50_seconds"] is None
            assert w["queue_latency_p95_seconds"] is None

    def test_collections_empty(self, mem_db):
        result = ExecutionAnalyticsService(mem_db).compute()
        for label in _WINDOW_LABELS:
            w = result["windows"][label]
            assert w["backend_distribution"] == {}
            assert w["phase_duration_seconds"] == {}

    def test_top_level_shape(self, mem_db):
        result = ExecutionAnalyticsService(mem_db).compute()
        assert "windows" in result
        assert "generated_at" in result
        assert result["metrics_version"] == 1
        assert set(result["windows"]) == {"7d", "30d", "all_time"}

    def test_window_keys_present(self, mem_db):
        result = ExecutionAnalyticsService(mem_db).compute()
        for label in _WINDOW_LABELS:
            assert set(result["windows"][label]) == _WINDOW_KEYS


# ── tests: execution count ────────────────────────────────────────────────────


class TestExecutionCount:
    def test_single_execution(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        t = _task(mem_db, p)
        _execution(mem_db, s, t)
        mem_db.commit()

        w = ExecutionAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["execution_count"] == 1

    def test_counts_all_executions(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        t = _task(mem_db, p)
        for i in range(4):
            _execution(mem_db, s, t, attempt=i + 1)
        mem_db.commit()

        w = ExecutionAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["execution_count"] == 4


# ── tests: mean execution duration ────────────────────────────────────────────


class TestMeanExecutionDuration:
    def test_null_when_no_timestamps(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        t = _task(mem_db, p)
        _execution(mem_db, s, t, started_at=None, completed_at=None)
        mem_db.commit()

        w = ExecutionAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["mean_execution_duration_seconds"] is None

    def test_null_when_only_started_at(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        t = _task(mem_db, p)
        now = datetime.now(UTC)
        _execution(mem_db, s, t, started_at=now, completed_at=None)
        mem_db.commit()

        w = ExecutionAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["mean_execution_duration_seconds"] is None

    def test_single_duration(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        t = _task(mem_db, p)
        start = datetime.now(UTC)
        end = start + timedelta(seconds=10)
        _execution(mem_db, s, t, started_at=start, completed_at=end)
        mem_db.commit()

        w = ExecutionAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["mean_execution_duration_seconds"] == 10.0

    def test_negative_duration_ignored(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        t = _task(mem_db, p)
        now = datetime.now(UTC)
        # completed_at < started_at — negative duration
        _execution(
            mem_db,
            s,
            t,
            started_at=now,
            completed_at=now - timedelta(seconds=5),
            attempt=1,
        )
        # valid row
        _execution(
            mem_db,
            s,
            t,
            started_at=now,
            completed_at=now + timedelta(seconds=20),
            attempt=2,
        )
        mem_db.commit()

        w = ExecutionAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["mean_execution_duration_seconds"] == 20.0

    def test_mean_across_multiple(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        t = _task(mem_db, p)
        now = datetime.now(UTC)
        _execution(
            mem_db,
            s,
            t,
            attempt=1,
            started_at=now,
            completed_at=now + timedelta(seconds=10),
        )
        _execution(
            mem_db,
            s,
            t,
            attempt=2,
            started_at=now,
            completed_at=now + timedelta(seconds=30),
        )
        mem_db.commit()

        w = ExecutionAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["mean_execution_duration_seconds"] == 20.0


# ── tests: queue latency percentiles ─────────────────────────────────────────


class TestQueueLatencyPercentiles:
    def test_null_when_no_latency_data(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        t = _task(mem_db, p)
        _execution(mem_db, s, t, queue_latency_seconds=None)
        mem_db.commit()

        w = ExecutionAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["queue_latency_p50_seconds"] is None
        assert w["queue_latency_p95_seconds"] is None

    def test_single_value(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        t = _task(mem_db, p)
        _execution(mem_db, s, t, queue_latency_seconds=5.0)
        mem_db.commit()

        w = ExecutionAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["queue_latency_p50_seconds"] == 5.0
        assert w["queue_latency_p95_seconds"] == 5.0

    def test_p50_median(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        t = _task(mem_db, p)
        for i, v in enumerate([1.0, 2.0, 3.0, 4.0, 5.0], start=1):
            _execution(mem_db, s, t, attempt=i, queue_latency_seconds=v)
        mem_db.commit()

        w = ExecutionAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["queue_latency_p50_seconds"] == 3.0

    def test_p95_high_end(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        t = _task(mem_db, p)
        # 20 values: 1..20
        for i in range(1, 21):
            _execution(mem_db, s, t, attempt=i, queue_latency_seconds=float(i))
        mem_db.commit()

        w = ExecutionAnalyticsService(mem_db).compute()["windows"]["all_time"]
        # p95 of [1..20] with linear interpolation: idx=19*0.95=18.05, lo=18, hi=19
        # s[18]=19, s[19]=20; 19 + 0.05*(20-19)=19.05
        assert w["queue_latency_p95_seconds"] == 19.05


class TestPercentileHelper:
    def test_empty_returns_none(self):
        assert _percentile([], 50) is None

    def test_single_element(self):
        assert _percentile([7.0], 50) == 7.0
        assert _percentile([7.0], 95) == 7.0

    def test_two_elements_median(self):
        assert _percentile([1.0, 3.0], 50) == 2.0

    def test_p100_is_max(self):
        assert _percentile([1.0, 2.0, 3.0], 100) == 3.0


# ── tests: token totals ───────────────────────────────────────────────────────


class TestTokenTotals:
    def test_zeros_when_no_executions(self, mem_db):
        w = ExecutionAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["tokens_in_total"] == 0
        assert w["tokens_out_total"] == 0

    def test_null_tokens_treated_as_zero(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        t = _task(mem_db, p)
        _execution(mem_db, s, t, tokens_in=None, tokens_out=None)
        mem_db.commit()

        w = ExecutionAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["tokens_in_total"] == 0
        assert w["tokens_out_total"] == 0

    def test_sums_tokens(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        t = _task(mem_db, p)
        _execution(mem_db, s, t, attempt=1, tokens_in=100, tokens_out=200)
        _execution(mem_db, s, t, attempt=2, tokens_in=50, tokens_out=75)
        mem_db.commit()

        w = ExecutionAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["tokens_in_total"] == 150
        assert w["tokens_out_total"] == 275

    def test_mixed_null_and_value(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        t = _task(mem_db, p)
        _execution(mem_db, s, t, attempt=1, tokens_in=100, tokens_out=None)
        _execution(mem_db, s, t, attempt=2, tokens_in=None, tokens_out=50)
        mem_db.commit()

        w = ExecutionAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["tokens_in_total"] == 100
        assert w["tokens_out_total"] == 50


# ── tests: backend distribution ───────────────────────────────────────────────


class TestBackendDistribution:
    def test_empty_when_no_executions(self, mem_db):
        w = ExecutionAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["backend_distribution"] == {}

    def test_known_backend(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        t = _task(mem_db, p)
        _execution(mem_db, s, t, attempt=1, backend_id="local")
        _execution(mem_db, s, t, attempt=2, backend_id="local")
        mem_db.commit()

        w = ExecutionAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["backend_distribution"]["local"] == 2

    def test_null_backend_becomes_unknown(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        t = _task(mem_db, p)
        _execution(mem_db, s, t, backend_id=None)
        mem_db.commit()

        w = ExecutionAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["backend_distribution"].get("unknown") == 1
        assert "None" not in w["backend_distribution"]

    def test_empty_string_backend_becomes_unknown(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        t = _task(mem_db, p)
        _execution(mem_db, s, t, backend_id="")
        mem_db.commit()

        w = ExecutionAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["backend_distribution"].get("unknown") == 1

    def test_multiple_backends(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        t = _task(mem_db, p)
        _execution(mem_db, s, t, attempt=1, backend_id="local")
        _execution(mem_db, s, t, attempt=2, backend_id="remote")
        _execution(mem_db, s, t, attempt=3, backend_id="local")
        mem_db.commit()

        dist = ExecutionAnalyticsService(mem_db).compute()["windows"]["all_time"][
            "backend_distribution"
        ]
        assert dist["local"] == 2
        assert dist["remote"] == 1


# ── tests: phase duration from events (tally_phase_events unit tests) ─────────


class TestTallyPhaseEvents:
    def test_empty_events(self):
        assert tally_phase_events([]) == []

    def test_single_matched_pair(self):
        now = datetime.now(UTC)
        events = [
            _phase_event(EventType.PHASE_STARTED, "planning", now),
            _phase_event(
                EventType.PHASE_FINISHED, "planning", now + timedelta(seconds=10)
            ),
        ]
        results = tally_phase_events(events)
        assert len(results) == 1
        phase, duration, finished_at = results[0]
        assert phase == "planning"
        assert duration == 10.0

    def test_unmatched_start_ignored(self):
        now = datetime.now(UTC)
        events = [_phase_event(EventType.PHASE_STARTED, "executing", now)]
        assert tally_phase_events(events) == []

    def test_unmatched_finish_ignored(self):
        now = datetime.now(UTC)
        events = [_phase_event(EventType.PHASE_FINISHED, "executing", now)]
        assert tally_phase_events(events) == []

    def test_negative_duration_discarded(self):
        now = datetime.now(UTC)
        events = [
            _phase_event(
                EventType.PHASE_STARTED, "planning", now + timedelta(seconds=10)
            ),
            _phase_event(EventType.PHASE_FINISHED, "planning", now),  # before start
        ]
        assert tally_phase_events(events) == []

    def test_multiple_phases(self):
        now = datetime.now(UTC)
        events = [
            _phase_event(EventType.PHASE_STARTED, "planning", now),
            _phase_event(
                EventType.PHASE_FINISHED, "planning", now + timedelta(seconds=5)
            ),
            _phase_event(
                EventType.PHASE_STARTED, "executing", now + timedelta(seconds=6)
            ),
            _phase_event(
                EventType.PHASE_FINISHED, "executing", now + timedelta(seconds=56)
            ),
        ]
        results = tally_phase_events(events)
        assert len(results) == 2
        by_phase = {r[0]: r[1] for r in results}
        assert by_phase["planning"] == 5.0
        assert by_phase["executing"] == 50.0

    def test_repeated_phase_fifo_matching(self):
        now = datetime.now(UTC)
        events = [
            _phase_event(EventType.PHASE_STARTED, "planning", now),
            _phase_event(
                EventType.PHASE_FINISHED, "planning", now + timedelta(seconds=10)
            ),
            _phase_event(
                EventType.PHASE_STARTED, "planning", now + timedelta(seconds=20)
            ),
            _phase_event(
                EventType.PHASE_FINISHED, "planning", now + timedelta(seconds=35)
            ),
        ]
        results = tally_phase_events(events)
        assert len(results) == 2
        durations = sorted(r[1] for r in results)
        assert durations == [10.0, 15.0]

    def test_missing_phase_name_skipped(self):
        now = datetime.now(UTC)
        events = [
            {"event_type": EventType.PHASE_STARTED, "timestamp": now.isoformat()},
            {
                "event_type": EventType.PHASE_FINISHED,
                "timestamp": (now + timedelta(seconds=5)).isoformat(),
            },
        ]
        assert tally_phase_events(events) == []

    def test_malformed_timestamp_skipped(self):
        events = [
            {
                "event_type": EventType.PHASE_STARTED,
                "phase": "planning",
                "timestamp": "not-a-date",
            },
            {
                "event_type": EventType.PHASE_FINISHED,
                "phase": "planning",
                "timestamp": "also-bad",
            },
        ]
        assert tally_phase_events(events) == []

    def test_non_phase_events_ignored(self):
        now = datetime.now(UTC)
        events = [
            {
                "event_type": "tool_invoked",
                "phase": "executing",
                "timestamp": now.isoformat(),
            },
            _phase_event(EventType.PHASE_STARTED, "executing", now),
            {
                "event_type": "step_finished",
                "phase": "executing",
                "timestamp": now.isoformat(),
            },
            _phase_event(
                EventType.PHASE_FINISHED, "executing", now + timedelta(seconds=30)
            ),
        ]
        results = tally_phase_events(events)
        assert len(results) == 1
        assert results[0][0] == "executing"
        assert results[0][1] == 30.0


# ── tests: phase_duration_seconds in compute() ────────────────────────────────


class TestPhaseDurationInCompute:
    def test_empty_when_no_events(self, mem_db):
        with patch.object(
            ExecutionAnalyticsService,
            "_collect_phase_duration_windows",
            return_value=_empty_phase_windows(),
        ):
            result = ExecutionAnalyticsService(mem_db).compute()

        for label in _WINDOW_LABELS:
            assert result["windows"][label]["phase_duration_seconds"] == {}

    def test_phase_durations_aggregated(self, mem_db):
        phase_windows = {
            "7d": {"planning": {"count": 2, "mean_seconds": 8.0}},
            "30d": {"planning": {"count": 3, "mean_seconds": 9.0}},
            "all_time": {"planning": {"count": 5, "mean_seconds": 10.0}},
        }
        with patch.object(
            ExecutionAnalyticsService,
            "_collect_phase_duration_windows",
            return_value=phase_windows,
        ):
            result = ExecutionAnalyticsService(mem_db).compute()

        w = result["windows"]["all_time"]
        assert w["phase_duration_seconds"]["planning"]["count"] == 5
        assert w["phase_duration_seconds"]["planning"]["mean_seconds"] == 10.0

    def test_multiple_phases(self, mem_db):
        phase_windows = {
            label: {
                "planning": {"count": 1, "mean_seconds": 5.0},
                "executing": {"count": 1, "mean_seconds": 60.0},
            }
            for label in _WINDOW_LABELS
        }
        with patch.object(
            ExecutionAnalyticsService,
            "_collect_phase_duration_windows",
            return_value=phase_windows,
        ):
            result = ExecutionAnalyticsService(mem_db).compute()

        w = result["windows"]["all_time"]["phase_duration_seconds"]
        assert "planning" in w
        assert "executing" in w


# ── tests: rolling window filtering ───────────────────────────────────────────


class TestRollingWindowFiltering:
    def test_old_execution_excluded_from_7d(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        t = _task(mem_db, p)
        old = datetime.now(UTC) - timedelta(days=10)
        _execution(mem_db, s, t, created_at=old)
        mem_db.commit()

        result = ExecutionAnalyticsService(mem_db).compute()
        assert result["windows"]["7d"]["execution_count"] == 0
        assert result["windows"]["all_time"]["execution_count"] == 1

    def test_old_execution_excluded_from_30d(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        t = _task(mem_db, p)
        old = datetime.now(UTC) - timedelta(days=35)
        _execution(mem_db, s, t, created_at=old)
        mem_db.commit()

        result = ExecutionAnalyticsService(mem_db).compute()
        assert result["windows"]["30d"]["execution_count"] == 0
        assert result["windows"]["all_time"]["execution_count"] == 1

    def test_recent_execution_in_7d(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        t = _task(mem_db, p)
        recent = datetime.now(UTC) - timedelta(days=2)
        _execution(mem_db, s, t, created_at=recent, tokens_in=100)
        mem_db.commit()

        result = ExecutionAnalyticsService(mem_db).compute()
        assert result["windows"]["7d"]["execution_count"] == 1
        assert result["windows"]["7d"]["tokens_in_total"] == 100

    def test_window_filters_backend_distribution(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        t = _task(mem_db, p)
        old = datetime.now(UTC) - timedelta(days=10)
        _execution(mem_db, s, t, attempt=1, backend_id="old-backend", created_at=old)
        _execution(mem_db, s, t, attempt=2, backend_id="new-backend")
        mem_db.commit()

        result = ExecutionAnalyticsService(mem_db).compute()
        assert "old-backend" not in result["windows"]["7d"]["backend_distribution"]
        assert "old-backend" in result["windows"]["all_time"]["backend_distribution"]


# ── tests: API contract ────────────────────────────────────────────────────────


class TestApiContract:
    def test_endpoint_returns_correct_shape(self, mem_db):
        from app.api.v1.endpoints.analytics import get_execution_analytics

        class _FakeUser:
            id = 1

        result = get_execution_analytics(current_user=_FakeUser(), db=mem_db)
        assert "windows" in result
        assert "generated_at" in result
        assert result["metrics_version"] == 1
        assert set(result["windows"]) == {"7d", "30d", "all_time"}

    def test_all_window_keys_present(self, mem_db):
        from app.api.v1.endpoints.analytics import get_execution_analytics

        class _FakeUser:
            id = 1

        result = get_execution_analytics(current_user=_FakeUser(), db=mem_db)
        for label in _WINDOW_LABELS:
            assert set(result["windows"][label]) == _WINDOW_KEYS

    def test_generated_at_is_iso8601(self, mem_db):
        from app.api.v1.endpoints.analytics import get_execution_analytics

        class _FakeUser:
            id = 1

        result = get_execution_analytics(current_user=_FakeUser(), db=mem_db)
        dt = datetime.fromisoformat(result["generated_at"])
        assert dt.tzinfo is not None


# ── tests: JSON serialization ─────────────────────────────────────────────────


class TestJsonSerialization:
    def test_empty_result_serializable(self, mem_db):
        result = ExecutionAnalyticsService(mem_db).compute()
        parsed = json.loads(json.dumps(result))
        assert parsed["metrics_version"] == 1

    def test_null_metrics_serialize_as_null(self, mem_db):
        result = ExecutionAnalyticsService(mem_db).compute()
        parsed = json.loads(json.dumps(result))
        for label in _WINDOW_LABELS:
            w = parsed["windows"][label]
            assert w["mean_execution_duration_seconds"] is None
            assert w["queue_latency_p50_seconds"] is None
            assert w["queue_latency_p95_seconds"] is None

    def test_counts_serialize_as_int(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        t = _task(mem_db, p)
        _execution(mem_db, s, t, tokens_in=50, tokens_out=100)
        mem_db.commit()

        result = ExecutionAnalyticsService(mem_db).compute()
        parsed = json.loads(json.dumps(result))
        w = parsed["windows"]["all_time"]
        assert isinstance(w["execution_count"], int)
        assert isinstance(w["tokens_in_total"], int)

    def test_phase_durations_serialize_as_object(self, mem_db):
        phase_windows = {
            label: {"planning": {"count": 1, "mean_seconds": 12.5}}
            for label in _WINDOW_LABELS
        }
        with patch.object(
            ExecutionAnalyticsService,
            "_collect_phase_duration_windows",
            return_value=phase_windows,
        ):
            result = ExecutionAnalyticsService(mem_db).compute()

        parsed = json.loads(json.dumps(result))
        pd = parsed["windows"]["all_time"]["phase_duration_seconds"]
        assert isinstance(pd, dict)
        assert pd["planning"]["count"] == 1
        assert pd["planning"]["mean_seconds"] == 12.5
