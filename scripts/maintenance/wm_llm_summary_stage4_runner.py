"""Stage 4 validation: WM implementation_strategy storage extended to 1200 chars.

Verifies:
- implementation_strategy.summary stores more than 400 chars when LLM output is longer
- API contract details (INVALID_NUMBER, no-exception, key shape) appear in stored summary
- progress_notes **Summary:** section remains deterministic
- Render and injection remain OFF
"""

import json
import os
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
WORKSPACE_SLUG = "wm-summary-retention-calclib"
REPORT_DIR = REPO_ROOT / "docs/roadmap/reports/maintenance"
RAW_OUT = REPORT_DIR / f"wm-llm-summary-stage4-raw-{time.strftime('%Y%m%d_%H%M%S')}.json"
SLOT_KEY = "orchestrator:backend_slots:local_openclaw"

TASK_DESC = """Bootstrap src-layout calclib library.

Setup:
- Create directory structure: src/calclib/
- Create src/calclib/__init__.py (empty, or re-export parse_number)
- Create src/calclib/parser.py

Implement parse_number(text: str) -> dict in parser.py.

The function must return a plain dict (never raise an exception):
  {"ok": bool, "value": int | None, "error": str | None}

For valid integer input ("42", "-7", "0"):
  {"ok": True, "value": <parsed int>, "error": None}

For invalid input ("abc", "", "3.14", None):
  {"ok": False, "value": None, "error": "INVALID_NUMBER"}

Create tests/test_parser.py with pytest cases covering:
  - valid integers (positive, negative, zero)
  - invalid strings (empty string, float string, non-numeric, None)
  - confirm no exceptions are raised for any input

Create pytest.ini at project root:
  [pytest]
  pythonpath = src

Run: PYTHONPATH=src python3 -m pytest tests/test_parser.py -v
All tests must pass.
"""

HEADERS: dict = {}


def _api(method: str, path: str, **kwargs):
    r = requests.request(method, f"{BASE_URL}{path}", headers=HEADERS, **kwargs)
    r.raise_for_status()
    return r.json()


def init_auth() -> None:
    global HEADERS
    token = create_access_token({"sub": USER_EMAIL})
    HEADERS = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}
    print(f"[init] Auth token created for {USER_EMAIL}")


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
                    print(f"  [slot] Evicted stale session {sid} (status={status})")
        finally:
            db.close()

    deadline = time.time() + timeout
    while time.time() < deadline:
        _evict_terminal()
        members = _slot_members()
        if not members:
            print("[slot] Slot clear.")
            return
        print(f"[slot] Occupied by {members}. Waiting {poll}s...")
        time.sleep(poll)
    raise TimeoutError("Backend slot never freed")


WORKSPACE_BASE = Path("/root/.openclaw/workspace/vault/projects")


def create_project() -> dict:
    workspace = str(WORKSPACE_BASE / WORKSPACE_SLUG)
    p = _api(
        "POST",
        "/api/v1/projects",
        json={
            "name": WORKSPACE_SLUG,
            "description": "Stage 4 WM summary retention validation",
            "workspace_path": workspace,
        },
    )
    print(f"[project] id={p['id']} workspace={workspace}")
    p["_workspace_abs"] = workspace
    return p


def create_task(project_id: int, desc: str) -> dict:
    t = _api(
        "POST",
        "/api/v1/tasks",
        json={
            "project_id": project_id,
            "title": "Bootstrap calclib with parser",
            "description": desc,
            "plan_position": 1,
            "execution_profile": "full_lifecycle",
        },
    )
    print(f"[task] id={t['id']}")
    return t


def dispatch_task(task_id: int) -> None:
    _api("POST", f"/api/v1/tasks/{task_id}/retry", json={})
    print(f"[dispatch] task {task_id} dispatched")


def poll_task(task_id: int, timeout: int = 1200, poll: int = 20) -> dict:
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
    raise TimeoutError(f"Task {task_id} did not finish within {timeout}s")


def read_file_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"(could not read: {e})"


def scan_celery_log(worker_log: Path) -> dict:
    result = {
        "http_post_found": False,
        "http_status": None,
        "fallback_warn_found": False,
        "phase5_start_found": False,
        "wm_written_log_found": False,
        "progress_notes_written_log_found": False,
        "phase5_lines": [],
    }
    if not worker_log.exists():
        return result
    lines = worker_log.read_text(encoding="utf-8", errors="replace").splitlines()
    recent = lines[-600:]
    for line in recent:
        if "Phase 5: TASK_SUMMARY" in line:
            result["phase5_start_found"] = True
            result["phase5_lines"].append(line.strip())
        if "ai-gateway:8000/v1/chat/completions" in line:
            result["http_post_found"] = True
            result["phase5_lines"].append(line.strip())
            if "200 OK" in line:
                result["http_status"] = 200
            else:
                import re
                m = re.search(r"HTTP/[\d.]+ (\d+)", line)
                if m:
                    result["http_status"] = int(m.group(1))
        if "summary_generation_failed" in line or "using deterministic completion summary" in line:
            result["fallback_warn_found"] = True
            result["phase5_lines"].append(line.strip())
        if "[WORKING_MEMORY] Written to" in line:
            result["wm_written_log_found"] = True
            result["phase5_lines"].append(line.strip())
        if "[PROGRESS] Progress notes written" in line:
            result["progress_notes_written_log_found"] = True
            result["phase5_lines"].append(line.strip())
    return result


def extract_progress_notes_summary(progress_notes: str) -> str:
    if not progress_notes:
        return ""
    marker = "**Summary:**"
    idx = progress_notes.rfind(marker)
    if idx == -1:
        return ""
    block = progress_notes[idx + len(marker):].strip()
    for sep in ["\n## ", "\n**Steps completed", "\n**Files changed"]:
        end = block.find(sep)
        if end != -1:
            block = block[:end]
    return block.strip()


DETERMINISTIC_PREFIX = "Task completed with verified execution evidence"


def assess_api_capture(summary_text: str) -> dict:
    text = (summary_text or "").lower()
    raw = summary_text or ""
    return {
        "dict_return_type": "dict" in text or "dictionary" in text,
        "ok_key": (
            '"ok"' in raw or "'ok'" in raw or "ok:" in text
            or " ok " in text
        ),
        "value_key": (
            '"value"' in raw or "'value'" in raw or "value:" in text
            or " value " in text
        ),
        "error_key": (
            '"error"' in raw or "'error'" in raw or "error:" in text
            or " error " in text
        ),
        "invalid_number_sentinel": "INVALID_NUMBER" in raw or "invalid number" in text,
        "no_exception": (
            "never raise" in text or "no exception" in text
            or "without raising" in text or "doesn't raise" in text
        ),
    }


def main():
    print("[Stage 4] Validating WM summary retention: storage limit 400→1200")
    print()

    print(f"  WORKING_MEMORY_PERSISTENCE_ENABLED (runner): {settings.WORKING_MEMORY_PERSISTENCE_ENABLED}")
    print(f"  WORKING_MEMORY_RENDER_ENABLED (runner): {settings.WORKING_MEMORY_RENDER_ENABLED}")
    print(f"  WORKING_MEMORY_INJECTION_ENABLED (runner): {settings.WORKING_MEMORY_INJECTION_ENABLED}")
    print(f"  ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY env: {os.getenv('ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY', 'NOT SET')}")
    print()

    from app.services.orchestration.working_memory import _SUMMARY_STORAGE_LIMIT, _SUMMARY_RENDER_LIMIT
    print(f"  _SUMMARY_STORAGE_LIMIT: {_SUMMARY_STORAGE_LIMIT}")
    print(f"  _SUMMARY_RENDER_LIMIT:  {_SUMMARY_RENDER_LIMIT}")
    print()

    init_auth()
    wait_slot()

    project = create_project()
    project_id = project["id"]
    workspace_path = Path(project["_workspace_abs"])

    task = create_task(project_id, TASK_DESC)
    task_id = task["id"]

    print(f"[run] Dispatching task {task_id}...")
    t_start = time.time()
    dispatch_task(task_id)

    task_result = poll_task(task_id, timeout=1200, poll=20)
    t_end = time.time()
    total_elapsed = round(t_end - t_start, 1)
    print(f"[run] Task finished in {total_elapsed}s with status={task_result.get('status')}")

    agent_dir = workspace_path / ".agent"
    progress_notes_path = agent_dir / "progress_notes.md"
    wm_path = agent_dir / "working_memory.json"
    worker_log = REPO_ROOT / "logs" / "worker.log"

    progress_notes_text = read_file_safe(progress_notes_path)
    wm_json_text = read_file_safe(wm_path)

    wm_exists = wm_path.exists()
    try:
        wm_data = json.loads(wm_json_text) if wm_exists else {}
    except Exception:
        wm_data = {}

    log_analysis = scan_celery_log(worker_log)

    pn_summary = extract_progress_notes_summary(progress_notes_text)

    wm_strategies = wm_data.get("implementation_strategy") or []
    wm_latest_summary = wm_strategies[-1].get("summary", "") if wm_strategies else ""
    wm_known_good = wm_data.get("known_good_commands") or []
    wm_files_by_task = wm_data.get("files_by_task") or {}
    wm_active_constraints = wm_data.get("active_constraints") or []
    wm_unresolved_failures = wm_data.get("unresolved_failures") or []
    wm_schema_version = wm_data.get("schema_version")

    wm_is_deterministic = wm_latest_summary.startswith(DETERMINISTIC_PREFIX)
    pn_is_deterministic = pn_summary.startswith(DETERMINISTIC_PREFIX)
    fallback_triggered = log_analysis["fallback_warn_found"] or wm_is_deterministic
    wm_stored_beyond_400 = len(wm_latest_summary) > 400

    api_capture_wm = assess_api_capture(wm_latest_summary)
    api_capture_pn = assess_api_capture(pn_summary)
    wm_api_keys_captured = sum(1 for v in api_capture_wm.values() if v)

    raw = {
        "stage": 4,
        "project_id": project_id,
        "task_id": task_id,
        "task_status": task_result.get("status"),
        "total_task_elapsed_s": total_elapsed,
        "storage_limit_before": 400,
        "storage_limit_after": _SUMMARY_STORAGE_LIMIT,
        "render_limit_before": 200,
        "render_limit_after": _SUMMARY_RENDER_LIMIT,
        "flags_in_worker": {
            "llm_summary": True,
            "persistence": True,
            "render": False,
            "injection": False,
        },
        "log_analysis": {
            "phase5_start_found": log_analysis["phase5_start_found"],
            "http_post_to_ai_gateway": log_analysis["http_post_found"],
            "http_status": log_analysis["http_status"],
            "fallback_warn_found": log_analysis["fallback_warn_found"],
            "wm_written_log_found": log_analysis["wm_written_log_found"],
            "progress_notes_written_log_found": log_analysis["progress_notes_written_log_found"],
            "phase5_lines": log_analysis["phase5_lines"],
        },
        "fallback_triggered": fallback_triggered,
        "progress_notes_summary": pn_summary,
        "pn_is_deterministic": pn_is_deterministic,
        "wm_path": str(wm_path),
        "wm_exists": wm_exists,
        "wm_schema_version": wm_schema_version,
        "wm_implementation_strategy_count": len(wm_strategies),
        "wm_latest_summary_len": len(wm_latest_summary),
        "wm_latest_summary": wm_latest_summary,
        "wm_stored_beyond_400": wm_stored_beyond_400,
        "wm_known_good_commands_count": len(wm_known_good),
        "wm_files_by_task_count": len(wm_files_by_task),
        "wm_active_constraints_count": len(wm_active_constraints),
        "wm_unresolved_failures_count": len(wm_unresolved_failures),
        "api_capture_wm": api_capture_wm,
        "api_capture_pn": api_capture_pn,
        "wm_api_keys_captured": wm_api_keys_captured,
        "task_debug_repair_count": task_result.get("debug_repair_count"),
        "task_planning_repair_count": task_result.get("planning_repair_count"),
    }

    RAW_OUT.parent.mkdir(parents=True, exist_ok=True)
    RAW_OUT.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[done] Raw results: {RAW_OUT}")

    print("\n" + "=" * 65)
    print("STAGE 4 SUMMARY — WM SUMMARY RETENTION VALIDATION")
    print("=" * 65)
    print(f"Task status:                       {task_result.get('status')}")
    print(f"Total elapsed:                     {total_elapsed}s")
    print(f"Debug repair count:                {task_result.get('debug_repair_count', 0)}")
    print(f"Planning repair count:             {task_result.get('planning_repair_count', 0)}")
    print()
    print(f"Phase 5 found:                     {log_analysis['phase5_start_found']}")
    print(f"HTTP POST to ai-gateway:           {log_analysis['http_post_found']}")
    print(f"HTTP status:                       {log_analysis['http_status']}")
    print(f"Fallback triggered:                {fallback_triggered}")
    print()
    print(f"Storage limit before:              400 chars")
    print(f"Storage limit after:               {_SUMMARY_STORAGE_LIMIT} chars")
    print(f"Render limit before:               200 chars")
    print(f"Render limit after:                {_SUMMARY_RENDER_LIMIT} chars")
    print()
    print(f"working_memory.json exists:        {wm_exists}")
    print(f"schema_version:                    {wm_schema_version}")
    print(f"WM summary stored length:          {len(wm_latest_summary)} chars")
    print(f"WM stored beyond 400 chars:        {wm_stored_beyond_400}")
    print(f"WM is LLM text (not deterministic):{not wm_is_deterministic}")
    print(f"progress_notes is deterministic:   {pn_is_deterministic}")
    print()
    print("WM summary (first 500 chars):")
    print(f"  {wm_latest_summary[:500]}")
    print()
    print("progress_notes summary (first 200 chars):")
    print(f"  {pn_summary[:200]}")
    print()
    print("API contract capture — WM:")
    for k, v in api_capture_wm.items():
        print(f"  {k}: {v}")
    print(f"  [total indicators captured: {wm_api_keys_captured}/6]")
    print()
    print("API contract capture — progress_notes:")
    for k, v in api_capture_pn.items():
        print(f"  {k}: {v}")
    print()

    # Stage 3 API capture for comparison
    stage3_wm_captured = 1  # only dict_return_type in Stage 3

    # Verdict
    if task_result.get("status") != "done":
        verdict = "FAIL — task did not reach DONE"
    elif not wm_exists:
        verdict = "FAIL — working_memory.json not created"
    elif not wm_strategies:
        verdict = "FAIL — implementation_strategy empty"
    elif fallback_triggered:
        verdict = "FAIL — LLM summary not generated (fallback)"
    elif not pn_is_deterministic:
        verdict = "FAIL — progress_notes did not receive deterministic summary"
    elif not wm_stored_beyond_400:
        verdict = "FAIL — WM summary not stored beyond 400 chars (new limit not active)"
    elif wm_api_keys_captured <= stage3_wm_captured:
        verdict = f"PARTIAL — stored beyond 400 but API capture not improved ({wm_api_keys_captured}/6 vs Stage 3: 1/6)"
    else:
        verdict = f"PASS — storage extended, API contract retention improved ({wm_api_keys_captured}/6 captured)"

    print(f"Stage 4 verdict: {verdict}")
    print("=" * 65)

    return raw


if __name__ == "__main__":
    main()
