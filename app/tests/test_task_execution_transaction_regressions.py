from __future__ import annotations

import pytest

from app.models import Project, Session as SessionModel, SessionTask, Task, TaskStatus


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
        worker_module.execute_orchestration_task,
        "delay",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("broker down")),
    )

    with pytest.raises(RuntimeError, match="broker down"):
        authenticated_client.post(f"/api/v1/tasks/{task.id}/retry")

    assert db_session.query(SessionModel).count() == 0
    assert db_session.query(SessionTask).count() == 0
    db_session.refresh(task)
    assert task.status == TaskStatus.FAILED


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
