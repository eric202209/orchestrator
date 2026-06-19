"""Tests for recovery inspection report generation helpers."""

from __future__ import annotations

import pytest

from scripts.evals.run_recovery_inspection_report import (
    InspectionRecord,
    RECOVERY_FAILED,
    RECOVERY_MISSED_OPPORTUNITY,
    RECOVERY_NOT_APPLICABLE_OUT_OF_SCOPE,
    RECOVERY_NOT_APPLICABLE_SAFETY,
    RECOVERY_SKIPPED,
    RECOVERY_SUCCEEDED,
    RECOVERY_UNKNOWN,
    _classify_recovery_applicability,
    _render_markdown,
    _threshold_band,
)


def test_threshold_band_boundaries():
    assert _threshold_band(0.0) == "GREEN"
    assert _threshold_band(0.049) == "GREEN"
    assert _threshold_band(0.05) == "YELLOW"
    assert _threshold_band(0.10) == "YELLOW"
    assert _threshold_band(0.101) == "RED"


def test_render_markdown_includes_metrics_and_records():
    record = InspectionRecord(
        session_id=1,
        task_id=2,
        project_id=3,
        timestamp="2026-06-19T00:00:00Z",
        scope="step",
        failure_class="pytest_failure",
        recovery_attempt_number=1,
        patch_path="app/foo.py",
        rerun_command="pytest app/",
        validator_accepted=True,
        recovery_duration_seconds=1.25,
        inspection_category="INCONCLUSIVE",
        checklist={
            "A": "INCONCLUSIVE",
            "B": "INCONCLUSIVE",
            "C": "INCONCLUSIVE",
            "D": "INCONCLUSIVE",
            "E": "INCONCLUSIVE",
        },
    )
    md = _render_markdown([record], "2026-06-19")
    assert "total_recovery_successes_reviewed" in md
    assert "human_review_false_positive_rate" in md
    assert "pytest_failure" in md
    assert "app/foo.py" in md


@pytest.mark.parametrize(
    "final_reason,recovery_events,expected",
    [
        ("verification_integrity_failed", set(), RECOVERY_NOT_APPLICABLE_SAFETY),
        ("permission_denied", set(), RECOVERY_NOT_APPLICABLE_SAFETY),
        ("planning_json_error", set(), RECOVERY_NOT_APPLICABLE_OUT_OF_SCOPE),
        ("import_error", set(), RECOVERY_MISSED_OPPORTUNITY),
        ("pytest_failure", set(), RECOVERY_MISSED_OPPORTUNITY),
        ("missing_requested_symbol", set(), RECOVERY_MISSED_OPPORTUNITY),
        ("something_else", {"execution_recovery_succeeded"}, RECOVERY_SUCCEEDED),
        ("something_else", {"execution_recovery_failed"}, RECOVERY_FAILED),
        ("something_else", {"execution_recovery_skipped"}, RECOVERY_SKIPPED),
        ("opaque_failure", set(), RECOVERY_UNKNOWN),
    ],
)
def test_classify_recovery_applicability(final_reason, recovery_events, expected):
    assert (
        _classify_recovery_applicability(
            final_reason=final_reason, recovery_events=recovery_events
        )
        == expected
    )
