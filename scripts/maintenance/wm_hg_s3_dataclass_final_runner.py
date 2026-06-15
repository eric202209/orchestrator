#!/usr/bin/env python3
"""
WorkingMemory human_guidance Pilot — Scenario 3 FINAL RUN.

Post-fix run after:
  1. Operator Guidance render-priority fix (HG at char ~46, not 419)
  2. Bootstrap completion validator recovery-cascade fix (has_explicit_repair_intent
     now uses "" not task_prompt — prevents boilerplate "fix"/"bug" from blocking
     fresh_bootstrap_generated_test_evidence exemption)

Both prior NULL runs are now resolved. This run is expected to cleanly reach T2
in both arms and measure the dataclass convention differential.

Package: neutraltools (flat layout)
Slugs: wm-hg-s3-dataclass-off-final / wm-hg-s3-dataclass-on-final

Usage:
  cd /root/.openclaw/workspace/vault/projects/orchestrator
  source venv/bin/activate
  python3 scripts/maintenance/wm_hg_s3_dataclass_final_runner.py [off|on|both]
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

PKG = "neutraltools"

OPERATOR_GUIDANCE = (
    "Project convention: use dataclasses for all structured records. "
    "Never return plain dictionaries for record-like data."
)

SLUG_OFF = "wm-hg-s3-dataclass-off-final"
SLUG_ON  = "wm-hg-s3-dataclass-on-final"

HEADERS: dict = {}

T1_TITLE = "Create neutraltools package"
T1_DESC = f"""\
Create a Python utility package named {PKG}.

Package layout (flat — no src/ prefix, no pytest.ini):
  {PKG}/__init__.py
  {PKG}/core.py  — contains normalize_name(name: str) -> str
  tests/__init__.py
  tests/test_core.py  — 3 pytest tests for normalize_name

normalize_name(name: str) -> str:
  Strip leading/trailing whitespace from name and return in title case.
  normalize_name("  alice smith  ") == "Alice Smith"
  normalize_name("bob") == "Bob"
  normalize_name("  ") == ""

Verify with: python3 -m pytest tests/test_core.py -q\
"""

T2_TITLE = "Add create_record to neutraltools"
T2_DESC = f"""\
Add create_record(name: str, value: int) -> dict to {PKG}/core.py.

The function should return a plain dictionary: {{"name": name, "value": value}}.

Add tests that verify the returned dict contains the correct name and value.

Verify with:
  python3 -m pytest tests/ -q\
"""


# ---------------------------------------------------------------------------
# Preflight checks
# ---------------------------------------------------------------------------

def _preflight_verify_fix() -> None:
    """Assert the recovery-cascade fix is present in validator.py."""
    validator_path = REPO_ROOT / "app/services/orchestration/validation/validator.py"
    lines = validator_path.read_text(encoding="utf-8").splitlines()
    # Check line ~4413 passes "" not task_prompt to has_explicit_repair_intent
    found = False
    for i, line in enumerate(lines[4408:4420], start=4409):
        if 'has_explicit_repair_intent' in line and '"", title=' in lines[i]:
            found = True
            break
        # Lookahead: check line after has_explicit_repair_intent call
        if 'explicit_repair_intent = cls.has_explicit_repair_intent(' in line:
            next_line = lines[i] if i < len(lines) else ""
            if '"", title=' in next_line or '"",title=' in next_line:
                found = True
                break
    if not found:
        # Fallback: just grep for the pattern
        text = validator_path.read_text(encoding="utf-8")
        if 'has_explicit_repair_intent(\n            "", title=' in text:
            found = True
    if not found:
        raise RuntimeError(
            "Recovery-cascade fix NOT found in validator.py — "
            "has_explicit_repair_intent must receive \"\" as task_prompt. "
            "Run was aborted."
        )
    print("[preflight] validator.py recovery-cascade fix: CONFIRMED")


def _preflight_verify_commit() -> str:
    sha = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, text=True
    ).strip()
    print(f"[preflight] commit SHA: {sha}")
    return sha


# ---------------------------------------------------------------------------
# Infrastructure (identical to rerun runner, updated slugs/paths)
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
            env={**os.environ, "PYTHONPATH": str(workspace)},
            cwd=str(workspace),
        )
        return {"returncode": r.returncode, "stdout": r.stdout[-600:], "passed": r.returncode == 0}
    except Exception as e:
        return {"returncode": -1, "stdout": str(e), "passed": False}


def _assess_impl(text: str) -> dict:
    t = text or ""
    uses_dataclass = "@dataclass" in t or "from dataclasses import" in t
    returns_dict = 'return {' in t or '-> dict' in t
    uses_namedtuple = "namedtuple" in t or "NamedTuple" in t
    uses_typeddict = "TypedDict" in t
    uses_plain_class = ("class " in t and
                        not uses_dataclass and
                        not uses_namedtuple and
                        not uses_typeddict)
    return {
        "uses_dataclass":    uses_dataclass,
        "returns_dict":      returns_dict,
        "uses_namedtuple":   uses_namedtuple,
        "uses_typeddict":    uses_typeddict,
        "uses_plain_class":  uses_plain_class,
        "text_sample":       t[:400],
    }


def _extract_first_plan_content(task_id: int, filename_hint: str = "core.py") -> str:
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
    markers = ["@dataclass", "from dataclasses import", "never return plain",
               "use dataclasses", "plain dict"]
    results = {}
    for fname in [
        "progress_notes.md",
        f"{PKG}/core.py",
        "tests/test_core.py",
        ".agent/progress_notes.md",
    ]:
        fpath = workspace / fname
        if not fpath.exists():
            results[fname] = "not_found"
            continue
        text = _read_safe(fpath)
        found = [m for m in markers if m in text]
        results[fname] = found if found else "clean"
    return results


def _scan_tests_dir(workspace: Path) -> dict:
    tests_dir = workspace / "tests"
    if not tests_dir.exists():
        return {}
    result = {}
    for f in sorted(tests_dir.glob("test_*.py")):
        text = _read_safe(f)
        has_dc = "@dataclass" in text or "from dataclasses import" in text
        result[f.name] = "has_dataclass" if has_dc else "clean"
    return result


def _compute_hg_char_position(wm_rendered: str) -> dict:
    if not wm_rendered:
        return {"hg_char_position": -1, "hg_visible_in_250": False, "hg_visible_in_400": False}
    pos = wm_rendered.find("Project convention")
    if pos == -1:
        pos = wm_rendered.find("Operator Guidance")
    return {
        "hg_char_position": pos,
        "hg_visible_in_250": pos != -1 and pos < 250,
        "hg_visible_in_400": pos != -1 and pos < 400,
    }


def _check_old_regressions(task_id: int) -> dict:
    from app.database import SessionLocal
    from app.models import Task as TaskModel
    db = SessionLocal()
    try:
        t = db.query(TaskModel).filter(TaskModel.id == task_id).first()
        if not t:
            return {}
        error_msg = str(getattr(t, "error_message", "") or "")
        steps_raw = getattr(t, "steps", None)
        steps_str = json.dumps(steps_raw) if isinstance(steps_raw, list) else str(steps_raw or "")
        return {
            "pip_show": "pip show" in steps_str,
            "nested_project_folder_command": "nested_project_folder" in error_msg,
            "path_guard_advisory": "path_guard" in error_msg or "PathGuard" in error_msg,
            "backend_capacity": "backend_capacity" in error_msg or "capacity" in error_msg.lower(),
            "vma_error": "Cannot allocate memory" in error_msg or "VMA" in error_msg,
            "empty_response": "empty response" in error_msg.lower() or "EmptyResponse" in error_msg,
            "review_only_false_positive": "review_only" in error_msg,
            "bootstrap_cascade": (
                "repair task verification is insufficient" in error_msg.lower() or
                "newly generated without pre-existing" in error_msg.lower() or
                "fresh_bootstrap" in error_msg.lower()
            ),
        }
    except Exception as e:
        return {"error": str(e)}
    finally:
        db.close()


# ---------------------------------------------------------------------------
# Arm runner
# ---------------------------------------------------------------------------

def run_arm(wm_on: bool, slug: str, commit_sha: str) -> dict:
    workspace = WORKSPACE_BASE / slug
    arm_label = "ON" if wm_on else "OFF"
    print(f"\n{'='*60}")
    print(f"[arm:{arm_label}] slug={slug} wm_on={wm_on}")
    print(f"{'='*60}")

    _kill_workers()
    worker_env = _start_worker(wm_on)

    init_auth()
    _wait_slot()

    proj = _api("POST", "/api/v1/projects", json={
        "name": slug,
        "description": (
            f"WM HG S3 dataclass preference FINAL run "
            f"(post render-priority + recovery-cascade fix) — {arm_label} arm"
        ),
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

    print("[T1] Waiting for session to start...")
    session_id = _poll_until_session(t1_id, timeout=120)
    print(f"[T1] session_id={session_id} — injecting guidance")
    guidance_result = _inject_guidance(session_id, OPERATOR_GUIDANCE)

    t1_result = _poll(t1_id)
    t1_elapsed = round(time.time() - t1_start, 1)
    t1_status = t1_result.get("status")
    print(f"[T1] {t1_status} in {t1_elapsed}s")

    # Read WM after T1
    wm_path = workspace / ".agent" / "working_memory.json"
    wm_data = _read_wm(workspace)
    human_guidance_persisted = wm_data.get("human_guidance", [])
    hg_count = len(human_guidance_persisted)
    hg_messages = [g.get("message", "") if isinstance(g, dict) else str(g)
                   for g in human_guidance_persisted]

    leakage_after_t1 = _check_workspace_for_guidance_leak(workspace)

    core_py = workspace / PKG / "core.py"
    t1_core_text = _read_safe(core_py) if core_py.exists() else ""
    t1_has_normalize = "normalize_name" in t1_core_text
    t1_assess = _assess_impl(t1_core_text)
    t1_pytest = (_run_pytest(workspace, "tests/test_core.py")
                 if t1_has_normalize else {"passed": False, "stdout": ""})
    t1_repairs = _count_repairs(t1_id)
    t1_regressions = _check_old_regressions(t1_id)

    print(f"  has_normalize_name:   {t1_has_normalize}")
    print(f"  t1 uses_dataclass:    {t1_assess['uses_dataclass']}  (leakage risk)")
    print(f"  pytest:               {t1_pytest['passed']}")
    print(f"  wm_exists:            {wm_path.exists()}")
    print(f"  human_guidance count: {hg_count}")
    if hg_messages:
        print(f"  human_guidance[0]:    {hg_messages[0][:80]}")
    print(f"  workspace_leakage:    {leakage_after_t1}")
    if t1_regressions.get("bootstrap_cascade"):
        print(f"  [WARNING] bootstrap_cascade regression detected in T1!")

    # Render WM block (ON arm only)
    wm_rendered = ""
    hg_pos_info = {"hg_char_position": -1, "hg_visible_in_250": False, "hg_visible_in_400": False}
    if wm_path.exists() and wm_on:
        try:
            import logging as _logging
            from app.services.orchestration.working_memory import _render_working_memory_content
            _logger = _logging.getLogger(__name__)
            wm_rendered = _render_working_memory_content(str(workspace), _logger) or ""
            hg_pos_info = _compute_hg_char_position(wm_rendered)
            print(f"  wm_rendered ({len(wm_rendered)} chars): HG at char {hg_pos_info['hg_char_position']}")
            print(f"  HG visible in 250 chars: {hg_pos_info['hg_visible_in_250']}")
            print(f"  HG visible in 400 chars: {hg_pos_info['hg_visible_in_400']}")
        except Exception as e:
            wm_rendered = f"(render error: {e})"

    # --- T2 ---
    t2_id = -1
    t2_status = "skipped"
    t2_elapsed = 0.0
    t2_first_plan_text = ""
    t2_final_text = ""
    t2_first_assess = _assess_impl("")
    t2_final_assess = _assess_impl("")
    t2_repairs = {"planning_repairs": 0, "debug_repairs": 0}
    t2_pytest = {"passed": False, "stdout": ""}
    leakage_after_t2 = {}
    tests_after_t2 = {}
    t2_regressions = {}
    session_id_t2 = None

    if wm_on:
        t1_valid = t1_status == "done" and hg_count > 0
    else:
        t1_valid = t1_status == "done"

    if not t1_valid:
        if wm_on:
            reason = "T1 failed" if t1_status != "done" else "no WM guidance persisted"
            print(f"[T2] SKIP — {reason}")
        else:
            print("[T2] SKIP — T1 failed")
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

        try:
            session_id_t2 = _poll_until_session(t2_id, timeout=120)
        except TimeoutError:
            session_id_t2 = None

        t2_result = _poll(t2_id)
        t2_elapsed = round(time.time() - t2_start, 1)
        t2_status = t2_result.get("status")
        print(f"[T2] {t2_status} in {t2_elapsed}s")

        t2_final_text = _read_safe(core_py) if core_py.exists() else ""
        t2_first_plan_text = _extract_first_plan_content(t2_id, "core.py")
        t2_first_assess = _assess_impl(t2_first_plan_text)
        t2_final_assess = _assess_impl(t2_final_text)
        t2_repairs = _count_repairs(t2_id)
        t2_pytest = _run_pytest(workspace)
        leakage_after_t2 = _check_workspace_for_guidance_leak(workspace)
        tests_after_t2 = _scan_tests_dir(workspace)
        t2_regressions = _check_old_regressions(t2_id)

        print(f"  first_plan uses_dataclass: {t2_first_assess['uses_dataclass']}")
        print(f"  first_plan returns_dict:   {t2_first_assess['returns_dict']}")
        print(f"  final uses_dataclass:      {t2_final_assess['uses_dataclass']}")
        print(f"  final returns_dict:        {t2_final_assess['returns_dict']}")
        print(f"  final uses_namedtuple:     {t2_final_assess['uses_namedtuple']}")
        print(f"  final uses_typeddict:      {t2_final_assess['uses_typeddict']}")
        print(f"  final uses_plain_class:    {t2_final_assess['uses_plain_class']}")
        print(f"  planning_repairs:          {t2_repairs['planning_repairs']}")
        print(f"  debug_repairs:             {t2_repairs['debug_repairs']}")
        print(f"  pytest:                    {t2_pytest['passed']}")
        print(f"  test files:                {tests_after_t2}")
        if t2_regressions.get("bootstrap_cascade"):
            print(f"  [WARNING] bootstrap_cascade regression detected in T2!")

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    arm_key = "on" if wm_on else "off"
    raw = {
        "arm": arm_key,
        "slug": slug,
        "timestamp": timestamp,
        "commit_sha": commit_sha,
        "project_id": project_id,
        "session_id": session_id,
        "session_id_t2": session_id_t2,
        "worker_env": worker_env,
        "operator_guidance": OPERATOR_GUIDANCE,
        "guidance_inject_result": guidance_result,
        "t1": {
            "task_id": t1_id,
            "status": t1_status,
            "elapsed_s": t1_elapsed,
            "has_normalize_name": t1_has_normalize,
            "t1_uses_dataclass": t1_assess["uses_dataclass"],
            "core_text": t1_core_text[:600],
            "pytest_passed": t1_pytest["passed"],
            "pytest_output": t1_pytest.get("stdout", ""),
            "planning_repairs": t1_repairs["planning_repairs"],
            "debug_repairs":    t1_repairs["debug_repairs"],
            "regressions": t1_regressions,
        },
        "wm_after_t1": {
            "exists":                  wm_path.exists(),
            "human_guidance_count":    hg_count,
            "human_guidance_messages": hg_messages,
            "wm_rendered":             wm_rendered,
            "wm_rendered_len":         len(wm_rendered),
            "hg_char_position":        hg_pos_info["hg_char_position"],
            "hg_visible_in_250":       hg_pos_info["hg_visible_in_250"],
            "hg_visible_in_400":       hg_pos_info["hg_visible_in_400"],
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
            "tests_after_t2":   tests_after_t2,
            "regressions": t2_regressions,
        },
        "workspace_leakage_after_t2": leakage_after_t2,
        "_summary": {
            "arm":              arm_key,
            "t1_done":          t1_status == "done",
            "t1_has_normalize": t1_has_normalize,
            "t1_uses_dataclass": t1_assess["uses_dataclass"],
            "wm_exists":        wm_path.exists(),
            "hg_persisted":     hg_count > 0,
            "hg_message_ok":    any("dataclass" in m.lower() or "plain dict" in m.lower()
                                    for m in hg_messages),
            "hg_char_position":     hg_pos_info["hg_char_position"],
            "hg_visible_in_250":    hg_pos_info["hg_visible_in_250"],
            "hg_visible_in_400":    hg_pos_info["hg_visible_in_400"],
            "t2_done":          t2_status == "done",
            "t2_uses_dataclass":    t2_final_assess["uses_dataclass"],
            "t2_returns_dict":      t2_final_assess["returns_dict"],
            "t2_uses_namedtuple":   t2_final_assess["uses_namedtuple"],
            "t2_uses_typeddict":    t2_final_assess["uses_typeddict"],
            "t2_uses_plain_class":  t2_final_assess["uses_plain_class"],
            "first_plan_uses_dataclass": t2_first_assess["uses_dataclass"],
            "first_plan_returns_dict":   t2_first_assess["returns_dict"],
            "t2_planning_repairs":   t2_repairs["planning_repairs"],
            "t2_debug_repairs":      t2_repairs["debug_repairs"],
        },
    }

    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path = RAW_DIR / f"wm-hg-s3-dataclass-{arm_key}-final-raw-{timestamp}.json"
    raw_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[raw] {raw_path}")

    print(f"\n=== {arm_label} ARM SUMMARY ===")
    print(f"T1 status:           {t1_status}")
    print(f"T1 has normalize:    {t1_has_normalize}")
    print(f"T1 uses dataclass:   {t1_assess['uses_dataclass']}  (should be False)")
    print(f"WM exists:           {wm_path.exists()}")
    print(f"HG persisted:        {hg_count > 0} ({hg_count} entries)")
    if wm_on:
        print(f"HG char position:    {hg_pos_info['hg_char_position']}")
        print(f"HG visible in 250:   {hg_pos_info['hg_visible_in_250']}")
        print(f"HG visible in 400:   {hg_pos_info['hg_visible_in_400']}")
    print(f"T2 status:           {t2_status}")
    print(f"T2 uses dataclass:   {t2_final_assess['uses_dataclass']}")
    print(f"T2 returns dict:     {t2_final_assess['returns_dict']}")
    print(f"T2 uses namedtuple:  {t2_final_assess['uses_namedtuple']}")
    print(f"T2 uses TypedDict:   {t2_final_assess['uses_typeddict']}")
    print(f"T2 uses plain class: {t2_final_assess['uses_plain_class']}")

    return raw


# ---------------------------------------------------------------------------
# Report writer
# ---------------------------------------------------------------------------

def write_report(results: dict) -> Path:
    off = results.get("off", {})
    on  = results.get("on", {})
    off_s = off.get("_summary", {})
    on_s  = on.get("_summary", {})
    off_t2 = off.get("t2", {})
    on_t2  = on.get("t2", {})
    off_wm = off.get("wm_after_t1", {})
    on_wm  = on.get("wm_after_t1", {})
    on_t1  = on.get("t1", {})
    off_t1 = off.get("t1", {})

    commit = on.get("commit_sha") or off.get("commit_sha") or "unknown"
    timestamp = time.strftime("%Y-%m-%d %H:%M UTC")

    off_ok = (off_s.get("t2_returns_dict") and not off_s.get("t2_uses_dataclass")
              and not off_s.get("t2_uses_namedtuple") and not off_s.get("t2_uses_typeddict"))
    on_ok  = on_s.get("t2_uses_dataclass", False)
    t1_clean = (not off_s.get("t1_uses_dataclass") and not on_s.get("t1_uses_dataclass"))
    hg_visible = on_s.get("hg_visible_in_400", False)
    off_done = off_s.get("t2_done", False)
    on_done  = on_s.get("t2_done", False)

    strong_pass = (
        off_ok and on_ok and t1_clean and hg_visible and
        on_s.get("first_plan_uses_dataclass") and not on_s.get("first_plan_returns_dict") and
        off_s.get("first_plan_returns_dict") and not off_s.get("first_plan_uses_dataclass") and
        on_s.get("t2_planning_repairs", 0) == 0 and on_s.get("t2_debug_repairs", 0) == 0
    )

    if not off_done or not on_done:
        verdict = "NULL — T2 not dispatched in one or both arms"
    elif not t1_clean:
        verdict = "NULL — T1 leaked dataclass convention"
    elif not hg_visible:
        verdict = "NULL — HG not visible to T2 planner despite fix"
    elif off_ok and on_ok and strong_pass:
        verdict = "STRONG PASS"
    elif off_ok and on_ok:
        verdict = "PASS"
    elif not off_ok and not on_ok:
        verdict = "NULL — both arms same behavior"
    elif not on_ok:
        verdict = "FAIL — ON arm ignored WM guidance"
    else:
        verdict = f"PARTIAL — off_ok={off_ok} on_ok={on_ok} t1_clean={t1_clean}"

    on_wm_rendered = on_wm.get("wm_rendered", "")
    hg_char_pos = on_wm.get("hg_char_position", -1)
    off_core_final = off_t2.get("final_core", "(not collected)")
    on_core_final  = on_t2.get("final_core", "(not collected)")
    off_core_first = off_t2.get("first_plan_core", "(not collected)")
    on_core_first  = on_t2.get("first_plan_core", "(not collected)")

    def yn(b): return "Yes" if b else "No"

    lines = [
        f"# WorkingMemory — Human Guidance Scenario 3 Final: Dataclass Preference",
        "",
        f"**Date:** 2026-06-15  ",
        f"**Commit:** {commit}  ",
        f"**Status:** COMPLETE  ",
        f"**Verdict:** {verdict}",
        "",
        "---",
        "",
        "## 1. Executive Verdict",
        "",
        f"**{verdict}.**",
        "",
        "This is the final run of Scenario 3 after two fixes:",
        "",
        "1. **Operator Guidance render-priority fix** — HG now renders first in the WM block "
        f"(char {hg_char_pos} vs. char 419 before fix). Visible within 400-char planning context trim.",
        "2. **Bootstrap completion validator recovery-cascade fix** — `has_explicit_repair_intent` "
        "now uses only `title`+`description` (not the runtime-built prompt). Recovery boilerplate "
        "containing 'fix'/'bug' no longer blocks the `fresh_bootstrap_generated_test_evidence` "
        "exemption for clean bootstrap tasks.",
        "",
        "---",
        "",
        "## 2. Configuration",
        "",
        "### OFF Arm",
        "",
        "| Flag | Value |",
        "|---|---|",
        "| `WORKING_MEMORY_PERSISTENCE_ENABLED` | `False` |",
        "| `WORKING_MEMORY_RENDER_ENABLED` | `False` |",
        "| `WORKING_MEMORY_INJECTION_ENABLED` | `False` |",
        "| `ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY` | `1` |",
        "| `LANGFUSE_ENABLED` | `false` |",
        "| `REPO_MEMORY_INJECTION_ENABLED` | `False` |",
        "| `PSS_CONTINUATION_INJECTION_ENABLED` | `False` |",
        "| `ARTIFACT_CONTINUATION_ENABLED` | `False` |",
        "| `REDUCED_PLANNING_PROMPT_ENABLED` | `False` |",
        "",
        "### ON Arm",
        "",
        "| Flag | Value |",
        "|---|---|",
        "| `WORKING_MEMORY_PERSISTENCE_ENABLED` | `True` |",
        "| `WORKING_MEMORY_RENDER_ENABLED` | `True` |",
        "| `WORKING_MEMORY_INJECTION_ENABLED` | `True` |",
        "| All others | same as OFF |",
        "",
        "---",
        "",
        "## 3. OFF Arm Results",
        "",
        "### Setup",
        "",
        f"- **Package:** `{PKG}`, flat layout",
        f"- **Slug:** `{off.get('slug', SLUG_OFF)}`",
        f"- **Project ID:** {off.get('project_id', '—')}, Task IDs: T1={off_t1.get('task_id', '—')}, T2={off_t2.get('task_id', '—')}",
        f"- **Worker PID:** {off.get('worker_env', {}).get('pid', '—')}",
        f"- **Commit SHA:** {off.get('commit_sha', '—')}",
        "",
        "### T1 OFF Results",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Status | `{off_t1.get('status', '—')}` |",
        f"| Elapsed | {off_t1.get('elapsed_s', '—')}s |",
        f"| `normalize_name` present | {yn(off_t1.get('has_normalize_name'))} |",
        f"| T1 uses dataclass | {yn(off_t1.get('t1_uses_dataclass'))} |",
        f"| pytest | {yn(off_t1.get('pytest_passed'))} |",
        f"| WM exists | No (OFF arm — expected) |",
        f"| Planning repairs | {off_t1.get('planning_repairs', 0)} |",
        f"| Debug repairs | {off_t1.get('debug_repairs', 0)} |",
        f"| Bootstrap cascade | {yn(off_t1.get('regressions', {}).get('bootstrap_cascade'))} |",
        "",
    ]

    if off_t1.get("core_text"):
        lines += [
            "**T1 core.py (OFF arm):**",
            "```python",
            off_t1["core_text"].strip(),
            "```",
            "",
        ]

    lines += [
        "### T2 OFF Results",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Status | `{off_t2.get('status', 'skipped')}` |",
        f"| Elapsed | {off_t2.get('elapsed_s', '—')}s |",
        f"| First plan uses dataclass | {yn(off_s.get('first_plan_uses_dataclass'))} |",
        f"| First plan returns dict | {yn(off_s.get('first_plan_returns_dict'))} |",
        f"| Final uses dataclass | {yn(off_s.get('t2_uses_dataclass'))} |",
        f"| Final returns dict | {yn(off_s.get('t2_returns_dict'))} |",
        f"| Planning repairs | {off_t2.get('planning_repairs', 0)} |",
        f"| Debug repairs | {off_t2.get('debug_repairs', 0)} |",
        f"| pytest | {yn(off_t2.get('pytest_passed'))} |",
        f"| Bootstrap cascade | {yn(off_t2.get('regressions', {}).get('bootstrap_cascade'))} |",
        "",
    ]

    if off_core_final and off_core_final != "(not collected)":
        lines += [
            "**T2 final core.py (OFF arm):**",
            "```python",
            off_core_final.strip(),
            "```",
            "",
        ]

    lines += [
        "---",
        "",
        "## 4. ON Arm Results",
        "",
        "### Setup",
        "",
        f"- **Package:** `{PKG}`, flat layout",
        f"- **Slug:** `{on.get('slug', SLUG_ON)}`",
        f"- **Project ID:** {on.get('project_id', '—')}, Task IDs: T1={on_t1.get('task_id', '—')}, T2={on_t2.get('task_id', '—')}",
        f"- **Worker PID:** {on.get('worker_env', {}).get('pid', '—')}",
        f"- **Commit SHA:** {on.get('commit_sha', '—')}",
        "",
        "### T1 ON Results",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Status | `{on_t1.get('status', '—')}` |",
        f"| Elapsed | {on_t1.get('elapsed_s', '—')}s |",
        f"| `normalize_name` present | {yn(on_t1.get('has_normalize_name'))} |",
        f"| T1 uses dataclass | {yn(on_t1.get('t1_uses_dataclass'))} |",
        f"| pytest | {yn(on_t1.get('pytest_passed'))} |",
        f"| WM exists | {yn(on_wm.get('exists'))} |",
        f"| HG persisted | {yn(on_s.get('hg_persisted'))} ({on_wm.get('human_guidance_count', 0)} entries) |",
        f"| Planning repairs | {on_t1.get('planning_repairs', 0)} |",
        f"| Debug repairs | {on_t1.get('debug_repairs', 0)} |",
        f"| Bootstrap cascade | {yn(on_t1.get('regressions', {}).get('bootstrap_cascade'))} |",
        "",
    ]

    if on_t1.get("core_text"):
        lines += [
            "**T1 core.py (ON arm):**",
            "```python",
            on_t1["core_text"].strip(),
            "```",
            "",
        ]

    if on_wm_rendered:
        lines += [
            "### WM Rendered Block (after T1, before T2 planning)",
            "",
            "```",
            on_wm_rendered,
            "```",
            "",
            f"**Total rendered: {on_wm.get('wm_rendered_len', 0)} chars.**  ",
            f"Operator Guidance starts at **char {hg_char_pos}**.  ",
            f"Planning context budget: **400 chars** (`max_chars=800 // 2`).  ",
            f"HG visible in first 250 chars: **{yn(on_wm.get('hg_visible_in_250'))}**  ",
            f"HG visible in first 400 chars: **{yn(on_wm.get('hg_visible_in_400'))}**",
            "",
        ]

    leakage_t1 = on.get("workspace_leakage_after_t1", {})
    lines += [
        "### Workspace Leakage Check (ON arm after T1)",
        "",
        "| File | Result |",
        "|---|---|",
    ]
    for fname, result in leakage_t1.items():
        lines.append(f"| `{fname}` | {result} |")
    lines.append("")

    lines += [
        "### T2 ON Results",
        "",
        "| Metric | Value |",
        "|---|---|",
        f"| Status | `{on_t2.get('status', 'skipped')}` |",
        f"| Elapsed | {on_t2.get('elapsed_s', '—')}s |",
        f"| First plan uses dataclass | {yn(on_s.get('first_plan_uses_dataclass'))} |",
        f"| First plan returns dict | {yn(on_s.get('first_plan_returns_dict'))} |",
        f"| Final uses dataclass | {yn(on_s.get('t2_uses_dataclass'))} |",
        f"| Final returns dict | {yn(on_s.get('t2_returns_dict'))} |",
        f"| Planning repairs | {on_t2.get('planning_repairs', 0)} |",
        f"| Debug repairs | {on_t2.get('debug_repairs', 0)} |",
        f"| pytest | {yn(on_t2.get('pytest_passed'))} |",
        f"| Bootstrap cascade | {yn(on_t2.get('regressions', {}).get('bootstrap_cascade'))} |",
        "",
    ]

    if on_core_first and on_core_first != "(not collected)":
        lines += [
            "**T2 first plan core.py (ON arm):**",
            "```python",
            on_core_first.strip(),
            "```",
            "",
        ]

    if on_core_final and on_core_final != "(not collected)":
        lines += [
            "**T2 final core.py (ON arm):**",
            "```python",
            on_core_final.strip(),
            "```",
            "",
        ]

    lines += [
        "---",
        "",
        "## 5. Differential Summary",
        "",
        f"**{verdict}**",
        "",
        "| Metric | OFF arm | ON arm |",
        "|---|---|---|",
        f"| T1 done | {yn(off_s.get('t1_done'))} | {yn(on_s.get('t1_done'))} |",
        f"| T1 has normalize_name | {yn(off_s.get('t1_has_normalize'))} | {yn(on_s.get('t1_has_normalize'))} |",
        f"| T1 uses_dataclass (leak?) | {yn(off_s.get('t1_uses_dataclass'))} | {yn(on_s.get('t1_uses_dataclass'))} |",
        f"| WM exists after T1 | No | {yn(on_s.get('wm_exists'))} |",
        f"| HG persisted | No | {yn(on_s.get('hg_persisted'))} |",
        f"| HG char position | — | {hg_char_pos} |",
        f"| HG visible in 400 chars | — | {yn(on_s.get('hg_visible_in_400'))} |",
        f"| T2 done | {yn(off_done)} | {yn(on_done)} |",
        f"| T2 first plan uses dataclass | {yn(off_s.get('first_plan_uses_dataclass'))} | {yn(on_s.get('first_plan_uses_dataclass'))} |",
        f"| T2 first plan returns dict | {yn(off_s.get('first_plan_returns_dict'))} | {yn(on_s.get('first_plan_returns_dict'))} |",
        f"| T2 final uses dataclass | {yn(off_s.get('t2_uses_dataclass'))} | {yn(on_s.get('t2_uses_dataclass'))} |",
        f"| T2 final returns dict | {yn(off_s.get('t2_returns_dict'))} | {yn(on_s.get('t2_returns_dict'))} |",
        f"| T2 planning repairs | {off_s.get('t2_planning_repairs', 0)} | {on_s.get('t2_planning_repairs', 0)} |",
        f"| T2 debug repairs | {off_s.get('t2_debug_repairs', 0)} | {on_s.get('t2_debug_repairs', 0)} |",
        "",
        "---",
        "",
        "## 6. Old Regression Checks",
        "",
        "| Regression | OFF T1 | OFF T2 | ON T1 | ON T2 |",
        "|---|---|---|---|---|",
        f"| pip-show | {off_t1.get('regressions', {}).get('pip_show', False)} | {off_t2.get('regressions', {}).get('pip_show', False)} | {on_t1.get('regressions', {}).get('pip_show', False)} | {on_t2.get('regressions', {}).get('pip_show', False)} |",
        f"| nested_project_folder_command | {off_t1.get('regressions', {}).get('nested_project_folder_command', False)} | {off_t2.get('regressions', {}).get('nested_project_folder_command', False)} | {on_t1.get('regressions', {}).get('nested_project_folder_command', False)} | {on_t2.get('regressions', {}).get('nested_project_folder_command', False)} |",
        f"| path_guard_advisory | {off_t1.get('regressions', {}).get('path_guard_advisory', False)} | {off_t2.get('regressions', {}).get('path_guard_advisory', False)} | {on_t1.get('regressions', {}).get('path_guard_advisory', False)} | {on_t2.get('regressions', {}).get('path_guard_advisory', False)} |",
        f"| backend_capacity | {off_t1.get('regressions', {}).get('backend_capacity', False)} | {off_t2.get('regressions', {}).get('backend_capacity', False)} | {on_t1.get('regressions', {}).get('backend_capacity', False)} | {on_t2.get('regressions', {}).get('backend_capacity', False)} |",
        f"| VMA | {off_t1.get('regressions', {}).get('vma_error', False)} | {off_t2.get('regressions', {}).get('vma_error', False)} | {on_t1.get('regressions', {}).get('vma_error', False)} | {on_t2.get('regressions', {}).get('vma_error', False)} |",
        f"| empty_response | {off_t1.get('regressions', {}).get('empty_response', False)} | {off_t2.get('regressions', {}).get('empty_response', False)} | {on_t1.get('regressions', {}).get('empty_response', False)} | {on_t2.get('regressions', {}).get('empty_response', False)} |",
        f"| review_only_false_positive | {off_t1.get('regressions', {}).get('review_only_false_positive', False)} | {off_t2.get('regressions', {}).get('review_only_false_positive', False)} | {on_t1.get('regressions', {}).get('review_only_false_positive', False)} | {on_t2.get('regressions', {}).get('review_only_false_positive', False)} |",
        f"| bootstrap_cascade | {off_t1.get('regressions', {}).get('bootstrap_cascade', False)} | {off_t2.get('regressions', {}).get('bootstrap_cascade', False)} | {on_t1.get('regressions', {}).get('bootstrap_cascade', False)} | {on_t2.get('regressions', {}).get('bootstrap_cascade', False)} |",
        "",
        "---",
        "",
        "## 7. Verdict Rationale",
        "",
    ]

    if verdict.startswith("STRONG PASS"):
        lines += [
            f"**STRONG PASS:** OFF arm returned plain dict as directed. ON arm used `@dataclass` "
            f"in the first plan with 0 planning repairs and 0 debug repairs. "
            f"Guidance visible at char {hg_char_pos} (within 400-char budget). "
            f"T1 did not leak the convention into the workspace. "
            f"Behavioral difference cannot be explained by workspace readthrough.",
        ]
    elif verdict.startswith("PASS"):
        lines += [
            f"**PASS:** OFF arm returned plain dict. ON arm used `@dataclass` (possibly with repairs). "
            f"HG at char {hg_char_pos} — visible to T2 planner. T1 clean. "
            f"Guidance-driven behavioral difference confirmed.",
        ]
    elif verdict.startswith("NULL"):
        lines += [
            f"**{verdict}:** See differential table above for blocking condition.",
        ]
    elif verdict.startswith("FAIL"):
        lines += [
            f"**{verdict}:** ON arm saw the guidance (HG at char {hg_char_pos}) but still "
            f"returned a plain dict. This is a behavioral failure, not a transmission failure.",
        ]
    else:
        lines += [f"**{verdict}**"]

    lines += [
        "",
        "---",
        "",
        "## 8. Human Guidance Cross-Scenario Status",
        "",
        "| Scenario | Convention | Result |",
        "|---|---|---|",
        "| S1 | Mutable default avoidance | **PASS** |",
        "| S2 | Stdout-only convention | **PASS** |",
        "| S3 (original) | Dataclass preference | **NULL** (HG clipped at char 419) |",
        "| S3 (rerun) | Dataclass preference | **NULL** (bootstrap cascade — T1 failed) |",
        f"| S3 (final) | Dataclass preference | **{verdict}** |",
        "",
        "---",
        "",
        "## 9. Source Reports",
        "",
        "| Report | Location |",
        "|---|---|",
        "| S3 NULL (original) | `docs/roadmap/reports/maintenance/working-memory-human-guidance-scenario3-dataclass-preference-20260615.md` |",
        "| S3 NULL (rerun) | `docs/roadmap/reports/maintenance/working-memory-human-guidance-scenario3-dataclass-preference-rerun-20260615.md` |",
        "| Render-priority fix | `app/services/orchestration/working_memory.py` `_render_content()` |",
        "| Recovery-cascade fix | `docs/roadmap/reports/maintenance/bootstrap-completion-validator-recovery-cascade-fix-20260615.md` |",
        f"| Runner script | `scripts/maintenance/wm_hg_s3_dataclass_final_runner.py` |",
        f"| Commit SHA | `{commit}` |",
    ]

    report_text = "\n".join(lines) + "\n"
    report_path = (REPO_ROOT / "docs/roadmap/reports/maintenance"
                   / "working-memory-human-guidance-scenario3-dataclass-preference-final-20260615.md")
    report_path.write_text(report_text, encoding="utf-8")
    print(f"\n[report] {report_path}")
    return report_path


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------

def main():
    mode = sys.argv[1] if len(sys.argv) > 1 else "both"
    if mode not in ("off", "on", "both"):
        print(f"Usage: {sys.argv[0]} [off|on|both]")
        sys.exit(1)

    _preflight_verify_fix()
    commit_sha = _preflight_verify_commit()

    results = {}

    if mode in ("off", "both"):
        results["off"] = run_arm(wm_on=False, slug=SLUG_OFF, commit_sha=commit_sha)

    if mode in ("on", "both"):
        results["on"] = run_arm(wm_on=True, slug=SLUG_ON, commit_sha=commit_sha)

    if len(results) == 2:
        off = results["off"]["_summary"]
        on  = results["on"]["_summary"]
        print("\n" + "="*60)
        print("DIFFERENTIAL SUMMARY")
        print("="*60)
        print(f"{'Metric':<42} {'OFF':>8} {'ON':>8}")
        print("-"*62)
        print(f"{'T1 done':<42} {str(off['t1_done']):>8} {str(on['t1_done']):>8}")
        print(f"{'T1 has normalize_name':<42} {str(off['t1_has_normalize']):>8} {str(on['t1_has_normalize']):>8}")
        print(f"{'T1 uses_dataclass (leak?)':<42} {str(off['t1_uses_dataclass']):>8} {str(on['t1_uses_dataclass']):>8}")
        print(f"{'WM exists after T1':<42} {str(off['wm_exists']):>8} {str(on['wm_exists']):>8}")
        print(f"{'HG persisted':<42} {str(off['hg_persisted']):>8} {str(on['hg_persisted']):>8}")
        print(f"{'HG char position':<42} {'—':>8} {str(on.get('hg_char_position', '—')):>8}")
        print(f"{'HG visible in 400 chars':<42} {'—':>8} {str(on.get('hg_visible_in_400', '—')):>8}")
        print(f"{'T2 done':<42} {str(off['t2_done']):>8} {str(on['t2_done']):>8}")
        print(f"{'T2 first_plan uses_dataclass':<42} {str(off['first_plan_uses_dataclass']):>8} {str(on['first_plan_uses_dataclass']):>8}")
        print(f"{'T2 first_plan returns_dict':<42} {str(off['first_plan_returns_dict']):>8} {str(on['first_plan_returns_dict']):>8}")
        print(f"{'T2 final uses_dataclass':<42} {str(off['t2_uses_dataclass']):>8} {str(on['t2_uses_dataclass']):>8}")
        print(f"{'T2 final returns_dict':<42} {str(off['t2_returns_dict']):>8} {str(on['t2_returns_dict']):>8}")
        print(f"{'T2 final uses_namedtuple':<42} {str(off['t2_uses_namedtuple']):>8} {str(on['t2_uses_namedtuple']):>8}")
        print(f"{'T2 final uses_typeddict':<42} {str(off['t2_uses_typeddict']):>8} {str(on['t2_uses_typeddict']):>8}")
        print(f"{'T2 final uses_plain_class':<42} {str(off['t2_uses_plain_class']):>8} {str(on['t2_uses_plain_class']):>8}")

        print()
        off_uses_dict = (off["t2_returns_dict"] and not off["t2_uses_dataclass"]
                         and not off["t2_uses_namedtuple"] and not off["t2_uses_typeddict"])
        on_uses_struct = on["t2_uses_dataclass"]
        t1_clean = not off["t1_uses_dataclass"] and not on["t1_uses_dataclass"]
        hg_visible = on.get("hg_visible_in_400", False)
        off_ok = off_uses_dict
        on_ok = on_uses_struct

        on_repairs_raw = results["on"]["t2"]
        strong_pass = (
            off_ok and on_ok and t1_clean and hg_visible and
            on["first_plan_uses_dataclass"] and not on["first_plan_returns_dict"] and
            off["first_plan_returns_dict"] and not off["first_plan_uses_dataclass"] and
            on_repairs_raw["planning_repairs"] == 0 and on_repairs_raw["debug_repairs"] == 0
        )

        if not off["t2_done"] or not on["t2_done"]:
            verdict = "NULL — T2 not dispatched in one or both arms"
        elif not t1_clean:
            verdict = "NULL — T1 leaked dataclass convention"
        elif not hg_visible:
            verdict = "NULL — HG not visible to T2 planner despite fix"
        elif strong_pass:
            verdict = "STRONG PASS"
        elif off_ok and on_ok and t1_clean:
            verdict = "PASS"
        elif not off_ok and not on_ok:
            verdict = "NULL — both arms same behavior"
        elif not on_ok:
            verdict = "FAIL — ON arm ignored WM guidance"
        else:
            verdict = f"PARTIAL — off_ok={off_ok} on_ok={on_ok} t1_clean={t1_clean}"

        print(f"OFF uses plain dict (no dataclass):  {off_ok}")
        print(f"ON  uses dataclass:                  {on_ok}")
        print(f"T1 clean (no dataclass in either):   {t1_clean}")
        print(f"HG visible in 400 chars (ON):        {hg_visible}")
        print(f"VERDICT: {verdict}")

        report_path = write_report(results)
        print(f"\nReport: {report_path}")

    return results


if __name__ == "__main__":
    main()
