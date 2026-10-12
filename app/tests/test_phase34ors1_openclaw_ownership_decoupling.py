"""Provider-free ORS1 tests for ephemeral OpenClaw ownership."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path

import pytest

from app.services.agents.openclaw_service import OpenClawSessionService
from app.services.agents.runtime_configuration import (
    BackendRole,
    RoleRuntimeConfiguration,
)
from app.services.orchestration.execution.executor_workspace_binding import (
    ExecutorWorkspaceBindingError,
    bind_openclaw_workspace,
)
from app.services.orchestration.execution.runtime_context import RuntimeExecutorContext


def _config(path: Path, *, project: Path) -> dict:
    value = {
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
                "workspace": str(project),
                "model": {
                    "primary": "ollama/qwen3-coder:30b",
                    "fallbacks": ["openai/qwen-local"],
                },
            },
            "list": [
                {
                    "id": "main",
                    "workspace": str(project),
                    "agentDir": str(path.parent / "operator-main-agent"),
                    "model": {"primary": "ollama/qwen3-coder:30b"},
                }
            ],
        },
    }
    path.write_text(json.dumps(value, indent=2), encoding="utf-8")
    return value


def _context(tmp_path: Path) -> tuple[Path, Path, Path, RuntimeExecutorContext]:
    project = tmp_path / "product"
    project.mkdir()
    runtime_root = tmp_path / "runtime-root"
    runtime = runtime_root / "tasks" / "probe"
    runtime.mkdir(parents=True)
    context = RuntimeExecutorContext(
        executor="openclaw",
        runtime_workspace=runtime,
        project_workspace=project,
        project_id=None,
        task_execution_id=None,
        runtime_root=runtime_root,
        sandbox=object(),
    )
    return project, runtime_root, runtime, context


def test_normal_binding_uses_synthetic_agent_and_preserves_persistent_state(tmp_path):
    project, _runtime_root, runtime, context = _context(tmp_path)
    config_path = tmp_path / "openclaw.json"
    original = _config(config_path, project=project)
    before_bytes = config_path.read_bytes()
    before_sha = hashlib.sha256(before_bytes).hexdigest()

    binding = bind_openclaw_workspace(
        context,
        real_config_path=config_path,
        model_ref="openai/qwen-local",
    )
    try:
        bound = json.loads(binding.config_path.read_text(encoding="utf-8"))
        selected = next(
            agent
            for agent in bound["agents"]["list"]
            if agent["id"] == binding.agent_id
        )
        assert binding.agent_id == "orchestrator-runtime"
        assert [agent["id"] for agent in bound["agents"]["list"]] == [
            "orchestrator-runtime",
        ]
        assert selected["workspace"] == str(runtime)
        assert selected["model"] == {
            "primary": "openai/qwen-local",
            "fallbacks": [],
        }
        assert bound["agents"]["defaults"] == {
            "workspace": str(runtime),
            "skipBootstrap": True,
        }
        assert config_path.read_bytes() == before_bytes
        assert hashlib.sha256(config_path.read_bytes()).hexdigest() == before_sha
        assert Path(selected["agentDir"]).is_dir()
        assert Path(binding.environment["OPENCLAW_STATE_DIR"]).is_dir()
    finally:
        temp_config = binding.config_path
        temp_agent_dir = Path(selected["agentDir"])
        temp_state_dir = Path(binding.environment["OPENCLAW_STATE_DIR"])
        binding.release()

    assert not temp_config.exists()
    assert not temp_agent_dir.exists()
    assert not temp_state_dir.exists()
    assert config_path.read_bytes() == before_bytes
    assert [
        agent["id"] for agent in json.loads(config_path.read_text())["agents"]["list"]
    ] == ["main"]


def test_normal_binding_ignores_missing_persistent_runner_and_explicitly_selects_agent(
    tmp_path, monkeypatch
):
    project, _runtime_root, runtime, context = _context(tmp_path)
    config_path = tmp_path / "openclaw.json"
    _config(config_path, project=project)
    monkeypatch.setenv("OPENCLAW_RUNNER_AGENT_ID", "missing-runner")

    binding = bind_openclaw_workspace(
        context,
        real_config_path=config_path,
        model_ref="openai/qwen-local",
    )
    try:
        service = object.__new__(OpenClawSessionService)
        service._openclaw_config_path = lambda: binding.config_path
        service._runtime_executor_context = context
        service._workspace_binding = binding
        service._last_selected_openclaw_agent_id = None
        service._log_entry = lambda *args, **kwargs: None
        command = service._build_openclaw_agent_command(["openclaw"], cwd=str(runtime))
        assert command == ["openclaw", "agent", "--agent", "orchestrator-runtime"]
    finally:
        binding.release()


def test_service_binding_requires_role_model_and_uses_it_over_defaults(tmp_path):
    project, _runtime_root, runtime, context = _context(tmp_path)
    config_path = tmp_path / "openclaw.json"
    _config(config_path, project=project)
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

    service.bind_runtime_workspace(context, runner_agent_id="not-used")
    try:
        bound = json.loads(service._workspace_binding.config_path.read_text())
        selected = next(
            agent
            for agent in bound["agents"]["list"]
            if agent["id"] == "orchestrator-runtime"
        )
        assert selected["workspace"] == str(runtime)
        assert selected["model"] == {
            "primary": "openai/qwen-local",
            "fallbacks": [],
        }
    finally:
        service.release_runtime_workspace_binding()


def test_service_command_builder_reads_active_ephemeral_config(tmp_path, monkeypatch):
    project, _runtime_root, runtime, context = _context(tmp_path)
    config_path = tmp_path / "openclaw.json"
    _config(config_path, project=project)
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(config_path))
    service = object.__new__(OpenClawSessionService)
    service.runtime_configuration = RoleRuntimeConfiguration(
        role=BackendRole.EXECUTION,
        backend_name="local_openclaw",
        model_family="qwen-local",
        adaptation_profile="openclaw_default",
    )
    service._openclaw_config_path_override = None
    service._workspace_binding = None
    service._runtime_executor_context = None
    service._runtime_runner_agent_id = None
    service._runtime_workspace_previous_cwd_override = None
    service._strict_planning_binding = None
    service._last_selected_openclaw_agent_id = None
    service.execution_cwd_override = None
    service._log_entry = lambda *args, **kwargs: None
    monkeypatch.setattr(service, "_resolve_openclaw_command", lambda: ["openclaw"])

    service.bind_runtime_workspace(context, runner_agent_id="missing-runner")
    try:
        command = service.build_cli_agent_command(
            "provider-free seam check",
            source_brain="local",
            timeout_seconds=30,
            session_prefix="ors1",
        )
        assert "--agent" in command
        assert command[command.index("--agent") + 1] == "orchestrator-runtime"
        assert str(service._workspace_binding.config_path) != str(config_path)
    finally:
        service.release_runtime_workspace_binding()


def test_missing_role_model_fails_closed_before_invocation_binding(tmp_path):
    project, _runtime_root, _runtime, context = _context(tmp_path)
    config_path = tmp_path / "openclaw.json"
    _config(config_path, project=project)

    with pytest.raises(
        ExecutorWorkspaceBindingError,
        match="Explicit OpenClaw runtime model is required",
    ):
        bind_openclaw_workspace(context, real_config_path=config_path)


@pytest.mark.parametrize("unsafe_context", ["outside", "inside_project", "overlap"])
def test_runtime_workspace_safety_remains_fail_closed(tmp_path, unsafe_context):
    project, runtime_root, runtime, context = _context(tmp_path)
    config_path = tmp_path / "openclaw.json"
    _config(config_path, project=project)
    if unsafe_context == "outside":
        runtime = tmp_path / "outside"
        runtime.mkdir()
    elif unsafe_context == "inside_project":
        runtime = project / "nested-runtime"
        runtime.mkdir()
    else:
        project = runtime_root / "project-overlap"
        project.mkdir()
        context = RuntimeExecutorContext(
            executor="openclaw",
            runtime_workspace=runtime,
            project_workspace=project,
            project_id=None,
            task_execution_id=None,
            runtime_root=runtime_root,
            sandbox=object(),
        )
    unsafe = RuntimeExecutorContext(
        executor=context.executor,
        runtime_workspace=runtime,
        project_workspace=project,
        project_id=context.project_id,
        task_execution_id=context.task_execution_id,
        runtime_root=context.runtime_root,
        sandbox=context.sandbox,
    )

    with pytest.raises(ExecutorWorkspaceBindingError):
        bind_openclaw_workspace(
            unsafe,
            real_config_path=config_path,
            model_ref="openai/qwen-local",
        )


def test_no_persistent_runner_is_required_after_normal_binding(tmp_path):
    project, _runtime_root, _runtime, context = _context(tmp_path)
    config_path = tmp_path / "openclaw.json"
    _config(config_path, project=project)
    binding = bind_openclaw_workspace(
        context,
        real_config_path=config_path,
        model_ref="openai/qwen-local",
    )
    try:
        persistent_ids = [
            agent["id"]
            for agent in json.loads(config_path.read_text())["agents"]["list"]
        ]
        ephemeral_ids = [
            agent["id"]
            for agent in json.loads(binding.config_path.read_text())["agents"]["list"]
        ]
        assert persistent_ids == ["main"]
        assert ephemeral_ids == ["orchestrator-runtime"]
    finally:
        binding.release()


def test_normal_binding_writes_only_ephemeral_config_and_not_operator_state(
    tmp_path, monkeypatch
):
    project, _runtime_root, _runtime, context = _context(tmp_path)
    config_path = tmp_path / "openclaw.json"
    _config(config_path, project=project)
    written_paths = []
    original_write_text = Path.write_text

    def record_write(path, *args, **kwargs):
        written_paths.append(path)
        return original_write_text(path, *args, **kwargs)

    monkeypatch.setattr(Path, "write_text", record_write)
    binding = bind_openclaw_workspace(
        context,
        real_config_path=config_path,
        model_ref="openai/qwen-local",
    )
    try:
        assert config_path not in written_paths
        assert binding.config_path in written_paths
        assert all(binding._tmp_dir in path.parents for path in written_paths)
    finally:
        binding.release()


@pytest.mark.parametrize(
    "protected_path",
    [
        ".openclaw/openclaw.json",
        ".openclaw/agents/main/agent/auth-profiles.json",
        "/root/.openclaw/openclaw.json",
    ],
)
def test_orchestrator_path_authority_rejects_openclaw_owned_state(protected_path):
    from app.services.orchestration.validation.path_authority import (
        PathDeclarationError,
        declare,
    )

    with pytest.raises(PathDeclarationError):
        declare(protected_path)
