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
    admit_openclaw_workspace_binding,
    admit_dogfood_workspace,
    admit_project_openclaw_binding_for_dispatch,
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
    # Phase 37 Pre-B F1: admission requires one persistent main agent.
    data = json.loads(config.read_text(encoding="utf-8"))
    data["agents"]["list"].insert(0, {"id": "main", "workspace": str(tmp_path)})
    config.write_text(json.dumps(data), encoding="utf-8")

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


def test_openclaw_binding_rejects_duplicate_explicit_runner_agents(
    tmp_path: Path, monkeypatch
):
    workspace = _clean_remote_workspace(tmp_path)
    runtime_root = tmp_path / "runtime"
    runner = runtime_root / "openclaw" / "runner"
    runtime_workspace = runtime_root / "tasks" / "1" / "1"
    runner.mkdir(parents=True)
    runtime_workspace.mkdir(parents=True)
    config = _openclaw_config(
        tmp_path / "openclaw.json", runner, "runtime-runner", "runtime-runner"
    )
    context = RuntimeExecutorContext(
        project_id=1,
        task_execution_id=1,
        project_workspace=workspace,
        runtime_workspace=runtime_workspace,
        runtime_root=runtime_root,
        executor="openclaw",
        sandbox=object(),
    )
    monkeypatch.setenv("OPENCLAW_RUNNER_AGENT_ID", "runtime-runner")

    with pytest.raises(
        ExecutorWorkspaceBindingError, match="Multiple OpenClaw runner entries"
    ):
        bind_openclaw_workspace(
            context,
            real_config_path=config,
            runner_agent_id="runtime-runner",
        )


def test_openclaw_project_binding_admission_uses_runtime_runner(
    tmp_path: Path, monkeypatch
):
    workspace = _clean_remote_workspace(tmp_path)
    project = Project(id=901, name="binding matrix", workspace_path=str(workspace))
    runtime_root = tmp_path / "runtime"
    runner = runtime_root / "openclaw" / "runner"
    runner.mkdir(parents=True)
    config = _openclaw_config(tmp_path / "openclaw.json", runner, "runtime-runner")
    monkeypatch.setenv("OPENCLAW_RUNNER_AGENT_ID", "runtime-runner")
    monkeypatch.setattr(
        "app.services.workspace.workspace_admission.get_effective_runtime_root",
        lambda _db: runtime_root,
    )

    admitted = admit_openclaw_workspace_binding(
        db=None,
        project=project,
        configured_provider="local_openclaw",
        openclaw_config_path=config,
    )
    assert admitted.openclaw_agent_id == "runtime-runner"
    assert admitted.matching_agent_count == 1
    assert admitted.workspace == str(workspace.resolve())

    alias = workspace.parent / "alias"
    alias.symlink_to(workspace, target_is_directory=True)
    alias_project = Project(id=902, name="alias binding", workspace_path=str(alias))
    alias_admitted = admit_openclaw_workspace_binding(
        db=None,
        project=alias_project,
        configured_provider="local_openclaw",
        openclaw_config_path=config,
    )
    assert alias_admitted.openclaw_agent_id == "runtime-runner"

    missing_workspace = tmp_path / "missing"
    with pytest.raises(WorkspaceAdmissionError) as exc_info:
        admit_openclaw_workspace_binding(
            db=None,
            project=Project(
                id=904,
                name="missing workspace",
                workspace_path=str(missing_workspace),
            ),
            configured_provider="local_openclaw",
            openclaw_config_path=config,
        )
    assert exc_info.value.metadata["workspace_exists"] is False
    assert exc_info.value.metadata["matching_agent_count"] == 0


def test_project_dispatch_binding_admission_skips_non_openclaw_backend(
    db_session, tmp_path: Path, monkeypatch
):
    project = Project(
        name="direct backend project",
        workspace_path=str(tmp_path / "not-registered"),
    )
    db_session.add(project)
    db_session.commit()

    monkeypatch.setattr(
        "app.services.agents.agent_runtime.resolve_backend_name_for_role",
        lambda db, role: "openai_chat_completions",
    )

    assert admit_project_openclaw_binding_for_dispatch(db_session, project) is None
