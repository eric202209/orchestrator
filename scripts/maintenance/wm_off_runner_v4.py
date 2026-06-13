#!/usr/bin/env python3
"""
WM OFF arm measurement runner — v4.

Key changes from v3:
  - Pre-seeded workspaces: runner creates package structure directly via
    Python file I/O before T1 dispatch.  No venv, no pip install required.
  - T1 = baseline verification only (PYTHONPATH=. python3 -m pytest tests/ -q).
  - T2–T6 use system python3/pytest; no venv prefix.
  - Corpus expanded to 4 projects: wm4-calclib, wm4-pathtools,
    wm4-strtools, wm4-listops.
  - Additional flag assertions: REPO_MEMORY_INJECTION_ENABLED,
    PSS_CONTINUATION_INJECTION_ENABLED, ARTIFACT_CONTINUATION_ENABLED.
  - All v3 monitoring, slot, stall, block-detection logic is unchanged.
"""
import json
import os
import subprocess
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
USER_EMAIL = os.environ["ORCHESTRATOR_USER_EMAIL"]
POLL_INTERVAL = 20
STALL_TIMEOUT = 120
PROJECT_TIMEOUT = 2400
SLOT_POLL_INTERVAL = 15
SLOT_KEY = "orchestrator:backend_slots:local_openclaw"
WORKSPACE_BASE = pathlib.Path("/root/.openclaw/workspace/vault/projects")

TERMINAL_TASK = {"done", "failed", "paused", "cancelled"}
TERMINAL_SESSION = {"completed", "failed", "cancelled", "paused", "error"}

TOKEN: str = ""
HEADERS: dict = {}
REDIS = None  # type: ignore[assignment]


def _init_runtime() -> None:
    """Verify all flags OFF, create auth token and Redis client."""
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


def api(method, path, **kwargs):
    r = requests.request(method, f"{BASE_URL}{path}", headers=HEADERS, **kwargs)
    r.raise_for_status()
    return r.json()


# ── Pre-seeding ────────────────────────────────────────────────────────────────

def seed_workspace(workspace_slug: str, lib_name: str) -> None:
    """Write package structure to workspace using direct Python file I/O."""
    ws = WORKSPACE_BASE / workspace_slug
    ws.mkdir(parents=True, exist_ok=True)
    (ws / lib_name).mkdir(exist_ok=True)
    (ws / "tests").mkdir(exist_ok=True)

    (ws / lib_name / "__init__.py").write_text(f'__version__ = "0.1.0"\n')
    (ws / "tests" / "__init__.py").write_text("")
    (ws / "tests" / "test_sanity.py").write_text(
        f"def test_package_importable():\n"
        f"    import {lib_name}\n"
        f'    assert {lib_name}.__version__ == "0.1.0"\n'
    )
    (ws / "setup.py").write_text(
        "from setuptools import setup\n"
        f'setup(name="{lib_name}", version="0.1.0", packages=["{lib_name}"])\n'
    )


def verify_preseed(workspace_slug: str) -> tuple[bool, str]:
    """Run PYTHONPATH=. python3 -m pytest tests/ -q; install pytest if missing and retry."""
    ws = WORKSPACE_BASE / workspace_slug
    for attempt in range(2):
        result = subprocess.run(
            ["python3", "-m", "pytest", "tests/", "-q"],
            cwd=str(ws),
            env={**os.environ, "PYTHONPATH": str(ws)},
            capture_output=True,
            timeout=30,
        )
        if result.returncode == 0:
            out = result.stdout.decode().strip()
            summary = out.splitlines()[-1] if out else "ok"
            return True, summary
        if attempt == 0:
            install = subprocess.run(
                ["python3", "-m", "pip", "install", "--user", "pytest"],
                capture_output=True,
                timeout=60,
            )
            if install.returncode != 0:
                return False, f"pip install pytest failed: {install.stderr.decode()[:200]}"
    stdout = result.stdout.decode().strip()[:200]
    stderr = result.stderr.decode().strip()[:200]
    return False, f"stdout={stdout} stderr={stderr}"


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
    """Block until backend slot is empty.  No hard timeout."""
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


# ── Event analysis ────────────────────────────────────────────────────────────

def get_task_events(workspace: str, task_id: int) -> list:
    agent_dir = pathlib.Path(
        f"/root/.openclaw/workspace/vault/projects/{workspace}/.agent/events"
    )
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


def is_pythonpath_repair(debug_classes: list, plan_reasons: list) -> bool:
    keywords = ["pythonpath", "importerror", "modulenotfound", "venv", "import"]
    for fc in debug_classes:
        if any(k in str(fc).lower() for k in keywords):
            return True
    for reasons in plan_reasons:
        for r in reasons:
            if any(k in str(r).lower() for k in keywords):
                return True
    return False


def is_env_capacity_failure(events: list, status: str) -> bool:
    claimed_count = sum(1 for e in events if e.get("event_type") == "task_claimed")
    exec_reached = any(
        e.get("event_type") in ("step_started", "step_finished") for e in events
    )
    if status == "failed" and not exec_reached and claimed_count >= 4:
        return True
    if "backend_capacity" in status or "capacity_limit" in status:
        return True
    return False


def working_memory_exists(workspace: str) -> bool:
    return pathlib.Path(
        f"/root/.openclaw/workspace/vault/projects/{workspace}/.agent/working_memory.json"
    ).exists()


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
    pythonpath_repair = is_pythonpath_repair(debug_classes, plan_reasons)
    wm_exists = working_memory_exists(workspace)
    exec_reached = any(
        e.get("event_type") in ("step_started", "step_finished") for e in events
    )
    env_fail = is_env_capacity_failure(events, final_status)

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
        "pythonpath_constraint_repair": pythonpath_repair,
        "working_memory_exists": wm_exists,
        "env_capacity_failure": env_fail,
        "event_count": len(events),
        **extra,
    }


# ── Project monitoring ────────────────────────────────────────────────────────

def monitor_project(proj_spec: dict, task_ids: list[int]) -> list[dict]:
    """
    Monitor all tasks in a project until all terminal or PROJECT_TIMEOUT.
    T1 dispatched externally; T2–T6 auto-advance.
    """
    workspace = proj_spec["workspace"]
    proj_name = proj_spec["name"]

    state = {tid: {
        "prior_done_since":             None,
        "prior_blocked_since":          None,
        "stall_retry_attempted":        False,
        "already_running_monitor_only": False,
        "auto_advance_stalled":         False,
        "blocked_prior_task_failed":    False,
        "runner_timeout":               False,
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
            "stall_retry_attempted":        s["stall_retry_attempted"],
            "already_running_monitor_only": s["already_running_monitor_only"],
            "auto_advance_stalled":         s["auto_advance_stalled"],
            "blocked_prior_task_failed":    s["blocked_prior_task_failed"],
            "runner_timeout":               s["runner_timeout"],
        }
        row = collect_task_data(proj_name, workspace, pos, tid, title, final_status, extra)
        results.append(row)

        status_line = (
            f"  T{pos} id={tid} [{final_status}] "
            f"debug={row['debug_repair_count']}{row['debug_repair_classes']} "
            f"plan={row['planning_repair_count']} "
            f"pythonpath={row['pythonpath_constraint_repair']} "
            f"env_cap={row['env_capacity_failure']} "
            f"wm={row['working_memory_exists']}"
        )
        if s["stall_retry_attempted"]:
            status_line += " [stall_retry]"
        if s["already_running_monitor_only"]:
            status_line += " [already_running]"
        if s["blocked_prior_task_failed"]:
            status_line += " [blocked]"
        print(status_line)

    return results


# ── Corpus ────────────────────────────────────────────────────────────────────

_T1_DESC = (
    "Baseline verification. The package workspace has already been set up. "
    "Run this command from the project root: "
    "PYTHONPATH=. python3 -m pytest tests/ -q. "
    "Confirm exit code 0 and at least 1 test passed. "
    "Do not modify any files. "
    "Report: what command was run, what the output was, and whether it passed."
)

PROJECTS = [
    {
        "name": "wm4-calclib",
        "workspace": "wm4-calclib-off",
        "lib": "calclib",
        "description": "calclib Python package — WM OFF arm measurement v4",
        "tasks": [
            {
                "title": "Baseline verification",
                "description": _T1_DESC,
            },
            {
                "title": "Arithmetic module",
                "description": (
                    "Create calclib/arithmetic.py with four functions: "
                    "add(a, b), subtract(a, b), multiply(a, b), divide(a, b). "
                    "divide must raise ZeroDivisionError when b is 0. "
                    "Create tests/test_arithmetic.py importing from calclib.arithmetic "
                    "and testing each function including the ZeroDivisionError case. "
                    "Verify: PYTHONPATH=. python3 -m pytest tests/test_arithmetic.py -q."
                ),
            },
            {
                "title": "Stats module",
                "description": (
                    "Create calclib/stats.py with mean(values) and median(values). "
                    "stats.py must import divide from calclib.arithmetic. "
                    "Both functions should raise ValueError for empty input. "
                    "Create tests/test_stats.py that imports from both calclib.arithmetic "
                    "and calclib.stats and tests both functions. "
                    "Verify: PYTHONPATH=. python3 -m pytest tests/ -q."
                ),
            },
            {
                "title": "Edge case tests",
                "description": (
                    "Create tests/test_edge_cases.py covering: "
                    "division by zero (from calclib.arithmetic), "
                    "mean([]) and median([]) (from calclib.stats), "
                    "single-element stats, negative numbers. "
                    "Tests must import from both calclib.arithmetic and calclib.stats. "
                    "Verify full suite: PYTHONPATH=. python3 -m pytest tests/ -q."
                ),
            },
            {
                "title": "Public API exports",
                "description": (
                    "Edit calclib/__init__.py to re-export: "
                    "from calclib.arithmetic import add, subtract, multiply, divide "
                    "and from calclib.stats import mean, median. "
                    "Create tests/test_public_api.py that does "
                    "'from calclib import add, mean' and calls both functions. "
                    "Verify: PYTHONPATH=. python3 -m pytest tests/ -q."
                ),
            },
            {
                "title": "Final verification",
                "description": (
                    "Run PYTHONPATH=. python3 -m pytest tests/ -q --tb=short. "
                    "All tests must pass. Report pass count and any failures. "
                    "Do not modify any files unless a test failure is a genuine bug "
                    "introduced in this task."
                ),
            },
        ],
    },
    {
        "name": "wm4-pathtools",
        "workspace": "wm4-pathtools-off",
        "lib": "pathtools",
        "description": "pathtools Python package — WM OFF arm measurement v4",
        "tasks": [
            {
                "title": "Baseline verification",
                "description": _T1_DESC,
            },
            {
                "title": "Filters module",
                "description": (
                    "Create pathtools/filters.py with two functions: "
                    "filter_by_extension(paths, ext) returning paths whose filename "
                    "ends with ext, and filter_by_prefix(paths, prefix) returning "
                    "paths whose filename starts with prefix. "
                    "Create tests/test_filters.py importing from pathtools.filters "
                    "and testing both functions with a list of sample path strings. "
                    "Verify: PYTHONPATH=. python3 -m pytest tests/test_filters.py -q."
                ),
            },
            {
                "title": "Walker module",
                "description": (
                    "Create pathtools/walker.py with list_files(root_dir, ext=None) "
                    "that uses os.walk to list files under root_dir. "
                    "When ext is provided, import filter_by_extension from "
                    "pathtools.filters and apply it to the result. "
                    "Create tests/test_walker.py using pytest's tmp_path fixture "
                    "to create a temporary directory tree. "
                    "Verify full suite: PYTHONPATH=. python3 -m pytest tests/ -q."
                ),
            },
            {
                "title": "Matchers module",
                "description": (
                    "Create pathtools/matchers.py with two functions: "
                    "glob_match(path, pattern) using fnmatch.fnmatch, "
                    "regex_match(path, pattern) using re.search. "
                    "Create tests/test_matchers.py importing from pathtools.matchers "
                    "and pathtools.filters. "
                    "Verify full suite: PYTHONPATH=. python3 -m pytest tests/ -q."
                ),
            },
            {
                "title": "Public API exports",
                "description": (
                    "Update pathtools/__init__.py to re-export: "
                    "from pathtools.filters import filter_by_extension, filter_by_prefix, "
                    "from pathtools.walker import list_files, "
                    "from pathtools.matchers import glob_match, regex_match. "
                    "Add tests/test_public_api.py that does "
                    "'from pathtools import filter_by_extension, list_files' and calls them. "
                    "Verify full suite: PYTHONPATH=. python3 -m pytest tests/ -q."
                ),
            },
            {
                "title": "Final verification",
                "description": (
                    "Run PYTHONPATH=. python3 -m pytest tests/ -q --tb=short. "
                    "All tests must pass. Report pass count and any failures. "
                    "Do not modify any files unless a test failure is a genuine bug "
                    "introduced in this task."
                ),
            },
        ],
    },
    {
        "name": "wm4-strtools",
        "workspace": "wm4-strtools-off",
        "lib": "strtools",
        "description": "strtools Python package — WM OFF arm measurement v4",
        "tasks": [
            {
                "title": "Baseline verification",
                "description": _T1_DESC,
            },
            {
                "title": "Transform module",
                "description": (
                    "Create strtools/transform.py with three functions: "
                    "to_snake_case(s) converting CamelCase or space-separated words to snake_case, "
                    "to_camel_case(s) converting snake_case to CamelCase, "
                    "strip_whitespace(s) stripping leading/trailing whitespace from each line. "
                    "Create tests/test_transform.py importing from strtools.transform "
                    "with at least two test cases per function. "
                    "Verify: PYTHONPATH=. python3 -m pytest tests/test_transform.py -q."
                ),
            },
            {
                "title": "Validate module",
                "description": (
                    "Create strtools/validate.py with three functions: "
                    "is_email(s) returning True if s matches a basic email pattern, "
                    "is_slug(s) returning True if s matches [a-z0-9-]+ only, "
                    "is_alpha_numeric(s) returning True if s contains only letters and digits. "
                    "validate.py must call strip_whitespace from strtools.transform "
                    "before checking each value. "
                    "Create tests/test_validate.py importing from strtools.validate "
                    "and strtools.transform. "
                    "Verify full suite: PYTHONPATH=. python3 -m pytest tests/ -q."
                ),
            },
            {
                "title": "Format module",
                "description": (
                    "Create strtools/format.py with two functions: "
                    "truncate(s, max_len, suffix='...') truncating s to max_len chars, "
                    "pad(s, width, char=' ') padding s on the right to width chars. "
                    "Create tests/test_format.py importing from strtools.format, "
                    "strtools.validate, and strtools.transform. "
                    "Verify full suite: PYTHONPATH=. python3 -m pytest tests/ -q."
                ),
            },
            {
                "title": "Edge case tests",
                "description": (
                    "Create tests/test_edge_cases.py covering: "
                    "empty string inputs to all transform functions, "
                    "None inputs to validate functions (should return False or handle gracefully), "
                    "unicode characters in transform functions. "
                    "The test file must import from strtools.transform, strtools.validate, "
                    "and strtools.format. "
                    "Verify full suite: PYTHONPATH=. python3 -m pytest tests/ -q."
                ),
            },
            {
                "title": "Final verification",
                "description": (
                    "Run PYTHONPATH=. python3 -m pytest tests/ -q --tb=short. "
                    "All tests must pass. Report pass count and any failures. "
                    "Do not modify any files unless a test failure is a genuine bug "
                    "introduced in this task."
                ),
            },
        ],
    },
    {
        "name": "wm4-listops",
        "workspace": "wm4-listops-off",
        "lib": "listops",
        "description": "listops Python package — WM OFF arm measurement v4",
        "tasks": [
            {
                "title": "Baseline verification",
                "description": _T1_DESC,
            },
            {
                "title": "Sorting module",
                "description": (
                    "Create listops/sorting.py with two functions: "
                    "bubble_sort(lst) and insertion_sort(lst), both returning "
                    "a new sorted list without modifying the input. "
                    "Create tests/test_sorting.py importing from listops.sorting "
                    "and testing both functions with numeric lists. "
                    "Verify: PYTHONPATH=. python3 -m pytest tests/test_sorting.py -q."
                ),
            },
            {
                "title": "Searching module",
                "description": (
                    "Create listops/searching.py with two functions: "
                    "linear_search(lst, target) returning the index or -1, "
                    "binary_search(lst, target) returning the index or -1. "
                    "binary_search must import insertion_sort from listops.sorting "
                    "and sort the list before searching. "
                    "Create tests/test_searching.py importing from both "
                    "listops.searching and listops.sorting. "
                    "Verify full suite: PYTHONPATH=. python3 -m pytest tests/ -q."
                ),
            },
            {
                "title": "Transforms module",
                "description": (
                    "Create listops/transforms.py with three functions: "
                    "flatten(nested) flattening one level of nesting, "
                    "chunk(lst, size) splitting a list into chunks of given size, "
                    "deduplicate(lst) removing duplicates while preserving order. "
                    "Create tests/test_transforms.py importing from listops.sorting, "
                    "listops.searching, and listops.transforms. "
                    "Verify full suite: PYTHONPATH=. python3 -m pytest tests/ -q."
                ),
            },
            {
                "title": "Public API exports",
                "description": (
                    "Update listops/__init__.py to re-export: "
                    "from listops.sorting import bubble_sort, insertion_sort, "
                    "from listops.searching import linear_search, binary_search, "
                    "from listops.transforms import flatten, chunk, deduplicate. "
                    "Add tests/test_public_api.py that does "
                    "'from listops import bubble_sort, flatten' and calls both. "
                    "Verify full suite: PYTHONPATH=. python3 -m pytest tests/ -q."
                ),
            },
            {
                "title": "Final verification",
                "description": (
                    "Run PYTHONPATH=. python3 -m pytest tests/ -q --tb=short. "
                    "All tests must pass. Report pass count and any failures. "
                    "Do not modify any files unless a test failure is a genuine bug "
                    "introduced in this task."
                ),
            },
        ],
    },
]


# ── Main loop ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _init_runtime()

    all_results = []
    preseed_results = {}
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_meta = {
        "runner_version": "v4",
        "already_running_monitor_only_count": 0,
        "auto_advance_stalls": 0,
        "runner_errors": 0,
        "preseed_failures": 0,
    }

    for proj_spec in PROJECTS:
        print(f"\n{'='*60}")
        print(f"PROJECT: {proj_spec['name']}")
        print(f"{'='*60}")

        workspace_slug = proj_spec["workspace"]
        lib_name = proj_spec["lib"]

        # ── Wait for slot ─────────────────────────────────────────────────────
        print(f"  [slot] Checking before {proj_spec['name']}...")
        wait_for_slot_clear()
        print(f"  [slot] Slot clear.")

        # ── Create project + tasks ────────────────────────────────────────────
        try:
            proj = api("POST", "/api/v1/projects", json={
                "name": proj_spec["name"],
                "description": proj_spec["description"],
                "workspace_path": workspace_slug,
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

        # ── Pre-seed workspace ────────────────────────────────────────────────
        print(f"  [preseed] Seeding workspace for {lib_name}...")
        try:
            seed_workspace(workspace_slug, lib_name)
        except Exception as e:
            print(f"  [PRESEED_FAIL] {proj_spec['name']}: seed_workspace raised: {e}")
            run_meta["preseed_failures"] += 1
            preseed_results[proj_spec["name"]] = {"ok": False, "message": str(e)}
            continue

        print(f"  [preseed] Verifying: PYTHONPATH=. python3 -m pytest tests/ -q ...")
        preseed_ok, preseed_msg = verify_preseed(workspace_slug)
        preseed_results[proj_spec["name"]] = {"ok": preseed_ok, "message": preseed_msg}

        if not preseed_ok:
            print(f"  [PRESEED_FAIL] {proj_spec['name']}: {preseed_msg}")
            run_meta["preseed_failures"] += 1
            continue

        print(f"  [PRESEED_OK] {proj_spec['name']}: {preseed_msg}")

        # ── Dispatch T1 only ──────────────────────────────────────────────────
        print(f"\n  Dispatching T1 (id={task_ids[0]})...")
        ok, err = dispatch_task(task_ids[0])
        if not ok:
            print(f"  ERROR dispatching T1: {err}")
            run_meta["runner_errors"] += 1
            continue
        print(f"  T1 dispatched. Monitoring all tasks (project timeout={PROJECT_TIMEOUT}s)...")

        # ── Monitor project ───────────────────────────────────────────────────
        proj_results = monitor_project(proj_spec, task_ids)
        all_results.extend(proj_results)

        for r in proj_results:
            if r.get("already_running_monitor_only"):
                run_meta["already_running_monitor_only_count"] += 1
            if r.get("auto_advance_stalled"):
                run_meta["auto_advance_stalls"] += 1

    # ── Save raw results ──────────────────────────────────────────────────────
    out_path = REPO_ROOT / "docs/roadmap/reports/maintenance" / f"wm-off-v4-raw-{run_ts}.json"
    out_path.write_text(json.dumps({
        "meta": run_meta,
        "preseed": preseed_results,
        "results": all_results,
    }, indent=2))
    print(f"\n\nRaw results saved: {out_path}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("WM OFF ARM SUMMARY (v4)")
    print("=" * 60)

    task2plus_eligible = [
        r for r in all_results
        if r["plan_position"] > 1
        and r["status"] in ("done", "failed")
        and r["execution_reached"]
        and not r["env_capacity_failure"]
    ]

    qualifying_repairs = [r for r in task2plus_eligible if r["debug_repair_count"] > 0]
    constraint_rediscoveries = [r for r in task2plus_eligible if r["pythonpath_constraint_repair"]]
    backend_cap_failures = [r for r in all_results if r.get("env_capacity_failure")]
    blocked_tasks = [r for r in all_results if r.get("blocked_prior_task_failed")]

    done_tasks = [r for r in all_results if r["status"] == "done"]
    terminal_tasks = [
        r for r in all_results
        if r["status"] in ("done", "failed", "paused", "cancelled")
    ]

    debug_repair_rate = (
        len(qualifying_repairs) / len(task2plus_eligible) if task2plus_eligible else 0.0
    )
    completion_str = (
        f"{len(done_tasks)}/{len(terminal_tasks)} "
        f"({len(done_tasks)/len(terminal_tasks):.1%})"
        if terminal_tasks else "N/A"
    )

    corpus_gate = len(task2plus_eligible) >= 10 and debug_repair_rate >= 0.10

    print(f"\nPre-seed results:")
    for pname, pr in preseed_results.items():
        status = "OK" if pr["ok"] else "FAIL"
        print(f"  [{status}] {pname}: {pr['message']}")

    print(f"\nTotal tasks recorded:              {len(all_results)}")
    print(f"Task 2+ eligible:                  {len(task2plus_eligible)}")
    print(f"Tasks with debug repairs (elig.):  {len(qualifying_repairs)}")
    print(f"Constraint rediscoveries:          {len(constraint_rediscoveries)}")
    print(f"debug_repair_rate_wm_off:          {debug_repair_rate:.1%}")
    print(f"Task completion rate:              {completion_str}")
    print(f"Backend capacity failures:         {len(backend_cap_failures)}")
    print(f"Blocked (prior task failed):       {len(blocked_tasks)}")
    print(f"Auto-advance stalls:               {run_meta['auto_advance_stalls']}")
    print(f"Already-running (monitor only):    {run_meta['already_running_monitor_only_count']}")
    print(f"Runner errors:                     {run_meta['runner_errors']}")
    print(f"Pre-seed failures:                 {run_meta['preseed_failures']}")

    print(f"\nCorpus validity gate (>=10 elig., >=10% repair): {'PASS' if corpus_gate else 'FAIL'}")
    print(f"WM ON arm approved:                {'YES' if corpus_gate else 'NO'}")
    print(f"\nRaw results: {out_path}")
