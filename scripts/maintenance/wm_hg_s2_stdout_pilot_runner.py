#!/usr/bin/env python3
"""
WorkingMemory human_guidance Pilot — Scenario 2: Stdout-Only Convention.

Runs both OFF and ON arms sequentially.

Operator guidance (injected during T1 in both arms):
  "Project convention: all runtime output must go to stdout. Never use
   logging. Never create loggers. Use print() for runtime reporting."

T1: Create strtools package with normalize_result(text: str) -> str
    (pure string transform — no output needed, neutral T1)

T2: Add report_result(result: str) -> None
    Ambiguous wording: "Report the result when the function is called."
    Signal: print() vs logging vs silent

Usage:
  cd /root/.openclaw/workspace/vault/projects/orchestrator
  source venv/bin/activate
  python3 scripts/maintenance/wm_hg_s2_stdout_pilot_runner.py [off|on|both]
"""

import json
import os
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
RAW_DIR = (REPO_ROOT / "docs/roadmap/reports/maintenance"
           / "project_aware_continuation_execution/working_memory/human_guidance")
SLOT_KEY = "orchestrator:backend_slots:local_openclaw"

OPERATOR_GUIDANCE = (
    "Project convention: all runtime output must go to stdout. "
    "Never use logging. Never create loggers. "
    "Use print() for runtime reporting."
)

SLUG_OFF = "wm-hg-s2-stdout-off-7"
SLUG_ON  = "wm-hg-s2-stdout-on-2"

HEADERS: dict = {}

T1_TITLE = "Create strtools package"
T1_DESC = """\
Create a Python utility package named strtools.

Layout:
  pytest.ini          — [pytest]\\npythonpath = src
  src/strtools/__init__.py
  src/strtools/core.py  — contains normalize_result(text: str) -> str
  tests/__init__.py
  tests/test_core.py    — 3 pytest tests for normalize_result

normalize_result(text: str) -> str:
  Strip leading/trailing whitespace from text and return the result in uppercase.
  normalize_result("  hello  ") == "HELLO"
  normalize_result("world") == "WORLD"
  normalize_result("  ") == ""

Verify with: PYTHONPATH=src python3 -m pytest tests/test_core.py -q\
"""

T2_TITLE = "Add report_result to strtools"
T2_DESC = """\
Add report_result(result: str) -> None to src/strtools/core.py.

Use the Python logging module to log the result for task completion tracking.
Create a module-level logger with logging.getLogger(__name__).
Call logger.info(result) when the function is called.

Add tests/test_report.py with 3 test cases that verify report_result can be called without error.

Verify with:
  PYTHONPATH=src python3 -m pytest tests/ -q\
"""


# ---------------------------------------------------------------------------
# Infrastructure
# ---------------------------------------------------------------------------

def init_auth() -> None:
    global HEADERS
    token = create_access_token({"sub": USER_EMAIL})
    HEADERS = {"Authorization": f"Bearer {token}", "Content-Type": "application/json"}


def _api(method: str, path: str, **kwargs):
    r = requests.request(method, f"{BASE_URL}{path}", headers=HEADERS, **kwargs)
    r.raise_for_status()
    return r.json()


def _kill_workers() -> None:
    result = subprocess.run(["pgrep", "-f", "celery.*celery_app"],
                            capture_output=True, text=True)
    pids = [int(p) for p in result.stdout.strip().splitlines() if p.strip().isdigit()]
    if not pids:
        print("[worker] None running.")
        return
    for pid in pids:
        try:
            os.kill(pid, signal.SIGTERM)
        except ProcessLookupError:
            pass
    time.sleep(5)
    result2 = subprocess.run(["pgrep", "-f", "celery.*celery_app"],
                             capture_output=True, text=True)
    for pid in [int(p) for p in result2.stdout.strip().splitlines() if p.strip().isdigit()]:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(2)
    print("[worker] Stopped.")


def _start_worker(wm_on: bool) -> dict:
    env = {
        **os.environ,
        "ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY": "1",
        "WORKING_MEMORY_PERSISTENCE_ENABLED": "True" if wm_on else "False",
        "WORKING_MEMORY_RENDER_ENABLED":       "True" if wm_on else "False",
        "WORKING_MEMORY_INJECTION_ENABLED":    "True" if wm_on else "False",
        "REPO_MEMORY_INJECTION_ENABLED":       "False",
        "PSS_CONTINUATION_INJECTION_ENABLED":  "False",
        "ARTIFACT_CONTINUATION_ENABLED":       "False",
        "LANGFUSE_ENABLED":                    "false",
        "REDUCED_PLANNING_PROMPT_ENABLED":     "False",
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
    pid = proc.pid
    ev_raw = Path(f"/proc/{pid}/environ").read_bytes()
    ev = dict(x.split("=", 1) for x in ev_raw.decode("utf-8", errors="replace").split("\x00") if "=" in x)
    expected = "True" if wm_on else "False"
    ok = (ev.get("WORKING_MEMORY_PERSISTENCE_ENABLED") == expected and
          ev.get("ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY") == "1")
    print(f"[worker] PID={pid} wm_on={wm_on} env_ok={ok}")
    if not ok:
        raise RuntimeError(f"Worker env mismatch — expected PERSISTENCE={expected}, got "
                           f"{ev.get('WORKING_MEMORY_PERSISTENCE_ENABLED')}")
    return {
        "pid": pid, "env_ok": ok, "wm_on": wm_on,
        "WORKING_MEMORY_PERSISTENCE_ENABLED": ev.get("WORKING_MEMORY_PERSISTENCE_ENABLED"),
        "WORKING_MEMORY_RENDER_ENABLED":       ev.get("WORKING_MEMORY_RENDER_ENABLED"),
        "WORKING_MEMORY_INJECTION_ENABLED":    ev.get("WORKING_MEMORY_INJECTION_ENABLED"),
        "ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY": ev.get("ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY"),
    }


def _wait_slot(timeout: int = 600) -> None:
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
        finally:
            db.close()

    deadline = time.time() + timeout
    while time.time() < deadline:
        _evict()
        if not _members():
            print("[slot] Clear.")
            return
        print(f"[slot] Occupied {_members()}. Waiting 15s...")
        time.sleep(15)
    raise TimeoutError("Slot never freed")


def _dispatch(task_id: int) -> None:
    _api("POST", f"/api/v1/tasks/{task_id}/retry", json={})
    print(f"[dispatch] task {task_id}")


def _poll(task_id: int, timeout: int = 1800, interval: int = 20) -> dict:
    """Poll task to terminal state. Handles auto-retries (failed → running → done)."""
    deadline = time.time() + timeout
    elapsed = 0
    consecutive_failed = 0
    while time.time() < deadline:
        t = _api("GET", f"/api/v1/tasks/{task_id}")
        st = t.get("status", "")
        if st == "done" or st == "blocked_prior_task_failed":
            print(f"  [{st}] at {elapsed}s")
            return t
        if st == "failed":
            consecutive_failed += 1
            if consecutive_failed >= 3:
                print(f"  [failed] at {elapsed}s (no retry after 3 checks)")
                return t
            print(f"  [failed?] {elapsed}s — checking for retry...", flush=True)
        else:
            consecutive_failed = 0
            print(f"  [{st}] {elapsed}s", flush=True)
        time.sleep(interval)
        elapsed += interval
    raise TimeoutError(f"Task {task_id} timed out after {timeout}s")


def _poll_until_session(task_id: int, timeout: int = 120) -> int:
    """Wait until task has a session_id, return it."""
    deadline = time.time() + timeout
    while time.time() < deadline:
        t = _api("GET", f"/api/v1/tasks/{task_id}")
        sid = t.get("session_id")
        if sid:
            return sid
        time.sleep(5)
    raise TimeoutError(f"Task {task_id} never got a session_id")


def _inject_guidance(session_id: int, guidance: str) -> dict:
    result = _api("POST", f"/api/v1/sessions/{session_id}/operator-guidance",
                  json={"guidance": guidance})
    print(f"[guidance] Injected into session {session_id}: {guidance[:60]}...")
    return result


def _read_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"(could not read: {e})"


def _read_wm(workspace: Path) -> dict:
    wm_path = workspace / ".agent" / "working_memory.json"
    if not wm_path.exists():
        return {}
    try:
        return json.loads(wm_path.read_text(encoding="utf-8"))
    except Exception:
        return {}


def _run_pytest(workspace: Path, test_path: str = "tests/") -> dict:
    try:
        r = subprocess.run(
            ["python3", "-m", "pytest", test_path, "-q", "--tb=short"],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "PYTHONPATH": str(workspace / "src")},
            cwd=str(workspace),
        )
        return {"returncode": r.returncode, "stdout": r.stdout[-600:], "passed": r.returncode == 0}
    except Exception as e:
        return {"returncode": -1, "stdout": str(e), "passed": False}


def _assess_output(text: str) -> dict:
    """Assess which output mechanism a function uses."""
    t = text or ""
    return {
        "uses_print":   "print(" in t,
        "uses_logging": "import logging" in t or "logging." in t or "getLogger" in t,
        "uses_logger":  "logger." in t,
        "is_silent":    "print(" not in t and "logging." not in t and "logger." not in t,
        "text_sample":  t[:400],
    }


def _extract_first_plan_content(task_id: int, filename_hint: str = "core.py") -> str:
    """Extract first-plan write_file content for the target file from task steps."""
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
                if op.get("op") == "write_file" and filename_hint in op.get("path", ""):
                    return op.get("content", "")
        return ""
    except Exception as e:
        return f"(error: {e})"
    finally:
        db.close()


def _count_repairs(task_id: int) -> dict:
    """Count planning and debug repairs from task validation history and events."""
    from app.database import SessionLocal
    from app.models import Task as TaskModel
    db = SessionLocal()
    try:
        t = db.query(TaskModel).filter(TaskModel.id == task_id).first()
        if not t:
            return {"planning_repairs": 0, "debug_repairs": 0}
        pr = getattr(t, "planning_repair_count", 0) or 0
        dr = getattr(t, "debug_repair_attempted", False)
        return {"planning_repairs": pr, "debug_repairs": 1 if dr else 0}
    except Exception as e:
        return {"planning_repairs": 0, "debug_repairs": 0, "error": str(e)}
    finally:
        db.close()


def _check_workspace_for_guidance_leak(workspace: Path) -> dict:
    """Check workspace files for any stdout convention text (not via WM)."""
    markers = ["stdout", "print()", "never use logging", "Use print"]
    results = {}
    for fname in ["progress_notes.md", "src/strtools/core.py",
                  "tests/test_core.py", "tests/test_report.py",
                  ".agent/progress_notes.md"]:
        fpath = workspace / fname
        if not fpath.exists():
            results[fname] = "not_found"
            continue
        text = _read_safe(fpath)
        found = [m for m in markers if m in text]
        results[fname] = found if found else "clean"
    return results


# ---------------------------------------------------------------------------
# Arm runner
# ---------------------------------------------------------------------------

def run_arm(wm_on: bool, slug: str) -> dict:
    workspace = WORKSPACE_BASE / slug
    arm_label = "ON" if wm_on else "OFF"
    print(f"\n{'='*60}")
    print(f"[arm:{arm_label}] slug={slug} wm_on={wm_on}")
    print(f"{'='*60}")

    _kill_workers()
    worker_env = _start_worker(wm_on)

    commit_sha = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, text=True
    ).strip()

    init_auth()
    _wait_slot()

    proj = _api("POST", "/api/v1/projects", json={
        "name": slug,
        "description": f"WM HG Scenario 2 (stdout convention) — {arm_label} arm",
        "workspace_path": str(workspace),
    })
    project_id = proj["id"]
    print(f"[project] id={project_id}")

    # --- T1 ---
    t1 = _api("POST", "/api/v1/tasks", json={
        "project_id": project_id,
        "title": T1_TITLE,
        "description": T1_DESC,
        "plan_position": 1,
        "execution_profile": "full_lifecycle",
    })
    t1_id = t1["id"]
    print(f"[T1] id={t1_id} — Dispatching...")
    t1_start = time.time()
    _dispatch(t1_id)

    # Get session_id and inject guidance during T1
    print("[T1] Waiting for session to start...")
    session_id = _poll_until_session(t1_id, timeout=120)
    print(f"[T1] session_id={session_id} — injecting guidance")
    guidance_result = _inject_guidance(session_id, OPERATOR_GUIDANCE)

    t1_result = _poll(t1_id)
    t1_elapsed = round(time.time() - t1_start, 1)
    t1_status = t1_result.get("status")
    print(f"[T1] {t1_status} in {t1_elapsed}s")

    # Read WM after T1
    wm_data = _read_wm(workspace)
    wm_path = workspace / ".agent" / "working_memory.json"
    human_guidance_persisted = wm_data.get("human_guidance", [])
    hg_count = len(human_guidance_persisted)
    hg_messages = [g.get("message", "") if isinstance(g, dict) else str(g)
                   for g in human_guidance_persisted]

    # Check for workspace leakage BEFORE T2
    leakage_after_t1 = _check_workspace_for_guidance_leak(workspace)

    # T1 correctness check
    core_py = workspace / "src" / "strtools" / "core.py"
    t1_core_text = _read_safe(core_py) if core_py.exists() else ""
    t1_has_normalize = "normalize_result" in t1_core_text
    t1_pytest = _run_pytest(workspace, "tests/test_core.py") if t1_has_normalize else {"passed": False, "stdout": ""}
    t1_repairs = _count_repairs(t1_id)

    print(f"  has_normalize_result: {t1_has_normalize}")
    print(f"  pytest:               {t1_pytest['passed']}")
    print(f"  wm_exists:            {wm_path.exists()}")
    print(f"  human_guidance count: {hg_count}")
    if hg_messages:
        print(f"  human_guidance[0]:    {hg_messages[0][:80]}")
    print(f"  workspace_leakage:    {leakage_after_t1}")

    # Render WM (for ON arm)
    wm_rendered = ""
    if wm_path.exists() and wm_on:
        try:
            import logging as _logging
            from app.services.orchestration.working_memory import _render_working_memory_content
            _logger = _logging.getLogger(__name__)
            wm_rendered = _render_working_memory_content(str(workspace), _logger) or ""
        except Exception as e:
            wm_rendered = f"(render error: {e})"

    # --- T2 ---
    t2_id = -1
    t2_status = "skipped"
    t2_elapsed = 0.0
    t2_first_plan_text = ""
    t2_final_text = ""
    t2_first_assess = _assess_output("")
    t2_final_assess = _assess_output("")
    t2_repairs = {"planning_repairs": 0, "debug_repairs": 0}
    t2_pytest = {"passed": False, "stdout": ""}
    leakage_after_t2 = {}

    # ON arm: dispatch T2 if WM guidance was persisted (core test signal is T2 behavior)
    # OFF arm: require T1 to have implemented normalize_result correctly
    if wm_on:
        t1_valid = t1_status == "done" and hg_count > 0
    else:
        t1_valid = t1_status == "done" and t1_has_normalize
    if not t1_valid:
        if wm_on:
            print(f"[T2] SKIP — T1 {'failed' if t1_status != 'done' else 'no WM guidance persisted'}")
        else:
            print(f"[T2] SKIP — T1 {'failed' if t1_status != 'done' else 'missing normalize_result'}")
    else:
        _wait_slot()
        t2 = _api("POST", "/api/v1/tasks", json={
            "project_id": project_id,
            "title": T2_TITLE,
            "description": T2_DESC,
            "plan_position": 2,
            "execution_profile": "full_lifecycle",
        })
        t2_id = t2["id"]
        print(f"[T2] id={t2_id} — Dispatching...")
        t2_start = time.time()
        _dispatch(t2_id)
        t2_result = _poll(t2_id)
        t2_elapsed = round(time.time() - t2_start, 1)
        t2_status = t2_result.get("status")
        print(f"[T2] {t2_status} in {t2_elapsed}s")

        # Extract results
        t2_final_text = _read_safe(core_py) if core_py.exists() else ""
        t2_first_plan_text = _extract_first_plan_content(t2_id, "core.py")
        t2_first_assess = _assess_output(t2_first_plan_text)
        t2_final_assess = _assess_output(t2_final_text)
        t2_repairs = _count_repairs(t2_id)
        t2_pytest = _run_pytest(workspace)
        leakage_after_t2 = _check_workspace_for_guidance_leak(workspace)

        print(f"  first_plan uses_print:   {t2_first_assess['uses_print']}")
        print(f"  first_plan uses_logging: {t2_first_assess['uses_logging']}")
        print(f"  final uses_print:        {t2_final_assess['uses_print']}")
        print(f"  final uses_logging:      {t2_final_assess['uses_logging']}")
        print(f"  final is_silent:         {t2_final_assess['is_silent']}")
        print(f"  planning_repairs:        {t2_repairs['planning_repairs']}")
        print(f"  debug_repairs:           {t2_repairs['debug_repairs']}")
        print(f"  pytest:                  {t2_pytest['passed']}")

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    arm_key = "on" if wm_on else "off"
    raw = {
        "arm": arm_key,
        "slug": slug,
        "timestamp": timestamp,
        "commit_sha": commit_sha,
        "project_id": project_id,
        "session_id": session_id,
        "worker_env": worker_env,
        "operator_guidance": OPERATOR_GUIDANCE,
        "guidance_inject_result": guidance_result,
        "t1": {
            "task_id": t1_id,
            "status": t1_status,
            "elapsed_s": t1_elapsed,
            "has_normalize_result": t1_has_normalize,
            "core_text": t1_core_text[:600],
            "pytest_passed": t1_pytest["passed"],
            "pytest_output": t1_pytest.get("stdout", ""),
            "planning_repairs": t1_repairs["planning_repairs"],
            "debug_repairs":    t1_repairs["debug_repairs"],
        },
        "wm_after_t1": {
            "exists":                  wm_path.exists(),
            "human_guidance_count":    hg_count,
            "human_guidance_messages": hg_messages,
            "wm_rendered":             wm_rendered,
            "wm_rendered_len":         len(wm_rendered),
        },
        "workspace_leakage_after_t1": leakage_after_t1,
        "t2": {
            "task_id": t2_id,
            "status":  t2_status,
            "elapsed_s": t2_elapsed,
            "first_plan_core": t2_first_plan_text[:800],
            "final_core":      t2_final_text[:800],
            "first_plan": t2_first_assess,
            "final":      t2_final_assess,
            "planning_repairs": t2_repairs["planning_repairs"],
            "debug_repairs":    t2_repairs["debug_repairs"],
            "pytest_passed":    t2_pytest["passed"],
            "pytest_output":    t2_pytest.get("stdout", ""),
        },
        "workspace_leakage_after_t2": leakage_after_t2,
        "_summary": {
            "arm":            arm_key,
            "t1_done":        t1_status == "done",
            "wm_exists":      wm_path.exists(),
            "hg_persisted":   hg_count > 0,
            "hg_message_ok":  any("stdout" in m.lower() or "print" in m.lower()
                                  for m in hg_messages),
            "t2_done":        t2_status == "done",
            "t2_uses_print":  t2_final_assess["uses_print"],
            "t2_uses_logging": t2_final_assess["uses_logging"],
            "t2_is_silent":   t2_final_assess["is_silent"],
            "first_plan_uses_print":   t2_first_assess["uses_print"],
            "first_plan_uses_logging": t2_first_assess["uses_logging"],
        },
    }

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RAW_DIR / f"wm-hg-s2-stdout-{arm_key}-raw-{timestamp}.json"
    raw_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[raw] {raw_path}")

    print(f"\n=== {arm_label} ARM SUMMARY ===")
    print(f"T1 status:          {t1_status}")
    print(f"WM exists:          {wm_path.exists()}")
    print(f"HG persisted:       {hg_count > 0} ({hg_count} entries)")
    print(f"T2 status:          {t2_status}")
    print(f"T2 uses print():    {t2_final_assess['uses_print']}")
    print(f"T2 uses logging:    {t2_final_assess['uses_logging']}")
    print(f"T2 is silent:       {t2_final_assess['is_silent']}")

    return raw


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "both"
    if mode not in ("off", "on", "both"):
        print(f"Usage: {sys.argv[0]} [off|on|both]")
        sys.exit(1)

    results = {}

    if mode in ("off", "both"):
        results["off"] = run_arm(wm_on=False, slug=SLUG_OFF)

    if mode in ("on", "both"):
        results["on"] = run_arm(wm_on=True, slug=SLUG_ON)

    if len(results) == 2:
        off = results["off"]["_summary"]
        on  = results["on"]["_summary"]
        print("\n" + "="*60)
        print("DIFFERENTIAL SUMMARY")
        print("="*60)
        print(f"{'Metric':<35} {'OFF':>8} {'ON':>8}")
        print("-"*55)
        print(f"{'T1 done':<35} {str(off['t1_done']):>8} {str(on['t1_done']):>8}")
        print(f"{'WM exists after T1':<35} {str(off['wm_exists']):>8} {str(on['wm_exists']):>8}")
        print(f"{'HG persisted':<35} {str(off['hg_persisted']):>8} {str(on['hg_persisted']):>8}")
        print(f"{'T2 done':<35} {str(off['t2_done']):>8} {str(on['t2_done']):>8}")
        print(f"{'T2 first_plan uses print()':<35} {str(off['first_plan_uses_print']):>8} {str(on['first_plan_uses_print']):>8}")
        print(f"{'T2 first_plan uses logging':<35} {str(off['first_plan_uses_logging']):>8} {str(on['first_plan_uses_logging']):>8}")
        print(f"{'T2 final uses print()':<35} {str(off['t2_uses_print']):>8} {str(on['t2_uses_print']):>8}")
        print(f"{'T2 final uses logging':<35} {str(off['t2_uses_logging']):>8} {str(on['t2_uses_logging']):>8}")
        print(f"{'T2 final is silent':<35} {str(off['t2_is_silent']):>8} {str(on['t2_is_silent']):>8}")

        # Verdict
        print()
        off_ok = (off["t2_done"] and
                  (off["t2_uses_logging"] or off["t2_is_silent"]) and
                  not off["t2_uses_print"])
        on_ok  = (on["t2_done"] and
                  on["t2_uses_print"] and
                  not on["t2_uses_logging"])
        verdict = "PASS" if (off_ok and on_ok) else "FAIL"
        print(f"OFF arm PASS condition (logging or silent, no print): {off_ok}")
        print(f"ON  arm PASS condition (uses print, no logging):       {on_ok}")
        print(f"VERDICT: {verdict}")

    return results


if __name__ == "__main__":
    main()
