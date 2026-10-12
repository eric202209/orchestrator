"""Provider-free RTO1 tests for runtime-owned OpenClaw template selection."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.agents.openclaw_service import OpenClawSessionService
from app.models import Project
from app.services.orchestration.execution.executor_workspace_binding import (
    ExecutorWorkspaceBindingError,
    bind_openclaw_workspace,
    select_runtime_owned_openclaw_template,
)
from app.services.orchestration.execution.runtime_context import (
    RuntimeExecutorContext,
)
from app.services.agents.runtime_configuration import (
    BackendRole,
    RoleRuntimeConfiguration,
)
from app.services.workspace.workspace_admission import (
    WorkspaceAdmissionError,
    admit_openclaw_workspace_binding,
)


def _write_config(path, *, project_root, runner_root):
    path.write_text(
        json.dumps(
            {
                "models": {
                    "providers": {
                        "openai": {
                            "baseUrl": "http://synthetic-ai-gateway:8000/v1",
                            "api": "openai-completions",
                            "apiKey": "synthetic-local-gateway-token-not-real",
                            "models": [{"id": "qwen-local"}],
                        }
                    }
                },
                "agents": {
                    "list": [
                        {"id": "project-agent", "workspace": str(project_root)},
                        {"id": "runtime-runner", "workspace": str(runner_root)},
                    ]
                },
            }
        ),
        encoding="utf-8",
    )


def _runtime_fixture(tmp_path, *, task_execution_id=9000):
    project_root = tmp_path / "projects" / "product"
    runtime_root = tmp_path / "orchestrator-runtime"
    runner_root = runtime_root / "openclaw" / "runner"
    runtime_workspace = runtime_root / "tasks" / "111" / str(task_execution_id)
    project_root.mkdir(parents=True)
    runner_root.mkdir(parents=True)
    runtime_workspace.mkdir(parents=True)
    context = RuntimeExecutorContext(
        executor="openclaw",
        runtime_workspace=runtime_workspace,
        project_workspace=project_root,
        project_id=111,
        task_execution_id=task_execution_id,
        runtime_root=runtime_root,
        sandbox=object(),
    )
    return project_root, runtime_root, runner_root, runtime_workspace, context


def test_missing_runner_identity_fails_closed(tmp_path, monkeypatch):
    project_root, runtime_root, runner_root, runtime_workspace, context = (
        _runtime_fixture(tmp_path)
    )
    config_path = tmp_path / "openclaw.json"
    _write_config(
        config_path,
        project_root=project_root,
        runner_root=runner_root,
    )
    monkeypatch.delenv("OPENCLAW_RUNNER_AGENT_ID", raising=False)
    monkeypatch.setattr("app.config.settings.OPENCLAW_RUNNER_AGENT_ID", "")

    with pytest.raises(
        ExecutorWorkspaceBindingError, match="runner agent ID is not configured"
    ):
        select_runtime_owned_openclaw_template(
            json.loads(config_path.read_text()), context
        )


def test_unknown_runner_identity_fails_closed(tmp_path, monkeypatch):
    project_root, runtime_root, runner_root, runtime_workspace, context = (
        _runtime_fixture(tmp_path)
    )
    config_path = tmp_path / "openclaw.json"
    _write_config(
        config_path,
        project_root=project_root,
        runner_root=runner_root,
    )
    monkeypatch.setenv("OPENCLAW_RUNNER_AGENT_ID", "missing-runner")

    with pytest.raises(
        ExecutorWorkspaceBindingError, match="runner agent .* was not found"
    ):
        select_runtime_owned_openclaw_template(
            json.loads(config_path.read_text()), context
        )


def test_duplicate_runner_identity_fails_closed(tmp_path, monkeypatch):
    project_root, runtime_root, runner_root, runtime_workspace, context = (
        _runtime_fixture(tmp_path)
    )
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(
        json.dumps(
            {
                "models": {
                    "providers": {
                        "openai": {
                            "baseUrl": "http://synthetic-ai-gateway:8000/v1",
                            "api": "openai-completions",
                            "apiKey": "synthetic-local-gateway-token-not-real",
                            "models": [{"id": "qwen-local"}],
                        }
                    }
                },
                "agents": {
                    "list": [
                        {"id": "runtime-runner", "workspace": str(runner_root)},
                        {"id": "runtime-runner", "workspace": str(runner_root)},
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCLAW_RUNNER_AGENT_ID", "runtime-runner")

    with pytest.raises(
        ExecutorWorkspaceBindingError, match="Multiple OpenClaw runner entries"
    ):
        select_runtime_owned_openclaw_template(
            json.loads(config_path.read_text()), context
        )


def test_runner_workspace_equal_to_project_root_fails_closed(tmp_path, monkeypatch):
    project_root, runtime_root, runner_root, runtime_workspace, context = (
        _runtime_fixture(tmp_path)
    )
    config_path = tmp_path / "openclaw.json"
    _write_config(
        config_path,
        project_root=project_root,
        runner_root=project_root,
    )
    monkeypatch.setenv("OPENCLAW_RUNNER_AGENT_ID", "runtime-runner")

    with pytest.raises(
        ExecutorWorkspaceBindingError, match="outside approved runtime root"
    ):
        select_runtime_owned_openclaw_template(
            json.loads(config_path.read_text()), context
        )


def test_runner_workspace_nested_under_project_root_fails_closed(tmp_path, monkeypatch):
    project_root, runtime_root, runner_root, runtime_workspace, context = (
        _runtime_fixture(tmp_path)
    )
    nested_runner = project_root / "runtime-runner"
    nested_runner.mkdir()
    config_path = tmp_path / "openclaw.json"
    _write_config(
        config_path,
        project_root=project_root,
        runner_root=nested_runner,
    )
    monkeypatch.setenv("OPENCLAW_RUNNER_AGENT_ID", "runtime-runner")

    with pytest.raises(
        ExecutorWorkspaceBindingError, match="outside approved runtime root"
    ):
        select_runtime_owned_openclaw_template(
            json.loads(config_path.read_text()), context
        )


def test_runner_workspace_parent_of_project_root_fails_closed(tmp_path, monkeypatch):
    project_root, runtime_root, runner_root, runtime_workspace, context = (
        _runtime_fixture(tmp_path)
    )
    parent_runner = project_root.parent
    config_path = tmp_path / "openclaw.json"
    _write_config(
        config_path,
        project_root=project_root,
        runner_root=parent_runner,
    )
    monkeypatch.setenv("OPENCLAW_RUNNER_AGENT_ID", "runtime-runner")

    with pytest.raises(
        ExecutorWorkspaceBindingError, match="outside approved runtime root"
    ):
        select_runtime_owned_openclaw_template(
            json.loads(config_path.read_text()), context
        )


def test_runner_workspace_missing_directory_fails_closed(tmp_path, monkeypatch):
    project_root, runtime_root, runner_root, runtime_workspace, context = (
        _runtime_fixture(tmp_path)
    )
    missing_runner = runtime_root / "openclaw" / "missing-runner"
    config_path = tmp_path / "openclaw.json"
    _write_config(
        config_path,
        project_root=project_root,
        runner_root=missing_runner,
    )
    monkeypatch.setenv("OPENCLAW_RUNNER_AGENT_ID", "runtime-runner")

    with pytest.raises(
        ExecutorWorkspaceBindingError, match="does not exist as a directory"
    ):
        select_runtime_owned_openclaw_template(
            json.loads(config_path.read_text()), context
        )


def test_missing_approved_runtime_root_fails_closed(tmp_path, monkeypatch):
    project_root = tmp_path / "projects" / "product"
    runtime_root = tmp_path / "missing-runtime"
    runner_root = runtime_root / "openclaw" / "runner"
    runtime_workspace = runtime_root / "tasks" / "111" / "9004"
    project_root.mkdir(parents=True)
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(
        json.dumps(
            {
                "models": {
                    "providers": {
                        "openai": {
                            "baseUrl": "http://synthetic-ai-gateway:8000/v1",
                            "api": "openai-completions",
                            "apiKey": "synthetic-local-gateway-token-not-real",
                            "models": [{"id": "qwen-local"}],
                        }
                    }
                },
                "agents": {
                    "list": [{"id": "runtime-runner", "workspace": str(runner_root)}]
                },
            }
        ),
        encoding="utf-8",
    )
    context = RuntimeExecutorContext(
        executor="openclaw",
        runtime_workspace=runtime_workspace,
        project_workspace=project_root,
        project_id=111,
        task_execution_id=9004,
        runtime_root=runtime_root,
        sandbox=object(),
    )
    monkeypatch.setenv("OPENCLAW_RUNNER_AGENT_ID", "runtime-runner")

    with pytest.raises(
        ExecutorWorkspaceBindingError, match="runtime root does not exist"
    ):
        bind_openclaw_workspace(context, real_config_path=config_path)


def test_project_and_runtime_roots_overlapping_fails_closed(tmp_path, monkeypatch):
    runtime_root = tmp_path / "orchestrator-runtime"
    project_root = runtime_root / "projects" / "product"
    runner_root = runtime_root / "openclaw" / "runner"
    runtime_workspace = runtime_root / "tasks" / "111" / "9005"
    project_root.mkdir(parents=True)
    runner_root.mkdir(parents=True)
    runtime_workspace.mkdir(parents=True)
    config_path = tmp_path / "openclaw.json"
    _write_config(
        config_path,
        project_root=project_root,
        runner_root=runner_root,
    )
    context = RuntimeExecutorContext(
        executor="openclaw",
        runtime_workspace=runtime_workspace,
        project_workspace=project_root,
        project_id=111,
        task_execution_id=9005,
        runtime_root=runtime_root,
        sandbox=object(),
    )
    monkeypatch.setenv("OPENCLAW_RUNNER_AGENT_ID", "runtime-runner")

    with pytest.raises(ExecutorWorkspaceBindingError, match="runtime root overlap"):
        bind_openclaw_workspace(context, real_config_path=config_path)


def test_explicit_binding_argument_precedes_environment_identity(tmp_path, monkeypatch):
    project_root, runtime_root, runner_root, runtime_workspace, context = (
        _runtime_fixture(tmp_path, task_execution_id=9006)
    )
    alternate_runner = runtime_root / "openclaw" / "alternate"
    alternate_runner.mkdir()
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(
        json.dumps(
            {
                "models": {
                    "providers": {
                        "openai": {
                            "baseUrl": "http://synthetic-ai-gateway:8000/v1",
                            "api": "openai-completions",
                            "apiKey": "synthetic-local-gateway-token-not-real",
                            "models": [{"id": "qwen-local"}],
                        }
                    }
                },
                "agents": {
                    "list": [
                        {"id": "runtime-runner", "workspace": str(runner_root)},
                        {"id": "alternate-runner", "workspace": str(alternate_runner)},
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCLAW_RUNNER_AGENT_ID", "alternate-runner")

    binding = bind_openclaw_workspace(
        context,
        real_config_path=config_path,
        runner_agent_id="runtime-runner",
        model_ref="openai/qwen-local",
    )
    try:
        assert binding.agent_id == "runtime-runner"
    finally:
        binding.release()


def test_runtime_workspace_outside_approved_root_fails_closed(tmp_path, monkeypatch):
    project_root, runtime_root, runner_root, runtime_workspace, context = (
        _runtime_fixture(tmp_path)
    )
    context = RuntimeExecutorContext(
        executor=context.executor,
        runtime_workspace=tmp_path / "outside-runtime",
        project_workspace=project_root,
        project_id=context.project_id,
        task_execution_id=context.task_execution_id,
        runtime_root=runtime_root,
        sandbox=context.sandbox,
    )
    context.runtime_workspace.mkdir()
    config_path = tmp_path / "openclaw.json"
    _write_config(
        config_path,
        project_root=project_root,
        runner_root=runner_root,
    )
    monkeypatch.setenv("OPENCLAW_RUNNER_AGENT_ID", "runtime-runner")

    with pytest.raises(
        ExecutorWorkspaceBindingError, match="outside approved runtime root"
    ):
        bind_openclaw_workspace(context, real_config_path=config_path)


def test_runtime_workspace_inside_project_root_fails_closed(tmp_path, monkeypatch):
    project_root, runtime_root, runner_root, runtime_workspace, context = (
        _runtime_fixture(tmp_path)
    )
    nested_runtime = project_root / "task-runtime"
    nested_runtime.mkdir()
    context = RuntimeExecutorContext(
        executor=context.executor,
        runtime_workspace=nested_runtime,
        project_workspace=project_root,
        project_id=context.project_id,
        task_execution_id=context.task_execution_id,
        runtime_root=runtime_root,
        sandbox=context.sandbox,
    )
    config_path = tmp_path / "openclaw.json"
    _write_config(
        config_path,
        project_root=project_root,
        runner_root=runner_root,
    )
    monkeypatch.setenv("OPENCLAW_RUNNER_AGENT_ID", "runtime-runner")

    with pytest.raises(ExecutorWorkspaceBindingError, match="inside Project Workspace"):
        bind_openclaw_workspace(context, real_config_path=config_path)


def test_unavailable_runtime_workspace_fails_closed(tmp_path, monkeypatch):
    project_root, runtime_root, runner_root, _runtime_workspace, context = (
        _runtime_fixture(tmp_path, task_execution_id=9007)
    )
    unavailable = runtime_root / "tasks" / "111" / "9008"
    context = RuntimeExecutorContext(
        executor=context.executor,
        runtime_workspace=unavailable,
        project_workspace=project_root,
        project_id=context.project_id,
        task_execution_id=9008,
        runtime_root=runtime_root,
        sandbox=context.sandbox,
    )
    config_path = tmp_path / "openclaw.json"
    _write_config(
        config_path,
        project_root=project_root,
        runner_root=runner_root,
    )
    monkeypatch.setenv("OPENCLAW_RUNNER_AGENT_ID", "runtime-runner")

    with pytest.raises(
        ExecutorWorkspaceBindingError, match="Runtime Workspace does not exist"
    ):
        bind_openclaw_workspace(context, real_config_path=config_path)


def test_dispatch_admission_accepts_runtime_runner_without_project_root_agent(
    db_session, tmp_path, monkeypatch
):
    project_root, runtime_root, runner_root, runtime_workspace, context = (
        _runtime_fixture(tmp_path)
    )
    project = Project(name="Runtime Runner Project", workspace_path=str(project_root))
    db_session.add(project)
    db_session.commit()
    config_path = tmp_path / "openclaw.json"
    _write_config(
        config_path,
        project_root=project_root,
        runner_root=runner_root,
    )
    monkeypatch.setenv("OPENCLAW_RUNNER_AGENT_ID", "runtime-runner")
    monkeypatch.setattr(
        "app.services.workspace.workspace_admission.get_effective_runtime_root",
        lambda _db: runtime_root,
    )

    admission = admit_openclaw_workspace_binding(
        db_session,
        project,
        configured_provider="local_openclaw",
        openclaw_config_path=config_path,
    )

    assert admission.openclaw_agent_id == "runtime-runner"
    assert admission.workspace == str(project_root.resolve())


def test_physical_binding_probe_writes_only_ephemeral_runtime_copy(
    tmp_path, monkeypatch
):
    project_root, runtime_root, runner_root, runtime_workspace, context = (
        _runtime_fixture(tmp_path, task_execution_id=9003)
    )
    (project_root / "source.txt").write_text("source\n", encoding="utf-8")
    (runner_root / "template.txt").write_text("template\n", encoding="utf-8")
    config_path = tmp_path / "openclaw.json"
    _write_config(
        config_path,
        project_root=project_root,
        runner_root=runner_root,
    )
    monkeypatch.setenv("OPENCLAW_RUNNER_AGENT_ID", "runtime-runner")
    before_entries = sorted(path.name for path in project_root.iterdir())
    before_mtime = project_root.stat().st_mtime_ns
    dot_openclaw = project_root / ".openclaw"
    bootstrap_names = (
        "AGENTS.md",
        "BOOTSTRAP.md",
        "HEARTBEAT.md",
        "IDENTITY.md",
        "SOUL.md",
        "TOOLS.md",
        "USER.md",
    )
    before_dot_openclaw_mtime = (
        dot_openclaw.stat().st_mtime_ns if dot_openclaw.exists() else None
    )
    before_bootstrap_mtimes = {
        name: (
            (project_root / name).stat().st_mtime_ns
            if (project_root / name).exists()
            else None
        )
        for name in bootstrap_names
    }

    binding = bind_openclaw_workspace(
        context,
        real_config_path=config_path,
        runner_agent_id="runtime-runner",
        model_ref="openai/qwen-local",
    )
    try:
        bound_config = json.loads(binding.config_path.read_text(encoding="utf-8"))
        selected = next(
            agent
            for agent in bound_config["agents"]["list"]
            if agent["id"] == "runtime-runner"
        )
        assert selected["workspace"] == str(runtime_workspace)
        assert selected["agentDir"] != str(runner_root)
        assert sorted(path.name for path in project_root.iterdir()) == before_entries
        assert project_root.stat().st_mtime_ns == before_mtime
        assert (
            dot_openclaw.stat().st_mtime_ns if dot_openclaw.exists() else None
        ) == before_dot_openclaw_mtime
        assert {
            name: (
                (project_root / name).stat().st_mtime_ns
                if (project_root / name).exists()
                else None
            )
            for name in bootstrap_names
        } == before_bootstrap_mtimes
    finally:
        binding.release()

    assert not binding.config_path.exists()
    assert sorted(path.name for path in project_root.iterdir()) == before_entries
    assert project_root.stat().st_mtime_ns == before_mtime
    assert (
        dot_openclaw.stat().st_mtime_ns if dot_openclaw.exists() else None
    ) == before_dot_openclaw_mtime
    assert {
        name: (
            (project_root / name).stat().st_mtime_ns
            if (project_root / name).exists()
            else None
        )
        for name in bootstrap_names
    } == before_bootstrap_mtimes


@pytest.mark.parametrize("runner_id", ["runtime-runner"])
def test_explicit_runner_id_is_selected_over_project_root_agent(
    tmp_path, monkeypatch, runner_id
):
    project_root = tmp_path / "projects" / "product"
    runtime_root = tmp_path / "orchestrator-runtime"
    runner_root = runtime_root / "openclaw" / "runner"
    runtime_workspace = runtime_root / "tasks" / "111" / "9001"
    project_root.mkdir(parents=True)
    runner_root.mkdir(parents=True)
    runtime_workspace.mkdir(parents=True)
    config_path = tmp_path / "openclaw.json"
    _write_config(
        config_path,
        project_root=project_root,
        runner_root=runner_root,
    )
    monkeypatch.setenv("OPENCLAW_RUNNER_AGENT_ID", runner_id)

    context = RuntimeExecutorContext(
        executor="openclaw",
        runtime_workspace=runtime_workspace,
        project_workspace=project_root,
        project_id=111,
        task_execution_id=9001,
        runtime_root=runtime_root,
        sandbox=object(),
    )

    binding = bind_openclaw_workspace(
        context,
        real_config_path=config_path,
        runner_agent_id=runner_id,
        model_ref="openai/qwen-local",
    )
    try:
        assert binding.agent_id == runner_id
        bound_config = json.loads(binding.config_path.read_text(encoding="utf-8"))
        selected = next(
            agent
            for agent in bound_config["agents"]["list"]
            if agent["id"] == runner_id
        )
        assert selected["workspace"] == str(runtime_workspace)
    finally:
        binding.release()


def test_binding_applies_explicit_runtime_model_without_fallbacks(
    tmp_path, monkeypatch
):
    project_root, runtime_root, runner_root, runtime_workspace, context = (
        _runtime_fixture(tmp_path, task_execution_id=9003)
    )
    config_path = tmp_path / "openclaw.json"
    config = {
        "models": {
            "providers": {
                "openai": {
                    "baseUrl": "http://synthetic-ai-gateway:8000/v1",
                    "api": "openai-completions",
                    "apiKey": "synthetic-local-gateway-token-not-real",
                    "models": [{"id": "qwen-local"}],
                }
            }
        },
        "agents": {
            "defaults": {
                "model": {
                    "primary": "ollama/qwen3-coder:30b",
                    "fallbacks": ["openai/qwen-local"],
                }
            },
            "list": [
                {"id": "project-agent", "workspace": str(project_root)},
                {
                    "id": "runtime-runner",
                    "workspace": str(runner_root),
                    "model": "ollama/qwen3-coder:30b",
                },
            ],
        },
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")
    persistent_before = config_path.read_bytes()
    monkeypatch.setenv("OPENCLAW_RUNNER_AGENT_ID", "runtime-runner")

    binding = bind_openclaw_workspace(
        context,
        real_config_path=config_path,
        model_ref="openai/qwen-local",
    )
    try:
        bound_config = json.loads(binding.config_path.read_text(encoding="utf-8"))
        selected = next(
            agent
            for agent in bound_config["agents"]["list"]
            if agent["id"] == binding.agent_id
        )
        assert selected["model"] == {
            "primary": "openai/qwen-local",
            "fallbacks": [],
        }
        assert bound_config["agents"]["defaults"] == {
            "workspace": str(runtime_workspace),
            "skipBootstrap": True,
        }
        assert selected["workspace"] == str(runtime_workspace)
        ephemeral_agent_dir = Path(selected["agentDir"])
        ephemeral_state_dir = Path(binding.environment["OPENCLAW_STATE_DIR"])
        assert ephemeral_agent_dir.is_dir()
        assert ephemeral_state_dir.is_dir()
        assert config_path.read_bytes() == persistent_before
    finally:
        binding.release()

    assert not binding.config_path.exists()
    assert not ephemeral_agent_dir.exists()
    assert not ephemeral_state_dir.exists()


def test_service_binding_uses_role_model_over_persistent_runner_model(
    tmp_path, monkeypatch
):
    project_root, runtime_root, runner_root, runtime_workspace, context = (
        _runtime_fixture(tmp_path, task_execution_id=9004)
    )
    config_path = tmp_path / "openclaw.json"
    config = {
        "models": {
            "providers": {
                "openai": {
                    "baseUrl": "http://synthetic-ai-gateway:8000/v1",
                    "api": "openai-completions",
                    "apiKey": "synthetic-local-gateway-token-not-real",
                    "models": [{"id": "qwen-local"}],
                },
                "ollama": {"models": [{"id": "qwen3-coder:30b"}]},
            }
        },
        "agents": {
            "defaults": {
                "model": {
                    "primary": "ollama/qwen3-coder:30b",
                    "fallbacks": ["openai/qwen-local"],
                }
            },
            "list": [
                {
                    "id": "runtime-runner",
                    "workspace": str(runner_root),
                    "model": "ollama/qwen3-coder:30b",
                }
            ],
        },
    }
    config_path.write_text(json.dumps(config), encoding="utf-8")
    persistent_before = config_path.read_bytes()
    monkeypatch.setenv("OPENCLAW_RUNNER_AGENT_ID", "runtime-runner")

    service = object.__new__(OpenClawSessionService)
    service.runtime_configuration = RoleRuntimeConfiguration(
        role=BackendRole.EXECUTION,
        backend_name="local_openclaw",
        model_family="qwen-local",
        adaptation_profile="openclaw_default",
    )
    service._openclaw_config_path = lambda: config_path
    service._workspace_binding = None
    service.execution_cwd_override = None

    service.bind_runtime_workspace(context)
    try:
        bound_config = json.loads(
            service._workspace_binding.config_path.read_text(encoding="utf-8")
        )
        selected = next(
            agent
            for agent in bound_config["agents"]["list"]
            if agent["id"] == service._workspace_binding.agent_id
        )
        assert selected["model"] == {
            "primary": "openai/qwen-local",
            "fallbacks": [],
        }
        assert selected["workspace"] == str(runtime_workspace)
        assert config_path.read_bytes() == persistent_before
    finally:
        service.release_runtime_workspace_binding()

    assert service._workspace_binding is None
    assert config_path.read_bytes() == persistent_before


def test_command_selection_uses_explicit_runner_id_not_workspace_order(
    tmp_path, monkeypatch
):
    project_root = tmp_path / "projects" / "product"
    runtime_root = tmp_path / "orchestrator-runtime"
    runtime_workspace = runtime_root / "tasks" / "111" / "9002"
    project_root.mkdir(parents=True)
    runtime_workspace.mkdir(parents=True)
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "list": [
                        {"id": "project-agent", "workspace": str(runtime_workspace)},
                        {"id": "runtime-runner", "workspace": str(runtime_workspace)},
                        {
                            "id": "orchestrator-runtime",
                            "workspace": str(runtime_workspace),
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCLAW_RUNNER_AGENT_ID", "runtime-runner")

    service = object.__new__(OpenClawSessionService)
    service._openclaw_config_path = lambda: config_path
    service._runtime_executor_context = RuntimeExecutorContext(
        executor="openclaw",
        runtime_workspace=runtime_workspace,
        project_workspace=project_root,
        project_id=111,
        task_execution_id=9002,
        runtime_root=runtime_root,
        sandbox=object(),
    )
    service.execution_cwd_override = str(runtime_workspace)
    service._workspace_binding = SimpleNamespace(agent_id="orchestrator-runtime")
    service._last_selected_openclaw_agent_id = None

    result = service._build_openclaw_agent_command(
        ["openclaw"], cwd=str(runtime_workspace)
    )

    assert result == ["openclaw", "agent", "--agent", "orchestrator-runtime"]
    assert service._last_selected_openclaw_agent_id == "orchestrator-runtime"


def test_two_tasks_share_template_identity_but_not_ephemeral_binding(
    tmp_path, monkeypatch
):
    project_root, runtime_root, runner_root, first_workspace, first_context = (
        _runtime_fixture(tmp_path, task_execution_id=9010)
    )
    second_workspace = runtime_root / "tasks" / "111" / "9011"
    second_workspace.mkdir(parents=True)
    second_context = RuntimeExecutorContext(
        executor=first_context.executor,
        runtime_workspace=second_workspace,
        project_workspace=project_root,
        project_id=first_context.project_id,
        task_execution_id=9011,
        runtime_root=runtime_root,
        sandbox=object(),
    )
    config_path = tmp_path / "openclaw.json"
    _write_config(
        config_path,
        project_root=project_root,
        runner_root=runner_root,
    )
    monkeypatch.setenv("OPENCLAW_RUNNER_AGENT_ID", "runtime-runner")

    first = bind_openclaw_workspace(
        first_context,
        real_config_path=config_path,
        runner_agent_id="runtime-runner",
        model_ref="openai/qwen-local",
    )
    second = bind_openclaw_workspace(
        second_context,
        real_config_path=config_path,
        runner_agent_id="runtime-runner",
        model_ref="openai/qwen-local",
    )
    try:
        assert first.agent_id == second.agent_id == "runtime-runner"
        assert first.config_path != second.config_path
        first_config = json.loads(first.config_path.read_text(encoding="utf-8"))
        second_config = json.loads(second.config_path.read_text(encoding="utf-8"))
        first_agent = next(
            agent
            for agent in first_config["agents"]["list"]
            if agent["id"] == "runtime-runner"
        )
        second_agent = next(
            agent
            for agent in second_config["agents"]["list"]
            if agent["id"] == "runtime-runner"
        )
        assert first_agent["workspace"] == str(first_workspace)
        assert second_agent["workspace"] == str(second_workspace)
        assert first_agent["agentDir"] != second_agent["agentDir"]
    finally:
        first.release()
        second.release()

    assert not first.config_path.exists()
    assert not second.config_path.exists()


def test_only_project_root_agent_without_runner_fails_dispatch_admission(
    db_session, tmp_path, monkeypatch
):
    project_root, runtime_root, _runner_root, _runtime_workspace, _context = (
        _runtime_fixture(tmp_path)
    )
    project = Project(name="No Runner Project", workspace_path=str(project_root))
    db_session.add(project)
    db_session.commit()
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "list": [{"id": "project-agent", "workspace": str(project_root)}]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCLAW_RUNNER_AGENT_ID", "runtime-runner")
    monkeypatch.setattr(
        "app.services.workspace.workspace_admission.get_effective_runtime_root",
        lambda _db: runtime_root,
    )

    with pytest.raises(WorkspaceAdmissionError, match="runner agent .* was not found"):
        admit_openclaw_workspace_binding(
            db_session,
            project,
            configured_provider="local_openclaw",
            openclaw_config_path=config_path,
        )
