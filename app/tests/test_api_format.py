"""Regression tests for current API response contracts."""

from pathlib import Path

from fastapi.testclient import TestClient

from app.main import app

main_client = TestClient(app)


def test_health_check_contract():
    response = main_client.get("/health")

    assert response.status_code in {200, 503}
    payload = response.json()
    assert payload["status"] in {"healthy", "degraded"}
    assert "checks" in payload
    assert "details" in payload


def test_projects_list_returns_array_contract():
    response = main_client.get("/api/v1/projects")

    assert response.status_code == 401


def test_projects_list_returns_array_contract_when_authenticated(authenticated_client):
    response = authenticated_client.get("/api/v1/projects")

    assert response.status_code == 200
    payload = response.json()
    assert isinstance(payload, list)


def test_project_create_and_detail_contract(authenticated_client):
    create_response = authenticated_client.post(
        "/api/v1/projects",
        json={
            "name": "Regression Test Project",
            "description": "Project contract verification",
        },
    )

    assert create_response.status_code == 201
    project = create_response.json()
    assert project["name"] == "Regression Test Project"
    assert "id" in project
    assert "created_at" in project
    assert Path(project["resolved_workspace_path"]).is_absolute()
    assert project["resolved_workspace_path"].endswith(project["workspace_path"])

    detail_response = authenticated_client.get(f"/api/v1/projects/{project['id']}")
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["id"] == project["id"]
    assert detail["name"] == project["name"]
    assert "description" in detail
    assert "updated_at" in detail


def test_project_create_writes_guard_to_isolated_test_workspace(
    authenticated_client,
    isolated_workspace_root: Path,
):
    create_response = authenticated_client.post(
        "/api/v1/projects",
        json={
            "name": "API Isolated Workspace Project",
            "description": "Verify test workspaces do not leak into the real vault",
        },
    )

    assert create_response.status_code == 201
    project = create_response.json()
    project_root = isolated_workspace_root / project["workspace_path"]
    gitignore = project_root / ".gitignore"

    assert Path(project["resolved_workspace_path"]) == project_root.resolve()
    assert gitignore.exists()
    assert "# BEGIN OpenClaw workspace guard" in gitignore.read_text(encoding="utf-8")


def test_missing_project_returns_detail_message(authenticated_client):
    response = authenticated_client.get("/api/v1/projects/999999")

    assert response.status_code == 404
    payload = response.json()
    assert "detail" in payload


def test_session_create_exposes_model_lane_without_runtime_behavior_change(
    authenticated_client,
):
    project_response = authenticated_client.post(
        "/api/v1/projects",
        json={
            "name": "Model Lane Contract Project",
            "description": "Verify model lane reporting on sessions",
        },
    )
    assert project_response.status_code == 201
    project = project_response.json()

    session_response = authenticated_client.post(
        "/api/v1/sessions",
        json={
            "project_id": project["id"],
            "name": "Model Lane Contract Session",
            "execution_mode": "automatic",
            "default_execution_profile": "full_lifecycle",
        },
    )

    assert session_response.status_code == 201
    session = session_response.json()
    assert session["model_lane_label"] == "local_openclaw"
    assert session["model_lane_metadata"]["label"] == "local_openclaw"
    assert session["model_lane_metadata"]["backend"] == "local_openclaw"
    assert session["model_lane_metadata"]["model_family"] == "local"
    assert session["default_execution_profile"] == "full_lifecycle"
