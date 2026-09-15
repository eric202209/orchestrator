"""Provider-free certification for RER-01O repair-response evidence capture."""

from __future__ import annotations

import json
from pathlib import Path

import httpx
import pytest

from app.services.agents.providers import openai_chat_adapter
from app.services.agents.providers.openai_chat_adapter import (
    OpenAIChatCompletionsRuntime,
)
from app.services.agents.runtime_invocation import RuntimeInvocationOptions
from app.services.observability import planning_provider_evidence
from app.services.orchestration.planning.planner import (
    PlannerService,
    PlanningRepairOutputContractViolation,
)


class _Response:
    def __init__(self, body: object, *, status_code: int = 200) -> None:
        self.status_code = status_code
        self.headers = {
            "content-type": "application/json",
            "x-request-id": "provider-request-test",
        }
        self.content = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self._body = body

    def json(self):
        return self._body

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            raise httpx.HTTPStatusError(
                "synthetic response failure",
                request=httpx.Request(
                    "POST", "http://provider.test/v1/chat/completions"
                ),
                response=httpx.Response(self.status_code),
            )


class _AsyncClient:
    body: object = None
    failure: BaseException | None = None
    calls: list[dict[str, object]] = []

    def __init__(self, **kwargs) -> None:
        del kwargs

    async def __aenter__(self):
        return self

    async def __aexit__(self, *_args):
        return False

    async def post(self, url, *, headers, json):
        self.calls.append({"url": url, "headers": headers, "json": json})
        if self.failure is not None:
            raise self.failure
        return _Response(self.body)


def _runtime(monkeypatch, body: object) -> OpenAIChatCompletionsRuntime:
    _AsyncClient.body = body
    _AsyncClient.failure = None
    _AsyncClient.calls = []
    monkeypatch.setattr(
        openai_chat_adapter.httpx,
        "AsyncClient",
        lambda **kwargs: _AsyncClient(**kwargs),
    )
    runtime = OpenAIChatCompletionsRuntime(None, session_id=None)
    runtime.backend_role = "repair"
    return runtime


def _options(path: Path | None = None) -> RuntimeInvocationOptions:
    return RuntimeInvocationOptions(
        timeout_seconds=30,
        max_output_tokens=64,
        temperature=0.0,
        reasoning_enabled=False,
        stream=False,
        provider_response_evidence_path=(str(path) if path else None),
        provider_response_evidence_correlation_id=(
            "rer01o-test-call" if path else None
        ),
    )


def _body(content: object, **extra: object) -> dict[str, object]:
    return {
        "id": "chatcmpl-synthetic",
        "model": "synthetic-model",
        "choices": [
            {
                "message": {"role": "assistant", "content": content},
                "finish_reason": "stop",
            }
        ],
        "usage": {"prompt_tokens": 11, "completion_tokens": 7, "total_tokens": 18},
        **extra,
    }


@pytest.mark.asyncio
async def test_capture_on_off_returns_identical_planner_visible_value(
    tmp_path, monkeypatch
):
    body = _body("[{}]")
    off = await _runtime(monkeypatch, body).invoke_prompt(
        "repair prompt", invocation_options=_options()
    )
    off_call = _AsyncClient.calls[0]
    evidence_path = tmp_path / "repair.json"
    on = await _runtime(monkeypatch, body).invoke_prompt(
        "repair prompt", invocation_options=_options(evidence_path)
    )
    on_call = _AsyncClient.calls[0]

    assert off["output"] == on["output"] == "[{}]"
    assert off_call["json"] == on_call["json"]
    assert off_call["headers"] == on_call["headers"]
    assert not (tmp_path / "disabled.json").exists()
    assert evidence_path.exists()
    artifact = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert artifact["capture_enabled"] is True
    assert artifact["correlation"]["provider_call_id"] == "rer01o-test-call"


@pytest.mark.asyncio
async def test_capture_retains_distinct_assistant_and_normalized_content(
    tmp_path, monkeypatch
):
    evidence_path = tmp_path / "repair.json"
    content = [
        {"type": "text", "text": "alpha "},
        {"type": "text", "text": "beta"},
    ]
    result = await _runtime(monkeypatch, _body(content)).invoke_prompt(
        "repair prompt", invocation_options=_options(evidence_path)
    )
    artifact = json.loads(evidence_path.read_text(encoding="utf-8"))

    assert result["output"] == "alpha beta"
    assert artifact["provider"]["http_status"] == 200
    assert artifact["provider"]["finish_reason"] == "stop"
    assert artifact["provider"]["usage"]["total_tokens"] == 18
    assert artifact["representations"]["raw_http_bytes"]["retained"] is False
    assert artifact["representations"]["assistant_content"]["value"] == content
    assert (
        artifact["representations"]["assistant_content"]["source_representation"]
        == "RAW_ASSISTANT_CONTENT"
    )
    assert (
        artifact["representations"]["adapter_normalized_content"]["value"]
        == "alpha beta"
    )
    assert (
        artifact["representations"]["planner_visible_content"]["value"] == "alpha beta"
    )


@pytest.mark.asyncio
async def test_planner_rejection_is_capture_neutral_and_verdict_is_retained(
    tmp_path, monkeypatch
):
    body = _body("I repaired the plan, but here are prose steps.")

    def run(path: Path | None):
        runtime = _runtime(monkeypatch, body)
        with pytest.raises(PlanningRepairOutputContractViolation) as caught:
            PlannerService.repair_output(
                runtime_service=runtime,
                task_description="Fix the existing behavior.",
                malformed_output="[]",
                project_dir=tmp_path,
                timeout_seconds=30,
                logger=__import__("logging").getLogger("rer01o"),
                emit_live=lambda *args, **kwargs: None,
                reason="plan_validation_failed",
                rejection_reasons=["behavioral repair requires implementation change"],
                provider_response_evidence_path=(str(path) if path else None),
                provider_response_evidence_correlation_id=(
                    "rer01o-planner-call" if path else None
                ),
            )
        return type(caught.value), str(caught.value), caught.value.runtime_diagnostics

    off_type, off_message, off_diagnostics = run(None)
    evidence_path = tmp_path / "planner-rejected.json"
    on_type, on_message, on_diagnostics = run(evidence_path)

    assert (off_type, off_message) == (on_type, on_message)
    for diagnostics in (off_diagnostics, on_diagnostics):
        assert diagnostics["output_contract_violated"] is True
        assert diagnostics["repair_output_fenced"] is False
    artifact = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert artifact["planner_output_contract"]["status"] == "rejected"
    assert artifact["planner_output_contract"]["reason"] == (
        "repair returned prose; expected bare JSON array"
    )


@pytest.mark.asyncio
async def test_fenced_output_retains_adapter_value_and_planner_normalization(
    tmp_path, monkeypatch
):
    evidence_path = tmp_path / "planner-accepted.json"
    fenced = "```json\n[{}]\n```"
    runtime = _runtime(monkeypatch, _body(fenced))
    result = PlannerService.repair_output(
        runtime_service=runtime,
        task_description="Fix the existing behavior.",
        malformed_output="[]",
        project_dir=tmp_path,
        timeout_seconds=30,
        logger=__import__("logging").getLogger("rer01o-fenced"),
        emit_live=lambda *args, **kwargs: None,
        reason="plan_validation_failed",
        rejection_reasons=["repair requires a complete Plan"],
        provider_response_evidence_path=str(evidence_path),
        provider_response_evidence_correlation_id="rer01o-fenced-call",
    )
    artifact = json.loads(evidence_path.read_text(encoding="utf-8"))

    assert result["output"] == "[{}]"
    assert artifact["representations"]["planner_visible_content"]["value"] == fenced
    assert artifact["planner_output_contract"]["status"] == "accepted"
    assert artifact["planner_output_contract"]["normalized"]["value"] == "[{}]"


@pytest.mark.asyncio
async def test_secret_values_are_redacted_only_in_evidence(tmp_path, monkeypatch):
    evidence_path = tmp_path / "redacted.json"
    secret = "super-secret-token-123456"
    content = f"Authorization: Bearer {secret}; api_key={secret}"
    result = await _runtime(monkeypatch, _body(content)).invoke_prompt(
        "repair prompt", invocation_options=_options(evidence_path)
    )
    artifact_text = evidence_path.read_text(encoding="utf-8")

    assert result["output"] == content
    assert secret not in artifact_text
    assert "<redacted>" in artifact_text


@pytest.mark.asyncio
async def test_provider_exception_still_persists_failure_evidence(
    tmp_path, monkeypatch
):
    evidence_path = tmp_path / "provider-failure.json"
    runtime = _runtime(monkeypatch, _body("unused"))
    _AsyncClient.failure = httpx.ConnectError(
        "synthetic connection failure",
        request=httpx.Request("POST", "http://provider.test"),
    )

    with pytest.raises(Exception) as caught:
        await runtime.invoke_prompt(
            "repair prompt", invocation_options=_options(evidence_path)
        )

    artifact = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert type(caught.value).__name__ == "AgentRuntimeError"
    assert artifact["error"]["type"] == "ConnectError"
    assert artifact["error"]["response_received"] is False
    assert artifact["representations"]["raw_http_bytes"]["retained"] is False


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "content",
    ["{}", "not-json", "", None],
    ids=["json-object", "malformed-json", "empty-content", "missing-content"],
)
async def test_capture_covers_non_successful_content_shapes(
    tmp_path, monkeypatch, content
):
    evidence_path = tmp_path / "content-shape.json"
    message = {"role": "assistant"}
    if content is not None:
        message["content"] = content
    result = await _runtime(
        monkeypatch, _body(None, choices=[{"message": message}])
    ).invoke_prompt("repair prompt", invocation_options=_options(evidence_path))

    artifact = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert artifact["capture_enabled"] is True
    assert artifact["provider"]["http_status"] == 200
    assert artifact["representations"]["assistant_content"]["retained"] is True
    assert artifact["representations"]["planner_visible_content"]["retained"] is True
    assert result["output"] == ("" if content is None else content)


@pytest.mark.asyncio
async def test_persistence_failure_is_visible_without_changing_runtime_value(
    tmp_path, monkeypatch
):
    def fail_write(*_args, **_kwargs):
        raise OSError("synthetic evidence sink failure")

    monkeypatch.setattr(planning_provider_evidence, "_write_json", fail_write)
    result = await _runtime(monkeypatch, _body("[{}]")).invoke_prompt(
        "repair prompt",
        invocation_options=_options(tmp_path / "unwritable.json"),
    )

    evidence_diagnostics = result["diagnostics"]["provider_response_evidence"]
    assert result["output"] == "[{}]"
    assert evidence_diagnostics["persistence_status"] == "incomplete"
    assert "OSError" in evidence_diagnostics["persistence_errors"][0]
