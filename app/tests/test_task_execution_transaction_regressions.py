from __future__ import annotations

import json

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
from app.services.task_service import TASK_CHANGE_SET_LOG_MESSAGE


class _FakeAsyncResult:
    id = "celery-123"


def _stub_retry_dispatch(monkeypatch, captured_kwargs: dict | None = None):
    from app.tasks import worker as worker_module

    monkeypatch.setattr(
        "app.api.v1.endpoints.tasks.ensure_task_workspace",
        lambda *a, **kw: {
            "workspace_path": "/tmp/retry-project",
            "task_subfolder": None,
            "stored_task_subfolder": "retry-task-1",
            "workspace_scope": "isolated_task_workspace",
        },
    )

    def _fake_delay(**kwargs):
        if captured_kwargs is not None:
            captured_kwargs.clear()
            captured_kwargs.update(kwargs)
        return _FakeAsyncResult()

    monkeypatch.setattr(worker_module.execute_orchestration_task, "delay", _fake_delay)


def test_sync_task_execution_uses_terminal_task_state_over_stale_running_link(
    db_session,
):
    from app.tasks.worker_support.execution_state import (
        _sync_task_execution_from_task_state,
    )

    project = Project(name="Terminal Sync Project")
    db_session.add(project)
    db_session.flush()
    session = SessionModel(
        project_id=project.id,
        name="Terminal Sync Session",
        status="running",
        is_active=True,
    )
    db_session.add(session)
    db_session.flush()
    task = Task(
        project_id=project.id,
        title="Terminal sync task",
        description="fail after debug parse",
        status=TaskStatus.FAILED,
    )
    db_session.add(task)
    db_session.flush()
    session_task = SessionTask(
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.RUNNING,
    )
    db_session.add(session_task)
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
    )
    db_session.add(execution)
    db_session.commit()

    _sync_task_execution_from_task_state(
        db_session,
        execution.id,
        task=task,
        session_task_link=session_task,
    )

    db_session.refresh(execution)
    assert execution.status == TaskStatus.FAILED
    assert execution.completed_at is not None


def test_task_retry_marks_attempt_failed_when_post_commit_dispatch_fails(
    authenticated_client, db_session, monkeypatch
):
    project = Project(name="Dispatch Failure Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Retry me",
        description="retry prompt",
        status=TaskStatus.FAILED,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    from app.tasks import worker as worker_module

    monkeypatch.setattr(
        "app.api.v1.endpoints.tasks.ensure_task_workspace",
        lambda *a, **kw: {
            "workspace_path": "/tmp/rollback-project",
            "task_subfolder": None,
            "stored_task_subfolder": "retry-me-1",
            "workspace_scope": "isolated_task_workspace",
        },
    )
    monkeypatch.setattr(
        worker_module.execute_orchestration_task,
        "delay",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("broker down")),
    )

    with pytest.raises(RuntimeError, match="broker down"):
        authenticated_client.post(f"/api/v1/tasks/{task.id}/retry")

    session = db_session.query(SessionModel).one()
    assert session.status == "stopped"
    assert session.is_active is False
    assert session.stopped_at is not None
    assert db_session.query(SessionTask).count() == 1
    task_execution = db_session.query(TaskExecution).one()
    assert task_execution.status == TaskStatus.FAILED
    assert task_execution.completed_at is not None
    db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
    assert task.error_message == "Failed to dispatch task to worker"


def test_task_retry_commits_records_before_worker_dispatch(
    authenticated_client, db_session, db_session_factory, monkeypatch
):
    project = Project(name="Dispatch Visibility Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Retry after commit",
        description="retry prompt",
        status=TaskStatus.FAILED,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    monkeypatch.setattr(
        "app.api.v1.endpoints.tasks.ensure_task_workspace",
        lambda *a, **kw: {
            "workspace_path": "/tmp/dispatch-visibility-project",
            "task_subfolder": None,
            "stored_task_subfolder": "retry-after-commit",
            "workspace_scope": "isolated_task_workspace",
        },
    )

    seen: dict[str, bool] = {}

    def _fake_delay(**kwargs):
        with db_session_factory() as fresh_db:
            seen["session_visible"] = (
                fresh_db.query(SessionModel)
                .filter(SessionModel.id == kwargs["session_id"])
                .first()
                is not None
            )
            seen["task_visible"] = (
                fresh_db.query(Task).filter(Task.id == kwargs["task_id"]).first()
                is not None
            )
            seen["task_execution_visible"] = (
                fresh_db.query(TaskExecution)
                .filter(TaskExecution.id == kwargs["task_execution_id"])
                .first()
                is not None
            )
        return _FakeAsyncResult()

    from app.tasks import worker as worker_module

    monkeypatch.setattr(worker_module.execute_orchestration_task, "delay", _fake_delay)

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry",
        json={"execution_scope": "new_session"},
    )

    assert response.status_code == 200
    assert seen == {
        "session_visible": True,
        "task_visible": True,
        "task_execution_visible": True,
    }


def test_task_retry_dual_writes_pending_task_execution(
    authenticated_client, db_session, monkeypatch
):
    project = Project(name="Dual Write Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Retry with execution",
        description="retry prompt",
        status=TaskStatus.FAILED,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    captured_kwargs = {}
    _stub_retry_dispatch(monkeypatch, captured_kwargs)

    response = authenticated_client.post(f"/api/v1/tasks/{task.id}/retry")

    assert response.status_code == 200
    payload = response.json()
    task_execution = db_session.query(TaskExecution).one()
    assert payload["task_execution_id"] == task_execution.id
    assert captured_kwargs["task_execution_id"] == task_execution.id
    assert task_execution.session_id == payload["session_id"]
    assert task_execution.task_id == task.id
    assert task_execution.attempt_number == 1
    assert task_execution.status == TaskStatus.PENDING


def test_task_retry_default_reuses_latest_project_session_without_duplicates(
    authenticated_client, db_session, monkeypatch
):
    project = Project(name="Workflow Retry Project")
    db_session.add(project)
    db_session.commit()

    older_session = SessionModel(
        project_id=project.id,
        name="Older workflow",
        status="stopped",
        is_active=False,
    )
    workflow_session = SessionModel(
        project_id=project.id,
        name="Project workflow",
        status="stopped",
        is_active=False,
        instance_id="workflow-instance",
    )
    old_isolated_session = SessionModel(
        project_id=project.id,
        name="Retry without duplicates session",
        status="stopped",
        is_active=False,
        instance_id="orchestrator-task-999-123",
    )
    task = Task(
        project_id=project.id,
        title="Retry without duplicates",
        description="retry prompt",
        status=TaskStatus.FAILED,
    )
    db_session.add_all([older_session, workflow_session, old_isolated_session, task])
    db_session.commit()
    db_session.refresh(workflow_session)
    db_session.refresh(task)

    captured_kwargs = {}
    _stub_retry_dispatch(monkeypatch, captured_kwargs)

    first = authenticated_client.post(f"/api/v1/tasks/{task.id}/retry")
    second = authenticated_client.post(f"/api/v1/tasks/{task.id}/retry")

    assert first.status_code == 200
    assert second.status_code == 200
    assert first.json()["session_id"] == workflow_session.id
    assert second.json()["session_id"] == workflow_session.id
    assert db_session.query(SessionModel).count() == 3
    assert db_session.query(SessionTask).count() == 1
    assert db_session.query(TaskExecution).count() == 2
    assert captured_kwargs["session_id"] == workflow_session.id
    assert captured_kwargs["task_execution_id"] == second.json()["task_execution_id"]


def test_task_retry_uses_requested_session_when_valid(
    authenticated_client, db_session, monkeypatch
):
    project = Project(name="Requested Session Project")
    db_session.add(project)
    db_session.commit()

    requested_session = SessionModel(
        project_id=project.id,
        name="Requested workflow",
        status="stopped",
        is_active=False,
    )
    other_session = SessionModel(
        project_id=project.id,
        name="Other workflow",
        status="stopped",
        is_active=False,
    )
    task = Task(
        project_id=project.id,
        title="Retry requested session",
        description="retry prompt",
        status=TaskStatus.FAILED,
    )
    db_session.add_all([requested_session, other_session, task])
    db_session.commit()
    db_session.refresh(requested_session)
    db_session.refresh(task)

    captured_kwargs = {}
    _stub_retry_dispatch(monkeypatch, captured_kwargs)

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry",
        json={"session_id": requested_session.id},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["session_id"] == requested_session.id
    assert captured_kwargs["session_id"] == requested_session.id
    task_execution = db_session.query(TaskExecution).one()
    assert task_execution.session_id == requested_session.id


def test_task_retry_explicit_new_session_preserves_legacy_isolated_session_creation(
    authenticated_client, db_session, monkeypatch
):
    project = Project(name="Explicit New Session Project")
    db_session.add(project)
    db_session.commit()

    workflow_session = SessionModel(
        project_id=project.id,
        name="Project workflow",
        status="stopped",
        is_active=False,
    )
    task = Task(
        project_id=project.id,
        title="Retry isolated",
        description="retry prompt",
        status=TaskStatus.FAILED,
    )
    db_session.add_all([workflow_session, task])
    db_session.commit()
    db_session.refresh(workflow_session)
    db_session.refresh(task)

    _stub_retry_dispatch(monkeypatch)

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry",
        json={"execution_scope": "new_session"},
    )

    assert response.status_code == 200
    payload = response.json()
    assert payload["session_id"] != workflow_session.id
    assert db_session.query(SessionModel).count() == 2
    new_session = (
        db_session.query(SessionModel)
        .filter(SessionModel.id == payload["session_id"])
        .one()
    )
    assert new_session.name == "Retry isolated session"


def test_task_retry_with_requested_changes_injects_operator_note_and_change_set(
    authenticated_client, db_session, monkeypatch
):
    project = Project(name="Requested Changes Repair Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Repair requested",
        description="original task prompt",
        status=TaskStatus.DONE,
        workspace_status="changes_requested",
        promotion_note="Tighten the README and remove the extra file.",
        task_subfolder="task-repair-requested",
    )
    session = SessionModel(
        project_id=project.id,
        name="Previous run",
        status="stopped",
        is_active=False,
    )
    db_session.add_all([task, session])
    db_session.commit()
    db_session.refresh(task)
    db_session.refresh(session)

    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.DONE,
    )
    db_session.add(execution)
    db_session.commit()
    db_session.refresh(execution)
    db_session.add(
        LogEntry(
            session_id=session.id,
            task_id=task.id,
            task_execution_id=execution.id,
            level="INFO",
            message=TASK_CHANGE_SET_LOG_MESSAGE,
            log_metadata=json.dumps(
                {
                    "schema": "openclaw.task_execution_change_set.v1",
                    "task_id": task.id,
                    "task_execution_id": execution.id,
                    "added_count": 1,
                    "modified_count": 1,
                    "deleted_count": 1,
                    "changed_count": 3,
                    "added_files": ["extra.md"],
                    "modified_files": ["README.md"],
                    "deleted_files": ["old.md"],
                    "warning_flags": ["deleted_files"],
                }
            ),
        )
    )
    db_session.commit()

    captured_kwargs = {}
    _stub_retry_dispatch(monkeypatch, captured_kwargs)
    monkeypatch.setattr(
        "app.services.task_service.TaskService.archive_task_workspace_for_repair_rerun",
        lambda *a, **kw: {"archived": False, "reason": "test"},
    )

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry",
        json={"execution_scope": "new_session", "create_new_session": True},
    )

    assert response.status_code == 200
    prompt = captured_kwargs["prompt"]
    assert "Operator requested changes" in prompt
    assert "Tighten the README" in prompt
    assert "added=1, modified=1, deleted=1" in prompt
    assert "README.md" in prompt
    assert "extra.md" in prompt
    assert "old.md" in prompt


@pytest.mark.asyncio
async def test_task_execute_endpoint_uses_runtime_factory(db_session, monkeypatch):
    from app.api.v1.endpoints.tasks import execute_task_with_runtime

    project = Project(name="Runtime Project", workspace_path="/tmp/runtime-project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Run via runtime",
        description="neutral runtime prompt",
        status=TaskStatus.PENDING,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    calls: list[tuple[str, str, int | None]] = []

    class _FakeRuntime:
        async def create_session(self, task_description: str, context=None) -> str:
            calls.append(("create_session", task_description, None))
            return "agent:main:main"

        async def execute_task(
            self, prompt: str, timeout_seconds: int = 300, log_callback=None
        ) -> dict:
            calls.append(("execute_task", prompt, timeout_seconds))
            return {"status": "completed", "output": "done"}

    monkeypatch.setattr(
        "app.api.v1.endpoints.tasks.create_agent_runtime",
        lambda db, session_id, task_id=None: _FakeRuntime(),
    )

    class _FakeRequest:
        async def json(self):
            return {"prompt": "neutral runtime prompt", "timeout_seconds": 42}

    result = await execute_task_with_runtime(task.id, _FakeRequest(), db_session, None)

    assert result["status"] == "completed"
    assert [call[0] for call in calls] == ["create_session", "execute_task"]
    assert calls[0][1] == "neutral runtime prompt"
    assert calls[1][2] == 600

    db_session.refresh(task)
    assert task.status == TaskStatus.DONE
    task_execution = db_session.query(TaskExecution).one()
    assert task_execution.session_id is not None
    assert task_execution.task_id == task.id
    assert task_execution.attempt_number == 1
    assert task_execution.status == TaskStatus.DONE
    assert task_execution.completed_at is not None


def test_legacy_worker_and_endpoint_aliases_still_exist():
    from app.api.v1.endpoints import tasks as task_endpoints
    from app.tasks import worker as worker_module

    assert (
        worker_module.execute_openclaw_task is worker_module.execute_orchestration_task
    )
    assert (
        task_endpoints.execute_task_with_openclaw
        is task_endpoints.execute_task_with_runtime
    )
