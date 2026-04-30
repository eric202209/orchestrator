from __future__ import annotations

from datetime import datetime, timezone
from pathlib import Path

from app.models import Project, Session as SessionModel, SessionTask, Task, TaskStatus
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.persistence import read_orchestration_events
from app.services.orchestration.validation.workspace_guard import (
    verify_workspace_contract,
)
from app.services.session.session_runtime_service import queue_task_for_session
from app.services.workspace.project_isolation_service import (
    resolve_project_workspace_path,
)
from app.tasks import worker as worker_module
from app.tasks.worker import _claim_queued_task_for_worker
from app.tasks.worker import _should_reject_stale_dispatch_claim


def test_queue_task_for_session_emits_queued_event_and_keeps_task_pending(
    db_session, monkeypatch, tmp_path
):
    class _FakeDelayResult:
        id = "celery-queued-1"

    class _FakeWorkerTask:
        @staticmethod
        def delay(**_kwargs):
            return _FakeDelayResult()

    monkeypatch.setattr(
        "app.tasks.worker.execute_orchestration_task",
        _FakeWorkerTask,
    )

    project = Project(
        name="Queue Reliability",
        workspace_path=str(tmp_path / "workspace-root"),
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    session = SessionModel(
        project_id=project.id,
        name="Queue Session",
        description="queue test",
        status="pending",
        instance_id="session-instance-1",
    )
    task = Task(
        project_id=project.id,
        title="Queue Task",
        description="write file",
        status=TaskStatus.PENDING,
    )
    db_session.add_all([session, task])
    db_session.commit()
    db_session.refresh(session)
    db_session.refresh(task)

    result = queue_task_for_session(db=db_session, session=session, task_id=task.id)

    db_session.refresh(session)
    db_session.refresh(task)
    session_task = (
        db_session.query(SessionTask)
        .filter(SessionTask.session_id == session.id, SessionTask.task_id == task.id)
        .first()
    )

    assert result["celery_id"] == "celery-queued-1"
    assert session.status == "running"
    assert session.is_active is True
    assert task.status == TaskStatus.PENDING
    assert task.started_at is None
    assert session_task is not None
    assert session_task.status == TaskStatus.PENDING
    assert session_task.started_at is None

    workspace_root = resolve_project_workspace_path(
        project.workspace_path, project.name
    )
    events = read_orchestration_events(Path(workspace_root), session.id, task.id)
    assert events[-1]["event_type"] == EventType.TASK_QUEUED
    assert events[-1]["details"]["session_instance_id"] == session.instance_id


def test_worker_claim_guard_claims_once_and_rejects_duplicate(db_session):
    project = Project(name="Claim Project")
    session = SessionModel(
        project=project,
        name="Claim Session",
        description="claim test",
        status="running",
        is_active=True,
        instance_id="claim-instance-1",
    )
    task = Task(
        project=project,
        title="Claim Task",
        description="claim work",
        status=TaskStatus.PENDING,
    )
    db_session.add_all([project, session, task])
    db_session.commit()
    db_session.refresh(session)
    db_session.refresh(task)

    session_task = SessionTask(
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.PENDING,
    )
    db_session.add(session_task)
    db_session.commit()
    db_session.refresh(session_task)

    claimed, reason, started_at, latest_link = _claim_queued_task_for_worker(
        db=db_session,
        session=session,
        task=task,
        session_task_link=session_task,
        expected_session_instance_id="claim-instance-1",
    )

    assert claimed is True
    assert reason == "claimed"
    assert started_at is not None
    db_session.refresh(task)
    assert task.status == TaskStatus.RUNNING
    assert latest_link is not None
    assert latest_link.status == TaskStatus.RUNNING

    claimed_again, reason_again, _, _ = _claim_queued_task_for_worker(
        db=db_session,
        session=session,
        task=task,
        session_task_link=latest_link,
        expected_session_instance_id="claim-instance-1",
    )
    assert claimed_again is False
    assert reason_again.startswith("task_not_claimable:")


def test_worker_rejects_stale_dispatch_that_already_progressed(tmp_path):
    project_dir = tmp_path / "skillsync"
    project_dir.mkdir()

    queue_event = {"event_id": "queued-1", "timestamp": "2026-04-29T14:59:02+00:00"}
    verify_project_dir = project_dir
    from app.services.orchestration.persistence import append_orchestration_event

    append_orchestration_event(
        project_dir=verify_project_dir,
        session_id=36,
        task_id=2,
        event_type=EventType.TASK_CLAIMED,
        details={"queued_event_id": "queued-1"},
    )

    reason = _should_reject_stale_dispatch_claim(
        dispatch_project_dir=verify_project_dir,
        session_id=36,
        task_id=2,
        queued_event=queue_event,
        queue_latency_seconds=20000.0,
    )

    assert reason == "stale_queue_dispatch_already_progressed"


def test_worker_claim_guard_rejects_stale_session_instance(db_session):
    project = Project(name="Stale Claim Project")
    session = SessionModel(
        project=project,
        name="Stale Session",
        description="stale test",
        status="running",
        is_active=True,
        instance_id="fresh-instance",
    )
    task = Task(
        project=project,
        title="Stale Task",
        description="stale work",
        status=TaskStatus.PENDING,
    )
    db_session.add_all([project, session, task])
    db_session.commit()
    db_session.refresh(session)
    db_session.refresh(task)

    session_task = SessionTask(
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.PENDING,
    )
    db_session.add(session_task)
    db_session.commit()

    claimed, reason, _, _ = _claim_queued_task_for_worker(
        db=db_session,
        session=session,
        task=task,
        session_task_link=session_task,
        expected_session_instance_id="stale-instance",
    )

    db_session.refresh(task)
    db_session.refresh(session_task)
    assert claimed is False
    assert reason == "session_instance_changed"
    assert task.status == TaskStatus.PENDING
    assert session_task.status == TaskStatus.PENDING


def test_worker_coerces_naive_started_at_to_utc():
    naive_started_at = datetime(2026, 4, 29, 22, 20, 56)
    coerced = worker_module._coerce_utc_datetime(naive_started_at)

    assert coerced is not None
    assert coerced.tzinfo == timezone.utc


def test_workspace_contract_detects_runtime_path_mismatch(tmp_path):
    expected_root = tmp_path / "workspace"
    task_dir = expected_root / "task-demo"
    task_dir.mkdir(parents=True)

    result = verify_workspace_contract(
        expected_root=expected_root,
        task_dir=task_dir,
        expected_task_subfolder="task-demo",
        runtime_session_context={
            "project_workspace_path": str(expected_root),
            "task_workspace_path": str(expected_root / "wrong-task"),
        },
    )

    assert result["ok"] is False
    assert result["expected_root"] == str(expected_root.resolve())
    assert result["task_dir"] == str(task_dir.resolve())
    assert "runtime task workspace path" in str(result["reason"])
