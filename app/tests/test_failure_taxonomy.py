from __future__ import annotations

import json

from scripts.failure_taxonomy import (
    contract_reason,
    failure_class,
    latest_terminal_reason,
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
