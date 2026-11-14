"""Public mutation boundaries: disposable DB and workspace fixtures only."""

import pytest

from app.models import (
    Project,
    Task,
    TaskStatus,
    TaskExecution,
    Session as SessionModel,
    SessionTask,
)
from app.config import settings


def graph(
    db,
    task_status=TaskStatus.PENDING,
    execution_status=None,
    session_status="stopped",
    active=False,
):
    project = Project(name="Integrity", workspace_path="integrity", user_id=1)
    db.add(project)
    db.flush()
    task = Task(
        project_id=project.id,
        title="Original",
        status=task_status,
        error_message="original error",
    )
    db.add(task)
    db.flush()
    session = SessionModel(
        project_id=project.id, name="Integrity", status=session_status, is_active=active
    )
    db.add(session)
    db.flush()
    db.add(SessionTask(task_id=task.id, session_id=session.id, status=task_status))
    if execution_status is not None:
        db.add(
            TaskExecution(
                task_id=task.id,
                session_id=session.id,
                status=execution_status,
                attempt_number=1,
            )
        )
    db.commit()
    return project, task, session


@pytest.mark.parametrize(
    "original,payload",
    [
        (TaskStatus.PENDING, {"status": "done"}),
        (TaskStatus.RUNNING, {"status": "pending"}),
        (TaskStatus.RUNNING, {"error_message": "forged", "title": "Forged"}),
    ],
)
def test_public_lifecycle_edits_rejected(
    authenticated_client, db_session, original, payload
):
    _, task, session = graph(db_session, original, TaskStatus.RUNNING, "running", True)
    response = authenticated_client.put(f"/api/v1/tasks/{task.id}", json=payload)
    assert response.status_code == 422
    db_session.expire_all()
    assert task.status == original
    assert task.title == "Original"
    assert task.error_message == "original error"
    assert session.status == "running" and session.is_active
    assert db_session.query(TaskExecution).one().status == TaskStatus.RUNNING


def test_metadata_edit(authenticated_client, db_session):
    _, task, _ = graph(db_session)
    response = authenticated_client.put(
        f"/api/v1/tasks/{task.id}",
        json={"title": "Updated title", "description": "Edited", "steps": "[]"},
    )
    assert response.status_code == 200
    assert response.json()["description"] == "Edited"
    assert response.json()["status"] == "pending"


@pytest.mark.parametrize(
    "task_status,execution_status,session_status,active",
    [
        (TaskStatus.RUNNING, None, "stopped", False),
        (TaskStatus.PENDING, TaskStatus.PENDING, "stopped", False),
        (TaskStatus.PENDING, TaskStatus.RUNNING, "stopped", False),
        (TaskStatus.DONE, TaskStatus.DONE, "running", True),
    ],
)
@pytest.mark.parametrize("target", ["task", "project"])
def test_active_graph_preserved(
    authenticated_client,
    db_session,
    task_status,
    execution_status,
    session_status,
    active,
    target,
):
    project, task, session = graph(
        db_session, task_status, execution_status, session_status, active
    )
    path = f"tasks/{task.id}" if target == "task" else f"projects/{project.id}"
    response = authenticated_client.delete(f"/api/v1/{path}")
    assert response.status_code == 409
    db_session.expire_all()
    assert db_session.get(Task, task.id) is not None
    assert db_session.query(SessionTask).count() == 1
    assert db_session.query(TaskExecution).count() == int(execution_status is not None)
    assert session.status == session_status and session.is_active == active
    assert project.deleted_at is None


@pytest.mark.parametrize(
    "task_status,execution_status",
    [(TaskStatus.PENDING, None), (TaskStatus.DONE, TaskStatus.DONE)],
)
def test_safe_delete(authenticated_client, db_session, task_status, execution_status):
    _, task, _ = graph(db_session, task_status, execution_status)
    response = authenticated_client.delete(f"/api/v1/tasks/{task.id}")
    assert response.status_code == 204
    assert db_session.query(Task).count() == 0
    assert db_session.query(TaskExecution).count() == 0


@pytest.mark.parametrize("api_only", [False, True])
@pytest.mark.parametrize(
    "path", ["../escape", "nested/../../escape", ".", "./", "\x00bad"]
)
def test_invalid_workspace_no_side_effects(
    authenticated_client,
    db_session,
    isolated_workspace_root,
    monkeypatch,
    path,
    api_only,
):
    monkeypatch.setattr(settings, "DEV_API_ONLY", api_only)
    response = authenticated_client.post(
        "/api/v1/projects", json={"name": "Invalid", "workspace_path": path}
    )
    assert response.status_code == 422
    assert db_session.query(Project).count() == 0
    assert not (isolated_workspace_root.parent / "escape").exists()
    assert not isolated_workspace_root.exists()


def test_absolute_and_symlink_escape(
    authenticated_client, db_session, isolated_workspace_root, tmp_path
):
    root = isolated_workspace_root
    root.mkdir()
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "link").symlink_to(outside, target_is_directory=True)
    for path in [
        str(outside / "new"),
        str(root),
        "link/new",
        str(root / "link" / "new"),
    ]:
        response = authenticated_client.post(
            "/api/v1/projects", json={"name": "Invalid", "workspace_path": path}
        )
        assert response.status_code == 422, path
    assert not (outside / "new").exists()
    assert not (outside / ".gitignore").exists()
    assert db_session.query(Project).count() == 0


@pytest.mark.parametrize(
    "path", ["child", "nested/child", "nested/../child", "", "projects/child"]
)
def test_valid_workspace_and_updates(
    authenticated_client, db_session, isolated_workspace_root, path
):
    response = authenticated_client.post(
        "/api/v1/projects", json={"name": "Valid", "workspace_path": path}
    )
    assert response.status_code == 201
    project_id = response.json()["id"]
    original = response.json()["workspace_path"]
    rejected = authenticated_client.put(
        f"/api/v1/projects/{project_id}", json={"workspace_path": "../escape"}
    )
    assert rejected.status_code == 422
    db_session.expire_all()
    assert db_session.get(Project, project_id).workspace_path == original
    updated = authenticated_client.put(
        f"/api/v1/projects/{project_id}",
        json={"workspace_path": str(isolated_workspace_root / "new" / "child")},
    )
    assert updated.status_code == 200
    assert (isolated_workspace_root / "new" / "child" / ".gitignore").exists()


def test_internal_completion_still_updates_attempt_graph(db_session):
    from app.services.session.session_execution_service import mark_execution_done

    _, task, _ = graph(
        db_session, TaskStatus.RUNNING, TaskStatus.RUNNING, "running", True
    )
    execution = db_session.query(TaskExecution).one()
    link = db_session.query(SessionTask).one()
    mark_execution_done(task=task, session_task_link=link, task_execution=execution)
    db_session.commit()
    assert task.status == execution.status == link.status == TaskStatus.DONE


@pytest.mark.parametrize("dispatch", ["PENDING_BINDING", "READY", "CLAIMED"])
def test_terminal_status_with_live_dispatch_is_not_deletable(
    authenticated_client, db_session, dispatch
):
    from app.services.execution.workflow_lifecycle import (
        configure_task_execution_dispatch,
    )

    _, task, session = graph(db_session, TaskStatus.DONE, TaskStatus.DONE)
    session.instance_id = "test-session"
    execution = db_session.query(TaskExecution).one()
    assert configure_task_execution_dispatch(
        db_session,
        task_execution=execution,
        session=session,
        task=task,
        dispatch_kind="ROOT",
        prompt=task.title,
        timeout_seconds=60,
    )
    execution.dispatch_state = dispatch
    db_session.commit()
    assert authenticated_client.delete(f"/api/v1/tasks/{task.id}").status_code == 409
    assert db_session.query(TaskExecution).count() == 1


@pytest.mark.parametrize("first", ["queue", "delete"])
def test_queue_delete_serialization(tmp_path, first):
    """Two real connections; pause one writer before the other attempts admission."""
    import threading
    from fastapi import HTTPException
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker
    from app.models import Base
    from app.services.tasks.execution import create_task_execution
    from app.services.tasks.task_deletion import delete_task_owned_graph

    engine = create_engine(
        f'sqlite:///{tmp_path / "race.sqlite"}',
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    with factory() as setup:
        _, task, session = graph(setup)
        task_id, session_id = task.id, session.id
    attempted = threading.Event()
    results = []

    def second_writer():
        with factory() as db:
            # Simulate a queue/delete request which already resolved the Task.
            assert db.get(Task, task_id) is not None

            def before_execute(
                conn, cursor, statement, parameters, context, executemany
            ):
                if statement.startswith("UPDATE tasks"):
                    attempted.set()

            event.listen(db.connection(), "before_cursor_execute", before_execute)
            try:
                if first == "delete":
                    create_task_execution(db, task_id=task_id, session_id=session_id)
                else:
                    delete_task_owned_graph(db, [task_id])
                db.commit()
                results.append("unexpected success")
            except HTTPException as exc:
                db.rollback()
                results.append(exc.status_code)

    with factory() as owner:
        if first == "delete":
            delete_task_owned_graph(owner, [task_id])
        else:
            create_task_execution(owner, task_id=task_id, session_id=session_id)
        thread = threading.Thread(target=second_writer)
        thread.start()
        assert attempted.wait(3)
        owner.commit()
        thread.join(6)
        assert not thread.is_alive()
    assert results == [409]
    with factory() as check:
        assert check.query(Task).count() == (first == "queue")
        assert check.query(TaskExecution).count() == (first == "queue")
    engine.dispose()


def test_pending_without_execution_cannot_be_forged(authenticated_client, db_session):
    _, task, _ = graph(db_session)
    for payload in [{"status": "done"}, {"status": None}, {"error_message": None}]:
        assert (
            authenticated_client.put(
                f"/api/v1/tasks/{task.id}", json=payload
            ).status_code
            == 422
        )
    db_session.expire_all()
    assert task.status == TaskStatus.PENDING
    assert db_session.query(TaskExecution).count() == 0


def test_workspace_components_and_symlink_updates(
    authenticated_client, db_session, isolated_workspace_root, tmp_path
):
    root = isolated_workspace_root
    root.mkdir()
    (root / "file").write_text("original")
    (root / "loop").symlink_to(root / "loop")
    outside = tmp_path / "outside"
    outside.mkdir()
    (root / "escape-link").symlink_to(outside, target_is_directory=True)
    response = authenticated_client.post(
        "/api/v1/projects", json={"name": "Valid", "workspace_path": "original"}
    )
    assert response.status_code == 201
    project_id = response.json()["id"]
    for path in [
        "file",
        "file/child",
        "loop/child",
        "escape-link/child",
        str(root),
        str(outside / "new"),
        "\x00bad",
    ]:
        assert (
            authenticated_client.put(
                f"/api/v1/projects/{project_id}", json={"workspace_path": path}
            ).status_code
            == 422
        )
        db_session.expire_all()
        assert db_session.get(Project, project_id).workspace_path == "original"
    assert not (outside / "child").exists()
    assert not (outside / "new").exists()
    assert (root / "file").read_text() == "original"
    (root / "inside-link").symlink_to(root / "original", target_is_directory=True)
    assert (
        authenticated_client.put(
            f"/api/v1/projects/{project_id}", json={"workspace_path": "inside-link"}
        ).status_code
        == 200
    )


def test_purge_cannot_bypass_active_graph(authenticated_client, db_session):
    from datetime import datetime, timedelta, timezone

    project, task, session = graph(
        db_session, TaskStatus.RUNNING, TaskStatus.RUNNING, "running", True
    )
    project.deleted_at = datetime.now(timezone.utc) - timedelta(days=10000)
    db_session.commit()
    assert (
        authenticated_client.delete("/api/v1/projects/purge-soft-deleted").status_code
        == 409
    )
    db_session.expire_all()
    assert db_session.get(Task, task.id) is not None
    assert db_session.query(TaskExecution).count() == 1
    assert db_session.query(SessionTask).count() == 1
    assert session.status == "running" and session.is_active


def test_execution_session_blocks_without_association(authenticated_client, db_session):
    _, task, session = graph(
        db_session, TaskStatus.DONE, TaskStatus.DONE, "recovering", False
    )
    db_session.query(SessionTask).delete()
    db_session.commit()
    assert authenticated_client.delete(f"/api/v1/tasks/{task.id}").status_code == 409
    db_session.expire_all()
    assert db_session.query(TaskExecution).count() == 1
    assert session.status == "recovering"


def test_project_task_creation_needs_no_execution(authenticated_client, db_session):
    project = authenticated_client.post(
        "/api/v1/projects", json={"name": "Metadata only"}
    )
    assert project.status_code == 201
    task = authenticated_client.post(
        "/api/v1/tasks",
        json={"project_id": project.json()["id"], "title": "Pending task"},
    )
    assert task.status_code == 201
    assert task.json()["status"] == "pending"
    assert db_session.query(SessionModel).count() == 0
    assert db_session.query(TaskExecution).count() == 0
