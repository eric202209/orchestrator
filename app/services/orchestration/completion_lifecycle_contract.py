"""Declarative completion and lifecycle contract boundary.

Phase 12P: Normalized representation that task outcome, terminal events,
session continuation, and scorer results can all be compared through.

This is NOT a new runtime.  It is a shared schema so that lifecycle-scorer
disagreements can be represented, classified, and reported rather than hidden.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from enum import StrEnum
from typing import Any


# ---------------------------------------------------------------------------
# Enumerations
# ---------------------------------------------------------------------------


class TaskOutcome(StrEnum):
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"
    REPAIR_EXHAUSTED = "repair_exhausted"
    VALIDATION_REJECTED = "validation_rejected"
    VERIFICATION_FAILED = "verification_failed"
    BLOCKED = "blocked"
    UNKNOWN = "unknown"


class SessionContinuation(StrEnum):
    AUTO_ADVANCED = "auto_advanced"
    SESSION_COMPLETED = "session_completed"
    SESSION_PAUSED = "session_paused"
    UNKNOWN = "unknown"


class LifecycleVerificationStatus(StrEnum):
    PASSED = "passed"
    FAILED = "failed"
    SKIPPED = "skipped"


class CompletionValidationStatus(StrEnum):
    ACCEPTED = "accepted"
    WARNING = "warning"
    REPAIR_REQUIRED = "repair_required"
    REJECTED = "rejected"
    UNKNOWN = "unknown"


class LifecycleScorerAgreement(StrEnum):
    AGREE = "agree"
    DISAGREE = "disagree"
    SCORER_NOT_EVALUATED = "scorer_not_evaluated"
    INDETERMINATE = "indeterminate"


class CompletionMismatchType(StrEnum):
    SCORER_LIFECYCLE_TERMINAL_EVENT_MISMATCH = (
        "SCORER_LIFECYCLE_TERMINAL_EVENT_MISMATCH"
    )
    SCORER_ARTIFACT_LIFECYCLE_MISMATCH = "SCORER_ARTIFACT_LIFECYCLE_MISMATCH"
    VALIDATION_VERIFICATION_MISMATCH = "VALIDATION_VERIFICATION_MISMATCH"
    SESSION_CONTINUATION_MISMATCH = "SESSION_CONTINUATION_MISMATCH"


# ---------------------------------------------------------------------------
# Contract dataclass
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class CompletionLifecycleContract:
    """Normalized representation of one task's completion lifecycle state.

    All completion surfaces (validation, verification, lifecycle event,
    scorer) can be compared through this shape.
    """

    task_outcome: str
    session_continuation: str
    required_terminal_event: str
    terminal_event_emitted: bool
    completion_validation_status: str
    verification_command: str | None
    verification_status: str
    scorer_passed: bool | None
    lifecycle_agrees_with_scorer: str
    repair_attempts: int
    source: str
    normalized: bool = True
    divergence_reason: str | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "task_outcome": self.task_outcome,
            "session_continuation": self.session_continuation,
            "required_terminal_event": self.required_terminal_event,
            "terminal_event_emitted": self.terminal_event_emitted,
            "completion_validation_status": self.completion_validation_status,
            "verification_command": self.verification_command,
            "verification_status": self.verification_status,
            "scorer_passed": self.scorer_passed,
            "lifecycle_agrees_with_scorer": self.lifecycle_agrees_with_scorer,
            "repair_attempts": self.repair_attempts,
            "source": self.source,
            "normalized": self.normalized,
            "divergence_reason": self.divergence_reason,
        }


# ---------------------------------------------------------------------------
# Lifecycle-scorer agreement helper
# ---------------------------------------------------------------------------


def _lifecycle_scorer_agreement(
    *,
    task_outcome: str,
    terminal_event_emitted: bool,
    scorer_passed: bool | None,
) -> str:
    if scorer_passed is None:
        return LifecycleScorerAgreement.SCORER_NOT_EVALUATED
    lifecycle_succeeded = (
        task_outcome == TaskOutcome.TASK_COMPLETED and terminal_event_emitted
    )
    if lifecycle_succeeded == scorer_passed:
        return LifecycleScorerAgreement.AGREE
    return LifecycleScorerAgreement.DISAGREE


# ---------------------------------------------------------------------------
# Surface adapters
# ---------------------------------------------------------------------------


def build_lifecycle_completion_contract(
    *,
    task_outcome: str,
    terminal_event_emitted: bool,
    completion_validation_status: str = CompletionValidationStatus.UNKNOWN,
    verification_command: str | None = None,
    verification_status: str = LifecycleVerificationStatus.SKIPPED,
    session_continuation: str = SessionContinuation.UNKNOWN,
    repair_attempts: int = 0,
    scorer_passed: bool | None = None,
    source: str = "lifecycle",
    divergence_reason: str | None = None,
) -> CompletionLifecycleContract:
    """Build a normalized contract from the lifecycle perspective.

    Use this adapter when describing what the task/session lifecycle recorded:
    task outcome, terminal event, continuation, and validator/verifier results.
    """
    required_terminal_event = (
        "task_completed"
        if task_outcome == TaskOutcome.TASK_COMPLETED
        else "task_failed"
    )
    agreement = _lifecycle_scorer_agreement(
        task_outcome=task_outcome,
        terminal_event_emitted=terminal_event_emitted,
        scorer_passed=scorer_passed,
    )
    return CompletionLifecycleContract(
        task_outcome=task_outcome,
        session_continuation=session_continuation,
        required_terminal_event=required_terminal_event,
        terminal_event_emitted=terminal_event_emitted,
        completion_validation_status=completion_validation_status,
        verification_command=verification_command,
        verification_status=verification_status,
        scorer_passed=scorer_passed,
        lifecycle_agrees_with_scorer=agreement,
        repair_attempts=repair_attempts,
        source=source,
        normalized=True,
        divergence_reason=divergence_reason,
    )


def build_scorer_completion_contract(
    *,
    scorer_passed: bool,
    task_completed_event_present: bool,
    required_files_present: bool = True,
    source: str = "scorer_verification",
    divergence_reason: str | None = None,
) -> CompletionLifecycleContract:
    """Build a normalized contract from the scorer perspective.

    Use this adapter when describing what the eval scorer recorded about
    verifier success, required-file presence, and terminal event presence.
    The scorer does not know about session continuation or repair attempts.
    """
    if scorer_passed and required_files_present:
        task_outcome = TaskOutcome.TASK_COMPLETED
    else:
        task_outcome = TaskOutcome.TASK_FAILED

    agreement = _lifecycle_scorer_agreement(
        task_outcome=task_outcome,
        terminal_event_emitted=task_completed_event_present,
        scorer_passed=scorer_passed,
    )
    return CompletionLifecycleContract(
        task_outcome=task_outcome,
        session_continuation=SessionContinuation.UNKNOWN,
        required_terminal_event="task_completed",
        terminal_event_emitted=task_completed_event_present,
        completion_validation_status=CompletionValidationStatus.UNKNOWN,
        verification_command=None,
        verification_status=(
            LifecycleVerificationStatus.PASSED
            if scorer_passed
            else LifecycleVerificationStatus.FAILED
        ),
        scorer_passed=scorer_passed,
        lifecycle_agrees_with_scorer=agreement,
        repair_attempts=0,
        source=source,
        normalized=True,
        divergence_reason=divergence_reason,
    )


# ---------------------------------------------------------------------------
# Contract comparison
# ---------------------------------------------------------------------------


def compare_completion_lifecycle_contracts(
    contracts: list[CompletionLifecycleContract],
) -> list[dict[str, Any]]:
    """Compare lifecycle contracts and return classified mismatch records.

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

        def _record(
            mtype: CompletionMismatchType, ref_val: Any, other_val: Any
        ) -> None:
            mismatches.append(
                {
                    "type": str(mtype),
                    "reference_source": reference.source,
                    "other_source": other.source,
                    "reference_value": ref_val,
                    "other_value": other_val,
                }
            )

        # Scorer-lifecycle terminal event disagreement
        if reference.terminal_event_emitted != other.terminal_event_emitted:
            _record(
                CompletionMismatchType.SCORER_LIFECYCLE_TERMINAL_EVENT_MISMATCH,
                reference.terminal_event_emitted,
                other.terminal_event_emitted,
            )

        # Task outcome disagrees between surfaces
        if reference.task_outcome != other.task_outcome:
            _record(
                CompletionMismatchType.SCORER_LIFECYCLE_TERMINAL_EVENT_MISMATCH,
                reference.task_outcome,
                other.task_outcome,
            )

        # Verification status disagrees
        if (
            reference.verification_status != LifecycleVerificationStatus.SKIPPED
            and other.verification_status != LifecycleVerificationStatus.SKIPPED
            and reference.verification_status != other.verification_status
        ):
            _record(
                CompletionMismatchType.VALIDATION_VERIFICATION_MISMATCH,
                reference.verification_status,
                other.verification_status,
            )

        # Session continuation only meaningful when both sides know it
        if (
            reference.session_continuation != SessionContinuation.UNKNOWN
            and other.session_continuation != SessionContinuation.UNKNOWN
            and reference.session_continuation != other.session_continuation
        ):
            _record(
                CompletionMismatchType.SESSION_CONTINUATION_MISMATCH,
                reference.session_continuation,
                other.session_continuation,
            )

    return mismatches


def detect_scorer_lifecycle_disagreement(
    *,
    lifecycle: CompletionLifecycleContract,
    scorer: CompletionLifecycleContract,
) -> dict[str, Any]:
    """Detect and classify scorer vs lifecycle terminal-event disagreement.

    Returns a structured summary of whether scorer and lifecycle agree on
    task completion, including the specific disagreement type if present.
    """
    scorer_succeeded = scorer.scorer_passed is True
    lifecycle_succeeded = (
        lifecycle.task_outcome == TaskOutcome.TASK_COMPLETED
        and lifecycle.terminal_event_emitted
    )
    agrees = scorer_succeeded == lifecycle_succeeded

    disagreement_type: str | None = None
    if not agrees:
        if scorer_succeeded and not lifecycle_succeeded:
            if not lifecycle.terminal_event_emitted:
                disagreement_type = "scorer_passed_task_completed_missing"
            else:
                disagreement_type = "scorer_passed_lifecycle_failed"
        else:
            disagreement_type = "lifecycle_completed_scorer_failed"

    return {
        "agrees": agrees,
        "disagreement_type": disagreement_type,
        "scorer_passed": scorer.scorer_passed,
        "lifecycle_task_outcome": lifecycle.task_outcome,
        "terminal_event_emitted": lifecycle.terminal_event_emitted,
        "lifecycle_agrees_with_scorer": lifecycle.lifecycle_agrees_with_scorer,
    }


def count_completion_lifecycle_mismatches(
    contracts: list[CompletionLifecycleContract],
) -> dict[str, Any]:
    """Return structured mismatch count for metric and report fields."""
    mismatches = compare_completion_lifecycle_contracts(contracts)
    by_type: dict[str, int] = {}
    for m in mismatches:
        mtype = str(m.get("type") or "UNKNOWN")
        by_type[mtype] = by_type.get(mtype, 0) + 1

    diverged = [c.source for c in contracts if c.divergence_reason is not None]

    return {
        "total_mismatch_count": len(mismatches),
        "mismatch_types": by_type,
        "sources_compared": [c.source for c in contracts],
        "intentionally_diverged_sources": diverged,
        "mismatches": mismatches,
    }
