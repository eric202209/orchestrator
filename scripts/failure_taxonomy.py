"""Shared read-only failure classification helpers for operational reports."""

from __future__ import annotations

import json
from typing import Any

DONE_STATUSES = {"completed", "done", "success", "succeeded"}
FAILED_EXECUTION_STATUSES = {"failed", "cancelled"}
TERMINAL_SESSION_STATUSES = {"completed", "done", "failed", "stopped", "cancelled"}

TERMINAL_REASON_PRIORITY = (
    "planning_validation_failed_after_repair",
    "planning_invalid_commands_after_repair",
    "planning_context_overflow",
    "planning_openclaw_lock_contention",
    "planning_timeout",
    "repair_output_contract_violation",
    "planning_repair_no_output_timeout",
    "malformed_planning_output_repair_timeout",
    "workspace isolation violation",
    "workspace_isolation_violation",
)

KNOWN_TERMINAL_REASONS = {
    "planning_validation_failed_after_repair",
    "planning_repair_prompt_too_large",
    "workspace_isolation_violation",
    "repair_output_contract_violation",
    "planning_repair_no_output_timeout",
    "planning_repair_timeout",
    "planning_timeout",
    "reasoning_artifact_validation_failed",
    "completion_validation_failed",
    "pytest_failure",
    "module_not_found",
    "import_error",
    "missing_dependency",
    "syntax_error",
    "runtime_assertion_failure",
}

REPORT_TERMINAL_REASONS = set(TERMINAL_REASON_PRIORITY) | KNOWN_TERMINAL_REASONS


def parse_log_metadata(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def status_key(value: Any) -> str:
    return str(value or "").strip().lower()


def failure_class(metadata: dict[str, Any]) -> str:
    envelope = metadata.get("debug_feedback_envelope")
    if not isinstance(envelope, dict):
        envelope = {}
    return str(
        metadata.get("debug_failure_class")
        or metadata.get("failure_class")
        or envelope.get("failure_class")
        or "unknown"
    )


def contract_reason(metadata: dict[str, Any]) -> str:
    value = (
        metadata.get("contract_violation_type")
        or metadata.get("reason")
        or _first_text(metadata.get("contract_violations"))
        or "unknown"
    )
    return str(value).strip() or "unknown"


def latest_terminal_reason(metadata_rows: list[dict[str, Any]]) -> str | None:
    reasons: list[str] = []
    for row in metadata_rows:
        metadata = parse_log_metadata(row.get("log_metadata"))
        reason = str(metadata.get("reason") or "").strip()
        if reason:
            reasons.append(reason)
    for preferred in TERMINAL_REASON_PRIORITY:
        if preferred in reasons:
            return preferred
    for reason in reasons:
        if reason in REPORT_TERMINAL_REASONS:
            return reason
    return None


def terminal_class(
    *,
    session: dict[str, Any],
    task_executions: list[dict[str, Any]],
    metadata_rows: list[dict[str, Any]],
) -> str:
    reason = latest_terminal_reason(metadata_rows)
    if reason:
        return reason

    execution_statuses = {status_key(row.get("status")) for row in task_executions}
    if execution_statuses & FAILED_EXECUTION_STATUSES:
        return "task_execution_failed"

    session_status = status_key(session.get("status"))
    if session_status in DONE_STATUSES:
        return "DONE"
    if session_status in TERMINAL_SESSION_STATUSES:
        return session_status
    if not task_executions:
        return f"{session_status or 'unknown'}_no_task_execution"
    return session_status or "unknown"


def terminal_reason_from_rows(
    rows: list[dict[str, Any]], context: dict[str, Any] | None = None
) -> str:
    for row in reversed(rows):
        metadata = parse_log_metadata(row.get("log_metadata"))
        details = metadata.get("details")
        if not isinstance(details, dict):
            details = {}
        candidates = (
            details.get("reason"),
            metadata.get("reason"),
            metadata.get("failure_type"),
        )
        for candidate in candidates:
            text = str(candidate or "").strip()
            if text in KNOWN_TERMINAL_REASONS:
                return text
    error_message = str((context or {}).get("error_message") or "").strip()
    if error_message:
        return error_message[:180]
    return ""


def _first_text(value: Any) -> str | None:
    if isinstance(value, list) and value:
        return str(value[0])
    if isinstance(value, str):
        return value
    return None
