"""Phase 17H: Slot Merge runtime tests."""

from __future__ import annotations

from types import SimpleNamespace

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


def test_slot_merge_runtime_validates_merged_candidate_once(tmp_path):
    calls = {"validate": 0}

    def validate_candidate(_plan, _output_text):
        calls["validate"] += 1
        return _verdict("accepted")

    result = execute_slot_merge_candidate_recovery(
        SlotMergeCandidateRecoveryRequest(
            project_dir=tmp_path,
            session_id=21,
            task_id=22,
            parent_a_plan=[
                {"step_number": 1, "description": "keep"},
                {"step_number": 2, "description": "bad"},
            ],
            parent_a_output_text="original-json",
            parent_a_verdict=_verdict("repair_required", ("Step 2: bad",)),
            parent_b_plan=[
                {"step_number": 1, "description": "changed"},
                {"step_number": 2, "description": "fixed"},
            ],
            parent_b_output_text="repair-json",
            parent_b_verdict=_verdict("rejected", ("Step 1: changed too much",)),
            runtime_profile="medium",
            parent_event_id=None,
            validate_candidate=validate_candidate,
        )
    )

    assert calls == {"validate": 1}
    assert result.selected is True
    assert result.selected_plan == [
        {"step_number": 1, "description": "keep"},
        {"step_number": 2, "description": "fixed"},
    ]
    assert result.outcome.candidate_count == 2
    assert result.outcome.operator_sequence == (
        "original",
        "repair_mutation",
        "slot_merge",
    )
    assert result.outcome.selected_candidate.candidate_id == "candidate-slot-merge-1"


def test_slot_merge_runtime_exhausts_when_merge_not_accepted(tmp_path):
    result = execute_slot_merge_candidate_recovery(
        SlotMergeCandidateRecoveryRequest(
            project_dir=tmp_path,
            session_id=23,
            task_id=24,
            parent_a_plan=[{"step_number": 1, "description": "a"}],
            parent_a_output_text="original-json",
            parent_a_verdict=_verdict("repair_required", ("Step 1: bad",)),
            parent_b_plan=[{"step_number": 1, "description": "b"}],
            parent_b_output_text="repair-json",
            parent_b_verdict=_verdict("rejected", ("Step 1: still bad",)),
            runtime_profile="medium",
            parent_event_id=None,
            validate_candidate=lambda _plan, _output_text: _verdict(
                "repair_required", ("Step 1: not fixed",)
            ),
        )
    )

    assert result.selected is False
    assert result.outcome.outcome == "exhausted"
