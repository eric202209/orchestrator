"""
WM API-Contract Parser Pilot Runner (WM OFF R3 / WM ON R2)

Measures whether the updated TASK_SUMMARY prompt (validated at 8/8 API-contract
score in task 907) now allows Working Memory to change T2 planner behavior.

Usage:
  python3 wm_api_contract_parser_pilot_runner.py --arm=off
  python3 wm_api_contract_parser_pilot_runner.py --arm=on
  python3 wm_api_contract_parser_pilot_runner.py --report

WM OFF arm — worker must be configured with:
  ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY=1
  WORKING_MEMORY_PERSISTENCE_ENABLED=False
  WORKING_MEMORY_RENDER_ENABLED=False
  WORKING_MEMORY_INJECTION_ENABLED=False
  REPO_MEMORY_INJECTION_ENABLED=False
  PSS_CONTINUATION_INJECTION_ENABLED=False
  ARTIFACT_CONTINUATION_ENABLED=False
  LANGFUSE_ENABLED=false

WM ON arm — worker must be configured with:
  ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY=1
  WORKING_MEMORY_PERSISTENCE_ENABLED=True
  WORKING_MEMORY_RENDER_ENABLED=True
  WORKING_MEMORY_INJECTION_ENABLED=True
  REPO_MEMORY_INJECTION_ENABLED=False
  PSS_CONTINUATION_INJECTION_ENABLED=False
  ARTIFACT_CONTINUATION_ENABLED=False
  LANGFUSE_ENABLED=false

After both arms complete:
  python3 wm_api_contract_parser_pilot_runner.py --report
  Reads the two most recent raw JSON files and writes the comparison report.
"""

import ast
import json
import os
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from app.config import settings  # noqa: E402
from app.auth import create_access_token  # noqa: E402
import requests  # noqa: E402

BASE_URL = "http://127.0.0.1:8080"
USER_EMAIL = os.environ.get("ORCHESTRATOR_USER_EMAIL", "eval@local.dev")

WORKSPACE_SLUG_OFF = "wm-api-contract-parser-pilot-off-r3"
WORKSPACE_SLUG_ON = "wm-api-contract-parser-pilot-on-r2"

WORKSPACE_BASE = Path("/root/.openclaw/workspace/vault/projects")
REPORT_DIR = REPO_ROOT / "docs/roadmap/reports/maintenance/project_aware_continuation_execution/working_memory"
SLOT_KEY = "orchestrator:backend_slots:local_openclaw"

DETERMINISTIC_PREFIX = "Task completed with verified execution evidence"

# ─────────────────────────────────────────────────────────────
# Task descriptions — exactly as in corpus design
# ─────────────────────────────────────────────────────────────

T1_TITLE = "Bootstrap parse_amount parser"
T1_DESC = """\
Create `src/calclib/parser.py` with `parse_amount(text: str) -> dict`.

Return `{"ok": True, "value": int}` when `text` is a valid integer string \
(after stripping whitespace). Return `{"ok": False, "code": str}` for any \
failure case. The `code` field must be one of: `"EMPTY"` (blank input), \
`"FORMAT"` (non-integer content), `"OVERFLOW"` (value outside -999999 to \
999999 inclusive). Never raise an exception.

Create `src/calclib/__init__.py` re-exporting `parse_amount`. Create \
`tests/test_parser.py` with at least 10 test cases covering: empty input, \
format errors, overflow, positive integer, zero, negative integer, and at \
least 4 edge cases that only check `result["ok"]` without checking \
`result["code"]` or `result["value"]`. Add `pytest.ini` at project root. \
Verify with `PYTHONPATH=src python3 -m pytest tests/test_parser.py -q`.\
"""

T2_TITLE = "Add format_amount formatter"
T2_DESC = """\
Add `format_amount(text: str) -> str` in `src/calclib/formatter.py`. \
Import and use the parser from T1. For a valid amount, return the parsed \
integer as a string. For an invalid amount, return the error code that the \
parser reports. Create `tests/test_formatter.py` with test cases for valid \
input, each error type, and edge cases. Verify with \
`PYTHONPATH=src python3 -m pytest tests/ -q`.\
"""

HEADERS: dict = {}


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
    print(f"[init] Auth for {USER_EMAIL}")


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


def create_project(slug: str, arm: str) -> dict:
    workspace = str(WORKSPACE_BASE / slug)
    p = _api(
        "POST",
        "/api/v1/projects",
        json={
            "name": slug,
            "description": f"WM API-contract parser pilot — {arm} arm",
            "workspace_path": workspace,
        },
    )
    print(f"[project] id={p['id']} slug={slug}")
    p["_workspace_abs"] = workspace
    return p


def create_task(project_id: int, title: str, desc: str, position: int) -> dict:
    t = _api(
        "POST",
        "/api/v1/tasks",
        json={
            "project_id": project_id,
            "title": title,
            "description": desc,
            "plan_position": position,
            "execution_profile": "full_lifecycle",
        },
    )
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
# API capture — 8 indicators matching validation task 907
# ─────────────────────────────────────────────────────────────

def assess_api_capture_8(summary_text: str) -> dict:
    """Check whether the T1 LLM summary captured all 8 required API contract terms."""
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
            "never raise" in text_lower
            or "never raises" in text_lower
            or "no exception" in text_lower
            or "doesn't raise" in text_lower
            or "does not raise" in text_lower
        ),
    }


# ─────────────────────────────────────────────────────────────
# Formatter content extraction
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
                if (
                    op.get("op") == "write_file"
                    and "formatter" in op.get("path", "")
                ):
                    return op.get("content", "")
        return ""
    except Exception as e:
        return f"(error: {e})"
    finally:
        db.close()


def assess_formatter_fields(formatter_text: str) -> dict:
    """Detect which dict keys the formatter accesses."""
    text = formatter_text or ""
    uses_code = (
        '["code"]' in text
        or "['code']" in text
        or '.get("code")' in text
        or ".get('code')" in text
    )
    uses_error = (
        '["error"]' in text
        or "['error']" in text
        or '.get("error")' in text
        or ".get('error')" in text
    )
    uses_ok = (
        '["ok"]' in text
        or "['ok']" in text
        or '.get("ok")' in text
        or ".get('ok')" in text
    )
    uses_value = (
        '["value"]' in text
        or "['value']" in text
        or '.get("value")' in text
        or ".get('value')" in text
    )
    return {
        "uses_code": uses_code,
        "uses_error": uses_error,
        "uses_ok": uses_ok,
        "uses_value": uses_value,
        "first_field": "code" if uses_code and not uses_error else (
            "error" if uses_error and not uses_code else (
                "both" if uses_code and uses_error else "neither"
            )
        ),
    }


# ─────────────────────────────────────────────────────────────
# Repair counts from task report
# ─────────────────────────────────────────────────────────────

def count_repairs_from_report(report_path: Path) -> dict:
    """Count debug and planning repairs from the task report log section."""
    if not report_path.exists():
        return {"debug_repairs": -1, "planning_repairs": -1, "error": "report not found"}
    text = report_path.read_text(encoding="utf-8", errors="replace")
    debug_repairs = text.count("[DEBUG_REPAIR_DIRECT] attempting")
    planning_repairs = text.count("[REPAIR_DIRECT] completed direct structured repair")
    return {
        "debug_repairs": debug_repairs,
        "planning_repairs": planning_repairs,
    }


# ─────────────────────────────────────────────────────────────
# LLM summary from worker log
# ─────────────────────────────────────────────────────────────

def extract_llm_summary_from_log(task_id: int, worker_log: Path) -> str:
    """Scan worker log for the Celery task result for task_id and extract summary.

    The worker logs the Celery result as a Python dict repr that may span multiple
    lines (summary text contains real newlines, not escaped \\n).
    """
    if not worker_log.exists():
        return ""
    lines = worker_log.read_text(encoding="utf-8", errors="replace").splitlines()

    for i, line in enumerate(lines):
        if f"'task_id': {task_id}," not in line or "succeeded in" not in line:
            continue
        # Found the completion line. Extract summary — may span multiple lines.
        summary_marker = "'summary': '"
        if summary_marker not in line:
            continue
        start = line.find(summary_marker) + len(summary_marker)
        # Collect content from this line and subsequent non-log-prefix lines
        collected = [line[start:]]
        j = i + 1
        while j < len(lines) and j < i + 100:
            next_line = lines[j]
            # Stop if we hit a new timestamped log entry
            if re.match(r"\[\d{4}-\d{2}-\d{2}", next_line):
                break
            collected.append(next_line)
            j += 1
        raw = "\n".join(collected)
        # Strip trailing '} if present
        if raw.endswith("'}"):
            raw = raw[:-2]
        elif raw.endswith("'"):
            raw = raw[:-1]
        return raw
    return ""


# ─────────────────────────────────────────────────────────────
# WM injection analysis (WM ON arm)
# ─────────────────────────────────────────────────────────────

def compute_injection_analysis(workspace_path: Path) -> dict:
    """Simulate planning context trim and report how much IS reaches the planner."""
    try:
        import logging
        from app.services.orchestration.working_memory import (
            _render_working_memory_content,
            _INJECTION_BUDGET,
            _SUMMARY_STORAGE_LIMIT,
            _SUMMARY_RENDER_LIMIT,
        )

        logger = logging.getLogger(__name__)
        wm_rendered = _render_working_memory_content(str(workspace_path), logger)
        wm_rendered_len = len(wm_rendered)

        # Replicate _shape_project_context: max_chars=800 → base_context capped at 400
        base_cap = 800 // 2  # 400 chars
        collapsed = " ".join(str(wm_rendered or "").split())
        trimmed = collapsed[:base_cap - 3].rstrip() + "..." if len(collapsed) > base_cap else collapsed
        trimmed_len = len(trimmed)

        impl_pos = trimmed.find("Implementation Strategy")
        constraints_pos = trimmed.find("Constraints")
        known_cmds_pos = trimmed.find("Known Good Commands")
        recent_files_pos = trimmed.find("Recent Files")

        impl_chars_visible = 0
        if impl_pos != -1:
            after = trimmed[impl_pos + len("Implementation Strategy"):]
            impl_chars_visible = len(after.strip())

        return {
            "wm_rendered_len": wm_rendered_len,
            "base_context_cap": base_cap,
            "trimmed_len": trimmed_len,
            "trimmed_content": trimmed,
            "impl_strategy_pos": impl_pos,
            "constraints_pos": constraints_pos,
            "known_good_commands_pos": known_cmds_pos,
            "recent_files_pos": recent_files_pos,
            "impl_strategy_reachable": impl_pos != -1,
            "impl_strategy_chars_visible": impl_chars_visible,
            "render_order_correct": (
                impl_pos != -1
                and (constraints_pos == -1 or impl_pos < constraints_pos)
                and known_cmds_pos == -1
                and recent_files_pos == -1
            ),
        }
    except Exception as e:
        return {"error": str(e)}


def scan_injection_log(worker_log: Path) -> dict:
    """Check worker log for [WORKING_MEMORY] Injected entries."""
    if not worker_log.exists():
        return {"found": False}
    lines = worker_log.read_text(encoding="utf-8", errors="replace").splitlines()
    recent = lines[-1000:]
    for line in reversed(recent):
        if "[WORKING_MEMORY] Injected" in line and "project_context" in line:
            m = re.search(r"Injected (\d+) chars.*plan_position=(\S+)\)", line)
            if m:
                return {
                    "found": True,
                    "chars": int(m.group(1)),
                    "plan_position": m.group(2).rstrip(")"),
                    "line": line.strip(),
                }
    return {"found": False}


def scan_regression_checks(worker_log: Path) -> dict:
    if not worker_log.exists():
        return {}
    text = "\n".join(
        worker_log.read_text(encoding="utf-8", errors="replace").splitlines()[-1000:]
    )
    return {
        "pip_show": "pip show" in text.lower(),
        "nested_project_folder": "nested_project_folder" in text,
        "path_guard_advisory": "PATH_GUARD" in text,
        "backend_capacity": "backend_capacity" in text.lower(),
        "vma_error": "[VMA]" in text or "verification_mutates_source" in text,
        "empty_model_response": "empty.*response" in text.lower(),
    }


# ─────────────────────────────────────────────────────────────
# Main run — single arm
# ─────────────────────────────────────────────────────────────

def run_arm(arm: str) -> dict:
    assert arm in ("off", "on"), f"Unknown arm: {arm!r}"

    slug = WORKSPACE_SLUG_OFF if arm == "off" else WORKSPACE_SLUG_ON
    wm_on = arm == "on"

    print()
    print(f"{'=' * 65}")
    print(f"WM API-CONTRACT PARSER PILOT — ARM: WM {'ON' if wm_on else 'OFF'}")
    print(f"Project: {slug}")
    print(f"{'=' * 65}")
    print()
    print("Expected worker configuration:")
    print(f"  ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY=1")
    print(f"  WORKING_MEMORY_PERSISTENCE_ENABLED={'True' if wm_on else 'False'}")
    print(f"  WORKING_MEMORY_RENDER_ENABLED={'True' if wm_on else 'False'}")
    print(f"  WORKING_MEMORY_INJECTION_ENABLED={'True' if wm_on else 'False'}")
    print(f"  REPO_MEMORY_INJECTION_ENABLED=False")
    print(f"  PSS_CONTINUATION_INJECTION_ENABLED=False")
    print(f"  ARTIFACT_CONTINUATION_ENABLED=False")
    print(f"  LANGFUSE_ENABLED=false")
    print()
    print("Runner process settings (for reference — not the worker):")
    print(f"  WORKING_MEMORY_PERSISTENCE_ENABLED: {settings.WORKING_MEMORY_PERSISTENCE_ENABLED}")
    print(f"  WORKING_MEMORY_RENDER_ENABLED:      {settings.WORKING_MEMORY_RENDER_ENABLED}")
    print(f"  WORKING_MEMORY_INJECTION_ENABLED:   {settings.WORKING_MEMORY_INJECTION_ENABLED}")
    print()

    worker_log = REPO_ROOT / "logs" / "worker.log"

    init_auth()
    wait_slot()

    project = create_project(slug, arm)
    project_id = project["id"]
    workspace_path = Path(project["_workspace_abs"])
    agent_dir = workspace_path / ".agent"
    wm_path = agent_dir / "working_memory.json"

    # ── T1 ──────────────────────────────────────────────────
    t1 = create_task(project_id, T1_TITLE, T1_DESC, 1)
    t1_id = t1["id"]
    t1_report_path = agent_dir / "task-reports" / f"task_report_{t1_id}.md"

    print(f"\n[T1] Dispatching task {t1_id}: {T1_TITLE!r}")
    t1_start = time.time()
    dispatch_task(t1_id)
    t1_result = poll_task(t1_id)
    t1_elapsed = round(time.time() - t1_start, 1)
    print(f"[T1] Done in {t1_elapsed}s  status={t1_result.get('status')}")

    # T1 artifacts
    wm_data_t1 = {}
    if wm_path.exists():
        try:
            wm_data_t1 = json.loads(wm_path.read_text(encoding="utf-8"))
        except Exception:
            pass

    wm_strategies_t1 = wm_data_t1.get("implementation_strategy") or []
    t1_wm_summary_from_json = (
        wm_strategies_t1[-1].get("summary", "") if wm_strategies_t1 else ""
    )
    # For WM OFF, fall back to worker log
    t1_llm_summary = t1_wm_summary_from_json or extract_llm_summary_from_log(t1_id, worker_log)
    t1_is_deterministic = t1_llm_summary.startswith(DETERMINISTIC_PREFIX)
    t1_api_capture = assess_api_capture_8(t1_llm_summary)
    t1_api_score = sum(1 for v in t1_api_capture.values() if v)

    t1_repairs = count_repairs_from_report(t1_report_path)
    t1_progress_notes = read_safe(agent_dir / "progress_notes.md")

    print(f"  WM JSON exists:       {wm_path.exists()}")
    print(f"  T1 summary source:    {'working_memory.json' if t1_wm_summary_from_json else 'worker log' if t1_llm_summary else 'NOT FOUND'}")
    print(f"  T1 is LLM (not det): {not t1_is_deterministic}")
    print(f"  T1 API score:         {t1_api_score}/8")
    for k, v in t1_api_capture.items():
        print(f"    {k}: {v}")
    print(f"  T1 debug repairs:     {t1_repairs['debug_repairs']}")
    print(f"  T1 planning repairs:  {t1_repairs['planning_repairs']}")
    print()

    # WM injection analysis (computed here, before T2 runs — shows what T2 will see)
    injection_analysis = {}
    if wm_on and wm_path.exists():
        injection_analysis = compute_injection_analysis(workspace_path)
        print("  WM render analysis (what T2 planner will see):")
        print(f"    Render order correct:      {injection_analysis.get('render_order_correct')}")
        print(f"    WM rendered block:         {injection_analysis.get('wm_rendered_len')} chars")
        print(f"    Planner base_context cap:  {injection_analysis.get('base_context_cap')} chars")
        print(f"    Chars trimmed to planner:  {injection_analysis.get('trimmed_len')}")
        print(f"    IS reachable in trim:      {injection_analysis.get('impl_strategy_reachable')}")
        print(f"    IS chars visible:          {injection_analysis.get('impl_strategy_chars_visible')}")
        print(f"    Trimmed content:")
        print(f"      {injection_analysis.get('trimmed_content', '')}")
        print()

    # ── T2 ──────────────────────────────────────────────────
    wait_slot()
    t2 = create_task(project_id, T2_TITLE, T2_DESC, 2)
    t2_id = t2["id"]
    t2_report_path = agent_dir / "task-reports" / f"task_report_{t2_id}.md"

    print(f"[T2] Dispatching task {t2_id}: {T2_TITLE!r}")
    t2_start = time.time()
    dispatch_task(t2_id)
    t2_result = poll_task(t2_id)
    t2_elapsed = round(time.time() - t2_start, 1)
    print(f"[T2] Done in {t2_elapsed}s  status={t2_result.get('status')}")

    # T2 artifacts
    formatter_path = workspace_path / "src" / "calclib" / "formatter.py"
    formatter_final = read_safe(formatter_path) if formatter_path.exists() else ""
    formatter_first_plan = extract_first_plan_formatter(t2_id)

    t2_first_fields = assess_formatter_fields(formatter_first_plan)
    t2_final_fields = assess_formatter_fields(formatter_final)
    t2_repairs = count_repairs_from_report(t2_report_path)

    # Injection log (WM ON)
    injection_log = {}
    if wm_on:
        injection_log = scan_injection_log(worker_log)

    regression_checks = scan_regression_checks(worker_log)

    print()
    print(f"  T2 formatter.py exists:   {formatter_path.exists()}")
    print()
    print(f"  First plan formatter (from t.steps):")
    print(f"    first_field: {t2_first_fields['first_field']}")
    print(f"    uses_code:   {t2_first_fields['uses_code']}")
    print(f"    uses_error:  {t2_first_fields['uses_error']}")
    print()
    print(f"  Final formatter:")
    print(f"    first_field: {t2_final_fields['first_field']}")
    print(f"    uses_code:   {t2_final_fields['uses_code']}")
    print(f"    uses_error:  {t2_final_fields['uses_error']}")
    print()
    print(f"  T2 debug repairs:         {t2_repairs['debug_repairs']}")
    print(f"  T2 planning repairs:      {t2_repairs['planning_repairs']}")
    if wm_on:
        print(f"  WM injected for T2:       {injection_log.get('found')} — {injection_log.get('chars')} chars")
    print()
    print("  Regression checks:")
    for k, v in regression_checks.items():
        print(f"    {k}: {v}")

    # ── Raw output ──────────────────────────────────────────
    timestamp = time.strftime("%Y%m%d_%H%M%S")
    raw_arm = "off" if not wm_on else "on"
    run_label = "r3" if not wm_on else "r2"
    raw_filename = f"wm-api-contract-pilot-{raw_arm}-{run_label}-raw-{timestamp}.json"
    raw_path = REPORT_DIR / raw_filename

    raw = {
        "arm": raw_arm,
        "run_label": run_label,
        "timestamp": timestamp,
        "project_id": project_id,
        "workspace_slug": slug,
        "t1_task_id": t1_id,
        "t2_task_id": t2_id,
        "t1_status": t1_result.get("status"),
        "t2_status": t2_result.get("status"),
        "t1_elapsed_s": t1_elapsed,
        "t2_elapsed_s": t2_elapsed,
        "flags_expected": {
            "llm_summary": True,
            "persistence": wm_on,
            "render": wm_on,
            "injection": wm_on,
            "repo_memory": False,
            "pss_continuation": False,
            "artifact_continuation": False,
            "langfuse": False,
        },
        "t1": {
            "status": t1_result.get("status"),
            "elapsed_s": t1_elapsed,
            "wm_json_exists": wm_path.exists(),
            "llm_summary": t1_llm_summary,
            "llm_summary_source": (
                "working_memory.json" if t1_wm_summary_from_json
                else "worker_log" if t1_llm_summary
                else "not_found"
            ),
            "is_deterministic": t1_is_deterministic,
            "api_capture": t1_api_capture,
            "api_score": t1_api_score,
            "debug_repairs": t1_repairs["debug_repairs"],
            "planning_repairs": t1_repairs["planning_repairs"],
        },
        "injection": injection_analysis,
        "injection_log": injection_log,
        "t2": {
            "status": t2_result.get("status"),
            "elapsed_s": t2_elapsed,
            "formatter_exists": formatter_path.exists(),
            "formatter_first_plan": formatter_first_plan[:600],
            "formatter_final": formatter_final[:600],
            "first_plan_fields": t2_first_fields,
            "final_fields": t2_final_fields,
            "debug_repairs": t2_repairs["debug_repairs"],
            "planning_repairs": t2_repairs["planning_repairs"],
        },
        "regression_checks": regression_checks,
    }

    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[raw] Written to: {raw_path}")

    # ── Arm verdict ─────────────────────────────────────────
    print()
    print(f"{'─' * 65}")
    print(f"ARM SUMMARY — WM {'ON' if wm_on else 'OFF'}")
    print(f"{'─' * 65}")
    print(f"T1 status:              {t1_result.get('status')}")
    print(f"T1 LLM summary:         {'FOUND (' + str(len(t1_llm_summary)) + ' chars)' if t1_llm_summary else 'NOT FOUND'}")
    print(f"T1 is LLM (not det.):   {not t1_is_deterministic}")
    print(f"T1 API contract score:  {t1_api_score}/8")
    print(f"  parse_amount:         {t1_api_capture['parse_amount']}")
    print(f"  ok key:               {t1_api_capture['ok_key']}")
    print(f"  value key:            {t1_api_capture['value_key']}")
    print(f"  code key:             {t1_api_capture['code_key']}")
    print(f"  EMPTY:                {t1_api_capture['EMPTY_sentinel']}")
    print(f"  FORMAT:               {t1_api_capture['FORMAT_sentinel']}")
    print(f"  OVERFLOW:             {t1_api_capture['OVERFLOW_sentinel']}")
    print(f"  never raises:         {t1_api_capture['never_raises']}")
    print(f"T1 debug / plan repairs: {t1_repairs['debug_repairs']} / {t1_repairs['planning_repairs']}")
    print()
    print(f"T2 status:              {t2_result.get('status')}")
    print(f"T2 first plan field:    {t2_first_fields['first_field']} (code={t2_first_fields['uses_code']}, error={t2_first_fields['uses_error']})")
    print(f"T2 final field:         {t2_final_fields['first_field']} (code={t2_final_fields['uses_code']}, error={t2_final_fields['uses_error']})")
    print(f"T2 debug / plan repairs: {t2_repairs['debug_repairs']} / {t2_repairs['planning_repairs']}")
    if wm_on and injection_analysis:
        print(f"WM IS chars visible:    {injection_analysis.get('impl_strategy_chars_visible')} (of {injection_analysis.get('base_context_cap')}-char cap)")
        print(f"WM injected (log):      {injection_log.get('found')} — {injection_log.get('chars')} chars")

    raw["_arm_summary"] = {
        "t1_done": t1_result.get("status") == "done",
        "t1_api_score": t1_api_score,
        "t1_llm_found": bool(t1_llm_summary and not t1_is_deterministic),
        "t2_done": t2_result.get("status") == "done",
        "t2_first_plan_field": t2_first_fields["first_field"],
        "t2_final_field": t2_final_fields["first_field"],
        "t2_debug_repairs": t2_repairs["debug_repairs"],
    }
    # Update the raw file with summary
    raw_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")

    return raw


# ─────────────────────────────────────────────────────────────
# Report generation
# ─────────────────────────────────────────────────────────────

def generate_report() -> None:
    """Read most recent OFF and ON raw JSONs and write the comparison report."""
    off_files = sorted(REPORT_DIR.glob("wm-api-contract-pilot-off-r3-raw-*.json"), reverse=True)
    on_files = sorted(REPORT_DIR.glob("wm-api-contract-pilot-on-r2-raw-*.json"), reverse=True)

    if not off_files:
        print("ERROR: No WM OFF r3 raw file found. Run --arm=off first.")
        sys.exit(1)
    if not on_files:
        print("ERROR: No WM ON r2 raw file found. Run --arm=on first.")
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
    off_inj = on_raw.get("injection", {})
    on_inj_log = on_raw.get("injection_log", {})

    wm_on_first_field = on.get("t2_first_plan_field", "unknown")
    wm_off_first_field = off.get("t2_first_plan_field", "unknown")

    if wm_off_first_field == "error" and wm_on_first_field == "code":
        verdict = "PASS — WM produced measurable planning improvement"
        explanation = (
            "WM OFF T2 used `result[\"error\"]` on the first plan. "
            "WM ON T2 used `result[\"code\"]` on the first plan. "
            "The injected Implementation Strategy carried the correct field name and changed planner behavior."
        )
    elif wm_off_first_field == "error" and wm_on_first_field == "error":
        verdict = "NULL / INCONCLUSIVE — WM ON did not change planner behavior"
        explanation = (
            "Both arms used `result[\"error\"]` on the first plan. "
            "WM injection occurred but the Implementation Strategy did not lead the planner to use `result[\"code\"]`. "
            "Check: (1) IS content in trimmed block, (2) IS chars visible, (3) IS text for code key presence."
        )
    elif wm_off_first_field == "code":
        verdict = "BASELINE FAIL — WM OFF already used result[\"code\"]"
        explanation = (
            "WM OFF T2 used `result[\"code\"]` without WM injection. "
            "The corpus exclusivity assumption failed — the planner obtained the correct key from inspection "
            "or the task description alone. Cannot measure WM benefit."
        )
    else:
        verdict = f"UNEXPECTED — WM OFF first field={wm_off_first_field!r}, WM ON first field={wm_on_first_field!r}"
        explanation = "Review first plan formatter content manually."

    report_ts = time.strftime("%Y%m%d")
    report_path = REPORT_DIR / f"working-memory-api-contract-parser-pilot-rerun-{report_ts}.md"

    off_t1_api = off_t1.get("api_capture", {})
    on_t1_api = on_t1.get("api_capture", {})

    lines = [
        "# WorkingMemory API-Contract Parser Pilot: Rerun (WM OFF R3 / WM ON R2)",
        "",
        f"**Date:** {report_ts[:4]}-{report_ts[4:6]}-{report_ts[6:]}",
        f"**Gate:** {verdict}",
        "",
        "## Context",
        "",
        "Prior WM ON pilot (2026-06-13) was invalidated because the T1 LLM summary omitted",
        "`code`, `EMPTY`, `FORMAT`, `OVERFLOW`. The TASK_SUMMARY prompt was updated and",
        "validated at 8/8 API-contract score (task 907). This rerun tests whether the",
        "improved prompt now allows WM to change T2 planner behavior.",
        "",
        "## Configuration",
        "",
        "| Setting | WM OFF R3 | WM ON R2 |",
        "|---|---|---|",
        "| `ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY` | `1` | `1` |",
        f"| `WORKING_MEMORY_PERSISTENCE_ENABLED` | `False` | `True` |",
        f"| `WORKING_MEMORY_RENDER_ENABLED` | `False` | `True` |",
        f"| `WORKING_MEMORY_INJECTION_ENABLED` | `False` | `True` |",
        "| All other continuation flags | `False` | `False` |",
        f"| WM OFF project | `{off_raw.get('workspace_slug')}` (id={off_raw.get('project_id')}) | — |",
        f"| WM ON project | — | `{on_raw.get('workspace_slug')}` (id={on_raw.get('project_id')}) |",
        "",
        "## T1 Results",
        "",
        "| Metric | WM OFF R3 | WM ON R2 |",
        "|---|---|---|",
        f"| Task ID | {off_raw.get('t1_task_id')} | {on_raw.get('t1_task_id')} |",
        f"| Status | {off_t1.get('status')} | {on_t1.get('status')} |",
        f"| Elapsed | {off_raw.get('t1_elapsed_s')}s | {on_raw.get('t1_elapsed_s')}s |",
        f"| Debug repairs | {off_t1.get('debug_repairs')} | {on_t1.get('debug_repairs')} |",
        f"| Planning repairs | {off_t1.get('planning_repairs')} | {on_t1.get('planning_repairs')} |",
        f"| LLM summary found | {off_t1.get('llm_summary_source')} | {on_t1.get('llm_summary_source')} |",
        f"| API contract score | {off_t1.get('api_score')}/8 | {on_t1.get('api_score')}/8 |",
        "",
        "### T1 API Contract Capture",
        "",
        "| Indicator | WM OFF | WM ON |",
        "|---|---|---|",
    ]
    for key in ["parse_amount", "ok_key", "value_key", "code_key",
                "EMPTY_sentinel", "FORMAT_sentinel", "OVERFLOW_sentinel", "never_raises"]:
        lines.append(f"| `{key}` | {off_t1_api.get(key, '?')} | {on_t1_api.get(key, '?')} |")

    # T1 LLM summaries
    off_llm = (off_t1.get("llm_summary") or "").strip()
    on_llm = (on_t1.get("llm_summary") or "").strip()

    lines += [
        "",
        "### T1 LLM Summary — WM OFF R3",
        "",
        "```",
        off_llm[:800] if off_llm else "(not captured)",
        "```",
        "",
        "### T1 LLM Summary — WM ON R2",
        "",
        "```",
        on_llm[:800] if on_llm else "(not captured)",
        "```",
    ]

    # WM injection analysis
    if off_inj or on_inj_log:
        lines += [
            "",
            "## WM Injection Analysis (WM ON R2)",
            "",
            f"| Metric | Value |",
            "|---|---|",
            f"| WM rendered block | {off_inj.get('wm_rendered_len', on_raw.get('injection', {}).get('wm_rendered_len', 'N/A'))} chars |",
            f"| Base context cap | {off_inj.get('base_context_cap', on_raw.get('injection', {}).get('base_context_cap', 'N/A'))} chars |",
            f"| Chars trimmed to planner | {on_raw.get('injection', {}).get('trimmed_len', 'N/A')} |",
            f"| IS reachable | {on_raw.get('injection', {}).get('impl_strategy_reachable', 'N/A')} |",
            f"| IS chars visible | {on_raw.get('injection', {}).get('impl_strategy_chars_visible', 'N/A')} |",
            f"| Render order correct | {on_raw.get('injection', {}).get('render_order_correct', 'N/A')} |",
            f"| Log injection found | {on_inj_log.get('found', 'N/A')} |",
            f"| Log injection chars | {on_inj_log.get('chars', 'N/A')} |",
            "",
            "**Trimmed content (what planner receives):**",
            "```",
            on_raw.get('injection', {}).get('trimmed_content', '(not captured)')[:600],
            "```",
        ]

    # T2 results
    off_first = off_t2.get("formatter_first_plan", "")
    on_first = on_t2.get("formatter_first_plan", "")
    off_final = off_t2.get("formatter_final", "")
    on_final = on_t2.get("formatter_final", "")

    lines += [
        "",
        "## T2 Results",
        "",
        "| Metric | WM OFF R3 | WM ON R2 |",
        "|---|---|---|",
        f"| Task ID | {off_raw.get('t2_task_id')} | {on_raw.get('t2_task_id')} |",
        f"| Status | {off_t2.get('status')} | {on_t2.get('status')} |",
        f"| Elapsed | {off_raw.get('t2_elapsed_s')}s | {on_raw.get('t2_elapsed_s')}s |",
        f"| Debug repairs | {off_t2.get('debug_repairs')} | {on_t2.get('debug_repairs')} |",
        f"| Planning repairs | {off_t2.get('planning_repairs')} | {on_t2.get('planning_repairs')} |",
        f"| **First plan field** | **{off.get('t2_first_plan_field', '?')}** | **{on.get('t2_first_plan_field', '?')}** |",
        f"| Final field | {off.get('t2_final_field', '?')} | {on.get('t2_final_field', '?')} |",
        "",
        "### T2 First Plan Formatter — WM OFF R3",
        "",
        "```python",
        off_first[:500] if off_first else "(not captured)",
        "```",
        "",
        "### T2 First Plan Formatter — WM ON R2",
        "",
        "```python",
        on_first[:500] if on_first else "(not captured)",
        "```",
        "",
        "### T2 Final Formatter — WM OFF R3",
        "",
        "```python",
        off_final[:500] if off_final else "(not found)",
        "```",
        "",
        "### T2 Final Formatter — WM ON R2",
        "",
        "```python",
        on_final[:500] if on_final else "(not found)",
        "```",
    ]

    # Comparison table
    lines += [
        "",
        "## Signal Comparison",
        "",
        "| Signal | WM OFF R3 | WM ON R2 |",
        "|---|---|---|",
        f"| T1 status | {off_t1.get('status')} | {on_t1.get('status')} |",
        f"| T1 API score | {off_t1.get('api_score')}/8 | {on_t1.get('api_score')}/8 |",
        f"| T1 summary `code` | {off_t1_api.get('code_key')} | {on_t1_api.get('code_key')} |",
        f"| WM injected | No | {on_inj_log.get('found', 'N/A')} |",
        f"| **T2 first field** | **{wm_off_first_field}** | **{wm_on_first_field}** |",
        f"| T2 debug repairs | {off_t2.get('debug_repairs')} | {on_t2.get('debug_repairs')} |",
        f"| T2 planning repairs | {off_t2.get('planning_repairs')} | {on_t2.get('planning_repairs')} |",
        "",
        "## Gate Decision",
        "",
        f"**{verdict}**",
        "",
        explanation,
    ]

    # Regression checks
    off_reg = off_raw.get("regression_checks", {})
    on_reg = on_raw.get("regression_checks", {})
    lines += [
        "",
        "## Regression Checks",
        "",
        "| Check | WM OFF | WM ON |",
        "|---|---|---|",
    ]
    for k in set(list(off_reg.keys()) + list(on_reg.keys())):
        lines.append(f"| `{k}` | {off_reg.get(k, 'N/A')} | {on_reg.get(k, 'N/A')} |")

    report_path.write_text("\n".join(lines) + "\n", encoding="utf-8")
    print(f"\n[report] Written to: {report_path}")
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
