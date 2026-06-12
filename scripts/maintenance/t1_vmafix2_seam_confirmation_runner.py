#!/usr/bin/env python3
"""
VMA Fix + Arbitration Seam Fixes — Confirmation Rerun (post-seam-fixes).

Corpus: same 6-task T1 corpus as prior vmafix runs.
Workspaces: t1-vmafix2-{calclib,pathtools,strtools} (fresh).
Seam fixes active: C-1, H-2, H-6 from arbitration-seam-fixes-20260611.md.
Baseline lane: PLANNING_BACKEND=None -> local_openclaw, qwen-local repair.
No WM, no lane swap, no validator changes. Validation only.
"""
import copy
import json
import pathlib
import sys
from datetime import datetime

from scripts.maintenance._runner_common import ensure_repo_on_syspath, load_sibling_module

ensure_repo_on_syspath()
pg = load_sibling_module("pathguard", "t1_pathguard_telemetry_runner.py")

r = pg.r


def vmafix2_projects() -> list:
    projects = copy.deepcopy(r.PROJECTS)
    for p in projects:
        p["name"] = p["name"].replace("t1-confirm-", "t1-vmafix2-")
        p["workspace"] = p["workspace"].replace("t1-confirm-", "t1-vmafix2-")
        p["description"] = p["description"].replace(
            "T1 reliability confirmation — venv pip show fix verification",
            "vma fix + arbitration seam fixes (C-1/H-2/H-6) confirmation rerun",
        )
    return projects


if __name__ == "__main__":
    r._init_runtime()
    pg.assert_baseline_lane_and_flags()

    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    all_results = []
    run_meta = {
        "runner": "t1_vmafix2_seam_confirmation",
        "run_ts": run_ts,
        "runner_errors": 0,
        "planning_backend": str(r.settings.PLANNING_BACKEND),
        "planning_repair_base_url": str(r.settings.PLANNING_REPAIR_BASE_URL),
        "planning_repair_model": str(r.settings.PLANNING_REPAIR_MODEL),
        "execution_backend": str(r.settings.EXECUTION_BACKEND),
        "wm_persistence": r.settings.WORKING_MEMORY_PERSISTENCE_ENABLED,
        "wm_render": r.settings.WORKING_MEMORY_RENDER_ENABLED,
        "wm_injection": r.settings.WORKING_MEMORY_INJECTION_ENABLED,
        "langfuse_enabled": r.settings.LANGFUSE_ENABLED,
        "repo_memory_injection": r.settings.REPO_MEMORY_INJECTION_ENABLED,
        "pss_continuation": r.settings.PSS_CONTINUATION_INJECTION_ENABLED,
        "artifact_continuation": r.settings.ARTIFACT_CONTINUATION_ENABLED,
        "reduced_planning_prompt": r.settings.REDUCED_PLANNING_PROMPT_ENABLED,
        "seam_fixes": ["C-1", "H-2", "H-6"],
    }

    for proj_spec in vmafix2_projects():
        print(f"\n{'='*60}")
        print(f"PROJECT: {proj_spec['name']}")
        print(f"{'='*60}")

        print(f"  [slot] Checking before {proj_spec['name']}...")
        r.wait_for_slot_clear()
        print(f"  [slot] Slot clear.")

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
            continue

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
            print("  ERROR: task creation failed; skipping project")
            run_meta["runner_errors"] += 1
            continue

        print(f"\n  Dispatching T1 (id={task_ids[0]})...")
        ok, err = r.dispatch_task(task_ids[0])
        if not ok:
            print(f"  ERROR dispatching T1: {err}")
            run_meta["runner_errors"] += 1
            continue
        print(f"  T1 dispatched. Monitoring (timeout={r.PROJECT_TIMEOUT}s)...")

        proj_results = pg.monitor_project_pathguard(proj_spec, task_ids)
        all_results.extend(proj_results)

    out_dir = pathlib.Path(
        "docs/roadmap/reports/maintenance"
        "/project_aware_continuation_execution"
        "/slices_C_working_memory_persistence"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"t1-vmafix2-seam-raw-{run_ts}.json"
    out_path.write_text(json.dumps({"meta": run_meta, "results": all_results}, indent=2))
    print(f"\n\nRaw results saved: {out_path}")

    print("\n" + "=" * 60)
    print("VMA FIX + SEAM FIXES CONFIRMATION RERUN SUMMARY")
    print("=" * 60)

    done_count = sum(1 for r2 in all_results if r2["status"] == "done")
    failed_count = sum(1 for r2 in all_results if r2["status"] == "failed")
    blocked_count = sum(1 for r2 in all_results if r2.get("blocked_prior_task_failed"))
    corpus_total = len(all_results)
    corpus_completed = done_count + failed_count + blocked_count

    t1_results = [r2 for r2 in all_results if r2["plan_position"] == 1]
    t1_done = [r2 for r2 in t1_results if r2["status"] == "done"]
    t6_results = [r2 for r2 in all_results if r2["plan_position"] == 6]
    t6_done = [r2 for r2 in t6_results if r2["status"] == "done"]

    advisory_total = sum(r2["advisory_event_count"] for r2 in all_results)
    nested_total = sum(r2["nested_project_folder_command_count"] for r2 in all_results)
    vma_tasks = [r2 for r2 in all_results if r2["verification_mutates_source_assets"]]
    debug_repair_total = sum(r2["debug_repair_count"] for r2 in all_results)
    pip_show_recurrences = [r2 for r2 in all_results if r2.get("pip_show_failure_detected")]
    env_cap_failures = [r2 for r2 in all_results if r2["env_capacity_failure"]]
    all_new_codes = []
    for r2 in all_results:
        all_new_codes.extend(r2.get("new_failure_codes", []))

    print(f"\nTotal tasks:                     {corpus_total}")
    print(f"DONE:                            {done_count}")
    print(f"FAILED:                          {failed_count}")
    print(f"Blocked (prior task failed):     {blocked_count}")
    print(f"Corpus completion:               {corpus_completed}/{corpus_total}")
    print(f"T1 success (done):               {len(t1_done)}/{len(t1_results)}")
    print(f"T6 success (done):               {len(t6_done)}/{len(t6_results)}")
    print(f"verification_mutates_source:     {len(vma_tasks)} (target=0)")
    print(f"advisory_event count:            {advisory_total} (target=0)")
    print(f"nested_project_folder_command:   {nested_total} (target=0)")
    print(f"pip-show recurrence:             {len(pip_show_recurrences)} (target=0)")
    print(f"debug_repair total:              {debug_repair_total}")
    print(f"backend_capacity recurrence:     {len(env_cap_failures)} (target=0)")
    print(f"new failure codes:               {len(all_new_codes)}")
    if all_new_codes:
        print(f"  codes: {all_new_codes[:5]}")

    print("\nPer-task summary:")
    for r2 in all_results:
        flags = []
        if r2["verification_mutates_source_assets"]:
            flags.append("VMA")
        if r2.get("pip_show_failure_detected"):
            flags.append("pip_show_RECURRED")
        if r2["advisory_event_count"] > 0:
            flags.append(f"ADVISORY:{r2['advisory_event_count']}")
        if r2.get("blocked_prior_task_failed"):
            flags.append("blocked")
        print(
            f"  {r2['project']} T{r2['plan_position']} [{r2['status']}] "
            f"nested={r2['nested_project_folder_command_count']} "
            f"advisory={r2['advisory_event_count']} "
            f"debug={r2['debug_repair_count']} "
            f"plan_repair={r2.get('planning_repair_count', 0)} "
            f"{' '.join(flags)}"
        )

    print("\n" + "-" * 60)
    print("SUCCESS CRITERIA EVALUATION")
    print("-" * 60)
    sc = {
        "T1 success >= 2/3": len(t1_done) >= 2,
        "T6 success >= 2/3": len(t6_done) >= 2,
        "vma occurrence = 0": len(vma_tasks) == 0,
        "advisory events = 0": advisory_total == 0,
        "nested_project_folder_command = 0": nested_total == 0,
        "pip-show recurrence = 0": len(pip_show_recurrences) == 0,
        "backend capacity recurrence = 0": len(env_cap_failures) == 0,
        "no new failure class": len(all_new_codes) == 0,
    }
    for label, ok in sc.items():
        print(f"  {'OK' if ok else 'FAIL'} {label}")

    print(f"\nRaw output: {out_path}")
