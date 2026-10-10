"""Phase 37 Pre-B F1: dogfood admission without persistent per-root agents.

Provider-free. Every OpenClaw config and state directory is a temp copy; the
operator's persistent installation is never read or written.
"""

from __future__ import annotations

import hashlib
import json
import subprocess
from pathlib import Path

import pytest

from app.models import Project
from app.services.agents.openclaw_service import OpenClawSessionService
from app.services.agents.runtime_configuration import (
    BackendRole,
    RoleRuntimeConfiguration,
)
from app.services.orchestration.execution.executor_workspace_binding import (
    EPHEMERAL_AGENT_ID,
    ExecutorWorkspaceBindingError,
    bind_openclaw_workspace,
)
from app.services.orchestration.execution.runtime_context import (
    RuntimeExecutorContext,
)
from app.services.workspace.workspace_admission import (
    WorkspaceAdmissionError,
    admit_dogfood_workspace,
)


def _git(workspace: Path, *args: str) -> None:
    subprocess.run(
        ["git", "-C", str(workspace), *args], check=True, capture_output=True
    )


def _product_root(tmp_path: Path, name: str = "product") -> Path:
    workspace = tmp_path / "projects" / name
    workspace.mkdir(parents=True)
    _git(workspace, "init")
    _git(workspace, "remote", "add", "origin", "https://example.invalid/p.git")
    return workspace


def _config(path: Path, agents: list[dict]) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"agents": {"list": agents}}), encoding="utf-8")
    return path


def _main(tmp_path: Path) -> dict:
    return {"id": "main", "default": True, "workspace": str(tmp_path / "main-ws")}


def _project(db_session, workspace: Path, name: str = "product") -> Project:
    project = Project(name=name, workspace_path=str(workspace))
    db_session.add(project)
    db_session.commit()
    return project


def test_main_only_config_admits_fresh_product_root(db_session, tmp_path):
    workspace = _product_root(tmp_path)
    project = _project(db_session, workspace)
    config = _config(tmp_path / "openclaw.json", [_main(tmp_path)])
    before = config.read_bytes()

    admitted = admit_dogfood_workspace(db_session, project, openclaw_config_path=config)

    assert admitted.workspace == str(workspace.resolve())
    assert admitted.openclaw_agent_id is None
    assert config.read_bytes() == before


def test_one_legacy_matching_agent_remains_admitted(db_session, tmp_path):
    workspace = _product_root(tmp_path)
    project = _project(db_session, workspace)
    config = _config(
        tmp_path / "openclaw.json",
        [_main(tmp_path), {"id": "legacy", "workspace": str(workspace)}],
    )

    admitted = admit_dogfood_workspace(db_session, project, openclaw_config_path=config)

    assert admitted.openclaw_agent_id == "legacy"


def test_two_matching_agents_are_rejected_as_ambiguous(db_session, tmp_path):
    workspace = _product_root(tmp_path)
    project = _project(db_session, workspace)
    config = _config(
        tmp_path / "openclaw.json",
        [
            _main(tmp_path),
            {"id": "a", "workspace": str(workspace)},
            {"id": "b", "workspace": str(workspace) + "/"},
        ],
    )

    with pytest.raises(WorkspaceAdmissionError) as exc_info:
        admit_dogfood_workspace(db_session, project, openclaw_config_path=config)
    assert exc_info.value.category == "workspace_openclaw_mismatch"


@pytest.mark.parametrize(
    "agents",
    [
        pytest.param([], id="missing-main"),
        pytest.param([{"id": "main"}, {"id": " main "}], id="duplicate-main"),
    ],
)
def test_main_must_exist_exactly_once(db_session, tmp_path, agents):
    workspace = _product_root(tmp_path)
    project = _project(db_session, workspace)
    config = _config(tmp_path / "openclaw.json", agents)

    with pytest.raises(WorkspaceAdmissionError) as exc_info:
        admit_dogfood_workspace(db_session, project, openclaw_config_path=config)
    assert exc_info.value.category == "openclaw_config_invalid"


@pytest.mark.parametrize(
    "content",
    [
        pytest.param("{not json", id="invalid-json"),
        pytest.param("[]", id="not-object"),
        pytest.param('{"agents": []}', id="agents-not-object"),
        pytest.param('{"agents": {"list": {}}}', id="list-not-array"),
        pytest.param(None, id="missing-file"),
    ],
)
def test_malformed_config_is_rejected(db_session, tmp_path, content):
    workspace = _product_root(tmp_path)
    project = _project(db_session, workspace)
    config = tmp_path / "openclaw.json"
    if content is not None:
        config.write_text(content, encoding="utf-8")

    with pytest.raises(WorkspaceAdmissionError) as exc_info:
        admit_dogfood_workspace(db_session, project, openclaw_config_path=config)
    assert exc_info.value.category == "openclaw_config_invalid"


def test_existing_workspace_rejections_are_preserved(db_session, tmp_path):
    config = _config(tmp_path / "openclaw.json", [_main(tmp_path)])

    missing = _project(db_session, tmp_path / "projects" / "missing", "missing")
    with pytest.raises(WorkspaceAdmissionError) as exc_info:
        admit_dogfood_workspace(db_session, missing, openclaw_config_path=config)
    assert exc_info.value.category == "workspace_missing"

    dirty_root = _product_root(tmp_path, "dirty")
    (dirty_root / "untracked.txt").write_text("x\n", encoding="utf-8")
    dirty = _project(db_session, dirty_root, "dirty")
    with pytest.raises(WorkspaceAdmissionError) as exc_info:
        admit_dogfood_workspace(db_session, dirty, openclaw_config_path=config)
    assert exc_info.value.category == "workspace_dirty"

    remoteless_root = tmp_path / "projects" / "remoteless"
    remoteless_root.mkdir(parents=True)
    _git(remoteless_root, "init")
    remoteless = _project(db_session, remoteless_root, "remoteless")
    with pytest.raises(WorkspaceAdmissionError) as exc_info:
        admit_dogfood_workspace(db_session, remoteless, openclaw_config_path=config)
    assert exc_info.value.category == "workspace_remote_missing"

    shared_root = _product_root(tmp_path, "shared")
    first = _project(db_session, shared_root, "first")
    _project(db_session, shared_root, "second")
    with pytest.raises(WorkspaceAdmissionError) as exc_info:
        admit_dogfood_workspace(db_session, first, openclaw_config_path=config)
    assert exc_info.value.category == "workspace_mapping_ambiguous"


def test_runtime_workspace_outside_approved_root_is_still_rejected(tmp_path):
    project_root = _product_root(tmp_path)
    runtime_root = tmp_path / "runtime"
    runtime_root.mkdir()
    outside = tmp_path / "elsewhere" / "task"
    outside.mkdir(parents=True)
    config = _config(tmp_path / "openclaw.json", [_main(tmp_path)])
    before = config.read_bytes()
    context = RuntimeExecutorContext(
        executor="openclaw",
        runtime_workspace=outside,
        project_workspace=project_root,
        project_id=1,
        task_execution_id=1,
        runtime_root=runtime_root,
        sandbox=object(),
    )

    with pytest.raises(ExecutorWorkspaceBindingError, match="outside approved"):
        bind_openclaw_workspace(
            context, real_config_path=config, model_ref="openai/qwen-local"
        )
    assert config.read_bytes() == before


def _snapshot(state: Path) -> dict:
    config = state / "openclaw.json"
    return {
        "sha256": hashlib.sha256(config.read_bytes()).hexdigest(),
        "agents_list": json.loads(config.read_text(encoding="utf-8"))["agents"]["list"],
        "agent_tree": sorted(
            str(p.relative_to(state / "agents")) for p in (state / "agents").rglob("*")
        ),
        "config_audit": (state / "logs" / "config-audit.jsonl").read_bytes(),
    }


def test_admission_bind_command_release_leaves_persistent_config_unchanged(
    db_session, tmp_path, monkeypatch
):
    home = tmp_path / "home"
    state = home / ".openclaw"
    (state / "agents" / "main" / "agent").mkdir(parents=True)
    (state / "agents" / "main" / "sessions").mkdir(parents=True)
    (state / "logs").mkdir(parents=True)
    (state / "logs" / "config-audit.jsonl").write_text("", encoding="utf-8")
    (state / "openclaw.json").write_text(
        json.dumps(
            {
                "models": {"providers": {"openai": {"models": [{"id": "qwen-local"}]}}},
                "agents": {
                    "list": [
                        {
                            "id": "main",
                            "default": True,
                            "workspace": str(state / "workspace"),
                            "agentDir": str(state / "agents" / "main" / "agent"),
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("HOME", str(home))
    monkeypatch.delenv("OPENCLAW_CONFIG_PATH", raising=False)
    monkeypatch.delenv("OPENCLAW_STATE_DIR", raising=False)
    monkeypatch.delenv("OPENCLAW_RUNNER_AGENT_ID", raising=False)
    before = _snapshot(state)

    workspace = _product_root(tmp_path, "fresh-product")
    project = _project(db_session, workspace, "fresh-product")
    admitted = admit_dogfood_workspace(db_session, project)
    assert admitted.openclaw_agent_id is None

    runtime_root = tmp_path / "runtime"
    runtime_workspace = runtime_root / "tasks" / str(project.id) / "1"
    runtime_workspace.mkdir(parents=True)
    context = RuntimeExecutorContext(
        executor="openclaw",
        runtime_workspace=runtime_workspace,
        project_workspace=workspace,
        project_id=project.id,
        task_execution_id=1,
        runtime_root=runtime_root,
        sandbox=object(),
    )
    service = object.__new__(OpenClawSessionService)
    service.runtime_configuration = RoleRuntimeConfiguration(
        role=BackendRole.EXECUTION,
        backend_name="local_openclaw",
        model_family="qwen-local",
        adaptation_profile="openclaw_default",
    )
    service._workspace_binding = None
    service.execution_cwd_override = None
    service._last_selected_openclaw_agent_id = None

    service.bind_runtime_workspace(context)
    try:
        binding = service._workspace_binding
        assert binding.agent_id == EPHEMERAL_AGENT_ID
        assert state not in binding.config_path.parents
        command = service._build_openclaw_agent_command(
            ["openclaw"], cwd=str(runtime_workspace)
        )
        assert command == ["openclaw", "agent", "--agent", EPHEMERAL_AGENT_ID]
        env = service._apply_workspace_binding_env({})
        assert env["OPENCLAW_CONFIG_PATH"] == str(binding.config_path)
        assert state not in Path(env["OPENCLAW_STATE_DIR"]).parents
        assert _snapshot(state) == before
    finally:
        service.release_runtime_workspace_binding()

    assert not binding.config_path.exists()
    assert _snapshot(state) == before
