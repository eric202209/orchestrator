#!/usr/bin/env python3
"""Phase 10K-P2 live evidence pilot runner.

Creates one pilot project with 20 real Orchestrator tasks, dispatches them
through the live API queue, and writes a maintenance report with observed
metrics and findings.
"""

from __future__ import annotations

import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

REPO_ROOT = Path(__file__).resolve().parents[2]
import sys

sys.path.insert(0, str(REPO_ROOT))

from app.auth import create_access_token  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import (  # noqa: E402
    HumanGuidanceConflict,
    HumanGuidanceUsage,
    LogEntry,
    PermissionRequest,
    Project,
    Session as SessionModel,
    TaskExecution,
)
import requests  # noqa: E402

BASE_URL = os.environ.get("ORCHESTRATOR_BASE_URL", "http://127.0.0.1:8080")
USER_EMAIL = os.environ.get("ORCHESTRATOR_USER_EMAIL", "eval@local.dev")
WORKSPACE_BASE = Path("/root/.openclaw/workspace/vault/projects")
REPORT_DIR = REPO_ROOT / "docs/roadmap/reports/maintenance"
REPORT_PATH = REPORT_DIR / "phase10k-p2-live-orchestrator-evidence-collection-20260618.md"
JSON_PATH = REPORT_DIR / "phase10k-p2-live-orchestrator-evidence-collection-20260618.json"


TASKS: list[dict[str, str]] = [
    {
        "title": "Update SETUP docs with ops endpoints",
        "description": "Update SETUP.md to document GET /ops/audit-events, GET /ops/queue-latency, and task token fields. Keep the change limited to setup documentation.",
    },
    {
        "title": "Add ADR for HG conflict detection",
        "description": "Add a new ADR in docs/adr describing advisory-only human guidance conflict detection for the pilot. Use the existing ADR format and avoid creating a new docs subdirectory.",
    },
    {
        "title": "Queue latency null handling test",
        "description": "Add a targeted test for GET /ops/queue-latency that verifies NULL queue latency values are excluded from average and max calculations. Keep the fix minimal and preserve existing tests.",
    },
    {
        "title": "Audit events since filter validation",
        "description": "Update the audit-events endpoint so malformed since values return HTTP 422 instead of being ignored. Add or update the smallest possible test coverage.",
    },
    {
        "title": "Session list queue badge",
        "description": "Add a queue latency badge to the session list UI using existing badge patterns. Do not introduce a new component directory.",
    },
    {
        "title": "ClawMobile permission timestamp",
        "description": "Add a created_at timestamp to the ClawMobile permission approval screen using the existing formatting utility.",
    },
    {
        "title": "Mutable default guidance trigger",
        "description": "Add an extra_context parameter to a planning utility. The task intentionally references def foo(items=[]): and should trigger the mutable default guidance. Implement the safe None-default form.",
    },
    {
        "title": "Missing symbol guidance trigger",
        "description": "Implement add_missing_function(...) even though it does not exist in the repository. The task should trigger requested symbol verification and stop instead of stubbing.",
    },
    {
        "title": "Delete-file approval flow",
        "description": "Delete an obsolete file in the workspace promotion path and require operator approval before promotion proceeds.",
    },
    {
        "title": "Rename-file approval flow",
        "description": "Rename a file in the workspace and keep the change narrowly scoped so the approval flow can observe the rename.",
    },
    {
        "title": "Move-file approval flow",
        "description": "Move a file to a neighboring module location and preserve behavior. Keep the diff minimal.",
    },
    {
        "title": "Add backend validation test",
        "description": "Add a focused backend test for validation behavior around task promotion, using the existing test style and preserving passing tests.",
    },
    {
        "title": "Fix failing guidance test",
        "description": "Investigate and fix a failing guidance-related test without deleting or weakening the assertion. Keep the patch narrow.",
    },
    {
        "title": "Update docs for workflow stages",
        "description": "Update a docs page to explain workflow stages and keep the edit limited to one documentation file.",
    },
    {
        "title": "Add validation guard",
        "description": "Add a small validation guard in a backend endpoint so invalid input is rejected earlier. Do not refactor unrelated code.",
    },
    {
        "title": "Add safe backend utility",
        "description": "Add a small backend utility function in the service layer with explicit typing and a targeted test.",
    },
    {
        "title": "Add output formatting test",
        "description": "Add a test that checks output formatting for a helper used by the backend or worker. Keep the implementation direct.",
    },
    {
        "title": "Update SETUP troubleshooting section",
        "description": "Update SETUP.md troubleshooting notes with one additional operational note. Avoid touching unrelated onboarding sections.",
    },
    {
        "title": "Add admin audit note",
        "description": "Add a short admin note that documents where to inspect audit and queue metrics during the pilot.",
    },
    {
        "title": "Cross-file cleanup task",
        "description": "Make a minimal cross-file cleanup related to the pilot runtime without broad refactoring.",
    },
]


def _api(token: str, method: str, path: str, **kwargs) -> Any:
    headers = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    r = requests.request(method, f"{BASE_URL}{path}", headers=headers, timeout=30, **kwargs)
    r.raise_for_status()
    return r.json()


def _counts(project_id: int) -> dict[str, int]:
    with SessionLocal() as db:
        session_ids = [row[0] for row in db.query(SessionModel.id).filter(SessionModel.project_id == project_id).all()]
        return {
            "task_executions": db.query(TaskExecution).filter(TaskExecution.session_id.in_(session_ids)).count(),
            "human_guidance_usage": db.query(HumanGuidanceUsage).filter(HumanGuidanceUsage.project_id == project_id).count(),
            "human_guidance_conflicts": db.query(HumanGuidanceConflict).filter(HumanGuidanceConflict.project_id == project_id).count(),
            "permission_requests": db.query(PermissionRequest).filter(PermissionRequest.project_id == project_id).count(),
            "log_entries": db.query(LogEntry).filter(LogEntry.session_id.in_(session_ids)).count(),
        }


def main() -> None:
    token = create_access_token({"sub": USER_EMAIL})
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    slug = f"phase10k-p2-live-pilot-{stamp}"
    workspace = str(WORKSPACE_BASE / slug)
    project = _api(token, "POST", "/api/v1/projects", json={
        "name": slug,
        "description": "Phase 10K-P2 live orchestrator evidence pilot",
        "workspace_path": workspace,
    })
    project_id = int(project["id"])
    print(f"[project] {project_id} {slug}")

    created_tasks: list[dict[str, Any]] = []
    for i, task in enumerate(TASKS, start=1):
        created = _api(token, "POST", "/api/v1/tasks", json={
            "project_id": project_id,
            "title": task["title"],
            "description": task["description"],
            "plan_position": i,
            "execution_profile": "full_lifecycle",
        })
        created_tasks.append(created)
        print(f"[task] {created['id']} {created['title']}")

    report_rows: list[dict[str, Any]] = []
    for task in created_tasks:
        task_id = int(task["id"])
        print(f"[dispatch] task={task_id}")
        _api(token, "POST", f"/api/v1/tasks/{task_id}/retry", json={})
        deadline = time.monotonic() + 7200
        last_status = None
        while time.monotonic() < deadline:
            current = _api(token, "GET", f"/api/v1/tasks/{task_id}")
            last_status = str(current.get("status") or "")
            if last_status in {"done", "failed", "blocked_prior_task_failed", "cancelled", "canceled"}:
                break
            time.sleep(15)
        counts = _counts(project_id)
        report_rows.append({
            "task_id": task_id,
            "title": task["title"],
            "status": last_status,
            "counts": counts,
        })
        print(f"[status] task={task_id} status={last_status} counts={counts}")

    final_counts = _counts(project_id)
    summary = {
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "project_id": project_id,
        "project_name": slug,
        "counts": final_counts,
        "tasks": report_rows,
        "targets": {
            "task_executions": 50,
            "human_guidance_usage": 200,
            "human_guidance_conflicts": 10,
            "permission_requests": 5,
            "log_entries": 500,
        },
    }
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    REPORT_DIR.chmod(0o755)
    JSON_PATH.write_text(json.dumps(summary, indent=2), encoding="utf-8")
    JSON_PATH.chmod(0o666)
    report_text = f"""# Phase 10K-P2 Live Orchestrator Evidence Collection

**Date:** {datetime.now(timezone.utc).date().isoformat()}
**Operator account:** `{USER_EMAIL}`
**Scope:** Live evidence collection for the Orchestrator repository itself
**Status:** Live pilot completed

## Summary

The pilot project `{slug}` was created and 20 tasks were dispatched through the live API queue.

## Final Counts

```json
{json.dumps(summary, indent=2)}
```

## Findings

- The pilot ran under `eval@local.dev`.
- The live campaign produced runtime evidence for task execution, but the final metrics must be read from the JSON bundle above.
- The task mix intentionally included guidance triggers, missing-symbol triggers, and permission-oriented workspace changes.

## Next Step

Review the task-by-task outcomes in the JSON bundle and compare the counts against the Phase 10K-P2 thresholds.
"""
    REPORT_PATH.write_text(report_text, encoding="utf-8")
    REPORT_PATH.chmod(0o666)
    print(json.dumps(summary, indent=2))


if __name__ == "__main__":
    main()
