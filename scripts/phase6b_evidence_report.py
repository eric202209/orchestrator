#!/usr/bin/env python3
"""Build a read-only Phase 6B operational evidence report."""

from __future__ import annotations

import argparse
import json
import sqlite3
import sys
import urllib.request
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(REPO_ROOT))


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn: sqlite3.Connection, query: str, params: tuple[Any, ...]) -> list[dict]:
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def _parse_csv_ints(value: str | None) -> set[int]:
    if not value:
        return set()
    return {int(item.strip()) for item in value.split(",") if item.strip()}


def _event_journal_path(project_dir: Path, session_id: int, task_id: int) -> Path:
    return (
        project_dir
        / ".openclaw"
        / "events"
        / f"session_{session_id}_task_{task_id}.jsonl"
    )


def _event_journal_report(project_dir: Path, session_id: int, task_id: int) -> dict:
    path = _event_journal_path(project_dir, session_id, task_id)
    if not path.exists():
        return {"path": str(path), "exists": False, "event_count": 0}

    event_count = 0
    malformed_count = 0
    last_event: dict[str, Any] | None = None
    for line in path.read_text(encoding="utf-8").splitlines():
        if not line.strip():
            continue
        try:
            last_event = json.loads(line)
            event_count += 1
        except json.JSONDecodeError:
            malformed_count += 1
    return {
        "path": str(path),
        "exists": True,
        "event_count": event_count,
        "malformed_count": malformed_count,
        "last_event_type": (last_event or {}).get("event_type"),
        "last_event_id": (last_event or {}).get("event_id"),
    }


def _replay_report(project_dir: Path, session_id: int, task_id: int) -> dict:
    from app.services.orchestration.replay import reconstruct_execution_state

    report = reconstruct_execution_state(
        project_dir=project_dir,
        session_id=session_id,
        task_id=task_id,
    )
    integrity = report.get("integrity") or {}
    state = report.get("state") or {}
    return {
        "evidence_returned": bool(integrity.get("event_count_read")),
        "event_count_read": integrity.get("event_count_read"),
        "event_count_applied": integrity.get("event_count_applied"),
        "confidence": integrity.get("confidence"),
        "phase": state.get("phase"),
        "status": state.get("status"),
        "latest_failure_event_id": state.get("latest_failure_event_id"),
    }


def _http_json(url: str, token: str | None) -> dict[str, Any]:
    request = urllib.request.Request(url, headers={"Accept": "application/json"})
    if token:
        request.add_header("Authorization", f"Bearer {token}")
    with urllib.request.urlopen(request, timeout=15) as response:
        return json.loads(response.read().decode("utf-8"))


def build_report(
    conn: sqlite3.Connection,
    *,
    project_id: int,
    session_id: int | None,
    task_id: int | None,
    expected_session_ids: set[int],
    max_session_id_before: int | None,
    since_log_id: int,
    project_dir: Path | None,
    api_base: str | None,
    api_token: str | None,
    require_failed_rerun: bool = False,
) -> dict[str, Any]:
    live_sessions = _rows(
        conn,
        """
        select id, name, status, is_active, created_at, started_at, stopped_at
        from sessions
        where project_id = ? and deleted_at is null
        order by id
        """,
        (project_id,),
    )
    live_session_ids = {int(row["id"]) for row in live_sessions}
    unexpected_sessions = sorted(live_session_ids - expected_session_ids)
    if max_session_id_before is not None:
        unexpected_sessions = sorted(
            set(unexpected_sessions)
            | {sid for sid in live_session_ids if sid > max_session_id_before}
        )

    bad_running_tasks = _rows(
        conn,
        """
        select t.id, t.title, t.status
        from tasks t
        where t.project_id = ?
          and t.status = 'RUNNING'
          and not exists (
            select 1
            from task_executions te
            where te.task_id = t.id and te.status = 'RUNNING'
          )
        order by t.id
        """,
        (project_id,),
    )
    stopped_running_executions = _rows(
        conn,
        """
        select s.id as session_id, s.status as session_status,
               te.id as task_execution_id, te.task_id, te.status as execution_status
        from sessions s
        join task_executions te on te.session_id = s.id
        where s.project_id = ?
          and s.status in ('stopped', 'cancelled', 'deleted')
          and te.status = 'RUNNING'
        order by s.id, te.id
        """,
        (project_id,),
    )

    attempt_report: dict[str, Any] | None = None
    if session_id is not None and task_id is not None:
        attempts = _rows(
            conn,
            """
            select id, session_id, task_id, attempt_number, status, started_at, completed_at
            from task_executions
            where task_id = ?
            order by session_id, attempt_number
            """,
            (task_id,),
        )
        matching_attempts = [row for row in attempts if row["session_id"] == session_id]
        failed_matching_attempts = [
            row for row in matching_attempts if row["status"] == "FAILED"
        ]
        attempt_report = {
            "task_id": task_id,
            "expected_session_id": session_id,
            "attempt_count_in_session": len(matching_attempts),
            "failed_attempt_count_in_session": len(failed_matching_attempts),
            "all_attempt_session_ids": sorted({row["session_id"] for row in attempts}),
            "rerun_stays_in_expected_session": len(matching_attempts) > 1
            and bool(failed_matching_attempts),
            "attempts": matching_attempts,
        }

    log_filters = [
        "session_id = ?",
        "id >= ?",
        "(message like '%[OPENCLAW]%' or message like '%[PERFORMANCE]%')",
    ]
    log_params: list[Any] = [session_id, since_log_id]
    if session_id is None:
        log_filters[0] = "session_id in (select id from sessions where project_id = ?)"
        log_params[0] = project_id
    runtime_logs = conn.execute(
        f"""
        select count(*) as total,
               sum(case when task_execution_id is not null then 1 else 0 end) as with_execution_id,
               sum(case when task_execution_id is null then 1 else 0 end) as missing_execution_id
        from log_entries
        where {' and '.join(log_filters)}
        """,
        tuple(log_params),
    ).fetchone()

    journal = None
    replay = None
    if project_dir is not None and session_id is not None and task_id is not None:
        journal = _event_journal_report(project_dir, session_id, task_id)
        replay = _replay_report(project_dir, session_id, task_id)

    endpoints: dict[str, Any] = {}
    if api_base and session_id is not None:
        base = api_base.rstrip("/")
        checks = {
            "decision_timeline": f"{base}/api/v1/sessions/{session_id}/decision-timeline?limit=20",
        }
        if task_id is not None:
            checks["replay"] = (
                f"{base}/api/v1/sessions/{session_id}/replay?task_id={task_id}"
            )
            checks["events"] = (
                f"{base}/api/v1/sessions/{session_id}/tasks/{task_id}/events"
            )
        for name, url in checks.items():
            try:
                payload = _http_json(url, api_token)
                endpoints[name] = {
                    "ok": True,
                    "url": url,
                    "evidence_returned": bool(payload),
                }
            except Exception as exc:
                endpoints[name] = {"ok": False, "url": url, "error": str(exc)}

    runtime_log_total = runtime_logs["total"] or 0
    runtime_log_missing_execution_id = runtime_logs["missing_execution_id"] or 0
    checks = {
        "no_unexpected_sessions": not unexpected_sessions,
        "stopped_sessions_have_no_running_executions": not stopped_running_executions,
        "running_tasks_have_running_executions": not bad_running_tasks,
        "runtime_logs_have_task_execution_id": runtime_log_total > 0
        and runtime_log_missing_execution_id == 0,
    }
    if attempt_report is not None:
        failed_rerun_check = bool(attempt_report["rerun_stays_in_expected_session"])
        if require_failed_rerun or failed_rerun_check:
            checks["failed_task_rerun_stays_in_same_workflow_session"] = (
                failed_rerun_check
            )
    if journal is not None:
        checks["event_journal_returns_evidence"] = bool(
            journal["exists"] and journal["event_count"] > 0
        )
    if replay is not None:
        checks["replay_returns_evidence"] = bool(replay["evidence_returned"])
    if endpoints:
        checks["timeline_replay_endpoints_return_evidence"] = all(
            item.get("ok") and item.get("evidence_returned")
            for item in endpoints.values()
        )

    return {
        "project_id": project_id,
        "session_id": session_id,
        "task_id": task_id,
        "checks": checks,
        "pass": all(checks.values()),
        "live_sessions": live_sessions,
        "unexpected_sessions": unexpected_sessions,
        "bad_running_tasks": bad_running_tasks,
        "stopped_running_executions": stopped_running_executions,
        "attempt_report": attempt_report,
        "runtime_log_identity": dict(runtime_logs),
        "event_journal": journal,
        "replay": replay,
        "endpoints": endpoints,
    }


def write_json_report(report: dict[str, Any], output_path: Path) -> None:
    output_path.parent.mkdir(parents=True, exist_ok=True)
    output_path.write_text(json.dumps(report, indent=2, default=str) + "\n")


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run Phase 6B read-only evidence checks against real artifacts."
    )
    parser.add_argument("--db", default="orchestrator.db")
    parser.add_argument("--project-id", type=int, required=True)
    parser.add_argument("--session-id", type=int)
    parser.add_argument("--task-id", type=int)
    parser.add_argument("--project-dir")
    parser.add_argument("--expected-session-ids")
    parser.add_argument("--max-session-id-before", type=int)
    parser.add_argument("--since-log-id", type=int, default=0)
    parser.add_argument("--api-base")
    parser.add_argument("--api-token")
    parser.add_argument(
        "--require-failed-rerun",
        action="store_true",
        help="Require evidence that a failed task rerun stayed in this session.",
    )
    parser.add_argument("--json", action="store_true")
    parser.add_argument(
        "--json-output",
        help="Write the machine-readable evidence report to this JSON file.",
    )
    args = parser.parse_args()

    with _connect(args.db) as conn:
        report = build_report(
            conn,
            project_id=args.project_id,
            session_id=args.session_id,
            task_id=args.task_id,
            expected_session_ids=_parse_csv_ints(args.expected_session_ids),
            max_session_id_before=args.max_session_id_before,
            since_log_id=args.since_log_id,
            project_dir=Path(args.project_dir) if args.project_dir else None,
            api_base=args.api_base,
            api_token=args.api_token,
            require_failed_rerun=args.require_failed_rerun,
        )

    if args.json_output:
        write_json_report(report, Path(args.json_output))

    if args.json:
        print(json.dumps(report, indent=2, default=str))
    else:
        print("Phase 6B Evidence Report")
        print("========================")
        print(f"project_id={report['project_id']}")
        print(f"session_id={report['session_id']}")
        print(f"task_id={report['task_id']}")
        print(f"pass={report['pass']}")
        print()
        print("Checks:")
        for name, ok in report["checks"].items():
            print(f"- {name}: {ok}")
        print()
        logs = report["runtime_log_identity"]
        print(
            "Runtime logs: "
            f"total={logs['total'] or 0} "
            f"with_task_execution_id={logs['with_execution_id'] or 0} "
            f"missing_task_execution_id={logs['missing_execution_id'] or 0}"
        )
        if report["unexpected_sessions"]:
            print(f"Unexpected sessions: {report['unexpected_sessions']}")
    return 0 if report["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
