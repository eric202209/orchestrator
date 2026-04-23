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
        worker_module.execute_openclaw_task,
        "delay",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("broker down")),
    )

    with pytest.raises(RuntimeError, match="broker down"):
        authenticated_client.post(f"/api/v1/tasks/{task.id}/retry")

    assert db_session.query(SessionModel).count() == 0
    assert db_session.query(SessionTask).count() == 0
    db_session.refresh(task)
    assert task.status == TaskStatus.FAILED
