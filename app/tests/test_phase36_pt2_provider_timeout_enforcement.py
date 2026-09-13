"""PHASE36-PT2 provider-free logical deadline enforcement regressions."""

from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import httpx
import pytest

from app.services.agents import agent_runtime
from app.services.agents.interfaces import AgentRuntimeError
from app.services.agents.providers import openai_chat_adapter
from app.services.agents.providers.openai_chat_adapter import (
    OpenAIChatCompletionsRuntime,
)
from app.services.agents.runtime_invocation import RuntimeInvocationOptions
from app.services.orchestration.coordinators import failure_coordinator
from app.services.orchestration.planning.grounding import (
    GroundingCoordinator,
    GroundingDecisionContext,
    GroundingExecutor,
    GroundingProviderError,
    GroundingRunConfig,
    GroundingTaskReference,
    PlanningGroundingProviderAdapter,
)
from app.services.planning.providers.openclaw import OpenClawPlanningProvider


class _SlowRuntime:
    backend_descriptor = SimpleNamespace(name="pt2-fake-runtime")

    def __init__(self, *, wait_seconds: float = 0.2, emit_output: bool = False):
        self.wait_seconds = wait_seconds
        self.emit_output = emit_output
        self.output_count = 0

    async def invoke_prompt(self, _prompt, **_kwargs):
        if self.emit_output:
            interval = self.wait_seconds / 10
            for _ in range(10):
                self.output_count += 1
                await asyncio.sleep(interval)
        else:
            await asyncio.sleep(self.wait_seconds)
        return {"status": "completed", "output": "late"}


class _FastRuntime:
    backend_descriptor = SimpleNamespace(name="pt2-fast-runtime")

    async def invoke_prompt(self, _prompt, **_kwargs):
        return {"status": "completed", "output": "fast"}


def _invoke_runtime_with_fake(monkeypatch, runtime, *, timeout_seconds=0.05):
    monkeypatch.setattr(
        agent_runtime, "create_agent_runtime", lambda *_a, **_k: runtime
    )
    return agent_runtime.invoke_runtime_prompt(
        None,
        "provider-free probe",
        timeout_seconds=timeout_seconds,
        session_prefix="grounding",
    )


def _grounding_context(tmp_path, provider):
    config = GroundingRunConfig(
        grounding_run_id="pt2-run",
        task_reference=GroundingTaskReference(task_id="pt2-task"),
        workspace_identity=str(tmp_path.resolve()),
        snapshot_identity="pt2-snapshot",
        max_steps=1,
        max_exploration_provider_requests=1,
        operator_task="Find the implementation.",
    )
    coordinator = GroundingCoordinator(
        executor=GroundingExecutor(tmp_path, snapshot_identity="pt2-snapshot"),
        provider=PlanningGroundingProviderAdapter(provider, timeout_seconds=1),
        config=config,
    )
    return GroundingDecisionContext(
        state=coordinator._initial_state(),
        rendered_grounding_state="",
        operator_task=config.operator_task,
    )


class _ImmediateTimeoutClient:
    timeouts: list[float] = []

    def __init__(self, *, timeout):
        self.timeouts.append(timeout)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def post(self, *_args, **_kwargs):
        raise httpx.ReadTimeout("provider-free PT2 timeout")


def _chat_timeout(runtime, *, timeout_seconds=1):
    try:
        asyncio.run(
            runtime._chat(
                system="system",
                user="probe",
                timeout_seconds=timeout_seconds,
                invocation_options=None,
            )
        )
    except Exception as exc:  # pragma: no cover - assertions own the outcome
        return exc
    raise AssertionError("timeout probe unexpectedly completed")


def _chat_runtime():
    runtime = OpenAIChatCompletionsRuntime.__new__(OpenAIChatCompletionsRuntime)
    runtime.backend_role = "planning"
    runtime.runtime_configuration = SimpleNamespace(adaptation_profile="pt2")
    runtime._invocation_base_url = lambda _options: "http://pt2.invalid/v1"
    runtime._invocation_api_key = lambda _options: ""
    runtime._model_name = lambda: "pt2-model"
    return runtime


def _timeout_runtime_diagnostics(*, logical=1, transport=31):
    return {
        "timed_out": True,
        "timeout_boundary": "provider_deadline",
        "timeout_seconds": logical,
        "configured_logical_timeout_seconds": logical,
        "effective_logical_deadline_seconds": logical,
        "effective_transport_timeout_seconds": transport,
    }


def test_grounding_logical_deadline_wins_over_underlying_wait(monkeypatch):
    started_at = time.monotonic()
    with pytest.raises(AgentRuntimeError) as caught:
        _invoke_runtime_with_fake(monkeypatch, _SlowRuntime(), timeout_seconds=0.05)
    elapsed = time.monotonic() - started_at

    assert 0.03 <= elapsed < 0.15
    assert caught.value.provider_failure_classification == "provider_timeout"
    diagnostics = caught.value.runtime_diagnostics
    assert diagnostics["timed_out"] is True
    assert diagnostics["timeout_classification"] == "provider_timeout"
    assert diagnostics["configured_logical_timeout_seconds"] == 0.05
    assert diagnostics["effective_logical_deadline_seconds"] == 0.05
    assert diagnostics["effective_transport_timeout_seconds"] == 30.05


def test_grounding_deadline_is_total_and_not_reset_by_output(monkeypatch):
    runtime = _SlowRuntime(wait_seconds=0.2, emit_output=True)
    started_at = time.monotonic()
    with pytest.raises(AgentRuntimeError, match="logical deadline"):
        _invoke_runtime_with_fake(monkeypatch, runtime, timeout_seconds=0.05)

    assert time.monotonic() - started_at < 0.15
    assert runtime.output_count >= 2


def test_grounding_fast_provider_success_is_preserved(monkeypatch):
    result = _invoke_runtime_with_fake(
        monkeypatch, _FastRuntime(), timeout_seconds=0.05
    )

    assert result["status"] == "completed"
    assert result["output"] == "fast"


def test_openai_chat_transport_margin_and_timeout_classification(monkeypatch):
    _ImmediateTimeoutClient.timeouts.clear()
    monkeypatch.setattr(
        openai_chat_adapter.httpx, "AsyncClient", _ImmediateTimeoutClient
    )
    error = _chat_timeout(_chat_runtime())

    assert _ImmediateTimeoutClient.timeouts == [31]
    assert error.provider_failure_classification == "provider_timeout"
    assert "logical deadline" in str(error)
    diagnostics = error.runtime_diagnostics
    assert diagnostics["configured_logical_timeout_seconds"] == 1
    assert diagnostics["effective_logical_deadline_seconds"] == 1
    assert diagnostics["effective_transport_timeout_seconds"] == 31


def test_reflection_logical_deadline_wins_over_padded_wait():
    started_at = time.monotonic()
    with pytest.raises(AgentRuntimeError) as caught:
        failure_coordinator._invoke_reflection_prompt(
            _SlowRuntime(), "reflection probe", timeout_seconds=0.05
        )

    assert 0.03 <= time.monotonic() - started_at < 0.15
    assert caught.value.provider_failure_classification == "provider_timeout"
    assert caught.value.runtime_diagnostics["effective_transport_timeout_seconds"] == (
        30.05
    )


def test_grounding_timeout_event_exposes_effective_deadlines(monkeypatch, tmp_path):
    def fake_invoke(*_args, **_kwargs):
        error = AgentRuntimeError("provider logical deadline exceeded")
        error.provider_failure_classification = "provider_timeout"
        error.runtime_diagnostics = _timeout_runtime_diagnostics()
        raise error

    monkeypatch.setattr(
        "app.services.planning.providers.openclaw.invoke_runtime_prompt",
        fake_invoke,
    )
    events = []
    provider = OpenClawPlanningProvider(None)
    adapter = PlanningGroundingProviderAdapter(
        provider,
        timeout_seconds=1,
        event_sink=lambda _kind, details: events.append(details),
    )

    with pytest.raises(GroundingProviderError, match="provider_timeout"):
        adapter.decide(_grounding_context(tmp_path, provider))

    details = events[-1]
    assert details["failure_classification"] == "provider_timeout"
    assert details["failure_layer"] == "L0_TRANSPORT"
    assert details["configured_logical_timeout_seconds"] == 1
    assert details["effective_logical_deadline_seconds"] == 1
    assert details["effective_transport_timeout_seconds"] == 31


def test_grounding_success_event_exposes_effective_deadlines(monkeypatch, tmp_path):
    def fake_invoke(*_args, **_kwargs):
        return {
            "status": "completed",
            "output": '{"action":"inspect_file","path":"app/example.py"}',
            "runtime_diagnostics": {
                **_timeout_runtime_diagnostics(),
                "timed_out": False,
                "diagnostic_category": "provider_success",
            },
        }

    monkeypatch.setattr(
        "app.services.planning.providers.openclaw.invoke_runtime_prompt",
        fake_invoke,
    )
    events = []
    provider = OpenClawPlanningProvider(None)
    adapter = PlanningGroundingProviderAdapter(
        provider,
        timeout_seconds=1,
        event_sink=lambda _kind, details: events.append(details),
    )

    proposal = adapter.decide(_grounding_context(tmp_path, provider))

    assert proposal.action_payload["action"] == "inspect_file"
    details = events[-1]
    assert details["configured_logical_timeout_seconds"] == 1
    assert details["effective_logical_deadline_seconds"] == 1
    assert details["effective_transport_timeout_seconds"] == 31


def test_timeout_contract_does_not_require_exact_invocation_options(monkeypatch):
    _ImmediateTimeoutClient.timeouts.clear()
    monkeypatch.setattr(
        openai_chat_adapter.httpx, "AsyncClient", _ImmediateTimeoutClient
    )
    runtime = _chat_runtime()
    error = _chat_timeout(runtime)

    assert isinstance(error, AgentRuntimeError)
    assert _ImmediateTimeoutClient.timeouts[-1] == 31
    assert RuntimeInvocationOptions(timeout_seconds=1).uses_legacy_chat_shape is False
