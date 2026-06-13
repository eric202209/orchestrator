"""Stage 1 validation: ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY=1.

Goal: run one task with a non-obvious dict-return API. Collect:
  - whether LLM summary fires vs. deterministic fallback
  - what text the summary contains (captures API contract?)
  - whether that text is also written to progress_notes
  - whether WM would receive the same text if persistence were ON
  - latency of the summary call
  - any failure or fallback

WorkingMemory is OFF throughout.
"""

import json
import os
import sys
import time
from pathlib import Path

# Set flag BEFORE any app imports so os.getenv sees it
os.environ["ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY"] = "1"

REPO_ROOT = Path(__file__).resolve().parents[2]
sys.path.insert(0, str(REPO_ROOT))

from app.config import settings  # noqa: E402

assert not settings.WORKING_MEMORY_PERSISTENCE_ENABLED, "WM must be OFF"
assert not settings.WORKING_MEMORY_RENDER_ENABLED, "WM must be OFF"
assert not settings.WORKING_MEMORY_INJECTION_ENABLED, "WM must be OFF"

import requests  # noqa: E402
from app.auth import create_access_token  # noqa: E402

BASE_URL = "http://127.0.0.1:8080"
USER_EMAIL = os.environ["ORCHESTRATOR_USER_EMAIL"]
WORKSPACE_SLUG = "wm-summary-fix-calclib"
REPORT_DIR = REPO_ROOT / "docs/roadmap/reports/maintenance"
RAW_OUT = REPORT_DIR / f"wm-llm-summary-smoke-raw-{time.strftime('%Y%m%d_%H%M%S')}.json"
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
    print("[init] Auth token created")


def wait_slot(poll: int = 15, timeout: int = 600) -> None:
    import redis as redis_lib
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    r = redis_lib.Redis()
    engine = create_engine(f"sqlite:///{REPO_ROOT}/orchestrator.db", connect_args={"check_same_thread": False})
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
                    __import__("sqlalchemy").text("SELECT status FROM sessions WHERE id=:id"),
                    {"id": sid},
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
    p = _api("POST", "/api/v1/projects", json={
        "name": WORKSPACE_SLUG,
        "description": "Stage 1 LLM summary smoke test",
        "workspace_path": workspace,
    })
    print(f"[project] id={p['id']} workspace={workspace}")
    # Return with resolved absolute path regardless of what API echoes back
    p["_workspace_abs"] = workspace
    return p


def create_task(project_id: int, desc: str) -> dict:
    t = _api("POST", "/api/v1/tasks", json={
        "project_id": project_id,
        "title": "Bootstrap calclib with parser",
        "description": desc,
        "plan_position": 1,
        "execution_profile": "full_lifecycle",
    })
    print(f"[task] id={t['id']}")
    return t


def dispatch_task(task_id: int) -> None:
    _api("POST", f"/api/v1/tasks/{task_id}/retry", json={})
    print(f"[dispatch] task {task_id} dispatched")


def poll_task(task_id: int, timeout: int = 900, poll: int = 20) -> dict:
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


def find_summary_in_events(project_dir: Path, task_id: int) -> dict:
    """Read JSONL event log and extract task_summary phase events."""
    result = {
        "summary_phase_start": False,
        "summary_events": [],
        "llm_summary_text": None,
        "fallback_used": None,
        "summary_latency_s": None,
        "error": None,
    }
    # Events live in .agent/events/session_NNN_task_NNN.jsonl
    events_dir = project_dir / ".agent" / "events"
    if not events_dir.exists():
        result["error"] = f"No events dir at {events_dir}"
        return result
    # Find the JSONL for this task_id
    import glob
    candidates = sorted(events_dir.glob(f"*_task_{task_id}.jsonl"))
    if not candidates:
        result["error"] = f"No events file for task {task_id} in {events_dir}"
        return result
    jsonl_path = candidates[-1]

    lines = jsonl_path.read_text(encoding="utf-8", errors="replace").splitlines()
    events = []
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            ev = json.loads(line)
            if str(ev.get("task_id")) == str(task_id):
                events.append(ev)
        except Exception:
            pass

    phase_start_ts = None
    phase_end_ts = None

    for ev in events:
        details = ev.get("details") or {}
        etype = ev.get("event_type", "")

        if etype == "phase_started" and details.get("phase") == "task_summary":
            result["summary_phase_start"] = True
            phase_start_ts = ev.get("timestamp")

        # Look for summary-related metadata events
        if details.get("phase") == "task_summary":
            result["summary_events"].append({
                "type": etype,
                "details": details,
                "ts": ev.get("timestamp"),
            })

        # task_complete event may carry the summary
        if etype in ("task_complete", "phase_completed", "completion_result"):
            summary = details.get("summary") or details.get("output") or ""
            if summary and len(summary) > 30:
                result["llm_summary_text"] = summary
                phase_end_ts = ev.get("timestamp")

    # try to compute latency
    if phase_start_ts and phase_end_ts:
        try:
            from datetime import datetime
            fmt = "%Y-%m-%dT%H:%M:%S"
            t0 = datetime.fromisoformat(phase_start_ts.replace("Z", "+00:00"))
            t1 = datetime.fromisoformat(phase_end_ts.replace("Z", "+00:00"))
            result["summary_latency_s"] = round((t1 - t0).total_seconds(), 1)
        except Exception:
            pass

    return result


def extract_deterministic_summary(project_dir: Path) -> str:
    """Re-run the deterministic summary function on the workspace to get expected text."""
    # We can't easily re-run it without orchestration_state, so we describe what it would produce
    return (
        "Task completed with verified execution evidence. Completed steps: N/M. "
        "Changed files: src/calclib/__init__.py, src/calclib/parser.py, tests/test_parser.py."
    )


def analyze_progress_notes(progress_notes: str, summary_text: str) -> dict:
    """Check whether the LLM summary text appears in progress_notes."""
    if not summary_text or not progress_notes:
        return {"llm_in_progress_notes": False, "deterministic_in_progress_notes": True}
    # Check if the summary text (or first 100 chars) appears in progress_notes
    probe = summary_text[:120].strip()
    llm_in_notes = probe in progress_notes
    # Check if deterministic pattern appears
    determ_in_notes = "Task completed with verified execution evidence" in progress_notes
    return {
        "llm_in_progress_notes": llm_in_notes,
        "deterministic_in_progress_notes": determ_in_notes,
    }


def assess_api_capture(summary_text: str) -> dict:
    """Check whether the summary captures the non-obvious API contract."""
    text = (summary_text or "").lower()
    return {
        "dict_return_type": "dict" in text or "dictionary" in text,
        "ok_key": '"ok"' in text or "'ok'" in text or " ok " in text or text.startswith("ok"),
        "value_key": '"value"' in text or "'value'" in text or " value " in text,
        "error_key": '"error"' in text or "'error'" in text or " error " in text,
        "invalid_number_sentinel": "invalid_number" in text or "invalid number" in text,
        "no_exception": "exception" in text or "no exception" in text or "raise" in text,
        "return_type_mentioned": any(
            w in text for w in ["returns", "return type", "dict", "result", "shape"]
        ),
    }


def main():
    print("[init] Verifying WM flags OFF and LLM summary flag ON")
    assert not settings.WORKING_MEMORY_PERSISTENCE_ENABLED
    assert not settings.WORKING_MEMORY_RENDER_ENABLED
    assert not settings.WORKING_MEMORY_INJECTION_ENABLED
    assert os.getenv("ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY") == "1"
    print("  WM all OFF ✓")
    print("  ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY=1 ✓")

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

    task_result = poll_task(task_id, timeout=900, poll=20)
    t_end = time.time()
    total_elapsed = round(t_end - t_start, 1)
    print(f"[run] Task finished in {total_elapsed}s with status={task_result.get('status')}")

    # Collect artifacts
    project_dir = workspace_path
    agent_dir = project_dir / ".agent"

    progress_notes_path = agent_dir / "progress_notes.md"
    wm_path = agent_dir / "working_memory.json"

    progress_notes = read_file_safe(progress_notes_path)
    wm_json_text = read_file_safe(wm_path)

    # Parse WM if present (shouldn't be populated since WM is OFF)
    try:
        wm_data = json.loads(wm_json_text)
    except Exception:
        wm_data = {}

    # Extract summary from event log
    events_analysis = find_summary_in_events(project_dir, task_id)

    # Try to get summary from task record
    summary_from_task = task_result.get("summary", "") or ""

    # The actual summary text: prefer event-extracted, fallback to task record
    actual_summary = events_analysis.get("llm_summary_text") or summary_from_task or ""

    # Check if summary appears in progress_notes
    notes_analysis = analyze_progress_notes(progress_notes, actual_summary)

    # Check API contract capture
    api_capture = assess_api_capture(actual_summary)

    # WM implementation_strategy if WM had been ON
    wm_strategies = wm_data.get("implementation_strategy", [])
    wm_summary_text = wm_strategies[-1].get("summary", "") if wm_strategies else ""
    wm_same_as_llm = (
        wm_summary_text[:100] == actual_summary[:100] if wm_summary_text and actual_summary else None
    )

    # Deterministic check: is the summary just file lists?
    is_deterministic = (
        actual_summary.startswith("Task completed with verified execution evidence")
        if actual_summary else False
    )

    raw = {
        "project_id": project_id,
        "task_id": task_id,
        "task_status": task_result.get("status"),
        "total_task_elapsed_s": total_elapsed,
        "wm_flags": {
            "persistence": settings.WORKING_MEMORY_PERSISTENCE_ENABLED,
            "render": settings.WORKING_MEMORY_RENDER_ENABLED,
            "injection": settings.WORKING_MEMORY_INJECTION_ENABLED,
        },
        "llm_summary_flag": os.getenv("ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY"),
        "summary_phase_started": events_analysis["summary_phase_start"],
        "summary_events": events_analysis["summary_events"],
        "actual_summary_text": actual_summary,
        "summary_is_deterministic": is_deterministic,
        "summary_latency_s": events_analysis.get("summary_latency_s"),
        "summary_error": events_analysis.get("error"),
        "progress_notes_excerpt": progress_notes[-2000:] if progress_notes else "",
        "notes_analysis": notes_analysis,
        "api_capture": api_capture,
        "wm_strategies": wm_strategies,
        "wm_summary_text": wm_summary_text,
        "wm_same_as_llm": wm_same_as_llm,
        "task_result_debug_repair_count": task_result.get("debug_repair_count"),
        "task_result_planning_repair_count": task_result.get("planning_repair_count"),
    }

    RAW_OUT.parent.mkdir(parents=True, exist_ok=True)
    RAW_OUT.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[done] Raw results: {RAW_OUT}")

    # Print summary
    print("\n" + "=" * 60)
    print("STAGE 1 SUMMARY — LLM TASK SUMMARY VALIDATION")
    print("=" * 60)
    print(f"Task status:               {task_result.get('status')}")
    print(f"Total elapsed:             {total_elapsed}s")
    print(f"Summary phase started:     {events_analysis['summary_phase_start']}")
    print(f"Summary is deterministic:  {is_deterministic}")
    print(f"Summary latency (est):     {events_analysis.get('summary_latency_s')}s")
    print(f"LLM summary text (first 300 chars):")
    print(f"  {actual_summary[:300]}")
    print()
    print(f"progress_notes has LLM summary:  {notes_analysis['llm_in_progress_notes']}")
    print(f"progress_notes has deterministic: {notes_analysis['deterministic_in_progress_notes']}")
    print()
    print(f"API contract capture:")
    for k, v in api_capture.items():
        print(f"  {k}: {v}")
    print()
    if wm_data:
        print(f"WM data (WM was OFF — should be empty): {bool(wm_data)}")
        print(f"  implementation_strategy entries: {len(wm_strategies)}")
    else:
        print("WM file: not present (expected — WM is OFF)")
    print()
    print(f"WM would receive same summary as progress_notes: {wm_same_as_llm}")
    print()
    api_captured_keys = sum(1 for v in api_capture.values() if v)
    if is_deterministic or not actual_summary:
        verdict = "FAIL — LLM summary not generated or only deterministic"
    elif api_captured_keys >= 3:
        verdict = "PASS — LLM summary generated and captures API contract"
    elif api_captured_keys >= 1:
        verdict = "PARTIAL — LLM summary generated but only partially captures API contract"
    else:
        verdict = "FAIL — LLM summary generated but does not capture API contract"
    print(f"Stage 1 verdict: {verdict}")
    print("=" * 60)

    return raw


if __name__ == "__main__":
    main()
