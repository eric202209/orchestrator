"""Phase 12P: Completion And Lifecycle Contract.

Adapter tests proving that task completion outcome, terminal events, session
continuation, and scorer results can all be represented through the shared
CompletionLifecycleContract, and that scorer-lifecycle disagreement is
classifiable.

No production behavior changes are introduced here.
"""

from __future__ import annotations

from app.services.orchestration.completion_lifecycle_contract import (
    CompletionLifecycleContract,
    CompletionMismatchType,
    CompletionValidationStatus,
    LifecycleScorerAgreement,
    LifecycleVerificationStatus,
    SessionContinuation,
    TaskOutcome,
    build_lifecycle_completion_contract,
    build_scorer_completion_contract,
    compare_completion_lifecycle_contracts,
    count_completion_lifecycle_mismatches,
    detect_scorer_lifecycle_disagreement,
)


# ---------------------------------------------------------------------------
# Contract shape tests — one per adapter
# ---------------------------------------------------------------------------


def test_lifecycle_completion_contract_normalizes_correctly():
    contract = build_lifecycle_completion_contract(
        task_outcome=TaskOutcome.TASK_COMPLETED,
        terminal_event_emitted=True,
        completion_validation_status=CompletionValidationStatus.ACCEPTED,
        verification_command="pytest",
        verification_status=LifecycleVerificationStatus.PASSED,
        session_continuation=SessionContinuation.SESSION_COMPLETED,
        repair_attempts=0,
        scorer_passed=True,
        source="lifecycle",
    )

    assert contract.task_outcome == TaskOutcome.TASK_COMPLETED
    assert contract.terminal_event_emitted is True
    assert contract.required_terminal_event == "task_completed"
    assert contract.session_continuation == SessionContinuation.SESSION_COMPLETED
    assert contract.completion_validation_status == CompletionValidationStatus.ACCEPTED
    assert contract.verification_command == "pytest"
    assert contract.verification_status == LifecycleVerificationStatus.PASSED
    assert contract.scorer_passed is True
    assert contract.lifecycle_agrees_with_scorer == LifecycleScorerAgreement.AGREE
    assert contract.repair_attempts == 0
    assert contract.normalized is True
    assert contract.divergence_reason is None


def test_scorer_completion_contract_normalizes_correctly():
    contract = build_scorer_completion_contract(
        scorer_passed=True,
        task_completed_event_present=True,
        required_files_present=True,
        source="scorer_verification",
    )

    assert contract.task_outcome == TaskOutcome.TASK_COMPLETED
    assert contract.scorer_passed is True
    assert contract.terminal_event_emitted is True
    assert contract.session_continuation == SessionContinuation.UNKNOWN
    assert contract.lifecycle_agrees_with_scorer == LifecycleScorerAgreement.AGREE
    assert contract.normalized is True


def test_contract_to_dict_has_all_fields():
    contract = build_lifecycle_completion_contract(
        task_outcome=TaskOutcome.TASK_FAILED,
        terminal_event_emitted=False,
        source="lifecycle",
    )
    d = contract.to_dict()
    required_keys = {
        "task_outcome",
        "session_continuation",
        "required_terminal_event",
        "terminal_event_emitted",
        "completion_validation_status",
        "verification_command",
        "verification_status",
        "scorer_passed",
        "lifecycle_agrees_with_scorer",
        "repair_attempts",
        "source",
        "normalized",
        "divergence_reason",
    }
    assert required_keys.issubset(d.keys())


# ---------------------------------------------------------------------------
# Task outcome shapes
# ---------------------------------------------------------------------------


def test_task_completed_sets_correct_required_event():
    contract = build_lifecycle_completion_contract(
        task_outcome=TaskOutcome.TASK_COMPLETED,
        terminal_event_emitted=True,
        source="lifecycle",
    )
    assert contract.required_terminal_event == "task_completed"


def test_task_failed_sets_correct_required_event():
    contract = build_lifecycle_completion_contract(
        task_outcome=TaskOutcome.TASK_FAILED,
        terminal_event_emitted=False,
        source="lifecycle",
    )
    assert contract.required_terminal_event == "task_failed"


def test_repair_exhausted_is_representable():
    contract = build_lifecycle_completion_contract(
        task_outcome=TaskOutcome.REPAIR_EXHAUSTED,
        terminal_event_emitted=False,
        repair_attempts=3,
        source="lifecycle",
    )
    assert contract.task_outcome == TaskOutcome.REPAIR_EXHAUSTED
    assert contract.repair_attempts == 3
    assert contract.terminal_event_emitted is False


# ---------------------------------------------------------------------------
# Scorer-lifecycle agreement
# ---------------------------------------------------------------------------


def test_scorer_and_lifecycle_agree_when_both_complete():
    lifecycle = build_lifecycle_completion_contract(
        task_outcome=TaskOutcome.TASK_COMPLETED,
        terminal_event_emitted=True,
        scorer_passed=True,
        source="lifecycle",
    )
    assert lifecycle.lifecycle_agrees_with_scorer == LifecycleScorerAgreement.AGREE


def test_scorer_and_lifecycle_disagree_when_scorer_passes_event_missing():
    """Characterizes the Phase 12M TERMINAL_EVENT_MISMATCH / SCORER_ONLY_MISMATCH finding."""
    lifecycle = build_lifecycle_completion_contract(
        task_outcome=TaskOutcome.TASK_FAILED,
        terminal_event_emitted=False,
        scorer_passed=True,
        source="lifecycle",
    )
    assert lifecycle.lifecycle_agrees_with_scorer == LifecycleScorerAgreement.DISAGREE


def test_scorer_not_evaluated_when_none():
    lifecycle = build_lifecycle_completion_contract(
        task_outcome=TaskOutcome.TASK_COMPLETED,
        terminal_event_emitted=True,
        scorer_passed=None,
        source="lifecycle",
    )
    assert (
        lifecycle.lifecycle_agrees_with_scorer
        == LifecycleScorerAgreement.SCORER_NOT_EVALUATED
    )


# ---------------------------------------------------------------------------
# Scorer vs lifecycle disagreement detection
# ---------------------------------------------------------------------------


def test_detect_disagreement_scorer_passes_task_completed_missing():
    """Scorer verifier passed but task_completed was not emitted."""
    lifecycle = build_lifecycle_completion_contract(
        task_outcome=TaskOutcome.TASK_FAILED,
        terminal_event_emitted=False,
        source="lifecycle",
    )
    scorer = build_scorer_completion_contract(
        scorer_passed=True,
        task_completed_event_present=False,
        source="scorer",
    )

    result = detect_scorer_lifecycle_disagreement(lifecycle=lifecycle, scorer=scorer)

    assert result["agrees"] is False
    assert result["disagreement_type"] == "scorer_passed_task_completed_missing"
    assert result["scorer_passed"] is True
    assert result["terminal_event_emitted"] is False


def test_detect_disagreement_lifecycle_completed_scorer_failed():
    lifecycle = build_lifecycle_completion_contract(
        task_outcome=TaskOutcome.TASK_COMPLETED,
        terminal_event_emitted=True,
        source="lifecycle",
    )
    scorer = build_scorer_completion_contract(
        scorer_passed=False,
        task_completed_event_present=True,
        source="scorer",
    )

    result = detect_scorer_lifecycle_disagreement(lifecycle=lifecycle, scorer=scorer)

    assert result["agrees"] is False
    assert result["disagreement_type"] == "lifecycle_completed_scorer_failed"


def test_detect_agreement_both_succeed():
    lifecycle = build_lifecycle_completion_contract(
        task_outcome=TaskOutcome.TASK_COMPLETED,
        terminal_event_emitted=True,
        source="lifecycle",
    )
    scorer = build_scorer_completion_contract(
        scorer_passed=True,
        task_completed_event_present=True,
        source="scorer",
    )

    result = detect_scorer_lifecycle_disagreement(lifecycle=lifecycle, scorer=scorer)

    assert result["agrees"] is True
    assert result["disagreement_type"] is None


# ---------------------------------------------------------------------------
# Contract comparison — mismatch classification
# ---------------------------------------------------------------------------


def test_compare_detects_terminal_event_mismatch():
    lifecycle = build_lifecycle_completion_contract(
        task_outcome=TaskOutcome.TASK_COMPLETED,
        terminal_event_emitted=True,
        source="lifecycle",
    )
    scorer = build_scorer_completion_contract(
        scorer_passed=True,
        task_completed_event_present=False,
        source="scorer",
    )

    mismatches = compare_completion_lifecycle_contracts([lifecycle, scorer])
    mismatch_types = {m["type"] for m in mismatches}
    assert (
        CompletionMismatchType.SCORER_LIFECYCLE_TERMINAL_EVENT_MISMATCH
        in mismatch_types
    )


def test_compare_no_mismatch_when_both_agree():
    lifecycle = build_lifecycle_completion_contract(
        task_outcome=TaskOutcome.TASK_COMPLETED,
        terminal_event_emitted=True,
        source="lifecycle",
    )
    scorer = build_scorer_completion_contract(
        scorer_passed=True,
        task_completed_event_present=True,
        source="scorer",
    )

    mismatches = compare_completion_lifecycle_contracts([lifecycle, scorer])
    assert mismatches == []


def test_intentional_divergence_suppresses_mismatch():
    lifecycle = build_lifecycle_completion_contract(
        task_outcome=TaskOutcome.TASK_COMPLETED,
        terminal_event_emitted=True,
        source="lifecycle",
    )
    scorer = build_scorer_completion_contract(
        scorer_passed=False,
        task_completed_event_present=True,
        source="scorer",
        divergence_reason="INTENTIONAL_SCOPE_DIFFERENCE",
    )

    mismatches = compare_completion_lifecycle_contracts([lifecycle, scorer])
    assert mismatches == []


# ---------------------------------------------------------------------------
# Mismatch count metric
# ---------------------------------------------------------------------------


def test_count_mismatches_returns_structured_summary():
    lifecycle = build_lifecycle_completion_contract(
        task_outcome=TaskOutcome.TASK_FAILED,
        terminal_event_emitted=False,
        source="lifecycle",
    )
    scorer = build_scorer_completion_contract(
        scorer_passed=True,
        task_completed_event_present=False,
        source="scorer",
    )

    summary = count_completion_lifecycle_mismatches([lifecycle, scorer])

    assert "total_mismatch_count" in summary
    assert "mismatch_types" in summary
    assert "sources_compared" in summary
    assert "intentionally_diverged_sources" in summary
    assert summary["total_mismatch_count"] > 0
    assert summary["intentionally_diverged_sources"] == []


def test_count_records_intentionally_diverged():
    lifecycle = build_lifecycle_completion_contract(
        task_outcome=TaskOutcome.TASK_COMPLETED,
        terminal_event_emitted=True,
        source="lifecycle",
    )
    scorer = build_scorer_completion_contract(
        scorer_passed=False,
        task_completed_event_present=True,
        source="scorer",
        divergence_reason="INTENTIONAL_SCOPE_DIFFERENCE",
    )

    summary = count_completion_lifecycle_mismatches([lifecycle, scorer])
    assert "scorer" in summary["intentionally_diverged_sources"]
    assert summary["total_mismatch_count"] == 0


# ---------------------------------------------------------------------------
# Session continuation shapes
# ---------------------------------------------------------------------------


def test_auto_advanced_continuation_is_representable():
    contract = build_lifecycle_completion_contract(
        task_outcome=TaskOutcome.TASK_COMPLETED,
        terminal_event_emitted=True,
        session_continuation=SessionContinuation.AUTO_ADVANCED,
        source="lifecycle",
    )
    assert contract.session_continuation == SessionContinuation.AUTO_ADVANCED


def test_session_paused_continuation_is_representable():
    contract = build_lifecycle_completion_contract(
        task_outcome=TaskOutcome.TASK_FAILED,
        terminal_event_emitted=False,
        session_continuation=SessionContinuation.SESSION_PAUSED,
        repair_attempts=2,
        source="lifecycle",
    )
    assert contract.session_continuation == SessionContinuation.SESSION_PAUSED
    assert contract.repair_attempts == 2
