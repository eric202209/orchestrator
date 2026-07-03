"""Deterministic candidate selection policy for Candidate Recovery."""

from __future__ import annotations

from typing import Iterable, Mapping, Optional

from app.services.planning.plan_candidate import PlanCandidate


STATUS_ORDER: Mapping[str, int] = {
    "accepted": 0,
    "warning": 1,
    "repair_required": 2,
    "rejected": 3,
}

OPERATOR_COST: Mapping[str, int] = {
    "original": 0,
    "slot_merge": 1,
    "repair_mutation": 2,
    "sibling_generation": 3,
    "crossover": 3,
}


def selection_key(candidate: PlanCandidate, *, policy_order: int) -> tuple:
    """Return the stable sort key for one candidate.

    Lower values win. This deliberately uses validator status and deterministic
    metadata only; no scoring, LLM ranking, or heuristic evaluation is involved.
    """

    return (
        STATUS_ORDER[candidate.validator_status],
        candidate.rejected_reason_count,
        candidate.repairable_reason_count,
        OPERATOR_COST.get(candidate.operator, 99),
        int(policy_order),
        candidate.candidate_id,
    )


def select_candidate(
    candidates: Iterable[PlanCandidate],
) -> Optional[PlanCandidate]:
    """Select the best candidate by deterministic policy order."""

    ordered = list(candidates)
    if not ordered:
        return None
    indexed = (
        (selection_key(candidate, policy_order=index), candidate)
        for index, candidate in enumerate(ordered)
    )
    return min(indexed, key=lambda item: item[0])[1]
