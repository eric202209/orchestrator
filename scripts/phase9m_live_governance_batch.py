#!/usr/bin/env python3
"""Run live Phase 9M review-policy governance evidence workloads."""

from __future__ import annotations

import argparse
import json
import time
import uuid
from collections import Counter
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from app.database import get_db_session
from app.models import Project, Session as SessionModel, Task, TaskExecution, TaskStatus
from app.services.session.session_runtime_service import queue_task_for_session
from app.services.task_service import TaskService
from app.services.workspace.project_isolation_service import (
    normalize_project_workspace_path,
)
from app.services.workspace.system_settings import get_effective_workspace_root

TERMINAL_STATUSES = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
GENERATED_WORKSPACE_DIR = ".openclaw-workspaces"

WORKLOADS = [
    {
        "slug": "safe-static-content",
        "title": "Create Phase 9M safe static content",
        "description": (
            "Create index.html and styles.css for a tiny static status page. "
            "Verify with a command that both files exist and index.html links "
            "styles.css. Do not create package.json or dependency files."
        ),
    },
    {
        "slug": "dependency-change",
        "title": "Create Phase 9M dependency metadata",
        "description": (
            "Create package.json for a tiny private Node utility with name, "
            "version, private=true, and a test script that runs node -e. "
            "Verify package.json is valid JSON."
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
        "status": getattr(execution.status, "value", str(execution.status)),
        "task_status": getattr(task.status, "value", str(task.status)) if task else None,
        "workspace_status": getattr(task, "workspace_status", None) if task else None,
        "session_status": session.status if session else None,
        "started_at": execution.started_at.isoformat() if execution.started_at else None,
        "completed_at": (
            execution.completed_at.isoformat() if execution.completed_at else None
        ),
        "error_message": (
            task.error_message[:1000] if task and task.error_message else None
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


def _capture_change_set(db, task_execution_id: int) -> dict[str, Any] | None:
    return TaskService(db).get_task_execution_change_set(
        task_execution_id=task_execution_id
    )


def _maybe_reject_held_change_set(
    db,
    *,
    project: Project,
    task: Task,
    change_set: dict[str, Any] | None,
    batch_id: str,
) -> dict[str, Any] | None:
    if not change_set:
        return None
    review_decision = change_set.get("review_decision") or {}
    if not review_decision.get("held_for_review"):
        return None
    task_execution_id = int(change_set["task_execution_id"])
    snapshot_key = str(change_set.get("snapshot_key") or "")
    return TaskService(db).reject_task_execution_change_set(
        project,
        task,
        task_execution_id=task_execution_id,
        snapshot_key=snapshot_key,
        reason="phase9m_live_operator_rejected_held_change_set",
        operator="phase9m-live-script",
    )


def run_batch(*, timeout_seconds: int, output_path: Path) -> dict[str, Any]:
    batch_id = f"phase9m-governance-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    db = get_db_session()
    results: list[dict[str, Any]] = []
    try:
        batch_root = _workspace_root(batch_id, db)
        batch_root.mkdir(parents=True, exist_ok=True)
        _chmod_shared(batch_root.parent, directory=True)
        _chmod_shared(batch_root, directory=True)

        for index, workload in enumerate(WORKLOADS, start=1):
            project_workspace = batch_root / f"{index:02d}-{workload['slug']}"
            project_workspace.mkdir(parents=True, exist_ok=True)
            _chmod_shared(project_workspace, directory=True)
            project_name = f"{batch_id}-{index:02d}-{workload['slug']}"
            stored_workspace_path = normalize_project_workspace_path(
                str(project_workspace),
                project_name=project_name,
                db=db,
            )

            project = Project(
                name=project_name,
                description="Phase 9M live governance policy evidence workload",
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
            change_set = _capture_change_set(db, int(queued["task_execution_id"]))
            override_result = _maybe_reject_held_change_set(
                db,
                project=project,
                task=task,
                change_set=change_set,
                batch_id=batch_id,
            )
            if override_result:
                change_set = _capture_change_set(db, int(queued["task_execution_id"]))

            results.append(
                {
                    "workload": workload["slug"],
                    "project_id": project.id,
                    "session_id": session.id,
                    "task_id": task.id,
                    "queued": queued,
                    "terminal": terminal,
                    "change_set": change_set,
                    "override_result": override_result,
                }
            )

        status_counts = Counter(
            str((result.get("terminal") or {}).get("status") or "unknown")
            for result in results
        )
        review_outcomes = Counter(
            str(
                (
                    ((result.get("change_set") or {}).get("review_decision") or {}).get(
                        "outcome"
                    )
                )
                or "missing"
            )
            for result in results
        )
        summary = {
            "batch_id": batch_id,
            "created_at": datetime.now(UTC).isoformat(),
            "batch_workspace_root": str(batch_root),
            "results": results,
            "batch_summary": {
                "requested_count": len(results),
                "status_counts": dict(sorted(status_counts.items())),
                "review_outcomes": dict(sorted(review_outcomes.items())),
                "override_count": sum(1 for item in results if item.get("override_result")),
            },
        }
        output_path.parent.mkdir(parents=True, exist_ok=True)
        _chmod_shared(output_path.parent, directory=True)
        output_path.write_text(
            json.dumps(summary, indent=2, sort_keys=True),
            encoding="utf-8",
        )
        _chmod_shared(output_path)
        return summary
    finally:
        db.close()


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Run live Phase 9M review-policy governance workloads."
    )
    parser.add_argument("--timeout-seconds", type=int, default=900)
    parser.add_argument(
        "--output",
        default=(
            "docs/roadmap/reports/sweeps/"
            "phase9m-live-governance-batch-20260514.json"
        ),
    )
    args = parser.parse_args()
    summary = run_batch(
        timeout_seconds=max(60, int(args.timeout_seconds)),
        output_path=Path(args.output),
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
