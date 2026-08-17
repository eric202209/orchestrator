"""POST33-D1-W4-SR1 pre-dispatch serialization matrix.

These tests exercise the existing session queue seam without Celery or a
provider. They assert the durable TaskExecution count, not only worker
non-entry.
"""

from __future__ import annotations

import json
from datetime import datetime, timezone

import pytest
from fastapi import HTTPException

from app.models import (
    Project,
    Session as SessionModel,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.session.session_runtime_service import queue_task_for_session
from app.services.workspace.project_isolation_service import (
    resolve_project_workspace_path,
)
from app.services.workspace.project_mutation_lock import (
    _lock_path_for_project_root,
    project_mutation_lock,
)


def _bypass_binding(monkeypatch):
    monkeypatch.setattr(
        "app.services.session.session_runtime_service.admit_project_openclaw_binding_for_dispatch",
        lambda *args, **kwargs: None,
    )


def _fake_dispatch(monkeypatch):
    calls: list[dict] = []

    class _Result:
        id = "sr1-queued"

    class _Worker:
        @staticmethod
        def delay(**kwargs):
            calls.append(kwargs)
            return _Result()

    monkeypatch.setattr("app.tasks.worker.execute_orchestration_task", _Worker)
    return calls


def _project_with_request(db, tmp_path, name="SR1 Project"):
    project = Project(
        name=name,
        workspace_path=str(tmp_path / name.lower().replace(" ", "-")),
    )
    db.add(project)
    db.commit()
    db.refresh(project)
    session = SessionModel(
        project_id=project.id,
        name="Request Session",
        status="stopped",
        is_active=False,
        instance_id=f"request-{project.id}",
    )
    task = Task(
        project_id=project.id,
        title="Request task",
        description="Perform the bounded request",
        status=TaskStatus.PENDING,
        plan_position=1,
    )
    db.add_all([session, task])
    db.commit()
    db.refresh(session)
    db.refresh(task)
    return project, session, task


def _add_active_owner(db, project, *, status=TaskStatus.RUNNING):
    owner_session = SessionModel(
        project_id=project.id,
        name="Owner Session",
        status="running" if status == TaskStatus.RUNNING else "stopped",
        is_active=status == TaskStatus.RUNNING,
        instance_id=f"owner-{project.id}",
    )
    owner_task = Task(
        project_id=project.id,
        title="Owner task",
        description="Own the project writer",
        status=status,
        plan_position=2,
        task_subfolder="task-owner",
    )
    db.add_all([owner_session, owner_task])
    db.commit()
    db.refresh(owner_session)
    db.refresh(owner_task)
    execution = TaskExecution(
        session_id=owner_session.id,
        task_id=owner_task.id,
        attempt_number=1,
        status=status,
    )
    db.add(execution)
    db.commit()
    return owner_session, owner_task, execution


def test_live_execution_without_lock_blocks_before_task_execution_creation(
    db_session, monkeypatch, tmp_path
):
    _bypass_binding(monkeypatch)
    _fake_dispatch(monkeypatch)
    project, session, task = _project_with_request(db_session, tmp_path)
    _add_active_owner(db_session, project)
    before = db_session.query(TaskExecution).count()

    with pytest.raises(HTTPException) as exc_info:
        queue_task_for_session(db_session, session, task.id)

    assert exc_info.value.status_code == 409
    assert "project_execution_serialization_conflict" in str(exc_info.value.detail)
    assert db_session.query(TaskExecution).count() == before
    assert session.status == "stopped"
    assert task.status == TaskStatus.PENDING
    assert not (
        resolve_project_workspace_path(
            project.workspace_path, project.name, db=db_session
        )
        / "task-request-task"
    ).exists()


def test_stale_lock_without_live_execution_is_reconciled_and_admitted(
    db_session, monkeypatch, tmp_path
):
    _bypass_binding(monkeypatch)
    calls = _fake_dispatch(monkeypatch)
    project, session, task = _project_with_request(db_session, tmp_path)
    project_root = resolve_project_workspace_path(
        project.workspace_path, project.name, db=db_session
    )
    lock_path = _lock_path_for_project_root(project_root)
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path.write_text(
        json.dumps(
            {
                "project_id": project.id,
                "pid": 999999,
                "created_at_epoch": datetime.now(timezone.utc).timestamp(),
            }
        ),
        encoding="utf-8",
    )

    result = queue_task_for_session(db_session, session, task.id)

    assert result["celery_id"] == "sr1-queued"
    assert calls
    assert db_session.query(TaskExecution).count() == 1
    assert not lock_path.exists()


def test_terminal_execution_without_lock_allows_manual_retry(
    db_session, monkeypatch, tmp_path
):
    _bypass_binding(monkeypatch)
    _fake_dispatch(monkeypatch)
    project, session, task = _project_with_request(db_session, tmp_path)
    _add_active_owner(db_session, project, status=TaskStatus.DONE)
    before = db_session.query(TaskExecution).count()

    result = queue_task_for_session(db_session, session, task.id)

    assert result["celery_id"] == "sr1-queued"
    assert db_session.query(TaskExecution).count() == before + 1


def test_active_execution_and_lock_on_other_project_do_not_block_dispatch(
    db_session, monkeypatch, tmp_path
):
    _bypass_binding(monkeypatch)
    _fake_dispatch(monkeypatch)
    owner_project, _, _ = _project_with_request(
        db_session, tmp_path, name="Owner Project"
    )
    _add_active_owner(db_session, owner_project)
    request_project, request_session, request_task = _project_with_request(
        db_session, tmp_path, name="Other Project"
    )
    owner_root = resolve_project_workspace_path(
        owner_project.workspace_path, owner_project.name, db=db_session
    )

    with project_mutation_lock(
        project_id=owner_project.id,
        project_root=owner_root,
        operation="execute_canonical_root_task",
        owner="session:owner:task:owner:execution:1",
    ):
        result = queue_task_for_session(db_session, request_session, request_task.id)

    assert result["celery_id"] == "sr1-queued"
    assert (
        db_session.query(TaskExecution)
        .filter(TaskExecution.task_id == request_task.id)
        .count()
        == 1
    )
