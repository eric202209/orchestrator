#!/usr/bin/env python3
"""
T1 Reliability Confirmation — resume driver v2 (post-reboot, post-stale-lock).

State when this driver starts:
  - calclib (project 585, tasks 707-712): T1/T2 DONE pre-reboot.
    T3 (709) FAILED at 14:38 UTC on a stale project mutation lock left by the
    rebooted session 668 (lock removed manually at ~14:43 UTC). T4-T6 PENDING.
  - pathtools (project 586, tasks 713-718): created by driver v1 phase 2;
    T1 (713) RUNNING in session 669 — do not re-dispatch, monitor only.
  - strtools: not created yet.

Phases:
  A: monitor pathtools 713-718 (T1 already running; auto-advance as usual).
  B: create + run t1-confirm-strtools exactly as the runner would.
  C: re-dispatch calclib T3 (709) now that the stale lock is gone; monitor
     707-712 to completion.

All slot/monitor/collection logic is imported from the runner unchanged.
"""
import json
import pathlib
from datetime import datetime

from scripts.maintenance._runner_common import ensure_repo_on_syspath, load_sibling_module

ensure_repo_on_syspath()
r = load_sibling_module("t1runner", "t1_reliability_confirmation_runner.py")

CALCLIB_SPEC = r.PROJECTS[0]
PATHTOOLS_SPEC = r.PROJECTS[1]
STRTOOLS_SPEC = r.PROJECTS[2]

CALCLIB_TASK_IDS = [707, 708, 709, 710, 711, 712]
PATHTOOLS_TASK_IDS = [713, 714, 715, 716, 717, 718]
CALCLIB_RESUME_TASK = 709

if __name__ == "__main__":
    r._init_runtime()

    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    all_results = []
    run_meta = {
        "runner": "t1_reliability_confirmation_resume_v2",
        "run_ts": run_ts,
        "resumed_after_reboot": True,
        "calclib_project_id": 585,
        "pathtools_project_id": 586,
        "calclib_t3_stale_lock_failure": True,
        "calclib_resumed_from_task": CALCLIB_RESUME_TASK,
        "runner_errors": 0,
        "planning_backend": str(r.settings.PLANNING_BACKEND),
        "planning_repair_model": str(r.settings.PLANNING_REPAIR_MODEL),
    }

    # ── Phase A: monitor pathtools (T1 already running in session 669) ────────
    print(f"\n{'='*60}")
    print(f"PROJECT (MONITOR-ONLY ATTACH): {PATHTOOLS_SPEC['name']} — tasks {PATHTOOLS_TASK_IDS}")
    print(f"{'='*60}")
    proj_results = r.monitor_project(PATHTOOLS_SPEC, PATHTOOLS_TASK_IDS)
    all_results.extend(proj_results)

    # ── Phase B: strtools (fresh, as runner main does) ─────────────────────────
    proj_spec = STRTOOLS_SPEC
    print(f"\n{'='*60}")
    print(f"PROJECT: {proj_spec['name']}")
    print(f"{'='*60}")
    print(f"  [slot] Checking before {proj_spec['name']}...")
    r.wait_for_slot_clear()
    print("  [slot] Slot clear.")

    strtools_ok = True
    try:
        proj = r.api("POST", "/api/v1/projects", json={
            "name": proj_spec["name"],
            "description": proj_spec["description"],
            "workspace_path": proj_spec["workspace"],
        })
        project_id = proj["id"]
        print(f"  Created project {project_id}: {proj['resolved_workspace_path']}")
    except Exception as e:
        print(f"  ERROR creating project: {e}")
        run_meta["runner_errors"] += 1
        strtools_ok = False

    if strtools_ok:
        task_ids = []
        for i, task_spec in enumerate(proj_spec["tasks"], start=1):
            try:
                t = r.api("POST", "/api/v1/tasks", json={
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
            print("  ERROR: task creation failed; skipping strtools")
            run_meta["runner_errors"] += 1
        else:
            print(f"\n  Dispatching T1 (id={task_ids[0]})...")
            ok, err = r.dispatch_task(task_ids[0])
            if not ok:
                print(f"  ERROR dispatching T1: {err}")
                run_meta["runner_errors"] += 1
            else:
                print(f"  T1 dispatched. Monitoring (timeout={r.PROJECT_TIMEOUT}s)...")
                proj_results = r.monitor_project(proj_spec, task_ids)
                all_results.extend(proj_results)

    # ── Phase C: resume calclib T3 (stale lock removed) ───────────────────────
    print(f"\n{'='*60}")
    print(f"PROJECT (RESUME T3): {CALCLIB_SPEC['name']} — tasks {CALCLIB_TASK_IDS}")
    print(f"{'='*60}")
    print("  [slot] Checking before calclib resume...")
    r.wait_for_slot_clear()
    print("  [slot] Slot clear.")

    print(f"  Re-dispatching T3 (id={CALCLIB_RESUME_TASK})...")
    ok, err = r.dispatch_task(CALCLIB_RESUME_TASK)
    if not ok:
        print(f"  ERROR dispatching T3: {err}")
        run_meta["runner_errors"] += 1
    else:
        print(f"  T3 dispatched. Monitoring (timeout={r.PROJECT_TIMEOUT}s)...")
    proj_results = r.monitor_project(CALCLIB_SPEC, CALCLIB_TASK_IDS)
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

    # ── Summary (same logic as runner main) ───────────────────────────────────
    print("\n" + "=" * 60)
    print("T1 RELIABILITY CONFIRMATION SUMMARY (RESUMED RUN v2)")
    print("=" * 60)

    t1_results = [x for x in all_results if x["plan_position"] == 1]
    t1_done = [x for x in t1_results if x["status"] == "done"]
    t1_failed = [x for x in t1_results if x["status"] == "failed"]
    pip_show_recurrences = [x for x in t1_results if x["pip_show_failure_detected"]]
    env_cap_failures = [x for x in all_results if x["env_capacity_failure"]]

    t2plus_results = [x for x in all_results if x["plan_position"] > 1]
    t2plus_eligible = [
        x for x in t2plus_results
        if x["status"] in ("done", "failed")
        and x["execution_reached"]
        and not x["env_capacity_failure"]
    ]

    print(f"\nT1 success (done):         {len(t1_done)}/{len(t1_results)} "
          f"(calclib T1 done pre-reboot, counted separately)")
    print(f"T1 failed:                 {len(t1_failed)}")
    print(f"pip show recurrence:       {len(pip_show_recurrences)} (should be 0)")
    print(f"Backend capacity failures: {len(env_cap_failures)}")
    print(f"T2+ eligible:              {len(t2plus_eligible)}")
    print(f"Runner errors:             {run_meta['runner_errors']}")

    print("\nT1 detail:")
    for x in t1_results:
        pip_flag = " ← pip show RECURRED" if x["pip_show_failure_detected"] else ""
        cvf = x.get("completion_validation_failures", [])
        cvf_str = ""
        if cvf:
            cmds = [f["failed_command"] for f in cvf]
            cvf_str = f" cvf=[{', '.join(cmds[:3])}]"
        print(
            f"  {x['project']} T1 [{x['status']}] "
            f"plan_repairs={x['planning_repair_count']} "
            f"debug_repairs={x['debug_repair_count']}"
            f"{cvf_str}{pip_flag}"
        )
    print(f"\nRaw results:               {out_path}")
