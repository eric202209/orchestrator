import pytest
from fastapi import HTTPException
from starlette.requests import Request

from app.config import settings
from app.models import Project, Session as SessionModel, Task, TaskStatus, User
from app.api.v1.endpoints.auth import generate_keypair
from app.services.auth_rate_limit import clear_auth_rate_limits, enforce_auth_rate_limit


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
