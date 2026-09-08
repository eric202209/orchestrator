"""Provider-free PGA1 typed grounding adapter and capture tests."""

from __future__ import annotations

from dataclasses import replace
import json
from pathlib import Path
import re
import subprocess
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.orchestration.phases import planning_grounding_integration
from app.services.orchestration.planning.grounding import (
    FIRST_TURN_WIRE_EXAMPLES,
    GroundingBudgetDelta,
    GroundingBudgetSnapshot,
    GroundingCoordinator,
    GroundingDecisionContext,
    GroundingExecutor,
    GroundingLifecycleState,
    GroundingOutcome,
    GroundingRejection,
    GroundingRunConfig,
    GroundingTaskReference,
    PlanningGroundingProviderAdapter,
    POST_OBSERVATION_WIRE_EXAMPLES,
    parse_grounding_provider_response,
    render_first_turn_prompt,
    render_post_observation_prompt,
    render_rejection_correction_prompt,
)
from app.services.orchestration.planning.grounding.contracts import (
    GroundingRequest,
    observation_from_request,
    parse_grounding_request,
)
from app.services.planning.providers.base import (
    ExecutionMetadata,
    PlanningArtifactKind,
    PlanningProviderExecutionError,
    PlanningResponse,
    ProviderCapabilities,
    ProviderFailureOrigin,
    ProviderHealth,
    ProviderRuntimeInformation,
)


class FakePlanningProvider:
    name = "fake-planning"
    version = "fake-1"
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

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def health(self):
        return ProviderHealth(available=True, ready=True, status="fake")

    def runtime_information(self):
        return ProviderRuntimeInformation(
            provider_name=self.name,
            provider_version=self.version,
            runtime_name="fake-backend",
            model="fake-model",
            adaptation_profile="fake-profile",
        )

    def generate(self, request):
        self.requests.append(request)
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        if callable(response):
            response = response(request)
        return PlanningResponse(
            candidate_text=response,
            provider_name=self.name,
            provider_version=self.version,
            diagnostics=SimpleNamespace(category="provider_success", details={}),
            latency_seconds=0.001,
            runtime_metadata=ExecutionMetadata(
                runtime_name="fake-backend",
                model="fake-model",
                adaptation_profile="fake-profile",
            ),
        )


def _context(tmp_path: Path, *, provider=None) -> GroundingDecisionContext:
    provider = provider or FakePlanningProvider([])
    config = GroundingRunConfig(
        grounding_run_id="pga1-run",
        task_reference=GroundingTaskReference(task_id="task-1"),
        workspace_identity=str(tmp_path.resolve()),
        snapshot_identity="snapshot-1",
        max_steps=4,
        max_provider_requests=4,
        operator_task="Find the implementation for the requested behavior.",
        orientation_advisory={"candidate_paths": ["app/example.py"]},
    )
    coordinator = GroundingCoordinator(
        executor=GroundingExecutor(tmp_path, snapshot_identity="snapshot-1"),
        provider=PlanningGroundingProviderAdapter(provider),
        config=config,
    )
    state = coordinator._initial_state()
    return GroundingDecisionContext(
        state=state,
        rendered_grounding_state="unused test projection",
        operator_task=config.operator_task,
    )


def _post_observation_context(tmp_path: Path) -> GroundingDecisionContext:
    context = _context(tmp_path)
    request = parse_grounding_request(
        {"action": "inspect_file", "path": "app/example.py"},
        grounding_run_id=context.state.grounding_run_id,
        request_id="grounding-request-1",
    )
    observation = observation_from_request(
        request,
        observation_id="grounding-observation-1",
        outcome=GroundingOutcome.FOUND,
        source_paths=("app/example.py",),
        source_versions={"app/example.py": "version-1"},
        bounded_content=b"def target():\n    return True\n",
        result_count=1,
        result_limit=1,
        budget_delta=GroundingBudgetDelta(
            repository_actions=1,
            source_evidence_bytes=31,
            distinct_files=1,
            positive_regions=1,
        ),
        budget_cumulative=GroundingBudgetSnapshot(
            repository_actions=1,
            source_evidence_bytes=31,
            distinct_files=1,
            positive_regions=1,
        ),
    )
    state = replace(
        context.state,
        lifecycle_state=GroundingLifecycleState.OBSERVED,
        observation_history=(observation,),
        remaining_budget={
            "provider_requests": 3,
            "repository_actions": 3,
            "source_evidence_bytes": 12257,
            "distinct_files": 3,
            "positive_regions": 3,
        },
    )
    return replace(context, state=state)


def _capture_sink():
    events = []
    return events, lambda event_type, details: events.append(
        {"event_type": event_type, **dict(details)}
    )


@pytest.mark.parametrize("wire", FIRST_TURN_WIRE_EXAMPLES)
def test_first_turn_wire_examples_are_accepted_by_the_production_parser(tmp_path, wire):
    provider = FakePlanningProvider([wire])
    adapter = PlanningGroundingProviderAdapter(provider)
    proposal = adapter.decide(_context(tmp_path, provider=provider))

    expected = json.loads(wire)
    assert dict(proposal.action_payload) == {
        **expected,
        **({"scopes": tuple(expected["scopes"])} if "scopes" in expected else {}),
    }
    assert provider.requests[0].artifact_kind is PlanningArtifactKind.GROUNDING


@pytest.mark.parametrize("wire", POST_OBSERVATION_WIRE_EXAMPLES)
def test_post_observation_wire_examples_are_accepted_by_the_production_parser(
    tmp_path, wire
):
    provider = FakePlanningProvider([wire])
    adapter = PlanningGroundingProviderAdapter(provider)

    proposal = adapter.decide(_post_observation_context(tmp_path))

    assert proposal.assessment_kind is not None
    assert (
        parse_grounding_provider_response(
            json.loads(wire), after_observation=True
        ).assessment_kind
        is proposal.assessment_kind
    )


@pytest.mark.parametrize(
    ("wire", "expected_code", "after_observation"),
    [
        (
            'prose {"action":"inspect_file","path":"app/example.py"}',
            "json_decode_failed",
            False,
        ),
        (
            '```json\n{"action":"inspect_file","path":"app/example.py"}\n```',
            "json_decode_failed",
            False,
        ),
        (
            '{"action":"inspect_file","path":"app/example.py"}{"action":"inspect_file","path":"app/example.py"}',
            "json_decode_failed",
            False,
        ),
        (
            '{"action":"inspect_file","path":"app/example.py","extra":true}',
            "unknown_fields",
            False,
        ),
        (
            '{"decision":"SUFFICIENT","cited_observation_ids":["grounding-observation-1"],"rationale":"ok","extra":true}',
            "unknown_fields",
            True,
        ),
        (
            '{"decision":"SUFFICIENT","cited_observation_ids":["grounding-observation-1"],"rationale":"ok"}',
            "invalid_first_turn_action",
            False,
        ),
        (
            '{"action":"inspect_file","path":"app/example.py"}',
            "invalid_post_observation_assessment",
            True,
        ),
        ('{"action":"inspect_file"', "json_decode_failed", False),
    ],
)
def test_wire_failures_are_strict_and_captured(
    tmp_path, wire, expected_code, after_observation
):
    provider = FakePlanningProvider([wire])
    events, sink = _capture_sink()
    adapter = PlanningGroundingProviderAdapter(provider, event_sink=sink)
    context = (
        _post_observation_context(tmp_path)
        if after_observation
        else _context(tmp_path, provider=provider)
    )

    with pytest.raises(Exception, match=expected_code):
        adapter.decide(context)

    assert events[-1]["event_type"] == "grounding_provider_turn"
    assert events[-1]["parser_rejection_code"] == expected_code
    assert events[-1]["parser_success"] is False
    assert events[-1]["prompt_length"] > 0
    assert "bounded_content" not in events[-1]


def test_provider_timeout_and_exception_are_captured_without_recovery(tmp_path):
    timeout = PlanningProviderExecutionError(
        classification="provider_timeout",
        detail="fake timeout",
        origin=ProviderFailureOrigin.INVOCATION,
    )
    for response, expected in (
        (timeout, "provider_timeout"),
        (RuntimeError("boom"), "provider_failure"),
    ):
        provider = FakePlanningProvider([response])
        events, sink = _capture_sink()
        adapter = PlanningGroundingProviderAdapter(provider, event_sink=sink)

        with pytest.raises(Exception, match=expected):
            adapter.decide(_context(tmp_path, provider=provider))

        assert events[-1]["parser_rejection_code"] == expected
        assert events[-1]["failure_classification"] == expected
        assert events[-1]["failure_layer"] == "L0_TRANSPORT"


def test_structured_mapping_is_the_only_non_string_candidate_representation(tmp_path):
    provider = FakePlanningProvider(
        [{"action": "inspect_file", "path": "app/example.py"}]
    )
    events, sink = _capture_sink()
    adapter = PlanningGroundingProviderAdapter(provider, event_sink=sink)

    proposal = adapter.decide(_context(tmp_path, provider=provider))

    assert proposal.action_payload["action"] == "inspect_file"
    assert events[-1]["json_decode_success"] is True
    assert events[-1]["provider_output_type"] == "dict"


def test_missing_and_non_object_content_have_distinct_bounded_capture_codes(tmp_path):
    for response, expected in (
        (None, "content_missing"),
        ('["not", "object"]', "json_not_object"),
    ):
        provider = FakePlanningProvider([response])
        events, sink = _capture_sink()
        adapter = PlanningGroundingProviderAdapter(provider, event_sink=sink)

        with pytest.raises(Exception, match=expected):
            adapter.decide(_context(tmp_path, provider=provider))

        assert events[-1]["parser_rejection_code"] == expected
        assert events[-1]["failure_layer"] in {
            "L2_CONTENT_EXTRACTION",
            "L4_JSON_ENVELOPE",
        }


def test_correction_prompt_contains_only_mechanical_rejection_data(tmp_path):
    context = _post_observation_context(tmp_path)
    context = replace(
        context,
        operator_task="Find the expected secret/path implementation.",
        state=replace(
            context.state,
            rejection_history=(
                GroundingRejection(
                    rejection_id="rejection-1",
                    grounding_run_id=context.state.grounding_run_id,
                    provider_request_id="grounding-provider-request-1",
                    code="invalid_request",
                    action_kind="inspect_file",
                    message="path-specific detail must not be copied",
                ),
            ),
        ),
    )
    prompt = render_rejection_correction_prompt(context)

    assert "PREVIOUS_REJECTION_CODE: invalid_request" in prompt
    assert "MECHANICAL_DIAGNOSTIC:" in prompt
    assert "path-specific detail must not be copied" not in prompt
    assert "expected secret/path" not in prompt
    assert "Find the expected" not in prompt
    assert "Do not return a Plan" in prompt


def test_first_and_post_prompts_preserve_operator_task_and_typed_state(tmp_path):
    first = _context(tmp_path)
    post = _post_observation_context(tmp_path)

    first_prompt = render_first_turn_prompt(first)
    post_prompt = render_post_observation_prompt(post)

    assert first.operator_task in first_prompt
    assert post.operator_task in post_prompt
    assert "candidate_paths" in first_prompt
    assert "grounding-observation-1" in post_prompt
    assert "remaining_budget" in first_prompt
    assert "NOT_FOUND" in post_prompt or "FOUND" in post_prompt


@pytest.mark.parametrize(
    ("wire", "after_observation"),
    [(wire, False) for wire in FIRST_TURN_WIRE_EXAMPLES]
    + [(wire, True) for wire in POST_OBSERVATION_WIRE_EXAMPLES],
)
def test_prompt_documented_examples_have_exact_parser_parity(
    tmp_path, wire, after_observation
):
    context = (
        _post_observation_context(tmp_path) if after_observation else _context(tmp_path)
    )
    prompt = (
        render_post_observation_prompt(context)
        if after_observation
        else render_first_turn_prompt(context)
    )
    payload = json.loads(wire)
    proposal = parse_grounding_provider_response(
        payload, after_observation=after_observation
    )

    assert wire in prompt
    assert proposal is not None


def test_normal_typed_path_selects_durable_adapter_without_external_injection(
    tmp_path, monkeypatch
):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "example.py").write_text("def target():\n    return True\n")
    subprocess.run(["git", "init", "-q"], cwd=tmp_path, check=True, shell=False)
    subprocess.run(["git", "add", "."], cwd=tmp_path, check=True, shell=False)
    provider = FakePlanningProvider(
        [
            '{"action":"inspect_file","path":"app/example.py"}',
            lambda request: {
                "decision": "SUFFICIENT",
                "cited_observation_ids": re.findall(
                    r"grounding-observation-[0-9a-f]+", request.prompt
                )[:1],
                "rationale": "The bounded observation is sufficient.",
            },
        ]
    )
    monkeypatch.setattr(settings, "ENABLE_TYPED_GROUNDING_COORDINATOR", True)
    monkeypatch.setattr(
        "app.services.planning.providers.create_planning_provider",
        lambda _db: provider,
    )
    ctx = SimpleNamespace(
        db=object(),
        session_id=1,
        task_id=2,
        task_execution_id=3,
        prompt="Find the implementation.",
        timeout_seconds=180,
        grounding_max_steps=2,
        grounding_max_provider_requests=3,
        grounding_snapshot_identity=str(tmp_path),
        orchestration_state=SimpleNamespace(project_dir=str(tmp_path)),
        control_state_location=tmp_path,
        logger=SimpleNamespace(debug=lambda *args, **kwargs: None),
    )

    result = planning_grounding_integration.run_typed_grounding_for_planning(ctx)

    assert result.terminal_state is GroundingLifecycleState.SUFFICIENT
    assert len(provider.requests) == 2
    assert provider.requests[0].artifact_kind is PlanningArtifactKind.GROUNDING
    assert not hasattr(ctx, "grounding_decision_provider")


def test_explicit_provider_override_remains_supported(tmp_path):
    provider = FakePlanningProvider(
        ['{"action":"inspect_file","path":"app/example.py"}']
    )
    adapter = PlanningGroundingProviderAdapter(provider)
    proposal = adapter.decide(_context(tmp_path, provider=provider))

    assert proposal.action_payload["action"] == "inspect_file"
    assert provider.requests
