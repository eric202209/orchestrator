"""Phase 18D: runtime evidence collection for validator telemetry.

Read-only evidence utilities that combine Phase 18B validator rule telemetry
with Phase 18A Candidate Recovery rollout records. These helpers consume
already-emitted audit events and recovery outcomes; they do not change
validator, planning, recovery, candidate selection, or feature flag behavior.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass, field
from typing import Any, Mapping, Sequence

from app.services.analytics.candidate_rollout_validation import (
    CandidateRolloutTelemetryRecord,
    telemetry_from_recovery_outcome,
)
from app.services.analytics.validator_rule_telemetry import (
    ValidatorRuleTelemetryRecord,
    telemetry_from_candidate_events,
)
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.recovery.recovery_outcome import RecoveryOutcome


@dataclass(frozen=True)
class RuntimeEvidenceCase:
    """One runtime replay/collection case."""

    case_id: str
    outcome: RecoveryOutcome
    events: tuple[Mapping[str, Any], ...]
    feature_flag_enabled: bool
    slot_merge_enabled: bool = False

    def __post_init__(self) -> None:
        if not self.case_id:
            raise ValueError("case_id is required")
        object.__setattr__(self, "events", tuple(self.events or ()))


@dataclass(frozen=True)
class RuleRuntimeEvidence:
    rule_id: str
    frequency: int
    validator_status_distribution: Mapping[str, int]
    recovery_trigger_count: int
    recovery_rescue_count: int
    selected_candidate_still_had_rule_count: int
    recovery_trigger_rate: float
    recovery_rescue_rate: float
    selected_candidate_still_had_rule_rate: float
    machine_profiles: Mapping[str, int]
    runtime_profiles: Mapping[str, int]
    failure_signatures: Mapping[str, int]
    average_latency_ms: float


@dataclass(frozen=True)
class ValidatorRuntimeEvidenceReport:
    total_cases: int
    total_rule_firings: int
    stable_rule_id_event_count: int
    rollout_records: tuple[CandidateRolloutTelemetryRecord, ...]
    rule_records: tuple[ValidatorRuleTelemetryRecord, ...]
    by_rule_id: Mapping[str, RuleRuntimeEvidence]
    high_frequency_rules: tuple[str, ...] = field(default_factory=tuple)
    rescue_correlated_rules: tuple[str, ...] = field(default_factory=tuple)
    noisy_or_redundant_rules: tuple[str, ...] = field(default_factory=tuple)


def collect_validator_runtime_evidence(
    cases: Sequence[RuntimeEvidenceCase],
) -> ValidatorRuntimeEvidenceReport:
    """Collect runtime validator evidence from existing case events/outcomes."""

    rollout_records: list[CandidateRolloutTelemetryRecord] = []
    rule_records: list[ValidatorRuleTelemetryRecord] = []
    latencies_by_rule: dict[str, list[int]] = {}
    stable_rule_id_event_count = 0

    for case in cases:
        rollout = telemetry_from_recovery_outcome(
            case_id=case.case_id,
            outcome=case.outcome,
            events=case.events,
            feature_flag_enabled=case.feature_flag_enabled,
            slot_merge_enabled=case.slot_merge_enabled,
        )
        rollout_records.append(rollout)
        records = telemetry_from_candidate_events(
            events=case.events,
            outcome=case.outcome,
        )
        rule_records.extend(records)
        for record in records:
            latencies_by_rule.setdefault(record.rule_id, []).append(
                rollout.recovery_latency_ms
            )
        stable_rule_id_event_count += _stable_rule_id_event_count(case.events)

    by_rule_id = _aggregate_rule_evidence(
        rule_records,
        latencies_by_rule=latencies_by_rule,
    )
    return ValidatorRuntimeEvidenceReport(
        total_cases=len(cases),
        total_rule_firings=len(rule_records),
        stable_rule_id_event_count=stable_rule_id_event_count,
        rollout_records=tuple(rollout_records),
        rule_records=tuple(rule_records),
        by_rule_id=by_rule_id,
        high_frequency_rules=_high_frequency_rules(by_rule_id),
        rescue_correlated_rules=tuple(
            rule_id
            for rule_id, evidence in sorted(by_rule_id.items())
            if evidence.recovery_rescue_count > 0
        ),
        noisy_or_redundant_rules=tuple(
            rule_id
            for rule_id, evidence in sorted(by_rule_id.items())
            if evidence.selected_candidate_still_had_rule_count > 0
        ),
    )


def render_validator_runtime_evidence_report(
    report: ValidatorRuntimeEvidenceReport,
) -> str:
    """Render a markdown runtime evidence report."""

    lines = [
        "# Phase 18D - Validator Runtime Evidence",
        "",
        f"Runtime cases: {report.total_cases}",
        f"Rule firings: {report.total_rule_firings}",
        f"Stable rule-ID audit events: {report.stable_rule_id_event_count}",
        "",
        "## Rule Evidence",
    ]
    for rule_id, evidence in sorted(
        report.by_rule_id.items(),
        key=lambda item: (-item[1].frequency, item[0]),
    ):
        lines.extend(
            [
                f"### `{rule_id}`",
                f"- frequency: {evidence.frequency}",
                f"- validator_status_distribution: {dict(evidence.validator_status_distribution)}",
                f"- recovery_trigger_rate: {evidence.recovery_trigger_rate:.3f}",
                f"- recovery_rescue_rate: {evidence.recovery_rescue_rate:.3f}",
                "- selected_candidate_still_had_rule_rate: "
                f"{evidence.selected_candidate_still_had_rule_rate:.3f}",
                f"- machine_profiles: {dict(evidence.machine_profiles)}",
                f"- runtime_profiles: {dict(evidence.runtime_profiles)}",
                f"- failure_signatures: {dict(evidence.failure_signatures)}",
                f"- average_latency_ms: {evidence.average_latency_ms:.1f}",
                "",
            ]
        )

    lines.extend(
        [
            "## Findings",
            "- high_frequency_rules: "
            + _format_rule_tuple(report.high_frequency_rules),
            "- rescue_correlated_rules: "
            + _format_rule_tuple(report.rescue_correlated_rules),
            "- noisy_or_redundant_rules: "
            + _format_rule_tuple(report.noisy_or_redundant_rules),
        ]
    )
    return "\n".join(lines) + "\n"


def _aggregate_rule_evidence(
    records: Sequence[ValidatorRuleTelemetryRecord],
    *,
    latencies_by_rule: Mapping[str, Sequence[int]],
) -> dict[str, RuleRuntimeEvidence]:
    grouped: dict[str, list[ValidatorRuleTelemetryRecord]] = {}
    for record in records:
        grouped.setdefault(record.rule_id, []).append(record)

    evidence_by_rule: dict[str, RuleRuntimeEvidence] = {}
    for rule_id, rule_records in grouped.items():
        statuses: dict[str, int] = {}
        machines: dict[str, int] = {}
        runtimes: dict[str, int] = {}
        signatures: dict[str, int] = {}
        trigger_count = 0
        rescue_count = 0
        selected_still_count = 0
        for record in rule_records:
            statuses[record.validator_status] = (
                statuses.get(record.validator_status, 0) + 1
            )
            machines[record.machine_profile] = (
                machines.get(record.machine_profile, 0) + 1
            )
            runtimes[record.runtime_profile] = (
                runtimes.get(record.runtime_profile, 0) + 1
            )
            signatures[record.failure_signature] = (
                signatures.get(record.failure_signature, 0) + 1
            )
            if record.candidate_recovery_triggered:
                trigger_count += 1
            if record.candidate_recovery_rescued:
                rescue_count += 1
            if record.selected_candidate_still_had_rule:
                selected_still_count += 1

        frequency = len(rule_records)
        latencies = list(latencies_by_rule.get(rule_id) or [])
        evidence_by_rule[rule_id] = RuleRuntimeEvidence(
            rule_id=rule_id,
            frequency=frequency,
            validator_status_distribution=statuses,
            recovery_trigger_count=trigger_count,
            recovery_rescue_count=rescue_count,
            selected_candidate_still_had_rule_count=selected_still_count,
            recovery_trigger_rate=(
                round(trigger_count / frequency, 3) if frequency else 0.0
            ),
            recovery_rescue_rate=(
                round(rescue_count / trigger_count, 3) if trigger_count else 0.0
            ),
            selected_candidate_still_had_rule_rate=(
                round(selected_still_count / frequency, 3) if frequency else 0.0
            ),
            machine_profiles=machines,
            runtime_profiles=runtimes,
            failure_signatures=signatures,
            average_latency_ms=statistics.mean(latencies) if latencies else 0.0,
        )
    return evidence_by_rule


def _stable_rule_id_event_count(events: Sequence[Mapping[str, Any]]) -> int:
    count = 0
    for event in events:
        if str(event.get("event_type") or "") != EventType.PLAN_CANDIDATE_VALIDATED:
            continue
        details = dict(event.get("details") or {})
        if details.get("validator_rule_ids"):
            count += 1
    return count


def _high_frequency_rules(
    by_rule_id: Mapping[str, RuleRuntimeEvidence],
) -> tuple[str, ...]:
    if not by_rule_id:
        return ()
    max_frequency = max(evidence.frequency for evidence in by_rule_id.values())
    return tuple(
        rule_id
        for rule_id, evidence in sorted(by_rule_id.items())
        if evidence.frequency == max_frequency
    )


def _format_rule_tuple(rule_ids: Sequence[str]) -> str:
    if not rule_ids:
        return "none"
    return ", ".join(f"`{rule_id}`" for rule_id in rule_ids)
