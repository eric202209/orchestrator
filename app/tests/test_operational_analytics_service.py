"""Tests for OperationalAnalyticsService — Phase 15A-2.

All tests are unit tests (SQLite in-memory, no HTTP, no event journal).
Covers:
- Empty database
- Single successful session
- Failed sessions
- Mixed retry attempts
- Rolling-window filtering
- Failure category aggregation
- API contract shape
- JSON serialization
"""

from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

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
from app.services.analytics.operational_analytics_service import (
    OperationalAnalyticsService,
)


# ── fixtures ──────────────────────────────────────────────────────────────────


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
    started: bool = True,
    created_at: datetime | None = None,
) -> SessionModel:
    now = created_at or datetime.now(UTC)
    s = SessionModel(
        project_id=project.id,
        name=f"session-{now.isoformat()}",
        status=status,
        started_at=now if started else None,
        created_at=now,
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
    status: TaskStatus = TaskStatus.DONE,
    failure_category: str | None = None,
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


# ── tests ─────────────────────────────────────────────────────────────────────


class TestEmptyDatabase:
    def test_counts_are_zero(self, mem_db):
        result = OperationalAnalyticsService(mem_db).compute()
        for label in ("7d", "30d", "all_time"):
            w = result["windows"][label]
            assert w["sessions_started"] == 0
            assert w["sessions_completed"] == 0
            assert w["sessions_failed"] == 0

    def test_rates_are_none_when_no_data(self, mem_db):
        result = OperationalAnalyticsService(mem_db).compute()
        for label in ("7d", "30d", "all_time"):
            w = result["windows"][label]
            assert w["session_success_rate"] is None
            assert w["first_attempt_success_rate"] is None

    def test_failure_distribution_is_empty_dict(self, mem_db):
        result = OperationalAnalyticsService(mem_db).compute()
        for label in ("7d", "30d", "all_time"):
            assert result["windows"][label]["failure_category_distribution"] == {}

    def test_top_level_shape(self, mem_db):
        result = OperationalAnalyticsService(mem_db).compute()
        assert "windows" in result
        assert "generated_at" in result
        assert result["metrics_version"] == 1
        assert set(result["windows"]) == {"7d", "30d", "all_time"}

    def test_window_keys_present(self, mem_db):
        result = OperationalAnalyticsService(mem_db).compute()
        expected_keys = {
            "session_success_rate",
            "first_attempt_success_rate",
            "failure_category_distribution",
            "sessions_started",
            "sessions_completed",
            "sessions_failed",
        }
        for label in ("7d", "30d", "all_time"):
            assert set(result["windows"][label]) == expected_keys


class TestSingleSuccessfulSession:
    def test_counts(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p, status="completed", started=True)
        t = _task(mem_db, p)
        _execution(mem_db, s, t, attempt=1, status=TaskStatus.DONE)
        mem_db.commit()

        result = OperationalAnalyticsService(mem_db).compute()
        w = result["windows"]["all_time"]
        assert w["sessions_started"] == 1
        assert w["sessions_completed"] == 1
        assert w["sessions_failed"] == 0

    def test_success_rate_is_one(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p, status="completed")
        t = _task(mem_db, p)
        _execution(mem_db, s, t, attempt=1, status=TaskStatus.DONE)
        mem_db.commit()

        w = OperationalAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["session_success_rate"] == 1.0

    def test_first_attempt_rate_is_one(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p, status="completed")
        t = _task(mem_db, p)
        _execution(mem_db, s, t, attempt=1, status=TaskStatus.DONE)
        mem_db.commit()

        w = OperationalAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["first_attempt_success_rate"] == 1.0


class TestFailedSessions:
    def test_stopped_sessions_counted_as_failed(self, mem_db):
        p = _project(mem_db)
        _session(mem_db, p, status="stopped", started=True)
        _session(mem_db, p, status="stopped", started=True)
        mem_db.commit()

        w = OperationalAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["sessions_failed"] == 2
        assert w["sessions_completed"] == 0

    def test_success_rate_zero_when_all_failed(self, mem_db):
        p = _project(mem_db)
        _session(mem_db, p, status="stopped")
        mem_db.commit()

        w = OperationalAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["session_success_rate"] == 0.0

    def test_success_rate_partial(self, mem_db):
        p = _project(mem_db)
        _session(mem_db, p, status="completed")
        _session(mem_db, p, status="stopped")
        mem_db.commit()

        w = OperationalAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["session_success_rate"] == 0.5

    def test_running_sessions_excluded_from_terminal_rate(self, mem_db):
        p = _project(mem_db)
        _session(mem_db, p, status="completed")
        _session(mem_db, p, status="running")  # not terminal — excluded
        mem_db.commit()

        w = OperationalAnalyticsService(mem_db).compute()["windows"]["all_time"]
        # 1 completed / (1 completed + 0 stopped) = 1.0
        assert w["session_success_rate"] == 1.0

    def test_not_started_session_excluded_from_started_count(self, mem_db):
        p = _project(mem_db)
        _session(mem_db, p, status="pending", started=False)
        mem_db.commit()

        w = OperationalAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["sessions_started"] == 0


class TestMixedRetryAttempts:
    def test_only_first_attempt_counted_in_rate(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p, status="completed")
        t = _task(mem_db, p)
        # first attempt fails, second succeeds — first_attempt_success_rate should be 0
        _execution(mem_db, s, t, attempt=1, status=TaskStatus.FAILED)
        _execution(mem_db, s, t, attempt=2, status=TaskStatus.DONE)
        mem_db.commit()

        w = OperationalAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["first_attempt_success_rate"] == 0.0

    def test_mixed_first_attempts_across_tasks(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p, status="completed")
        t1 = _task(mem_db, p)
        t2 = _task(mem_db, p)
        # t1: first attempt succeeds; t2: first attempt fails
        _execution(mem_db, s, t1, attempt=1, status=TaskStatus.DONE)
        _execution(mem_db, s, t2, attempt=1, status=TaskStatus.FAILED)
        mem_db.commit()

        w = OperationalAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["first_attempt_success_rate"] == 0.5

    def test_non_terminal_first_attempts_excluded_from_denominator(self, mem_db):
        """A running attempt_number=1 is not terminal and must not affect the rate."""
        p = _project(mem_db)
        s = _session(mem_db, p, status="running")
        t = _task(mem_db, p)
        # running execution — not terminal, should not count
        _execution(mem_db, s, t, attempt=1, status=TaskStatus.RUNNING)
        mem_db.commit()

        w = OperationalAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["first_attempt_success_rate"] is None

    def test_retry_attempts_ignored(self, mem_db):
        """attempt_number > 1 must never appear in the first-attempt denominator."""
        p = _project(mem_db)
        s = _session(mem_db, p, status="completed")
        t = _task(mem_db, p)
        # Only attempt_number=2 and above — first attempt rate should be None
        _execution(mem_db, s, t, attempt=2, status=TaskStatus.DONE)
        _execution(mem_db, s, t, attempt=3, status=TaskStatus.DONE)
        mem_db.commit()

        w = OperationalAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["first_attempt_success_rate"] is None


class TestRollingWindowFiltering:
    def test_old_session_excluded_from_7d_window(self, mem_db):
        p = _project(mem_db)
        old = datetime.now(UTC) - timedelta(days=10)
        _session(mem_db, p, status="completed", started=True, created_at=old)
        mem_db.commit()

        result = OperationalAnalyticsService(mem_db).compute()
        assert result["windows"]["7d"]["sessions_completed"] == 0
        assert result["windows"]["all_time"]["sessions_completed"] == 1

    def test_old_session_excluded_from_30d_window(self, mem_db):
        p = _project(mem_db)
        old = datetime.now(UTC) - timedelta(days=35)
        _session(mem_db, p, status="completed", started=True, created_at=old)
        mem_db.commit()

        result = OperationalAnalyticsService(mem_db).compute()
        assert result["windows"]["30d"]["sessions_completed"] == 0
        assert result["windows"]["all_time"]["sessions_completed"] == 1

    def test_recent_session_appears_in_7d_window(self, mem_db):
        p = _project(mem_db)
        recent = datetime.now(UTC) - timedelta(days=2)
        _session(mem_db, p, status="completed", started=True, created_at=recent)
        mem_db.commit()

        result = OperationalAnalyticsService(mem_db).compute()
        assert result["windows"]["7d"]["sessions_completed"] == 1
        assert result["windows"]["30d"]["sessions_completed"] == 1

    def test_old_executions_excluded_from_7d_failure_distribution(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p, status="stopped")
        t = _task(mem_db, p)
        old = datetime.now(UTC) - timedelta(days=10)
        _execution(
            mem_db,
            s,
            t,
            attempt=1,
            status=TaskStatus.FAILED,
            failure_category="tool_failure",
            created_at=old,
        )
        mem_db.commit()

        result = OperationalAnalyticsService(mem_db).compute()
        assert result["windows"]["7d"]["failure_category_distribution"] == {}
        assert result["windows"]["all_time"]["failure_category_distribution"] == {
            "tool_failure": 1
        }


class TestFailureCategoryAggregation:
    def test_single_category(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p, status="stopped")
        t = _task(mem_db, p)
        _execution(
            mem_db,
            s,
            t,
            attempt=1,
            status=TaskStatus.FAILED,
            failure_category="context_overflow",
        )
        mem_db.commit()

        w = OperationalAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["failure_category_distribution"] == {"context_overflow": 1}

    def test_multiple_categories_counted_separately(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p, status="stopped")
        t1 = _task(mem_db, p)
        t2 = _task(mem_db, p)
        t3 = _task(mem_db, p)
        _execution(
            mem_db,
            s,
            t1,
            attempt=1,
            status=TaskStatus.FAILED,
            failure_category="tool_failure",
        )
        _execution(
            mem_db,
            s,
            t2,
            attempt=1,
            status=TaskStatus.FAILED,
            failure_category="tool_failure",
        )
        _execution(
            mem_db,
            s,
            t3,
            attempt=1,
            status=TaskStatus.FAILED,
            failure_category="validation_failure",
        )
        mem_db.commit()

        w = OperationalAnalyticsService(mem_db).compute()["windows"]["all_time"]
        dist = w["failure_category_distribution"]
        assert dist["tool_failure"] == 2
        assert dist["validation_failure"] == 1

    def test_null_failure_category_excluded(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p, status="completed")
        t = _task(mem_db, p)
        _execution(
            mem_db, s, t, attempt=1, status=TaskStatus.DONE, failure_category=None
        )
        mem_db.commit()

        w = OperationalAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["failure_category_distribution"] == {}

    def test_retry_attempts_included_in_failure_distribution(self, mem_db):
        """failure_category_distribution counts all executions with a category,
        not just attempt_number=1, because it measures total failure volume."""
        p = _project(mem_db)
        s = _session(mem_db, p, status="stopped")
        t = _task(mem_db, p)
        _execution(
            mem_db,
            s,
            t,
            attempt=1,
            status=TaskStatus.FAILED,
            failure_category="path_contract",
        )
        _execution(
            mem_db,
            s,
            t,
            attempt=2,
            status=TaskStatus.FAILED,
            failure_category="path_contract",
        )
        mem_db.commit()

        w = OperationalAnalyticsService(mem_db).compute()["windows"]["all_time"]
        assert w["failure_category_distribution"]["path_contract"] == 2


class TestApiContract:
    def test_endpoint_returns_correct_shape(self, mem_db):
        from app.api.v1.endpoints.analytics import get_operational_analytics

        class _FakeUser:
            id = 1
            email = "admin@example.com"

        result = get_operational_analytics(current_user=_FakeUser(), db=mem_db)

        assert "windows" in result
        assert "generated_at" in result
        assert result["metrics_version"] == 1
        assert set(result["windows"]) == {"7d", "30d", "all_time"}

    def test_generated_at_is_iso8601(self, mem_db):
        from app.api.v1.endpoints.analytics import get_operational_analytics

        class _FakeUser:
            id = 1

        result = get_operational_analytics(current_user=_FakeUser(), db=mem_db)
        # Must parse without raising
        dt = datetime.fromisoformat(result["generated_at"])
        assert dt.tzinfo is not None

    def test_all_window_fields_present(self, mem_db):
        from app.api.v1.endpoints.analytics import get_operational_analytics

        class _FakeUser:
            id = 1

        result = get_operational_analytics(current_user=_FakeUser(), db=mem_db)
        required = {
            "session_success_rate",
            "first_attempt_success_rate",
            "failure_category_distribution",
            "sessions_started",
            "sessions_completed",
            "sessions_failed",
        }
        for label in ("7d", "30d", "all_time"):
            assert (
                set(result["windows"][label]) == required
            ), f"Window '{label}' missing keys"


class TestJsonSerialization:
    def test_result_is_json_serializable(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p, status="completed")
        t = _task(mem_db, p)
        _execution(mem_db, s, t, attempt=1, status=TaskStatus.DONE)
        mem_db.commit()

        result = OperationalAnalyticsService(mem_db).compute()
        # Must not raise
        serialized = json.dumps(result)
        parsed = json.loads(serialized)
        assert parsed["metrics_version"] == 1

    def test_none_rates_serialize_as_null(self, mem_db):
        result = OperationalAnalyticsService(mem_db).compute()
        serialized = json.dumps(result)
        parsed = json.loads(serialized)
        for label in ("7d", "30d", "all_time"):
            assert parsed["windows"][label]["session_success_rate"] is None
            assert parsed["windows"][label]["first_attempt_success_rate"] is None

    def test_failure_distribution_serializes_as_object(self, mem_db):
        p = _project(mem_db)
        s = _session(mem_db, p, status="stopped")
        t = _task(mem_db, p)
        _execution(
            mem_db,
            s,
            t,
            attempt=1,
            status=TaskStatus.FAILED,
            failure_category="unknown",
        )
        mem_db.commit()

        result = OperationalAnalyticsService(mem_db).compute()
        serialized = json.dumps(result)
        parsed = json.loads(serialized)
        dist = parsed["windows"]["all_time"]["failure_category_distribution"]
        assert isinstance(dist, dict)
        assert dist["unknown"] == 1
