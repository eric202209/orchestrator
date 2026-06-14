"""
WM API-Contract Parser Pilot Runner — WM OFF R4 / WM ON R3

Rerun after the failure-first TASK_SUMMARY fix (commit 4b19b69).
Measurement only — no code changes.

Usage:
  python3 wm_parser_pilot_r4r3_runner.py --arm=off   # run WM OFF R4
  python3 wm_parser_pilot_r4r3_runner.py --arm=on    # run WM ON R3
  python3 wm_parser_pilot_r4r3_runner.py --report    # combine raw files → report

WM OFF arm — restart worker with:
  ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY=1
  WORKING_MEMORY_PERSISTENCE_ENABLED=False
  WORKING_MEMORY_RENDER_ENABLED=False
  WORKING_MEMORY_INJECTION_ENABLED=False
  REPO_MEMORY_INJECTION_ENABLED=False
  PSS_CONTINUATION_INJECTION_ENABLED=False
  ARTIFACT_CONTINUATION_ENABLED=False
  LANGFUSE_ENABLED=false
  REDUCED_PLANNING_PROMPT_ENABLED=False

WM ON arm — restart worker with:
  ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY=1
  WORKING_MEMORY_PERSISTENCE_ENABLED=True
  WORKING_MEMORY_RENDER_ENABLED=True
  WORKING_MEMORY_INJECTION_ENABLED=True
  (other flags same as OFF)
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
COMMIT_SHA = subprocess.check_output(
    ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, text=True
).strip()

WORKSPACE_SLUG_OFF = "wm-api-contract-parser-pilot-off-r4"
WORKSPACE_SLUG_ON = "wm-api-contract-parser-pilot-on-r3"

WORKSPACE_BASE = Path("/root/.openclaw/workspace/vault/projects")
REPORT_DIR = REPO_ROOT / "docs/roadmap/reports/maintenance/project_aware_continuation_execution/working_memory"
SLOT_KEY = "orchestrator:backend_slots:local_openclaw"

DETERMINISTIC_PREFIX = "Task completed with verified execution evidence"
PLANNING_CONTEXT_CAP = 400  # _shape_project_context: max_chars=800 → 800//2

HEADERS: dict = {}

# ─────────────────────────────────────────────────────────────
# Task descriptions
# ─────────────────────────────────────────────────────────────

T1_TITLE = "Bootstrap parse_amount parser"
T1_DESC = """\
Create `src/calclib/parser.py` with `parse_amount(text: str) -> dict`.

Return `{"ok": True, "value": int}` when `text` is a valid integer string \
(after stripping whitespace).

Return `{"ok": False, "code": str}` for failure cases:
- `"EMPTY"` for blank input
- `"FORMAT"` for non-integer content
- `"OVERFLOW"` for values outside -999999 to 999999 inclusive

Never raise an exception for invalid input.

Create `src/calclib/__init__.py` re-exporting `parse_amount`.

Create `pytest.ini` at project root with `pythonpath = src`.

Create `tests/test_parser.py` with at least 12 test cases:
- empty input
- format errors
- overflow high
- overflow low
- positive integer
- zero
- negative integer
- whitespace trimming

Important test ordering: \
Put code-revealing assertions early in the file (checking `result["code"] == "EMPTY"`, \
`result["code"] == "FORMAT"`, `result["code"] == "OVERFLOW"`). \
Put at least 4 padding assertions at the END of the test file \
that only check `result["ok"]`, not `result["code"]` or `result["value"]`.

Verify with: `PYTHONPATH=src python3 -m pytest tests/test_parser.py -q`\
"""

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
# Auth / API helpers
# ─────────────────────────────────────────────────────────────

def _api(method: str, path: str, **kwargs):
    r = requests.request(method, f"{BASE_URL}{path}", headers=HEADERS, **kwargs)
    r.raise_for_status()
    return r.json()


def init_auth() -> None:
    global HEADERS
    token = create_access_token({"sub": USER_EMAIL})
    HEADERS = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    print(f"[init] Auth for {USER_EMAIL}  commit={COMMIT_SHA}")


# ─────────────────────────────────────────────────────────────
# Worker management
# ─────────────────────────────────────────────────────────────

def _kill_celery_workers() -> None:
    """SIGTERM all celery worker processes."""
    result = subprocess.run(
        ["pgrep", "-f", "celery.*app.celery_app"],
        capture_output=True, text=True
    )
    pids = [int(p) for p in result.stdout.strip().splitlines() if p.strip().isdigit()]
    if not pids:
        print("[worker] No celery workers found.")
        return
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
            print(f"[worker] Sent SIGTERM to {pid}")
        except ProcessLookupError:
            pass
    time.sleep(3)
    # Force-kill any survivors
    result2 = subprocess.run(
        ["pgrep", "-f", "celery.*app.celery_app"],
        capture_output=True, text=True
    )
    survivors = [int(p) for p in result2.stdout.strip().splitlines() if p.strip().isdigit()]
    for pid in survivors:
        try:
            os.kill(pid, signal.SIGKILL)
            print(f"[worker] Sent SIGKILL to {pid}")
        except ProcessLookupError:
            pass
    time.sleep(2)
    print("[worker] Workers stopped.")


def start_worker(wm_on: bool) -> None:
    """Start celery worker with appropriate WM flags."""
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
        "PLANNING_REPAIR_BASE_URL": os.environ.get("PLANNING_REPAIR_BASE_URL", "http://ai-gateway:8000/v1"),
        "PLANNING_REPAIR_MODEL": os.environ.get("PLANNING_REPAIR_MODEL", "qwen-local"),
    }
    cmd = [
        str(REPO_ROOT / "venv" / "bin" / "celery"),
        "-A", "app.celery_app", "worker", "--loglevel=info",
    ]
    log_path = REPO_ROOT / "logs" / "worker.log"
    with open(log_path, "a") as log_fh:
        proc = subprocess.Popen(
            cmd,
            env=env,
            cwd=str(REPO_ROOT),
            stdout=log_fh,
            stderr=log_fh,
            start_new_session=True,
        )
    print(f"[worker] Started PID={proc.pid} wm_on={wm_on}")
    time.sleep(8)  # allow worker to finish initializing


def verify_worker_env(wm_on: bool) -> dict:
    """Check live worker env via /proc/*/environ for celery processes."""
    result = subprocess.run(
        ["pgrep", "-f", "celery.*app.celery_app"],
        capture_output=True, text=True
    )
    pids = [p.strip() for p in result.stdout.strip().splitlines() if p.strip().isdigit()]
    if not pids:
        return {"error": "no celery worker found"}
    pid = pids[0]
    try:
        env_raw = Path(f"/proc/{pid}/environ").read_bytes()
        env_vars = dict(
            item.split("=", 1)
            for item in env_raw.decode("utf-8", errors="replace").split("\x00")
            if "=" in item
        )
        checked = {
            "ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY": env_vars.get("ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY", "(unset)"),
            "WORKING_MEMORY_PERSISTENCE_ENABLED": env_vars.get("WORKING_MEMORY_PERSISTENCE_ENABLED", "(unset)"),
            "WORKING_MEMORY_RENDER_ENABLED": env_vars.get("WORKING_MEMORY_RENDER_ENABLED", "(unset)"),
            "WORKING_MEMORY_INJECTION_ENABLED": env_vars.get("WORKING_MEMORY_INJECTION_ENABLED", "(unset)"),
            "REPO_MEMORY_INJECTION_ENABLED": env_vars.get("REPO_MEMORY_INJECTION_ENABLED", "(unset)"),
            "PSS_CONTINUATION_INJECTION_ENABLED": env_vars.get("PSS_CONTINUATION_INJECTION_ENABLED", "(unset)"),
            "ARTIFACT_CONTINUATION_ENABLED": env_vars.get("ARTIFACT_CONTINUATION_ENABLED", "(unset)"),
            "LANGFUSE_ENABLED": env_vars.get("LANGFUSE_ENABLED", "(unset)"),
            "PLANNING_REPAIR_BASE_URL": env_vars.get("PLANNING_REPAIR_BASE_URL", "(unset)"),
            "PLANNING_REPAIR_MODEL": env_vars.get("PLANNING_REPAIR_MODEL", "(unset)"),
            "pid": pid,
        }
        expected_persist = "True" if wm_on else "False"
        ok = (
            checked["ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY"] == "1"
            and checked["WORKING_MEMORY_PERSISTENCE_ENABLED"] == expected_persist
        )
        checked["env_ok"] = ok
        return checked
    except Exception as e:
        return {"error": str(e)}


def restart_worker(wm_on: bool) -> dict:
    print(f"\n[worker] Restarting for WM {'ON' if wm_on else 'OFF'} arm...")
    _kill_celery_workers()
    start_worker(wm_on)
    env_check = verify_worker_env(wm_on)
    print(f"[worker] Env check:")
    for k, v in env_check.items():
        print(f"  {k} = {v}")
    if not env_check.get("env_ok"):
        raise RuntimeError(f"Worker env mismatch: {env_check}")
    return env_check


# ─────────────────────────────────────────────────────────────
# Slot management
# ─────────────────────────────────────────────────────────────

def wait_slot(poll: int = 15, timeout: int = 600) -> None:
    import redis as redis_lib
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    r = redis_lib.Redis()
    engine = create_engine(
        f"sqlite:///{REPO_ROOT}/orchestrator.db",
        connect_args={"check_same_thread": False},
    )
    DBSession = sessionmaker(bind=engine)
    TERMINAL = {"completed", "failed", "error", "cancelled", "expired"}

    def _slot_members():
        try:
            return [int(m) for m in (r.smembers(SLOT_KEY) or set())]
        except Exception:
            return []

    def _evict_terminal():
        db = DBSession()
        try:
            for sid in _slot_members():
                row = db.execute(
                    text("SELECT status FROM sessions WHERE id=:id"), {"id": sid}
                ).fetchone()
                status = row[0] if row else "not_found"
                if status in TERMINAL or status == "not_found":
                    r.srem(SLOT_KEY, str(sid))
                    print(f"  [slot] Evicted stale session {sid} ({status})")
        finally:
            db.close()

    deadline = time.time() + timeout
    while time.time() < deadline:
        _evict_terminal()
        members = _slot_members()
        if not members:
            print("[slot] Clear.")
            return
        print(f"[slot] Occupied by {members}. Waiting {poll}s...")
        time.sleep(poll)
    raise TimeoutError("Backend slot never freed")


# ─────────────────────────────────────────────────────────────
# Project / task helpers
# ─────────────────────────────────────────────────────────────

def create_project(slug: str, arm: str) -> dict:
    workspace = str(WORKSPACE_BASE / slug)
    p = _api("POST", "/api/v1/projects", json={
        "name": slug,
        "description": f"WM API-contract parser pilot R4/R3 — {arm} arm",
        "workspace_path": workspace,
    })
    print(f"[project] id={p['id']} slug={slug}")
    p["_workspace_abs"] = workspace
    return p


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


def dispatch_task(task_id: int) -> None:
    _api("POST", f"/api/v1/tasks/{task_id}/retry", json={})
    print(f"[dispatch] task {task_id}")


def poll_task(task_id: int, timeout: int = 1500, poll: int = 20) -> dict:
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
# API contract capture
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


# ─────────────────────────────────────────────────────────────
# WM trim analysis
# ─────────────────────────────────────────────────────────────

def compute_trim_analysis(workspace_path: Path) -> dict:
    """Simulate 400-char planning context trim and report key term positions."""
    try:
        import logging
        from app.services.orchestration.working_memory import _render_working_memory_content
        logger = logging.getLogger(__name__)
        wm_rendered = _render_working_memory_content(str(workspace_path), logger)
        wm_len = len(wm_rendered)

        collapsed = " ".join((wm_rendered or "").split())
        trimmed = (
            collapsed[:PLANNING_CONTEXT_CAP - 3].rstrip() + "..."
            if len(collapsed) > PLANNING_CONTEXT_CAP
            else collapsed
        )

        def _pos(term: str) -> int:
            return collapsed.find(term)

        failure_pos = _pos("failure return")
        success_pos = _pos("success return")
        # prefer quoted "code" key, fall back to bare word
        code_pos = _pos('"code"')
        if code_pos == -1:
            code_pos = _pos("'code'")
        if code_pos == -1:
            # find "code" that's part of API contract section, not prose
            idx = collapsed.find("code")
            code_pos = idx

        empty_pos = _pos("EMPTY")
        format_pos = _pos("FORMAT")
        overflow_pos = _pos("OVERFLOW")

        return {
            "wm_rendered_len": wm_len,
            "base_cap": PLANNING_CONTEXT_CAP,
            "trimmed_content": trimmed,
            "failure_return_pos": failure_pos,
            "success_return_pos": success_pos,
            "failure_before_success": (
                failure_pos != -1 and success_pos != -1 and failure_pos < success_pos
            ),
            "code_pos": code_pos,
            "code_in_400": code_pos != -1 and code_pos < PLANNING_CONTEXT_CAP,
            "EMPTY_pos": empty_pos,
            "FORMAT_pos": format_pos,
            "OVERFLOW_pos": overflow_pos,
            "EMPTY_in_400": empty_pos != -1 and empty_pos < PLANNING_CONTEXT_CAP,
        }
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────
# Test source injection — last-3 expected behavior lines
# ─────────────────────────────────────────────────────────────

def extract_last3_behavior(workspace_path: Path) -> dict:
    """Simulate what _expected_behavior_lines() will inject for T2 planning."""
    try:
        from app.services.project.source_imports import python_test_source_context_from_tests
        ctx = python_test_source_context_from_tests(workspace_path)
        # Check for code leak in the context
        code_in_ctx = '"code"' in (ctx or "") or "'code'" in (ctx or "")
        value_in_ctx = '"value"' in (ctx or "") or "'value'" in (ctx or "")
        return {
            "context": ctx or "",
            "code_leaks": code_in_ctx,
            "value_leaks": value_in_ctx,
            "context_len": len(ctx or ""),
        }
    except Exception as e:
        return {"error": str(e), "code_leaks": False, "value_leaks": False}


# ─────────────────────────────────────────────────────────────
# Formatter analysis
# ─────────────────────────────────────────────────────────────

def extract_first_plan_formatter(task_id: int) -> str:
    """Read task.steps from DB and find the first write_file for formatter.py."""
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
    uses_code = '["code"]' in t or "['code']" in t or '.get("code")' in t or ".get('code')" in t
    uses_error = '["error"]' in t or "['error']" in t or '.get("error")' in t or ".get('error')" in t
    uses_ok = '["ok"]' in t or "['ok']" in t or '.get("ok")' in t or ".get('ok')" in t
    uses_value = '["value"]' in t or "['value']" in t or '.get("value")' in t or ".get('value')" in t
    verbatim_empty = '"EMPTY"' in t or "'EMPTY'" in t
    verbatim_format = '"FORMAT"' in t or "'FORMAT'" in t
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
        "uses_code": uses_code,
        "uses_error": uses_error,
        "uses_ok": uses_ok,
        "uses_value": uses_value,
        "first_field": first_field,
        "verbatim_EMPTY": verbatim_empty,
        "verbatim_FORMAT": verbatim_format,
        "verbatim_OVERFLOW": verbatim_overflow,
        "verbatim_codes": verbatim_empty or verbatim_format or verbatim_overflow,
    }


# ─────────────────────────────────────────────────────────────
# Repair / regression helpers
# ─────────────────────────────────────────────────────────────

def count_repairs_from_report(report_path: Path) -> dict:
    if not report_path.exists():
        return {"debug_repairs": -1, "planning_repairs": -1, "error": "report not found"}
    text = report_path.read_text(encoding="utf-8", errors="replace")
    return {
        "debug_repairs": text.count("[DEBUG_REPAIR_DIRECT] attempting"),
        "planning_repairs": text.count("[REPAIR_DIRECT] completed direct structured repair"),
    }


def extract_llm_summary_from_log(task_id: int, worker_log: Path) -> str:
    if not worker_log.exists():
        return ""
    lines = worker_log.read_text(encoding="utf-8", errors="replace").splitlines()
    for i, line in enumerate(lines):
        if f"'task_id': {task_id}," not in line or "succeeded in" not in line:
            continue
        summary_marker = "'summary': '"
        if summary_marker not in line:
            continue
        start = line.find(summary_marker) + len(summary_marker)
        collected = [line[start:]]
        j = i + 1
        while j < len(lines) and j < i + 100:
            next_line = lines[j]
            if re.match(r"\[\d{4}-\d{2}-\d{2}", next_line):
                break
            collected.append(next_line)
            j += 1
        raw = "\n".join(collected)
        if raw.endswith("'}"):
            raw = raw[:-2]
        elif raw.endswith("'"):
            raw = raw[:-1]
        return raw
    return ""


def scan_injection_log(worker_log: Path, log_offset: int) -> dict:
    if not worker_log.exists():
        return {"found": False}
    lines = worker_log.read_text(encoding="utf-8", errors="replace").splitlines()
    arm_lines = lines[log_offset:]
    for line in reversed(arm_lines):
        if "[WORKING_MEMORY] Injected" in line and "project_context" in line:
            m = re.search(r"Injected (\d+) chars.*plan_position=(\S+)\)", line)
            if m:
                return {
                    "found": True,
                    "chars": int(m.group(1)),
                    "plan_position": m.group(2).rstrip(")"),
                }
    return {"found": False}


def scan_regression_checks(worker_log: Path, log_offset: int) -> dict:
    if not worker_log.exists():
        return {}
    lines = worker_log.read_text(encoding="utf-8", errors="replace").splitlines()
    text = "\n".join(lines[log_offset:])
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
# Main arm runner
# ─────────────────────────────────────────────────────────────

def run_arm(arm: str) -> dict:
    assert arm in ("off", "on"), f"Unknown arm: {arm!r}"
    wm_on = arm == "on"
    slug = WORKSPACE_SLUG_OFF if not wm_on else WORKSPACE_SLUG_ON

    print()
    print("=" * 65)
    print(f"WM API-CONTRACT PARSER PILOT R4/R3 — ARM: WM {'ON' if wm_on else 'OFF'}")
    print(f"Project: {slug}  Commit: {COMMIT_SHA}")
    print("=" * 65)

    worker_log = REPO_ROOT / "logs" / "worker.log"

    # Restart worker with correct env
    worker_env = restart_worker(wm_on)
    log_offset = get_log_offset(worker_log)

    init_auth()
    wait_slot()

    project = create_project(slug, arm)
    project_id = project["id"]
    workspace_path = Path(project["_workspace_abs"])
    agent_dir = workspace_path / ".agent"
    wm_path = agent_dir / "working_memory.json"

    # ── T1 ──────────────────────────────────────────────────────
    t1 = create_task(project_id, T1_TITLE, T1_DESC, 1)
    t1_id = t1["id"]
    t1_report_path = agent_dir / "task-reports" / f"task_report_{t1_id}.md"

    print(f"\n[T1] Dispatching {t1_id}: {T1_TITLE!r}")
    t1_start = time.time()
    dispatch_task(t1_id)
    t1_result = poll_task(t1_id)
    t1_elapsed = round(time.time() - t1_start, 1)
    print(f"[T1] {t1_result.get('status')} in {t1_elapsed}s")

    # T1 artifacts
    wm_data_t1 = {}
    if wm_path.exists():
        try:
            wm_data_t1 = json.loads(wm_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    strategies_t1 = wm_data_t1.get("implementation_strategy") or []
    t1_wm_summary = strategies_t1[-1].get("summary", "") if strategies_t1 else ""
    t1_llm_summary = t1_wm_summary or extract_llm_summary_from_log(t1_id, worker_log)
    t1_is_det = t1_llm_summary.startswith(DETERMINISTIC_PREFIX)
    t1_api_capture = assess_api_capture_8(t1_llm_summary)
    t1_api_score = sum(1 for v in t1_api_capture.values() if v)
    t1_repairs = count_repairs_from_report(t1_report_path)

    print(f"  WM JSON exists:    {wm_path.exists()}")
    print(f"  Summary source:    {'wm.json' if t1_wm_summary else 'worker_log' if t1_llm_summary else 'NOT FOUND'}")
    print(f"  Is LLM (not det):  {not t1_is_det}")
    print(f"  API score:         {t1_api_score}/8")
    for k, v in t1_api_capture.items():
        print(f"    {k}: {v}")
    print(f"  debug repairs:     {t1_repairs['debug_repairs']}")
    print(f"  planning repairs:  {t1_repairs['planning_repairs']}")

    # Test source injection analysis (before T2)
    test_source_analysis = extract_last3_behavior(workspace_path)
    print(f"\n  Test source injection:")
    print(f"    code_leaks: {test_source_analysis.get('code_leaks')}")
    print(f"    value_leaks: {test_source_analysis.get('value_leaks')}")
    print(f"    context ({test_source_analysis.get('context_len')} chars):")
    ctx_preview = (test_source_analysis.get("context") or "")[:300]
    for ln in ctx_preview.splitlines():
        print(f"      {ln}")

    # WM trim analysis (WM ON only — before T2)
    trim_analysis = {}
    if wm_on and wm_path.exists():
        trim_analysis = compute_trim_analysis(workspace_path)
        print(f"\n  WM trim analysis (what T2 planner will see):")
        print(f"    WM rendered block:      {trim_analysis.get('wm_rendered_len')} chars")
        print(f"    failure return pos:     {trim_analysis.get('failure_return_pos')}")
        print(f"    success return pos:     {trim_analysis.get('success_return_pos')}")
        print(f"    failure before success: {trim_analysis.get('failure_before_success')}")
        print(f"    code pos:               {trim_analysis.get('code_pos')}")
        print(f"    code in 400 chars:      {trim_analysis.get('code_in_400')}")
        print(f"    EMPTY pos:              {trim_analysis.get('EMPTY_pos')}")
        print(f"    trimmed content:")
        print(f"      {trim_analysis.get('trimmed_content', '')}")
        print()

        # Gate check: verify code is visible before dispatching T2
        if not trim_analysis.get("code_in_400"):
            print(f"  WARNING: code NOT visible in first 400 chars — T2 planner may not see failure return")
        else:
            print(f"  OK: code visible in first 400 chars — proceeding to T2")
    elif not wm_on:
        print()

    # ── T2 ──────────────────────────────────────────────────────
    if t1_result.get("status") != "done":
        print(f"[T2] Skipped — T1 not done (status={t1_result.get('status')})")
        t2_result = {"status": "skipped"}
        t2_id = -1
        t2_elapsed = 0
        t2_first_fields = assess_formatter_fields("")
        t2_final_fields = assess_formatter_fields("")
        t2_repairs = {"debug_repairs": 0, "planning_repairs": 0}
        formatter_first_plan = ""
        formatter_final = ""
    else:
        # Confirm progress_notes exists
        pn_path = agent_dir / "progress_notes.md"
        if not pn_path.exists():
            print(f"  WARNING: progress_notes.md not found — proceeding anyway")

        wait_slot()
        t2 = create_task(project_id, T2_TITLE, T2_DESC, 2)
        t2_id = t2["id"]
        t2_report_path = agent_dir / "task-reports" / f"task_report_{t2_id}.md"

        print(f"\n[T2] Dispatching {t2_id}: {T2_TITLE!r}")
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

        print(f"\n  First plan formatter:")
        print(f"    first_field:     {t2_first_fields['first_field']}")
        print(f"    uses_code:       {t2_first_fields['uses_code']}")
        print(f"    uses_error:      {t2_first_fields['uses_error']}")
        print(f"    verbatim_codes:  {t2_first_fields['verbatim_codes']}")
        print(f"\n  Final formatter:")
        print(f"    first_field:     {t2_final_fields['first_field']}")
        print(f"    uses_code:       {t2_final_fields['uses_code']}")
        print(f"    uses_error:      {t2_final_fields['uses_error']}")
        print(f"    verbatim_codes:  {t2_final_fields['verbatim_codes']}")
        print(f"\n  debug repairs:   {t2_repairs['debug_repairs']}")
        print(f"  planning repairs:{t2_repairs['planning_repairs']}")

    # Injection log
    injection_log = scan_injection_log(worker_log, log_offset) if wm_on else {"found": False}

    # Regression checks (scoped to this arm's log lines)
    regression = scan_regression_checks(worker_log, log_offset)
    print(f"\n  Regression checks:")
    for k, v in regression.items():
        print(f"    {k}: {v}")

    # ── Raw output ──────────────────────────────────────────────
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    run_label = "r4" if not wm_on else "r3"
    raw_filename = f"wm-api-contract-pilot-{arm}-{run_label}-raw-{timestamp}.json"
    raw_path = REPORT_DIR / raw_filename

    raw = {
        "arm": arm,
        "run_label": run_label,
        "timestamp": timestamp,
        "commit_sha": COMMIT_SHA,
        "project_id": project_id,
        "workspace_slug": slug,
        "t1_task_id": t1_id,
        "t2_task_id": t2_id,
        "t1_status": t1_result.get("status"),
        "t2_status": t2_result.get("status"),
        "t1_elapsed_s": t1_elapsed,
        "t2_elapsed_s": t2_elapsed,
        "worker_env": worker_env,
        "flags_verified": {
            "llm_summary": worker_env.get("ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY") == "1",
            "persistence": worker_env.get("WORKING_MEMORY_PERSISTENCE_ENABLED") == ("True" if wm_on else "False"),
            "render": worker_env.get("WORKING_MEMORY_RENDER_ENABLED") == ("True" if wm_on else "False"),
            "injection": worker_env.get("WORKING_MEMORY_INJECTION_ENABLED") == ("True" if wm_on else "False"),
        },
        "t1": {
            "status": t1_result.get("status"),
            "elapsed_s": t1_elapsed,
            "wm_json_exists": wm_path.exists(),
            "llm_summary": t1_llm_summary,
            "llm_summary_source": (
                "working_memory.json" if t1_wm_summary else
                "worker_log" if t1_llm_summary else "not_found"
            ),
            "is_deterministic": t1_is_det,
            "api_capture": t1_api_capture,
            "api_score": t1_api_score,
            "debug_repairs": t1_repairs["debug_repairs"],
            "planning_repairs": t1_repairs["planning_repairs"],
            "test_source_injection": test_source_analysis,
        },
        "trim_analysis": trim_analysis,
        "injection_log": injection_log,
        "t2": {
            "status": t2_result.get("status"),
            "elapsed_s": t2_elapsed,
            "formatter_exists": (workspace_path / "src" / "calclib" / "formatter.py").exists() if t2_id != -1 else False,
            "formatter_first_plan": formatter_first_plan[:800] if t2_id != -1 else "",
            "formatter_final": formatter_final[:800] if t2_id != -1 else "",
            "first_plan_fields": t2_first_fields,
            "final_fields": t2_final_fields,
            "debug_repairs": t2_repairs["debug_repairs"],
            "planning_repairs": t2_repairs["planning_repairs"],
        },
        "regression_checks": regression,
        "_arm_summary": {
            "t1_done": t1_result.get("status") == "done",
            "t1_api_score": t1_api_score,
            "t1_llm_found": bool(t1_llm_summary and not t1_is_det),
            "t2_done": t2_result.get("status") == "done",
            "t2_first_plan_field": t2_first_fields["first_field"],
            "t2_final_field": t2_final_fields["first_field"],
            "t2_debug_repairs": t2_repairs["debug_repairs"],
            "t2_planning_repairs": t2_repairs["planning_repairs"],
            "code_in_400": trim_analysis.get("code_in_400", None),
            "failure_before_success": trim_analysis.get("failure_before_success", None),
            "test_source_code_leak": test_source_analysis.get("code_leaks", None),
        },
    }

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[raw] {raw_path}")
    return raw


# ─────────────────────────────────────────────────────────────
# Report generator
# ─────────────────────────────────────────────────────────────

def generate_report() -> None:
    off_files = sorted(REPORT_DIR.glob("wm-api-contract-pilot-off-r4-raw-*.json"), reverse=True)
    on_files = sorted(REPORT_DIR.glob("wm-api-contract-pilot-on-r3-raw-*.json"), reverse=True)

    if not off_files:
        print("ERROR: No WM OFF r4 raw file. Run --arm=off first.")
        sys.exit(1)
    if not on_files:
        print("ERROR: No WM ON r3 raw file. Run --arm=on first.")
        sys.exit(1)

    off_raw = json.loads(off_files[0].read_text(encoding="utf-8"))
    on_raw = json.loads(on_files[0].read_text(encoding="utf-8"))
    print(f"[report] OFF raw: {off_files[0].name}")
    print(f"[report] ON  raw: {on_files[0].name}")

    off = off_raw.get("_arm_summary", {})
    on = on_raw.get("_arm_summary", {})
    off_t1 = off_raw.get("t1", {})
    on_t1 = on_raw.get("t1", {})
    off_t2 = off_raw.get("t2", {})
    on_t2 = on_raw.get("t2", {})
    off_trim = off_raw.get("trim_analysis", {})
    on_trim = on_raw.get("trim_analysis", {})
    on_inj_log = on_raw.get("injection_log", {})

    off_first = off.get("t2_first_plan_field", "unknown")
    on_first = on.get("t2_first_plan_field", "unknown")
    on_code_in_400 = on.get("code_in_400")
    on_failure_before_success = on.get("failure_before_success")
    on_test_leak = on.get("test_source_code_leak")
    off_test_leak = off.get("test_source_code_leak")

    on_verbatim = on_t2.get("first_plan_fields", {}).get("verbatim_codes", False)
    off_verbatim = off_t2.get("first_plan_fields", {}).get("verbatim_codes", False)

    # Verdict
    if off_first == "error" and on_first == "code" and on_code_in_400:
        verdict = "STRONG PASS"
        explanation = (
            "WM OFF T2 used `result[\"error\"]` on first plan. "
            "WM ON T2 used `result[\"code\"]` on first plan. "
            f"`code` was visible in first 400 chars of rendered WM block. "
            "WM injection changed planner behavior in the expected direction."
        )
    elif off_first in ("error", "neither") and on_first == "code" and on_code_in_400:
        verdict = "PASS"
        explanation = (
            f"WM OFF T2 first field={off_first!r}. "
            "WM ON T2 used `result[\"code\"]` on first plan. "
            f"`code` was visible in first 400 chars. "
            "WM injection produced measurable improvement."
        )
    elif on_code_in_400 and on_first == on_first and on_first == off_first:
        verdict = "NULL / INCONCLUSIVE"
        explanation = (
            f"Both arms used first_field={on_first!r}. "
            "WM injection did not change planner behavior despite `code` being visible."
        )
    elif not on_code_in_400:
        verdict = "NULL / INCONCLUSIVE"
        explanation = (
            "`code` not visible within first 400 chars of rendered WM block. "
            "The 400-char trim is still the limiting factor."
        )
    elif off_first == "code":
        verdict = "NULL / INCONCLUSIVE"
        explanation = (
            "WM OFF baseline already used `result[\"code\"]`. "
            "Cannot measure WM benefit — leak or task description exposed the key."
        )
    elif on_first == "error" and on_code_in_400:
        verdict = "FAIL"
        explanation = (
            f"`code` was visible in first 400 chars but WM ON T2 still used `result[\"error\"]`. "
            "WM injection occurred but did not change planner behavior."
        )
    else:
        verdict = "NULL / INCONCLUSIVE"
        explanation = (
            f"WM OFF first_field={off_first!r}, WM ON first_field={on_first!r}. "
            "Review first plan content manually."
        )

    today = time.strftime("%Y-%m-%d")
    report_path = REPO_ROOT / "docs/roadmap/reports/maintenance" / "working-memory-api-contract-parser-pilot-rerun-after-failure-first-20260613.md"

    off_t1_api = off_t1.get("api_capture", {})
    on_t1_api = on_t1.get("api_capture", {})
    off_first_fmt = off_t2.get("formatter_first_plan", "")
    on_first_fmt = on_t2.get("formatter_first_plan", "")

    lines = [
        "# WM API-Contract Parser Pilot: Rerun After Failure-First Fix",
        "",
        f"**Date:** {today}",
        f"**Commit:** `{on_raw.get('commit_sha', '?')}`",
        f"**Verdict: {verdict}**",
        "",
        "---",
        "",
        "## Context",
        "",
        "Prior rerun (WM OFF R3 / WM ON R2) was NULL because `_shape_project_context`",
        "trims the WM block to 400 chars and `code` appeared at char ~431 — just past the",
        "cutoff. The TASK_SUMMARY template was updated to place `failure return:` before",
        "`success return:` (Option A fix), moving `code` to char ~377 in the validation run.",
        "This rerun tests whether that fix allows WM to change T2 planner behavior.",
        "",
        "## Configuration",
        "",
        "| Setting | WM OFF R4 | WM ON R3 |",
        "|---|---|---|",
        "| `ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY` | `1` | `1` |",
        "| `WORKING_MEMORY_PERSISTENCE_ENABLED` | `False` | `True` |",
        "| `WORKING_MEMORY_RENDER_ENABLED` | `False` | `True` |",
        "| `WORKING_MEMORY_INJECTION_ENABLED` | `False` | `True` |",
        "| All other continuation flags | `False` | `False` |",
        f"| Project | `{off_raw.get('workspace_slug')}` (id={off_raw.get('project_id')}) | `{on_raw.get('workspace_slug')}` (id={on_raw.get('project_id')}) |",
        "",
        "### Worker Env Verified",
        "",
        "| Flag | WM OFF R4 | WM ON R3 |",
        "|---|---|---|",
    ]
    off_we = off_raw.get("worker_env", {})
    on_we = on_raw.get("worker_env", {})
    for k in ["ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY", "WORKING_MEMORY_PERSISTENCE_ENABLED",
              "WORKING_MEMORY_RENDER_ENABLED", "WORKING_MEMORY_INJECTION_ENABLED"]:
        lines.append(f"| `{k}` | `{off_we.get(k, '?')}` | `{on_we.get(k, '?')}` |")

    lines += [
        "",
        "---",
        "",
        "## T1 Results",
        "",
        "| Metric | WM OFF R4 | WM ON R3 |",
        "|---|---|---|",
        f"| Task ID | {off_raw.get('t1_task_id')} | {on_raw.get('t1_task_id')} |",
        f"| Status | {off_t1.get('status')} | {on_t1.get('status')} |",
        f"| Elapsed | {off_raw.get('t1_elapsed_s')}s | {on_raw.get('t1_elapsed_s')}s |",
        f"| Debug repairs | {off_t1.get('debug_repairs')} | {on_t1.get('debug_repairs')} |",
        f"| Planning repairs | {off_t1.get('planning_repairs')} | {on_t1.get('planning_repairs')} |",
        f"| LLM summary source | {off_t1.get('llm_summary_source')} | {on_t1.get('llm_summary_source')} |",
        f"| Is deterministic | {off_t1.get('is_deterministic')} | {on_t1.get('is_deterministic')} |",
        f"| API contract score | **{off_t1.get('api_score')}/8** | **{on_t1.get('api_score')}/8** |",
        "",
        "### T1 API Contract Capture",
        "",
        "| Indicator | WM OFF R4 | WM ON R3 |",
        "|---|---|---|",
    ]
    for key in ["parse_amount", "ok_key", "value_key", "code_key",
                "EMPTY_sentinel", "FORMAT_sentinel", "OVERFLOW_sentinel", "never_raises"]:
        off_v = "✓" if off_t1_api.get(key) else "✗"
        on_v = "✓" if on_t1_api.get(key) else "✗"
        lines.append(f"| `{key}` | {off_v} | {on_v} |")

    off_llm = (off_t1.get("llm_summary") or "").strip()
    on_llm = (on_t1.get("llm_summary") or "").strip()
    lines += [
        "",
        "### T1 LLM Summary — WM OFF R4",
        "```",
        off_llm[:800] if off_llm else "(not captured)",
        "```",
        "",
        "### T1 LLM Summary — WM ON R3",
        "```",
        on_llm[:800] if on_llm else "(not captured)",
        "```",
        "",
        "---",
        "",
        "## Test Source Injection (Leak Control)",
        "",
        "| Metric | WM OFF R4 | WM ON R3 |",
        "|---|---|---|",
        f"| `code` leaks via test source injection | {off_test_leak} | {on_test_leak} |",
        f"| `value` leaks via test source injection | {off.get('test_source_code_leak')} | {on.get('test_source_code_leak')} |",
        "",
        "### Test Source Context — WM ON R3",
        "```",
        (on_t1.get("test_source_injection", {}).get("context") or "(not captured)")[:600],
        "```",
        "",
        "---",
    ]

    if on_trim:
        lines += [
            "",
            "## WM Trim Analysis (WM ON R3)",
            "",
            "| Metric | Value |",
            "|---|---|",
            f"| WM rendered block | {on_trim.get('wm_rendered_len')} chars |",
            f"| Planning context cap | {on_trim.get('base_cap')} chars |",
            f"| `failure return:` position | {on_trim.get('failure_return_pos')} |",
            f"| `success return:` position | {on_trim.get('success_return_pos')} |",
            f"| failure before success in render | **{on_trim.get('failure_before_success')}** |",
            f"| `code` position | **{on_trim.get('code_pos')}** |",
            f"| `code` within first 400 chars | **{on_trim.get('code_in_400')}** |",
            f"| `EMPTY` position | {on_trim.get('EMPTY_pos')} |",
            f"| `FORMAT` position | {on_trim.get('FORMAT_pos')} |",
            f"| `OVERFLOW` position | {on_trim.get('OVERFLOW_pos')} |",
            f"| `EMPTY` within first 400 chars | {on_trim.get('EMPTY_in_400')} |",
            f"| WM injected (log) | {on_inj_log.get('found')} — {on_inj_log.get('chars')} chars |",
            "",
            "**Trimmed content (what T2 planner sees):**",
            "```",
            on_trim.get("trimmed_content", "(not captured)")[:600],
            "```",
            "",
            "---",
        ]

    lines += [
        "",
        "## T2 Results",
        "",
        "| Metric | WM OFF R4 | WM ON R3 |",
        "|---|---|---|",
        f"| Task ID | {off_raw.get('t2_task_id')} | {on_raw.get('t2_task_id')} |",
        f"| Status | {off_t2.get('status')} | {on_t2.get('status')} |",
        f"| Elapsed | {off_raw.get('t2_elapsed_s')}s | {on_raw.get('t2_elapsed_s')}s |",
        f"| Debug repairs | {off_t2.get('debug_repairs')} | {on_t2.get('debug_repairs')} |",
        f"| Planning repairs | {off_t2.get('planning_repairs')} | {on_t2.get('planning_repairs')} |",
        f"| **First plan field** | **{off.get('t2_first_plan_field', '?')}** | **{on.get('t2_first_plan_field', '?')}** |",
        f"| Final field | {off.get('t2_final_field', '?')} | {on.get('t2_final_field', '?')} |",
        f"| Verbatim codes (EMPTY/FORMAT/OVERFLOW) | {off_verbatim} | {on_verbatim} |",
        "",
        "### T2 First Plan Formatter — WM OFF R4",
        "```python",
        off_first_fmt[:600] if off_first_fmt else "(not captured)",
        "```",
        "",
        "### T2 First Plan Formatter — WM ON R3",
        "```python",
        on_first_fmt[:600] if on_first_fmt else "(not captured)",
        "```",
        "",
        "### T2 Final Formatter — WM OFF R4",
        "```python",
        off_t2.get("formatter_final", "")[:500] or "(not found)",
        "```",
        "",
        "### T2 Final Formatter — WM ON R3",
        "```python",
        on_t2.get("formatter_final", "")[:500] or "(not found)",
        "```",
        "",
        "---",
        "",
        "## Signal Comparison",
        "",
        "| Signal | WM OFF R4 | WM ON R3 |",
        "|---|---|---|",
        f"| T1 status | {off_t1.get('status')} | {on_t1.get('status')} |",
        f"| T1 API score | {off_t1.get('api_score')}/8 | {on_t1.get('api_score')}/8 |",
        f"| `code` in T1 summary | {off_t1_api.get('code_key')} | {on_t1_api.get('code_key')} |",
        f"| `code` visible in 400-char trim | N/A | {on_code_in_400} |",
        f"| failure before success in render | N/A | {on_failure_before_success} |",
        f"| Test source `code` leak | {off_test_leak} | {on_test_leak} |",
        f"| WM injected | No | {on_inj_log.get('found', 'N/A')} |",
        f"| **T2 first plan field** | **{off_first}** | **{on_first}** |",
        f"| T2 debug repairs | {off_t2.get('debug_repairs')} | {on_t2.get('debug_repairs')} |",
        f"| T2 planning repairs | {off_t2.get('planning_repairs')} | {on_t2.get('planning_repairs')} |",
        f"| T2 verbatim codes | {off_verbatim} | {on_verbatim} |",
        "",
        "---",
        "",
        "## Verdict",
        "",
        f"**{verdict}**",
        "",
        explanation,
        "",
        "---",
        "",
        "## Regression Checks",
        "",
        "| Check | WM OFF R4 | WM ON R3 |",
        "|---|---|---|",
    ]
    off_reg = off_raw.get("regression_checks", {})
    on_reg = on_raw.get("regression_checks", {})
    for k in set(list(off_reg.keys()) + list(on_reg.keys())):
        lines.append(f"| `{k}` | {off_reg.get(k, 'N/A')} | {on_reg.get(k, 'N/A')} |")

    lines += [
        "",
        "---",
        "",
        "## Limiting Factor History",
        "",
        "| Phase | Limiting Factor | Status |",
        "|---|---|---|",
        "| Prior WM ON pilot (initial) | LLM summary quality — 0/6 API contract score | **RESOLVED** |",
        "| WM OFF R3 / WM ON R2 rerun | 400-char trim clips `code` at char ~431 | **RESOLVED** (failure-first fix) |",
        f"| This rerun | See verdict above | **{verdict}** |",
        "",
        "---",
        "",
        "## Raw Data",
        "",
        f"- WM OFF R4: `{off_files[0].name}`",
        f"- WM ON R3: `{on_files[0].name}`",
    ]

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[report] {report_path}")
    print(f"\nVerdict: {verdict}")


# ─────────────────────────────────────────────────────────────
# Entry point
# ─────────────────────────────────────────────────────────────

def main():
    args = sys.argv[1:]
    if not args:
        print(__doc__)
        sys.exit(0)
    mode = args[0].lstrip("-")
    if mode == "report":
        generate_report()
    elif mode in ("arm=off", "arm=on"):
        arm = mode.split("=")[1]
        run_arm(arm)
    else:
        print(f"Unknown argument: {args[0]!r}")
        print("Usage: --arm=off | --arm=on | --report")
        sys.exit(1)


if __name__ == "__main__":
    main()
