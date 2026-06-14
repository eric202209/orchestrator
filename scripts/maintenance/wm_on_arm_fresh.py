"""
Fresh ON arm only — project on-r4c.

Previous on-r4b (task 924) accumulated 9 persisted_planning_failures in session 725,
causing the circuit breaker to open on every attempt. This uses a new project/slug
to get a completely fresh session with 0 prior failures.

Usage:
  python3 wm_on_arm_fresh.py [--slug on-r4c]
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
RAW_DIR = REPO_ROOT / "docs/roadmap/reports/maintenance/project_aware_continuation_execution/working_memory"
REPORT_DIR = REPO_ROOT / "docs/roadmap/reports/maintenance"
SLOT_KEY = "orchestrator:backend_slots:local_openclaw"
PLANNING_CONTEXT_CAP = 400
DETERMINISTIC_PREFIX = "Task completed with verified execution evidence"

SLUG = sys.argv[2] if len(sys.argv) > 2 and sys.argv[1] == "--slug" else "wm-api-contract-parser-pilot-on-r4c"

HEADERS: dict = {}

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
    time.sleep(4)
    result2 = subprocess.run(["pgrep", "-f", "celery.*celery_app"],
                             capture_output=True, text=True)
    for pid in [int(p) for p in result2.stdout.strip().splitlines() if p.strip().isdigit()]:
        try:
            os.kill(pid, signal.SIGKILL)
        except ProcessLookupError:
            pass
    time.sleep(2)
    print("[worker] Stopped.")


def start_wm_on_worker() -> dict:
    env = {
        **os.environ,
        "ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY": "1",
        "WORKING_MEMORY_PERSISTENCE_ENABLED": "True",
        "WORKING_MEMORY_RENDER_ENABLED":       "True",
        "WORKING_MEMORY_INJECTION_ENABLED":    "True",
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
    ok = (ev.get("WORKING_MEMORY_PERSISTENCE_ENABLED") == "True" and
          ev.get("ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY") == "1")
    print(f"[worker] PID={pid} wm_on=True env_ok={ok}")
    if not ok:
        raise RuntimeError("Worker env mismatch")
    return {"pid": pid, "env_ok": ok,
            "WORKING_MEMORY_PERSISTENCE_ENABLED": ev.get("WORKING_MEMORY_PERSISTENCE_ENABLED"),
            "ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY": ev.get("ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY")}


def wait_slot(timeout: int = 600) -> None:
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


def dispatch(task_id: int) -> None:
    _api("POST", f"/api/v1/tasks/{task_id}/retry", json={})
    print(f"[dispatch] task {task_id}")


def poll(task_id: int, timeout: int = 1800, interval: int = 20) -> dict:
    deadline = time.time() + timeout
    elapsed = 0
    while time.time() < deadline:
        t = _api("GET", f"/api/v1/tasks/{task_id}")
        st = t.get("status", "")
        if st in ("done", "failed", "blocked_prior_task_failed"):
            print(f"  [{st}] at {elapsed}s")
            return t
        print(f"  [{st}] {elapsed}s", flush=True)
        time.sleep(interval)
        elapsed += interval
    raise TimeoutError(f"Task {task_id} timed out")


def read_safe(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception as e:
        return f"(could not read: {e})"


def check_impl(workspace: Path) -> dict:
    p = workspace / "src" / "calclib" / "parser.py"
    if not p.exists():
        return {"exists": False, "defines_parse_amount": False, "returns_dict": False, "text": ""}
    text = read_safe(p)
    return {
        "exists": True,
        "defines_parse_amount": "def parse_amount" in text,
        "returns_dict": ('{"ok"' in text or '"ok":' in text or "'ok':" in text),
        "text": text[:600],
    }


def run_pytest(workspace: Path) -> dict:
    try:
        r = subprocess.run(
            ["python3", "-m", "pytest", "tests/test_parser.py", "-q", "--tb=short"],
            capture_output=True, text=True, timeout=60,
            env={**os.environ, "PYTHONPATH": str(workspace / "src")},
            cwd=str(workspace),
        )
        return {"returncode": r.returncode, "stdout": r.stdout[-600:], "passed": r.returncode == 0}
    except Exception as e:
        return {"returncode": -1, "stdout": str(e), "passed": False}


def assess_api_8(text: str) -> dict:
    raw = text or ""
    tl = raw.lower()
    return {
        "parse_amount":    "parse_amount" in raw,
        "ok_key":          '"ok"' in raw or "'ok'" in raw or "ok:" in tl,
        "value_key":       '"value"' in raw or "'value'" in raw or "value:" in tl,
        "code_key":        '"code"' in raw or "'code'" in raw or "code:" in tl,
        "EMPTY_sentinel":  "EMPTY" in raw,
        "FORMAT_sentinel": "FORMAT" in raw,
        "OVERFLOW_sentinel": "OVERFLOW" in raw,
        "never_raises": ("never raise" in tl or "never raises" in tl or
                         "no exception" in tl or "doesn't raise" in tl),
    }


def compute_trim(workspace: Path) -> dict:
    try:
        import logging
        from app.services.orchestration.working_memory import _render_working_memory_content
        logger = logging.getLogger(__name__)
        rendered = _render_working_memory_content(str(workspace), logger)
        collapsed = " ".join((rendered or "").split())
        trimmed = (collapsed[:PLANNING_CONTEXT_CAP - 3].rstrip() + "..."
                   if len(collapsed) > PLANNING_CONTEXT_CAP else collapsed)

        def pos(t): return collapsed.find(t)
        api_pos  = pos("API Contract:")
        summ_pos = pos("Summary:")
        code_pos = pos('"code"') if pos('"code"') != -1 else pos("'code'")
        empty_pos, format_pos, overflow_pos = pos("EMPTY"), pos("FORMAT"), pos("OVERFLOW")
        failure_pos, success_pos = pos("failure return"), pos("success return")

        return {
            "wm_rendered": rendered, "wm_rendered_len": len(rendered or ""),
            "collapsed_len": len(collapsed), "trimmed_content": trimmed,
            "api_contract_pos": api_pos, "summary_pos": summ_pos,
            "api_before_summary": api_pos != -1 and (summ_pos == -1 or api_pos < summ_pos),
            "failure_return_pos": failure_pos, "success_return_pos": success_pos,
            "failure_before_success": failure_pos != -1 and success_pos != -1 and failure_pos < success_pos,
            "code_pos": code_pos,
            "code_in_250": code_pos != -1 and code_pos < 250,
            "code_in_400": code_pos != -1 and code_pos < PLANNING_CONTEXT_CAP,
            "EMPTY_pos": empty_pos, "FORMAT_pos": format_pos, "OVERFLOW_pos": overflow_pos,
            "EMPTY_in_400": empty_pos != -1 and empty_pos < PLANNING_CONTEXT_CAP,
            "FORMAT_in_400": format_pos != -1 and format_pos < PLANNING_CONTEXT_CAP,
            "OVERFLOW_in_400": overflow_pos != -1 and overflow_pos < PLANNING_CONTEXT_CAP,
            "all_sentinels_in_400": (
                empty_pos != -1 and empty_pos < PLANNING_CONTEXT_CAP and
                format_pos != -1 and format_pos < PLANNING_CONTEXT_CAP and
                overflow_pos != -1 and overflow_pos < PLANNING_CONTEXT_CAP
            ),
        }
    except Exception as e:
        return {"error": str(e), "code_in_250": False, "all_sentinels_in_400": False}


def assess_fields(text: str) -> dict:
    t = text or ""
    uses_code  = '["code"]'  in t or "['code']"  in t or '.get("code")'  in t
    uses_error = '["error"]' in t or "['error']" in t or '.get("error")' in t
    uses_ok    = '["ok"]'    in t or "['ok']"    in t or '.get("ok")'    in t
    uses_value = '["value"]' in t or "['value']" in t or '.get("value")' in t
    verbatim = '"EMPTY"' in t or '"FORMAT"' in t or '"OVERFLOW"' in t
    if uses_code and not uses_error:
        first = "code"
    elif uses_error and not uses_code:
        first = "error"
    elif uses_code and uses_error:
        first = "both"
    else:
        first = "neither"
    return {"uses_code": uses_code, "uses_error": uses_error, "uses_ok": uses_ok,
            "uses_value": uses_value, "first_field": first, "verbatim_codes": verbatim}


def extract_formatter_plan(task_id: int) -> str:
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


def main():
    workspace = WORKSPACE_BASE / SLUG
    wm_path = workspace / ".agent" / "working_memory.json"

    print(f"[on-arm-fresh] project slug: {SLUG}")
    print("[on-arm-fresh] Starting WM ON worker...")

    _kill_workers()
    worker_env = start_wm_on_worker()

    commit_sha = subprocess.check_output(
        ["git", "rev-parse", "--short", "HEAD"], cwd=REPO_ROOT, text=True
    ).strip()

    init_auth()
    wait_slot()

    proj = _api("POST", "/api/v1/projects", json={
        "name": SLUG,
        "description": f"WM API-contract pilot — ON arm ({SLUG})",
        "workspace_path": str(workspace),
    })
    project_id = proj["id"]
    print(f"[project] id={project_id} slug={SLUG}")

    t1 = _api("POST", "/api/v1/tasks", json={
        "project_id": project_id,
        "title": T1_TITLE,
        "description": T1_DESC,
        "plan_position": 1,
        "execution_profile": "full_lifecycle",
    })
    t1_id = t1["id"]
    print(f"[task] T1 id={t1_id}")

    print("[T1] Dispatching...")
    t1_start = time.time()
    dispatch(t1_id)
    t1_result = poll(t1_id)
    t1_elapsed = round(time.time() - t1_start, 1)
    t1_status = t1_result.get("status")
    print(f"[T1] {t1_status} in {t1_elapsed}s")

    wm_data = {}
    if wm_path.exists():
        try:
            wm_data = json.loads(wm_path.read_text(encoding="utf-8"))
        except Exception:
            pass
    strategies = wm_data.get("implementation_strategy") or []
    t1_summary = strategies[-1].get("summary", "") if strategies else ""
    t1_is_det = t1_summary.startswith(DETERMINISTIC_PREFIX)
    t1_api = assess_api_8(t1_summary)
    t1_api_score = sum(1 for v in t1_api.values() if v)
    t1_impl = check_impl(workspace)
    t1_pytest = run_pytest(workspace)

    from app.database import SessionLocal
    from app.models import Task as TaskModel
    db = SessionLocal()
    t1_db = db.query(TaskModel).filter(TaskModel.id == t1_id).first()
    t1_error = (t1_db.error_message or "") if t1_db else ""
    db.close()

    print(f"  API score:         {t1_api_score}/8")
    print(f"  defines_parse_amount: {t1_impl['defines_parse_amount']}")
    print(f"  returns_dict:      {t1_impl['returns_dict']}")
    print(f"  pytest:            {t1_pytest['passed']}")
    if t1_error:
        print(f"  error:             {t1_error[:120]}")

    trim = {}
    if wm_path.exists():
        trim = compute_trim(workspace)
        print(f"  code_pos:          {trim.get('code_pos')}")
        print(f"  code_in_250:       {trim.get('code_in_250')}")
        print(f"  all_sentinels_400: {trim.get('all_sentinels_in_400')}")
        print(f"  api_before_summ:   {trim.get('api_before_summary')}")
        print(f"  Trimmed: {trim.get('trimmed_content','')}")

    t2_id = -1
    t2_status = "skipped"
    t2_elapsed = 0
    t2_first_plan = ""
    t2_final_text = ""
    t2_first_fields = assess_fields("")
    t2_final_fields = assess_fields("")

    t1_valid = (t1_status == "done" and t1_impl["defines_parse_amount"]
                and t1_impl["returns_dict"])

    if not t1_valid:
        print(f"[T2] SKIP — T1 {'failed' if t1_status != 'done' else 'wrong API'}")
    else:
        wait_slot()
        t2 = _api("POST", "/api/v1/tasks", json={
            "project_id": project_id,
            "title": T2_TITLE,
            "description": T2_DESC,
            "plan_position": 2,
            "execution_profile": "full_lifecycle",
        })
        t2_id = t2["id"]
        print(f"[T2] Dispatching {t2_id}...")
        t2_start = time.time()
        dispatch(t2_id)
        t2_result = poll(t2_id)
        t2_elapsed = round(time.time() - t2_start, 1)
        t2_status = t2_result.get("status")
        print(f"[T2] {t2_status} in {t2_elapsed}s")

        formatter_py = workspace / "src" / "calclib" / "formatter.py"
        t2_final_text = read_safe(formatter_py) if formatter_py.exists() else ""
        t2_first_plan = extract_formatter_plan(t2_id)
        t2_first_fields = assess_fields(t2_first_plan)
        t2_final_fields = assess_fields(t2_final_text)

        print(f"  first_field:  {t2_first_fields['first_field']}")
        print(f"  uses_code:    {t2_first_fields['uses_code']}")
        print(f"  uses_error:   {t2_first_fields['uses_error']}")
        print(f"  verbatim:     {t2_first_fields['verbatim_codes']}")

    timestamp = time.strftime("%Y%m%d_%H%M%S")
    run_label = SLUG.split("-")[-1]  # e.g. "r4c"
    raw_path = RAW_DIR / f"wm-api-contract-clean-rerun-on-{run_label}-raw-{timestamp}.json"
    raw = {
        "arm": "on", "run_label": run_label, "timestamp": timestamp,
        "commit_sha": commit_sha, "slug": SLUG, "project_id": project_id,
        "worker_env": worker_env,
        "t1_task_id": t1_id, "t2_task_id": t2_id,
        "t1_status": t1_status, "t2_status": t2_status,
        "t1_elapsed_s": t1_elapsed, "t2_elapsed_s": t2_elapsed,
        "t1": {
            "status": t1_status, "elapsed_s": t1_elapsed, "error_message": t1_error,
            "wm_json_exists": wm_path.exists(),
            "llm_summary": t1_summary, "is_deterministic": t1_is_det,
            "api_capture": t1_api, "api_score": t1_api_score,
            "defines_parse_amount": t1_impl["defines_parse_amount"],
            "returns_dict": t1_impl["returns_dict"],
            "parser_text": t1_impl.get("text", ""),
            "pytest_passed": t1_pytest["passed"],
            "pytest_output": t1_pytest.get("stdout", ""),
        },
        "trim_analysis": trim,
        "t2": {
            "status": t2_status, "elapsed_s": t2_elapsed,
            "formatter_first_plan": t2_first_plan[:800],
            "formatter_final": t2_final_text[:800],
            "first_plan_fields": t2_first_fields, "final_fields": t2_final_fields,
        },
        "_summary": {
            "t1_done": t1_status == "done",
            "t1_api_score": t1_api_score,
            "t1_impl_ok": t1_impl["defines_parse_amount"],
            "t1_returns_dict": t1_impl["returns_dict"],
            "t2_done": t2_status == "done",
            "t2_first_field": t2_first_fields["first_field"],
            "t2_final_field": t2_final_fields["first_field"],
            "t2_verbatim_codes": t2_first_fields["verbatim_codes"],
            "code_in_250": trim.get("code_in_250"),
            "all_sentinels_in_400": trim.get("all_sentinels_in_400"),
            "api_before_summary": trim.get("api_before_summary"),
        },
    }
    RAW_DIR.mkdir(parents=True, exist_ok=True)
    raw_path.write_text(json.dumps(raw, indent=2, ensure_ascii=False), encoding="utf-8")
    print(f"\n[raw] {raw_path}")
    print(f"\n=== ON ARM ({SLUG}) SUMMARY ===")
    print(f"T1 status:       {t1_status}")
    print(f"T1 returns_dict: {t1_impl['returns_dict']}")
    print(f"T2 first_field:  {t2_first_fields['first_field']}")
    print(f"code_in_250:     {trim.get('code_in_250')}")
    print(f"sentinels_400:   {trim.get('all_sentinels_in_400')}")
    return raw


if __name__ == "__main__":
    main()
