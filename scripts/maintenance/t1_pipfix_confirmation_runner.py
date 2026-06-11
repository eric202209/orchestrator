#!/usr/bin/env python3
"""
Pathtools pip-show Recurrence Fix — Confirmation Run + Path Guard Phase 1 Run 2/3.

Purpose: confirm `_strip_orchestrator_pip_shadow` (no-venv branch) together with
`_inject_project_venv_path` (venv branch) eliminates the pip-show failure class,
and collect path guard Phase 1 advisory telemetry run 2 of 3.

Same 6-task T1-corpus as prior runs.
Fresh workspaces: t1-pipfix-{calclib,pathtools,strtools}.
Baseline lane: PLANNING_BACKEND=None -> local_openclaw, qwen-local repair.
No WM, no lane swap, no validator changes. Validation only.

Primary metrics:
- pathtools T1 DONE with 0 pip-show recurrence
- nested_project_folder_created_advisory count = 0 expected
- nested_project_folder_command count = 0 expected
"""
import copy
import importlib.util
import json
import pathlib
import sys
from datetime import datetime

sys.path.insert(0, "/root/.openclaw/workspace/vault/projects/orchestrator")

_PATHGUARD = pathlib.Path(__file__).parent / "t1_pathguard_telemetry_runner.py"
spec = importlib.util.spec_from_file_location("pathguard", str(_PATHGUARD))
pg = importlib.util.module_from_spec(spec)
spec.loader.exec_module(pg)

r = pg.r


def pipfix_projects() -> list:
    projects = copy.deepcopy(r.PROJECTS)
    for p in projects:
        p["name"] = p["name"].replace("t1-confirm-", "t1-pipfix-")
        p["workspace"] = p["workspace"].replace("t1-confirm-", "t1-pipfix-")
        p["description"] = p["description"].replace(
            "T1 reliability confirmation — venv pip show fix verification",
            "pip-show no-venv fix confirmation + path guard Phase 1 run 2",
        )
    return projects


if __name__ == "__main__":
    r._init_runtime()
    pg.assert_baseline_lane_and_flags()

    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    all_results = []
    run_meta = {
        "runner": "t1_pipfix_confirmation",
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
    }

    for proj_spec in pipfix_projects():
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
    out_path = out_dir / f"t1-pipfix-raw-{run_ts}.json"
    out_path.write_text(json.dumps({"meta": run_meta, "results": all_results}, indent=2))
    print(f"\n\nRaw results saved: {out_path}")

    print("\n" + "=" * 60)
    print("PIPFIX CONFIRMATION + PATH GUARD RUN 2 SUMMARY")
    print("=" * 60)

    done_count = sum(1 for r2 in all_results if r2["status"] == "done")
    failed_count = sum(1 for r2 in all_results if r2["status"] == "failed")
    blocked_count = sum(1 for r2 in all_results if r2.get("blocked_prior_task_failed"))
    corpus_total = len(all_results)
    corpus_completed = done_count + failed_count + blocked_count

    t1_results = [r2 for r2 in all_results if r2["plan_position"] == 1]
    t1_done = [r2 for r2 in t1_results if r2["status"] == "done"]
    pathtools_t1 = [
        r2 for r2 in t1_results if "pathtools" in r2["project"]
    ]

    advisory_total = sum(r2["advisory_event_count"] for r2 in all_results)
    advisory_tasks = [r2 for r2 in all_results if r2["advisory_event_count"] > 0]
    nested_total = sum(r2["nested_project_folder_command_count"] for r2 in all_results)
    vma_tasks = [r2 for r2 in all_results if r2["verification_mutates_source_assets"]]
    debug_repair_total = sum(r2["debug_repair_count"] for r2 in all_results)
    pip_show_recurrences = [r2 for r2 in all_results if r2.get("pip_show_failure_detected")]
    env_cap_failures = [r2 for r2 in all_results if r2["env_capacity_failure"]]
    all_new_codes = []
    for r2 in all_results:
        all_new_codes.extend(r2.get("new_failure_codes", []))
    t2plus_eligible = [
        r2 for r2 in all_results
        if r2["plan_position"] >= 2 and r2["execution_reached"]
    ]

    print(f"\nTotal tasks:                     {corpus_total}")
    print(f"DONE:                            {done_count}")
    print(f"FAILED:                          {failed_count}")
    print(f"Blocked (prior task failed):     {blocked_count}")
    print(f"Corpus completion:               {corpus_completed}/{corpus_total}")
    print(f"T1 success (done):               {len(t1_done)}/{len(t1_results)}")
    print(f"pathtools T1:                    "
          f"{pathtools_t1[0]['status'] if pathtools_t1 else 'NOT RUN'}")
    print(f"T2+ exec reached (eligible):     {len(t2plus_eligible)}")
    print(f"pip-show recurrence:             {len(pip_show_recurrences)} (target=0)")
    print(f"advisory_event count:            {advisory_total} (target=0)")
    if advisory_tasks:
        for r2 in advisory_tasks:
            print(f"  [ADVISORY HIT] T{r2['plan_position']} {r2['project']} "
                  f"task_id={r2['task_id']} count={r2['advisory_event_count']}")
    print(f"nested_project_folder_command:   {nested_total} (target=0)")
    print(f"verification_mutates_source:     {len(vma_tasks)}")
    print(f"debug_repair total:              {debug_repair_total}")
    print(f"backend_capacity recurrence:     {len(env_cap_failures)} (target=0)")
    print(f"new failure codes:               {len(all_new_codes)}")
    if all_new_codes:
        print(f"  codes: {all_new_codes[:5]}")

    print("\nPer-task summary:")
    for r2 in all_results:
        flags = []
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
            f"{' '.join(flags)}"
        )

    print("\n" + "-" * 60)
    print("SUCCESS CRITERIA EVALUATION")
    print("-" * 60)
    sc = {
        "pathtools T1 DONE": bool(pathtools_t1) and pathtools_t1[0]["status"] == "done",
        "pip-show recurrence = 0": len(pip_show_recurrences) == 0,
        "advisory events = 0": advisory_total == 0,
        "nested_project_folder_command = 0": nested_total == 0,
        "T1 success >= 2/3": len(t1_done) >= 2,
        "backend capacity recurrence = 0": len(env_cap_failures) == 0,
        "no new failure class": len(all_new_codes) == 0,
    }
    for label, ok in sc.items():
        print(f"{'✓' if ok else '✗'} {label}")
    print(f"\nRESULT: {'CLEAN RUN' if all(sc.values()) else 'SEE FINDINGS'}")
    print(f"Raw results:   {out_path}")
    print(f"Run timestamp: {run_ts}")
