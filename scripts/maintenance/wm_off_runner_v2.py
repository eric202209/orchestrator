#!/usr/bin/env python3
"""
WM OFF arm measurement runner — v2 (fixed).

Fixes vs v1:
  - Task timeout 600s → 900s
  - Pre-project backend slot check: wait for clear, evict stale (terminal) sessions
  - No hard stop on Task-1 failure; attempt all tasks, record dispatch blockers
  - Terminal status read from DB, not inferred from runner timeout
  - Fresh workspaces (wm2-* prefix) for clean measurement

Run: WM OFF only, 3 projects × 6 tasks, serial.
"""
import json
import os
import sys
import time
import pathlib
import requests
from datetime import datetime

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
POLL_INTERVAL = 20       # seconds between status polls
TASK_TIMEOUT = 900       # seconds max per task (up from 600)
SLOT_WAIT_TIMEOUT = 600  # seconds max to wait for slot to clear before each project
SLOT_KEY = "orchestrator:backend_slots:local_openclaw"
TERMINAL_TASK = {"done", "failed", "paused", "cancelled"}
TERMINAL_SESSION = {"completed", "failed", "cancelled", "paused", "error"}

# ── Flag verification ──────────────────────────────────────────────────────────
assert not settings.WORKING_MEMORY_PERSISTENCE_ENABLED, "WORKING_MEMORY_PERSISTENCE_ENABLED must be False"
assert not settings.WORKING_MEMORY_RENDER_ENABLED, "WORKING_MEMORY_RENDER_ENABLED must be False"
assert not settings.WORKING_MEMORY_INJECTION_ENABLED, "WORKING_MEMORY_INJECTION_ENABLED must be False"
assert not settings.REDUCED_PLANNING_PROMPT_ENABLED, "REDUCED_PLANNING_PROMPT_ENABLED must be False"
assert not settings.LANGFUSE_ENABLED, "LANGFUSE_ENABLED must be False"
print("✓ All flags confirmed OFF")

# ── Auth ───────────────────────────────────────────────────────────────────────
TOKEN = create_access_token({"sub": USER_EMAIL})
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}

# ── Redis client ───────────────────────────────────────────────────────────────
from urllib.parse import urlparse  # noqa: E402
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


# ── Project / task corpus (unchanged from v1) ──────────────────────────────────
PROJECTS = [
    {
        "name": "wm2-calclib",
        "workspace": "wm2-calclib-off",
        "description": "calclib Python package — WM OFF arm measurement v2",
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
                    "Verify pytest can discover tests: run venv/bin/python3 -m pytest --collect-only "
                    "and confirm it exits without error."
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
                    "Run the test suite and verify all tests pass."
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
                    "Run the full test suite (all tests including arithmetic) "
                    "and verify all pass."
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
                    "Run the full test suite and verify all tests pass."
                ),
            },
            {
                "title": "Add public API exports",
                "description": (
                    "Update calclib/__init__.py to re-export all public functions: "
                    "from calclib.arithmetic import add, subtract, multiply, divide "
                    "and from calclib.stats import mean, median. "
                    "Add a test in tests/test_edge_cases.py (or a new file) that imports "
                    "directly from calclib: 'from calclib import add, mean' and calls them. "
                    "Run the full test suite and verify all tests pass."
                ),
            },
            {
                "title": "Final verification build",
                "description": (
                    "Run the complete calclib test suite with verbose output and confirm "
                    "0 failures, 0 errors. "
                    "Then install the package in editable mode using venv/bin/pip install -e . "
                    "and verify the import works: venv/bin/python3 -c "
                    "\"import calclib; print(calclib.__version__)\". "
                    "Report the final test count and pass rate."
                ),
            },
        ],
    },
    {
        "name": "wm2-pathtools",
        "workspace": "wm2-pathtools-off",
        "description": "pathtools Python package — WM OFF arm measurement v2",
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
                    "Run the test suite and verify all tests pass."
                ),
            },
            {
                "title": "Implement walker module",
                "description": (
                    "Create pathtools/walker.py with a function "
                    "list_files(root_dir) that uses os.walk to return all files under root_dir. "
                    "Import from pathtools.filters to apply filter_by_extension as an optional "
                    "parameter. "
                    "Create tests/test_walker.py using a temporary directory (use Python's "
                    "tempfile or pytest's tmp_path fixture). "
                    "Run the full test suite and verify all tests pass."
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
                    "Run the full test suite and verify all tests pass."
                ),
            },
            {
                "title": "Add public API exports",
                "description": (
                    "Update pathtools/__init__.py to re-export public functions: "
                    "from pathtools.filters import filter_by_extension, filter_by_prefix "
                    "and from pathtools.walker import list_files "
                    "and from pathtools.matchers import glob_match, regex_match. "
                    "Add a test that imports 'from pathtools import filter_by_extension' "
                    "and calls it. Run the full test suite and verify all tests pass."
                ),
            },
            {
                "title": "Integration test and final verification",
                "description": (
                    "Create tests/test_integration.py that imports from all three modules "
                    "(filters, walker, matchers) and runs a pipeline: list files in a temp dir, "
                    "filter by extension, then match against a pattern. "
                    "Run the complete test suite and verify 0 failures. "
                    "Report the final test count."
                ),
            },
        ],
    },
    {
        "name": "wm2-strtools",
        "workspace": "wm2-strtools-off",
        "description": "strtools Python package — WM OFF arm measurement v2",
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
                    "Run the test suite and verify all tests pass."
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
                    "Run the full test suite and verify all tests pass."
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
                    "Run the full test suite and verify all tests pass."
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
                    "Run the full test suite and verify all tests pass."
                ),
            },
            {
                "title": "Final verification and exports",
                "description": (
                    "Update strtools/__init__.py to re-export key functions: "
                    "to_snake_case, to_camel_case (from transform), "
                    "is_email, is_slug (from validate), "
                    "truncate, pad (from format). "
                    "Verify: import directly from package works: "
                    "venv/bin/python3 -c \"from strtools import to_snake_case; "
                    "print(to_snake_case('FooBar'))\". "
                    "Run the complete test suite and verify 0 failures."
                ),
            },
        ],
    },
]


# ── Helpers ────────────────────────────────────────────────────────────────────

def api(method, path, **kwargs):
    r = requests.request(method, f"{BASE_URL}{path}", headers=HEADERS, **kwargs)
    r.raise_for_status()
    return r.json()


def db_task_status(task_id: int) -> str:
    db = SessionLocal()
    try:
        db.expire_all()
        t = db.query(Task).filter(Task.id == task_id).first()
        return t.status.value if t else "not_found"
    finally:
        db.close()


def db_session_status(session_id: int) -> str:
    db = SessionLocal()
    try:
        db.expire_all()
        s = db.query(OrchestratorSession).filter(OrchestratorSession.id == session_id).first()
        return s.status if s else "not_found"
    finally:
        db.close()


def slot_members() -> list[int]:
    try:
        members = REDIS.smembers(SLOT_KEY)
        return [int(m) for m in members]
    except Exception:
        return []


def evict_terminal_sessions() -> list[int]:
    """Remove slot entries whose owning sessions are terminal. Returns evicted IDs."""
    evicted = []
    for sid in slot_members():
        status = db_session_status(sid)
        if status in TERMINAL_SESSION or status == "not_found":
            REDIS.srem(SLOT_KEY, str(sid))
            evicted.append(sid)
            print(f"  [slot] Evicted stale session {sid} (status={status})")
    return evicted


def wait_for_slot_clear(max_wait: int = SLOT_WAIT_TIMEOUT) -> bool:
    """
    Wait until the backend slot is empty.
    First evict any terminal sessions, then poll until empty or timeout.
    Returns True if slot is clear, False if timed out.
    """
    evict_terminal_sessions()
    start = time.time()
    while True:
        members = slot_members()
        if not members:
            return True
        elapsed = int(time.time() - start)
        if elapsed >= max_wait:
            print(f"  [slot] WARNING: slot still occupied by {members} after {max_wait}s — proceeding anyway")
            return False
        print(f"  [slot] Slot occupied by sessions {members}, waiting... ({elapsed}s elapsed)", end="\r")
        time.sleep(15)
        evict_terminal_sessions()


def wait_for_task(task_id: int, timeout: int = TASK_TIMEOUT) -> str:
    start = time.time()
    while True:
        status = db_task_status(task_id)
        if status in TERMINAL_TASK:
            return status
        elapsed = time.time() - start
        if elapsed > timeout:
            # Record DB status at timeout (don't assume failure)
            db_status = db_task_status(task_id)
            return f"runner_timeout_{int(elapsed)}s__db_{db_status}"
        print(f"    [{status}] task {task_id} ... {int(elapsed)}s elapsed", end="\r")
        time.sleep(POLL_INTERVAL)


def dispatch_task(task_id: int) -> tuple[bool, str]:
    """Returns (success, error_reason)."""
    try:
        api("POST", f"/api/v1/tasks/{task_id}/retry", json={})
        return True, ""
    except requests.HTTPError as e:
        body = ""
        try:
            body = e.response.json().get("detail", str(e))
        except Exception:
            body = str(e)
        return False, body
    except Exception as e:
        return False, str(e)


def get_task_events(workspace: str, task_id: int) -> list:
    agent_dir = pathlib.Path(
        f"/root/.openclaw/workspace/vault/projects/{workspace}/.agent/events"
    )
    if not agent_dir.exists():
        return []
    events = []
    for jsonl_file in agent_dir.glob(f"*task_{task_id}.jsonl"):
        try:
            with open(jsonl_file) as f:
                for line in f:
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


def is_env_capacity_failure(status: str, debug_classes: list) -> bool:
    """True if failure appears to be env/capacity rather than real execution failure."""
    if "backend_capacity" in status or "capacity_limit" in status:
        return True
    if "runner_timeout" in status and not debug_classes:
        return True
    return False


def working_memory_exists(workspace: str) -> bool:
    return pathlib.Path(
        f"/root/.openclaw/workspace/vault/projects/{workspace}/.agent/working_memory.json"
    ).exists()


def collect_task_data(proj_name: str, workspace: str, pos: int,
                      task_id: int, title: str, final_status: str,
                      dispatch_ok: bool, dispatch_error: str) -> dict:
    events = get_task_events(workspace, task_id)
    debug_count, debug_classes = count_debug_repairs(events)
    plan_count, plan_reasons = count_planning_repairs(events)
    pythonpath_repair = is_pythonpath_repair(debug_classes, plan_reasons)
    wm_exists = working_memory_exists(workspace)
    execution_reached = any(
        e.get("event_type") in ("step_started", "step_finished")
        for e in events
    )
    env_failure = is_env_capacity_failure(final_status, debug_classes)
    return {
        "project": proj_name,
        "plan_position": pos,
        "task_id": task_id,
        "title": title,
        "status": final_status,
        "dispatch_ok": dispatch_ok,
        "dispatch_error": dispatch_error,
        "execution_reached": execution_reached,
        "debug_repair_count": debug_count,
        "debug_repair_classes": debug_classes,
        "planning_repair_count": plan_count,
        "planning_repair_reasons": [str(r) for r in plan_reasons],
        "pythonpath_constraint_repair": pythonpath_repair,
        "working_memory_exists": wm_exists,
        "env_capacity_failure": env_failure,
        "event_count": len(events),
    }


# ── Main measurement loop ──────────────────────────────────────────────────────
results = []
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

for proj_spec in PROJECTS:
    print(f"\n{'='*60}")
    print(f"PROJECT: {proj_spec['name']}")
    print(f"{'='*60}")

    # Wait for backend slot to clear before starting this project
    print(f"  [slot] Checking backend slot before {proj_spec['name']}...")
    wait_for_slot_clear()
    print(f"  [slot] Slot clear. Proceeding.")

    # Create project
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
        continue

    # Create all tasks upfront
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
            print(f"  Task {i} created: id={t['id']} title={task_spec['title']!r}")
        except Exception as e:
            print(f"  ERROR creating task {i}: {e}")
            task_ids.append(None)

    # Track which prior task failed (blocks subsequent plan_position dispatch)
    prior_failed = False
    prior_failed_pos = None

    for i, (task_id, task_spec) in enumerate(zip(task_ids, proj_spec["tasks"]), start=1):
        if task_id is None:
            results.append({
                "project": proj_spec["name"],
                "plan_position": i,
                "task_id": None,
                "title": task_spec["title"],
                "status": "skipped_creation_failed",
                "dispatch_ok": False,
                "dispatch_error": "task_creation_failed",
            })
            continue

        print(f"\n  ── Task {i}/6: {task_spec['title']} (id={task_id}) ──")

        # Always attempt dispatch; capture blocking error if it occurs
        ok, err = dispatch_task(task_id)
        if not ok:
            print(f"  Dispatch failed: {err}")
            # Determine if blocked by prior task or an actual error
            reason = "dispatch_blocked_prior_task_not_done" if prior_failed else f"dispatch_error: {err}"
            results.append({
                "project": proj_spec["name"],
                "plan_position": i,
                "task_id": task_id,
                "title": task_spec["title"],
                "status": reason,
                "dispatch_ok": False,
                "dispatch_error": err,
                "execution_reached": False,
                "debug_repair_count": 0,
                "debug_repair_classes": [],
                "planning_repair_count": 0,
                "planning_repair_reasons": [],
                "pythonpath_constraint_repair": False,
                "working_memory_exists": False,
                "env_capacity_failure": False,
                "event_count": 0,
            })
            if not prior_failed:
                prior_failed = True
                prior_failed_pos = i
            continue

        print(f"  Dispatched. Polling every {POLL_INTERVAL}s (max {TASK_TIMEOUT}s)...")
        final_status = wait_for_task(task_id, TASK_TIMEOUT)
        print()
        print(f"  Final status: {final_status}")

        row = collect_task_data(
            proj_spec["name"], proj_spec["workspace"], i,
            task_id, task_spec["title"], final_status,
            True, "",
        )
        results.append(row)

        print(f"  debug_repairs={row['debug_repair_count']} "
              f"plan_repairs={row['planning_repair_count']} "
              f"pythonpath={row['pythonpath_constraint_repair']} "
              f"wm={row['working_memory_exists']} "
              f"env_fail={row['env_capacity_failure']}")

        # Mark prior_failed only if this task actually failed (not env/capacity)
        if final_status not in ("done",) and not row["env_capacity_failure"]:
            if not prior_failed:
                prior_failed = True
                prior_failed_pos = i

    if prior_failed:
        print(f"\n  NOTE: Task {prior_failed_pos} failed (non-env); subsequent tasks may be blocked.")

# ── Save raw results ───────────────────────────────────────────────────────────
out_path = pathlib.Path(
    REPO_ROOT / "docs/roadmap/reports/maintenance" / f"wm-off-v2-raw-{timestamp}.json"
)
out_path.write_text(json.dumps(results, indent=2))
print(f"\n\nRaw results saved: {out_path}")

# ── Summary ────────────────────────────────────────────────────────────────────
print("\n" + "=" * 60)
print("WM OFF ARM SUMMARY (v2)")
print("=" * 60)

task2plus_eligible = [
    r for r in results
    if r.get("plan_position", 0) > 1
    and r.get("status") in ("done", "failed")
    and r.get("execution_reached", False)
    and not r.get("env_capacity_failure", False)
]

qualifying_repairs = [r for r in task2plus_eligible if r.get("debug_repair_count", 0) > 0]
constraint_rediscoveries = [r for r in task2plus_eligible if r.get("pythonpath_constraint_repair")]

done_tasks = [r for r in results if r.get("status") == "done"]
terminal_tasks = [
    r for r in results
    if r.get("status") in ("done", "failed", "paused", "cancelled")
]

debug_repair_rate = (
    len(qualifying_repairs) / len(task2plus_eligible) if task2plus_eligible else 0.0
)
completion_rate = (
    f"{len(done_tasks)}/{len(terminal_tasks)} ({len(done_tasks)/len(terminal_tasks):.1%})"
    if terminal_tasks else "N/A"
)

corpus_gate = len(task2plus_eligible) >= 10 and debug_repair_rate >= 0.10

print(f"Total tasks recorded:       {len(results)}")
print(f"Task 2+ eligible:           {len(task2plus_eligible)}")
print(f"Tasks with debug repairs:   {len(qualifying_repairs)}")
print(f"Constraint rediscoveries:   {len(constraint_rediscoveries)}")
print(f"debug_repair_rate_wm_off:   {debug_repair_rate:.1%}")
print(f"Task completion rate:       {completion_rate}")
print(f"\nCorpus validity gate (≥10 eligible, ≥10% repair): {'PASS' if corpus_gate else 'FAIL'}")
print(f"WM ON arm approved:         {'YES' if corpus_gate else 'NO'}")
print(f"\nRaw results: {out_path}")
