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
