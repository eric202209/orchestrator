#!/usr/bin/env python3
"""Priority 8 Arm B — 15-task pilot validation runner.

Runs 15 tasks serially through the orchestrator API with
REDUCED_PLANNING_PROMPT_ENABLED=True already set in .env and celery workers
restarted. Records per-task metrics and computes pilot summary.

Usage:
  python scripts/evals/run_arm_b_15task_pilot.py --token <TOKEN>
"""
from __future__ import annotations

import argparse
import json
import time
from datetime import UTC, datetime
from pathlib import Path
from typing import Any
from urllib import error, request as urllib_request

# ---------------------------------------------------------------------------
# 15-task corpus (4 tiny / 5 small / 4 medium / 2 large)
# ---------------------------------------------------------------------------

TASKS = [
    # --- TINY (≤75 chars) ---
    {
        "id": "T01",
        "band": "tiny",
        "title": "Add __str__ to Money",
        "description": "Add a __str__ method to a Money class in money.py that returns 'amount currency'.",
    },
    {
        "id": "T02",
        "band": "tiny",
        "title": "Create empty __init__",
        "description": "Create an empty tests/__init__.py file.",
    },
    {
        "id": "T03",
        "band": "tiny",
        "title": "Add .gitignore",
        "description": "Create a .gitignore that ignores __pycache__, *.pyc, and .env files.",
    },
    {
        "id": "T04",
        "band": "tiny",
        "title": "Create hello.py",
        "description": "Create hello.py with a main() function that prints 'Hello, world!'.",
    },
    # --- SMALL (76–150 chars) ---
    {
        "id": "T05",
        "band": "small",
        "title": "Add --verbose CLI flag",
        "description": "Add a --verbose CLI argument to a script cli.py using argparse. Print 'Verbose mode on' when --verbose is passed.",
    },
    {
        "id": "T06",
        "band": "small",
        "title": "Create parse_date helper",
        "description": "Create utils.py with a parse_date(s: str) -> datetime function using datetime.fromisoformat. Add a pytest test in tests/test_utils.py.",
    },
    {
        "id": "T07",
        "band": "small",
        "title": "Add requirements.txt",
        "description": "Create requirements.txt listing fastapi>=0.100.0, uvicorn>=0.20.0, and pytest>=7.0.0 as dependencies.",
    },
    {
        "id": "T08",
        "band": "small",
        "title": "Write conftest fixture",
        "description": "Create tests/conftest.py with a pytest fixture named sample_data that returns a list of three integers [1, 2, 3].",
    },
    {
        "id": "T09",
        "band": "small",
        "title": "Create config.py",
        "description": "Create config.py with a Config dataclass holding app_name: str and debug: bool = False. Add a test in tests/test_config.py that instantiates Config.",
    },
    # --- MEDIUM (151–250 chars) ---
    {
        "id": "T10",
        "band": "medium",
        "title": "FastAPI GET /health",
        "description": "Create a FastAPI app in app.py with a GET /health endpoint that returns {\"status\": \"ok\", \"uptime\": <seconds since startup>}. Include uvicorn as a dependency in requirements.txt and write a pytest test using TestClient that calls /health and asserts status is ok.",
    },
    {
        "id": "T11",
        "band": "medium",
        "title": "Add pagination to list endpoint",
        "description": "Add skip and limit query parameters to a GET /items endpoint in api.py. Items are read from a hardcoded list. Return the sliced sublist. Write a test in tests/test_api.py verifying skip=1&limit=2 returns the correct slice.",
    },
    {
        "id": "T12",
        "band": "medium",
        "title": "Create CSV reader module",
        "description": "Create csv_reader.py with a read_csv(path: str) -> list[dict] function that reads a CSV file using the csv module and returns rows as dicts. Write tests/test_csv_reader.py that creates a temp CSV, calls the function, and asserts the result.",
    },
    {
        "id": "T13",
        "band": "medium",
        "title": "Implement retry decorator",
        "description": "Create retry.py with a retry(max_attempts: int = 3, delay: float = 0.0) decorator that retries the decorated function on exception up to max_attempts times. Write tests/test_retry.py that tests a failing function recovers after 2 retries.",
    },
    # --- LARGE (>250 chars) ---
    {
        "id": "T14",
        "band": "large",
        "title": "CLI summary with filters",
        "description": "Build a CLI tool in cli_summary.py using argparse with a summary subcommand that reads a JSON file of records (each with name, amount, category fields), supports --category filter and --min-amount filter, and prints a formatted table to stdout. Include tests in tests/test_cli_summary.py that write a temp JSON file and call the CLI via subprocess, asserting filtered output.",
    },
    {
        "id": "T15",
        "band": "large",
        "title": "FastAPI CRUD with SQLite",
        "description": "Build a FastAPI app in app.py with SQLite backend using sqlite3 directly (no ORM). Implement POST /items (creates item with name and value), GET /items (lists all), and DELETE /items/{id}. Initialize the DB table in a setup_db() function called at startup. Write tests using TestClient in tests/test_app.py that create, list, and delete items, and run pytest to verify all pass.",
    },
]

BASE_URL = "http://127.0.0.1:8080/api/v1"
TASK_TIMEOUT_SECONDS = 600
POLL_INTERVAL_SECONDS = 8.0
WORKSPACE_ROOT = Path("/root/.openclaw/workspace/vault/projects")


def _api(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    url = f"{BASE_URL}/{path.lstrip('/')}"
    body = json.dumps(payload).encode() if payload else None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib_request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib_request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
    except error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {method} {path}: {detail}") from exc
    return json.loads(raw) if raw.strip() else {}


def _wait_terminal(session_id: int, token: str) -> dict:
    terminal = {"completed", "stopped", "failed", "cancelled", "canceled", "paused"}
    deadline = time.monotonic() + TASK_TIMEOUT_SECONDS
    while time.monotonic() < deadline:
        s = _api("GET", f"sessions/{session_id}", token)
        if str(s.get("status", "")).lower() in terminal:
            return s
        time.sleep(POLL_INTERVAL_SECONDS)
    return _api("GET", f"sessions/{session_id}", token)


def _get_session_logs_summary(session_id: int, token: str) -> dict:
    """Extract planning/repair info from session logs."""
    try:
        logs = _api("GET", f"sessions/{session_id}/logs?limit=200", token)
        if isinstance(logs, list):
            entries = logs
        else:
            entries = logs.get("items", []) or logs.get("logs", []) or []
    except Exception:
        return {}

    planning_prompts = []
    repair_triggered = False
    repair_reason = ""
    violation_type = ""

    for entry in entries:
        msg = str(entry.get("message") or "")
        meta = entry.get("log_metadata") or {}
        if isinstance(meta, str):
            try:
                meta = json.loads(meta)
            except Exception:
                meta = {}

        if "[PLANNING_PROMPT]" in msg or "planning_prompt_chars" in str(meta):
            chars = meta.get("planning_prompt_chars") or meta.get("prompt_chars")
            if chars:
                planning_prompts.append(int(chars))

        if "[REPAIR]" in msg or "repair" in msg.lower():
            repair_triggered = True
            repair_reason = repair_reason or msg[:200]

        if "VIOLATION" in msg or "validation_error" in str(meta):
            violation_type = violation_type or msg[:100]

    return {
        "planning_prompt_chars": planning_prompts,
        "repair_triggered": repair_triggered,
        "repair_reason": repair_reason[:200] if repair_reason else "",
        "violation_type": violation_type[:100] if violation_type else "",
    }


def _get_checkpoint_data(session_id: int, token: str) -> dict:
    """Get planning metrics from checkpoints."""
    try:
        cps = _api("GET", f"sessions/{session_id}/checkpoints?limit=20", token)
        if isinstance(cps, list):
            items = cps
        else:
            items = cps.get("items", []) or []
    except Exception:
        return {}

    planning_chars = None
    planning_duration = None
    repair_triggered = False
    repair_reason = ""
    violation = ""
    execution_reached = False

    for cp in items:
        cp_type = str(cp.get("checkpoint_type") or cp.get("phase") or "")
        data = cp.get("checkpoint_data") or cp.get("data") or {}
        if isinstance(data, str):
            try:
                data = json.loads(data)
            except Exception:
                data = {}

        if "planning" in cp_type.lower():
            planning_chars = data.get("prompt_chars") or data.get("planning_prompt_chars") or planning_chars
            planning_duration = data.get("planning_duration_seconds") or data.get("duration_seconds") or planning_duration

        if "repair" in cp_type.lower():
            repair_triggered = True
            repair_reason = str(data.get("reason") or data.get("repair_reason") or "")[:200]

        if "violation" in cp_type.lower() or "validation" in cp_type.lower():
            violation = str(data.get("violation_type") or data.get("error") or "")[:100]

        if "execution" in cp_type.lower() or "step" in cp_type.lower():
            execution_reached = True

    return {
        "planning_chars": planning_chars,
        "planning_duration": planning_duration,
        "repair_triggered": repair_triggered,
        "repair_reason": repair_reason,
        "violation": violation,
        "execution_reached": execution_reached,
    }


def run_task(task: dict, token: str, run_index: int) -> dict:
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    workspace = WORKSPACE_ROOT / f"arm-b-pilot-{task['id'].lower()}-{ts}"
    workspace.mkdir(parents=True, exist_ok=True)

    print(f"  [{run_index:02d}/15] {task['id']} ({task['band']}) — {task['title'][:50]}", flush=True)

    project = _api("POST", "projects", token, {
        "name": f"arm-b-pilot-{task['id']}-{ts}",
        "description": f"Arm B pilot task {task['id']}",
        "workspace_path": str(workspace),
    })
    project_id = project["id"]

    t = _api("POST", "tasks", token, {
        "project_id": project_id,
        "title": task["title"],
        "description": task["description"],
        "priority": 0,
        "plan_position": 1,
    })
    task_id = t["id"]

    session = _api("POST", "sessions", token, {
        "project_id": project_id,
        "name": f"arm-b-pilot-{task['id']}-{ts}",
        "execution_mode": "manual",
        "default_execution_profile": "full_lifecycle",
    })
    session_id = session["id"]

    t_start = time.monotonic()
    _api("POST", f"sessions/{session_id}/tasks/{task_id}/run", token)
    final = _wait_terminal(session_id, token)
    t_end = time.monotonic()

    status = str(final.get("status", "unknown")).lower()
    failure_cat = str(final.get("failure_category") or "").strip()

    cp_data = _get_checkpoint_data(session_id, token)
    log_data = _get_session_logs_summary(session_id, token)

    repair = cp_data.get("repair_triggered") or log_data.get("repair_triggered", False)
    repair_reason = cp_data.get("repair_reason") or log_data.get("repair_reason", "")
    violation = cp_data.get("violation") or log_data.get("violation_type", "")
    execution_reached = cp_data.get("execution_reached", False) or (status == "completed")
    planning_chars = cp_data.get("planning_chars")
    if not planning_chars and log_data.get("planning_prompt_chars"):
        planning_chars = log_data["planning_prompt_chars"][0]
    planning_duration = cp_data.get("planning_duration")

    clean_success = (status == "completed" and not failure_cat)
    total_time = t_end - t_start

    # Heuristic: check failure_category for planning-related stops
    planning_success = status != "failed" or "execution" in failure_cat.lower()
    if failure_cat in ("planning_timeout", "planning_failed", "plan_validation_failed"):
        planning_success = False
    if status == "completed":
        planning_success = True

    result = {
        "task_id": task["id"],
        "session_id": session_id,
        "band": task["band"],
        "title": task["title"],
        "description_chars": len(task["description"]),
        "planning_prompt_chars": planning_chars,
        "planning_duration_s": round(planning_duration, 1) if planning_duration else None,
        "total_duration_s": round(total_time, 1),
        "session_status": status,
        "failure_category": failure_cat or None,
        "planning_success": planning_success,
        "repair_triggered": repair,
        "repair_reason": repair_reason[:150] if repair_reason else None,
        "violation_type": violation[:100] if violation else None,
        "execution_reached": execution_reached,
        "clean_success": clean_success,
        "workspace": str(workspace),
    }

    icon = "✓" if clean_success else ("~" if planning_success else "✗")
    print(f"       {icon} status={status} repair={repair} exec_reached={execution_reached} time={total_time:.0f}s", flush=True)
    return result


def compute_metrics(results: list[dict]) -> dict:
    n = len(results)
    if n == 0:
        return {}

    repairs = sum(1 for r in results if r.get("repair_triggered"))
    planning_ok = sum(1 for r in results if r.get("planning_success"))
    planning_fail = n - planning_ok
    exec_reached = sum(1 for r in results if r.get("execution_reached"))
    clean = sum(1 for r in results if r.get("clean_success"))
    timeout = sum(1 for r in results if r.get("failure_category") in (
        "planning_timeout", "backend_timeout"))

    chars = [r["planning_prompt_chars"] for r in results if r.get("planning_prompt_chars")]
    durations = [r["planning_duration_s"] for r in results if r.get("planning_duration_s")]

    return {
        "tasks_run": n,
        "repair_rate": round(repairs / n, 3),
        "repairs": repairs,
        "planning_violation_rate": round(planning_fail / n, 3),
        "planning_failures": planning_fail,
        "planning_timeout_rate": round(timeout / n, 3),
        "planning_timeouts": timeout,
        "execution_reached_rate": round(exec_reached / n, 3),
        "execution_reached": exec_reached,
        "clean_success_rate": round(clean / n, 3),
        "clean_success": clean,
        "avg_planning_prompt_chars": round(sum(chars) / len(chars), 0) if chars else None,
        "avg_planning_duration_s": round(sum(durations) / len(durations), 1) if durations else None,
        "prompt_chars_sample_count": len(chars),
    }


def _check_stop_conditions(metrics: dict, results: list[dict]) -> tuple[bool, str]:
    """Return (should_stop, reason)."""
    BASELINE_REPAIR_RATE = 0.35  # conservative baseline from characterization data
    repair_rate = metrics.get("repair_rate", 0)
    if repair_rate > BASELINE_REPAIR_RATE + 0.05:
        return True, f"repair_rate={repair_rate:.1%} > baseline+5% ({BASELINE_REPAIR_RATE+0.05:.1%})"

    # Check for repeated structural failures
    from collections import Counter
    failure_cats = Counter(r.get("failure_category") for r in results if r.get("failure_category"))
    for cat, count in failure_cats.items():
        if cat in ("plan_validation_failed", "planning_failed") and count >= 3:
            return True, f"structural planning failure repeated: {cat} x{count}"

    return False, ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", required=True)
    parser.add_argument("--tasks", type=int, default=15)
    parser.add_argument("--output-dir", type=Path,
                        default=Path("docs/roadmap/reports/maintenance"))
    args = parser.parse_args()

    print("=== Priority 8 Arm B — 15-Task Pilot ===")
    print(f"Started: {datetime.now(UTC).isoformat()}")
    print(f"Backend: local_openclaw | Model: qwen3-coder:30b")
    print(f"Flag: REDUCED_PLANNING_PROMPT_ENABLED=True")
    print()

    results = []
    stop_hit = False
    stop_reason = ""

    for i, task in enumerate(TASKS[: args.tasks], start=1):
        try:
            result = run_task(task, args.token, i)
            results.append(result)
        except Exception as exc:
            print(f"  ERROR on task {task['id']}: {exc}", flush=True)
            results.append({
                "task_id": task["id"],
                "band": task["band"],
                "title": task["title"],
                "description_chars": len(task["description"]),
                "error": str(exc)[:200],
                "planning_success": False,
                "repair_triggered": False,
                "execution_reached": False,
                "clean_success": False,
            })

        # Check stop condition after 8+ tasks
        if len(results) >= 8:
            metrics_so_far = compute_metrics(results)
            stop_hit, stop_reason = _check_stop_conditions(metrics_so_far, results)
            if stop_hit:
                print(f"\n  STOP CONDITION HIT: {stop_reason}")
                break

    metrics = compute_metrics(results)
    stop_hit_final, stop_reason_final = _check_stop_conditions(metrics, results)

    # Pilot pass condition
    pilot_pass = (
        not stop_hit_final
        and metrics.get("planning_violation_rate", 1) < 0.2
        and (metrics.get("avg_planning_prompt_chars") or 0) > 0
    )

    print()
    print("=== PILOT METRICS ===")
    for k, v in metrics.items():
        print(f"  {k}: {v}")
    print(f"  stop_condition_hit: {stop_hit or stop_hit_final}")
    print(f"  stop_reason: {stop_reason or stop_reason_final or 'none'}")
    print(f"  pilot_pass: {pilot_pass}")

    report = {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "experiment": "priority_8_arm_b_15task_pilot",
        "flag": "REDUCED_PLANNING_PROMPT_ENABLED=True",
        "model": "qwen3-coder:30b",
        "backend": "local_openclaw",
        "arm_b_prompt_size_reduction_measured": "2206c (43.4%) vs Arm A minimal baseline",
        "metrics": metrics,
        "stop_condition_hit": stop_hit or stop_hit_final,
        "stop_reason": stop_reason or stop_reason_final or "",
        "pilot_pass": pilot_pass,
        "results": results,
    }

    ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    out_path = args.output_dir / f"prompt-reduction-arm-b-15-task-pilot-{ts}.json"
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nReport: {out_path}")

    return report, out_path


if __name__ == "__main__":
    main()
