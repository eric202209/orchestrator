#!/usr/bin/env python3
"""Run a small live Phase 9G shadow-warning evidence batch."""

from __future__ import annotations

import argparse
import json
import sqlite3
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.database import get_db_session
from app.config import settings
from app.models import Project, Session as SessionModel, Task, TaskExecution, TaskStatus
from app.services.session.session_runtime_service import queue_task_for_session
from app.services.workspace.project_isolation_service import normalize_project_workspace_path
from app.services.workspace.system_settings import get_effective_workspace_root
from scripts.planning_contract_report import summarize as summarize_planning_contracts


TERMINAL_STATUSES = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
GENERATED_WORKSPACE_DIR = ".openclaw-workspaces"


WORKLOADS = [
    {
        "slug": "docs-metadata",
        "title": "Update README metadata",
        "description": (
            "Create or update README.md with a short project title, purpose, and "
            "one usage note. Use deterministic file operations where possible."
        ),
    },
    {
        "slug": "python-config",
        "title": "Create Python config module",
        "description": (
            "Create app_config.py with a DEFAULT_PORT constant, a get_settings() "
            "function returning a dict, and verify it with python -m py_compile "
            "plus a small import assertion."
        ),
    },
    {
        "slug": "package-metadata",
        "title": "Create package metadata",
        "description": (
            "Create package.json for a tiny Node utility with name, version, "
            "private flag, and a test script that runs node -e."
        ),
    },
    {
        "slug": "python-cli",
        "title": "Create word count CLI",
        "description": (
            "Create word_count.py with a count_words(text) function and a small "
            "CLI entry point. Verify the function using python -c import checks."
        ),
    },
    {
        "slug": "static-page",
        "title": "Create static status page",
        "description": (
            "Create index.html and styles.css for a simple status page. Verify "
            "with node -e that both files exist and index.html links styles.css."
        ),
    },
]


def _chmod_shared(path: Path, *, directory: bool = False) -> None:
    try:
        path.chmod(0o777 if directory else 0o666)
    except FileNotFoundError:
        return


def _resolve_batch_workspace_root(batch_id: str, db=None) -> Path:
    return (
        get_effective_workspace_root(db=db) / GENERATED_WORKSPACE_DIR / batch_id
    ).resolve()


def _stored_project_workspace_path(
    project_workspace: Path, *, project_name: str, db=None
) -> str:
    return normalize_project_workspace_path(
        str(project_workspace),
        project_name=project_name,
        db=db,
    )


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
        "status": getattr(execution.status, "value", str(execution.status)),
        "task_status": getattr(task.status, "value", str(task.status)) if task else None,
        "session_status": session.status if session else None,
        "started_at": execution.started_at.isoformat() if execution.started_at else None,
        "completed_at": (
            execution.completed_at.isoformat() if execution.completed_at else None
        ),
        "error_message": (task.error_message[:1000] if task and task.error_message else None),
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


def _batch_contract_records(
    planning_report: dict[str, Any], task_execution_ids: set[int]
) -> list[dict[str, Any]]:
    records = planning_report.get("records")
    if not isinstance(records, list):
        return []
    return [
        record
        for record in records
        if int(record.get("task_execution_id") or 0) in task_execution_ids
    ]


def _batch_summary(
    results: list[dict[str, Any]], planning_report: dict[str, Any]
) -> dict[str, Any]:
    status_counts = Counter(
        str((result.get("terminal") or {}).get("status") or "unknown")
        for result in results
    )
    task_execution_ids = {
        int((result.get("terminal") or {}).get("task_execution_id") or 0)
        for result in results
        if (result.get("terminal") or {}).get("task_execution_id")
    }
    batch_records = _batch_contract_records(planning_report, task_execution_ids)
    shadow_warning_rule_counts = Counter(
        str(rule_id)
        for record in batch_records
        for rule_id in (record.get("shadow_warning_rule_ids") or [])
        if str(rule_id or "").strip()
    )
    return {
        "requested_count": len(results),
        "task_execution_ids": sorted(task_execution_ids),
        "status_counts": dict(sorted(status_counts.items())),
        "shadow_warning_rule_counts": dict(sorted(shadow_warning_rule_counts.items())),
        "contract_records_found": len(batch_records),
    }


def run_batch(*, count: int, timeout_seconds: int, output_path: Path) -> dict[str, Any]:
    batch_id = f"phase9g-shadow-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    db = get_db_session()
    results: list[dict[str, Any]] = []
    try:
        openclaw_workspace_root = get_effective_workspace_root(db=db)
        batch_workspace_root = _resolve_batch_workspace_root(batch_id, db=db)
        batch_workspace_root.mkdir(parents=True, exist_ok=True)
        _chmod_shared(openclaw_workspace_root, directory=True)
        _chmod_shared(batch_workspace_root.parent, directory=True)
        _chmod_shared(batch_workspace_root, directory=True)

        for index, workload in enumerate(WORKLOADS[:count], start=1):
            project_workspace = (
                batch_workspace_root / f"{index:02d}-{workload['slug']}"
            )
            project_workspace.mkdir(parents=True, exist_ok=True)
            _chmod_shared(project_workspace, directory=True)
            project_name = f"{batch_id}-{index:02d}-{workload['slug']}"
            stored_workspace_path = _stored_project_workspace_path(
                project_workspace,
                project_name=project_name,
                db=db,
            )

            project = Project(
                name=project_name,
                description="Phase 9G live shadow-warning evidence workload",
                workspace_path=stored_workspace_path,
            )
            db.add(project)
            db.flush()

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

            queued = queue_task_for_session(
                db,
                session,
                task.id,
                timeout_seconds=timeout_seconds,
            )
            terminal = _wait_for_terminal(
                db, int(queued["task_execution_id"]), timeout_seconds
            )
            results.append(
                {
                    "workload": workload["slug"],
                    "project_id": project.id,
                    "session_id": session.id,
                    "task_id": task.id,
                    "queued": queued,
                    "terminal": terminal,
                }
            )

        planning_report = _planning_contract_summary(limit=max(50, count * 10))
        summary = {
            "batch_id": batch_id,
            "created_at": datetime.now(UTC).isoformat(),
            "openclaw_workspace_root": str(openclaw_workspace_root),
            "batch_workspace_root": str(batch_workspace_root),
            "requested_count": count,
            "results": results,
            "batch_summary": _batch_summary(results, planning_report),
            "planning_contract_report": planning_report,
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _chmod_shared(output_path.parent, directory=True)
        output_path.write_text(json.dumps(summary, indent=2, sort_keys=True), encoding="utf-8")
        _chmod_shared(output_path)
        return summary
    finally:
        db.close()


def _planning_contract_summary(*, limit: int) -> dict[str, Any]:
    database_url = str(settings.DATABASE_URL)
    if not database_url.startswith("sqlite:///"):
        raise RuntimeError("phase9g live batch report currently supports sqlite only")
    db_path = database_url.replace("sqlite:///", "", 1)
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    try:
        return summarize_planning_contracts(
            conn,
            limit=limit,
            diagnostic_threshold=3,
        )
    finally:
        conn.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run live Phase 9G shadow-warning evidence workloads."
    )
    parser.add_argument("--count", type=int, default=5)
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument(
        "--output",
        default="docs/roadmap/reports/sweeps/phase9g-live-shadow-batch-20260513.json",
    )
    args = parser.parse_args()

    count = max(1, min(int(args.count), len(WORKLOADS)))
    summary = run_batch(
        count=count,
        timeout_seconds=max(60, int(args.timeout_seconds)),
        output_path=Path(args.output),
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
