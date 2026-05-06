#!/usr/bin/env python3
"""Inspect runtime logs and task_execution_id coverage."""

from __future__ import annotations

import argparse
import json
import sqlite3
from typing import Any


def _rows(conn: sqlite3.Connection, query: str, params: tuple[Any, ...]) -> list[dict]:
    return [dict(row) for row in conn.execute(query, params).fetchall()]


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Inspect OpenClaw/PERFORMANCE logs with execution identity."
    )
    parser.add_argument("--db", default="orchestrator.db")
    parser.add_argument("--session-id", type=int, required=True)
    parser.add_argument("--task-execution-id", type=int)
    parser.add_argument("--since-log-id", type=int, default=0)
    parser.add_argument("--limit", type=int, default=50)
    parser.add_argument("--json", action="store_true")
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    filters = [
        "session_id = ?",
        "id >= ?",
        "(message like '%[OPENCLAW]%' or message like '%[PERFORMANCE]%')",
    ]
    params: list[Any] = [args.session_id, args.since_log_id]
    if args.task_execution_id is not None:
        filters.append("task_execution_id = ?")
        params.append(args.task_execution_id)

    rows = _rows(
        conn,
        f"""
        select id, session_id, task_id, task_execution_id, level, message, created_at
        from log_entries
        where {' and '.join(filters)}
        order by id desc
        limit ?
        """,
        tuple(params + [args.limit]),
    )
    coverage = conn.execute(
        f"""
        select count(*) as total,
               sum(case when task_execution_id is not null then 1 else 0 end) as with_execution_id,
               sum(case when task_execution_id is null then 1 else 0 end) as missing_execution_id
        from log_entries
        where {' and '.join(filters)}
        """,
        tuple(params),
    ).fetchone()
    conn.close()

    report = {"coverage": dict(coverage), "logs": rows}
    if args.json:
        print(json.dumps(report, indent=2, default=str))
        return 0

    print(
        "Runtime log identity: "
        f"total={coverage['total'] or 0} "
        f"with_task_execution_id={coverage['with_execution_id'] or 0} "
        f"missing_task_execution_id={coverage['missing_execution_id'] or 0}"
    )
    for row in rows:
        print(
            f"- log={row['id']} execution={row['task_execution_id']} "
            f"task={row['task_id']} {row['level']}: {row['message'][:160]}"
        )
    return 0 if (coverage["missing_execution_id"] or 0) == 0 else 1


if __name__ == "__main__":
    raise SystemExit(main())
