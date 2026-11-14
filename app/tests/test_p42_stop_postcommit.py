"""Operator Stop acknowledges its authoritative commit despite ancillary failure."""

import json
import logging

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.database import Base
from app.dependencies import get_current_user
from app.models import LogEntry, Project, Session as SessionModel, SessionTask
from app.models import Task, TaskExecution, TaskStatus, User
from app.services.session import session_lifecycle_service as lifecycle
from app.tests.test_task_execution_transaction_regressions import (
    _create_running_attempt,
)


@pytest.fixture
def db_session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'postcommit.sqlite3'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, autoflush=False)
    finally:
        engine.dispose()


@pytest.mark.parametrize("boundary", ["workflow", "logging", "projection"])
@pytest.mark.parametrize("http_exception", [False, True])
def test_operator_stop_acknowledges_postcommit_failure(
    api_app, db_session_factory, monkeypatch, caplog, boundary, http_exception
):
    with db_session_factory() as db:
        task_id, link_id, execution_id = _create_running_attempt(db)
        session_id = db.get(SessionTask, link_id).session_id
        db.get(Project, db.get(Task, task_id).project_id).user_id = 1
        db.commit()
    api_app.dependency_overrides[get_current_user] = lambda: User(
        id=1, email="eval@local.dev", is_active=True, hashed_password="unused"
    )
    monkeypatch.setattr(
        lifecycle,
        "CheckpointService",
        lambda *a, **k: (_ for _ in ()).throw(OSError("offline checkpoint")),
    )
    revokes = []

    def revoke(db, sid, terminate):
        revokes.append(sid)
        return []

    monkeypatch.setattr(lifecycle, "revoke_session_celery_tasks", revoke)

    async def no_lease(*args, **kwargs):
        return {"status": "not_owned", "backend_id": None}

    monkeypatch.setattr(lifecycle, "_await_backend_lease_release", no_lease)
    monkeypatch.setattr(
        lifecycle,
        "create_agent_runtime",
        lambda *a, **k: pytest.fail("force Stop must not create a runtime"),
    )
    observed = []

    def assert_committed():
        # Read on a separate connection while the failing request is still active:
        # a flush in the request's transaction would not be visible here.
        with db_session_factory() as check:
            assert check.get(Task, task_id).status == TaskStatus.CANCELLED
            assert check.get(SessionTask, link_id).status == TaskStatus.CANCELLED
            assert check.get(TaskExecution, execution_id).status == TaskStatus.CANCELLED
            session = check.get(SessionModel, session_id)
            assert session.status == "stopped"
            assert session.is_active is False
        observed.append(boundary)

    def fail():
        assert_committed()
        if http_exception:
            raise HTTPException(status_code=503, detail="injected postcommit failure")
        raise RuntimeError("injected postcommit failure")

    engine = db_session_factory.kw["bind"]
    if boundary == "workflow":

        def fail_workflow(db, *args, **kwargs):
            # Uncommitted ancillary mutations must be discarded after the error.
            db.get(Task, task_id).title = "uncommitted ancillary change"
            fail()

        monkeypatch.setattr(lifecycle, "close_session_workflows", fail_workflow)
    elif boundary == "logging":

        def fail_success_log(conn, cursor, statement, parameters, context, executemany):
            if statement.startswith("INSERT INTO log_entries"):
                fail()

        event.listen(engine, "before_cursor_execute", fail_success_log)
    else:
        monkeypatch.setattr(
            lifecycle, "_with_lifecycle_projection", lambda *a, **k: fail()
        )

    try:
        with caplog.at_level(logging.ERROR), TestClient(api_app) as client:
            response = client.post(f"/api/v1/sessions/{session_id}/stop?force=true")
            # Establish durable cancellation before testing the response: this
            # makes the pre-fix red result distinct from an uncommitted Stop.
            assert_committed()
            assert response.status_code == 200, response.text
            payload = response.json()
            assert payload["status"] == "stopped"
            assert payload["session_id"] == session_id
            assert payload["initiated_by"] == "eval@local.dev"
            assert payload["source"] == f"api:POST /api/v1/sessions/{session_id}/stop"
            assert (
                payload["message"]
                == "Session 'Terminal CAS Session' stopped successfully"
            )
            if boundary == "projection":
                assert "orchestration_state" not in payload
            else:
                assert payload["orchestration_state"]["logical_terminal"] is True
            assert "injected postcommit failure" in caplog.text
            assert "Failed to stop session:" not in caplog.text
            repeated = client.post(f"/api/v1/sessions/{session_id}/stop?force=true")
            assert repeated.status_code == 200, repeated.text
            assert repeated.json()["status"] == "stopped"
            assert "already stopped" in repeated.json()["message"]
            assert revokes == [session_id]
        with db_session_factory() as check:
            assert check.get(Task, task_id).title == "Terminal CAS Task"
            logs = list(check.query(LogEntry))
            success_logs = [
                row
                for row in logs
                if json.loads(row.log_metadata or "{}").get("event_type")
                == "session_stopped"
            ]
            assert len(success_logs) == (1 if boundary == "projection" else 0)
        assert observed.count(boundary) >= 2
    finally:
        if boundary == "logging":
            event.remove(engine, "before_cursor_execute", fail_success_log)
