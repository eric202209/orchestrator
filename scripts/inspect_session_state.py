#!/usr/bin/env python3
"""Inspect session/task/execution state and Phase 6A invariants."""

from __future__ import annotations

import argparse
import json
import sqlite3
from pathlib import Path
from typing import Any


def _connect(db_path: str) -> sqlite3.Connection:
    conn = sqlite3.connect(db_path)
    conn.row_factory = sqlite3.Row
    return conn


def _rows(conn: sqlite3.Connection, query: str, params: tuple[Any, ...]) -> list[dict]:
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def build_report(conn: sqlite3.Connection, *, session_id: int) -> dict[str, Any]:
    session = conn.execute(
        """
        select s.id, s.project_id, s.name, s.status, s.is_active, s.instance_id,
               s.created_at, s.started_at, s.stopped_at, s.paused_at, s.resumed_at,
               p.name as project_name, p.workspace_path
        from sessions s
        join projects p on p.id = s.project_id
        where s.id = ?
        """,
        (session_id,),
    ).fetchone()
    if session is None:
        raise SystemExit(f"Session {session_id} not found")

    tasks = _rows(
        conn,
        """
        select t.id, t.title, t.status, t.current_step, t.error_message,
               st.status as session_task_status
        from session_tasks st
        join tasks t on t.id = st.task_id
        where st.session_id = ?
        order by t.id
        """,
        (session_id,),
    )
    executions = _rows(
        conn,
        """
        select id, task_id, attempt_number, status, started_at, completed_at
        from task_executions
        where session_id = ?
        order by task_id, attempt_number
        """,
        (session_id,),
    )
    runtime_logs = conn.execute(
        """
        select count(*) as total,
               sum(case when task_execution_id is not null then 1 else 0 end) as with_execution_id,
               sum(case when task_execution_id is null then 1 else 0 end) as missing_execution_id
        from log_entries
        where session_id = ?
          and (message like '%[OPENCLAW]%' or message like '%[PERFORMANCE]%')
        """,
        (session_id,),
    ).fetchone()

    running_execution_task_ids = {
        row["task_id"] for row in executions if row["status"] == "RUNNING"
    }
    running_tasks_without_execution = [
        task
        for task in tasks
        if task["status"] == "RUNNING" and task["id"] not in running_execution_task_ids
    ]
    stopped_session_running_executions = [
        row
        for row in executions
        if session["status"] in {"stopped", "cancelled", "deleted"}
        and row["status"] == "RUNNING"
    ]

    return {
        "session": dict(session),
        "tasks": tasks,
        "task_executions": executions,
        "runtime_log_identity": dict(runtime_logs),
        "invariants": {
            "running_tasks_without_running_execution": running_tasks_without_execution,
            "stopped_session_running_executions": stopped_session_running_executions,
            "pass": not running_tasks_without_execution
            and not stopped_session_running_executions,
        },
    }


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect one session and check execution identity invariants."
    )
    parser.add_argument("--db", default="orchestrator.db", help="Path to sqlite DB")
    parser.add_argument("--session-id", type=int, required=True)
    parser.add_argument("--json", action="store_true", help="Emit JSON")
    args = parser.parse_args()

    with _connect(args.db) as conn:
        report = build_report(conn, session_id=args.session_id)

    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    session = report["session"]
    print(f"Session {session['id']}: {session['name']}")
    print(f"status={session['status']} active={bool(session['is_active'])}")
    print(f"project={session['project_name']} workspace={session['workspace_path']}")
    print()
    print("Task Executions:")
    for item in report["task_executions"]:
        print(
            f"- execution={item['id']} task={item['task_id']} "
            f"attempt={item['attempt_number']} status={item['status']}"
        )
    print()
    print("Runtime Logs:")
    logs = report["runtime_log_identity"]
    print(
        f"- total={logs['total'] or 0} with_task_execution_id={logs['with_execution_id'] or 0} "
        f"missing_task_execution_id={logs['missing_execution_id'] or 0}"
    )
    print()
    print("Invariants:")
    print(f"- pass={report['invariants']['pass']}")
    for key, values in report["invariants"].items():
        if key != "pass" and values:
            print(f"- {key}: {values}")
    return 0 if report["invariants"]["pass"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
