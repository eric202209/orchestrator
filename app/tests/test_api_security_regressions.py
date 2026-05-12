import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.auth import create_access_token
from app.config import settings
from app.models import (
    PlanningSession,
    Project,
    Session as SessionModel,
    Task,
    TaskStatus,
    User,
)
from app.api.v1.endpoints.auth import generate_keypair
from app.services.auth_rate_limit import clear_auth_rate_limits, enforce_auth_rate_limit
from app.dependencies import get_current_active_user, get_current_user


def test_session_create_starts_pending_and_inactive(authenticated_client, db_session):
    project = Project(name="Session Security Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    response = authenticated_client.post(
        "/api/v1/sessions",
        json={
            "project_id": project.id,
            "name": "Fresh Session",
            "description": "Security regression coverage",
        },
    )

    assert response.status_code == 201
    payload = response.json()
    assert payload["status"] == "pending"
    assert payload["is_active"] is False


def test_mobile_connection_secret_refuses_to_return_raw_secret(authenticated_client):
    response = authenticated_client.get("/api/v1/mobile-admin/connection-secret")

    assert response.status_code == 200
    payload = response.json()
    assert payload["api_key"] is None
    assert payload["header_name"] == "X-OpenClaw-API-Key"
    assert payload["detail"] == "Raw mobile gateway secrets are not returned by the API"


def test_settings_exposes_backend_metadata_without_secrets(authenticated_client):
    response = authenticated_client.get("/api/v1/settings")

    assert response.status_code == 200
    payload = response.json()
    assert payload["system"]["agent_backend"] == settings.ORCHESTRATOR_AGENT_BACKEND
    assert (
        payload["system"]["agent_model_family"]
        == settings.ORCHESTRATOR_AGENT_MODEL_FAMILY
    )
    assert payload["system"]["backend_capabilities"]["supports_planning"] is True
    assert "api_key" not in payload["system"]["backend_capabilities"]


def test_settings_system_update_requires_admin_in_multi_user_deployments(
    authenticated_client, db_session
):
    db_session.add_all(
        [
            User(email="one@example.com", hashed_password="x", is_active=True),
            User(email="two@example.com", hashed_password="y", is_active=True),
        ]
    )
    db_session.commit()

    response = authenticated_client.patch(
        "/api/v1/settings/system",
        json={"workspace_root": "/tmp/secure-root"},
    )

    assert response.status_code == 403
    assert response.json()["detail"] == "Admin privileges are required for this action"


def test_task_update_rejects_unsupported_fields(authenticated_client, db_session):
    project = Project(name="Task Security Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Contract Task",
        status=TaskStatus.PENDING,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    response = authenticated_client.put(
        f"/api/v1/tasks/{task.id}",
        json={"execution_profile": "debug_only"},
    )

    assert response.status_code == 400
    assert response.json()["detail"] == "Unsupported fields: ['execution_profile']"


def test_task_routes_require_authentication(api_client, db_session):
    project = Project(name="Anonymous Access Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Protected Task",
        status=TaskStatus.PENDING,
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    response = api_client.get(f"/api/v1/tasks/{task.id}")

    assert response.status_code == 401


def test_session_routes_require_authentication(api_client, db_session):
    project = Project(name="Anonymous Session Project")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    session = SessionModel(project_id=project.id, name="Protected Session")
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    response = api_client.get(f"/api/v1/sessions/{session.id}")

    assert response.status_code == 401


def test_inactive_user_bearer_token_is_rejected(api_client, db_session):
    inactive_user = User(
        email="inactive-bearer@example.com",
        hashed_password="not-used",
        is_active=False,
    )
    db_session.add(inactive_user)
    db_session.commit()

    token = create_access_token({"sub": inactive_user.email})
    response = api_client.get(
        "/api/v1/sessions",
        headers={"Authorization": f"Bearer {token}"},
    )

    assert response.status_code == 401


def test_project_routes_are_visible_to_authenticated_users(api_app, db_session):
    user_one = User(
        id=101,
        email="owner-one@example.com",
        hashed_password="not-used",
        is_active=True,
    )
    user_two = User(
        id=202,
        email="owner-two@example.com",
        hashed_password="not-used",
        is_active=True,
    )
    own_project = Project(name="Own Project", user_id=user_one.id)
    other_project = Project(name="Other Project", user_id=user_two.id)
    db_session.add_all([user_one, user_two, own_project, other_project])
    db_session.commit()
    db_session.refresh(own_project)
    db_session.refresh(other_project)

    def override_current_user():
        return user_one

    api_app.dependency_overrides[get_current_user] = override_current_user
    api_app.dependency_overrides[get_current_active_user] = override_current_user

    from fastapi.testclient import TestClient

    with TestClient(api_app) as client:
        list_response = client.get("/api/v1/projects")
        other_response = client.get(f"/api/v1/projects/{other_project.id}")

    assert list_response.status_code == 200
    assert [project["id"] for project in list_response.json()] == [
        own_project.id,
        other_project.id,
    ]
    assert other_response.status_code == 200
    assert other_response.json()["id"] == other_project.id


def test_legacy_ownerless_projects_visible_to_authenticated_users(api_app, db_session):
    first_user = User(
        id=101,
        email="primary-local@example.com",
        hashed_password="not-used",
        is_active=True,
    )
    later_user = User(
        id=202,
        email="later-test@example.com",
        hashed_password="not-used",
        is_active=True,
    )
    legacy_project = Project(name="Legacy Ownerless Project", user_id=None)
    later_project = Project(name="Later User Project", user_id=later_user.id)
    db_session.add_all([first_user, later_user, legacy_project, later_project])
    db_session.commit()
    db_session.refresh(legacy_project)
    db_session.refresh(later_project)

    from fastapi.testclient import TestClient

    def override_current_user():
        return first_user

    api_app.dependency_overrides[get_current_user] = override_current_user
    api_app.dependency_overrides[get_current_active_user] = override_current_user
    with TestClient(api_app) as client:
        first_response = client.get("/api/v1/projects")

    def override_later_user():
        return later_user

    api_app.dependency_overrides[get_current_user] = override_later_user
    api_app.dependency_overrides[get_current_active_user] = override_later_user
    with TestClient(api_app) as client:
        later_response = client.get("/api/v1/projects")
        legacy_detail_response = client.get(f"/api/v1/projects/{legacy_project.id}")

    assert first_response.status_code == 200
    assert [project["id"] for project in first_response.json()] == [
        legacy_project.id,
        later_project.id,
    ]
    assert later_response.status_code == 200
    assert [project["id"] for project in later_response.json()] == [
        legacy_project.id,
        later_project.id,
    ]
    assert legacy_detail_response.status_code == 200
    assert legacy_detail_response.json()["id"] == legacy_project.id


def test_session_routes_are_visible_to_authenticated_users(api_app, db_session):
    user_one = User(
        id=301,
        email="session-owner@example.com",
        hashed_password="not-used",
        is_active=True,
    )
    user_two = User(
        id=302,
        email="session-other@example.com",
        hashed_password="not-used",
        is_active=True,
    )
    own_project = Project(name="Own Session Project", user_id=user_one.id)
    other_project = Project(name="Other Session Project", user_id=user_two.id)
    db_session.add_all([user_one, user_two, own_project, other_project])
    db_session.flush()
    own_session = SessionModel(project_id=own_project.id, name="Own Session")
    other_session = SessionModel(project_id=other_project.id, name="Other Session")
    db_session.add_all([own_session, other_session])
    db_session.commit()
    db_session.refresh(own_session)
    db_session.refresh(other_session)

    def override_current_user():
        return user_one

    api_app.dependency_overrides[get_current_user] = override_current_user
    api_app.dependency_overrides[get_current_active_user] = override_current_user

    from fastapi.testclient import TestClient

    with TestClient(api_app) as client:
        list_response = client.get("/api/v1/sessions")
        other_response = client.get(f"/api/v1/sessions/{other_session.id}")

    assert list_response.status_code == 200
    assert {session["id"] for session in list_response.json()} == {
        own_session.id,
        other_session.id,
    }
    assert other_response.status_code == 200
    assert other_response.json()["id"] == other_session.id


def test_global_task_list_is_visible_to_authenticated_users(api_app, db_session):
    user_one = User(
        id=311,
        email="task-owner@example.com",
        hashed_password="not-used",
        is_active=True,
    )
    user_two = User(
        id=312,
        email="task-other@example.com",
        hashed_password="not-used",
        is_active=True,
    )
    own_project = Project(name="Own Task Project", user_id=user_one.id)
    other_project = Project(name="Other Task Project", user_id=user_two.id)
    db_session.add_all([user_one, user_two, own_project, other_project])
    db_session.flush()
    own_task = Task(
        project_id=own_project.id,
        title="Own Task",
        status=TaskStatus.PENDING,
    )
    other_task = Task(
        project_id=other_project.id,
        title="Other Task",
        status=TaskStatus.PENDING,
    )
    db_session.add_all([own_task, other_task])
    db_session.commit()
    db_session.refresh(own_task)
    db_session.refresh(other_task)

    def override_current_user():
        return user_one

    api_app.dependency_overrides[get_current_user] = override_current_user
    api_app.dependency_overrides[get_current_active_user] = override_current_user

    from fastapi.testclient import TestClient

    with TestClient(api_app) as client:
        list_response = client.get("/api/v1/tasks")

    assert list_response.status_code == 200
    assert [task["id"] for task in list_response.json()] == [
        own_task.id,
        other_task.id,
    ]


def test_planning_session_routes_are_visible_to_authenticated_users(
    api_app, db_session
):
    user_one = User(
        id=401,
        email="planning-owner@example.com",
        hashed_password="not-used",
        is_active=True,
    )
    user_two = User(
        id=402,
        email="planning-other@example.com",
        hashed_password="not-used",
        is_active=True,
    )
    own_project = Project(name="Own Planning Project", user_id=user_one.id)
    other_project = Project(name="Other Planning Project", user_id=user_two.id)
    db_session.add_all([user_one, user_two, own_project, other_project])
    db_session.flush()
    own_session = PlanningSession(
        project_id=own_project.id,
        title="Own Plan",
        prompt="Plan mine",
    )
    other_session = PlanningSession(
        project_id=other_project.id,
        title="Other Plan",
        prompt="Plan other",
    )
    db_session.add_all([own_session, other_session])
    db_session.commit()
    db_session.refresh(own_session)
    db_session.refresh(other_session)

    def override_current_user():
        return user_one

    api_app.dependency_overrides[get_current_user] = override_current_user
    api_app.dependency_overrides[get_current_active_user] = override_current_user

    from fastapi.testclient import TestClient

    with TestClient(api_app) as client:
        list_response = client.get("/api/v1/planning/sessions")
        other_response = client.get(f"/api/v1/planning/sessions/{other_session.id}")

    assert list_response.status_code == 200
    assert {session["id"] for session in list_response.json()} == {
        own_session.id,
        other_session.id,
    }
    assert other_response.status_code == 200
    assert other_response.json()["id"] == other_session.id


def test_tool_track_requires_authentication(api_client, db_session):
    project = Project(name="Tool Track Project")
    db_session.add(project)
    db_session.flush()
    session = SessionModel(project_id=project.id, name="Tool Track Session")
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    response = api_client.post(
        f"/api/v1/sessions/{session.id}/tools/track",
        json={
            "execution_id": "exec-1",
            "tool_name": "shell",
            "params": {},
            "result": "ok",
            "success": True,
        },
    )

    assert response.status_code == 401


def _build_request(client_host: str = "127.0.0.1") -> Request:
    scope = {
        "type": "http",
        "method": "POST",
        "path": "/api/v1/auth/tokens",
        "headers": [],
        "client": (client_host, 12345),
        "scheme": "http",
        "server": ("testserver", 80),
        "query_string": b"",
    }
    return Request(scope)


def test_generate_keypair_is_disabled_by_default():
    with pytest.raises(HTTPException) as exc_info:
        generate_keypair()

    assert exc_info.value.status_code == 404
    assert exc_info.value.detail == "Not found"


def test_generate_keypair_can_be_enabled_for_testing():
    settings.ALLOW_TEST_KEYPAIR_ENDPOINT = True

    payload = generate_keypair()
    assert payload["public_key"]
    assert payload["private_key"]


def test_auth_token_endpoint_is_rate_limited():
    settings.AUTH_RATE_LIMIT_MAX_ATTEMPTS = 2
    settings.AUTH_RATE_LIMIT_WINDOW_SECONDS = 60
    clear_auth_rate_limits()

    request = _build_request()
    enforce_auth_rate_limit(request, "tokens")
    enforce_auth_rate_limit(request, "tokens")

    with pytest.raises(HTTPException) as exc_info:
        enforce_auth_rate_limit(request, "tokens")

    assert exc_info.value.status_code == 429
    assert exc_info.value.headers["Retry-After"]


def test_auth_refresh_endpoint_is_rate_limited_per_action_and_client():
    settings.AUTH_RATE_LIMIT_MAX_ATTEMPTS = 1
    settings.AUTH_RATE_LIMIT_WINDOW_SECONDS = 60
    clear_auth_rate_limits()

    first_client = _build_request("127.0.0.1")
    second_client = _build_request("127.0.0.2")

    enforce_auth_rate_limit(first_client, "refresh")
    enforce_auth_rate_limit(second_client, "refresh")
    enforce_auth_rate_limit(first_client, "tokens")

    with pytest.raises(HTTPException) as exc_info:
        enforce_auth_rate_limit(first_client, "refresh")

    assert exc_info.value.status_code == 429
