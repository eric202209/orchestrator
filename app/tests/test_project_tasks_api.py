from datetime import UTC, datetime, timedelta

from app.models import Project, Session as SessionModel, SessionTask, Task, TaskStatus


def test_project_tasks_include_latest_session_id(authenticated_client, db_session):
    project = Project(name="Tasks API", workspace_path="/tmp/tasks_api", user_id=1)
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Resume-capable task",
        description="test",
        status=TaskStatus.PENDING,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    older_session = SessionModel(
        project_id=project.id,
        name="Older Session",
        description="older",
        status="stopped",
        is_active=False,
        execution_mode="manual",
    )
    newer_session = SessionModel(
        project_id=project.id,
        name="Newer Session",
        description="newer",
        status="paused",
        is_active=True,
        execution_mode="manual",
    )
    db_session.add_all([older_session, newer_session])
    db_session.commit()
    db_session.refresh(older_session)
    db_session.refresh(newer_session)

    db_session.add_all(
        [
            SessionTask(
                session_id=older_session.id,
                task_id=task.id,
                status=TaskStatus.DONE,
                started_at=datetime.now(UTC) - timedelta(hours=1),
            ),
            SessionTask(
                session_id=newer_session.id,
                task_id=task.id,
                status=TaskStatus.PENDING,
                started_at=datetime.now(UTC),
            ),
        ]
    )
    db_session.commit()

    response = authenticated_client.get(f"/api/v1/projects/{project.id}/tasks")

    assert response.status_code == 200
    body = response.json()
    assert len(body) == 1
    assert body[0]["id"] == task.id
    assert body[0]["session_id"] == newer_session.id
