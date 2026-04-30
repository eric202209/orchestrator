"""Regression tests for session lifecycle boundary conditions.

Covers start/stop/pause/resume edge cases that have historically caused
silent failures or confusing error responses.
"""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime

import pytest

from app.models import (
    LogEntry,
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskStatus,
)
from app.services.session.session_lifecycle_service import (
    pause_session_lifecycle,
    resume_session_lifecycle,
    start_session_lifecycle,
    stop_session_lifecycle,
)
from app.services.workspace.checkpoint_service import CheckpointError
from fastapi import HTTPException


# ── helpers ───────────────────────────────────────────────────────────────────


def _make_project(db):
    project = Project(name="LC Regression", workspace_path="/tmp/lc_test")
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _make_session(
    db, project, *, status="stopped", is_active=False, execution_mode="manual"
):
    session = SessionModel(
        project_id=project.id,
        name="Test Session",
        description="test",
        status=status,
        is_active=is_active,
        execution_mode=execution_mode,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _make_task(db, project, *, status=TaskStatus.PENDING):
    task = Task(
        project_id=project.id,
        title="Test task",
        description="do something",
        status=status,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


# ── start boundary conditions ─────────────────────────────────────────────────


def test_start_already_running_session_returns_400(db_session, monkeypatch):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="running", is_active=True)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(start_session_lifecycle(db_session, session.id))

    assert exc_info.value.status_code == 400
    assert "already" in exc_info.value.detail.lower()


def test_start_recovers_orphaned_planning_run_before_restarting(
    db_session, monkeypatch
):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="running", is_active=True)
    task = _make_task(db_session, project, status=TaskStatus.RUNNING)
    task.current_step = 0
    db_session.commit()

    session_task = SessionTask(
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.RUNNING,
        started_at=None,
    )
    db_session.add(session_task)
    db_session.add(
        LogEntry(
            session_id=session.id,
            task_id=task.id,
            level="INFO",
            message="[ORCHESTRATION] Planning response received; parsing and validating plan",
            created_at=datetime.now(UTC),
        )
    )
    db_session.commit()

    stale_log = (
        db_session.query(LogEntry)
        .filter(LogEntry.session_id == session.id, LogEntry.task_id == task.id)
        .order_by(LogEntry.id.desc())
        .first()
    )
    stale_log.created_at = datetime(2026, 4, 28, 12, 0, 0)
    db_session.commit()

    class _FakeRuntime:
        backend_descriptor = type("D", (), {"name": "local_openclaw"})()

        async def create_session(self, task_description):
            return "fake-key"

    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.create_agent_runtime",
        lambda *a, **kw: _FakeRuntime(),
    )
    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.queue_task_for_session",
        lambda **kw: {"task_id": kw["task_id"]},
    )

    result = asyncio.run(start_session_lifecycle(db_session, session.id))

    db_session.refresh(session)
    db_session.refresh(task)
    assert result["status"] == "started"
    assert session.status == "running"
    assert task.status == TaskStatus.PENDING

    recovery_log = (
        db_session.query(LogEntry)
        .filter(
            LogEntry.session_id == session.id,
            LogEntry.message
            == "Recovered orphaned running task after planning-response handling stalled without further progress",
        )
        .order_by(LogEntry.id.desc())
        .first()
    )
    assert recovery_log is not None


def test_start_already_paused_session_returns_400(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="paused", is_active=True)

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(start_session_lifecycle(db_session, session.id))

    assert exc_info.value.status_code == 400


def test_start_nonexistent_session_returns_404(db_session):
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(start_session_lifecycle(db_session, 99999))

    assert exc_info.value.status_code == 404


def test_start_stuck_pending_session_resets_and_proceeds(db_session, monkeypatch):
    """A session stuck in pending+is_active should be reset then attempted."""
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="pending", is_active=True)

    _make_task(db_session, project, status=TaskStatus.PENDING)

    call_log = []

    class _FakeRuntime:
        backend_descriptor = type("D", (), {"name": "local_openclaw"})()

        async def create_session(self, task_description):
            call_log.append("create_session")
            return "fake-key"

    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.create_agent_runtime",
        lambda *a, **kw: _FakeRuntime(),
    )
    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.queue_task_for_session",
        lambda **kw: {"task_id": kw["task_id"]},
    )

    result = asyncio.run(start_session_lifecycle(db_session, session.id))

    assert result["status"] == "started"
    db_session.refresh(session)
    assert session.status == "running"


def test_start_automatic_session_requeues_failed_task_even_after_auto_budget_exhausted(
    db_session, monkeypatch
):
    project = _make_project(db_session)
    session = _make_session(
        db_session,
        project,
        status="stopped",
        is_active=False,
        execution_mode="automatic",
    )
    task = _make_task(db_session, project, status=TaskStatus.FAILED)
    task.error_message = (
        "step 1 rollback blocked: Parent-directory traversal is not allowed"
    )
    db_session.commit()

    db_session.add(
        LogEntry(
            session_id=session.id,
            task_id=task.id,
            level="INFO",
            message=(
                "Recovered earliest failed/cancelled ordered task for automatic retry: "
                "#None Test task"
            ),
        )
    )
    db_session.commit()

    class _FakeRuntime:
        backend_descriptor = type("D", (), {"name": "local_openclaw"})()

        async def create_session(self, task_description):
            return "fake-key"

    queued = []

    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.create_agent_runtime",
        lambda *a, **kw: _FakeRuntime(),
    )
    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.queue_task_for_session",
        lambda **kw: queued.append(kw["task_id"]) or {"task_id": kw["task_id"]},
    )

    result = asyncio.run(start_session_lifecycle(db_session, session.id))

    assert result["status"] == "started"
    assert queued == [task.id]
    db_session.refresh(task)
    assert task.status == TaskStatus.PENDING


# ── stop boundary conditions ──────────────────────────────────────────────────


def test_stop_already_stopped_session_returns_400(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="stopped")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(stop_session_lifecycle(db_session, session.id))

    assert exc_info.value.status_code == 400


def test_stop_nonexistent_session_returns_404(db_session):
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(stop_session_lifecycle(db_session, 99999))

    assert exc_info.value.status_code == 404


def test_stop_running_session_sets_status_stopped(db_session, monkeypatch):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="running", is_active=True)

    class _FakeRuntime:
        backend_descriptor = type("D", (), {"name": "local_openclaw"})()

        async def stop_session(self):
            pass

    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.create_agent_runtime",
        lambda *a, **kw: _FakeRuntime(),
    )
    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.revoke_session_celery_tasks",
        lambda db, session_id, terminate=False: [],
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

    result = asyncio.run(
        stop_session_lifecycle(
            db_session,
            session.id,
            initiated_by="tester@example.com",
            source="api:POST /sessions/1/stop",
        )
    )

    assert result["status"] == "stopped"
    assert result["initiated_by"] == "tester@example.com"
    assert result["source"] == "api:POST /sessions/1/stop"
    db_session.refresh(session)
    assert session.status == "stopped"
    assert not session.is_active

    stop_log = (
        db_session.query(LogEntry)
        .filter(
            LogEntry.session_id == session.id,
            LogEntry.message == f"Session stopped: {session.name}",
        )
        .order_by(LogEntry.id.desc())
        .first()
    )
    assert stop_log is not None
    stop_metadata = json.loads(stop_log.log_metadata or "{}")
    assert stop_metadata["initiated_by"] == "tester@example.com"
    assert stop_metadata["source"] == "api:POST /sessions/1/stop"


def test_stop_session_saves_rich_checkpoint_when_latest_checkpoint_is_hollow(
    db_session, monkeypatch
):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="running", is_active=True)
    task = _make_task(db_session, project, status=TaskStatus.RUNNING)
    task.task_subfolder = "apps/frontend"
    task.steps = json.dumps(
        [
            {
                "step_number": 1,
                "description": "Create app shell",
                "commands": ["mkdir -p apps/frontend/src"],
                "verification": "test -d apps/frontend/src",
                "rollback": "rm -rf apps/frontend/src",
                "expected_files": ["apps/frontend/src/main.tsx"],
            }
        ]
    )
    task.current_step = 1
    db_session.add(
        SessionTask(
            session_id=session.id,
            task_id=task.id,
            status=TaskStatus.RUNNING,
            started_at=datetime.now(UTC).replace(tzinfo=None),
        )
    )
    db_session.commit()

    captured = {}

    class _FakeCheckpointService:
        def __init__(self, db):
            self.db = db

        def load_checkpoint(self, session_id):
            return {
                "context": {"task_id": task.id, "task_description": task.description},
                "orchestration_state": {},
                "step_results": [],
            }

        def save_checkpoint(
            self,
            session_id,
            checkpoint_name="manual",
            context_data=None,
            orchestration_state=None,
            current_step_index=None,
            step_results=None,
        ):
            captured["saved"] = {
                "session_id": session_id,
                "checkpoint_name": checkpoint_name,
                "context_data": context_data or {},
                "orchestration_state": orchestration_state or {},
                "current_step_index": current_step_index,
                "step_results": step_results or [],
            }
            return {"success": True}

    class _FakeRuntime:
        async def stop_session(self):
            return None

    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.CheckpointService",
        _FakeCheckpointService,
    )
    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.revoke_session_celery_tasks",
        lambda *a, **kw: [],
    )
    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.create_agent_runtime",
        lambda *a, **kw: _FakeRuntime(),
    )

    result = asyncio.run(stop_session_lifecycle(db_session, session.id))

    assert result["status"] == "stopped"
    saved = captured["saved"]
    assert saved["context_data"]["task_id"] == task.id
    assert saved["context_data"]["task_subfolder"] == "apps/frontend"
    assert saved["context_data"]["workspace_path_override"].endswith("lc_test")
    assert saved["context_data"]["project_dir_override"].endswith("lc_test")
    assert saved["orchestration_state"]["plan"][0]["description"] == "Create app shell"
    assert saved["current_step_index"] == 1


# ── pause boundary conditions ─────────────────────────────────────────────────


def test_pause_stopped_session_returns_400(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="stopped")

    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(pause_session_lifecycle(db_session, session.id))

    assert exc_info.value.status_code == 400


def test_pause_nonexistent_session_returns_404(db_session):
    with pytest.raises(HTTPException) as exc_info:
        asyncio.run(pause_session_lifecycle(db_session, 99999))

    assert exc_info.value.status_code == 404


def test_pause_session_saves_rich_checkpoint_when_only_hollow_checkpoint_exists(
    db_session, monkeypatch
):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="running", is_active=True)
    task = _make_task(db_session, project, status=TaskStatus.RUNNING)
    task.task_subfolder = "backend"
    task.steps = json.dumps(
        [
            {
                "step_number": 1,
                "description": "Bootstrap backend",
                "commands": ["mkdir -p backend/src"],
                "verification": "test -d backend/src",
                "rollback": "rm -rf backend/src",
                "expected_files": ["backend/src/index.ts"],
            }
        ]
    )
    task.current_step = 0
    db_session.add(
        SessionTask(
            session_id=session.id,
            task_id=task.id,
            status=TaskStatus.RUNNING,
            started_at=datetime.now(UTC).replace(tzinfo=None),
        )
    )
    db_session.commit()

    captured = {}

    class _FakeCheckpointService:
        def __init__(self, db):
            self.db = db

        def load_checkpoint(self, session_id, checkpoint_name=None):
            return {
                "context": {"task_id": task.id, "task_description": task.description},
                "orchestration_state": {},
                "step_results": [],
            }

        def save_checkpoint(
            self,
            session_id,
            checkpoint_name="manual",
            context_data=None,
            orchestration_state=None,
            current_step_index=None,
            step_results=None,
        ):
            captured["saved"] = {
                "session_id": session_id,
                "checkpoint_name": checkpoint_name,
                "context_data": context_data or {},
                "orchestration_state": orchestration_state or {},
                "current_step_index": current_step_index,
                "step_results": step_results or [],
            }
            return {"success": True}

    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.CheckpointService",
        _FakeCheckpointService,
    )
    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.revoke_session_celery_tasks",
        lambda *a, **kw: [],
    )

    result = asyncio.run(pause_session_lifecycle(db_session, session.id))

    assert result["status"] == "paused"
    saved = captured["saved"]
    assert saved["context_data"]["task_id"] == task.id
    assert saved["context_data"]["task_subfolder"] == "backend"
    assert saved["context_data"]["project_dir_override"].endswith("lc_test")
    assert saved["orchestration_state"]["plan"][0]["description"] == "Bootstrap backend"
    assert saved["current_step_index"] == 0


# ── manual mode start ─────────────────────────────────────────────────────────


def test_start_manual_session_with_no_tasks_starts_without_queuing(
    db_session, monkeypatch
):
    """Manual sessions should reach running status even with no pending tasks."""
    project = _make_project(db_session)
    session = _make_session(
        db_session, project, status="stopped", execution_mode="manual"
    )

    class _FakeRuntime:
        backend_descriptor = type("D", (), {"name": "local_openclaw"})()

        async def create_session(self, task_description):
            return "fake-key"

    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.create_agent_runtime",
        lambda *a, **kw: _FakeRuntime(),
    )

    result = asyncio.run(start_session_lifecycle(db_session, session.id))

    assert result["status"] == "started"
    db_session.refresh(session)
    assert session.status == "running"


def test_start_manual_session_requeues_last_selected_task(db_session, monkeypatch):
    project = _make_project(db_session)
    session = _make_session(
        db_session, project, status="stopped", execution_mode="manual"
    )
    task = _make_task(db_session, project, status=TaskStatus.PENDING)
    db_session.add(
        SessionTask(
            session_id=session.id,
            task_id=task.id,
            status=TaskStatus.PENDING,
        )
    )
    db_session.commit()

    captured = {}

    class _FakeRuntime:
        backend_descriptor = type("D", (), {"name": "local_openclaw"})()

        async def create_session(self, task_description):
            return "fake-key"

    class _FakeCheckpointService:
        def __init__(self, db):
            self.db = db

        def load_resume_checkpoint(self, session_id, checkpoint_name=None):
            raise CheckpointError("no replayable checkpoint")

    def _fake_queue_task_for_session(*, db, session, task_id, timeout_seconds=1800):
        captured["queued_task"] = {
            "session_id": session.id,
            "task_id": task_id,
            "timeout_seconds": timeout_seconds,
        }
        return {"task_id": task_id, "celery_id": "manual-requeue-1"}

    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.create_agent_runtime",
        lambda *a, **kw: _FakeRuntime(),
    )
    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.CheckpointService",
        _FakeCheckpointService,
    )
    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.queue_task_for_session",
        _fake_queue_task_for_session,
    )

    result = asyncio.run(start_session_lifecycle(db_session, session.id))

    assert result["status"] == "started"
    assert captured["queued_task"]["task_id"] == task.id
    db_session.refresh(session)
    assert session.status == "running"


def test_start_manual_session_resumes_last_task_from_checkpoint(
    db_session, monkeypatch
):
    project = _make_project(db_session)
    session = _make_session(
        db_session, project, status="stopped", execution_mode="manual"
    )
    task = _make_task(db_session, project, status=TaskStatus.PENDING)
    db_session.add(
        SessionTask(
            session_id=session.id,
            task_id=task.id,
            status=TaskStatus.PENDING,
        )
    )
    db_session.commit()

    captured = {}

    class _FakeRuntime:
        backend_descriptor = type("D", (), {"name": "local_openclaw"})()

        async def create_session(self, task_description):
            return "fake-key"

    class _FakeCheckpointService:
        def __init__(self, db):
            self.db = db

        def load_resume_checkpoint(self, session_id, checkpoint_name=None):
            return {
                "_requested_checkpoint_name": checkpoint_name,
                "_resolved_checkpoint_name": "stopped_20260428_120000",
                "checkpoint_name": "stopped_20260428_120000",
                "context": {
                    "task_id": task.id,
                    "task_description": "resume from checkpoint",
                },
                "orchestration_state": {
                    "plan": [
                        {
                            "step_number": 1,
                            "description": "Resume work",
                            "commands": ["echo resume"],
                            "verification": "test -n resume",
                            "rollback": "true",
                            "expected_files": [],
                        }
                    ],
                    "current_step_index": 0,
                    "execution_results": [],
                },
                "step_results": [],
            }

    class _FakeDelayResult:
        id = "manual-resume-1"

    class _FakeWorkerTask:
        @staticmethod
        def delay(**kwargs):
            captured["delay_kwargs"] = kwargs
            return _FakeDelayResult()

    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.create_agent_runtime",
        lambda *a, **kw: _FakeRuntime(),
    )
    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.CheckpointService",
        _FakeCheckpointService,
    )
    monkeypatch.setattr(
        "app.tasks.worker.execute_orchestration_task",
        _FakeWorkerTask,
    )

    result = asyncio.run(start_session_lifecycle(db_session, session.id))

    assert result["status"] == "started"
    assert captured["delay_kwargs"]["task_id"] == task.id
    assert (
        captured["delay_kwargs"]["resume_checkpoint_name"] == "stopped_20260428_120000"
    )
    db_session.refresh(session)
    assert session.status == "running"


def test_resume_session_requeues_fresh_when_checkpoint_has_no_execution_progress(
    db_session, monkeypatch
):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="paused", is_active=True)
    task = _make_task(db_session, project, status=TaskStatus.PENDING)

    captured = {}

    class _FakeCheckpointService:
        def __init__(self, db):
            self.db = db

        def load_resume_checkpoint(self, session_id, checkpoint_name=None):
            captured["requested_checkpoint_name"] = checkpoint_name
            return {
                "_requested_checkpoint_name": checkpoint_name,
                "_resolved_checkpoint_name": checkpoint_name or "paused_latest",
                "checkpoint_name": checkpoint_name or "paused_latest",
                "context": {
                    "task_id": task.id,
                    "task_description": "resume from checkpoint",
                },
                "orchestration_state": {},
                "step_results": [],
            }

        def _checkpoint_restore_fidelity(self, data):
            return {
                "score": 35,
                "status": "low",
                "summary": "Checkpoint replay is fragile; important state is missing",
                "present_signals": ["task id", "task description"],
                "warnings": ["missing workspace path", "missing execution plan"],
            }

    def _fake_queue_task_for_session(*, db, session, task_id, timeout_seconds=1800):
        captured["queued_task"] = {
            "session_id": session.id,
            "task_id": task_id,
            "timeout_seconds": timeout_seconds,
        }
        return {"celery_id": "celery-queued-fresh-1"}

    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.CheckpointService",
        _FakeCheckpointService,
    )
    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.queue_task_for_session",
        _fake_queue_task_for_session,
    )

    result = asyncio.run(
        resume_session_lifecycle(
            db_session,
            session.id,
            checkpoint_name="paused_20260424_034703",
        )
    )

    db_session.refresh(session)
    db_session.refresh(task)

    assert result["status"] == "resumed"
    assert result["restore_fidelity"]["status"] == "low"
    assert captured["requested_checkpoint_name"] == "paused_20260424_034703"
    assert captured["queued_task"]["task_id"] == task.id
    assert session.status == "running"
    assert session.is_active is True
    assert task.status == TaskStatus.PENDING
    assert "no execution progress to replay" in result["message"]

    session_task = (
        db_session.query(SessionModel)
        .filter(SessionModel.id == session.id)
        .first()
        .tasks[0]
    )
    assert session_task.task_id == task.id
    assert session_task.status == TaskStatus.PENDING


def test_resume_session_without_explicit_checkpoint_skips_hollow_checkpoint_replay(
    db_session, monkeypatch
):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="paused", is_active=True)
    task = _make_task(db_session, project, status=TaskStatus.PENDING)
    original_instance_id = "stale-instance-id"
    session.instance_id = original_instance_id
    db_session.commit()
    db_session.refresh(session)

    captured = {"load_resume_checkpoint_calls": []}

    class _FakeCheckpointService:
        def __init__(self, db):
            self.db = db

        def load_resume_checkpoint(self, session_id, checkpoint_name=None):
            captured["load_resume_checkpoint_calls"].append(checkpoint_name)
            return {
                "_requested_checkpoint_name": checkpoint_name,
                "_resolved_checkpoint_name": checkpoint_name or "paused_latest",
                "checkpoint_name": checkpoint_name or "paused_latest",
                "context": {
                    "task_id": task.id,
                    "task_description": "resume from checkpoint",
                },
                "orchestration_state": {},
                "step_results": [],
            }

        def _checkpoint_restore_fidelity(self, data):
            return {
                "score": 30,
                "status": "low",
                "summary": "Checkpoint replay is fragile; important state is missing",
                "present_signals": ["task id", "task description"],
                "warnings": ["missing workspace path", "missing execution plan"],
            }

    def _fake_queue_task_for_session(*, db, session, task_id, timeout_seconds=1800):
        captured["queued_task"] = {
            "session_id": session.id,
            "task_id": task_id,
            "timeout_seconds": timeout_seconds,
        }
        return {"celery_id": "celery-queued-fresh-2"}

    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.CheckpointService",
        _FakeCheckpointService,
    )
    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.queue_task_for_session",
        _fake_queue_task_for_session,
    )

    result = asyncio.run(resume_session_lifecycle(db_session, session.id))

    assert result["status"] == "resumed"
    assert result["resolved_checkpoint_name"] is None
    assert "current workspace" in result["message"]
    assert captured["queued_task"]["task_id"] == task.id
    assert session.instance_id != original_instance_id
    assert captured["load_resume_checkpoint_calls"] == [
        "autosave_latest",
        "autosave_error",
        None,
    ]

    resume_log = (
        db_session.query(LogEntry)
        .filter(
            LogEntry.session_id == session.id,
            LogEntry.message.like("Session resumed:%"),
        )
        .order_by(LogEntry.id.desc())
        .first()
    )
    assert resume_log is not None
    metadata = json.loads(resume_log.log_metadata or "{}")
    assert metadata["dispatch_mode"] == "fresh_requeue"
    assert metadata["resolved_checkpoint_name"] is None


def test_resume_rotates_session_instance_id_before_checkpoint_resume(
    db_session, monkeypatch
):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="paused", is_active=True)
    task = _make_task(db_session, project, status=TaskStatus.PENDING)
    session.instance_id = "old-instance-id"
    db_session.commit()
    db_session.refresh(session)

    captured = {}

    class _FakeCheckpointService:
        def __init__(self, db):
            self.db = db

        def load_resume_checkpoint(self, session_id, checkpoint_name=None):
            return {
                "_requested_checkpoint_name": checkpoint_name,
                "_resolved_checkpoint_name": checkpoint_name or "autosave_latest",
                "checkpoint_name": checkpoint_name or "autosave_latest",
                "context": {
                    "task_id": task.id,
                    "task_description": "resume from checkpoint",
                },
                "orchestration_state": {
                    "plan": [
                        {
                            "step_number": 1,
                            "description": "Resume work",
                            "commands": ["echo resume"],
                            "verification": "test -n resume",
                            "rollback": "true",
                            "expected_files": [],
                        }
                    ],
                    "current_step_index": 0,
                    "execution_results": [],
                },
                "step_results": [],
            }

        def _checkpoint_restore_fidelity(self, data):
            return {
                "score": 80,
                "status": "high",
                "summary": "Checkpoint has strong replay state coverage",
                "present_signals": ["task id", "task description", "execution plan"],
                "warnings": [],
            }

    class _FakeDelayResult:
        id = "resume-celery-id"

    class _FakeWorkerTask:
        @staticmethod
        def delay(**kwargs):
            captured["delay_kwargs"] = kwargs
            return _FakeDelayResult()

    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.CheckpointService",
        _FakeCheckpointService,
    )
    monkeypatch.setattr(
        "app.tasks.worker.execute_orchestration_task",
        _FakeWorkerTask,
    )

    result = asyncio.run(resume_session_lifecycle(db_session, session.id))

    db_session.refresh(session)
    assert result["status"] == "resumed"
    assert session.instance_id != "old-instance-id"
    assert (
        captured["delay_kwargs"]["expected_session_instance_id"] == session.instance_id
    )


def test_resume_requested_checkpoint_does_not_silently_switch_to_fallback(
    db_session, monkeypatch, tmp_path
):
    import json as _json

    project = _make_project(db_session)
    session = _make_session(db_session, project, status="paused", is_active=True)
    task = _make_task(db_session, project, status=TaskStatus.PENDING)

    checkpoint_root = tmp_path / "checkpoints"
    checkpoint_root.mkdir(parents=True, exist_ok=True)

    requested_checkpoint = {
        "session_id": session.id,
        "checkpoint_name": "paused_requested",
        "created_at": "2026-04-28T17:00:00",
        "context": {
            "task_id": task.id,
            "task_description": task.description,
        },
        "orchestration_state": {
            "plan": [
                {
                    "step_number": 1,
                    "description": "old plan",
                    "commands": [],
                    "verification": None,
                    "rollback": None,
                    "expected_files": [],
                }
            ],
            "execution_results": [],
            "current_step_index": 0,
        },
        "current_step_index": 0,
        "step_results": [],
    }
    fallback_checkpoint = {
        "session_id": session.id,
        "checkpoint_name": "autosave_latest",
        "created_at": "2026-04-28T17:05:00",
        "context": {
            "task_id": task.id,
            "task_description": "fallback plan",
        },
        "orchestration_state": {
            "plan": [
                {
                    "step_number": 1,
                    "description": "fallback plan",
                    "commands": [],
                    "verification": None,
                    "rollback": None,
                    "expected_files": [],
                }
            ],
            "execution_results": [],
            "current_step_index": 0,
        },
        "current_step_index": 0,
        "step_results": [],
    }

    (checkpoint_root / f"session_{session.id}_paused_requested.json").write_text(
        _json.dumps(requested_checkpoint), encoding="utf-8"
    )
    (checkpoint_root / f"session_{session.id}_autosave_latest.json").write_text(
        _json.dumps(fallback_checkpoint), encoding="utf-8"
    )

    from app.services.workspace.checkpoint_service import CheckpointService

    original_init = CheckpointService.__init__

    def patched_init(self, db):
        original_init(self, db)
        self.checkpoint_dir = checkpoint_root

    monkeypatch.setattr(
        "app.services.workspace.checkpoint_service.CheckpointService.__init__",
        patched_init,
    )

    class _FakeDelayResult:
        id = "celery-resume-requested"

    class _FakeWorkerTask:
        @staticmethod
        def delay(**kwargs):
            return _FakeDelayResult()

    monkeypatch.setattr(
        "app.tasks.worker.execute_orchestration_task",
        _FakeWorkerTask,
    )

    def _fake_queue_task_for_session(*, db, session, task_id, timeout_seconds=1800):
        return {"celery_id": "celery-queued-fresh-2"}

    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.queue_task_for_session",
        _fake_queue_task_for_session,
    )

    result = asyncio.run(
        resume_session_lifecycle(
            db_session,
            session.id,
            checkpoint_name="paused_requested",
        )
    )

    assert result["resolved_checkpoint_name"] == "paused_requested"
