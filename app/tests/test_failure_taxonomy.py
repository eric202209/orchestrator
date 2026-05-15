from __future__ import annotations

import json

from scripts.failure_taxonomy import (
    KNOWN_TERMINAL_REASONS,
    contract_reason,
    failure_class,
    latest_terminal_reason,
    outcome_class,
    parse_log_metadata,
    terminal_class,
    terminal_reason_from_rows,
)


def test_parse_log_metadata_degrades_to_empty_dict():
    assert parse_log_metadata(None) == {}
    assert parse_log_metadata("not-json") == {}
    assert parse_log_metadata(json.dumps(["not", "a", "dict"])) == {}


def test_failure_class_prefers_explicit_then_envelope():
    assert failure_class({"debug_failure_class": "pytest_failure"}) == "pytest_failure"
    assert failure_class({"failure_class": "completion_validation_failed"}) == (
        "completion_validation_failed"
    )
    assert (
        failure_class({"debug_feedback_envelope": {"failure_class": "import_error"}})
        == "import_error"
    )
    assert failure_class({}) == "unknown"


def test_contract_reason_falls_back_through_report_metadata_shapes():
    assert (
        contract_reason({"contract_violation_type": "brittle_command"})
        == "brittle_command"
    )
    assert contract_reason({"reason": "planning_timeout"}) == "planning_timeout"
    assert (
        contract_reason({"contract_violations": ["missing verification"]})
        == "missing verification"
    )
    assert contract_reason({}) == "unknown"


def test_terminal_reason_uses_known_reason_then_error_message():
    rows = [
        {"log_metadata": json.dumps({"reason": "non_terminal_diagnostic"})},
        {
            "log_metadata": json.dumps(
                {"details": {"reason": "completion_validation_failed"}}
            )
        },
    ]

    assert terminal_reason_from_rows(rows, {}) == "completion_validation_failed"
    assert terminal_reason_from_rows([], {"error_message": "x" * 250}) == "x" * 180


def test_terminal_class_prioritizes_terminal_reason_then_statuses():
    rows = [
        {"log_metadata": json.dumps({"reason": "project_root_is_source_of_truth"})},
        {"log_metadata": json.dumps({"reason": "planning_timeout"})},
        {"log_metadata": json.dumps({"reason": "completion_validation_failed"})},
    ]

    assert latest_terminal_reason(rows) == "planning_timeout"
    assert (
        terminal_class(
            session={"status": "done"}, task_executions=[], metadata_rows=rows
        )
        == "planning_timeout"
    )
    assert (
        terminal_class(
            session={"status": "running"},
            task_executions=[{"status": "failed"}],
            metadata_rows=[],
        )
        == "task_execution_failed"
    )
    assert (
        terminal_class(session={"status": "done"}, task_executions=[], metadata_rows=[])
        == "DONE"
    )


def test_terminal_class_ignores_nonterminal_metadata_reasons():
    rows = [{"log_metadata": json.dumps({"reason": "project_root_is_source_of_truth"})}]

    assert latest_terminal_reason(rows) is None
    assert (
        terminal_class(
            session={"status": "stopped"},
            task_executions=[{"status": "cancelled"}],
            metadata_rows=rows,
        )
        == "task_execution_failed"
    )


# ---------------------------------------------------------------------------
# outcome_class tests
# ---------------------------------------------------------------------------


def _done_session(status: str = "done") -> dict:
    return {"status": status, "started_at": "2026-01-01T00:00:00+00:00"}


def _execution(attempt: int = 1, status: str = "done") -> dict:
    return {"attempt_number": attempt, "status": status}


def _reason_row(reason: str) -> dict:
    return {"log_metadata": json.dumps({"reason": reason})}


def test_outcome_class_first_pass_success():
    result = outcome_class(
        _done_session(),
        [_execution(attempt=1, status="done")],
        [],
    )
    assert result == "first_pass_success"


def test_outcome_class_recovered_success_via_attempt_number():
    result = outcome_class(
        _done_session(),
        [_execution(attempt=2, status="done")],
        [],
    )
    assert result == "recovered_success"


def test_outcome_class_recovered_success_via_repair_metadata():
    repair_row = {"log_metadata": json.dumps({"repair_attempts": 1})}
    result = outcome_class(
        _done_session(),
        [_execution(attempt=1, status="done")],
        [repair_row],
    )
    assert result == "recovered_success"


def test_outcome_class_failed_but_actionable_known_reason():
    reason = next(iter(KNOWN_TERMINAL_REASONS))
    result = outcome_class(
        {"status": "failed", "started_at": "2026-01-01T00:00:00+00:00"},
        [_execution(status="failed")],
        [_reason_row(reason)],
    )
    assert result == "failed_but_actionable"


def test_outcome_class_failed_but_actionable_any_diagnostic():
    result = outcome_class(
        {"status": "stopped", "started_at": "2026-01-01T00:00:00+00:00"},
        [_execution(status="cancelled")],
        [_reason_row("some_diagnostic_reason_not_in_known_set")],
    )
    assert result == "failed_but_actionable"


def test_outcome_class_failed_but_actionable_via_failure_summary():
    result = outcome_class(
        {"status": "stopped", "started_at": "2026-01-01T00:00:00+00:00"},
        [_execution(status="cancelled")],
        [],
        failure_summary_generated=True,
    )
    assert result == "failed_but_actionable"


def test_outcome_class_stuck_no_reason_no_summary():
    result = outcome_class(
        {"status": "failed", "started_at": "2026-01-01T00:00:00+00:00"},
        [_execution(status="failed")],
        [],
    )
    assert result == "stuck_or_manual_db_cleanup"


def test_outcome_class_stuck_active_execution_after_session_stopped():
    result = outcome_class(
        {"status": "stopped", "started_at": "2026-01-01T00:00:00+00:00"},
        [_execution(status="running")],
        [],
    )
    assert result == "stuck_or_manual_db_cleanup"


def test_outcome_class_in_progress_for_active_session():
    from datetime import UTC, datetime, timedelta

    recent = (datetime.now(UTC) - timedelta(seconds=60)).isoformat()
    result = outcome_class(
        {"status": "running", "started_at": recent},
        [],
        [],
    )
    assert result == "in_progress"


def test_outcome_class_in_progress_without_started_at():
    result = outcome_class(
        {"status": "running", "started_at": None},
        [],
        [],
    )
    assert result == "in_progress"
