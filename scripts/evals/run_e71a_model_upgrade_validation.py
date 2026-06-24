#!/usr/bin/env python3
"""E71A: Model Upgrade Validation (OpenAI GPT as CR backend).

Identical to E70 in every way except COMPLETION_REPAIR_BACKEND=openai_chat_completions.
Goal: answer whether the E70 failure rate is caused by the local model or by Orchestrator.

E70 baselines (the numbers to beat):
  completed:                0/10
  CR invocations:           3/10
  store.tasks occurrences:  5
  format_summary(store):    4
  old_not_found:            2/3 CR
  MULTI_FILE_REPAIR:        1/3 CR
  apply_ok+wrong_impl:      0/3 (apply never returned ok)

Decision matrix:
  Case 1 (GPT materially improves: completed >= 3, hallucinations near 0)
    → CONFIRM_LOCAL_MODEL_CAPACITY_LIMIT
  Case 2 (GPT slightly improves: completed = 1, hallucinations reduced)
    → LOCAL_MODEL_LIMIT likely, some architecture debt remains
  Case 3 (GPT behaves almost identically: 0/10, same hallucinations)
    → Model NOT the primary bottleneck → architecture/fixture investigation
"""

from __future__ import annotations

import json
import re
import shutil
import subprocess
import sys
import time
from datetime import UTC, datetime
from pathlib import Path

BASE_URL = "http://localhost:8080/api/v1"
FIXTURE_ROOT = Path(
    "/root/.openclaw/workspace/vault/projects/orchestrator/scripts/evals/fixtures"
)
WORKSPACE_ROOT = Path("/root/.openclaw/workspace/vault/projects")
OUTPUT_FILE = Path("/tmp/e71a-results.json")
DB_PATH = Path(
    "/root/.openclaw/workspace/vault/projects/orchestrator/orchestrator.db"
)
VENV_PYTHON = Path(
    "/root/.openclaw/workspace/vault/projects/orchestrator/venv/bin/python3"
)

TASK_TIMEOUT = 7200
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
        "id": f"E71A-M{i:02d}",
        "fixture": "medium_cli_multi_file_feature",
        "prompt": MEDIUM_CLI_PROMPT,
    }
    for i in range(1, 11)
]

TARGET_FILES = {
    "cli.py": "src/medium_cli/cli.py",
    "store.py": "src/medium_cli/store.py",
    "formatting.py": "src/medium_cli/formatting.py",
}

HALLUCINATION_PATTERNS = {
    "store_tasks_attr": re.compile(r"\bstore\.tasks\b"),
    "format_summary_store": re.compile(r"\bformat_summary\s*\(\s*store\b"),
}

SUMMARY_NOT_IMPLEMENTED_RE = re.compile(
    r"def\s+summary.*?raise\s+NotImplementedError", re.DOTALL
)
SUMMARY_IMPLEMENTED_RE = re.compile(r"def\s+summary.*?return\s+\(", re.DOTALL)


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
    raise SystemExit(
        f"Dispatcher safety timeout ({TASK_TIMEOUT}s) fired waiting for session "
        f"{session_id}. Aborting batch."
    )


def _fresh_workspace(fixture: str, tag: str) -> Path:
    fixture_dir = FIXTURE_ROOT / fixture
    dest = WORKSPACE_ROOT / f"e71a-{fixture.replace('_', '-')}-{tag}"
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


def _db_signals(task_id: int) -> dict:
    try:
        import sqlite3

        conn = sqlite3.connect(str(DB_PATH))
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

    result: dict = {
        "execution_reached": len(exec_rows) > 0,
        "failure_category": exec_rows[-1][2] if exec_rows else None,
        "attempt_count": len(exec_rows),
        "completion_repair_invoked": False,
        "completion_repair_prompt_chars": None,
        "completion_repair_duration_seconds": None,
        "completion_repair_timed_out": None,
        "completion_repair_output_chars": None,
        "completion_repair_fast_profile_selected": None,
        "planning_circuit_breaker": False,
        "old_not_found_in_log": False,
        "apply_success_in_log": False,
    }

    for _level, msg, raw_meta in log_rows:
        try:
            meta = json.loads(raw_meta or "{}")
        except Exception:
            meta = {}

        msg_lower = str(msg or "").lower()

        if "root_cause_oscillation" in msg_lower or "planning_circuit_breaker" in msg_lower:
            result["planning_circuit_breaker"] = True

        if "'old' text not found" in msg_lower or "old_not_found" in msg_lower:
            result["old_not_found_in_log"] = True

        if "direct ops applied" in msg_lower or (
            "completion repair step" in msg_lower and "successfully" in msg_lower
        ):
            result["apply_success_in_log"] = True

        if "completion_repair_prompt_chars" not in meta:
            continue

        result["completion_repair_invoked"] = True
        result["completion_repair_prompt_chars"] = meta.get("completion_repair_prompt_chars")
        result["completion_repair_duration_seconds"] = meta.get("completion_repair_duration_seconds")
        result["completion_repair_timed_out"] = meta.get("completion_repair_timed_out")
        result["completion_repair_output_chars"] = meta.get("completion_repair_output_chars")
        result["completion_repair_fast_profile_selected"] = meta.get(
            "completion_repair_fast_profile_selected"
        )

    return result


def _event_signals(workspace_path: Path, session_id: int, task_id: int) -> dict:
    event_log = (
        workspace_path
        / ".agent"
        / "events"
        / f"session_{session_id}_task_{task_id}.jsonl"
    )
    result: dict = {
        "cr_repair_generated": False,
        "cr_repair_applied": False,
        "cr_repair_rejected": False,
        "cr_rejection_reason": None,
        "cr_old_not_found": False,
        "cr_signature_guard_checked": False,
        "cr_candidate_unavailable": False,
        "cr_signature_violation_count": 0,
        "cr_apply_success": False,
        "cr_expected_files": [],
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
        phase = str(details.get("phase", "")).lower()

        if event_type == "repair_generated" and "completion" in phase:
            result["cr_repair_generated"] = True

        if event_type == "repair_applied" and "completion" in phase:
            result["cr_repair_applied"] = True
            result["cr_apply_success"] = True
            result["cr_expected_files"] = list(details.get("expected_files", []) or [])

        if event_type == "repair_rejected" and "completion" in phase:
            result["cr_repair_rejected"] = True
            reason = str(
                details.get("reason") or details.get("rejection_reason") or ""
            )
            result["cr_rejection_reason"] = reason
            if "'old' text not found" in reason.lower() or "old_not_found" in reason.lower():
                result["cr_old_not_found"] = True

        if "completion_repair_signature_guard_checked" in details:
            result["cr_signature_guard_checked"] = bool(
                details.get("completion_repair_signature_guard_checked")
            )
            result["cr_candidate_unavailable"] = bool(
                details.get("completion_repair_signature_guard_candidate_unavailable")
            )
            viol = details.get("completion_repair_signature_violation_count") or 0
            result["cr_signature_violation_count"] = int(viol)

    return result


def _workspace_repair_analysis(workspace_path: Path, fixture_path: Path) -> dict:
    result: dict = {
        "ws_files_modified": [],
        "ws_cli_modified": False,
        "ws_store_modified": False,
        "ws_formatting_modified": False,
        "ws_unique_modified_count": 0,
        "ws_multi_file_repair": False,
        "ws_store_tasks_count": 0,
        "ws_format_summary_store_count": 0,
        "ws_api_hallucination_count": 0,
        "ws_api_hallucination_types": [],
        "ws_summary_implemented": False,
        "ws_summary_stub_remains": False,
        "ws_formatting_content_modified": False,
        "ws_pytest_result": None,
        "ws_pytest_output": "",
    }

    modified_files = []
    for short_name, rel_path in TARGET_FILES.items():
        ws_file = workspace_path / rel_path
        fix_file = fixture_path / rel_path
        if not ws_file.exists():
            continue
        try:
            ws_content = ws_file.read_text(encoding="utf-8", errors="replace")
            fix_content = (
                fix_file.read_text(encoding="utf-8", errors="replace")
                if fix_file.exists()
                else ""
            )
        except OSError:
            continue

        if ws_content != fix_content:
            modified_files.append(rel_path)
            if short_name == "cli.py":
                result["ws_cli_modified"] = True
            elif short_name == "store.py":
                result["ws_store_modified"] = True
                if SUMMARY_IMPLEMENTED_RE.search(ws_content) and not SUMMARY_NOT_IMPLEMENTED_RE.search(ws_content):
                    result["ws_summary_implemented"] = True
                else:
                    result["ws_summary_stub_remains"] = True
            elif short_name == "formatting.py":
                result["ws_formatting_modified"] = True
                result["ws_formatting_content_modified"] = True

        if ws_content != fix_content or short_name == "cli.py":
            result["ws_store_tasks_count"] += len(
                HALLUCINATION_PATTERNS["store_tasks_attr"].findall(ws_content)
            )
            result["ws_format_summary_store_count"] += len(
                HALLUCINATION_PATTERNS["format_summary_store"].findall(ws_content)
            )

    result["ws_files_modified"] = modified_files
    result["ws_unique_modified_count"] = len(modified_files)
    result["ws_multi_file_repair"] = len(modified_files) >= 2

    hallucination_types = []
    total = result["ws_store_tasks_count"] + result["ws_format_summary_store_count"]
    if result["ws_store_tasks_count"] > 0:
        hallucination_types.append(f"store.tasks(x{result['ws_store_tasks_count']})")
    if result["ws_format_summary_store_count"] > 0:
        hallucination_types.append(
            f"format_summary(store)(x{result['ws_format_summary_store_count']})"
        )
    result["ws_api_hallucination_count"] = total
    result["ws_api_hallucination_types"] = hallucination_types

    try:
        proc = subprocess.run(
            [str(VENV_PYTHON), "-m", "pytest", "-q", "--tb=short", "--no-header"],
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
        output = (proc.stdout + proc.stderr).strip()[:800]
        result["ws_pytest_output"] = output
        result["ws_pytest_result"] = "pass" if proc.returncode == 0 else "fail"
    except Exception as exc:
        result["ws_pytest_output"] = f"pytest error: {exc}"
        result["ws_pytest_result"] = "error"

    return result


def _classify(session: dict, db_sig: dict) -> str:
    status = str(session.get("status") or "").lower()
    if status == "completed":
        return "completed"

    failure_cat = str(db_sig.get("failure_category") or "").lower()
    if "plan_accepted_execution_failed" in failure_cat or "paef" in failure_cat:
        return "PLAN_ACCEPTED_EXECUTION_FAILED"
    if "planning_circuit_breaker" in failure_cat or "circuit_breaker" in failure_cat:
        return "planning_circuit_breaker"
    if "backend_capacity" in failure_cat:
        return "BACKEND_CAPACITY"
    if "debug_repair_budget" in failure_cat or "debug_repair" in failure_cat:
        return "debug_repair_budget_exhausted"
    if "planning_json" in failure_cat or "json_parse" in failure_cat:
        return "planning_json_parse_failure"
    if "planning_repair" in failure_cat and "timeout" in failure_cat:
        return "planning_repair_timeout"
    if "planning_repair" in failure_cat:
        return "planning_repair_failure"

    err = str(
        session.get("last_alert_message") or session.get("error_message") or ""
    ).lower()
    if "oscillation" in err or "root_cause_oscillation" in err:
        return "planning_circuit_breaker"
    if "json" in err and "parse" in err:
        return "planning_json_parse_failure"
    if "debug_repair_budget_exhausted" in err:
        return "debug_repair_budget_exhausted"
    if "execution failed" in err or "task1_execution" in err:
        return "PLAN_ACCEPTED_EXECUTION_FAILED"
    if "timed out" in err or "timeout" in err or "capacity" in err:
        return "BACKEND_CAPACITY"
    if "planning failed" in err or "circuit" in err:
        return "planning_circuit_breaker"
    if db_sig.get("planning_circuit_breaker"):
        return "planning_circuit_breaker"
    return "other"


def _e71a_in_process_check() -> bool:
    import inspect

    from app.services.orchestration.phases.completion_repair_capsule import (
        build_bounded_completion_repair_prompt,
        _extract_source_api_contract,
        MAX_SOURCE_CONTENT_PER_FILE_CHARS,
        MAX_SOURCE_CONTENT_TOTAL_CHARS,
    )

    src = inspect.getsource(build_bounded_completion_repair_prompt)
    capsule_src = inspect.getsource(_extract_source_api_contract)

    checks = {
        "E69 _extract_source_api_contract present": capsule_src is not None,
        "E69 SOURCE API CONTRACT in prompt": "SOURCE API CONTRACT" in src,
        "E69 Rule 14 (multi-file ops)": "every file" in src,
        "E69 Rule 15 (.tasks prohibition)": ".tasks" in src and "invent" in src,
        "E69 Rule 16 (signature shape)": "argument shapes" in src,
        "E69 Rule 17 (NotImplementedError stubs)": "NotImplementedError" in src,
        "E66 PER_FILE=2000": MAX_SOURCE_CONTENT_PER_FILE_CHARS == 2000,
        "E66 TOTAL=5000": MAX_SOURCE_CONTENT_TOTAL_CHARS == 5000,
    }

    all_pass = True
    for label, ok in checks.items():
        print(f"  {'PASS' if ok else 'FAIL'}: {label}")
        if not ok:
            all_pass = False

    try:
        from app.config import settings as live_settings
        backend_val = getattr(live_settings, "COMPLETION_REPAIR_BACKEND", None)
        expected = "openai_chat_completions"
        ok = backend_val == expected
        print(
            f"  {'PASS' if ok else 'FAIL'}: COMPLETION_REPAIR_BACKEND={backend_val!r} "
            f"(expected {expected!r})"
        )
        if not ok:
            all_pass = False

        model_val = getattr(live_settings, "OPENAI_CHAT_COMPLETIONS_MODEL", None)
        model_ok = bool(model_val)
        print(f"  {'PASS' if model_ok else 'WARN'}: OPENAI_CHAT_COMPLETIONS_MODEL={model_val!r}")

        api_key = getattr(live_settings, "OPENAI_API_KEY", "") or ""
        key_ok = len(api_key) > 10
        print(
            f"  {'PASS' if key_ok else 'FAIL'}: OPENAI_API_KEY={'set' if key_ok else 'missing/empty'}"
        )
        if not key_ok:
            all_pass = False
    except Exception as exc:
        print(f"  WARN: Could not read live settings: {exc}")

    return all_pass


def main() -> None:
    token = _get_token()

    print("[E71A] E69+OpenAI in-process verification:")
    if not _e71a_in_process_check():
        print("  ERROR: Pre-conditions not met. Check COMPLETION_REPAIR_BACKEND and restart worker.")
        sys.exit(1)

    try:
        result = subprocess.run(
            ["pgrep", "-f", "celery -A app.celery_app worker"],
            capture_output=True,
            text=True,
        )
        pids = result.stdout.strip().split()
        worker_pid = pids[0] if pids else "unknown"
    except Exception:
        worker_pid = "unknown"
    print(f"  Active worker PID: {worker_pid}")
    print()

    fixture_path = FIXTURE_ROOT / "medium_cli_multi_file_feature"
    results: list[dict] = []
    ts = datetime.now(UTC).strftime("%Y%m%d%H%M")

    for i, task_spec in enumerate(TASK_CORPUS):
        tag = f"{ts}-{i + 1:02d}"
        fixture = task_spec["fixture"]
        tid = task_spec["id"]
        print(
            f"\n[E71A] ({i + 1}/{len(TASK_CORPUS)}) {tid} fixture={fixture}",
            flush=True,
        )

        try:
            ws = _fresh_workspace(fixture, tag)
        except Exception as exc:
            print(f"  workspace provision failed: {exc}")
            results.append({"id": tid, "fixture": fixture, "error": str(exc), "category": "infra_error"})
            continue

        session_id = None
        task_id = None
        try:
            proj = _api(
                "POST",
                "projects",
                token,
                {
                    "name": f"E71A {tid}",
                    "description": f"E71A model-upgrade validation {tid}",
                    "workspace_path": str(ws),
                },
            )
            proj_id = proj["id"]

            task_obj = _api(
                "POST",
                "tasks",
                token,
                {
                    "project_id": proj_id,
                    "title": task_spec["id"],
                    "description": task_spec["prompt"],
                    "priority": 0,
                    "plan_position": 1,
                },
            )
            task_id = task_obj["id"]

            session = _api(
                "POST",
                "sessions",
                token,
                {
                    "project_id": proj_id,
                    "name": f"e71a-{task_spec['id'].lower()}-{tag}",
                    "execution_mode": "manual",
                    "default_execution_profile": "full_lifecycle",
                },
            )
            session_id = session["id"]

            _api("POST", f"sessions/{session_id}/tasks/{task_id}/run", token)
            print(
                f"  session={session_id} task={task_id} dispatched — waiting...",
                flush=True,
            )
        except Exception as exc:
            print(f"  dispatch failed: {exc}")
            results.append(
                {"id": tid, "fixture": fixture, "error": str(exc), "category": "dispatch_error"}
            )
            continue

        try:
            final = _wait_terminal(session_id, token)
        except SystemExit as exc:
            print(f"  {exc}")
            results.append(
                {
                    "id": tid,
                    "fixture": fixture,
                    "task_id": task_id,
                    "session_id": session_id,
                    "error": str(exc),
                    "category": "dispatcher_safety_timeout",
                }
            )
            print("  ABORTING BATCH.")
            break

        db_sig = _db_signals(task_id)
        ev_sig = _event_signals(ws, session_id, task_id)
        ws_analysis = _workspace_repair_analysis(ws, fixture_path)
        category = _classify(final, db_sig)

        final_status = str(final.get("status", "unknown")).lower()

        if ev_sig.get("cr_repair_generated"):
            if ws_analysis["ws_unique_modified_count"] >= 2:
                repair_class = "MULTI_FILE_REPAIR"
            elif ws_analysis["ws_unique_modified_count"] == 1:
                repair_class = "SINGLE_FILE_REPAIR"
            else:
                repair_class = "NO_FILES_MODIFIED"
        else:
            repair_class = "CR_NOT_INVOKED"

        source_api_contract_present = (
            ev_sig.get("cr_repair_generated")
            and (db_sig.get("completion_repair_prompt_chars") or 0) > 3500
        )

        rec: dict = {
            "id": tid,
            "fixture": fixture,
            "task_id": task_id,
            "session_id": session_id,
            "status": final_status,
            "last_alert_message": str(final.get("last_alert_message") or "")[:300],
            "category": category,
            "execution_reached": db_sig.get("execution_reached", False),
            "completion_repair_invoked": (
                ev_sig.get("cr_repair_generated", False)
                or db_sig.get("completion_repair_invoked", False)
            ),
            "completion_repair_prompt_chars": db_sig.get("completion_repair_prompt_chars"),
            "completion_repair_duration_seconds": db_sig.get("completion_repair_duration_seconds"),
            "completion_repair_timed_out": db_sig.get("completion_repair_timed_out"),
            "completion_repair_output_chars": db_sig.get("completion_repair_output_chars"),
            "cr_signature_guard_checked": ev_sig.get("cr_signature_guard_checked"),
            "cr_candidate_unavailable": ev_sig.get("cr_candidate_unavailable"),
            "cr_signature_violation_count": ev_sig.get("cr_signature_violation_count", 0),
            "cr_old_not_found": ev_sig.get("cr_old_not_found") or db_sig.get("old_not_found_in_log"),
            "cr_apply_success": ev_sig.get("cr_apply_success") or db_sig.get("apply_success_in_log"),
            "cr_rejection_reason": ev_sig.get("cr_rejection_reason"),
            "repair_class": repair_class,
            "ws_files_modified": ws_analysis["ws_files_modified"],
            "ws_unique_modified_count": ws_analysis["ws_unique_modified_count"],
            "ws_cli_touched": ws_analysis["ws_cli_modified"],
            "ws_store_touched": ws_analysis["ws_store_modified"],
            "ws_formatting_touched": ws_analysis["ws_formatting_modified"],
            "ws_store_tasks_occurrences": ws_analysis["ws_store_tasks_count"],
            "ws_format_summary_store_occurrences": ws_analysis["ws_format_summary_store_count"],
            "ws_api_hallucination_count": ws_analysis["ws_api_hallucination_count"],
            "ws_api_hallucination_types": ws_analysis["ws_api_hallucination_types"],
            "ws_summary_stub_targeted": ws_analysis["ws_summary_implemented"],
            "ws_summary_stub_remains": ws_analysis["ws_summary_stub_remains"],
            "ws_formatting_stub_targeted": ws_analysis["ws_formatting_content_modified"],
            "source_api_contract_present": source_api_contract_present,
            "ws_pytest_result": ws_analysis["ws_pytest_result"],
            "ws_pytest_output": ws_analysis["ws_pytest_output"],
        }
        results.append(rec)

        print(
            f"  → status={final_status} cat={category} "
            f"exec={rec['execution_reached']} "
            f"cr_invoked={rec['completion_repair_invoked']} "
            f"repair_class={repair_class} "
            f"files_modified={ws_analysis['ws_files_modified']} "
            f"hallucinations={ws_analysis['ws_api_hallucination_count']} "
            f"summary_impl={ws_analysis['ws_summary_implemented']} "
            f"pytest={ws_analysis['ws_pytest_result']}",
            flush=True,
        )

    OUTPUT_FILE.write_text(json.dumps(results, indent=2), encoding="utf-8")
    print(f"\n[E71A] Raw results → {OUTPUT_FILE}")

    valid = [r for r in results if r.get("category") not in ("infra_error", "dispatch_error")]
    total = len(valid)

    def _count(recs: list, key: str, val: object = True) -> int:
        return sum(1 for r in recs if r.get(key) == val)

    def _count_cat(recs: list, cat: str) -> int:
        return sum(1 for r in recs if r.get("category") == cat)

    completed_count = _count_cat(valid, "completed")
    paef_count = _count_cat(valid, "PLAN_ACCEPTED_EXECUTION_FAILED")
    debug_repair_count = _count_cat(valid, "debug_repair_budget_exhausted")
    circuit_count = _count_cat(valid, "planning_circuit_breaker")
    backend_count = _count_cat(valid, "BACKEND_CAPACITY")
    json_fail_count = _count_cat(valid, "planning_json_parse_failure")
    planning_repair_timeout_count = _count_cat(valid, "planning_repair_timeout")
    other_count = (
        total
        - completed_count
        - paef_count
        - debug_repair_count
        - circuit_count
        - backend_count
        - json_fail_count
        - planning_repair_timeout_count
    )

    cr_invoked_recs = [r for r in valid if r.get("completion_repair_invoked")]
    cr_count = len(cr_invoked_recs)

    multi_file_reps = [r for r in cr_invoked_recs if r.get("repair_class") == "MULTI_FILE_REPAIR"]
    single_file_reps = [r for r in cr_invoked_recs if r.get("repair_class") == "SINGLE_FILE_REPAIR"]
    no_files_reps = [r for r in cr_invoked_recs if r.get("repair_class") == "NO_FILES_MODIFIED"]

    cli_touched = sum(1 for r in cr_invoked_recs if r.get("ws_cli_touched"))
    store_touched = sum(1 for r in cr_invoked_recs if r.get("ws_store_touched"))
    formatting_touched = sum(1 for r in cr_invoked_recs if r.get("ws_formatting_touched"))

    store_tasks_total = sum(r.get("ws_store_tasks_occurrences", 0) for r in cr_invoked_recs)
    fmt_store_total = sum(r.get("ws_format_summary_store_occurrences", 0) for r in cr_invoked_recs)
    total_hallucinations = store_tasks_total + fmt_store_total
    hallucination_tasks = sum(1 for r in cr_invoked_recs if r.get("ws_api_hallucination_count", 0) > 0)

    summary_impl_count = sum(1 for r in cr_invoked_recs if r.get("ws_summary_stub_targeted"))
    formatting_targeted_count = sum(1 for r in cr_invoked_recs if r.get("ws_formatting_stub_targeted"))
    old_not_found_count = sum(1 for r in cr_invoked_recs if r.get("cr_old_not_found"))
    apply_success_count = sum(1 for r in cr_invoked_recs if r.get("cr_apply_success"))
    contract_present = sum(1 for r in cr_invoked_recs if r.get("source_api_contract_present"))

    print("\n" + "=" * 80)
    print("[E71A] AGGREGATES (E70 baselines: multi-file 1/3, hallucination 9, completed 0/10)")
    print("=" * 80)
    print(f"  Total valid tasks:                  {total}/10")
    print(f"  completed:                          {completed_count}/10  (E70: 0/10)")
    print(f"  PLAN_ACCEPTED_EXECUTION_FAILED:     {paef_count}/10  (E70: 0/10)")
    print(f"  debug_repair_budget_exhausted:      {debug_repair_count}/10  (E70: 4/10)")
    print(f"  planning_circuit_breaker:           {circuit_count}/10  (E70: 0/10)")
    print(f"  BACKEND_CAPACITY:                   {backend_count}/10  (E70: 1/10)")
    print(f"  planning_json_parse_failure:        {json_fail_count}/10  (E70: 0/10)")
    print(f"  planning_repair_timeout:            {planning_repair_timeout_count}/10  (E70: 1/10)")
    print(f"  other:                              {other_count}/10  (E70: 3/10)")
    print()
    print("[E71A] COMPLETION REPAIR INVOCATION")
    print(f"  CR invocation count:                {cr_count}/10  (E70: 3/10)")
    print(f"  old_not_found:                      {old_not_found_count}/{cr_count or 1}  (E70: 2/3)")
    print(f"  apply_success:                      {apply_success_count}/{cr_count or 1}  (E70: 0/3)")
    print()
    print("[E71A] MULTI-FILE REPAIR — METRIC A")
    print(f"  SINGLE_FILE_REPAIR count:           {len(single_file_reps)}/{cr_count or 1}  (E70: 2/3)")
    print(f"  MULTI_FILE_REPAIR count:            {len(multi_file_reps)}/{cr_count or 1}  (E70: 1/3)")
    print(f"  NO_FILES_MODIFIED count:            {len(no_files_reps)}/{cr_count or 1}")
    print(f"  cli.py touched:                     {cli_touched}/{cr_count or 1}  (E70: 1/3)")
    print(f"  store.py touched:                   {store_touched}/{cr_count or 1}  (E70: 0/3)")
    print(f"  formatting.py touched:              {formatting_touched}/{cr_count or 1}  (E70: 3/3)")
    multi_rate = f"{len(multi_file_reps)/cr_count:.0%}" if cr_count else "N/A"
    print(f"  MULTI_FILE_REPAIR rate:             {multi_rate}")
    threshold_a = "PASS (>= 3/5)" if len(multi_file_reps) >= 3 else f"FAIL (< 3/5, got {len(multi_file_reps)}/{cr_count})"
    print(f"  Metric A threshold (>= 3/5 CR):     {threshold_a}")
    print()
    print("[E71A] API HALLUCINATION — METRIC B")
    print(f"  store.tasks occurrences:            {store_tasks_total}  (E70: 5)")
    print(f"  format_summary(store) occurrences:  {fmt_store_total}  (E70: 4)")
    print(f"  total_hallucinations:               {total_hallucinations}  (E70: 9)")
    print(f"  tasks with any hallucination:       {hallucination_tasks}/{cr_count or 1}  (E70: 3/3)")
    threshold_b = "PASS (<= 1)" if total_hallucinations <= 1 else f"FAIL (> 1, got {total_hallucinations})"
    print(f"  Metric B threshold (<= 1):          {threshold_b}")
    print()
    print("[E71A] STUB IMPLEMENTATION — METRIC C")
    print(f"  TaskStore.summary() implemented:    {summary_impl_count}/{cr_count or 1}  (E70: 0/3)")
    print(f"  formatting.py targeted:             {formatting_targeted_count}/{cr_count or 1}  (E70: 3/3)")
    print()
    print("[E71A] OLD_NOT_FOUND — METRIC D")
    print(f"  old_not_found count:                {old_not_found_count}/{cr_count or 1}  (E70: 2/3)")
    threshold_d = "PASS (<= 1/5)" if old_not_found_count <= 1 else f"FAIL (> 1/5, got {old_not_found_count}/{cr_count})"
    print(f"  Metric D threshold (<= 1/5):        {threshold_d}")
    print()
    print("[E71A] E69 SOURCE API CONTRACT COMPLIANCE")
    print(f"  contract present in prompt:         {contract_present}/{cr_count or 1}")
    print()
    print("[E71A] DECISION")

    # Case determination
    if completed_count >= 3 and total_hallucinations <= 2:
        case = "Case1_CONFIRM_LOCAL_MODEL_CAPACITY_LIMIT"
    elif completed_count >= 1 and total_hallucinations < 9:
        case = "Case2_LOCAL_MODEL_LIMIT_LIKELY_SOME_ARCHITECTURE_DEBT"
    elif completed_count == 0 and total_hallucinations >= 7:
        case = "Case3_MODEL_NOT_PRIMARY_BOTTLENECK"
    else:
        case = "PENDING_ANALYSIS"

    print(f"  Metric A (multi-file >= 3/5):       {'PASS' if len(multi_file_reps) >= 3 else 'FAIL'}")
    print(f"  Metric B (hallucinations <= 1):     {'PASS' if total_hallucinations <= 1 else 'FAIL'}")
    print(f"  Metric C (summary targeted):        {'PASS' if summary_impl_count > 0 else 'FAIL'}")
    print(f"  Metric D (old_not_found <= 1/5):    {'PASS' if old_not_found_count <= 1 else 'FAIL'}")
    print(f"  E71A Case:                          {case}")
    print()

    print("[E71A] PER-TASK RESULTS")
    print("=" * 80)
    hdr = (
        f"{'ID':<13} {'task':<6} {'sess':<6} {'status':<8} "
        f"{'category':<32} {'cr':<3} {'class':<20} "
        f"{'cli':<4} {'sto':<4} {'fmt':<4} "
        f"{'hall':<5} {'summ':<5} {'pytest'}"
    )
    print(hdr)
    for r in valid:
        print(
            f"{r['id']:<13} {str(r.get('task_id','?')):<6} {str(r.get('session_id','?')):<6} "
            f"{r['status']:<8} {r['category']:<32} "
            f"{'Y' if r.get('completion_repair_invoked') else 'N':<3} "
            f"{str(r.get('repair_class','')):<20} "
            f"{'Y' if r.get('ws_cli_touched') else 'N':<4} "
            f"{'Y' if r.get('ws_store_touched') else 'N':<4} "
            f"{'Y' if r.get('ws_formatting_touched') else 'N':<4} "
            f"{str(r.get('ws_api_hallucination_count',0)):<5} "
            f"{'Y' if r.get('ws_summary_stub_targeted') else 'N':<5} "
            f"{str(r.get('ws_pytest_result','?'))}"
        )
    print()
    print(f"[E71A] Case: {case}")


if __name__ == "__main__":
    main()
