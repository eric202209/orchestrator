"""Regression tests for DB session cleanup in maintenance Celery tasks.

Phase 18L-R observed live pool exhaustion under concurrent load. Part of the
root cause: process_github_webhook, scheduled_task_execution, and
cleanup_old_logs only called db.close() on their success path, so any
exception raised while the session was open (including the retry exception
itself) leaked the connection back to nothing -- it was never returned to
the pool. See docs/roadmap/done/phase19/phase19a-db-pool-sqlite-concurrency-hardening-report.md.
"""

from __future__ import annotations

from unittest.mock import MagicMock

from app.tasks import maintenance


def test_cleanup_old_logs_closes_session_on_query_failure(monkeypatch):
    fake_db = MagicMock()
    fake_db.query.side_effect = RuntimeError("boom")
    monkeypatch.setattr(maintenance, "get_db_session", lambda: fake_db)

    try:
        maintenance.cleanup_old_logs(days=1)
    except Exception:
        pass

    assert fake_db.close.called


def test_process_github_webhook_closes_session_on_failure(monkeypatch):
    fake_db = MagicMock()
    fake_db.query.side_effect = RuntimeError("boom")
    monkeypatch.setattr(maintenance, "get_db_session", lambda: fake_db)

    try:
        maintenance.process_github_webhook(
            webhook_data={"type": "PushEvent"},
            repo_owner="acme",
            repo_name="widgets",
        )
    except Exception:
        pass

    assert fake_db.close.called


def test_scheduled_task_execution_closes_session_on_failure(monkeypatch):
    fake_db = MagicMock()
    fake_db.query.side_effect = RuntimeError("boom")
    monkeypatch.setattr(maintenance, "get_db_session", lambda: fake_db)

    try:
        maintenance.scheduled_task_execution(
            task_id=1,
            scheduled_time="2020-01-01T00:00:00",
            prompt="do it",
        )
    except Exception:
        pass

    assert fake_db.close.called


def test_scheduled_task_execution_future_schedule_does_not_open_session(monkeypatch):
    """A schedule in the future retries without ever touching the DB, so
    get_db_session should not be called at all -- confirms the fix didn't
    change this early-exit path's behavior."""

    called = {"count": 0}

    def _tracking_get_db_session():
        called["count"] += 1
        return MagicMock()

    monkeypatch.setattr(maintenance, "get_db_session", _tracking_get_db_session)

    try:
        maintenance.scheduled_task_execution(
            task_id=1,
            scheduled_time="2999-01-01T00:00:00Z",
            prompt="do it",
        )
    except Exception:
        pass

    assert called["count"] == 0


def test_cleanup_old_logs_closes_session_on_success(monkeypatch):
    fake_db = MagicMock()
    fake_query = MagicMock()
    fake_query.filter.return_value = fake_query
    fake_query.delete.return_value = 3
    fake_db.query.return_value = fake_query

    monkeypatch.setattr(maintenance, "get_db_session", lambda: fake_db)

    result = maintenance.cleanup_old_logs(days=1)

    assert result["deleted_count"] == 3
    assert fake_db.close.called
