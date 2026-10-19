import json

from app.services.agents.agent_backends import (
    UnsupportedAgentBackendError,
    get_backend_descriptor,
    list_supported_backends,
)
from app.services.agents.openclaw_service import OpenClawSessionService
from app.services.observability.openclaw_diagnostics import OpenClawDiagnosticEvent
from app.services.orchestration.execution.runtime_context import RuntimeExecutorContext
from app.models import Project, Session as SessionModel
from app.config import settings
from app.services.model_adaptation import resolve_adaptation_profile


def test_default_backend_descriptor_is_local_openclaw():
    descriptor = get_backend_descriptor(None)

    assert descriptor.name == "local_openclaw"
    assert descriptor.health.status in {"ready", "degraded"}
    assert descriptor.capabilities.supports_planning is True
    assert descriptor.capabilities.supports_checkpoint_resume is True
    assert descriptor.lane_traits.structured_output_reliability == "variable"
    assert descriptor.to_dict()["lane_traits"]["configured_available"] in {
        True,
        False,
    }


def test_unknown_backend_is_rejected():
    try:
        get_backend_descriptor("future_backend")
    except UnsupportedAgentBackendError as exc:
        assert "Unsupported orchestration backend" in str(exc)
        return

    raise AssertionError("Expected UnsupportedAgentBackendError")


def test_supported_backends_contains_registered_future_metadata():
    descriptors = list_supported_backends()
    names = [descriptor.name for descriptor in descriptors]

    assert "local_openclaw" in names
    assert "openai_responses_api" in names
    assert "openai_chat_completions" in names
    backend = next(
        descriptor
        for descriptor in descriptors
        if descriptor.name == "openai_responses_api"
    )
    assert backend.implemented is True
    assert backend.config.transport_mode == "api"
    assert backend.config.supported_prompt_format == "structured_prompt_envelope"
    assert backend.config.prompt_dialect == "responses_json"
    assert backend.config.tool_call_shape == "responses_tools"
    assert backend.lane_traits.evidence_following == "strong"
    assert backend.health.status in {"ready", "degraded"}

    chat_backend = next(
        descriptor
        for descriptor in descriptors
        if descriptor.name == "openai_chat_completions"
    )
    assert chat_backend.implemented is True
    assert chat_backend.config.prompt_dialect == "openai_chat_completions"
    assert chat_backend.config.auth_mode == "optional_api_key"


def test_resolve_adaptation_profile_prefers_matching_backend_and_model_family():
    profile = resolve_adaptation_profile(
        backend="openai_responses_api",
        model_family="gpt-5.5",
        preferred_name="openai_responses_structured",
    )

    assert profile.backend == "openai_responses_api"
    assert profile.name == "openai_responses_structured"
    assert profile.prompt_dialect == "responses_json"


def test_openclaw_cli_args_are_parsed_into_resolved_command(
    db_session, monkeypatch, tmp_path
):
    cli_path = tmp_path / "openclaw"
    cli_path.write_text("#!/bin/sh\n", encoding="utf-8")
    cli_path.chmod(0o755)
    monkeypatch.setattr(settings, "OPENCLAW_CLI_PATH", str(cli_path))
    monkeypatch.setattr(settings, "OPENCLAW_CLI_ARGS", '--profile "load test" --json')

    project = Project(name="CLI Args Project")
    db_session.add(project)
    db_session.flush()
    session = SessionModel(name="CLI Args Session", project_id=project.id)
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    service = OpenClawSessionService(db_session, session.id)

    assert service._resolve_openclaw_command() == [
        str(cli_path),
        "--profile",
        "load test",
        "--json",
    ]


def test_openclaw_agent_command_selects_explicit_runtime_runner(monkeypatch, tmp_path):
    project_root = tmp_path / "projects" / "product"
    runtime_root = tmp_path / "orchestrator-runtime"
    runtime_workspace = runtime_root / "tasks" / "111" / "1"
    project_root.mkdir(parents=True)
    runtime_workspace.mkdir(parents=True)
    config_path = tmp_path / "openclaw.json"
    config_path.write_text(
        json.dumps(
            {
                "agents": {
                    "list": [
                        {
                            "id": "main",
                            "workspace": str(tmp_path / "workspace"),
                        },
                        {
                            "id": "orchestrator",
                            "workspace": str(runtime_workspace),
                        },
                    ]
                }
            }
        ),
        encoding="utf-8",
    )
    monkeypatch.setenv("OPENCLAW_CONFIG_PATH", str(config_path))
    monkeypatch.setenv("OPENCLAW_RUNNER_AGENT_ID", "orchestrator")

    service = object.__new__(OpenClawSessionService)
    service._runtime_executor_context = RuntimeExecutorContext(
        executor="openclaw",
        runtime_workspace=runtime_workspace,
        project_workspace=project_root,
        project_id=111,
        task_execution_id=1,
        runtime_root=runtime_root,
        sandbox=object(),
    )
    service._workspace_binding = type("Binding", (), {"agent_id": "orchestrator"})()

    assert service._build_openclaw_agent_command(
        ["openclaw"], cwd=str(runtime_workspace)
    ) == ["openclaw", "agent", "--agent", "orchestrator"]


def test_openclaw_workspace_guard_rejects_parent_workspace(tmp_path):
    project_root = tmp_path / "workspace" / "vault" / "projects" / "orchestrator"
    project_root.mkdir(parents=True)
    logged = []
    service = object.__new__(OpenClawSessionService)
    service._log_openclaw_diagnostic = (
        lambda event, diagnostics, **kwargs: logged.append((event, diagnostics, kwargs))
    )

    result = service._apply_reported_workspace_guard(
        {"status": "completed", "output": "done"},
        reported_workspace_dir=str(tmp_path / "workspace"),
        expected_project_root=str(project_root),
    )

    assert result["status"] == "failed"
    assert result["workspace_contract_failed"] is True
    assert "outside the resolved project root" in result["error"]
    assert logged
    assert logged[0][0] is OpenClawDiagnosticEvent.WORKSPACE_GUARD
    assert logged[0][1] == {
        "diagnostic_category": "workspace_contract_failure",
        "runtime_status": "failed",
    }
    assert logged[0][2]["level"] == "ERROR"
    assert logged[0][2]["failure_category"] == "workspace_contract_failure"
