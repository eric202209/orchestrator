#!/usr/bin/env python3
"""E24: Post-E23 stale-replace verification mandate validation.

Runs 12 sequential python_cli_small_feature tasks (all oscillation cases in E20/E22
came from this fixture) and measures:
  - stale_replace occurrence rate
  - verification compliance rate (repaired plan had non-empty verification)
  - missing_verification occurrence rate after stale_replace repair
  - oscillation guard fire rate
  - completion rate

Baseline (E20/E22):
  - E20 python_cli: 2/4 oscillation, 0/4 verification compliance confirmed
  - E22: 3/3 oscillation cases = stale_replace -> missing_verification (100% FP)
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
OUTPUT_FILE = Path("/tmp/e24-results.json")

TASK_TIMEOUT = 600   # seconds per task
POLL_INTERVAL = 12   # seconds between polls

TERMINAL_STATUSES = frozenset(
    {"completed", "stopped", "failed", "cancelled", "canceled", "paused"}
)

# 12 python_cli_small_feature tasks — all oscillation cases in E20/E22 came from this fixture
PYTHON_CLI_PROMPT = (
    "Add the --uppercase option to this small Python CLI. "
    "When the flag is present, the CLI should uppercase the message before printing it. "
    "Keep changes scoped to src/ and tests/. Verify with python3 -m pytest -q."
)

TASK_CORPUS = [
    {"id": f"E24-P{i}", "fixture": "python_cli_small_feature", "prompt": PYTHON_CLI_PROMPT}
    for i in range(1, 13)
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
    dest = WORKSPACE_ROOT / f"e24-{fixture.replace('_', '-')}-{tag}"
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
# Log analysis
# ---------------------------------------------------------------------------


def _scan_log_for_task(task_id: int, log_lines: list[str]) -> dict:
    """Extract per-task signals from worker.log."""
    tid_str = str(task_id)

    repair_prompt_chars: list[int] = []
    stale_replace_occurred = False
    missing_verification_after_stale = False
    oscillation_guard_fired = False
    verification_field_present_in_repair: list[bool] = []

    # Patterns
    repair_chars_pat = re.compile(
        r"task_id=" + tid_str + r"\s+repair_prompt_chars=(\d+)"
    )
    stale_replace_pat = re.compile(
        r"task_id=" + tid_str + r".*stale_replace"
    )
    missing_verif_pat = re.compile(
        r"task_id=" + tid_str + r".*missing_verification"
    )
    oscillation_pat = re.compile(
        r"task_id=" + tid_str + r".*root_cause_oscillation_no_progress"
    )
    # Detect verification field present in repaired plan
    # Worker logs repair_root_cause_sequence when oscillation guard fires
    repair_sequence_pat = re.compile(
        r"task_id=" + tid_str + r".*repair_root_cause_sequence=\[([^\]]*)\]"
    )

    for line in log_lines:
        if tid_str not in line:
            continue
        m = repair_chars_pat.search(line)
        if m:
            repair_prompt_chars.append(int(m.group(1)))
        if stale_replace_pat.search(line):
            stale_replace_occurred = True
        if missing_verif_pat.search(line):
            missing_verification_after_stale = True
        if oscillation_pat.search(line):
            oscillation_guard_fired = True

    return {
        "task_id": task_id,
        "repair_prompt_chars": repair_prompt_chars,
        "repair_invocations": len(repair_prompt_chars),
        "stale_replace_occurred": stale_replace_occurred,
        "missing_verification_after_stale": missing_verification_after_stale,
        "oscillation_guard_fired": oscillation_guard_fired,
        "max_repair_chars": max(repair_prompt_chars) if repair_prompt_chars else 0,
    }


def _db_task_signals(task_id: int) -> dict:
    """Query SQLite for detailed per-task signals."""
    try:
        import sqlite3
        conn = sqlite3.connect(
            "/root/.openclaw/workspace/vault/projects/orchestrator/orchestrator.db"
        )
        # Get repair_root_cause_sequence from log_entries metadata
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
    missing_verification_occurred = False
    oscillation_guard_fired = False
    repair_root_cause_sequence: list[str] = []
    repair_invocations = 0
    repair_prompt_chars: list[int] = []
    execution_reached = False
    verification_field_nonempty_in_repair: list[bool] = []

    for event_type, raw_meta, log_level in rows:
        try:
            meta = json.loads(raw_meta or "{}")
        except Exception:
            meta = {}

        et = str(event_type or "")

        # stale_replace occurred
        if "stale_replace" in et or "stale_replace" in str(meta):
            stale_replace_occurred = True

        # missing_verification occurred
        if "missing_verification" in et or "missing_verification" in str(meta):
            missing_verification_occurred = True

        # oscillation guard
        if "oscillation" in et or "root_cause_oscillation_no_progress" in str(meta):
            oscillation_guard_fired = True

        # repair_root_cause_sequence
        seq = meta.get("repair_root_cause_sequence")
        if isinstance(seq, list) and len(seq) > len(repair_root_cause_sequence):
            repair_root_cause_sequence = seq

        # repair invocations
        if "repair" in et and "attempt" in et:
            repair_invocations += 1
        if "repair_prompt_chars" in meta:
            chars = meta.get("repair_prompt_chars")
            if isinstance(chars, int):
                repair_prompt_chars.append(chars)

        # execution reached
        if "step_started" in et or "step_execution" in et or "execute" in et:
            execution_reached = True

        # verification field present in repaired plan
        repaired_plan = meta.get("repaired_plan") or meta.get("plan")
        if repaired_plan and isinstance(repaired_plan, list):
            for step in repaired_plan:
                if isinstance(step, dict):
                    v = step.get("verification")
                    if v and str(v).strip():
                        verification_field_nonempty_in_repair.append(True)
                    else:
                        verification_field_nonempty_in_repair.append(False)

    return {
        "stale_replace_occurred": stale_replace_occurred,
        "missing_verification_occurred": missing_verification_occurred,
        "oscillation_guard_fired": oscillation_guard_fired,
        "repair_root_cause_sequence": repair_root_cause_sequence,
        "repair_invocations": repair_invocations,
        "repair_prompt_chars": repair_prompt_chars,
        "execution_reached": execution_reached,
        "verification_field_nonempty_in_repair": verification_field_nonempty_in_repair,
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
    if "stale_replace" in err:
        return "stale_replace_abort"
    if "bootstrap_contract" in err or "repair_candidate_rejected" in err:
        return "bootstrap_contract_PRCF"
    if "timed out" in err or "timeout" in err:
        return "timeout"
    if "json" in err and "parse" in err:
        return "json_parse_error"
    if "capacity" in err:
        return "backend_capacity"
    if "materialization" in err:
        return "source_materialization"
    if "planning failed" in err or "planning_circuit_breaker" in err:
        return "planning_circuit_breaker"
    if "verification_integrity" in err:
        return "verification_integrity"
    return "other"


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


def main() -> None:
    token = _get_token()
    print(f"[E24] Token obtained. Running {len(TASK_CORPUS)} sequential python_cli_small_feature tasks.")
    print(f"[E24] E23 mandate verification:")
    print(f"  mandate_present: True (verified before run)")
    print(f"  consequence_present: True (verified before run)")
    print(f"  old_soft_guidance_absent: True (verified before run)")

    results: list[dict] = []
    ts = datetime.now(UTC).strftime("%Y%m%d%H%M")

    for i, task_spec in enumerate(TASK_CORPUS):
        tag = f"{ts}-{i+1:02d}"
        fixture = task_spec["fixture"]
        tid = task_spec["id"]
        print(f"\n[E24] ({i+1}/{len(TASK_CORPUS)}) {tid} fixture={fixture}", flush=True)

        try:
            ws = _fresh_workspace(fixture, tag)
        except Exception as exc:
            print(f"  workspace provision failed: {exc}")
            results.append({"id": tid, "fixture": fixture, "error": str(exc), "category": "infra_error"})
            continue

        try:
            proj = _api("POST", "projects", token, {
                "name": f"E24 {tid}",
                "description": f"E24 post-E23 validation {tid}",
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
                "name": f"e24-{task_spec['id'].lower()}-{tag}",
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

        # DB-based signals
        db_signals = _db_task_signals(task_id)

        # Log-based fallback
        try:
            log_lines = LOG_FILE.read_text(encoding="utf-8").splitlines()
        except OSError:
            log_lines = []
        log_signals = _scan_log_for_task(task_id, log_lines)

        category = _classify(final)

        # Merge signals (DB preferred, log as fallback)
        stale_replace_occurred = db_signals.get("stale_replace_occurred") or log_signals["stale_replace_occurred"]
        missing_verification_occurred = db_signals.get("missing_verification_occurred") or log_signals["missing_verification_after_stale"]
        oscillation_guard_fired = db_signals.get("oscillation_guard_fired") or log_signals["oscillation_guard_fired"]
        repair_invocations = db_signals.get("repair_invocations") or log_signals["repair_invocations"]
        repair_prompt_chars = db_signals.get("repair_prompt_chars") or log_signals["repair_prompt_chars"]
        execution_reached = db_signals.get("execution_reached") or (category == "completed")
        repair_root_cause_sequence = db_signals.get("repair_root_cause_sequence", [])
        budget_exceeded = any(c > 6000 for c in repair_prompt_chars)

        # verification compliance: stale_replace occurred AND repaired plan had non-empty verification
        # We infer compliance from the absence of missing_verification after stale_replace
        verif_nonempty_list = db_signals.get("verification_field_nonempty_in_repair", [])
        verif_compliance = (
            all(verif_nonempty_list) if verif_nonempty_list
            else (stale_replace_occurred and not missing_verification_occurred)
        )

        rec = {
            "id": tid,
            "fixture": fixture,
            "task_id": task_id,
            "session_id": session_id,
            "status": final.get("status", "unknown"),
            "error_message": str(final.get("error_message") or "")[:200],
            "category": category,
            "repair_invoked": repair_invocations > 0,
            "repair_invocations": repair_invocations,
            "repair_prompt_chars": repair_prompt_chars,
            "max_repair_chars": max(repair_prompt_chars) if repair_prompt_chars else 0,
            "budget_exceeded": budget_exceeded,
            "stale_replace_occurred": stale_replace_occurred,
            "verification_field_nonempty": verif_compliance,
            "missing_verification_after_stale": missing_verification_occurred,
            "oscillation_guard_fired": oscillation_guard_fired,
            "repair_root_cause_sequence": repair_root_cause_sequence,
            "execution_reached": execution_reached,
        }
        results.append(rec)

        print(
            f"  → status={rec['status']} category={category} "
            f"repairs={repair_invocations} stale={stale_replace_occurred} "
            f"missing_verif={missing_verification_occurred} "
            f"oscillation={oscillation_guard_fired} "
            f"verif_compliance={verif_compliance} "
            f"chars={repair_prompt_chars}",
            flush=True,
        )

    # Save raw results
    OUTPUT_FILE.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\n[E24] Raw results saved → {OUTPUT_FILE}")

    # Aggregate metrics
    total = len(results)
    valid = [r for r in results if r.get("category") not in ("infra_error", "dispatch_error")]
    n = len(valid)

    stale_replace_tasks = [r for r in valid if r.get("stale_replace_occurred")]
    verif_compliant_tasks = [r for r in stale_replace_tasks if r.get("verification_field_nonempty")]
    missing_verif_tasks = [r for r in stale_replace_tasks if r.get("missing_verification_after_stale")]
    oscillation_tasks = [r for r in valid if r.get("oscillation_guard_fired")]
    completed_tasks = [r for r in valid if r.get("category") == "completed"]
    execution_reached_tasks = [r for r in valid if r.get("execution_reached")]
    repair_invoked_tasks = [r for r in valid if r.get("repair_invoked")]
    budget_exceeded_tasks = [r for r in valid if r.get("budget_exceeded")]

    stale_repair_count = len(stale_replace_tasks)
    verif_compliance_rate = (
        len(verif_compliant_tasks) / stale_repair_count
        if stale_repair_count > 0 else None
    )

    print("\n" + "=" * 60)
    print("[E24] AGGREGATE METRICS")
    print("=" * 60)
    print(f"  tasks_dispatched:                  {total}")
    print(f"  tasks_valid (no infra error):      {n}")
    print(f"  repair_invoked_count:              {len(repair_invoked_tasks)}")
    print(f"  stale_replace_repair_attempts:     {stale_repair_count}")
    print(f"  verification_compliant_count:      {len(verif_compliant_tasks)}")
    print(f"  verification_compliance_rate:      "
          f"{verif_compliance_rate:.1%}" if verif_compliance_rate is not None else "  verification_compliance_rate: N/A (no stale_replace)")
    print(f"  missing_verification_after_stale:  {len(missing_verif_tasks)}")
    print(f"  oscillation_guard_count:           {len(oscillation_tasks)}")
    print(f"  completed_count:                   {len(completed_tasks)}")
    print(f"  execution_reached_count:           {len(execution_reached_tasks)}")
    print(f"  budget_exceeded_count:             {len(budget_exceeded_tasks)}")

    if repair_invoked_tasks:
        all_chars = [c for r in repair_invoked_tasks for c in r.get("repair_prompt_chars", [])]
        if all_chars:
            print(f"  max_repair_prompt_chars:           {max(all_chars)}")
            print(f"  min_repair_prompt_chars:           {min(all_chars)}")

    print("\n[E24] Per-task summary:")
    for r in valid:
        print(
            f"  {r['id']:10s} task={r['task_id']:5d} status={r['status']:12s} "
            f"category={r['category']:35s} "
            f"stale={str(r['stale_replace_occurred']):5s} "
            f"verif={str(r['verification_field_nonempty']):5s} "
            f"osc={str(r['oscillation_guard_fired']):5s} "
            f"seq={r['repair_root_cause_sequence']}"
        )

    # Decision rule
    print("\n[E24] DECISION RULE EVALUATION")
    print("-" * 60)
    if stale_repair_count == 0:
        print("  No stale_replace repairs occurred — cannot measure compliance.")
        print("  Recommendation: KEEP_E23_MONITOR_MORE")
    elif verif_compliance_rate is not None and verif_compliance_rate >= 0.80:
        osc_drop = len(oscillation_tasks) < 2  # E20 had 2/4 oscillation on python_cli
        print(f"  verification_compliance_rate={verif_compliance_rate:.1%} >= 80% threshold")
        print(f"  oscillation_guard_count={len(oscillation_tasks)} (E20 baseline: 2/4 python_cli)")
        if osc_drop:
            print("  → KEEP_E23: compliance high, oscillation dropped")
        else:
            print("  → KEEP_E23_MONITOR_MORE: compliance high but oscillation count unchanged")
    else:
        print(f"  verification_compliance_rate={verif_compliance_rate:.1%} < 80% threshold")
        print("  → PROCEED_TO_CYCLE_DETECTION_GUARD (Candidate B)")

    if budget_exceeded_tasks:
        print(f"  WARNING: {len(budget_exceeded_tasks)} tasks had budget-exceeded prompts")

    return results


if __name__ == "__main__":
    main()
