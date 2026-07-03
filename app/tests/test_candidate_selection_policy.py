"""Phase 17F: deterministic candidate selection policy tests."""

from __future__ import annotations

from app.services.planning.candidate_selection_policy import (
    select_candidate,
    selection_key,
)
from app.services.planning.plan_candidate import PlanCandidate


def _candidate(
    candidate_id: str,
    status: str,
    *,
    reasons: tuple[str, ...] = (),
    operator: str = "sibling_generation",
) -> PlanCandidate:
    return PlanCandidate(
        candidate_id=candidate_id,
        validator_status=status,
        validator_reasons=reasons,
        operator=operator,
    )


def test_selection_order_prefers_accepted_warning_repair_required_rejected():
    candidates = [
        _candidate("rejected", "rejected"),
        _candidate("repair", "repair_required"),
        _candidate("warning", "warning"),
        _candidate("accepted", "accepted"),
    ]

    assert select_candidate(candidates).candidate_id == "accepted"


def test_warning_beats_repair_required():
    selected = select_candidate(
        [
            _candidate("repair", "repair_required"),
            _candidate("warning", "warning"),
        ]
    )

    assert selected.candidate_id == "warning"


def test_rejected_tie_break_prefers_fewer_rejected_reasons():
    selected = select_candidate(
        [
            _candidate("many", "rejected", reasons=("a", "b")),
            _candidate("few", "rejected", reasons=("a",)),
        ]
    )

    assert selected.candidate_id == "few"


def test_repair_required_tie_break_prefers_fewer_repairable_reasons():
    selected = select_candidate(
        [
            _candidate("many", "repair_required", reasons=("a", "b")),
            _candidate("few", "repair_required", reasons=("a",)),
        ]
    )

    assert selected.candidate_id == "few"


def test_tie_break_prefers_lower_operator_cost():
    selected = select_candidate(
        [
            _candidate("sibling", "accepted", operator="sibling_generation"),
            _candidate("original", "accepted", operator="original"),
        ]
    )

    assert selected.candidate_id == "original"


def test_tie_break_prefers_earlier_policy_order_before_candidate_id():
    selected = select_candidate(
        [
            _candidate("z-candidate", "accepted", operator="original"),
            _candidate("a-candidate", "accepted", operator="original"),
        ]
    )

    assert selected.candidate_id == "z-candidate"


def test_lexicographic_candidate_id_is_final_tie_break():
    a = _candidate("a-candidate", "accepted", operator="original")
    z = _candidate("z-candidate", "accepted", operator="original")

    assert selection_key(z, policy_order=0) > selection_key(a, policy_order=0)


def test_empty_candidate_list_returns_none():
    assert select_candidate([]) is None
