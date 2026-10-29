from __future__ import annotations

import json
from unittest.mock import Mock

import pytest

from scripts.benchmark.product_recovery_05_r1_call_gate import (
    CallGateRefusal,
    ProviderCallGate,
)


CASE_A = "D2 C1 / Task 31"
CASE_B = "E2 / Task 49"
ENDPOINT = "http://host.docker.internal:11434/v1"
MODEL = "qwen3-coder:30b"
URL = ENDPOINT + "/chat/completions"


def _body(model: str = MODEL) -> bytes:
    return json.dumps(
        {"model": model, "messages": [{"role": "user", "content": "Plan"}]}
    ).encode()


def _gate(**kwargs) -> ProviderCallGate:
    return ProviderCallGate(
        endpoint=ENDPOINT,
        model=MODEL,
        case_ids=(CASE_A, CASE_B),
        **kwargs,
    )


def _mock_dispatch(gate, case, stage, *, url=URL, body=None, send=None):
    sender = send or Mock(return_value="response")
    with gate.scope(case, stage):
        receipt = gate.authorize(
            method="POST", url=url, body=body if body is not None else _body()
        )
        gate.begin_dispatch(receipt)
        try:
            result = sender()
        except BaseException as exc:
            gate.record_error(receipt, error=exc, latency_seconds=0.01)
            raise
        return result


def test_one_initial_planning_request_is_counted():
    gate = _gate()

    assert _mock_dispatch(gate, CASE_A, "planning") == "response"
    assert gate.total_dispatched == 1
    assert gate.per_case_counts[CASE_A] == 1


def test_one_repair_request_is_counted_after_initial_request():
    gate = _gate()
    _mock_dispatch(gate, CASE_A, "planning")

    _mock_dispatch(gate, CASE_A, "repair")
    assert gate.total_dispatched == 2
    assert gate.per_case_counts[CASE_A] == 2


def test_third_request_for_same_case_is_refused_before_provider_dispatch():
    gate = _gate()
    _mock_dispatch(gate, CASE_A, "planning")
    _mock_dispatch(gate, CASE_A, "repair")
    sender = Mock()

    with pytest.raises(CallGateRefusal, match="case_ceiling_exhausted"):
        _mock_dispatch(gate, CASE_A, "planning", send=sender)

    sender.assert_not_called()
    assert gate.total_dispatched == 2


def test_fifth_total_request_is_refused_before_provider_dispatch():
    gate = _gate()
    for case in (CASE_A, CASE_B):
        _mock_dispatch(gate, case, "planning")
        _mock_dispatch(gate, case, "repair")
    sender = Mock()

    with pytest.raises(CallGateRefusal, match="total_ceiling_exhausted"):
        _mock_dispatch(gate, CASE_A, "repair", send=sender)

    sender.assert_not_called()
    assert gate.total_dispatched == 4


def test_fallback_dispatch_cannot_bypass_the_gate():
    gate = _gate()
    sender = Mock()
    _mock_dispatch(gate, CASE_A, "planning")

    with pytest.raises(CallGateRefusal, match="stage_dispatch_already_used"):
        _mock_dispatch(gate, CASE_A, "planning", send=sender)

    sender.assert_not_called()
    assert gate.total_dispatched == 1


def test_alternate_model_and_endpoint_are_refused():
    gate = _gate()
    sender = Mock()
    with gate.scope(CASE_A, "planning"):
        with pytest.raises(CallGateRefusal, match="alternate_model"):
            gate.authorize(method="POST", url=URL, body=_body("other-model"))
        with pytest.raises(CallGateRefusal, match="alternate_endpoint_or_path"):
            gate.authorize(
                method="POST",
                url="http://other-provider:8000/v1/chat/completions",
                body=_body(),
            )
    sender.assert_not_called()
    assert gate.total_dispatched == 0


def test_timeout_or_error_consumes_budget():
    gate = _gate()
    sender = Mock(side_effect=TimeoutError("mock provider timeout"))

    with pytest.raises(TimeoutError):
        _mock_dispatch(gate, CASE_A, "planning", send=sender)

    assert gate.total_dispatched == 1
    assert gate.per_case_counts[CASE_A] == 1
    with pytest.raises(CallGateRefusal):
        _mock_dispatch(gate, CASE_A, "planning", send=Mock())


def test_request_evidence_preserves_limits_and_hashes_exact_rendered_prompt(tmp_path):
    import hashlib

    from scripts.benchmark.product_recovery_05_r1_call_gate import (
        install_httpx_boundary,
    )

    gate = _gate(evidence_root=tmp_path)
    body = json.dumps(
        {
            "model": MODEL,
            "max_tokens": 2048,
            "messages": [
                {"role": "system", "content": "Rules"},
                {"role": "user", "content": "Plan"},
            ],
            "api_key": "must-not-be-retained",
        }
    ).encode()

    import httpx

    async def respond(request):
        return httpx.Response(
            200,
            json={
                "choices": [{"message": {"content": "Plan output"}}],
                "usage": {"prompt_tokens": 7, "completion_tokens": 3},
            },
            request=request,
        )

    with install_httpx_boundary(gate):

        async def dispatch():
            request = httpx.Request("POST", URL, content=body)
            transport = httpx.MockTransport(respond)
            client = httpx.AsyncClient(transport=transport)
            with gate.scope(CASE_A, "planning"):
                await client.send(request)
            await client.aclose()

        import asyncio

        asyncio.run(dispatch())

    request_record = json.loads(
        (tmp_path / "requests" / CASE_A / "request-01" / "request.json").read_text()
    )
    transport_record = json.loads(
        (tmp_path / "requests" / CASE_A / "request-01" / "transport.json").read_text()
    )
    prompt = (tmp_path / "requests" / CASE_A / "request-01" / "prompt.txt").read_text()
    assert request_record["request_payload"]["max_tokens"] == 2048
    assert request_record["request_payload"]["reasoning_effort"] == "none"
    assert "api_key" not in request_record["request_payload"]
    assert "chat_template_kwargs" not in request_record["request_payload"]
    assert (
        request_record["prompt_sha256"] == hashlib.sha256(prompt.encode()).hexdigest()
    )
    assert transport_record["transport_retries"] == 0
    assert transport_record["follow_redirects"] is False
    dispatch_record = json.loads(
        (tmp_path / "requests" / CASE_A / "request-01" / "dispatch.json").read_text()
    )
    assert dispatch_record["dispatch_started"] is True


def test_http_redirects_are_refused_before_dispatch():
    import asyncio
    import httpx
    from scripts.benchmark.product_recovery_05_r1_call_gate import (
        install_httpx_boundary,
    )

    gate = _gate()
    dispatches = []

    async def respond(request):
        dispatches.append(request.url)
        return httpx.Response(200, json={}, request=request)

    async def dispatch():
        client = httpx.AsyncClient(
            transport=httpx.MockTransport(respond), follow_redirects=True
        )
        try:
            request = httpx.Request("POST", URL, content=_body())
            with gate.scope(CASE_A, "planning"):
                with pytest.raises(
                    CallGateRefusal, match="http_redirects_not_permitted"
                ):
                    await client.send(request)
        finally:
            await client.aclose()

    with install_httpx_boundary(gate):
        asyncio.run(dispatch())
    assert dispatches == []
    assert gate.total_dispatched == 0


def test_transport_retry_attempt_is_refused_after_first_dispatch():
    import httpx

    gate = _gate()
    sender = Mock(side_effect=[TimeoutError("transport error"), "retry"])
    with gate.scope(CASE_A, "planning"):
        receipt = gate.authorize(method="POST", url=URL, body=_body())
        gate.begin_dispatch(receipt)
        try:
            sender()
        except TimeoutError as exc:
            gate.record_error(receipt, error=exc, latency_seconds=0.01)
        with pytest.raises(CallGateRefusal):
            gate.authorize(method="POST", url=URL, body=_body())

    assert sender.call_count == 1
    assert gate.total_dispatched == 1

    from scripts.benchmark.product_recovery_05_r1_call_gate import (
        install_httpx_boundary,
    )

    with install_httpx_boundary(gate):
        with pytest.raises(
            CallGateRefusal, match="automatic_transport_retries_not_permitted"
        ):
            httpx.AsyncHTTPTransport(retries=1)


def test_actual_outbound_body_uses_ollama_supported_reasoning_control():
    import asyncio
    import httpx
    from scripts.benchmark.product_recovery_05_r1_call_gate import (
        install_httpx_boundary,
    )

    gate = _gate()
    sent_payloads = []

    async def respond(request):
        sent_payloads.append(json.loads(request.content.decode()))
        return httpx.Response(200, json={"choices": []}, request=request)

    async def dispatch():
        payload = {
            "model": MODEL,
            "messages": [{"role": "user", "content": "Plan"}],
            "temperature": 0.1,
            "max_tokens": 2048,
            "stream": False,
            "think": False,
            "enable_thinking": False,
            "chat_template_kwargs": {"enable_thinking": False},
        }
        client = httpx.AsyncClient(transport=httpx.MockTransport(respond))
        try:
            with gate.scope(CASE_A, "planning"):
                await client.post(URL, json=payload)
        finally:
            await client.aclose()

    with install_httpx_boundary(gate):
        asyncio.run(dispatch())

    assert len(sent_payloads) == 1
    assert sent_payloads[0]["reasoning_effort"] == "none"
    assert "think" not in sent_payloads[0]
    assert "enable_thinking" not in sent_payloads[0]
    assert "chat_template_kwargs" not in sent_payloads[0]
    assert gate.total_dispatched == 1


def test_allowlisted_qdrant_collection_metadata_is_not_a_generation_call():
    gate = _gate(permitted_read_urls=("http://localhost:6333/collections",))

    with gate.scope(CASE_A, "planning"):
        assert (
            gate.authorize(
                method="GET",
                url="http://localhost:6333/collections",
                body=b"",
            )
            is None
        )

    assert gate.total_dispatched == 0
    assert gate.permitted_non_generation_requests == [
        {
            "method": "GET",
            "endpoint": "http://localhost:6333/collections",
            "purpose": "allowlisted_non_generation_metadata",
        }
    ]
