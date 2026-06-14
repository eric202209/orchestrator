"""
Clean rerun continuation — retry existing tasks 919/920.

Tasks 919 (OFF R5) and 920 (ON R4) failed due to planning validation issues.
This script retries them, creates T2 if T1 succeeds, and writes the report.

Projects already exist:
  639 — wm-api-contract-parser-pilot-off-r5
  640 — wm-api-contract-parser-pilot-on-r4

Usage:
  python3 wm_clean_rerun_continue.py
"""

import json
import os
import re
import signal
import subprocess
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from app.auth import create_access_token  # noqa: E402
import requests  # noqa: E402

BASE_URL = "http://127.0.0.1:8080"
USER_EMAIL = os.environ.get("ORCHESTRATOR_USER_EMAIL", "eval@local.dev")

WORKSPACE_BASE = Path("/root/.openclaw/workspace/vault/projects")
REPORT_DIR = REPO_ROOT / "docs/roadmap/reports/maintenance"
RAW_DIR = REPO_ROOT / "docs/roadmap/reports/maintenance/project_aware_continuation_execution/working_memory"

SLOT_KEY = "orchestrator:backend_slots:local_openclaw"
PLANNING_CONTEXT_CAP = 400
DETERMINISTIC_PREFIX = "Task completed with verified execution evidence"

HEADERS: dict = {}

# Pre-existing state from first run attempt
OFF_PROJECT_ID   = 639
OFF_T1_TASK_ID   = 919
OFF_SLUG         = "wm-api-contract-parser-pilot-off-r5"
OFF_WORKSPACE    = WORKSPACE_BASE / OFF_SLUG

ON_PROJECT_ID    = 640
ON_T1_TASK_ID    = 920
ON_SLUG          = "wm-api-contract-parser-pilot-on-r4"
ON_WORKSPACE     = WORKSPACE_BASE / ON_SLUG

T2_TITLE = "Add format_amount formatter"
T2_DESC = """\
Add `format_amount(text: str) -> str` in `src/calclib/formatter.py`. \
Import and use the parser from T1. \
For a valid amount, return the parsed integer as a string. \
For an invalid amount, return the error code that the parser reports. \
Create `tests/test_formatter.py` with test cases for: \
valid positive integer, valid zero, valid negative integer, \
empty input, format error, overflow high, overflow low. \
Verify with: `PYTHONPATH=src python3 -m pytest tests/ -q`.\
"""


# ─────────────────────────────────────────────────────────────
# Auth / API
# ─────────────────────────────────────────────────────────────

def init_auth() -> None:
    global HEADERS
    token = create_access_token({"sub": USER_EMAIL})
    HEADERS = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _api(method: str, path: str, **kwargs):
    r = requests.request(method, f"{BASE_URL}{path}", headers=HEADERS, **kwargs)
    r.raise_for_status()
    return r.json()


# ─────────────────────────────────────────────────────────────
# Worker management
# ─────────────────────────────────────────────────────────────

def _kill_celery_workers() -> None:
    result = subprocess.run(["pgrep", "-f", "celery.*celery_app"],
                            capture_output=True, text=True)
    pids = [int(p) for p in result.stdout.strip().splitlines() if p.strip().isdigit()]
    if not pids:
        print("[worker] No workers found.")
        return
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    time.sleep(4)
    result2 = subprocess.run(["pgrep", "-f", "celery.*celery_app"],
                             capture_output=True, text=True)
    survivors = [int(p) for p in result2.stdout.strip().splitlines() if p.strip().isdigit()]
    for pid in survivors:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(2)
    print("[worker] Stopped.")


def start_worker(wm_on: bool) -> None:
    env = {
        **os.environ,
        "ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY": "1",
        "WORKING_MEMORY_PERSISTENCE_ENABLED": "True" if wm_on else "False",
        "WORKING_MEMORY_RENDER_ENABLED": "True" if wm_on else "False",
        "WORKING_MEMORY_INJECTION_ENABLED": "True" if wm_on else "False",
        "REPO_MEMORY_INJECTION_ENABLED": "False",
        "PSS_CONTINUATION_INJECTION_ENABLED": "False",
        "ARTIFACT_CONTINUATION_ENABLED": "False",
        "LANGFUSE_ENABLED": "false",
        "REDUCED_PLANNING_PROMPT_ENABLED": "False",
        "PLANNING_REPAIR_BASE_URL": os.environ.get("PLANNING_REPAIR_BASE_URL",
                                                    "http://ai-gateway:8000/v1"),
        "PLANNING_REPAIR_MODEL": os.environ.get("PLANNING_REPAIR_MODEL", "qwen-local"),
    }
    log_path = REPO_ROOT / "logs" / "worker.log"
    with open(log_path, "a") as fh:
        proc = subprocess.Popen(
            [str(REPO_ROOT / "venv" / "bin" / "celery"),
             "-A", "app.celery_app", "worker", "--loglevel=info"],
            env=env, cwd=str(REPO_ROOT), stdout=fh, stderr=fh, start_new_session=True,
        )
    time.sleep(10)
    print(f"[worker] Started PID={proc.pid} wm_on={wm_on}")


def verify_worker_env(wm_on: bool) -> dict:
    result = subprocess.run(["pgrep", "-f", "celery.*celery_app"],
                            capture_output=True, text=True)
    pids = [p.strip() for p in result.stdout.strip().splitlines() if p.strip().isdigit()]
    for pid in pids:
        try:
            env_raw = Path(f"/proc/{pid}/environ").read_bytes()
            env_vars = dict(
                item.split("=", 1)
                for item in env_raw.decode("utf-8", errors="replace").split("\x00")
                if "=" in item
            )
            if "WORKING_MEMORY_PERSISTENCE_ENABLED" not in env_vars:
                continue
            expected = "True" if wm_on else "False"
            ok = (
                env_vars.get("ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY") == "1"
                and env_vars.get("WORKING_MEMORY_PERSISTENCE_ENABLED") == expected
            )
            return {
                "pid": pid,
                "WORKING_MEMORY_PERSISTENCE_ENABLED":
                    env_vars.get("WORKING_MEMORY_PERSISTENCE_ENABLED"),
                "ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY":
                    env_vars.get("ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY"),
                "env_ok": ok,
            }
        except Exception:
            continue
    return {"error": "no matching worker", "env_ok": False}


def restart_worker(wm_on: bool) -> dict:
    print(f"\n[worker] Restarting WM {'ON' if wm_on else 'OFF'}...")
    _kill_celery_workers()
    start_worker(wm_on)
    env = verify_worker_env(wm_on)
    print(f"[worker] {env}")
    if not env.get("env_ok"):
        raise RuntimeError(f"Worker env mismatch: {env}")
    return env


# ─────────────────────────────────────────────────────────────
# Slot
# ─────────────────────────────────────────────────────────────

def wait_slot(poll: int = 15, timeout: int = 600) -> None:
    import redis as redis_lib
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    r = redis_lib.Redis()
    engine = create_engine(f"sqlite:///{REPO_ROOT}/orchestrator.db",
                           connect_args={"check_same_thread": False})
    DBSession = sessionmaker(bind=engine)
    TERMINAL = {"completed", "failed", "error", "cancelled", "expired"}

    def _members():
        try:
            return [int(m) for m in (r.smembers(SLOT_KEY) or set())]
        except Exception:
            return []

    def _evict():
        db = DBSession()
        try:
            for sid in _members():
                row = db.execute(text("SELECT status FROM sessions WHERE id=:id"),
                                 {"id": sid}).fetchone()
                status = row[0] if row else "not_found"
                if status in TERMINAL or status == "not_found":
                    r.srem(SLOT_KEY, str(sid))
                    print(f"  [slot] Evicted {sid} ({status})")
        finally:
            db.close()

    deadline = time.time() + timeout
    while time.time() < deadline:
        _evict()
        if not _members():
            print("[slot] Clear.")
            return
        print(f"[slot] Occupied by {_members()}. Waiting {poll}s...")
        time.sleep(poll)
    raise TimeoutError("Slot never freed")


# ─────────────────────────────────────────────────────────────
# Task helpers
# ─────────────────────────────────────────────────────────────

def dispatch_task(task_id: int) -> None:
    _api("POST", f"/api/v1/tasks/{task_id}/retry", json={})
    print(f"[dispatch] task {task_id}")


def create_task(project_id: int, title: str, desc: str, position: int) -> dict:
    t = _api("POST", "/api/v1/tasks", json={
        "project_id": project_id,
        "title": title,
        "description": desc,
        "plan_position": position,
        "execution_profile": "full_lifecycle",
    })
    print(f"[task] id={t['id']} pos={position} title={title!r}")
    return t


def poll_task(task_id: int, timeout: int = 1800, poll: int = 20) -> dict:
    deadline = time.time() + timeout
    elapsed = 0
    while time.time() < deadline:
        t = _api("GET", f"/api/v1/tasks/{task_id}")
        status = t.get("status", "")
        if status in ("done", "failed", "blocked_prior_task_failed"):
            print(f"  [{status}] at {elapsed}s")
            return t
        print(f"  [{status}] {elapsed}s")
        time.sleep(poll)
        elapsed += poll
    raise TimeoutError(f"Task {task_id} timed out after {timeout}s")


def read_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"(could not read: {e})"


# ─────────────────────────────────────────────────────────────
# Analysis helpers (imported from main runner)
# ─────────────────────────────────────────────────────────────

def assess_api_capture_8(summary_text: str) -> dict:
    raw = summary_text or ""
    text_lower = raw.lower()
    return {
        "parse_amount": "parse_amount" in raw,
        "ok_key": '"ok"' in raw or "'ok'" in raw or "ok:" in text_lower,
        "value_key": '"value"' in raw or "'value'" in raw or "value:" in text_lower,
        "code_key": '"code"' in raw or "'code'" in raw or "code:" in text_lower,
        "EMPTY_sentinel": '"EMPTY"' in raw or "'EMPTY'" in raw or "EMPTY" in raw,
        "FORMAT_sentinel": '"FORMAT"' in raw or "'FORMAT'" in raw or "FORMAT" in raw,
        "OVERFLOW_sentinel": '"OVERFLOW"' in raw or "'OVERFLOW'" in raw or "OVERFLOW" in raw,
        "never_raises": (
            "never raise" in text_lower or "never raises" in text_lower
            or "no exception" in text_lower or "doesn't raise" in text_lower
            or "does not raise" in text_lower
        ),
    }


def compute_trim_analysis(workspace_path: Path) -> dict:
    try:
        import logging
        from app.services.orchestration.working_memory import _render_working_memory_content
        logger = logging.getLogger(__name__)
        wm_rendered = _render_working_memory_content(str(workspace_path), logger)
        wm_len = len(wm_rendered)
        collapsed = " ".join((wm_rendered or "").split())
        trimmed = (
            collapsed[:PLANNING_CONTEXT_CAP - 3].rstrip() + "..."
            if len(collapsed) > PLANNING_CONTEXT_CAP else collapsed
        )

        def _pos(t: str) -> int:
            return collapsed.find(t)

        api_pos = _pos("API Contract:")
        summary_pos = _pos("Summary:")
        failure_pos = _pos("failure return")
        success_pos = _pos("success return")
        code_pos = _pos('"code"')
        if code_pos == -1:
            code_pos = _pos("'code'")
        empty_pos  = _pos("EMPTY")
        format_pos = _pos("FORMAT")
        overflow_pos = _pos("OVERFLOW")

        return {
            "wm_rendered": wm_rendered,
            "wm_rendered_len": wm_len,
            "collapsed_len": len(collapsed),
            "trimmed_content": trimmed,
            "api_contract_pos": api_pos,
            "summary_pos": summary_pos,
            "api_before_summary": api_pos != -1 and (summary_pos == -1 or api_pos < summary_pos),
            "failure_return_pos": failure_pos,
            "success_return_pos": success_pos,
            "failure_before_success": failure_pos != -1 and success_pos != -1 and failure_pos < success_pos,
            "code_pos": code_pos,
            "code_in_250": code_pos != -1 and code_pos < 250,
            "code_in_400": code_pos != -1 and code_pos < PLANNING_CONTEXT_CAP,
            "EMPTY_pos": empty_pos, "FORMAT_pos": format_pos, "OVERFLOW_pos": overflow_pos,
            "EMPTY_in_400": empty_pos != -1 and empty_pos < PLANNING_CONTEXT_CAP,
            "FORMAT_in_400": format_pos != -1 and format_pos < PLANNING_CONTEXT_CAP,
            "OVERFLOW_in_400": overflow_pos != -1 and overflow_pos < PLANNING_CONTEXT_CAP,
            "all_sentinels_in_400": (
                empty_pos != -1 and empty_pos < PLANNING_CONTEXT_CAP
                and format_pos != -1 and format_pos < PLANNING_CONTEXT_CAP
                and overflow_pos != -1 and overflow_pos < PLANNING_CONTEXT_CAP
            ),
        }
    except Exception as e:
        return {"error": str(e), "code_in_250": False, "all_sentinels_in_400": False}


def check_t1_implementation(workspace_path: Path) -> dict:
    parser_py = workspace_path / "src" / "calclib" / "parser.py"
    if not parser_py.exists():
        return {"exists": False, "defines_parse_amount": False, "text": ""}
    text = read_safe(parser_py)
    return {
        "exists": True,
        "defines_parse_amount": "def parse_amount" in text,
        "returns_dict": '{"ok"' in text or "'ok':" in text,
        "text": text[:600],
    }


def run_independent_pytest(workspace_path: Path) -> dict:
    try:
        result = subprocess.run(
            ["python3", "-m", "pytest", "tests/test_parser.py", "-q", "--tb=short"],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "PYTHONPATH": str(workspace_path / "src")},
            cwd=str(workspace_path),
        )
        return {"returncode": result.returncode,
                "stdout": result.stdout[-600:],
                "passed": result.returncode == 0}
    except Exception as e:
        return {"returncode": -1, "stdout": str(e), "passed": False}


def extract_last3_behavior(workspace_path: Path) -> dict:
    try:
        from app.services.project.source_imports import python_test_source_context_from_tests
        ctx = python_test_source_context_from_tests(workspace_path)
        return {
            "context": ctx or "",
            "code_leaks": '"code"' in (ctx or "") or "'code'" in (ctx or ""),
            "value_leaks": '"value"' in (ctx or "") or "'value'" in (ctx or ""),
            "context_len": len(ctx or ""),
        }
    except Exception as e:
        return {"error": str(e), "code_leaks": False, "value_leaks": False, "context": ""}


def extract_first_plan_formatter(task_id: int) -> str:
    from app.database import SessionLocal
    from app.models import Task as TaskModel
    db = SessionLocal()
    try:
        t = db.query(TaskModel).filter(TaskModel.id == task_id).first()
        if t is None or not t.steps:
            return ""
        steps = t.steps if isinstance(t.steps, list) else json.loads(t.steps)
        for step in steps:
            for op in (step.get("ops") or []):
                if op.get("op") == "write_file" and "formatter" in op.get("path", ""):
                    return op.get("content", "")
        return ""
    except Exception as e:
        return f"(error: {e})"
    finally:
        db.close()


def assess_formatter_fields(text: str) -> dict:
    t = text or ""
    uses_code  = '["code"]'  in t or "['code']"  in t or '.get("code")'  in t or ".get('code')"  in t
    uses_error = '["error"]' in t or "['error']" in t or '.get("error")' in t or ".get('error')" in t
    uses_ok    = '["ok"]'    in t or "['ok']"    in t or '.get("ok")'    in t or ".get('ok')"    in t
    uses_value = '["value"]' in t or "['value']" in t or '.get("value")' in t or ".get('value')" in t
    verbatim_empty    = '"EMPTY"'    in t or "'EMPTY'"    in t
    verbatim_format   = '"FORMAT"'   in t or "'FORMAT'"   in t
    verbatim_overflow = '"OVERFLOW"' in t or "'OVERFLOW'" in t
    if uses_code and not uses_error:
        first_field = "code"
    elif uses_error and not uses_code:
        first_field = "error"
    elif uses_code and uses_error:
        first_field = "both"
    else:
        first_field = "neither"
    return {
        "uses_code": uses_code, "uses_error": uses_error,
        "uses_ok": uses_ok, "uses_value": uses_value,
        "first_field": first_field,
        "verbatim_EMPTY": verbatim_empty, "verbatim_FORMAT": verbatim_format,
        "verbatim_OVERFLOW": verbatim_overflow,
        "verbatim_codes": verbatim_empty or verbatim_format or verbatim_overflow,
    }


def count_repairs_from_report(report_path: Path) -> dict:
    if not report_path.exists():
        return {"debug_repairs": -1, "planning_repairs": -1}
    text = report_path.read_text(encoding="utf-8", errors="replace")
    return {
        "debug_repairs": text.count("[DEBUG_REPAIR_DIRECT] attempting"),
        "planning_repairs": text.count("[REPAIR_DIRECT] completed direct structured repair"),
    }


def scan_injection_log(worker_log: Path, log_offset: int) -> dict:
    if not worker_log.exists():
        return {"found": False}
    lines = worker_log.read_text(encoding="utf-8", errors="replace").splitlines()
    for line in reversed(lines[log_offset:]):
        if "[WORKING_MEMORY] Injected" in line and "project_context" in line:
            m = re.search(r"Injected (\d+) chars.*plan_position=(\S+?)\)", line)
            if m:
                return {"found": True, "chars": int(m.group(1)),
                        "plan_position": m.group(2).rstrip(")")}
    return {"found": False}


def scan_regression_checks(worker_log: Path, log_offset: int) -> dict:
    if not worker_log.exists():
        return {}
    text = "\n".join(
        worker_log.read_text(encoding="utf-8", errors="replace").splitlines()[log_offset:]
    )
    return {
        "pip_show": "pip show" in text.lower(),
        "nested_project_folder": "nested_project_folder" in text,
        "path_guard_advisory": "PATH_GUARD" in text,
        "backend_capacity": "backend_capacity" in text.lower(),
        "vma_error": "[VMA]" in text or "verification_mutates_source" in text,
        "empty_model_response": bool(re.search(r"empty.*response", text, re.I)),
    }


def get_log_offset(worker_log: Path) -> int:
    if not worker_log.exists():
        return 0
    return len(worker_log.read_text(encoding="utf-8", errors="replace").splitlines())


# ─────────────────────────────────────────────────────────────
# Arm runner (retries existing task, creates T2 if needed)
# ─────────────────────────────────────────────────────────────

def run_arm(arm: str, t1_id: int, project_id: int, workspace_path: Path) -> dict:
    wm_on = arm == "on"
    slug = ON_SLUG if wm_on else OFF_SLUG
    wm_path = workspace_path / ".agent" / "working_memory.json"
    agent_dir = workspace_path / ".agent"

    print(f"\n{'='*65}")
    print(f"ARM: WM {'ON' if wm_on else 'OFF'} — retrying T1={t1_id}")
    print(f"{'='*65}")

    worker_log = REPO_ROOT / "logs" / "worker.log"
    worker_env = restart_worker(wm_on)
    log_offset = get_log_offset(worker_log)

    commit_sha = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, text=True
    ).strip()

    init_auth()
    wait_slot()

    # Retry T1
    t1_report_path = agent_dir / "task-reports" / f"task_report_{t1_id}.md"
    print(f"[T1] Retrying task {t1_id}")
    t1_start = time.time()
    dispatch_task(t1_id)
    t1_result = poll_task(t1_id)
    t1_elapsed = round(time.time() - t1_start, 1)
    print(f"[T1] {t1_result.get('status')} in {t1_elapsed}s")

    wm_data = {}
    if wm_path.exists():
        try:
            wm_data = json.loads(wm_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    strategies = wm_data.get("implementation_strategy") or []
    t1_wm_summary = strategies[-1].get("summary", "") if strategies else ""
    t1_is_det = t1_wm_summary.startswith(DETERMINISTIC_PREFIX)
    t1_api_capture = assess_api_capture_8(t1_wm_summary)
    t1_api_score = sum(1 for v in t1_api_capture.values() if v)
    t1_repairs = count_repairs_from_report(t1_report_path)
    t1_impl = check_t1_implementation(workspace_path)
    t1_pytest = run_independent_pytest(workspace_path)
    test_source = extract_last3_behavior(workspace_path)

    print(f"  API score:         {t1_api_score}/8")
    print(f"  impl parse_amount: {t1_impl['defines_parse_amount']}")
    print(f"  pytest rerun:      passed={t1_pytest['passed']}")
    print(f"  debug repairs:     {t1_repairs['debug_repairs']}")
    print(f"  planning repairs:  {t1_repairs['planning_repairs']}")
    print(f"  code_leaks:        {test_source.get('code_leaks')}")

    trim_analysis = {}
    if wm_on and wm_path.exists():
        trim_analysis = compute_trim_analysis(workspace_path)
        print(f"  code_pos:          {trim_analysis.get('code_pos')}")
        print(f"  code_in_250:       {trim_analysis.get('code_in_250')}")
        print(f"  all_sentinels_400: {trim_analysis.get('all_sentinels_in_400')}")
        print(f"  api_before_summ:   {trim_analysis.get('api_before_summary')}")
        print(f"  Trimmed: {trim_analysis.get('trimmed_content','')}")

    # T2
    if t1_result.get("status") != "done" or not t1_impl["defines_parse_amount"]:
        reason = "T1 not DONE" if t1_result.get("status") != "done" else "impl invalid"
        print(f"\n[T2] SKIP — {reason}")
        t2_result = {"status": "skipped"}
        t2_id = -1
        t2_elapsed = 0
        formatter_first_plan = ""
        formatter_final = ""
        t2_first_fields = assess_formatter_fields("")
        t2_final_fields = assess_formatter_fields("")
        t2_repairs = {"debug_repairs": 0, "planning_repairs": 0}
    else:
        wait_slot()
        t2 = create_task(project_id, T2_TITLE, T2_DESC, 2)
        t2_id = t2["id"]
        t2_report_path = agent_dir / "task-reports" / f"task_report_{t2_id}.md"
        print(f"\n[T2] Dispatching {t2_id}")
        t2_start = time.time()
        dispatch_task(t2_id)
        t2_result = poll_task(t2_id)
        t2_elapsed = round(time.time() - t2_start, 1)
        print(f"[T2] {t2_result.get('status')} in {t2_elapsed}s")

        formatter_path = workspace_path / "src" / "calclib" / "formatter.py"
        formatter_final = read_safe(formatter_path) if formatter_path.exists() else ""
        formatter_first_plan = extract_first_plan_formatter(t2_id)
        t2_first_fields = assess_formatter_fields(formatter_first_plan)
        t2_final_fields = assess_formatter_fields(formatter_final)
        t2_repairs = count_repairs_from_report(t2_report_path)

        print(f"  first_field: {t2_first_fields['first_field']}")
        print(f"  uses_code:   {t2_first_fields['uses_code']}")
        print(f"  uses_error:  {t2_first_fields['uses_error']}")
        print(f"  verbatim:    {t2_first_fields['verbatim_codes']}")

    injection_log = scan_injection_log(worker_log, log_offset) if wm_on else {"found": False}
    regression = scan_regression_checks(worker_log, log_offset)

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    run_label = "r4" if wm_on else "r5"
    raw_filename = f"wm-api-contract-clean-rerun-{arm}-{run_label}-raw-{timestamp}.json"
    raw_path = RAW_DIR / raw_filename

    raw = {
        "arm": arm, "run_label": run_label, "timestamp": timestamp,
        "commit_sha": commit_sha,
        "project_id": project_id, "workspace_slug": slug,
        "worker_env": worker_env, "flags_ok": worker_env.get("env_ok", False),
        "t1_task_id": t1_id, "t2_task_id": t2_id,
        "t1_status": t1_result.get("status"), "t2_status": t2_result.get("status"),
        "t1_elapsed_s": t1_elapsed, "t2_elapsed_s": t2_elapsed,
        "t1": {
            "status": t1_result.get("status"), "elapsed_s": t1_elapsed,
            "wm_json_exists": wm_path.exists(),
            "llm_summary": t1_wm_summary, "is_deterministic": t1_is_det,
            "api_capture": t1_api_capture, "api_score": t1_api_score,
            "defines_parse_amount": t1_impl["defines_parse_amount"],
            "parser_text": t1_impl.get("text", ""),
            "pytest_rerun_passed": t1_pytest["passed"],
            "pytest_rerun_output": t1_pytest.get("stdout", ""),
            "debug_repairs": t1_repairs["debug_repairs"],
            "planning_repairs": t1_repairs["planning_repairs"],
            "test_source_injection": test_source,
        },
        "trim_analysis": trim_analysis, "injection_log": injection_log,
        "t2": {
            "status": t2_result.get("status"), "elapsed_s": t2_elapsed,
            "formatter_first_plan": formatter_first_plan[:800],
            "formatter_final": formatter_final[:800],
            "first_plan_fields": t2_first_fields, "final_fields": t2_final_fields,
            "debug_repairs": t2_repairs.get("debug_repairs", 0),
            "planning_repairs": t2_repairs.get("planning_repairs", 0),
        },
        "regression_checks": regression,
        "_summary": {
            "t1_done": t1_result.get("status") == "done",
            "t1_api_score": t1_api_score,
            "t1_impl_ok": t1_impl["defines_parse_amount"],
            "t1_pytest_ok": t1_pytest["passed"],
            "t2_done": t2_result.get("status") == "done",
            "t2_first_field": t2_first_fields["first_field"],
            "t2_final_field": t2_final_fields["first_field"],
            "t2_verbatim_codes": t2_first_fields["verbatim_codes"],
            "code_in_250": trim_analysis.get("code_in_250"),
            "all_sentinels_in_400": trim_analysis.get("all_sentinels_in_400"),
            "api_before_summary": trim_analysis.get("api_before_summary"),
            "test_source_code_leak": test_source.get("code_leaks"),
            "injection_found": injection_log.get("found"),
        },
    }

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[raw] {raw_path}")
    return raw


# ─────────────────────────────────────────────────────────────
# Report
# ─────────────────────────────────────────────────────────────

def generate_report(off_raw: dict, on_raw: dict) -> None:
    today = time.strftime("%Y-%m-%d")
    commit = on_raw.get("commit_sha", "?")
    off_s = off_raw.get("_summary", {})
    on_s  = on_raw.get("_summary", {})
    off_t1 = off_raw.get("t1", {})
    on_t1  = on_raw.get("t1", {})
    off_t2 = off_raw.get("t2", {})
    on_t2  = on_raw.get("t2", {})
    on_trim = on_raw.get("trim_analysis", {})
    on_inj  = on_raw.get("injection_log", {})

    off_first = off_s.get("t2_first_field", "unknown")
    on_first  = on_s.get("t2_first_field", "unknown")
    on_code_in_250 = on_s.get("code_in_250")
    on_sentinels   = on_s.get("all_sentinels_in_400")
    on_api_before  = on_s.get("api_before_summary")

    # Verdict
    if off_first in ("error", "neither") and on_first == "code" and on_code_in_250:
        verdict = "STRONG PASS" if off_first == "error" else "PASS"
        verdict_text = (
            f"WM OFF first field=`{off_first}`. "
            f"WM ON first field=`code` (correct). "
            f"`code` visible at char {on_trim.get('code_pos')} (within 250). "
            "WM injection changed planner behavior in the expected direction."
        )
    elif not on_s.get("t1_done"):
        verdict = "NULL / INCONCLUSIVE"
        verdict_text = "T1 WM ON did not complete (failed/skipped)."
    elif not off_s.get("t1_done"):
        verdict = "NULL / INCONCLUSIVE"
        verdict_text = "T1 WM OFF did not complete — no clean baseline."
    elif not on_code_in_250:
        verdict = "NULL / INCONCLUSIVE"
        verdict_text = (
            f"`code` at char {on_trim.get('code_pos','?')} — not within 250. "
            "Render-first fix may not have applied."
        )
    elif off_first == "code":
        verdict = "NULL / INCONCLUSIVE"
        verdict_text = (
            "WM OFF baseline used `code` — task description or test source leaked the key."
        )
    elif on_first == "error" and on_code_in_250:
        verdict = "FAIL"
        verdict_text = (
            f"`code` visible at char {on_trim.get('code_pos')} but WM ON still used "
            "`result[\"error\"]`. Injection did not change behavior."
        )
    elif on_first == off_first:
        verdict = "NULL / INCONCLUSIVE"
        verdict_text = (
            f"Both arms used `{on_first}`. WM injection did not change behavior."
        )
    else:
        verdict = "NULL / INCONCLUSIVE"
        verdict_text = (
            f"WM OFF=`{off_first}`, WM ON=`{on_first}`. Review manually."
        )

    off_api = off_t1.get("api_capture", {})
    on_api  = on_t1.get("api_capture", {})
    off_reg = off_raw.get("regression_checks", {})
    on_reg  = on_raw.get("regression_checks", {})
    off_we  = off_raw.get("worker_env", {})
    on_we   = on_raw.get("worker_env", {})

    lines = [
        "# WM API-Contract Parser Pilot — Clean Rerun (OFF R5 / ON R4)",
        "",
        f"**Date:** {today}",
        f"**Commit:** `{commit}`",
        f"**Verdict: {verdict}**",
        "",
        "---",
        "",
        "## Context",
        "",
        "Clean rerun after three sequential fixes applied to commit `7f17a91`:",
        "1. LLM summary quality fix — API contract score 8/8",
        "2. Failure-first prompt fix — `failure return` before `success return` in WM render",
        "3. Render-first render fix — API Contract section extracted before prose deterministically",
        "",
        "Prior OFF R4 contaminated by T1 hallucinating `parse(expr) -> float`. Fresh projects used.",
        "Tasks 919 (OFF) and 920 (ON) initially failed due to Qwen planning validation issues.",
        "This report covers the retry run.",
        "",
        "---",
        "",
        "## Configuration",
        "",
        "| Setting | WM OFF R5 | WM ON R4 |",
        "|---|---|---|",
        "| `ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY` | `1` | `1` |",
        "| `WORKING_MEMORY_PERSISTENCE_ENABLED` | `False` | `True` |",
        "| `WORKING_MEMORY_RENDER_ENABLED` | `False` | `True` |",
        "| `WORKING_MEMORY_INJECTION_ENABLED` | `False` | `True` |",
        "| All other continuation flags | `False` | `False` |",
        f"| Project | `{off_raw.get('workspace_slug')}` (id={off_raw.get('project_id')}) | `{on_raw.get('workspace_slug')}` (id={on_raw.get('project_id')}) |",
        f"| T1 task id | {off_raw.get('t1_task_id')} | {on_raw.get('t1_task_id')} |",
        f"| T2 task id | {off_raw.get('t2_task_id')} | {on_raw.get('t2_task_id')} |",
        "",
        "### Worker Env Verified",
        "",
        "| Flag | WM OFF R5 | WM ON R4 |",
        "|---|---|---|",
    ]
    for k in ["ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY",
              "WORKING_MEMORY_PERSISTENCE_ENABLED",
              "WORKING_MEMORY_RENDER_ENABLED",
              "WORKING_MEMORY_INJECTION_ENABLED"]:
        lines.append(f"| `{k}` | `{off_we.get(k,'?')}` | `{on_we.get(k,'?')}` |")

    lines += [
        "",
        "---",
        "",
        "## T1 Results",
        "",
        "| Metric | WM OFF R5 | WM ON R4 |",
        "|---|---|---|",
        f"| Status | **{off_t1.get('status')}** | **{on_t1.get('status')}** |",
        f"| Elapsed | {off_raw.get('t1_elapsed_s')}s | {on_raw.get('t1_elapsed_s')}s |",
        f"| Defines `parse_amount` | {off_t1.get('defines_parse_amount')} | {on_t1.get('defines_parse_amount')} |",
        f"| Independent pytest | {off_t1.get('pytest_rerun_passed')} | {on_t1.get('pytest_rerun_passed')} |",
        f"| LLM summary (not det.) | {not off_t1.get('is_deterministic')} | {not on_t1.get('is_deterministic')} |",
        f"| API contract score | **{off_t1.get('api_score')}/8** | **{on_t1.get('api_score')}/8** |",
        f"| WM JSON stored | N/A | {on_t1.get('wm_json_exists')} |",
        f"| Debug repairs | {off_t1.get('debug_repairs')} | {on_t1.get('debug_repairs')} |",
        f"| Planning repairs | {off_t1.get('planning_repairs')} | {on_t1.get('planning_repairs')} |",
        "",
        "### API Contract Capture",
        "",
        "| Indicator | WM OFF R5 | WM ON R4 |",
        "|---|---|---|",
    ]
    for key in ["parse_amount","ok_key","value_key","code_key",
                "EMPTY_sentinel","FORMAT_sentinel","OVERFLOW_sentinel","never_raises"]:
        ov = "✓" if off_api.get(key) else "✗"
        nv = "✓" if on_api.get(key)  else "✗"
        lines.append(f"| `{key}` | {ov} | {nv} |")

    off_llm = (off_t1.get("llm_summary") or "").strip()
    on_llm  = (on_t1.get("llm_summary")  or "").strip()
    lines += [
        "",
        "### T1 LLM Summary — WM OFF R5",
        "```",
        off_llm[:800] or "(not captured)",
        "```",
        "",
        "### T1 LLM Summary — WM ON R4",
        "```",
        on_llm[:800] or "(not captured)",
        "```",
        "",
        "### T1 Parser Implementation — WM OFF R5",
        "```python",
        (off_t1.get("parser_text") or "(not found)")[:400],
        "```",
        "",
        "### T1 Parser Implementation — WM ON R4",
        "```python",
        (on_t1.get("parser_text") or "(not found)")[:400],
        "```",
        "",
        "### Independent pytest",
        "**WM OFF R5:**",
        "```",
        (off_t1.get("pytest_rerun_output") or "(not run)")[:300],
        "```",
        "**WM ON R4:**",
        "```",
        (on_t1.get("pytest_rerun_output") or "(not run)")[:300],
        "```",
        "",
        "---",
        "",
        "## Test Source Injection (Leak Control)",
        "",
        "| Metric | WM OFF R5 | WM ON R4 |",
        "|---|---|---|",
        f"| `code` leaks | {off_s.get('test_source_code_leak')} | {on_s.get('test_source_code_leak')} |",
        f"| `value` leaks | {off_t1.get('test_source_injection',{}).get('value_leaks')} | {on_t1.get('test_source_injection',{}).get('value_leaks')} |",
        "",
        "**WM OFF R5 test source:**",
        "```",
        (off_t1.get("test_source_injection",{}).get("context") or "(empty)")[:400],
        "```",
        "**WM ON R4 test source:**",
        "```",
        (on_t1.get("test_source_injection",{}).get("context") or "(empty)")[:400],
        "```",
        "",
        "---",
    ]

    if on_trim:
        lines += [
            "",
            "## WM Trim Analysis (WM ON R4)",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| Rendered len | {on_trim.get('wm_rendered_len')} chars |",
            f"| Collapsed len | {on_trim.get('collapsed_len')} chars |",
            f"| Planning context cap | 400 chars |",
            f"| `API Contract:` pos | **{on_trim.get('api_contract_pos')}** |",
            f"| `Summary:` pos | {on_trim.get('summary_pos')} |",
            f"| API Contract before Summary | **{on_trim.get('api_before_summary')}** |",
            f"| `failure return:` pos | {on_trim.get('failure_return_pos')} |",
            f"| `success return:` pos | {on_trim.get('success_return_pos')} |",
            f"| failure before success | **{on_trim.get('failure_before_success')}** |",
            f"| `\"code\"` pos | **{on_trim.get('code_pos')}** |",
            f"| `\"code\"` within 250 | **{on_trim.get('code_in_250')}** |",
            f"| `EMPTY` pos | {on_trim.get('EMPTY_pos')} |",
            f"| `FORMAT` pos | {on_trim.get('FORMAT_pos')} |",
            f"| `OVERFLOW` pos | {on_trim.get('OVERFLOW_pos')} |",
            f"| All sentinels in 400 | **{on_trim.get('all_sentinels_in_400')}** |",
            f"| WM injected | {on_inj.get('found')} ({on_inj.get('chars','?')} chars) |",
            "",
            "**Rendered WM block:**",
            "```",
            (on_trim.get("wm_rendered") or "")[:700],
            "```",
            "",
            "**400-char trimmed content (T2 planner view):**",
            "```",
            on_trim.get("trimmed_content", "")[:500],
            "```",
            "",
            "---",
        ]

    lines += [
        "",
        "## T2 Results",
        "",
        "| Metric | WM OFF R5 | WM ON R4 |",
        "|---|---|---|",
        f"| Status | **{off_t2.get('status')}** | **{on_t2.get('status')}** |",
        f"| Elapsed | {off_raw.get('t2_elapsed_s')}s | {on_raw.get('t2_elapsed_s')}s |",
        f"| **First plan field** | **{off_s.get('t2_first_field','?')}** | **{on_s.get('t2_first_field','?')}** |",
        f"| Final field | {off_s.get('t2_final_field','?')} | {on_s.get('t2_final_field','?')} |",
        f"| Verbatim codes (EMPTY/FORMAT/OVERFLOW) | {off_s.get('t2_verbatim_codes')} | {on_s.get('t2_verbatim_codes')} |",
        f"| Debug repairs | {off_t2.get('debug_repairs')} | {on_t2.get('debug_repairs')} |",
        f"| Planning repairs | {off_t2.get('planning_repairs')} | {on_t2.get('planning_repairs')} |",
        "",
        "### T2 First Plan — WM OFF R5",
        "```python",
        (off_t2.get("formatter_first_plan") or "(not captured)")[:600],
        "```",
        "",
        "### T2 First Plan — WM ON R4",
        "```python",
        (on_t2.get("formatter_first_plan") or "(not captured)")[:600],
        "```",
        "",
        "### T2 Final Formatter — WM OFF R5",
        "```python",
        (off_t2.get("formatter_final") or "(not found)")[:500],
        "```",
        "",
        "### T2 Final Formatter — WM ON R4",
        "```python",
        (on_t2.get("formatter_final") or "(not found)")[:500],
        "```",
        "",
        "---",
        "",
        "## Signal Comparison",
        "",
        "| Signal | WM OFF R5 | WM ON R4 |",
        "|---|---|---|",
        f"| T1 status | {off_t1.get('status')} | {on_t1.get('status')} |",
        f"| T1 API score | {off_t1.get('api_score')}/8 | {on_t1.get('api_score')}/8 |",
        f"| T1 impl ok | {off_t1.get('defines_parse_amount')} | {on_t1.get('defines_parse_amount')} |",
        f"| Test source `code` leak | {off_s.get('test_source_code_leak')} | {on_s.get('test_source_code_leak')} |",
        f"| WM injected | N/A | {on_s.get('injection_found','N/A')} |",
        f"| `code` in 250 chars | N/A | {on_code_in_250} |",
        f"| All sentinels in 400 chars | N/A | {on_sentinels} |",
        f"| API Contract before prose | N/A | {on_api_before} |",
        f"| **T2 first plan field** | **{off_first}** | **{on_first}** |",
        f"| T2 debug repairs | {off_t2.get('debug_repairs')} | {on_t2.get('debug_repairs')} |",
        f"| T2 planning repairs | {off_t2.get('planning_repairs')} | {on_t2.get('planning_repairs')} |",
        f"| T2 verbatim codes | {off_s.get('t2_verbatim_codes')} | {on_s.get('t2_verbatim_codes')} |",
        "",
        "---",
        "",
        "## Verdict",
        "",
        f"**{verdict}**",
        "",
        verdict_text,
        "",
        "---",
        "",
        "## Regression Checks",
        "",
        "| Check | WM OFF R5 | WM ON R4 |",
        "|---|---|---|",
    ]
    for k in sorted(set(list(off_reg.keys()) + list(on_reg.keys()))):
        lines.append(f"| `{k}` | {off_reg.get(k,'N/A')} | {on_reg.get(k,'N/A')} |")

    lines += [
        "",
        "---",
        "",
        "## Limiting Factor History",
        "",
        "| Phase | Factor | Status |",
        "|---|---|---|",
        "| Initial WM pilot | LLM summary quality — 0/6 API contract score | **Resolved** |",
        "| OFF R3 / ON R2 | 400-char trim clips `code` at char ~431 | **Resolved** |",
        "| OFF R4 / ON R3 | Prose pushes `code` to char 394 (truncated to `co`) | **Resolved** |",
        f"| This rerun (OFF R5 / ON R4) | See verdict above | **{verdict}** |",
        "",
        "---",
        "",
        "## Raw Data",
        "",
        f"- OFF R5: `{RAW_DIR.name}/wm-api-contract-clean-rerun-off-r5-raw-*.json`",
        f"- ON R4:  `{RAW_DIR.name}/wm-api-contract-clean-rerun-on-r4-raw-*.json`",
    ]

    report_path = REPORT_DIR / "working-memory-api-contract-parser-pilot-clean-rerun-20260614.md"
    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[report] {report_path}")
    print(f"Verdict: {verdict}")


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def main():
    print("[continue] Retrying tasks 919 (OFF) and 920 (ON)")

    off_raw = run_arm(
        arm="off",
        t1_id=OFF_T1_TASK_ID,
        project_id=OFF_PROJECT_ID,
        workspace_path=OFF_WORKSPACE,
    )

    on_raw = run_arm(
        arm="on",
        t1_id=ON_T1_TASK_ID,
        project_id=ON_PROJECT_ID,
        workspace_path=ON_WORKSPACE,
    )

    generate_report(off_raw, on_raw)

    print("\n=== SUMMARY ===")
    print(f"OFF first_field: {off_raw['_summary']['t2_first_field']}")
    print(f"ON  first_field: {on_raw['_summary']['t2_first_field']}")
    print(f"ON  code_in_250: {on_raw['_summary']['code_in_250']}")
    print(f"ON  sentinels:   {on_raw['_summary']['all_sentinels_in_400']}")


if __name__ == "__main__":
    main()
