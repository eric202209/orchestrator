"""PHASE35-ECR1 — evaluator contract repair, provider-free.

PHASE35-TBD1 established that the collapsed ``FOUND_RELEVANT`` metric was
stronger than the production sufficiency contract: production cites and hands
off ``inspect_file`` FOUND evidence carrying no ``StructuralIdentity``.  These
tests pin the repaired orthogonal metrics and prove the frozen truth still
cannot reach provider-visible state.
"""

from __future__ import annotations

from pathlib import Path

import pytest

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
from app.tests.evals.phase35_pvh1_validation_harness import (
    CASE_A_TRUTH,
    CASE_B_TRUTH,
    EVALUATOR_TARGET_METRICS,
    ProviderValidationHarness,
    evaluate_frozen_case,
    evaluate_observation,
)
from app.tests.test_phase35_pvh1_provider_validation_harness import (
    ScriptedPlanningProvider,
    _repo,
    _sufficient,
)


RATE_LIMIT_SOURCE = (
    '"""Best-effort in-memory auth rate limiting."""\n'
    "\n"
    "\n"
    "class RateLimitBucket:\n"
    "    attempts = 0\n"
    "\n"
    "\n"
    "def enforce_auth_rate_limit(request, action):\n"
    "    return None\n"
    "\n"
    "\n"
    "def unrelated_helper():\n"
    "    return None\n"
)

CASE_B_FILES = {
    CASE_B_TRUTH.source_path: RATE_LIMIT_SOURCE,
    "app/services/other/decoy.py": "def enforce_auth_rate_limit():\n    return 1\n",
}

CASE_A_FILES = {
    CASE_A_TRUTH.source_path: (
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
}


def _observe(root: Path, action: dict) -> object:
    """Produce one real observation through the production executor."""

    request = parse_grounding_request(
        action, grounding_run_id="ecr1-run", request_id="ecr1-request-1"
    )
    return GroundingExecutor(root, snapshot_identity="ecr1-snapshot").execute(request)


def _metrics(evaluation) -> tuple[bool, bool, bool]:
    return (
        evaluation.target_path_reached,
        evaluation.target_content_inspected,
        evaluation.target_structure_resolved,
    )


def _case_b_repo(root: Path) -> Path:
    return _repo(root, CASE_B_FILES)


def _run(root: Path, files, responses, *, evaluator_case, label="E1", max_steps=4):
    """Drive the real coordinator; no production behaviour is altered."""

    _repo(root, files)
    harness = ProviderValidationHarness(labels=(label,))
    run = harness.start_run(label, grounding_run_id=f"ecr1-{label}-run")
    provider = ScriptedPlanningProvider(list(responses))
    adapter = PlanningGroundingProviderAdapter(
        run.capture_provider(provider), event_sink=run.event_sink
    )
    config = GroundingRunConfig(
        grounding_run_id=f"ecr1-{label}-run",
        task_reference=GroundingTaskReference(task_id=f"task-{label}"),
        workspace_identity=str(root.resolve()),
        snapshot_identity="ecr1-snapshot",
        max_steps=max_steps,
        max_exploration_provider_requests=4,
        operator_task="Find the relevant implementation.",
    )
    result = GroundingCoordinator(
        executor=GroundingExecutor(root, snapshot_identity="ecr1-snapshot"),
        provider=adapter,
        config=config,
        event_sink=run.event_sink,
    ).run()
    record = harness.finalize(label, result, evaluator_case=evaluator_case)
    return record, result, provider


# T1 -------------------------------------------------------------------------
def test_t1_search_text_hit_on_expected_path_is_candidate_only(tmp_path):
    root = _case_b_repo(tmp_path)
    observation = _observe(
        root, {"action": "search_text", "query": "RateLimitBucket", "scopes": ["app"]}
    )
    assert observation.outcome is GroundingOutcome.FOUND
    assert _metrics(evaluate_observation(observation, CASE_B_TRUTH)) == (
        True,
        False,
        False,
    )


# T2 -------------------------------------------------------------------------
def test_t2_inspect_file_exact_target_reaches_path_and_content(tmp_path):
    root = _case_b_repo(tmp_path)
    observation = _observe(
        root, {"action": "inspect_file", "path": CASE_B_TRUTH.source_path}
    )
    evaluation = evaluate_observation(observation, CASE_B_TRUTH)
    assert observation.structural_identity is None
    assert _metrics(evaluation) == (True, True, False)
    assert evaluation.bounded_content_length > 0
    # The legacy label is retained but is explicitly not the success metric.
    assert evaluation.classification == "FOUND_NON_RELEVANT"


# T3 -------------------------------------------------------------------------
def test_t3_resolve_structure_exact_case_a_route_resolves_structure(tmp_path):
    root = _repo(tmp_path, CASE_A_FILES)
    observation = _observe(
        root,
        {
            "action": "resolve_structure",
            "relation": "mounted_route",
            "locator": {
                "path": CASE_A_TRUTH.source_path,
                "method": "GET",
                "decorator_path": "/",
            },
        },
    )
    assert _metrics(evaluate_observation(observation, CASE_A_TRUTH)) == (
        True,
        False,
        True,
    )


# T4 -------------------------------------------------------------------------
@pytest.mark.parametrize("symbol", ["enforce_auth_rate_limit", "RateLimitBucket"])
def test_t4_resolve_structure_exact_case_b_symbol_resolves_structure(tmp_path, symbol):
    root = _case_b_repo(tmp_path)
    observation = _observe(
        root,
        {
            "action": "resolve_structure",
            "relation": "symbol_definition",
            "locator": {"path": CASE_B_TRUTH.source_path, "name": symbol},
        },
    )
    assert _metrics(evaluate_observation(observation, CASE_B_TRUTH)) == (
        True,
        False,
        True,
    )


# T5 -------------------------------------------------------------------------
def test_t5_exact_path_with_wrong_symbol_reaches_path_but_not_structure(tmp_path):
    root = _case_b_repo(tmp_path)
    observation = _observe(
        root,
        {
            "action": "resolve_structure",
            "relation": "symbol_definition",
            "locator": {"path": CASE_B_TRUTH.source_path, "name": "unrelated_helper"},
        },
    )
    assert _metrics(evaluate_observation(observation, CASE_B_TRUTH)) == (
        True,
        False,
        False,
    )


# T6 -------------------------------------------------------------------------
def test_t6_wrong_path_evidence_satisfies_no_metric(tmp_path):
    root = _case_b_repo(tmp_path)
    observation = _observe(
        root, {"action": "inspect_file", "path": "app/services/other/decoy.py"}
    )
    assert observation.outcome is GroundingOutcome.FOUND
    assert _metrics(evaluate_observation(observation, CASE_B_TRUTH)) == (
        False,
        False,
        False,
    )


# T7 -------------------------------------------------------------------------
def test_t7_not_found_satisfies_no_metric_and_keeps_outcome(tmp_path):
    root = _case_b_repo(tmp_path)
    observation = _observe(
        root, {"action": "search_text", "query": "zzz-absent-token", "scopes": ["app"]}
    )
    evaluation = evaluate_observation(observation, CASE_B_TRUTH)
    assert observation.outcome is GroundingOutcome.NOT_FOUND
    assert _metrics(evaluation) == (False, False, False)
    assert evaluation.classification == "NOT_FOUND"
    assert evaluation.outcome == "NOT_FOUND"


# T8 -------------------------------------------------------------------------
def test_t8_ambiguous_outcome_is_preserved_and_satisfies_no_metric(tmp_path):
    root = _repo(
        tmp_path,
        {
            CASE_B_TRUTH.source_path: (
                "def enforce_auth_rate_limit():\n"
                "    return 1\n"
                "\n"
                "\n"
                "def enforce_auth_rate_limit():\n"
                "    return 2\n"
            )
        },
    )
    observation = _observe(
        root,
        {
            "action": "resolve_structure",
            "relation": "symbol_definition",
            "locator": {
                "path": CASE_B_TRUTH.source_path,
                "name": "enforce_auth_rate_limit",
            },
        },
    )
    evaluation = evaluate_observation(observation, CASE_B_TRUTH)
    assert observation.outcome is GroundingOutcome.AMBIGUOUS
    assert _metrics(evaluation) == (False, False, False)
    assert evaluation.classification == "AMBIGUOUS"


# T9 -------------------------------------------------------------------------
def test_t9_pgv5_b3_replay_reports_path_and_content_without_structure(tmp_path):
    """The exact PGV5 B3 shape: search, then inspect the frozen target file."""

    record, result, _ = _run(
        tmp_path,
        CASE_B_FILES,
        [
            {"action": "search_text", "query": "rate limit", "scopes": ["app"]},
            {
                "decision": "NEED_MORE_EVIDENCE",
                "next_action": {
                    "action": "inspect_file",
                    "path": CASE_B_TRUTH.source_path,
                },
                "rationale": "Inspect the current rate limiting implementation.",
            },
            {"decision": "INSUFFICIENT", "reason": "Caller context is still missing."},
        ],
        evaluator_case="B",
    )
    evaluation = record.evaluator_result
    assert evaluation["target_path_reached"] is True
    assert evaluation["target_content_inspected"] is True
    assert evaluation["target_structure_resolved"] is False
    assert record.target_path_reached is True
    assert record.target_content_inspected is True
    assert record.target_structure_resolved is False
    assert [item["depth"] for item in evaluation["observations"]] == [
        "CANDIDATE_ONLY",
        "CONTENT_INSPECTED",
    ]
    assert all(item.structural_identity is None for item in result.observations)


# T10 ------------------------------------------------------------------------
def test_t10_aggregates_are_monotonic_across_later_unrelated_evidence(tmp_path):
    record, _, _ = _run(
        tmp_path,
        CASE_B_FILES,
        [
            {"action": "inspect_file", "path": CASE_B_TRUTH.source_path},
            {
                "decision": "NEED_MORE_EVIDENCE",
                "next_action": {
                    "action": "inspect_file",
                    "path": "app/services/other/decoy.py",
                },
                "rationale": "Check the unrelated helper for completeness.",
            },
            _sufficient,
        ],
        evaluator_case="B",
        label="E10",
    )
    evaluation = record.evaluator_result
    # The later wrong-path observation must not erase the earlier target facts.
    assert evaluation["observations"][1]["target_path_reached"] is False
    assert evaluation["target_path_reached"] is True
    assert evaluation["target_content_inspected"] is True
    assert evaluation["target_structure_resolved"] is False


# T11 ------------------------------------------------------------------------
def test_t11_candidate_only_search_hit_remains_refinement_eligible(tmp_path):
    record, _, _ = _run(
        tmp_path,
        CASE_B_FILES,
        [
            {"action": "search_text", "query": "RateLimitBucket", "scopes": ["app"]},
            {
                "decision": "NEED_MORE_EVIDENCE",
                "next_action": {
                    "action": "inspect_file",
                    "path": CASE_B_TRUTH.source_path,
                },
                "rationale": "The search only produced a candidate path.",
            },
            _sufficient,
        ],
        evaluator_case="B",
        label="E11",
    )
    evaluation = record.evaluator_result
    assert evaluation["observations"][0]["target_path_reached"] is True
    assert evaluation["first_observation_depth"] == "CANDIDATE_ONLY"
    assert evaluation["refinement_eligible"] is True
    assert record.refinement_eligible is True


def test_t11b_target_content_on_first_observation_ends_refinement_eligibility(tmp_path):
    record, _, _ = _run(
        tmp_path,
        CASE_B_FILES,
        [
            {"action": "inspect_file", "path": CASE_B_TRUTH.source_path},
            _sufficient,
        ],
        evaluator_case="B",
        label="E11B",
    )
    evaluation = record.evaluator_result
    assert evaluation["first_observation_depth"] == "CONTENT_INSPECTED"
    assert evaluation["refinement_eligible"] is False


# T12 ------------------------------------------------------------------------
def test_t12_frozen_truth_never_reaches_provider_visible_state(tmp_path):
    record, result, provider = _run(
        tmp_path,
        CASE_B_FILES,
        [
            {"action": "inspect_file", "path": CASE_B_TRUTH.source_path},
            _sufficient,
        ],
        evaluator_case="B",
        label="E12",
    )
    prompts = "\n".join(request.prompt for request in provider.requests)
    assert prompts
    # No evaluator vocabulary reaches any provider turn.
    for token in (
        "target_path_reached",
        "target_content_inspected",
        "target_structure_resolved",
        "FOUND_RELEVANT",
        "FOUND_NON_RELEVANT",
        "CANDIDATE_ONLY",
        "STRUCTURE_RESOLVED",
        "legacy_classification_authoritative",
    ):
        assert token not in prompts
    # The frozen target cannot be seeded: it is absent from the first turn,
    # before the model has chosen any action of its own.  Later turns legitimately
    # echo whatever the model itself retrieved, which is production evidence and
    # not evaluator truth.
    first_prompt = provider.requests[0].prompt
    assert CASE_B_TRUTH.source_path not in first_prompt
    for symbol in CASE_B_TRUTH.symbol_names:
        assert symbol not in first_prompt
    # The evaluator result is report-only and carries no provider context.
    evaluation = record.evaluator_result
    assert "provider_prompt" not in evaluation
    assert "GroundingDecisionContext" not in evaluation
    assert CASE_A_TRUTH.source_path not in str(evaluation)
    # Frozen truth is absent from the production result itself.
    assert not result.orientation_advisory


# T13 ------------------------------------------------------------------------
def test_t13_legacy_classification_is_retained_but_not_authoritative(tmp_path):
    root = _case_b_repo(tmp_path)
    observation = _observe(
        root, {"action": "inspect_file", "path": CASE_B_TRUTH.source_path}
    )
    evaluation = evaluate_observation(observation, CASE_B_TRUTH)
    # Legacy label still says non-relevant; the repaired metrics say otherwise.
    assert evaluation.classification == "FOUND_NON_RELEVANT"
    assert evaluation.target_content_inspected is True


def test_t13b_evaluate_frozen_case_marks_legacy_label_non_authoritative(tmp_path):
    record, _, _ = _run(
        tmp_path,
        CASE_B_FILES,
        [
            {"action": "inspect_file", "path": CASE_B_TRUTH.source_path},
            _sufficient,
        ],
        evaluator_case="B",
        label="E13",
    )
    evaluation = record.evaluator_result
    assert evaluation["legacy_classification_authoritative"] is False
    assert set(EVALUATOR_TARGET_METRICS) <= set(evaluation)


# T14 ------------------------------------------------------------------------
def test_t14_evaluation_does_not_mutate_production_objects(tmp_path):
    root = _case_b_repo(tmp_path)
    observation = _observe(
        root, {"action": "inspect_file", "path": CASE_B_TRUTH.source_path}
    )
    before = (
        observation.observation_id,
        observation.action_identity,
        observation.outcome,
        tuple(observation.source_paths),
        observation.structural_identity,
        observation.bounded_content,
        dict(observation.source_versions),
    )
    evaluate_observation(observation, CASE_B_TRUTH)
    evaluate_observation(observation, CASE_A_TRUTH)
    after = (
        observation.observation_id,
        observation.action_identity,
        observation.outcome,
        tuple(observation.source_paths),
        observation.structural_identity,
        observation.bounded_content,
        dict(observation.source_versions),
    )
    assert before == after
    with pytest.raises((AttributeError, TypeError)):
        observation.outcome = GroundingOutcome.NOT_FOUND


def test_t14b_evaluate_observation_rejects_non_production_observation():
    class FakeObservation:
        action_identity = "inspect_file"
        outcome = GroundingOutcome.FOUND
        source_paths = (CASE_B_TRUTH.source_path,)
        structural_identity = None
        bounded_content = b""
        observation_id = "fake"

    with pytest.raises(TypeError):
        evaluate_observation(FakeObservation(), CASE_B_TRUTH)


def test_t15_evaluate_frozen_case_requires_a_known_case(tmp_path):
    record, result, _ = _run(
        tmp_path,
        CASE_B_FILES,
        [
            {"action": "inspect_file", "path": CASE_B_TRUTH.source_path},
            _sufficient,
        ],
        evaluator_case="B",
        label="E15",
    )
    assert record.evaluator_result["case"] == "B"
    with pytest.raises(ValueError):
        evaluate_frozen_case(result, "Z")
