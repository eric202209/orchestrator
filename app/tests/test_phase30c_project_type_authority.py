"""Phase 30C, Program 3 — Project-Type Authority focused tests.

Covers: derivation from observable repository markers, manual override
precedence, unknown fallback, and evidence integration (the value exposed
via the Project API/response). This is metadata-only: no test here asserts
any effect on execution, planning, orchestration, or provider selection.
"""

from __future__ import annotations

from pathlib import Path

from app.services.project.project_type_authority import (
    KNOWN_PROJECT_TYPES,
    UNKNOWN_PROJECT_TYPE,
    derive_project_type,
    resolve_project_type,
)


# ---------------------------------------------------------------------------
# Derivation
# ---------------------------------------------------------------------------


def test_derive_project_type_python_from_pyproject(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    assert derive_project_type(tmp_path) == "python"


def test_derive_project_type_python_from_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text("pytest\n")
    assert derive_project_type(tmp_path) == "python"


def test_derive_project_type_node(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    assert derive_project_type(tmp_path) == "node"


def test_derive_project_type_mixed(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    (tmp_path / "package.json").write_text("{}")
    assert derive_project_type(tmp_path) == "mixed"


def test_derive_project_type_unknown_when_no_markers(tmp_path):
    assert derive_project_type(tmp_path) == UNKNOWN_PROJECT_TYPE


def test_derive_project_type_unknown_when_directory_missing(tmp_path):
    assert derive_project_type(tmp_path / "does-not-exist") == UNKNOWN_PROJECT_TYPE


def test_derive_project_type_unknown_when_none():
    assert derive_project_type(None) == UNKNOWN_PROJECT_TYPE


def test_derive_project_type_no_confidence_score_returned(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[project]\nname='x'\n")
    result = derive_project_type(tmp_path)
    assert isinstance(result, str)
    assert result in KNOWN_PROJECT_TYPES | {UNKNOWN_PROJECT_TYPE}


# ---------------------------------------------------------------------------
# Manual override
# ---------------------------------------------------------------------------


def test_resolve_project_type_override_always_wins(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    assert resolve_project_type(tmp_path, override="python") == "python"


def test_resolve_project_type_falls_back_to_derivation_when_no_override(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    assert resolve_project_type(tmp_path, override=None) == "node"


def test_resolve_project_type_blank_override_does_not_count_as_set(tmp_path):
    (tmp_path / "package.json").write_text("{}")
    assert resolve_project_type(tmp_path, override="   ") == "node"


def test_resolve_project_type_unknown_when_derivation_unavailable():
    assert resolve_project_type(None, override=None) == UNKNOWN_PROJECT_TYPE


# ---------------------------------------------------------------------------
# Evidence integration — the value the Project API exposes (new records only)
# ---------------------------------------------------------------------------


def test_project_response_exposes_unknown_type_with_no_markers(authenticated_client):
    response = authenticated_client.post(
        "/api/v1/projects",
        json={"name": "Phase30C Project Type Unknown"},
    )
    assert response.status_code == 201
    project = response.json()
    assert project["project_type"] == UNKNOWN_PROJECT_TYPE


def test_project_response_derives_python_type_from_workspace(authenticated_client):
    create_response = authenticated_client.post(
        "/api/v1/projects",
        json={"name": "Phase30C Project Type Python"},
    )
    assert create_response.status_code == 201
    project = create_response.json()
    workspace_dir = Path(project["resolved_workspace_path"])
    workspace_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / "pyproject.toml").write_text("[project]\nname='x'\n")

    detail_response = authenticated_client.get(f"/api/v1/projects/{project['id']}")
    assert detail_response.status_code == 200
    assert detail_response.json()["project_type"] == "python"


def test_project_response_manual_override_wins_over_derivation(authenticated_client):
    create_response = authenticated_client.post(
        "/api/v1/projects",
        json={"name": "Phase30C Project Type Override"},
    )
    assert create_response.status_code == 201
    project = create_response.json()
    workspace_dir = Path(project["resolved_workspace_path"])
    workspace_dir.mkdir(parents=True, exist_ok=True)
    (workspace_dir / "package.json").write_text("{}")

    update_response = authenticated_client.put(
        f"/api/v1/projects/{project['id']}",
        json={"project_type_override": "node-service"},
    )
    assert update_response.status_code == 200
    assert update_response.json()["project_type"] == "node-service"
    assert update_response.json()["project_type_override"] == "node-service"
