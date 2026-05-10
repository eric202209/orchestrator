#!/usr/bin/env python3
"""Summarize Phase 7L workspace evidence effectiveness from read-only logs."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
import os
from pathlib import Path
import sqlite3
from statistics import mean
import sys
from typing import Any

DEBUG_FEEDBACK = "debug_feedback_captured"
WORKSPACE_EVIDENCE = "workspace_evidence_collected"
DEBUG_REPAIR_ATTEMPTED = "debug_repair_attempted"
REPAIR_REJECTED = "repair_rejected"
REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.failure_taxonomy import (  # noqa: E402
    failure_class as extract_failure_class,
    parse_log_metadata,
    status_key,
)

DEFAULT_WORKSPACE_ROOT = REPO_ROOT.parent


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(
    conn: sqlite3.Connection, query: str, params: tuple[Any, ...] = ()
) -> list[dict[str, Any]]:
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def _status(value: Any) -> str:
    return status_key(value)


def _mean(values: list[int]) -> float:
    return round(mean(values), 2) if values else 0.0


def _event_type(metadata: dict[str, Any]) -> str:
    return str(metadata.get("event_type") or "").strip()


def _execution_context(
    conn: sqlite3.Connection, task_execution_id: int
) -> dict[str, Any]:
    row = conn.execute(
        """
        select te.status as task_execution_status,
               te.session_id,
               te.task_id,
               t.status as task_status,
               s.status as session_status,
               p.workspace_path,
               p.name as project_name
        from task_executions te
        join tasks t on t.id = te.task_id
        join sessions s on s.id = te.session_id
        join projects p on p.id = t.project_id
        where te.id = ?
        """,
        (task_execution_id,),
    ).fetchone()
    if row is None:
        return {
            "task_execution_status": "unknown",
            "task_status": "unknown",
            "session_status": "unknown",
        }
    return dict(row)


def _resolve_workspace_path(workspace_path: Any) -> Path | None:
    raw = str(workspace_path or "").strip()
    if not raw:
        return None
    path = Path(raw)
    if path.is_absolute():
        return path
    workspace_root = Path(
        os.environ.get("OPENCLAW_WORKSPACE", str(DEFAULT_WORKSPACE_ROOT))
    )
    return workspace_root / raw


def _journal_events(context: dict[str, Any]) -> list[dict[str, Any]]:
    workspace_path = _resolve_workspace_path(context.get("workspace_path"))
    if workspace_path is None:
        return []
    path = (
        workspace_path
        / ".openclaw"
        / "events"
        / f"session_{context['session_id']}_task_{context['task_id']}.jsonl"
    )
    if not path.exists():
        return []
    events: list[dict[str, Any]] = []
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError:
        return []
    for line in lines:
        if not line.strip():
            continue
        try:
            event = json.loads(line)
        except ValueError:
            continue
        if isinstance(event, dict):
            events.append(event)
    return events


def _journal_details(
    events: list[dict[str, Any]], event_type: str
) -> list[dict[str, Any]]:
    details: list[dict[str, Any]] = []
    for event in events:
        if str(event.get("event_type") or "") != event_type:
            continue
        payload = event.get("details")
        if isinstance(payload, dict):
            details.append(payload)
    return details


def _recent_task_execution_ids(conn: sqlite3.Connection, limit: int) -> list[int]:
    rows = _rows(
        conn,
        """
        select distinct le.task_execution_id, max(le.id) as latest_log_id
        from log_entries le
        where le.task_execution_id is not null
          and le.log_metadata is not null
          and (
            le.log_metadata like '%debug_feedback_captured%'
            or le.log_metadata like '%workspace_evidence_collected%'
            or le.log_metadata like '%debug_repair_attempted%'
          )
        group by le.task_execution_id
        order by latest_log_id desc
        limit ?
        """,
        (limit,),
    )
    return [int(row["task_execution_id"]) for row in rows]


def _metadata_rows(
    conn: sqlite3.Connection, task_execution_id: int
) -> list[dict[str, Any]]:
    return _rows(
        conn,
        """
        select id, message, log_metadata
        from log_entries
        where task_execution_id = ?
          and log_metadata is not null
        order by id asc
        """,
        (task_execution_id,),
    )


def summarize(conn: sqlite3.Connection, limit: int) -> dict[str, Any]:
    task_execution_ids = _recent_task_execution_ids(conn, limit)
    records: list[dict[str, Any]] = []

    for task_execution_id in task_execution_ids:
        metadata_rows = _metadata_rows(conn, task_execution_id)
        parsed_rows = [
            parse_log_metadata(row.get("log_metadata")) for row in metadata_rows
        ]
        debug_rows = [
            metadata
            for metadata in parsed_rows
            if _event_type(metadata) == DEBUG_FEEDBACK
            or metadata.get("debug_feedback_captured")
        ]
        evidence_rows = [
            metadata
            for metadata in parsed_rows
            if _event_type(metadata) == WORKSPACE_EVIDENCE
        ]
        repair_attempt_rows = [
            metadata
            for metadata in parsed_rows
            if _event_type(metadata) == DEBUG_REPAIR_ATTEMPTED
            or metadata.get("debug_repair_attempted") is True
        ]
        repair_rejected_rows = [
            metadata
            for metadata in parsed_rows
            if _event_type(metadata) == REPAIR_REJECTED
        ]
        context = _execution_context(conn, task_execution_id)
        journal_events = _journal_events(context)
        journal_evidence_rows = _journal_details(journal_events, WORKSPACE_EVIDENCE)
        journal_repair_attempt_rows = _journal_details(
            journal_events, DEBUG_REPAIR_ATTEMPTED
        )
        evidence_rows = [*evidence_rows, *journal_evidence_rows]
        repair_attempt_rows = [*repair_attempt_rows, *journal_repair_attempt_rows]
        final_status = context
        debug_feedback = debug_rows[-1] if debug_rows else {}
        if not evidence_rows and (
            debug_feedback.get("evidence_capsule_used")
            or int(debug_feedback.get("evidence_chars_total") or 0) > 0
        ):
            evidence_rows = [
                {
                    "failure_class": extract_failure_class(debug_feedback),
                    "evidence_chars_total": int(
                        debug_feedback.get("evidence_chars_total") or 0
                    ),
                    "commands_run": debug_feedback.get("commands_run") or [],
                    "evidence_files_inspected": debug_feedback.get(
                        "evidence_files_inspected"
                    )
                    or [],
                }
            ]
        class_name = extract_failure_class(
            debug_rows[-1] if debug_rows else evidence_rows[-1] if evidence_rows else {}
        )
        evidence_chars = sum(
            int(metadata.get("evidence_chars_total") or 0) for metadata in evidence_rows
        )
        evidence_commands: list[str] = []
        evidence_files: list[str] = []
        for metadata in evidence_rows:
            evidence_commands.extend(
                str(item)
                for item in (metadata.get("commands_run") or [])
                if str(item).strip()
            )
            evidence_files.extend(
                str(item)
                for item in (metadata.get("evidence_files_inspected") or [])
                if str(item).strip()
            )

        capsule_used = bool(debug_feedback.get("evidence_capsule_used"))
        collected = bool(evidence_rows)
        command_count = len(evidence_commands)
        localization_count = len(evidence_files)
        evidence_empty = bool(debug_rows) and not capsule_used and not collected
        evidence_partial = collected and command_count > localization_count
        repair_attempted = bool(repair_attempt_rows)
        repair_rejected = bool(repair_rejected_rows)
        final_task_status = _status(final_status.get("task_status"))
        final_execution_status = _status(final_status.get("task_execution_status"))
        repair_success = repair_attempted and (
            final_task_status == "done" or final_execution_status == "done"
        )

        records.append(
            {
                "task_execution_id": task_execution_id,
                "failure_class": class_name,
                "workspace_evidence_collected": collected,
                "workspace_evidence_partial": evidence_partial,
                "workspace_evidence_empty": evidence_empty,
                "evidence_command_count": command_count,
                "evidence_total_chars": evidence_chars,
                "evidence_localization_count": localization_count,
                "debug_prompt_mode": _latest_value(
                    repair_attempt_rows, "debug_prompt_mode"
                ),
                "completion_repair_prompt_mode": _latest_value(
                    repair_attempt_rows, "completion_repair_prompt_mode"
                ),
                "repair_attempted": repair_attempted,
                "repair_rejected": repair_rejected,
                "repair_success": repair_success,
                "final_task_status": final_task_status,
                "final_task_execution_status": final_execution_status,
                "final_session_status": _status(final_status.get("session_status")),
                "evidence_commands": evidence_commands,
                "evidence_files": evidence_files,
            }
        )

    return _aggregate(records, limit)


def _latest_value(rows: list[dict[str, Any]], key: str) -> str | None:
    for row in reversed(rows):
        value = row.get(key)
        if value:
            return str(value)
    return None


def _aggregate(records: list[dict[str, Any]], limit: int) -> dict[str, Any]:
    failure_classes = Counter(record["failure_class"] for record in records)
    records_with_evidence = [
        record for record in records if record["workspace_evidence_collected"]
    ]
    records_without_evidence = [
        record
        for record in records
        if record["repair_attempted"] and not record["workspace_evidence_collected"]
    ]
    repair_with_evidence = [
        record for record in records_with_evidence if record["repair_attempted"]
    ]
    repair_without_evidence = records_without_evidence
    command_counter: Counter[str] = Counter()
    file_counter: Counter[str] = Counter()
    for record in records:
        command_counter.update(record["evidence_commands"])
        file_counter.update(record["evidence_files"])

    by_failure_class: dict[str, dict[str, Any]] = {}
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        grouped[record["failure_class"]].append(record)
    for failure_class, class_records in sorted(grouped.items()):
        by_failure_class[failure_class] = {
            "total": len(class_records),
            "evidence_collected": sum(
                1 for record in class_records if record["workspace_evidence_collected"]
            ),
            "evidence_empty": sum(
                1 for record in class_records if record["workspace_evidence_empty"]
            ),
            "repair_success": sum(
                1 for record in class_records if record["repair_success"]
            ),
        }

    return {
        "limit": limit,
        "task_execution_count": len(records),
        "workspace_evidence_collected": len(records_with_evidence),
        "workspace_evidence_empty": sum(
            1 for record in records if record["workspace_evidence_empty"]
        ),
        "workspace_evidence_partial": sum(
            1 for record in records if record["workspace_evidence_partial"]
        ),
        "average_evidence_chars": _mean(
            [record["evidence_total_chars"] for record in records_with_evidence]
        ),
        "failure_classes": dict(failure_classes),
        "by_failure_class": by_failure_class,
        "repair_success_with_evidence": _ratio(repair_with_evidence),
        "repair_success_without_evidence": _ratio(repair_without_evidence),
        "top_evidence_commands": command_counter.most_common(10),
        "top_evidence_files": file_counter.most_common(10),
        "timeline_event_coverage": {
            "debug_feedback_captured": sum(
                1 for record in records if record["failure_class"] != "unknown"
            ),
            "workspace_evidence_collected": len(records_with_evidence),
            "debug_repair_attempted": sum(
                1 for record in records if record["repair_attempted"]
            ),
        },
        "records": records,
    }


def _ratio(records: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(records)
    successes = sum(1 for record in records if record["repair_success"])
    rate = round(successes / total, 3) if total else 0.0
    return {"successes": successes, "total": total, "rate": rate}


def print_report(summary: dict[str, Any]) -> None:
    print("Workspace Evidence Report")
    print(f"TaskExecutions analyzed: {summary['task_execution_count']}")
    print(
        "Evidence: "
        f"collected={summary['workspace_evidence_collected']} "
        f"empty={summary['workspace_evidence_empty']} "
        f"partial={summary['workspace_evidence_partial']} "
        f"avg_chars={summary['average_evidence_chars']}"
    )
    print("Failure classes:")
    for failure_class, data in summary["by_failure_class"].items():
        print(
            f"- {failure_class}: total={data['total']} "
            f"collected={data['evidence_collected']} "
            f"empty={data['evidence_empty']} "
            f"repair_success={data['repair_success']}"
        )
    print("Repair success:")
    with_ev = summary["repair_success_with_evidence"]
    without_ev = summary["repair_success_without_evidence"]
    print(
        f"- with evidence: {with_ev['successes']}/{with_ev['total']} "
        f"({with_ev['rate']})"
    )
    print(
        f"- without evidence: {without_ev['successes']}/{without_ev['total']} "
        f"({without_ev['rate']})"
    )
    print("Top evidence commands:")
    for command, count in summary["top_evidence_commands"]:
        print(f"- {count}x {command}")
    print("Top evidence files:")
    for file_path, count in summary["top_evidence_files"]:
        print(f"- {count}x {file_path}")
    coverage = summary["timeline_event_coverage"]
    print(
        "Timeline/event coverage: "
        f"debug_feedback={coverage['debug_feedback_captured']} "
        f"workspace_evidence={coverage['workspace_evidence_collected']} "
        f"debug_repair={coverage['debug_repair_attempted']}"
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Summarize workspace evidence collection and repair outcomes."
    )
    parser.add_argument("--db", default="orchestrator.db")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    conn = _connect(args.db)
    try:
        summary = summarize(conn, args.limit)
    finally:
        conn.close()

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print_report(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
