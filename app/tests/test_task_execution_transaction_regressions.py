from __future__ import annotations

import pytest

from app.models import (
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
)


def test_task_retry_rolls_back_session_creation_when_queueing_fails(
    authenticated_client, db_session, monkeypatch
):
    project = Project(name="Rollback Project")
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

    assert db_session.query(SessionModel).count() == 0
    assert db_session.query(SessionTask).count() == 0
    assert db_session.query(TaskExecution).count() == 0
    db_session.refresh(task)
    assert task.status == TaskStatus.FAILED


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

    class _FakeAsyncResult:
        id = "celery-123"

    captured_kwargs = {}

    def _fake_delay(**kwargs):
        captured_kwargs.update(kwargs)
        return _FakeAsyncResult()

    from app.tasks import worker as worker_module

    monkeypatch.setattr(
        "app.api.v1.endpoints.tasks.ensure_task_workspace",
        lambda *a, **kw: {
            "workspace_path": "/tmp/dual-write-project",
            "task_subfolder": None,
            "stored_task_subfolder": "retry-with-execution-1",
            "workspace_scope": "isolated_task_workspace",
        },
    )
    monkeypatch.setattr(worker_module.execute_orchestration_task, "delay", _fake_delay)

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
