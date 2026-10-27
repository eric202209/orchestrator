"""PHASE34-C4 provider-free discovery output contract replay."""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.agents.providers import openai_chat_adapter
from app.services.agents.providers.openai_chat_adapter import (
    OpenAIChatCompletionsRuntime,
    _extract_chat_completion_content,
)
from app.services.agents.runtime_configuration import (
    BackendRole,
    InferenceRoutingPolicy,
    RoleRuntimeConfiguration,
)
from app.services.agents.runtime_invocation import RuntimeInvocationOptions
from app.services.orchestration.planning.read_only_discovery import (
    DiscoveryContractError,
    build_discovery_prompt,
    run_discovery_stage,
    parse_discovery_request,
)


class _FakeResponse:
    def raise_for_status(self) -> None:
        return None

    def json(self) -> dict:
        return {
            "id": "chatcmpl-discovery-fixture",
            "model": "Qwen2.5-Coder-14B-Instruct-Q5_K_M.gguf",
            "choices": [
                {
                    "message": {
                        "content": (
                            "Here is the read-only action:\n"
                            '```json\n{"action":"stop"}\n```'
                        )
                    }
                }
            ],
        }


class _FakeAsyncClient:
    captured: dict[str, object] = {}

    def __init__(self, *args, **kwargs) -> None:
        del args, kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, headers=None, json=None):
        self.captured.update({"url": url, "headers": dict(headers or {}), "json": json})
        return _FakeResponse()


@pytest.mark.asyncio
async def test_discovery_replay_requires_json_object_response_format(
    monkeypatch,
):
    """RED: the captured C3-like envelope fails the real strict parser."""

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
    _FakeAsyncClient.captured = {}

    runtime = OpenAIChatCompletionsRuntime(
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
    prompt = build_discovery_prompt(
        "Implement the function answer() that returns 42 | order=1 | P1 | effort=low | .",
        "One small Python source and focused test created by structured Orchestrator mutation.",
    )

    result = await runtime.execute_task(
        prompt,
        timeout_seconds=120,
        diagnostic_label="PLANNING_DISCOVERY",
        diagnostic_metadata={"stage": "read_only_discovery"},
        invocation_options=RuntimeInvocationOptions(
            extra_provider_options={"response_format": {"type": "json_object"}}
        ),
    )
    extracted = _extract_chat_completion_content(
        {
            "choices": [
                {"message": {"content": result["output"]}},
            ]
        }
    )
    with pytest.raises(DiscoveryContractError, match="discovery_output_not_json"):
        parse_discovery_request(extracted)

    payload = _FakeAsyncClient.captured["json"]
    assert isinstance(payload, dict)
    assert payload["model"] == "Qwen2.5-Coder-14B-Instruct-Q5_K_M.gguf"
    assert "response_format" in payload
    assert payload["response_format"] == {"type": "json_object"}


def test_production_discovery_stage_passes_only_bounded_json_option(
    tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "LOW_RESOURCE_SINGLE_MODEL", True)
    captured: dict[str, object] = {}

    class _Planner:
        @staticmethod
        async def _execute_task_with_planning_lock(*args, **kwargs):
            captured.update(kwargs)
            return {"status": "completed", "output": '{"action":"stop"}'}

    ctx = SimpleNamespace(
        read_only_discovery_completed=False,
        runtime_service=object(),
        prompt="Implement answer() in answer.py.",
        orchestration_state=SimpleNamespace(
            project_context="A tiny deterministic Python fixture.",
            project_dir=tmp_path,
        ),
        emit_live=lambda *args, **kwargs: None,
        session_id=1,
        task_id=2,
        task_execution_id=3,
    )

    observation = run_discovery_stage(
        ctx=ctx,
        planning_timeout_seconds=120,
        extract_structured_text=lambda value: str(value),
        planner_service=_Planner,
        emit_phase_event=lambda *args, **kwargs: None,
    )

    assert observation.action == "stop"
    options = captured["invocation_options"]
    assert isinstance(options, RuntimeInvocationOptions)
    assert dict(options.extra_provider_options or {}) == {
        "response_format": {"type": "json_object"}
    }


def test_legacy_discovery_stage_omits_l1_json_option(tmp_path, monkeypatch):
    monkeypatch.setattr(settings, "LOW_RESOURCE_SINGLE_MODEL", False)
    captured: dict[str, object] = {}

    class _Planner:
        @staticmethod
        async def _execute_task_with_planning_lock(*args, **kwargs):
            captured.update(kwargs)
            return {"status": "completed", "output": '{"action":"stop"}'}

    ctx = SimpleNamespace(
        read_only_discovery_completed=False,
        runtime_service=object(),
        prompt="Inspect the existing implementation.",
        orchestration_state=SimpleNamespace(
            project_context="A legacy multi-model fixture.",
            project_dir=tmp_path,
        ),
        emit_live=lambda *args, **kwargs: None,
        session_id=4,
        task_id=5,
        task_execution_id=6,
    )

    observation = run_discovery_stage(
        ctx=ctx,
        planning_timeout_seconds=120,
        extract_structured_text=lambda value: str(value),
        planner_service=_Planner,
        emit_phase_event=lambda *args, **kwargs: None,
    )

    assert observation.action == "stop"
    assert "invocation_options" not in captured


@pytest.mark.asyncio
async def test_ordinary_execution_request_does_not_receive_discovery_format(
    monkeypatch,
):
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
    _FakeAsyncClient.captured = {}

    runtime = OpenAIChatCompletionsRuntime(
        None,
        session_id=None,
        runtime_configuration=RoleRuntimeConfiguration(
            role=BackendRole.EXECUTION,
            backend_name="openai_chat_completions",
            model_family="Qwen2.5-Coder-14B-Instruct-Q5_K_M.gguf",
            adaptation_profile="ollama_default",
            routing_policy=InferenceRoutingPolicy.SINGLE_MODEL,
            provider_endpoint="http://llama.test/v1",
            fallback_enabled=False,
        ),
    )
    await runtime.execute_task(
        "Execute the accepted implementation step.",
        timeout_seconds=120,
        diagnostic_label="EXECUTION_STEP",
    )

    payload = _FakeAsyncClient.captured["json"]
    assert isinstance(payload, dict)
    assert "response_format" not in payload
