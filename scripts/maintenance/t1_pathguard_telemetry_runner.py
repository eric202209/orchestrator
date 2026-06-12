#!/usr/bin/env python3
"""
Path Guard Phase 1 Advisory Telemetry — T1 Corpus Run.

Purpose: first telemetry run for execution-time path guard Phase 1.
Measures whether the advisory guard produces any events, and whether
any events are false positives (advisory fires at mature-workspace T5/T6
positions or for in-place work on existing package dirs).

Same 6-task T1-corpus as prior runs.
Fresh workspaces: t1-pathguard-{calclib,pathtools,strtools}.
Baseline lane: PLANNING_BACKEND=None -> local_openclaw, qwen-local repair.
No WM, no lane swap, no validator changes.

Primary metric: nested_project_folder_created_advisory count = 0 expected.
"""
import copy
import json
import pathlib
import sqlite3
import sys
from datetime import datetime

from scripts.maintenance._runner_common import ensure_repo_on_syspath, load_sibling_module

ensure_repo_on_syspath()
nf = load_sibling_module("nestedfix", "t1_nestedfix_confirmation_runner.py")

r = nf.r
DB_PATH = r.DB_PATH


# ── Advisory event detection ─────────────────────────────────────────────────

def detect_advisory_nested_scaffold_events(task_id: int) -> list[dict]:
    """
    Return all PATH_GUARD advisory events emitted for this task.
    Looks for log_entries with message containing '[PATH_GUARD]' AND
    contract_violation_type='nested_project_folder_created_advisory'.
    """
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.cursor()
        cur.execute(
            "SELECT message, log_metadata FROM log_entries "
            "WHERE task_id=? AND message LIKE '%PATH_GUARD%' "
            "ORDER BY id",
            (task_id,),
        )
        rows = cur.fetchall()
        conn.close()
        results = []
        for (msg, meta_str) in rows:
            if not meta_str:
                # Plain text PATH_GUARD log line (logger.warning, no metadata)
                results.append({"message": msg, "metadata": None, "source": "log_message"})
                continue
            try:
                meta = json.loads(meta_str)
            except Exception:
                results.append({"message": msg, "metadata": meta_str, "source": "log_message"})
                continue
            cvt = str(meta.get("contract_violation_type", "")).lower()
            if "nested_project_folder_created_advisory" in cvt or "path_guard" in msg.lower():
                results.append({
                    "message": msg[:120],
                    "metadata": meta,
                    "source": "emit_live",
                    "step_index": meta.get("step_index"),
                    "new_top_dir": meta.get("new_top_dir"),
                    "files_written": meta.get("files_written", [])[:6],
                    "mode": meta.get("mode"),
                    "contract_violation_type": meta.get("contract_violation_type"),
                })
        return results
    except Exception as e:
        return [{"error": str(e)}]


# ── Extended collect_task_data with advisory events ──────────────────────────

KNOWN_FAILURE_CODES = nf.KNOWN_FAILURE_CODES


def collect_task_data_pathguard(
    proj_name: str,
    workspace: str,
    pos: int,
    task_id: int,
    title: str,
    final_status: str,
    extra: dict,
) -> dict:
    # Call nestedfix extended collector (includes nested_project_folder_command, vma, etc.)
    base = nf.collect_task_data_extended(
        proj_name, workspace, pos, task_id, title, final_status, extra
    )
    # Add advisory events
    advisory_events = detect_advisory_nested_scaffold_events(task_id)
    base["advisory_event_count"] = len(advisory_events)
    base["advisory_events"] = advisory_events
    return base


# ── Patched monitor_project with advisory reporting ──────────────────────────

def monitor_project_pathguard(proj_spec: dict, task_ids: list[int]) -> list[dict]:
    """Run monitoring loop and collect per-task results with advisory event data."""
    import time

    workspace = proj_spec["workspace"]
    proj_name = proj_spec["name"]

    state = {tid: {
        "prior_done_since": None,
        "prior_blocked_since": None,
        "stall_retry_attempted": False,
        "already_running_monitor_only": False,
        "auto_advance_stalled": False,
        "blocked_prior_task_failed": False,
        "runner_timeout": False,
    } for tid in task_ids}

    proj_start = time.time()
    last_print: dict[int, str] = {}

    STALL_TIMEOUT = r.STALL_TIMEOUT
    PROJECT_TIMEOUT = r.PROJECT_TIMEOUT
    POLL_INTERVAL = r.POLL_INTERVAL

    def project_complete(statuses: dict[int, str]) -> bool:
        for tid in task_ids:
            if statuses[tid] in r.TERMINAL_TASK:
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
        statuses = r.db_all_statuses(task_ids)

        for pos, tid in enumerate(task_ids, start=1):
            status = statuses[tid]
            s = state[tid]

            if status in r.TERMINAL_TASK or s["blocked_prior_task_failed"]:
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
                        ok, err = r.dispatch_task(tid)
                        s["stall_retry_attempted"] = True
                        if not ok:
                            if r.is_already_running_error(err):
                                s["already_running_monitor_only"] = True
                                print(f"    T{pos} id={tid} already running — monitor only")
                            else:
                                s["auto_advance_stalled"] = True
                                print(f"    T{pos} id={tid} stall dispatch failed: {err[:80]}")
                        else:
                            s["auto_advance_stalled"] = True
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
        statuses = r.db_all_statuses(task_ids)
        for tid in task_ids:
            if statuses[tid] not in r.TERMINAL_TASK and not state[tid]["blocked_prior_task_failed"]:
                state[tid]["runner_timeout"] = True
        print(f"  [WARNING] Project monitoring timed out after {PROJECT_TIMEOUT}s")

    statuses = r.db_all_statuses(task_ids)
    results = []
    for pos, (tid, title) in enumerate(
        zip(task_ids, [t["title"] for t in proj_spec["tasks"]]), start=1
    ):
        s = state[tid]
        db_status = statuses[tid]

        if s["blocked_prior_task_failed"]:
            final_status = "blocked_prior_task_failed"
        elif s["runner_timeout"] and db_status not in r.TERMINAL_TASK:
            final_status = f"runner_timeout__{db_status}"
        else:
            final_status = db_status

        extra = {
            "stall_retry_attempted": s["stall_retry_attempted"],
            "already_running_monitor_only": s["already_running_monitor_only"],
            "auto_advance_stalled": s["auto_advance_stalled"],
            "blocked_prior_task_failed": s["blocked_prior_task_failed"],
            "runner_timeout": s["runner_timeout"],
        }
        row = collect_task_data_pathguard(proj_name, workspace, pos, tid, title, final_status, extra)
        results.append(row)

        advisory_note = f" advisory={row['advisory_event_count']}" if row["advisory_event_count"] > 0 else " advisory=0"
        status_line = (
            f"  T{pos} id={tid} [{final_status}] "
            f"nested={row['nested_project_folder_command_count']}"
            f"{advisory_note} "
            f"vma={row['verification_mutates_source_assets']} "
            f"debug={row['debug_repair_count']}{row['debug_repair_classes']} "
            f"plan_repair={row['planning_repair_count']} "
            f"exec_reached={row['execution_reached']} "
            f"timeout={row['execution_timeout']} "
            f"constraint_rediscov={row['constraint_rediscovery']}"
        )
        if s["blocked_prior_task_failed"]:
            status_line += " [blocked]"
        if row["new_failure_codes"]:
            status_line += f" [NEW_CODES:{row['new_failure_codes']}]"
        if row.get("pip_show_failure_detected"):
            status_line += " [pip_show_RECURRED]"
        if row["advisory_event_count"] > 0:
            for adv in row["advisory_events"]:
                nd = adv.get("new_top_dir") or "?"
                fw = ", ".join((adv.get("files_written") or [])[:3])
                status_line += f" [ADVISORY: new_top_dir={nd!r} files={fw!r}]"
        print(status_line)

    return results


# ── Project list ──────────────────────────────────────────────────────────────

def pathguard_projects() -> list:
    projects = copy.deepcopy(r.PROJECTS)
    for p in projects:
        p["name"] = p["name"].replace("t1-confirm-", "t1-pathguard-")
        p["workspace"] = p["workspace"].replace("t1-confirm-", "t1-pathguard-")
        p["description"] = p["description"].replace(
            "T1 reliability confirmation — venv pip show fix verification",
            "T1 path guard Phase 1 advisory telemetry run",
        )
    return projects


# ── Flag verification ─────────────────────────────────────────────────────────

def assert_baseline_lane_and_flags() -> None:
    s = r.settings
    errors = []

    if s.PLANNING_BACKEND is not None:
        errors.append(f"PLANNING_BACKEND={s.PLANNING_BACKEND!r}, expected None")
    if "ai-gateway" not in s.PLANNING_REPAIR_BASE_URL and "8000" not in s.PLANNING_REPAIR_BASE_URL:
        errors.append(f"PLANNING_REPAIR_BASE_URL={s.PLANNING_REPAIR_BASE_URL!r}")
    if s.PLANNING_REPAIR_MODEL != "qwen-local":
        errors.append(f"PLANNING_REPAIR_MODEL={s.PLANNING_REPAIR_MODEL!r}, expected qwen-local")
    if s.EXECUTION_BACKEND is not None:
        errors.append(f"EXECUTION_BACKEND={s.EXECUTION_BACKEND!r}, expected None")
    if s.WORKING_MEMORY_PERSISTENCE_ENABLED:
        errors.append("WORKING_MEMORY_PERSISTENCE_ENABLED is True")
    if s.WORKING_MEMORY_RENDER_ENABLED:
        errors.append("WORKING_MEMORY_RENDER_ENABLED is True")
    if s.WORKING_MEMORY_INJECTION_ENABLED:
        errors.append("WORKING_MEMORY_INJECTION_ENABLED is True")
    if s.LANGFUSE_ENABLED:
        errors.append("LANGFUSE_ENABLED is True")
    if s.REPO_MEMORY_INJECTION_ENABLED:
        errors.append("REPO_MEMORY_INJECTION_ENABLED is True")
    if s.PSS_CONTINUATION_INJECTION_ENABLED:
        errors.append("PSS_CONTINUATION_INJECTION_ENABLED is True")
    if s.ARTIFACT_CONTINUATION_ENABLED:
        errors.append("ARTIFACT_CONTINUATION_ENABLED is True")
    if s.REDUCED_PLANNING_PROMPT_ENABLED:
        errors.append("REDUCED_PLANNING_PROMPT_ENABLED is True")

    if errors:
        for e in errors:
            print(f"  ✗ {e}")
        raise AssertionError(f"Lane/flag checks failed: {errors}")

    print("✓ All flags confirmed OFF")
    print(f"  PLANNING_BACKEND: {s.PLANNING_BACKEND!r} (None = local_openclaw)")
    print(f"  PLANNING_REPAIR_BASE_URL: {s.PLANNING_REPAIR_BASE_URL!r}")
    print(f"  PLANNING_REPAIR_MODEL: {s.PLANNING_REPAIR_MODEL!r}")
    print(f"  EXECUTION_BACKEND: {s.EXECUTION_BACKEND!r} (None = local_openclaw)")
    debug_base = str(getattr(s, "DEBUG_REPAIR_BASE_URL", "") or "")
    debug_model = str(getattr(s, "DEBUG_REPAIR_MODEL", "") or "")
    if debug_base or debug_model:
        print(f"  DEBUG_REPAIR_BASE_URL: {debug_base!r}")
        print(f"  DEBUG_REPAIR_MODEL: {debug_model!r}")
    else:
        print("  DEBUG_REPAIR_BASE_URL/MODEL: unset (fallback to baseline)")
    print("✓ Baseline lane confirmed")


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import time

    r._init_runtime()
    assert_baseline_lane_and_flags()

    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    all_results = []
    run_meta = {
        "runner": "t1_pathguard_telemetry",
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

    for proj_spec in pathguard_projects():
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

        proj_results = monitor_project_pathguard(proj_spec, task_ids)
        all_results.extend(proj_results)

    # ── Save raw results ──────────────────────────────────────────────────────
    out_dir = pathlib.Path(
        "docs/roadmap/reports/maintenance"
        "/project_aware_continuation_execution"
        "/slices_C_working_memory_persistence"
    )
    out_dir.mkdir(parents=True, exist_ok=True)
    out_path = out_dir / f"t1-pathguard-raw-{run_ts}.json"
    out_path.write_text(json.dumps({"meta": run_meta, "results": all_results}, indent=2))
    print(f"\n\nRaw results saved: {out_path}")

    # ── Aggregate ─────────────────────────────────────────────────────────────
    print("\n" + "=" * 60)
    print("PATH GUARD PHASE 1 ADVISORY TELEMETRY SUMMARY")
    print("=" * 60)

    done_count = sum(1 for r2 in all_results if r2["status"] == "done")
    failed_count = sum(1 for r2 in all_results if r2["status"] == "failed")
    blocked_count = sum(1 for r2 in all_results if r2.get("blocked_prior_task_failed"))
    corpus_total = len(all_results)
    corpus_completed = done_count + failed_count + blocked_count

    t1_results = [r2 for r2 in all_results if r2["plan_position"] == 1]
    t1_done = [r2 for r2 in t1_results if r2["status"] == "done"]

    t5_results = [r2 for r2 in all_results if r2["plan_position"] == 5]
    t6_results = [r2 for r2 in all_results if r2["plan_position"] == 6]
    t5_exec_reached = [r2 for r2 in t5_results if r2["execution_reached"]]
    t6_exec_reached = [r2 for r2 in t6_results if r2["execution_reached"]]

    # Advisory events
    advisory_total = sum(r2["advisory_event_count"] for r2 in all_results)
    advisory_tasks = [r2 for r2 in all_results if r2["advisory_event_count"] > 0]
    t5_t6_advisories = [
        r2 for r2 in all_results
        if r2["advisory_event_count"] > 0 and r2["plan_position"] in (5, 6)
    ]

    # Existing metrics
    nested_total = sum(r2["nested_project_folder_command_count"] for r2 in all_results)
    vma_tasks = [r2 for r2 in all_results if r2["verification_mutates_source_assets"]]
    plan_exhaust_tasks = [
        r2 for r2 in all_results
        if r2["status"] == "failed" and not r2["execution_reached"] and r2["planning_repair_count"] > 0
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

    print(f"\nProjects run:                    3")
    print(f"Total tasks:                     {corpus_total}")
    print(f"DONE:                            {done_count}")
    print(f"FAILED:                          {failed_count}")
    print(f"Blocked (prior task failed):     {blocked_count}")
    print(f"Corpus completion:               {corpus_completed}/{corpus_total}")
    print(f"T1 success (done):               {len(t1_done)}/{len(t1_results)}")
    print(f"T5 exec reached:                 {len(t5_exec_reached)}/3")
    print(f"T6 exec reached:                 {len(t6_exec_reached)}/3")
    print(f"")
    print(f"--- PATH GUARD ADVISORY ---")
    print(f"advisory_event count:            {advisory_total} (target=0)")
    if advisory_tasks:
        for r2 in advisory_tasks:
            print(f"  [ADVISORY HIT] T{r2['plan_position']} {r2['project']} task_id={r2['task_id']} "
                  f"advisory_count={r2['advisory_event_count']}")
            for adv in r2["advisory_events"]:
                nd = adv.get("new_top_dir") or "?"
                fw = ", ".join((adv.get("files_written") or [])[:4])
                print(f"    new_top_dir={nd!r} files={fw!r}")
    print(f"advisory T5/T6 false positives:  {len(t5_t6_advisories)} (target=0)")
    print(f"")
    print(f"--- PLAN-TIME VALIDATOR ---")
    print(f"nested_project_folder_command:   {nested_total} (target=0)")
    print(f"")
    print(f"--- EXISTING FAILURE CLASSES ---")
    print(f"verification_mutates_source:     {len(vma_tasks)}")
    print(f"planning_repair_exhaustion:      {len(plan_exhaust_tasks)}")
    print(f"constraint_rediscovery:          {len(constraint_rediscov_tasks)}")
    print(f"execution_timeout:               {len(exec_timeout_tasks)}")
    print(f"debug_repair total:              {debug_repair_total} "
          f"across {len(debug_repair_tasks)} tasks")
    print(f"pip-show recurrence:             {len(pip_show_recurrences)}")
    print(f"backend_capacity recurrence:     {len(env_cap_failures)} (target=0)")
    print(f"new failure codes introduced:    {len(all_new_codes)}")
    if all_new_codes:
        print(f"  codes: {all_new_codes[:5]}")

    print("\nPer-task summary (advisory=N means N advisory events):")
    for r2 in all_results:
        pos = r2["plan_position"]
        status = r2["status"]
        nested = r2["nested_project_folder_command_count"]
        advisory = r2["advisory_event_count"]
        vma = "vma" if r2["verification_mutates_source_assets"] else ""
        blocked = "[blocked]" if r2.get("blocked_prior_task_failed") else ""
        adv_note = f"[ADVISORY:{advisory}]" if advisory > 0 else ""
        print(
            f"  {r2['project']} T{pos} [{status}] "
            f"nested={nested} advisory={advisory} {vma} {blocked} {adv_note}"
        )

    print("\n" + "-" * 60)
    print("SUCCESS CRITERIA EVALUATION")
    print("-" * 60)

    sc_advisory_zero = advisory_total == 0
    sc_no_t5_t6_advisory = len(t5_t6_advisories) == 0
    sc_nested_zero = nested_total == 0
    sc_t1_success = len(t1_done) >= 2
    sc_no_new_class = len(all_new_codes) == 0
    sc_backend_cap = len(env_cap_failures) == 0

    print(f"{'✓' if sc_advisory_zero else '✗'} 0 advisory events:            {advisory_total}")
    print(f"{'✓' if sc_no_t5_t6_advisory else '✗'} 0 T5/T6 advisory events:      {len(t5_t6_advisories)}")
    print(f"{'✓' if sc_nested_zero else '✗'} 0 nested_project_folder_cmd:  {nested_total}")
    print(f"{'✓' if sc_t1_success else '✗'} T1 success >= 2/3:            {len(t1_done)}/3")
    print(f"{'✓' if sc_no_new_class else '✗'} No new failure class:         {len(all_new_codes)} new")
    print(f"{'✓' if sc_backend_cap else '✗'} Backend capacity recurrence=0: {len(env_cap_failures)}")

    all_pass = all([sc_advisory_zero, sc_no_t5_t6_advisory, sc_nested_zero,
                    sc_t1_success, sc_no_new_class, sc_backend_cap])
    print(f"\nPHASE 1 TELEMETRY: {'CLEAN RUN' if all_pass else 'SEE FINDINGS — check advisory hits'}")
    print(f"Raw results:   {out_path}")
    print(f"Run timestamp: {run_ts}")
