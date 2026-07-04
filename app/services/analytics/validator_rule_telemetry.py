"""Phase 18B: validator rule hit-rate telemetry analytics.

Read-only helpers for offline rule usage analysis. These functions consume
existing validation/candidate audit events and recovery outcomes; they do not
emit events or change validator, planning, recovery, or candidate selection
behavior.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Mapping, Optional, Sequence

from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.recovery.recovery_outcome import RecoveryOutcome


_SUCCESS_STATUSES = frozenset({"accepted", "warning"})
_RULE_ID_RE = re.compile(r"[^a-z0-9]+")


@dataclass(frozen=True)
class ValidatorRuleTelemetryRecord:
    """One validator rule firing observation."""

    rule_id: str
    validator_status: str
    planning_phase: str
    failure_signature: str
    candidate_recovery_triggered: bool
    candidate_recovery_rescued: bool
    selected_candidate_still_had_rule: bool
    machine_profile: str
    runtime_profile: str
    timestamp: str
    candidate_id: str = ""
    selected_candidate_id: Optional[str] = None

    def __post_init__(self) -> None:
        required = {
            "rule_id": self.rule_id,
            "validator_status": self.validator_status,
            "planning_phase": self.planning_phase,
            "machine_profile": self.machine_profile,
            "runtime_profile": self.runtime_profile,
            "timestamp": self.timestamp,
        }
        missing = [name for name, value in required.items() if not str(value).strip()]
        if missing:
            raise ValueError(f"missing validator rule telemetry fields: {missing}")


@dataclass(frozen=True)
class ValidatorRuleCorrelation:
    firings: int
    recovery_triggered: int
    recovery_rescued: int
    selected_candidate_still_had_rule: int
    rescue_rate: float
    selected_still_had_rule_rate: float


@dataclass(frozen=True)
class ValidatorRuleTelemetrySummary:
    total: int
    by_rule_id: Mapping[str, int]
    by_validator_status: Mapping[str, int]
    by_planning_phase: Mapping[str, int]
    by_failure_signature: Mapping[str, int]
    rescue_correlation: Mapping[str, ValidatorRuleCorrelation]


def telemetry_from_candidate_events(
    *,
    events: Sequence[Mapping[str, Any]],
    outcome: Optional[RecoveryOutcome] = None,
    planning_phase: str = "planning",
    machine_profile: str = "",
    runtime_profile: str = "",
) -> tuple[ValidatorRuleTelemetryRecord, ...]:
    """Build rule telemetry records from existing candidate validation events."""

    runtime_profile = runtime_profile or _runtime_profile(outcome, events)
    machine_profile = machine_profile or _machine_profile(runtime_profile)
    failure_signature = _failure_signature(outcome, events)
    selected_candidate_id = _selected_candidate_id(outcome, events)
    triggered = _candidate_recovery_triggered(outcome, events)
    rescued = _candidate_recovery_rescued(outcome, events)
    selected_rules = _rules_by_candidate(events).get(selected_candidate_id or "", ())

    records: list[ValidatorRuleTelemetryRecord] = []
    for event in events:
        if str(event.get("event_type") or "") != EventType.PLAN_CANDIDATE_VALIDATED:
            continue
        details = dict(event.get("details") or {})
        candidate_id = str(details.get("candidate_id") or "")
        validator_status = str(details.get("validator_status") or "")
        timestamp = str(event.get("timestamp") or "")
        for rule_id in _rule_ids(details):
            records.append(
                ValidatorRuleTelemetryRecord(
                    rule_id=rule_id,
                    validator_status=validator_status,
                    planning_phase=planning_phase,
                    failure_signature=failure_signature,
                    candidate_recovery_triggered=triggered,
                    candidate_recovery_rescued=rescued,
                    selected_candidate_still_had_rule=rule_id in selected_rules,
                    machine_profile=machine_profile,
                    runtime_profile=runtime_profile,
                    timestamp=timestamp,
                    candidate_id=candidate_id,
                    selected_candidate_id=selected_candidate_id,
                )
            )
    return tuple(records)


def aggregate_validator_rule_telemetry(
    records: Sequence[ValidatorRuleTelemetryRecord],
) -> ValidatorRuleTelemetrySummary:
    """Aggregate rule frequency and Candidate Recovery rescue correlation."""

    by_rule_id: dict[str, int] = {}
    by_status: dict[str, int] = {}
    by_phase: dict[str, int] = {}
    by_signature: dict[str, int] = {}
    correlation: dict[str, dict[str, int]] = {}

    for record in records:
        by_rule_id[record.rule_id] = by_rule_id.get(record.rule_id, 0) + 1
        by_status[record.validator_status] = (
            by_status.get(record.validator_status, 0) + 1
        )
        by_phase[record.planning_phase] = by_phase.get(record.planning_phase, 0) + 1
        by_signature[record.failure_signature] = (
            by_signature.get(record.failure_signature, 0) + 1
        )
        bucket = correlation.setdefault(
            record.rule_id,
            {
                "firings": 0,
                "recovery_triggered": 0,
                "recovery_rescued": 0,
                "selected_candidate_still_had_rule": 0,
            },
        )
        bucket["firings"] += 1
        if record.candidate_recovery_triggered:
            bucket["recovery_triggered"] += 1
        if record.candidate_recovery_rescued:
            bucket["recovery_rescued"] += 1
        if record.selected_candidate_still_had_rule:
            bucket["selected_candidate_still_had_rule"] += 1

    rescue_correlation = {
        rule_id: ValidatorRuleCorrelation(
            firings=data["firings"],
            recovery_triggered=data["recovery_triggered"],
            recovery_rescued=data["recovery_rescued"],
            selected_candidate_still_had_rule=data["selected_candidate_still_had_rule"],
            rescue_rate=(
                round(data["recovery_rescued"] / data["recovery_triggered"], 3)
                if data["recovery_triggered"]
                else 0.0
            ),
            selected_still_had_rule_rate=round(
                data["selected_candidate_still_had_rule"] / data["firings"], 3
            ),
        )
        for rule_id, data in correlation.items()
    }

    return ValidatorRuleTelemetrySummary(
        total=len(records),
        by_rule_id=by_rule_id,
        by_validator_status=by_status,
        by_planning_phase=by_phase,
        by_failure_signature=by_signature,
        rescue_correlation=rescue_correlation,
    )


def verify_validator_rule_telemetry_complete(
    record: ValidatorRuleTelemetryRecord,
) -> tuple[bool, list[str]]:
    """Verify the Phase 18B required telemetry fields."""

    issues: list[str] = []
    for field_name in (
        "rule_id",
        "validator_status",
        "planning_phase",
        "failure_signature",
        "machine_profile",
        "runtime_profile",
        "timestamp",
    ):
        if not str(getattr(record, field_name) or "").strip():
            issues.append(f"{field_name} is required")
    return len(issues) == 0, issues


def render_validator_rule_hit_rate_report(
    summary: ValidatorRuleTelemetrySummary,
) -> str:
    """Render an offline markdown report from aggregated telemetry."""

    lines = [
        "# Validator Rule Hit-Rate Telemetry",
        "",
        f"Total rule firings: {summary.total}",
        "",
        "## Rule Frequency",
    ]
    for rule_id, count in sorted(
        summary.by_rule_id.items(), key=lambda item: (-item[1], item[0])
    ):
        lines.append(f"- `{rule_id}`: {count}")

    lines.extend(["", "## Rescue Correlation"])
    for rule_id, data in sorted(summary.rescue_correlation.items()):
        lines.append(
            "- "
            f"`{rule_id}`: firings={data.firings}, "
            f"triggered={data.recovery_triggered}, "
            f"rescued={data.recovery_rescued}, "
            f"rescue_rate={data.rescue_rate:.3f}, "
            "selected_candidate_still_had_rule="
            f"{data.selected_candidate_still_had_rule}"
        )
    return "\n".join(lines) + "\n"


def _rule_ids(details: Mapping[str, Any]) -> tuple[str, ...]:
    explicit = details.get("validator_rule_ids") or details.get("rule_ids")
    source = explicit if explicit else details.get("validator_reasons") or ()
    return tuple(
        rule_id
        for rule_id in (_normalize_rule_id(value) for value in source)
        if rule_id
    )


def _normalize_rule_id(value: Any) -> str:
    raw = str(value or "").strip().lower()
    if not raw:
        return ""
    return _RULE_ID_RE.sub("_", raw).strip("_")


def _rules_by_candidate(
    events: Sequence[Mapping[str, Any]],
) -> dict[str, tuple[str, ...]]:
    rules: dict[str, tuple[str, ...]] = {}
    for event in events:
        if str(event.get("event_type") or "") != EventType.PLAN_CANDIDATE_VALIDATED:
            continue
        details = dict(event.get("details") or {})
        candidate_id = str(details.get("candidate_id") or "")
        if candidate_id:
            rules[candidate_id] = _rule_ids(details)
    return rules


def _runtime_profile(
    outcome: Optional[RecoveryOutcome],
    events: Sequence[Mapping[str, Any]],
) -> str:
    if outcome is not None:
        runtime_profile = str(outcome.recovery_context.runtime_profile or "")
        if runtime_profile:
            return runtime_profile
    for event in events:
        details = dict(event.get("details") or {})
        runtime_profile = str(details.get("runtime_profile") or "")
        if runtime_profile:
            return runtime_profile
    return "unknown"


def _machine_profile(runtime_profile: str) -> str:
    return "machine-a" if runtime_profile == "standard" else runtime_profile


def _failure_signature(
    outcome: Optional[RecoveryOutcome],
    events: Sequence[Mapping[str, Any]],
) -> str:
    if outcome is not None:
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
    return "unknown"


def _selected_candidate_id(
    outcome: Optional[RecoveryOutcome],
    events: Sequence[Mapping[str, Any]],
) -> Optional[str]:
    if outcome is not None:
        candidate_outcome = dict(
            dict(outcome.strategy_result or {}).get("candidate_outcome") or {}
        )
        selected_candidate = dict(candidate_outcome.get("selected_candidate") or {})
        selected_candidate_id = str(selected_candidate.get("candidate_id") or "")
        if selected_candidate_id:
            return selected_candidate_id
    for event in events:
        if str(event.get("event_type") or "") != EventType.PLAN_CANDIDATE_SELECTED:
            continue
        details = dict(event.get("details") or {})
        selected_candidate_id = str(details.get("candidate_id") or "")
        if selected_candidate_id:
            return selected_candidate_id
    return None


def _candidate_recovery_triggered(
    outcome: Optional[RecoveryOutcome],
    events: Sequence[Mapping[str, Any]],
) -> bool:
    if any(
        str(event.get("event_type") or "") == EventType.RECOVERY_STARTED
        for event in events
    ):
        return True
    return bool(outcome and outcome.succeeded)


def _candidate_recovery_rescued(
    outcome: Optional[RecoveryOutcome],
    events: Sequence[Mapping[str, Any]],
) -> bool:
    if outcome is not None and not outcome.succeeded:
        return False
    candidate_statuses = _candidate_statuses(events)
    selected_candidate_id = _selected_candidate_id(outcome, events)
    selected_status = candidate_statuses.get(selected_candidate_id or "")
    original_status = candidate_statuses.get("candidate-original")
    return bool(
        selected_status in _SUCCESS_STATUSES
        and original_status
        and original_status not in _SUCCESS_STATUSES
    )


def _candidate_statuses(events: Sequence[Mapping[str, Any]]) -> dict[str, str]:
    statuses: dict[str, str] = {}
    for event in events:
        if str(event.get("event_type") or "") != EventType.PLAN_CANDIDATE_VALIDATED:
            continue
        details = dict(event.get("details") or {})
        candidate_id = str(details.get("candidate_id") or "")
        if candidate_id:
            statuses[candidate_id] = str(details.get("validator_status") or "")
    return statuses
