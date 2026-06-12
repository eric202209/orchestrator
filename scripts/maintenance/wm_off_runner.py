#!/usr/bin/env python3
"""
WM OFF arm measurement runner.
Runs 3 Python package projects × 6 tasks each, all WM flags OFF.
Collects debug_repair_attempted + planning repair events per task.
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

from app.auth import create_access_token  # noqa: E402
from app.database import SessionLocal  # noqa: E402
from app.models import Task, Project, TaskStatus  # noqa: E402
from app.config import settings  # noqa: E402

BASE_URL = "http://127.0.0.1:8080"
USER_EMAIL = "REDACTED"
POLL_INTERVAL = 20   # seconds between status polls
TASK_TIMEOUT = 600   # seconds max per task

# ── Verify flag state ──────────────────────────────────────────────────────────
assert not settings.WORKING_MEMORY_PERSISTENCE_ENABLED, "WORKING_MEMORY_PERSISTENCE_ENABLED must be False"
assert not settings.WORKING_MEMORY_RENDER_ENABLED, "WORKING_MEMORY_RENDER_ENABLED must be False"
assert not settings.WORKING_MEMORY_INJECTION_ENABLED, "WORKING_MEMORY_INJECTION_ENABLED must be False"
assert not settings.REDUCED_PLANNING_PROMPT_ENABLED, "REDUCED_PLANNING_PROMPT_ENABLED must be False"
assert not settings.LANGFUSE_ENABLED, "LANGFUSE_ENABLED must be False"
print("✓ All flags confirmed OFF")

# ── Auth ───────────────────────────────────────────────────────────────────────
TOKEN = create_access_token({"sub": USER_EMAIL})
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def api(method, path, **kwargs):
    r = requests.request(method, f"{BASE_URL}{path}", headers=HEADERS, **kwargs)
    r.raise_for_status()
    return r.json()


# ── Project / task corpus ──────────────────────────────────────────────────────
PROJECTS = [
    {
        "name": "wm-calclib",
        "workspace": "wm-calclib-off",
        "description": "calclib Python package — WM OFF arm measurement",
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
        "name": "wm-pathtools",
        "workspace": "wm-pathtools-off",
        "description": "pathtools Python package — WM OFF arm measurement",
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
        "name": "wm-strtools",
        "workspace": "wm-strtools-off",
        "description": "strtools Python package — WM OFF arm measurement",
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


# ── Data collection helpers ────────────────────────────────────────────────────

def get_task_events(project_workspace: str, task_id: int) -> list:
    """Read all events for a task from the agent events directory."""
    agent_dir = pathlib.Path(
        f"/root/.openclaw/workspace/vault/projects/{project_workspace}/.agent/events"
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
    """Count debug_repair_attempted events and return their failure classes."""
    repairs = [
        e for e in events if e.get("event_type") == "debug_repair_attempted"
    ]
    classes = [
        e.get("details", {}).get("debug_failure_class", "unknown") for e in repairs
    ]
    return len(repairs), classes


def count_planning_repairs(events: list) -> tuple[int, list]:
    """Count plan:repair_required validation events and return their reasons."""
    repairs = []
    for e in events:
        if e.get("event_type") == "validation_result":
            d = e.get("details", {})
            if d.get("stage") == "plan" and d.get("status") == "repair_required":
                repairs.append(d.get("reasons", []))
    return len(repairs), repairs


def is_pythonpath_repair(failure_classes: list, planning_reasons: list) -> bool:
    """Detect if any repair involves PYTHONPATH / venv / ImportError."""
    keywords = ["pythonpath", "importerror", "modulenotfound", "venv", "import", "python"]
    for fc in failure_classes:
        if any(k in str(fc).lower() for k in keywords):
            return True
    for reasons in planning_reasons:
        for r in reasons:
            if any(k in str(r).lower() for k in keywords):
                return True
    return False


def working_memory_exists(workspace: str) -> bool:
    path = pathlib.Path(
        f"/root/.openclaw/workspace/vault/projects/{workspace}/.agent/working_memory.json"
    )
    return path.exists()


def wait_for_task(task_id: int, timeout: int = TASK_TIMEOUT) -> str:
    """Poll task status until terminal. Returns final status string."""
    db = SessionLocal()
    try:
        start = time.time()
        while True:
            db.expire_all()
            task = db.query(Task).filter(Task.id == task_id).first()
            if not task:
                return "not_found"
            status = task.status.value if task.status else "unknown"
            if status in ("done", "failed", "paused", "cancelled"):
                return status
            elapsed = time.time() - start
            if elapsed > timeout:
                return f"timeout_after_{int(elapsed)}s"
            print(f"    [{status}] task {task_id} ... {int(elapsed)}s elapsed", end="\r")
            time.sleep(POLL_INTERVAL)
    finally:
        db.close()


def run_task(task_id: int) -> dict:
    """Dispatch task via retry endpoint and wait."""
    try:
        resp = api("POST", f"/api/v1/tasks/{task_id}/retry", json={})
    except Exception as e:
        return {"error": str(e), "dispatched": False}
    return {"dispatched": True, "celery_id": resp.get("celery_task_id")}


# ── Main measurement loop ──────────────────────────────────────────────────────

results = []
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")

for proj_spec in PROJECTS:
    print(f"\n{'='*60}")
    print(f"PROJECT: {proj_spec['name']}")
    print(f"{'='*60}")

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

    # Create all tasks upfront with ordered plan_positions
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

    # Execute tasks in order
    for i, (task_id, task_spec) in enumerate(zip(task_ids, proj_spec["tasks"]), start=1):
        if task_id is None:
            results.append({
                "project": proj_spec["name"],
                "plan_position": i,
                "task_id": None,
                "title": task_spec["title"],
                "status": "skipped_creation_failed",
            })
            continue

        print(f"\n  ── Task {i}/6: {task_spec['title']} (id={task_id}) ──")
        dispatch_result = run_task(task_id)
        if not dispatch_result.get("dispatched"):
            print(f"  ERROR dispatching: {dispatch_result.get('error')}")
            results.append({
                "project": proj_spec["name"],
                "plan_position": i,
                "task_id": task_id,
                "title": task_spec["title"],
                "status": "dispatch_failed",
                "dispatch_error": dispatch_result.get("error"),
            })
            continue

        print(f"  Dispatched. Polling every {POLL_INTERVAL}s (max {TASK_TIMEOUT}s)...")
        final_status = wait_for_task(task_id, TASK_TIMEOUT)
        print()
        print(f"  Final status: {final_status}")

        # Collect events
        events = get_task_events(proj_spec["workspace"], task_id)
        debug_count, debug_classes = count_debug_repairs(events)
        plan_count, plan_reasons = count_planning_repairs(events)
        pythonpath_repair = is_pythonpath_repair(debug_classes, plan_reasons)
        wm_exists = working_memory_exists(proj_spec["workspace"])

        # Determine execution reached
        exec_events = [e for e in events if e.get("event_type") in (
            "phase_started", "step_started", "step_finished"
        )]
        execution_reached = any(
            e.get("details", {}).get("phase") == "execution" or
            e.get("event_type") in ("step_started", "step_finished")
            for e in events
        )

        row = {
            "project": proj_spec["name"],
            "plan_position": i,
            "task_id": task_id,
            "title": task_spec["title"],
            "status": final_status,
            "execution_reached": execution_reached,
            "debug_repair_count": debug_count,
            "debug_repair_classes": debug_classes,
            "planning_repair_count": plan_count,
            "planning_repair_reasons": [str(r) for r in plan_reasons],
            "pythonpath_constraint_repair": pythonpath_repair,
            "working_memory_exists": wm_exists,
            "event_count": len(events),
        }
        results.append(row)

        print(f"  debug_repairs={debug_count} plan_repairs={plan_count} "
              f"pythonpath_repair={pythonpath_repair} wm_file={wm_exists}")

        # Stop condition: Task 1 failed
        if i == 1 and final_status not in ("done",):
            print(f"  STOP: Task 1 failed with status={final_status}. "
                  f"Skipping remaining tasks in this project.")
            for j in range(i + 1, 7):
                if task_ids[j - 1]:
                    results.append({
                        "project": proj_spec["name"],
                        "plan_position": j,
                        "task_id": task_ids[j - 1],
                        "title": proj_spec["tasks"][j - 1]["title"],
                        "status": "skipped_task1_failed",
                    })
            break

    # Check stop condition: DONE rate after first project
    if proj_spec == PROJECTS[0]:
        task2plus = [r for r in results
                     if r.get("project") == proj_spec["name"]
                     and r.get("plan_position", 0) > 1
                     and r.get("status") in ("done", "failed")]
        done_count = sum(1 for r in task2plus if r.get("status") == "done")
        if task2plus and (done_count / len(task2plus)) < 0.4:
            print(f"\nSTOP: First project Task 2+ DONE rate {done_count}/{len(task2plus)} < 40%")
            print("Corpus may be wrong. Stopping run.")
            break

# ── Save raw results ───────────────────────────────────────────────────────────
out_path = pathlib.Path(
    REPO_ROOT / "docs/roadmap/reports/maintenance" / f"wm-off-raw-{timestamp}.json"
)
out_path.write_text(json.dumps(results, indent=2))
print(f"\n\nRaw results saved: {out_path}")

# ── Summary stats ──────────────────────────────────────────────────────────────
print("\n" + "="*60)
print("WM OFF ARM SUMMARY")
print("="*60)

task2plus_eligible = [
    r for r in results
    if r.get("plan_position", 0) > 1
    and r.get("status") in ("done", "failed")
    and r.get("execution_reached", False)
]

qualifying_repairs = [
    r for r in task2plus_eligible
    if r.get("debug_repair_count", 0) > 0
]

constraint_rediscoveries = [
    r for r in task2plus_eligible
    if r.get("pythonpath_constraint_repair", False)
]

done_tasks = [r for r in results if r.get("status") == "done"]
all_terminal = [r for r in results if r.get("status") in ("done", "failed", "paused")]

debug_repair_rate = (
    len(qualifying_repairs) / len(task2plus_eligible)
    if task2plus_eligible else 0.0
)

print(f"Total tasks run:            {len(results)}")
print(f"Task 2+ eligible:           {len(task2plus_eligible)}")
print(f"Tasks with debug repairs:   {len(qualifying_repairs)}")
print(f"Constraint rediscoveries:   {len(constraint_rediscoveries)}")
print(f"debug_repair_rate_wm_off:   {debug_repair_rate:.1%}")
print(f"Task completion rate:       {len(done_tasks)}/{len(all_terminal)} "
      f"({len(done_tasks)/len(all_terminal):.1%}" if all_terminal else "N/A)")

corpus_gate = debug_repair_rate >= 0.10 and len(task2plus_eligible) >= 10
print(f"\nCorpus validity gate (≥10 eligible, ≥10% repair rate): "
      f"{'PASS' if corpus_gate else 'FAIL'}")
print(f"WM ON arm approved: {'YES' if corpus_gate else 'NO'}")

print(f"\nRaw results: {out_path}")
