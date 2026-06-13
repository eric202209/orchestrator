#!/usr/bin/env python3
"""
WM active_constraints smoke test runner.

Purpose:
  Run a 2-task smoke test (T1 bootstrap + T2 ambiguous arithmetic) for
  project wm5-smoke-calclib (src-layout, WM OFF) to determine whether
  qwen-local generates weak verification on first attempt when the task
  description uses only ambiguous language ("verify correctness", etc.)
  and does NOT mention pytest, PYTHONPATH, or test runner.

  If T2 first plan triggers a planning validator weak_verification rejection
  -> active_constraints signal is viable -> proceed with full WM corpus redesign.

  If T2 first plan directly uses pytest -> signal is not stimulated by
  ambiguous language -> halt corpus redesign, seek alternative signal.

Deliverable:
  docs/roadmap/reports/maintenance/working-memory-active-constraints-smoke-test-20260612.md
"""
import json
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
BASE_URL        = "http://127.0.0.1:8080"
USER_EMAIL      = "REDACTED"
POLL_INTERVAL   = 20
STALL_TIMEOUT   = 120
PROJECT_TIMEOUT = 3000
SLOT_POLL_INTERVAL = 15
SLOT_KEY        = "orchestrator:backend_slots:local_openclaw"
WORKSPACE_BASE  = pathlib.Path("/root/.openclaw/workspace/vault/projects")
WORKSPACE_SLUG  = "wm5-smoke-calclib-v2"

TERMINAL_TASK    = {"done", "failed", "paused", "cancelled"}
TERMINAL_SESSION = {"completed", "failed", "cancelled", "paused", "error"}

TOKEN:   str  = ""
HEADERS: dict = {}
REDIS         = None  # type: ignore[assignment]

# ── Task descriptions ──────────────────────────────────────────────────────────

T1_DESC = (
    "Bootstrap calclib: "
    "Create src/calclib/__init__.py with __version__ = \"0.1.0\". "
    "Create tests/__init__.py (empty). "
    "Create tests/test_sanity.py with one test that imports calclib and "
    "asserts calclib.__version__ == \"0.1.0\". "
    "Create pytest.ini at the project root with content: "
    "[pytest]\\npythonpath = src\\n"
    "(this makes pytest discover calclib from the src/ directory). "
    "Verify explicitly with: PYTHONPATH=src python3 -m pytest tests/ -q. "
    "Confirm 1 test passed."
)

# Intentionally ambiguous — no pytest, no PYTHONPATH, no "test runner"
T2_DESC = (
    "Arithmetic module: "
    "Add src/calclib/arithmetic.py with four functions: "
    "add(a, b), subtract(a, b), multiply(a, b), divide(a, b). "
    "divide must raise ZeroDivisionError when b == 0. "
    "Create tests/test_arithmetic.py with tests for all four functions, "
    "including the ZeroDivisionError case. "
    "Verify correctness. "
    "Check that the implementation works. "
    "Confirm the module behaves correctly."
)

# ── Runtime init ───────────────────────────────────────────────────────────────

def _init_runtime() -> None:
    global TOKEN, HEADERS, REDIS
    assert not settings.WORKING_MEMORY_PERSISTENCE_ENABLED, \
        "WORKING_MEMORY_PERSISTENCE_ENABLED must be False"
    assert not settings.WORKING_MEMORY_RENDER_ENABLED, \
        "WORKING_MEMORY_RENDER_ENABLED must be False"
    assert not settings.WORKING_MEMORY_INJECTION_ENABLED, \
        "WORKING_MEMORY_INJECTION_ENABLED must be False"
    print("[init] All WM flags confirmed OFF")

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
    print("[init] Auth token created, Redis connected")


# ── API helpers ────────────────────────────────────────────────────────────────

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


# ── Dispatch ───────────────────────────────────────────────────────────────────

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

def get_task_events(task_id: int) -> list:
    events_dir = WORKSPACE_BASE / WORKSPACE_SLUG / ".agent" / "events"
    if not events_dir.exists():
        return []
    events = []
    for jsonl_file in events_dir.glob(f"*task_{task_id}.jsonl"):
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


def get_state_snapshots(task_id: int) -> list:
    events_dir = WORKSPACE_BASE / WORKSPACE_SLUG / ".agent" / "events"
    if not events_dir.exists():
        return []
    snapshots = []
    for jsonl_file in events_dir.glob(f"*task_{task_id}_state_snapshots.jsonl"):
        try:
            with open(jsonl_file) as fh:
                for line in fh:
                    line = line.strip()
                    if line:
                        try:
                            snapshots.append(json.loads(line))
                        except json.JSONDecodeError:
                            pass
        except Exception:
            pass
    return snapshots


def extract_first_plan_verification(snapshots: list) -> dict:
    """
    Extract verification commands from the FIRST plan attempt.
    Returns info about each step's verification command.
    """
    # Find first snapshot with plan_steps that has been validated
    for snap in snapshots:
        if snap.get("trigger") == "validation_plan" and snap.get("plan_steps"):
            steps = snap["plan_steps"]
            verifications = []
            commands_all = []
            for step in steps:
                v = step.get("verification", "")
                cmds = step.get("commands", [])
                verifications.append(v)
                commands_all.extend(cmds)
            return {
                "plan_steps_count": len(steps),
                "step_verifications": verifications,
                "step_commands": commands_all,
                "snapshot_index": snap.get("snapshot_index"),
                "raw_steps": steps,
            }
    return {}


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


def classify_weak_verification(first_plan: dict) -> dict:
    """
    Classify whether the first plan uses weak or strong verification.
    Weak: python -c, print(ok), inline import check, no pytest invocation.
    Strong: any pytest/python -m pytest invocation.
    """
    all_text = []
    for v in first_plan.get("step_verifications", []):
        all_text.append(str(v).lower())
    for c in first_plan.get("step_commands", []):
        all_text.append(str(c).lower())

    full_text = " ".join(all_text)

    has_pytest = "pytest" in full_text
    has_python_c = "python" in full_text and ("-c" in full_text or "python -c" in full_text)
    has_inline_import = ("import " in full_text and
                         ("print(" in full_text or "assert " in full_text) and
                         not has_pytest)
    has_pythonpath_src = "pythonpath=src" in full_text

    if has_pytest:
        verdict = "strong_pytest"
    elif has_python_c or has_inline_import:
        verdict = "weak_inline"
    else:
        verdict = "ambiguous_or_empty"

    return {
        "verdict": verdict,
        "has_pytest": has_pytest,
        "has_python_c": has_python_c,
        "has_inline_import": has_inline_import,
        "has_pythonpath_src": has_pythonpath_src,
        "full_text_sample": full_text[:400],
    }


def detect_old_regressions(events: list) -> dict:
    """Check for known pre-existing failure classes."""
    all_text = json.dumps(events).lower()
    return {
        "pip_show": "pip show" in all_text and "pip_show" in all_text,
        "nested_project_folder_command": "nested_project_folder_command" in all_text,
        "vma": ("verification plan mutates" in all_text or
                "vma_repair_triggered" in all_text),
        "path_guard_advisory": "path_guard_advisory" in all_text,
        "backend_capacity": "backend_capacity" in all_text,
    }


# ── Monitor single task ────────────────────────────────────────────────────────

def monitor_task(task_id: int, label: str) -> str:
    """Poll until task reaches terminal status or PROJECT_TIMEOUT."""
    start = time.time()
    last_status = ""
    while time.time() - start < PROJECT_TIMEOUT:
        status = db_task_status(task_id)
        if status != last_status:
            elapsed = int(time.time() - start)
            print(f"  [{label}] id={task_id} [{status}] {elapsed}s")
            last_status = status
        if status in TERMINAL_TASK:
            return status
        # Check for stall — if pending for too long after dispatch, retry
        time.sleep(POLL_INTERVAL)
    return f"runner_timeout__{db_task_status(task_id)}"


# ── Main ───────────────────────────────────────────────────────────────────────

def main() -> dict:
    _init_runtime()

    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    print(f"\n[smoke] Starting {WORKSPACE_SLUG} smoke test at {run_ts}")
    print(f"[smoke] Workspace: {WORKSPACE_BASE / WORKSPACE_SLUG}")

    # ── Wait for slot ─────────────────────────────────────────────────────────
    print("\n[slot] Checking backend slot...")
    wait_for_slot_clear()
    print("[slot] Slot clear.")

    # ── Create project ────────────────────────────────────────────────────────
    print(f"\n[project] Creating project {WORKSPACE_SLUG}...")
    proj = api("POST", "/api/v1/projects", json={
        "name": WORKSPACE_SLUG,
        "description": "WM active_constraints smoke test — calclib src-layout, 2 tasks, WM OFF",
        "workspace_path": WORKSPACE_SLUG,
    })
    project_id = proj["id"]
    print(f"[project] id={project_id}, workspace={proj.get('resolved_workspace_path')}")

    # ── Create tasks ──────────────────────────────────────────────────────────
    t1 = api("POST", "/api/v1/tasks", json={
        "project_id": project_id,
        "title": "Bootstrap calclib",
        "description": T1_DESC,
        "plan_position": 1,
        "execution_profile": "full_lifecycle",
    })
    t1_id = t1["id"]
    print(f"[tasks] T1 id={t1_id} 'Bootstrap calclib'")

    t2 = api("POST", "/api/v1/tasks", json={
        "project_id": project_id,
        "title": "Arithmetic module",
        "description": T2_DESC,
        "plan_position": 2,
        "execution_profile": "full_lifecycle",
    })
    t2_id = t2["id"]
    print(f"[tasks] T2 id={t2_id} 'Arithmetic module'")

    # ── Dispatch T1 ───────────────────────────────────────────────────────────
    print(f"\n[T1] Dispatching id={t1_id}...")
    ok, err = dispatch_task(t1_id)
    if not ok:
        raise RuntimeError(f"T1 dispatch failed: {err}")
    print(f"[T1] Dispatched. Monitoring...")

    t1_status = monitor_task(t1_id, "T1")
    print(f"[T1] Final status: {t1_status}")

    # ── Collect T1 events ─────────────────────────────────────────────────────
    t1_events = get_task_events(t1_id)
    t1_debug_count, t1_debug_classes = count_debug_repairs(t1_events)
    t1_plan_count, _ = count_planning_repairs(t1_events)
    print(f"[T1] debug_repairs={t1_debug_count} planning_repairs={t1_plan_count}")

    # ── Wait for slot before T2 ───────────────────────────────────────────────
    print("\n[slot] Waiting for slot before T2...")
    wait_for_slot_clear()
    print("[slot] Slot clear.")

    # ── Dispatch T2 (stall-retry may have already auto-advanced; try anyway) ──
    print(f"\n[T2] Dispatching id={t2_id}...")
    ok2, err2 = dispatch_task(t2_id)
    if not ok2:
        if "already running" in err2.lower():
            print(f"[T2] Already running — monitoring only")
        else:
            print(f"[T2] Dispatch warning: {err2}")
    else:
        print(f"[T2] Dispatched.")

    print(f"[T2] Monitoring id={t2_id}...")
    t2_status = monitor_task(t2_id, "T2")
    print(f"[T2] Final status: {t2_status}")

    # ── Collect T2 events and snapshots ───────────────────────────────────────
    t2_events    = get_task_events(t2_id)
    t2_snapshots = get_state_snapshots(t2_id)

    t2_debug_count, t2_debug_classes = count_debug_repairs(t2_events)
    t2_plan_count, t2_plan_reasons   = count_planning_repairs(t2_events)

    first_plan = extract_first_plan_verification(t2_snapshots)
    verification_class = classify_weak_verification(first_plan)
    old_regressions = detect_old_regressions(t2_events)

    # ── Determine active_constraints viability ────────────────────────────────
    # viable if: first T2 plan was rejected (planning_repair_count >= 1) AND
    # at least one rejection reason is non-empty
    planning_repair_fired = t2_plan_count >= 1
    rejection_reasons_nonempty = any(
        len(r) > 0 for r in t2_plan_reasons
    )
    active_constraints_viable = planning_repair_fired and rejection_reasons_nonempty

    # active_constraints would be populated if WM were ON after a repair
    wm_would_populate = active_constraints_viable and t2_status == "done"

    result = {
        "run_ts": run_ts,
        "project_id": project_id,
        "t1_task_id": t1_id,
        "t2_task_id": t2_id,
        "t1_status": t1_status,
        "t1_debug_repair_count": t1_debug_count,
        "t1_planning_repair_count": t1_plan_count,
        "t2_status": t2_status,
        "t2_debug_repair_count": t2_debug_count,
        "t2_debug_repair_classes": t2_debug_classes,
        "t2_planning_repair_count": t2_plan_count,
        "t2_planning_repair_reasons": [list(r) for r in t2_plan_reasons],
        "t2_first_plan": first_plan,
        "t2_verification_classification": verification_class,
        "planning_repair_fired": planning_repair_fired,
        "active_constraints_viable": active_constraints_viable,
        "wm_would_populate_active_constraints": wm_would_populate,
        "old_regressions": old_regressions,
    }

    # ── Save raw JSON ─────────────────────────────────────────────────────────
    raw_path = (REPO_ROOT / "docs/roadmap/reports/maintenance" /
                f"wm-active-constraints-smoke-raw-{run_ts}.json")
    raw_path.write_text(json.dumps(result, indent=2))
    print(f"\n[done] Raw results: {raw_path}")

    return result


if __name__ == "__main__":
    result = main()

    t1_status  = result["t1_status"]
    t2_status  = result["t2_status"]
    pr_fired   = result["planning_repair_fired"]
    ac_viable  = result["active_constraints_viable"]
    vc         = result["t2_verification_classification"]
    reasons    = result["t2_planning_repair_reasons"]
    pr_count   = result["t2_planning_repair_count"]
    dr_count   = result["t2_debug_repair_count"]
    regressions = result["old_regressions"]
    fp         = result["t2_first_plan"]

    print("\n" + "="*60)
    print("SMOKE TEST SUMMARY")
    print("="*60)
    print(f"T1 status:                {t1_status}")
    print(f"T2 status:                {t2_status}")
    print(f"T2 first plan verdict:    {vc.get('verdict')}")
    print(f"T2 planning_repair_count: {pr_count}")
    print(f"T2 debug_repair_count:    {dr_count}")
    print(f"Planning repair fired:    {pr_fired}")
    print(f"active_constraints viable:{ac_viable}")
    print(f"WM would populate:        {result['wm_would_populate_active_constraints']}")
    print()
    if pr_fired:
        print("Rejection reasons:")
        for i, r in enumerate(reasons, 1):
            for line in r:
                print(f"  [{i}] {line}")
    print()
    print("First plan step verifications:")
    for i, v in enumerate(fp.get("step_verifications", []), 1):
        print(f"  step {i}: {v[:200]}")
    print()
    print("Old regression checks:")
    for k, v in regressions.items():
        print(f"  {k}: {v}")
    print()

    if ac_viable:
        print("DECISION: active_constraints signal IS viable.")
        print("  -> Proceed with full WM corpus redesign around planning_repair_rate.")
    else:
        verb = vc.get("verdict", "unknown")
        print(f"DECISION: active_constraints signal NOT stimulated.")
        print(f"  -> T2 first plan used '{verb}' — no planning repair fired.")
        print("  -> Do NOT run full WM corpus.")
        print("  -> Recommend alternative signal or LLM summary experiment.")
