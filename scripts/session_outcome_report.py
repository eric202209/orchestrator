#!/usr/bin/env python3
"""Summarize recent session outcomes from read-only operational evidence."""

from __future__ import annotations

import argparse
from collections import Counter
from collections.abc import Iterable
import json
import sqlite3
import sys
from pathlib import Path
from statistics import mean
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))

TERMINAL_SESSION_STATUSES = {"completed", "done", "failed", "stopped", "cancelled"}
DONE_STATUSES = {"completed", "done", "success", "succeeded"}
FAILED_EXECUTION_STATUSES = {"failed", "cancelled"}
RUNTIME_LOG_FILTER = "(message like '%[OPENCLAW]%' or message like '%[PERFORMANCE]%')"
SECOND_REPAIR_REASONS = {
    "post_repair_weak_verification_second_pass",
    "post_repair_background_process_second_pass",
    "post_repair_missing_verification_second_pass",
}
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
JOURNAL_TERMINAL_EVENT_TYPES = {
    "task_failed",
    "completion_evidence_failed",
    "task_dispatch_rejected",
}


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(
    conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def _one(
    conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()
) -> dict[str, Any] | None:
    row = conn.execute(query, params).fetchone()
    return dict(row) if row is not None else None


def _parse_metadata(value: Any) -> dict[str, Any]:
    if not value:
        return {}
    try:
        parsed = json.loads(value)
    except (TypeError, ValueError):
        return {}
    return parsed if isinstance(parsed, dict) else {}


def _status(value: Any) -> str:
    return str(value or "").strip().lower()


def _number(value: Any) -> float | None:
    try:
        parsed = float(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed >= 0 else None


def _mean_or_none(values: Iterable[float]) -> float | None:
    collected = list(values)
    if not collected:
        return None
    return round(mean(collected), 3)


def _latest_terminal_reason(metadata_rows: list[dict[str, Any]]) -> str | None:
    reasons: list[str] = []
    for row in metadata_rows:
        metadata = _parse_metadata(row.get("log_metadata"))
        reason = str(metadata.get("reason") or "").strip()
        if reason:
            reasons.append(reason)
    for preferred in TERMINAL_REASON_PRIORITY:
        if preferred in reasons:
            return preferred
    return reasons[0] if reasons else None


def _terminal_class(
    *,
    session: dict[str, Any],
    task_executions: list[dict[str, Any]],
    metadata_rows: list[dict[str, Any]],
) -> str:
    reason = _latest_terminal_reason(metadata_rows)
    if reason:
        return reason

    execution_statuses = {_status(row.get("status")) for row in task_executions}
    if execution_statuses & FAILED_EXECUTION_STATUSES:
        return "task_execution_failed"

    session_status = _status(session.get("status"))
    if session_status in DONE_STATUSES:
        return "DONE"
    if session_status in TERMINAL_SESSION_STATUSES:
        return session_status
    if not task_executions:
        return f"{session_status or 'unknown'}_no_task_execution"
    return session_status or "unknown"


def _metadata_evidence(metadata_rows: list[dict[str, Any]]) -> dict[str, Any]:
    planning_durations: list[float] = []
    repair_durations: list[float] = []
    repair_used = False
    second_repair_used = False
    diagnostic_reason = None

    for row in metadata_rows:
        metadata = _parse_metadata(row.get("log_metadata"))
        message = str(row.get("message") or "")
        reason = str(metadata.get("reason") or "").strip()
        if reason and diagnostic_reason is None:
            diagnostic_reason = reason

        planning_duration = _number(metadata.get("planning_duration"))
        if planning_duration is not None:
            planning_durations.append(planning_duration)

        repair_attempts = _number(metadata.get("repair_attempts")) or 0
        is_repair = (
            repair_attempts > 0
            or metadata.get("retry") == "repair_prompt"
            or metadata.get("attempt") == "repair"
            or "repair" in str(metadata.get("strategy") or "").lower()
        )
        if is_repair:
            repair_used = True
            duration = _number(metadata.get("duration_seconds"))
            if duration is not None:
                repair_durations.append(duration)

        if reason in SECOND_REPAIR_REASONS or "targeted second repair" in message:
            second_repair_used = True

    return {
        "repair_used": repair_used,
        "second_repair_used": second_repair_used,
        "planning_durations": planning_durations,
        "repair_durations": repair_durations,
        "diagnostic_reason": diagnostic_reason,
    }


def _journal_paths(session: dict[str, Any], task_ids: Iterable[int]) -> list[Path]:
    workspace_path = str(session.get("workspace_path") or "").strip()
    if not workspace_path:
        return []
    workspace = Path(workspace_path)
    project_name = str(session.get("project_name") or "").strip()
    roots = [workspace]
    if project_name:
        roots.append(workspace / project_name)
    paths: list[Path] = []
    for root in roots:
        for task_id in task_ids:
            paths.append(
                root
                / ".openclaw"
                / "events"
                / f"session_{session['id']}_task_{task_id}.jsonl"
            )
    return paths


def _journal_has_terminal_event(paths: Iterable[Path]) -> bool:
    for path in paths:
        if not path.exists():
            continue
        try:
            lines = path.read_text(encoding="utf-8").splitlines()
        except OSError:
            continue
        for line in lines:
            if not line.strip():
                continue
            try:
                event = json.loads(line)
            except ValueError:
                continue
            if str(event.get("event_type") or "") in JOURNAL_TERMINAL_EVENT_TYPES:
                return True
    return False


def _timeline_terminal_event(
    session: dict[str, Any],
    *,
    task_executions: list[dict[str, Any]],
    metadata_rows: list[dict[str, Any]],
    enabled: bool,
) -> dict[str, Any]:
    if not enabled:
        return {"checked": False, "has_terminal_event": None, "error": None}

    terminal_reason = _latest_terminal_reason(metadata_rows)
    if terminal_reason in TERMINAL_REASON_PRIORITY:
        return {"checked": True, "has_terminal_event": True, "error": None}

    latest_failed_execution = next(
        (
            row
            for row in reversed(task_executions)
            if _status(row.get("status")) in FAILED_EXECUTION_STATUSES
        ),
        None,
    )
    if (
        latest_failed_execution
        and _status(latest_failed_execution.get("status")) == "cancelled"
    ):
        return {"checked": True, "has_terminal_event": True, "error": None}

    task_ids = {
        int(row["task_id"]) for row in task_executions if row.get("task_id") is not None
    }
    has_journal_terminal = _journal_has_terminal_event(
        _journal_paths(session, task_ids)
    )
    return {
        "checked": True,
        "has_terminal_event": has_journal_terminal,
        "error": None,
    }


def _session_report(
    conn: sqlite3.Connection,
    *,
    session: dict[str, Any],
    check_timeline: bool,
) -> dict[str, Any]:
    session_id = int(session["id"])
    task_executions = _rows(
        conn,
        """
        select id, task_id, attempt_number, status, started_at, completed_at, created_at
        from task_executions
        where session_id = ?
        order by id
        """,
        (session_id,),
    )
    metadata_rows = _rows(
        conn,
        """
        select id, task_id, task_execution_id, level, message, log_metadata, created_at
        from log_entries
        where session_id = ?
          and log_metadata is not null
        order by id desc
        """,
        (session_id,),
    )
    runtime_logs = (
        _one(
            conn,
            f"""
        select count(*) as total,
               sum(case when task_execution_id is not null then 1 else 0 end) as with_execution_id,
               sum(case when task_execution_id is null then 1 else 0 end) as missing_execution_id
        from log_entries
        where session_id = ?
          and {RUNTIME_LOG_FILTER}
        """,
            (session_id,),
        )
        or {"total": 0, "with_execution_id": 0, "missing_execution_id": 0}
    )
    failure_summary = _one(
        conn,
        """
        select id, generated_at
        from execution_failure_summaries
        where session_id = ?
        """,
        (session_id,),
    )

    evidence = _metadata_evidence(metadata_rows)
    terminal_class = _terminal_class(
        session=session,
        task_executions=task_executions,
        metadata_rows=metadata_rows,
    )
    failed_execution_count = sum(
        1
        for row in task_executions
        if _status(row.get("status")) in FAILED_EXECUTION_STATUSES
    )
    terminal = terminal_class != "DONE" and _status(session.get("status")) in (
        TERMINAL_SESSION_STATUSES | {"failed"}
    )
    terminal = terminal or (terminal_class != "DONE" and failed_execution_count > 0)
    failure_summary_explains = bool(
        failure_summary or evidence["diagnostic_reason"] or terminal_class == "DONE"
    )
    timeline = _timeline_terminal_event(
        session,
        task_executions=task_executions,
        metadata_rows=metadata_rows,
        enabled=check_timeline and terminal,
    )

    return {
        "session_id": session_id,
        "session_name": session.get("name"),
        "project_id": session.get("project_id"),
        "project_name": session.get("project_name"),
        "status": session.get("status"),
        "created_at": session.get("created_at"),
        "started_at": session.get("started_at"),
        "stopped_at": session.get("stopped_at"),
        "terminal_class": terminal_class,
        "repair_used": bool(evidence["repair_used"]),
        "second_repair_used": bool(evidence["second_repair_used"]),
        "avg_planning_duration_seconds": _mean_or_none(evidence["planning_durations"]),
        "avg_repair_duration_seconds": _mean_or_none(evidence["repair_durations"]),
        "failure_summary_explains": failure_summary_explains,
        "failure_summary_cached": bool(failure_summary),
        "failure_diagnostic_reason": evidence["diagnostic_reason"],
        "decision_timeline": timeline,
        "runtime_logs": {
            "total": runtime_logs["total"] or 0,
            "with_task_execution_id": runtime_logs["with_execution_id"] or 0,
            "missing_task_execution_id": runtime_logs["missing_execution_id"] or 0,
        },
        "task_execution_count": len(task_executions),
        "failed_task_execution_count": failed_execution_count,
        "terminal": terminal,
    }


def build_report(
    conn: sqlite3.Connection,
    *,
    limit: int,
    check_timeline: bool,
) -> dict[str, Any]:
    sessions = _rows(
        conn,
        """
        select s.id, s.project_id, s.name, s.status, s.is_active,
               s.created_at, s.started_at, s.stopped_at,
               p.name as project_name, p.workspace_path
        from sessions s
        left join projects p on p.id = s.project_id
        where s.deleted_at is null
        order by s.id desc
        limit ?
        """,
        (limit,),
    )
    session_reports = [
        _session_report(
            conn,
            session=session,
            check_timeline=check_timeline,
        )
        for session in sessions
    ]

    terminal_counts = Counter(row["terminal_class"] for row in session_reports)
    runtime_total = sum(row["runtime_logs"]["total"] for row in session_reports)
    runtime_missing = sum(
        row["runtime_logs"]["missing_task_execution_id"] for row in session_reports
    )
    terminal_failures = [row for row in session_reports if row["terminal"]]
    missing_timeline = [
        row
        for row in terminal_failures
        if row["decision_timeline"]["checked"]
        and row["decision_timeline"]["has_terminal_event"] is False
    ]

    return {
        "limit": limit,
        "sessions_analyzed": len(session_reports),
        "summary": {
            "done": terminal_counts.get("DONE", 0),
            "terminal_counts": dict(sorted(terminal_counts.items())),
            "repair_used_sessions": sum(
                1 for row in session_reports if row["repair_used"]
            ),
            "second_repair_used_sessions": sum(
                1 for row in session_reports if row["second_repair_used"]
            ),
            "avg_planning_duration_seconds": _mean_or_none(
                value
                for row in session_reports
                for value in [row["avg_planning_duration_seconds"]]
                if value is not None
            ),
            "avg_repair_duration_seconds": _mean_or_none(
                value
                for row in session_reports
                for value in [row["avg_repair_duration_seconds"]]
                if value is not None
            ),
            "missing_failure_summary_explanation": sum(
                1 for row in terminal_failures if not row["failure_summary_explains"]
            ),
            "missing_decision_timeline_terminal_event": len(missing_timeline),
            "runtime_logs_total": runtime_total,
            "runtime_logs_missing_task_execution_id": runtime_missing,
        },
        "sessions": session_reports,
    }


def _print_text(report: dict[str, Any]) -> None:
    summary = report["summary"]
    print("Recent Session Outcome Report")
    print("=============================")
    print(f"sessions_analyzed={report['sessions_analyzed']} limit={report['limit']}")
    print(f"done={summary['done']}")
    print(f"repair_used_sessions={summary['repair_used_sessions']}")
    print(f"second_repair_used_sessions={summary['second_repair_used_sessions']}")
    print(
        "avg_planning_duration_seconds=" f"{summary['avg_planning_duration_seconds']}"
    )
    print(f"avg_repair_duration_seconds={summary['avg_repair_duration_seconds']}")
    print(
        "missing_failure_summary_explanation="
        f"{summary['missing_failure_summary_explanation']}"
    )
    print(
        "missing_decision_timeline_terminal_event="
        f"{summary['missing_decision_timeline_terminal_event']}"
    )
    print(
        "runtime_logs="
        f"total={summary['runtime_logs_total']} "
        f"missing_task_execution_id={summary['runtime_logs_missing_task_execution_id']}"
    )
    print()
    print("Terminal Classes:")
    for terminal_class, count in summary["terminal_counts"].items():
        print(f"- {terminal_class}: {count}")
    print()
    print("Sessions:")
    for row in report["sessions"]:
        timeline = row["decision_timeline"]
        timeline_value = timeline["has_terminal_event"]
        if timeline["error"]:
            timeline_value = f"error: {timeline['error']}"
        elif not timeline["checked"]:
            timeline_value = "not_checked"
        print(
            f"- session={row['session_id']} status={row['status']} "
            f"terminal={row['terminal_class']} repair={row['repair_used']} "
            f"second_repair={row['second_repair_used']} "
            f"failure_summary={row['failure_summary_explains']} "
            f"timeline_terminal={timeline_value} "
            f"runtime_missing_ids={row['runtime_logs']['missing_task_execution_id']}"
        )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Summarize recent sessions by outcome and evidence coverage."
    )
    parser.add_argument("--db", default="orchestrator.db", help="Path to sqlite DB")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument(
        "--skip-timeline",
        action="store_true",
        help="Skip read-only decision timeline projection checks.",
    )
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    args = parser.parse_args()

    if args.limit <= 0:
        raise SystemExit("--limit must be greater than zero")

    with _connect(args.db) as conn:
        report = build_report(
            conn,
            limit=args.limit,
            check_timeline=not args.skip_timeline,
        )

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        _print_text(report)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
