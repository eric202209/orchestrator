from app.config import settings
from app.services.agents.agent_backends import UnsupportedAgentBackendError
from app.services.agents.agent_runtime import (
    build_runtime_cli_agent_command,
    create_agent_runtime,
    runtime_reports_context_overflow,
)
from app.services.agents.openclaw_service import OpenClawSessionService
from app.services.workspace.system_settings import AGENT_BACKEND_KEY, set_setting_value


def test_create_agent_runtime_uses_configured_local_backend(db_session):
    runtime = create_agent_runtime(db_session, session_id=None)

    assert isinstance(runtime, OpenClawSessionService)
    assert runtime.backend_descriptor.name == settings.ORCHESTRATOR_AGENT_BACKEND


def test_create_agent_runtime_rejects_unknown_backend(db_session, monkeypatch):
    monkeypatch.setattr(settings, "ORCHESTRATOR_AGENT_BACKEND", "unknown_backend")

    try:
        create_agent_runtime(db_session, session_id=None)
    except UnsupportedAgentBackendError as exc:
        assert "Unsupported orchestration backend" in str(exc)
        return

    raise AssertionError("Expected UnsupportedAgentBackendError")


def test_create_agent_runtime_uses_db_backend_override(db_session, monkeypatch):
    monkeypatch.setattr(settings, "ORCHESTRATOR_AGENT_BACKEND", "unknown_backend")
    set_setting_value(db_session, AGENT_BACKEND_KEY, "local_openclaw")

    runtime = create_agent_runtime(db_session, session_id=None)

    assert isinstance(runtime, OpenClawSessionService)
    assert runtime.backend_descriptor.name == "local_openclaw"


def test_build_runtime_cli_agent_command_uses_active_runtime(db_session, monkeypatch):
    monkeypatch.setattr(
        OpenClawSessionService,
        "_resolve_openclaw_command",
        lambda self: ["/usr/bin/openclaw"],
    )

    command = build_runtime_cli_agent_command(
        db_session,
        "Generate planning artifacts",
        source_brain="local",
        timeout_seconds=90,
    )

    assert command[:3] == ["/usr/bin/openclaw", "agent", "--local"]
    assert "--timeout" in command
    assert "90" in command


def test_runtime_reports_context_overflow_matches_openclaw_detector():
    assert runtime_reports_context_overflow({"error": "Context window exceeded"})
    assert not runtime_reports_context_overflow({"error": "Connection refused"})
