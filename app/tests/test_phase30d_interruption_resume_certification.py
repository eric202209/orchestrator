"""Phase 30D — Program 1 Interruption / Resume Certification evidence.

This file exercises the EXISTING legacy Task/Session/TaskExecution runtime
recovery path exactly as it stands today:
  - `app.services.session.session_lifecycle_service.recover_stale_running_sessions`
    (Celery-beat-driven stale-session sweep, real PID-liveness ownership check)
  - `app.services.session.session_lifecycle_service.stop_session_lifecycle`
    (operator-initiated interruption)
  - `app.services.session.session_lifecycle_service.resume_session_lifecycle`
    (checkpoint-based resume after interruption)

No recovery, execution, or ownership behavior is modified here. Each test
also writes a certification evidence record (project/session/task/execution
identifier, interruption type/point/timestamps, recovery authority, resumed
state, terminal state, event journal, state snapshot, replay result) to
`docs/roadmap/reports/evidence/phase30d-interruption/` for the Phase 30D
Program 1 report. Evidence files are untracked (docs/roadmap is gitignored),
matching the Phase 30C/30C-V convention.
"""

from __future__ import annotations

import asyncio
import json
import os
import signal
import socket
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

import pytest

from app.models import (
    LogEntry,
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.session.session_lifecycle_service import (
    recover_stale_running_sessions,
    resume_session_lifecycle,
    stop_session_lifecycle,
)

EVIDENCE_ROOT = (
    Path(__file__).resolve().parents[2]
    / "docs"
    / "roadmap"
    / "reports"
    / "evidence"
    / "phase30d-interruption"
)


def _write_evidence(subdir: str, name: str, record: dict[str, Any]) -> None:
    out_dir = EVIDENCE_ROOT / subdir
    out_dir.mkdir(parents=True, exist_ok=True)
    (out_dir / f"{name}.json").write_text(json.dumps(record, indent=2, default=str))


def _event_journal(db, *, session_id: int) -> list[dict[str, Any]]:
    rows = (
        db.query(LogEntry)
        .filter(LogEntry.session_id == session_id)
        .order_by(LogEntry.id.asc())
        .all()
    )
    return [
        {
            "id": row.id,
            "level": row.level,
            "message": row.message,
            "task_id": row.task_id,
            "task_execution_id": row.task_execution_id,
            "log_metadata": row.log_metadata,
        }
        for row in rows
    ]


def _snapshot(db, *, session, task=None, link=None, execution=None) -> dict[str, Any]:
    db.refresh(session)
    out = {
        "session_status": session.status,
        "session_is_active": session.is_active,
    }
    if task is not None:
        db.refresh(task)
        out["task_status"] = (
            task.status.value if hasattr(task.status, "value") else task.status
        )
    if link is not None:
        db.refresh(link)
        out["session_task_status"] = (
            link.status.value if hasattr(link.status, "value") else link.status
        )
    if execution is not None:
        db.refresh(execution)
        out["execution_status"] = (
            execution.status.value
            if hasattr(execution.status, "value")
            else execution.status
        )
        out["execution_completed_at"] = execution.completed_at
    return out


def _make_project(db) -> Project:
    project = Project(name="Phase30D Cert", workspace_path="/tmp/phase30d_cert")
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _make_session(db, project, *, status="running", is_active=True) -> SessionModel:
    session = SessionModel(
        project_id=project.id,
        name="Phase30D Session",
        description="interruption certification",
        status=status,
        is_active=is_active,
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _make_task(db, project, *, status=TaskStatus.RUNNING) -> Task:
    task = Task(
        project_id=project.id,
        title="Phase30D task",
        description="interruption certification task",
        status=status,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _spawn_real_worker() -> subprocess.Popen:
    """Spawn a real OS process to stand in for a worker owning a TaskExecution.

    Using an actual child process (not a fabricated PID) means the
    liveness check in `_execution_owner_is_alive` (os.kill(pid, 0)) is
    exercised against genuine OS process-table state, not a mock.
    """
    return subprocess.Popen(
        [sys.executable, "-c", "import time; time.sleep(120)"],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )


# ── S1: real worker/process interruption (SIGKILL) ─────────────────────────


def test_s1_worker_process_interruption_recovers_and_is_idempotent(
    db_session, monkeypatch
):
    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service._record_failure_knowledge_for_recovery",
        lambda *args, **kwargs: False,
    )
    project = _make_project(db_session)
    session = _make_session(db_session, project)
    task = _make_task(db_session, project)
    link = SessionTask(
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.RUNNING,
        started_at=datetime(2026, 1, 1, 0, 0, 0),
    )

    worker = _spawn_real_worker()
    real_pid = worker.pid
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
        started_at=datetime(2026, 1, 1, 0, 0, 0),
        worker_pid=real_pid,
        worker_hostname=socket.gethostname(),
    )
    db_session.add_all([link, execution])
    db_session.commit()

    before_snapshot = _snapshot(
        db_session, session=session, task=task, link=link, execution=execution
    )

    interruption_ts = datetime.now(UTC).isoformat()
    os.kill(real_pid, signal.SIGKILL)
    worker.wait(timeout=5)
    # Confirm the OS genuinely reclaimed the PID before asserting recovery.
    with pytest.raises(ProcessLookupError):
        os.kill(real_pid, 0)

    recovery_start = time.perf_counter()
    recovered_first = recover_stale_running_sessions(db_session, stale_after_seconds=0)
    recovery_duration_ms = int((time.perf_counter() - recovery_start) * 1000)
    recovery_ts = datetime.now(UTC).isoformat()

    after_snapshot = _snapshot(
        db_session, session=session, task=task, link=link, execution=execution
    )

    assert recovered_first == [
        {
            "session_id": session.id,
            "task_id": task.id,
            "stop_reason": "hard_time_limit_or_worker_killed",
            "knowledge_recorded": False,
        }
    ]
    assert after_snapshot["session_status"] == "stopped"
    assert after_snapshot["session_is_active"] is False
    assert after_snapshot["task_status"] == "pending"
    assert after_snapshot["execution_status"] == "cancelled"
    assert after_snapshot["execution_completed_at"] is not None

    journal_after_first = _event_journal(db_session, session_id=session.id)

    # Idempotency / duplicate-mutation check: re-running the sweep against the
    # now-terminal state must not re-recover, re-transition, or duplicate log
    # entries. This is the deterministic replay pass.
    recovered_second = recover_stale_running_sessions(db_session, stale_after_seconds=0)
    journal_after_second = _event_journal(db_session, session_id=session.id)
    replay_snapshot = _snapshot(
        db_session, session=session, task=task, link=link, execution=execution
    )

    assert recovered_second == []
    assert journal_after_second == journal_after_first
    assert replay_snapshot == after_snapshot

    record = {
        "project": {"id": project.id, "name": project.name},
        "session": {"id": session.id},
        "task": {"id": task.id},
        "execution_identifier": {
            "task_execution_id": execution.id,
            "worker_pid": real_pid,
        },
        "interruption_type": "worker_process_interruption",
        "interruption_point": "task_execution_running_owned_by_real_os_process",
        "interruption_timestamp": interruption_ts,
        "recovery_timestamp": recovery_ts,
        "recovery_duration_ms": recovery_duration_ms,
        "recovery_authority": "session_lifecycle_service.recover_stale_running_sessions"
        " (Celery beat: recover-orphaned-running-sessions, PID-liveness check"
        " via os.kill(pid, 0) in _execution_owner_is_alive)",
        "resumed_execution_state": None,
        "terminal_state": after_snapshot,
        "event_journal": journal_after_first,
        "state_snapshot": {"before": before_snapshot, "after": after_snapshot},
        "replay_result": {
            "second_sweep_recovered": recovered_second,
            "journal_unchanged": journal_after_second == journal_after_first,
            "state_unchanged": replay_snapshot == after_snapshot,
            "match": True,
        },
        "outcome": "RECOVERY_SUCCEEDED",
    }
    _write_evidence("worker-process", "s1_worker_killed_sigkill", record)


# ── S2: negative control — owner still alive must NOT be recovered ─────────


def test_s2_live_worker_owner_is_not_falsely_recovered(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project)
    task = _make_task(db_session, project)
    link = SessionTask(
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.RUNNING,
        started_at=datetime(2026, 1, 1, 0, 0, 0),
    )

    worker = _spawn_real_worker()
    real_pid = worker.pid
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
        started_at=datetime(2026, 1, 1, 0, 0, 0),
        worker_pid=real_pid,
        worker_hostname=socket.gethostname(),
    )
    db_session.add_all([link, execution])
    db_session.commit()

    try:
        before_snapshot = _snapshot(
            db_session, session=session, task=task, link=link, execution=execution
        )
        recovered = recover_stale_running_sessions(db_session, stale_after_seconds=0)
        after_snapshot = _snapshot(
            db_session, session=session, task=task, link=link, execution=execution
        )

        assert recovered == []
        assert after_snapshot == before_snapshot
        assert after_snapshot["session_status"] == "running"
        assert after_snapshot["execution_status"] == "running"

        record = {
            "project": {"id": project.id, "name": project.name},
            "session": {"id": session.id},
            "task": {"id": task.id},
            "execution_identifier": {
                "task_execution_id": execution.id,
                "worker_pid": real_pid,
            },
            "interruption_type": "control_no_interruption_live_owner",
            "interruption_point": "n/a_negative_control",
            "interruption_timestamp": None,
            "recovery_timestamp": datetime.now(UTC).isoformat(),
            "recovery_duration_ms": 0,
            "recovery_authority": "session_lifecycle_service.recover_stale_running_sessions",
            "resumed_execution_state": None,
            "terminal_state": after_snapshot,
            "event_journal": _event_journal(db_session, session_id=session.id),
            "state_snapshot": {"before": before_snapshot, "after": after_snapshot},
            "replay_result": {"match": True, "recovered": recovered},
            "outcome": "CORRECTLY_NOT_RECOVERED",
        }
        _write_evidence("worker-process", "s2_live_owner_not_recovered", record)
    finally:
        worker.kill()
        worker.wait(timeout=5)


# ── S3: service restart — recovery re-attached from a fresh DB session ─────


def test_s3_service_restart_recovery_is_deterministic_across_fresh_sessions(
    db_session, db_session_factory, monkeypatch
):
    """Simulate a service restart: the sweep is re-entered from a brand new
    DB Session object (as a freshly-started process/service would), using
    the same underlying engine/state. Recovery outcome must be identical
    whichever process instance performs the sweep — no process-local state
    on the recovery path.
    """
    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service._record_failure_knowledge_for_recovery",
        lambda *args, **kwargs: False,
    )
    project = _make_project(db_session)
    session = _make_session(db_session, project)
    task = _make_task(db_session, project)
    link = SessionTask(
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.RUNNING,
        started_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    dead_pid_worker = _spawn_real_worker()
    dead_pid = dead_pid_worker.pid
    os.kill(dead_pid, signal.SIGKILL)
    dead_pid_worker.wait(timeout=5)

    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
        started_at=datetime(2026, 1, 1, 0, 0, 0),
        worker_pid=dead_pid,
        worker_hostname=socket.gethostname(),
    )
    db_session.add_all([link, execution])
    db_session.commit()
    session_id = session.id

    interruption_ts = datetime.now(UTC).isoformat()

    # First "service instance": performs the sweep and commits.
    recovered_a = recover_stale_running_sessions(db_session, stale_after_seconds=0)
    snapshot_a = _snapshot(
        db_session, session=session, task=task, link=link, execution=execution
    )
    journal_a = _event_journal(db_session, session_id=session_id)

    # Second "service instance": brand new Session bound to the same engine,
    # standing in for a process restart re-attaching to persisted state.
    fresh_db = db_session_factory()
    try:
        recovered_b = recover_stale_running_sessions(fresh_db, stale_after_seconds=0)
        session_b = fresh_db.get(SessionModel, session_id)
        task_b = fresh_db.get(Task, task.id)
        link_b = fresh_db.get(SessionTask, link.id)
        execution_b = fresh_db.get(TaskExecution, execution.id)
        snapshot_b = _snapshot(
            fresh_db, session=session_b, task=task_b, link=link_b, execution=execution_b
        )
        journal_b = _event_journal(fresh_db, session_id=session_id)
    finally:
        fresh_db.close()

    assert recovered_b == []  # already terminal — no re-recovery, no duplicate mutation
    assert snapshot_b == snapshot_a
    assert journal_b == journal_a

    record = {
        "project": {"id": project.id, "name": project.name},
        "session": {"id": session_id},
        "task": {"id": task.id},
        "execution_identifier": {
            "task_execution_id": execution.id,
            "worker_pid": dead_pid,
        },
        "interruption_type": "service_restart",
        "interruption_point": "sweep_re_entered_from_new_process_local_db_session",
        "interruption_timestamp": interruption_ts,
        "recovery_timestamp": datetime.now(UTC).isoformat(),
        "recovery_duration_ms": None,
        "recovery_authority": "session_lifecycle_service.recover_stale_running_sessions"
        " (no process-local state; authority is persisted DB rows only)",
        "resumed_execution_state": None,
        "terminal_state": snapshot_a,
        "event_journal": journal_a,
        "state_snapshot": {"instance_a": snapshot_a, "instance_b": snapshot_b},
        "replay_result": {
            "recovered_instance_a": recovered_a,
            "recovered_instance_b": recovered_b,
            "match": snapshot_b == snapshot_a and journal_b == journal_a,
        },
        "outcome": "RECOVERY_SUCCEEDED_DETERMINISTIC_ACROSS_RESTART",
    }
    _write_evidence("service-restart", "s3_fresh_session_reattach", record)


# ── S4: operator interruption — explicit stop, idempotent double-stop ──────


def test_s4_operator_interruption_stop_is_idempotent(db_session, monkeypatch):
    project = _make_project(db_session)
    session = _make_session(db_session, project)

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

    before_snapshot = _snapshot(db_session, session=session)
    interruption_ts = datetime.now(UTC).isoformat()

    first = asyncio.run(
        stop_session_lifecycle(
            db_session,
            session.id,
            initiated_by="operator@example.com",
            source="phase30d_certification",
        )
    )
    recovery_ts = datetime.now(UTC).isoformat()
    after_first = _snapshot(db_session, session=session)
    journal_after_first = _event_journal(db_session, session_id=session.id)

    # Duplicate-mutation / re-entrancy check: a second operator stop on an
    # already-stopped session must be a no-op (idempotent terminal state),
    # not a duplicate LogEntry or a second transition.
    second = asyncio.run(
        stop_session_lifecycle(
            db_session,
            session.id,
            initiated_by="operator@example.com",
            source="phase30d_certification",
        )
    )
    after_second = _snapshot(db_session, session=session)
    journal_after_second = _event_journal(db_session, session_id=session.id)

    assert first["status"] == "stopped"
    assert second["status"] == "stopped"
    assert after_first["session_status"] == "stopped"
    assert after_second == after_first
    assert journal_after_second == journal_after_first

    record = {
        "project": {"id": project.id, "name": project.name},
        "session": {"id": session.id},
        "task": None,
        "execution_identifier": None,
        "interruption_type": "operator_interruption",
        "interruption_point": "explicit_stop_session_lifecycle_call",
        "interruption_timestamp": interruption_ts,
        "recovery_timestamp": recovery_ts,
        "recovery_duration_ms": None,
        "recovery_authority": "session_lifecycle_service.stop_session_lifecycle"
        " (operator-initiated, initiated_by=operator@example.com)",
        "resumed_execution_state": None,
        "terminal_state": after_first,
        "event_journal": journal_after_first,
        "state_snapshot": {"before": before_snapshot, "after": after_first},
        "replay_result": {
            "second_stop_result": second,
            "state_unchanged": after_second == after_first,
            "journal_unchanged": journal_after_second == journal_after_first,
            "match": True,
        },
        "outcome": "RECOVERY_SUCCEEDED",
    }
    _write_evidence("operator", "s4_explicit_operator_stop", record)


# ── S5: scheduler-recovered session resumed via checkpoint path ────────────


def test_s5_scheduler_interruption_then_checkpoint_resume(db_session, monkeypatch):
    """A stale session recovered by the scheduler sweep (S1) is later
    resumed by an operator. Verifies exactly one requeue occurs — no
    duplicate dispatch — and the resumed state is coherent.
    """
    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service._record_failure_knowledge_for_recovery",
        lambda *args, **kwargs: False,
    )
    project = _make_project(db_session)
    session = _make_session(db_session, project)
    task = _make_task(db_session, project)
    link = SessionTask(
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.RUNNING,
        started_at=datetime(2026, 1, 1, 0, 0, 0),
    )
    worker = _spawn_real_worker()
    dead_pid = worker.pid
    os.kill(dead_pid, signal.SIGKILL)
    worker.wait(timeout=5)
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
        started_at=datetime(2026, 1, 1, 0, 0, 0),
        worker_pid=dead_pid,
        worker_hostname=socket.gethostname(),
    )
    db_session.add_all([link, execution])
    db_session.commit()

    interruption_ts = datetime.now(UTC).isoformat()
    # Step 1: scheduler sweep recovers the stale session (task -> pending).
    recover_stale_running_sessions(db_session, stale_after_seconds=0)
    db_session.refresh(session)
    db_session.refresh(task)
    session.status = "paused"
    db_session.commit()
    recovery_ts = datetime.now(UTC).isoformat()

    captured: dict[str, Any] = {"calls": 0}

    class _FakeCheckpointService:
        def __init__(self, db):
            self.db = db

        def load_resume_checkpoint(self, session_id, checkpoint_name=None):
            return {
                "_requested_checkpoint_name": checkpoint_name,
                "_resolved_checkpoint_name": "autosave_latest",
                "checkpoint_name": "autosave_latest",
                "context": {"task_id": task.id, "task_description": task.description},
                "orchestration_state": {
                    "plan": [],
                    "current_step_index": 0,
                    "execution_results": [],
                },
                "step_results": [],
            }

        def _checkpoint_restore_fidelity(self, data):
            return {
                "score": 70,
                "status": "high",
                "summary": "ok",
                "present_signals": [],
                "warnings": [],
            }

    def fake_queue_task_for_session(*, db, session, task_id, timeout_seconds=1800):
        captured["calls"] += 1
        captured["task_id"] = task_id
        return {"celery_id": "celery-phase30d-resume"}

    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.CheckpointService",
        _FakeCheckpointService,
    )
    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.queue_task_for_session",
        fake_queue_task_for_session,
    )

    result = asyncio.run(resume_session_lifecycle(db_session, session.id))
    after_snapshot = _snapshot(
        db_session, session=session, task=task, link=link, execution=execution
    )

    assert result["status"] == "resumed"
    assert captured["calls"] == 1
    assert captured["task_id"] == task.id

    record = {
        "project": {"id": project.id, "name": project.name},
        "session": {"id": session.id},
        "task": {"id": task.id},
        "execution_identifier": {
            "task_execution_id": execution.id,
            "worker_pid": dead_pid,
        },
        "interruption_type": "scheduler_interruption_then_resume",
        "interruption_point": "stale_sweep_recovery_followed_by_checkpoint_resume",
        "interruption_timestamp": interruption_ts,
        "recovery_timestamp": recovery_ts,
        "recovery_duration_ms": None,
        "recovery_authority": "recover_stale_running_sessions (scheduler)"
        " -> resume_session_lifecycle (operator/checkpoint)",
        "resumed_execution_state": result,
        "terminal_state": after_snapshot,
        "event_journal": _event_journal(db_session, session_id=session.id),
        "state_snapshot": {"after_resume": after_snapshot},
        "replay_result": {"requeue_call_count": captured["calls"], "match": True},
        "outcome": "RECOVERY_SUCCEEDED_RESUMED_EXACTLY_ONCE",
    }
    _write_evidence("scheduler-resume", "s5_sweep_then_checkpoint_resume", record)
