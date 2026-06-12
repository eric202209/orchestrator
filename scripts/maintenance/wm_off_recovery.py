#!/usr/bin/env python3
"""
WM OFF recovery runner.
Monitors strtools tasks 611-616 (already running), then dispatches
calclib (599-604) and pathtools (605-610) in sequence.
Collects event data and produces final report.
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
from app.models import Task, TaskStatus  # noqa: E402
from app.config import settings  # noqa: E402

BASE_URL = "http://127.0.0.1:8080"
USER_EMAIL = "REDACTED"
POLL_INTERVAL = 20
TASK_TIMEOUT = 600

assert not settings.WORKING_MEMORY_PERSISTENCE_ENABLED
assert not settings.WORKING_MEMORY_RENDER_ENABLED
assert not settings.WORKING_MEMORY_INJECTION_ENABLED
assert not settings.REDUCED_PLANNING_PROMPT_ENABLED
print("✓ All flags confirmed OFF")

TOKEN = create_access_token({"sub": USER_EMAIL})
HEADERS = {"Authorization": f"Bearer {TOKEN}", "Content-Type": "application/json"}


def api(method, path, **kwargs):
    r = requests.request(method, f"{BASE_URL}{path}", headers=HEADERS, **kwargs)
    r.raise_for_status()
    return r.json()


# Known project/task IDs from the initial runner
PROJECTS = [
    {
        "name": "wm-strtools",
        "workspace": "wm-strtools-off",
        "project_id": 569,
        "task_ids": [611, 612, 613, 614, 615, 616],
        "titles": [
            "Bootstrap strtools package",
            "Implement transform module",
            "Implement validate module",
            "Implement format module",
            "Add edge case tests",
            "Final verification and exports",
        ],
        "monitor_only": True,  # already running; don't dispatch task 1
    },
    {
        "name": "wm-calclib",
        "workspace": "wm-calclib-off",
        "project_id": 567,
        "task_ids": [599, 600, 601, 602, 603, 604],
        "titles": [
            "Bootstrap calclib package",
            "Implement arithmetic module",
            "Implement stats module",
            "Add edge case tests",
            "Add public API exports",
            "Final verification build",
        ],
        "monitor_only": False,
    },
    {
        "name": "wm-pathtools",
        "workspace": "wm-pathtools-off",
        "project_id": 568,
        "task_ids": [605, 606, 607, 608, 609, 610],
        "titles": [
            "Bootstrap pathtools package",
            "Implement filters module",
            "Implement walker module",
            "Implement matchers module",
            "Add public API exports",
            "Integration test and final verification",
        ],
        "monitor_only": False,
    },
]

TERMINAL = {"done", "failed", "paused", "cancelled"}


def get_task_status(task_id: int) -> str:
    db = SessionLocal()
    try:
        db.expire_all()
        t = db.query(Task).filter(Task.id == task_id).first()
        if not t:
            return "not_found"
        return t.status.value
    finally:
        db.close()


def wait_for_task(task_id: int, timeout: int = TASK_TIMEOUT) -> str:
    start = time.time()
    while True:
        status = get_task_status(task_id)
        if status in TERMINAL:
            return status
        elapsed = time.time() - start
        if elapsed > timeout:
            return f"timeout_after_{int(elapsed)}s"
        print(f"    [{status}] task {task_id} ... {int(elapsed)}s elapsed", end="\r")
        time.sleep(POLL_INTERVAL)


def dispatch_task(task_id: int) -> bool:
    try:
        api("POST", f"/api/v1/tasks/{task_id}/retry", json={})
        return True
    except Exception as e:
        print(f"  ERROR dispatching task {task_id}: {e}")
        return False


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


def count_debug_repairs(events: list) -> tuple:
    repairs = [e for e in events if e.get("event_type") == "debug_repair_attempted"]
    classes = [e.get("details", {}).get("debug_failure_class", "unknown") for e in repairs]
    return len(repairs), classes


def count_planning_repairs(events: list) -> tuple:
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


def working_memory_exists(workspace: str) -> bool:
    return pathlib.Path(
        f"/root/.openclaw/workspace/vault/projects/{workspace}/.agent/working_memory.json"
    ).exists()


results = []

for proj in PROJECTS:
    print(f"\n{'='*60}")
    print(f"PROJECT: {proj['name']} (id={proj['project_id']})")
    print(f"{'='*60}")

    task_ids = proj["task_ids"]
    titles = proj["titles"]
    workspace = proj["workspace"]
    monitor_only = proj["monitor_only"]

    for i, (task_id, title) in enumerate(zip(task_ids, titles), start=1):
        plan_pos = i

        # For strtools, check if task 1 is already done before monitoring
        if monitor_only and i == 1:
            status = get_task_status(task_id)
            print(f"  Task {i}/6: {title} (id={task_id}) — already {status}")
            events = get_task_events(workspace, task_id)
            debug_count, debug_classes = count_debug_repairs(events)
            plan_count, plan_reasons = count_planning_repairs(events)
            pythonpath_repair = is_pythonpath_repair(debug_classes, plan_reasons)
            wm_exists = working_memory_exists(workspace)
            execution_reached = any(
                e.get("event_type") in ("step_started", "step_finished")
                for e in events
            )
            row = {
                "project": proj["name"],
                "plan_position": plan_pos,
                "task_id": task_id,
                "title": title,
                "status": status,
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
            if status not in ("done",):
                print(f"  STOP: strtools Task 1 not done (status={status}). Skipping project.")
                break
            continue

        print(f"\n  ── Task {i}/6: {title} (id={task_id}) ──")

        # Check if already terminal (e.g. from prior run)
        current_status = get_task_status(task_id)

        if current_status in TERMINAL and not monitor_only:
            # Needs re-dispatch
            print(f"  Current status: {current_status}. Dispatching...")
            ok = dispatch_task(task_id)
            if not ok:
                results.append({
                    "project": proj["name"],
                    "plan_position": plan_pos,
                    "task_id": task_id,
                    "title": title,
                    "status": "dispatch_failed",
                })
                if i == 1:
                    print(f"  STOP: Task 1 dispatch failed. Skipping remaining tasks.")
                    break
                continue
            print(f"  Dispatched. Polling every {POLL_INTERVAL}s (max {TASK_TIMEOUT}s)...")
            final_status = wait_for_task(task_id, TASK_TIMEOUT)
            print()
        elif current_status == "running" or current_status == "pending":
            # Already in-flight, just monitor
            print(f"  Current status: {current_status}. Monitoring...")
            final_status = wait_for_task(task_id, TASK_TIMEOUT)
            print()
        else:
            # Terminal already but monitor_only context (strtools T2+)
            final_status = current_status
            print(f"  Already terminal: {final_status}")

        print(f"  Final status: {final_status}")

        events = get_task_events(workspace, task_id)
        debug_count, debug_classes = count_debug_repairs(events)
        plan_count, plan_reasons = count_planning_repairs(events)
        pythonpath_repair = is_pythonpath_repair(debug_classes, plan_reasons)
        wm_exists = working_memory_exists(workspace)
        execution_reached = any(
            e.get("event_type") in ("step_started", "step_finished")
            for e in events
        )

        row = {
            "project": proj["name"],
            "plan_position": plan_pos,
            "task_id": task_id,
            "title": title,
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

        if i == 1 and final_status not in ("done",):
            print(f"  STOP: Task 1 failed with status={final_status}. Skipping remaining tasks.")
            for j in range(i + 1, 7):
                results.append({
                    "project": proj["name"],
                    "plan_position": j,
                    "task_id": task_ids[j - 1],
                    "title": titles[j - 1],
                    "status": "skipped_task1_failed",
                })
            break

# Save raw results
timestamp = datetime.now().strftime("%Y%m%d_%H%M%S")
out_path = pathlib.Path(
    REPO_ROOT / "docs/roadmap/reports/maintenance" / f"wm-off-recovery-raw-{timestamp}.json"
)
out_path.write_text(json.dumps(results, indent=2))
print(f"\n\nRaw results saved: {out_path}")

# Summary
print("\n" + "=" * 60)
print("WM OFF ARM SUMMARY (Recovery)")
print("=" * 60)

task2plus_eligible = [
    r for r in results
    if r.get("plan_position", 0) > 1
    and r.get("status") in ("done", "failed")
    and r.get("execution_reached", False)
]

qualifying_repairs = [r for r in task2plus_eligible if r.get("debug_repair_count", 0) > 0]
constraint_rediscoveries = [r for r in task2plus_eligible if r.get("pythonpath_constraint_repair")]
done_tasks = [r for r in results if r.get("status") == "done"]
all_terminal = [r for r in results if r.get("status") in ("done", "failed", "paused", "cancelled")]

debug_repair_rate = (
    len(qualifying_repairs) / len(task2plus_eligible) if task2plus_eligible else 0.0
)

completion_rate = (
    f"{len(done_tasks)}/{len(all_terminal)} ({len(done_tasks)/len(all_terminal):.1%})"
    if all_terminal else "N/A"
)

print(f"Total tasks recorded:       {len(results)}")
print(f"Task 2+ eligible:           {len(task2plus_eligible)}")
print(f"Tasks with debug repairs:   {len(qualifying_repairs)}")
print(f"Constraint rediscoveries:   {len(constraint_rediscoveries)}")
print(f"debug_repair_rate_wm_off:   {debug_repair_rate:.1%}")
print(f"Task completion rate:       {completion_rate}")

corpus_gate = debug_repair_rate >= 0.10 and len(task2plus_eligible) >= 10
print(f"\nCorpus validity gate (≥10 eligible, ≥10% repair rate): {'PASS' if corpus_gate else 'FAIL'}")
print(f"WM ON arm approved: {'YES' if corpus_gate else 'NO'}")
print(f"\nRaw results: {out_path}")
