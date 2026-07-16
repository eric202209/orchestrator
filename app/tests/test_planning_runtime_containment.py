import json
import subprocess
from pathlib import Path

import pytest

from app.models import Project
from app.services.agents.openclaw_service import OpenClawSessionService


@pytest.mark.asyncio
async def test_planning_openclaw_uses_hydrated_disposable_runtime_and_cleans_it(
    db_session, tmp_path, monkeypatch
):
    canonical = tmp_path / "canonical"
    canonical.mkdir()
    (canonical / "project.txt").write_text("canonical content\n", encoding="utf-8")
    project = Project(
        name="Planning Containment",
        workspace_path=str(canonical),
    )
    db_session.add(project)
    db_session.commit()

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

    observed = {}

    def fake_bind(context):
        observed["context"] = context

    def fake_release():
        observed["released"] = True

    def fake_build(*args, **kwargs):
        observed["build_cwd"] = service._resolve_execution_cwd()
        return ["openclaw", "agent", "--session-id", "planning-contained"]

    async def fake_run(full_cmd, *, cwd, **kwargs):
        runtime = Path(cwd)
        observed["runtime"] = runtime
        observed["hydrated"] = (runtime / "project.txt").read_text(encoding="utf-8")
        (runtime / "AGENTS.md").write_text(
            "# AGENTS.md - Your Workspace\nThis folder is home. Treat it that way.\n",
            encoding="utf-8",
        )
        payload = json.dumps(
            {
                "payloads": [{"text": "planning artifacts"}],
                "meta": {"agentMeta": {"sessionId": "planning-contained"}},
            }
        )
        return subprocess.CompletedProcess(full_cmd, 0, payload, ""), {"cwd": cwd}

    monkeypatch.setattr(service, "bind_runtime_workspace", fake_bind)
    monkeypatch.setattr(service, "release_runtime_workspace_binding", fake_release)
    monkeypatch.setattr(service, "build_cli_agent_command", fake_build)
    monkeypatch.setattr(service, "_run_cli_prompt_with_diagnostics", fake_run)

    result = await service.invoke_prompt("plan", session_prefix="planning")

    runtime = observed["runtime"]
    assert runtime != canonical
    assert observed["build_cwd"] == str(runtime)
    assert observed["context"].project_workspace == canonical.resolve()
    assert observed["context"].runtime_workspace == runtime
    assert observed["context"].is_sandboxed is True
    assert observed["hydrated"] == "canonical content\n"
    assert result["output"] == "planning artifacts"
    assert observed["released"] is True
    assert not runtime.exists()
    assert (canonical / "project.txt").read_text(
        encoding="utf-8"
    ) == "canonical content\n"
    assert not (canonical / "AGENTS.md").exists()
