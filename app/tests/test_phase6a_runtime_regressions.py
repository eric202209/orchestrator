"""Regression tests for Phase 6A runtime state and log consistency."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

from app.models import (
    LogEntry,
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.agents.openclaw_service import OpenClawSessionService
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.state.persistence import append_orchestration_event
from app.tasks.worker_support.dispatch import _find_queued_event_for_dispatch
from app.tasks.worker import _emit_dispatch_rejected


def test_rejected_cancelled_dispatch_clears_orphaned_running_task_state(
    db_session, tmp_path: Path
):
    project = Project(name="Dispatch Project", workspace_path=str(tmp_path))
    db_session.add(project)
    db_session.commit()
    session = SessionModel(
        project_id=project.id,
        name="Dispatch Session",
        status="running",
        is_active=True,
        instance_id="session-instance",
    )
    task = Task(
        project_id=project.id,
        title="Dispatch Task",
        description="run",
        status=TaskStatus.RUNNING,
        started_at=datetime.now(UTC).replace(tzinfo=None),
    )
    db_session.add_all([session, task])
    db_session.commit()
    link = SessionTask(
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.RUNNING,
        started_at=task.started_at,
    )
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.CANCELLED,
        started_at=task.started_at,
        completed_at=datetime.now(UTC).replace(tzinfo=None),
    )
    db_session.add_all([link, execution])
    db_session.commit()

    result = _emit_dispatch_rejected(
        reason="task_not_claimable:running",
        log_message="[ORCHESTRATION] Rejected stale or duplicate task dispatch: task_not_claimable:running",
        db=db_session,
        session=session,
        session_id=session.id,
        task_id=task.id,
        task_execution_id=execution.id,
        dispatch_project_dir=tmp_path,
        expected_session_instance_id=None,
        celery_task_id="celery-1",
        queue_latency_seconds=None,
        queued_event=None,
        emit_live=lambda *_args, **_kwargs: None,
    )

    db_session.refresh(task)
    db_session.refresh(link)
    assert result["status"] == "ignored"
    assert task.status != TaskStatus.RUNNING
    assert link.status != TaskStatus.RUNNING


def test_current_rejected_dispatch_terminalizes_the_canonical_graph(
    db_session, tmp_path: Path
):
    project = Project(name="Terminal Dispatch Project", workspace_path=str(tmp_path))
    db_session.add(project)
    db_session.commit()
    session = SessionModel(
        project_id=project.id,
        name="Terminal Dispatch Session",
        status="running",
        is_active=True,
        instance_id="terminal-session-instance",
    )
    task = Task(
        project_id=project.id,
        title="Terminal Dispatch Task",
        description="run",
        status=TaskStatus.PENDING,
    )
    db_session.add_all([session, task])
    db_session.commit()
    link = SessionTask(
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.PENDING,
    )
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.FAILED,
        completed_at=datetime.now(UTC).replace(tzinfo=None),
    )
    db_session.add_all([link, execution])
    db_session.commit()
    queued_event = append_orchestration_event(
        project_dir=tmp_path,
        session_id=session.id,
        task_id=task.id,
        event_type=EventType.TASK_QUEUED,
        details={"task_execution_id": execution.id, "dispatch_attempt": 1},
    )
    assert (
        _find_queued_event_for_dispatch(
            dispatch_project_dir=tmp_path,
            session_id=session.id,
            task_id=task.id,
        )["event_id"]
        == queued_event["event_id"]
    )
    result = _emit_dispatch_rejected(
        reason="stale_queue_dispatch_already_progressed",
        log_message="rejected current stale dispatch",
        db=db_session,
        session=session,
        session_id=session.id,
        task_id=task.id,
        task_execution_id=execution.id,
        dispatch_project_dir=tmp_path,
        expected_session_instance_id=session.instance_id,
        celery_task_id="celery-terminal-1",
        queue_latency_seconds=1200.0,
        queued_event=queued_event,
        emit_live=lambda *_args, **_kwargs: None,
    )

    db_session.refresh(session)
    db_session.refresh(task)
    db_session.refresh(link)
    db_session.refresh(execution)
    assert result["status"] == "ignored"
    assert task.status == TaskStatus.FAILED
    assert task.workspace_status == "not_created"
    assert link.status == TaskStatus.FAILED
    assert execution.status == TaskStatus.FAILED
    assert session.status == "paused"
    assert session.is_active is False

    # Reprocessing the same rejected delivery is lifecycle-idempotent.
    _emit_dispatch_rejected(
        reason="stale_queue_dispatch_already_progressed",
        log_message="rejected current stale dispatch",
        db=db_session,
        session=session,
        session_id=session.id,
        task_id=task.id,
        task_execution_id=execution.id,
        dispatch_project_dir=tmp_path,
        expected_session_instance_id=session.instance_id,
        celery_task_id="celery-terminal-1",
        queue_latency_seconds=1200.0,
        queued_event=queued_event,
        emit_live=lambda *_args, **_kwargs: None,
    )
    db_session.refresh(task)
    db_session.refresh(link)
    db_session.refresh(execution)
    assert task.status == TaskStatus.FAILED
    assert link.status == TaskStatus.FAILED
    assert execution.status == TaskStatus.FAILED


def test_rejected_old_dispatch_does_not_consume_a_newer_retry(
    db_session, tmp_path: Path
):
    project = Project(name="Duplicate Dispatch Project", workspace_path=str(tmp_path))
    db_session.add(project)
    db_session.commit()
    session = SessionModel(
        project_id=project.id,
        name="Duplicate Dispatch Session",
        status="running",
        is_active=True,
        instance_id="duplicate-session-instance",
    )
    task = Task(
        project_id=project.id,
        title="Duplicate Dispatch Task",
        description="run",
        status=TaskStatus.PENDING,
    )
    db_session.add_all([session, task])
    db_session.commit()
    link = SessionTask(
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.PENDING,
    )
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.FAILED,
        completed_at=datetime.now(UTC).replace(tzinfo=None),
    )
    db_session.add_all([link, execution])
    db_session.commit()
    old_event = append_orchestration_event(
        project_dir=tmp_path,
        session_id=session.id,
        task_id=task.id,
        event_type=EventType.TASK_QUEUED,
        details={"dispatch_attempt": 1},
    )
    append_orchestration_event(
        project_dir=tmp_path,
        session_id=session.id,
        task_id=task.id,
        event_type=EventType.TASK_QUEUED,
        details={"dispatch_attempt": 2, "architecture_owned_retry": True},
    )

    _emit_dispatch_rejected(
        reason="stale_queue_dispatch_already_progressed",
        log_message="rejected old duplicate dispatch",
        db=db_session,
        session=session,
        session_id=session.id,
        task_id=task.id,
        task_execution_id=execution.id,
        dispatch_project_dir=tmp_path,
        expected_session_instance_id=session.instance_id,
        celery_task_id="celery-duplicate-1",
        queue_latency_seconds=1200.0,
        queued_event=old_event,
        emit_live=lambda *_args, **_kwargs: None,
    )

    db_session.refresh(session)
    db_session.refresh(task)
    db_session.refresh(link)
    db_session.refresh(execution)
    assert task.status == TaskStatus.PENDING
    assert link.status == TaskStatus.PENDING
    assert execution.status == TaskStatus.FAILED
    assert session.status == "running"
    assert session.is_active is True


def test_rejected_duplicate_of_completed_dispatch_preserves_terminal_state(
    db_session, tmp_path: Path
):
    project = Project(name="Completed Dispatch Project", workspace_path=str(tmp_path))
    db_session.add(project)
    db_session.commit()
    session = SessionModel(
        project_id=project.id,
        name="Completed Dispatch Session",
        status="completed",
        is_active=False,
        instance_id="completed-session-instance",
    )
    task = Task(
        project_id=project.id,
        title="Completed Dispatch Task",
        description="run",
        status=TaskStatus.DONE,
    )
    db_session.add_all([session, task])
    db_session.commit()
    link = SessionTask(
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.DONE,
    )
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.DONE,
        completed_at=datetime.now(UTC).replace(tzinfo=None),
    )
    db_session.add_all([link, execution])
    db_session.commit()
    queued_event = append_orchestration_event(
        project_dir=tmp_path,
        session_id=session.id,
        task_id=task.id,
        event_type=EventType.TASK_QUEUED,
        details={"dispatch_attempt": 1},
    )

    _emit_dispatch_rejected(
        reason="stale_queue_dispatch_already_progressed",
        log_message="rejected completed duplicate dispatch",
        db=db_session,
        session=session,
        session_id=session.id,
        task_id=task.id,
        task_execution_id=execution.id,
        dispatch_project_dir=tmp_path,
        expected_session_instance_id=session.instance_id,
        celery_task_id="celery-completed-1",
        queue_latency_seconds=1200.0,
        queued_event=queued_event,
        emit_live=lambda *_args, **_kwargs: None,
    )

    db_session.refresh(task)
    db_session.refresh(link)
    db_session.refresh(execution)
    db_session.refresh(session)
    assert task.status == TaskStatus.DONE
    assert link.status == TaskStatus.DONE
    assert execution.status == TaskStatus.DONE
    assert session.status == "completed"
    assert session.is_active is False


def test_openclaw_log_entry_carries_task_execution_id(db_session):
    project = Project(name="Log Project", workspace_path="/tmp/log-project")
    db_session.add(project)
    db_session.commit()
    session = SessionModel(project_id=project.id, name="Log Session", status="running")
    task = Task(project_id=project.id, title="Log Task", status=TaskStatus.RUNNING)
    db_session.add_all([session, task])
    db_session.commit()
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
    )
    db_session.add(execution)
    db_session.commit()
    service = OpenClawSessionService(db_session, session.id, task.id)
    service.task_execution_id = execution.id

    service._log_entry(
        "INFO",
        "[PERFORMANCE] Task executed in 1.23s (optimized prompt)",
        commit=True,
    )

    log = db_session.query(LogEntry).filter(LogEntry.session_id == session.id).one()
    assert log.task_execution_id == execution.id
