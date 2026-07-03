"""Phase 18A: controlled Candidate Recovery rollout analytics.

Telemetry-only helpers for Machine A rollout evidence. These functions derive
records from existing registry outcomes and audit events; they do not change
runtime behavior, policy, planning, validation, or feature flag defaults.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Mapping, Optional, Sequence

from app.services.analytics.candidate_recovery_validation import (
    verify_candidate_audit_sequence,
)
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.recovery.recovery_outcome import RecoveryOutcome


_SUCCESS_STATUSES = frozenset({"accepted", "warning"})
_CANDIDATE_EVENT_TYPES = frozenset(
    {
        EventType.PLAN_CANDIDATE_CREATED,
        EventType.PLAN_CANDIDATE_VALIDATED,
        EventType.PLAN_CANDIDATE_SELECTED,
        EventType.PLAN_CANDIDATE_REJECTED,
        EventType.PLAN_CANDIDATE_EXHAUSTED,
    }
)


@dataclass(frozen=True)
class CandidateRolloutTelemetryRecord:
    """One controlled rollout telemetry observation."""

    case_id: str
    feature_flag_enabled: bool
    slot_merge_enabled: bool
    runtime_profile: str
    triggered: bool
    failure_signature: str
    validator_result: str
    original_candidate_status: str
    recovery_candidate_status: str
    selected_candidate_id: Optional[str]
    selected_lineage: Optional[str]
    recovery_succeeded: bool
    recovery_exhausted: bool
    recovery_latency_ms: int
    validator_rejection_reasons: tuple[str, ...] = field(default_factory=tuple)
    candidate_count: int = 0
    duplicate_signature_suppressed: bool = False
    audit_event_types: tuple[str, ...] = field(default_factory=tuple)
    audit_event_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.case_id:
            raise ValueError("case_id is required")
        candidate_count = int(self.candidate_count)
        if candidate_count < 0:
            raise ValueError("candidate_count must be non-negative")
        latency = int(self.recovery_latency_ms)
        if latency < 0:
            raise ValueError("recovery_latency_ms must be non-negative")
        object.__setattr__(self, "candidate_count", candidate_count)
        object.__setattr__(self, "recovery_latency_ms", latency)
        object.__setattr__(
            self,
            "validator_rejection_reasons",
            tuple(str(reason) for reason in self.validator_rejection_reasons),
        )
        object.__setattr__(
            self,
            "audit_event_types",
            tuple(str(event_type) for event_type in self.audit_event_types),
        )
        object.__setattr__(
            self,
            "audit_event_ids",
            tuple(str(event_id) for event_id in self.audit_event_ids),
        )

    @property
    def rescued(self) -> bool:
        return (
            self.triggered
            and self.recovery_succeeded
            and self.original_candidate_status not in _SUCCESS_STATUSES
            and self.validator_result in _SUCCESS_STATUSES
        )


@dataclass(frozen=True)
class CandidateRolloutSummary:
    total: int
    trigger_count: int
    rescue_count: int
    exhaustion_count: int
    duplicate_signature_suppression_count: int
    rescue_rate: float
    exhaustion_rate: float
    validator_rejection_distribution: Mapping[str, int]
    average_recovery_latency_ms: float
    average_candidate_count: float
    by_runtime_profile: Mapping[str, int]


def telemetry_from_recovery_outcome(
    *,
    case_id: str,
    outcome: RecoveryOutcome,
    events: Sequence[Mapping[str, Any]],
    feature_flag_enabled: bool,
    slot_merge_enabled: bool = False,
) -> CandidateRolloutTelemetryRecord:
    """Build one telemetry record from an existing runtime outcome."""

    event_types = tuple(str(event.get("event_type") or "") for event in events)
    event_ids = tuple(str(event.get("event_id") or "") for event in events)
    candidate_details = _candidate_details(events)
    strategy_result = dict(outcome.strategy_result or {})
    candidate_outcome = dict(strategy_result.get("candidate_outcome") or {})
    selected_candidate = dict(candidate_outcome.get("selected_candidate") or {})
    selected_lineage = str(selected_candidate.get("source_lineage") or "") or None
    selected_candidate_id = str(selected_candidate.get("candidate_id") or "") or None

    original = candidate_details.get("candidate-original", {})
    sibling = candidate_details.get("candidate-sibling-1", {})
    reason = str(strategy_result.get("reason") or "")
    triggered = EventType.RECOVERY_STARTED in event_types
    exhausted = candidate_outcome.get("outcome") == "exhausted"

    validator_result = _validator_result(
        selected_candidate=selected_candidate,
        original=original,
        sibling=sibling,
        exhausted=exhausted,
    )
    return CandidateRolloutTelemetryRecord(
        case_id=case_id,
        feature_flag_enabled=feature_flag_enabled,
        slot_merge_enabled=slot_merge_enabled,
        runtime_profile=str(outcome.recovery_context.runtime_profile or ""),
        triggered=triggered,
        failure_signature=_failure_signature(outcome, events),
        validator_result=validator_result,
        original_candidate_status=str(original.get("validator_status") or ""),
        recovery_candidate_status=str(sibling.get("validator_status") or ""),
        selected_candidate_id=selected_candidate_id,
        selected_lineage=selected_lineage,
        recovery_succeeded=bool(outcome.succeeded),
        recovery_exhausted=bool(exhausted or reason == "candidate_exhausted"),
        recovery_latency_ms=outcome.duration_ms,
        validator_rejection_reasons=_validator_rejection_reasons(
            original=original,
            sibling=sibling,
        ),
        candidate_count=int(candidate_outcome.get("candidate_count") or 0),
        duplicate_signature_suppressed=reason == "signature_already_attempted",
        audit_event_types=event_types,
        audit_event_ids=event_ids,
    )


def aggregate_candidate_rollout(
    records: Sequence[CandidateRolloutTelemetryRecord],
) -> CandidateRolloutSummary:
    """Aggregate controlled rollout telemetry."""

    triggered = [record for record in records if record.triggered]
    rescue_count = sum(1 for record in records if record.rescued)
    exhaustion_count = sum(1 for record in records if record.recovery_exhausted)

    rejection_distribution: dict[str, int] = {}
    by_profile: dict[str, int] = {}
    for record in records:
        by_profile[record.runtime_profile] = (
            by_profile.get(record.runtime_profile, 0) + 1
        )
        for reason in record.validator_rejection_reasons:
            rejection_distribution[reason] = rejection_distribution.get(reason, 0) + 1

    return CandidateRolloutSummary(
        total=len(records),
        trigger_count=len(triggered),
        rescue_count=rescue_count,
        exhaustion_count=exhaustion_count,
        duplicate_signature_suppression_count=sum(
            1 for record in records if record.duplicate_signature_suppressed
        ),
        rescue_rate=round(rescue_count / len(triggered), 3) if triggered else 0.0,
        exhaustion_rate=(
            round(exhaustion_count / len(triggered), 3) if triggered else 0.0
        ),
        validator_rejection_distribution=rejection_distribution,
        average_recovery_latency_ms=(
            statistics.mean(record.recovery_latency_ms for record in triggered)
            if triggered
            else 0.0
        ),
        average_candidate_count=(
            statistics.mean(record.candidate_count for record in triggered)
            if triggered
            else 0.0
        ),
        by_runtime_profile=by_profile,
    )


def verify_rollout_telemetry_complete(
    record: CandidateRolloutTelemetryRecord,
) -> tuple[bool, list[str]]:
    """Verify the Phase 18A telemetry fields required for rollout evidence."""

    issues: list[str] = []
    if not record.runtime_profile:
        issues.append("runtime_profile is required")
    if record.slot_merge_enabled:
        issues.append("slot merge must remain disabled for Phase 18A")
    if record.triggered:
        if record.runtime_profile != "standard":
            issues.append("Phase 18A rollout must trigger only on Machine A")
        if not record.feature_flag_enabled:
            issues.append("triggered record must have Candidate Recovery flag on")
        if not record.failure_signature:
            issues.append("failure_signature is required")
        if not record.validator_result:
            issues.append("validator_result is required")
        if not record.original_candidate_status:
            issues.append("original_candidate_status is required")
        if not record.recovery_candidate_status:
            issues.append("recovery_candidate_status is required")
        if not (record.recovery_succeeded or record.recovery_exhausted):
            issues.append("triggered record must be succeeded or exhausted")
        if record.candidate_count < 2:
            issues.append("triggered record must include original plus sibling")
        if EventType.RECOVERY_DECISION_ROUTED not in record.audit_event_types:
            issues.append("missing recovery decision audit event")
    return len(issues) == 0, issues


def verify_rollout_audit_complete(
    record: CandidateRolloutTelemetryRecord,
) -> tuple[bool, list[str]]:
    """Verify audit completeness for triggered rollout records."""

    if not record.triggered:
        if EventType.RECOVERY_DECISION_ROUTED not in record.audit_event_types:
            return False, ["skipped record missing recovery decision audit event"]
        return True, []
    return verify_candidate_audit_sequence(
        record.audit_event_types,
        audit_event_ids=record.audit_event_ids,
    )


def compare_rollout_flag_replays(
    flag_off: Sequence[CandidateRolloutTelemetryRecord],
    flag_on: Sequence[CandidateRolloutTelemetryRecord],
) -> tuple[bool, list[str]]:
    """Verify rollback requires only the Candidate Recovery feature flag."""

    issues: list[str] = []
    off_by_case = {record.case_id: record for record in flag_off}
    on_by_case = {record.case_id: record for record in flag_on}
    if set(off_by_case) != set(on_by_case):
        issues.append("rollout replay case sets differ")

    for case_id, off_record in off_by_case.items():
        if off_record.feature_flag_enabled:
            issues.append(f"{case_id}: OFF replay has feature flag enabled")
        if off_record.triggered:
            issues.append(f"{case_id}: OFF replay triggered Candidate Recovery")
        candidate_events = [
            event_type
            for event_type in off_record.audit_event_types
            if event_type in _CANDIDATE_EVENT_TYPES
        ]
        if candidate_events:
            issues.append(f"{case_id}: OFF replay emitted candidate events")

    for case_id, on_record in on_by_case.items():
        if not on_record.feature_flag_enabled:
            issues.append(f"{case_id}: ON replay has feature flag disabled")
        if on_record.runtime_profile != "standard":
            issues.append(f"{case_id}: ON replay is outside Machine A")

    return len(issues) == 0, issues


def deterministic_rollout_trace(
    records: Sequence[CandidateRolloutTelemetryRecord],
) -> tuple[tuple[str, str, str, str], ...]:
    """Return a stable rollout trace for replay comparison."""

    return tuple(
        (
            record.case_id,
            record.failure_signature,
            record.selected_candidate_id or "",
            "succeeded" if record.recovery_succeeded else record.validator_result,
        )
        for record in records
    )


def _candidate_details(
    events: Sequence[Mapping[str, Any]],
) -> dict[str, Mapping[str, Any]]:
    candidates: dict[str, Mapping[str, Any]] = {}
    for event in events:
        event_type = str(event.get("event_type") or "")
        if event_type not in {
            EventType.PLAN_CANDIDATE_CREATED,
            EventType.PLAN_CANDIDATE_VALIDATED,
            EventType.PLAN_CANDIDATE_SELECTED,
            EventType.PLAN_CANDIDATE_REJECTED,
        }:
            continue
        details = dict(event.get("details") or {})
        candidate_id = str(details.get("candidate_id") or "")
        if candidate_id:
            candidates[candidate_id] = details
    return candidates


def _failure_signature(
    outcome: RecoveryOutcome,
    events: Sequence[Mapping[str, Any]],
) -> str:
    metadata = dict(outcome.recovery_context.recovery_metadata or {})
    if metadata.get("planning_failure_signature"):
        return str(metadata["planning_failure_signature"])
    for event in events:
        details = dict(event.get("details") or {})
        signature = details.get("planning_failure_signature") or details.get(
            "signature_hash"
        )
        if signature:
            return str(signature)
    return ""


def _validator_result(
    *,
    selected_candidate: Mapping[str, Any],
    original: Mapping[str, Any],
    sibling: Mapping[str, Any],
    exhausted: bool,
) -> str:
    if selected_candidate.get("validator_status"):
        return str(selected_candidate["validator_status"])
    if exhausted:
        return "exhausted"
    return str(
        sibling.get("validator_status") or original.get("validator_status") or ""
    )


def _validator_rejection_reasons(
    *,
    original: Mapping[str, Any],
    sibling: Mapping[str, Any],
) -> tuple[str, ...]:
    reasons: list[str] = []
    for candidate in (original, sibling):
        status = str(candidate.get("validator_status") or "")
        if status in _SUCCESS_STATUSES:
            continue
        for reason in candidate.get("validator_reasons") or ():
            reasons.append(str(reason))
    return tuple(reasons)
