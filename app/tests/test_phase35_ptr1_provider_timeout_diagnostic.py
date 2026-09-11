"""PHASE35-PTR1 provider-timeout ownership characterization."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx

from app.services.agents.providers import openai_chat_adapter
from app.services.agents.providers.openai_chat_adapter import (
    OpenAIChatCompletionsRuntime,
)
from app.services.agents.runtime_invocation import RuntimeInvocationOptions
from app.services.orchestration.planning.grounding import (
    GroundingCoordinator,
    GroundingDecisionContext,
    GroundingExecutor,
    GroundingRunConfig,
    GroundingTaskReference,
    PlanningGroundingProviderAdapter,
)
from app.services.planning.providers.base import (
    ExecutionMetadata,
    PlanningArtifactKind,
    PlanningResponse,
    ProviderCapabilities,
    ProviderDiagnostics,
    ProviderHealth,
    ProviderRuntimeInformation,
    PlanningRequest,
    PlanningRuntimeOptions,
)
from app.services.planning.providers.openclaw import OpenClawPlanningProvider


class _CapturingPlanningProvider:
    name = "ptr1-fake"
    version = "ptr1"
    capabilities = ProviderCapabilities(
        supports_reasoning_control=False,
        supports_response_format=False,
        supports_tool_calling=False,
        supports_deterministic_sampling=False,
        supports_prompt_ownership=False,
        supports_request_ownership=False,
        supports_streaming=False,
        supports_cancellation=False,
        supports_timeout_control=True,
        supports_structured_output=False,
        supports_seed=False,
        supports_top_p=False,
        supports_health_endpoint=False,
    )

    def __init__(self) -> None:
        self.requests: list[PlanningRequest] = []

    def health(self) -> ProviderHealth:
        return ProviderHealth(available=True, ready=True, status="ptr1")

    def runtime_information(self) -> ProviderRuntimeInformation:
        return ProviderRuntimeInformation(
            provider_name=self.name,
            provider_version=self.version,
            runtime_name="ptr1-backend",
            model="ptr1-model",
        )

    def generate(self, request: PlanningRequest) -> PlanningResponse:
        self.requests.append(request)
        return PlanningResponse(
            candidate_text='{"action":"inspect_file","path":"app/example.py"}',
            provider_name=self.name,
            provider_version=self.version,
            diagnostics=ProviderDiagnostics(category="provider_success"),
            latency_seconds=0.001,
            runtime_metadata=ExecutionMetadata(
                runtime_name="ptr1-backend", model="ptr1-model"
            ),
        )


def _grounding_context(tmp_path, provider) -> GroundingDecisionContext:
    config = GroundingRunConfig(
        grounding_run_id="ptr1-run",
        task_reference=GroundingTaskReference(task_id="ptr1-task"),
        workspace_identity=str(tmp_path.resolve()),
        snapshot_identity="ptr1-snapshot",
        max_steps=1,
        max_exploration_provider_requests=1,
        operator_task="Find the implementation.",
    )
    coordinator = GroundingCoordinator(
        executor=GroundingExecutor(tmp_path, snapshot_identity="ptr1-snapshot"),
        provider=PlanningGroundingProviderAdapter(provider, timeout_seconds=180),
        config=config,
    )
    return GroundingDecisionContext(
        state=coordinator._initial_state(),
        rendered_grounding_state="",
        operator_task=config.operator_task,
    )


def test_typed_grounding_adapter_passes_180_to_planning_provider(tmp_path):
    provider = _CapturingPlanningProvider()

    PlanningGroundingProviderAdapter(provider, timeout_seconds=180).decide(
        _grounding_context(tmp_path, provider)
    )

    assert provider.requests[0].artifact_kind is PlanningArtifactKind.GROUNDING
    assert provider.requests[0].runtime_options.timeout_seconds == 180


def test_openclaw_planning_provider_forwards_runtime_timeout(monkeypatch):
    captured: dict[str, object] = {}

    def fake_invoke(_db, _prompt, **kwargs):
        captured.update(kwargs)
        return {
            "status": "completed",
            "output": '{"action":"inspect_file","path":"app/example.py"}',
            "runtime_diagnostics": {
                "duration_seconds": 0.001,
                "backend": "openai_chat_completions",
                "model_family": "qwen-local",
            },
        }

    monkeypatch.setattr(
        "app.services.planning.providers.openclaw.invoke_runtime_prompt",
        fake_invoke,
    )
    request = PlanningRequest(
        artifact_kind=PlanningArtifactKind.GROUNDING,
        prompt="Find the implementation.",
        protocol_input={},
        runtime_options=PlanningRuntimeOptions(timeout_seconds=180),
    )

    response = OpenClawPlanningProvider(None).generate(request)

    assert response.candidate_text.startswith('{"action"')
    assert captured["timeout_seconds"] == 180


class _ImmediateTimeoutClient:
    timeouts: list[float] = []

    def __init__(self, *, timeout):
        self.timeouts.append(timeout)

    async def __aenter__(self):
        return self

    async def __aexit__(self, exc_type, exc, traceback):
        return False

    async def post(self, *_args, **_kwargs):
        raise httpx.ReadTimeout("provider-free PTR1 timeout")


def _run_chat_probe(options):
    runtime = OpenAIChatCompletionsRuntime.__new__(OpenAIChatCompletionsRuntime)
    runtime.backend_role = "planning"
    runtime.runtime_configuration = SimpleNamespace(adaptation_profile="ptr1")
    runtime._invocation_base_url = lambda _options: "http://ptr1.invalid/v1"
    runtime._invocation_api_key = lambda _options: ""
    runtime._model_name = lambda: "ptr1-model"
    try:
        asyncio.run(
            runtime._chat(
                system="system",
                user="probe",
                timeout_seconds=1,
                invocation_options=options,
            )
        )
    except Exception as exc:  # pragma: no cover - assertion below owns outcome
        return type(exc).__name__, str(exc)
    raise AssertionError("timeout probe unexpectedly completed")


def test_legacy_transport_adds_cleanup_margin_exact_contract_does_not(monkeypatch):
    monkeypatch.setattr(
        openai_chat_adapter.httpx, "AsyncClient", _ImmediateTimeoutClient
    )

    legacy_error = _run_chat_probe(None)
    exact_error = _run_chat_probe(RuntimeInvocationOptions(timeout_seconds=1))

    assert _ImmediateTimeoutClient.timeouts[-2:] == [31, 1]
    assert legacy_error[0] == exact_error[0] == "AgentRuntimeError"
    assert "after 1s" in legacy_error[1]
    assert "after 1s" in exact_error[1]
