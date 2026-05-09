"""Regression tests for current API response contracts."""

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

    detail_response = authenticated_client.get(f"/api/v1/projects/{project['id']}")
    assert detail_response.status_code == 200
    detail = detail_response.json()
    assert detail["id"] == project["id"]
    assert detail["name"] == project["name"]
    assert "description" in detail
    assert "updated_at" in detail


def test_missing_project_returns_detail_message(authenticated_client):
    response = authenticated_client.get("/api/v1/projects/999999")

    assert response.status_code == 404
    payload = response.json()
    assert "detail" in payload
