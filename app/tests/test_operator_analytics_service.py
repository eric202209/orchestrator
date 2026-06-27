"""Tests for OperatorAnalyticsService — Phase 15A-6.

All tests use SQLite in-memory. No filesystem or event-journal dependencies.

Covers:
- Empty database
- Intervention request counts
- Response rate and missing replies
- Response latency (mean, median, negative ignored)
- Autonomy rate and terminal session counts
- Pause / resume / stop counts
- Intervention type grouping
- Rolling window filtering
- Endpoint contract shape
- JSON serialization
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker
from sqlalchemy.pool import StaticPool

from app.models import Base, InterventionRequest, Project, Session as SessionModel
from app.services.analytics.operator_analytics_service import OperatorAnalyticsService

# ── constants ──────────────────────────────────────────────────────────────────

_WINDOW_LABELS = ("7d", "30d", "all_time")

_WINDOW_KEYS = {
    "intervention_requests",
    "intervention_responses",
    "intervention_response_rate",
    "mean_response_seconds",
    "median_response_seconds",
    "sessions_with_intervention",
    "sessions_without_intervention",
    "autonomy_rate",
    "pause_count",
    "resume_count",
    "stop_count",
    "intervention_type_distribution",
    "phase_intervention_distribution",
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
    created_at: datetime | None = None,
    paused_at: datetime | None = None,
    resumed_at: datetime | None = None,
    stopped_at: datetime | None = None,
    deleted_at: datetime | None = None,
) -> SessionModel:
    now = created_at or datetime.now(UTC)
    s = SessionModel(
        project_id=project.id,
        name=f"s-{now.isoformat()}",
        status=status,
        created_at=now,
        paused_at=paused_at,
        resumed_at=resumed_at,
        stopped_at=stopped_at,
        deleted_at=deleted_at,
    )
    db.add(s)
    db.flush()
    return s


def _intervention(
    db,
    session: SessionModel,
    *,
    intervention_type: str = "guidance",
    created_at: datetime | None = None,
    replied_at: datetime | None = None,
    operator_reply: str | None = None,
) -> InterventionRequest:
    now = created_at or datetime.now(UTC)
    iv = InterventionRequest(
        session_id=session.id,
        project_id=session.project_id,
        intervention_type=intervention_type,
        prompt="please advise",
        created_at=now,
        replied_at=replied_at,
        operator_reply=operator_reply,
    )
    db.add(iv)
    db.flush()
    return iv


# ── helpers ────────────────────────────────────────────────────────────────────


def _svc(db) -> OperatorAnalyticsService:
    return OperatorAnalyticsService(db)


def _result(db) -> dict:
    return _svc(db).compute()


def _window(db, label: str = "all_time") -> dict:
    return _result(db)["windows"][label]


# ── TestEmptyDatabase ──────────────────────────────────────────────────────────


class TestEmptyDatabase:
    def test_returns_three_windows(self, mem_db):
        r = _result(mem_db)
        assert set(r["windows"].keys()) == set(_WINDOW_LABELS)

    def test_all_windows_have_correct_keys(self, mem_db):
        for label in _WINDOW_LABELS:
            assert set(_result(mem_db)["windows"][label].keys()) == _WINDOW_KEYS

    def test_intervention_requests_zero(self, mem_db):
        assert _window(mem_db)["intervention_requests"] == 0

    def test_intervention_responses_zero(self, mem_db):
        assert _window(mem_db)["intervention_responses"] == 0

    def test_response_rate_null(self, mem_db):
        assert _window(mem_db)["intervention_response_rate"] is None

    def test_mean_response_null(self, mem_db):
        assert _window(mem_db)["mean_response_seconds"] is None

    def test_median_response_null(self, mem_db):
        assert _window(mem_db)["median_response_seconds"] is None

    def test_sessions_with_intervention_zero(self, mem_db):
        assert _window(mem_db)["sessions_with_intervention"] == 0

    def test_sessions_without_intervention_zero(self, mem_db):
        assert _window(mem_db)["sessions_without_intervention"] == 0

    def test_autonomy_rate_null(self, mem_db):
        assert _window(mem_db)["autonomy_rate"] is None

    def test_pause_count_zero(self, mem_db):
        assert _window(mem_db)["pause_count"] == 0

    def test_resume_count_zero(self, mem_db):
        assert _window(mem_db)["resume_count"] == 0

    def test_stop_count_zero(self, mem_db):
        assert _window(mem_db)["stop_count"] == 0

    def test_type_distribution_empty(self, mem_db):
        assert _window(mem_db)["intervention_type_distribution"] == {}

    def test_phase_distribution_empty(self, mem_db):
        assert _window(mem_db)["phase_intervention_distribution"] == {}


# ── TestInterventionCounts ─────────────────────────────────────────────────────


class TestInterventionCounts:
    def test_single_request_counted(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        _intervention(mem_db, s)
        assert _window(mem_db)["intervention_requests"] == 1

    def test_multiple_requests_counted(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        _intervention(mem_db, s)
        _intervention(mem_db, s, intervention_type="approval")
        assert _window(mem_db)["intervention_requests"] == 2

    def test_request_without_reply_not_response(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        _intervention(mem_db, s)
        w = _window(mem_db)
        assert w["intervention_requests"] == 1
        assert w["intervention_responses"] == 0

    def test_request_with_reply_counted_as_response(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        now = datetime.now(UTC)
        _intervention(
            mem_db,
            s,
            created_at=now,
            replied_at=now + timedelta(seconds=60),
            operator_reply="approved",
        )
        w = _window(mem_db)
        assert w["intervention_requests"] == 1
        assert w["intervention_responses"] == 1

    def test_response_rate_computed(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        now = datetime.now(UTC)
        _intervention(mem_db, s, created_at=now, replied_at=now + timedelta(seconds=30))
        _intervention(mem_db, s, intervention_type="approval")
        w = _window(mem_db)
        assert w["intervention_response_rate"] == 0.5

    def test_response_rate_null_when_no_requests(self, mem_db):
        assert _window(mem_db)["intervention_response_rate"] is None

    def test_full_response_rate_is_one(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        now = datetime.now(UTC)
        _intervention(mem_db, s, replied_at=now + timedelta(seconds=10))
        assert _window(mem_db)["intervention_response_rate"] == 1.0


# ── TestResponseLatency ────────────────────────────────────────────────────────


class TestResponseLatency:
    def test_mean_computed_correctly(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        now = datetime.now(UTC)
        _intervention(mem_db, s, created_at=now, replied_at=now + timedelta(seconds=60))
        _intervention(
            mem_db, s, created_at=now, replied_at=now + timedelta(seconds=120)
        )
        w = _window(mem_db)
        assert w["mean_response_seconds"] == 90.0

    def test_median_computed_correctly(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        now = datetime.now(UTC)
        _intervention(mem_db, s, created_at=now, replied_at=now + timedelta(seconds=10))
        _intervention(mem_db, s, created_at=now, replied_at=now + timedelta(seconds=30))
        _intervention(
            mem_db, s, created_at=now, replied_at=now + timedelta(seconds=200)
        )
        w = _window(mem_db)
        assert w["median_response_seconds"] == 30.0

    def test_negative_latency_ignored(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        now = datetime.now(UTC)
        # replied_at BEFORE created_at — negative duration
        _intervention(
            mem_db,
            s,
            created_at=now + timedelta(seconds=60),
            replied_at=now,
        )
        w = _window(mem_db)
        assert w["mean_response_seconds"] is None
        assert w["median_response_seconds"] is None

    def test_unanswered_requests_excluded_from_latency(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        now = datetime.now(UTC)
        _intervention(mem_db, s, created_at=now, replied_at=now + timedelta(seconds=40))
        _intervention(mem_db, s)  # no reply
        w = _window(mem_db)
        assert w["mean_response_seconds"] == 40.0

    def test_latency_null_when_no_replies(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        _intervention(mem_db, s)
        w = _window(mem_db)
        assert w["mean_response_seconds"] is None
        assert w["median_response_seconds"] is None

    def test_single_reply_mean_equals_median(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        now = datetime.now(UTC)
        _intervention(
            mem_db, s, created_at=now, replied_at=now + timedelta(seconds=100)
        )
        w = _window(mem_db)
        assert w["mean_response_seconds"] == 100.0
        assert w["median_response_seconds"] == 100.0


# ── TestAutonomyRate ───────────────────────────────────────────────────────────


class TestAutonomyRate:
    def test_no_terminal_sessions_autonomy_null(self, mem_db):
        assert _window(mem_db)["autonomy_rate"] is None

    def test_all_sessions_autonomous(self, mem_db):
        p = _project(mem_db)
        _session(mem_db, p, status="completed")
        _session(mem_db, p, status="stopped")
        w = _window(mem_db)
        assert w["terminal_sessions"] if "terminal_sessions" in w else True
        assert w["sessions_with_intervention"] == 0
        assert w["sessions_without_intervention"] == 2
        assert w["autonomy_rate"] == 1.0

    def test_all_sessions_have_interventions(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p, status="completed")
        now = datetime.now(UTC)
        _intervention(mem_db, s, created_at=now)
        w = _window(mem_db)
        assert w["sessions_with_intervention"] == 1
        assert w["sessions_without_intervention"] == 0
        assert w["autonomy_rate"] == 0.0

    def test_mixed_autonomy_rate(self, mem_db):
        p = _project(mem_db)
        s1 = _session(mem_db, p, status="completed")
        _session(mem_db, p, status="completed")
        now = datetime.now(UTC)
        _intervention(mem_db, s1, created_at=now)
        w = _window(mem_db)
        assert w["sessions_without_intervention"] == 1
        assert w["autonomy_rate"] == 0.5

    def test_running_sessions_not_terminal(self, mem_db):
        p = _project(mem_db)
        _session(mem_db, p, status="running")
        _session(mem_db, p, status="pending")
        w = _window(mem_db)
        assert w["autonomy_rate"] is None

    def test_deleted_sessions_excluded(self, mem_db):
        p = _project(mem_db)
        _session(mem_db, p, status="completed", deleted_at=datetime.now(UTC))
        w = _window(mem_db)
        assert w["autonomy_rate"] is None

    def test_sessions_without_intervention_not_negative(self, mem_db):
        """Intervention in window from a session created before window."""
        p = _project(mem_db)
        # Only 1 terminal session in all_time window
        s = _session(mem_db, p, status="completed")
        now = datetime.now(UTC)
        # Two interventions from this session in window
        _intervention(mem_db, s, created_at=now)
        _intervention(mem_db, s, created_at=now)
        w = _window(mem_db)
        # sessions_with_intervention=1 (same session, distinct count), terminal=1
        # without_intervention = max(0, 1-1) = 0 — not negative
        assert w["sessions_without_intervention"] == 0


# ── TestPauseResumeStop ────────────────────────────────────────────────────────


class TestPauseResumeStop:
    def test_paused_session_counted(self, mem_db):
        p = _project(mem_db)
        now = datetime.now(UTC)
        _session(mem_db, p, status="paused", paused_at=now)
        assert _window(mem_db)["pause_count"] == 1

    def test_unpaused_session_not_counted(self, mem_db):
        p = _project(mem_db)
        _session(mem_db, p, status="running")
        assert _window(mem_db)["pause_count"] == 0

    def test_resumed_session_counted(self, mem_db):
        p = _project(mem_db)
        now = datetime.now(UTC)
        _session(mem_db, p, status="running", resumed_at=now)
        assert _window(mem_db)["resume_count"] == 1

    def test_stopped_session_counted(self, mem_db):
        p = _project(mem_db)
        now = datetime.now(UTC)
        _session(mem_db, p, status="stopped", stopped_at=now)
        assert _window(mem_db)["stop_count"] == 1

    def test_multiple_stopped_sessions(self, mem_db):
        p = _project(mem_db)
        now = datetime.now(UTC)
        _session(mem_db, p, status="stopped", stopped_at=now)
        _session(mem_db, p, status="stopped", stopped_at=now)
        assert _window(mem_db)["stop_count"] == 2

    def test_deleted_sessions_excluded_from_pause(self, mem_db):
        p = _project(mem_db)
        now = datetime.now(UTC)
        _session(mem_db, p, status="paused", paused_at=now, deleted_at=now)
        assert _window(mem_db)["pause_count"] == 0

    def test_pause_resume_stop_independent(self, mem_db):
        p = _project(mem_db)
        now = datetime.now(UTC)
        _session(mem_db, p, status="completed", paused_at=now, resumed_at=now)
        _session(mem_db, p, status="stopped", stopped_at=now)
        w = _window(mem_db)
        assert w["pause_count"] == 1
        assert w["resume_count"] == 1
        assert w["stop_count"] == 1


# ── TestInterventionTypeDistribution ──────────────────────────────────────────


class TestInterventionTypeDistribution:
    def test_single_type(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        _intervention(mem_db, s, intervention_type="guidance")
        dist = _window(mem_db)["intervention_type_distribution"]
        assert dist == {"guidance": 1}

    def test_multiple_types(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        _intervention(mem_db, s, intervention_type="guidance")
        _intervention(mem_db, s, intervention_type="guidance")
        _intervention(mem_db, s, intervention_type="approval")
        dist = _window(mem_db)["intervention_type_distribution"]
        assert dist == {"guidance": 2, "approval": 1}

    def test_empty_string_type_mapped_to_unknown(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        # intervention_type column is NOT NULL, but an empty string is falsy
        # and should map to "unknown" in the distribution
        _intervention(mem_db, s, intervention_type="")
        dist = _window(mem_db)["intervention_type_distribution"]
        assert "unknown" in dist

    def test_phase_distribution_always_empty(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        _intervention(mem_db, s)
        assert _window(mem_db)["phase_intervention_distribution"] == {}


# ── TestRollingWindowFiltering ─────────────────────────────────────────────────


class TestRollingWindowFiltering:
    def test_old_intervention_excluded_from_7d(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        old = datetime.now(UTC) - timedelta(days=10)
        _intervention(mem_db, s, created_at=old)
        assert _window(mem_db, "7d")["intervention_requests"] == 0
        assert _window(mem_db, "all_time")["intervention_requests"] == 1

    def test_recent_intervention_in_7d(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        recent = datetime.now(UTC) - timedelta(days=3)
        _intervention(mem_db, s, created_at=recent)
        assert _window(mem_db, "7d")["intervention_requests"] == 1

    def test_old_intervention_excluded_from_30d(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        old = datetime.now(UTC) - timedelta(days=35)
        _intervention(mem_db, s, created_at=old)
        assert _window(mem_db, "30d")["intervention_requests"] == 0
        assert _window(mem_db, "all_time")["intervention_requests"] == 1

    def test_pause_count_filtered_by_paused_at(self, mem_db):
        p = _project(mem_db)
        old = datetime.now(UTC) - timedelta(days=10)
        recent = datetime.now(UTC) - timedelta(hours=1)
        _session(mem_db, p, status="paused", paused_at=old)
        _session(mem_db, p, status="paused", paused_at=recent)
        assert _window(mem_db, "7d")["pause_count"] == 1
        assert _window(mem_db, "all_time")["pause_count"] == 2

    def test_stop_count_filtered_by_stopped_at(self, mem_db):
        p = _project(mem_db)
        old = datetime.now(UTC) - timedelta(days=35)
        recent = datetime.now(UTC) - timedelta(hours=1)
        _session(mem_db, p, status="stopped", stopped_at=old)
        _session(mem_db, p, status="stopped", stopped_at=recent)
        assert _window(mem_db, "30d")["stop_count"] == 1
        assert _window(mem_db, "all_time")["stop_count"] == 2

    def test_terminal_sessions_filtered_by_created_at(self, mem_db):
        p = _project(mem_db)
        old = datetime.now(UTC) - timedelta(days=10)
        recent = datetime.now(UTC) - timedelta(hours=1)
        _session(mem_db, p, status="completed", created_at=old)
        _session(mem_db, p, status="completed", created_at=recent)
        w7 = _window(mem_db, "7d")
        wall = _window(mem_db, "all_time")
        assert w7["sessions_without_intervention"] == 1
        assert wall["sessions_without_intervention"] == 2

    def test_response_rate_filtered_by_intervention_created_at(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        old = datetime.now(UTC) - timedelta(days=10)
        recent = datetime.now(UTC) - timedelta(hours=1)
        _intervention(mem_db, s, created_at=old, replied_at=old + timedelta(seconds=30))
        _intervention(mem_db, s, created_at=recent)  # no reply
        w7 = _window(mem_db, "7d")
        wall = _window(mem_db, "all_time")
        assert w7["intervention_response_rate"] == 0.0  # 1 request, 0 replies
        assert wall["intervention_response_rate"] == 0.5  # 2 requests, 1 reply


# ── TestApiContract ────────────────────────────────────────────────────────────


class TestApiContract:
    def test_top_level_keys_present(self, mem_db):
        r = _result(mem_db)
        assert "windows" in r
        assert "generated_at" in r
        assert "metrics_version" in r

    def test_metrics_version_is_one(self, mem_db):
        assert _result(mem_db)["metrics_version"] == 1

    def test_generated_at_is_iso8601(self, mem_db):
        ts = _result(mem_db)["generated_at"]
        assert isinstance(ts, str)
        dt = datetime.fromisoformat(ts)
        assert dt.tzinfo is not None

    def test_all_window_keys_present_empty(self, mem_db):
        for label in _WINDOW_LABELS:
            assert set(_result(mem_db)["windows"][label].keys()) == _WINDOW_KEYS

    def test_all_window_keys_present_with_data(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        now = datetime.now(UTC)
        _intervention(mem_db, s, created_at=now, replied_at=now + timedelta(seconds=30))
        for label in _WINDOW_LABELS:
            assert set(_result(mem_db)["windows"][label].keys()) == _WINDOW_KEYS


# ── TestJsonSerialization ──────────────────────────────────────────────────────


class TestJsonSerialization:
    def test_empty_result_serializable(self, mem_db):
        r = _result(mem_db)
        assert json.dumps(r) is not None

    def test_populated_result_serializable(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        now = datetime.now(UTC)
        _intervention(mem_db, s, created_at=now, replied_at=now + timedelta(seconds=45))
        assert json.dumps(_result(mem_db)) is not None

    def test_nulls_are_json_null(self, mem_db):
        r = json.loads(json.dumps(_result(mem_db)))
        for label in _WINDOW_LABELS:
            w = r["windows"][label]
            assert w["intervention_response_rate"] is None
            assert w["mean_response_seconds"] is None

    def test_counts_are_integers(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        _intervention(mem_db, s)
        r = json.loads(json.dumps(_result(mem_db)))
        for label in _WINDOW_LABELS:
            w = r["windows"][label]
            assert isinstance(w["intervention_requests"], int)
            assert isinstance(w["intervention_responses"], int)
            assert isinstance(w["pause_count"], int)

    def test_type_distribution_is_dict(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p)
        _intervention(mem_db, s, intervention_type="approval")
        r = json.loads(json.dumps(_result(mem_db)))
        for label in _WINDOW_LABELS:
            assert isinstance(
                r["windows"][label]["intervention_type_distribution"], dict
            )

    def test_phase_distribution_is_dict(self, mem_db):
        r = json.loads(json.dumps(_result(mem_db)))
        for label in _WINDOW_LABELS:
            assert r["windows"][label]["phase_intervention_distribution"] == {}
