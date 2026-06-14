"""
WM API-Contract Render-First Pilot Runner

Focused single-arm (WM ON) T1 validation of the render-first fix.
Verifies that after the fix, API Contract appears before prose in the rendered
WM block and critical keys/sentinels survive the 400-char planning context trim.

Project: wm-api-contract-render-first-calclib

Usage:
  python3 wm_api_contract_render_first_runner.py

Worker must be running with:
  ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY=1
  WORKING_MEMORY_PERSISTENCE_ENABLED=True
  WORKING_MEMORY_RENDER_ENABLED=True
  WORKING_MEMORY_INJECTION_ENABLED=True
  REPO_MEMORY_INJECTION_ENABLED=False
  PSS_CONTINUATION_INJECTION_ENABLED=False
  ARTIFACT_CONTINUATION_ENABLED=False
  LANGFUSE_ENABLED=false
  REDUCED_PLANNING_PROMPT_ENABLED=False
"""

import json
import os
import re
import sys
import time
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from app.auth import create_access_token  # noqa: E402
import requests  # noqa: E402

BASE_URL = "http://127.0.0.1:8080"
USER_EMAIL = os.environ.get("ORCHESTRATOR_USER_EMAIL", "eval@local.dev")

WORKSPACE_SLUG = "wm-api-contract-render-first-calclib"
WORKSPACE_BASE = Path("/root/.openclaw/workspace/vault/projects")
REPORT_DIR = REPO_ROOT / "docs/roadmap/reports/maintenance"

PLANNING_CONTEXT_CAP = 400
DETERMINISTIC_PREFIX = "Task completed with verified execution evidence"

HEADERS: dict = {}


# ─────────────────────────────────────────────────────────────
# Task description
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


# ─────────────────────────────────────────────────────────────
# Slot management
# ─────────────────────────────────────────────────────────────

SLOT_KEY = "orchestrator:backend_slots:local_openclaw"


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

def create_project(slug: str) -> dict:
    workspace = str(WORKSPACE_BASE / slug)
    p = _api("POST", "/api/v1/projects", json={
        "name": slug,
        "description": "WM API-contract render-first focused validation",
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


# ─────────────────────────────────────────────────────────────
# API contract scoring
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
        code_pos = _pos('"code"')
        if code_pos == -1:
            code_pos = _pos("'code'")

        empty_pos = _pos("EMPTY")
        format_pos = _pos("FORMAT")
        overflow_pos = _pos("OVERFLOW")
        api_contract_pos = _pos("API Contract:")

        return {
            "wm_rendered_len": wm_len,
            "collapsed_len": len(collapsed),
            "base_cap": PLANNING_CONTEXT_CAP,
            "trimmed_content": trimmed,
            "api_contract_pos": api_contract_pos,
            "failure_return_pos": failure_pos,
            "success_return_pos": success_pos,
            "failure_before_success": (
                failure_pos != -1 and success_pos != -1 and failure_pos < success_pos
            ),
            "code_pos": code_pos,
            "code_in_250": code_pos != -1 and code_pos < 250,
            "code_in_400": code_pos != -1 and code_pos < PLANNING_CONTEXT_CAP,
            "EMPTY_pos": empty_pos,
            "FORMAT_pos": format_pos,
            "OVERFLOW_pos": overflow_pos,
            "EMPTY_in_400": empty_pos != -1 and empty_pos < PLANNING_CONTEXT_CAP,
            "FORMAT_in_400": format_pos != -1 and format_pos < PLANNING_CONTEXT_CAP,
            "OVERFLOW_in_400": overflow_pos != -1 and overflow_pos < PLANNING_CONTEXT_CAP,
            "all_sentinels_in_400": (
                empty_pos != -1 and empty_pos < PLANNING_CONTEXT_CAP
                and format_pos != -1 and format_pos < PLANNING_CONTEXT_CAP
                and overflow_pos != -1 and overflow_pos < PLANNING_CONTEXT_CAP
            ),
            "api_before_summary_in_collapsed": (
                api_contract_pos != -1
                and (collapsed.find("Summary:") == -1 or api_contract_pos < collapsed.find("Summary:"))
            ),
        }
    except Exception as e:
        return {"error": str(e)}


# ─────────────────────────────────────────────────────────────
# Regression checks
# ─────────────────────────────────────────────────────────────

def count_repairs_from_report(report_path: Path) -> dict:
    if not report_path.exists():
        return {"debug_repairs": -1, "planning_repairs": -1, "error": "report not found"}
    text = report_path.read_text(encoding="utf-8", errors="replace")
    return {
        "debug_repairs": text.count("[DEBUG_REPAIR_DIRECT] attempting"),
        "planning_repairs": text.count("[REPAIR_DIRECT] completed direct structured repair"),
    }


def scan_regression_checks(worker_log: Path, log_offset: int) -> dict:
    if not worker_log.exists():
        return {}
    lines = worker_log.read_text(encoding="utf-8", errors="replace").splitlines()
    text = "\n".join(lines[log_offset:])
    return {
        "pip_show": "pip show" in text.lower(),
        "nested_project_folder": "nested_project_folder" in text,
        "path_guard_advisory": "PATH_GUARD" in text,
        "vma_error": "[VMA]" in text or "verification_mutates_source" in text,
        "empty_model_response": bool(re.search(r"empty.*response", text, re.I)),
    }


def get_log_offset(worker_log: Path) -> int:
    if not worker_log.exists():
        return 0
    return len(worker_log.read_text(encoding="utf-8", errors="replace").splitlines())


def scan_progress_notes(agent_dir: Path) -> dict:
    pn_path = agent_dir / "progress_notes.md"
    if not pn_path.exists():
        return {"exists": False, "deterministic": None}
    text = pn_path.read_text(encoding="utf-8", errors="replace")
    return {
        "exists": True,
        "deterministic": DETERMINISTIC_PREFIX in text,
        "len": len(text),
    }


# ─────────────────────────────────────────────────────────────
# Main
# ─────────────────────────────────────────────────────────────

def run() -> dict:
    import subprocess
    commit_sha = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, text=True
    ).strip()

    print()
    print("=" * 65)
    print("WM API-CONTRACT RENDER-FIRST PILOT — WM ON (T1 focused)")
    print(f"Project: {WORKSPACE_SLUG}  Commit: {commit_sha}")
    print("=" * 65)

    worker_log = REPO_ROOT / "logs" / "worker.log"
    log_offset = get_log_offset(worker_log)

    init_auth()
    wait_slot()

    project = create_project(WORKSPACE_SLUG)
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

    # Collect T1 artifacts
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
    pn_info = scan_progress_notes(agent_dir)
    regression = scan_regression_checks(worker_log, log_offset)

    print(f"\n  WM JSON exists:    {wm_path.exists()}")
    print(f"  Is LLM (not det):  {not t1_is_det}")
    print(f"  API score:         {t1_api_score}/8")
    for k, v in t1_api_capture.items():
        print(f"    {k}: {v}")
    print(f"  debug repairs:     {t1_repairs['debug_repairs']}")
    print(f"  planning repairs:  {t1_repairs['planning_repairs']}")
    print(f"  progress_notes:    exists={pn_info['exists']} det={pn_info.get('deterministic')}")

    # WM trim analysis
    trim_analysis = {}
    if wm_path.exists():
        trim_analysis = compute_trim_analysis(workspace_path)
        print(f"\n  WM trim analysis:")
        print(f"    rendered len:           {trim_analysis.get('wm_rendered_len')} chars")
        print(f"    collapsed len:          {trim_analysis.get('collapsed_len')} chars")
        print(f"    API Contract pos:       {trim_analysis.get('api_contract_pos')}")
        print(f"    API before Summary:     {trim_analysis.get('api_before_summary_in_collapsed')}")
        print(f"    failure return pos:     {trim_analysis.get('failure_return_pos')}")
        print(f"    success return pos:     {trim_analysis.get('success_return_pos')}")
        print(f"    failure before success: {trim_analysis.get('failure_before_success')}")
        print(f"    \"code\" pos:            {trim_analysis.get('code_pos')}")
        print(f"    \"code\" in 250:         {trim_analysis.get('code_in_250')}")
        print(f"    \"code\" in 400:         {trim_analysis.get('code_in_400')}")
        print(f"    EMPTY pos:              {trim_analysis.get('EMPTY_pos')}")
        print(f"    FORMAT pos:             {trim_analysis.get('FORMAT_pos')}")
        print(f"    OVERFLOW pos:           {trim_analysis.get('OVERFLOW_pos')}")
        print(f"    all sentinels in 400:   {trim_analysis.get('all_sentinels_in_400')}")
        print(f"  Trimmed content (400 chars):")
        print(f"    {trim_analysis.get('trimmed_content', '')}")

    print(f"\n  Regression checks:")
    for k, v in regression.items():
        print(f"    {k}: {v}")

    # Determine verdict
    t1_done = t1_result.get("status") == "done"
    api_score_ok = t1_api_score == 8
    code_in_250 = trim_analysis.get("code_in_250", False)
    sentinels_in_400 = trim_analysis.get("all_sentinels_in_400", False)
    api_before_summary = trim_analysis.get("api_before_summary_in_collapsed", False)
    det_ok = pn_info.get("deterministic", False)

    if t1_done and api_score_ok and code_in_250 and sentinels_in_400 and api_before_summary:
        verdict = "PASS"
    elif not t1_done:
        verdict = "FAIL — T1 not DONE"
    elif not api_score_ok:
        verdict = f"FAIL — API score {t1_api_score}/8"
    elif not code_in_250:
        verdict = f"FAIL — code at char {trim_analysis.get('code_pos')}, not within 250"
    elif not sentinels_in_400:
        verdict = "FAIL — sentinels not within 400 chars"
    else:
        verdict = "NULL / INCONCLUSIVE"

    print(f"\n  Verdict: {verdict}")

    raw = {
        "commit_sha": commit_sha,
        "project_id": project_id,
        "workspace_slug": WORKSPACE_SLUG,
        "t1_task_id": t1_id,
        "t1_status": t1_result.get("status"),
        "t1_elapsed_s": t1_elapsed,
        "t1_api_score": t1_api_score,
        "t1_api_capture": t1_api_capture,
        "t1_wm_summary": t1_wm_summary,
        "t1_is_deterministic": t1_is_det,
        "t1_debug_repairs": t1_repairs["debug_repairs"],
        "t1_planning_repairs": t1_repairs["planning_repairs"],
        "progress_notes": pn_info,
        "trim_analysis": trim_analysis,
        "regression_checks": regression,
        "verdict": verdict,
    }

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    raw_filename = f"wm-api-contract-render-first-raw-{timestamp}.json"
    raw_path = REPORT_DIR / raw_filename
    REPORT_DIR.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[raw] {raw_path}")
    return raw


if __name__ == "__main__":
    run()
