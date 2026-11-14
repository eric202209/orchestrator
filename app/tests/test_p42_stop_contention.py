"""Real Operator Stop must report a held SQLite writer without writing again."""

import json
import logging

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import create_engine, event
from sqlalchemy.orm import sessionmaker

from app.celery_app import celery_app
from app.config import settings
from app.database import Base
from app.dependencies import get_current_user
from app.models import LogEntry, Project, Session as SessionModel, SessionTask
from app.models import Task, TaskExecution, TaskStatus, User
from app.services.session import session_lifecycle_service as lifecycle
from app.services.workspace.checkpoint_service import CheckpointService
from app.tests.test_task_execution_transaction_regressions import (
    _create_running_attempt,
)


@pytest.fixture
def db_session_factory(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'stop.sqlite3'}",
        connect_args={"check_same_thread": False, "timeout": 0.05},
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(engine)
    try:
        yield sessionmaker(bind=engine, autoflush=False)
    finally:
        engine.dispose()


@pytest.mark.parametrize("force", [False, True])
@pytest.mark.parametrize("checkpoint_fails", [False, True])
def test_operator_stop_writer_contention(
    api_app, db_session_factory, monkeypatch, tmp_path, caplog, checkpoint_fails, force
):
    with db_session_factory() as db:
        task_id, link_id, execution_id = _create_running_attempt(db)
        session_id = db.get(SessionTask, link_id).session_id
        db.get(Project, db.get(Task, task_id).project_id).user_id = 1
        db.add(
            LogEntry(
                session_id=session_id,
                level="INFO",
                message="Task queued",
                log_metadata=json.dumps({"celery_task_id": "disposable-worker"}),
            )
        )
        db.commit()
    api_app.dependency_overrides[get_current_user] = lambda: User(
        id=1, email="eval@local.dev", is_active=True, hashed_password="unused"
    )
    checkpoint_root = tmp_path / "checkpoints"
    monkeypatch.setattr(settings, "CHECKPOINT_DIR", str(checkpoint_root))
    monkeypatch.setattr(
        CheckpointService, "LEGACY_CHECKPOINT_DIR", str(checkpoint_root)
    )
    monkeypatch.setattr(
        CheckpointService,
        "load_checkpoint",
        lambda *a, **k: {
            "context": {"task_id": task_id},
            "step_results": [{"ok": True}],
        },
    )
    order = []
    real_write = CheckpointService._write_checkpoint_json_atomic

    def write_checkpoint(self, *args, **kwargs):
        order.append("checkpoint")
        if checkpoint_fails:
            raise OSError("disposable checkpoint failure")
        return real_write(self, *args, **kwargs)

    monkeypatch.setattr(
        CheckpointService, "_write_checkpoint_json_atomic", write_checkpoint
    )
    revokes = []

    def revoke(task_id, **kwargs):
        order.append("revoke")
        revokes.append((task_id, kwargs))

    monkeypatch.setattr(celery_app.control, "revoke", revoke)

    async def no_lease(*args, **kwargs):
        return {"status": "not_owned", "backend_id": None}

    monkeypatch.setattr(lifecycle, "_await_backend_lease_release", no_lease)

    class OfflineRuntime:
        async def stop_session(self):
            order.append("runtime_stop")

    def runtime(*args, **kwargs):
        assert not force, "force Stop must not create a runtime"
        return OfflineRuntime()

    monkeypatch.setattr(lifecycle, "create_agent_runtime", runtime)
    engine = db_session_factory.kw["bind"]
    writes = []
    failures = []
    rollbacks = []

    def before_sql(conn, cursor, statement, parameters, context, executemany):
        if statement.lstrip().split()[0].upper() in {"INSERT", "UPDATE", "DELETE"}:
            writes.append(statement)

    def sql_failed(context):
        failures.append(str(context.original_exception))

    def rolled_back(conn):
        rollbacks.append(len(writes))

    # Separate Worker connection owns the lock before the HTTP request starts.
    # Synchronous acquisition/release gives deterministic ordering without sleeps.
    with engine.connect() as worker:
        worker.exec_driver_sql("BEGIN IMMEDIATE")
        worker.exec_driver_sql("UPDATE tasks SET status=status WHERE id=?", (task_id,))
        event.listen(engine, "before_cursor_execute", before_sql)
        event.listen(engine, "handle_error", sql_failed)
        event.listen(engine, "rollback", rolled_back)
        try:
            with caplog.at_level(logging.ERROR), TestClient(api_app) as client:
                response = client.post(
                    f"/api/v1/sessions/{session_id}/stop?force={str(force).lower()}"
                )
            assert response.status_code == 500, response.text
            assert "database is locked" in response.json()["detail"]
            assert len(failures) == 1
            assert len(writes) == 1  # no second INSERT for failure LogEntry
            assert rollbacks == [1]
            assert "Failed to stop session" in caplog.text
            assert order == ["checkpoint", "revoke"] + (
                [] if force else ["runtime_stop"]
            )
            assert revokes == [
                ("disposable-worker", {"terminate": True, "signal": "SIGTERM"})
            ]
            assert (
                bool(list(checkpoint_root.glob("session_*_stopped_*.json")))
                is not checkpoint_fails
            )
            with db_session_factory() as check:
                assert check.get(Task, task_id).status == TaskStatus.RUNNING
                assert check.get(SessionTask, link_id).status == TaskStatus.RUNNING
                assert (
                    check.get(TaskExecution, execution_id).status == TaskStatus.RUNNING
                )
                assert check.get(SessionModel, session_id).status == "running"
                assert check.query(LogEntry).count() == 1
                assert not any(
                    json.loads(row.log_metadata or "{}").get("event_type")
                    == "session_stopped"
                    for row in check.query(LogEntry)
                )
        finally:
            event.remove(engine, "before_cursor_execute", before_sql)
            event.remove(engine, "handle_error", sql_failed)
            event.remove(engine, "rollback", rolled_back)
            worker.rollback()
    # A real subsequent cancellation remains admissible after releasing the lock.
    with TestClient(api_app) as client:
        response = client.post(
            f"/api/v1/sessions/{session_id}/stop?force={str(force).lower()}"
        )
    assert response.status_code == 200, response.text
    assert response.json()["status"] == "stopped"
    with db_session_factory() as check:
        assert check.get(Task, task_id).status == TaskStatus.CANCELLED
        assert check.get(SessionTask, link_id).status == TaskStatus.CANCELLED
        assert check.get(TaskExecution, execution_id).status == TaskStatus.CANCELLED
        stopped = [
            row
            for row in check.query(LogEntry)
            if json.loads(row.log_metadata or "{}").get("event_type")
            == "session_stopped"
        ]
        assert len(stopped) == 1
        assert (
            json.loads(stopped[0].log_metadata)["checkpoint_name"] is None
        ) is checkpoint_fails
