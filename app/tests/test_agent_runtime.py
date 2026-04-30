from app.config import settings
from app.models import Project, Session as SessionModel, Task, TaskStatus
from app.services.agents.agent_backends import UnsupportedAgentBackendError
from app.services.agents.interfaces import AgentRuntimeError, UnsupportedCapabilityError
from app.services.agents.agent_runtime import (
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


def test_unsupported_capability_error_is_runtime_neutral():
    assert issubclass(UnsupportedCapabilityError, AgentRuntimeError)


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


def test_openclaw_stderr_noise_filter_hides_json_telemetry_lines():
    assert (
        OpenClawSessionService._should_emit_stderr_line('"schemaChars": 2799,') is False
    )
    assert (
        OpenClawSessionService._should_emit_stderr_line(
            '"path": "/root/.openclaw/workspace/AGENTS.md",'
        )
        is False
    )
    assert (
        OpenClawSessionService._should_emit_stderr_line(
            "[OPENCLAW] embedded run failover decision: runId=abc"
        )
        is True
    )
    assert (
        OpenClawSessionService._should_emit_stderr_line(
            '"finalAssistantVisibleText": "```json..."'
        )
        is True
    )


def test_provider_registry_exposes_runtime_factory():
    assert get_runtime_factory("local_openclaw") is not None
    assert get_runtime_factory("remote_openclaw_gateway") is not None
    assert get_runtime_factory("openai_responses_api") is not None
    assert get_runtime_factory("unknown_backend") is None


def test_permission_request_emits_waiting_for_input_event(
    db_session, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        "app.services.workspace.project_isolation_service.get_effective_workspace_root",
        lambda db=None: tmp_path,
    )

    project = Project(name="Permission Events", workspace_path="permission-events")
    db_session.add(project)
    db_session.commit()
    db_session.refresh(project)

    task = Task(
        project_id=project.id,
        title="Permission gated task",
        status=TaskStatus.RUNNING,
        task_subfolder="task-9",
    )
    db_session.add(task)
    db_session.commit()
    db_session.refresh(task)

    session = SessionModel(
        project_id=project.id,
        name="Permission Session",
        description="needs approval",
        status="running",
        is_active=True,
        execution_mode="manual",
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)

    class _FakePermission:
        id = 77

    class _FakePermissionService:
        def __init__(self, db):
            self.db = db

        def check_permission_required(self, operation_type, target_path):
            return True

        def is_permission_granted(
            self, project_id, operation_type, target_path, session_id
        ):
            return False

        def create_permission_request(self, **kwargs):
            return _FakePermission()

    monkeypatch.setattr(
        "app.services.agents.openclaw_service.PermissionApprovalService",
        _FakePermissionService,
    )

    service = OpenClawSessionService(db_session, session.id, task.id)

    import asyncio
    import json

    granted = asyncio.run(
        service._check_and_request_permission(
            operation_type="write_file",
            target_path="src/app.py",
            command="echo hi > src/app.py",
            description="Write src/app.py",
        )
    )

    assert granted is False

    log_path = (
        tmp_path
        / "permission-events"
        / ".openclaw"
        / "events"
        / f"session_{session.id}_task_{task.id}.jsonl"
    )
    lines = [
        json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()
    ]

    assert lines[-1]["event_type"] == "waiting_for_input"
    assert lines[-1]["details"]["kind"] == "permission_request"
    assert lines[-1]["details"]["permission_request_id"] == 77


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
