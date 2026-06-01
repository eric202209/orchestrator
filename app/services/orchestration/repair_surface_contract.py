"""Declarative repair surface contract boundary.

Phase 12Q: Normalized representation that planning-repair, debug-repair, and
completion-repair surfaces can all be compared through.

This is NOT a new runtime.  It is a shared schema so that repair-surface
differences in failure class eligibility, proposal shape, outcome, and
rejection reason can be represented and classified.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any

from app.services.orchestration.diagnostics.debug_feedback import (
    ELIGIBLE_DEBUG_FAILURE_CLASSES,
)


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class RepairSurface(StrEnum):
    PLANNING_REPAIR = "planning_repair"
    DEBUG_REPAIR = "debug_repair"
    COMPLETION_REPAIR = "completion_repair"


class RepairOutcome(StrEnum):
    ACCEPTED = "accepted"
    REJECTED = "rejected"
    EXHAUSTED = "exhausted"
    CHURN_STOPPED = "churn_stopped"
    UNKNOWN = "unknown"


class RepairMismatchType(StrEnum):
    ELIGIBILITY_MISMATCH = "ELIGIBILITY_MISMATCH"
    PROPOSAL_SHAPE_MISMATCH = "PROPOSAL_SHAPE_MISMATCH"
    REJECTION_REASON_MISMATCH = "REJECTION_REASON_MISMATCH"
    VERIFICATION_REQUIREMENT_MISMATCH = "VERIFICATION_REQUIREMENT_MISMATCH"


# ---------------------------------------------------------------------------
# Failure class eligibility per surface
# ---------------------------------------------------------------------------

# Planning repair: eligible classes are those where arbitration can classify
# a repaired plan as improved_or_preserved.  All non-unknown classes can be
# arbitrated; eligibility is determined by the arbitration outcome label.
_PLANNING_REPAIR_ELIGIBLE_CLASSES: frozenset[str] = frozenset(
    {
        "removed_materialization",
        "removed_verification",
        "stale_replace",
        "framework_drift",
        "test_rewrite",
        "workspace_rewrite",
        "package_root_drift",
        "source_api_regression",
        "invalid_output",
    }
)

# Debug repair: from ELIGIBLE_DEBUG_FAILURE_CLASSES in debug_feedback.py.
_DEBUG_REPAIR_ELIGIBLE_CLASSES: frozenset[str] = ELIGIBLE_DEBUG_FAILURE_CLASSES

# Completion repair: classes that _classify_completion_verification_failure
# can return as repair_required.
_COMPLETION_REPAIR_ELIGIBLE_CLASSES: frozenset[str] = frozenset(
    {
        "missing_dependency",
        "module_not_found",
        "completion_validation_failed",
    }
)

# Required proposal fields per surface
_PROPOSAL_FIELDS: dict[str, list[str]] = {
    RepairSurface.PLANNING_REPAIR: ["steps"],
    RepairSurface.DEBUG_REPAIR: ["commands"],
    RepairSurface.COMPLETION_REPAIR: ["commands"],
}


# ---------------------------------------------------------------------------
# Contract dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class RepairSurfaceContract:
    """Normalized representation of one repair surface's semantics.

    All repair surfaces (planning, debug, completion) can be represented and
    compared through this shape.
    """

    surface: str
    failure_class: str
    eligible: bool
    outcome: str
    rejection_reason: str | None
    post_repair_verification_required: bool
    proposal_fields_required: list[str]
    source: str
    normalized: bool = True
    divergence_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "surface": self.surface,
            "failure_class": self.failure_class,
            "eligible": self.eligible,
            "outcome": self.outcome,
            "rejection_reason": self.rejection_reason,
            "post_repair_verification_required": self.post_repair_verification_required,
            "proposal_fields_required": list(self.proposal_fields_required),
            "source": self.source,
            "normalized": self.normalized,
            "divergence_reason": self.divergence_reason,
        }


# ---------------------------------------------------------------------------
# Surface adapters
# ---------------------------------------------------------------------------


def build_planning_repair_contract(
    *,
    failure_class: str,
    outcome: str = RepairOutcome.UNKNOWN,
    rejection_reason: str | None = None,
    source: str = "planning_repair_arbitration",
    divergence_reason: str | None = None,
) -> RepairSurfaceContract:
    """Build a normalized contract for a planning repair attempt.

    Planning repair input is a rejected plan candidate. The arbitration
    classifies the repaired plan as improved_or_preserved, regressed, or
    neutral.  Post-repair verification is always required for planning repair.
    """
    raw_class = str(failure_class or "unknown").strip()
    eligible = raw_class in _PLANNING_REPAIR_ELIGIBLE_CLASSES
    return RepairSurfaceContract(
        surface=RepairSurface.PLANNING_REPAIR,
        failure_class=raw_class,
        eligible=eligible,
        outcome=outcome,
        rejection_reason=rejection_reason,
        post_repair_verification_required=True,
        proposal_fields_required=list(_PROPOSAL_FIELDS[RepairSurface.PLANNING_REPAIR]),
        source=source,
        normalized=True,
        divergence_reason=divergence_reason,
    )


def build_debug_repair_contract(
    *,
    failure_class: str,
    outcome: str = RepairOutcome.UNKNOWN,
    rejection_reason: str | None = None,
    source: str = "debug_feedback_envelope",
    divergence_reason: str | None = None,
) -> RepairSurfaceContract:
    """Build a normalized contract for a debug repair attempt.

    Debug repair input is a DebugFeedbackEnvelope with a failure_class.
    Eligibility is determined by ELIGIBLE_DEBUG_FAILURE_CLASSES.
    Post-repair verification is required (step verifier re-runs).
    """
    raw_class = str(failure_class or "unknown").strip()
    eligible = raw_class in _DEBUG_REPAIR_ELIGIBLE_CLASSES
    return RepairSurfaceContract(
        surface=RepairSurface.DEBUG_REPAIR,
        failure_class=raw_class,
        eligible=eligible,
        outcome=outcome,
        rejection_reason=rejection_reason,
        post_repair_verification_required=True,
        proposal_fields_required=list(_PROPOSAL_FIELDS[RepairSurface.DEBUG_REPAIR]),
        source=source,
        normalized=True,
        divergence_reason=divergence_reason,
    )


def build_completion_repair_contract(
    *,
    failure_class: str,
    outcome: str = RepairOutcome.UNKNOWN,
    rejection_reason: str | None = None,
    source: str = "completion_repair",
    divergence_reason: str | None = None,
) -> RepairSurfaceContract:
    """Build a normalized contract for a completion repair attempt.

    Completion repair input is a ValidationVerdict with repair_required status.
    Eligibility is determined by _COMPLETION_REPAIR_ELIGIBLE_CLASSES.
    Post-repair verification re-runs the completion verification command.
    """
    raw_class = str(failure_class or "unknown").strip()
    eligible = raw_class in _COMPLETION_REPAIR_ELIGIBLE_CLASSES
    return RepairSurfaceContract(
        surface=RepairSurface.COMPLETION_REPAIR,
        failure_class=raw_class,
        eligible=eligible,
        outcome=outcome,
        rejection_reason=rejection_reason,
        post_repair_verification_required=True,
        proposal_fields_required=list(
            _PROPOSAL_FIELDS[RepairSurface.COMPLETION_REPAIR]
        ),
        source=source,
        normalized=True,
        divergence_reason=divergence_reason,
    )


# ---------------------------------------------------------------------------
# Contract comparison
# ---------------------------------------------------------------------------


def compare_repair_surface_contracts(
    contracts: list[RepairSurfaceContract],
) -> list[dict[str, Any]]:
    """Compare repair surface contracts and return classified mismatches.

    Contracts with a non-None divergence_reason are excluded from comparison.
    """
    if len(contracts) < 2:
        return []

    normalized = [c for c in contracts if c.divergence_reason is None]
    if len(normalized) < 2:
        return []

    reference = normalized[0]
    mismatches: list[dict[str, Any]] = []

    for other in normalized[1:]:

        def _record(mtype: RepairMismatchType, ref_val: Any, other_val: Any) -> None:
            mismatches.append(
                {
                    "type": str(mtype),
                    "reference_surface": reference.surface,
                    "other_surface": other.surface,
                    "reference_value": ref_val,
                    "other_value": other_val,
                }
            )

        if reference.eligible != other.eligible:
            _record(
                RepairMismatchType.ELIGIBILITY_MISMATCH,
                reference.eligible,
                other.eligible,
            )

        if set(reference.proposal_fields_required) != set(
            other.proposal_fields_required
        ):
            _record(
                RepairMismatchType.PROPOSAL_SHAPE_MISMATCH,
                sorted(reference.proposal_fields_required),
                sorted(other.proposal_fields_required),
            )

        if reference.rejection_reason != other.rejection_reason:
            _record(
                RepairMismatchType.REJECTION_REASON_MISMATCH,
                reference.rejection_reason,
                other.rejection_reason,
            )

        if (
            reference.post_repair_verification_required
            != other.post_repair_verification_required
        ):
            _record(
                RepairMismatchType.VERIFICATION_REQUIREMENT_MISMATCH,
                reference.post_repair_verification_required,
                other.post_repair_verification_required,
            )

    return mismatches


def failure_class_eligibility_matrix(failure_class: str) -> dict[str, bool]:
    """Return eligibility for a given failure class across all three surfaces."""
    raw = str(failure_class or "unknown").strip()
    return {
        RepairSurface.PLANNING_REPAIR: raw in _PLANNING_REPAIR_ELIGIBLE_CLASSES,
        RepairSurface.DEBUG_REPAIR: raw in _DEBUG_REPAIR_ELIGIBLE_CLASSES,
        RepairSurface.COMPLETION_REPAIR: raw in _COMPLETION_REPAIR_ELIGIBLE_CLASSES,
    }


def count_repair_surface_mismatches(
    contracts: list[RepairSurfaceContract],
) -> dict[str, Any]:
    """Return structured mismatch count for metric and report fields."""
    mismatches = compare_repair_surface_contracts(contracts)
    by_type: dict[str, int] = {}
    for m in mismatches:
        mtype = str(m.get("type") or "UNKNOWN")
        by_type[mtype] = by_type.get(mtype, 0) + 1

    diverged = [c.surface for c in contracts if c.divergence_reason is not None]

    return {
        "total_mismatch_count": len(mismatches),
        "mismatch_types": by_type,
        "surfaces_compared": [c.surface for c in contracts],
        "intentionally_diverged_surfaces": diverged,
        "mismatches": mismatches,
    }
