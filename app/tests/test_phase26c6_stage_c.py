"""Phase 26C-6 Stage C role-runtime repair-lane certification tests."""

from __future__ import annotations

import asyncio
import hashlib
import json
from unittest.mock import MagicMock

import pytest

from app.config import settings
from app.services.agents.agent_runtime import BackendRole, create_agent_runtime
from app.services.agents.providers.ollama_adapter import OllamaRuntime
from app.services.agents.providers.openai_chat_adapter import (
    OpenAIChatCompletionsRuntime,
)
from app.services.agents.runtime_configuration import RoleRuntimeConfiguration
from app.services.agents.runtime_invocation import RuntimeInvocationOptions
from app.services.orchestration.phases.completion_summary import _call_planning_lane
from app.services.orchestration.planning.planner import PlannerService


class _Response:
    def __init__(self, body: dict, status_code: int = 200):
        self._body = body
        self.status_code = status_code
        self.text = json.dumps(body)

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx

            request = httpx.Request("POST", "http://test/v1/chat/completions")
            response = httpx.Response(self.status_code, request=request, text=self.text)
            raise httpx.HTTPStatusError(
                "request failed", request=request, response=response
            )

    def json(self):
        return self._body


class _Client:
    captured: list[dict] = []
    response_body: dict = {"choices": [{"message": {"content": "ok"}}]}

    def __init__(self, timeout):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        return False

    async def post(self, url, **kwargs):
        self.captured.append(
            {
                "url": url,
                "timeout": self.timeout,
                "headers": kwargs.get("headers", {}),
                "json": kwargs.get("json"),
            }
        )
        return _Response(self.response_body)


def _options(*, max_output_tokens: int = 2048) -> RuntimeInvocationOptions:
    return RuntimeInvocationOptions(
        timeout_seconds=60,
        no_output_timeout_seconds=60,
        max_output_tokens=max_output_tokens,
        temperature=0.0,
        reasoning_enabled=False,
        stream=False,
    )


def _config(backend: str, role: BackendRole, model: str) -> RoleRuntimeConfiguration:
    return RoleRuntimeConfiguration(
        role=role,
        backend_name=backend,
        model_family=model,
        adaptation_profile=(
            "ollama_default" if backend == "direct_ollama" else "ollama_default"
        ),
    )


def _canonical_request(record: dict, options: RuntimeInvocationOptions) -> dict:
    return {
        "url": record["url"],
        "headers": {
            key: ("<redacted>" if key.lower() == "authorization" else value)
            for key, value in sorted(record["headers"].items())
        },
        "body": record["json"],
        "invocation_options": options.to_dict(),
        "timeout": record["timeout"],
        "response_interpretation": "choices[0].message.content, list text parts joined; empty shape -> empty string",
    }


@pytest.mark.parametrize(
    ("runtime_type", "backend"),
    [
        (OpenAIChatCompletionsRuntime, "openai_chat_completions"),
        (OllamaRuntime, "direct_ollama"),
    ],
)
def test_supported_adapters_preserve_repair_chat_contract(
    monkeypatch, runtime_type, backend
):
    _Client.captured = []
    monkeypatch.setattr(
        "app.services.agents.providers.openai_chat_adapter.httpx.AsyncClient", _Client
    )
    monkeypatch.setattr(
        "app.services.agents.providers.ollama_adapter.httpx.AsyncClient", _Client
    )
    monkeypatch.setattr(
        settings, "PLANNING_REPAIR_BASE_URL", "http://repair-gateway/v1"
    )
    monkeypatch.setattr(settings, "PLANNING_REPAIR_API_KEY", "repair-secret")
    monkeypatch.setattr(settings, "PLANNING_REPAIR_MODEL", "repair-model")
    monkeypatch.setattr(settings, "OLLAMA_BASE_URL", "http://ollama:11434")
    monkeypatch.setattr(settings, "OLLAMA_AGENT_MODEL", "ollama-model")

    runtime = runtime_type(
        None,
        None,
        runtime_configuration=_config(backend, BackendRole.REPAIR, "repair-model"),
    )
    options = _options()
    result = asyncio.run(
        runtime.invoke_prompt(
            "repair bytes",
            timeout_seconds=60,
            session_prefix="planning-repair",
            invocation_options=options,
        )
    )

    record = _Client.captured[-1]
    assert result["output"] == "ok"
    assert result["role"] == "repair"
    assert record["url"] == "http://repair-gateway/v1/chat/completions"
    assert record["timeout"] == 60
    assert record["headers"] == {
        "Authorization": "Bearer repair-secret",
        "Content-Type": "application/json",
    }
    assert record["json"] == {
        "model": "repair-model",
        "messages": [{"role": "user", "content": "repair bytes"}],
        "temperature": 0.0,
        "max_tokens": 2048,
        "stream": False,
        "think": False,
        "enable_thinking": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }


def test_openai_chat_repair_retains_redacted_response_shape_observability(
    db_session, monkeypatch
):
    _Client.captured = []
    _Client.response_body = {
        "id": "chatcmpl-test",
        "choices": [
            {
                "index": 0,
                "message": {"role": "assistant", "content": "[]"},
            }
        ],
    }
    monkeypatch.setattr(
        "app.services.agents.providers.openai_chat_adapter.httpx.AsyncClient", _Client
    )
    runtime = OpenAIChatCompletionsRuntime(
        db_session,
        None,
        runtime_configuration=_config(
            "openai_chat_completions", BackendRole.DEBUG_REPAIR, "debug-model"
        ),
    )

    result = asyncio.run(
        runtime.invoke_prompt(
            "debug bytes",
            timeout_seconds=45,
            session_prefix="debug-repair",
            invocation_options=_options(),
        )
    )

    evidence = result["provider_response_observability"]
    assert result["output"] == "[]"
    assert evidence["raw_response_type"] == "dict"
    assert evidence["raw_top_level_json_type"] == "dict"
    assert evidence["raw_top_level_keys"] == ["choices", "id"]
    assert evidence["raw_nested_candidate_keys"]["choices[0]"] == [
        "index",
        "message",
    ]
    assert evidence["content_type"] == "str"
    assert evidence["normalization_branch"] == "choices_message_content_string"
    assert evidence["normalized_response_length"] == 2
    assert '"[]"' not in json.dumps(evidence)
    _Client.response_body = {"choices": [{"message": {"content": "ok"}}]}


def test_invocation_options_are_typed_secret_free_and_reject_unsupported_values():
    options = _options()
    assert options.to_dict()["extra_provider_options"] is None
    with pytest.raises(ValueError, match="unsupported provider"):
        RuntimeInvocationOptions(extra_provider_options={"unknown": True})
    with pytest.raises(ValueError, match="secrets"):
        RuntimeInvocationOptions(extra_provider_options={"api_key": "secret"})
    with pytest.raises(ValueError, match="streaming"):
        RuntimeInvocationOptions(stream=True)


def test_planning_repair_uses_explicit_repair_runtime_and_registry(
    db_session, monkeypatch
):
    _Client.captured = []
    monkeypatch.setattr(
        "app.services.agents.providers.openai_chat_adapter.httpx.AsyncClient", _Client
    )
    monkeypatch.setattr(settings, "REPAIR_BACKEND", "openai_chat_completions")
    monkeypatch.setattr(
        settings, "PLANNING_REPAIR_BASE_URL", "http://repair-gateway/v1"
    )
    monkeypatch.setattr(settings, "PLANNING_REPAIR_API_KEY", "")
    monkeypatch.setattr(settings, "PLANNING_REPAIR_MODEL", "repair-model")

    runtime_service = MagicMock(db=db_session, session_id=None, task_id=None)
    result = asyncio.run(
        PlannerService._invoke_repair_prompt(runtime_service, "repair bytes", 60)
    )

    assert result["planning_repair_runtime_role"] == "repair"
    assert result["planning_repair_direct"] is False
    assert _Client.captured[-1]["json"]["model"] == "repair-model"


def test_debug_repair_uses_debug_role_and_preserves_fallback_model(
    db_session, monkeypatch
):
    _Client.captured = []
    monkeypatch.setattr(
        "app.services.agents.providers.openai_chat_adapter.httpx.AsyncClient", _Client
    )
    monkeypatch.setattr(settings, "DEBUG_REPAIR_BACKEND", "openai_chat_completions")
    monkeypatch.setattr(settings, "DEBUG_REPAIR_MODEL", "")
    monkeypatch.setattr(settings, "PLANNING_REPAIR_MODEL", "repair-fallback-model")
    monkeypatch.setattr(settings, "DEBUG_REPAIR_BASE_URL", "http://debug-gateway/v1")
    monkeypatch.setattr(settings, "DEBUG_REPAIR_API_KEY", "")
    monkeypatch.setattr(settings, "DEBUG_REPAIR_DISABLE_THINKING", True)

    runtime = create_agent_runtime(
        db_session, None, None, role=BackendRole.DEBUG_REPAIR
    )
    result = asyncio.run(
        runtime.invoke_prompt(
            "debug bytes",
            timeout_seconds=45,
            session_prefix="debug-repair",
            invocation_options=_options(),
        )
    )

    assert result["role"] == "debug_repair"
    assert result["model_family"] == "repair-fallback-model"
    assert _Client.captured[-1]["url"] == "http://debug-gateway/v1/chat/completions"
    assert _Client.captured[-1]["json"]["think"] is False


def test_completion_summary_uses_repair_role_and_adapter_extraction(
    db_session, monkeypatch
):
    _Client.captured = []
    _Client.response_body = {
        "choices": [{"message": {"content": [{"text": "part-1"}, {"text": "part-2"}]}}]
    }
    monkeypatch.setattr(
        "app.services.agents.providers.openai_chat_adapter.httpx.AsyncClient", _Client
    )
    monkeypatch.setattr(settings, "REPAIR_BACKEND", "openai_chat_completions")
    monkeypatch.setattr(
        settings, "PLANNING_REPAIR_BASE_URL", "http://repair-gateway/v1"
    )
    monkeypatch.setattr(settings, "PLANNING_REPAIR_MODEL", "summary-model")

    result = asyncio.run(_call_planning_lane("summary bytes", db=db_session))

    assert result == "part-1part-2"
    assert _Client.captured[-1]["json"]["max_tokens"] == 512
    _Client.response_body = {"choices": [{"message": {"content": "ok"}}]}


def test_canonical_contract_digest_is_secret_free(monkeypatch):
    record = {
        "url": "http://repair-gateway/v1/chat/completions",
        "headers": {
            "Content-Type": "application/json",
            "Authorization": "Bearer secret",
        },
        "json": {
            "model": "repair-model",
            "messages": [{"role": "user", "content": "repair bytes"}],
        },
        "timeout": 60,
    }
    canonical = _canonical_request(record, _options())
    encoded = json.dumps(canonical, sort_keys=True, separators=(",", ":")).encode()
    assert "secret" not in json.dumps(canonical)
    assert hashlib.sha256(encoded).hexdigest()
