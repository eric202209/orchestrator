"""Phase 17B-V: Reflection validation analytics and quality classification.

Offline evaluation only. No runtime behavior changes.

17B-V-1: Lightweight analytics — aggregate ReflectionRecord lists.
17B-V-2: Quality classification — manual rule-based, no LLM.
17B-V-4: Audit sequence verification — check event completeness.
"""

from __future__ import annotations

import statistics
from dataclasses import dataclass
from enum import Enum
from typing import Optional


# ── Quality classification ─────────────────────────────────────────────────────


class ReflectionQuality(str, Enum):
    """Offline quality category for a single reflection LLM output."""

    USEFUL = "useful"
    PARTIALLY_USEFUL = "partially_useful"
    REDUNDANT = "redundant"
    NOISE = "noise"


_ACTIONABLE_KEYWORDS = (
    "fix",
    "apply",
    "change",
    "update",
    "replace",
    "remove",
    "add",
    "check",
    "verify",
    "ensure",
    "use",
    "set",
    "install",
    "import",
    "rewrite",
    "rename",
    "correct",
    "adjust",
)

_REDUNDANT_PHRASES = (
    "as stated",
    "as mentioned",
    "as described in the error",
    "as shown above",
)


def classify_reflection_output(
    llm_output: Optional[str],
    error_message: str = "",
) -> ReflectionQuality:
    """Classify one LLM reflection output into a quality category.

    Rule-based — no LLM involved. For offline evaluation only.

    Rules (first match wins):
    1. Empty or NO_RECOVERY_POSSIBLE → NOISE
    2. High token overlap with error_message, no new tokens → REDUNDANT
    3. Contains redundant phrases → REDUNDANT
    4. Contains actionable keyword and ≥30 chars → USEFUL
    5. Otherwise → PARTIALLY_USEFUL
    """
    if not llm_output or llm_output.strip() == "":
        return ReflectionQuality.NOISE

    output_stripped = llm_output.strip()
    output_lower = output_stripped.lower()

    if "no_recovery_possible" in output_lower:
        return ReflectionQuality.NOISE

    # Redundant: output is mostly a restatement of the error
    if error_message:
        err_tokens = set(error_message.lower().split())
        out_tokens = set(output_lower.split())
        if err_tokens:
            overlap_ratio = len(err_tokens & out_tokens) / len(err_tokens)
            if overlap_ratio > 0.7 and len(output_stripped) < len(error_message) * 1.3:
                return ReflectionQuality.REDUNDANT

    for phrase in _REDUNDANT_PHRASES:
        if phrase in output_lower:
            return ReflectionQuality.REDUNDANT

    # Useful: actionable and substantive
    if any(kw in output_lower for kw in _ACTIONABLE_KEYWORDS):
        if len(output_stripped) >= 30:
            return ReflectionQuality.USEFUL
        return ReflectionQuality.PARTIALLY_USEFUL

    return ReflectionQuality.PARTIALLY_USEFUL


# ── Record and summary dataclasses ────────────────────────────────────────────


@dataclass
class ReflectionRecord:
    """Captures one reflection outcome for offline evaluation."""

    failure_class: str
    machine_profile: str
    outcome: str  # "success" | "failed" | "skipped"
    duration_ms: int
    llm_output: Optional[str] = None
    error: Optional[str] = None
    quality: Optional[ReflectionQuality] = None  # set during evaluation


@dataclass
class ReflectionSummary:
    """Aggregated analytics over a set of ReflectionRecords."""

    total: int
    completed: int
    failed: int
    skipped: int
    avg_latency_ms: float
    median_latency_ms: float
    success_rate: float
    by_machine_profile: dict[str, int]
    by_failure_class: dict[str, int]
    by_quality: dict[str, int]


def aggregate_reflections(records: list[ReflectionRecord]) -> ReflectionSummary:
    """Aggregate a list of ReflectionRecords into a summary."""
    total = len(records)
    completed = sum(1 for r in records if r.outcome == "success")
    failed = sum(1 for r in records if r.outcome == "failed")
    skipped = sum(1 for r in records if r.outcome == "skipped")

    latencies = [r.duration_ms for r in records]
    avg_latency = statistics.mean(latencies) if latencies else 0.0
    median_latency = statistics.median(latencies) if latencies else 0.0

    terminal = completed + failed + skipped
    success_rate = round(completed / terminal, 3) if terminal > 0 else 0.0

    by_profile: dict[str, int] = {}
    by_class: dict[str, int] = {}
    by_quality: dict[str, int] = {}

    for r in records:
        by_profile[r.machine_profile] = by_profile.get(r.machine_profile, 0) + 1
        by_class[r.failure_class] = by_class.get(r.failure_class, 0) + 1
        if r.quality is not None:
            q_key = r.quality.value
            by_quality[q_key] = by_quality.get(q_key, 0) + 1

    return ReflectionSummary(
        total=total,
        completed=completed,
        failed=failed,
        skipped=skipped,
        avg_latency_ms=avg_latency,
        median_latency_ms=median_latency,
        success_rate=success_rate,
        by_machine_profile=by_profile,
        by_failure_class=by_class,
        by_quality=by_quality,
    )


# ── Audit sequence verification ───────────────────────────────────────────────

_REFLECTION_TERMINAL_EVENTS = frozenset(
    {
        "recovery_reflection_completed",
        "recovery_reflection_failed",
        "recovery_reflection_skipped",
    }
)

_LOW_RESOURCE_PROFILES = frozenset({"low_resource", "compact_local"})

_REFLECTION_ELIGIBLE_CLASSES = frozenset({"unknown_failure", "debug_parse_error"})


def audit_sequence_complete(
    emitted_event_types: list[str],
    failure_class: str,
    machine_profile: str = "standard",
) -> tuple[bool, list[str]]:
    """Verify emitted events form the expected 17B audit sequence.

    Returns (is_complete, list_of_issues).

    Expected sequence for reflection-eligible failures on non-low_resource:
      RECOVERY_REFLECTION_STARTED (unless dedup-skipped)
      → RECOVERY_REFLECTION_{COMPLETED|FAILED}  (or SKIPPED instead of above two)
      → RECOVERY_DECISION_ROUTED

    Non-reflection failures:
      RECOVERY_DECISION_ROUTED only.

    Invariants:
    - Exactly 1 RECOVERY_DECISION_ROUTED
    - No duplicate reflection terminal events
    - STARTED must precede COMPLETED/FAILED when both are present
    - RECOVERY_DECISION_ROUTED must come after all reflection events
    """
    issues: list[str] = []

    routed_count = emitted_event_types.count("recovery_decision_routed")
    if routed_count != 1:
        issues.append(
            f"expected exactly 1 recovery_decision_routed, got {routed_count}"
        )

    expects_reflection = (
        failure_class in _REFLECTION_ELIGIBLE_CLASSES
        and machine_profile not in _LOW_RESOURCE_PROFILES
    )

    terminal_count = sum(
        emitted_event_types.count(e) for e in _REFLECTION_TERMINAL_EVENTS
    )

    if not expects_reflection:
        # Should have no reflection events at all
        started_count = emitted_event_types.count("recovery_reflection_started")
        if started_count > 0 or terminal_count > 0:
            issues.append(
                "unexpected reflection events for non-reflection failure/profile"
            )
        return len(issues) == 0, issues

    # For reflection-eligible failures:
    if terminal_count != 1:
        issues.append(
            f"expected exactly 1 reflection terminal event, got {terminal_count}"
        )

    started_count = emitted_event_types.count("recovery_reflection_started")
    skipped_count = emitted_event_types.count("recovery_reflection_skipped")
    completed_count = emitted_event_types.count("recovery_reflection_completed")
    failed_count = emitted_event_types.count("recovery_reflection_failed")

    if started_count > 1:
        issues.append(f"duplicate recovery_reflection_started ({started_count})")

    if completed_count > 1:
        issues.append(f"duplicate recovery_reflection_completed ({completed_count})")

    if failed_count > 1:
        issues.append(f"duplicate recovery_reflection_failed ({failed_count})")

    # Either (STARTED + COMPLETED/FAILED) or (SKIPPED only)
    if skipped_count == 0 and started_count == 0:
        issues.append("no reflection started or skipped event emitted")

    if skipped_count > 0 and started_count > 0:
        issues.append("both reflection_started and reflection_skipped emitted")

    # Order checks when STARTED is present
    if started_count == 1 and (completed_count == 1 or failed_count == 1):
        started_idx = emitted_event_types.index("recovery_reflection_started")
        for te in ("recovery_reflection_completed", "recovery_reflection_failed"):
            if te in emitted_event_types:
                terminal_idx = emitted_event_types.index(te)
                if terminal_idx < started_idx:
                    issues.append(f"{te} emitted before recovery_reflection_started")
                break

    # DECISION_ROUTED must come after all reflection events
    if routed_count == 1:
        routed_idx = emitted_event_types.index("recovery_decision_routed")
        for reflection_event in (
            "recovery_reflection_started",
            "recovery_reflection_completed",
            "recovery_reflection_failed",
            "recovery_reflection_skipped",
        ):
            if reflection_event in emitted_event_types:
                re_idx = emitted_event_types.index(reflection_event)
                if re_idx > routed_idx:
                    issues.append(
                        f"{reflection_event} emitted after recovery_decision_routed"
                    )

    return len(issues) == 0, issues
