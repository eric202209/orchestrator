"""Phase 17G-V: Candidate Recovery validation analytics.

Offline evaluation only. No runtime behavior changes.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Mapping, Optional, Sequence

from app.services.orchestration.events.event_types import EventType
from app.services.planning.candidate_recovery import stable_plan_hash
from app.services.planning.candidate_selection_policy import select_candidate
from app.services.planning.plan_candidate import PlanCandidate


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
class CandidateReplayCase:
    """One offline replay input for deterministic selection verification."""

    case_id: str
    candidates: tuple[PlanCandidate, ...]
    original_plan: object
    sibling_plan: object
    duration_ms: int = 0
    token_estimate: int = 0

    def __post_init__(self) -> None:
        if not self.case_id:
            raise ValueError("case_id is required")
        object.__setattr__(self, "candidates", tuple(self.candidates))


@dataclass(frozen=True)
class CandidateRecoveryRecord:
    """Captures one Candidate Recovery replay/runtime observation."""

    case_id: str
    feature_flag_enabled: bool
    machine_profile: str
    triggered: bool
    outcome: str
    candidate_count: int = 0
    selected_candidate_id: Optional[str] = None
    selected_lineage: Optional[str] = None
    validator_statuses: Mapping[str, str] = field(default_factory=dict)
    duration_ms: int = 0
    token_estimate: int = 0
    baseline_validator_status: str = "rejected"
    final_validator_status: str = "rejected"
    audit_event_types: tuple[str, ...] = field(default_factory=tuple)
    audit_event_ids: tuple[str, ...] = field(default_factory=tuple)
    final_plan_hash: Optional[str] = None

    def __post_init__(self) -> None:
        if not self.case_id:
            raise ValueError("case_id is required")
        candidate_count = int(self.candidate_count)
        if candidate_count < 0:
            raise ValueError("candidate_count must be non-negative")
        object.__setattr__(self, "candidate_count", candidate_count)
        object.__setattr__(
            self,
            "validator_statuses",
            dict(self.validator_statuses or {}),
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
    def skipped(self) -> bool:
        return self.outcome == "skipped"

    @property
    def success(self) -> bool:
        return self.outcome == "selected"

    @property
    def exhausted(self) -> bool:
        return self.outcome == "exhausted"

    @property
    def planning_rescued(self) -> bool:
        return (
            self.baseline_validator_status not in _SUCCESS_STATUSES
            and self.final_validator_status in _SUCCESS_STATUSES
        )


@dataclass(frozen=True)
class CandidateRecoverySummary:
    total: int
    trigger_count: int
    skipped_count: int
    success_count: int
    exhausted_count: int
    selected_original: int
    selected_sibling: int
    rescue_count: int
    rescue_rate: float
    sibling_selection_rate: float
    validator_status_distribution: Mapping[str, int]
    average_candidate_count: float
    average_latency_ms: float
    average_token_estimate: float
    by_machine_profile: Mapping[str, int]


def aggregate_candidate_recovery(
    records: Sequence[CandidateRecoveryRecord],
) -> CandidateRecoverySummary:
    """Aggregate Candidate Recovery observations for offline validation."""

    total = len(records)
    triggered = [record for record in records if record.triggered]
    successful = [record for record in records if record.success]
    rescue_count = sum(1 for record in records if record.planning_rescued)
    selected_sibling = sum(
        1 for record in records if record.selected_lineage == "sibling"
    )

    status_distribution: dict[str, int] = {}
    by_profile: dict[str, int] = {}
    for record in records:
        by_profile[record.machine_profile] = (
            by_profile.get(record.machine_profile, 0) + 1
        )
        for status in record.validator_statuses.values():
            status_distribution[status] = status_distribution.get(status, 0) + 1

    return CandidateRecoverySummary(
        total=total,
        trigger_count=len(triggered),
        skipped_count=sum(1 for record in records if record.skipped),
        success_count=len(successful),
        exhausted_count=sum(1 for record in records if record.exhausted),
        selected_original=sum(
            1 for record in records if record.selected_lineage == "original"
        ),
        selected_sibling=selected_sibling,
        rescue_count=rescue_count,
        rescue_rate=round(rescue_count / len(triggered), 3) if triggered else 0.0,
        sibling_selection_rate=(
            round(selected_sibling / len(successful), 3) if successful else 0.0
        ),
        validator_status_distribution=status_distribution,
        average_candidate_count=(
            statistics.mean(record.candidate_count for record in triggered)
            if triggered
            else 0.0
        ),
        average_latency_ms=(
            statistics.mean(record.duration_ms for record in triggered)
            if triggered
            else 0.0
        ),
        average_token_estimate=(
            statistics.mean(record.token_estimate for record in triggered)
            if triggered
            else 0.0
        ),
        by_machine_profile=by_profile,
    )


def verify_candidate_audit_sequence(
    emitted_event_types: Sequence[str],
    *,
    audit_event_ids: Sequence[str] = (),
) -> tuple[bool, list[str]]:
    """Verify the Phase 17G Candidate Recovery audit sequence."""

    event_types = tuple(str(event_type) for event_type in emitted_event_types)
    issues: list[str] = []

    if audit_event_ids and len(set(audit_event_ids)) != len(tuple(audit_event_ids)):
        issues.append("duplicate audit_event_ids")

    routed_count = event_types.count(EventType.RECOVERY_DECISION_ROUTED)
    if routed_count != 1:
        issues.append(
            f"expected exactly 1 recovery_decision_routed, got {routed_count}"
        )

    started_count = event_types.count(EventType.RECOVERY_STARTED)
    if started_count != 1:
        issues.append(f"expected exactly 1 recovery_started, got {started_count}")

    completed_count = event_types.count(EventType.RECOVERY_COMPLETED)
    failed_count = event_types.count(EventType.RECOVERY_FAILED)
    resumed_count = event_types.count(EventType.RECOVERY_RESUMED)
    if completed_count + failed_count != 1:
        issues.append("expected exactly one recovery terminal event")
    if completed_count == 1 and resumed_count != 1:
        issues.append("successful recovery must emit exactly 1 recovery_resumed")
    if failed_count == 1 and resumed_count:
        issues.append("failed recovery must not emit recovery_resumed")

    created_count = event_types.count(EventType.PLAN_CANDIDATE_CREATED)
    validated_count = event_types.count(EventType.PLAN_CANDIDATE_VALIDATED)
    if created_count != validated_count:
        issues.append("candidate created/validated counts differ")
    if created_count < 2:
        issues.append("expected at least original plus sibling candidates")

    selected_count = event_types.count(EventType.PLAN_CANDIDATE_SELECTED)
    exhausted_count = event_types.count(EventType.PLAN_CANDIDATE_EXHAUSTED)
    if selected_count + exhausted_count != 1:
        issues.append("expected exactly one candidate terminal event")

    if selected_count == 1 and event_types.count(EventType.PLAN_CANDIDATE_REJECTED) < 1:
        issues.append("selected recovery must reject non-selected candidates")
    if (
        exhausted_count == 1
        and event_types.count(EventType.PLAN_CANDIDATE_REJECTED) < 2
    ):
        issues.append("exhausted recovery must reject all candidates")

    _check_order(event_types, issues)
    return len(issues) == 0, issues


def deterministic_selection_trace(
    replay_cases: Sequence[CandidateReplayCase],
) -> tuple[tuple[str, str, str], ...]:
    """Return a stable selection trace for repeated offline replay comparison."""

    trace: list[tuple[str, str, str]] = []
    for case in replay_cases:
        selected = select_candidate(case.candidates)
        if selected is None:
            trace.append((case.case_id, "", ""))
            continue
        plan = (
            case.original_plan
            if selected.source_lineage == "original"
            else case.sibling_plan
        )
        trace.append((case.case_id, selected.candidate_id, stable_plan_hash(plan)))
    return tuple(trace)


def compare_feature_flag_replays(
    flag_off: Sequence[CandidateRecoveryRecord],
    flag_on: Sequence[CandidateRecoveryRecord],
) -> tuple[bool, list[str]]:
    """Verify rollback comparison for matching OFF and ON replay corpora."""

    issues: list[str] = []
    off_by_case = {record.case_id: record for record in flag_off}
    on_by_case = {record.case_id: record for record in flag_on}
    if set(off_by_case) != set(on_by_case):
        issues.append("feature flag replay case sets differ")

    for case_id, off_record in off_by_case.items():
        if off_record.feature_flag_enabled:
            issues.append(f"{case_id}: OFF replay record has feature flag enabled")
        if off_record.triggered:
            issues.append(
                f"{case_id}: OFF replay should not trigger candidate recovery"
            )
        if not off_record.skipped:
            issues.append(f"{case_id}: OFF replay should be skipped")

    for case_id, on_record in on_by_case.items():
        if not on_record.feature_flag_enabled:
            issues.append(f"{case_id}: ON replay record has feature flag disabled")

    return len(issues) == 0, issues


def _check_order(event_types: tuple[str, ...], issues: list[str]) -> None:
    positions = {event_type: index for index, event_type in enumerate(event_types)}

    def _before(left: str, right: str) -> None:
        if (
            left in positions
            and right in positions
            and positions[left] > positions[right]
        ):
            issues.append(f"{left} must precede {right}")

    _before(EventType.RECOVERY_DECISION_ROUTED, EventType.RECOVERY_STARTED)
    for event_type in _CANDIDATE_EVENT_TYPES:
        _before(EventType.RECOVERY_STARTED, event_type)
        _before(event_type, EventType.RECOVERY_COMPLETED)
        _before(event_type, EventType.RECOVERY_FAILED)
    _before(EventType.RECOVERY_COMPLETED, EventType.RECOVERY_RESUMED)
