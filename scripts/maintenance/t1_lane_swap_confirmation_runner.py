#!/usr/bin/env python3
"""
T1 Item 2 — post-lane-swap confirmation runner.

Same corpus and method as the Step 2 confirmation
(t1_reliability_confirmation_runner.py, imported unchanged), with:
  - fresh workspaces: t1-lane-calclib / t1-lane-pathtools / t1-lane-strtools
  - additional lane asserts: PLANNING_BACKEND=direct_ollama,
    PLANNING_REPAIR_BASE_URL -> Ollama /v1, PLANNING_REPAIR_MODEL=qwen3-coder:30b,
    EXECUTION_BACKEND unset (local_openclaw)
  - RAM stability guard (operator instruction): sample /proc/meminfo before
    each project; if MemAvailable < 3 GiB, stop dispatching further projects
    and mark the run aborted_for_stability.

Deployment-specific: this configuration is valid only for the current
local_high_capacity-style deployment (see lane profile addendum). Not a
universal default.
"""
import copy
import json
import pathlib
from datetime import datetime

from scripts.maintenance._runner_common import ensure_repo_on_syspath, load_sibling_module

ensure_repo_on_syspath()
r = load_sibling_module("t1runner", "t1_reliability_confirmation_runner.py")

MIN_AVAILABLE_GIB = 3.0


def mem_snapshot() -> dict:
    info = {}
    with open("/proc/meminfo") as fh:
        for line in fh:
            key, _, rest = line.partition(":")
            info[key.strip()] = int(rest.strip().split()[0])  # kB
    total = info.get("MemTotal", 0) / 1024 / 1024
    avail = info.get("MemAvailable", 0) / 1024 / 1024
    return {"total_gib": round(total, 1), "available_gib": round(avail, 1),
            "used_gib": round(total - avail, 1)}


def assert_lane() -> None:
    s = r.settings
    assert s.PLANNING_BACKEND == "direct_ollama", \
        f"PLANNING_BACKEND={s.PLANNING_BACKEND!r}, expected 'direct_ollama'"
    assert s.PLANNING_REPAIR_BASE_URL.rstrip("/") == \
        "http://host.docker.internal:11434/v1", \
        f"PLANNING_REPAIR_BASE_URL={s.PLANNING_REPAIR_BASE_URL!r}"
    assert s.PLANNING_REPAIR_MODEL == "qwen3-coder:30b", \
        f"PLANNING_REPAIR_MODEL={s.PLANNING_REPAIR_MODEL!r}"
    assert not s.PLANNING_REPAIR_API_KEY, "PLANNING_REPAIR_API_KEY must be empty"
    assert s.EXECUTION_BACKEND is None, \
        f"EXECUTION_BACKEND={s.EXECUTION_BACKEND!r}, expected None (local)"
    print("✓ Lane asserts passed: direct_ollama + qwen3-coder:30b, execution local")


def lane_projects() -> list:
    projects = copy.deepcopy(r.PROJECTS)
    for p in projects:
        p["name"] = p["name"].replace("t1-confirm-", "t1-lane-")
        p["workspace"] = p["workspace"].replace("t1-confirm-", "t1-lane-")
        p["description"] = p["description"].replace(
            "T1 reliability confirmation — venv pip show fix verification",
            "T1 Item 2 lane-swap confirmation — direct_ollama planning/repair",
        )
    return projects


if __name__ == "__main__":
    r._init_runtime()
    assert_lane()

    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    all_results = []
    run_meta = {
        "runner": "t1_lane_swap_confirmation",
        "run_ts": run_ts,
        "planning_backend": str(r.settings.PLANNING_BACKEND),
        "planning_repair_base_url": str(r.settings.PLANNING_REPAIR_BASE_URL),
        "planning_repair_model": str(r.settings.PLANNING_REPAIR_MODEL),
        "execution_backend": str(r.settings.EXECUTION_BACKEND),
        "runner_errors": 0,
        "aborted_for_stability": False,
        "mem_samples": [],
    }

    for proj_spec in lane_projects():
        mem = mem_snapshot()
        run_meta["mem_samples"].append({"before": proj_spec["name"], **mem})
        print(f"\n[MEM] before {proj_spec['name']}: used={mem['used_gib']}GiB "
              f"avail={mem['available_gib']}GiB / {mem['total_gib']}GiB")
        if mem["available_gib"] < MIN_AVAILABLE_GIB:
            print(f"[MEM] ABORT: available {mem['available_gib']}GiB < "
                  f"{MIN_AVAILABLE_GIB}GiB — stopping for machine stability")
            run_meta["aborted_for_stability"] = True
            break

        print(f"{'='*60}")
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

        mem = mem_snapshot()
        run_meta["mem_samples"].append({"after": proj_spec["name"], **mem})
        print(f"[MEM] after {proj_spec['name']}: used={mem['used_gib']}GiB "
              f"avail={mem['available_gib']}GiB")

    # ── Save raw results ──────────────────────────────────────────────────────
    out_dir = pathlib.Path(
        "docs/roadmap/reports/maintenance"
        "/project_aware_continuation_execution"
        "/slices_C_working_memory_persistence"
    )
    out_path = out_dir / f"t1-lane-confirm-raw-{run_ts}.json"
    out_path.write_text(json.dumps({"meta": run_meta, "results": all_results}, indent=2))
    print(f"\n\nRaw results saved: {out_path}")

    # ── Summary ───────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("T1 LANE-SWAP CONFIRMATION SUMMARY")
    print("=" * 60)

    t1_results = [x for x in all_results if x["plan_position"] == 1]
    t1_done = [x for x in t1_results if x["status"] == "done"]
    pip_show_recurrences = [x for x in t1_results if x["pip_show_failure_detected"]]
    env_cap_failures = [x for x in all_results if x["env_capacity_failure"]]

    t2plus_results = [x for x in all_results if x["plan_position"] > 1]
    t2plus_eligible = [
        x for x in t2plus_results
        if x["status"] in ("done", "failed")
        and x["execution_reached"]
        and not x["env_capacity_failure"]
    ]
    t5_plan_exhaustion = [
        x for x in all_results
        if x["plan_position"] == 5
        and x["status"] == "failed"
        and x["planning_repair_count"] > 0
        and not x["execution_reached"]
    ]
    plan_failures_all = [
        x for x in all_results
        if x["status"] == "failed" and not x["execution_reached"]
    ]

    print(f"\nT1 success (done):           {len(t1_done)}/{len(t1_results)}")
    print(f"pip show recurrence:         {len(pip_show_recurrences)} (must be 0)")
    print(f"env capacity failures:       {len(env_cap_failures)}")
    print(f"T2+ eligible:                {len(t2plus_eligible)} (baseline 8)")
    print(f"T5 planning repair exhaust.: {len(t5_plan_exhaustion)} (baseline 2)")
    print(f"planning-stage failures:     {len(plan_failures_all)}")
    print(f"runner errors:               {run_meta['runner_errors']}")
    print(f"aborted for stability:       {run_meta['aborted_for_stability']}")

    print("\nPer-task detail:")
    for x in all_results:
        cvf = x.get("completion_validation_failures", [])
        cvf_str = f" cvf=[{', '.join(f['failed_command'][:40] for f in cvf)}]" if cvf else ""
        print(
            f"  {x['project']} T{x['plan_position']} [{x['status']}] "
            f"plan={x['planning_repair_count']} debug={x['debug_repair_count']}"
            f"{cvf_str}"
        )
    print(f"\nRaw results: {out_path}")
