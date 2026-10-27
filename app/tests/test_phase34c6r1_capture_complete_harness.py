"""PHASE34-C6R1 provider-free capture-path and parser proofs."""

from __future__ import annotations

import asyncio
import json as json_module
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
from app.services.agents.runtime_invocation import RuntimeInvocationOptions
from app.services.orchestration.planning import read_only_discovery
from app.services.orchestration.planning.discovery_contract_capture import (
    DISCOVERY_ACTION_SCHEMA,
)
from app.services.orchestration.planning.read_only_discovery import (
    DiscoveryContractError,
)

from app.tests.phase34c6r1_capture_harness import bind_discovery_capture


class _FakeResponse:
    def __init__(self, body: dict[str, object]) -> None:
        self.status_code = 200
        self.headers = {"content-type": "application/json"}
        self.content = json_module.dumps(body, separators=(",", ":")).encode("utf-8")

    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict[str, object]:
        return json_module.loads(self.content)


class _FakeAsyncClient:
    response_body: dict[str, object] = {}
    capture_path: Path | None = None

    def __init__(self, *args, **kwargs) -> None:
        del args, kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, headers=None, json=None):
        del url, headers
        assert self.capture_path is not None
        assert self.capture_path.exists()
        artifact = json_module.loads(self.capture_path.read_text(encoding="utf-8"))
        assert artifact["request"]["outbound_json_body"] == json
        return _FakeResponse(self.response_body)


def _runtime(monkeypatch) -> OpenAIChatCompletionsRuntime:
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
        prompt="Inspect tiny_calc.py.",
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


def _options() -> RuntimeInvocationOptions:
    return RuntimeInvocationOptions(
        extra_provider_options={"response_format": {"type": "json_object"}},
        response_schema=DISCOVERY_ACTION_SCHEMA,
    )


def _run_discovery(tmp_path, runtime):
    return read_only_discovery.run_discovery_stage(
        ctx=_context(tmp_path, runtime),
        planning_timeout_seconds=120,
        extract_structured_text=lambda value: str(value),
        planner_service=_planner(),
        emit_phase_event=lambda *args, **kwargs: None,
    )


def test_c6r1_canonical_discovery_uses_production_parser_and_capture(
    tmp_path, monkeypatch
):
    (tmp_path / "test_tiny_calc.py").write_text(
        "from tiny_calc import answer\n", encoding="utf-8"
    )
    artifact_path = tmp_path / "evidence" / "live-discovery-contract.json"
    _FakeAsyncClient.response_body = {
        "model": "Qwen2.5-Coder-14B-Instruct-Q5_K_M.gguf",
        "choices": [
            {
                "message": {
                    "content": '{"action":"search_text","query":"answer","paths":["test_tiny_calc.py"]}'
                },
                "finish_reason": "stop",
            }
        ],
    }
    _FakeAsyncClient.capture_path = artifact_path
    runtime = _runtime(monkeypatch)
    parser_calls: list[str] = []
    original_parser = read_only_discovery.parse_discovery_request

    def tracked_parser(value: str):
        parser_calls.append(value)
        return original_parser(value)

    monkeypatch.setattr(read_only_discovery, "parse_discovery_request", tracked_parser)

    with bind_discovery_capture(artifact_path) as binding:
        observation = _run_discovery(tmp_path, runtime)

    assert binding.incoming_capture_path is None
    assert binding.injected is True
    assert observation.action == "search_text"
    assert parser_calls == [
        '{"action":"search_text","query":"answer","paths":["test_tiny_calc.py"]}'
    ]
    artifact = json_module.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact["response"]["raw_response_captured"] is True
    assert artifact["stages"]["parser_input"] == parser_calls[0]
    assert artifact["parser"] == {
        "success": True,
        "reason": None,
        "action": "search_text",
    }
    assert artifact["action"]["validation_pass"] is True
    assert artifact["action"]["executable"] is True


def test_c6r1_malformed_discovery_retains_raw_before_production_parser_failure(
    tmp_path, monkeypatch
):
    artifact_path = tmp_path / "evidence" / "malformed-discovery.json"
    _FakeAsyncClient.response_body = {
        "model": "Qwen2.5-Coder-14B-Instruct-Q5_K_M.gguf",
        "choices": [
            {
                "message": {"content": "Here is the action: not JSON"},
                "finish_reason": "stop",
            }
        ],
    }
    _FakeAsyncClient.capture_path = artifact_path
    runtime = _runtime(monkeypatch)

    with bind_discovery_capture(artifact_path):
        with pytest.raises(DiscoveryContractError, match="discovery_output_not_json"):
            _run_discovery(tmp_path, runtime)

    artifact = json_module.loads(artifact_path.read_text(encoding="utf-8"))
    assert artifact["response"]["raw_response_captured"] is True
    assert artifact["stages"]["extracted_content"] == "Here is the action: not JSON"
    assert artifact["parser"]["success"] is False
    assert artifact["parser"]["reason"] == "discovery_output_not_json"
    assert not (tmp_path / "tiny_calc.py").exists()


def test_c6r1_binding_is_certification_plumbing_only():
    assert "parse_discovery_request" not in bind_discovery_capture.__code__.co_names
