"""Provider-free Phase 37 S1 orphan-sweep safety regressions."""

from __future__ import annotations

import socket
from datetime import UTC, datetime, timedelta

from app.models import (
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.session.orphan_ownership import ProcessObservation
from app.services.session.session_lifecycle_service import (
    recover_stale_running_sessions,
)


STALE_AT = datetime(2026, 1, 1, tzinfo=UTC)


def _queued_execution_graph(db_session):
    project = Project(name="S1 queued execution", workspace_path="/tmp/s1-queued")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    session = SessionModel(
        project_id=project.id,
        name="S1 queued session",
        description="provider-free orphan sweep fixture",
        status="running",
        is_active=True,
        execution_mode="manual",
        started_at=STALE_AT,
        updated_at=STALE_AT,
    )
    task = Task(
        project_id=project.id,
        title="Queued task",
        description="queued but not claimed",
        status=TaskStatus.PENDING,
        started_at=None,
    )
    db_session.add_all([session, task])
    db_session.commit()
    db_session.refresh(session)
    db_session.refresh(task)

    link = SessionTask(
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.PENDING,
        started_at=None,
    )
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.PENDING,
        started_at=None,
    )
    db_session.add_all([link, execution])
    db_session.commit()
    return session, task, link, execution


def test_stale_session_does_not_stop_while_pending_execution_is_queued(db_session):
    session, task, link, execution = _queued_execution_graph(db_session)
    decisions = []

    recovered = recover_stale_running_sessions(
        db_session, stale_after_seconds=0, decision_records=decisions
    )

    db_session.refresh(session)
    db_session.refresh(task)
    db_session.refresh(link)
    db_session.refresh(execution)
    assert recovered == []
    assert session.status == "running"
    assert session.is_active is True
    assert task.status == TaskStatus.PENDING
    assert link.status == TaskStatus.PENDING
    assert execution.status == TaskStatus.PENDING
    assert decisions == [
        {
            "execution_id": execution.id,
            "outcome": "pending_execution_protected",
            "ownership_classification": "AMBIGUOUS_FAIL_SAFE",
            "recovery_eligible": False,
            "signals": ["PENDING_EXECUTION", "OWNER_IDENTITY_INCOMPLETE"],
            "decision_reason": (
                "pending execution has no ownership evidence; "
                "session age is insufficient proof of abandonment"
            ),
            "selected_action": "preserve_pending_execution",
            "session_id": session.id,
        }
    ]


def _running_execution_graph(db_session, *, heartbeat_at, worker_pid=4242):
    project = Project(
        name=f"S1 running execution {worker_pid}",
        workspace_path=f"/tmp/s1-running-{worker_pid}",
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    session = SessionModel(
        project_id=project.id,
        name="S1 running session",
        description="provider-free active execution fixture",
        status="running",
        is_active=True,
        execution_mode="manual",
        started_at=STALE_AT,
        updated_at=STALE_AT,
    )
    task = Task(
        project_id=project.id,
        title="Running task",
        description="long-running task",
        status=TaskStatus.RUNNING,
        started_at=STALE_AT,
    )
    db_session.add_all([session, task])
    db_session.commit()
    db_session.refresh(session)
    db_session.refresh(task)

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
        worker_hostname=socket.gethostname(),
        worker_pid=worker_pid,
        worker_process_start_identity=f"boot-a:{worker_pid}",
        heartbeat_at=heartbeat_at,
        started_at=STALE_AT,
    )
    db_session.add_all([link, execution])
    db_session.commit()
    return session, task, link, execution


def test_stale_session_age_does_not_recover_fresh_owned_execution(
    db_session, monkeypatch
):
    now = datetime.now(UTC)
    session, _task, _link, execution = _running_execution_graph(
        db_session, heartbeat_at=now - timedelta(seconds=5)
    )
    monkeypatch.setattr(
        "app.services.session.orphan_ownership.probe_process_identity",
        lambda *_args: ProcessObservation(
            state="observed",
            exists=True,
            process_start_identity=execution.worker_process_start_identity,
        ),
    )
    decisions = []

    recovered = recover_stale_running_sessions(
        db_session, stale_after_seconds=30, decision_records=decisions
    )

    db_session.refresh(session)
    db_session.refresh(execution)
    assert recovered == []
    assert session.status == "running"
    assert execution.status == TaskStatus.RUNNING
    assert decisions[0]["ownership_classification"] == "LIVE_OWNER_CONFIRMED"
    assert decisions[0]["recovery_eligible"] is False


def test_abandoned_execution_recovery_is_consistent_and_idempotent(
    db_session, monkeypatch
):
    session, task, link, execution = _running_execution_graph(
        db_session, heartbeat_at=STALE_AT
    )
    monkeypatch.setattr(
        "app.services.session.orphan_ownership.probe_process_identity",
        lambda *_args: ProcessObservation(
            state="not_found", exists=False, process_start_identity=None
        ),
    )
    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service._record_failure_knowledge_for_recovery",
        lambda *_args, **_kwargs: False,
    )

    first = recover_stale_running_sessions(db_session, stale_after_seconds=30)
    second = recover_stale_running_sessions(db_session, stale_after_seconds=30)

    db_session.refresh(session)
    db_session.refresh(task)
    db_session.refresh(link)
    db_session.refresh(execution)
    assert len(first) == 1
    assert second == []
    assert session.status == "stopped"
    assert session.is_active is False
    assert task.status == TaskStatus.PENDING
    assert link.status == TaskStatus.PENDING
    assert execution.status == TaskStatus.CANCELLED


def test_capacity_retry_pending_session_is_outside_orphan_sweep(db_session):
    session, task, link, execution = _queued_execution_graph(db_session)
    session.status = "retry_pending"
    session.continuation_task_id = task.id
    session.continuation_kind = "backend_capacity"
    session.continuation_retry_count = 3
    db_session.commit()

    recovered = recover_stale_running_sessions(db_session, stale_after_seconds=0)

    db_session.refresh(session)
    db_session.refresh(task)
    db_session.refresh(link)
    db_session.refresh(execution)
    assert recovered == []
    assert session.status == "retry_pending"
    assert task.status == TaskStatus.PENDING
    assert link.status == TaskStatus.PENDING
    assert execution.status == TaskStatus.PENDING


def test_orphan_recovery_does_not_cross_project_boundary(db_session, monkeypatch):
    stale_session, _stale_task, _stale_link, stale_execution = _running_execution_graph(
        db_session, heartbeat_at=STALE_AT, worker_pid=4242
    )
    live_session, _live_task, _live_link, live_execution = _running_execution_graph(
        db_session, heartbeat_at=STALE_AT, worker_pid=4243
    )

    def _probe(_hostname, pid):
        if pid == stale_execution.worker_pid:
            return ProcessObservation(
                state="not_found", exists=False, process_start_identity=None
            )
        return ProcessObservation(
            state="observed",
            exists=True,
            process_start_identity=live_execution.worker_process_start_identity,
        )

    monkeypatch.setattr(
        "app.services.session.orphan_ownership.probe_process_identity", _probe
    )
    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service._record_failure_knowledge_for_recovery",
        lambda *_args, **_kwargs: False,
    )

    recovered = recover_stale_running_sessions(db_session, stale_after_seconds=30)

    db_session.refresh(stale_session)
    db_session.refresh(live_session)
    assert [item["session_id"] for item in recovered] == [stale_session.id]
    assert stale_session.status == "stopped"
    assert live_session.status == "running"


def test_recovery_does_not_remove_credential_bearing_binding(
    db_session, monkeypatch, tmp_path
):
    binding = tmp_path / "binding"
    binding.mkdir()
    credential_copy = binding / "agent" / "auth-profiles.json"
    credential_copy.parent.mkdir()
    credential_copy.write_text("synthetic-sentinel", encoding="utf-8")
    monkeypatch.setenv("ORCHESTRATOR_OPENCLAW_BINDING_ROOT", str(tmp_path))

    _session, _task, _link, _execution = _running_execution_graph(
        db_session, heartbeat_at=STALE_AT
    )
    monkeypatch.setattr(
        "app.services.session.orphan_ownership.probe_process_identity",
        lambda *_args: ProcessObservation(
            state="not_found", exists=False, process_start_identity=None
        ),
    )
    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service._record_failure_knowledge_for_recovery",
        lambda *_args, **_kwargs: False,
    )

    recover_stale_running_sessions(db_session, stale_after_seconds=30)

    assert credential_copy.read_text(encoding="utf-8") == "synthetic-sentinel"
