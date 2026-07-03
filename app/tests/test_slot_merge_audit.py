"""Phase 17H: Slot Merge audit tests."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.state.persistence import read_orchestration_events
from app.services.planning.candidate_recovery import (
    SlotMergeCandidateRecoveryRequest,
    execute_slot_merge_candidate_recovery,
)


def _verdict(status: str, reasons: tuple[str, ...] = ()):
    return SimpleNamespace(
        status=status,
        accepted=status in {"accepted", "warning"},
        warning=status == "warning",
        repairable=status == "repair_required",
        rejected=status == "rejected",
        reasons=list(reasons),
        details={},
        verdict={"status": status},
    )


def test_slot_merge_runtime_emits_plan_slot_merged_event(tmp_path):
    execute_slot_merge_candidate_recovery(
        SlotMergeCandidateRecoveryRequest(
            project_dir=tmp_path,
            session_id=31,
            task_id=32,
            parent_a_plan=[{"step_number": 1, "description": "a"}],
            parent_a_output_text="original-json",
            parent_a_verdict=_verdict("repair_required", ("Step 1: bad",)),
            parent_b_plan=[{"step_number": 1, "description": "b"}],
            parent_b_output_text="repair-json",
            parent_b_verdict=_verdict("rejected", ("Step 1: still bad",)),
            runtime_profile="medium",
            parent_event_id=None,
            validate_candidate=lambda _plan, _output_text: _verdict("accepted"),
        )
    )

    slot_merge_events = read_orchestration_events(
        tmp_path,
        session_id=31,
        task_id=32,
        event_type_filter=EventType.PLAN_SLOT_MERGED,
    )

    assert len(slot_merge_events) == 1
    details = slot_merge_events[0]["details"]
    assert details["parent_candidate_ids"] == [
        "candidate-original",
        "candidate-repair",
    ]
    assert details["merged_candidate_id"] == "candidate-slot-merge-1"
    assert details["operator"] == "slot_merge"
    assert details["validator_status"] == "accepted"
    assert details["runtime_profile"] == "medium"
    assert details["policy_version"] == "phase17h"
