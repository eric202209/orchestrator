#!/usr/bin/env python3
"""Inspect orchestrator.db and explain why a run appears stuck at planning."""

from __future__ import annotations

import argparse
import importlib.util
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
MODULE_PATH = REPO_ROOT / "app" / "services" / "orchestration" / "stuck_diagnostics.py"
SPEC = importlib.util.spec_from_file_location("stuck_diagnostics", MODULE_PATH)
if SPEC is None or SPEC.loader is None:
    raise SystemExit(f"Unable to load diagnosis module from {MODULE_PATH}")
MODULE = importlib.util.module_from_spec(SPEC)
sys.modules[SPEC.name] = MODULE
SPEC.loader.exec_module(MODULE)

PlanningLogSnapshot = MODULE.PlanningLogSnapshot
PlanningRunSnapshot = MODULE.PlanningRunSnapshot
ValidationCheckpointSnapshot = MODULE.ValidationCheckpointSnapshot
diagnose_planning_stuck = MODULE.diagnose_planning_stuck


def _parse_db_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    try:
        return datetime.fromisoformat(value.replace("Z", "+00:00")).replace(tzinfo=None)
    except ValueError:
        return None


def _fetch_one(
    conn: sqlite3.Connection, query: str, params: tuple
) -> sqlite3.Row | None:
    return conn.execute(query, params).fetchone()


def build_snapshot(
    conn: sqlite3.Connection,
    *,
    session_id: int,
    task_id: int,
) -> PlanningRunSnapshot:
    session_row = _fetch_one(
        conn,
        """
        select id, status, is_active
        from sessions
        where id = ?
        """,
        (session_id,),
    )
    if session_row is None:
        raise SystemExit(f"Session {session_id} not found")

    task_row = _fetch_one(
        conn,
        """
        select id, status, current_step, error_message
        from tasks
        where id = ?
        """,
        (task_id,),
    )
    if task_row is None:
        raise SystemExit(f"Task {task_id} not found")

    log_rows = conn.execute(
        """
        select created_at, level, message
        from log_entries
        where session_id = ? and task_id = ?
        order by id desc
        limit 25
        """,
        (session_id, task_id),
    ).fetchall()

    checkpoint_rows = conn.execute(
        """
        select checkpoint_type, description, created_at
        from task_checkpoints
        where session_id = ? and task_id = ?
        order by id desc
        limit 10
        """,
        (session_id, task_id),
    ).fetchall()

    latest_logs = tuple(
        PlanningLogSnapshot(
            created_at=_parse_db_datetime(row["created_at"]),
            message=row["message"] or "",
            level=row["level"] or "INFO",
        )
        for row in log_rows
    )
    validation_checkpoints = tuple(
        ValidationCheckpointSnapshot(
            checkpoint_type=row["checkpoint_type"] or "",
            description=row["description"] or "",
            created_at=_parse_db_datetime(row["created_at"]),
        )
        for row in checkpoint_rows
    )

    return PlanningRunSnapshot(
        session_id=session_id,
        task_id=task_id,
        session_status=session_row["status"] or "",
        session_is_active=bool(session_row["is_active"]),
        task_status=task_row["status"] or "",
        task_current_step=int(task_row["current_step"] or 0),
        task_error_message=task_row["error_message"],
        latest_logs=latest_logs,
        validation_checkpoints=validation_checkpoints,
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Diagnose why an orchestration run looks stuck at planning."
    )
    parser.add_argument("--db", default="orchestrator.db", help="Path to sqlite DB")
    parser.add_argument("--session-id", type=int, required=True)
    parser.add_argument("--task-id", type=int, required=True)
    args = parser.parse_args()

    conn = sqlite3.connect(args.db)
    conn.row_factory = sqlite3.Row
    try:
        snapshot = build_snapshot(
            conn,
            session_id=args.session_id,
            task_id=args.task_id,
        )
    finally:
        conn.close()

    diagnosis = diagnose_planning_stuck(
        snapshot,
        now=datetime.now(timezone.utc).replace(tzinfo=None),
    )

    print("Planning Stuck Diagnosis")
    print("========================")
    print(f"session_id: {snapshot.session_id}")
    print(f"task_id: {snapshot.task_id}")
    print(f"session_status: {snapshot.session_status}")
    print(f"session_is_active: {snapshot.session_is_active}")
    print(f"task_status: {snapshot.task_status}")
    print(f"task_current_step: {snapshot.task_current_step}")
    print()
    print(f"diagnosis: {diagnosis.state}")
    print(f"summary: {diagnosis.summary}")
    print()
    print("Evidence:")
    for item in diagnosis.evidence:
        print(f"- {item}")
    print()
    print("Recommendations:")
    for item in diagnosis.recommendations:
        print(f"- {item}")
    print()

    if snapshot.latest_logs:
        print("Latest Logs:")
        for log in snapshot.latest_logs[:8]:
            when = log.created_at.isoformat(sep=" ") if log.created_at else "unknown"
            print(f"- [{when}] {log.level}: {log.message[:180]}")
        print()

    if snapshot.validation_checkpoints:
        print("Recent Validation Checkpoints:")
        for cp in snapshot.validation_checkpoints[:5]:
            when = cp.created_at.isoformat(sep=" ") if cp.created_at else "unknown"
            print(f"- [{when}] {cp.checkpoint_type}: {cp.description}")

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
