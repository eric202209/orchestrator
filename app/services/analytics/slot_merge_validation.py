"""Phase 17H-V: Slot Merge validation analytics.

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
_MACHINE_B_PROFILE = "medium"


@dataclass(frozen=True)
class SlotMergeRecord:
    """Captures one offline Slot Merge replay observation."""

    case_id: str
    slot_merge_enabled: bool
    machine_profile: str
    triggered: bool
    outcome: str
    merged_candidate_count: int = 0
    selected_candidate_id: Optional[str] = None
    selected_lineage: Optional[str] = None
    parent_candidate_ids: tuple[str, ...] = field(default_factory=tuple)
    merged_candidate_id: Optional[str] = None
    validator_statuses: Mapping[str, str] = field(default_factory=dict)
    duration_ms: int = 0
    token_estimate: int = 0
    baseline_validator_status: str = "rejected"
    final_validator_status: str = "rejected"
    merged_plan_hash: Optional[str] = None
    selected_plan_hash: Optional[str] = None
    audit_event_types: tuple[str, ...] = field(default_factory=tuple)
    audit_event_ids: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.case_id:
            raise ValueError("case_id is required")
        merged_candidate_count = int(self.merged_candidate_count)
        if merged_candidate_count < 0:
            raise ValueError("merged_candidate_count must be non-negative")
        object.__setattr__(self, "merged_candidate_count", merged_candidate_count)
        object.__setattr__(
            self,
            "parent_candidate_ids",
            tuple(str(value) for value in self.parent_candidate_ids),
        )
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
    def selected(self) -> bool:
        return self.outcome == "selected"

    @property
    def exhausted(self) -> bool:
        return self.outcome == "exhausted"

    @property
    def merged_selected(self) -> bool:
        return (
            self.selected
            and self.selected_candidate_id is not None
            and self.selected_candidate_id == self.merged_candidate_id
        )

    @property
    def rescued(self) -> bool:
        return (
            self.baseline_validator_status not in _SUCCESS_STATUSES
            and self.final_validator_status in _SUCCESS_STATUSES
        )


@dataclass(frozen=True)
class SlotMergeSummary:
    total: int
    trigger_count: int
    skipped_count: int
    merged_candidate_count: int
    merged_selected_count: int
    exhausted_count: int
    rescue_count: int
    rescue_rate: float
    merged_selection_rate: float
    validator_status_distribution: Mapping[str, int]
    average_latency_ms: float
    average_token_estimate: float
    by_machine_profile: Mapping[str, int]


@dataclass(frozen=True)
class SlotMergeReplayCase:
    case_id: str
    original_plan: object
    merged_plan: object
    candidates: tuple[PlanCandidate, ...]
    audit_event_types: tuple[str, ...] = field(default_factory=tuple)

    def __post_init__(self) -> None:
        if not self.case_id:
            raise ValueError("case_id is required")
        object.__setattr__(self, "candidates", tuple(self.candidates))
        object.__setattr__(
            self,
            "audit_event_types",
            tuple(str(event_type) for event_type in self.audit_event_types),
        )


def aggregate_slot_merge(records: Sequence[SlotMergeRecord]) -> SlotMergeSummary:
    """Aggregate Slot Merge replay/runtime observations."""

    total = len(records)
    triggered = [record for record in records if record.triggered]
    selected = [record for record in records if record.selected]
    merged_selected = [record for record in records if record.merged_selected]

    status_distribution: dict[str, int] = {}
    by_profile: dict[str, int] = {}
    for record in records:
        by_profile[record.machine_profile] = (
            by_profile.get(record.machine_profile, 0) + 1
        )
        for status in record.validator_statuses.values():
            status_distribution[status] = status_distribution.get(status, 0) + 1

    rescue_count = sum(1 for record in records if record.rescued)
    return SlotMergeSummary(
        total=total,
        trigger_count=len(triggered),
        skipped_count=sum(1 for record in records if record.skipped),
        merged_candidate_count=sum(
            record.merged_candidate_count for record in triggered
        ),
        merged_selected_count=len(merged_selected),
        exhausted_count=sum(1 for record in records if record.exhausted),
        rescue_count=rescue_count,
        rescue_rate=round(rescue_count / len(triggered), 3) if triggered else 0.0,
        merged_selection_rate=(
            round(len(merged_selected) / len(selected), 3) if selected else 0.0
        ),
        validator_status_distribution=status_distribution,
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


def verify_slot_merge_audit_sequence(
    emitted_event_types: Sequence[str],
    *,
    audit_event_ids: Sequence[str] = (),
) -> tuple[bool, list[str]]:
    """Verify expected Slot Merge audit lifecycle for replay certification."""

    event_types = tuple(str(event_type) for event_type in emitted_event_types)
    issues: list[str] = []
    if audit_event_ids and len(set(audit_event_ids)) != len(tuple(audit_event_ids)):
        issues.append("duplicate audit_event_ids")

    _expect_count(event_types, EventType.RECOVERY_DECISION_ROUTED, 1, issues)
    _expect_count(event_types, EventType.RECOVERY_STARTED, 1, issues)
    _expect_count(event_types, EventType.PLAN_SLOT_MERGED, 1, issues)

    selected_count = event_types.count(EventType.PLAN_CANDIDATE_SELECTED)
    exhausted_count = event_types.count(EventType.PLAN_CANDIDATE_EXHAUSTED)
    if selected_count + exhausted_count != 1:
        issues.append("expected exactly one slot merge candidate terminal event")

    completed_count = event_types.count(EventType.RECOVERY_COMPLETED)
    failed_count = event_types.count(EventType.RECOVERY_FAILED)
    resumed_count = event_types.count(EventType.RECOVERY_RESUMED)
    if completed_count + failed_count != 1:
        issues.append("expected exactly one recovery terminal event")
    if completed_count == 1 and resumed_count != 1:
        issues.append("successful slot merge must emit recovery_resumed")
    if failed_count == 1 and resumed_count:
        issues.append("failed slot merge must not emit recovery_resumed")

    _check_order(event_types, issues)
    return len(issues) == 0, issues


def verify_slot_merge_lineage(record: SlotMergeRecord) -> tuple[bool, list[str]]:
    """Verify merged candidate lineage metadata is complete and singular."""

    issues: list[str] = []
    if record.triggered and record.machine_profile != _MACHINE_B_PROFILE:
        issues.append("slot merge triggered outside Machine B medium profile")
    if record.triggered and record.merged_candidate_count != 1:
        issues.append("expected exactly one merged candidate")
    if record.triggered and record.parent_candidate_ids != (
        "candidate-original",
        "candidate-repair",
    ):
        issues.append("unexpected parent_candidate_ids")
    if record.triggered and record.merged_candidate_id != "candidate-slot-merge-1":
        issues.append("unexpected merged_candidate_id")
    return len(issues) == 0, issues


def deterministic_slot_merge_trace(
    replay_cases: Sequence[SlotMergeReplayCase],
) -> tuple[tuple[str, str, str, tuple[str, ...]], ...]:
    """Return stable merged artifact, selection, and audit traces."""

    trace: list[tuple[str, str, str, tuple[str, ...]]] = []
    for case in replay_cases:
        selected = select_candidate(case.candidates)
        selected_id = selected.candidate_id if selected else ""
        trace.append(
            (
                case.case_id,
                selected_id,
                stable_plan_hash(case.merged_plan),
                tuple(case.audit_event_types),
            )
        )
    return tuple(trace)


def compare_slot_merge_flag_replays(
    slot_merge_off: Sequence[SlotMergeRecord],
    slot_merge_on: Sequence[SlotMergeRecord],
) -> tuple[bool, list[str]]:
    """Verify OFF replay is skipped while ON replay uses matching cases."""

    issues: list[str] = []
    off_by_case = {record.case_id: record for record in slot_merge_off}
    on_by_case = {record.case_id: record for record in slot_merge_on}
    if set(off_by_case) != set(on_by_case):
        issues.append("slot merge replay case sets differ")

    for case_id, off_record in off_by_case.items():
        if off_record.slot_merge_enabled:
            issues.append(f"{case_id}: OFF replay has slot merge enabled")
        if off_record.triggered:
            issues.append(f"{case_id}: OFF replay should not trigger slot merge")
        if not off_record.skipped:
            issues.append(f"{case_id}: OFF replay should be skipped")

    for case_id, on_record in on_by_case.items():
        if not on_record.slot_merge_enabled:
            issues.append(f"{case_id}: ON replay has slot merge disabled")
        if on_record.machine_profile != _MACHINE_B_PROFILE and on_record.triggered:
            issues.append(f"{case_id}: ON replay triggered outside Machine B")

    return len(issues) == 0, issues


def _expect_count(
    event_types: tuple[str, ...], event_type: str, expected: int, issues: list[str]
) -> None:
    actual = event_types.count(event_type)
    if actual != expected:
        issues.append(f"expected {expected} {event_type}, got {actual}")


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
    _before(EventType.RECOVERY_STARTED, EventType.PLAN_SLOT_MERGED)
    _before(EventType.PLAN_SLOT_MERGED, EventType.PLAN_CANDIDATE_SELECTED)
    _before(EventType.PLAN_SLOT_MERGED, EventType.PLAN_CANDIDATE_EXHAUSTED)
    _before(EventType.PLAN_CANDIDATE_SELECTED, EventType.RECOVERY_COMPLETED)
    _before(EventType.PLAN_CANDIDATE_EXHAUSTED, EventType.RECOVERY_FAILED)
    _before(EventType.RECOVERY_COMPLETED, EventType.RECOVERY_RESUMED)
