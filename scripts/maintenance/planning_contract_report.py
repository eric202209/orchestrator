#!/usr/bin/env python3
"""Summarize planning contract failures and repair outcomes from read-only logs."""

from __future__ import annotations

import argparse
from collections import Counter, defaultdict
import json
from pathlib import Path
import re
import sqlite3
import sys
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from scripts.failure_taxonomy import (  # noqa: E402
    contract_reason,
    parse_log_metadata,
    status_key,
    terminal_reason_from_rows,
)

try:
    from app.services.orchestration.task_rules import get_workflow_profile
except Exception:  # pragma: no cover - fallback for damaged local envs
    get_workflow_profile = None  # type: ignore[assignment]


PLANNING_CONTRACT_MESSAGE = (
    "[OPENCLAW][PLANNING_DIAGNOSTICS] contract violation detected"
)
DIAGNOSTIC_THRESHOLD_DEFAULT = 3


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


def _recent_task_execution_ids(conn: sqlite3.Connection, limit: int) -> list[int]:
    rows = _rows(
        conn,
        """
        select distinct le.task_execution_id, max(le.id) as latest_log_id
        from log_entries le
        where le.task_execution_id is not null
          and le.log_metadata is not null
          and (
            le.log_metadata like '%"phase": "planning"%'
            or le.log_metadata like '%"diagnostic_label": "PLANNING"%'
            or le.message like '%PLANNING_DIAGNOSTICS%'
            or le.message like '%Planning repair%'
            or le.message like '%planning validation%'
          )
        group by le.task_execution_id
        order by latest_log_id desc
        limit ?
        """,
        (limit,),
    )
    return [int(row["task_execution_id"]) for row in rows]


def _execution_context(
    conn: sqlite3.Connection, task_execution_id: int
) -> dict[str, Any]:
    row = conn.execute(
        """
        select te.status as task_execution_status,
               te.session_id,
               te.task_id,
               t.title as task_title,
               t.description as task_description,
               t.status as task_status,
               t.execution_profile,
               t.error_message,
               s.status as session_status,
               p.name as project_name,
               p.workspace_path
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
            "execution_profile": "unknown",
        }
    return dict(row)


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


def _workflow_profile(context: dict[str, Any]) -> str:
    if get_workflow_profile is None:
        return _fallback_workflow_profile(context)
    return str(
        get_workflow_profile(
            str(context.get("execution_profile") or "full_lifecycle"),
            context.get("task_title"),
            context.get("task_description"),
        )
    )


def _fallback_workflow_profile(context: dict[str, Any]) -> str:
    execution_profile = str(context.get("execution_profile") or "full_lifecycle")
    if execution_profile in {"review_only", "debug_only"}:
        return execution_profile
    text = " ".join(
        [
            str(context.get("task_title") or ""),
            str(context.get("task_description") or ""),
        ]
    ).lower()
    has_frontend = _has_stack_marker(
        text, ("frontend", "front end", "react", "vite", "next.js", "nextjs")
    )
    has_backend = _has_stack_marker(
        text, ("backend", "fastapi", "django", "flask", "express", "node.js", "api")
    )
    scaffold_markers = (
        "set up",
        "setup",
        "scaffold",
        "bootstrap",
        "create",
        "build",
        "clean architecture",
    )
    if any(marker in text for marker in scaffold_markers):
        if has_frontend and has_backend:
            return "fullstack_scaffold"
        if has_frontend:
            return "frontend_only"
        if has_backend:
            return "backend_only"
    return "default"


def _has_stack_marker(text: str, markers: tuple[str, ...]) -> bool:
    return any(marker in text for marker in markers) and not _contains_negated_marker(
        text, markers
    )


def _contains_negated_marker(text: str, markers: tuple[str, ...]) -> bool:
    for marker in markers:
        escaped = re.escape(marker)
        patterns = (
            rf"\bdo\s+not\s+(?:create|build|add|include|use|make|set\s+up|setup)\s+(?:a\s+|an\s+)?{escaped}\b",
            rf"\bdon't\s+(?:create|build|add|include|use|make|set\s+up|setup)\s+(?:a\s+|an\s+)?{escaped}\b",
            rf"\bwithout\s+(?:a\s+|an\s+)?{escaped}\b",
            rf"\bno\s+(?:new\s+)?{escaped}\b",
        )
        if any(re.search(pattern, text) for pattern in patterns):
            return True
    return False


def summarize(
    conn: sqlite3.Connection,
    *,
    limit: int,
    diagnostic_threshold: int = DIAGNOSTIC_THRESHOLD_DEFAULT,
) -> dict[str, Any]:
    records: list[dict[str, Any]] = []
    for task_execution_id in _recent_task_execution_ids(conn, limit):
        context = _execution_context(conn, task_execution_id)
        metadata_rows = _metadata_rows(conn, task_execution_id)
        parsed_rows = [
            {**row, "metadata": parse_log_metadata(row.get("log_metadata"))}
            for row in metadata_rows
        ]
        contract_rows = [
            row
            for row in parsed_rows
            if row["message"] == PLANNING_CONTRACT_MESSAGE
            or row["metadata"].get("contract_violation_type")
        ]
        repair_start_rows = [
            row
            for row in parsed_rows
            if row["metadata"].get("phase") == "planning"
            and (
                row["metadata"].get("attempt") == "repair"
                and "attempt is now running" in str(row["message"])
            )
        ]
        repair_completed_rows = [
            row
            for row in parsed_rows
            if row["metadata"].get("phase") == "planning"
            and row["metadata"].get("attempt") == "repair"
            and "Planning repair completed" in str(row["message"])
        ]
        generated_plan = any(
            row["metadata"].get("phase") == "planning"
            and "Generated" in str(row["message"])
            and "steps in plan" in str(row["message"])
            for row in parsed_rows
        )
        saved_plan_reused = any(
            row["metadata"].get("phase") == "planning"
            and row["metadata"].get("source") == "stored_task_plan"
            for row in parsed_rows
        )
        planning_completed = generated_plan or saved_plan_reused
        initial_planning_seen = any(
            row["metadata"].get("planning_attempt") == "initial"
            or row["metadata"].get("diagnostic_label") == "PLANNING"
            or row["metadata"].get("phase_state") == "planning_response_received"
            or row["metadata"].get("source") == "stored_task_plan"
            for row in parsed_rows
        )
        contract_reasons = [contract_reason(row["metadata"]) for row in contract_rows]
        semantic_codes: list[str] = []
        brittle_subcodes: list[str] = []
        truncated_subcodes: list[str] = []
        shadow_warning_rule_ids: list[str] = []
        for row in contract_rows:
            metadata = row["metadata"]
            semantic_codes.extend(
                str(code)
                for code in (metadata.get("semantic_violation_codes") or [])
                if str(code).strip()
            )
            brittle_subcodes.extend(
                str(code)
                for code in (metadata.get("brittle_command_subcodes") or [])
                if str(code).strip()
            )
            truncated_subcodes.extend(
                str(code)
                for code in (metadata.get("truncated_multistep_subcodes") or [])
                if str(code).strip()
            )
            for warning in metadata.get("shadow_warnings") or []:
                if not isinstance(warning, dict):
                    continue
                rule_id = str(warning.get("rule_id") or "").strip()
                if rule_id:
                    shadow_warning_rule_ids.append(rule_id)
        repair_attempt_count = len(repair_start_rows)
        record = {
            "task_execution_id": task_execution_id,
            "session_id": context.get("session_id"),
            "task_id": context.get("task_id"),
            "project_name": context.get("project_name"),
            "task_title": context.get("task_title"),
            "execution_profile": context.get("execution_profile"),
            "workflow_profile": _workflow_profile(context),
            "initial_planning_seen": initial_planning_seen,
            "initial_contract_failed": bool(contract_rows),
            "planning_completed": planning_completed,
            "saved_plan_reused": saved_plan_reused,
            "planning_repair_count": repair_attempt_count,
            "planning_repair_completed_count": len(repair_completed_rows),
            "planning_repair_recovered": repair_attempt_count > 0
            and planning_completed,
            "contract_reasons": contract_reasons,
            "semantic_violation_codes": sorted(set(semantic_codes)),
            "brittle_command_subcodes": sorted(set(brittle_subcodes)),
            "truncated_multistep_subcodes": sorted(set(truncated_subcodes)),
            "shadow_warning_rule_ids": sorted(set(shadow_warning_rule_ids)),
            "terminal_reason": terminal_reason_from_rows(metadata_rows, context),
            "final_task_status": _status(context.get("task_status")),
            "final_task_execution_status": _status(
                context.get("task_execution_status")
            ),
            "final_session_status": _status(context.get("session_status")),
        }
        records.append(record)

    return _aggregate(records, diagnostic_threshold)


def _aggregate(
    records: list[dict[str, Any]], diagnostic_threshold: int
) -> dict[str, Any]:
    reason_to_task_executions: dict[str, set[int]] = defaultdict(set)
    for record in records:
        for reason in record["contract_reasons"]:
            reason_to_task_executions[reason].add(int(record["task_execution_id"]))

    reason_counts = {
        reason: len(task_execution_ids)
        for reason, task_execution_ids in reason_to_task_executions.items()
    }
    diagnostic_candidates = {
        reason: count
        for reason, count in sorted(
            reason_counts.items(), key=lambda item: (-item[1], item[0])
        )
        if count >= diagnostic_threshold
    }
    workflow_profiles = Counter(str(record["workflow_profile"]) for record in records)
    terminal_reasons = Counter(
        str(record["terminal_reason"])
        for record in records
        if str(record["terminal_reason"]).strip()
    )
    shadow_warning_rule_counts = Counter(
        rule_id
        for record in records
        for rule_id in (record.get("shadow_warning_rule_ids") or [])
    )
    recovered_records = [
        record for record in records if record["planning_repair_recovered"]
    ]

    return {
        "task_execution_count": len(records),
        "planning_completed": sum(
            1 for record in records if record["planning_completed"]
        ),
        "initial_contract_failed": sum(
            1 for record in records if record["initial_contract_failed"]
        ),
        "planning_repair_attempted": sum(
            1 for record in records if record["planning_repair_count"] > 0
        ),
        "planning_repair_recovered": sum(
            1 for record in records if record["planning_repair_recovered"]
        ),
        "contract_reason_counts": dict(
            sorted(reason_counts.items(), key=lambda item: (-item[1], item[0]))
        ),
        "diagnostic_change_threshold": diagnostic_threshold,
        "diagnostic_change_candidates": diagnostic_candidates,
        "workflow_profiles": dict(workflow_profiles),
        "terminal_reasons": dict(terminal_reasons),
        "shadow_warning_rule_counts": dict(
            sorted(
                shadow_warning_rule_counts.items(), key=lambda item: (-item[1], item[0])
            )
        ),
        "recovered_outcomes": _recovered_outcomes(recovered_records),
        "recovered_outcomes_by_contract_reason": _recovered_outcomes_by_key(
            recovered_records, _record_contract_reason_keys
        ),
        "recovered_outcomes_by_workflow_profile": _recovered_outcomes_by_key(
            recovered_records, lambda record: [str(record["workflow_profile"])]
        ),
        "records": records,
    }


def _record_contract_reason_keys(record: dict[str, Any]) -> list[str]:
    reasons = [str(reason) for reason in record.get("contract_reasons") or []]
    return reasons or ["no_contract_reason"]


def _recovered_outcomes(records: list[dict[str, Any]]) -> dict[str, Any]:
    total = len(records)
    done = [record for record in records if _record_is_done(record)]
    not_done = [record for record in records if not _record_is_done(record)]
    terminal_reasons = Counter(
        str(record["terminal_reason"])
        for record in not_done
        if str(record["terminal_reason"]).strip()
    )
    return {
        "total": total,
        "done": len(done),
        "not_done": len(not_done),
        "done_rate": round(len(done) / total, 3) if total else 0.0,
        "terminal_reasons": dict(
            sorted(terminal_reasons.items(), key=lambda item: (-item[1], item[0]))
        ),
        "not_done_task_executions": [
            int(record["task_execution_id"]) for record in not_done
        ],
    }


def _record_is_done(record: dict[str, Any]) -> bool:
    return (
        str(record.get("final_task_status") or "").lower() == "done"
        or str(record.get("final_task_execution_status") or "").lower() == "done"
    )


def _recovered_outcomes_by_key(records: list[dict[str, Any]], key_fn: Any) -> dict:
    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for record in records:
        for key in key_fn(record):
            grouped[str(key)].append(record)
    return {key: _recovered_outcomes(grouped[key]) for key in sorted(grouped)}


def print_report(summary: dict[str, Any]) -> None:
    print("Planning Contract Report")
    print(f"TaskExecutions analyzed: {summary['task_execution_count']}")
    print(
        "Planning: "
        f"completed={summary['planning_completed']} "
        f"initial_contract_failed={summary['initial_contract_failed']} "
        f"repair_attempted={summary['planning_repair_attempted']} "
        f"repair_recovered={summary['planning_repair_recovered']}"
    )
    print("Workflow profiles:")
    for profile, count in sorted(summary["workflow_profiles"].items()):
        print(f"- {profile}: {count}")
    print("Contract reasons:")
    for reason, count in summary["contract_reason_counts"].items():
        print(f"- {count}x {reason}")
    print(
        "Diagnostic change candidates "
        f"(>= {summary['diagnostic_change_threshold']} distinct TaskExecutions):"
    )
    if summary["diagnostic_change_candidates"]:
        for reason, count in summary["diagnostic_change_candidates"].items():
            print(f"- {count}x {reason}")
    else:
        print("- none")
    recovered = summary["recovered_outcomes"]
    print(
        "Recovered planning outcomes: "
        f"done={recovered['done']}/{recovered['total']} "
        f"not_done={recovered['not_done']} "
        f"done_rate={recovered['done_rate']}"
    )
    print("Recovered outcomes by contract reason:")
    for reason, data in summary["recovered_outcomes_by_contract_reason"].items():
        print(
            f"- {reason}: done={data['done']}/{data['total']} "
            f"not_done={data['not_done']} done_rate={data['done_rate']}"
        )
    print("Recovered outcomes by workflow profile:")
    for profile, data in summary["recovered_outcomes_by_workflow_profile"].items():
        print(
            f"- {profile}: done={data['done']}/{data['total']} "
            f"not_done={data['not_done']} done_rate={data['done_rate']}"
        )
    print("Terminal reasons:")
    for reason, count in sorted(summary["terminal_reasons"].items()):
        print(f"- {count}x {reason}")
    print("Shadow warning rules:")
    if summary["shadow_warning_rule_counts"]:
        for rule_id, count in summary["shadow_warning_rule_counts"].items():
            print(f"- {count}x {rule_id}")
    else:
        print("- none")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Summarize planning contract failures and repair outcomes."
    )
    parser.add_argument("--db", default="orchestrator.db")
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--diagnostic-threshold", type=int, default=3)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    conn = _connect(args.db)
    try:
        summary = summarize(
            conn,
            limit=args.limit,
            diagnostic_threshold=args.diagnostic_threshold,
        )
    finally:
        conn.close()

    if args.json:
        print(json.dumps(summary, indent=2, sort_keys=True))
    else:
        print_report(summary)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
