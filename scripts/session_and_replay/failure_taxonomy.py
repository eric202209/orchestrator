"""Shared read-only failure classification helpers for operational reports."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any

DONE_STATUSES = {"completed", "done", "success", "succeeded"}
FAILED_EXECUTION_STATUSES = {"failed", "cancelled"}
TERMINAL_SESSION_STATUSES = {"completed", "done", "failed", "stopped", "cancelled"}

TERMINAL_REASON_PRIORITY = (
    "planning_validation_failed_after_repair",
    "planning_invalid_commands_after_repair",
    "planning_context_overflow",
    "planning_openclaw_lock_contention",
    "project_mutation_lock_conflict",
    "openclaw_timeout",
    "debug_parse_error",
    "planning_timeout",
    "repair_output_contract_violation",
    "planning_repair_no_output_timeout",
    "malformed_planning_output_repair_timeout",
    "workspace isolation violation",
    "workspace_isolation_violation",
)

KNOWN_TERMINAL_REASONS = {
    # Planning failures
    "planning_validation_failed_after_repair",
    "planning_repair_prompt_too_large",
    "planning_repair_no_output_timeout",
    "planning_repair_timeout",
    "planning_timeout",
    "planning_invalid_commands_after_repair",
    "planning_context_overflow",
    "planning_openclaw_lock_contention",
    "project_mutation_lock_conflict",
    "openclaw_timeout",
    "malformed_planning_output_repair_timeout",
    "planning_json_error",
    "planning_parse_error",
    "plan_revision_cap_reached",
    "planning_circuit_breaker_opened",
    "planning_circuit_breaker_opened_persisted_attempts",
    "revised_plan_validation_failed",
    "truncated_multistep_plan_after_retry",
    # Execution / repair failures
    "repair_output_contract_violation",
    "workspace_isolation_violation",
    "op_contract_violation",
    "debug_repair_budget_exhausted",
    "repair_attempt_limit_reached",
    "max_attempts_reached",
    "repeated_tool_path_failure",
    "reasoning_artifact_validation_failed",
    # Completion / verification failures
    "completion_validation_failed",
    "completion_repair_failed",
    "repeat_completion_failure_signature",
    # Code-level failures
    "pytest_failure",
    "module_not_found",
    "import_error",
    "missing_dependency",
    "syntax_error",
    "runtime_assertion_failure",
    "debug_parse_error",
    # System / lifecycle failures
    "baseline_publish_validation_failed",
    "agent_requested_human_intervention",
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


STUCK_TIMEOUT_SECONDS = 3600  # sessions active beyond this with no terminal are stuck


def _has_repair_evidence(metadata_rows: list[dict[str, Any]]) -> bool:
    for row in metadata_rows:
        meta = parse_log_metadata(row.get("log_metadata"))
        if (
            int(meta.get("repair_attempts") or 0) > 0
            or meta.get("retry") == "repair_prompt"
            or meta.get("attempt") == "repair"
            or "repair" in str(meta.get("strategy") or "").lower()
        ):
            return True
    return False


def _int_value(value: Any, default: int = 0) -> int:
    try:
        return int(value if value is not None else default)
    except (TypeError, ValueError):
        return default


def _is_capacity_limit_row(row: dict[str, Any]) -> bool:
    return str(row.get("failure_category") or "").strip() == "backend_capacity_limit"


def _capacity_retry_budget_exhausted(metadata_rows: list[dict[str, Any]]) -> bool:
    for row in metadata_rows:
        metadata = parse_log_metadata(row.get("log_metadata"))
        if str(metadata.get("reason") or "").strip() != "backend_capacity_limit":
            continue
        if bool(
            metadata.get("retry_budget_exhausted")
            or metadata.get("max_retries_exhausted")
            or metadata.get("capacity_retry_budget_exhausted")
        ):
            return True
    return False


def _latest_task_executions(
    task_executions: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    latest_by_task: dict[Any, dict[str, Any]] = {}
    for index, row in enumerate(task_executions):
        task_key = row.get("task_id")
        if task_key is None:
            task_key = ("row", index)
        current = latest_by_task.get(task_key)
        if current is None:
            latest_by_task[task_key] = row
            continue
        row_order = (
            _int_value(row.get("attempt_number"), 1),
            _int_value(row.get("id")),
        )
        current_order = (
            _int_value(current.get("attempt_number"), 1),
            _int_value(current.get("id")),
        )
        if row_order >= current_order:
            latest_by_task[task_key] = row
    return list(latest_by_task.values())


def outcome_class(
    session_row: dict[str, Any],
    task_executions: list[dict[str, Any]],
    metadata_rows: list[dict[str, Any]],
    *,
    failure_summary_generated: bool = False,
    stuck_timeout_seconds: int = STUCK_TIMEOUT_SECONDS,
) -> str:
    """
    Classify a session into one of four production-readiness outcome classes:

    - "first_pass_success"       — DONE on attempt 1, no repair
    - "recovered_success"        — DONE after repair or retry
    - "failed_but_actionable"    — FAILED with known reason; operator has next step
    - "stuck_or_manual_db_cleanup" — requires manual intervention
    - "in_progress"              — session is still active (not yet classified)
    """
    session_status = status_key(session_row.get("status"))

    # Still active — check if elapsed time indicates stuck
    if session_status in ("running", "pending", "active"):
        started = session_row.get("started_at")
        if started:
            try:
                if isinstance(started, str):
                    started_dt = datetime.fromisoformat(started.replace("Z", "+00:00"))
                    if started_dt.tzinfo is None:
                        started_dt = started_dt.replace(tzinfo=UTC)
                else:
                    started_dt = started
                elapsed = (datetime.now(UTC) - started_dt).total_seconds()
                if elapsed > stuck_timeout_seconds:
                    return "stuck_or_manual_db_cleanup"
            except Exception:
                pass
        return "in_progress"

    final_task_executions = _latest_task_executions(task_executions)

    # Task executions still in running/pending after session stopped = stuck.
    # Retries can leave older failed executions behind, so only the latest
    # execution per task determines whether that task is still active.
    active_executions = [
        r
        for r in final_task_executions
        if status_key(r.get("status")) in ("running", "pending", "active")
    ]
    if active_executions:
        return "stuck_or_manual_db_cleanup"

    # Sessions in this system end as "stopped" even on success — treat as done
    # when session is stopped/terminal AND the latest execution for every task
    # reached a done status.
    all_tasks_done = bool(final_task_executions) and all(
        status_key(r.get("status")) in DONE_STATUSES for r in final_task_executions
    )
    is_done = session_status in DONE_STATUSES or (
        session_status in TERMINAL_SESSION_STATUSES and all_tasks_done
    )

    if is_done:
        non_capacity_executions = [
            row for row in task_executions if not _is_capacity_limit_row(row)
        ]
        max_attempt = max(
            (int(row.get("attempt_number") or 1) for row in non_capacity_executions),
            default=1,
        )
        had_failed_attempt = any(
            status_key(row.get("status")) in FAILED_EXECUTION_STATUSES
            for row in non_capacity_executions
        )
        had_capacity_attempt = any(_is_capacity_limit_row(row) for row in task_executions)
        repair_used = _has_repair_evidence(metadata_rows)
        if had_capacity_attempt and not repair_used and not had_failed_attempt:
            return "first_pass_success"
        if max_attempt == 1 and not repair_used and not had_failed_attempt:
            return "first_pass_success"
        return "recovered_success"

    # Terminal failure — classify as actionable or stuck
    terminal_reason = latest_terminal_reason(metadata_rows)
    if terminal_reason and terminal_reason in KNOWN_TERMINAL_REASONS:
        return "failed_but_actionable"

    # Any diagnostic reason (not necessarily a known terminal reason) is actionable
    capacity_retry_budget_exhausted = _capacity_retry_budget_exhausted(metadata_rows)
    for row in metadata_rows:
        meta = parse_log_metadata(row.get("log_metadata"))
        reason = str(meta.get("reason") or "").strip()
        if reason == "backend_capacity_limit" and not capacity_retry_budget_exhausted:
            continue
        if reason:
            return "failed_but_actionable"

    if failure_summary_generated:
        return "failed_but_actionable"

    # Paused sessions with no failure evidence are intentionally paused, not stuck
    if session_status == "paused":
        return "in_progress"

    return "stuck_or_manual_db_cleanup"
