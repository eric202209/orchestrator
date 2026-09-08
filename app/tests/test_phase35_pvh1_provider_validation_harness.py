"""Provider-free PHASE35-PVH1 validation-harness reliability tests."""

from __future__ import annotations

from pathlib import Path
import re
import subprocess
from types import SimpleNamespace

import pytest

from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.planning.grounding import (
    GroundingCoordinator,
    GroundingExecutor,
    GroundingOutcome,
    GroundingRunConfig,
    GroundingTaskReference,
    PlanningGroundingProviderAdapter,
)
from app.services.orchestration.planning.grounding.contracts import (
    observation_from_request,
    parse_grounding_request,
)
from app.services.orchestration.planning.grounding.coordinator_contracts import (
    GroundingLifecycleState,
    GroundingTerminalReason,
)
from app.services.planning.providers.base import (
    ExecutionMetadata,
    PlanningResponse,
    PlanningProviderExecutionError,
    ProviderCapabilities,
    ProviderFailureOrigin,
    ProviderHealth,
    ProviderRuntimeInformation,
)
from app.tests.evals.phase35_pvh1_validation_harness import (
    CASE_A_TRUTH,
    CASE_B_TRUTH,
    ProviderValidationHarness,
    RawRunCapture,
    build_validation_run_record,
    normalize_event_type,
    serialize_grounding_observation,
)


class ScriptedPlanningProvider:
    """Provider-free PlanningProvider double for production coordinator runs."""

    name = "pvh1-scripted"
    version = "pvh1-1"
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
        return ProviderHealth(available=True, ready=True, status="scripted")

    def runtime_information(self):
        return ProviderRuntimeInformation(
            provider_name=self.name,
            provider_version=self.version,
            runtime_name="scripted-backend",
            model="scripted-model",
            adaptation_profile="scripted-profile",
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
                runtime_name="scripted-backend",
                model="scripted-model",
                adaptation_profile="scripted-profile",
            ),
        )


def _repo(root: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    subprocess.run(["git", "init", "-q"], cwd=root, check=True, shell=False)
    subprocess.run(["git", "add", "."], cwd=root, check=True, shell=False)
    return root


def _sufficient(request):
    observation_ids = re.findall(r"grounding-observation-[0-9a-f]+", request.prompt)
    return {
        "decision": "SUFFICIENT",
        "cited_observation_ids": [observation_ids[-1]],
        "rationale": "The bounded typed observation is sufficient.",
    }


def _run_case(root: Path, label: str, files, responses, *, evaluator_case=None):
    _repo(root, files)
    harness = ProviderValidationHarness(labels=(label,))
    run = harness.start_run(label, grounding_run_id=f"pvh1-{label}-run")
    provider = ScriptedPlanningProvider(responses)
    adapter = PlanningGroundingProviderAdapter(provider, event_sink=run.event_sink)
    config = GroundingRunConfig(
        grounding_run_id=f"pvh1-{label}-run",
        task_reference=GroundingTaskReference(task_id=f"task-{label}"),
        workspace_identity=str(root.resolve()),
        snapshot_identity="pvh1-snapshot",
        max_steps=4,
        max_provider_requests=4,
        operator_task="Find the relevant implementation.",
    )
    result = GroundingCoordinator(
        executor=GroundingExecutor(root, snapshot_identity="pvh1-snapshot"),
        provider=adapter,
        config=config,
        event_sink=run.event_sink,
    ).run()
    record = harness.finalize(label, result, evaluator_case=evaluator_case)
    return record, result, harness, provider


def test_a1_eventtype_failure_is_reproduced_and_fixed():
    with pytest.raises(AttributeError, match="has no attribute 'value'"):
        EventType.GROUNDING_PROVIDER_TURN.value

    assert normalize_event_type(EventType.GROUNDING_PROVIDER_TURN) == (
        "grounding_provider_turn"
    )
    assert normalize_event_type("custom-event") == "custom-event"


def test_a1_enum_normalization_is_supported_without_affecting_eventtype_contract():
    from enum import Enum

    class EventIdentity(Enum):
        CAPTURE = "capture"

    assert normalize_event_type(EventIdentity.CAPTURE) == "capture"


def test_b2_summary_failure_is_reproduced_and_typed_serialization_is_fixed():
    request = parse_grounding_request(
        {"action": "inspect_file", "path": "app/example.py"},
        grounding_run_id="b2-run",
        request_id="b2-request",
    )
    observation = observation_from_request(
        request,
        observation_id="b2-observation",
        outcome=GroundingOutcome.NOT_FOUND,
        source_paths=("app/example.py",),
        result_limit=1,
    )
    with pytest.raises(AttributeError, match="has no attribute 'summary'"):
        observation.summary

    serialized = serialize_grounding_observation(observation)
    assert serialized["observation_id"] == "b2-observation"
    assert serialized["request_id"] == "b2-request"
    assert serialized["outcome"] == "NOT_FOUND"
    assert serialized["source_paths"] == ["app/example.py"]
    assert "summary" not in serialized


def test_raw_capture_precedes_normalization_and_survives_intentional_failure(
    tmp_path,
):
    _, result, harness, _ = _run_case(
        tmp_path,
        "B2",
        {"app/example.py": "value = 1\n"},
        [
            {"action": "inspect_file", "path": "app/example.py"},
            {"decision": "INSUFFICIENT", "reason": "capture self-test"},
        ],
    )
    run = harness.run("B2")

    def broken_normalizer(_raw: RawRunCapture):
        raise RuntimeError("intentional report normalization failure")

    with pytest.raises(RuntimeError, match="intentional"):
        run.finalize(normalizer=broken_normalizer)

    raw = harness.raw_capture("B2")
    assert len(raw.provider_turn_events) == result.provider_request_count
    assert len(raw.results) == 1
    assert raw.results[0].observations[0].observation_id == (
        result.observations[0].observation_id
    )
    assert build_validation_run_record(raw).provider_turn_count == (
        result.provider_request_count
    )


def test_run_capture_isolation_preserves_earlier_label_after_later_failure():
    harness = ProviderValidationHarness(labels=("A1", "B2"))
    first = harness.start_run("A1", "a1-run")
    later = harness.start_run("B2", "b2-run")
    first.event_sink(
        EventType.GROUNDING_PROVIDER_TURN,
        {"grounding_run_id": "a1-run", "provider_request_id": "a1-turn"},
    )
    first_record = harness.finalize("A1")
    later.event_sink(
        EventType.GROUNDING_PROVIDER_TURN,
        {"grounding_run_id": "b2-run", "provider_request_id": "b2-turn"},
    )

    with pytest.raises(RuntimeError, match="normalization"):
        harness.finalize(
            "B2",
            normalizer=lambda _raw: (_ for _ in ()).throw(
                RuntimeError("normalization")
            ),
        )

    assert (
        harness.raw_capture("A1").provider_turn_events[0].details["provider_request_id"]
        == "a1-turn"
    )
    assert (
        harness.raw_capture("B2").provider_turn_events[0].details["provider_request_id"]
        == "b2-turn"
    )
    assert harness.matrix_records() == (first_record,)
    assert harness.matrix_summary()[0]["provider_turn_count"] == 1


def test_provider_free_matrix_fixtures_have_complete_final_summaries(tmp_path):
    cases = [
        (
            "A1",
            {
                "app/api/v1/endpoints/projects.py": (
                    "from fastapi import APIRouter\n"
                    "router = APIRouter()\n"
                    "@router.get('/')\n"
                    "def get_projects():\n"
                    "    return []\n"
                ),
                "app/api/v1/router.py": (
                    "from fastapi import APIRouter\n"
                    "from app.api.v1.endpoints.projects import router as projects_router\n"
                    "api_router = APIRouter()\n"
                    "api_router.include_router(projects_router, prefix='/projects')\n"
                ),
            },
            [
                {
                    "action": "resolve_structure",
                    "relation": "mounted_route",
                    "locator": {
                        "path": "app/api/v1/endpoints/projects.py",
                        "method": "GET",
                        "decorator_path": "/",
                    },
                },
                _sufficient,
            ],
            "A",
        ),
        (
            "B2",
            {
                "app/services/auth/rate_limit.py": (
                    "class RateLimitBucket:\n"
                    "    pass\n\n"
                    "def enforce_auth_rate_limit():\n"
                    "    return RateLimitBucket()\n"
                ),
            },
            [
                {"action": "inspect_file", "path": "app/services/auth/missing.py"},
                {
                    "decision": "NEED_MORE_EVIDENCE",
                    "next_action": {
                        "action": "resolve_structure",
                        "relation": "symbol_definition",
                        "locator": {
                            "path": "app/services/auth/rate_limit.py",
                            "name": "enforce_auth_rate_limit",
                        },
                    },
                    "rationale": "The first repository request was not found.",
                },
                _sufficient,
            ],
            "B",
        ),
        (
            "A2",
            {"app/repeat.py": "value = 1\n"},
            [
                {"action": "inspect_file", "path": "app/missing.py"},
                {
                    "decision": "NEED_MORE_EVIDENCE",
                    "next_action": {
                        "action": "inspect_file",
                        "path": "app/missing.py",
                    },
                    "rationale": "Repeat the bounded request.",
                },
                {"decision": "INSUFFICIENT", "reason": "The request repeated."},
            ],
            None,
        ),
        (
            "A3",
            {"app/target.py": "value = 1\n"},
            ['{"action":"inspect_file","path":"app/target.py","extra":true}'],
            None,
        ),
        (
            "B1",
            {"app/target.py": "value = 1\n"},
            [
                PlanningProviderExecutionError(
                    classification="provider_timeout",
                    detail="synthetic timeout",
                    origin=ProviderFailureOrigin.INVOCATION,
                )
            ],
            None,
        ),
        (
            "B3",
            {"app/target.py": "value = 1\n"},
            [RuntimeError("synthetic provider failure")],
            None,
        ),
        (
            "C1",
            {"app/target.py": "value = 1\n"},
            ["not json"],
            None,
        ),
        (
            "C2",
            {"app/target.py": "value = 1\n"},
            [
                {"action": "inspect_file", "path": "../outside.py"},
                {"action": "inspect_file", "path": "../outside.py"},
            ],
            None,
        ),
        (
            "C3",
            {
                "app/ambiguous.py": (
                    "def duplicate():\n"
                    "    return 1\n\n"
                    "def duplicate():\n"
                    "    return 2\n"
                )
            },
            [
                {
                    "action": "resolve_structure",
                    "relation": "symbol_definition",
                    "locator": {"path": "app/ambiguous.py", "name": "duplicate"},
                },
                {"decision": "INSUFFICIENT", "reason": "The region is ambiguous."},
            ],
            None,
        ),
    ]

    records = []
    for index, (label, files, responses, evaluator_case) in enumerate(cases):
        record, result, _, _ = _run_case(
            tmp_path / f"case-{index}",
            label,
            files,
            responses,
            evaluator_case=evaluator_case,
        )
        records.append(record)
        assert record.serialized_result is not None
        assert record.grounding_run_id == result.grounding_run_id
        assert record.provider_turn_count == len(record.provider_turns)
        assert record.observation_count == len(record.observations)
        assert record.terminal_state == result.terminal_state.value
        assert record.terminal_reason == result.terminal_reason.value

    by_label = {record.label: record for record in records}
    assert by_label["A1"].terminal_reason == "SUFFICIENT"
    assert by_label["A1"].observation_outcomes == ("FOUND",)
    assert by_label["A1"].observed_paths == (
        "app/api/v1/endpoints/projects.py",
        "app/api/v1/router.py",
    )
    assert by_label["A1"].structural_identities[0]["handler_name"] == ("get_projects")
    assert by_label["A1"].observations[0]["source_versions"]
    assert by_label["A1"].post_observation_assessment_count == 1
    assert by_label["A1"].assessments[0]["kind"] == "SUFFICIENT"
    assert (
        by_label["A1"].evaluator_result["observations"][0]["classification"]
        == "FOUND_RELEVANT"
    )
    assert by_label["B2"].observation_outcomes == ("NOT_FOUND", "FOUND")
    assert by_label["B2"].refinement_eligible is True
    assert by_label["B2"].hypothesis_changed is True
    assert by_label["B2"].first_action_digest != by_label["B2"].next_action_digest
    assert by_label["B2"].post_observation_assessment_count == 2
    assert by_label["A2"].second_action_differs_from_first is False
    assert by_label["A3"].parser_failure_count == 1
    assert by_label["B1"].runtime_failures == ("provider_timeout",)
    assert by_label["B3"].runtime_failures == ("provider_failure",)
    assert by_label["C1"].parser_failure_count == 1
    assert by_label["C2"].terminal_reason == "INVALID_MODEL_REQUEST"
    assert by_label["C2"].provider_turn_count == 2
    assert by_label["C2"].rejections[0]["code"] == "invalid_request"
    assert by_label["C3"].observation_outcomes == ("AMBIGUOUS",)


def test_coordinator_invalid_request_retains_adapter_acceptance_and_supplement(
    tmp_path,
):
    record, result, harness, _ = _run_case(
        tmp_path,
        "H",
        {"app/target.py": "value = 1\n"},
        [
            {"action": "inspect_file", "path": "../outside.py"},
            {"action": "inspect_file", "path": "../outside.py"},
        ],
    )
    raw = harness.raw_capture("H")
    assert result.terminal_reason is GroundingTerminalReason.INVALID_MODEL_REQUEST
    assert record.provider_turn_count == 2
    assert len(raw.provider_turn_events) == 4
    assert any(
        event.details.get("parser_success") is True
        and "capture_stage" not in event.details
        for event in raw.provider_turn_events
    )
    assert any(
        event.details.get("capture_stage") == "coordinator_request_validation"
        for event in raw.provider_turn_events
    )
    assert all(
        field not in raw.provider_turn_events[0].details
        for field in ("prompt", "candidate_text", "source_content")
    )


def test_canonical_sufficient_grounding_result_is_reported_from_actual_contract(
    tmp_path,
):
    record, result, _, _ = _run_case(
        tmp_path,
        "I",
        {"app/example.py": "def target():\n    return True\n"},
        [
            {"action": "inspect_file", "path": "app/example.py"},
            _sufficient,
        ],
    )
    assert result.terminal_state is GroundingLifecycleState.SUFFICIENT
    assert record.serialized_result["terminal_reason"] == "SUFFICIENT"
    assert record.serialized_result["provider_request_count"] == 2
    assert record.serialized_result["observations"][0]["action_identity"] == (
        "inspect_file"
    )


def test_frozen_evaluator_is_report_only_and_not_provider_context(tmp_path):
    record, result, _, provider = _run_case(
        tmp_path,
        "A",
        {
            "app/api/v1/endpoints/projects.py": (
                "from fastapi import APIRouter\n"
                "router = APIRouter()\n"
                "@router.get('/')\n"
                "def get_projects():\n"
                "    return []\n"
            ),
            "app/api/v1/router.py": (
                "from fastapi import APIRouter\n"
                "from app.api.v1.endpoints.projects import router as projects_router\n"
                "api_router = APIRouter()\n"
                "api_router.include_router(projects_router, prefix='/projects')\n"
            ),
        },
        [
            {
                "action": "resolve_structure",
                "relation": "mounted_route",
                "locator": {
                    "path": CASE_A_TRUTH.source_path,
                    "method": "GET",
                    "decorator_path": "/",
                },
            },
            _sufficient,
        ],
        evaluator_case="A",
    )
    evaluation = record.evaluator_result
    assert evaluation["observations"][0]["classification"] == "FOUND_RELEVANT"
    assert "provider_prompt" not in evaluation
    assert "GroundingDecisionContext" not in evaluation
    assert CASE_B_TRUTH.source_path not in str(evaluation)
    assert all("FOUND_RELEVANT" not in request.prompt for request in provider.requests)
    assert result.grounding_run_id == record.grounding_run_id
