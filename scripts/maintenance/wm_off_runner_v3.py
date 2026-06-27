#!/usr/bin/env python3
"""
WM OFF arm measurement runner — v3.

Design: the Orchestrator runs each project as a single continuous
auto-advancing session.  T1 is dispatched explicitly; T2–T6 start
automatically when their predecessor completes (no external trigger needed).

Key changes from v2:
  - Dispatch T1 only.  Never call retry for T2–T6 proactively.
  - Monitor all tasks together in a single polling loop per project.
  - Stall detection: T(N) DONE and T(N+1) PENDING for >120s → one retry.
  - "Already running" response → already_running_monitor_only; keep watching.
  - Block detection: T(N) FAILED and T(N+1) PENDING for >120s →
    blocked_prior_task_failed; propagates to T(N+2) … T6.
  - Slot wait: no hard fallthrough; evict stale (terminal) sessions until
    genuinely empty.  Do not start next project until current is complete.
  - Per-project monitoring timeout: 2400s.  DB status is always authoritative.
"""
import json
import os
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
POLL_INTERVAL = 20          # seconds between polling cycles
STALL_TIMEOUT = 120         # seconds before stall/block detection fires
PROJECT_TIMEOUT = 2400      # per-project monitoring ceiling
SLOT_POLL_INTERVAL = 15     # seconds between slot-check retries
SLOT_KEY = "orchestrator:backend_slots:local_openclaw"

TERMINAL_TASK = {"done", "failed", "paused", "cancelled"}
TERMINAL_SESSION = {"completed", "failed", "cancelled", "paused", "error"}

# ── Runtime globals (initialised in _init_runtime, not at import time) ─────────
# Tests import this module without calling _init_runtime().
TOKEN: str = ""
HEADERS: dict = {}
REDIS = None  # type: ignore[assignment]


def _init_runtime() -> None:
    """Verify flags, create auth token and Redis client.  Called by __main__ only."""
    global TOKEN, HEADERS, REDIS

    user_email = os.environ.get("ORCHESTRATOR_USER_EMAIL", "")
    if not user_email:
        raise RuntimeError("ORCHESTRATOR_USER_EMAIL must be set")

    assert not settings.WORKING_MEMORY_PERSISTENCE_ENABLED, "WORKING_MEMORY_PERSISTENCE_ENABLED must be False"
    assert not settings.WORKING_MEMORY_RENDER_ENABLED,      "WORKING_MEMORY_RENDER_ENABLED must be False"
    assert not settings.WORKING_MEMORY_INJECTION_ENABLED,   "WORKING_MEMORY_INJECTION_ENABLED must be False"
    assert not settings.REDUCED_PLANNING_PROMPT_ENABLED,    "REDUCED_PLANNING_PROMPT_ENABLED must be False"
    assert not settings.LANGFUSE_ENABLED,                   "LANGFUSE_ENABLED must be False"
    print("✓ All flags confirmed OFF")

    TOKEN = create_access_token({"sub": user_email})
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
    """Block until the backend slot is empty.  No hard timeout."""
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
    """Returns (success, error_detail)."""
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
    """True when failure is backend_capacity_limit, not a real execution failure."""
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
    Monitor all tasks in a project until all are terminal or PROJECT_TIMEOUT.

    Only T1 was dispatched externally.  T2–T6 auto-advance.  Stall and block
    detection fire when a task stays PENDING longer than STALL_TIMEOUT after
    its predecessor transitions to DONE or FAILED.
    """
    n = len(task_ids)
    workspace = proj_spec["workspace"]
    proj_name = proj_spec["name"]

    # Per-task mutable state (keyed by task_id)
    state = {tid: {
        "prior_done_since":    None,
        "prior_blocked_since": None,
        "stall_retry_attempted":       False,
        "already_running_monitor_only": False,
        "auto_advance_stalled":        False,
        "blocked_prior_task_failed":   False,
        "runner_timeout":              False,
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
        """True if any task before pos is failed/blocked (so pos cannot start)."""
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

            # Already terminal or synthetically blocked — nothing to do.
            if status in TERMINAL_TASK or s["blocked_prior_task_failed"]:
                if status != last_print.get(tid):
                    print(f"    T{pos} id={tid} [{status}]")
                    last_print[tid] = status
                continue

            # T1: only print progress
            if pos == 1:
                if status != last_print.get(tid):
                    elapsed = int(now - proj_start)
                    print(f"    T1 id={tid} [{status}] {elapsed}s")
                    last_print[tid] = status
                continue

            # T2–T6: monitor, stall-detect, block-detect
            prior_id = task_ids[pos - 2]
            prior_status = statuses[prior_id]

            if status == "pending":
                # ── Block detection ───────────────────────────────────────
                if prior_is_blocking(pos, statuses):
                    if s["prior_blocked_since"] is None:
                        s["prior_blocked_since"] = now
                    elif now - s["prior_blocked_since"] >= STALL_TIMEOUT:
                        s["blocked_prior_task_failed"] = True
                        print(f"    T{pos} id={tid} [blocked — prior task failed]")
                        last_print[tid] = "blocked"
                    continue

                # ── Stall detection ───────────────────────────────────────
                if prior_status == "done":
                    if s["prior_done_since"] is None:
                        s["prior_done_since"] = now
                    elif now - s["prior_done_since"] >= STALL_TIMEOUT and not s["stall_retry_attempted"]:
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
                # Running or other non-pending, non-terminal state
                if status != last_print.get(tid):
                    elapsed = int(now - proj_start)
                    print(f"    T{pos} id={tid} [{status}] {elapsed}s")
                    last_print[tid] = status

        if project_complete(statuses):
            print(f"  Project complete at {int(time.time() - proj_start)}s")
            break

        time.sleep(POLL_INTERVAL)
    else:
        # Timeout — mark still-pending/running tasks
        statuses = db_all_statuses(task_ids)
        for tid in task_ids:
            if statuses[tid] not in TERMINAL_TASK and not state[tid]["blocked_prior_task_failed"]:
                state[tid]["runner_timeout"] = True
        print(f"  [WARNING] Project monitoring timed out after {PROJECT_TIMEOUT}s")

    # ── Collect final data ────────────────────────────────────────────────────
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
PROJECTS = [
    {
        "name": "wm3-calclib",
        "workspace": "wm3-calclib-off",
        "description": "calclib Python package — WM OFF arm measurement v3",
        "tasks": [
            {
                "title": "Bootstrap calclib package",
                "description": (
                    "Create a Python package called calclib in the current directory. "
                    "Structure: calclib/__init__.py (with __version__ = '0.1.0'), "
                    "tests/__init__.py, setup.py (name='calclib', version='0.1.0', "
                    "packages=['calclib']), requirements.txt containing only 'pytest'. "
                    "Create a Python virtual environment at venv/ using python3 -m venv venv. "
                    "Install requirements: venv/bin/pip install -r requirements.txt. "
                    "Verify pytest can discover tests: run venv/bin/python3 -m pytest "
                    "--collect-only and confirm it exits without error."
                ),
            },
            {
                "title": "Implement arithmetic module",
                "description": (
                    "Create calclib/arithmetic.py with four functions: "
                    "add(a, b), subtract(a, b), multiply(a, b), divide(a, b). "
                    "divide must raise ZeroDivisionError when b is 0. "
                    "Create tests/test_arithmetic.py that imports from calclib.arithmetic "
                    "and tests each function including the ZeroDivisionError case. "
                    "Run the test suite using venv/bin/python3 -m pytest and verify all tests pass."
                ),
            },
            {
                "title": "Implement stats module",
                "description": (
                    "Create calclib/stats.py with two functions: "
                    "mean(values) returning the arithmetic mean of a list, "
                    "median(values) returning the median. "
                    "Both should raise ValueError for empty input. "
                    "Create tests/test_stats.py that imports from calclib.stats "
                    "and tests mean, median, and empty-input error cases. "
                    "Run the full test suite using venv/bin/python3 -m pytest "
                    "and verify all tests pass."
                ),
            },
            {
                "title": "Add edge case tests",
                "description": (
                    "Create tests/test_edge_cases.py with edge case coverage: "
                    "division by zero (from calclib.arithmetic), "
                    "single-element list for mean and median (from calclib.stats), "
                    "negative numbers for arithmetic operations. "
                    "The test file must import from both calclib.arithmetic and calclib.stats. "
                    "Run the full test suite using venv/bin/python3 -m pytest "
                    "and verify all tests pass."
                ),
            },
            {
                "title": "Add public API exports",
                "description": (
                    "Update calclib/__init__.py to re-export all public functions: "
                    "from calclib.arithmetic import add, subtract, multiply, divide "
                    "and from calclib.stats import mean, median. "
                    "Add a test that imports directly from calclib: "
                    "'from calclib import add, mean' and calls them. "
                    "Run the full test suite using venv/bin/python3 -m pytest "
                    "and verify all tests pass."
                ),
            },
            {
                "title": "Final verification build",
                "description": (
                    "Run the complete calclib test suite with verbose output and confirm "
                    "0 failures, 0 errors. "
                    "Install the package in editable mode using venv/bin/pip install -e . "
                    "and verify the import works: "
                    "venv/bin/python3 -c \"import calclib; print(calclib.__version__)\". "
                    "Report the final test count and pass rate."
                ),
            },
        ],
    },
    {
        "name": "wm3-pathtools",
        "workspace": "wm3-pathtools-off",
        "description": "pathtools Python package — WM OFF arm measurement v3",
        "tasks": [
            {
                "title": "Bootstrap pathtools package",
                "description": (
                    "Create a Python package called pathtools in the current directory. "
                    "Structure: pathtools/__init__.py (with __version__ = '0.1.0'), "
                    "tests/__init__.py, setup.py (name='pathtools', version='0.1.0', "
                    "packages=['pathtools']), requirements.txt containing only 'pytest'. "
                    "Create a virtual environment at venv/ using python3 -m venv venv. "
                    "Install requirements: venv/bin/pip install -r requirements.txt. "
                    "Verify pytest discovers the tests directory: "
                    "venv/bin/python3 -m pytest --collect-only."
                ),
            },
            {
                "title": "Implement filters module",
                "description": (
                    "Create pathtools/filters.py with two functions: "
                    "filter_by_extension(paths, ext) returning paths matching the extension, "
                    "filter_by_prefix(paths, prefix) returning paths starting with the prefix. "
                    "Create tests/test_filters.py that imports from pathtools.filters "
                    "and tests both functions with a list of sample paths. "
                    "Run the test suite using venv/bin/python3 -m pytest "
                    "and verify all tests pass."
                ),
            },
            {
                "title": "Implement walker module",
                "description": (
                    "Create pathtools/walker.py with list_files(root_dir) that uses os.walk. "
                    "Import from pathtools.filters to apply filter_by_extension as an optional "
                    "parameter. "
                    "Create tests/test_walker.py using a temporary directory "
                    "(use pytest's tmp_path fixture). "
                    "Run the full test suite using venv/bin/python3 -m pytest "
                    "and verify all tests pass."
                ),
            },
            {
                "title": "Implement matchers module",
                "description": (
                    "Create pathtools/matchers.py with two functions: "
                    "glob_match(path, pattern) using fnmatch, "
                    "regex_match(path, pattern) using re.match. "
                    "Create tests/test_matchers.py that imports from pathtools.matchers "
                    "and from pathtools.filters, testing both matching functions. "
                    "Run the full test suite using venv/bin/python3 -m pytest "
                    "and verify all tests pass."
                ),
            },
            {
                "title": "Add public API exports",
                "description": (
                    "Update pathtools/__init__.py to re-export public functions: "
                    "from pathtools.filters import filter_by_extension, filter_by_prefix, "
                    "from pathtools.walker import list_files, "
                    "from pathtools.matchers import glob_match, regex_match. "
                    "Add a test importing 'from pathtools import filter_by_extension' "
                    "and calling it. "
                    "Run the full test suite using venv/bin/python3 -m pytest "
                    "and verify all tests pass."
                ),
            },
            {
                "title": "Integration test and final verification",
                "description": (
                    "Create tests/test_integration.py that imports from all three modules "
                    "(filters, walker, matchers) and runs a pipeline: list files in a temp dir, "
                    "filter by extension, then match against a pattern. "
                    "Run the complete test suite using venv/bin/python3 -m pytest "
                    "and verify 0 failures. Report the final test count."
                ),
            },
        ],
    },
    {
        "name": "wm3-strtools",
        "workspace": "wm3-strtools-off",
        "description": "strtools Python package — WM OFF arm measurement v3",
        "tasks": [
            {
                "title": "Bootstrap strtools package",
                "description": (
                    "Create a Python package called strtools in the current directory. "
                    "Structure: strtools/__init__.py (with __version__ = '0.1.0'), "
                    "tests/__init__.py, setup.py (name='strtools', version='0.1.0', "
                    "packages=['strtools']), requirements.txt containing only 'pytest'. "
                    "Create a virtual environment at venv/ using python3 -m venv venv. "
                    "Install requirements: venv/bin/pip install -r requirements.txt. "
                    "Verify: venv/bin/python3 -m pytest --collect-only."
                ),
            },
            {
                "title": "Implement transform module",
                "description": (
                    "Create strtools/transform.py with three functions: "
                    "to_snake_case(s) converting CamelCase/spaces to snake_case, "
                    "to_camel_case(s) converting snake_case to CamelCase, "
                    "strip_whitespace(s) stripping leading/trailing whitespace from each line. "
                    "Create tests/test_transform.py importing from strtools.transform "
                    "with at least two test cases per function. "
                    "Run the test suite using venv/bin/python3 -m pytest "
                    "and verify all tests pass."
                ),
            },
            {
                "title": "Implement validate module",
                "description": (
                    "Create strtools/validate.py with three functions: "
                    "is_email(s) returning True if s looks like a valid email, "
                    "is_slug(s) returning True if s matches [a-z0-9-]+ pattern, "
                    "is_alpha_numeric(s) returning True if s contains only letters and digits. "
                    "Create tests/test_validate.py importing from strtools.validate. "
                    "Also import to_snake_case from strtools.transform in the test to verify "
                    "cross-module imports work. "
                    "Run the full test suite using venv/bin/python3 -m pytest "
                    "and verify all tests pass."
                ),
            },
            {
                "title": "Implement format module",
                "description": (
                    "Create strtools/format.py with two functions: "
                    "truncate(s, max_len, suffix='...') truncating s to max_len characters, "
                    "pad(s, width, char=' ') padding s to width characters. "
                    "Create tests/test_format.py importing from strtools.format, "
                    "strtools.validate, and strtools.transform. "
                    "Run the full test suite using venv/bin/python3 -m pytest "
                    "and verify all tests pass."
                ),
            },
            {
                "title": "Add edge case tests",
                "description": (
                    "Create tests/test_edge_cases.py covering: "
                    "empty string inputs to all transform functions, "
                    "None or non-string inputs to validate functions (should return False), "
                    "unicode characters in transform functions. "
                    "The test file must import from strtools.transform, strtools.validate, "
                    "and strtools.format. "
                    "Run the full test suite using venv/bin/python3 -m pytest "
                    "and verify all tests pass."
                ),
            },
            {
                "title": "Final verification and exports",
                "description": (
                    "Update strtools/__init__.py to re-export key functions: "
                    "to_snake_case, to_camel_case (from transform), "
                    "is_email, is_slug (from validate), "
                    "truncate, pad (from format). "
                    "Verify import from package works: "
                    "venv/bin/python3 -c \"from strtools import to_snake_case; "
                    "print(to_snake_case('FooBar'))\". "
                    "Run the complete test suite using venv/bin/python3 -m pytest "
                    "and verify 0 failures."
                ),
            },
        ],
    },
]


# ── Main loop ─────────────────────────────────────────────────────────────────
if __name__ == "__main__":
    _init_runtime()

    all_results = []
    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    run_meta = {
        "already_running_monitor_only_count": 0,
        "auto_advance_stalls": 0,
        "runner_errors": 0,
    }

    for proj_spec in PROJECTS:
        print(f"\n{'='*60}")
        print(f"PROJECT: {proj_spec['name']}")
        print(f"{'='*60}")

        # ── Wait for slot ─────────────────────────────────────────────────────
        print(f"  [slot] Checking before {proj_spec['name']}...")
        wait_for_slot_clear()
        print(f"  [slot] Slot clear.")

        # ── Create project + tasks ────────────────────────────────────────────
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

        # Tally run-level counters
        for r in proj_results:
            if r.get("already_running_monitor_only"):
                run_meta["already_running_monitor_only_count"] += 1
            if r.get("auto_advance_stalled"):
                run_meta["auto_advance_stalls"] += 1

    # ── Save raw results ──────────────────────────────────────────────────────
    out_path = REPO_ROOT / "docs/roadmap/reports/maintenance" / f"wm-off-v3-raw-{run_ts}.json"
    out_path.write_text(json.dumps({"meta": run_meta, "results": all_results}, indent=2))
    print(f"\n\nRaw results saved: {out_path}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("WM OFF ARM SUMMARY (v3)")
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
    terminal_tasks = [r for r in all_results if r["status"] in ("done", "failed", "paused", "cancelled")]

    debug_repair_rate = (
        len(qualifying_repairs) / len(task2plus_eligible) if task2plus_eligible else 0.0
    )
    completion_str = (
        f"{len(done_tasks)}/{len(terminal_tasks)} "
        f"({len(done_tasks)/len(terminal_tasks):.1%})"
        if terminal_tasks else "N/A"
    )

    corpus_gate = len(task2plus_eligible) >= 10 and debug_repair_rate >= 0.10

    print(f"Total tasks recorded:              {len(all_results)}")
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

    print(f"\nCorpus validity gate (≥10 elig., ≥10% repair): {'PASS' if corpus_gate else 'FAIL'}")
    print(f"WM ON arm approved:                {'YES' if corpus_gate else 'NO'}")
    print(f"\nRaw results: {out_path}")
