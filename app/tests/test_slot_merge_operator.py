"""Phase 17H: Slot Merge operator tests."""

from __future__ import annotations

from app.services.planning.slot_merge_operator import (
    SlotMergeInput,
    SlotMergeOperator,
)


def test_slot_merge_operator_produces_exactly_one_merged_plan():
    result = SlotMergeOperator().merge(
        SlotMergeInput(
            parent_a_plan=[
                {"step_number": 1, "description": "keep original"},
                {"step_number": 2, "description": "bad original"},
            ],
            parent_b_plan=[
                {"step_number": 1, "description": "repair changed"},
                {"step_number": 2, "description": "fixed repair"},
            ],
            parent_a_reasons=("Step 2: missing verification",),
            parent_b_reasons=(),
        )
    )

    assert result.operator == "slot_merge"
    assert result.merged_candidate_id == "candidate-slot-merge-1"
    assert result.parent_candidate_ids == ("candidate-original", "candidate-repair")
    assert result.merged_plan == [
        {"step_number": 1, "description": "keep original"},
        {"step_number": 2, "description": "fixed repair"},
    ]


def test_slot_merge_operator_is_not_recursive():
    result = SlotMergeOperator().merge(
        SlotMergeInput(
            parent_a_plan=[{"step_number": 1, "description": "a"}],
            parent_b_plan=[{"step_number": 1, "description": "b"}],
            parent_a_reasons=(),
            parent_b_reasons=("Step 1: still bad",),
        )
    )

    assert len(result.merged_plan) == 1
    assert result.merged_candidate_id == "candidate-slot-merge-1"
