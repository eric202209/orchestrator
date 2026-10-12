import json
import subprocess

import pytest

from app.models import Project
from app.services.agents.openclaw_service import OpenClawSessionService
from app.services.agents.runtime_configuration import (
    BackendRole,
    RoleRuntimeConfiguration,
)


def test_planning_retry_session_ids_are_unique_within_one_second(monkeypatch):
    service = object.__new__(OpenClawSessionService)
    monkeypatch.setattr(service, "_resolve_openclaw_command", lambda: ["openclaw"])
    monkeypatch.setattr(service, "_resolve_execution_cwd", lambda: "/tmp/planning")
    monkeypatch.setattr(
        service,
        "_build_openclaw_agent_command",
        lambda command, cwd: [*command, "agent"],
    )
    monkeypatch.setattr("app.services.agents.openclaw_service.time.time", lambda: 100)

    first = service.build_cli_agent_command("first", session_prefix="planning")
    second = service.build_cli_agent_command("second", session_prefix="planning")

    first_id = first[first.index("--session-id") + 1]
    second_id = second[second.index("--session-id") + 1]
    assert first_id != second_id


@pytest.mark.asyncio
async def test_planning_invocations_bind_unique_openclaw_history_keys(
    db_session, tmp_path, monkeypatch
):
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    (canonical / "project.txt").write_text("project\n", encoding="utf-8")
    project = Project(name="Isolated Planning", workspace_path=str(canonical))
    db_session.add(project)
    db_session.commit()

    config_path = tmp_path / "openclaw.json"
    runtime_root = tmp_path / "runtime-root"
    runner_workspace = runtime_root / "openclaw" / "runner"
    runner_workspace.mkdir(parents=True)
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
                        },
                    }
                },
                "agents": {
                    "list": [
                        {
                            "id": "orchestrator",
                            "workspace": str(runner_workspace),
                        }
                    ]
                },
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("OPENCLAW_RUNNER_AGENT_ID", "orchestrator")

    service = object.__new__(OpenClawSessionService)
    service.db = db_session
    service.session_id = None
    service.task_id = None
    service.task_execution_id = None
    service.session_model = None
    service.task_model = None
    service.project_id = project.id
    service.execution_cwd_override = None
    service.runtime_configuration = RoleRuntimeConfiguration(
        role=BackendRole.PLANNING,
        backend_name="local_openclaw",
        model_family="qwen-local",
        adaptation_profile="openclaw_default",
    )
    service._workspace_binding = None
    service._openclaw_config_path_override = None
    service._runtime_runner_agent_id = None
    service._runtime_executor_context = None
    service._log_entry = lambda *args, **kwargs: None

    observed: list[tuple[str, str, str, str]] = []
    seen_store_paths: set[str] = set()

    async def fake_run(full_cmd, **kwargs):
        # Model OpenClaw 2026.4.10 session semantics (Phase 26E-8):
        # `session.mainKey` is normalized to "main" at config load, so an
        # explicit-agent run reuses the persistent `agent:<id>:main` store
        # entry and its pinned transcript unless the invocation binds a
        # fresh `session.store`. The response `meta.agentMeta.sessionId`
        # reports the transcript header id that actually served the run --
        # it echoes `--session-id` only for a genuinely new session file.
        invocation_id = full_cmd[full_cmd.index("--session-id") + 1]
        bound_config = json.loads(
            service._openclaw_config_path_override.read_text(encoding="utf-8")
        )
        session_config = bound_config.get("session") or {}
        store_path = str(session_config.get("store") or "")
        fresh_store = bool(store_path) and store_path not in seen_store_paths
        if store_path:
            seen_store_paths.add(store_path)
        response_session_id = (
            invocation_id if fresh_store else "orchestrator-task-2-1783533647"
        )
        binding_dir = str(service._openclaw_config_path_override.parent)
        observed.append(
            (invocation_id, session_config.get("mainKey", ""), store_path, binding_dir)
        )
        payload = json.dumps(
            {
                "payloads": [{"text": "same result"}],
                "meta": {"agentMeta": {"sessionId": response_session_id}},
            }
        )
        return subprocess.CompletedProcess(full_cmd, 0, payload, ""), {}

    monkeypatch.setattr(service, "_resolve_openclaw_command", lambda: ["openclaw"])
    monkeypatch.setattr(service, "_run_cli_prompt_with_diagnostics", fake_run)

    first = await service.invoke_prompt("same input", session_prefix="planning")
    second = await service.invoke_prompt("same input", session_prefix="planning")

    assert first["output"] == second["output"] == "same result"
    assert len(observed) == 2
    assert observed[0][0] == observed[0][1]
    assert observed[1][0] == observed[1][1]
    assert observed[0][1] != observed[1][1]
    # Phase 26E-8: each planning invocation must bind a fresh, ephemeral
    # session store -- the only boundary OpenClaw 2026.4.10 honors for
    # history isolation under an explicit agent.
    first_store, second_store = observed[0][2], observed[1][2]
    assert first_store and second_store
    assert first_store != second_store
    assert first_store.startswith(observed[0][3])
    assert second_store.startswith(observed[1][3])
