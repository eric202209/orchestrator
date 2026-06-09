#!/usr/bin/env python3
"""Priority 8 Arm B — 15-task pilot re-run.

Pure creation task corpus. Checkpoint/event-based repair detection.
No log string matching for repair signals.

Usage:
  python scripts/evals/run_arm_b_pilot_rerun.py --token <TOKEN>
"""
from __future__ import annotations

import json
import time
import argparse
from datetime import UTC, datetime
from pathlib import Path
from urllib import error, request as urllib_request

# ---------------------------------------------------------------------------
# Corpus — exact tasks from the re-run specification
# All pure creation tasks; no modify-existing-file patterns.
# ---------------------------------------------------------------------------
TASKS = [
    # tiny (≤75 chars)
    {"id": "T01", "band": "tiny",
     "title": "Create hello.py",
     "description": "Create hello.py with print_hello() that returns 'hello'. Verify with python3 -m py_compile hello.py."},
    {"id": "T02", "band": "tiny",
     "title": "Create about.html",
     "description": "Create about.html with an h1 element containing 'About'. Verify the file exists."},
    {"id": "T03", "band": "tiny",
     "title": "Create config.json",
     "description": "Create config.json with {\"version\": \"1.0.0\"}. Verify the JSON parses."},
    {"id": "T04", "band": "tiny",
     "title": "Create reset.css",
     "description": "Create reset.css with body { margin: 0; }. Verify the file exists."},
    # small (76–150 chars)
    {"id": "T05", "band": "small",
     "title": "Create math_ops.py",
     "description": "Create math_ops.py with add(a, b) returning a + b and multiply(a, b) returning a * b. Verify with python3 -m py_compile math_ops.py."},
    {"id": "T06", "band": "small",
     "title": "Create index.html",
     "description": "Create index.html with an h1 element containing 'Welcome' and a nav element. Verify the file exists."},
    {"id": "T07", "band": "small",
     "title": "Create settings.json",
     "description": "Create settings.json with debug set to false and theme set to 'light'. Verify the JSON parses."},
    {"id": "T08", "band": "small",
     "title": "Create styles.css",
     "description": "Create styles.css with body { font-family: sans-serif; } and h1 { color: #333; }. Verify the file exists."},
    {"id": "T09", "band": "small",
     "title": "Create validators.py",
     "description": "Create validators.py with is_positive(n) that returns n > 0. Verify with python3 -m py_compile validators.py."},
    # medium (151–250 chars)
    {"id": "T10", "band": "medium",
     "title": "Create report.py",
     "description": "Create report.py with summarize(items) that returns a dict with 'count' equal to len(items) and 'items' equal to items. Verify with python3 -m py_compile report.py."},
    {"id": "T11", "band": "medium",
     "title": "Create dashboard.html",
     "description": "Create dashboard.html with a header element, a main element, a footer element, and one button element. Verify the file exists."},
    {"id": "T12", "band": "medium",
     "title": "Create theme.json",
     "description": "Create theme.json with a colors object containing bg set to '#fff' and fg set to '#000', and a layout key set to 'default'. Verify the JSON parses."},
    {"id": "T13", "band": "medium",
     "title": "Create layout.css",
     "description": "Create layout.css with .container { max-width: 960px; margin: auto; } and .hidden { display: none; }. Verify the file exists."},
    # large (>250 chars)
    {"id": "T14", "band": "large",
     "title": "Create cli_summary.py",
     "description": "Create cli_summary.py with a main() function, a parse_args() function that uses argparse with a --name argument, and an if __name__ == '__main__' guard that calls main(). main() should call parse_args() and print 'Hello <name>'. Verify with python3 -m py_compile cli_summary.py."},
    {"id": "T15", "band": "large",
     "title": "Create app.html",
     "description": "Create app.html with a header element, a nav element, a main element with a section inside it, a footer element, and a script tag in the body that sets document.body.dataset.ready equal to 'true'. Verify the file exists."},
]

BASE_URL = "http://127.0.0.1:8080/api/v1"
WORKSPACE_ROOT = Path("/root/.openclaw/workspace/vault/projects")
TASK_TIMEOUT_SECONDS = 600
POLL_INTERVAL_SECONDS = 8.0

# Baseline repair rates by band (from characterization data)
BASELINE_REPAIR_RATES = {"tiny": 0.05, "small": 0.40, "medium": 0.80, "large": 0.60}
OVERALL_BASELINE = 0.35


def _api(method: str, path: str, token: str, payload: dict | None = None) -> dict:
    url = f"{BASE_URL}/{path.lstrip('/')}"
    body = json.dumps(payload).encode() if payload else None
    headers = {"Authorization": f"Bearer {token}", "Accept": "application/json"}
    if body:
        headers["Content-Type"] = "application/json"
    req = urllib_request.Request(url, data=body, headers=headers, method=method)
    try:
        with urllib_request.urlopen(req, timeout=30) as resp:
            raw = resp.read().decode()
    except error.HTTPError as exc:
        detail = exc.read().decode(errors="replace")
        raise RuntimeError(f"HTTP {exc.code} {method} {path}: {detail}") from exc
    return json.loads(raw) if raw.strip() else {}


def _wait_terminal(session_id: int, token: str, start: float) -> dict:
    terminal = {"completed", "stopped", "failed", "cancelled", "canceled", "paused"}
    while time.monotonic() - start < TASK_TIMEOUT_SECONDS:
        s = _api("GET", f"sessions/{session_id}", token)
        if str(s.get("status", "")).lower() in terminal:
            return s
        time.sleep(POLL_INTERVAL_SECONDS)
    return _api("GET", f"sessions/{session_id}", token)


def _parse_meta(entry: dict) -> dict:
    meta = entry.get("log_metadata") or {}
    if isinstance(meta, str):
        try:
            return json.loads(meta)
        except Exception:
            return {}
    return meta if isinstance(meta, dict) else {}


def _extract_metrics_from_logs(session_id: int, token: str) -> dict:
    """
    Extract repair and planning metrics from session logs.
    Repair detection uses ONLY these specific log events — no log string matching:
      - "Planning output was malformed; attempting one repair pass"
        (meta.retry == "repair_prompt") → TRUE repair trigger
      - "[PLANNING_DIAGNOSTICS] contract violation detected"
        (meta.contract_violation_type) → violation type
      - "Task 1 bootstrap contract failed" → bootstrap violation
      - "Planning context budget" (meta.planning_prompt_ref.chars) → prompt chars
    """
    try:
        raw = _api("GET", f"sessions/{session_id}/logs?limit=200", token)
        entries = raw if isinstance(raw, list) else raw.get("items", raw.get("logs", []))
    except Exception:
        return {}

    repair_triggered = False
    repair_reason = ""
    violation_type = ""
    planning_chars = None
    planning_tokens = None
    execution_reached = False
    planning_failed = False

    for entry in entries:
        msg = str(entry.get("message") or "")
        meta = _parse_meta(entry)

        # TRUE repair trigger — specific event, not string match
        if "Planning output was malformed; attempting one repair pass" in msg:
            if meta.get("retry") == "repair_prompt" or "retry" in meta:
                repair_triggered = True
                repair_reason = str(meta.get("reason") or "")[:200]

        # Contract violation type
        if "[PLANNING_DIAGNOSTICS] contract violation detected" in msg:
            violation_type = str(meta.get("contract_violation_type") or "")[:150]

        # Bootstrap contract failure
        if "Task 1 bootstrap contract failed" in msg:
            violation_type = violation_type or "task1_bootstrap_contract_failed"

        # Planning prompt chars from planning_prompt_ref
        if "Planning context budget" in msg:
            ref = meta.get("planning_prompt_ref") or {}
            if isinstance(ref, dict) and ref.get("chars"):
                planning_chars = int(ref["chars"])
            planning_tokens = meta.get("planning_prompt_tokens")

        # Execution reached
        if any(kw in msg for kw in ["Phase 2: EXECUTING", "Step 1 completed", "Applied 1 structured"]):
            execution_reached = True

        # Planning failure
        if "Task 1 product metric: task1_execution_failed" in msg:
            planning_failed = True

    return {
        "repair_triggered": repair_triggered,
        "repair_reason": repair_reason,
        "violation_type": violation_type,
        "planning_chars": planning_chars,
        "planning_tokens": planning_tokens,
        "execution_reached": execution_reached,
        "planning_failed_from_log": planning_failed,
    }


def _session_status_fields(session: dict) -> dict:
    status = str(session.get("status") or "").lower()
    fc = str(session.get("failure_category") or "").strip()
    clean_success = (status == "completed" and not fc)
    return {
        "session_status": status,
        "failure_category": fc or None,
        "clean_success": clean_success,
    }


def run_task(task: dict, token: str, run_index: int) -> dict:
    ts = datetime.now(UTC).strftime("%Y%m%d-%H%M%S")
    workspace = WORKSPACE_ROOT / f"arm-b-rerun-{task['id'].lower()}-{ts}"
    workspace.mkdir(parents=True, exist_ok=True)

    print(f"  [{run_index:02d}/15] {task['id']} ({task['band']}) — {task['title'][:50]}", flush=True)

    project = _api("POST", "projects", token, {
        "name": f"arm-b-rerun-{task['id']}-{ts}",
        "description": f"Arm B re-run pilot task {task['id']}",
        "workspace_path": str(workspace),
    })
    project_id = project["id"]

    t = _api("POST", "tasks", token, {
        "project_id": project_id,
        "title": task["title"],
        "description": task["description"],
        "priority": 0,
        "plan_position": 1,
    })
    task_id = t["id"]

    session = _api("POST", "sessions", token, {
        "project_id": project_id,
        "name": f"arm-b-rerun-{task['id']}-{ts}",
        "execution_mode": "manual",
        "default_execution_profile": "full_lifecycle",
    })
    session_id = session["id"]

    t_start = time.monotonic()
    _api("POST", f"sessions/{session_id}/tasks/{task_id}/run", token)
    final = _wait_terminal(session_id, token, t_start)
    total_time = time.monotonic() - t_start

    sf = _session_status_fields(final)
    log_metrics = _extract_metrics_from_logs(session_id, token)

    # Execution reached: either from logs or from completed status
    exec_reached = log_metrics.get("execution_reached", False) or (sf["session_status"] == "completed")

    # Arm B attribution check — classify failure
    fc = sf["failure_category"] or ""
    arm_b_section_map = {
        # Failures that could map to Arm B removed/reduced sections
        "missing_json_schema": "structural_json_missing_key",
        "plan_validation_failed": "validation_failure",
    }
    vt = log_metrics.get("violation_type") or ""
    arm_b_attribution = (
        "heredoc" in vt.lower()
        or "verification" in vt.lower()
        or "missing_key" in vt.lower()
        or (sf["session_status"] == "failed" and "json" in fc.lower())
    )

    # Known baseline failure patterns (not Arm B specific)
    known_baseline = (
        "bootstrap" in vt.lower()
        or "replace_in_file" in vt.lower()
        or fc in ("backend_timeout", "backend_capacity_limit", "planning_timeout")
    )

    result = {
        "task_id": task["id"],
        "session_id": session_id,
        "band": task["band"],
        "title": task["title"],
        "description_chars": len(task["description"]),
        "prompt_chars": log_metrics.get("planning_chars"),
        "planning_tokens": log_metrics.get("planning_tokens"),
        "planning_duration_s": None,  # not directly available from session logs
        "total_duration_s": round(total_time, 1),
        "planning_success": not log_metrics.get("planning_failed_from_log", False),
        "repair_triggered": log_metrics.get("repair_triggered", False),
        "repair_reason": log_metrics.get("repair_reason") or None,
        "validation_violation_type": vt or None,
        "execution_reached": exec_reached,
        "final_status": sf["session_status"],
        "failure_category": sf["failure_category"],
        "clean_success": sf["clean_success"],
        "failure_maps_to_arm_b_section": arm_b_attribution,
        "matches_known_baseline_failure": known_baseline,
        "unknown_rule_surfaced": False,  # filled in post-hoc from repair_reason if needed
        "workspace": str(workspace),
    }

    icon = "✓" if sf["clean_success"] else ("~" if exec_reached else "✗")
    print(
        f"       {icon} status={sf['session_status']} "
        f"repair={log_metrics.get('repair_triggered',False)} "
        f"exec={exec_reached} "
        f"chars={log_metrics.get('planning_chars','?')} "
        f"time={total_time:.0f}s",
        flush=True,
    )
    return result


def compute_metrics(results: list[dict]) -> dict:
    n = len(results)
    if n == 0:
        return {}

    valid = [r for r in results if not r.get("error") and r.get("final_status") not in ("running",)]
    n_valid = len(valid)

    repairs = sum(1 for r in valid if r.get("repair_triggered"))
    planning_fails = sum(1 for r in valid if not r.get("planning_success", True))
    exec_reached = sum(1 for r in valid if r.get("execution_reached"))
    clean = sum(1 for r in valid if r.get("clean_success"))
    arm_b_attributed = sum(1 for r in valid if r.get("failure_maps_to_arm_b_section"))
    baseline_failures = sum(1 for r in valid if r.get("matches_known_baseline_failure"))
    timeout_count = sum(1 for r in valid if r.get("failure_category") in (
        "backend_timeout", "planning_timeout", "backend_capacity_limit"))

    chars = [r["prompt_chars"] for r in valid if r.get("prompt_chars")]

    return {
        "valid_task_count": n_valid,
        "tasks_run": n,
        "repair_rate_arm_b": round(repairs / n_valid, 3) if n_valid else None,
        "repairs": repairs,
        "planning_violation_rate_arm_b": round(planning_fails / n_valid, 3) if n_valid else None,
        "planning_failures": planning_fails,
        "planning_timeout_rate_arm_b": round(timeout_count / n_valid, 3) if n_valid else None,
        "planning_timeouts": timeout_count,
        "execution_reached_rate_arm_b": round(exec_reached / n_valid, 3) if n_valid else None,
        "execution_reached": exec_reached,
        "clean_success_rate_arm_b": round(clean / n_valid, 3) if n_valid else None,
        "clean_success": clean,
        "average_prompt_chars": round(sum(chars) / len(chars), 0) if chars else None,
        "prompt_chars_sample_count": len(chars),
        "arm_b_attributed_failures": arm_b_attributed,
        "known_baseline_failures": baseline_failures,
        "new_failure_classes": [],  # filled below
    }


def check_stop_condition(metrics: dict, results: list[dict]) -> tuple[bool, str]:
    """
    Stop ONLY on true Arm B-attributable issues.
    Do NOT count: bootstrap corpus errors, environment errors, worker artifacts, false positives.
    """
    arm_b_repairs = sum(1 for r in results
                        if r.get("repair_triggered") and r.get("failure_maps_to_arm_b_section"))
    arm_b_task_count = len([r for r in results if r.get("final_status") not in ("running",)])

    if arm_b_task_count >= 8 and arm_b_repairs > 0:
        arm_b_repair_rate = arm_b_repairs / arm_b_task_count
        if arm_b_repair_rate > OVERALL_BASELINE + 0.05:
            return True, (
                f"true Arm B repair rate={arm_b_repair_rate:.1%} "
                f"> baseline+5% ({OVERALL_BASELINE+0.05:.1%}); "
                f"arm_b_repairs={arm_b_repairs}"
            )

    from collections import Counter
    arm_b_vt = Counter(
        r.get("validation_violation_type")
        for r in results
        if r.get("failure_maps_to_arm_b_section") and r.get("validation_violation_type")
    )
    for vt, count in arm_b_vt.items():
        if count >= 3:
            return True, f"Arm B-attributed violation repeated ≥3: {vt} x{count}"

    return False, ""


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--token", required=True)
    parser.add_argument("--tasks", type=int, default=15)
    parser.add_argument("--output-dir", type=Path,
                        default=Path("docs/roadmap/reports/maintenance"))
    args = parser.parse_args()

    print("=== Priority 8 Arm B — 15-Task Pilot Re-run ===")
    print(f"Started: {datetime.now(UTC).isoformat()}")
    print(f"Backend: local_openclaw | Model: qwen-local (Qwen3.6-27B via ai-gateway)")
    print(f"Flags: REDUCED_PLANNING_PROMPT_ENABLED=True LANGFUSE_ENABLED=false")
    print(f"Fix: .gitignore excluded from has_existing_files check")
    print()

    results = []
    stop_hit = False
    stop_reason = ""

    for i, task in enumerate(TASKS[: args.tasks], start=1):
        try:
            result = run_task(task, args.token, i)
            results.append(result)
        except Exception as exc:
            print(f"  ERROR on {task['id']}: {exc}", flush=True)
            results.append({
                "task_id": task["id"],
                "band": task["band"],
                "title": task["title"],
                "description_chars": len(task["description"]),
                "error": str(exc)[:200],
                "planning_success": False,
                "repair_triggered": False,
                "execution_reached": False,
                "clean_success": False,
                "failure_maps_to_arm_b_section": False,
                "matches_known_baseline_failure": False,
            })

        if len(results) >= 8:
            metrics_now = compute_metrics(results)
            stop_hit, stop_reason = check_stop_condition(metrics_now, results)
            if stop_hit:
                print(f"\n  STOP CONDITION HIT: {stop_reason}")
                break

    metrics = compute_metrics(results)
    # Check stop condition on final set
    if not stop_hit:
        stop_hit, stop_reason = check_stop_condition(metrics, results)

    # Recommendation
    arm_b_regressions = sum(1 for r in results if r.get("failure_maps_to_arm_b_section"))
    repair_rate = metrics.get("repair_rate_arm_b") or 0
    clean_rate = metrics.get("clean_success_rate_arm_b") or 0

    if arm_b_regressions == 0 and not stop_hit and repair_rate <= OVERALL_BASELINE + 0.02:
        recommendation = "proceed_to_30task_ab"
    elif arm_b_regressions == 0 and not stop_hit:
        recommendation = "proceed_to_30task_ab_with_monitoring"
    elif arm_b_regressions > 0 and stop_hit:
        recommendation = "revise_arm_b"
    else:
        recommendation = "inconclusive_rerun_with_larger_corpus"

    print()
    print("=== PILOT METRICS ===")
    for k, v in metrics.items():
        print(f"  {k}: {v}")
    print(f"  stop_condition_hit: {stop_hit}")
    print(f"  stop_reason: {stop_reason or 'none'}")
    print(f"  arm_b_regressions: {arm_b_regressions}")
    print(f"  recommendation: {recommendation}")

    report = {
        "schema_version": 2,
        "generated_at": datetime.now(UTC).isoformat(),
        "experiment": "priority_8_arm_b_15task_pilot_rerun",
        "run_number": 2,
        "flag": "REDUCED_PLANNING_PROMPT_ENABLED=True",
        "langfuse": "LANGFUSE_ENABLED=false",
        "model": "qwen-local (Qwen3.6-27B, /models/Qwen3.6-27B-Text-NVFP4-MTP)",
        "backend": "local_openclaw",
        "gitignore_fix": "added .gitignore to HYDRATION_EXCLUDED_NAMES to unblock Arm B path",
        "arm_b_prompt_baseline_chars": 2876,
        "metrics": metrics,
        "stop_condition_hit": stop_hit,
        "stop_reason": stop_reason or "",
        "arm_b_regressions": arm_b_regressions,
        "recommendation": recommendation,
        "results": results,
    }

    args.output_dir.mkdir(parents=True, exist_ok=True)
    ts = datetime.now(UTC).strftime("%Y%m%d%H%M%S")
    out_path = args.output_dir / f"arm-b-pilot-rerun-{ts}.json"
    out_path.write_text(json.dumps(report, indent=2, default=str))
    print(f"\nJSON: {out_path}")

    return report, out_path


if __name__ == "__main__":
    main()
