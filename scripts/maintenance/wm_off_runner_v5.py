#!/usr/bin/env python3
"""
WM OFF arm measurement runner — v5.

Active_constraints signal corpus.
Gate metric: planning_repair_rate (tasks with ≥1 planning validator rejection
             / eligible T2+ tasks).

Key changes from v4:
  - Corpus: 4 projects × 6 tasks, src-layout (src/{lib}/).
  - T1 writes its own files (no pre-seeding); includes pytest.ini creation.
  - pytest.ini: [pytest]\\npythonpath = src — required for completion validator
    compatibility with src-layout.
  - T2 states src-layout explicitly; gives exact PYTHONPATH=src command.
  - T3-T5 use cross-module package imports (from {lib}.module import ...) without
    restating the exact command — forcing the model to rely on context/memory.
  - T6: final verification only, no source file changes.
  - Primary metric: planning_repair_rate (NOT debug_repair_rate).
  - Eligible definition: plan_position > 1, terminal status, execution_reached=True
    OR planning_repair_count > 0, env_capacity_failure=False.
  - Tracks: planning_repair_count, planning_repair_reasons, physical src prefix
    rejection count, active_constraints that WM ON would store.
  - PROJECT_TIMEOUT: 3600s (increased from 2400s).
  - WM OFF gate: T1 success ≥ 3/4, eligible T2+ ≥ 15,
    planning_repair_rate ≥ 10%, ≥2 active_constraints-style rejections.

Deliverable:
  docs/roadmap/reports/maintenance/working-memory-active-constraints-wm-off-20260612.md
"""
import json
import os
import pathlib
import sys
import time
from datetime import datetime
from urllib.parse import urlparse

from scripts.maintenance._runner_common import chdir_repo_root, ensure_repo_on_syspath

ensure_repo_on_syspath()
REPO_ROOT = chdir_repo_root()

import requests                                              # noqa: E402
import redis as redis_lib                                    # noqa: E402
from app.auth import create_access_token                     # noqa: E402
from app.database import SessionLocal                        # noqa: E402
from app.models import Task, Session as OrchestratorSession  # noqa: E402
from app.config import settings                              # noqa: E402

# ── Constants ──────────────────────────────────────────────────────────────────
BASE_URL           = "http://127.0.0.1:8080"
USER_EMAIL         = os.environ["ORCHESTRATOR_USER_EMAIL"]
POLL_INTERVAL      = 20
STALL_TIMEOUT      = 120
PROJECT_TIMEOUT    = 3600
SLOT_POLL_INTERVAL = 15
SLOT_KEY           = "orchestrator:backend_slots:local_openclaw"
WORKSPACE_BASE     = pathlib.Path("/root/.openclaw/workspace/vault/projects")

TERMINAL_TASK    = {"done", "failed", "paused", "cancelled"}
TERMINAL_SESSION = {"completed", "failed", "cancelled", "paused", "error"}

TOKEN:   str  = ""
HEADERS: dict = {}
REDIS         = None  # type: ignore[assignment]


# ── T1 template (shared across all 4 projects) ────────────────────────────────

def _t1_desc(lib: str) -> str:
    return (
        f"Bootstrap {lib}: "
        f"Create src/{lib}/__init__.py with __version__ = \"0.1.0\". "
        f"Create tests/__init__.py (empty). "
        f"Create tests/test_sanity.py with one test: "
        f"import {lib}; assert {lib}.__version__ == \"0.1.0\". "
        f"Create pytest.ini at the project root with content: "
        f"[pytest]\\npythonpath = src\\n "
        f"(this allows pytest to find the {lib} package under src/). "
        f"Verify explicitly: PYTHONPATH=src python3 -m pytest tests/ -q. "
        f"Confirm at least 1 test passed."
    )


# ── Corpus ─────────────────────────────────────────────────────────────────────

PROJECTS = [
    # ── wm5-calclib ─────────────────────────────────────────────────────────
    {
        "name": "wm5-calclib",
        "workspace": "wm5-calclib",
        "lib": "calclib",
        "description": "calclib Python src-layout package — WM OFF active_constraints v5",
        "tasks": [
            {
                "title": "Bootstrap calclib",
                "description": _t1_desc("calclib"),
            },
            {
                "title": "Arithmetic module",
                "description": (
                    "This project uses a src/ layout (package root is src/calclib/). "
                    "Add src/calclib/arithmetic.py with four functions: "
                    "add(a, b), subtract(a, b), multiply(a, b), "
                    "divide(a, b) — raises ZeroDivisionError when b == 0. "
                    "Create tests/test_arithmetic.py importing from calclib.arithmetic "
                    "(not src.calclib.arithmetic). "
                    "Test all four functions including the ZeroDivisionError case. "
                    "Verify: PYTHONPATH=src python3 -m pytest tests/test_arithmetic.py -q."
                ),
            },
            {
                "title": "Stats module",
                "description": (
                    "Add src/calclib/stats.py with mean(values) and median(values). "
                    "stats.py must import divide from calclib.arithmetic "
                    "using the package import path established in this project "
                    "(not src.calclib.arithmetic). "
                    "Both functions should raise ValueError for empty input. "
                    "Create tests/test_stats.py importing from both "
                    "calclib.arithmetic and calclib.stats. "
                    "Run the full test suite using the project's standard test command."
                ),
            },
            {
                "title": "Edge case tests",
                "description": (
                    "Add tests/test_edge_cases.py. "
                    "Tests must exercise all public functions from calclib.arithmetic "
                    "and calclib.stats. "
                    "Cover: division by zero, mean([]), median([]), "
                    "single-element inputs, negative numbers. "
                    "All imports must use the package import style established for "
                    "this project. "
                    "Run the full test suite using the project's established "
                    "verification command."
                ),
            },
            {
                "title": "Public API exports",
                "description": (
                    "Update src/calclib/__init__.py to re-export all public functions "
                    "from calclib.arithmetic and calclib.stats. "
                    "Add tests/test_public_api.py that imports from the top-level "
                    "calclib package (e.g. from calclib import add, mean) "
                    "and runs basic assertions. "
                    "Run the full test suite following the project's established "
                    "conventions."
                ),
            },
            {
                "title": "Final verification",
                "description": (
                    "Run the complete test suite. "
                    "Do not modify any source files. "
                    "All tests must pass. "
                    "Report the command used, how many tests passed, and any failures."
                ),
            },
        ],
    },

    # ── wm5-pathtools ────────────────────────────────────────────────────────
    {
        "name": "wm5-pathtools",
        "workspace": "wm5-pathtools",
        "lib": "pathtools",
        "description": "pathtools Python src-layout package — WM OFF active_constraints v5",
        "tasks": [
            {
                "title": "Bootstrap pathtools",
                "description": _t1_desc("pathtools"),
            },
            {
                "title": "Filters module",
                "description": (
                    "This project uses a src/ layout (package root is src/pathtools/). "
                    "Add src/pathtools/filters.py with two functions: "
                    "filter_by_extension(paths, ext) returning paths whose filename "
                    "ends with ext, and filter_by_prefix(paths, prefix) returning "
                    "paths whose filename starts with prefix. "
                    "Create tests/test_filters.py importing from pathtools.filters "
                    "(not src.pathtools.filters) and testing both functions. "
                    "Verify: PYTHONPATH=src python3 -m pytest tests/test_filters.py -q."
                ),
            },
            {
                "title": "Walker module",
                "description": (
                    "Add src/pathtools/walker.py with list_files(root_dir, ext=None) "
                    "using os.walk to list files under root_dir. "
                    "When ext is provided, import filter_by_extension from "
                    "pathtools.filters using the package import path established in "
                    "this project. "
                    "Create tests/test_walker.py using pytest's tmp_path fixture. "
                    "Run the full test suite using the project's standard test command."
                ),
            },
            {
                "title": "Matchers module",
                "description": (
                    "Add src/pathtools/matchers.py with: "
                    "glob_match(path, pattern) using fnmatch.fnmatch, "
                    "regex_match(path, pattern) using re.search. "
                    "Create tests/test_matchers.py importing from pathtools.matchers "
                    "and pathtools.filters using the package import style for "
                    "this project. "
                    "Run the full test suite using the project's established "
                    "verification command."
                ),
            },
            {
                "title": "Public API exports",
                "description": (
                    "Update src/pathtools/__init__.py to re-export: "
                    "filter_by_extension and filter_by_prefix from pathtools.filters, "
                    "list_files from pathtools.walker, "
                    "glob_match and regex_match from pathtools.matchers. "
                    "Add tests/test_public_api.py importing from the top-level "
                    "pathtools package. "
                    "Run the full test suite following project conventions."
                ),
            },
            {
                "title": "Final verification",
                "description": (
                    "Run the complete test suite. "
                    "Do not modify any source files. "
                    "All tests must pass. "
                    "Report the command used, how many tests passed, and any failures."
                ),
            },
        ],
    },

    # ── wm5-strtools ─────────────────────────────────────────────────────────
    {
        "name": "wm5-strtools",
        "workspace": "wm5-strtools",
        "lib": "strtools",
        "description": "strtools Python src-layout package — WM OFF active_constraints v5",
        "tasks": [
            {
                "title": "Bootstrap strtools",
                "description": _t1_desc("strtools"),
            },
            {
                "title": "Transform module",
                "description": (
                    "This project uses a src/ layout (package root is src/strtools/). "
                    "Add src/strtools/transform.py with three functions: "
                    "to_snake_case(s) converting CamelCase or space-separated words "
                    "to snake_case, "
                    "to_camel_case(s) converting snake_case to CamelCase, "
                    "strip_whitespace(s) stripping leading/trailing whitespace. "
                    "Create tests/test_transform.py importing from strtools.transform "
                    "(not src.strtools.transform) with at least two test cases per function. "
                    "Verify: PYTHONPATH=src python3 -m pytest tests/test_transform.py -q."
                ),
            },
            {
                "title": "Validate module",
                "description": (
                    "Add src/strtools/validate.py with three functions: "
                    "is_email(s) returning True if s matches a basic email pattern, "
                    "is_slug(s) returning True if s matches [a-z0-9-]+ only, "
                    "is_alpha_numeric(s) returning True if s contains only letters and digits. "
                    "validate.py must call strip_whitespace from strtools.transform "
                    "using the package import path established in this project. "
                    "Create tests/test_validate.py importing from strtools.validate "
                    "and strtools.transform. "
                    "Run the full test suite using the project's standard test command."
                ),
            },
            {
                "title": "Format module",
                "description": (
                    "Add src/strtools/format.py with two functions: "
                    "truncate(s, max_len, suffix='...') truncating s to max_len chars, "
                    "pad(s, width, char=' ') padding s on the right to width chars. "
                    "Create tests/test_format.py importing from strtools.format, "
                    "strtools.validate, and strtools.transform using the package import "
                    "style established for this project. "
                    "Run the full test suite using the project's established "
                    "verification command."
                ),
            },
            {
                "title": "Public API exports",
                "description": (
                    "Update src/strtools/__init__.py to re-export all public functions "
                    "from strtools.transform, strtools.validate, and strtools.format. "
                    "Add tests/test_public_api.py importing from the top-level "
                    "strtools package. "
                    "Run the full test suite following project conventions."
                ),
            },
            {
                "title": "Final verification",
                "description": (
                    "Run the complete test suite. "
                    "Do not modify any source files. "
                    "All tests must pass. "
                    "Report the command used, how many tests passed, and any failures."
                ),
            },
        ],
    },

    # ── wm5-listops ──────────────────────────────────────────────────────────
    {
        "name": "wm5-listops",
        "workspace": "wm5-listops",
        "lib": "listops",
        "description": "listops Python src-layout package — WM OFF active_constraints v5",
        "tasks": [
            {
                "title": "Bootstrap listops",
                "description": _t1_desc("listops"),
            },
            {
                "title": "Sorting module",
                "description": (
                    "This project uses a src/ layout (package root is src/listops/). "
                    "Add src/listops/sorting.py with two functions: "
                    "bubble_sort(lst) and insertion_sort(lst), both returning "
                    "a new sorted list without modifying the input. "
                    "Create tests/test_sorting.py importing from listops.sorting "
                    "(not src.listops.sorting) with numeric list tests. "
                    "Verify: PYTHONPATH=src python3 -m pytest tests/test_sorting.py -q."
                ),
            },
            {
                "title": "Searching module",
                "description": (
                    "Add src/listops/searching.py with two functions: "
                    "linear_search(lst, target) returning the index or -1, "
                    "binary_search(lst, target) returning the index or -1. "
                    "binary_search must import insertion_sort from listops.sorting "
                    "using the package import path established in this project. "
                    "Create tests/test_searching.py importing from both "
                    "listops.searching and listops.sorting. "
                    "Run the full test suite using the project's standard test command."
                ),
            },
            {
                "title": "Transforms module",
                "description": (
                    "Add src/listops/transforms.py with three functions: "
                    "flatten(nested) flattening one level of nesting, "
                    "chunk(lst, size) splitting into chunks of given size, "
                    "deduplicate(lst) removing duplicates while preserving order. "
                    "Create tests/test_transforms.py importing from listops.sorting, "
                    "listops.searching, and listops.transforms using the package "
                    "import style established for this project. "
                    "Run the full test suite using the project's established "
                    "verification command."
                ),
            },
            {
                "title": "Public API exports",
                "description": (
                    "Update src/listops/__init__.py to re-export: "
                    "bubble_sort and insertion_sort from listops.sorting, "
                    "linear_search and binary_search from listops.searching, "
                    "flatten, chunk, and deduplicate from listops.transforms. "
                    "Add tests/test_public_api.py importing from the top-level "
                    "listops package. "
                    "Run the full test suite following project conventions."
                ),
            },
            {
                "title": "Final verification",
                "description": (
                    "Run the complete test suite. "
                    "Do not modify any source files. "
                    "All tests must pass. "
                    "Report the command used, how many tests passed, and any failures."
                ),
            },
        ],
    },
]


# ── Runtime init ───────────────────────────────────────────────────────────────

def _init_runtime() -> None:
    global TOKEN, HEADERS, REDIS

    # WM flags
    assert not settings.WORKING_MEMORY_PERSISTENCE_ENABLED, \
        "WORKING_MEMORY_PERSISTENCE_ENABLED must be False"
    assert not settings.WORKING_MEMORY_RENDER_ENABLED, \
        "WORKING_MEMORY_RENDER_ENABLED must be False"
    assert not settings.WORKING_MEMORY_INJECTION_ENABLED, \
        "WORKING_MEMORY_INJECTION_ENABLED must be False"
    # Other flags
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
    print(f"✓ Auth token created, Redis connected")
    print(f"✓ PLANNING_BACKEND: {settings.PLANNING_BACKEND}")


# ── API ────────────────────────────────────────────────────────────────────────

def api(method, path, **kwargs):
    r = requests.request(method, f"{BASE_URL}{path}", headers=HEADERS, **kwargs)
    r.raise_for_status()
    return r.json()


# ── Slot helpers ───────────────────────────────────────────────────────────────

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


def evict_terminal_sessions() -> None:
    for sid in slot_members():
        status = session_db_status(sid)
        if status in TERMINAL_SESSION or status == "not_found":
            REDIS.srem(SLOT_KEY, str(sid))
            print(f"  [slot] Evicted stale session {sid} (status={status})")


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


# ── DB helpers ─────────────────────────────────────────────────────────────────

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


# ── Dispatch ───────────────────────────────────────────────────────────────────

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


# ── Event analysis ─────────────────────────────────────────────────────────────

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
                        try:
                            events.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except Exception:
            pass
    return events


def count_planning_repairs(events: list) -> tuple[int, list[list[str]]]:
    """Count plan validation_result events with status=repair_required."""
    repairs = []
    for e in events:
        if e.get("event_type") == "validation_result":
            d = e.get("details", {})
            if d.get("stage") == "plan" and d.get("status") == "repair_required":
                repairs.append(d.get("reasons", []))
    return len(repairs), repairs


def count_debug_repairs(events: list) -> tuple[int, list[str]]:
    repairs = [e for e in events if e.get("event_type") == "debug_repair_attempted"]
    classes = [e.get("details", {}).get("debug_failure_class", "unknown") for e in repairs]
    return len(repairs), classes


def count_completion_repairs(events: list) -> int:
    return sum(
        1 for e in events
        if e.get("event_type") == "debug_repair_attempted"
        and e.get("details", {}).get("phase") == "completion"
    )


def is_execution_reached(events: list) -> bool:
    return any(
        e.get("event_type") in ("step_started", "step_finished") for e in events
    )


def is_env_capacity_failure(events: list, status: str) -> bool:
    claimed_count = sum(1 for e in events if e.get("event_type") == "task_claimed")
    exec_reached = is_execution_reached(events)
    if status == "failed" and not exec_reached and claimed_count >= 4:
        return True
    all_text = json.dumps(events).lower()
    return "backend_capacity" in all_text or "capacity_limit" in all_text


def first_planning_rejection_reason(plan_reasons: list[list[str]]) -> str:
    """Return the first non-empty rejection reason string."""
    for reasons in plan_reasons:
        for r in reasons:
            if r:
                return str(r)
    return ""


def is_src_prefix_rejection(reason: str) -> bool:
    """True if the rejection is about the physical src. import prefix."""
    lowered = reason.lower()
    return (
        "src." in lowered and "import" in lowered
    ) or (
        "physical" in lowered and "src" in lowered
    ) or (
        "src-layout" in lowered and ("prefix" in lowered or "import" in lowered)
    )


def detect_old_regressions(events: list) -> dict:
    all_text = json.dumps(events).lower()
    return {
        "pip_show": "pip show" in all_text and "pip_show" in all_text,
        "nested_project_folder_command": "nested_project_folder_command" in all_text,
        "vma_deadlock": (
            "verification plan mutates" in all_text
            and "vma_repair_triggered" not in all_text
        ),
        "path_guard_advisory": "path_guard_advisory" in all_text,
        "backend_capacity": "backend_capacity" in all_text or "capacity_limit" in all_text,
        "empty_response": "empty_response" in all_text or "empty response" in all_text,
    }


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
    plan_count, plan_reasons = count_planning_repairs(events)
    debug_count, debug_classes = count_debug_repairs(events)
    compl_count = count_completion_repairs(events)
    exec_reached = is_execution_reached(events)
    env_fail = is_env_capacity_failure(events, final_status)
    first_reason = first_planning_rejection_reason(plan_reasons)
    src_prefix = is_src_prefix_rejection(first_reason)
    regressions = detect_old_regressions(events)

    return {
        "project": proj_name,
        "plan_position": pos,
        "task_id": task_id,
        "title": title,
        "status": final_status,
        "execution_reached": exec_reached,
        "planning_repair_count": plan_count,
        "planning_repair_reasons": [list(r) for r in plan_reasons],
        "first_planning_rejection": first_reason,
        "src_prefix_rejection": src_prefix,
        "debug_repair_count": debug_count,
        "debug_repair_classes": debug_classes,
        "completion_repair_count": compl_count,
        "env_capacity_failure": env_fail,
        "event_count": len(events),
        "old_regressions": regressions,
        **extra,
    }


def is_eligible(row: dict) -> bool:
    """
    Eligible T2+ task:
    - plan_position > 1
    - status in DONE or FAILED
    - execution_reached=True OR planning_repair_count > 0
    - env_capacity_failure=False
    """
    if row["plan_position"] <= 1:
        return False
    status = row["status"]
    if status not in ("done", "failed"):
        return False
    if not (row["execution_reached"] or row["planning_repair_count"] > 0):
        return False
    if row["env_capacity_failure"]:
        return False
    return True


# ── Project monitoring ─────────────────────────────────────────────────────────

def monitor_project(proj_spec: dict, task_ids: list[int]) -> list[dict]:
    workspace  = proj_spec["workspace"]
    proj_name  = proj_spec["name"]
    task_titles = [t["title"] for t in proj_spec["tasks"]]

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
    for pos, (tid, title) in enumerate(zip(task_ids, task_titles), start=1):
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
            f"plan={row['planning_repair_count']}"
        )
        if row["planning_repair_count"] > 0:
            status_line += f" src_prefix={row['src_prefix_rejection']}"
            if row["first_planning_rejection"]:
                status_line += f" reason={row['first_planning_rejection'][:80]!r}"
        status_line += (
            f" debug={row['debug_repair_count']}{row['debug_repair_classes']}"
            f" env_cap={row['env_capacity_failure']}"
        )
        if s["blocked_prior_task_failed"]:
            status_line += " [blocked]"
        if s["runner_timeout"]:
            status_line += " [runner_timeout]"
        print(status_line)

    return results


# ── Metrics ────────────────────────────────────────────────────────────────────

def compute_metrics(all_results: list[dict]) -> dict:
    t1_rows     = [r for r in all_results if r["plan_position"] == 1]
    t1_success  = sum(1 for r in t1_rows if r["status"] == "done")
    t1_total    = len(t1_rows)

    eligible    = [r for r in all_results if is_eligible(r)]
    elig_count  = len(eligible)

    planning_repairs   = [r for r in eligible if r["planning_repair_count"] > 0]
    planning_repair_n  = len(planning_repairs)
    planning_repair_rate = planning_repair_n / elig_count if elig_count > 0 else 0.0

    src_prefix_n = sum(1 for r in eligible if r["src_prefix_rejection"])

    debug_repairs  = sum(r["debug_repair_count"] for r in eligible)
    done_eligible  = sum(1 for r in eligible if r["status"] == "done")

    # active_constraints: tasks where planning repair fired and task eventually DONE
    # (WM ON would store constraint from those tasks for subsequent planners)
    active_constraints_writable = [
        r for r in eligible
        if r["planning_repair_count"] > 0 and r["status"] == "done"
    ]

    # Old regression counts across all tasks
    regressions = {
        "pip_show":                    sum(1 for r in all_results if r["old_regressions"].get("pip_show")),
        "nested_project_folder_command": sum(1 for r in all_results if r["old_regressions"].get("nested_project_folder_command")),
        "vma_deadlock":                sum(1 for r in all_results if r["old_regressions"].get("vma_deadlock")),
        "path_guard_advisory":         sum(1 for r in all_results if r["old_regressions"].get("path_guard_advisory")),
        "backend_capacity":            sum(1 for r in all_results if r["old_regressions"].get("backend_capacity")),
        "empty_response":              sum(1 for r in all_results if r["old_regressions"].get("empty_response")),
    }

    # WM OFF gate
    gate_t1_ok         = t1_success >= 3
    gate_eligible_ok   = elig_count >= 15
    gate_pr_rate_ok    = planning_repair_rate >= 0.10
    gate_ac_count_ok   = src_prefix_n >= 2
    gate_pip_ok        = regressions["pip_show"] == 0
    gate_nested_ok     = regressions["nested_project_folder_command"] == 0
    gate_cap_ok        = regressions["backend_capacity"] == 0

    gate_pass = (
        gate_t1_ok and gate_eligible_ok and gate_pr_rate_ok
        and gate_ac_count_ok and gate_pip_ok and gate_nested_ok and gate_cap_ok
    )

    return {
        "t1_success":           t1_success,
        "t1_total":             t1_total,
        "eligible_count":       elig_count,
        "planning_repair_n":    planning_repair_n,
        "planning_repair_rate": planning_repair_rate,
        "src_prefix_n":         src_prefix_n,
        "debug_repairs":        debug_repairs,
        "done_eligible":        done_eligible,
        "active_constraints_writable": len(active_constraints_writable),
        "regressions":          regressions,
        "gate": {
            "t1_success":    gate_t1_ok,
            "eligible":      gate_eligible_ok,
            "pr_rate":       gate_pr_rate_ok,
            "ac_count":      gate_ac_count_ok,
            "pip_show":      gate_pip_ok,
            "nested":        gate_nested_ok,
            "backend_cap":   gate_cap_ok,
            "PASS":          gate_pass,
        },
    }


# ── Console summary ────────────────────────────────────────────────────────────

def print_summary(all_results: list[dict], metrics: dict) -> None:
    print("\n" + "=" * 70)
    print("WM OFF GATE SUMMARY — v5 active_constraints corpus")
    print("=" * 70)
    print(f"T1 success: {metrics['t1_success']}/{metrics['t1_total']}")
    print(f"Eligible T2+: {metrics['eligible_count']}")
    print(f"Planning repair count: {metrics['planning_repair_n']}")
    print(f"planning_repair_rate: {metrics['planning_repair_rate']:.1%}  (gate: ≥10%)")
    print(f"src.prefix rejections: {metrics['src_prefix_n']}  (gate: ≥2)")
    print(f"active_constraints writable (WM ON): {metrics['active_constraints_writable']}")
    print(f"debug_repair_count (eligible): {metrics['debug_repairs']}")
    print(f"DONE eligible: {metrics['done_eligible']}/{metrics['eligible_count']}")
    print()
    print("Old regression checks:")
    for k, v in metrics["regressions"].items():
        mark = "✓" if v == 0 else "✗"
        print(f"  {mark} {k}: {v}")
    print()
    print("Gate criteria:")
    g = metrics["gate"]
    for criterion, ok in g.items():
        if criterion == "PASS":
            continue
        mark = "✓" if ok else "✗"
        print(f"  {mark} {criterion}")
    print()
    result = "PASS" if g["PASS"] else "FAIL"
    print(f"WM OFF GATE: {result}")
    print(f"WM ON approved: {'YES' if g['PASS'] else 'NO'}")
    print("=" * 70)

    print("\nPer-task detail:")
    print(f"{'Proj':<16} {'Pos':>3} {'Status':<26} {'PR':>3} {'DR':>3} {'SrcPfx':>7} {'First rejection reason'}")
    for r in all_results:
        reason = r["first_planning_rejection"][:55] if r["first_planning_rejection"] else ""
        print(
            f"{r['project']:<16} T{r['plan_position']:<2} {r['status']:<26} "
            f"{r['planning_repair_count']:>3} {r['debug_repair_count']:>3} "
            f"{'Yes' if r['src_prefix_rejection'] else '':>7}  {reason}"
        )


# ── Report writer ──────────────────────────────────────────────────────────────

def write_report(
    all_results: list[dict],
    metrics: dict,
    run_ts: str,
    commit_sha: str,
    raw_path: pathlib.Path,
) -> pathlib.Path:
    g = metrics["gate"]
    gate_result = "PASS" if g["PASS"] else "FAIL"
    wm_on_approved = "YES" if g["PASS"] else "NO"

    eligible_rows = [r for r in all_results if is_eligible(r)]

    # Build per-project table
    proj_names = [p["name"] for p in PROJECTS]
    proj_blocks = []
    for pname in proj_names:
        rows = [r for r in all_results if r["project"] == pname]
        t1 = next((r for r in rows if r["plan_position"] == 1), None)
        t2plus = [r for r in rows if is_eligible(r)]
        t1_status = t1["status"] if t1 else "—"
        t1_ok = "✓" if t1_status == "done" else "✗"
        eligible_n = len(t2plus)
        pr_n = sum(r["planning_repair_count"] > 0 for r in t2plus)
        pr_rate = pr_n / eligible_n if eligible_n > 0 else 0.0
        src_n = sum(r["src_prefix_rejection"] for r in t2plus)
        done_n = sum(r["status"] == "done" for r in t2plus)
        proj_blocks.append(
            f"| {pname} | {t1_ok} {t1_status} | {eligible_n} | {pr_n}/{eligible_n} "
            f"({pr_rate:.0%}) | {src_n} | {done_n}/{eligible_n} |"
        )

    # Build per-task table
    task_rows = []
    for r in all_results:
        reason = (r["first_planning_rejection"][:70] if r["first_planning_rejection"] else "—")
        src_mark = "Yes" if r["src_prefix_rejection"] else "—"
        status_display = r["status"]
        elig_mark = "✓" if is_eligible(r) else "—"
        task_rows.append(
            f"| {r['project']} | T{r['plan_position']} | {status_display} | "
            f"{elig_mark} | {r['planning_repair_count']} | {r['debug_repair_count']} | "
            f"{r['completion_repair_count']} | {src_mark} | {reason} |"
        )

    # Build rejection inventory
    rejections = []
    for r in all_results:
        for reasons in r["planning_repair_reasons"]:
            for reason in reasons:
                if reason:
                    rejections.append((r["project"], r["plan_position"], r["task_id"], reason))

    rejection_lines = []
    for proj, pos, tid, reason in rejections:
        src = "Yes" if is_src_prefix_rejection(reason) else "No"
        rejection_lines.append(f"| {proj} | T{pos} | {tid} | {src} | {reason[:100]} |")

    report_path = (
        REPO_ROOT / "docs/roadmap/reports/maintenance"
        / "working-memory-active-constraints-wm-off-20260612.md"
    )
    report_path.write_text(f"""\
# WorkingMemory WM OFF Gate — active_constraints Corpus
**Date:** 2026-06-12
**Status:** COMPLETE
**Runner:** `scripts/maintenance/wm_off_runner_v5.py`
**Commit SHA:** `{commit_sha}`
**Raw data:** `{raw_path.name}`
**Gate result:** **{gate_result}**
**WM ON approved:** **{wm_on_approved}**

Preceded by:
- `working-memory-corpus-design-review-20260612.md` — active_constraints identified as only genuine WM-exclusive signal
- `working-memory-active-constraints-smoke-test-20260612.md` — smoke test PASSED; signal confirmed viable (src. import prefix rejection, planning_repair_count=1 in T2)
- `working-memory-corpus-redesign-after-certification-20260612.md` — wm5 corpus design (src-layout)

---

## 1. Pre-run Verification

### 1.1 Flag state

| Flag | Value | Required |
|------|-------|---------|
| WORKING_MEMORY_PERSISTENCE_ENABLED | False | False ✓ |
| WORKING_MEMORY_RENDER_ENABLED | False | False ✓ |
| WORKING_MEMORY_INJECTION_ENABLED | False | False ✓ |
| REDUCED_PLANNING_PROMPT_ENABLED | False | False ✓ |
| LANGFUSE_ENABLED | False | False ✓ |
| REPO_MEMORY_INJECTION_ENABLED | False | False ✓ |
| PSS_CONTINUATION_INJECTION_ENABLED | False | False ✓ |
| ARTIFACT_CONTINUATION_ENABLED | False | False ✓ |

### 1.2 Lane

| Setting | Value |
|---------|-------|
| PLANNING_BACKEND | {settings.PLANNING_BACKEND or "None (local_openclaw)"} |
| Backend | local_openclaw |
| Models | qwen-local (planning, repair); qwen3-coder (execution) |

### 1.3 Corpus

4 projects × 6 tasks. All projects use src-layout (`src/{{lib}}/`).
T1 creates `pytest.ini` with `pythonpath = src` (completion validator fix for src-layout).
T3–T5 require cross-module imports using the package import path established in T2.

---

## 2. Corpus Summary

| Metric | Value |
|--------|-------|
| Total tasks | {len(all_results)} |
| T1 success | {metrics['t1_success']}/{metrics['t1_total']} |
| Eligible T2+ | {metrics['eligible_count']} |
| Tasks with planning repair | {metrics['planning_repair_n']} |
| **planning_repair_rate** | **{metrics['planning_repair_rate']:.1%}** |
| src.prefix rejections | {metrics['src_prefix_n']} |
| active_constraints writable (WM ON would store) | {metrics['active_constraints_writable']} |
| debug_repair_count (eligible T2+) | {metrics['debug_repairs']} |
| DONE eligible | {metrics['done_eligible']}/{metrics['eligible_count']} |

---

## 3. Per-Project Summary

| Project | T1 | Eligible T2+ | Planning repairs | src.prefix | DONE T2+ |
|---------|----|:---:|:---:|:---:|:---:|
{chr(10).join(proj_blocks)}

---

## 4. Per-Task Detail

| Project | Pos | Status | Elig | PR | DR | CR | SrcPfx | First rejection reason |
|---------|-----|--------|:----:|:--:|:--:|:--:|:------:|------------------------|
{chr(10).join(task_rows)}

PR = planning repairs, DR = debug repairs, CR = completion repairs, Elig = eligible T2+

---

## 5. Validator Rejection Inventory

| Project | Pos | Task ID | src.prefix | Reason |
|---------|-----|---------|:----------:|--------|
{chr(10).join(rejection_lines) if rejection_lines else "| — | — | — | — | No planning rejections recorded |"}

---

## 6. active_constraints Signal Assessment

### 6.1 Signal mechanism

The smoke test established that qwen-local generates imports using the physical
filesystem prefix (`from src.calclib.arithmetic import ...`) in src-layout projects.
The planning validator rejects this: "use package import, not physical src prefix."

If WM were ON, `active_constraints` would carry this constraint from each T2+ task
where a planning repair fired, injecting it into all subsequent planners.

### 6.2 Signal counts from this run

- Planning repairs in eligible T2+: **{metrics['planning_repair_n']}** tasks
- planning_repair_rate: **{metrics['planning_repair_rate']:.1%}**
- src.prefix rejections specifically: **{metrics['src_prefix_n']}**
- active_constraints writable: **{metrics['active_constraints_writable']}** (planning repair AND DONE)

### 6.3 Signal viability

{"active_constraints signal is CONFIRMED viable." if metrics['src_prefix_n'] >= 2 else
 "active_constraints signal is INSUFFICIENT — fewer than 2 src.prefix rejections observed."}
{"planning_repair_rate meets the ≥10% gate." if metrics['planning_repair_rate'] >= 0.10 else
 "planning_repair_rate does NOT meet the ≥10% gate."}

---

## 7. Old Regression Checks

| Check | Count | Pass? |
|-------|------:|:-----:|
| pip-show recurrence | {metrics['regressions']['pip_show']} | {'✓' if metrics['regressions']['pip_show'] == 0 else '✗'} |
| nested_project_folder_command | {metrics['regressions']['nested_project_folder_command']} | {'✓' if metrics['regressions']['nested_project_folder_command'] == 0 else '✗'} |
| VMA deadlock | {metrics['regressions']['vma_deadlock']} | {'✓' if metrics['regressions']['vma_deadlock'] == 0 else '✗'} |
| path_guard_advisory | {metrics['regressions']['path_guard_advisory']} | {'✓' if metrics['regressions']['path_guard_advisory'] == 0 else '✗'} |
| backend_capacity | {metrics['regressions']['backend_capacity']} | {'✓' if metrics['regressions']['backend_capacity'] == 0 else '✗'} |
| empty_response | {metrics['regressions']['empty_response']} | {'✓' if metrics['regressions']['empty_response'] == 0 else '✗'} |

---

## 8. WM OFF Gate

| Criterion | Threshold | Actual | Pass? |
|-----------|:---------:|:------:|:-----:|
| T1 success | ≥ 3/4 | {metrics['t1_success']}/{metrics['t1_total']} | {'✓' if g['t1_success'] else '✗'} |
| Eligible T2+ | ≥ 15 | {metrics['eligible_count']} | {'✓' if g['eligible'] else '✗'} |
| planning_repair_rate | ≥ 10% | {metrics['planning_repair_rate']:.1%} | {'✓' if g['pr_rate'] else '✗'} |
| src.prefix rejections | ≥ 2 | {metrics['src_prefix_n']} | {'✓' if g['ac_count'] else '✗'} |
| pip-show recurrence | 0 | {metrics['regressions']['pip_show']} | {'✓' if g['pip_show'] else '✗'} |
| nested_project_folder_command | 0 | {metrics['regressions']['nested_project_folder_command']} | {'✓' if g['nested'] else '✗'} |
| backend_capacity failures | 0 | {metrics['regressions']['backend_capacity']} | {'✓' if g['backend_cap'] else '✗'} |

**WM OFF GATE: {gate_result}**

---

## 9. Recommendation

**WM ON run: {wm_on_approved}**

{"The WM OFF gate passed. The active_constraints signal is confirmed viable with sufficient planning repair rate. Proceed with the WM ON arm using the same corpus. Expected WM ON outcome: T3+ planners that receive the active_constraints injection will generate correct package imports directly (from calclib.arithmetic import ...) without a planning repair, reducing planning_repair_rate(WM ON) relative to WM OFF." if g["PASS"] else
 "The WM OFF gate did not pass. Do not run the WM ON arm. Review the failure criteria above and redesign the corpus or investigate signal strength before proceeding."}
""")
    return report_path


# ── Main ───────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    _init_runtime()

    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    commit_sha = "unknown"
    try:
        import subprocess
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            capture_output=True, text=True, timeout=5
        )
        if result.returncode == 0:
            commit_sha = result.stdout.strip()
    except Exception:
        pass

    print(f"\n{'='*70}")
    print(f"WM OFF v5 — active_constraints corpus — {run_ts}")
    print(f"Commit: {commit_sha[:12]}")
    print(f"PROJECT_TIMEOUT: {PROJECT_TIMEOUT}s per project")
    print(f"{'='*70}")

    all_results: list[dict] = []
    run_meta = {
        "runner_version":                    "v5",
        "run_ts":                            run_ts,
        "commit_sha":                        commit_sha,
        "already_running_monitor_only_count": 0,
        "auto_advance_stalls":               0,
        "runner_errors":                     0,
    }

    for proj_spec in PROJECTS:
        print(f"\n{'='*60}")
        print(f"PROJECT: {proj_spec['name']}")
        print(f"{'='*60}")

        # ── Slot ──────────────────────────────────────────────────────────────
        print(f"  [slot] Checking before {proj_spec['name']}...")
        wait_for_slot_clear()
        print(f"  [slot] Slot clear.")

        # ── Create project ────────────────────────────────────────────────────
        try:
            proj = api("POST", "/api/v1/projects", json={
                "name":        proj_spec["name"],
                "description": proj_spec["description"],
                "workspace_path": proj_spec["workspace"],
            })
            project_id = proj["id"]
            print(f"  Created project {project_id}: {proj.get('resolved_workspace_path')}")
        except Exception as e:
            print(f"  ERROR creating project: {e}")
            run_meta["runner_errors"] += 1
            continue

        # ── Create tasks ──────────────────────────────────────────────────────
        task_ids: list[int] = []
        for i, task_spec in enumerate(proj_spec["tasks"], start=1):
            try:
                t = api("POST", "/api/v1/tasks", json={
                    "project_id":      project_id,
                    "title":           task_spec["title"],
                    "description":     task_spec["description"],
                    "plan_position":   i,
                    "execution_profile": "full_lifecycle",
                })
                task_ids.append(t["id"])
                print(f"  T{i} created: id={t['id']} {task_spec['title']!r}")
            except Exception as e:
                print(f"  ERROR creating T{i}: {e}")
                run_meta["runner_errors"] += 1
                task_ids.append(None)  # type: ignore[arg-type]

        if None in task_ids:
            print("  ERROR: task creation failed; skipping project")
            run_meta["runner_errors"] += 1
            continue

        # ── Dispatch T1 ───────────────────────────────────────────────────────
        print(f"\n  Dispatching T1 (id={task_ids[0]})...")
        ok, err = dispatch_task(task_ids[0])
        if not ok:
            print(f"  ERROR dispatching T1: {err}")
            run_meta["runner_errors"] += 1
            continue
        print(f"  T1 dispatched. Monitoring (timeout={PROJECT_TIMEOUT}s)...")

        # ── Monitor project ───────────────────────────────────────────────────
        proj_results = monitor_project(proj_spec, task_ids)
        all_results.extend(proj_results)

        for r in proj_results:
            if r.get("already_running_monitor_only"):
                run_meta["already_running_monitor_only_count"] += 1
            if r.get("auto_advance_stalled"):
                run_meta["auto_advance_stalls"] += 1

    # ── Compute metrics ───────────────────────────────────────────────────────
    metrics = compute_metrics(all_results)
    print_summary(all_results, metrics)

    # ── Save raw JSON ─────────────────────────────────────────────────────────
    raw_path = (
        REPO_ROOT / "docs/roadmap/reports/maintenance"
        / f"wm-off-v5-raw-{run_ts}.json"
    )
    raw_path.write_text(json.dumps({
        "meta":    run_meta,
        "metrics": metrics,
        "results": all_results,
    }, indent=2))
    print(f"\nRaw results: {raw_path}")

    # ── Write report ──────────────────────────────────────────────────────────
    report_path = write_report(all_results, metrics, run_ts, commit_sha, raw_path)
    print(f"Report: {report_path}")
