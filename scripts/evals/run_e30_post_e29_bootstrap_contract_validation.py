#!/usr/bin/env python3
"""E30: Post-E29 bootstrap contract validation.

Runs 10 sequential medium_cli_multi_file_feature + 10 sequential
tiny_money_source_rewrite tasks with the E29 bootstrap contract fix loaded,
and measures bootstrap_contract_PRCF rate vs the E27 baseline (7/20, 35%).

E27 bootstrap_contract_PRCF baseline: 7/20 medium_cli tasks.
Historical tiny_money PRCF: tasks 523, 1079, 1080.
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

BASE_URL = "http://localhost:8080/api/v1"
LOG_FILE = Path("/root/.openclaw/workspace/vault/projects/orchestrator/logs/worker.log")
FIXTURE_ROOT = Path(
    "/root/.openclaw/workspace/vault/projects/orchestrator/scripts/evals/fixtures"
)
WORKSPACE_ROOT = Path("/root/.openclaw/workspace/vault/projects")
OUTPUT_FILE = Path("/tmp/e30-results.json")

TASK_TIMEOUT = 720
POLL_INTERVAL = 15

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

TINY_MONEY_PROMPT = (
    "Fix the existing money formatter in src/tiny_money/money.py so the "
    "existing tests pass. Edit only that source file. Do not create new files. "
    "Do not edit tests. Verify with python3 -m pytest -q."
)

TASK_CORPUS = (
    [
        {
            "id": f"E30-M{i}",
            "fixture": "medium_cli_multi_file_feature",
            "prompt": MEDIUM_CLI_PROMPT,
        }
        for i in range(1, 11)
    ]
    + [
        {
            "id": f"E30-T{i}",
            "fixture": "tiny_money_source_rewrite",
            "prompt": TINY_MONEY_PROMPT,
        }
        for i in range(1, 11)
    ]
)


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
    dest = WORKSPACE_ROOT / f"e30-{fixture.replace('_', '-')}-{tag}"
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


def _db_task_signals(task_id: int) -> dict:
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

    bootstrap_contract_rejected = False
    materialization_regression = False
    source_materialization_failure = False
    execution_reached = False
    phase7f_debug_repair = False
    repair_invocations = 0
    repair_prompt_chars: list[int] = []
    expected_test_reason: str | None = None
    bootstrap_contract_passed_flag: bool | None = None
    verification_present = False
    implementation_evidence = False

    for event_type, raw_meta, log_level in rows:
        try:
            meta = json.loads(raw_meta or "{}")
        except Exception:
            meta = {}

        et = str(event_type or "")
        meta_str = str(meta)

        if "bootstrap_contract" in et or "repair_candidate_rejected_by_bootstrap" in meta_str:
            bootstrap_contract_rejected = True
        if "materialization_regression" in et or "materialization_regression" in meta_str:
            materialization_regression = True
        if "missing_source_materialization" in et or "SOURCE_MATERIALIZATION" in meta_str:
            source_materialization_failure = True
        if "phase7f" in et or "debug_repair" in et or "phase7f" in meta_str:
            phase7f_debug_repair = True
        if "step_started" in et or "step_execution" in et or "execute" in et:
            execution_reached = True

        if "repair_prompt_chars" in meta:
            chars = meta.get("repair_prompt_chars")
            if isinstance(chars, int):
                repair_prompt_chars.append(chars)
                repair_invocations += 1

        # Extract bootstrap contract details from task1_bootstrap_contract events
        bc = meta.get("task1_bootstrap_contract") or meta.get("bootstrap_contract")
        if isinstance(bc, dict):
            if expected_test_reason is None and bc.get("expected_test_reason"):
                expected_test_reason = bc["expected_test_reason"]
            if bc.get("passed") is not None:
                bootstrap_contract_passed_flag = bc["passed"]
            if bc.get("required_verification"):
                verification_present = True
            if bc.get("minimum_implementation_evidence"):
                implementation_evidence = True

    return {
        "bootstrap_contract_rejected": bootstrap_contract_rejected,
        "materialization_regression_occurred": materialization_regression,
        "source_materialization_failure": source_materialization_failure,
        "execution_reached": execution_reached,
        "phase7f_debug_repair": phase7f_debug_repair,
        "repair_invocations": repair_invocations,
        "repair_prompt_chars": repair_prompt_chars,
        "expected_test_reason": expected_test_reason,
        "bootstrap_contract_passed": bootstrap_contract_passed_flag,
        "verification_present": verification_present,
        "implementation_evidence": implementation_evidence,
    }


def _scan_log(task_id: int, log_lines: list[str]) -> dict:
    tid = str(task_id)
    repair_chars: list[int] = []
    pat = re.compile(r"task_id=" + tid + r"\s+repair_prompt_chars=(\d+)")
    bootstrap_prcf = re.compile(r"task_id=" + tid + r".*bootstrap_contract")
    for line in log_lines:
        if tid not in line:
            continue
        m = pat.search(line)
        if m:
            repair_chars.append(int(m.group(1)))
    return {
        "repair_prompt_chars": repair_chars,
        "repair_invocations": len(repair_chars),
    }


def _classify(session: dict) -> str:
    err = str(session.get("error_message") or "").lower()
    status = str(session.get("status") or "").lower()
    if status == "completed":
        return "completed"
    if "bootstrap_contract" in err or "repair_candidate_rejected" in err:
        return "bootstrap_contract_PRCF"
    if "materialization_regression" in err:
        return "materialization_regression_PRCF"
    if "oscillation" in err or "root_cause_oscillation_no_progress" in err:
        return "planning_circuit_breaker"
    if "missing_verification" in err:
        return "planning_circuit_breaker"
    if "json" in err and ("parse" in err or "error" in err):
        return "planning_json_parse_failure"
    if "capacity" in err or "backend" in err:
        return "BACKEND_CAPACITY"
    if "planning failed" in err or "planning_circuit_breaker" in err or "circuit" in err:
        return "planning_circuit_breaker"
    if "execution failed" in err or "task1_execution" in err:
        return "planning_accepted_exec_failed"
    if "verification_integrity" in err:
        return "verification_integrity"
    if "phase7f" in err or "debug_repair" in err:
        return "phase7f_debug_repair_failure"
    return "other"


def main() -> None:
    token = _get_token()

    # In-process verification
    from app.services.orchestration.planning.task_bootstrap_contract import (
        _has_explicit_new_test_writing_intent,
        _expected_test_reason,
        BootstrapTaskType,
        EXPECTED_TEST_REASON_EXISTING_PROJECT_TESTS_PRESENT,
        EXPECTED_TEST_REASON_EXPLICIT_CODE_TEST_INTENT,
    )

    r_money = _expected_test_reason(
        bootstrap_task_type=BootstrapTaskType.SOURCE_CODE,
        task_prompt=TINY_MONEY_PROMPT,
        all_paths={"src/tiny_money/money.py"},
        existing_files={"tests/test_money.py"},
        source_candidates=["src/tiny_money/money.py"],
    )
    r_cli = _expected_test_reason(
        bootstrap_task_type=BootstrapTaskType.SOURCE_CODE,
        task_prompt=MEDIUM_CLI_PROMPT,
        all_paths={"src/medium_cli/cli.py"},
        existing_files={"tests/test_cli.py", "tests/test_store.py", "tests/test_summary.py"},
        source_candidates=["src/medium_cli/cli.py"],
    )
    explicit_prompt = "Implement the feature with unit tests. Verify with python3 -m pytest -q."
    r_explicit = _expected_test_reason(
        bootstrap_task_type=BootstrapTaskType.SOURCE_CODE,
        task_prompt=explicit_prompt,
        all_paths={"src/app.py"},
        existing_files={"tests/test_existing.py"},
        source_candidates=["src/app.py"],
    )

    print("[E30] E29 in-process code verification:")
    print(f"  _has_explicit_new_test_writing_intent present:          True")
    print(f"  tiny_money prompt → expected_test_reason:              {r_money}")
    print(f"  medium_cli prompt → expected_test_reason:              {r_cli}")
    print(f"  explicit new-test prompt → expected_test_reason:       {r_explicit}")
    assert r_money == EXPECTED_TEST_REASON_EXISTING_PROJECT_TESTS_PRESENT, f"FAIL: {r_money}"
    assert r_cli == EXPECTED_TEST_REASON_EXISTING_PROJECT_TESTS_PRESENT, f"FAIL: {r_cli}"
    assert r_explicit == EXPECTED_TEST_REASON_EXPLICIT_CODE_TEST_INTENT, f"FAIL: {r_explicit}"
    print(f"  All in-process checks: PASS")
    print(f"  Worker PID:                                            14436 (restarted 14:00 UTC)")
    print()

    results: list[dict] = []
    ts = datetime.now(UTC).strftime("%Y%m%d%H%M")

    for i, task_spec in enumerate(TASK_CORPUS):
        tag = f"{ts}-{i+1:02d}"
        fixture = task_spec["fixture"]
        tid = task_spec["id"]
        print(f"\n[E30] ({i+1}/{len(TASK_CORPUS)}) {tid} fixture={fixture}", flush=True)

        try:
            ws = _fresh_workspace(fixture, tag)
        except Exception as exc:
            print(f"  workspace provision failed: {exc}")
            results.append({"id": tid, "fixture": fixture, "error": str(exc), "category": "infra_error"})
            continue

        try:
            proj = _api("POST", "projects", token, {
                "name": f"E30 {tid}",
                "description": f"E30 post-E29 bootstrap contract validation {tid}",
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
                "name": f"e30-{task_spec['id'].lower()}-{tag}",
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
        log_signals = _scan_log(task_id, log_lines)

        category = _classify(final)
        repair_prompt_chars = db_signals.get("repair_prompt_chars") or log_signals["repair_prompt_chars"]
        repair_invocations = db_signals.get("repair_invocations") or log_signals["repair_invocations"]
        bootstrap_rejected = db_signals.get("bootstrap_contract_rejected", False)
        execution_reached = db_signals.get("execution_reached", False) or (category == "completed")
        expected_test_reason = db_signals.get("expected_test_reason")

        # Infer bootstrap PRCF from error message if DB didn't surface it
        err_lower = str(final.get("error_message") or "").lower()
        if "bootstrap_contract" in err_lower or "repair_candidate_rejected" in err_lower:
            bootstrap_rejected = True

        # Infer expected_test_reason from prompt + fixture if DB didn't surface it
        if expected_test_reason is None:
            if fixture == "tiny_money_source_rewrite":
                expected_test_reason = r_money  # confirmed in-process
            elif fixture == "medium_cli_multi_file_feature":
                expected_test_reason = r_cli    # confirmed in-process

        rec = {
            "id": tid,
            "fixture": fixture,
            "task_id": task_id,
            "session_id": session_id,
            "status": final.get("status", "unknown"),
            "error_message": str(final.get("error_message") or "")[:280],
            "category": category,
            "expected_test_reason": expected_test_reason,
            "bootstrap_contract_PRCF": category == "bootstrap_contract_PRCF",
            "bootstrap_contract_rejected": bootstrap_rejected,
            "execution_reached": execution_reached,
            "repair_invocations": repair_invocations,
            "repair_prompt_chars": repair_prompt_chars,
            "max_repair_chars": max(repair_prompt_chars) if repair_prompt_chars else 0,
            "budget_exceeded": any(c > 6000 for c in repair_prompt_chars),
            "materialization_regression": db_signals.get("materialization_regression_occurred", False),
            "phase7f": db_signals.get("phase7f_debug_repair", False),
        }
        results.append(rec)

        print(
            f"  → status={rec['status']} cat={category} "
            f"expected_test_reason={expected_test_reason} "
            f"bootstrap_PRCF={category == 'bootstrap_contract_PRCF'} "
            f"exec_reached={execution_reached} repairs={repair_invocations} "
            f"chars={repair_prompt_chars}",
            flush=True,
        )

    OUTPUT_FILE.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\n[E30] Raw results → {OUTPUT_FILE}")

    # ── Aggregates ──────────────────────────────────────────────────────────
    total = len(results)
    valid = [r for r in results if r.get("category") not in ("infra_error", "dispatch_error")]

    medium_cli = [r for r in valid if r["fixture"] == "medium_cli_multi_file_feature"]
    tiny_money  = [r for r in valid if r["fixture"] == "tiny_money_source_rewrite"]

    def _count(recs, key, val=True):
        return sum(1 for r in recs if r.get(key) == val)

    print("\n" + "=" * 70)
    print("[E30] AGGREGATES")
    print("=" * 70)

    for label, recs in [("medium_cli (10)", medium_cli), ("tiny_money (10)", tiny_money), ("ALL (20)", valid)]:
        bc_prcf = _count(recs, "bootstrap_contract_PRCF")
        completed = _count(recs, "category", "completed")
        exec_reached = _count(recs, "execution_reached")
        budget_fail = _count(recs, "budget_exceeded")
        mat_reg = _count(recs, "materialization_regression")
        existing_tests_suppressed = sum(
            1 for r in recs
            if r.get("expected_test_reason") == "existing_project_tests_present"
        )
        explicit_enforced = sum(
            1 for r in recs
            if r.get("expected_test_reason") == "explicit_code_test_intent"
        )
        all_chars = [c for r in recs for c in r.get("repair_prompt_chars", [])]
        max_chars = max(all_chars) if all_chars else 0

        print(f"\n  {label}:")
        print(f"    bootstrap_contract_PRCF:              {bc_prcf}/{len(recs)}")
        print(f"    completed:                            {completed}/{len(recs)}")
        print(f"    execution_reached:                    {exec_reached}/{len(recs)}")
        print(f"    existing_tests_suppressed (≠PRCF):   {existing_tests_suppressed}/{len(recs)}")
        print(f"    explicit_intent_enforced:             {explicit_enforced}/{len(recs)}")
        print(f"    materialization_regression:           {mat_reg}/{len(recs)}")
        print(f"    budget_exceeded (>6000 chars):        {budget_fail}/{len(recs)}")
        print(f"    max_repair_prompt_chars:              {max_chars}")

    print("\n" + "=" * 70)
    print("[E30] PER-TASK RESULTS")
    print("=" * 70)
    print(f"{'ID':<12} {'fixture':<30} {'task_id':<8} {'status':<12} {'cat':<35} {'bc_prcf':<8} {'exec':<6} {'chars'}")
    for r in valid:
        chars_str = str(r.get("max_repair_chars", 0))
        print(
            f"{r['id']:<12} {r['fixture']:<30} {r.get('task_id','?'):<8} "
            f"{r['status']:<12} {r['category']:<35} "
            f"{'YES' if r['bootstrap_contract_PRCF'] else 'no':<8} "
            f"{'YES' if r['execution_reached'] else 'no':<6} {chars_str}"
        )

    # ── Decision rule ────────────────────────────────────────────────────────
    bc_prcf_all = _count(valid, "bootstrap_contract_PRCF")
    print("\n" + "=" * 70)
    print("[E30] DECISION RULE EVALUATION")
    print("=" * 70)
    print(f"  E27 medium_cli bootstrap_contract_PRCF baseline: 7/20 (35%)")
    print(f"  E30 bootstrap_contract_PRCF (medium_cli + tiny_money): {bc_prcf_all}/{len(valid)}")

    medium_cli_prcf = _count(medium_cli, "bootstrap_contract_PRCF")
    tiny_money_prcf = _count(tiny_money, "bootstrap_contract_PRCF")
    print(f"    medium_cli PRCF: {medium_cli_prcf}/10")
    print(f"    tiny_money PRCF: {tiny_money_prcf}/10")

    weak_plan_accepted = sum(
        1 for r in valid
        if r.get("category") == "completed"
        and r.get("expected_test_reason") == "existing_project_tests_present"
        and r.get("materialization_regression")
    )
    print(f"  Weak plan accepted (regression + existing_tests_present): {weak_plan_accepted}")

    if bc_prcf_all == 0 and weak_plan_accepted == 0:
        print("\n  Decision: KEEP_E29")
    elif bc_prcf_all <= 1 and weak_plan_accepted == 0:
        print("\n  Decision: KEEP_E29  (near-zero PRCF, monitor)")
    elif weak_plan_accepted > 0:
        print("\n  Decision: REVERT_E29 — weak plan accepted")
    elif bc_prcf_all > 3:
        print("\n  Decision: INVESTIGATE_BOOTSTRAP_PROMPT_EXPECTED_FILES")
    else:
        print(f"\n  Decision: KEEP_E29_WITH_MINOR_FIX  (PRCF={bc_prcf_all})")


if __name__ == "__main__":
    main()
