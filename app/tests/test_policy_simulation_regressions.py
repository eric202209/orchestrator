from __future__ import annotations

import copy

import pytest

from app.services.orchestration.reporting.policy_simulation import (
    MAX_POLICY_EVIDENCE_EVENTS,
    MAX_POLICY_FINDINGS,
    MAX_POLICY_REASON_CODES,
    POLICY_COMPATIBILITY_VERSION,
    SIMULATION_VERSION,
    assert_policy_report_bounded,
    assert_simulation_safe_vocabulary,
    compare_policy_simulations,
    simulate_policy_from_replay,
)
from app.services.orchestration.reporting.replay import (
    COMPATIBILITY_VERSION,
    REDUCER_VERSION,
    reconstruct_execution_state,
)
from app.tests.replay_fixture_corpus import (
    REPLAY_FIXTURES,
    SESSION_ID,
    TASK_ID,
    materialize_replay_fixture,
)
from app.tests.report_semantic_assertions import (
    assert_policy_budget_contract,
    assert_policy_matches_golden,
)


@pytest.fixture(
    params=REPLAY_FIXTURES, ids=[fixture.fixture_id for fixture in REPLAY_FIXTURES]
)
def replay_report(tmp_path, request):
    fixture = request.param
    project_dir, checkpoint_dir = materialize_replay_fixture(tmp_path, fixture)
    return reconstruct_execution_state(
        project_dir=project_dir,
        session_id=SESSION_ID,
        task_id=TASK_ID,
        boundary=fixture.boundary,
        checkpoint_dir=checkpoint_dir,
        compare_workspace=fixture.compare_workspace,
    )


def test_policy_simulation_overlay_is_deterministic_and_bounded(replay_report):
    first = simulate_policy_from_replay(replay_report, policy_profile="balanced")
    second = simulate_policy_from_replay(replay_report, policy_profile="balanced")

    assert first == second
    assert first["simulation_version"] == SIMULATION_VERSION
    assert first["compatibility"]["version"] == POLICY_COMPATIBILITY_VERSION
    assert first["compatibility"]["reducer_version_supported"] == REDUCER_VERSION
    assert (
        first["compatibility"]["replay_compatibility_supported"]
        == COMPATIBILITY_VERSION
    )
    assert_policy_report_bounded(first)
    assert_policy_budget_contract(first)


@pytest.mark.parametrize(
    "fixture",
    REPLAY_FIXTURES,
    ids=[fixture.fixture_id for fixture in REPLAY_FIXTURES],
)
def test_policy_simulation_golden_reports(tmp_path, fixture):
    project_dir, checkpoint_dir = materialize_replay_fixture(tmp_path, fixture)
    replay_report = reconstruct_execution_state(
        project_dir=project_dir,
        session_id=SESSION_ID,
        task_id=TASK_ID,
        boundary=fixture.boundary,
        checkpoint_dir=checkpoint_dir,
        compare_workspace=fixture.compare_workspace,
    )
    policy_report = simulate_policy_from_replay(
        replay_report,
        policy_profile="balanced",
    )

    assert_policy_matches_golden(
        fixture.fixture_id,
        policy_report,
        profile="balanced",
    )


def test_policy_action_change_detection_for_strict_validation_rejection(tmp_path):
    fixture = next(
        item
        for item in REPLAY_FIXTURES
        if item.fixture_id == "validation_rejection_trace"
    )
    project_dir, checkpoint_dir = materialize_replay_fixture(tmp_path, fixture)
    replay_report = reconstruct_execution_state(
        project_dir=project_dir,
        session_id=SESSION_ID,
        task_id=TASK_ID,
        boundary=fixture.boundary,
        checkpoint_dir=checkpoint_dir,
    )

    balanced = simulate_policy_from_replay(replay_report, policy_profile="balanced")
    strict = simulate_policy_from_replay(replay_report, policy_profile="strict")
    comparison = compare_policy_simulations(balanced, strict)

    assert balanced["recommendation"]["action"] == "repair"
    assert strict["recommendation"]["action"] == "halt"
    assert comparison["classification"] == "action_change"


def test_policy_compatibility_drift_is_reported_without_runtime_effect(replay_report):
    incompatible = copy.deepcopy(replay_report)
    incompatible["reducer_version"] = "phase4a-v0"
    incompatible["compatibility_version"] = "legacy-compat"

    report = simulate_policy_from_replay(incompatible)

    finding_types = {finding["type"] for finding in report["compatibility"]["findings"]}
    assert "reducer_version_mismatch" in finding_types
    assert "compatibility_version_mismatch" in finding_types
    assert report["policy_determinism"]["level"] == "degraded"
    assert_policy_report_bounded(report)


def test_policy_evidence_budgets_are_enforced(replay_report):
    overloaded = copy.deepcopy(replay_report)
    overloaded["integrity"]["unknown_event_types"] = [
        f"future_event_{index}" for index in range(MAX_POLICY_EVIDENCE_EVENTS * 2)
    ]
    overloaded["drift_findings"] = [
        {"type": f"drift_{index}", "severity": "info"}
        for index in range(MAX_POLICY_FINDINGS * 2)
    ]
    overloaded["state"]["validation_verdict_status_history"] = [
        "rejected" for _ in range(MAX_POLICY_REASON_CODES * 2)
    ]

    report = simulate_policy_from_replay(overloaded)

    assert len(report["compatibility"]["evidence_gaps"]) <= 10
    assert_policy_report_bounded(report)
    assert_policy_budget_contract(report)


def test_simulation_safe_event_vocabulary_is_replay_compatible():
    assert_simulation_safe_vocabulary()
