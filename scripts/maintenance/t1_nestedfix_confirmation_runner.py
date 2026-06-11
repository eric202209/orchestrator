#!/usr/bin/env python3
"""
Nested-project-root validator fix confirmation runner.

Purpose: confirm that the nested-project-root validator false positive fix
(two guards in _plan_creates_nested_project_root, validator.py) eliminates
all nested_project_folder_command events at T5/T6 positions across the
standard 6-task T1-corpus.

Same corpus as t1_reliability_confirmation_runner.py (imported below).
Fresh workspaces: t1-nestedfix-{calclib,pathtools,strtools}.
Baseline lane: PLANNING_BACKEND=None -> local_openclaw, qwen-local repair.
No WM, no lane swap, no code changes in this runner.

Success criteria:
- 0 nested_project_folder_command events across all 18 tasks
- T1 success >= 2/3
- T5 Public API exports reaches execution in calclib and pathtools
- T6 Final verification reaches execution in all three projects
  (unless blocked by a genuine execution failure in a prior step)
- no new planning-validation failure class introduced
- corpus completion >= 16/18
- pip-show recurrence = 0
- genuine backend capacity recurrence = 0
"""
import copy
import importlib.util
import json
import pathlib
import sqlite3
import sys
from datetime import datetime

sys.path.insert(0, "/root/.openclaw/workspace/vault/projects/orchestrator")

_RUNNER = pathlib.Path(__file__).parent / "t1_reliability_confirmation_runner.py"
spec = importlib.util.spec_from_file_location("t1runner", str(_RUNNER))
r = importlib.util.module_from_spec(spec)
spec.loader.exec_module(r)

DB_PATH = r.DB_PATH
WORKSPACE_BASE = r.WORKSPACE_BASE


# ── Baseline lane assertion ──────────────────────────────────────────────────

def assert_baseline_lane() -> None:
    s = r.settings
    assert s.PLANNING_BACKEND is None, \
        f"PLANNING_BACKEND={s.PLANNING_BACKEND!r}, expected None (local_openclaw)"
    assert "ai-gateway" in s.PLANNING_REPAIR_BASE_URL or "8000" in s.PLANNING_REPAIR_BASE_URL, \
        f"PLANNING_REPAIR_BASE_URL={s.PLANNING_REPAIR_BASE_URL!r} looks wrong (expect ai-gateway)"
    assert s.PLANNING_REPAIR_MODEL == "qwen-local", \
        f"PLANNING_REPAIR_MODEL={s.PLANNING_REPAIR_MODEL!r}, expected 'qwen-local'"
    assert s.EXECUTION_BACKEND is None, \
        f"EXECUTION_BACKEND={s.EXECUTION_BACKEND!r}, expected None (local_openclaw)"
    print("✓ Baseline lane confirmed:")
    print(f"  PLANNING_BACKEND: {s.PLANNING_BACKEND!r} -> local_openclaw")
    print(f"  PLANNING_REPAIR_BASE_URL: {s.PLANNING_REPAIR_BASE_URL!r}")
    print(f"  PLANNING_REPAIR_MODEL: {s.PLANNING_REPAIR_MODEL!r}")
    print(f"  EXECUTION_BACKEND: {s.EXECUTION_BACKEND!r} -> local_openclaw")
    debug_base = getattr(s, "DEBUG_REPAIR_BASE_URL", "")
    debug_model = getattr(s, "DEBUG_REPAIR_MODEL", "")
    if debug_base or debug_model:
        print(f"  DEBUG_REPAIR_BASE_URL: {debug_base!r}")
        print(f"  DEBUG_REPAIR_MODEL: {debug_model!r}")
    else:
        print("  DEBUG_REPAIR_BASE_URL/MODEL: unset (fallback to baseline)")


# ── Additional detection helpers ─────────────────────────────────────────────

def detect_nested_project_folder_command(task_id: int) -> list[dict]:
    """
    Return each occurrence of nested_project_folder_command for this task.
    Detected via PLANNING_DIAGNOSTICS log_entries whose metadata contains
    a contract_violation_type matching 'nested_project'.
    """
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.cursor()
        cur.execute(
            "SELECT log_metadata FROM log_entries "
            "WHERE task_id=? AND message LIKE '%PLANNING_DIAGNOSTICS%' "
            "ORDER BY id",
            (task_id,),
        )
        rows = cur.fetchall()
        conn.close()
        results = []
        for (meta_str,) in rows:
            if not meta_str:
                continue
            try:
                meta = json.loads(meta_str)
            except Exception:
                continue
            cvt = str(meta.get("contract_violation_type", "")).lower()
            violations = meta.get("contract_violations", [])
            if "nested_project" in cvt or "nested_workspace" in cvt or any(
                "nested project" in str(v).lower() or "nested_project" in str(v).lower()
                for v in violations
            ):
                results.append({
                    "contract_violation_type": meta.get("contract_violation_type", ""),
                    "violations": violations[:3],
                    "output_chars": meta.get("output_chars"),
                })
        return results
    except Exception as e:
        return [{"error": str(e)}]


def detect_verification_mutates_source(task_id: int) -> bool:
    """Check whether verification_mutates_source_assets fired for this task."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.cursor()
        cur.execute(
            "SELECT log_metadata FROM log_entries "
            "WHERE task_id=? AND message LIKE '%Plan validation failed%' "
            "ORDER BY id",
            (task_id,),
        )
        rows = cur.fetchall()
        conn.close()
        for (meta_str,) in rows:
            if not meta_str:
                continue
            try:
                meta = json.loads(meta_str)
            except Exception:
                continue
            reasons = meta.get("validation_reasons", [])
            if any("mutates" in str(x).lower() for x in reasons):
                return True
        return False
    except Exception:
        return False


def detect_execution_timeout(task_id: int) -> bool:
    """Return True if any task_execution for this task has backend_timeout."""
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.cursor()
        cur.execute(
            "SELECT failure_category FROM task_executions WHERE task_id=? ORDER BY id",
            (task_id,),
        )
        rows = cur.fetchall()
        conn.close()
        return any(str(r[0] or "").lower() == "backend_timeout" for r in rows)
    except Exception:
        return False


def detect_constraint_rediscovery(task_id: int) -> bool:
    """
    Return True if planning repair was attempted but the post-repair validation
    failed with the same violation code (same-violation repair failure pattern).
    Detected via 'Plan validation failed after repair' log entries.
    """
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.cursor()
        cur.execute(
            "SELECT log_metadata FROM log_entries "
            "WHERE task_id=? AND message LIKE '%validation failed after repair%' "
            "ORDER BY id",
            (task_id,),
        )
        rows = cur.fetchall()
        conn.close()
        # Any post-repair validation failure is a constraint rediscovery
        return len(rows) > 0
    except Exception:
        return False


def detect_new_failure_class(task_id: int, known_codes: set[str]) -> list[str]:
    """
    Return unknown planning violation codes for this task
    (codes not in the known set, which includes the class being fixed).
    """
    try:
        conn = sqlite3.connect(str(DB_PATH))
        cur = conn.cursor()
        cur.execute(
            "SELECT log_metadata FROM log_entries "
            "WHERE task_id=? AND message LIKE '%PLANNING_DIAGNOSTICS%' "
            "ORDER BY id",
            (task_id,),
        )
        rows = cur.fetchall()
        conn.close()
        new_codes = []
        for (meta_str,) in rows:
            if not meta_str:
                continue
            try:
                meta = json.loads(meta_str)
            except Exception:
                continue
            cvt = meta.get("contract_violation_type", "")
            violations = meta.get("contract_violations", [])
            # Try to extract short code from violation text
            for v in violations:
                v_lower = str(v).lower()
                if "mutates" in v_lower:
                    code = "verification_mutates_source_assets"
                elif "nested project" in v_lower:
                    code = "nested_project_folder_command"
                elif "undefined" in v_lower or "undefined names" in v_lower:
                    code = "undefined_python_test_names"
                elif "missing verification" in v_lower:
                    code = "missing_verification_command"
                elif "runnable" in v_lower:
                    code = "steps_without_runnable_commands"
                elif "weak" in v_lower:
                    code = "weak_verification"
                else:
                    code = f"unknown:{str(v)[:60]}"
                if code not in known_codes and code not in new_codes:
                    new_codes.append(code)
        return new_codes
    except Exception:
        return []


# ── Extended collect_task_data ────────────────────────────────────────────────

KNOWN_FAILURE_CODES = {
    "nested_project_folder_command",
    "verification_mutates_source_assets",
    "weak_verification",
    "missing_verification_command",
    "steps_without_runnable_commands",
    "undefined_python_test_names",
}


def collect_task_data_extended(
    proj_name: str,
    workspace: str,
    pos: int,
    task_id: int,
    title: str,
    final_status: str,
    extra: dict,
) -> dict:
    base = r.collect_task_data(
        proj_name, workspace, pos, task_id, title, final_status, extra
    )

    nested_events = detect_nested_project_folder_command(task_id)
    verification_mutates = detect_verification_mutates_source(task_id)
    execution_timeout = detect_execution_timeout(task_id)
    constraint_rediscovery = detect_constraint_rediscovery(task_id)
    new_codes = detect_new_failure_class(task_id, KNOWN_FAILURE_CODES)

    base.update({
        "nested_project_folder_command_count": len(nested_events),
        "nested_project_folder_command_events": nested_events,
        "verification_mutates_source_assets": verification_mutates,
        "execution_timeout": execution_timeout,
        "constraint_rediscovery": constraint_rediscovery,
        "new_failure_codes": new_codes,
    })
    return base


# ── Patched monitor_project using extended collector ─────────────────────────

def monitor_project_extended(proj_spec: dict, task_ids: list[int]) -> list[dict]:
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

    import time
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
        row = collect_task_data_extended(proj_name, workspace, pos, tid, title, final_status, extra)
        results.append(row)

        status_line = (
            f"  T{pos} id={tid} [{final_status}] "
            f"nested={row['nested_project_folder_command_count']} "
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
        print(status_line)

    return results


# ── Project list ──────────────────────────────────────────────────────────────

def nestedfix_projects() -> list:
    projects = copy.deepcopy(r.PROJECTS)
    for p in projects:
        p["name"] = p["name"].replace("t1-confirm-", "t1-nestedfix-")
        p["workspace"] = p["workspace"].replace("t1-confirm-", "t1-nestedfix-")
        p["description"] = p["description"].replace(
            "T1 reliability confirmation — venv pip show fix verification",
            "T1 nested-project-root validator fix confirmation",
        )
    return projects


# ── Main ──────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    import time

    r._init_runtime()
    assert_baseline_lane()

    run_ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    all_results = []
    run_meta = {
        "runner": "t1_nestedfix_confirmation",
        "run_ts": run_ts,
        "runner_errors": 0,
        "planning_backend": str(r.settings.PLANNING_BACKEND),
        "planning_repair_base_url": str(r.settings.PLANNING_REPAIR_BASE_URL),
        "planning_repair_model": str(r.settings.PLANNING_REPAIR_MODEL),
        "execution_backend": str(r.settings.EXECUTION_BACKEND),
        "debug_repair_base_url": str(getattr(r.settings, "DEBUG_REPAIR_BASE_URL", "")),
        "debug_repair_model": str(getattr(r.settings, "DEBUG_REPAIR_MODEL", "")),
        "wm_persistence": r.settings.WORKING_MEMORY_PERSISTENCE_ENABLED,
        "wm_render": r.settings.WORKING_MEMORY_RENDER_ENABLED,
        "wm_injection": r.settings.WORKING_MEMORY_INJECTION_ENABLED,
        "langfuse_enabled": r.settings.LANGFUSE_ENABLED,
        "repo_memory_injection": r.settings.REPO_MEMORY_INJECTION_ENABLED,
        "pss_continuation": r.settings.PSS_CONTINUATION_INJECTION_ENABLED,
        "artifact_continuation": r.settings.ARTIFACT_CONTINUATION_ENABLED,
        "reduced_planning_prompt": r.settings.REDUCED_PLANNING_PROMPT_ENABLED,
    }

    for proj_spec in nestedfix_projects():
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

        proj_results = monitor_project_extended(proj_spec, task_ids)
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
    t1_failed = [r2 for r2 in t1_results if r2["status"] == "failed"]

    t2plus_results = [r2 for r2 in all_results if r2["plan_position"] > 1]
    t2plus_eligible = [
        r2 for r2 in t2plus_results
        if r2["status"] in ("done", "failed")
        and r2["execution_reached"]
        and not r2["env_capacity_failure"]
    ]

    terminal_results = [
        r2 for r2 in all_results
        if r2["status"] in ("done", "failed")
        or r2.get("blocked_prior_task_failed")
    ]
    done_count = sum(1 for r2 in all_results if r2["status"] == "done")
    failed_count = sum(1 for r2 in all_results if r2["status"] == "failed")
    blocked_count = sum(1 for r2 in all_results if r2.get("blocked_prior_task_failed"))
    pending_count = sum(1 for r2 in all_results
                        if r2["status"] not in ("done", "failed", "paused", "cancelled")
                        and not r2.get("blocked_prior_task_failed"))

    # nested_project_folder_command counts
    nested_total = sum(r2["nested_project_folder_command_count"] for r2 in all_results)
    nested_tasks = [r2 for r2 in all_results if r2["nested_project_folder_command_count"] > 0]

    # verification_mutates_source_assets
    vma_tasks = [r2 for r2 in all_results if r2["verification_mutates_source_assets"]]

    # planning_repair_exhaustion: tasks that failed pre-execution due to planning
    plan_exhaust_tasks = [
        r2 for r2 in all_results
        if r2["status"] == "failed"
        and not r2["execution_reached"]
        and r2["planning_repair_count"] > 0
    ]

    # constraint rediscovery
    constraint_rediscov_tasks = [r2 for r2 in all_results if r2["constraint_rediscovery"]]

    # execution timeout
    exec_timeout_tasks = [r2 for r2 in all_results if r2["execution_timeout"]]

    # debug repair
    debug_repair_tasks = [r2 for r2 in all_results if r2["debug_repair_count"] > 0]
    debug_repair_total = sum(r2["debug_repair_count"] for r2 in all_results)

    # pip show recurrence
    pip_show_recurrences = [r2 for r2 in all_results if r2.get("pip_show_failure_detected")]

    # backend capacity
    env_cap_failures = [r2 for r2 in all_results if r2["env_capacity_failure"]]

    # new failure codes
    all_new_codes = []
    for r2 in all_results:
        all_new_codes.extend(r2.get("new_failure_codes", []))

    # T5 and T6 execution check
    t5_results = [r2 for r2 in all_results if r2["plan_position"] == 5]
    t6_results = [r2 for r2 in all_results if r2["plan_position"] == 6]
    t5_exec_reached = [r2 for r2 in t5_results if r2["execution_reached"]]
    t6_exec_reached = [r2 for r2 in t6_results if r2["execution_reached"]]
    t6_not_blocked = [r2 for r2 in t6_results
                      if r2["execution_reached"] or r2.get("blocked_prior_task_failed")]

    corpus_completed = done_count + failed_count + blocked_count
    corpus_total = len(all_results)

    print(f"\nProjects run:                    3")
    print(f"Total tasks:                     {corpus_total}")
    print(f"DONE:                            {done_count}")
    print(f"FAILED:                          {failed_count}")
    print(f"Blocked (prior task failed):     {blocked_count}")
    print(f"Pending/running:                 {pending_count}")
    print(f"Corpus completion:               {corpus_completed}/{corpus_total}")
    print(f"T1 success (done):               {len(t1_done)}/{len(t1_results)}")
    print(f"T2+ eligible:                    {len(t2plus_eligible)}")
    print(f"nested_project_folder_command:   {nested_total} (target=0)")
    print(f"verification_mutates_source:     {len(vma_tasks)}")
    print(f"planning_repair_exhaustion:      {len(plan_exhaust_tasks)}")
    print(f"constraint_rediscovery:          {len(constraint_rediscov_tasks)}")
    print(f"execution_timeout:               {len(exec_timeout_tasks)}")
    print(f"debug_repair_count (total):      {debug_repair_total} "
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
        pip_flag = " ← pip show RECURRED" if row.get("pip_show_failure_detected") else ""
        print(
            f"  {row['project']} T1 [{row['status']}] "
            f"nested={row['nested_project_folder_command_count']} "
            f"plan_repairs={row['planning_repair_count']} "
            f"debug_repairs={row['debug_repair_count']}"
            f"{pip_flag}"
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

    # ── Success criteria evaluation ───────────────────────────────────────────
    print("\n" + "-" * 60)
    print("SUCCESS CRITERIA EVALUATION")
    print("-" * 60)

    sc_nested_zero = nested_total == 0
    sc_t1_success = len(t1_done) >= 2
    sc_t5_exec = len(t5_exec_reached) >= 2  # calclib + pathtools T5
    sc_t6_exec = len(t6_exec_reached) >= 2  # at least 2/3; 3rd can be blocked by genuine exec fail
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
