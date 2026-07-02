"""Phase 17B-V: Tests for reflection validation analytics.

Covers:
- 17B-V-1: ReflectionRecord aggregation
- 17B-V-2: Quality classification
- 17B-V-4: Audit sequence completeness
"""

from __future__ import annotations

import pytest

from app.services.analytics.reflection_validation import (
    ReflectionQuality,
    ReflectionRecord,
    ReflectionSummary,
    aggregate_reflections,
    audit_sequence_complete,
    classify_reflection_output,
)


# ── Helpers ───────────────────────────────────────────────────────────────────


def _record(
    outcome: str = "success",
    duration_ms: int = 100,
    failure_class: str = "unknown_failure",
    machine_profile: str = "standard",
    llm_output: str | None = "apply the fix",
    quality: ReflectionQuality | None = None,
) -> ReflectionRecord:
    return ReflectionRecord(
        failure_class=failure_class,
        machine_profile=machine_profile,
        outcome=outcome,
        duration_ms=duration_ms,
        llm_output=llm_output,
        quality=quality,
    )


# ── 17B-V-1: aggregate_reflections ───────────────────────────────────────────


class TestAggregateReflections:
    def test_empty_returns_zero_summary(self):
        s = aggregate_reflections([])
        assert s.total == 0
        assert s.completed == 0
        assert s.avg_latency_ms == 0.0
        assert s.median_latency_ms == 0.0
        assert s.success_rate == 0.0

    def test_counts_outcomes_correctly(self):
        records = [
            _record("success"),
            _record("success"),
            _record("failed"),
            _record("skipped"),
        ]
        s = aggregate_reflections(records)
        assert s.total == 4
        assert s.completed == 2
        assert s.failed == 1
        assert s.skipped == 1

    def test_success_rate(self):
        records = [_record("success"), _record("failed"), _record("failed")]
        s = aggregate_reflections(records)
        assert s.success_rate == pytest.approx(1 / 3, rel=1e-3)

    def test_avg_latency(self):
        records = [_record(duration_ms=100), _record(duration_ms=300)]
        s = aggregate_reflections(records)
        assert s.avg_latency_ms == pytest.approx(200.0)

    def test_median_latency(self):
        records = [
            _record(duration_ms=100),
            _record(duration_ms=200),
            _record(duration_ms=300),
        ]
        s = aggregate_reflections(records)
        assert s.median_latency_ms == pytest.approx(200.0)

    def test_by_machine_profile(self):
        records = [
            _record(machine_profile="standard"),
            _record(machine_profile="standard"),
            _record(machine_profile="medium"),
        ]
        s = aggregate_reflections(records)
        assert s.by_machine_profile["standard"] == 2
        assert s.by_machine_profile["medium"] == 1

    def test_by_failure_class(self):
        records = [
            _record(failure_class="unknown_failure"),
            _record(failure_class="debug_parse_error"),
            _record(failure_class="unknown_failure"),
        ]
        s = aggregate_reflections(records)
        assert s.by_failure_class["unknown_failure"] == 2
        assert s.by_failure_class["debug_parse_error"] == 1

    def test_by_quality_counts(self):
        records = [
            _record(quality=ReflectionQuality.USEFUL),
            _record(quality=ReflectionQuality.USEFUL),
            _record(quality=ReflectionQuality.NOISE),
        ]
        s = aggregate_reflections(records)
        assert s.by_quality["useful"] == 2
        assert s.by_quality["noise"] == 1

    def test_by_quality_skips_none(self):
        records = [_record(quality=None)]
        s = aggregate_reflections(records)
        assert s.by_quality == {}

    def test_returns_summary_type(self):
        s = aggregate_reflections([_record()])
        assert isinstance(s, ReflectionSummary)


# ── 17B-V-2: classify_reflection_output ──────────────────────────────────────


class TestClassifyReflectionOutput:
    def test_none_is_noise(self):
        assert classify_reflection_output(None) == ReflectionQuality.NOISE

    def test_empty_string_is_noise(self):
        assert classify_reflection_output("") == ReflectionQuality.NOISE

    def test_whitespace_only_is_noise(self):
        assert classify_reflection_output("   ") == ReflectionQuality.NOISE

    def test_no_recovery_possible_is_noise(self):
        assert (
            classify_reflection_output("NO_RECOVERY_POSSIBLE")
            == ReflectionQuality.NOISE
        )

    def test_no_recovery_possible_case_insensitive(self):
        assert (
            classify_reflection_output("no_recovery_possible")
            == ReflectionQuality.NOISE
        )

    def test_actionable_output_is_useful(self):
        result = classify_reflection_output(
            "Fix the import by adding the missing module to requirements.txt"
        )
        assert result == ReflectionQuality.USEFUL

    def test_short_actionable_is_partially_useful(self):
        result = classify_reflection_output("fix it")
        assert result == ReflectionQuality.PARTIALLY_USEFUL

    def test_redundant_phrase_is_redundant(self):
        result = classify_reflection_output(
            "As mentioned, the error occurred due to a parse failure."
        )
        assert result == ReflectionQuality.REDUNDANT

    def test_high_error_overlap_is_redundant(self):
        error = "ValueError bad input token missing parse failure"
        output = "ValueError bad input token missing parse failure nothing new"
        result = classify_reflection_output(output, error_message=error)
        assert result == ReflectionQuality.REDUNDANT

    def test_returns_reflection_quality_enum(self):
        result = classify_reflection_output("Update the configuration file")
        assert isinstance(result, ReflectionQuality)

    def test_low_overlap_with_error_not_redundant(self):
        error = "ValueError unexpected token"
        output = (
            "Add a null check before calling parse() to handle missing values safely."
        )
        result = classify_reflection_output(output, error_message=error)
        assert result in (ReflectionQuality.USEFUL, ReflectionQuality.PARTIALLY_USEFUL)

    def test_no_actionable_without_error_is_partially_useful(self):
        result = classify_reflection_output("The situation requires attention.")
        assert result == ReflectionQuality.PARTIALLY_USEFUL


# ── 17B-V-4: audit_sequence_complete ──────────────────────────────────────────


class TestAuditSequenceComplete:
    # Non-reflection path: only RECOVERY_DECISION_ROUTED

    def test_terminal_only_failure_class(self):
        events = ["recovery_decision_routed"]
        ok, issues = audit_sequence_complete(
            events, failure_class="planning_timeout", machine_profile="standard"
        )
        assert ok is True
        assert issues == []

    def test_existing_recovery_no_reflection_events(self):
        events = ["recovery_decision_routed"]
        ok, issues = audit_sequence_complete(
            events, failure_class="pytest_failure", machine_profile="standard"
        )
        assert ok is True

    def test_missing_decision_routed(self):
        events = []
        ok, issues = audit_sequence_complete(
            events, failure_class="planning_timeout", machine_profile="standard"
        )
        assert ok is False
        assert any("recovery_decision_routed" in i for i in issues)

    def test_duplicate_decision_routed(self):
        events = ["recovery_decision_routed", "recovery_decision_routed"]
        ok, issues = audit_sequence_complete(
            events, failure_class="planning_timeout", machine_profile="standard"
        )
        assert ok is False

    # Reflection path: standard machine + eligible failure class

    def test_reflection_completed_sequence(self):
        events = [
            "recovery_reflection_started",
            "recovery_reflection_completed",
            "recovery_decision_routed",
        ]
        ok, issues = audit_sequence_complete(
            events, failure_class="unknown_failure", machine_profile="standard"
        )
        assert ok is True, issues

    def test_reflection_failed_sequence(self):
        events = [
            "recovery_reflection_started",
            "recovery_reflection_failed",
            "recovery_decision_routed",
        ]
        ok, issues = audit_sequence_complete(
            events, failure_class="debug_parse_error", machine_profile="standard"
        )
        assert ok is True, issues

    def test_reflection_dedup_skipped_sequence(self):
        events = [
            "recovery_reflection_skipped",
            "recovery_decision_routed",
        ]
        ok, issues = audit_sequence_complete(
            events, failure_class="unknown_failure", machine_profile="standard"
        )
        assert ok is True, issues

    def test_missing_reflection_terminal_event(self):
        events = [
            "recovery_reflection_started",
            "recovery_decision_routed",
        ]
        ok, issues = audit_sequence_complete(
            events, failure_class="unknown_failure", machine_profile="standard"
        )
        assert ok is False

    def test_duplicate_reflection_started(self):
        events = [
            "recovery_reflection_started",
            "recovery_reflection_started",
            "recovery_reflection_completed",
            "recovery_decision_routed",
        ]
        ok, issues = audit_sequence_complete(
            events, failure_class="unknown_failure", machine_profile="standard"
        )
        assert ok is False

    def test_duplicate_reflection_completed(self):
        events = [
            "recovery_reflection_started",
            "recovery_reflection_completed",
            "recovery_reflection_completed",
            "recovery_decision_routed",
        ]
        ok, issues = audit_sequence_complete(
            events, failure_class="unknown_failure", machine_profile="standard"
        )
        assert ok is False

    def test_terminal_before_started_is_invalid(self):
        events = [
            "recovery_reflection_completed",
            "recovery_reflection_started",
            "recovery_decision_routed",
        ]
        ok, issues = audit_sequence_complete(
            events, failure_class="unknown_failure", machine_profile="standard"
        )
        assert ok is False

    def test_decision_routed_before_reflection_events_is_invalid(self):
        events = [
            "recovery_decision_routed",
            "recovery_reflection_started",
            "recovery_reflection_completed",
        ]
        ok, issues = audit_sequence_complete(
            events, failure_class="unknown_failure", machine_profile="standard"
        )
        assert ok is False

    # Low-resource machine: reflection should be skipped (no reflection events expected)

    def test_low_resource_no_reflection_events_ok(self):
        events = ["recovery_decision_routed"]
        ok, issues = audit_sequence_complete(
            events, failure_class="unknown_failure", machine_profile="low_resource"
        )
        assert ok is True

    def test_compact_local_no_reflection_events_ok(self):
        events = ["recovery_decision_routed"]
        ok, issues = audit_sequence_complete(
            events, failure_class="debug_parse_error", machine_profile="compact_local"
        )
        assert ok is True

    def test_low_resource_with_reflection_events_is_invalid(self):
        events = [
            "recovery_reflection_started",
            "recovery_reflection_completed",
            "recovery_decision_routed",
        ]
        ok, issues = audit_sequence_complete(
            events, failure_class="unknown_failure", machine_profile="low_resource"
        )
        assert ok is False

    def test_both_started_and_skipped_is_invalid(self):
        events = [
            "recovery_reflection_started",
            "recovery_reflection_skipped",
            "recovery_decision_routed",
        ]
        ok, issues = audit_sequence_complete(
            events, failure_class="unknown_failure", machine_profile="standard"
        )
        assert ok is False

    def test_returns_tuple_bool_list(self):
        events = ["recovery_decision_routed"]
        result = audit_sequence_complete(
            events, failure_class="unknown_failure", machine_profile="low_resource"
        )
        assert isinstance(result, tuple)
        assert len(result) == 2
        assert isinstance(result[0], bool)
        assert isinstance(result[1], list)
