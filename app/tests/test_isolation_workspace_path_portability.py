"""Workspace-path portability for the project isolation update endpoint.

The endpoint previously validated against a hard-coded
``/root/.openclaw/workspace/vault`` base, which bypassed the configured
workspace-root resolver. That base was also one level above the resolved
project root, so a normal project-root-relative ``workspace_path`` resolved to
a directory that does not exist and the request failed with HTTP 400.
"""

from pathlib import Path

from app.models import Project

ENDPOINT = "/api/v1/projects/{project_id}/isolation/update-workspace"


def _project(db_session, name: str = "Portability Project") -> Project:
    project = Project(name=name, user_id=1)
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)
    return project


def test_relative_workspace_path_resolves_under_configured_root(
    authenticated_client, db_session, isolated_workspace_root
):
    project = _project(db_session)
    target = isolated_workspace_root / "portable-project"
    target.mkdir(parents=True)

    response = authenticated_client.post(
        ENDPOINT.format(project_id=project.id),
        json={"workspace_path": "portable-project"},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["workspace_path"] == "portable-project"
    assert Path(body["full_path"]) == target.resolve()


def test_absolute_path_inside_workspace_root_is_stored_relative(
    authenticated_client, db_session, isolated_workspace_root
):
    """Matches the normalization the canonical project-update route applies."""
    project = _project(db_session, name="Absolute Inside Project")
    target = isolated_workspace_root / "inside-project"
    target.mkdir(parents=True)

    response = authenticated_client.post(
        ENDPOINT.format(project_id=project.id),
        json={"workspace_path": str(target)},
    )

    assert response.status_code == 200, response.text
    body = response.json()
    assert body["workspace_path"] == "inside-project"
    assert Path(body["full_path"]) == target.resolve()


def test_absolute_stored_workspace_path_still_resolves_absolutely(db_session, tmp_path):
    """The read-side resolver keeps an already-absolute stored path as-is."""
    from app.services.workspace.project_isolation_service import (
        resolve_project_workspace_path,
    )

    elsewhere = tmp_path / "absolute-elsewhere"
    elsewhere.mkdir()

    resolved = resolve_project_workspace_path(
        str(elsewhere), "Absolute Stored Project", db=db_session
    )

    assert resolved == elsewhere.resolve()


def test_missing_directory_is_rejected(authenticated_client, db_session):
    project = _project(db_session, name="Missing Dir Project")

    response = authenticated_client.post(
        ENDPOINT.format(project_id=project.id),
        json={"workspace_path": "does-not-exist"},
    )

    assert response.status_code == 400
    assert "does not exist" in response.json()["detail"]


def test_file_target_is_rejected(
    authenticated_client, db_session, isolated_workspace_root
):
    project = _project(db_session, name="File Target Project")
    isolated_workspace_root.mkdir(parents=True, exist_ok=True)
    (isolated_workspace_root / "a-file").write_text("not a directory")

    response = authenticated_client.post(
        ENDPOINT.format(project_id=project.id),
        json={"workspace_path": "a-file"},
    )

    assert response.status_code == 400
    assert "not a directory" in response.json()["detail"]


def test_unknown_project_is_rejected(authenticated_client, db_session):
    response = authenticated_client.post(
        ENDPOINT.format(project_id=987654),
        json={"workspace_path": "anything"},
    )

    assert response.status_code == 404


def test_endpoint_module_has_no_machine_specific_workspace_base():
    source = Path("app/api/v1/endpoints/isolation.py").read_text(encoding="utf-8")
    assert "/root/.openclaw" not in source
    assert "/root/.orchestrator" not in source
