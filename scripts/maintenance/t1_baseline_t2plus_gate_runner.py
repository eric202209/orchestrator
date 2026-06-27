#!/usr/bin/env python3
"""
WM baseline T2+ corpus gate on the restored baseline lane (post T1 fixes).

Corpus and mechanics: identical to the Step 2 confirmation runner
(t1_reliability_confirmation_runner.py, imported unchanged), with fresh
workspaces t1-baseline-gate-{calclib,pathtools,strtools}.

Metrics mirror the historical WM OFF runner eligibility rules:
  - eligible T2+ = plan_position > 1, terminal done/failed, execution_reached
  - debug_repair_rate_baseline = eligible tasks with >=1 debug repair / eligible
  - constraint rediscovery = eligible task whose repair classes/reasons match
    pythonpath/importerror/modulenotfound/venv/import/python keywords
Gate: T1 >= 2/3 AND eligible >= 10 AND debug_repair_rate >= 10%.

Baseline lane asserts: PLANNING_BACKEND unset, repair on ai-gateway/qwen-local,
DEBUG_REPAIR_* unset (documented fallback to the same baseline lane),
EXECUTION_BACKEND unset. Option A must remain rolled back.
"""
import copy
import json
import pathlib
import sqlite3
from datetime import datetime

from scripts.maintenance._runner_common import ensure_repo_on_syspath, load_sibling_module

ensure_repo_on_syspath()
r = load_sibling_module("t1runner", "t1_reliability_confirmation_runner.py")

PYTHONPATH_KEYWORDS = [
    "pythonpath", "importerror", "modulenotfound", "venv", "import", "python",
]


def assert_baseline_lane() -> None:
    s = r.settings
    assert s.PLANNING_BACKEND is None, \
        f"PLANNING_BACKEND={s.PLANNING_BACKEND!r} — Option A must stay rolled back"
    assert s.PLANNING_REPAIR_BASE_URL.rstrip("/") == "http://ai-gateway:8000/v1", \
        f"PLANNING_REPAIR_BASE_URL={s.PLANNING_REPAIR_BASE_URL!r}"
    assert s.PLANNING_REPAIR_MODEL == "qwen-local", \
        f"PLANNING_REPAIR_MODEL={s.PLANNING_REPAIR_MODEL!r}"
    assert not s.DEBUG_REPAIR_BASE_URL and not s.DEBUG_REPAIR_MODEL, \
        "DEBUG_REPAIR_* must be unset (documented fallback to baseline lane)"
    assert s.EXECUTION_BACKEND is None, \
        f"EXECUTION_BACKEND={s.EXECUTION_BACKEND!r}"
    print("✓ Baseline lane asserts passed: local_openclaw + ai-gateway/qwen-local; "
          "debug repair falls back to the same baseline lane")


def is_pythonpath_repair(failure_classes: list, planning_reasons: list) -> bool:
    for fc in failure_classes:
        if any(k in str(fc).lower() for k in PYTHONPATH_KEYWORDS):
            return True
    for reasons in planning_reasons:
        if any(k in str(reasons).lower() for k in PYTHONPATH_KEYWORDS):
            return True
    return False


def count_debug_parse_errors(task_id: int) -> int:
    try:
        conn = sqlite3.connect(str(r.DB_PATH))
        cur = conn.cursor()
        cur.execute(
            "SELECT COUNT(*) FROM log_entries "
            "WHERE task_id=? AND message LIKE '%debug parse error%'",
            (task_id,),
        )
        n = cur.fetchone()[0]
        conn.close()
        return int(n)
    except Exception:
        return 0


def gate_projects() -> list:
    projects = copy.deepcopy(r.PROJECTS)
    for p in projects:
        p["name"] = p["name"].replace("t1-confirm-", "t1-baseline-gate-")
        p["workspace"] = p["workspace"].replace("t1-confirm-", "t1-baseline-gate-")
        p["description"] = p["description"].replace(
            "T1 reliability confirmation — venv pip show fix verification",
            "WM baseline T2+ corpus gate on restored baseline lane",
        )
    return projects


if __name__ == "__main__":
    r._init_runtime()
    assert_baseline_lane()

    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    all_results = []
    run_meta = {
        "runner": "t1_baseline_t2plus_gate",
        "run_ts": run_ts,
        "planning_backend": str(r.settings.PLANNING_BACKEND),
        "planning_repair_base_url": str(r.settings.PLANNING_REPAIR_BASE_URL),
        "planning_repair_model": str(r.settings.PLANNING_REPAIR_MODEL),
        "runner_errors": 0,
    }

    for proj_spec in gate_projects():
        print(f"\n{'='*60}")
        print(f"PROJECT: {proj_spec['name']}")
        print(f"{'='*60}")

        print(f"  [slot] Checking before {proj_spec['name']}...")
        r.wait_for_slot_clear()
        print("  [slot] Slot clear.")

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

        proj_results = r.monitor_project(proj_spec, task_ids)
        all_results.extend(proj_results)

    # ── Post-collection enrichment ────────────────────────────────────────────
    for x in all_results:
        x["pythonpath_constraint_repair"] = is_pythonpath_repair(
            x.get("debug_repair_classes", []),
            x.get("planning_repair_reasons", []),
        )
        x["debug_parse_error_count"] = count_debug_parse_errors(x["task_id"])

    # ── Save raw results ──────────────────────────────────────────────────────
    out_dir = pathlib.Path(
        "docs/roadmap/reports/maintenance"
        "/project_aware_continuation_execution"
        "/slices_C_working_memory_persistence"
    )
    out_path = out_dir / f"t1-baseline-gate-raw-{run_ts}.json"
    out_path.write_text(json.dumps({"meta": run_meta, "results": all_results}, indent=2))
    print(f"\n\nRaw results saved: {out_path}")

    # ── Gate metrics (WM OFF runner definitions) ──────────────────────────────
    print("\n" + "=" * 60)
    print("WM BASELINE T2+ CORPUS GATE SUMMARY")
    print("=" * 60)

    t1_results = [x for x in all_results if x["plan_position"] == 1]
    t1_done = [x for x in t1_results if x["status"] == "done"]
    pip_show_recurrences = [x for x in t1_results if x["pip_show_failure_detected"]]
    env_cap_failures = [x for x in all_results if x["env_capacity_failure"]]

    t2plus_eligible = [
        x for x in all_results
        if x["plan_position"] > 1
        and x["status"] in ("done", "failed")
        and x["execution_reached"]
    ]
    qualifying_repairs = [
        x for x in t2plus_eligible if x["debug_repair_count"] > 0
    ]
    constraint_rediscoveries = [
        x for x in t2plus_eligible if x["pythonpath_constraint_repair"]
    ]
    debug_repair_rate = (
        len(qualifying_repairs) / len(t2plus_eligible) if t2plus_eligible else 0.0
    )
    plan_exhaustion = [
        x for x in all_results
        if x["status"] == "failed"
        and not x["execution_reached"]
        and x["planning_repair_count"] > 0
    ]
    debug_parse_errors = sum(x["debug_parse_error_count"] for x in all_results)
    blocked = [x for x in all_results if x["status"] == "blocked_prior_task_failed"]

    print(f"\nT1 success:                  {len(t1_done)}/{len(t1_results)}")
    print(f"T2+ eligible:                {len(t2plus_eligible)} (gate: >=10)")
    print(f"Tasks with debug repairs:    {len(qualifying_repairs)}")
    print(f"debug_repair_rate_baseline:  {debug_repair_rate:.1%} (gate: >=10%)")
    print(f"Constraint rediscoveries:    {len(constraint_rediscoveries)}")
    print(f"Planning repair exhaustion:  {len(plan_exhaustion)}")
    print(f"Debug parse errors:          {debug_parse_errors}")
    print(f"pip show recurrence:         {len(pip_show_recurrences)} (must be 0)")
    print(f"env capacity failures:       {len(env_cap_failures)}")
    print(f"blocked_prior_task_failed:   {len(blocked)}")
    print(f"runner errors:               {run_meta['runner_errors']}")

    gate_pass = (
        len(t1_done) >= 2
        and len(t2plus_eligible) >= 10
        and debug_repair_rate >= 0.10
    )
    print(f"\nGATE: {'PASS' if gate_pass else 'FAIL'} "
          f"(T1>=2/3: {len(t1_done) >= 2}, eligible>=10: {len(t2plus_eligible) >= 10}, "
          f"rate>=10%: {debug_repair_rate >= 0.10})")
    print(f"WM ON arm approved next (do not run now): {'YES' if gate_pass else 'NO'}")

    print("\nPer-task detail:")
    for x in all_results:
        flags = []
        if x["pythonpath_constraint_repair"]:
            flags.append("constraint-rediscovery")
        if x["debug_parse_error_count"]:
            flags.append(f"debug_parse_errors={x['debug_parse_error_count']}")
        print(
            f"  {x['project']} T{x['plan_position']} [{x['status']}] "
            f"plan={x['planning_repair_count']} debug={x['debug_repair_count']}"
            f"{' ' + ' '.join(flags) if flags else ''}"
        )
    print(f"\nRaw results: {out_path}")
