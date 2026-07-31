"""Focused regression coverage for Phase 22B-1R1 workspace admission."""

from __future__ import annotations

import json
import subprocess
from datetime import datetime, timezone
from pathlib import Path

import pytest

from app.models import Project
from app.services.orchestration.execution.executor_workspace_binding import (
    ExecutorWorkspaceBindingError,
    bind_openclaw_workspace,
)
from app.services.orchestration.execution.runtime_context import RuntimeExecutorContext
from app.services.workspace.workspace_admission import (
    WorkspaceAdmissionError,
    active_workspace_owners,
    admit_dogfood_workspace,
    assert_unique_active_workspace_owner,
    canonical_workspace_realpath,
)


def _git(workspace: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(workspace), *args], check=True, capture_output=True
    )


def _clean_remote_workspace(tmp_path: Path) -> Path:
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    _git(workspace, "init")
    _git(workspace, "remote", "add", "origin", "https://example.invalid/workspace.git")
    return workspace


def _openclaw_config(path: Path, workspace: Path, *agent_ids: str) -> Path:
    path.write_text(
        json.dumps(
            {
                "agents": {
                    "list": [
                        {"id": agent_id, "workspace": str(workspace)}
                        for agent_id in agent_ids
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    return path


def test_canonical_workspace_realpath_collapses_aliases(tmp_path: Path):
    workspace = tmp_path / "workspace"
    workspace.mkdir()
    alias = tmp_path / "alias"
    alias.symlink_to(workspace, target_is_directory=True)

    assert (
        canonical_workspace_realpath(workspace / ".." / "workspace")
        == workspace.resolve()
    )
    assert canonical_workspace_realpath(str(workspace) + "/") == workspace.resolve()
    assert canonical_workspace_realpath(alias) == workspace.resolve()


def test_active_duplicate_mapping_fails_closed_and_soft_deleted_history_does_not(
    db_session, tmp_path: Path
):
    workspace = _clean_remote_workspace(tmp_path)
    canonical = Project(name="canonical", workspace_path=str(workspace))
    duplicate = Project(name="duplicate", workspace_path=str(workspace / "."))
    historical = Project(
        name="historical",
        workspace_path=str(workspace),
        deleted_at=datetime.now(timezone.utc),
    )
    db_session.add_all([canonical, duplicate, historical])
    db_session.commit()

    assert [p.id for p in active_workspace_owners(db_session, workspace)] == [
        canonical.id,
        duplicate.id,
    ]
    with pytest.raises(WorkspaceAdmissionError, match="workspace_mapping_ambiguous"):
        assert_unique_active_workspace_owner(db_session, canonical)

    duplicate.deleted_at = datetime.now(timezone.utc)
    db_session.commit()
    assert (
        assert_unique_active_workspace_owner(db_session, canonical)
        == workspace.resolve()
    )


def test_retired_duplicate_releases_active_workspace_ownership_without_history_loss(
    db_session, tmp_path: Path
):
    workspace = _clean_remote_workspace(tmp_path)
    canonical = Project(name="canonical", workspace_path=str(workspace))
    duplicate = Project(name="retired duplicate", workspace_path=str(workspace))
    db_session.add_all([canonical, duplicate])
    db_session.commit()

    duplicate.retired_at = datetime.now(timezone.utc)
    duplicate.retirement_reason = "legacy_duplicate_workspace_owner"
    db_session.commit()

    assert [p.id for p in active_workspace_owners(db_session, workspace)] == [
        canonical.id
    ]
    assert (
        assert_unique_active_workspace_owner(db_session, canonical)
        == workspace.resolve()
    )


def test_dogfood_admission_requires_clean_remote_and_one_matching_agent(
    db_session, tmp_path: Path
):
    workspace = _clean_remote_workspace(tmp_path)
    project = Project(name="eligible", workspace_path=str(workspace))
    db_session.add(project)
    db_session.commit()
    config = _openclaw_config(tmp_path / "openclaw.json", workspace, "eligible-agent")

    admitted = admit_dogfood_workspace(db_session, project, openclaw_config_path=config)
    assert admitted.workspace == str(workspace.resolve())
    assert admitted.openclaw_agent_id == "eligible-agent"

    (workspace / "untracked.txt").write_text("dirty\n", encoding="utf-8")
    with pytest.raises(WorkspaceAdmissionError) as exc_info:
        admit_dogfood_workspace(db_session, project, openclaw_config_path=config)
    assert exc_info.value.category == "workspace_dirty"
    assert "?? untracked.txt" in exc_info.value.paths


def test_session_dogfood_admission_fails_before_session_row(
    authenticated_client, db_session, tmp_path: Path
):
    workspace = _clean_remote_workspace(tmp_path)
    first = Project(name="first", workspace_path=str(workspace))
    second = Project(name="second", workspace_path=str(workspace))
    db_session.add_all([first, second])
    db_session.commit()

    response = authenticated_client.post(
        "/api/v1/sessions",
        json={
            "project_id": first.id,
            "name": "must-not-create",
            "dogfood_admission": True,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["category"] == "workspace_mapping_ambiguous"
    from app.models import Session as SessionModel

    assert (
        db_session.query(SessionModel)
        .filter(SessionModel.project_id == first.id)
        .count()
        == 0
    )


def test_dogfood_admission_rejects_non_git_and_remote_less_workspaces(
    db_session, tmp_path: Path
):
    workspace = tmp_path / "not-git"
    workspace.mkdir()
    project = Project(name="not-git", workspace_path=str(workspace))
    db_session.add(project)
    db_session.commit()
    config = _openclaw_config(tmp_path / "openclaw.json", workspace, "agent")

    with pytest.raises(WorkspaceAdmissionError) as exc_info:
        admit_dogfood_workspace(db_session, project, openclaw_config_path=config)
    assert exc_info.value.category == "workspace_not_git"

    _git(workspace, "init")
    with pytest.raises(WorkspaceAdmissionError) as exc_info:
        admit_dogfood_workspace(db_session, project, openclaw_config_path=config)
    assert exc_info.value.category == "workspace_remote_missing"


def test_openclaw_binding_rejects_duplicate_exact_workspace_agents(tmp_path: Path):
    workspace = _clean_remote_workspace(tmp_path)
    config = _openclaw_config(tmp_path / "openclaw.json", workspace, "first", "second")
    runtime = tmp_path / "runtime"
    runtime.mkdir()
    context = RuntimeExecutorContext.for_project_workspace(
        project_workspace=workspace,
        executor="openclaw",
        project_id=1,
        task_execution_id=1,
    )
    context = RuntimeExecutorContext(
        project_id=context.project_id,
        task_execution_id=context.task_execution_id,
        project_workspace=context.project_workspace,
        runtime_workspace=runtime,
        executor=context.executor,
    )

    with pytest.raises(ExecutorWorkspaceBindingError, match="Multiple OpenClaw agents"):
        bind_openclaw_workspace(context, real_config_path=config)
