#!/usr/bin/env python3
"""E41: Post-E40 Bounded Execution Debug Repair Context Validation.

Runs 10 sequential medium_cli_multi_file_feature tasks with E40 code loaded
and measures whether changed-file context improves bounded execution debug repair.

E38/E35 baseline (medium_cli, 10 tasks):
- completed: 0/10
- PLAN_ACCEPTED_EXECUTION_FAILED (PAEF): 4/10
- debug_repair_budget_exhausted: 4/10
- BACKEND_CAPACITY: 5/10
- TaskStore.summary post-execution implementation: 2/10
- format_summary correct signature: 6/10
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
OUTPUT_FILE = Path("/tmp/e41-results.json")

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
        "id": f"E41-M{i}",
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
    dest = WORKSPACE_ROOT / f"e41-{fixture.replace('_', '-')}-{tag}"
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
        log_rows = conn.execute(
            "SELECT level, message, log_metadata FROM log_entries WHERE task_id=? ORDER BY id",
            (task_id,),
        ).fetchall()
        exec_rows = conn.execute(
            "SELECT attempt_number, status, failure_category FROM task_executions WHERE task_id=? ORDER BY id",
            (task_id,),
        ).fetchall()
        conn.close()
    except Exception:
        return {}

    execution_reached = len(exec_rows) > 0
    bounded_debug_repair_invoked = False
    repair_invocations = 0
    repair_prompt_chars: list[int] = []
    planning_repair_chars: list[int] = []
    failure_category = exec_rows[-1][2] if exec_rows else None

    for level, msg, raw_meta in log_rows:
        try:
            meta = json.loads(raw_meta or "{}")
        except Exception:
            meta = {}

        msg_str = str(msg or "").lower()
        meta_str = str(meta).lower()

        # Detect bounded execution debug repair invocation
        if (
            "bounded_execution_debug_repair" in meta_str
            or "debug_repair_scope" in meta_str
            or "phase7f_debug_repair" in meta_str
            or "debug_repair_direct" in msg_str
        ):
            bounded_debug_repair_invoked = True

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
        "execution_reached": execution_reached,
        "bounded_debug_repair_invoked": bounded_debug_repair_invoked,
        "repair_invocations": repair_invocations,
        "repair_prompt_chars": repair_prompt_chars,
        "planning_repair_chars": planning_repair_chars,
        "failure_category": failure_category,
        "attempt_count": len(exec_rows),
    }


def _orchestration_event_signals(workspace_path: Path, session_id: int, task_id: int) -> dict:
    """Read JSONL orchestration event log for E40-specific signals."""
    event_log = workspace_path / ".agent" / "events" / f"session_{session_id}_task_{task_id}.jsonl"
    result = {
        "bounded_debug_repair_invoked": False,
        "changed_file_context_present": False,
        "changed_file_context_paths": [],
        "changed_file_context_chars": 0,
        "repair_output_hash_present": False,
        "repair_output_changed_paths": [],
        "debug_repair_attempt_count": 0,
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

        event_type = event.get("event_type", "")
        details = event.get("details", {})
        debug_scope = str(details.get("debug_repair_scope", "")).lower()

        # Detect bounded execution debug repair
        if event_type == "DEBUG_REPAIR_ATTEMPTED" and (
            debug_scope == "bounded_execution_debug_repair"
            or "bounded_execution_debug_repair" in str(details).lower()
        ):
            result["bounded_debug_repair_invoked"] = True
            result["debug_repair_attempt_count"] += 1
            manifest = details.get("prompt_manifest") or {}
            if manifest.get("bounded_execution_debug_repair_changed_file_context_present"):
                result["changed_file_context_present"] = True
                result["changed_file_context_paths"] = list(
                    manifest.get("bounded_execution_debug_repair_changed_file_context_paths") or []
                )
                result["changed_file_context_chars"] = int(
                    manifest.get("bounded_execution_debug_repair_changed_file_context_chars") or 0
                )

        # Detect REPAIR_GENERATED with E40 hash observability
        if event_type == "REPAIR_GENERATED" and details.get("repair_output_sha256"):
            result["repair_output_hash_present"] = True
            result["repair_output_changed_paths"] = list(
                details.get("repair_output_changed_paths") or []
            )

        # Also detect bounded debug repair from budget-exhausted event
        if event_type == "REPAIR_REJECTED" and (
            details.get("debug_repair_scope") == "bounded_execution_debug_repair"
            or details.get("debug_repair_terminal_reason") == "debug_repair_budget_exhausted"
        ):
            result["bounded_debug_repair_invoked"] = True

    return result


def _inspect_workspace(workspace_path: Path) -> dict:
    """Inspect the post-task workspace for implementation quality."""
    store_py = workspace_path / "src" / "medium_cli" / "store.py"
    formatting_py = workspace_path / "src" / "medium_cli" / "formatting.py"

    result = {
        "store_summary_implemented": False,
        "store_summary_raises_not_implemented": True,
        "format_summary_correct_signature": False,
        "format_task_line_preserved": False,
        "pytest_passed": False,
        "pytest_output": "",
        "store_py_exists": store_py.exists(),
        "formatting_py_exists": formatting_py.exists(),
    }

    if store_py.exists():
        try:
            store_text = store_py.read_text(encoding="utf-8")
            if "def summary(" in store_text:
                if "raise NotImplementedError" in store_text:
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

    if formatting_py.exists():
        try:
            fmt_text = formatting_py.read_text(encoding="utf-8")
            if "def format_task_line(" in fmt_text:
                if "task: Task" in fmt_text or "task," in fmt_text or "task: " in fmt_text:
                    result["format_task_line_preserved"] = True

            if "def format_summary(" in fmt_text:
                try:
                    tree = ast.parse(fmt_text)
                    for node in ast.walk(tree):
                        if isinstance(node, ast.FunctionDef) and node.name == "format_summary":
                            args = node.args
                            arg_names = [a.arg for a in args.args]
                            body = node.body
                            if "total" in arg_names and "completed" in arg_names:
                                result["format_summary_correct_signature"] = True
                                if not (len(body) == 1 and isinstance(body[0], ast.Raise)):
                                    result["store_summary_raises_not_implemented"] = False
                except SyntaxError:
                    pass
        except Exception:
            pass

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
    if "debug_repair_budget_exhausted" in err or "debug repair budget" in err:
        return "PLAN_ACCEPTED_EXECUTION_FAILED"
    if "execution failed" in err or "task1_execution" in err:
        return "PLAN_ACCEPTED_EXECUTION_FAILED"
    if "timed out" in err or "timeout" in err or "capacity" in err or "backend" in err:
        return "BACKEND_CAPACITY"
    if "planning failed" in err or "planning_circuit_breaker" in err or "circuit" in err:
        return "planning_circuit_breaker"
    if "debug_repair_budget" in err or "debug repair" in err:
        return "PLAN_ACCEPTED_EXECUTION_FAILED"
    if "verification_integrity" in err:
        return "verification_integrity"
    if "phase7f" in err or "debug_repair" in err:
        return "PLAN_ACCEPTED_EXECUTION_FAILED"
    return "other"


def _e40_in_process_check() -> bool:
    """Verify E40 functions are loaded in this Python process."""
    from app.services.orchestration.diagnostics.debug_feedback import (
        build_bounded_debug_repair_changed_file_context,
        build_bounded_debug_repair_prompt_with_metadata,
    )
    import inspect

    src_helper = inspect.getsource(build_bounded_debug_repair_changed_file_context)
    src_main = inspect.getsource(build_bounded_debug_repair_prompt_with_metadata)

    checks = {
        "changed_file_context_present field": "bounded_execution_debug_repair_changed_file_context_present" in src_helper,
        "changed_file_context_paths field": "bounded_execution_debug_repair_changed_file_context_paths" in src_helper,
        "changed_file_context_chars field": "bounded_execution_debug_repair_changed_file_context_chars" in src_helper,
        "CURRENT CONTENT section": "CURRENT CONTENT OF IMPLICATED SOURCE FILES" in src_helper,
        "main fn calls helper": "build_bounded_debug_repair_changed_file_context" in src_main,
        "metadata merged": "changed_file_context_metadata" in src_main,
    }
    all_pass = True
    for label, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}: E40 {label}")
        if not ok:
            all_pass = False

    from app.services.orchestration.phases.execution_loop import (
        _bounded_debug_repair_prior_source_paths,
        _bounded_debug_repair_prompt_manifest,
    )
    src_loop = inspect.getsource(_bounded_debug_repair_prompt_manifest)
    loop_ok = "bounded_execution_debug_repair_changed_file_context_present" in src_loop
    print(f"  {'PASS' if loop_ok else 'FAIL'}: E40 execution_loop manifest fields")
    if not loop_ok:
        all_pass = False

    return all_pass


def main() -> None:
    token = _get_token()

    # ── E40 in-process verification ──────────────────────────────────────────
    print("[E41] E40 in-process code verification:")
    if not _e40_in_process_check():
        print("  ERROR: E40 not loaded. Worker restart required before eval.")
        sys.exit(1)

    try:
        import subprocess as sp
        result = sp.run(
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
        print(f"\n[E41] ({i+1}/{len(TASK_CORPUS)}) {tid} fixture={fixture}", flush=True)

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
                "name": f"E41 {tid}",
                "description": f"E41 post-E40 bounded debug repair context validation {tid}",
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
                "name": f"e41-{task_spec['id'].lower()}-{tag}",
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
        event_signals = _orchestration_event_signals(ws, session_id, task_id)
        category = _classify(final)

        repair_prompt_chars = db_signals.get("repair_prompt_chars", [])
        execution_reached = db_signals.get("execution_reached", False) or (category == "completed")
        bounded_debug_repair_invoked = (
            db_signals.get("bounded_debug_repair_invoked", False)
            or event_signals.get("bounded_debug_repair_invoked", False)
        )
        changed_file_context_present = event_signals.get("changed_file_context_present", False)
        changed_file_context_paths = event_signals.get("changed_file_context_paths", [])
        changed_file_context_chars = event_signals.get("changed_file_context_chars", 0)
        repair_output_hash_present = event_signals.get("repair_output_hash_present", False)
        repair_output_changed_paths = event_signals.get("repair_output_changed_paths", [])

        ws_signals = _inspect_workspace(ws)

        rec = {
            "id": tid,
            "fixture": fixture,
            "task_id": task_id,
            "session_id": session_id,
            "status": final.get("status", "unknown"),
            "last_alert_message": str(final.get("last_alert_message") or "")[:280],
            "category": category,
            "execution_reached": execution_reached,
            # Bounded execution debug repair signals
            "bounded_debug_repair_invoked": bounded_debug_repair_invoked,
            "changed_file_context_present": changed_file_context_present,
            "changed_file_context_paths": changed_file_context_paths,
            "changed_file_context_chars": changed_file_context_chars,
            "repair_output_hash_present": repair_output_hash_present,
            "repair_output_changed_paths": repair_output_changed_paths,
            # Repair budget / planning repair
            "repair_invocations": db_signals.get("repair_invocations", 0),
            "repair_prompt_chars": repair_prompt_chars,
            "max_repair_chars": max(repair_prompt_chars) if repair_prompt_chars else 0,
            "budget_exceeded": any(c > 6000 for c in repair_prompt_chars),
            # Workspace quality signals
            "store_summary_implemented": ws_signals["store_summary_implemented"],
            "format_summary_correct_signature": ws_signals["format_summary_correct_signature"],
            "format_task_line_preserved": ws_signals["format_task_line_preserved"],
            "pytest_passed": ws_signals["pytest_passed"],
            "pytest_output": ws_signals["pytest_output"],
        }
        results.append(rec)

        ctx_str = (
            f"ctx_present={changed_file_context_present} "
            f"ctx_paths={changed_file_context_paths} "
            f"ctx_chars={changed_file_context_chars}"
        ) if bounded_debug_repair_invoked else "no_bounded_repair"

        print(
            f"  → status={rec['status']} cat={category} "
            f"exec={execution_reached} bounded_repair={bounded_debug_repair_invoked} "
            f"{ctx_str} "
            f"hash={repair_output_hash_present} "
            f"store.summary={'IMPL' if ws_signals['store_summary_implemented'] else 'STUB'} "
            f"fmt_sig={'OK' if ws_signals['format_summary_correct_signature'] else 'BAD'} "
            f"fmt_task_line={'OK' if ws_signals['format_task_line_preserved'] else 'MISSING'} "
            f"pytest={'PASS' if ws_signals['pytest_passed'] else 'FAIL'}",
            flush=True,
        )

    OUTPUT_FILE.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\n[E41] Raw results → {OUTPUT_FILE}")

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
    other_count = (
        total - completed_count - paef_count - backend_count
        - oscillation_count - json_fail_count - prcf_count
    )

    exec_reached_count = _count(valid, "execution_reached")
    bounded_repair_count = _count(valid, "bounded_debug_repair_invoked")
    ctx_present_count = _count(valid, "changed_file_context_present")
    repair_hash_count = _count(valid, "repair_output_hash_present")

    store_impl_count = _count(valid, "store_summary_implemented")
    fmt_sig_ok_count = _count(valid, "format_summary_correct_signature")
    fmt_task_preserved_count = _count(valid, "format_task_line_preserved")
    pytest_pass_count = _count(valid, "pytest_passed")
    budget_fail_count = _count(valid, "budget_exceeded")
    all_chars = [c for r in valid for c in r.get("repair_prompt_chars", [])]
    max_chars = max(all_chars) if all_chars else 0

    # Sub-population: tasks where bounded debug repair was invoked
    repair_pop = [r for r in valid if r.get("bounded_debug_repair_invoked")]
    repair_pop_size = len(repair_pop)
    store_in_repair_pop = sum(1 for r in repair_pop if r.get("store_summary_implemented"))
    fmt_in_repair_pop = sum(1 for r in repair_pop if r.get("format_summary_correct_signature"))
    fmt_task_in_repair_pop = sum(1 for r in repair_pop if r.get("format_task_line_preserved"))
    ctx_in_repair_pop = sum(1 for r in repair_pop if r.get("changed_file_context_present"))

    print("\n" + "=" * 70)
    print("[E41] AGGREGATES (vs E38/E35 baseline)")
    print("=" * 70)
    print(f"  Total tasks:                              {total}/10")
    print(f"  completed:                                {completed_count}/10  (E38: 0/10)")
    print(f"  PLAN_ACCEPTED_EXECUTION_FAILED (PAEF):    {paef_count}/10  (E38: 4/10)")
    print(f"  BACKEND_CAPACITY:                         {backend_count}/10  (E38: 5/10)")
    print(f"  planning_circuit_breaker:                 {oscillation_count}/10  (E38: 0/10)")
    print(f"  planning_json_parse_failure:              {json_fail_count}/10  (E38: 1/10)")
    print(f"  bootstrap_contract_PRCF:                  {prcf_count}/10  (E38: 0/10)")
    print(f"  other:                                    {other_count}/10")
    print()
    print(f"  execution_reached:                        {exec_reached_count}/10")
    print(f"  bounded_execution_debug_repair_invoked:   {bounded_repair_count}/10  (E38: 4/10 PAEF)")
    print()
    print("[E41] E40-SPECIFIC SIGNALS")
    print(f"  changed_file_context_present (all):       {ctx_present_count}/10")
    if repair_pop_size > 0:
        print(f"  changed_file_context_present (repair pop):{ctx_in_repair_pop}/{repair_pop_size}")
    print(f"  repair_output_hash_present:               {repair_hash_count}/10")
    print()
    print("[E41] WORKSPACE QUALITY (all tasks)")
    print(f"  store.summary() implemented:              {store_impl_count}/10  (E38: 2/10)")
    print(f"  format_summary(total,completed) OK:       {fmt_sig_ok_count}/10  (E38: 6/10)")
    print(f"  format_task_line preserved:               {fmt_task_preserved_count}/10")
    print(f"  pytest passed in workspace:               {pytest_pass_count}/10")
    print()
    if repair_pop_size > 0:
        print("[E41] WORKSPACE QUALITY (bounded debug repair sub-population)")
        print(f"  store.summary() implemented:              {store_in_repair_pop}/{repair_pop_size}  (E38: 2/4)")
        print(f"  format_summary(total,completed) OK:       {fmt_in_repair_pop}/{repair_pop_size}  (E38: partial)")
        print(f"  format_task_line preserved:               {fmt_task_in_repair_pop}/{repair_pop_size}")
        print()
    print(f"  budget_exceeded (>6000):                  {budget_fail_count}/10  (target: 0)")
    print(f"  max_repair_prompt_chars:                  {max_chars}  (limit: 6000)")

    print("\n" + "=" * 70)
    print("[E41] PER-TASK RESULTS")
    print("=" * 70)
    hdr = (
        f"{'ID':<12} {'task_id':<8} {'status':<10} {'category':<35} "
        f"{'exec':<5} {'bdr':<5} {'ctx':<5} {'hash':<5} "
        f"{'store':<6} {'fmt':<5} {'ftl':<5} {'pytest'}"
    )
    print(hdr)
    for r in valid:
        print(
            f"{r['id']:<12} {r.get('task_id','?'):<8} "
            f"{r['status']:<10} {r['category']:<35} "
            f"{'Y' if r['execution_reached'] else 'N':<5} "
            f"{'Y' if r['bounded_debug_repair_invoked'] else 'N':<5} "
            f"{'Y' if r['changed_file_context_present'] else 'N':<5} "
            f"{'Y' if r['repair_output_hash_present'] else 'N':<5} "
            f"{'IMPL' if r['store_summary_implemented'] else 'STUB':<6} "
            f"{'OK' if r['format_summary_correct_signature'] else 'BAD':<5} "
            f"{'OK' if r['format_task_line_preserved'] else 'BAD':<5} "
            f"{'PASS' if r['pytest_passed'] else 'FAIL'}"
        )

    # ── Decision rule ────────────────────────────────────────────────────────
    print("\n" + "=" * 70)
    print("[E41] DECISION RULE EVALUATION")
    print("=" * 70)

    ctx_injection_working = (
        repair_pop_size > 0 and ctx_in_repair_pop >= repair_pop_size * 0.75
    )
    paef_dropped = paef_count < 4
    repair_quality_improved = (
        repair_pop_size > 0
        and (store_in_repair_pop / repair_pop_size) > (2 / 4)
    )
    api_regressions = fmt_task_preserved_count < 8

    print(f"  Context injection working (ctx in ≥75% repair pop): {'YES' if ctx_injection_working else 'NO'}")
    print(f"  PAEF dropped from 4/10:                              {'YES' if paef_dropped else 'NO'} ({paef_count}/10)")
    print(f"  Repair quality improved vs E38:                      {'YES' if repair_quality_improved else 'NO'}")
    print(f"  format_task_line regressions (< 8/10):               {'YES' if api_regressions else 'NO'} ({fmt_task_preserved_count}/10)")
    print(f"  Context not reaching bounded repair path:            {'YES' if (repair_pop_size > 0 and ctx_in_repair_pop == 0) else 'NO'}")

    print()
    if api_regressions:
        recommendation = "REVERT_E40"
        rationale = "format_task_line regressions detected — API weakening worsened."
    elif repair_pop_size > 0 and ctx_in_repair_pop == 0:
        recommendation = "FIX_E40_INJECTION_PATH"
        rationale = "Bounded debug repair invoked but changed-file context never present — injection path broken."
    elif not ctx_injection_working and repair_pop_size > 0:
        recommendation = "FIX_E40_INJECTION_PATH"
        rationale = f"Context present in only {ctx_in_repair_pop}/{repair_pop_size} bounded repair cases — injection unreliable."
    elif paef_dropped and repair_quality_improved:
        recommendation = "KEEP_E40"
        rationale = "PAEF dropped and repair quality improved with changed-file context."
    elif ctx_injection_working and not repair_quality_improved:
        recommendation = "KEEP_E40_MODEL_CAPACITY_LIMITED"
        rationale = "Context present in bounded repair but repair quality not improved — model capacity limited."
    elif ctx_injection_working and repair_quality_improved:
        recommendation = "KEEP_E40"
        rationale = "Changed-file context present and quality improved in repair sub-population."
    elif repair_pop_size == 0:
        recommendation = "KEEP_E40_NO_REPAIR_CASES"
        rationale = "No bounded debug repair cases observed — cannot evaluate repair quality."
    else:
        recommendation = "INVESTIGATE_MODEL_CAPACITY"
        rationale = "Context injection working but repair quality signals inconclusive."

    print(f"  Recommendation: {recommendation}")
    print(f"  Rationale: {rationale}")
    print()
    print(f"[E41] Raw results saved to {OUTPUT_FILE}")


if __name__ == "__main__":
    main()
