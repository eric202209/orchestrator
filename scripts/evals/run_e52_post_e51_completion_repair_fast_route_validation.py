#!/usr/bin/env python3
"""E52: Post-E51 Completion Repair Fast-Route Validation.

Runs 10 sequential medium_cli_multi_file_feature tasks with E51 code loaded and
COMPLETION_REPAIR_BACKEND=direct_ollama set. Measures whether routing completion-repair
generation to the fast runtime reduces the 120s timeout failure rate observed in E46
(6/6 = 100% timeout).

E46 baseline:
  - completion_repair_timeout_count: 6/6 (100%)
  - completion_repair_prompt_chars: not logged
  - fast_profile_selected: 0/6 (fast route not yet implemented)
  - completed_count: 0/10
  - PAEF_count: 6/10

Decision thresholds:
  - If timeout rate drops significantly and repair responses arrive:
      KEEP_E51_FAST_ROUTE
  - If timeout rate stays high but prompt_chars are large:
      INVESTIGATE_COMPLETION_REPAIR_PROMPT_REDUCTION
  - If fast_profile falls back frequently:
      FIX_COMPLETION_REPAIR_BACKEND_CONFIG
  - If responses arrive but repairs remain wrong:
      INVESTIGATE_COMPLETION_REPAIR_QUALITY
  - If false-success appears:
      REVERT_E51_ROUTE
"""

from __future__ import annotations

import json
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

BASE_URL = "http://localhost:8080/api/v1"
LOG_FILE = Path("/root/.openclaw/workspace/vault/projects/orchestrator/logs/worker.log")
FIXTURE_ROOT = Path(
    "/root/.openclaw/workspace/vault/projects/orchestrator/scripts/evals/fixtures"
)
WORKSPACE_ROOT = Path("/root/.openclaw/workspace/vault/projects")
OUTPUT_FILE = Path("/tmp/e52-results.json")

TASK_TIMEOUT = 900
POLL_INTERVAL = 15

TERMINAL_STATUSES = frozenset(
    {"completed", "stopped", "failed", "cancelled", "canceled", "paused"}
)

MEDIUM_CLI_PROMPT = (
    "Add the summary command to this Python CLI. "
    "The command should print a compact summary of the current task list as "
    '"3 tasks, 2 complete". '
    "Keep the change scoped to the existing src/ and tests/ files. "
    "The feature should use the existing TaskStore and formatting module instead of "
    "hard-coding the output in the CLI. Verify with python3 -m pytest -q."
)

TASK_CORPUS = [
    {
        "id": f"E52-M{i}",
        "fixture": "medium_cli_multi_file_feature",
        "prompt": MEDIUM_CLI_PROMPT,
    }
    for i in range(1, 11)
]


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
    from urllib import error, request as urllib_request

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
    dest = WORKSPACE_ROOT / f"e52-{fixture.replace('_', '-')}-{tag}"
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


def _db_completion_repair_signals(task_id: int) -> dict:
    """Read log_entries for E51 completion-repair telemetry fields.

    The emit_live call in _attempt_completion_repair writes structured metadata
    to log_entries.log_metadata with completion_repair_* fields. We look for ANY
    row where log_metadata contains "completion_repair_prompt_chars" to confirm
    the repair was invoked.
    """
    try:
        import sqlite3
        conn = sqlite3.connect(
            "/root/.openclaw/workspace/vault/projects/orchestrator/orchestrator.db"
        )
        log_rows = conn.execute(
            "SELECT level, message, log_metadata FROM log_entries WHERE task_id=? ORDER BY id",
            (task_id,),
        ).fetchall()
        exec_rows = conn.execute(
            "SELECT attempt_number, status, failure_category FROM task_executions WHERE task_id=? ORDER BY id",
            (task_id,),
        ).fetchall()
        conn.close()
    except Exception as exc:
        return {"db_error": str(exc)}

    result = {
        "execution_reached": len(exec_rows) > 0,
        "pytest_failure_in_log": False,
        "completion_repair_invoked": False,
        "completion_repair_prompt_chars": None,
        "completion_repair_timeout_seconds": None,
        "completion_repair_runtime_profile": None,
        "completion_repair_duration_seconds": None,
        "completion_repair_timed_out": None,
        "completion_repair_output_chars": None,
        "completion_repair_exception_type": None,
        "completion_repair_fast_profile_selected": None,
        "completion_repair_fast_profile_fallback": None,
        "completion_repair_response_parsed": None,
        "failure_category": exec_rows[-1][2] if exec_rows else None,
        "attempt_count": len(exec_rows),
        "planning_circuit_breaker": False,
    }

    for level, msg, raw_meta in log_rows:
        try:
            meta = json.loads(raw_meta or "{}")
        except Exception:
            meta = {}

        msg_lower = str(msg or "").lower()

        # Detect pytest failure from log messages
        if (
            "pytest" in msg_lower and ("fail" in msg_lower or "error" in msg_lower)
        ) or "completion_verification:pytest_failure" in msg_lower:
            result["pytest_failure_in_log"] = True

        # Detect planning circuit breaker
        if "root_cause_oscillation" in msg_lower or "planning_circuit_breaker" in msg_lower:
            result["planning_circuit_breaker"] = True

        # Detect E51 completion repair telemetry (emitted on both success and failure path)
        if "completion_repair_prompt_chars" not in meta:
            continue

        result["completion_repair_invoked"] = True
        # These fields are present on both paths
        result["completion_repair_prompt_chars"] = meta.get("completion_repair_prompt_chars")
        result["completion_repair_timeout_seconds"] = meta.get("completion_repair_timeout_seconds")
        result["completion_repair_runtime_profile"] = meta.get("completion_repair_runtime_profile")
        result["completion_repair_duration_seconds"] = meta.get("completion_repair_duration_seconds")
        result["completion_repair_timed_out"] = meta.get("completion_repair_timed_out")
        result["completion_repair_fast_profile_selected"] = meta.get("completion_repair_fast_profile_selected")
        result["completion_repair_fast_profile_fallback"] = meta.get("completion_repair_fast_profile_fallback")
        # Success-path only
        if "completion_repair_output_chars" in meta:
            result["completion_repair_output_chars"] = meta.get("completion_repair_output_chars")
        # Failure-path only
        if "completion_repair_exception_type" in meta:
            result["completion_repair_exception_type"] = meta.get("completion_repair_exception_type")

    return result


def _orchestration_event_signals(workspace_path: Path, session_id: int, task_id: int) -> dict:
    """Read JSONL orchestration events for completion repair parsed/accepted signals.

    Event types in JSONL are lowercase (confirmed E41). We look for:
    - "repair_generated" with phase="completion_repair" → response arrived and JSON parsed
    - "repair_rejected" with phase="completion_repair" → repair was rejected (path guard etc.)
    """
    event_log = workspace_path / ".agent" / "events" / f"session_{session_id}_task_{task_id}.jsonl"
    result = {
        "completion_repair_response_parsed": False,
        "completion_repair_accepted": False,
        "completion_repair_rejected_reason": None,
        "pytest_failure_in_events": False,
    }

    if not event_log.exists():
        return result

    try:
        lines = event_log.read_text(encoding="utf-8").splitlines()
    except Exception:
        return result

    for line in lines:
        try:
            event = json.loads(line)
        except Exception:
            continue

        event_type = str(event.get("event_type", "")).lower()
        details = event.get("details", {}) or {}

        # Detect completion repair events — phase field distinguishes from bounded debug repair
        phase = str(details.get("phase", "")).lower()
        if event_type == "repair_generated" and "completion" in phase:
            result["completion_repair_response_parsed"] = True

        if event_type == "repair_rejected" and "completion" in phase:
            reason = (
                details.get("reason")
                or details.get("rejection_reason")
                or details.get("debug_repair_terminal_reason")
            )
            if reason:
                result["completion_repair_rejected_reason"] = str(reason)

        # Detect pytest failure evidence
        if "pytest" in str(details).lower() and "fail" in str(details).lower():
            result["pytest_failure_in_events"] = True
        if event_type in ("completion_verification_failed", "completion_repair_failed"):
            result["pytest_failure_in_events"] = True

    return result


def _check_false_success(workspace_path: Path, final_status: str) -> dict:
    """Check for false-success: session says completed but pytest actually fails."""
    result = {"false_success_evidence": False, "final_pytest_result": None, "pytest_output": ""}

    if final_status != "completed":
        return result

    venv_python = Path("/root/.openclaw/workspace/vault/projects/orchestrator/venv/bin/python3")
    try:
        proc = subprocess.run(
            [str(venv_python), "-m", "pytest", "-q", "--tb=short", "--no-header"],
            cwd=str(workspace_path),
            capture_output=True,
            text=True,
            timeout=60,
            env={
                "PATH": "/usr/bin:/bin",
                "HOME": "/root",
                "PYTHONPATH": str(workspace_path / "src"),
            },
        )
        output = (proc.stdout + proc.stderr).strip()[:600]
        result["pytest_output"] = output
        result["final_pytest_result"] = "pass" if proc.returncode == 0 else "fail"
        if proc.returncode != 0:
            result["false_success_evidence"] = True
    except Exception as exc:
        result["pytest_output"] = f"pytest error: {exc}"

    return result


def _classify(session: dict, db_signals: dict) -> str:
    status = str(session.get("status") or "").lower()
    if status == "completed":
        return "completed"

    err = str(
        session.get("last_alert_message")
        or session.get("error_message")
        or ""
    ).lower()

    # Use DB failure_category if available — most reliable signal
    failure_cat = str(db_signals.get("failure_category") or "").lower()
    if "plan_accepted_execution_failed" in failure_cat or "paef" in failure_cat:
        return "PLAN_ACCEPTED_EXECUTION_FAILED"
    if "planning_circuit_breaker" in failure_cat or "circuit_breaker" in failure_cat:
        return "planning_circuit_breaker"
    if "backend_capacity" in failure_cat:
        return "BACKEND_CAPACITY"

    # Fallback to session message
    if "bootstrap_contract" in err or "repair_candidate_rejected" in err:
        return "bootstrap_contract_PRCF"
    if "materialization_regression" in err:
        return "materialization_regression_PRCF"
    if "oscillation" in err or "root_cause_oscillation" in err:
        return "planning_circuit_breaker"
    if "missing_verification" in err:
        return "planning_circuit_breaker"
    if "json" in err and ("parse" in err or "error" in err):
        return "planning_json_parse_failure"
    if "debug_repair_budget_exhausted" in err or "execution failed" in err or "task1_execution" in err:
        return "PLAN_ACCEPTED_EXECUTION_FAILED"
    if "timed out" in err or "timeout" in err or "capacity" in err or "backend" in err:
        return "BACKEND_CAPACITY"
    if "planning failed" in err or "circuit" in err:
        return "planning_circuit_breaker"
    if db_signals.get("planning_circuit_breaker"):
        return "planning_circuit_breaker"
    return "other"


def _e51_in_process_check() -> bool:
    """Verify E51 telemetry fields and COMPLETION_REPAIR_BACKEND config are loaded."""
    import inspect

    from app.services.orchestration.phases.completion_flow import _attempt_completion_repair
    from app.config import Settings

    src = inspect.getsource(_attempt_completion_repair)

    checks = {
        "E51 completion_repair_prompt_chars field": "completion_repair_prompt_chars" in src,
        "E51 completion_repair_timed_out field": "completion_repair_timed_out" in src,
        "E51 completion_repair_fast_profile_selected": "completion_repair_fast_profile_selected" in src,
        "E51 _create_completion_repair_runtime import": "_create_completion_repair_runtime" in src,
        "E51 time.monotonic usage": "_cr_start_mono" in src,
        "E51 COMPLETION_REPAIR_BACKEND config": "COMPLETION_REPAIR_BACKEND" in (Settings.model_fields if hasattr(Settings, 'model_fields') else {}),
        "E51 completion_repair_exception_type field": "completion_repair_exception_type" in src,
        "E51 completion_repair_output_chars field": "completion_repair_output_chars" in src,
    }

    all_pass = True
    for label, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}: {label}")
        if not ok:
            all_pass = False

    # Check COMPLETION_REPAIR_BACKEND in live settings
    try:
        from app.config import settings as live_settings
        backend_val = getattr(live_settings, "COMPLETION_REPAIR_BACKEND", None)
        configured = bool(backend_val)
        print(f"  {'PASS' if configured else 'FAIL'}: COMPLETION_REPAIR_BACKEND set in live settings: {backend_val!r}")
        if not configured:
            all_pass = False
    except Exception as exc:
        print(f"  WARN: Could not read live settings: {exc}")

    return all_pass


def main() -> None:
    token = _get_token()

    # ── E51 in-process verification ─────────────────────────────────────────
    print("[E52] E51 in-process code verification:")
    if not _e51_in_process_check():
        print("  ERROR: E51 not loaded or COMPLETION_REPAIR_BACKEND not set. Restart worker.")
        sys.exit(1)

    try:
        result = subprocess.run(
            ["pgrep", "-f", "celery -A app.celery_app worker"],
            capture_output=True, text=True,
        )
        pids = result.stdout.strip().split()
        worker_pid = pids[0] if pids else "unknown"
    except Exception:
        worker_pid = "unknown"
    print(f"  Active worker PID: {worker_pid}")
    print()

    results: list[dict] = []
    ts = datetime.now(UTC).strftime("%Y%m%d%H%M")

    for i, task_spec in enumerate(TASK_CORPUS):
        tag = f"{ts}-{i+1:02d}"
        fixture = task_spec["fixture"]
        tid = task_spec["id"]
        print(f"\n[E52] ({i+1}/{len(TASK_CORPUS)}) {tid} fixture={fixture}", flush=True)

        try:
            ws = _fresh_workspace(fixture, tag)
        except Exception as exc:
            print(f"  workspace provision failed: {exc}")
            results.append({"id": tid, "fixture": fixture, "error": str(exc), "category": "infra_error"})
            continue

        session_id = None
        task_id = None
        try:
            proj = _api("POST", "projects", token, {
                "name": f"E52 {tid}",
                "description": f"E52 post-E51 completion repair fast-route validation {tid}",
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
                "name": f"e52-{task_spec['id'].lower()}-{tag}",
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

        db_signals = _db_completion_repair_signals(task_id)
        event_signals = _orchestration_event_signals(ws, session_id, task_id)
        category = _classify(final, db_signals)

        final_status = str(final.get("status", "unknown")).lower()
        false_success_signals = _check_false_success(ws, final_status)

        # Merge parsed/accepted from events
        cr_response_parsed = (
            db_signals.get("completion_repair_output_chars") is not None
            and (db_signals.get("completion_repair_output_chars") or 0) > 0
        ) or event_signals.get("completion_repair_response_parsed", False)

        cr_accepted = (
            event_signals.get("completion_repair_accepted", False)
            or (category == "completed" and db_signals.get("completion_repair_invoked", False))
        )

        rec = {
            "id": tid,
            "fixture": fixture,
            "task_id": task_id,
            "session_id": session_id,
            "status": final_status,
            "last_alert_message": str(final.get("last_alert_message") or "")[:300],
            "category": category,
            "execution_reached": db_signals.get("execution_reached", False),
            "pytest_failure": db_signals.get("pytest_failure_in_log", False) or event_signals.get("pytest_failure_in_events", False),
            "completion_repair_invoked": db_signals.get("completion_repair_invoked", False),
            "completion_repair_prompt_chars": db_signals.get("completion_repair_prompt_chars"),
            "completion_repair_timeout_seconds": db_signals.get("completion_repair_timeout_seconds"),
            "completion_repair_runtime_profile": db_signals.get("completion_repair_runtime_profile"),
            "completion_repair_duration_seconds": db_signals.get("completion_repair_duration_seconds"),
            "completion_repair_timed_out": db_signals.get("completion_repair_timed_out"),
            "completion_repair_output_chars": db_signals.get("completion_repair_output_chars"),
            "completion_repair_exception_type": db_signals.get("completion_repair_exception_type"),
            "completion_repair_fast_profile_selected": db_signals.get("completion_repair_fast_profile_selected"),
            "completion_repair_fast_profile_fallback": db_signals.get("completion_repair_fast_profile_fallback"),
            "completion_repair_response_parsed": cr_response_parsed,
            "completion_repair_accepted": cr_accepted,
            "completion_repair_rejected_reason": event_signals.get("completion_repair_rejected_reason"),
            "false_success_evidence": false_success_signals.get("false_success_evidence", False),
            "final_pytest_result": false_success_signals.get("final_pytest_result"),
        }
        results.append(rec)

        cr_inv = rec["completion_repair_invoked"]
        cr_dur = rec["completion_repair_duration_seconds"]
        cr_to = rec["completion_repair_timed_out"]
        cr_fast = rec["completion_repair_fast_profile_selected"]
        cr_fallback = rec["completion_repair_fast_profile_fallback"]
        cr_chars = rec["completion_repair_prompt_chars"]
        cr_out = rec["completion_repair_output_chars"]
        print(
            f"  → status={final_status} cat={category} "
            f"exec={rec['execution_reached']} pytest_fail={rec['pytest_failure']} "
            f"cr_invoked={cr_inv} "
            f"cr_prompt_chars={cr_chars} cr_dur={cr_dur}s cr_timed_out={cr_to} "
            f"cr_fast={cr_fast} cr_fallback={cr_fallback} "
            f"cr_out_chars={cr_out} cr_parsed={cr_response_parsed} cr_accepted={cr_accepted} "
            f"false_success={rec['false_success_evidence']}",
            flush=True,
        )

    OUTPUT_FILE.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\n[E52] Raw results → {OUTPUT_FILE}")

    # ── Aggregates ──────────────────────────────────────────────────────────
    valid = [r for r in results if r.get("category") not in ("infra_error", "dispatch_error")]
    total = len(valid)

    def _count(recs, key, val=True):
        return sum(1 for r in recs if r.get(key) == val)

    def _count_cat(recs, cat):
        return sum(1 for r in recs if r.get("category") == cat)

    completed_count = _count_cat(valid, "completed")
    paef_count = _count_cat(valid, "PLAN_ACCEPTED_EXECUTION_FAILED")
    backend_count = _count_cat(valid, "BACKEND_CAPACITY")
    circuit_count = _count_cat(valid, "planning_circuit_breaker")
    json_fail_count = _count_cat(valid, "planning_json_parse_failure")
    other_count = total - completed_count - paef_count - backend_count - circuit_count - json_fail_count

    cr_invoked_recs = [r for r in valid if r.get("completion_repair_invoked")]
    cr_invocation_count = len(cr_invoked_recs)
    cr_timeout_count = sum(1 for r in cr_invoked_recs if r.get("completion_repair_timed_out") is True)
    cr_success_response_count = sum(1 for r in cr_invoked_recs if (r.get("completion_repair_output_chars") or 0) > 0)
    cr_response_parsed_count = sum(1 for r in cr_invoked_recs if r.get("completion_repair_response_parsed"))
    cr_accepted_count = sum(1 for r in cr_invoked_recs if r.get("completion_repair_accepted"))
    fast_selected_count = sum(1 for r in cr_invoked_recs if r.get("completion_repair_fast_profile_selected") is True)
    fast_fallback_count = sum(1 for r in cr_invoked_recs if r.get("completion_repair_fast_profile_fallback") is True)

    prompt_chars_list = [r["completion_repair_prompt_chars"] for r in cr_invoked_recs if r.get("completion_repair_prompt_chars") is not None]
    duration_list = [r["completion_repair_duration_seconds"] for r in cr_invoked_recs if r.get("completion_repair_duration_seconds") is not None]
    output_chars_list = [r["completion_repair_output_chars"] for r in cr_invoked_recs if r.get("completion_repair_output_chars") is not None]

    avg_prompt_chars = round(sum(prompt_chars_list) / len(prompt_chars_list), 0) if prompt_chars_list else None
    avg_duration = round(sum(duration_list) / len(duration_list), 1) if duration_list else None
    false_success_count = _count(valid, "false_success_evidence")

    print("\n" + "=" * 75)
    print("[E52] AGGREGATES (vs E46 baseline: 6/6 timeout, 0% fast route)")
    print("=" * 75)
    print(f"  Total tasks:                                  {total}/10")
    print(f"  completed:                                    {completed_count}/10")
    print(f"  PLAN_ACCEPTED_EXECUTION_FAILED (PAEF):        {paef_count}/10  (E46 baseline: 6/10)")
    print(f"  planning_circuit_breaker:                     {circuit_count}/10  (E46 baseline: 4/10)")
    print(f"  BACKEND_CAPACITY:                             {backend_count}/10")
    print(f"  planning_json_parse_failure:                  {json_fail_count}/10")
    print(f"  other:                                        {other_count}/10")
    print()
    print("[E52] COMPLETION REPAIR TELEMETRY")
    print(f"  completion_repair_invocation_count:           {cr_invocation_count}/10  (E46 baseline: 6/10)")
    print(f"  completion_repair_timeout_count:              {cr_timeout_count}/{cr_invocation_count or 1}  (E46 baseline: 6/6 = 100%)")
    timeout_rate = f"{cr_timeout_count / cr_invocation_count:.0%}" if cr_invocation_count > 0 else "N/A"
    print(f"  completion_repair_timeout_rate:               {timeout_rate}  (E46 baseline: 100%)")
    print(f"  completion_repair_success_response_count:     {cr_success_response_count}/{cr_invocation_count or 1}")
    print(f"  completion_repair_response_parsed_count:      {cr_response_parsed_count}/{cr_invocation_count or 1}")
    print(f"  completion_repair_accepted_count:             {cr_accepted_count}/{cr_invocation_count or 1}")
    print(f"  completion_repair_avg_prompt_chars:           {avg_prompt_chars}  (E46: not logged)")
    print(f"  completion_repair_avg_duration_seconds:       {avg_duration}  (E46: 120s timeout only)")
    print(f"  completion_repair_output_chars_distribution:  {output_chars_list}")
    print(f"  fast_profile_selected_count:                  {fast_selected_count}/{cr_invocation_count or 1}  (E46: 0/6 — not implemented)")
    print(f"  fast_profile_fallback_count:                  {fast_fallback_count}/{cr_invocation_count or 1}")
    print(f"  false_success_count:                          {false_success_count}/10")
    print()

    print("[E52] PER-TASK RESULTS")
    print("=" * 75)
    hdr = (
        f"{'ID':<12} {'task_id':<8} {'sess':<6} {'status':<10} {'cat':<30} "
        f"{'exec':<5} {'pytest_f':<9} {'cr_inv':<7} {'cr_timed_out':<13} "
        f"{'cr_dur':<8} {'cr_pchars':<10} {'cr_ochars':<10} "
        f"{'fast':<5} {'fallb':<6} {'parsed':<7} {'accept':<7} {'false_s'}"
    )
    print(hdr)
    for r in valid:
        print(
            f"{r['id']:<12} {r.get('task_id','?'):<8} {r.get('session_id','?'):<6} "
            f"{r['status']:<10} {r['category']:<30} "
            f"{'Y' if r.get('execution_reached') else 'N':<5} "
            f"{'Y' if r.get('pytest_failure') else 'N':<9} "
            f"{'Y' if r.get('completion_repair_invoked') else 'N':<7} "
            f"{str(r.get('completion_repair_timed_out')):<13} "
            f"{str(r.get('completion_repair_duration_seconds')):<8} "
            f"{str(r.get('completion_repair_prompt_chars')):<10} "
            f"{str(r.get('completion_repair_output_chars')):<10} "
            f"{'Y' if r.get('completion_repair_fast_profile_selected') else 'N':<5} "
            f"{'Y' if r.get('completion_repair_fast_profile_fallback') else 'N':<6} "
            f"{'Y' if r.get('completion_repair_response_parsed') else 'N':<7} "
            f"{'Y' if r.get('completion_repair_accepted') else 'N':<7} "
            f"{'Y' if r.get('false_success_evidence') else 'N'}"
        )

    print()
    # ── Decision rule ───────────────────────────────────────────────────────
    print("[E52] DECISION RULE")
    print("=" * 75)
    if false_success_count > 0:
        decision = "REVERT_E51_ROUTE"
        reason = f"false_success_count={false_success_count} > 0 — safety violation"
    elif cr_invocation_count == 0:
        decision = "MONITOR_MORE"
        reason = "No completion repair invocations in this batch — tasks failed upstream"
    elif fast_fallback_count == cr_invocation_count and cr_invocation_count > 0:
        decision = "FIX_COMPLETION_REPAIR_BACKEND_CONFIG"
        reason = f"Fast profile fell back every time ({fast_fallback_count}/{cr_invocation_count}) — backend not available"
    elif cr_timeout_count == 0 and cr_success_response_count > 0:
        decision = "KEEP_E51_FAST_ROUTE"
        reason = f"Timeout rate dropped to 0/{cr_invocation_count} (E46: 6/6=100%) with responses arriving"
    elif cr_timeout_count < cr_invocation_count and cr_success_response_count > 0:
        decision = "KEEP_E51_FAST_ROUTE"
        reason = f"Timeout rate dropped to {cr_timeout_count}/{cr_invocation_count} vs E46 100%; responses arriving"
    elif cr_timeout_count == cr_invocation_count and avg_prompt_chars and avg_prompt_chars > 3000:
        decision = "INVESTIGATE_COMPLETION_REPAIR_PROMPT_REDUCTION"
        reason = f"Timeout rate unchanged ({cr_timeout_count}/{cr_invocation_count}), avg prompt chars={avg_prompt_chars} — likely prompt-size bound"
    elif cr_timeout_count == cr_invocation_count:
        decision = "INVESTIGATE_COMPLETION_REPAIR_PROMPT_REDUCTION"
        reason = f"Timeout rate unchanged ({cr_timeout_count}/{cr_invocation_count}) — investigate prompt size or capacity"
    elif cr_success_response_count > 0 and cr_accepted_count == 0:
        decision = "INVESTIGATE_COMPLETION_REPAIR_QUALITY"
        reason = "Responses arriving but no repairs accepted — quality issue"
    else:
        decision = "MONITOR_MORE"
        reason = f"Mixed signals: timeout={cr_timeout_count}/{cr_invocation_count} success_response={cr_success_response_count}/{cr_invocation_count}"

    print(f"  Decision: {decision}")
    print(f"  Reason:   {reason}")

    return decision


if __name__ == "__main__":
    main()
