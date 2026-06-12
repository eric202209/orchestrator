#!/usr/bin/env python3
"""
T1 Reliability Confirmation Runner.

Purpose: confirm that the Step 2 venv PATH fix eliminates the `pip show <pkg>`
completion_validation_failed failure class observed in tasks 599, 605, 653.

This is NOT a WM A/B measurement run.
- Dispatches T1 only; monitors T2+ auto-advance if T1 succeeds.
- Bootstrap T1: creates venv, pip-installs package, verifies with pip show.
- Collects pip_show_failure_detected per task from DB log_entries.
- Reports whether the exact failure class from tasks 599/605/653 recurred.

Based on runner v3 structure (same slot/monitor/block-detection logic).
"""
import json
import os
import sqlite3
import sys
import time
import pathlib
import requests
from datetime import datetime
from urllib.parse import urlparse

from scripts.maintenance._runner_common import chdir_repo_root, ensure_repo_on_syspath

ensure_repo_on_syspath()
REPO_ROOT = chdir_repo_root()

import redis as redis_lib  # noqa: E402
from app.auth import create_access_token  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import Task, Session as OrchestratorSession  # noqa: E402
from app.config import settings  # noqa: E402

# ── Constants ──────────────────────────────────────────────────────────────────
BASE_URL = "http://127.0.0.1:8080"
USER_EMAIL = "REDACTED"
POLL_INTERVAL = 20
STALL_TIMEOUT = 120
PROJECT_TIMEOUT = 2400
SLOT_POLL_INTERVAL = 15
SLOT_KEY = "orchestrator:backend_slots:local_openclaw"
WORKSPACE_BASE = pathlib.Path("/root/.openclaw/workspace/vault/projects")
DB_PATH = REPO_ROOT / "orchestrator.db"

TERMINAL_TASK = {"done", "failed", "paused", "cancelled"}
TERMINAL_SESSION = {"completed", "failed", "cancelled", "paused", "error"}

TOKEN: str = ""
HEADERS: dict = {}
REDIS = None  # type: ignore[assignment]


def _init_runtime() -> None:
    global TOKEN, HEADERS, REDIS

    assert not settings.WORKING_MEMORY_PERSISTENCE_ENABLED, \
        "WORKING_MEMORY_PERSISTENCE_ENABLED must be False"
    assert not settings.WORKING_MEMORY_RENDER_ENABLED, \
        "WORKING_MEMORY_RENDER_ENABLED must be False"
    assert not settings.WORKING_MEMORY_INJECTION_ENABLED, \
        "WORKING_MEMORY_INJECTION_ENABLED must be False"
    assert not settings.REDUCED_PLANNING_PROMPT_ENABLED, \
        "REDUCED_PLANNING_PROMPT_ENABLED must be False"
    assert not settings.LANGFUSE_ENABLED, \
        "LANGFUSE_ENABLED must be False"
    assert not settings.REPO_MEMORY_INJECTION_ENABLED, \
        "REPO_MEMORY_INJECTION_ENABLED must be False"
    assert not settings.PSS_CONTINUATION_INJECTION_ENABLED, \
        "PSS_CONTINUATION_INJECTION_ENABLED must be False"
    assert not settings.ARTIFACT_CONTINUATION_ENABLED, \
        "ARTIFACT_CONTINUATION_ENABLED must be False"
    print("✓ All flags confirmed OFF")
    print(f"  PLANNING_BACKEND: {settings.PLANNING_BACKEND!r} (None = local_openclaw)")
    print(f"  PLANNING_REPAIR_MODEL: {settings.PLANNING_REPAIR_MODEL!r}")
    print(f"  EXECUTION_BACKEND: {settings.EXECUTION_BACKEND!r}")

    import app.tasks.worker as worker_module
    retry_max = worker_module.BACKEND_CAPACITY_RETRY_MAX_RETRIES
    print(f"  BACKEND_CAPACITY_RETRY_MAX_RETRIES: {retry_max}")
    assert retry_max == 20, f"Expected 20, got {retry_max} — Step 1 fix not applied"

    TOKEN = create_access_token({"sub": USER_EMAIL})
    HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

    _url = urlparse(settings.CELERY_BROKER_URL)
    REDIS = redis_lib.Redis(
        host=_url.hostname or "localhost",
        port=_url.port or 6379,
        db=int((_url.path or "/0").lstrip("/") or "0"),
        password=_url.password,
        decode_responses=True,
        socket_connect_timeout=3,
        socket_timeout=3,
    )
    print("  Redis: OK")


def api(method, path, **kwargs):
    r = requests.request(method, f"{BASE_URL}{path}", headers=HEADERS, **kwargs)
    r.raise_for_status()
    return r.json()


# ── Redis slot helpers ─────────────────────────────────────────────────────────

def slot_members() -> list[int]:
    try:
        return [int(m) for m in (REDIS.smembers(SLOT_KEY) or set())]
    except Exception:
        return []


def session_db_status(session_id: int) -> str:
    db = SessionLocal()
    try:
        db.expire_all()
        s = db.query(OrchestratorSession).filter(OrchestratorSession.id == session_id).first()
        return s.status if s else "not_found"
    finally:
        db.close()


def evict_terminal_sessions() -> list[int]:
    evicted = []
    for sid in slot_members():
        status = session_db_status(sid)
        if status in TERMINAL_SESSION or status == "not_found":
            REDIS.srem(SLOT_KEY, str(sid))
            evicted.append(sid)
            print(f"  [slot] Evicted stale session {sid} (db_status={status})")
    return evicted


def wait_for_slot_clear() -> None:
    elapsed = 0
    while True:
        evict_terminal_sessions()
        members = slot_members()
        if not members:
            return
        print(f"  [slot] Occupied by {members}; waiting {SLOT_POLL_INTERVAL}s "
              f"(total {elapsed}s)...", end="\r")
        time.sleep(SLOT_POLL_INTERVAL)
        elapsed += SLOT_POLL_INTERVAL


# ── DB polling ────────────────────────────────────────────────────────────────

def db_task_status(task_id: int) -> str:
    db = SessionLocal()
    try:
        db.expire_all()
        t = db.query(Task).filter(Task.id == task_id).first()
        return t.status.value if t else "not_found"
    finally:
        db.close()


def db_all_statuses(task_ids: list[int]) -> dict[int, str]:
    db = SessionLocal()
    try:
        db.expire_all()
        out = {}
        for task_id in task_ids:
            t = db.query(Task).filter(Task.id == task_id).first()
            out[task_id] = t.status.value if t else "not_found"
        return out
    finally:
        db.close()


# ── pip show failure detection ─────────────────────────────────────────────────

def detect_pip_show_failure(task_id: int) -> dict:
    """
    Query log_entries for the exact failure class from tasks 599/605/653:
    completion_validation_failed with failed_command containing 'pip show'.
    Returns {detected: bool, failed_command: str, stdout_excerpt: str}.
    """
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.cursor()
        cur.execute(
            "SELECT log_metadata FROM log_entries "
            "WHERE task_id=? AND message LIKE '%completion_validation_failed%' "
            "ORDER BY id",
            (task_id,),
        )
        rows = cur.fetchall()
        conn.close()
        for (meta_str,) in rows:
            if not meta_str:
                continue
            try:
                meta = json.loads(meta_str)
            except Exception:
                continue
            envelope = meta.get("debug_feedback_envelope", {})
            failed_cmd = str(envelope.get("failed_command", ""))
            if "pip show" in failed_cmd or "pip3 show" in failed_cmd:
                return {
                    "detected": True,
                    "failed_command": failed_cmd,
                    "stdout_excerpt": str(envelope.get("stdout_excerpt", ""))[:200],
                    "stderr_excerpt": str(envelope.get("stderr_excerpt", ""))[:200],
                }
        return {"detected": False, "failed_command": "", "stdout_excerpt": "", "stderr_excerpt": ""}
    except Exception as e:
        return {"detected": False, "failed_command": "", "stdout_excerpt": "", "stderr_excerpt": f"error: {e}"}


def detect_env_capacity_failure(task_id: int) -> bool:
    """Check task_executions for backend_capacity_limit failure_category."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.cursor()
        cur.execute(
            "SELECT failure_category FROM task_executions "
            "WHERE task_id=? ORDER BY id",
            (task_id,),
        )
        rows = cur.fetchall()
        conn.close()
        return any(
            str(r[0] or "").lower() in ("backend_capacity_limit", "env_capacity")
            for r in rows
        )
    except Exception:
        return False


def detect_completion_validation_failures(task_id: int) -> list[dict]:
    """Return all completion_validation_failed events for this task."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.cursor()
        cur.execute(
            "SELECT log_metadata FROM log_entries "
            "WHERE task_id=? AND message LIKE '%completion_validation_failed%' "
            "ORDER BY id",
            (task_id,),
        )
        rows = cur.fetchall()
        conn.close()
        results = []
        for (meta_str,) in rows:
            if not meta_str:
                continue
            try:
                meta = json.loads(meta_str)
                envelope = meta.get("debug_feedback_envelope", {})
                results.append({
                    "failed_command": str(envelope.get("failed_command", "")),
                    "failure_class": str(envelope.get("failure_class", "")),
                    "step_index": envelope.get("step_index"),
                })
            except Exception:
                pass
        return results
    except Exception:
        return []


# ── Event analysis ────────────────────────────────────────────────────────────

def get_task_events(workspace: str, task_id: int) -> list:
    agent_dir = WORKSPACE_BASE / workspace / ".agent" / "events"
    if not agent_dir.exists():
        return []
    events = []
    for jsonl_file in agent_dir.glob(f"*task_{task_id}.jsonl"):
        try:
            with open(jsonl_file) as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        events.append(json.loads(line))
        except Exception:
            pass
    return events


def count_debug_repairs(events: list) -> tuple[int, list]:
    repairs = [e for e in events if e.get("event_type") == "debug_repair_attempted"]
    classes = [e.get("details", {}).get("debug_failure_class", "unknown") for e in repairs]
    return len(repairs), classes


def count_planning_repairs(events: list) -> tuple[int, list]:
    repairs = []
    for e in events:
        if e.get("event_type") == "validation_result":
            d = e.get("details", {})
            if d.get("stage") == "plan" and d.get("status") == "repair_required":
                repairs.append(d.get("reasons", []))
    return len(repairs), repairs


def is_env_capacity_failure_from_events(events: list, status: str) -> bool:
    claimed_count = sum(1 for e in events if e.get("event_type") == "task_claimed")
    exec_reached = any(
        e.get("event_type") in ("step_started", "step_finished") for e in events
    )
    if status == "failed" and not exec_reached and claimed_count >= 4:
        return True
    return False


def collect_task_data(
    proj_name: str,
    workspace: str,
    pos: int,
    task_id: int,
    title: str,
    final_status: str,
    extra: dict,
) -> dict:
    events = get_task_events(workspace, task_id)
    debug_count, debug_classes = count_debug_repairs(events)
    plan_count, plan_reasons = count_planning_repairs(events)
    exec_reached = any(
        e.get("event_type") in ("step_started", "step_finished") for e in events
    )
    env_fail_events = is_env_capacity_failure_from_events(events, final_status)
    env_fail_db = detect_env_capacity_failure(task_id)
    env_cap = env_fail_events or env_fail_db

    pip_show = detect_pip_show_failure(task_id)
    completion_val_failures = detect_completion_validation_failures(task_id)

    return {
        "project": proj_name,
        "plan_position": pos,
        "task_id": task_id,
        "title": title,
        "status": final_status,
        "execution_reached": exec_reached,
        "debug_repair_count": debug_count,
        "debug_repair_classes": debug_classes,
        "planning_repair_count": plan_count,
        "planning_repair_reasons": [str(r) for r in plan_reasons],
        "env_capacity_failure": env_cap,
        "pip_show_failure_detected": pip_show["detected"],
        "pip_show_failed_command": pip_show["failed_command"],
        "pip_show_stdout": pip_show["stdout_excerpt"],
        "completion_validation_failures": completion_val_failures,
        "event_count": len(events),
        **extra,
    }


# ── Dispatch ──────────────────────────────────────────────────────────────────

def is_already_running_error(err_msg: str) -> bool:
    return "already running" in err_msg.lower()


def dispatch_task(task_id: int) -> tuple[bool, str]:
    try:
        api("POST", f"/api/v1/tasks/{task_id}/retry", json={})
        return True, ""
    except requests.HTTPError as e:
        detail = ""
        try:
            detail = e.response.json().get("detail", str(e))
        except Exception:
            detail = str(e)
        return False, detail
    except Exception as e:
        return False, str(e)


# ── Project monitoring ────────────────────────────────────────────────────────

def monitor_project(proj_spec: dict, task_ids: list[int]) -> list[dict]:
    workspace = proj_spec["workspace"]
    proj_name = proj_spec["name"]

    state = {tid: {
        "prior_done_since": None,
        "prior_blocked_since": None,
        "stall_retry_attempted": False,
        "already_running_monitor_only": False,
        "auto_advance_stalled": False,
        "blocked_prior_task_failed": False,
        "runner_timeout": False,
    } for tid in task_ids}

    proj_start = time.time()
    last_print: dict[int, str] = {}

    def project_complete(statuses: dict[int, str]) -> bool:
        for tid in task_ids:
            if statuses[tid] in TERMINAL_TASK:
                continue
            if state[tid]["blocked_prior_task_failed"]:
                continue
            return False
        return True

    def prior_is_blocking(pos: int, statuses: dict[int, str]) -> bool:
        for p in range(1, pos):
            prior_id = task_ids[p - 1]
            if statuses[prior_id] in ("failed", "paused", "cancelled"):
                return True
            if state[prior_id]["blocked_prior_task_failed"]:
                return True
        return False

    while time.time() - proj_start < PROJECT_TIMEOUT:
        now = time.time()
        statuses = db_all_statuses(task_ids)

        for pos, tid in enumerate(task_ids, start=1):
            status = statuses[tid]
            s = state[tid]

            if status in TERMINAL_TASK or s["blocked_prior_task_failed"]:
                if status != last_print.get(tid):
                    print(f"    T{pos} id={tid} [{status}]")
                    last_print[tid] = status
                continue

            if pos == 1:
                if status != last_print.get(tid):
                    elapsed = int(now - proj_start)
                    print(f"    T1 id={tid} [{status}] {elapsed}s")
                    last_print[tid] = status
                continue

            prior_id = task_ids[pos - 2]
            prior_status = statuses[prior_id]

            if status == "pending":
                if prior_is_blocking(pos, statuses):
                    if s["prior_blocked_since"] is None:
                        s["prior_blocked_since"] = now
                    elif now - s["prior_blocked_since"] >= STALL_TIMEOUT:
                        s["blocked_prior_task_failed"] = True
                        print(f"    T{pos} id={tid} [blocked — prior task failed]")
                        last_print[tid] = "blocked"
                    continue

                if prior_status == "done":
                    if s["prior_done_since"] is None:
                        s["prior_done_since"] = now
                    elif (now - s["prior_done_since"] >= STALL_TIMEOUT
                          and not s["stall_retry_attempted"]):
                        stall_age = int(now - s["prior_done_since"])
                        print(f"    T{pos} id={tid} [stall {stall_age}s] — attempting dispatch")
                        ok, err = dispatch_task(tid)
                        s["stall_retry_attempted"] = True
                        if not ok:
                            if is_already_running_error(err):
                                s["already_running_monitor_only"] = True
                                print(f"    T{pos} id={tid} already running — monitor only")
                            else:
                                s["auto_advance_stalled"] = True
                                print(f"    T{pos} id={tid} stall dispatch failed: {err[:80]}")
                        else:
                            s["auto_advance_stalled"] = True
                            print(f"    T{pos} id={tid} stall dispatch accepted")
            else:
                if status != last_print.get(tid):
                    elapsed = int(now - proj_start)
                    print(f"    T{pos} id={tid} [{status}] {elapsed}s")
                    last_print[tid] = status

        if project_complete(statuses):
            print(f"  Project complete at {int(time.time() - proj_start)}s")
            break

        time.sleep(POLL_INTERVAL)
    else:
        statuses = db_all_statuses(task_ids)
        for tid in task_ids:
            if statuses[tid] not in TERMINAL_TASK and not state[tid]["blocked_prior_task_failed"]:
                state[tid]["runner_timeout"] = True
        print(f"  [WARNING] Project monitoring timed out after {PROJECT_TIMEOUT}s")

    statuses = db_all_statuses(task_ids)
    results = []
    for pos, (tid, title) in enumerate(
        zip(task_ids, [t["title"] for t in proj_spec["tasks"]]), start=1
    ):
        s = state[tid]
        db_status = statuses[tid]

        if s["blocked_prior_task_failed"]:
            final_status = "blocked_prior_task_failed"
        elif s["runner_timeout"] and db_status not in TERMINAL_TASK:
            final_status = f"runner_timeout__{db_status}"
        else:
            final_status = db_status

        extra = {
            "stall_retry_attempted": s["stall_retry_attempted"],
            "already_running_monitor_only": s["already_running_monitor_only"],
            "auto_advance_stalled": s["auto_advance_stalled"],
            "blocked_prior_task_failed": s["blocked_prior_task_failed"],
            "runner_timeout": s["runner_timeout"],
        }
        row = collect_task_data(proj_name, workspace, pos, tid, title, final_status, extra)
        results.append(row)

        status_line = (
            f"  T{pos} id={tid} [{final_status}] "
            f"debug={row['debug_repair_count']}{row['debug_repair_classes']} "
            f"plan={row['planning_repair_count']} "
            f"env_cap={row['env_capacity_failure']} "
            f"pip_show_fail={row['pip_show_failure_detected']}"
        )
        if s["blocked_prior_task_failed"]:
            status_line += " [blocked]"
        if row["completion_validation_failures"]:
            for f in row["completion_validation_failures"]:
                status_line += f" [cvf:{f['failed_command'][:30]}]"
        print(status_line)

    return results


# ── Corpus: Bootstrap T1 (venv + pip install + pip show verification) ──────────
#
# This is the ORIGINAL corpus design from wm3-*.  These are the tasks that
# triggered the pip show failure class (tasks 599/605/653).
# New workspace names to avoid collision with previous runs.
#

_BOOTSTRAP_T1 = {
    "calclib": (
        "Create a Python package called calclib in the current directory. "
        "Structure: calclib/__init__.py (with __version__ = '0.1.0'), "
        "tests/__init__.py, setup.py (name='calclib', version='0.1.0', "
        "packages=['calclib']), requirements.txt containing only 'pytest'. "
        "Create a Python virtual environment at .venv/ using python3 -m venv .venv. "
        "Install the package in editable mode: .venv/bin/pip install -e . "
        "and install requirements: .venv/bin/pip install -r requirements.txt. "
        "Verify the package is installed: pip show calclib and confirm it is found. "
        "Verify pytest can discover tests: run .venv/bin/python3 -m pytest "
        "--collect-only and confirm it exits without error."
    ),
    "pathtools": (
        "Create a Python package called pathtools in the current directory. "
        "Structure: pathtools/__init__.py (with __version__ = '0.1.0'), "
        "tests/__init__.py, setup.py (name='pathtools', version='0.1.0', "
        "packages=['pathtools']), requirements.txt containing only 'pytest'. "
        "Create a Python virtual environment at .venv/ using python3 -m venv .venv. "
        "Install the package in editable mode: .venv/bin/pip install -e . "
        "and install requirements: .venv/bin/pip install -r requirements.txt. "
        "Verify the package is installed: pip show pathtools and confirm it is found. "
        "Verify pytest can discover tests: run .venv/bin/python3 -m pytest "
        "--collect-only and confirm it exits without error."
    ),
    "strtools": (
        "Create a Python package called strtools in the current directory. "
        "Structure: strtools/__init__.py (with __version__ = '0.1.0'), "
        "tests/__init__.py, setup.py (name='strtools', version='0.1.0', "
        "packages=['strtools']), requirements.txt containing only 'pytest'. "
        "Create a Python virtual environment at .venv/ using python3 -m venv .venv. "
        "Install the package in editable mode: .venv/bin/pip install -e . "
        "and install requirements: .venv/bin/pip install -r requirements.txt. "
        "Verify the package is installed: pip show strtools and confirm it is found. "
        "Verify pytest can discover tests: run .venv/bin/python3 -m pytest "
        "--collect-only and confirm it exits without error."
    ),
}

PROJECTS = [
    {
        "name": "t1-confirm-calclib",
        "workspace": "t1-confirm-calclib",
        "lib": "calclib",
        "description": "calclib T1 reliability confirmation — venv pip show fix verification",
        "tasks": [
            {
                "title": "Bootstrap calclib package",
                "description": _BOOTSTRAP_T1["calclib"],
            },
            {
                "title": "Implement arithmetic module",
                "description": (
                    "Create calclib/arithmetic.py with four functions: "
                    "add(a, b), subtract(a, b), multiply(a, b), divide(a, b). "
                    "divide must raise ZeroDivisionError when b is 0. "
                    "Create tests/test_arithmetic.py that imports from calclib.arithmetic "
                    "and tests each function including the ZeroDivisionError case. "
                    "Run the test suite using .venv/bin/python3 -m pytest and verify all tests pass."
                ),
            },
            {
                "title": "Implement stats module",
                "description": (
                    "Create calclib/stats.py with mean(values) and median(values). "
                    "stats.py must import divide from calclib.arithmetic. "
                    "Both functions should raise ValueError for empty input. "
                    "Create tests/test_stats.py that imports from both calclib.arithmetic "
                    "and calclib.stats and tests both functions. "
                    "Run .venv/bin/python3 -m pytest and verify all tests pass."
                ),
            },
            {
                "title": "Edge case tests",
                "description": (
                    "Create tests/test_edge_cases.py covering: "
                    "division by zero (from calclib.arithmetic), "
                    "mean([]) and median([]) (from calclib.stats), "
                    "single-element stats, negative numbers. "
                    "Run .venv/bin/python3 -m pytest and verify all tests pass."
                ),
            },
            {
                "title": "Public API exports",
                "description": (
                    "Edit calclib/__init__.py to re-export: "
                    "from calclib.arithmetic import add, subtract, multiply, divide "
                    "and from calclib.stats import mean, median. "
                    "Create tests/test_public_api.py that imports and calls them. "
                    "Run .venv/bin/python3 -m pytest and verify all tests pass."
                ),
            },
            {
                "title": "Final verification",
                "description": (
                    "Run .venv/bin/python3 -m pytest --tb=short. "
                    "All tests must pass. Report pass count and any failures."
                ),
            },
        ],
    },
    {
        "name": "t1-confirm-pathtools",
        "workspace": "t1-confirm-pathtools",
        "lib": "pathtools",
        "description": "pathtools T1 reliability confirmation — venv pip show fix verification",
        "tasks": [
            {
                "title": "Bootstrap pathtools package",
                "description": _BOOTSTRAP_T1["pathtools"],
            },
            {
                "title": "Implement filters module",
                "description": (
                    "Create pathtools/filters.py with filter_by_extension(paths, ext) "
                    "and filter_by_prefix(paths, prefix). "
                    "Create tests/test_filters.py and verify with "
                    ".venv/bin/python3 -m pytest tests/test_filters.py -q."
                ),
            },
            {
                "title": "Implement walker module",
                "description": (
                    "Create pathtools/walker.py with list_files(root_dir, ext=None). "
                    "Import filter_by_extension from pathtools.filters when ext provided. "
                    "Create tests/test_walker.py. Verify: .venv/bin/python3 -m pytest."
                ),
            },
            {
                "title": "Matchers module",
                "description": (
                    "Create pathtools/matchers.py with glob_match(path, pattern) "
                    "and regex_match(path, pattern). "
                    "Create tests/test_matchers.py. Verify: .venv/bin/python3 -m pytest."
                ),
            },
            {
                "title": "Public API exports",
                "description": (
                    "Update pathtools/__init__.py to re-export all public functions. "
                    "Add tests/test_public_api.py. Verify: .venv/bin/python3 -m pytest."
                ),
            },
            {
                "title": "Final verification",
                "description": (
                    "Run .venv/bin/python3 -m pytest --tb=short. "
                    "All tests must pass."
                ),
            },
        ],
    },
    {
        "name": "t1-confirm-strtools",
        "workspace": "t1-confirm-strtools",
        "lib": "strtools",
        "description": "strtools T1 reliability confirmation — venv pip show fix verification",
        "tasks": [
            {
                "title": "Bootstrap strtools package",
                "description": _BOOTSTRAP_T1["strtools"],
            },
            {
                "title": "Implement transform module",
                "description": (
                    "Create strtools/transform.py with to_snake_case(s), to_camel_case(s), "
                    "strip_whitespace(s). "
                    "Create tests/test_transform.py. Verify: .venv/bin/python3 -m pytest."
                ),
            },
            {
                "title": "Implement validate module",
                "description": (
                    "Create strtools/validate.py with is_email(s), is_slug(s), is_alpha_numeric(s). "
                    "validate.py must call strip_whitespace from strtools.transform. "
                    "Create tests/test_validate.py. Verify: .venv/bin/python3 -m pytest."
                ),
            },
            {
                "title": "Implement format module",
                "description": (
                    "Create strtools/format.py with truncate(s, max_len, suffix='...') "
                    "and pad(s, width, char=' '). "
                    "Create tests/test_format.py. Verify: .venv/bin/python3 -m pytest."
                ),
            },
            {
                "title": "Edge case tests",
                "description": (
                    "Create tests/test_edge_cases.py covering empty string inputs, "
                    "None inputs to validate functions, unicode characters. "
                    "Verify: .venv/bin/python3 -m pytest."
                ),
            },
            {
                "title": "Final verification",
                "description": (
                    "Run .venv/bin/python3 -m pytest --tb=short. "
                    "All tests must pass."
                ),
            },
        ],
    },
]


# ── Main ──────────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _init_runtime()

    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    all_results = []
    run_meta = {
        "runner": "t1_reliability_confirmation",
        "run_ts": run_ts,
        "runner_errors": 0,
        "planning_backend": str(settings.PLANNING_BACKEND),
        "planning_repair_model": str(settings.PLANNING_REPAIR_MODEL),
    }

    for proj_spec in PROJECTS:
        print(f"\n{'='*60}")
        print(f"PROJECT: {proj_spec['name']}")
        print(f"{'='*60}")

        print(f"  [slot] Checking before {proj_spec['name']}...")
        wait_for_slot_clear()
        print(f"  [slot] Slot clear.")

        try:
            proj = api("POST", "/api/v1/projects", json={
                "name": proj_spec["name"],
                "description": proj_spec["description"],
                "workspace_path": proj_spec["workspace"],
            })
            project_id = proj["id"]
            print(f"  Created project {project_id}: {proj['resolved_workspace_path']}")
        except Exception as e:
            print(f"  ERROR creating project: {e}")
            run_meta["runner_errors"] += 1
            continue

        task_ids = []
        for i, task_spec in enumerate(proj_spec["tasks"], start=1):
            try:
                t = api("POST", "/api/v1/tasks", json={
                    "project_id": project_id,
                    "title": task_spec["title"],
                    "description": task_spec["description"],
                    "plan_position": i,
                    "execution_profile": "full_lifecycle",
                })
                task_ids.append(t["id"])
                print(f"  T{i} created: id={t['id']} {task_spec['title']!r}")
            except Exception as e:
                print(f"  ERROR creating task {i}: {e}")
                run_meta["runner_errors"] += 1
                task_ids.append(None)

        if None in task_ids:
            print("  ERROR: task creation failed; skipping project")
            run_meta["runner_errors"] += 1
            continue

        print(f"\n  Dispatching T1 (id={task_ids[0]})...")
        ok, err = dispatch_task(task_ids[0])
        if not ok:
            print(f"  ERROR dispatching T1: {err}")
            run_meta["runner_errors"] += 1
            continue
        print(f"  T1 dispatched. Monitoring (timeout={PROJECT_TIMEOUT}s)...")

        proj_results = monitor_project(proj_spec, task_ids)
        all_results.extend(proj_results)

    # ── Save raw results ──────────────────────────────────────────────────────
    out_dir = pathlib.Path(
        "docs/roadmap/reports/maintenance"
        "/project_aware_continuation_execution"
        "/slices_C_working_memory_persistence"
    )
    out_path = out_dir / f"t1-confirm-raw-{run_ts}.json"
    out_path.write_text(json.dumps({"meta": run_meta, "results": all_results}, indent=2))
    print(f"\n\nRaw results saved: {out_path}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("T1 RELIABILITY CONFIRMATION SUMMARY")
    print("=" * 60)

    t1_results = [r for r in all_results if r["plan_position"] == 1]
    t1_done = [r for r in t1_results if r["status"] == "done"]
    t1_failed = [r for r in t1_results if r["status"] == "failed"]
    pip_show_recurrences = [r for r in t1_results if r["pip_show_failure_detected"]]
    env_cap_failures = [r for r in all_results if r["env_capacity_failure"]]

    t2plus_results = [r for r in all_results if r["plan_position"] > 1]
    t2plus_eligible = [
        r for r in t2plus_results
        if r["status"] in ("done", "failed")
        and r["execution_reached"]
        and not r["env_capacity_failure"]
    ]

    print(f"\nProjects run:              {len(PROJECTS)}")
    print(f"T1 success (done):         {len(t1_done)}/{len(t1_results)}")
    print(f"T1 failed:                 {len(t1_failed)}")
    print(f"pip show recurrence:       {len(pip_show_recurrences)} (should be 0)")
    print(f"Backend capacity failures: {len(env_cap_failures)}")
    print(f"T2+ eligible:              {len(t2plus_eligible)}")
    print(f"Runner errors:             {run_meta['runner_errors']}")

    print("\nT1 detail:")
    for r in t1_results:
        pip_flag = " ← pip show RECURRED" if r["pip_show_failure_detected"] else ""
        cvf = r.get("completion_validation_failures", [])
        cvf_str = ""
        if cvf:
            cmds = [f["failed_command"] for f in cvf]
            cvf_str = f" cvf=[{', '.join(cmds[:3])}]"
        print(
            f"  {r['project']} T1 [{r['status']}] "
            f"plan_repairs={r['planning_repair_count']} "
            f"debug_repairs={r['debug_repair_count']}"
            f"{cvf_str}{pip_flag}"
        )

    step2_confirmed = (
        len(t1_done) >= 2
        and len(pip_show_recurrences) == 0
        and len(env_cap_failures) == 0
    )
    print(f"\nStep 2 fix confirmed:      {'YES' if step2_confirmed else 'NO'}")
    print(f"Raw results:               {out_path}")
