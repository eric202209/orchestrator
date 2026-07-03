"""Phase 17H-V: Slot Merge certification analytics tests."""

from __future__ import annotations

import json
from pathlib import Path

from app.services.analytics.slot_merge_validation import (
    SlotMergeRecord,
    SlotMergeReplayCase,
    aggregate_slot_merge,
    compare_slot_merge_flag_replays,
    deterministic_slot_merge_trace,
    verify_slot_merge_audit_sequence,
    verify_slot_merge_lineage,
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
        parent_candidate_ids=(
            ("candidate-original", "candidate-repair")
            if operator == "slot_merge"
            else ()
        ),
        operator=operator,
        source_lineage=lineage,
        artifact_hash=f"hash-{candidate_id}",
        validator_status=status,
        validator_reasons=(),
        planning_failure_signature="sig",
        runtime_profile="medium",
    )


def test_slot_merge_analytics_aggregate_certification_metrics():
    summary = aggregate_slot_merge(
        [
            SlotMergeRecord(
                case_id="rescued",
                slot_merge_enabled=True,
                machine_profile="medium",
                triggered=True,
                outcome="selected",
                merged_candidate_count=1,
                selected_candidate_id="candidate-slot-merge-1",
                selected_lineage="slot_merge",
                parent_candidate_ids=("candidate-original", "candidate-repair"),
                merged_candidate_id="candidate-slot-merge-1",
                validator_statuses={
                    "candidate-original": "repair_required",
                    "candidate-repair": "rejected",
                    "candidate-slot-merge-1": "accepted",
                },
                duration_ms=420,
                token_estimate=120,
                baseline_validator_status="repair_required",
                final_validator_status="accepted",
            ),
            SlotMergeRecord(
                case_id="exhausted",
                slot_merge_enabled=True,
                machine_profile="medium",
                triggered=True,
                outcome="exhausted",
                merged_candidate_count=1,
                parent_candidate_ids=("candidate-original", "candidate-repair"),
                merged_candidate_id="candidate-slot-merge-1",
                validator_statuses={
                    "candidate-original": "rejected",
                    "candidate-repair": "repair_required",
                    "candidate-slot-merge-1": "repair_required",
                },
                duration_ms=380,
                token_estimate=100,
                baseline_validator_status="rejected",
                final_validator_status="rejected",
            ),
            SlotMergeRecord(
                case_id="machine-a-skipped",
                slot_merge_enabled=True,
                machine_profile="standard",
                triggered=False,
                outcome="skipped",
            ),
        ]
    )

    assert summary.total == 3
    assert summary.trigger_count == 2
    assert summary.skipped_count == 1
    assert summary.merged_candidate_count == 2
    assert summary.merged_selected_count == 1
    assert summary.exhausted_count == 1
    assert summary.rescue_count == 1
    assert summary.rescue_rate == 0.5
    assert summary.merged_selection_rate == 1.0
    assert summary.average_latency_ms == 400
    assert summary.average_token_estimate == 110
    assert summary.validator_status_distribution == {
        "repair_required": 3,
        "rejected": 2,
        "accepted": 1,
    }


def test_slot_merge_audit_sequence_complete_for_success():
    complete, issues = verify_slot_merge_audit_sequence(
        [
            EventType.RECOVERY_DECISION_ROUTED,
            EventType.RECOVERY_STARTED,
            EventType.PLAN_SLOT_MERGED,
            EventType.PLAN_CANDIDATE_SELECTED,
            EventType.RECOVERY_COMPLETED,
            EventType.RECOVERY_RESUMED,
        ],
        audit_event_ids=("1", "2", "3"),
    )

    assert complete is True
    assert issues == []


def test_slot_merge_audit_sequence_reports_missing_merge():
    complete, issues = verify_slot_merge_audit_sequence(
        [
            EventType.RECOVERY_DECISION_ROUTED,
            EventType.RECOVERY_STARTED,
            EventType.PLAN_CANDIDATE_SELECTED,
            EventType.RECOVERY_COMPLETED,
            EventType.RECOVERY_RESUMED,
        ],
        audit_event_ids=("same", "same"),
    )

    assert complete is False
    assert "expected 1 plan_slot_merged, got 0" in issues
    assert "duplicate audit_event_ids" in issues


def test_slot_merge_lineage_requires_one_medium_merge():
    complete, issues = verify_slot_merge_lineage(
        SlotMergeRecord(
            case_id="lineage",
            slot_merge_enabled=True,
            machine_profile="medium",
            triggered=True,
            outcome="selected",
            merged_candidate_count=1,
            parent_candidate_ids=("candidate-original", "candidate-repair"),
            merged_candidate_id="candidate-slot-merge-1",
        )
    )

    assert complete is True
    assert issues == []


def test_deterministic_slot_merge_trace_is_stable():
    cases = (
        SlotMergeReplayCase(
            case_id="case-a",
            original_plan=[{"step_number": 1, "description": "original"}],
            merged_plan=[{"step_number": 1, "description": "merged"}],
            candidates=(
                _candidate(
                    "candidate-original",
                    status="repair_required",
                    lineage="original",
                    operator="original",
                ),
                _candidate(
                    "candidate-slot-merge-1",
                    status="accepted",
                    lineage="slot_merge",
                    operator="slot_merge",
                ),
            ),
            audit_event_types=(
                EventType.RECOVERY_DECISION_ROUTED,
                EventType.RECOVERY_STARTED,
                EventType.PLAN_SLOT_MERGED,
                EventType.PLAN_CANDIDATE_SELECTED,
                EventType.RECOVERY_COMPLETED,
                EventType.RECOVERY_RESUMED,
            ),
        ),
    )

    first_trace = deterministic_slot_merge_trace(cases)
    second_trace = deterministic_slot_merge_trace(cases)

    assert first_trace == second_trace
    assert first_trace[0][1] == "candidate-slot-merge-1"


def test_slot_merge_flag_replay_comparison_verifies_rollback():
    off = (
        SlotMergeRecord(
            case_id="case-a",
            slot_merge_enabled=False,
            machine_profile="medium",
            triggered=False,
            outcome="skipped",
        ),
    )
    on = (
        SlotMergeRecord(
            case_id="case-a",
            slot_merge_enabled=True,
            machine_profile="medium",
            triggered=True,
            outcome="selected",
            merged_candidate_count=1,
            parent_candidate_ids=("candidate-original", "candidate-repair"),
            merged_candidate_id="candidate-slot-merge-1",
        ),
    )

    complete, issues = compare_slot_merge_flag_replays(off, on)

    assert complete is True
    assert issues == []


def test_slot_merge_replay_corpus_supports_certification_metrics():
    fixture_path = Path(__file__).parent / "fixtures" / "slot_merge_replay_corpus.json"
    payload = json.loads(fixture_path.read_text(encoding="utf-8"))

    off_records: list[SlotMergeRecord] = []
    on_records: list[SlotMergeRecord] = []
    for case in payload["cases"]:
        off = case["slot_merge_off"]
        on = case["slot_merge_on"]
        off_records.append(
            SlotMergeRecord(
                case_id=case["case_id"],
                slot_merge_enabled=False,
                machine_profile=on["machine_profile"],
                triggered=off["triggered"],
                outcome=off["outcome"],
                baseline_validator_status=off["baseline_validator_status"],
                final_validator_status=off["final_validator_status"],
            )
        )
        on_records.append(
            SlotMergeRecord(
                case_id=case["case_id"],
                slot_merge_enabled=True,
                machine_profile=on["machine_profile"],
                triggered=on["triggered"],
                outcome=on["outcome"],
                merged_candidate_count=on["merged_candidate_count"],
                selected_candidate_id=on["selected_candidate_id"],
                selected_lineage=on["selected_lineage"],
                parent_candidate_ids=tuple(on["parent_candidate_ids"]),
                merged_candidate_id=on["merged_candidate_id"],
                validator_statuses=on["validator_statuses"],
                duration_ms=on["duration_ms"],
                token_estimate=on["token_estimate"],
                baseline_validator_status=on["baseline_validator_status"],
                final_validator_status=on["final_validator_status"],
                merged_plan_hash=on["merged_plan_hash"],
            )
        )

    rollback_ok, rollback_issues = compare_slot_merge_flag_replays(
        off_records, on_records
    )
    summary = aggregate_slot_merge(on_records)
    lineage_results = [
        verify_slot_merge_lineage(record) for record in on_records if record.triggered
    ]

    assert rollback_ok is True
    assert rollback_issues == []
    assert all(complete for complete, _issues in lineage_results)
    assert summary.trigger_count == 2
    assert summary.skipped_count == 2
    assert summary.merged_candidate_count == 2
    assert summary.merged_selected_count == 1
    assert summary.exhausted_count == 1
    assert summary.rescue_count == 1
    assert summary.average_latency_ms == 400
    assert summary.average_token_estimate == 110
