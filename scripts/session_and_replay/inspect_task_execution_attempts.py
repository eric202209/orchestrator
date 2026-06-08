#!/usr/bin/env python3
"""Inspect TaskExecution attempts for one session or task."""

from __future__ import annotations

import argparse
import json
import sqlite3
from typing import Any


def _rows(conn: sqlite3.Connection, query: str, params: tuple[Any, ...]) -> list[dict]:
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Show task execution attempts and whether reruns stayed in session."
    )
    parser.add_argument("--db", default="orchestrator.db")
    parser.add_argument("--session-id", type=int, required=True)
    parser.add_argument("--task-id", type=int)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    filters = ["te.session_id = ?"]
    params: list[Any] = [args.session_id]
    if args.task_id is not None:
        filters.append("te.task_id = ?")
        params.append(args.task_id)
    rows = _rows(
        conn,
        f"""
        select te.id, te.session_id, te.task_id, t.title, te.attempt_number,
               te.status, te.started_at, te.completed_at,
               count(le.id) as log_count
        from task_executions te
        join tasks t on t.id = te.task_id
        left join log_entries le on le.task_execution_id = te.id
        where {' and '.join(filters)}
        group by te.id
        order by te.task_id, te.attempt_number
        """,
        tuple(params),
    )
    conn.close()

    if args.json:
        print(json.dumps({"attempts": rows}, indent=2, default=str))
        return 0

    print(f"TaskExecution attempts for session {args.session_id}")
    for row in rows:
        print(
            f"- task={row['task_id']} attempt={row['attempt_number']} "
            f"execution={row['id']} status={row['status']} logs={row['log_count']}"
        )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
