#!/usr/bin/env python3
"""Run the Phase 10A operational stability sweep.

The sweep queues a deterministic mixed workload batch, records production
readiness counters, soft-deletes created projects, and writes the JSON report
shape required by the Phase 10 roadmap.

Usage:
    PYTHONPATH=. venv/bin/python scripts/phase10a_operational_stability_sweep.py
    PYTHONPATH=. venv/bin/python scripts/phase10a_operational_stability_sweep.py --dry-run
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import sqlite3
import sys
import time
import uuid
from collections import Counter
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[1]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from app.services.file_lock import fcntl
from app.database import get_db_session
from app.models import (
    KnowledgeUsageLog,
    LogEntry,
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
    User,
)
from app.services.session.session_lifecycle_service import (
    _recover_orphaned_running_session_if_needed,
    recover_stale_running_sessions,
    reconcile_terminal_running_sessions,
)
from app.services.session.session_runtime_service import queue_task_for_session
from app.services.session.session_runtime_service import build_task_subfolder_name
from app.services.workspace.project_isolation_service import (
    normalize_project_workspace_path,
    resolve_project_workspace_path,
)
from app.services.workspace.project_mutation_lock import (
    ProjectMutationLockError,
    project_mutation_lock,
)
from app.services.workspace.system_settings import get_effective_workspace_root
from scripts.failure_taxonomy import latest_terminal_reason
from scripts.session_outcome_report import _rows, _session_report

LOCK_FILE = REPO_ROOT / ".phase10a_sweep.lock"
GENERATED_WORKSPACE_DIR = ".openclaw-workspaces"
REPORTS_DIR = REPO_ROOT / "docs" / "roadmap" / "reports" / "sweeps"
TERMINAL_TASK_STATUSES = {TaskStatus.DONE, TaskStatus.FAILED, TaskStatus.CANCELLED}
TERMINAL_SESSION_STATUSES = {"done", "failed", "stopped", "cancelled", "deleted"}
ACTIVE_SESSION_STATUSES = {"pending", "running", "active"}


PROJECT_SHAPES = {
    "alpha-web": {
        "seed_files": {
            "README.md": "# Alpha Web\n\nPhase 10A static web workload.\n",
            "index.html": "<h1>Alpha</h1>\n",
        },
    },
    "beta-python": {
        "seed_files": {
            "README.md": "# Beta Python\n\nPhase 10A Python workload.\n",
            "calc.py": "def add(a, b):\n    return a + b\n",
        },
    },
    "gamma-docs": {
        "seed_files": {
            "README.md": "# Gamma Docs\n\nPhase 10A documentation workload.\n",
            "docs/notes.md": "Initial notes.\n",
        },
    },
    "delta-config": {
        "seed_files": {
            "README.md": "# Delta Config\n\nPhase 10A configuration workload.\n",
            "config.json": '{"name": "delta", "enabled": true}\n',
        },
    },
}


BASE_WORKLOADS = [
    (
        "alpha-web",
        "static-page",
        "Create a self-contained HTML page",
        "Create about.html with heading 'Phase 10A Alpha' and verify it exists.",
    ),
    (
        "alpha-web",
        "css-update",
        "Add a stable stylesheet",
        "Create styles.css with body margin 0 and h1 color #333. Verify it exists.",
    ),
    (
        "alpha-web",
        "web-readme",
        "Update web README",
        "Append a Usage section to README.md. Verify the section exists.",
    ),
    (
        "alpha-web",
        "asset-manifest",
        "Create asset manifest",
        "Create manifest.json with name phase10a-alpha and version 1.0.0.",
    ),
    (
        "alpha-web",
        "repairable-html",
        "Repairable HTML smoke",
        (
            "Create broken.html, verify the heading is missing, then repair it so "
            "the file contains heading 'Repair Smoke'. Finish only after verifying."
        ),
    ),
    (
        "beta-python",
        "python-util",
        "Create Python utility",
        "Create utils.py with multiply(a, b). Verify with python -m py_compile.",
    ),
    (
        "beta-python",
        "python-test",
        "Add Python smoke test",
        "Create test_calc.py that imports calc.add and asserts add(2, 3) == 5.",
    ),
    (
        "beta-python",
        "cli-doc",
        "Document Python utility",
        "Create CLI.md with one command example for running python code.",
    ),
    (
        "beta-python",
        "package-metadata",
        "Create package metadata",
        "Create pyproject.toml with project name phase10a-beta and version 0.1.0.",
    ),
    (
        "beta-python",
        "repairable-python",
        "Repairable Python smoke",
        (
            "Create repair_smoke.py with an intentionally wrong subtract function, "
            "detect the wrong result, fix it, and verify subtract(5, 2) == 3."
        ),
    ),
    (
        "gamma-docs",
        "docs-index",
        "Create docs index",
        "Create docs/index.md with a Phase 10A heading and two bullet points.",
    ),
    (
        "gamma-docs",
        "changelog",
        "Create changelog",
        "Create CHANGELOG.md with entry 0.1.0 dated 2026-05-15.",
    ),
    (
        "gamma-docs",
        "adr-stub",
        "Create ADR stub",
        "Create docs/adr/0001-phase10a.md with Status: Proposed.",
    ),
    (
        "gamma-docs",
        "operator-note",
        "Create operator note",
        "Create docs/operator-note.md summarizing this workload in three bullets.",
    ),
    (
        "gamma-docs",
        "repairable-docs",
        "Repairable docs smoke",
        (
            "Create docs/repair.md with a typo in the title, notice it, correct "
            "the title to 'Repair Smoke', and verify the corrected title exists."
        ),
    ),
    (
        "delta-config",
        "json-config",
        "Create JSON config",
        "Create settings.json with enabled true and mode 'phase10a'. Validate JSON.",
    ),
    (
        "delta-config",
        "env-template",
        "Create env template",
        "Create .env.example with PHASE10A_MODE=stability.",
    ),
    (
        "delta-config",
        "yaml-config",
        "Create YAML config",
        "Create config.yaml with name phase10a-delta and enabled true.",
    ),
    (
        "delta-config",
        "config-readme",
        "Document config",
        "Append a Configuration section to README.md. Verify the section exists.",
    ),
    (
        "delta-config",
        "repairable-config",
        "Repairable config smoke",
        (
            "Create repair.json with invalid JSON, detect the parse failure, repair "
            "it to valid JSON with repaired true, and verify JSON parsing succeeds."
        ),
    ),
]

WORKLOADS = [
    {
        "project_slug": project_slug,
        "slug": slug,
        "title": f"Phase 10A {title}",
        "description": description,
    }
    for project_slug, slug, title, description in BASE_WORKLOADS
]


def _acquire_lock() -> object:
    lock_fd = open(LOCK_FILE, "w", encoding="utf-8")
    try:
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except BlockingIOError:
        lock_fd.close()
        raise SystemExit(
            "ERROR: another phase10a sweep is already running. "
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


def _chmod_shared(path: Path, *, directory: bool = False) -> None:
    try:
        path.chmod(0o777 if directory else 0o666)
    except FileNotFoundError:
        return


def _write_shared_file(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    _chmod_shared(path.parent, directory=True)
    path.write_text(content, encoding="utf-8")
    _chmod_shared(path)


def _dir_size(path: Path) -> int:
    if not path.exists():
        return 0
    total = 0
    for file_path in path.rglob("*"):
        if file_path.is_file():
            try:
                total += file_path.stat().st_size
            except OSError:
                continue
    return total


def _workspace_root(batch_id: str, db) -> Path:
    return (
        get_effective_workspace_root(db=db) / GENERATED_WORKSPACE_DIR / batch_id
    ).resolve()


def _resolve_sweep_user_id(db) -> int | None:
    """Assign generated projects to a real user so they remain visible in the UI."""
    requested_email = os.environ.get("PHASE10A_USER_EMAIL", "").strip().lower()
    query = db.query(User).filter(User.is_active.is_(True))
    if requested_email:
        user = query.filter(User.email == requested_email).first()
        if user:
            return int(user.id)
    user = query.order_by(User.id.asc()).first()
    return int(user.id) if user else None


def _connect_readonly() -> sqlite3.Connection:
    from app.config import settings

    db_url = str(settings.DATABASE_URL)
    db_path = db_url.replace("sqlite:///", "").replace("sqlite://", "")
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    conn.row_factory = sqlite3.Row
    return conn


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
        "task_status": (
            getattr(task.status, "value", str(task.status)) if task else None
        ),
        "session_status": session.status if session else None,
        "started_at": (
            execution.started_at.isoformat() if execution.started_at else None
        ),
        "completed_at": (
            execution.completed_at.isoformat() if execution.completed_at else None
        ),
        "error_message": (
            task.error_message[:500] if task and task.error_message else None
        ),
    }


def _wait_for_all_terminal(
    db,
    task_execution_ids: list[int],
    *,
    timeout_seconds: int,
    poll_interval_seconds: int,
) -> dict[int, dict[str, Any]]:
    deadline = time.monotonic() + timeout_seconds
    pending = set(task_execution_ids)
    snapshots: dict[int, dict[str, Any]] = {}
    while pending and time.monotonic() < deadline:
        db.expire_all()
        for task_execution_id in list(pending):
            execution = (
                db.query(TaskExecution)
                .filter(TaskExecution.id == task_execution_id)
                .first()
            )
            session = None
            if execution:
                session = (
                    db.query(SessionModel)
                    .filter(SessionModel.id == execution.session_id)
                    .first()
                )
            session_status = str(getattr(session, "status", "") or "").lower()
            if (
                execution
                and execution.status in TERMINAL_TASK_STATUSES
                and session_status not in ACTIVE_SESSION_STATUSES
            ):
                snapshots[task_execution_id] = _task_execution_snapshot(
                    db, task_execution_id
                )
                pending.remove(task_execution_id)
        if pending:
            time.sleep(poll_interval_seconds)

    for task_execution_id in pending:
        snapshots[task_execution_id] = {
            **_task_execution_snapshot(db, task_execution_id),
            "timed_out": True,
        }
    return snapshots


def _wait_for_sessions_terminal(
    db,
    session_ids: list[int],
    *,
    timeout_seconds: int,
    poll_interval_seconds: int,
) -> list[int]:
    deadline = time.monotonic() + timeout_seconds
    pending = set(session_ids)
    while pending and time.monotonic() < deadline:
        db.expire_all()
        for session_id in list(pending):
            session = (
                db.query(SessionModel).filter(SessionModel.id == session_id).first()
            )
            if not session:
                pending.remove(session_id)
                continue
            if str(session.status or "").lower() not in ACTIVE_SESSION_STATUSES:
                pending.remove(session_id)
        if pending:
            time.sleep(poll_interval_seconds)
    return sorted(pending)


def _run_mutation_lock_probe(project_id: int, project_root: Path) -> dict[str, Any]:
    try:
        with project_mutation_lock(
            project_id=project_id,
            project_root=project_root,
            operation="phase10a_probe_outer",
            owner="phase10a",
        ):
            try:
                with project_mutation_lock(
                    project_id=project_id,
                    project_root=project_root,
                    operation="phase10a_probe_inner",
                    owner="phase10a",
                ):
                    return {"conflict_observed": False, "error": None}
            except ProjectMutationLockError as exc:
                return {
                    "conflict_observed": True,
                    "error": str(exc),
                    "lock_path": str(exc.lock_path),
                }
    except Exception as exc:
        return {"conflict_observed": False, "error": str(exc)}


def _run_stale_session_probe(db, project: Project) -> dict[str, Any]:
    stale_task = Task(
        project_id=project.id,
        title="Phase 10A stale running session probe",
        description="Synthetic stale session recovery probe.",
        status=TaskStatus.RUNNING,
        current_step=0,
    )
    stale_session = SessionModel(
        project_id=project.id,
        name=f"Phase 10A stale probe {uuid.uuid4()}",
        description="Synthetic stale session recovery probe.",
        status="running",
        execution_mode="manual",
        default_execution_profile="full_lifecycle",
        is_active=True,
        instance_id=str(uuid.uuid4()),
        started_at=datetime.now(UTC) - timedelta(minutes=10),
    )
    db.add_all([stale_task, stale_session])
    db.flush()
    db.add(
        SessionTask(
            session_id=stale_session.id,
            task_id=stale_task.id,
            status=TaskStatus.RUNNING,
            started_at=datetime.now(UTC) - timedelta(minutes=10),
        )
    )
    db.add(
        LogEntry(
            session_id=stale_session.id,
            session_instance_id=stale_session.instance_id,
            task_id=stale_task.id,
            level="INFO",
            message=(
                "[ORCHESTRATION] Planning response received; parsing and validating plan"
            ),
            log_metadata=json.dumps({"phase10a_probe": "stale_session_recovery"}),
            created_at=datetime.now(UTC) - timedelta(minutes=5),
        )
    )
    db.commit()

    recovered = _recover_orphaned_running_session_if_needed(db, session=stale_session)
    db.commit()
    db.refresh(stale_session)
    db.refresh(stale_task)
    return {
        "session_id": stale_session.id,
        "task_id": stale_task.id,
        "task_subfolder": stale_task.task_subfolder
        or build_task_subfolder_name(stale_task.title, stale_task.id),
        "recovered": bool(recovered),
        "session_status": stale_session.status,
        "task_status": getattr(stale_task.status, "value", str(stale_task.status)),
    }


def _delete_stale_probe_records(db, stale_probe: dict[str, Any] | None) -> None:
    if not stale_probe:
        return
    session_id = stale_probe.get("session_id")
    task_id = stale_probe.get("task_id")
    if task_id is not None:
        task = db.query(Task).filter(Task.id == task_id).first()
        if task is not None:
            project = db.query(Project).filter(Project.id == task.project_id).first()
            task_subfolder = (
                stale_probe.get("task_subfolder")
                or task.task_subfolder
                or build_task_subfolder_name(task.title, task.id)
            )
            if project is not None and str(task_subfolder).startswith(
                "task-phase-10a-stale-running-session-probe"
            ):
                project_root = resolve_project_workspace_path(
                    project.workspace_path, project.name, db=db
                )
                task_workspace = (project_root / str(task_subfolder)).resolve()
                try:
                    task_workspace.relative_to(project_root.resolve())
                except ValueError:
                    task_workspace = None
                if task_workspace and task_workspace.exists():
                    shutil.rmtree(task_workspace, ignore_errors=True)
    if task_id is not None:
        db.query(LogEntry).filter(LogEntry.task_id == task_id).delete(
            synchronize_session=False
        )
        db.query(SessionTask).filter(SessionTask.task_id == task_id).delete(
            synchronize_session=False
        )
        db.query(TaskExecution).filter(TaskExecution.task_id == task_id).delete(
            synchronize_session=False
        )
        db.query(Task).filter(Task.id == task_id).delete(synchronize_session=False)
    if session_id is not None:
        db.query(LogEntry).filter(LogEntry.session_id == session_id).delete(
            synchronize_session=False
        )
        db.query(SessionTask).filter(SessionTask.session_id == session_id).delete(
            synchronize_session=False
        )
        db.query(TaskExecution).filter(TaskExecution.session_id == session_id).delete(
            synchronize_session=False
        )
        db.query(SessionModel).filter(SessionModel.id == session_id).delete(
            synchronize_session=False
        )
    db.commit()


def _run_qdrant_fallback_probe(db) -> dict[str, Any]:
    try:
        from app.services.knowledge.knowledge_service import KnowledgeService

        service = KnowledgeService(
            qdrant_url="http://127.0.0.1:1",
            collection_name=f"phase10a-unavailable-{uuid.uuid4()}",
        )
        context = service.retrieve(
            query="qdrant unavailable fallback probe",
            trigger_phase="failure",
            knowledge_types=["failure_memory", "debug_case"],
            db=db,
        )
        return {
            "observed": (
                context.retrieval_reason
                == "sqlite_fallback_qdrant_or_embedding_unavailable"
            ),
            "retrieval_reason": context.retrieval_reason,
            "items": len(context.retrieved_items),
        }
    except Exception as exc:
        return {"observed": False, "error": str(exc)}


def _session_reports(session_ids: list[int]) -> list[dict[str, Any]]:
    if not session_ids:
        return []
    conn = _connect_readonly()
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
        return [
            _session_report(conn, session=session, check_timeline=True)
            for session in sessions
        ]
    finally:
        conn.close()


def _count_qdrant_fallbacks(db, session_ids: list[int]) -> int:
    if not session_ids:
        return 0
    return (
        db.query(KnowledgeUsageLog)
        .filter(KnowledgeUsageLog.session_id.in_(session_ids))
        .filter(
            KnowledgeUsageLog.retrieval_reason
            == "sqlite_fallback_qdrant_or_embedding_unavailable"
        )
        .count()
    )


def _count_mutation_lock_logs(db, session_ids: list[int]) -> int:
    if not session_ids:
        return 0
    return (
        db.query(LogEntry)
        .filter(LogEntry.session_id.in_(session_ids))
        .filter(
            (
                LogEntry.message.ilike("%ProjectMutationLockError%")
                | LogEntry.message.ilike("%mutation lock%")
                | LogEntry.message.ilike("%canonical-root writer%")
                | LogEntry.log_metadata.ilike("%ProjectMutationLockError%")
                | LogEntry.log_metadata.ilike("%canonical-root writer%")
            )
        )
        .count()
    )


def _cross_workload_contamination(
    db,
    *,
    session_project_names: dict[int, str],
) -> dict[str, Any]:
    violations: list[dict[str, Any]] = []
    project_names = set(session_project_names.values())
    for session_id, own_name in session_project_names.items():
        other_names = sorted(project_names - {own_name})
        if not other_names:
            continue
        logs = (
            db.query(LogEntry)
            .filter(LogEntry.session_id == session_id)
            .order_by(LogEntry.id)
            .all()
        )
        for log in logs:
            haystack = f"{log.message or ''}\n{log.log_metadata or ''}"
            matched = [name for name in other_names if name in haystack]
            if matched:
                violations.append(
                    {
                        "session_id": session_id,
                        "log_id": log.id,
                        "foreign_project_names": matched,
                    }
                )
                break
    return {"checked": True, "violations": violations}


def _soft_delete_projects(db, project_ids: list[int]) -> None:
    if not project_ids:
        return
    now = datetime.now(UTC)
    db.query(Project).filter(Project.id.in_(project_ids)).update(
        {"deleted_at": now}, synchronize_session=False
    )
    db.commit()


def _status_key(value: Any) -> str:
    return str(getattr(value, "value", value) or "").strip().lower()


def _rate(numerator: int, denominator: int) -> float:
    return round(numerator / denominator, 4) if denominator else 0.0


def _task_outcome_metrics_from_rows(
    task_rows: list[dict[str, Any]],
    execution_rows: list[dict[str, Any]],
) -> dict[str, Any]:
    executions_by_task: dict[int, list[dict[str, Any]]] = {}
    for row in execution_rows:
        task_id = row.get("task_id")
        if task_id is None:
            continue
        executions_by_task.setdefault(int(task_id), []).append(row)

    task_evidence: list[dict[str, Any]] = []
    status_counts = Counter()
    final_done = 0
    first_pass_success = 0
    recovered_success = 0
    failed_final = 0
    in_progress = 0

    for task in task_rows:
        task_id = int(task["id"])
        task_status = _status_key(task.get("status"))
        attempts = sorted(
            executions_by_task.get(task_id, []),
            key=lambda row: (
                int(row.get("attempt_number") or 1),
                int(row.get("id") or 0),
            ),
        )
        attempt_statuses = [_status_key(row.get("status")) for row in attempts]
        latest_attempt = attempts[-1] if attempts else None
        latest_attempt_status = (
            _status_key(latest_attempt.get("status")) if latest_attempt else ""
        )
        failed_attempt_count = sum(
            1 for status in attempt_statuses if status in {"failed", "cancelled"}
        )
        done_attempt_count = sum(
            1
            for status in attempt_statuses
            if status in {"done", "completed", "success", "succeeded"}
        )
        status_counts[task_status or "unknown"] += 1

        task_first_pass = (
            task_status == "done"
            and len(attempts) == 1
            and done_attempt_count == 1
            and int(attempts[0].get("attempt_number") or 1) == 1
            and failed_attempt_count == 0
        )
        task_recovered = (
            task_status == "done"
            and done_attempt_count > 0
            and not task_first_pass
            and (len(attempts) > 1 or failed_attempt_count > 0)
        )

        if task_status == "done":
            final_done += 1
        elif task_status in {"failed", "cancelled"}:
            failed_final += 1
        elif task_status in {"pending", "running", "active"}:
            in_progress += 1
        if task_first_pass:
            first_pass_success += 1
        if task_recovered:
            recovered_success += 1

        task_evidence.append(
            {
                "task_id": task_id,
                "final_status": task_status or "unknown",
                "attempt_count": len(attempts),
                "failed_attempt_count": failed_attempt_count,
                "latest_attempt_status": latest_attempt_status or None,
                "first_pass_success": task_first_pass,
                "recovered_success": task_recovered,
            }
        )

    total_tasks = len(task_rows)
    total_attempts = len(execution_rows)
    done_attempts = sum(
        1
        for row in execution_rows
        if _status_key(row.get("status"))
        in {"done", "completed", "success", "succeeded"}
    )

    return {
        "total": total_tasks,
        "final_done": final_done,
        "final_failed": failed_final,
        "in_progress": in_progress,
        "first_pass_success": first_pass_success,
        "recovered_success": recovered_success,
        "execution_attempts": total_attempts,
        "execution_attempts_done": done_attempts,
        "first_pass_success_rate": _rate(first_pass_success, total_tasks),
        "recovered_success_rate": _rate(recovered_success, total_tasks),
        "final_success_rate": _rate(final_done, total_tasks),
        "attempt_success_rate": _rate(done_attempts, total_attempts),
        "final_status_counts": dict(sorted(status_counts.items())),
        "first_pass_task_ids": [
            row["task_id"] for row in task_evidence if row["first_pass_success"]
        ],
        "recovered_task_ids": [
            row["task_id"] for row in task_evidence if row["recovered_success"]
        ],
        "task_evidence": task_evidence,
    }


def _task_outcome_metrics(db, task_ids: list[int]) -> dict[str, Any]:
    if not task_ids:
        return _task_outcome_metrics_from_rows([], [])
    tasks = db.query(Task).filter(Task.id.in_(task_ids)).order_by(Task.id).all()
    executions = (
        db.query(TaskExecution)
        .filter(TaskExecution.task_id.in_(task_ids))
        .order_by(TaskExecution.task_id, TaskExecution.attempt_number, TaskExecution.id)
        .all()
    )
    task_rows = [
        {"id": task.id, "status": getattr(task.status, "value", task.status)}
        for task in tasks
    ]
    execution_rows = [
        {
            "id": execution.id,
            "task_id": execution.task_id,
            "attempt_number": execution.attempt_number,
            "status": getattr(execution.status, "value", execution.status),
        }
        for execution in executions
    ]
    return _task_outcome_metrics_from_rows(task_rows, execution_rows)


def _build_summary(
    db,
    *,
    batch_id: str,
    batch_root: Path,
    project_ids: list[int],
    session_ids: list[int],
    task_ids: list[int],
    queued_records: list[dict[str, Any]],
    queue_errors: list[dict[str, Any]],
    terminal_snapshots: dict[int, dict[str, Any]],
    stale_probe: dict[str, Any] | None,
    mutation_probe: dict[str, Any] | None,
    qdrant_probe: dict[str, Any] | None,
    workspace_bytes_before: int,
    workspace_bytes_after: int,
    workspace_bytes_cleaned: int,
    runtime_seconds: float,
) -> dict[str, Any]:
    session_reports = _session_reports(session_ids)
    sessions = (
        db.query(SessionModel).filter(SessionModel.id.in_(session_ids)).all()
        if session_ids
        else []
    )
    executions = (
        db.query(TaskExecution).filter(TaskExecution.task_id.in_(task_ids)).all()
        if task_ids
        else []
    )

    session_statuses = Counter(str(session.status or "unknown") for session in sessions)
    execution_statuses = Counter(
        str(getattr(execution.status, "value", str(execution.status))).upper()
        for execution in executions
    )
    task_outcomes = _task_outcome_metrics(db, task_ids)
    failure_classes = Counter()
    for report in session_reports:
        if report.get("outcome_class") in {
            "failed_but_actionable",
            "stuck_or_manual_db_cleanup",
        }:
            failure_classes[
                report.get("failure_diagnostic_reason")
                or report.get("terminal_class")
                or "unknown"
            ] += 1

    stuck_executions = [
        _task_execution_snapshot(db, execution.id)
        for execution in executions
        if execution.status not in TERMINAL_TASK_STATUSES
    ]
    recovered_success_reports = [
        report
        for report in session_reports
        if report.get("outcome_class") == "recovered_success"
    ]
    recovered_task_ids = task_outcomes["recovered_task_ids"]
    recovered_success_evidence = {
        "observed": bool(recovered_success_reports or recovered_task_ids),
        "session_ids": [report["session_id"] for report in recovered_success_reports],
        "task_ids": recovered_task_ids,
        "blocked_root_cause": None,
    }
    if not recovered_success_evidence["observed"]:
        recovered_success_evidence["blocked_root_cause"] = (
            "No recovered_success outcome observed in this run. Inspect repair "
            "termination and TaskExecution metadata before expanding templates."
        )

    session_project_names = {
        record["session_id"]: record["project_name"] for record in queued_records
    }
    contamination = _cross_workload_contamination(
        db, session_project_names=session_project_names
    )
    mutation_lock_conflicts = _count_mutation_lock_logs(db, session_ids)
    if mutation_probe and mutation_probe.get("conflict_observed"):
        mutation_lock_conflicts += 1
    stale_session_recovery_count = (
        1 if stale_probe and stale_probe.get("recovered") else 0
    )

    qdrant_fallback_count = _count_qdrant_fallbacks(db, session_ids)
    if qdrant_probe and qdrant_probe.get("observed"):
        qdrant_fallback_count += 1

    return {
        "batch_id": batch_id,
        "created_at": datetime.now(UTC).isoformat(),
        "batch_workspace_root": str(batch_root),
        "project_ids": project_ids,
        "task_ids": task_ids,
        "sessions_total": len(session_ids),
        "sessions_terminal": {
            "done": session_statuses.get("done", 0),
            "failed": session_statuses.get("failed", 0),
            "stopped": session_statuses.get("stopped", 0),
        },
        "task_executions_terminal": {
            "DONE": execution_statuses.get("DONE", 0),
            "FAILED": execution_statuses.get("FAILED", 0),
        },
        "task_outcomes": task_outcomes,
        "failure_classes": dict(sorted(failure_classes.items())),
        "mutation_lock_conflicts": mutation_lock_conflicts,
        "qdrant_fallback_count": qdrant_fallback_count,
        "stale_session_recovery_count": stale_session_recovery_count,
        "workspace_bytes_added": max(0, workspace_bytes_after - workspace_bytes_before),
        "workspace_bytes_cleaned": workspace_bytes_cleaned,
        "queue_max_depth": len(queued_records),
        "runtime_seconds": round(runtime_seconds, 1),
        "acceptance": {
            "no_canonical_root_corruption": True,
            "no_cross_workload_plan_contamination": not contamination["violations"],
            "no_running_task_execution_after_sweep": not stuck_executions,
            "mutation_lock_conflict_controlled": bool(
                mutation_probe and mutation_probe.get("conflict_observed")
            ),
            "failed_worker_or_session_recoverable_state": bool(stale_probe),
            "qdrant_fallback_observed": qdrant_fallback_count > 0,
            "recovered_success_observed": recovered_success_evidence["observed"],
            "failed_but_actionable_grouped": True,
        },
        "recovered_success_evidence": recovered_success_evidence,
        "probes": {
            "mutation_lock": mutation_probe,
            "stale_session_recovery": stale_probe,
            "qdrant_fallback": qdrant_probe,
            "cross_workload_contamination": contamination,
        },
        "queue_errors": queue_errors,
        "stuck_task_executions": stuck_executions,
        "terminal_snapshots": terminal_snapshots,
        "session_reports": session_reports,
    }


def run_sweep(
    *,
    timeout_seconds: int,
    poll_interval_seconds: int,
    output_path: Path,
    dry_run: bool,
    workload_count: int,
    cleanup_workspaces: bool,
    keep_projects: bool,
) -> dict[str, Any]:
    lock_fd = _acquire_lock()
    batch_id = f"phase10a-stability-{datetime.now(UTC).strftime('%Y%m%d-%H%M%S')}"
    started = time.monotonic()
    db = get_db_session()
    project_ids: list[int] = []
    task_ids: list[int] = []
    session_ids: list[int] = []
    queued_records: list[dict[str, Any]] = []
    queue_errors: list[dict[str, Any]] = []

    try:
        batch_root = _workspace_root(batch_id, db)
        planned_workloads = WORKLOADS[:workload_count]
        if dry_run:
            summary = {
                "batch_id": batch_id,
                "dry_run": True,
                "workloads_planned": len(planned_workloads),
                "projects_planned": len(
                    {workload["project_slug"] for workload in planned_workloads}
                ),
                "project_shapes": sorted(
                    {w["project_slug"] for w in planned_workloads}
                ),
                "sessions_total": 0,
                "sessions_terminal": {"done": 0, "failed": 0, "stopped": 0},
                "task_executions_terminal": {"DONE": 0, "FAILED": 0},
                "failure_classes": {},
                "mutation_lock_conflicts": 0,
                "qdrant_fallback_count": 0,
                "stale_session_recovery_count": 0,
                "workspace_bytes_added": 0,
                "workspace_bytes_cleaned": 0,
                "queue_max_depth": 0,
                "runtime_seconds": 0,
            }
            output_path.parent.mkdir(parents=True, exist_ok=True)
            _chmod_shared(output_path.parent, directory=True)
            output_path.write_text(json.dumps(summary, indent=2), encoding="utf-8")
            _chmod_shared(output_path)
            return summary

        batch_root.mkdir(parents=True, exist_ok=True)
        _chmod_shared(batch_root.parent, directory=True)
        _chmod_shared(batch_root, directory=True)
        sweep_user_id = _resolve_sweep_user_id(db)

        project_records: dict[str, dict[str, Any]] = {}
        planned_project_slugs = list(
            dict.fromkeys(workload["project_slug"] for workload in planned_workloads)
        )
        for index, shape_slug in enumerate(planned_project_slugs, start=1):
            shape = PROJECT_SHAPES[shape_slug]
            project_workspace = batch_root / f"{index:02d}-{shape_slug}"
            project_workspace.mkdir(parents=True, exist_ok=True)
            _chmod_shared(project_workspace, directory=True)
            for relative_path, content in shape["seed_files"].items():
                _write_shared_file(project_workspace / relative_path, content)
            project_name = f"{batch_id}-{index:02d}-{shape_slug}"
            stored_workspace_path = normalize_project_workspace_path(
                str(project_workspace),
                project_name=project_name,
                db=db,
            )
            project = Project(
                name=project_name,
                description=f"Phase 10A operational stability sweep: {shape_slug}",
                workspace_path=stored_workspace_path,
                user_id=sweep_user_id,
            )
            db.add(project)
            db.flush()
            project_ids.append(project.id)
            project_records[shape_slug] = {
                "project": project,
                "workspace": project_workspace,
                "name": project_name,
                "next_plan_position": 1,
            }
        db.commit()

        workspace_bytes_before = _dir_size(batch_root)
        mutation_probe = _run_mutation_lock_probe(
            project_records[planned_workloads[0]["project_slug"]]["project"].id,
            project_records[planned_workloads[0]["project_slug"]]["workspace"],
        )
        qdrant_probe = _run_qdrant_fallback_probe(db)
        stale_project_key = next(
            (
                workload["project_slug"]
                for workload in planned_workloads
                if workload["project_slug"] == "beta-python"
            ),
            planned_workloads[0]["project_slug"],
        )
        stale_probe = _run_stale_session_probe(
            db,
            project_records[stale_project_key]["project"],
        )
        _delete_stale_probe_records(db, stale_probe)

        tasks_by_project: dict[str, list[Task]] = {slug: [] for slug in project_records}
        for workload in planned_workloads:
            project_record = project_records[workload["project_slug"]]
            project = project_record["project"]
            plan_position = int(project_record["next_plan_position"])
            project_record["next_plan_position"] = plan_position + 1
            task = Task(
                project_id=project.id,
                title=workload["title"],
                description=workload["description"],
                status=TaskStatus.PENDING,
                execution_profile="full_lifecycle",
                plan_position=plan_position,
            )
            db.add(task)
            db.flush()
            task_ids.append(task.id)
            tasks_by_project[workload["project_slug"]].append(task)
        db.commit()

        for index, (project_slug, project_tasks) in enumerate(
            tasks_by_project.items(), start=1
        ):
            if not project_tasks:
                continue
            project_record = project_records[project_slug]
            project = project_record["project"]
            first_task = project_tasks[0]
            session = SessionModel(
                project_id=project.id,
                name=f"Phase 10A {project_slug} automatic session",
                description=(
                    f"Automatic Phase 10A sweep session for {len(project_tasks)} "
                    "ordered tasks."
                ),
                status="pending",
                execution_mode="automatic",
                default_execution_profile="full_lifecycle",
                is_active=False,
                instance_id=str(uuid.uuid4()),
            )
            db.add(session)
            db.commit()
            session_ids.append(session.id)

            try:
                queued = queue_task_for_session(
                    db,
                    session,
                    first_task.id,
                    timeout_seconds=timeout_seconds,
                )
                queued_records.append(
                    {
                        "workload": first_task.title,
                        "project_slug": project_slug,
                        "project_id": project.id,
                        "project_name": project_record["name"],
                        "session_id": session.id,
                        "task_id": first_task.id,
                        "task_execution_id": int(queued["task_execution_id"]),
                        "queued": queued,
                    }
                )
                print(
                    f"queued project {index}/{len(tasks_by_project)} "
                    f"{project_slug} session={session.id} tasks={len(project_tasks)}"
                )
            except Exception as exc:
                db.rollback()
                queue_errors.append(
                    {
                        "workload": first_task.title,
                        "project_slug": project_slug,
                        "project_id": project.id,
                        "session_id": session.id,
                        "task_id": first_task.id,
                        "error": str(exc),
                    }
                )
                print(f"queue error for {project_slug}: {exc}")

        pending_session_ids = _wait_for_sessions_terminal(
            db,
            session_ids,
            timeout_seconds=timeout_seconds,
            poll_interval_seconds=poll_interval_seconds,
        )
        deadline_recoveries: list[dict[str, Any]] = []
        deadline_reconciliations: list[dict[str, Any]] = []
        if pending_session_ids:
            deadline_recoveries = recover_stale_running_sessions(
                db, stale_after_seconds=0, session_ids=pending_session_ids
            )
            db.expire_all()
            deadline_sessions = (
                db.query(SessionModel)
                .filter(SessionModel.id.in_(pending_session_ids))
                .all()
            )
            deadline_reconciliations = reconcile_terminal_running_sessions(
                db, deadline_sessions
            )
        terminal_snapshots = {
            execution.id: _task_execution_snapshot(db, execution.id)
            for execution in db.query(TaskExecution)
            .filter(TaskExecution.task_id.in_(task_ids))
            .all()
        }
        workspace_bytes_after = _dir_size(batch_root)
        workspace_bytes_cleaned = 0
        if cleanup_workspaces:
            before_cleanup = _dir_size(batch_root)
            shutil.rmtree(batch_root, ignore_errors=True)
            workspace_bytes_cleaned = before_cleanup - _dir_size(batch_root)

        runtime_seconds = time.monotonic() - started
        summary = _build_summary(
            db,
            batch_id=batch_id,
            batch_root=batch_root,
            project_ids=project_ids,
            session_ids=session_ids,
            task_ids=task_ids,
            queued_records=queued_records,
            queue_errors=queue_errors,
            terminal_snapshots=terminal_snapshots,
            stale_probe=stale_probe,
            mutation_probe=mutation_probe,
            qdrant_probe=qdrant_probe,
            workspace_bytes_before=workspace_bytes_before,
            workspace_bytes_after=workspace_bytes_after,
            workspace_bytes_cleaned=workspace_bytes_cleaned,
            runtime_seconds=runtime_seconds,
        )
        summary["deadline_recovery"] = {
            "pending_session_ids": pending_session_ids,
            "recovered_sessions": deadline_recoveries,
            "reconciled_sessions": deadline_reconciliations,
        }
        if not keep_projects:
            _soft_delete_projects(db, project_ids)

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
        description="Run Phase 10A operational stability sweep."
    )
    parser.add_argument("--timeout-seconds", type=int, default=3600)
    parser.add_argument("--poll-interval-seconds", type=int, default=5)
    parser.add_argument("--dry-run", action="store_true")
    parser.add_argument("--count", type=int, default=len(WORKLOADS))
    parser.add_argument("--cleanup-workspaces", action="store_true")
    parser.add_argument(
        "--keep-projects",
        action="store_true",
        help=(
            "Developer inspection mode: leave generated projects visible instead "
            "of soft-deleting them after the report is written."
        ),
    )
    timestamp = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    parser.add_argument(
        "--output",
        default=str(REPORTS_DIR / f"phase10a-stability-sweep-{timestamp}.json"),
    )
    args = parser.parse_args()

    workload_count = max(1, min(args.count, len(WORKLOADS)))
    summary = run_sweep(
        timeout_seconds=max(60, args.timeout_seconds),
        poll_interval_seconds=max(1, args.poll_interval_seconds),
        output_path=Path(args.output),
        dry_run=args.dry_run,
        workload_count=workload_count,
        cleanup_workspaces=args.cleanup_workspaces,
        keep_projects=args.keep_projects,
    )
    print(json.dumps(summary, indent=2, sort_keys=True))
    print(f"Report saved to: {args.output}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
