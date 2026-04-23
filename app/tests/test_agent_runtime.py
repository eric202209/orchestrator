from app.config import settings
from app.services.agents.agent_backends import UnsupportedAgentBackendError
from app.services.agents.interfaces import AgentRuntimeError
from app.services.agents.agent_runtime import (
    build_runtime_cli_agent_command,
    create_agent_runtime,
    invoke_runtime_prompt,
    runtime_reports_context_overflow,
)
from app.services.agents.providers import get_runtime_factory
from app.services.agents.providers.openai_adapter import OpenAIResponsesRuntime
from app.services.agents.openclaw_service import (
    OpenClawSessionError,
    OpenClawSessionService,
)
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


def test_create_agent_runtime_supports_openai_backend(db_session, monkeypatch):
    monkeypatch.setattr(settings, "ORCHESTRATOR_AGENT_BACKEND", "openai_responses_api")
    runtime = create_agent_runtime(db_session, session_id=None)
    assert isinstance(runtime, OpenAIResponsesRuntime)
    assert runtime.backend_descriptor.name == "openai_responses_api"


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


def test_runtime_reports_context_overflow_matches_openclaw_detector(db_session):
    from app.services.agents.openclaw_service import OpenClawSessionService

    assert runtime_reports_context_overflow(
        db_session, {"error": "Context window exceeded"}
    ) == OpenClawSessionService._is_context_overflow_result(
        {"error": "Context window exceeded"}
    )
    assert not runtime_reports_context_overflow(
        db_session, {"error": "Connection refused"}
    )


def test_openclaw_error_is_runtime_neutral():
    assert issubclass(OpenClawSessionError, AgentRuntimeError)


def test_provider_registry_exposes_runtime_factory():
    assert get_runtime_factory("local_openclaw") is not None
    assert get_runtime_factory("remote_openclaw_gateway") is not None
    assert get_runtime_factory("openai_responses_api") is not None
    assert get_runtime_factory("unknown_backend") is None


def test_invoke_runtime_prompt_supports_openai_backend(db_session, monkeypatch):
    monkeypatch.setattr(settings, "ORCHESTRATOR_AGENT_BACKEND", "openai_responses_api")
    monkeypatch.setattr(settings, "OPENAI_API_KEY", "test-key")
    monkeypatch.setattr(settings, "ORCHESTRATOR_AGENT_MODEL_FAMILY", "gpt-5")

    class _FakeResponse:
        status_code = 200

        def json(self):
            return {
                "id": "resp_123",
                "output_text": '{"requirements":"# Requirements"}',
            }

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def post(self, url, headers=None, json=None):
            assert url.endswith("/responses")
            assert headers["Authorization"] == "Bearer test-key"
            assert json["model"] == "gpt-5"
            return _FakeResponse()

    monkeypatch.setattr(
        "app.services.agents.providers.openai_adapter.httpx.AsyncClient",
        _FakeAsyncClient,
    )

    result = invoke_runtime_prompt(db_session, "Return JSON only")

    assert result["status"] == "completed"
    assert result["response_id"] == "resp_123"
    assert result["backend"] == "openai_responses_api"
