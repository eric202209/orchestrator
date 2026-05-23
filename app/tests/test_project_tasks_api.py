from datetime import UTC, datetime, timedelta

from app.models import Project, Session as SessionModel, SessionTask, Task, TaskStatus
from app.services.task_service import TaskService


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


def test_create_project_task_assigns_sequential_plan_position(
    authenticated_client, db_session
):
    project = Project(
        name="Manual Order", workspace_path="/tmp/manual_order", user_id=1
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    first = authenticated_client.post(
        "/api/v1/tasks",
        json={
            "project_id": project.id,
            "title": "Task 1",
            "description": "First task",
        },
    )
    second = authenticated_client.post(
        "/api/v1/tasks",
        json={
            "project_id": project.id,
            "title": "Task 2",
            "description": "Second task",
        },
    )

    assert first.status_code == 201
    assert second.status_code == 201
    assert first.json()["plan_position"] == 1
    assert second.json()["plan_position"] == 2


def test_create_project_task_preserves_explicit_plan_position(
    authenticated_client, db_session
):
    project = Project(
        name="Manual Explicit Order",
        workspace_path="/tmp/manual_explicit_order",
        user_id=1,
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    response = authenticated_client.post(
        "/api/v1/tasks",
        json={
            "project_id": project.id,
            "title": "Task 10",
            "description": "Explicit task",
            "plan_position": 10,
        },
    )

    assert response.status_code == 201
    assert response.json()["plan_position"] == 10


def test_next_plan_position_starts_at_one_with_legacy_null_position_tasks(db_session):
    project = Project(
        name="Legacy Manual Position", workspace_path="/tmp/legacy_position", user_id=1
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)
    db_session.add(
        Task(
            project_id=project.id,
            title="Legacy task",
            description="Unpositioned task",
            status=TaskStatus.PENDING,
            plan_position=None,
        )
    )
    db_session.commit()

    assert TaskService(db_session).next_plan_position(project.id) == 1


def test_null_position_tasks_still_block_later_manual_tasks(db_session):
    project = Project(
        name="Legacy Manual Order", workspace_path="/tmp/legacy_order", user_id=1
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    older_failed = Task(
        project_id=project.id,
        title="Task 1",
        description="First task",
        status=TaskStatus.FAILED,
        plan_position=None,
        created_at=datetime.now(UTC) - timedelta(minutes=2),
    )
    later_pending = Task(
        project_id=project.id,
        title="Task 2",
        description="Second task",
        status=TaskStatus.PENDING,
        plan_position=None,
        created_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    db_session.add_all([older_failed, later_pending])
    db_session.commit()

    assert TaskService(db_session).get_next_pending_task(project.id) is None

    older_failed.status = TaskStatus.DONE
    db_session.commit()

    assert (
        TaskService(db_session).get_next_pending_task(project.id).id == later_pending.id
    )


def test_cancelled_null_position_tasks_do_not_block_later_manual_tasks(db_session):
    project = Project(
        name="Cancelled Manual Order",
        workspace_path="/tmp/cancelled_order",
        user_id=1,
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    older_cancelled = Task(
        project_id=project.id,
        title="Task 1",
        description="Cancelled task",
        status=TaskStatus.CANCELLED,
        plan_position=None,
        created_at=datetime.now(UTC) - timedelta(minutes=2),
    )
    later_pending = Task(
        project_id=project.id,
        title="Task 2",
        description="Second task",
        status=TaskStatus.PENDING,
        plan_position=None,
        created_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    db_session.add_all([older_cancelled, later_pending])
    db_session.commit()

    assert (
        TaskService(db_session).get_next_pending_task(project.id).id == later_pending.id
    )


def test_legacy_null_position_task_blocks_new_positioned_task(db_session):
    project = Project(
        name="Mixed Manual Order", workspace_path="/tmp/mixed_order", user_id=1
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    older_failed = Task(
        project_id=project.id,
        title="Legacy task",
        description="Unpositioned task",
        status=TaskStatus.FAILED,
        plan_position=None,
        created_at=datetime.now(UTC) - timedelta(minutes=2),
    )
    new_pending = Task(
        project_id=project.id,
        title="New task",
        description="Positioned task",
        status=TaskStatus.PENDING,
        plan_position=1,
        created_at=datetime.now(UTC) - timedelta(minutes=1),
    )
    db_session.add_all([older_failed, new_pending])
    db_session.commit()

    assert TaskService(db_session).get_blocking_prior_tasks(new_pending) == [
        older_failed
    ]
    assert TaskService(db_session).get_next_pending_task(project.id) is None


def test_blocking_prior_tasks_are_scoped_to_same_plan(db_session):
    project = Project(
        name="Plan Scoped Order", workspace_path="/tmp/plan_scoped", user_id=1
    )
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    original_failed = Task(
        project_id=project.id,
        plan_id=10,
        title="Original failed task",
        description="Original plan failed.",
        status=TaskStatus.FAILED,
        plan_position=1,
    )
    recovery_prior_done = Task(
        project_id=project.id,
        plan_id=11,
        title="Recovery diagnosis",
        description="Diagnose failure.",
        status=TaskStatus.DONE,
        plan_position=1,
    )
    recovery_validation = Task(
        project_id=project.id,
        plan_id=11,
        title="Validate recovery path",
        description="Validate the recovery.",
        status=TaskStatus.PENDING,
        plan_position=2,
    )
    db_session.add_all([original_failed, recovery_prior_done, recovery_validation])
    db_session.commit()

    assert TaskService(db_session).get_blocking_prior_tasks(recovery_validation) == []
