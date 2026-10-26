"""Provider-free regressions for the Planning adapter's request deadline."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from app.services.agents.interfaces import AgentRuntimeError
from app.services.agents.providers import openai_chat_adapter
from app.services.agents.providers.openai_chat_adapter import (
    OpenAIChatCompletionsRuntime,
)
from app.services.agents.runtime_configuration import (
    BackendRole,
    RoleRuntimeConfiguration,
)
from app.services.agents.runtime_invocation import RuntimeInvocationOptions
from app.config import settings


def _runtime() -> OpenAIChatCompletionsRuntime:
    return OpenAIChatCompletionsRuntime(
        None,
        session_id=None,
        runtime_configuration=RoleRuntimeConfiguration(
            role=BackendRole.PLANNING,
            backend_name="openai_chat_completions",
            model_family="qwen3-coder:30b",
            adaptation_profile="ollama_default",
        ),
    )


class _HangingClient:
    def __init__(self) -> None:
        self.calls = 0
        self.started = asyncio.Event()
        self.cancelled = False
        self.exited = False

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, tb):
        del exc_type, exc, tb
        self.exited = True
        return False

    async def post(self, *args, **kwargs):
        del args, kwargs
        self.calls += 1
        self.started.set()
        try:
            await asyncio.Event().wait()
        except asyncio.CancelledError:
            self.cancelled = True
            raise


def _options(path: Path, *, timeout_seconds: float) -> RuntimeInvocationOptions:
    return RuntimeInvocationOptions(
        timeout_seconds=timeout_seconds,
        stream=False,
        provider_response_evidence_path=str(path),
        provider_response_evidence_correlation_id="phase40e2-timeout-test",
    )


def test_provider_total_deadline_classifies_and_persists_timeout_once(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "PLANNING_DIRECT_BASE_URL", "http://provider.test/v1")
    client = _HangingClient()
    monkeypatch.setattr(
        openai_chat_adapter.httpx,
        "AsyncClient",
        lambda **_kwargs: client,
    )
    evidence_path = tmp_path / "timeout-evidence.json"

    with pytest.raises(AgentRuntimeError, match="total request deadline") as raised:
        asyncio.run(
            asyncio.wait_for(
                _runtime().execute_task(
                    "Return a complete planning response.",
                    timeout_seconds=1,
                    diagnostic_label="PHASE40E2_TEST",
                    invocation_options=_options(evidence_path, timeout_seconds=1),
                ),
                timeout=2,
            )
        )

    assert getattr(raised.value, "provider_failure_classification", None) == (
        "provider_timeout"
    )
    diagnostics = getattr(raised.value, "runtime_diagnostics", {})
    assert diagnostics.get("timeout_boundary") == "runtime_invocation_total_deadline"
    assert diagnostics.get("timeout_seconds") == 1
    assert client.calls == 1
    assert client.cancelled is True
    assert client.exited is True

    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["error"]["type"] == "TimeoutError"
    assert evidence["error"]["response_received"] is False
    assert evidence["timing"]["request_started_utc"]
    assert evidence["timing"]["request_finished_utc"]
    assert evidence["provider"]["http_status"] is None


def test_caller_cancellation_closes_request_and_finishes_evidence(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    monkeypatch.setattr(settings, "PLANNING_DIRECT_BASE_URL", "http://provider.test/v1")
    client = _HangingClient()
    monkeypatch.setattr(
        openai_chat_adapter.httpx,
        "AsyncClient",
        lambda **_kwargs: client,
    )
    evidence_path = tmp_path / "cancel-evidence.json"

    async def cancel_inflight_request():
        request = asyncio.create_task(
            _runtime().execute_task(
                "Return a complete planning response.",
                timeout_seconds=10,
                diagnostic_label="PHASE40E2_TEST",
                invocation_options=_options(evidence_path, timeout_seconds=10),
            )
        )
        await asyncio.wait_for(client.started.wait(), timeout=0.5)
        request.cancel()
        with pytest.raises(asyncio.CancelledError):
            await request

    asyncio.run(cancel_inflight_request())

    assert client.calls == 1
    assert client.cancelled is True
    assert client.exited is True
    evidence = json.loads(evidence_path.read_text(encoding="utf-8"))
    assert evidence["error"]["type"] == "CancelledError"
    assert evidence["error"]["response_received"] is False
    assert evidence["timing"]["request_finished_utc"]
