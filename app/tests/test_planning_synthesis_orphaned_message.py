import json
import subprocess

import pytest

from app.models import Project
from app.services.agents.openclaw_service import OpenClawSessionService


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
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "list": [
                        {
                            "id": "orchestrator",
                            "workspace": str(canonical),
                        }
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(config_path))

    service = object.__new__(OpenClawSessionService)
    service.db = db_session
    service.session_id = None
    service.task_id = None
    service.task_execution_id = None
    service.session_model = None
    service.task_model = None
    service.project_id = project.id
    service.execution_cwd_override = None
    service._workspace_binding = None
    service._openclaw_config_path_override = None
    service._log_entry = lambda *args, **kwargs: None

    observed: list[tuple[str, str]] = []

    async def fake_run(full_cmd, **kwargs):
        invocation_id = full_cmd[full_cmd.index("--session-id") + 1]
        bound_config = json.loads(
            service._openclaw_config_path_override.read_text(encoding="utf-8")
        )
        observed.append((invocation_id, bound_config["session"]["mainKey"]))
        payload = json.dumps(
            {
                "payloads": [{"text": "same result"}],
                "meta": {"agentMeta": {"sessionId": invocation_id}},
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
