#!/usr/bin/env python3
"""E20: Sustained traffic validation — 16 sequential tasks across E18-loaded worker."""

from __future__ import annotations

import json
import re
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib import error, request as urllib_request

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = "http://localhost:8080/api/v1"
LOG_FILE = Path("/root/.openclaw/workspace/vault/projects/orchestrator/logs/worker.log")
FIXTURE_ROOT = Path(
    "/root/.openclaw/workspace/vault/projects/orchestrator/scripts/evals/fixtures"
)
WORKSPACE_ROOT = Path("/root/.openclaw/workspace/vault/projects")
OUTPUT_FILE = Path("/tmp/e20-results.json")

TASK_TIMEOUT = 720   # seconds per task
POLL_INTERVAL = 15   # seconds between polls

TERMINAL_STATUSES = frozenset(
    {"completed", "stopped", "failed", "cancelled", "canceled", "paused"}
)

# 16-task corpus (mix of fixture types, sequential)
# medium_cli_multi_file_feature: dominant PRCF source from E17 (6 tasks)
# python_cli_small_feature: stale_replace / oscillation (4 tasks)
# tiny_slug_source_rewrite: test assertion preservation, no-new-file (3 tasks)
# tiny_money_source_rewrite: placeholder-type source rewrite (3 tasks)
TASK_CORPUS = [
    # --- medium_cli_multi_file_feature (6) ---
    {"id": "E20-M1", "fixture": "medium_cli_multi_file_feature",
     "prompt": "Add the summary command to this Python CLI. The command should print a compact summary of the current task list as \"3 tasks, 2 complete\". Keep the change scoped to the existing src/ and tests/ files. The feature should use the existing TaskStore and formatting module instead of hard-coding the output in the CLI. Verify with python3 -m pytest -q."},
    {"id": "E20-M2", "fixture": "medium_cli_multi_file_feature",
     "prompt": "Add the summary command to this Python CLI. The command should print a compact summary of the current task list as \"3 tasks, 2 complete\". Keep the change scoped to the existing src/ and tests/ files. The feature should use the existing TaskStore and formatting module instead of hard-coding the output in the CLI. Verify with python3 -m pytest -q."},
    {"id": "E20-M3", "fixture": "medium_cli_multi_file_feature",
     "prompt": "Add the summary command to this Python CLI. The command should print a compact summary of the current task list as \"3 tasks, 2 complete\". Keep the change scoped to the existing src/ and tests/ files. The feature should use the existing TaskStore and formatting module instead of hard-coding the output in the CLI. Verify with python3 -m pytest -q."},
    {"id": "E20-M4", "fixture": "medium_cli_multi_file_feature",
     "prompt": "Add the summary command to this Python CLI. The command should print a compact summary of the current task list as \"3 tasks, 2 complete\". Keep the change scoped to the existing src/ and tests/ files. The feature should use the existing TaskStore and formatting module instead of hard-coding the output in the CLI. Verify with python3 -m pytest -q."},
    {"id": "E20-M5", "fixture": "medium_cli_multi_file_feature",
     "prompt": "Add the summary command to this Python CLI. The command should print a compact summary of the current task list as \"3 tasks, 2 complete\". Keep the change scoped to the existing src/ and tests/ files. The feature should use the existing TaskStore and formatting module instead of hard-coding the output in the CLI. Verify with python3 -m pytest -q."},
    {"id": "E20-M6", "fixture": "medium_cli_multi_file_feature",
     "prompt": "Add the summary command to this Python CLI. The command should print a compact summary of the current task list as \"3 tasks, 2 complete\". Keep the change scoped to the existing src/ and tests/ files. The feature should use the existing TaskStore and formatting module instead of hard-coding the output in the CLI. Verify with python3 -m pytest -q."},
    # --- python_cli_small_feature (4) ---
    {"id": "E20-P1", "fixture": "python_cli_small_feature",
     "prompt": "Add the --uppercase option to this small Python CLI. When the flag is present, the CLI should uppercase the message before printing it. Keep changes scoped to src/ and tests/. Verify with python3 -m pytest -q."},
    {"id": "E20-P2", "fixture": "python_cli_small_feature",
     "prompt": "Add the --uppercase option to this small Python CLI. When the flag is present, the CLI should uppercase the message before printing it. Keep changes scoped to src/ and tests/. Verify with python3 -m pytest -q."},
    {"id": "E20-P3", "fixture": "python_cli_small_feature",
     "prompt": "Add the --uppercase option to this small Python CLI. When the flag is present, the CLI should uppercase the message before printing it. Keep changes scoped to src/ and tests/. Verify with python3 -m pytest -q."},
    {"id": "E20-P4", "fixture": "python_cli_small_feature",
     "prompt": "Add the --uppercase option to this small Python CLI. When the flag is present, the CLI should uppercase the message before printing it. Keep changes scoped to src/ and tests/. Verify with python3 -m pytest -q."},
    # --- tiny_slug_source_rewrite (3) ---
    {"id": "E20-S1", "fixture": "tiny_slug_source_rewrite",
     "prompt": "Fix the existing slug formatter in src/tiny_slug/slug.py so the existing tests pass. Edit only that source file. Do not create new files. Do not edit tests. Verify with python3 -m pytest -q."},
    {"id": "E20-S2", "fixture": "tiny_slug_source_rewrite",
     "prompt": "Fix the existing slug formatter in src/tiny_slug/slug.py so the existing tests pass. Edit only that source file. Do not create new files. Do not edit tests. Verify with python3 -m pytest -q."},
    {"id": "E20-S3", "fixture": "tiny_slug_source_rewrite",
     "prompt": "Fix the existing slug formatter in src/tiny_slug/slug.py so the existing tests pass. Edit only that source file. Do not create new files. Do not edit tests. Verify with python3 -m pytest -q."},
    # --- tiny_money_source_rewrite (3) ---
    {"id": "E20-N1", "fixture": "tiny_money_source_rewrite",
     "prompt": "Fix the existing money formatter in src/tiny_money/money.py so the existing tests pass. Edit only that source file. Do not create new files. Do not edit tests. Verify with python3 -m pytest -q."},
    {"id": "E20-N2", "fixture": "tiny_money_source_rewrite",
     "prompt": "Fix the existing money formatter in src/tiny_money/money.py so the existing tests pass. Edit only that source file. Do not create new files. Do not edit tests. Verify with python3 -m pytest -q."},
    {"id": "E20-N3", "fixture": "tiny_money_source_rewrite",
     "prompt": "Fix the existing money formatter in src/tiny_money/money.py so the existing tests pass. Edit only that source file. Do not create new files. Do not edit tests. Verify with python3 -m pytest -q."},
]


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------


def _get_token() -> str:
    """Generate a fresh access token using the app's auth module."""
    repo_root = Path(__file__).parent.parent.parent
    sys.path.insert(0, str(repo_root))
    from app.database import SessionLocal
    from app.models import User
    from app.auth import create_access_token
    from datetime import timedelta

    db = SessionLocal()
    user = db.query(User).filter_by(email="eval@local.dev").first()
    token = create_access_token(
        data={"sub": user.email}, expires_delta=timedelta(hours=12)
    )
    db.close()
    return token


def _api(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    url = f"{BASE_URL}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib_request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib_request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:200]}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"URLError: {exc.reason}") from exc
    return json.loads(raw) if raw.strip() else {}


def _wait_terminal(session_id: int, token: str) -> dict:
    deadline = time.monotonic() + TASK_TIMEOUT
    while time.monotonic() < deadline:
        s = _api("GET", f"sessions/{session_id}", token)
        if str(s.get("status", "")).lower() in TERMINAL_STATUSES:
            return s
        time.sleep(POLL_INTERVAL)
    return _api("GET", f"sessions/{session_id}", token)


def _fresh_workspace(fixture: str, tag: str) -> Path:
    fixture_dir = FIXTURE_ROOT / fixture
    dest = WORKSPACE_ROOT / f"{fixture.replace('_', '-')}-{tag}"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(fixture_dir, dest)
    for p in dest.rglob("*"):
        try:
            p.chmod(0o777 if p.is_dir() else 0o666)
        except OSError:
            pass
    dest.chmod(0o777)
    return dest


# ---------------------------------------------------------------------------
# Per-task metric extraction from worker.log
# ---------------------------------------------------------------------------


def _extract_repair_metrics(task_id: int) -> list[dict]:
    """Read worker.log lines for repair_prompt_chars for this task_id."""
    pattern = re.compile(
        r"session_id=\d+ task_id=" + str(task_id) + r"\s+repair_prompt_chars=(\d+)"
    )
    failure_pattern = re.compile(
        r"task_id=" + str(task_id) + r"\s+failure_type=(\S+)"
    )
    repairs: list[dict] = []
    failure_type: str | None = None
    try:
        lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
    except OSError:
        return repairs
    for line in lines:
        m = pattern.search(line)
        if m:
            repairs.append({"repair_prompt_chars": int(m.group(1)), "line": line.strip()})
        fm = failure_pattern.search(line)
        if fm:
            failure_type = fm.group(1)
    if failure_type and repairs:
        repairs[-1]["failure_type"] = failure_type
    return repairs


def _classify_failure(session: dict, failure_type_log: str | None) -> str:
    """Return a short failure category string."""
    err = str(session.get("error_message") or "").lower()
    fail_cat = str(session.get("failure_category") or "").lower()
    status = str(session.get("status") or "").lower()
    if status == "completed":
        return "completed"
    # PRCF markers
    prcf_markers = [
        "planning repair still produced invalid commands",
        "planning repair for an implementation-heavy task",
        "post_repair_task1_bootstrap_contract",
        "repair_candidate_rejected",
        "brittle",
        "placeholder",
        "heredoc",
        "planning failed 3 time",
    ]
    if any(m in err for m in prcf_markers):
        return "PRCF"
    if "planning repair root cause oscillated" in err or "oscillation" in err:
        return "PRCF_oscillation"
    if "planning json parse failed" in err or "planning_json_error" in (failure_type_log or ""):
        return "planning_json_error"
    if "backend" in err and "capacity" in err:
        return "backend_capacity"
    if "timed out" in err:
        return "execution_timeout"
    if "restore" in err and "workspace" in err:
        return "workspace_restore"
    if "planning repair budget exceeded" in err:
        return "budget_failure"
    if "planning failed" in err:
        return "planning_failure"
    if fail_cat:
        return fail_cat
    if "verification_integrity" in err:
        return "verification_integrity"
    return "other_failed"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    token = _get_token()
    print(f"[E20] Token obtained. Running {len(TASK_CORPUS)} sequential tasks.")

    results: list[dict] = []
    ts = datetime.now(UTC).strftime("%Y%m%d%H%M")

    for i, task_spec in enumerate(TASK_CORPUS):
        tag = f"{ts}-{i+1:02d}"
        fixture = task_spec["fixture"]
        tid = task_spec["id"]
        print(f"\n[E20] ({i+1}/{len(TASK_CORPUS)}) {tid} fixture={fixture}", flush=True)

        # Provision workspace
        try:
            ws = _fresh_workspace(fixture, tag)
        except Exception as exc:
            print(f"  workspace provision failed: {exc}")
            results.append({"id": tid, "fixture": fixture, "error": str(exc), "category": "infra_error"})
            continue

        # Create project
        try:
            proj = _api("POST", "projects", token, {
                "name": f"E20 {tid}",
                "description": f"E20 sustained traffic {tid}",
                "workspace_path": str(ws),
            })
            proj_id = proj["id"]

            task_obj = _api("POST", "tasks", token, {
                "project_id": proj_id,
                "title": task_spec["id"],
                "description": task_spec["prompt"],
                "priority": 0,
                "plan_position": 1,
            })
            task_id = task_obj["id"]

            session = _api("POST", "sessions", token, {
                "project_id": proj_id,
                "name": f"e20-{task_spec['id'].lower()}-{tag}",
                "execution_mode": "manual",
                "default_execution_profile": "full_lifecycle",
            })
            session_id = session["id"]

            _api("POST", f"sessions/{session_id}/tasks/{task_id}/run", token)
            print(f"  session={session_id} task={task_id} dispatched — waiting...", flush=True)
        except Exception as exc:
            print(f"  dispatch failed: {exc}")
            results.append({"id": tid, "fixture": fixture, "error": str(exc), "category": "dispatch_error"})
            continue

        # Wait for terminal state
        try:
            final = _wait_terminal(session_id, token)
        except Exception as exc:
            print(f"  wait failed: {exc}")
            final = {}

        # Collect repair metrics from log
        repair_log = _extract_repair_metrics(task_id)
        failure_type_log = repair_log[-1].get("failure_type") if repair_log else None

        category = _classify_failure(final, failure_type_log)
        repair_prompt_chars = [r["repair_prompt_chars"] for r in repair_log]
        budget_exceeded = any(c > 6000 for c in repair_prompt_chars)

        rec = {
            "id": tid,
            "fixture": fixture,
            "task_id": task_id,
            "session_id": session_id,
            "status": final.get("status", "unknown"),
            "failure_category": final.get("failure_category"),
            "error_message": str(final.get("error_message") or "")[:200],
            "category": category,
            "repair_invocations": len(repair_log),
            "repair_prompt_chars": repair_prompt_chars,
            "budget_exceeded": budget_exceeded,
        }
        results.append(rec)

        prcf = category.startswith("PRCF")
        print(
            f"  → status={rec['status']} category={category} "
            f"repairs={len(repair_log)} "
            f"chars={repair_prompt_chars} "
            f"budget_exceeded={budget_exceeded}",
            flush=True,
        )

    # Write results
    OUTPUT_FILE.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\n[E20] Done. Results → {OUTPUT_FILE}")

    # Summary
    cats = {}
    for r in results:
        cats[r["category"]] = cats.get(r["category"], 0) + 1
    print("[E20] Category summary:", json.dumps(cats, indent=2))


if __name__ == "__main__":
    main()
