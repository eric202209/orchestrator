from __future__ import annotations

from datetime import UTC, datetime

import pytest
from fastapi import HTTPException
from fastapi.testclient import TestClient

from app.dependencies import get_current_user
from app.models import Project, Session as SessionModel, Task, User
from app.services.session.session_runtime_service import ensure_task_workspace


def test_create_session_rejects_inactive_user(api_app, db_session):
    project = Project(name="Inactive User Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    inactive_user = User(
        id=99,
        email="inactive@example.com",
        hashed_password="not-used",
        is_active=False,
    )

    def override_current_user():
        return inactive_user

    api_app.dependency_overrides[get_current_user] = override_current_user

    with TestClient(api_app) as client:
        response = client.post(
            "/api/v1/sessions",
            json={"project_id": project.id, "name": "Unauthorized session"},
        )

    assert response.status_code == 403
    assert db_session.query(SessionModel).count() == 0


def test_create_session_rejects_soft_deleted_project(authenticated_client, db_session):
    project = Project(name="Deleted Project", deleted_at=datetime.now(UTC))
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    response = authenticated_client.post(
        "/api/v1/sessions",
        json={"project_id": project.id, "name": "Deleted project session"},
    )

    assert response.status_code == 404
    assert db_session.query(SessionModel).count() == 0


def test_task_subfolder_cannot_escape_project_workspace(db_session, tmp_path):
    project_workspace = tmp_path / "project"
    project = Project(name="Traversal Project", workspace_path=str(project_workspace))
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    session = SessionModel(project_id=project.id, name="Traversal Session")
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    task = Task(
        project_id=project.id,
        title="Traversal Task",
        description="try to escape",
        task_subfolder="../outside",
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    with pytest.raises(HTTPException) as exc_info:
        ensure_task_workspace(db_session, session, task.id)

    assert exc_info.value.status_code == 400
    assert not (tmp_path / "outside").exists()
