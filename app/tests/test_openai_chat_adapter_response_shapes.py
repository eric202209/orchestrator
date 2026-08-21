import hashlib

import httpx
import pytest

from app.services.agents.interfaces import AgentRuntimeError
from app.services.agents.providers import openai_chat_adapter
from app.services.agents.providers.openai_chat_adapter import (
    OpenAIChatCompletionsRuntime,
    _extract_chat_completion_content,
    _strip_thinking,
)
from app.services.orchestration.planning.planner import PlannerService


def _body(content):
    return {"choices": [{"message": {"content": content}}]}


def test_openai_chat_extracts_string_content_unchanged():
    assert _extract_chat_completion_content(_body("plain text")) == "plain text"


def test_openai_chat_dict_content_does_not_raise_raw_type_error():
    assert _extract_chat_completion_content(_body({"unexpected": "shape"})) == ""


def test_openai_chat_list_content_does_not_raise_raw_type_error():
    assert _extract_chat_completion_content(_body([{"unexpected": "shape"}])) == ""


def test_openai_chat_dict_text_field_extracts_text():
    assert _extract_chat_completion_content(_body({"text": "dict text"})) == "dict text"


def test_openai_chat_list_of_text_parts_extracts_text():
    assert (
        _extract_chat_completion_content(
            _body(
                [
                    {"type": "text", "text": "alpha "},
                    {"type": "output_text", "output_text": "beta "},
                    {"content": {"text": "gamma"}},
                ]
            )
        )
        == "alpha beta gamma"
    )


def test_openai_chat_unsupported_shape_is_deterministic_empty_text():
    assert _extract_chat_completion_content(_body({"metadata": {"tokens": 3}})) == ""
    assert _extract_chat_completion_content(_body([{"metadata": {"tokens": 3}}])) == ""


def test_openai_chat_strip_thinking_runs_after_text_normalization():
    content = {"text": "<think>private reasoning</think>visible answer"}

    assert _strip_thinking(_extract_chat_completion_content(_body(content))) == (
        "visible answer"
    )


def test_openai_chat_strip_thinking_defensively_handles_non_string_values():
    assert _strip_thinking({"text": "<think>hidden</think>shown"}) == "shown"
    assert _strip_thinking([{"text": "<think>hidden</think>"}, {"text": "shown"}]) == (
        "shown"
    )


class _AsyncClient:
    def __init__(self, *, outcome=None, enter_error=None, calls=None, **kwargs):
        del kwargs
        self.outcome = outcome
        self.enter_error = enter_error
        self.calls = calls

    async def __aenter__(self):
        if self.enter_error is not None:
            raise self.enter_error
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, **kwargs):
        self.calls.append((url, kwargs))
        if isinstance(self.outcome, BaseException):
            raise self.outcome
        return self.outcome


def _install_async_client(monkeypatch, *, outcome=None, enter_error=None):
    calls = []
    monkeypatch.setattr(
        openai_chat_adapter.httpx,
        "AsyncClient",
        lambda **kwargs: _AsyncClient(
            outcome=outcome,
            enter_error=enter_error,
            calls=calls,
            **kwargs,
        ),
    )
    return calls


def _repair_runtime():
    runtime = OpenAIChatCompletionsRuntime(None, session_id=None)
    runtime.backend_role = "repair"
    return runtime


def _response(body, *, status_code=200):
    return httpx.Response(
        status_code,
        json=body,
        request=httpx.Request("POST", "http://provider.test/v1/chat/completions"),
    )


def _assert_p6(diagnostics, prompt, *, started, response):
    assert diagnostics["prompt_stage"] == "P6_PROVIDER_BOUND_PROMPT"
    assert (
        diagnostics["provider_bound_prompt_sha256_12"]
        == hashlib.sha256(prompt.encode("utf-8")).hexdigest()[:12]
    )
    assert diagnostics["provider_bound_prompt_chars"] == len(prompt)
    assert diagnostics["provider_bound_prompt_token_estimate"] == (len(prompt) + 3) // 4
    assert diagnostics["provider_bound_prompt_token_estimator"] == "ceil_chars_div_4"
    assert diagnostics["provider_invocation_kind"] == "repair_chat_completions"
    assert diagnostics["provider_invocation_started"] is started
    assert diagnostics["provider_response_received"] is response
    assert prompt not in str(diagnostics)


@pytest.mark.asyncio
async def test_repair_adapter_success_exposes_exact_p6_and_request_content(monkeypatch):
    prompt = "EXACT REPAIR PROMPT"
    calls = _install_async_client(monkeypatch, outcome=_response(_body("[{}]")))

    result = await _repair_runtime().invoke_prompt(prompt)

    assert calls[0][1]["json"]["messages"][-1]["content"] == prompt
    _assert_p6(result["diagnostics"], prompt, started=True, response=True)
    assert result["provider_response_observability"]["content_type"] == "str"
    assert "EXACT REPAIR PROMPT" not in str(result["provider_response_observability"])


@pytest.mark.asyncio
async def test_planner_repair_boundary_preserves_adapter_diagnostics(monkeypatch):
    prompt = "planner repair boundary prompt"
    _install_async_client(monkeypatch, outcome=_response(_body("[{}]")))

    result = await PlannerService._invoke_repair_prompt(
        _repair_runtime(),
        prompt,
        repair_timeout=30,
        allow_registry_fallback=False,
    )

    _assert_p6(result["diagnostics"], prompt, started=True, response=True)


@pytest.mark.asyncio
async def test_repair_adapter_initialization_failure_is_false_false(monkeypatch):
    prompt = "setup failure prompt"
    _install_async_client(monkeypatch, enter_error=RuntimeError("setup failed"))

    with pytest.raises(RuntimeError) as exc_info:
        await _repair_runtime().invoke_prompt(prompt)

    _assert_p6(
        exc_info.value.runtime_diagnostics,
        prompt,
        started=False,
        response=False,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "failure",
    [
        httpx.ConnectError(
            "connection failed",
            request=httpx.Request("POST", "http://provider.test"),
        ),
        httpx.ReadTimeout(
            "timed out",
            request=httpx.Request("POST", "http://provider.test"),
        ),
    ],
    ids=["connection", "timeout"],
)
async def test_repair_adapter_transport_failures_preserve_p6_truth(
    monkeypatch, failure
):
    prompt = "transport failure prompt"
    _install_async_client(monkeypatch, outcome=failure)

    with pytest.raises(AgentRuntimeError) as exc_info:
        await _repair_runtime().invoke_prompt(prompt)

    _assert_p6(
        exc_info.value.runtime_diagnostics,
        prompt,
        started=True,
        response=False,
    )


@pytest.mark.asyncio
async def test_repair_adapter_http_error_and_malformed_model_content_are_response_true(
    monkeypatch,
):
    prompt = "response outcome prompt"
    calls = _install_async_client(
        monkeypatch, outcome=_response({"error": {"message": "bad"}}, status_code=503)
    )

    with pytest.raises(AgentRuntimeError) as exc_info:
        await _repair_runtime().invoke_prompt(prompt)

    _assert_p6(
        exc_info.value.runtime_diagnostics,
        prompt,
        started=True,
        response=True,
    )
    assert calls

    malformed_prompt = "malformed model content prompt"
    _install_async_client(
        monkeypatch,
        outcome=_response(_body({"unexpected": "shape"})),
    )
    result = await _repair_runtime().invoke_prompt(malformed_prompt)
    _assert_p6(result["diagnostics"], malformed_prompt, started=True, response=True)
    assert result["output"] == ""


@pytest.mark.asyncio
async def test_repair_adapter_response_json_failure_is_response_true(monkeypatch):
    prompt = "invalid response json prompt"
    response = httpx.Response(
        200,
        content=b"not-json",
        request=httpx.Request("POST", "http://provider.test/v1/chat/completions"),
    )
    _install_async_client(monkeypatch, outcome=response)

    with pytest.raises(ValueError) as exc_info:
        await _repair_runtime().invoke_prompt(prompt)

    _assert_p6(
        exc_info.value.runtime_diagnostics,
        prompt,
        started=True,
        response=True,
    )
