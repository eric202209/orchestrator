#!/usr/bin/env python3
"""Run live Phase 9N outcome classification sweep.

Runs 6 workloads across 2 projects, computes the four production-readiness
outcome rates from the resulting DB state, and saves a JSON report.

Usage:
    python scripts/phase9n_outcome_classification_sweep.py
    python scripts/phase9n_outcome_classification_sweep.py --timeout-seconds 900
    python scripts/phase9n_outcome_classification_sweep.py --dry-run
"""

from __future__ import annotations

import argparse
import fcntl
import json
import os
import sqlite3
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
import sys

if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

LOCK_FILE = REPO_ROOT / ".phase9n_sweep.lock"


def _acquire_lock() -> object:
    """Acquire an exclusive process lock. Exits immediately if another instance runs."""
    lock_fd = open(LOCK_FILE, "w")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_fd.close()
        raise SystemExit(
            "ERROR: another phase9n sweep is already running (lock held). "
            f"If no sweep is running, delete {LOCK_FILE} and retry."
        )
    lock_fd.write(str(os.getpid()))
    lock_fd.flush()
    return lock_fd


def _release_lock(lock_fd: object) -> None:
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_UN)
        lock_fd.close()
        LOCK_FILE.unlink(missing_ok=True)
    except Exception:
        pass

from app.database import get_db_session
from app.models import Project, Session as SessionModel, Task, TaskExecution, TaskStatus
from app.services.session.session_runtime_service import queue_task_for_session
from app.services.workspace.project_isolation_service import (
    normalize_project_workspace_path,
)
from app.services.workspace.system_settings import get_effective_workspace_root
from scripts.failure_taxonomy import outcome_class as classify_outcome
from scripts.session_outcome_report import (
    _operator_review_count,
    _outcome_rates,
    _rows,
    _session_report,
)

TERMINAL_STATUSES = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
GENERATED_WORKSPACE_DIR = ".openclaw-workspaces"
REPORTS_DIR = REPO_ROOT / "docs" / "roadmap" / "reports" / "sweeps"

WORKLOADS = [
    {
        "project_slug": "alpha",
        "slug": "static-html",
        "title": "Create Phase 9N static HTML page",
        "description": (
            "Create index.html with a heading 'Phase 9N Test' and a paragraph "
            "describing the page. Verify the file exists and contains the heading."
        ),
    },
    {
        "project_slug": "alpha",
        "slug": "python-util",
        "title": "Create Phase 9N Python utility",
        "description": (
            "Create utils.py with a function add(a, b) that returns a + b. "
            "Add a brief docstring. Verify the file is valid Python."
        ),
    },
    {
        "project_slug": "alpha",
        "slug": "readme-update",
        "title": "Create Phase 9N README",
        "description": (
            "Create README.md with a title 'Phase 9N Test Project', a short "
            "description, and a Usage section. Verify the file exists."
        ),
    },
    {
        "project_slug": "beta",
        "slug": "config-json",
        "title": "Create Phase 9N config file",
        "description": (
            "Create config.json with keys: name='phase9n-test', version='0.1.0', "
            "enabled=true. Verify the file is valid JSON."
        ),
    },
    {
        "project_slug": "beta",
        "slug": "css-styles",
        "title": "Create Phase 9N stylesheet",
        "description": (
            "Create styles.css with body { margin: 0; font-family: sans-serif; } "
            "and h1 { color: #333; }. Verify the file exists."
        ),
    },
    {
        "project_slug": "beta",
        "slug": "changelog",
        "title": "Create Phase 9N changelog",
        "description": (
            "Create CHANGELOG.md with a single entry: "
            "## [0.1.0] - 2026-05-15\n### Added\n- Initial release. "
            "Verify the file exists and contains the version heading."
        ),
    },
]


def _chmod_shared(path: Path, *, directory: bool = False) -> None:
    try:
        path.chmod(0o777 if directory else 0o666)
    except FileNotFoundError:
        return


def _workspace_root(batch_id: str, db) -> Path:
    return (
        get_effective_workspace_root(db=db) / GENERATED_WORKSPACE_DIR / batch_id
    ).resolve()


def _task_execution_snapshot(db, task_execution_id: int) -> dict[str, Any]:
    execution = (
        db.query(TaskExecution).filter(TaskExecution.id == task_execution_id).first()
    )
    if not execution:
        return {"task_execution_id": task_execution_id, "status": "missing"}
    task = db.query(Task).filter(Task.id == execution.task_id).first()
    session = (
        db.query(SessionModel).filter(SessionModel.id == execution.session_id).first()
    )
    return {
        "task_execution_id": execution.id,
        "session_id": execution.session_id,
        "task_id": execution.task_id,
        "attempt_number": execution.attempt_number,
        "status": getattr(execution.status, "value", str(execution.status)),
        "task_status": getattr(task.status, "value", str(task.status)) if task else None,
        "session_status": session.status if session else None,
        "started_at": execution.started_at.isoformat() if execution.started_at else None,
        "completed_at": (
            execution.completed_at.isoformat() if execution.completed_at else None
        ),
        "error_message": (
            task.error_message[:500] if task and task.error_message else None
        ),
    }


def _wait_for_terminal(db, task_execution_id: int, timeout_seconds: int) -> dict[str, Any]:
    deadline = time.monotonic() + timeout_seconds
    while time.monotonic() < deadline:
        db.expire_all()
        execution = (
            db.query(TaskExecution).filter(TaskExecution.id == task_execution_id).first()
        )
        if execution and execution.status in TERMINAL_STATUSES:
            return _task_execution_snapshot(db, task_execution_id)
        time.sleep(5)
    return {**_task_execution_snapshot(db, task_execution_id), "timed_out": True}


def _connect_readonly(db) -> sqlite3.Connection:
    from app.config import settings

    db_url = str(settings.DATABASE_URL)
    db_path = db_url.replace("sqlite:///", "").replace("sqlite://", "")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


def _compute_outcome_rates(
    session_ids: list[int], db
) -> tuple[dict[str, float], dict[str, int], int, list[dict[str, Any]]]:
    conn = _connect_readonly(db)
    try:
        placeholders = ",".join("?" * len(session_ids))
        sessions = _rows(
            conn,
            f"""
            select s.id, s.project_id, s.name, s.status, s.is_active,
                   s.created_at, s.started_at, s.stopped_at,
                   p.name as project_name, p.workspace_path
            from sessions s
            left join projects p on p.id = s.project_id
            where s.id in ({placeholders})
            """,
            tuple(session_ids),
        )
        session_reports = [
            _session_report(conn, session=s, check_timeline=False) for s in sessions
        ]
        rates, oc_counts = _outcome_rates(session_reports)
        op_review = _operator_review_count(conn)
        stuck_sessions = [
            {
                "session_id": r["session_id"],
                "status": r["status"],
                "outcome_class": r.get("outcome_class"),
                "terminal_class": r["terminal_class"],
                "failure_diagnostic_reason": r.get("failure_diagnostic_reason"),
            }
            for r in session_reports
            if r.get("outcome_class") == "stuck_or_manual_db_cleanup"
        ]
        return rates, oc_counts, op_review, stuck_sessions
    finally:
        conn.close()


def run_sweep(*, timeout_seconds: int, output_path: Path, dry_run: bool) -> dict[str, Any]:
    lock_fd = _acquire_lock()
    batch_id = f"phase9n-classification-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    start_ts = time.monotonic()
    db = get_db_session()
    results: list[dict[str, Any]] = []
    session_ids: list[int] = []

    try:
        batch_root = _workspace_root(batch_id, db)
        batch_root.mkdir(parents=True, exist_ok=True)
        _chmod_shared(batch_root.parent, directory=True)
        _chmod_shared(batch_root, directory=True)

        # Create project workspaces keyed by slug
        project_workspaces: dict[str, dict[str, Any]] = {}
        for slug in {"alpha", "beta"}:
            project_workspace = batch_root / slug
            project_workspace.mkdir(parents=True, exist_ok=True)
            _chmod_shared(project_workspace, directory=True)
            project_name = f"{batch_id}-{slug}"
            stored_workspace_path = normalize_project_workspace_path(
                str(project_workspace),
                project_name=project_name,
                db=db,
            )
            project = Project(
                name=project_name,
                description=f"Phase 9N outcome classification sweep — {slug}",
                workspace_path=stored_workspace_path,
            )
            db.add(project)
            db.flush()
            project_workspaces[slug] = {
                "project": project,
                "workspace": project_workspace,
            }
        db.commit()

        for index, workload in enumerate(WORKLOADS, start=1):
            proj_slug = workload["project_slug"]
            project = project_workspaces[proj_slug]["project"]

            task = Task(
                project_id=project.id,
                title=workload["title"],
                description=workload["description"],
                status=TaskStatus.PENDING,
                execution_profile="full_lifecycle",
            )
            db.add(task)
            db.flush()

            session = SessionModel(
                project_id=project.id,
                name=f"{workload['title']} session",
                description=workload["description"][:500],
                status="pending",
                execution_mode="manual",
                default_execution_profile="full_lifecycle",
                is_active=False,
                instance_id=str(uuid.uuid4()),
            )
            db.add(session)
            db.commit()

            if dry_run:
                print(
                    f"  [dry-run] would queue workload {index}/{len(WORKLOADS)}: "
                    f"{workload['slug']} on project {project.id}"
                )
                continue

            print(
                f"  queuing workload {index}/{len(WORKLOADS)}: {workload['slug']} "
                f"(project={project.id} session={session.id})"
            )
            queued = queue_task_for_session(
                db, session, task.id, timeout_seconds=timeout_seconds
            )
            terminal = _wait_for_terminal(
                db, int(queued["task_execution_id"]), timeout_seconds
            )
            session_ids.append(session.id)
            results.append(
                {
                    "workload": workload["slug"],
                    "project_slug": proj_slug,
                    "project_id": project.id,
                    "session_id": session.id,
                    "task_id": task.id,
                    "queued": queued,
                    "terminal": terminal,
                }
            )
            print(
                f"    done: status={terminal.get('status')} "
                f"timed_out={terminal.get('timed_out', False)}"
            )

        if dry_run:
            summary = {
                "batch_id": batch_id,
                "dry_run": True,
                "workloads_planned": len(WORKLOADS),
                "outcome_rates": {},
                "outcome_counts": {},
                "pass": False,
            }
            output_path.parent.mkdir(parents=True, exist_ok=True)
            output_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
            return summary

        # Compute outcome rates
        rates, oc_counts, op_review, stuck_sessions = _compute_outcome_rates(
            session_ids, db
        )
        status_counts = Counter(
            str((r.get("terminal") or {}).get("status") or "unknown") for r in results
        )
        gate_pass = oc_counts.get("stuck_or_manual_db_cleanup", 0) == 0
        runtime = round(time.monotonic() - start_ts, 1)

        summary = {
            "batch_id": batch_id,
            "created_at": datetime.now(UTC).isoformat(),
            "batch_workspace_root": str(batch_root),
            "sessions_run": len(results),
            "runtime_seconds": runtime,
            "outcome_rates": rates,
            "outcome_counts": oc_counts,
            "avg_repair_count": 0.0,
            "avg_replan_count": 0.0,
            "operator_review_count": op_review,
            "stuck_sessions": stuck_sessions,
            "pass": gate_pass,
            "batch_summary": {
                "status_counts": dict(sorted(status_counts.items())),
            },
            "results": results,
        }

        output_path.parent.mkdir(parents=True, exist_ok=True)
        _chmod_shared(output_path.parent, directory=True)
        output_path.write_text(
            json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8"
        )
        _chmod_shared(output_path)
        return summary
    finally:
        db.close()
        _release_lock(lock_fd)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run live Phase 9N outcome classification sweep."
    )
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument("--dry-run", action="store_true")
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    parser.add_argument(
        "--output",
        default=str(
            REPORTS_DIR / f"phase9n-outcome-classification-sweep-{timestamp}.json"
        ),
    )
    args = parser.parse_args()

    output_path = Path(args.output)
    summary = run_sweep(
        timeout_seconds=max(60, args.timeout_seconds),
        output_path=output_path,
        dry_run=args.dry_run,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    if not args.dry_run:
        gate = summary.get("pass", False)
        print(
            f"\nPhase 9N gate: {'PASS' if gate else 'FAIL'} "
            f"(stuck_or_manual_db_cleanup_rate="
            f"{summary['outcome_rates'].get('stuck_or_manual_db_cleanup_rate', '?')})"
        )
        print(f"Report saved to: {output_path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
