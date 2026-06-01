"""Phase 12Q: Repair Surface Contract.

Adapter tests proving that planning-repair, debug-repair, and
completion-repair surfaces can all be represented through the shared
RepairSurfaceContract, and that failure class eligibility, proposal shape,
outcome, and rejection reason differences are classifiable.

No production behavior changes are introduced here.
"""

from __future__ import annotations

from app.services.orchestration.repair_surface_contract import (
    RepairMismatchType,
    RepairOutcome,
    RepairSurface,
    RepairSurfaceContract,
    build_completion_repair_contract,
    build_debug_repair_contract,
    build_planning_repair_contract,
    compare_repair_surface_contracts,
    count_repair_surface_mismatches,
    failure_class_eligibility_matrix,
)
from app.services.orchestration.diagnostics.debug_feedback import (
    ELIGIBLE_DEBUG_FAILURE_CLASSES,
)


# ---------------------------------------------------------------------------
# Contract shape tests — one per surface
# ---------------------------------------------------------------------------


def test_planning_repair_contract_normalizes_correctly():
    contract = build_planning_repair_contract(
        failure_class="removed_materialization",
        outcome=RepairOutcome.REJECTED,
        rejection_reason="removed_materialization",
        source="planning_repair_arbitration",
    )

    assert contract.surface == RepairSurface.PLANNING_REPAIR
    assert contract.failure_class == "removed_materialization"
    assert contract.eligible is True
    assert contract.outcome == RepairOutcome.REJECTED
    assert contract.rejection_reason == "removed_materialization"
    assert contract.post_repair_verification_required is True
    assert "steps" in contract.proposal_fields_required
    assert contract.normalized is True
    assert contract.divergence_reason is None


def test_debug_repair_contract_normalizes_correctly():
    contract = build_debug_repair_contract(
        failure_class="import_error",
        outcome=RepairOutcome.ACCEPTED,
        source="debug_feedback_envelope",
    )

    assert contract.surface == RepairSurface.DEBUG_REPAIR
    assert contract.failure_class == "import_error"
    assert contract.eligible is True
    assert contract.outcome == RepairOutcome.ACCEPTED
    assert contract.rejection_reason is None
    assert contract.post_repair_verification_required is True
    assert "commands" in contract.proposal_fields_required
    assert contract.normalized is True


def test_completion_repair_contract_normalizes_correctly():
    contract = build_completion_repair_contract(
        failure_class="missing_dependency",
        outcome=RepairOutcome.ACCEPTED,
        source="completion_repair",
    )

    assert contract.surface == RepairSurface.COMPLETION_REPAIR
    assert contract.failure_class == "missing_dependency"
    assert contract.eligible is True
    assert contract.outcome == RepairOutcome.ACCEPTED
    assert contract.post_repair_verification_required is True
    assert "commands" in contract.proposal_fields_required
    assert contract.normalized is True


def test_contract_to_dict_has_all_fields():
    contract = build_debug_repair_contract(failure_class="import_error")
    d = contract.to_dict()
    required_keys = {
        "surface",
        "failure_class",
        "eligible",
        "outcome",
        "rejection_reason",
        "post_repair_verification_required",
        "proposal_fields_required",
        "source",
        "normalized",
        "divergence_reason",
    }
    assert required_keys.issubset(d.keys())


# ---------------------------------------------------------------------------
# Eligibility per surface
# ---------------------------------------------------------------------------


def test_debug_eligible_classes_match_production_constant():
    """Debug repair eligibility in the contract matches ELIGIBLE_DEBUG_FAILURE_CLASSES."""
    for failure_class in ELIGIBLE_DEBUG_FAILURE_CLASSES:
        contract = build_debug_repair_contract(failure_class=failure_class)
        assert (
            contract.eligible is True
        ), f"{failure_class} should be eligible for debug repair"


def test_debug_ineligible_class_not_eligible():
    contract = build_debug_repair_contract(failure_class="unknown")
    assert contract.eligible is False


def test_planning_eligible_class_is_eligible():
    for cls in ("removed_materialization", "stale_replace", "invalid_output"):
        contract = build_planning_repair_contract(failure_class=cls)
        assert (
            contract.eligible is True
        ), f"{cls} should be eligible for planning repair"


def test_completion_eligible_class_is_eligible():
    for cls in (
        "missing_dependency",
        "module_not_found",
        "completion_validation_failed",
    ):
        contract = build_completion_repair_contract(failure_class=cls)
        assert (
            contract.eligible is True
        ), f"{cls} should be eligible for completion repair"


def test_failure_class_eligibility_matrix():
    matrix = failure_class_eligibility_matrix("import_error")
    assert matrix[RepairSurface.DEBUG_REPAIR] is True
    assert matrix[RepairSurface.PLANNING_REPAIR] is False
    assert matrix[RepairSurface.COMPLETION_REPAIR] is False


def test_module_not_found_eligible_on_debug_and_completion():
    matrix = failure_class_eligibility_matrix("module_not_found")
    assert matrix[RepairSurface.DEBUG_REPAIR] is True
    assert matrix[RepairSurface.COMPLETION_REPAIR] is True


def test_unknown_class_ineligible_on_all_surfaces():
    matrix = failure_class_eligibility_matrix("unknown")
    assert all(not v for v in matrix.values())


# ---------------------------------------------------------------------------
# Proposal shape mismatch
# ---------------------------------------------------------------------------


def test_proposal_shape_mismatch_between_planning_and_debug():
    planning = build_planning_repair_contract(failure_class="removed_materialization")
    debug = build_debug_repair_contract(failure_class="import_error")

    assert "steps" in planning.proposal_fields_required
    assert "commands" in debug.proposal_fields_required
    assert "steps" not in debug.proposal_fields_required

    mismatches = compare_repair_surface_contracts([planning, debug])
    mismatch_types = {m["type"] for m in mismatches}
    assert RepairMismatchType.PROPOSAL_SHAPE_MISMATCH in mismatch_types


def test_debug_and_completion_share_proposal_shape():
    debug = build_debug_repair_contract(failure_class="import_error")
    completion = build_completion_repair_contract(failure_class="module_not_found")

    assert set(debug.proposal_fields_required) == set(
        completion.proposal_fields_required
    )

    mismatches = compare_repair_surface_contracts([debug, completion])
    mismatch_types = {m["type"] for m in mismatches}
    assert RepairMismatchType.PROPOSAL_SHAPE_MISMATCH not in mismatch_types


# ---------------------------------------------------------------------------
# Eligibility mismatch
# ---------------------------------------------------------------------------


def test_eligibility_mismatch_when_class_eligible_on_debug_but_not_planning():
    debug = build_debug_repair_contract(failure_class="pytest_failure")
    planning = build_planning_repair_contract(failure_class="pytest_failure")

    assert debug.eligible is True
    assert planning.eligible is False

    mismatches = compare_repair_surface_contracts([debug, planning])
    mismatch_types = {m["type"] for m in mismatches}
    assert RepairMismatchType.ELIGIBILITY_MISMATCH in mismatch_types


# ---------------------------------------------------------------------------
# Rejection reason mismatch
# ---------------------------------------------------------------------------


def test_rejection_reason_mismatch_across_surfaces():
    debug = build_debug_repair_contract(
        failure_class="import_error",
        outcome=RepairOutcome.REJECTED,
        rejection_reason="inventory_guard",
    )
    completion = build_completion_repair_contract(
        failure_class="module_not_found",
        outcome=RepairOutcome.REJECTED,
        rejection_reason="repeat_failure_signature",
    )

    mismatches = compare_repair_surface_contracts([debug, completion])
    mismatch_types = {m["type"] for m in mismatches}
    assert RepairMismatchType.REJECTION_REASON_MISMATCH in mismatch_types


# ---------------------------------------------------------------------------
# Outcome shapes
# ---------------------------------------------------------------------------


def test_exhausted_outcome_is_representable():
    contract = build_debug_repair_contract(
        failure_class="import_error",
        outcome=RepairOutcome.EXHAUSTED,
    )
    assert contract.outcome == RepairOutcome.EXHAUSTED


def test_churn_stopped_outcome_is_representable():
    contract = build_completion_repair_contract(
        failure_class="completion_validation_failed",
        outcome=RepairOutcome.CHURN_STOPPED,
        rejection_reason="repair_churn_limit",
    )
    assert contract.outcome == RepairOutcome.CHURN_STOPPED
    assert contract.rejection_reason == "repair_churn_limit"


# ---------------------------------------------------------------------------
# All three surfaces representable through shared contract
# ---------------------------------------------------------------------------


def test_all_three_surfaces_representable():
    planning = build_planning_repair_contract(failure_class="removed_materialization")
    debug = build_debug_repair_contract(failure_class="import_error")
    completion = build_completion_repair_contract(failure_class="module_not_found")

    contracts = [planning, debug, completion]
    for c in contracts:
        assert isinstance(c, RepairSurfaceContract)
        assert c.normalized is True
        assert c.post_repair_verification_required is True

    assert set(c.surface for c in contracts) == {
        RepairSurface.PLANNING_REPAIR,
        RepairSurface.DEBUG_REPAIR,
        RepairSurface.COMPLETION_REPAIR,
    }


# ---------------------------------------------------------------------------
# Intentional divergence
# ---------------------------------------------------------------------------


def test_intentional_divergence_suppresses_mismatch():
    debug = build_debug_repair_contract(failure_class="import_error")
    planning = build_planning_repair_contract(
        failure_class="import_error",
        divergence_reason="INTENTIONAL_SCOPE_DIFFERENCE",
    )

    mismatches = compare_repair_surface_contracts([debug, planning])
    assert mismatches == []


# ---------------------------------------------------------------------------
# Mismatch count metric
# ---------------------------------------------------------------------------


def test_count_mismatches_returns_structured_summary():
    planning = build_planning_repair_contract(failure_class="removed_materialization")
    debug = build_debug_repair_contract(failure_class="import_error")

    summary = count_repair_surface_mismatches([planning, debug])

    assert "total_mismatch_count" in summary
    assert "mismatch_types" in summary
    assert "surfaces_compared" in summary
    assert "intentionally_diverged_surfaces" in summary
    assert summary["total_mismatch_count"] > 0


def test_count_records_intentionally_diverged_surface():
    debug = build_debug_repair_contract(failure_class="import_error")
    planning = build_planning_repair_contract(
        failure_class="import_error",
        divergence_reason="INTENTIONAL_SCOPE_DIFFERENCE",
    )

    summary = count_repair_surface_mismatches([debug, planning])
    assert RepairSurface.PLANNING_REPAIR in summary["intentionally_diverged_surfaces"]
    assert summary["total_mismatch_count"] == 0
