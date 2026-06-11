#!/usr/bin/env python3
"""
Resume runner for nested-project-root validator fix confirmation.

t1-nestedfix-calclib was already created (project_id=594, tasks 761-766, T1 running).
This runner:
  1. Monitors calclib tasks until terminal.
  2. Creates t1-nestedfix-pathtools and t1-nestedfix-strtools fresh.
  3. Collects all results and produces the confirmation summary.
"""
import copy
import importlib.util
import json
import pathlib
import sys

sys.path.insert(0, "/root/.openclaw/workspace/vault/projects/orchestrator")

_RUNNER = pathlib.Path(__file__).parent / "t1_nestedfix_confirmation_runner.py"
spec = importlib.util.spec_from_file_location("nestedfix", str(_RUNNER))
nf = importlib.util.module_from_spec(spec)
spec.loader.exec_module(nf)

# Re-export base runner as r for convenience
r = nf.r

if __name__ == "__main__":
    import time
    from datetime import datetime

    r._init_runtime()
    nf.assert_baseline_lane()

    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    all_results = []
    run_meta = {
        "runner": "t1_nestedfix_confirmation_resume",
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
        "resume": True,
        "calclib_project_id": 594,
        "calclib_task_ids": [761, 762, 763, 764, 765, 766],
    }

    # ── Project 1: calclib (already created, T1 running) ──────────────────────
    calclib_spec = nf.nestedfix_projects()[0]
    assert calclib_spec["name"] == "t1-nestedfix-calclib"

    print(f"\n{'='*60}")
    print(f"PROJECT: {calclib_spec['name']} (RESUMING — project_id=594)")
    print(f"{'='*60}")
    print("  T1 already dispatched; monitoring T1-T6...")

    calclib_task_ids = run_meta["calclib_task_ids"]
    calclib_results = nf.monitor_project_extended(
        calclib_spec, calclib_task_ids
    )
    all_results.extend(calclib_results)

    # ── Project 2+: pathtools and strtools ────────────────────────────────────
    for proj_spec in nf.nestedfix_projects()[1:]:
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

        proj_results = nf.monitor_project_extended(proj_spec, task_ids)
        all_results.extend(proj_results)

    # ── Save raw results ──────────────────────────────────────────────────────
    out_dir = pathlib.Path(
        "docs/roadmap/reports/maintenance"
        "/project_aware_continuation_execution"
        "/slices_C_working_memory_persistence"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"t1-nestedfix-raw-{run_ts}.json"
    out_path.write_text(json.dumps({"meta": run_meta, "results": all_results}, indent=2))
    print(f"\n\nRaw results saved: {out_path}")

    # ── Aggregate metrics ─────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("NESTED-PROJECT-ROOT FIX CONFIRMATION SUMMARY")
    print("=" * 60)

    t1_results = [r2 for r2 in all_results if r2["plan_position"] == 1]
    t1_done = [r2 for r2 in t1_results if r2["status"] == "done"]

    t2plus_results = [r2 for r2 in all_results if r2["plan_position"] > 1]
    t2plus_eligible = [
        r2 for r2 in t2plus_results
        if r2["status"] in ("done", "failed")
        and r2["execution_reached"]
        and not r2["env_capacity_failure"]
    ]

    done_count = sum(1 for r2 in all_results if r2["status"] == "done")
    failed_count = sum(1 for r2 in all_results if r2["status"] == "failed")
    blocked_count = sum(1 for r2 in all_results if r2.get("blocked_prior_task_failed"))

    nested_total = sum(r2["nested_project_folder_command_count"] for r2 in all_results)
    nested_tasks = [r2 for r2 in all_results if r2["nested_project_folder_command_count"] > 0]

    vma_tasks = [r2 for r2 in all_results if r2["verification_mutates_source_assets"]]
    plan_exhaust_tasks = [
        r2 for r2 in all_results
        if r2["status"] == "failed"
        and not r2["execution_reached"]
        and r2["planning_repair_count"] > 0
    ]
    constraint_rediscov_tasks = [r2 for r2 in all_results if r2["constraint_rediscovery"]]
    exec_timeout_tasks = [r2 for r2 in all_results if r2["execution_timeout"]]
    debug_repair_total = sum(r2["debug_repair_count"] for r2 in all_results)
    debug_repair_tasks = [r2 for r2 in all_results if r2["debug_repair_count"] > 0]
    pip_show_recurrences = [r2 for r2 in all_results if r2.get("pip_show_failure_detected")]
    env_cap_failures = [r2 for r2 in all_results if r2["env_capacity_failure"]]
    all_new_codes = []
    for r2 in all_results:
        all_new_codes.extend(r2.get("new_failure_codes", []))

    t5_results = [r2 for r2 in all_results if r2["plan_position"] == 5]
    t6_results = [r2 for r2 in all_results if r2["plan_position"] == 6]
    t5_exec_reached = [r2 for r2 in t5_results if r2["execution_reached"]]
    t6_exec_reached = [r2 for r2 in t6_results if r2["execution_reached"]]

    corpus_completed = done_count + failed_count + blocked_count
    corpus_total = len(all_results)

    print(f"\nProjects run:                    3")
    print(f"Total tasks:                     {corpus_total}")
    print(f"DONE:                            {done_count}")
    print(f"FAILED:                          {failed_count}")
    print(f"Blocked (prior task failed):     {blocked_count}")
    print(f"Corpus completion:               {corpus_completed}/{corpus_total}")
    print(f"T1 success (done):               {len(t1_done)}/{len(t1_results)}")
    print(f"T2+ eligible:                    {len(t2plus_eligible)}")
    print(f"nested_project_folder_command:   {nested_total} (target=0)")
    print(f"verification_mutates_source:     {len(vma_tasks)}")
    print(f"planning_repair_exhaustion:      {len(plan_exhaust_tasks)}")
    print(f"constraint_rediscovery:          {len(constraint_rediscov_tasks)}")
    print(f"execution_timeout:               {len(exec_timeout_tasks)}")
    print(f"debug_repair total:              {debug_repair_total} "
          f"across {len(debug_repair_tasks)} tasks")
    print(f"pip-show recurrence:             {len(pip_show_recurrences)} (target=0)")
    print(f"backend_capacity recurrence:     {len(env_cap_failures)} (target=0)")
    print(f"new failure codes introduced:    {len(all_new_codes)}")
    if all_new_codes:
        print(f"  codes: {all_new_codes[:5]}")
    print(f"\nT5 execution reached:            {len(t5_exec_reached)}/3")
    print(f"T6 execution reached:            {len(t6_exec_reached)}/3")

    print("\nT1 detail:")
    for row in t1_results:
        print(
            f"  {row['project']} T1 [{row['status']}] "
            f"nested={row['nested_project_folder_command_count']} "
            f"plan_repairs={row['planning_repair_count']} "
            f"debug_repairs={row['debug_repair_count']}"
        )

    print("\nPer-task summary:")
    for row in all_results:
        pos = row["plan_position"]
        status = row["status"]
        nested = row["nested_project_folder_command_count"]
        vma = "vma" if row["verification_mutates_source_assets"] else ""
        blocked = "[blocked]" if row.get("blocked_prior_task_failed") else ""
        new_codes = f"[new:{row['new_failure_codes']}]" if row["new_failure_codes"] else ""
        print(
            f"  {row['project']} T{pos} [{status}] "
            f"nested={nested} {vma} {blocked} {new_codes}"
        )

    print("\n" + "-" * 60)
    print("SUCCESS CRITERIA EVALUATION")
    print("-" * 60)

    sc_nested_zero = nested_total == 0
    sc_t1_success = len(t1_done) >= 2
    sc_t5_exec = len(t5_exec_reached) >= 2
    sc_t6_exec = len(t6_exec_reached) >= 2
    sc_no_new_class = len(all_new_codes) == 0
    sc_completion = corpus_completed >= 16
    sc_pip_show = len(pip_show_recurrences) == 0
    sc_backend_cap = len(env_cap_failures) == 0

    print(f"✓/✗ 0 nested_project_folder_command: {'✓' if sc_nested_zero else '✗'} ({nested_total})")
    print(f"✓/✗ T1 success >= 2/3:              {'✓' if sc_t1_success else '✗'} ({len(t1_done)}/3)")
    print(f"✓/✗ T5 exec reached >= 2/3:         {'✓' if sc_t5_exec else '✗'} ({len(t5_exec_reached)}/3)")
    print(f"✓/✗ T6 exec reached >= 2/3:         {'✓' if sc_t6_exec else '✗'} ({len(t6_exec_reached)}/3)")
    print(f"✓/✗ No new failure class:           {'✓' if sc_no_new_class else '✗'} ({len(all_new_codes)} new)")
    print(f"✓/✗ Corpus completion >= 16/18:     {'✓' if sc_completion else '✗'} ({corpus_completed}/{corpus_total})")
    print(f"✓/✗ pip-show recurrence = 0:        {'✓' if sc_pip_show else '✗'} ({len(pip_show_recurrences)})")
    print(f"✓/✗ Backend capacity recurrence=0:  {'✓' if sc_backend_cap else '✗'} ({len(env_cap_failures)})")

    all_pass = all([
        sc_nested_zero, sc_t1_success, sc_t5_exec,
        sc_no_new_class, sc_completion, sc_pip_show, sc_backend_cap,
    ])
    print(f"\nFIX CONFIRMED: {'YES' if all_pass else 'NO — see failure analysis above'}")
    print(f"Raw results:   {out_path}")
    print(f"Run timestamp: {run_ts}")
