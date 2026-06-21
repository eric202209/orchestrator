#!/usr/bin/env python3
"""E35: Post-E34 context injection validation.

Runs 10 sequential medium_cli_multi_file_feature tasks with E34 code loaded
(top-5 TEST CONTRACT SUMMARY + TEST CONTRACT SUMMARY in debug repair) and measures:

- PLAN_ACCEPTED_EXECUTION_FAILED rate (target: ≤ 2/10, down from 4/10 in E30)
- TaskStore.summary() implementation rate (target: ≥ 3/4 of exec-failed cases)
- format_summary(total, completed) signature correctness
- format_task_line preservation
- Debug repair invocation and TEST CONTRACT SUMMARY presence

E30 baseline (medium_cli, 10 tasks):
- completed: 1/10
- PLAN_ACCEPTED_EXECUTION_FAILED: 4/10
- BACKEND_CAPACITY/TASK_SUMMARY timeout: 3/10
- planning_json_parse_failure: 1/10
- oscillation: 1/10
"""

from __future__ import annotations

import ast
import json
import re
import shutil
import subprocess
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
OUTPUT_FILE = Path("/tmp/e35-results.json")

TASK_TIMEOUT = 780
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
        "id": f"E35-M{i}",
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
    dest = WORKSPACE_ROOT / f"e35-{fixture.replace('_', '-')}-{tag}"
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
        # log_entries has task_id directly; columns are level/message/log_metadata (not event_type/log_level)
        log_rows = conn.execute(
            "SELECT level, message, log_metadata FROM log_entries WHERE task_id=? ORDER BY id",
            (task_id,),
        ).fetchall()
        # task_executions tracks execution attempts and failure_category
        exec_rows = conn.execute(
            "SELECT attempt_number, status, failure_category FROM task_executions WHERE task_id=? ORDER BY id",
            (task_id,),
        ).fetchall()
        conn.close()
    except Exception:
        return {}

    bootstrap_contract_rejected = False
    execution_reached = len(exec_rows) > 0
    phase7f_debug_repair = False
    repair_invocations = 0
    repair_prompt_chars: list[int] = []
    debug_repair_count = 0
    planning_repair_chars: list[int] = []
    failure_category = exec_rows[-1][2] if exec_rows else None
    attempt_count = len(exec_rows)

    for level, msg, raw_meta in log_rows:
        try:
            meta = json.loads(raw_meta or "{}")
        except Exception:
            meta = {}

        msg_str = str(msg or "").lower()
        meta_str = str(meta).lower()

        if "bootstrap_contract" in msg_str or "repair_candidate_rejected_by_bootstrap" in meta_str:
            bootstrap_contract_rejected = True
        if "phase7f_debug_repair" in meta_str or "debug_repair_direct" in msg_str:
            phase7f_debug_repair = True
            if "attempting direct structured repair" in msg_str:
                debug_repair_count += 1
        if "executing" in msg_str and "step" in msg_str:
            execution_reached = True

        if "repair_prompt_chars" in meta:
            chars = meta.get("repair_prompt_chars")
            if isinstance(chars, int):
                planning_repair_chars.append(chars)
        if "debug_repair_prompt_chars" in meta:
            chars = meta.get("debug_repair_prompt_chars")
            if isinstance(chars, int):
                repair_prompt_chars.append(chars)
                repair_invocations += 1

    return {
        "bootstrap_contract_rejected": bootstrap_contract_rejected,
        "execution_reached": execution_reached,
        "phase7f_debug_repair": phase7f_debug_repair,
        "debug_repair_count": debug_repair_count,
        "repair_invocations": repair_invocations,
        "repair_prompt_chars": repair_prompt_chars,
        "planning_repair_chars": planning_repair_chars,
        "failure_category": failure_category,
        "attempt_count": attempt_count,
    }


def _inspect_workspace(workspace_path: Path) -> dict:
    """Inspect the post-task workspace to detect what was implemented."""
    store_py = workspace_path / "src" / "medium_cli" / "store.py"
    formatting_py = workspace_path / "src" / "medium_cli" / "formatting.py"
    cli_py = workspace_path / "src" / "medium_cli" / "cli.py"

    result = {
        "store_summary_implemented": False,
        "store_summary_raises_not_implemented": True,
        "format_summary_correct_signature": False,
        "format_summary_raises_not_implemented": True,
        "format_task_line_preserved": False,
        "pytest_passed": False,
        "pytest_output": "",
        "store_py_exists": store_py.exists(),
        "formatting_py_exists": formatting_py.exists(),
    }

    # Inspect store.py
    if store_py.exists():
        try:
            store_text = store_py.read_text(encoding="utf-8")
            # summary() implemented if it exists and doesn't just raise NotImplementedError
            if "def summary(" in store_text:
                if "raise NotImplementedError" in store_text:
                    # Check if it's the ONLY content of summary()
                    try:
                        tree = ast.parse(store_text)
                        for node in ast.walk(tree):
                            if isinstance(node, ast.FunctionDef) and node.name == "summary":
                                body = node.body
                                if not (len(body) == 1 and isinstance(body[0], ast.Raise)):
                                    result["store_summary_implemented"] = True
                                    result["store_summary_raises_not_implemented"] = False
                    except SyntaxError:
                        pass
                else:
                    result["store_summary_implemented"] = True
                    result["store_summary_raises_not_implemented"] = False
        except Exception:
            pass

    # Inspect formatting.py
    if formatting_py.exists():
        try:
            fmt_text = formatting_py.read_text(encoding="utf-8")

            # Check format_task_line preserved
            if "def format_task_line(" in fmt_text:
                if "task: Task" in fmt_text or "task," in fmt_text or "task: " in fmt_text:
                    result["format_task_line_preserved"] = True

            # Check format_summary signature
            if "def format_summary(" in fmt_text:
                # Correct: format_summary(total: int, completed: int) or format_summary(total, completed)
                # Wrong: format_summary(store: TaskStore) or format_summary(store)
                if "raise NotImplementedError" not in fmt_text or "def format_summary" in fmt_text:
                    # Try to extract actual signature
                    try:
                        tree = ast.parse(fmt_text)
                        for node in ast.walk(tree):
                            if isinstance(node, ast.FunctionDef) and node.name == "format_summary":
                                args = node.args
                                arg_names = [a.arg for a in args.args]
                                body = node.body
                                # Correct: takes (total, completed) not (store,)
                                if "total" in arg_names and "completed" in arg_names:
                                    result["format_summary_correct_signature"] = True
                                    if not (len(body) == 1 and isinstance(body[0], ast.Raise)):
                                        result["format_summary_raises_not_implemented"] = False
                                elif "store" not in arg_names:
                                    # Some other signature
                                    result["format_summary_correct_signature"] = False
                                # If 'store' is the only arg, it's wrong
                    except SyntaxError:
                        pass
        except Exception:
            pass

    # Run pytest to check if tests pass
    venv_python = Path("/root/.openclaw/workspace/vault/projects/orchestrator/venv/bin/python3")
    try:
        proc = subprocess.run(
            [str(venv_python), "-m", "pytest", "-q", "--tb=no", "--no-header"],
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
        result["pytest_output"] = (proc.stdout + proc.stderr).strip()[:400]
        result["pytest_passed"] = proc.returncode == 0
    except Exception as exc:
        result["pytest_output"] = f"pytest error: {exc}"

    return result


def _classify(session: dict) -> str:
    # sessions.last_alert_message is the correct terminal reason field (not error_message)
    err = str(
        session.get("last_alert_message")
        or session.get("error_message")
        or ""
    ).lower()
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
    if "debug_repair_budget_exhausted" in err or "debug repair" in err:
        return "PLAN_ACCEPTED_EXECUTION_FAILED"
    if "execution failed" in err or "task1_execution" in err:
        return "PLAN_ACCEPTED_EXECUTION_FAILED"
    if "timed out" in err or "timeout" in err or "capacity" in err or "backend" in err:
        return "BACKEND_CAPACITY"
    if "planning failed" in err or "planning_circuit_breaker" in err or "circuit" in err:
        return "planning_circuit_breaker"
    if "timeout" in err and ("task_summary" in err or "summary" in err):
        return "BACKEND_CAPACITY"
    if "debug_repair_budget" in err or "debug repair" in err:
        return "PLAN_ACCEPTED_EXECUTION_FAILED"
    if "verification_integrity" in err:
        return "verification_integrity"
    if "phase7f" in err or "debug_repair" in err:
        return "PLAN_ACCEPTED_EXECUTION_FAILED"
    return "other"


def main() -> None:
    token = _get_token()

    # In-process E34 verification
    print("[E35] E34 in-process code verification:")
    from app.services.project.source_imports import _expected_behavior_lines, render_python_test_contract_summary
    from app.services.orchestration.context.assembly import assemble_debugging_prompt
    import inspect

    src_eblines = inspect.getsource(_expected_behavior_lines)
    src_render = inspect.getsource(render_python_test_contract_summary)
    src_debug = inspect.getsource(assemble_debugging_prompt)

    i1_ok = ">= 5" in src_eblines and "[:5]" in src_render
    i2_ok = "python_test_source_context_from_tests" in src_debug and "max_chars=600" in src_debug

    print(f"  E33-I1 (_expected_behavior_lines >= 5, render [:5]):    {'PASS' if i1_ok else 'FAIL'}")
    print(f"  E33-I2 (debug repair TEST CONTRACT SUMMARY injection):  {'PASS' if i2_ok else 'FAIL'}")

    if not i1_ok or not i2_ok:
        print("  ERROR: E34 not loaded. Worker restart required before eval.")
        sys.exit(1)

    # Verify planning TEST CONTRACT SUMMARY has 5 lines for medium_cli fixture
    from app.services.project.source_imports import python_test_source_context_from_tests
    fixture_dir = FIXTURE_ROOT / "medium_cli_multi_file_feature"
    planning_summary = python_test_source_context_from_tests(fixture_dir, max_chars=2200)
    store_summary_visible = "store.summary() should equal (3, 2)" in planning_summary
    if "Expected behavior:" in planning_summary:
        behavior_section = planning_summary.split("Expected behavior:")[1]
        behavior_items = [
            l.strip() for l in behavior_section.splitlines()
            if l.strip().startswith("-") and "truncated" not in l.lower()
        ]
        behavior_count = len(behavior_items)
    else:
        behavior_count = 0

    print(f"  Planning behavior line count (medium_cli fixture):     {behavior_count} (target: 5)")
    print(f"  store.summary() visible in planning context:           {store_summary_visible}")

    new_worker_pid = None
    try:
        import subprocess as sp
        result = sp.run(["pgrep", "-f", "celery -A app.celery_app worker"], capture_output=True, text=True)
        pids = result.stdout.strip().split()
        new_worker_pid = pids[0] if pids else "unknown"
    except Exception:
        pass
    print(f"  Active worker PID:                                     {new_worker_pid}")
    print()

    results: list[dict] = []
    ts = datetime.now(UTC).strftime("%Y%m%d%H%M")

    for i, task_spec in enumerate(TASK_CORPUS):
        tag = f"{ts}-{i+1:02d}"
        fixture = task_spec["fixture"]
        tid = task_spec["id"]
        print(f"\n[E35] ({i+1}/{len(TASK_CORPUS)}) {tid} fixture={fixture}", flush=True)

        try:
            ws = _fresh_workspace(fixture, tag)
        except Exception as exc:
            print(f"  workspace provision failed: {exc}")
            results.append({"id": tid, "fixture": fixture, "error": str(exc), "category": "infra_error"})
            continue

        try:
            proj = _api("POST", "projects", token, {
                "name": f"E35 {tid}",
                "description": f"E35 post-E34 context validation {tid}",
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
                "name": f"e35-{task_spec['id'].lower()}-{tag}",
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
        category = _classify(final)

        repair_prompt_chars = db_signals.get("repair_prompt_chars", [])
        repair_invocations = db_signals.get("repair_invocations", 0)
        execution_reached = db_signals.get("execution_reached", False) or (category == "completed")
        phase7f = db_signals.get("phase7f_debug_repair", False)

        # Inspect workspace for implementation quality
        ws_signals = _inspect_workspace(ws)

        rec = {
            "id": tid,
            "fixture": fixture,
            "task_id": task_id,
            "session_id": session_id,
            "status": final.get("status", "unknown"),
            "error_message": str(final.get("error_message") or "")[:280],
            "category": category,
            "execution_reached": execution_reached,
            "phase7f_debug_repair": phase7f,
            "repair_invocations": repair_invocations,
            "repair_prompt_chars": repair_prompt_chars,
            "max_repair_chars": max(repair_prompt_chars) if repair_prompt_chars else 0,
            "budget_exceeded": any(c > 6000 for c in repair_prompt_chars),
            # E34-specific signals
            "store_summary_implemented": ws_signals["store_summary_implemented"],
            "store_summary_raises_not_implemented": ws_signals["store_summary_raises_not_implemented"],
            "format_summary_correct_signature": ws_signals["format_summary_correct_signature"],
            "format_task_line_preserved": ws_signals["format_task_line_preserved"],
            "pytest_passed": ws_signals["pytest_passed"],
            "pytest_output": ws_signals["pytest_output"],
        }
        results.append(rec)

        print(
            f"  → status={rec['status']} cat={category} "
            f"exec={execution_reached} phase7f={phase7f} repairs={repair_invocations} "
            f"chars={repair_prompt_chars} "
            f"store.summary={'IMPL' if ws_signals['store_summary_implemented'] else 'STUB'} "
            f"fmt_sig={'OK' if ws_signals['format_summary_correct_signature'] else 'BAD'} "
            f"fmt_task_line={'OK' if ws_signals['format_task_line_preserved'] else 'MISSING'} "
            f"pytest={'PASS' if ws_signals['pytest_passed'] else 'FAIL'}",
            flush=True,
        )

    OUTPUT_FILE.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\n[E35] Raw results → {OUTPUT_FILE}")

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
    oscillation_count = _count_cat(valid, "planning_circuit_breaker")
    json_fail_count = _count_cat(valid, "planning_json_parse_failure")
    prcf_count = _count_cat(valid, "bootstrap_contract_PRCF")
    other_count = total - completed_count - paef_count - backend_count - oscillation_count - json_fail_count - prcf_count

    exec_reached_count = _count(valid, "execution_reached")
    phase7f_count = _count(valid, "phase7f_debug_repair")
    store_impl_count = _count(valid, "store_summary_implemented")
    fmt_sig_ok_count = _count(valid, "format_summary_correct_signature")
    fmt_task_preserved_count = _count(valid, "format_task_line_preserved")
    pytest_pass_count = _count(valid, "pytest_passed")
    budget_fail_count = _count(valid, "budget_exceeded")
    all_chars = [c for r in valid for c in r.get("repair_prompt_chars", [])]
    max_chars = max(all_chars) if all_chars else 0

    print("\n" + "=" * 70)
    print("[E35] AGGREGATES (vs E30 baseline)")
    print("=" * 70)
    print(f"  Total tasks:                        {total}/10")
    print(f"  completed:                          {completed_count}/10  (E30: 1/10)")
    print(f"  PLAN_ACCEPTED_EXECUTION_FAILED:     {paef_count}/10  (E30: 4/10, target: ≤2)")
    print(f"  BACKEND_CAPACITY/TASK_SUMMARY:      {backend_count}/10  (E30: 3/10)")
    print(f"  planning_circuit_breaker:           {oscillation_count}/10  (E30: 1/10)")
    print(f"  planning_json_parse_failure:        {json_fail_count}/10  (E30: 1/10)")
    print(f"  bootstrap_contract_PRCF:            {prcf_count}/10  (E30: 0/10)")
    print(f"  other:                              {other_count}/10")
    print()
    print(f"  execution_reached:                  {exec_reached_count}/10  (E30: 7/10)")
    print(f"  debug_repair_invoked (phase7f):     {phase7f_count}/10")
    print()
    print("[E35] E34-SPECIFIC SIGNALS")
    print(f"  store.summary() implemented:        {store_impl_count}/10")
    print(f"  format_summary(total,completed) OK: {fmt_sig_ok_count}/10")
    print(f"  format_task_line preserved:         {fmt_task_preserved_count}/10")
    print(f"  pytest passed in workspace:         {pytest_pass_count}/10")
    print()
    print(f"  budget_exceeded (>6000):            {budget_fail_count}/10  (target: 0)")
    print(f"  max_repair_prompt_chars:            {max_chars}  (limit: 6000)")

    print("\n" + "=" * 70)
    print("[E35] PER-TASK RESULTS")
    print("=" * 70)
    hdr = f"{'ID':<12} {'task_id':<8} {'status':<10} {'category':<35} {'exec':<5} {'7f':<5} {'store':<6} {'fmt':<5} {'pytest'}"
    print(hdr)
    for r in valid:
        print(
            f"{r['id']:<12} {r.get('task_id','?'):<8} "
            f"{r['status']:<10} {r['category']:<35} "
            f"{'Y' if r['execution_reached'] else 'N':<5} "
            f"{'Y' if r['phase7f_debug_repair'] else 'N':<5} "
            f"{'IMPL' if r['store_summary_implemented'] else 'STUB':<6} "
            f"{'OK' if r['format_summary_correct_signature'] else 'BAD':<5} "
            f"{'PASS' if r['pytest_passed'] else 'FAIL'}"
        )

    # ── Decision rule ────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("[E35] DECISION RULE EVALUATION")
    print("=" * 70)
    print(f"  E30 PLAN_ACCEPTED_EXECUTION_FAILED baseline: 4/10 (40%)")
    print(f"  E35 PLAN_ACCEPTED_EXECUTION_FAILED:          {paef_count}/10 ({paef_count*10}%)")
    print(f"  E30 completed baseline:                      1/10 (10%)")
    print(f"  E35 completed:                               {completed_count}/10 ({completed_count*10}%)")
    print(f"  store.summary() implementation rate:         {store_impl_count}/10")
    print(f"  format_summary correct signature rate:       {fmt_sig_ok_count}/10")
    print()

    if budget_fail_count > 0:
        print(f"  WARNING: {budget_fail_count} repair prompts exceeded 6000-char budget. Check E34 impact.")

    if paef_count <= 2 and completed_count > 1:
        decision = "KEEP_E34"
        rationale = f"PLAN_ACCEPTED_EXECUTION_FAILED dropped to {paef_count}/10 (≤ 2/10 target met) and completed improved to {completed_count}/10."
    elif store_impl_count >= 3 and paef_count > 2:
        decision = "KEEP_E34"
        rationale = f"store.summary() now implemented in {store_impl_count}/10 tasks (E34 context gap addressed). Remaining failures likely MODEL_REASONING. Consider source stub/signature context next."
    elif paef_count <= 2 and completed_count == 1:
        decision = "KEEP_E34"
        rationale = f"PLAN_ACCEPTED_EXECUTION_FAILED dropped to {paef_count}/10 (target met). completed count unchanged due to BACKEND_CAPACITY ({backend_count}/10)."
    elif paef_count > 4:
        decision = "REVERT_E34"
        rationale = f"PLAN_ACCEPTED_EXECUTION_FAILED increased to {paef_count}/10 (regression above E30 baseline of 4/10). Check for unintended side effects."
    elif store_impl_count < 2 and fmt_sig_ok_count < 2:
        decision = "INVESTIGATE_SOURCE_STUB_CONTEXT"
        rationale = f"Context injection had no measurable effect: store.summary() implemented in only {store_impl_count}/10, format_summary correct in {fmt_sig_ok_count}/10. Model reasoning failures dominate."
    elif paef_count == 4 and store_impl_count >= 2:
        decision = "KEEP_E34"
        rationale = f"No regression in PLAN_ACCEPTED_EXECUTION_FAILED ({paef_count}/10 same as E30) but store.summary() now in {store_impl_count}/10 — partial improvement. Investigate MODEL_REASONING failures for remaining cases."
    else:
        decision = "KEEP_E34_WITH_MINOR_FIX"
        rationale = f"Mixed signals: PAEF={paef_count}/10, store_impl={store_impl_count}/10, fmt_sig={fmt_sig_ok_count}/10."

    print(f"\n  Decision: {decision}")
    print(f"  Rationale: {rationale}")

    if backend_count >= 3:
        print(f"\n  Note: BACKEND_CAPACITY={backend_count}/10 — consider TASK_SUMMARY timeout investigation (separate from E34 context changes).")


if __name__ == "__main__":
    main()
