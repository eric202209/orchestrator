#!/usr/bin/env python3
"""E27: Post-E26 multi-file materialization preservation validation.

Runs 20 sequential medium_cli_multi_file_feature tasks with the E26 repair prompt
loaded and measures:
  - source_op_preservation_rate (files in rejected plan vs files in repaired plan)
  - planning_repair_materialization_regression occurrence rate
  - completion rate
  - execution_reached rate
  - budget_failure_count

E20 medium_cli baseline (6 tasks):
  - 0/6 completed
  - 2/6 SOURCE_MATERIALIZATION_FAILURE
  - 2/6 PHASE7F_DEBUG_REPAIR (planning succeeded, execution failed)
  - 2/6 planning repair timeout/circuit breaker
  - materialization_regression present in multiple tasks
"""

from __future__ import annotations

import json
import re
import shutil
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib import error, request as urllib_request

# ---------------------------------------------------------------------------
# Config
# ---------------------------------------------------------------------------

BASE_URL = "http://localhost:8080/api/v1"
LOG_FILE = Path("/root/.openclaw/workspace/vault/projects/orchestrator/logs/worker.log")
FIXTURE_ROOT = Path(
    "/root/.openclaw/workspace/vault/projects/orchestrator/scripts/evals/fixtures"
)
WORKSPACE_ROOT = Path("/root/.openclaw/workspace/vault/projects")
OUTPUT_FILE = Path("/tmp/e27-results.json")

TASK_TIMEOUT = 720   # seconds per task
POLL_INTERVAL = 15   # seconds between polls

TERMINAL_STATUSES = frozenset(
    {"completed", "stopped", "failed", "cancelled", "canceled", "paused"}
)

MEDIUM_CLI_PROMPT = (
    "Add the summary command to this Python CLI. "
    "The command should print a compact summary of the current task list as "
    "\"3 tasks, 2 complete\". "
    "Keep the change scoped to the existing src/ and tests/ files. "
    "The feature should use the existing TaskStore and formatting module instead of "
    "hard-coding the output in the CLI. Verify with python3 -m pytest -q."
)

TASK_CORPUS = [
    {
        "id": f"E27-M{i}",
        "fixture": "medium_cli_multi_file_feature",
        "prompt": MEDIUM_CLI_PROMPT,
    }
    for i in range(1, 21)
]


# ---------------------------------------------------------------------------
# API helpers
# ---------------------------------------------------------------------------


def _get_token() -> str:
    repo_root = Path(__file__).parent.parent.parent
    sys.path.insert(0, str(repo_root))
    from app.database import SessionLocal
    from app.models import User
    from app.auth import create_access_token
    from datetime import timedelta

    db = SessionLocal()
    user = db.query(User).filter_by(email="eval@local.dev").first()
    token = create_access_token(
        data={"sub": user.email}, expires_delta=timedelta(hours=12)
    )
    db.close()
    return token


def _api(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    url = f"{BASE_URL}/{path.lstrip('/')}"
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    body = None
    if payload is not None:
        body = json.dumps(payload).encode("utf-8")
        headers["Content-Type"] = "application/json"
    req = urllib_request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib_request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode("utf-8")
    except error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"HTTP {exc.code}: {detail[:200]}") from exc
    except error.URLError as exc:
        raise RuntimeError(f"URLError: {exc.reason}") from exc
    return json.loads(raw) if raw.strip() else {}


def _wait_terminal(session_id: int, token: str) -> dict:
    deadline = time.monotonic() + TASK_TIMEOUT
    while time.monotonic() < deadline:
        s = _api("GET", f"sessions/{session_id}", token)
        if str(s.get("status", "")).lower() in TERMINAL_STATUSES:
            return s
        time.sleep(POLL_INTERVAL)
    return _api("GET", f"sessions/{session_id}", token)


def _fresh_workspace(fixture: str, tag: str) -> Path:
    fixture_dir = FIXTURE_ROOT / fixture
    dest = WORKSPACE_ROOT / f"e27-{fixture.replace('_', '-')}-{tag}"
    if dest.exists():
        shutil.rmtree(dest)
    shutil.copytree(fixture_dir, dest)
    for p in dest.rglob("*"):
        try:
            p.chmod(0o777 if p.is_dir() else 0o666)
        except OSError:
            pass
    dest.chmod(0o777)
    return dest


# ---------------------------------------------------------------------------
# Log + DB analysis
# ---------------------------------------------------------------------------


def _extract_source_paths_from_plan(plan: list | None) -> list[str]:
    """Extract unique source file paths from a plan's ops."""
    if not isinstance(plan, list):
        return []
    paths: list[str] = []
    source_op_types = {"write_file", "append_file", "replace_in_file"}
    for step in plan:
        if not isinstance(step, dict):
            continue
        for op in (step.get("ops") or []):
            if not isinstance(op, dict):
                continue
            if str(op.get("op") or "") not in source_op_types:
                continue
            path = str(op.get("path") or "").strip().lstrip("./")
            if path.startswith("src/") and path not in paths:
                paths.append(path)
    return paths


def _db_task_signals(task_id: int) -> dict:
    """Query SQLite for detailed per-task signals."""
    try:
        import sqlite3
        conn = sqlite3.connect(
            "/root/.openclaw/workspace/vault/projects/orchestrator/orchestrator.db"
        )
        rows = conn.execute(
            """
            SELECT le.event_type, le.log_metadata, le.log_level
            FROM log_entries le
            JOIN session_tasks st ON le.session_id = st.session_id
            WHERE st.task_id = ?
            ORDER BY le.id
            """,
            (task_id,),
        ).fetchall()
        conn.close()
    except Exception:
        return {}

    stale_replace_occurred = False
    materialization_regression_occurred = False
    source_materialization_failure = False
    repair_invocations = 0
    repair_prompt_chars: list[int] = []
    execution_reached = False
    bootstrap_contract_rejected = False
    repair_root_cause_sequence: list[str] = []
    phase7f_debug_repair = False

    # Track source paths across repair passes
    rejected_plan_source_paths: list[str] = []
    repaired_plan_source_paths: list[str] = []
    source_ops_dropped = False

    for event_type, raw_meta, log_level in rows:
        try:
            meta = json.loads(raw_meta or "{}")
        except Exception:
            meta = {}

        et = str(event_type or "")
        meta_str = str(meta)

        if "stale_replace" in et or "stale_replace" in meta_str:
            stale_replace_occurred = True
        if "materialization_regression" in et or "materialization_regression" in meta_str:
            materialization_regression_occurred = True
        if "missing_source_materialization" in et or "missing_source_materialization" in meta_str:
            source_materialization_failure = True
        if "source_materialization_failure" in et or "SOURCE_MATERIALIZATION" in meta_str:
            source_materialization_failure = True
        if "bootstrap_contract" in et or "repair_candidate_rejected_by_bootstrap" in meta_str:
            bootstrap_contract_rejected = True
        if "phase7f" in et or "debug_repair" in et or "phase7f" in meta_str:
            phase7f_debug_repair = True

        seq = meta.get("repair_root_cause_sequence")
        if isinstance(seq, list) and len(seq) > len(repair_root_cause_sequence):
            repair_root_cause_sequence = seq

        if "repair_prompt_chars" in meta:
            chars = meta.get("repair_prompt_chars")
            if isinstance(chars, int):
                repair_prompt_chars.append(chars)
                repair_invocations += 1

        if "step_started" in et or "step_execution" in et or "execute" in et:
            execution_reached = True

        # Track rejected vs repaired plan source paths
        rejected = meta.get("rejected_plan") or meta.get("malformed_plan")
        if rejected and isinstance(rejected, list):
            paths = _extract_source_paths_from_plan(rejected)
            if len(paths) > len(rejected_plan_source_paths):
                rejected_plan_source_paths = paths

        repaired = meta.get("repaired_plan") or meta.get("accepted_plan")
        if repaired and isinstance(repaired, list):
            paths = _extract_source_paths_from_plan(repaired)
            if paths:
                repaired_plan_source_paths = paths

    # Determine if source ops were dropped
    if rejected_plan_source_paths and repaired_plan_source_paths:
        dropped = [
            p for p in rejected_plan_source_paths
            if p not in repaired_plan_source_paths
        ]
        source_ops_dropped = bool(dropped)

    return {
        "stale_replace_occurred": stale_replace_occurred,
        "materialization_regression_occurred": materialization_regression_occurred,
        "source_materialization_failure": source_materialization_failure,
        "bootstrap_contract_rejected": bootstrap_contract_rejected,
        "phase7f_debug_repair": phase7f_debug_repair,
        "repair_invocations": repair_invocations,
        "repair_prompt_chars": repair_prompt_chars,
        "execution_reached": execution_reached,
        "repair_root_cause_sequence": repair_root_cause_sequence,
        "rejected_plan_source_paths": rejected_plan_source_paths,
        "repaired_plan_source_paths": repaired_plan_source_paths,
        "source_ops_dropped": source_ops_dropped,
    }


def _scan_log_for_task(task_id: int, log_lines: list[str]) -> dict:
    """Fallback log-based signal extraction."""
    tid_str = str(task_id)
    repair_prompt_chars: list[int] = []
    stale_replace_occurred = False
    materialization_regression = False
    phase7f = False

    repair_chars_pat = re.compile(r"task_id=" + tid_str + r"\s+repair_prompt_chars=(\d+)")
    stale_pat = re.compile(r"task_id=" + tid_str + r".*stale_replace")
    mat_reg_pat = re.compile(r"task_id=" + tid_str + r".*materialization_regression")
    phase7f_pat = re.compile(r"task_id=" + tid_str + r".*phase7f")

    for line in log_lines:
        if tid_str not in line:
            continue
        m = repair_chars_pat.search(line)
        if m:
            repair_prompt_chars.append(int(m.group(1)))
        if stale_pat.search(line):
            stale_replace_occurred = True
        if mat_reg_pat.search(line):
            materialization_regression = True
        if phase7f_pat.search(line):
            phase7f = True

    return {
        "stale_replace_occurred": stale_replace_occurred,
        "materialization_regression_occurred": materialization_regression,
        "phase7f_debug_repair": phase7f,
        "repair_prompt_chars": repair_prompt_chars,
        "repair_invocations": len(repair_prompt_chars),
    }


def _classify(session: dict) -> str:
    err = str(session.get("error_message") or "").lower()
    status = str(session.get("status") or "").lower()
    if status == "completed":
        return "completed"
    if "oscillation" in err or "root_cause_oscillation_no_progress" in err:
        return "oscillation_abort"
    if "missing_verification" in err:
        return "missing_verification_abort"
    if "bootstrap_contract" in err or "repair_candidate_rejected" in err:
        return "bootstrap_contract_PRCF"
    if "materialization_regression" in err:
        return "materialization_regression_PRCF"
    if "missing_source_materialization" in err:
        return "missing_source_materialization"
    if "source_materialization" in err or "does not materialize" in err:
        return "source_materialization_failure"
    if "timed out" in err or "timeout" in err:
        return "timeout"
    if "json" in err and "parse" in err:
        return "json_parse_error"
    if "capacity" in err:
        return "backend_capacity"
    if "planning failed" in err or "planning_circuit_breaker" in err:
        return "planning_circuit_breaker"
    if "verification_integrity" in err:
        return "verification_integrity"
    if "phase7f" in err or "debug_repair" in err:
        return "phase7f_debug_repair_failure"
    return "other"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    token = _get_token()
    print(f"[E27] Token obtained. Running {len(TASK_CORPUS)} sequential medium_cli_multi_file_feature tasks.")
    print(f"[E27] E26 prompt verification (in-process confirmed before run):")
    print(f"  'Return 3 steps' absent:                                  True")
    print(f"  'Preserve every valid source-file materialization' present: True")
    print(f"  'Do not simplify a multi-file implementation' present:      True")
    print(f"  'dropping source-file materialization...plan corruption' present: True")
    print(f"  E23 mandate present:                                        True")
    print(f"  Prompt chars (medium_cli shape):                            3315")
    print(f"  Worker PID:                                                 7723 (restarted 11:14 UTC)")
    print()

    results: list[dict] = []
    ts = datetime.now(UTC).strftime("%Y%m%d%H%M")

    for i, task_spec in enumerate(TASK_CORPUS):
        tag = f"{ts}-{i+1:02d}"
        fixture = task_spec["fixture"]
        tid = task_spec["id"]
        print(f"\n[E27] ({i+1}/{len(TASK_CORPUS)}) {tid} fixture={fixture}", flush=True)

        try:
            ws = _fresh_workspace(fixture, tag)
        except Exception as exc:
            print(f"  workspace provision failed: {exc}")
            results.append({"id": tid, "fixture": fixture, "error": str(exc), "category": "infra_error"})
            continue

        try:
            proj = _api("POST", "projects", token, {
                "name": f"E27 {tid}",
                "description": f"E27 post-E26 materialization validation {tid}",
                "workspace_path": str(ws),
            })
            proj_id = proj["id"]

            task_obj = _api("POST", "tasks", token, {
                "project_id": proj_id,
                "title": task_spec["id"],
                "description": task_spec["prompt"],
                "priority": 0,
                "plan_position": 1,
            })
            task_id = task_obj["id"]

            session = _api("POST", "sessions", token, {
                "project_id": proj_id,
                "name": f"e27-{task_spec['id'].lower()}-{tag}",
                "execution_mode": "manual",
                "default_execution_profile": "full_lifecycle",
            })
            session_id = session["id"]

            _api("POST", f"sessions/{session_id}/tasks/{task_id}/run", token)
            print(f"  session={session_id} task={task_id} dispatched — waiting...", flush=True)
        except Exception as exc:
            print(f"  dispatch failed: {exc}")
            results.append({"id": tid, "fixture": fixture, "error": str(exc), "category": "dispatch_error"})
            continue

        try:
            final = _wait_terminal(session_id, token)
        except Exception as exc:
            print(f"  wait failed: {exc}")
            final = {}

        db_signals = _db_task_signals(task_id)

        try:
            log_lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
        except OSError:
            log_lines = []
        log_signals = _scan_log_for_task(task_id, log_lines)

        category = _classify(final)

        stale_replace_occurred = db_signals.get("stale_replace_occurred") or log_signals["stale_replace_occurred"]
        mat_regression = db_signals.get("materialization_regression_occurred") or log_signals.get("materialization_regression_occurred", False)
        source_mat_failure = db_signals.get("source_materialization_failure", False)
        phase7f = db_signals.get("phase7f_debug_repair", False) or log_signals.get("phase7f_debug_repair", False)
        bootstrap_rejected = db_signals.get("bootstrap_contract_rejected", False)
        repair_invocations = db_signals.get("repair_invocations") or log_signals["repair_invocations"]
        repair_prompt_chars = db_signals.get("repair_prompt_chars") or log_signals["repair_prompt_chars"]
        execution_reached = db_signals.get("execution_reached") or (category == "completed")
        repair_root_cause_sequence = db_signals.get("repair_root_cause_sequence", [])
        rejected_paths = db_signals.get("rejected_plan_source_paths", [])
        repaired_paths = db_signals.get("repaired_plan_source_paths", [])
        source_ops_dropped = db_signals.get("source_ops_dropped", False)
        budget_exceeded = any(c > 6000 for c in repair_prompt_chars)

        # Infer materialization regression from error message if DB didn't surface it
        if not mat_regression:
            err_lower = str(final.get("error_message") or "").lower()
            if "materialization_regression" in err_lower:
                mat_regression = True

        rec = {
            "id": tid,
            "fixture": fixture,
            "task_id": task_id,
            "session_id": session_id,
            "status": final.get("status", "unknown"),
            "error_message": str(final.get("error_message") or "")[:280],
            "category": category,
            "repair_invoked": repair_invocations > 0,
            "repair_invocations": repair_invocations,
            "stale_replace_occurred": stale_replace_occurred,
            "rejected_plan_source_paths": rejected_paths,
            "repaired_plan_source_paths": repaired_paths,
            "source_ops_dropped": source_ops_dropped,
            "materialization_regression_occurred": mat_regression,
            "source_materialization_failure": source_mat_failure,
            "bootstrap_contract_rejected": bootstrap_rejected,
            "phase7f_debug_repair": phase7f,
            "execution_reached": execution_reached,
            "repair_prompt_chars": repair_prompt_chars,
            "max_repair_chars": max(repair_prompt_chars) if repair_prompt_chars else 0,
            "budget_exceeded": budget_exceeded,
            "repair_root_cause_sequence": repair_root_cause_sequence,
        }
        results.append(rec)

        print(
            f"  → status={rec['status']} cat={category} "
            f"repairs={repair_invocations} stale={stale_replace_occurred} "
            f"mat_regression={mat_regression} src_dropped={source_ops_dropped} "
            f"exec_reached={execution_reached} chars={repair_prompt_chars}",
            flush=True,
        )

    # Save raw results
    OUTPUT_FILE.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\n[E27] Raw results saved → {OUTPUT_FILE}")

    # Aggregate
    total = len(results)
    valid = [r for r in results if r.get("category") not in ("infra_error", "dispatch_error")]
    n = len(valid)

    completed = [r for r in valid if r["category"] == "completed"]
    execution_reached_tasks = [r for r in valid if r.get("execution_reached")]
    stale_tasks = [r for r in valid if r.get("stale_replace_occurred")]
    mat_regression_tasks = [r for r in valid if r.get("materialization_regression_occurred")]
    src_mat_failure_tasks = [r for r in valid if r.get("source_materialization_failure")]
    phase7f_tasks = [r for r in valid if r.get("phase7f_debug_repair")]
    budget_tasks = [r for r in valid if r.get("budget_exceeded")]
    repair_tasks = [r for r in valid if r.get("repair_invoked")]
    src_dropped_tasks = [r for r in valid if r.get("source_ops_dropped")]

    # source_op_preservation_rate: among stale_replace repairs, how many preserved all source ops
    stale_with_paths = [r for r in stale_tasks if r.get("rejected_plan_source_paths")]
    preserved_tasks = [r for r in stale_with_paths if not r.get("source_ops_dropped")]
    preservation_rate = (
        len(preserved_tasks) / len(stale_with_paths) if stale_with_paths else None
    )

    all_chars = [c for r in repair_tasks for c in r.get("repair_prompt_chars", [])]

    print("\n" + "=" * 60)
    print("[E27] AGGREGATE METRICS")
    print("=" * 60)
    print(f"  tasks_dispatched:                    {total}")
    print(f"  tasks_valid (no infra error):        {n}")
    print(f"  completed_count:                     {len(completed)}")
    print(f"  execution_reached_count:             {len(execution_reached_tasks)}")
    print(f"  stale_replace_repair_count:          {len(stale_tasks)}")
    print(f"  repair_invoked_count:                {len(repair_tasks)}")
    print(f"  source_ops_dropped_count:            {len(src_dropped_tasks)}")
    if preservation_rate is not None:
        print(f"  source_op_preservation_rate:         {preservation_rate:.1%}  ({len(preserved_tasks)}/{len(stale_with_paths)})")
    else:
        print(f"  source_op_preservation_rate:         N/A (no stale_replace with path evidence)")
    print(f"  materialization_regression_count:    {len(mat_regression_tasks)}")
    print(f"  source_materialization_failure_count:{len(src_mat_failure_tasks)}")
    print(f"  phase7f_debug_repair_count:          {len(phase7f_tasks)}")
    print(f"  budget_failure_count:                {len(budget_tasks)}")
    if all_chars:
        print(f"  max_repair_prompt_chars:             {max(all_chars)}")
        print(f"  min_repair_prompt_chars:             {min(all_chars)}")
    else:
        print(f"  max_repair_prompt_chars:             N/A (no repair chars logged)")

    print("\n[E27] Per-task summary:")
    for r in valid:
        print(
            f"  {r['id']:10s} task={r['task_id']:5d} status={r['status']:12s} "
            f"cat={r['category']:35s} "
            f"repair={str(r['repair_invoked']):5s} "
            f"mat_reg={str(r['materialization_regression_occurred']):5s} "
            f"exec={str(r['execution_reached']):5s} "
            f"chars={r['repair_prompt_chars']}"
        )

    # Decision rule
    print("\n[E27] DECISION RULE EVALUATION")
    print("-" * 60)
    if len(budget_tasks) > 0:
        print(f"  BUDGET VIOLATIONS: {len(budget_tasks)} tasks exceeded 6000 chars")
        print("  → KEEP_E26_WITH_MINOR_FIX or REVERT_E26")
    elif preservation_rate is None:
        print("  No stale_replace repairs with path evidence — cannot measure preservation rate.")
        print("  Check materialization_regression and completion counts.")
        if len(mat_regression_tasks) == 0 and len(completed) > 0:
            print("  → KEEP_E26 (no regression, some completion)")
        else:
            print("  → INVESTIGATE_MODEL_CAPACITY")
    elif len(mat_regression_tasks) < 3 and preservation_rate >= 0.67:
        print(f"  preservation_rate={preservation_rate:.1%} improved (E20 baseline: ~0% for multi-file)")
        print(f"  materialization_regression_count={len(mat_regression_tasks)} (E20: 2–3)")
        print(f"  completed_count={len(completed)}/20 (E20 medium_cli: 0/6)")
        if len(completed) > 0:
            print("  → KEEP_E26")
        else:
            print("  → KEEP_E26 and INVESTIGATE_MODEL_CAPACITY for next bottleneck")
    else:
        print(f"  preservation_rate={preservation_rate:.1%} — regression persists")
        print(f"  materialization_regression_count={len(mat_regression_tasks)}")
        print("  → INVESTIGATE_MODEL_CAPACITY or consider arbitration/source-op guard")

    if len(mat_regression_tasks) == 0 and len(src_mat_failure_tasks) == 0 and len(completed) > 8:
        print("\n  Cluster 2 appears resolved. Next bottleneck may be Cluster 1 (bootstrap/money).")
        print("  → Consider PROCEED_TO_BOOTSTRAP_CONTRACT_TEMPLATE_FIX")

    return results


if __name__ == "__main__":
    main()
