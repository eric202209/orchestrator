"""LEGACY_EXECUTION_REPAIR_03 -- the stop lifecycle's backend lease wait is bounded.

Provider-free and signal-free: Redis is an in-memory fake, the worker is a
recorded identity on another hostname (so its liveness is unverifiable from the
API, exactly as in the split API/worker deployment), and every stop call runs
under an outer ``asyncio.wait_for`` so a regression cannot hang the suite.
"""

from __future__ import annotations

import asyncio
import json

import pytest
from fastapi import HTTPException

import app.services.session.session_lifecycle_service as session_lifecycle_service
from app.models import LogEntry, SessionTask, TaskExecution, TaskStatus
from app.services.session.session_lifecycle_service import stop_session_lifecycle
from app.tests.test_session_lifecycle_regressions import (
    _make_project,
    _make_session,
    _make_task,
)

_HARNESS_TIMEOUT_SECONDS = 3.0


class _LeaseRedis:
    """Slot set for one backend; ``release_after`` polls ends the hold."""

    def __init__(self, members, *, release_after=None, error=None):
        self.members = {str(member) for member in members}
        self.release_after = release_after
        self.error = error
        self.polls = 0
        self.srem_calls: list[str] = []

    def smembers(self, _key):
        if self.error is not None:
            raise self.error
        self.polls += 1
        if self.release_after is not None and self.polls > self.release_after:
            self.members.clear()
        return set(self.members)

    def srem(self, _key, member):
        self.srem_calls.append(str(member))
        self.members.discard(str(member))

    def hdel(self, _key, _field):
        return 1


class _Runtime:
    def __init__(self, on_stop=None):
        self.stop_calls = 0
        self.on_stop = on_stop

    async def stop_session(self):
        self.stop_calls += 1
        if self.on_stop is not None:
            self.on_stop()


def _running_execution(db, *, worker_hostname="remote-worker-host"):
    project = _make_project(db)
    session = _make_session(db, project, status="running", is_active=True)
    task = _make_task(db, project, status=TaskStatus.RUNNING)
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
        backend_id="local_openclaw",
        worker_pid=424242,
        worker_hostname=worker_hostname,
    )
    db.add_all(
        [
            SessionTask(
                session_id=session.id, task_id=task.id, status=TaskStatus.RUNNING
            ),
            execution,
        ]
    )
    db.commit()
    return session, execution


def _patch_stop(monkeypatch, redis, runtime, *, timeout_seconds=0.2):
    monkeypatch.setattr(
        session_lifecycle_service, "_BACKEND_LEASE_POLL_INTERVAL_SECONDS", 0.01
    )
    monkeypatch.setattr(
        session_lifecycle_service,
        "_BACKEND_LEASE_RELEASE_TIMEOUT_SECONDS",
        timeout_seconds,
        raising=False,
    )
    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.make_redis_client",
        lambda: redis,
    )
    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.create_agent_runtime",
        lambda *a, **kw: runtime,
    )
    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.revoke_session_celery_tasks",
        lambda *a, **kw: ["celery-task-id"],
    )
    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.CheckpointService",
        type(
            "FakeCS",
            (),
            {
                "__init__": lambda self, db: None,
                "load_checkpoint": lambda self, sid: (_ for _ in ()).throw(
                    Exception("no checkpoint")
                ),
            },
        ),
    )


def _stop(db, session_id):
    return asyncio.run(
        asyncio.wait_for(
            stop_session_lifecycle(db, session_id),
            timeout=_HARNESS_TIMEOUT_SECONDS,
        )
    )


def _messages(db, session_id) -> list[str]:
    return [
        entry.message
        for entry in db.query(LogEntry).filter(LogEntry.session_id == session_id)
    ]


def _assert_not_stopped(db, session, execution, redis, runtime):
    db.refresh(session)
    db.refresh(execution)
    assert session.status == "running"
    assert session.is_active
    assert execution.status == TaskStatus.RUNNING
    assert str(session.id) in redis.members
    assert redis.srem_calls == []
    assert f"Session stopped: {session.name}" not in _messages(db, session.id)


def test_live_unverifiable_owner_fails_stop_after_bound_without_release(
    db_session, monkeypatch
):
    session, execution = _running_execution(db_session)
    redis = _LeaseRedis({session.id})
    runtime = _Runtime()
    _patch_stop(monkeypatch, redis, runtime)

    with pytest.raises(HTTPException) as exc_info:
        _stop(db_session, session.id)

    assert exc_info.value.status_code == 500
    assert "not confirmed" in exc_info.value.detail
    _assert_not_stopped(db_session, session, execution, redis, runtime)
    assert runtime.stop_calls == 0
    failure = (
        db_session.query(LogEntry)
        .filter(
            LogEntry.session_id == session.id,
            LogEntry.message.like("Failed to stop session:%"),
        )
        .one()
    )
    assert json.loads(failure.log_metadata)["event_type"] == "session_stop_failed"


def test_lease_reacquired_before_terminal_commit_rolls_back_stop(
    db_session, monkeypatch
):
    """The bound on the second wait must not commit the flushed stop state."""
    session, execution = _running_execution(db_session)
    redis = _LeaseRedis(set())
    runtime = _Runtime(on_stop=lambda: redis.members.add(str(session.id)))
    _patch_stop(monkeypatch, redis, runtime)

    with pytest.raises(HTTPException) as exc_info:
        _stop(db_session, session.id)

    assert exc_info.value.status_code == 500
    assert runtime.stop_calls == 1
    _assert_not_stopped(db_session, session, execution, redis, runtime)


def test_lease_released_normally_stops_without_api_release(db_session, monkeypatch):
    session, execution = _running_execution(db_session)
    redis = _LeaseRedis({session.id}, release_after=3)
    runtime = _Runtime()
    _patch_stop(monkeypatch, redis, runtime, timeout_seconds=2.0)

    result = _stop(db_session, session.id)

    assert result["status"] == "stopped"
    db_session.refresh(session)
    db_session.refresh(execution)
    assert session.status == "stopped"
    assert execution.status == TaskStatus.CANCELLED
    assert redis.srem_calls == []
    stop_log = (
        db_session.query(LogEntry)
        .filter(LogEntry.message == f"Session stopped: {session.name}")
        .one()
    )
    assert json.loads(stop_log.log_metadata)["backend_lease"]["status"] == "released"


def test_confirmed_dead_owner_is_reclaimed_and_stopped(db_session, monkeypatch):
    session, _execution = _running_execution(db_session)
    redis = _LeaseRedis({session.id})
    runtime = _Runtime()
    _patch_stop(monkeypatch, redis, runtime)
    monkeypatch.setattr(
        session_lifecycle_service, "_execution_owner_is_alive", lambda _e: False
    )

    result = _stop(db_session, session.id)

    assert result["status"] == "stopped"
    assert redis.srem_calls == [str(session.id)]
    db_session.refresh(session)
    assert session.status == "stopped"


def test_slot_held_by_another_session_is_left_untouched(db_session, monkeypatch):
    session, _execution = _running_execution(db_session)
    redis = _LeaseRedis({999})
    runtime = _Runtime()
    _patch_stop(monkeypatch, redis, runtime)

    result = _stop(db_session, session.id)

    assert result["status"] == "stopped"
    assert redis.members == {"999"}
    assert redis.srem_calls == []


def test_redis_error_with_recorded_execution_fails_stop(db_session, monkeypatch):
    session, execution = _running_execution(db_session)
    redis = _LeaseRedis({session.id}, error=ConnectionError("redis down"))
    runtime = _Runtime()
    _patch_stop(monkeypatch, redis, runtime)

    with pytest.raises(HTTPException) as exc_info:
        _stop(db_session, session.id)

    assert exc_info.value.status_code == 500
    assert "redis down" in exc_info.value.detail
    redis.error = None
    _assert_not_stopped(db_session, session, execution, redis, runtime)


def test_cancelled_stop_request_leaves_ownership_and_state(db_session, monkeypatch):
    session, execution = _running_execution(db_session)
    redis = _LeaseRedis({session.id})
    runtime = _Runtime()
    _patch_stop(monkeypatch, redis, runtime, timeout_seconds=30.0)

    async def _cancel_mid_wait():
        stop_task = asyncio.create_task(stop_session_lifecycle(db_session, session.id))
        while redis.polls < 3:
            await asyncio.sleep(0.01)
        stop_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await stop_task

    asyncio.run(asyncio.wait_for(_cancel_mid_wait(), timeout=_HARNESS_TIMEOUT_SECONDS))

    db_session.rollback()
    _assert_not_stopped(db_session, session, execution, redis, runtime)
