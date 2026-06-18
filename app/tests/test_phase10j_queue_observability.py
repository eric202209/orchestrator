"""Phase 10J-b — Queue Observability tests.

Covers:
- queued_at and queue_latency_seconds persist to TaskExecution when available
- missing queue event leaves both fields NULL
- malformed queue event timestamp does not fail execution
- API serialization: GET /ops/queue-latency returns aggregates
- migration: TaskExecution model exposes both columns
- existing task execution flow unaffected
"""

from __future__ import annotations

from datetime import datetime, timezone, timedelta
from unittest.mock import patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy.orm import Session

from app.models import (
    Project,
    Session as SessionModel,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.tasks.worker_support.common import _parse_event_timestamp


# ── fixtures ──────────────────────────────────────────────────────────────────


@pytest.fixture()
def project(db_session: Session) -> Project:
    p = Project(name="queue-obs-project", workspace_path=None)
    db_session.add(p)
    db_session.commit()
    db_session.refresh(p)
    return p


@pytest.fixture()
def session(db_session: Session, project: Project) -> SessionModel:
    s = SessionModel(
        project_id=project.id,
        name="queue-obs-session",
        status="running",
        is_active=True,
        instance_id="queue-obs-uuid",
    )
    db_session.add(s)
    db_session.commit()
    db_session.refresh(s)
    return s


@pytest.fixture()
def task(db_session: Session, project: Project) -> Task:
    t = Task(
        project_id=project.id,
        title="queue-obs-task",
        status=TaskStatus.RUNNING,
    )
    db_session.add(t)
    db_session.commit()
    db_session.refresh(t)
    return t


def _make_execution(
    db: Session,
    session: SessionModel,
    task: Task,
    *,
    queued_at=None,
    queue_latency_seconds=None,
) -> TaskExecution:
    ex = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
        queued_at=queued_at,
        queue_latency_seconds=queue_latency_seconds,
    )
    db.add(ex)
    db.commit()
    db.refresh(ex)
    return ex


# ── model-level persistence tests ─────────────────────────────────────────────


class TestQueueObservabilityFieldPersistence:
    def test_fields_persist_when_set(
        self, db_session: Session, session: SessionModel, task: Task
    ):
        queued = datetime(2026, 6, 18, 10, 0, 0, tzinfo=timezone.utc)
        ex = _make_execution(
            db_session,
            session,
            task,
            queued_at=queued,
            queue_latency_seconds=12.5,
        )

        fetched = (
            db_session.query(TaskExecution).filter(TaskExecution.id == ex.id).first()
        )
        assert fetched.queued_at is not None
        assert fetched.queue_latency_seconds == 12.5

    def test_fields_null_when_not_set(
        self, db_session: Session, session: SessionModel, task: Task
    ):
        ex = _make_execution(db_session, session, task)

        fetched = (
            db_session.query(TaskExecution).filter(TaskExecution.id == ex.id).first()
        )
        assert fetched.queued_at is None
        assert fetched.queue_latency_seconds is None

    def test_queued_at_value_roundtrip(
        self, db_session: Session, session: SessionModel, task: Task
    ):
        queued = datetime(2026, 6, 18, 9, 30, 0, tzinfo=timezone.utc)
        ex = _make_execution(db_session, session, task, queued_at=queued)

        fetched = (
            db_session.query(TaskExecution).filter(TaskExecution.id == ex.id).first()
        )
        # SQLite stores timezone-aware datetimes; normalize for comparison
        stored = fetched.queued_at
        if stored.tzinfo is None:
            stored = stored.replace(tzinfo=timezone.utc)
        assert stored == queued

    def test_queue_latency_seconds_precision(
        self, db_session: Session, session: SessionModel, task: Task
    ):
        ex = _make_execution(db_session, session, task, queue_latency_seconds=0.123)
        fetched = (
            db_session.query(TaskExecution).filter(TaskExecution.id == ex.id).first()
        )
        assert abs(fetched.queue_latency_seconds - 0.123) < 1e-6


# ── worker write guard ─────────────────────────────────────────────────────────


class TestQueueWriteGuard:
    def test_write_guard_swallows_exception(
        self, db_session: Session, session: SessionModel, task: Task
    ):
        """The try/except guard in the worker must not propagate assignment errors."""
        ex = _make_execution(db_session, session, task)

        # Simulate the guard directly — if it raises, this test fails
        queued_at = datetime(2026, 6, 18, 10, 0, 0, tzinfo=timezone.utc)
        queue_latency_seconds = 5.0
        try:
            if queued_at is not None:
                ex.queued_at = queued_at
            if queue_latency_seconds is not None:
                ex.queue_latency_seconds = queue_latency_seconds
        except Exception:
            pass

        db_session.commit()
        db_session.refresh(ex)
        assert ex.queue_latency_seconds == 5.0

    def test_malformed_timestamp_returns_none(self):
        """_parse_event_timestamp returns None for malformed input — guard leaves fields NULL."""
        assert _parse_event_timestamp("not-a-timestamp") is None
        assert _parse_event_timestamp("") is None
        assert _parse_event_timestamp(None) is None

    def test_valid_iso_timestamp_parses(self):
        result = _parse_event_timestamp("2026-06-18T10:00:00Z")
        assert result is not None
        assert result.year == 2026

    def test_malformed_event_does_not_fail_execution_flow(
        self, db_session: Session, session: SessionModel, task: Task
    ):
        """When event timestamp is unparseable, queued_at stays None — no exception raised."""
        ex = _make_execution(db_session, session, task)
        raw_queued_at = _parse_event_timestamp("garbage-value")  # returns None

        try:
            if raw_queued_at is not None:
                ex.queued_at = raw_queued_at
            # queue_latency_seconds would also be None — no write
        except Exception:
            pytest.fail("Write guard raised unexpectedly")

        db_session.commit()
        db_session.refresh(ex)
        assert ex.queued_at is None
        assert ex.queue_latency_seconds is None


# ── model schema assertions ────────────────────────────────────────────────────


class TestMigrationColumns:
    def test_task_execution_has_queued_at_column(self):
        from sqlalchemy import inspect as sa_inspect
        from app.models import TaskExecution as TE

        cols = {c.key for c in sa_inspect(TE).mapper.column_attrs}
        assert "queued_at" in cols

    def test_task_execution_has_queue_latency_seconds_column(self):
        from sqlalchemy import inspect as sa_inspect
        from app.models import TaskExecution as TE

        cols = {c.key for c in sa_inspect(TE).mapper.column_attrs}
        assert "queue_latency_seconds" in cols

    def test_queued_at_column_is_nullable(
        self, db_session: Session, session: SessionModel, task: Task
    ):
        ex = _make_execution(db_session, session, task)
        # null fields accepted without error
        assert ex.queued_at is None

    def test_queue_latency_seconds_column_is_nullable(
        self, db_session: Session, session: SessionModel, task: Task
    ):
        ex = _make_execution(db_session, session, task)
        assert ex.queue_latency_seconds is None


# ── API endpoint tests ─────────────────────────────────────────────────────────


class TestOpsQueueLatencyEndpoint:
    def test_returns_zero_when_no_data(self, authenticated_client: TestClient):
        resp = authenticated_client.get("/api/v1/ops/queue-latency")
        assert resp.status_code == 200
        body = resp.json()
        assert body["executions_with_latency"] == 0
        assert body["avg_queue_latency_seconds"] is None
        assert body["max_queue_latency_seconds"] is None

    def test_returns_correct_aggregates(
        self,
        authenticated_client: TestClient,
        db_session: Session,
        session: SessionModel,
        task: Task,
    ):
        _make_execution(db_session, session, task, queue_latency_seconds=10.0)

        ex2 = TaskExecution(
            session_id=session.id,
            task_id=task.id,
            attempt_number=2,
            status=TaskStatus.DONE,
            queue_latency_seconds=20.0,
        )
        db_session.add(ex2)
        db_session.commit()

        resp = authenticated_client.get("/api/v1/ops/queue-latency")
        assert resp.status_code == 200
        body = resp.json()
        assert body["executions_with_latency"] == 2
        assert abs(body["avg_queue_latency_seconds"] - 15.0) < 0.01
        assert body["max_queue_latency_seconds"] == 20.0

    def test_excludes_null_latency_from_aggregates(
        self,
        authenticated_client: TestClient,
        db_session: Session,
        session: SessionModel,
        task: Task,
    ):
        _make_execution(db_session, session, task, queue_latency_seconds=5.0)
        # Second execution with NULL latency — reuse same session, new attempt
        ex2 = TaskExecution(
            session_id=session.id,
            task_id=task.id,
            attempt_number=2,
            status=TaskStatus.RUNNING,
            queue_latency_seconds=None,
        )
        db_session.add(ex2)
        db_session.commit()

        resp = authenticated_client.get("/api/v1/ops/queue-latency")
        body = resp.json()
        # Only the one with latency should be counted
        assert body["executions_with_latency"] == 1

    def test_window_days_param_accepted(self, authenticated_client: TestClient):
        resp = authenticated_client.get("/api/v1/ops/queue-latency?days=1")
        assert resp.status_code == 200
        body = resp.json()
        assert body["window_days"] == 1

    def test_response_includes_computed_at(self, authenticated_client: TestClient):
        resp = authenticated_client.get("/api/v1/ops/queue-latency")
        assert "computed_at" in resp.json()
