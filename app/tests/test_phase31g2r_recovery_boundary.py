"""Phase 31G-2R recovery ownership and state-graph contract tests."""

from __future__ import annotations

import json
import os
import socket
from datetime import UTC, datetime
from pathlib import Path

from sqlalchemy.exc import OperationalError

from app.models import (
    LogEntry,
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.session.orphan_ownership import ProcessObservation
from app.services.session.recovery_coordinator import (
    RECOVERY_SOURCE_PERIODIC,
    inspect_running_claim,
    inspect_startup_running_executions,
    recover_stale_execution,
)


STALE_AT = datetime(2026, 1, 1, 0, 0, 0)


def _graph(db, *, workspace_path: str = "/tmp/g2r-recovery"):
    project = Project(name="G2R Recovery", workspace_path=workspace_path)
    db.add(project)
    db.flush()
    session = SessionModel(
        project_id=project.id,
        name="G2R session",
        status="running",
        is_active=True,
        started_at=STALE_AT,
    )
    task = Task(
        project_id=project.id,
        title="G2R task",
        status=TaskStatus.RUNNING,
        started_at=STALE_AT,
    )
    db.add_all([session, task])
    db.flush()
    link = SessionTask(
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.RUNNING,
        started_at=STALE_AT,
    )
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
        started_at=STALE_AT,
        worker_hostname=socket.gethostname(),
        worker_pid=4242,
        worker_process_start_identity="g2r-worker-start",
    )
    db.add_all([link, execution])
    db.commit()
    return project, session, task, link, execution


def _dead_owner(monkeypatch):
    monkeypatch.setattr(
        "app.services.session.orphan_ownership.probe_process_identity",
        lambda *_args: ProcessObservation(
            state="not_found", exists=False, process_start_identity=None
        ),
    )


def _live_owner(monkeypatch):
    monkeypatch.setattr(
        "app.services.session.orphan_ownership.probe_process_identity",
        lambda *_args: ProcessObservation(
            state="observed",
            exists=True,
            process_start_identity="g2r-live-start",
        ),
    )


def test_boot_observes_and_defers_eligible_execution(db_session, monkeypatch):
    _dead_owner(monkeypatch)
    _project, session, task, link, execution = _graph(db_session)

    results = inspect_startup_running_executions(
        db_session,
        stale_after_seconds=0,
        now=datetime(2026, 7, 28, tzinfo=UTC),
    )

    assert results[0]["outcome"] == "recovery_requested"
    assert results[0]["source"] == "BOOT_RECOVERY"
    assert results[0]["selected_action"] == "periodic_sweep"
    db_session.refresh(session)
    db_session.refresh(task)
    db_session.refresh(link)
    db_session.refresh(execution)
    assert (session.status, task.status, link.status, execution.status) == (
        "running",
        TaskStatus.RUNNING,
        TaskStatus.RUNNING,
        TaskStatus.RUNNING,
    )


def test_periodic_recovery_owns_mutation_and_is_idempotent(db_session, monkeypatch):
    _dead_owner(monkeypatch)
    _project, session, task, link, execution = _graph(db_session)

    first = recover_stale_execution(
        db_session,
        execution.id,
        stale_after_seconds=0,
        source=RECOVERY_SOURCE_PERIODIC,
        now=datetime(2026, 7, 28, tzinfo=UTC),
        knowledge_recorder=lambda *_args, **_kwargs: False,
    )
    second = recover_stale_execution(
        db_session,
        execution.id,
        stale_after_seconds=0,
        source=RECOVERY_SOURCE_PERIODIC,
        now=datetime(2026, 7, 28, tzinfo=UTC),
        knowledge_recorder=lambda *_args, **_kwargs: True,
    )

    assert first["outcome"] == "recovered"
    assert first["source"] == "PERIODIC_SWEEP"
    assert second["outcome"] == "already_recovered"
    db_session.refresh(session)
    db_session.refresh(task)
    db_session.refresh(link)
    db_session.refresh(execution)
    assert session.status == "stopped"
    assert session.is_active is False
    assert task.status == TaskStatus.PENDING
    assert link.status == TaskStatus.PENDING
    assert execution.status == TaskStatus.CANCELLED
    assert execution.completed_at is not None
    events = (
        db_session.query(LogEntry)
        .filter(LogEntry.task_execution_id == execution.id)
        .all()
    )
    assert any("RECOVERY_COMPLETED" in (event.log_metadata or "") for event in events)
    assert any("already_recovered" in (event.log_metadata or "") for event in events)


def test_duplicate_delivery_preserves_live_original_execution(db_session, monkeypatch):
    _live_owner(monkeypatch)
    _project, session, task, link, execution = _graph(db_session)
    execution.worker_pid = os.getpid()
    execution.worker_process_start_identity = "g2r-live-start"
    execution.heartbeat_at = datetime(2026, 7, 28, tzinfo=UTC)
    db_session.commit()

    result = inspect_running_claim(
        db_session,
        session_id=session.id,
        task_id=task.id,
        execution_id=execution.id,
        stale_after_seconds=2100,
        now=datetime(2026, 7, 28, tzinfo=UTC),
    )

    assert result["outcome"] == "duplicate_delivery_ignored"
    db_session.refresh(session)
    db_session.refresh(task)
    db_session.refresh(link)
    db_session.refresh(execution)
    assert session.status == "running"
    assert task.status == TaskStatus.RUNNING
    assert link.status == TaskStatus.RUNNING
    assert execution.status == TaskStatus.RUNNING


def test_stale_claim_requests_periodic_recovery_without_partial_reset(
    db_session, monkeypatch
):
    _dead_owner(monkeypatch)
    _project, session, task, link, execution = _graph(db_session)

    result = inspect_running_claim(
        db_session,
        session_id=session.id,
        task_id=task.id,
        execution_id=execution.id,
        stale_after_seconds=0,
        now=datetime(2026, 7, 28, tzinfo=UTC),
    )

    assert result["outcome"] == "recovery_requested"
    assert result["selected_action"] == "periodic_sweep"
    db_session.refresh(session)
    db_session.refresh(task)
    db_session.refresh(link)
    db_session.refresh(execution)
    assert session.status == "running"
    assert task.status == TaskStatus.RUNNING
    assert link.status == TaskStatus.RUNNING
    assert execution.status == TaskStatus.RUNNING


def test_ambiguous_claim_is_fail_safe_and_requests_no_mutation(db_session, monkeypatch):
    monkeypatch.setattr(
        "app.services.session.orphan_ownership.probe_process_identity",
        lambda *_args: ProcessObservation(
            state="unavailable", exists=True, process_start_identity=None
        ),
    )
    _project, session, task, link, execution = _graph(db_session)

    result = inspect_running_claim(
        db_session,
        session_id=session.id,
        task_id=task.id,
        execution_id=execution.id,
        stale_after_seconds=0,
        now=datetime(2026, 7, 28, tzinfo=UTC),
    )

    assert result["outcome"] == "duplicate_delivery_ignored"
    assert result["recovery_eligible"] is False
    db_session.refresh(task)
    db_session.refresh(link)
    db_session.refresh(execution)
    assert task.status == TaskStatus.RUNNING
    assert link.status == TaskStatus.RUNNING
    assert execution.status == TaskStatus.RUNNING


def test_contradictory_graph_is_blocked_without_resetting_task(db_session, monkeypatch):
    _dead_owner(monkeypatch)
    _project, session, task, link, execution = _graph(db_session)
    task.status = TaskStatus.PENDING
    db_session.commit()

    result = recover_stale_execution(
        db_session,
        execution.id,
        stale_after_seconds=0,
        source=RECOVERY_SOURCE_PERIODIC,
        now=datetime(2026, 7, 28, tzinfo=UTC),
    )

    assert result["outcome"] == "blocked_contradictory_graph"
    db_session.refresh(session)
    db_session.refresh(task)
    db_session.refresh(link)
    db_session.refresh(execution)
    assert session.status == "running"
    assert task.status == TaskStatus.PENDING
    assert link.status == TaskStatus.RUNNING
    assert execution.status == TaskStatus.RUNNING


def test_recovery_coordinator_does_not_touch_workspace(
    db_session, monkeypatch, tmp_path
):
    _dead_owner(monkeypatch)
    workspace = tmp_path / "project"
    workspace.mkdir()
    marker = workspace / "existing.txt"
    marker.write_text("keep", encoding="utf-8")
    _project, _session, _task, _link, execution = _graph(
        db_session, workspace_path=str(workspace)
    )

    result = recover_stale_execution(
        db_session,
        execution.id,
        stale_after_seconds=0,
        source=RECOVERY_SOURCE_PERIODIC,
        now=datetime(2026, 7, 28, tzinfo=UTC),
    )

    assert result["outcome"] == "recovered"
    assert marker.read_text(encoding="utf-8") == "keep"
    assert sorted(path.name for path in Path(workspace).iterdir()) == ["existing.txt"]


def test_recovery_event_metadata_is_structured(db_session, monkeypatch):
    _dead_owner(monkeypatch)
    _project, _session, _task, _link, execution = _graph(db_session)

    recover_stale_execution(
        db_session,
        execution.id,
        stale_after_seconds=0,
        source=RECOVERY_SOURCE_PERIODIC,
        now=datetime(2026, 7, 28, tzinfo=UTC),
    )

    event = (
        db_session.query(LogEntry)
        .filter(LogEntry.task_execution_id == execution.id)
        .order_by(LogEntry.id.desc())
        .first()
    )
    payload = json.loads(event.log_metadata)
    assert payload["source"] == "PERIODIC_SWEEP"
    assert payload["execution_id"] == execution.id
    assert payload["lock_result"] == "acquired"
    assert payload["mutation_result"] == "committed"
    assert payload["post_mutation_graph"]["execution"]["status"] == "cancelled"


def test_lock_unavailable_fails_safe_without_graph_mutation(db_session, monkeypatch):
    _dead_owner(monkeypatch)
    _project, session, task, link, execution = _graph(db_session)

    def _raise_lock(*_args, **_kwargs):
        raise OperationalError("row lock unavailable", {}, RuntimeError("busy"))

    monkeypatch.setattr(
        "app.services.session.recovery_coordinator._load_graph", _raise_lock
    )
    result = recover_stale_execution(
        db_session,
        execution.id,
        stale_after_seconds=0,
        source=RECOVERY_SOURCE_PERIODIC,
        now=datetime(2026, 7, 28, tzinfo=UTC),
    )

    assert result["outcome"] == "lock_unavailable"
    db_session.refresh(session)
    db_session.refresh(task)
    db_session.refresh(link)
    db_session.refresh(execution)
    assert session.status == "running"
    assert task.status == TaskStatus.RUNNING
    assert link.status == TaskStatus.RUNNING
    assert execution.status == TaskStatus.RUNNING


def test_backend_slot_is_released_once_before_recovery_commit(db_session, monkeypatch):
    _dead_owner(monkeypatch)
    _project, _session, _task, _link, execution = _graph(db_session)
    execution.backend_id = "local_openclaw"
    db_session.commit()
    calls = []
    monkeypatch.setattr(
        "app.services.session.recovery_coordinator.make_redis_client",
        lambda: object(),
    )
    monkeypatch.setattr(
        "app.services.session.recovery_coordinator.release_backend_slot",
        lambda redis, backend_id, session_id: calls.append(
            (redis, backend_id, session_id)
        )
        or True,
    )

    result = recover_stale_execution(
        db_session,
        execution.id,
        stale_after_seconds=0,
        source=RECOVERY_SOURCE_PERIODIC,
        now=datetime(2026, 7, 28, tzinfo=UTC),
    )

    assert result["outcome"] == "recovered"
    assert len(calls) == 1
    assert calls[0][1] == "local_openclaw"


def test_backend_slot_failure_rolls_back_recovery_graph(db_session, monkeypatch):
    _dead_owner(monkeypatch)
    _project, session, task, link, execution = _graph(db_session)
    execution.backend_id = "local_openclaw"
    db_session.commit()
    monkeypatch.setattr(
        "app.services.session.recovery_coordinator.make_redis_client",
        lambda: object(),
    )
    monkeypatch.setattr(
        "app.services.session.recovery_coordinator.release_backend_slot",
        lambda *_args: False,
    )

    result = recover_stale_execution(
        db_session,
        execution.id,
        stale_after_seconds=0,
        source=RECOVERY_SOURCE_PERIODIC,
        now=datetime(2026, 7, 28, tzinfo=UTC),
    )

    assert result["outcome"] == "mutation_failed"
    db_session.refresh(session)
    db_session.refresh(task)
    db_session.refresh(link)
    db_session.refresh(execution)
    assert session.status == "running"
    assert task.status == TaskStatus.RUNNING
    assert link.status == TaskStatus.RUNNING
    assert execution.status == TaskStatus.RUNNING
