"""PHASE34-C5 provider-free discovery wire-contract tests."""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.agents.providers import openai_chat_adapter
from app.services.agents.providers.openai_chat_adapter import (
    OpenAIChatCompletionsRuntime,
)
from app.services.agents.runtime_configuration import (
    BackendRole,
    InferenceRoutingPolicy,
    RoleRuntimeConfiguration,
)
from app.services.orchestration.planning.discovery_contract_capture import (
    DISCOVERY_ACTION_SCHEMA,
)
from app.services.orchestration.planning.read_only_discovery import (
    DiscoveryContractError,
    parse_discovery_request,
    run_discovery_stage,
)


class _FakeResponse:
    def __init__(self, body: dict[str, object]) -> None:
        self.status_code = 200
        self.headers = {"content-type": "application/json"}
        self.content = json.dumps(body, separators=(",", ":")).encode("utf-8")

    @property
    def text(self) -> str:
        return self.content.decode("utf-8")

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return json.loads(self.content)


class _FakeAsyncClient:
    response_body: dict[str, object] = {}
    capture_path: Path | None = None
    captured: dict[str, object] = {}

    def __init__(self, *args, **kwargs) -> None:
        del args, kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, headers=None, json=None):
        assert self.capture_path is not None
        assert self.capture_path.exists(), "request must be persisted before dispatch"
        artifact = json_module_load(self.capture_path)
        assert artifact["request"]["outbound_json_body"] == json
        self.captured.update({"url": url, "headers": dict(headers or {}), "json": json})
        return _FakeResponse(self.response_body)


def json_module_load(path: Path) -> dict[str, object]:
    return json.loads(path.read_text(encoding="utf-8"))


def _runtime(monkeypatch, capture_path: Path) -> OpenAIChatCompletionsRuntime:
    monkeypatch.setattr(settings, "LOW_RESOURCE_SINGLE_MODEL", True)
    monkeypatch.setattr(settings, "AGENT_BACKEND", "openai_chat_completions")
    monkeypatch.setattr(settings, "PLANNING_BACKEND", "openai_chat_completions")
    monkeypatch.setattr(settings, "EXECUTION_BACKEND", "openai_chat_completions")
    monkeypatch.setattr(settings, "PLANNING_DIRECT_BASE_URL", "http://llama.test/v1")
    monkeypatch.setattr(settings, "PLANNING_DIRECT_API_KEY", "")
    monkeypatch.setattr(
        settings, "PLANNING_DIRECT_MODEL", "Qwen2.5-Coder-14B-Instruct-Q5_K_M.gguf"
    )
    monkeypatch.setattr(
        settings, "PLANNER_MODEL", "Qwen2.5-Coder-14B-Instruct-Q5_K_M.gguf"
    )
    monkeypatch.setattr(
        settings, "EXECUTION_MODEL", "Qwen2.5-Coder-14B-Instruct-Q5_K_M.gguf"
    )
    _FakeAsyncClient.capture_path = capture_path
    _FakeAsyncClient.captured = {}
    monkeypatch.setattr(openai_chat_adapter.httpx, "AsyncClient", _FakeAsyncClient)
    return OpenAIChatCompletionsRuntime(
        None,
        session_id=None,
        runtime_configuration=RoleRuntimeConfiguration(
            role=BackendRole.PLANNING,
            backend_name="openai_chat_completions",
            model_family="Qwen2.5-Coder-14B-Instruct-Q5_K_M.gguf",
            adaptation_profile="ollama_default",
            routing_policy=InferenceRoutingPolicy.SINGLE_MODEL,
            provider_endpoint="http://llama.test/v1",
            fallback_enabled=False,
        ),
    )


def _context(tmp_path: Path, runtime: OpenAIChatCompletionsRuntime):
    return SimpleNamespace(
        read_only_discovery_completed=False,
        runtime_service=runtime,
        prompt="Inspect the existing implementation.",
        orchestration_state=SimpleNamespace(
            project_context="A tiny deterministic Python fixture.",
            project_dir=tmp_path,
        ),
        emit_live=lambda *args, **kwargs: None,
        session_id=1,
        task_id=2,
        task_execution_id=3,
    )


def _planner():
    class _Planner:
        @staticmethod
        async def _execute_task_with_planning_lock(*args, **kwargs):
            runtime = args[0]
            return await runtime.execute_task(args[1], **kwargs)

    return _Planner


def test_live_shaped_capture_is_artifact_first_and_records_parser_success(
    tmp_path, monkeypatch
):
    artifact_path = tmp_path / "live-discovery-contract.json"
    _FakeAsyncClient.response_body = {
        "id": "chatcmpl-test",
        "model": "Qwen2.5-Coder-14B-Instruct-Q5_K_M.gguf",
        "choices": [
            {
                "message": {"content": '{"action":"stop"}'},
                "finish_reason": "stop",
            }
        ],
    }
    runtime = _runtime(monkeypatch, artifact_path)

    observation = run_discovery_stage(
        ctx=_context(tmp_path, runtime),
        planning_timeout_seconds=120,
        extract_structured_text=lambda value: str(value),
        planner_service=_planner(),
        emit_phase_event=lambda *args, **kwargs: None,
        capture_path=artifact_path,
    )

    assert observation.action == "stop"
    artifact = json_module_load(artifact_path)
    assert artifact["request"]["backend_role"] == "planning"
    assert artifact["request"]["low_resource_single_model"] is True
    assert artifact["request"]["outbound_json_body"]["response_format"]["type"] == (
        "json_schema"
    )
    assert artifact["response"]["http_status"] == 200
    assert artifact["response"]["raw_body_text"]
    assert artifact["response"]["response_model"] == (
        "Qwen2.5-Coder-14B-Instruct-Q5_K_M.gguf"
    )
    assert artifact["stages"]["message_content"] == '{"action":"stop"}'
    assert artifact["stages"]["extracted_content"] == '{"action":"stop"}'
    assert artifact["stages"]["normalized_content"] == '{"action":"stop"}'
    assert artifact["stages"]["parser_input"] == '{"action":"stop"}'
    assert artifact["parser"]["success"] is True
    assert artifact["parser"]["action"] == "stop"


def test_raw_response_survives_content_extraction_exception(tmp_path, monkeypatch):
    artifact_path = tmp_path / "extraction-failure.json"
    _FakeAsyncClient.response_body = {
        "model": "Qwen2.5-Coder-14B-Instruct-Q5_K_M.gguf",
        "choices": [{"message": {"content": "provider content"}}],
    }
    runtime = _runtime(monkeypatch, artifact_path)

    def fail_extraction(body):
        del body
        raise RuntimeError("synthetic extraction failure")

    monkeypatch.setattr(
        openai_chat_adapter, "_extract_chat_completion_content", fail_extraction
    )

    with pytest.raises(RuntimeError, match="synthetic extraction failure"):
        import asyncio

        asyncio.run(
            runtime.execute_task(
                "Return one discovery action.",
                timeout_seconds=120,
                diagnostic_label="PLANNING_DISCOVERY",
                diagnostic_metadata={
                    "discovery_contract_capture_path": str(artifact_path)
                },
                invocation_options=runtime_invocation_options(),
            )
        )

    artifact = json_module_load(artifact_path)
    assert artifact["response"]["raw_body_text"]
    assert artifact["response"]["http_status"] == 200


def test_extracted_content_survives_parser_failure_with_deterministic_reason(
    tmp_path, monkeypatch
):
    artifact_path = tmp_path / "parser-failure.json"
    _FakeAsyncClient.response_body = {
        "model": "Qwen2.5-Coder-14B-Instruct-Q5_K_M.gguf",
        "choices": [
            {
                "message": {"content": 'Here is the action: {"action":"stop"}'},
                "finish_reason": "stop",
            }
        ],
    }
    runtime = _runtime(monkeypatch, artifact_path)

    with pytest.raises(DiscoveryContractError, match="discovery_output_not_json"):
        run_discovery_stage(
            ctx=_context(tmp_path, runtime),
            planning_timeout_seconds=120,
            extract_structured_text=lambda value: str(value),
            planner_service=_planner(),
            emit_phase_event=lambda *args, **kwargs: None,
            capture_path=artifact_path,
        )

    artifact = json_module_load(artifact_path)
    assert artifact["response"]["raw_body_text"]
    assert artifact["stages"]["extracted_content"].startswith("Here is")
    assert artifact["stages"]["parser_input"].startswith("Here is")
    assert artifact["parser"]["success"] is False
    assert artifact["parser"]["reason"] == "discovery_output_not_json"


def runtime_invocation_options():
    from app.services.agents.runtime_invocation import RuntimeInvocationOptions

    return RuntimeInvocationOptions(
        extra_provider_options={"response_format": {"type": "json_object"}},
        response_schema=DISCOVERY_ACTION_SCHEMA,
    )


def test_discovery_schema_is_wire_shape_only_and_parser_remains_authoritative():
    assert set(DISCOVERY_ACTION_SCHEMA) == {"$schema", "title", "type", "oneOf"}
    schema_text = json.dumps(DISCOVERY_ACTION_SCHEMA)
    for forbidden in ("expected", "authorize", "mutation", "source_version", "apa"):
        assert forbidden not in schema_text.lower()

    assert parse_discovery_request('{"action":"stop"}').action == "stop"
    with pytest.raises(DiscoveryContractError, match="discovery_stop_has_extra_fields"):
        parse_discovery_request('{"action":"stop","expected":true}')
