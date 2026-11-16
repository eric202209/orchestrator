from __future__ import annotations

from pathlib import Path

from app.models import Project, Task, TaskStatus
from app.services.tasks.service import TaskService


def _seed_workspace_status_fixture(db_session, tmp_path: Path):
    project_root = tmp_path / "dashboard-read-only-project"
    project_root.mkdir()
    project = Project(
        name="Dashboard read-only project",
        workspace_path=str(project_root),
    )
    db_session.add(project)
    db_session.flush()
    task = Task(
        project_id=project.id,
        title="Task with stale derived workspace status",
        description="The dashboard must not reconcile this task while reading it.",
        status=TaskStatus.PENDING,
        workspace_status="isolated",
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(project)
    db_session.refresh(task)
    return project, task


def _task_snapshot(db_session, task_id: int) -> tuple:
    db_session.expire_all()
    task = db_session.query(Task).filter(Task.id == task_id).one()
    project = db_session.query(Project).filter(Project.id == task.project_id).one()
    return (
        task.status,
        task.workspace_status,
        task.updated_at,
        task.task_subfolder,
        project.workspace_path,
    )


def test_dashboard_workspace_overview_get_does_not_persist_task_reconciliation(
    authenticated_client,
    db_session,
    tmp_path: Path,
):
    project, task = _seed_workspace_status_fixture(db_session, tmp_path)
    before = _task_snapshot(db_session, task.id)

    response = authenticated_client.get(
        f"/api/v1/projects/{project.id}/workspace-overview"
    )

    assert response.status_code == 200
    body = response.json()
    assert body["project_id"] == project.id
    assert body["project_name"] == project.name
    assert {
        "counts",
        "baseline",
        "audit",
        "promoted_tasks",
        "pending_change_sets",
        "ready_task_ids",
    } <= body.keys()
    assert body["counts"] == {"not_created": 1}
    assert _task_snapshot(db_session, task.id) == before


def test_repeated_dashboard_workspace_overview_gets_remain_read_only(
    authenticated_client,
    db_session,
    tmp_path: Path,
):
    project, task = _seed_workspace_status_fixture(db_session, tmp_path)
    before = _task_snapshot(db_session, task.id)

    responses = [
        authenticated_client.get(f"/api/v1/projects/{project.id}/workspace-overview")
        for _ in range(3)
    ]

    assert [response.status_code for response in responses] == [200, 200, 200]
    assert [_task_snapshot(db_session, task.id) for _ in responses] == [
        before,
        before,
        before,
    ]


def test_explicit_task_service_query_preserves_workspace_status_synchronization(
    db_session,
    tmp_path: Path,
):
    project, task = _seed_workspace_status_fixture(db_session, tmp_path)

    tasks = TaskService(db_session).get_project_tasks(project.id)

    assert [item.id for item in tasks] == [task.id]
    persisted = db_session.query(Task).filter(Task.id == task.id).one()
    assert persisted.workspace_status == "not_created"
    assert persisted.updated_at is not None
