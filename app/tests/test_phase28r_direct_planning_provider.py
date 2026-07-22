"""Focused Phase 28R direct OpenAI-compatible provider tests."""

from __future__ import annotations

import json
import threading

import httpx
import pytest

from app.config import settings
from app.services.planning.planning_brief_stage import build_planning_brief_request
from app.services.planning.providers.base import (
    PlanningArtifactKind,
    PlanningProviderExecutionError,
    PlanningRequest,
    PlanningRuntimeOptions,
    ReasoningControls,
    SamplingControls,
)
from app.services.planning.providers.direct_openai_compatible import (
    DirectOpenAICompatiblePlanningProvider,
    DirectProviderConfigurationError,
)
from app.services.planning.providers.selection import create_planning_provider
from app.services.planning.structured_task_plan_stage import (
    build_structured_task_plan_request,
)


def _configure(monkeypatch):
    monkeypatch.setattr(settings, "PLANNING_DIRECT_BASE_URL", "http://gateway:8000/v1")
    monkeypatch.setattr(settings, "PLANNING_DIRECT_MODEL", "qwen-local")
    monkeypatch.setattr(settings, "PLANNING_DIRECT_API_KEY", "")
    monkeypatch.setattr(settings, "PLANNING_DIRECT_TIMEOUT_SECONDS", 360)
    monkeypatch.setattr(settings, "PLANNING_DIRECT_MAX_COMPLETION_TOKENS", 16384)
    monkeypatch.setattr(settings, "PLANNING_DIRECT_TEMPERATURE", 0.0)


def _request(
    kind=PlanningArtifactKind.PLANNING_BRIEF,
    *,
    prompt="application-owned Protocol v2 prompt",
    metadata=None,
    timeout=360,
):
    return PlanningRequest(
        artifact_kind=kind,
        prompt=prompt,
        protocol_input={"bounded": True},
        runtime_options=PlanningRuntimeOptions(timeout_seconds=timeout),
        reasoning=ReasoningControls(enabled=False),
        sampling=SamplingControls(temperature=0),
        metadata=metadata or {},
    )


class _Response:
    def __init__(self, body, *, content_type="text/event-stream", status_code=200):
        self.status_code = status_code
        self.headers = {"content-type": content_type}
        self._body = body

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def iter_bytes(self):
        if isinstance(self._body, BaseException):
            raise self._body
        yield self._body


class _Client:
    response = None
    calls = []
    timeout = None

    def __init__(self, *, timeout):
        type(self).timeout = timeout

    def __enter__(self):
        return self

    def __exit__(self, *_args):
        return False

    def stream(self, method, url, *, headers, content):
        type(self).calls.append(
            {
                "method": method,
                "url": url,
                "headers": headers,
                "body": json.loads(content),
            }
        )
        return type(self).response

    def get(self, url, *, headers):
        type(self).calls.append({"method": "GET", "url": url, "headers": headers})
        return type(self).response

    def close(self):
        return None


def _sse(*events):
    chunks = []
    for event in events:
        chunks.append(b"data: " + json.dumps(event).encode() + b"\n\n")
    chunks.append(b"data: [DONE]\n\n")
    return b"".join(chunks)


def _valid_sse(text='{"ok":true}'):
    return _sse(
        {
            "choices": [
                {"delta": {"role": "assistant", "content": text}, "finish_reason": None}
            ]
        },
        {"choices": [{"delta": {}, "finish_reason": "stop"}]},
    )


def _install_client(monkeypatch, response):
    _Client.calls = []
    _Client.response = response
    monkeypatch.setattr(
        "app.services.planning.providers.direct_openai_compatible.httpx.Client",
        _Client,
    )


def test_request_body_is_allowlisted_and_application_owned(monkeypatch):
    _configure(monkeypatch)
    provider = DirectOpenAICompatiblePlanningProvider(None)
    request = _request(prompt="EXACT APPLICATION PROMPT")

    body = provider.build_request_body(request)

    assert body == {
        "model": "qwen-local",
        "messages": [{"role": "user", "content": "EXACT APPLICATION PROMPT"}],
        "temperature": 0.0,
        "max_tokens": 16384,
        "stream": True,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    assert "tools" not in body
    assert "tool_choice" not in body
    assert "response_format" not in body
    assert "AGENTS.md" not in json.dumps(body)


def test_both_artifact_kinds_use_the_same_direct_adapter_path(monkeypatch):
    _configure(monkeypatch)
    provider = DirectOpenAICompatiblePlanningProvider(None)
    brief_request = build_planning_brief_request(
        type(
            "BriefProviderInput",
            (),
            {
                "manifest_id": "manifest:1",
                "manifest_hash": "hash",
                "manifest_schema_version": "v1",
                "sources": (),
                "stage_configuration": {},
                "schema_instructions": {},
                "project_id": None,
                "canonical_bytes": lambda self: b"{}",
                "to_dict": lambda self: {},
            },
        )()
    )
    task_input = type(
        "TaskProviderInput",
        (),
        {
            "stage_configuration": {},
            "schema_instructions": {},
            "canonical_bytes": lambda self: b"{}",
            "to_dict": lambda self: {},
            "brief_checkpoint_id": "checkpoint:1",
            "brief_hash": "brief-hash",
            "manifest_id": "manifest:1",
            "manifest_hash": "manifest-hash",
            "project_id": None,
        },
    )()
    task_request = build_structured_task_plan_request(task_input)

    assert provider.name == "direct_openai_compatible"
    assert brief_request.artifact_kind is PlanningArtifactKind.PLANNING_BRIEF
    assert task_request.artifact_kind is PlanningArtifactKind.STRUCTURED_TASK_PLAN
    assert (
        provider.build_request_body(brief_request)["messages"][0]["content"]
        == brief_request.prompt
    )
    assert (
        provider.build_request_body(task_request)["messages"][0]["content"]
        == task_request.prompt
    )


def test_valid_stream_response_returns_only_assistant_content(monkeypatch):
    _configure(monkeypatch)
    _install_client(monkeypatch, _Response(_valid_sse()))
    response = DirectOpenAICompatiblePlanningProvider(None).generate(_request())

    assert response.candidate_text == '{"ok":true}'
    assert response.diagnostics.category == "provider_success"
    assert response.diagnostics.details["response_mode"] == "streaming_sse"
    assert response.diagnostics.details["candidate_length_bytes"] == len(b'{"ok":true}')
    assert response.runtime_metadata.details["http_status"] == 200
    assert _Client.calls[0]["url"] == "http://gateway:8000/v1/chat/completions"


def test_direct_provider_sends_reasoning_and_sampling_controls(monkeypatch):
    _configure(monkeypatch)
    _install_client(monkeypatch, _Response(_valid_sse()))
    DirectOpenAICompatiblePlanningProvider(None).generate(
        _request(PlanningArtifactKind.STRUCTURED_TASK_PLAN)
    )

    body = _Client.calls[0]["body"]
    assert body["model"] == "qwen-local"
    assert body["temperature"] == 0.0
    assert body["max_tokens"] == 16384
    assert body["chat_template_kwargs"] == {"enable_thinking": False}
    assert body["stream"] is True
    assert _Client.timeout.read <= 360
    assert _Client.timeout.connect <= 360


@pytest.mark.parametrize(
    "body,content_type,detail",
    [
        (b'{"choices":[]}', "application/json", "exactly one choice"),
        (b'{"choices":[{},{}]}', "application/json", "exactly one choice"),
        (
            b'{"choices":[{"message":{"role":"assistant","reasoning_content":"think"}}]}',
            "application/json",
            "reasoning",
        ),
        (b"not-json", "application/json", "valid JSON"),
        (b"\xff", "application/json", "valid JSON"),
    ],
)
def test_response_boundary_rejects_missing_ambiguous_reasoning_or_malformed_output(
    monkeypatch, body, content_type, detail
):
    _configure(monkeypatch)
    _install_client(monkeypatch, _Response(body, content_type=content_type))

    with pytest.raises(PlanningProviderExecutionError) as caught:
        DirectOpenAICompatiblePlanningProvider(None).generate(_request())

    assert caught.value.classification == "provider_output_failure"
    assert detail in caught.value.detail
    assert "not-json" not in caught.value.detail


def test_non_2xx_is_normalized_without_response_body_leak(monkeypatch):
    _configure(monkeypatch)
    _install_client(
        monkeypatch,
        _Response(b"private gateway error", content_type="text/plain", status_code=502),
    )

    with pytest.raises(PlanningProviderExecutionError) as caught:
        DirectOpenAICompatiblePlanningProvider(None).generate(_request())

    assert caught.value.classification == "provider_http_failure"
    assert caught.value.diagnostics.details["http_status"] == 502
    assert caught.value.diagnostics.details["response_content_type"] == "text/plain"
    assert "private gateway error" not in str(caught.value.diagnostics.details)


def test_stream_interruption_does_not_return_partial_content(monkeypatch):
    _configure(monkeypatch)
    _install_client(
        monkeypatch,
        _Response(
            _sse({"choices": [{"delta": {"content": "{"}, "finish_reason": None}]}),
        ),
    )
    _Client.response._body = httpx.ReadError("stream interrupted")

    with pytest.raises(PlanningProviderExecutionError) as caught:
        DirectOpenAICompatiblePlanningProvider(None).generate(_request())

    assert caught.value.classification == "transport_failure"
    assert not hasattr(caught.value, "candidate_text")


def test_timeout_and_cancellation_are_distinct(monkeypatch):
    _configure(monkeypatch)
    _install_client(monkeypatch, _Response(httpx.ReadTimeout("slow")))
    with pytest.raises(PlanningProviderExecutionError) as timeout:
        DirectOpenAICompatiblePlanningProvider(None).generate(_request())
    assert timeout.value.classification == "provider_timeout"

    event = threading.Event()
    event.set()
    with pytest.raises(PlanningProviderExecutionError) as cancelled:
        DirectOpenAICompatiblePlanningProvider(None).generate(
            _request(metadata={"cancel_event": event})
        )
    assert cancelled.value.classification == "provider_cancelled"


def test_capabilities_runtime_and_health_are_bounded(monkeypatch):
    _configure(monkeypatch)
    provider = DirectOpenAICompatiblePlanningProvider(None)
    info = provider.runtime_information()
    assert provider.capabilities.supports_prompt_ownership is True
    assert provider.capabilities.supports_request_ownership is True
    assert provider.capabilities.supports_seed is False
    assert provider.capabilities.supports_top_p is False
    assert info.details["endpoint_origin"] == "http://gateway:8000"
    assert info.details["reasoning_mode"].endswith("false")

    _install_client(monkeypatch, _Response(b"{}", content_type="application/json"))
    health = provider.health()
    assert health.status == "reachable"
    assert _Client.calls[0]["url"] == "http://gateway:8000/v1/models"


def test_selection_fails_closed_for_missing_direct_configuration(monkeypatch):
    monkeypatch.setattr(settings, "PLANNING_DIRECT_BASE_URL", "")
    monkeypatch.setattr(settings, "PLANNING_DIRECT_MODEL", "")

    with pytest.raises(DirectProviderConfigurationError):
        create_planning_provider(None, provider_name="direct_openai_compatible")


def test_selection_rejects_unknown_provider(monkeypatch):
    _configure(monkeypatch)
    with pytest.raises(ValueError, match="Unsupported planning provider"):
        create_planning_provider(None, provider_name="unknown")
