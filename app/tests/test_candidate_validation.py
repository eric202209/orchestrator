"""Phase 17G-V: Candidate Recovery validation analytics tests."""

from __future__ import annotations

import json
from pathlib import Path

from app.services.analytics.candidate_recovery_validation import (
    CandidateRecoveryRecord,
    CandidateReplayCase,
    aggregate_candidate_recovery,
    compare_feature_flag_replays,
    deterministic_selection_trace,
    verify_candidate_audit_sequence,
)
from app.services.orchestration.events.event_types import EventType
from app.services.planning.plan_candidate import PlanCandidate


def _candidate(
    candidate_id: str,
    *,
    status: str,
    lineage: str,
    operator: str,
) -> PlanCandidate:
    return PlanCandidate(
        candidate_id=candidate_id,
        parent_candidate_ids=(),
        operator=operator,
        source_lineage=lineage,
        artifact_hash=f"hash-{candidate_id}",
        validator_status=status,
        validator_reasons=(),
        planning_failure_signature="sig",
        runtime_profile="standard",
    )


def test_candidate_recovery_analytics_aggregate_validation_metrics():
    summary = aggregate_candidate_recovery(
        [
            CandidateRecoveryRecord(
                case_id="rescued",
                feature_flag_enabled=True,
                machine_profile="standard",
                triggered=True,
                outcome="selected",
                candidate_count=2,
                selected_candidate_id="candidate-sibling-1",
                selected_lineage="sibling",
                validator_statuses={
                    "candidate-original": "repair_required",
                    "candidate-sibling-1": "accepted",
                },
                duration_ms=1200,
                token_estimate=900,
                baseline_validator_status="repair_required",
                final_validator_status="accepted",
            ),
            CandidateRecoveryRecord(
                case_id="exhausted",
                feature_flag_enabled=True,
                machine_profile="standard",
                triggered=True,
                outcome="exhausted",
                candidate_count=2,
                validator_statuses={
                    "candidate-original": "rejected",
                    "candidate-sibling-1": "repair_required",
                },
                duration_ms=800,
                token_estimate=700,
                baseline_validator_status="rejected",
                final_validator_status="rejected",
            ),
            CandidateRecoveryRecord(
                case_id="flag-off",
                feature_flag_enabled=False,
                machine_profile="standard",
                triggered=False,
                outcome="skipped",
            ),
        ]
    )

    assert summary.total == 3
    assert summary.trigger_count == 2
    assert summary.skipped_count == 1
    assert summary.success_count == 1
    assert summary.exhausted_count == 1
    assert summary.selected_sibling == 1
    assert summary.rescue_count == 1
    assert summary.rescue_rate == 0.5
    assert summary.sibling_selection_rate == 1.0
    assert summary.average_candidate_count == 2
    assert summary.average_latency_ms == 1000
    assert summary.average_token_estimate == 800
    assert summary.validator_status_distribution == {
        "repair_required": 2,
        "accepted": 1,
        "rejected": 1,
    }


def test_candidate_audit_sequence_complete_for_success():
    complete, issues = verify_candidate_audit_sequence(
        [
            EventType.RECOVERY_DECISION_ROUTED,
            EventType.RECOVERY_STARTED,
            EventType.PLAN_CANDIDATE_CREATED,
            EventType.PLAN_CANDIDATE_VALIDATED,
            EventType.PLAN_CANDIDATE_CREATED,
            EventType.PLAN_CANDIDATE_VALIDATED,
            EventType.PLAN_CANDIDATE_SELECTED,
            EventType.PLAN_CANDIDATE_REJECTED,
            EventType.RECOVERY_COMPLETED,
            EventType.RECOVERY_RESUMED,
        ],
        audit_event_ids=("1", "2", "3"),
    )

    assert complete is True
    assert issues == []


def test_candidate_audit_sequence_reports_missing_selected_candidate():
    complete, issues = verify_candidate_audit_sequence(
        [
            EventType.RECOVERY_DECISION_ROUTED,
            EventType.RECOVERY_STARTED,
            EventType.PLAN_CANDIDATE_CREATED,
            EventType.PLAN_CANDIDATE_VALIDATED,
            EventType.PLAN_CANDIDATE_CREATED,
            EventType.PLAN_CANDIDATE_VALIDATED,
            EventType.RECOVERY_COMPLETED,
            EventType.RECOVERY_RESUMED,
        ],
        audit_event_ids=("same", "same"),
    )

    assert complete is False
    assert "expected exactly one candidate terminal event" in issues
    assert "duplicate audit_event_ids" in issues


def test_deterministic_selection_trace_is_stable_across_replays():
    cases = (
        CandidateReplayCase(
            case_id="case-a",
            candidates=(
                _candidate(
                    "candidate-original",
                    status="repair_required",
                    lineage="original",
                    operator="original",
                ),
                _candidate(
                    "candidate-sibling-1",
                    status="accepted",
                    lineage="sibling",
                    operator="sibling_generation",
                ),
            ),
            original_plan=[{"description": "original"}],
            sibling_plan=[{"description": "sibling"}],
        ),
        CandidateReplayCase(
            case_id="case-b",
            candidates=(
                _candidate(
                    "candidate-original",
                    status="warning",
                    lineage="original",
                    operator="original",
                ),
                _candidate(
                    "candidate-sibling-1",
                    status="warning",
                    lineage="sibling",
                    operator="sibling_generation",
                ),
            ),
            original_plan=[{"description": "original-b"}],
            sibling_plan=[{"description": "sibling-b"}],
        ),
    )

    first_trace = deterministic_selection_trace(cases)
    second_trace = deterministic_selection_trace(cases)

    assert first_trace == second_trace
    assert first_trace[0][1] == "candidate-sibling-1"
    assert first_trace[1][1] == "candidate-original"


def test_feature_flag_replay_comparison_verifies_rollback():
    flag_off = (
        CandidateRecoveryRecord(
            case_id="case-a",
            feature_flag_enabled=False,
            machine_profile="standard",
            triggered=False,
            outcome="skipped",
        ),
    )
    flag_on = (
        CandidateRecoveryRecord(
            case_id="case-a",
            feature_flag_enabled=True,
            machine_profile="standard",
            triggered=True,
            outcome="selected",
            candidate_count=2,
            selected_candidate_id="candidate-sibling-1",
            selected_lineage="sibling",
            final_validator_status="accepted",
        ),
    )

    complete, issues = compare_feature_flag_replays(flag_off, flag_on)

    assert complete is True
    assert issues == []


def test_replay_corpus_fixture_supports_certification_metrics():
    fixture_path = (
        Path(__file__).parent / "fixtures" / "candidate_recovery_replay_corpus.json"
    )
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))

    flag_off: list[CandidateRecoveryRecord] = []
    flag_on: list[CandidateRecoveryRecord] = []
    for case in payload["cases"]:
        off = case["feature_flag_off"]
        on = case["feature_flag_on"]
        flag_off.append(
            CandidateRecoveryRecord(
                case_id=case["case_id"],
                feature_flag_enabled=False,
                machine_profile="standard",
                triggered=off["triggered"],
                outcome=off["outcome"],
                baseline_validator_status=off["baseline_validator_status"],
                final_validator_status=off["final_validator_status"],
            )
        )
        flag_on.append(
            CandidateRecoveryRecord(
                case_id=case["case_id"],
                feature_flag_enabled=True,
                machine_profile="standard",
                triggered=on["triggered"],
                outcome=on["outcome"],
                candidate_count=on["candidate_count"],
                selected_candidate_id=on["selected_candidate_id"],
                selected_lineage=on["selected_lineage"],
                validator_statuses=on["validator_statuses"],
                duration_ms=on["duration_ms"],
                token_estimate=on["token_estimate"],
                baseline_validator_status=on["baseline_validator_status"],
                final_validator_status=on["final_validator_status"],
            )
        )

    rollback_ok, rollback_issues = compare_feature_flag_replays(flag_off, flag_on)
    summary = aggregate_candidate_recovery(flag_on)

    assert rollback_ok is True
    assert rollback_issues == []
    assert summary.trigger_count == 3
    assert summary.success_count == 2
    assert summary.exhausted_count == 1
    assert summary.rescue_count == 1
    assert summary.selected_sibling == 1
