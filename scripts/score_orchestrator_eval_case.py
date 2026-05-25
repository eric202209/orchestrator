#!/usr/bin/env python3
"""Score one completed orchestrator evaluation case.

This is intentionally not a benchmark runner. It consumes an existing project
workspace plus session/task IDs, parses the current event journal and state
snapshots, runs the case verifier command, and emits a JSON report.
"""

from __future__ import annotations

import argparse
from collections import Counter
from datetime import UTC, datetime
import json
from pathlib import Path
import subprocess
import sys
from typing import Any


EVENT_DIR = ".openclaw/events"


def _json_default(value: Any) -> str:
    return str(value)


def _load_json(path: Path) -> dict[str, Any]:
    try:
        payload = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        raise SystemExit(f"Manifest not found: {path}") from None
    except json.JSONDecodeError as exc:
        raise SystemExit(f"Manifest is not valid JSON: {path}: {exc}") from exc
    if not isinstance(payload, dict):
        raise SystemExit(f"Manifest root must be an object: {path}")
    return payload


def _select_case(manifest: dict[str, Any], case_id: str) -> dict[str, Any]:
    cases = manifest.get("cases") or []
    for case in cases:
        if isinstance(case, dict) and case.get("case_id") == case_id:
            return case
    raise SystemExit(f"Case {case_id!r} not found in manifest")


def _read_jsonl(path: Path) -> tuple[list[dict[str, Any]], list[dict[str, Any]]]:
    records: list[dict[str, Any]] = []
    malformed: list[dict[str, Any]] = []
    if not path.exists():
        return records, [{"line": None, "error": "file_not_found", "path": str(path)}]
    try:
        lines = path.read_text(encoding="utf-8").splitlines()
    except OSError as exc:
        return records, [{"line": None, "error": str(exc), "path": str(path)}]
    for line_number, line in enumerate(lines, 1):
        if not line.strip():
            continue
        try:
            item = json.loads(line)
        except json.JSONDecodeError as exc:
            malformed.append({"line": line_number, "error": str(exc)})
            continue
        if isinstance(item, dict):
            records.append(item)
        else:
            malformed.append({"line": line_number, "error": "record_not_object"})
    return records, malformed


def _event_path(project_dir: Path, session_id: int, task_id: int) -> Path:
    return project_dir / EVENT_DIR / f"session_{session_id}_task_{task_id}.jsonl"


def _state_snapshot_path(project_dir: Path, session_id: int, task_id: int) -> Path:
    return (
        project_dir
        / EVENT_DIR
        / f"session_{session_id}_task_{task_id}_state_snapshots.jsonl"
    )


def _as_list(value: Any) -> list[str]:
    if not isinstance(value, list):
        return []
    return [str(item) for item in value if str(item).strip()]


def _relative_path_exists(project_dir: Path, relative: str) -> bool:
    normalized = relative.strip().replace("\\", "/").lstrip("/")
    if not normalized:
        return False
    return (project_dir / normalized).exists()


def _file_checks(project_dir: Path, case: dict[str, Any]) -> dict[str, Any]:
    required = _as_list(case.get("required_files"))
    forbidden_existing = _as_list(case.get("forbidden_existing_files"))
    required_status = {
        path: _relative_path_exists(project_dir, path) for path in required
    }
    forbidden_status = {
        path: _relative_path_exists(project_dir, path) for path in forbidden_existing
    }
    return {
        "required_files": required_status,
        "missing_required_files": [
            path for path, exists in required_status.items() if not exists
        ],
        "forbidden_existing_files": forbidden_status,
        "present_forbidden_existing_files": [
            path for path, exists in forbidden_status.items() if exists
        ],
    }


def _collect_touched_files(
    events: list[dict[str, Any]], snapshots: list[dict[str, Any]]
) -> list[str]:
    touched: list[str] = []
    for snapshot in snapshots:
        for path in _as_list(snapshot.get("files_touched")):
            touched.append(path)
    for event in events:
        details = event.get("details") or {}
        if not isinstance(details, dict):
            continue
        candidate_keys = (
            "files_touched",
            "files_changed",
            "changed_files",
            "actual_files",
            "expected_artifacts",
            "missing_expected_files",
        )
        for key in candidate_keys:
            for path in _as_list(details.get(key)):
                touched.append(path)
    return sorted(dict.fromkeys(path for path in touched if path))


def _matches_prefix(path: str, prefixes: list[str]) -> bool:
    normalized = path.replace("\\", "/").lstrip("./")
    for prefix in prefixes:
        cleaned = prefix.replace("\\", "/").lstrip("./")
        if cleaned and normalized.startswith(cleaned):
            return True
    return False


def _touch_scope(touched_files: list[str], case: dict[str, Any]) -> dict[str, Any]:
    allowed_prefixes = _as_list(case.get("allowed_touched_prefixes"))
    expected_files = set(_as_list(case.get("expected_touched_files")))
    forbidden_prefixes = _as_list(case.get("forbidden_touched_prefixes"))
    forbidden_touched = [
        path for path in touched_files if _matches_prefix(path, forbidden_prefixes)
    ]
    unexpected_touched: list[str] = []
    if allowed_prefixes or expected_files:
        for path in touched_files:
            if path in expected_files:
                continue
            if _matches_prefix(path, allowed_prefixes):
                continue
            if path.startswith(".openclaw/"):
                continue
            unexpected_touched.append(path)
    return {
        "touched_files": touched_files,
        "expected_touched_files": sorted(expected_files),
        "allowed_touched_prefixes": allowed_prefixes,
        "forbidden_touched_prefixes": forbidden_prefixes,
        "forbidden_touched_files": sorted(dict.fromkeys(forbidden_touched)),
        "unexpected_touched_files": sorted(dict.fromkeys(unexpected_touched)),
    }


def _run_verifier(project_dir: Path, case: dict[str, Any]) -> dict[str, Any]:
    verifier = case.get("verifier") or {}
    if not isinstance(verifier, dict) or not verifier.get("command"):
        return {"available": False, "reason": "verifier_missing"}
    command = str(verifier["command"])
    timeout = int(verifier.get("timeout_seconds") or 60)
    started = datetime.now(UTC)
    try:
        completed = subprocess.run(
            command,
            cwd=project_dir,
            shell=True,
            text=True,
            capture_output=True,
            timeout=timeout,
            check=False,
        )
        finished = datetime.now(UTC)
        return {
            "available": True,
            "command": command,
            "timeout_seconds": timeout,
            "exit_code": completed.returncode,
            "passed": completed.returncode == 0,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "stdout_tail": completed.stdout[-4000:],
            "stderr_tail": completed.stderr[-4000:],
        }
    except subprocess.TimeoutExpired as exc:
        finished = datetime.now(UTC)
        return {
            "available": True,
            "command": command,
            "timeout_seconds": timeout,
            "exit_code": None,
            "passed": False,
            "timed_out": True,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "stdout_tail": (
                (exc.stdout or "")[-4000:] if isinstance(exc.stdout, str) else ""
            ),
            "stderr_tail": (
                (exc.stderr or "")[-4000:] if isinstance(exc.stderr, str) else ""
            ),
        }


def _event_summary(events: list[dict[str, Any]]) -> dict[str, Any]:
    counts = Counter(str(event.get("event_type") or "") for event in events)
    retry_count = counts.get("retry_entered", 0)
    repair_events = {
        name: counts.get(name, 0)
        for name in (
            "debug_feedback_captured",
            "debug_repair_attempted",
            "repair_generated",
            "repair_applied",
            "repair_rejected",
        )
    }
    checkpoint_events = {
        name: counts.get(name, 0)
        for name in (
            "checkpoint_saved",
            "checkpoint_loaded",
            "checkpoint_cursor_reconciled",
            "checkpoint_redirected",
            "resume_workspace_drift",
            "workspace_retry_dirty",
        )
    }
    health_scores = [
        (event.get("details") or {}).get("score")
        for event in events
        if event.get("event_type") == "health_score_updated"
    ]
    return {
        "event_count": len(events),
        "event_type_counts": dict(sorted(counts.items())),
        "retry_count": retry_count,
        "repair_events": repair_events,
        "checkpoint_events": checkpoint_events,
        "task_completed": counts.get("task_completed", 0) > 0,
        "task_failed": counts.get("task_failed", 0) > 0,
        "divergence_detected": counts.get("divergence_detected", 0) > 0,
        "intent_outcome_mismatch_count": counts.get("intent_outcome_mismatch", 0),
        "workspace_contract_failed_count": counts.get("workspace_contract_failed", 0),
        "health_score_min": min(health_scores) if health_scores else None,
        "health_score_final": health_scores[-1] if health_scores else None,
    }


def _snapshot_summary(snapshots: list[dict[str, Any]]) -> dict[str, Any]:
    prompt_sizes = [
        item.get("prompt_byte_estimate")
        for item in snapshots
        if isinstance(item.get("prompt_byte_estimate"), int)
    ]
    retry_budgets = [
        item.get("retry_budget_remaining")
        for item in snapshots
        if isinstance(item.get("retry_budget_remaining"), int)
    ]
    final = snapshots[-1] if snapshots else None
    return {
        "snapshot_count": len(snapshots),
        "state_snapshot_present": bool(snapshots),
        "prompt_byte_first": prompt_sizes[0] if prompt_sizes else None,
        "prompt_byte_max": max(prompt_sizes) if prompt_sizes else None,
        "prompt_byte_final": prompt_sizes[-1] if prompt_sizes else None,
        "retry_budget_min": min(retry_budgets) if retry_budgets else None,
        "retry_budget_final": retry_budgets[-1] if retry_budgets else None,
        "final_status": final.get("status") if final else None,
        "final_current_step_index": final.get("current_step_index") if final else None,
        "final_workspace_hash": final.get("workspace_hash") if final else None,
    }


def _required_event_results(
    case: dict[str, Any], counts: dict[str, int]
) -> dict[str, Any]:
    required_events = _as_list(case.get("required_events"))
    present = {event: counts.get(event, 0) > 0 for event in required_events}
    return {
        "required_events": present,
        "missing_required_events": [
            event for event, found in present.items() if not found
        ],
    }


def _derive_clean_success(
    *,
    case: dict[str, Any],
    verifier: dict[str, Any],
    files: dict[str, Any],
    scope: dict[str, Any],
    required_events: dict[str, Any],
    event_summary: dict[str, Any],
    snapshot_summary: dict[str, Any],
) -> tuple[bool, list[str]]:
    blockers: list[str] = []
    if not verifier.get("passed"):
        blockers.append("verifier_failed")
    if files["missing_required_files"]:
        blockers.append("required_files_missing")
    if files["present_forbidden_existing_files"]:
        blockers.append("forbidden_files_present")
    if scope["forbidden_touched_files"]:
        blockers.append("forbidden_files_touched")
    if required_events["missing_required_events"]:
        blockers.append("required_events_missing")
    if not event_summary["task_completed"]:
        blockers.append("task_completed_event_missing")
    if "state_snapshot_present" in _as_list(case.get("success_criteria")) and not (
        snapshot_summary["state_snapshot_present"]
    ):
        blockers.append("state_snapshot_missing")
    return not blockers, blockers


def _score_case(
    *,
    manifest_path: Path,
    manifest: dict[str, Any],
    case: dict[str, Any],
    project_dir: Path,
    session_id: int,
    task_id: int,
) -> dict[str, Any]:
    events_path = _event_path(project_dir, session_id, task_id)
    snapshots_path = _state_snapshot_path(project_dir, session_id, task_id)
    events, malformed_events = _read_jsonl(events_path)
    snapshots, malformed_snapshots = _read_jsonl(snapshots_path)
    event_summary = _event_summary(events)
    snapshot_summary = _snapshot_summary(snapshots)
    touched_files = _collect_touched_files(events, snapshots)
    scope = _touch_scope(touched_files, case)
    files = _file_checks(project_dir, case)
    verifier = _run_verifier(project_dir, case)
    required_events = _required_event_results(case, event_summary["event_type_counts"])
    clean_success, blockers = _derive_clean_success(
        case=case,
        verifier=verifier,
        files=files,
        scope=scope,
        required_events=required_events,
        event_summary=event_summary,
        snapshot_summary=snapshot_summary,
    )
    hallucination_signals = {
        "unexpected_touched_files": scope["unexpected_touched_files"],
        "intent_outcome_mismatch_count": event_summary["intent_outcome_mismatch_count"],
        "workspace_contract_failed_count": event_summary[
            "workspace_contract_failed_count"
        ],
        "repair_rejected_count": event_summary["repair_events"]["repair_rejected"],
    }
    return {
        "schema_version": 1,
        "generated_at": datetime.now(UTC).isoformat(),
        "tool": "scripts/score_orchestrator_eval_case.py",
        "manifest": {
            "path": str(manifest_path),
            "benchmark_id": manifest.get("benchmark_id"),
            "baseline_label": manifest.get("baseline_label"),
            "schema_version": manifest.get("schema_version"),
        },
        "case": {
            "case_id": case.get("case_id"),
            "category": case.get("category"),
            "purpose": case.get("purpose"),
        },
        "input": {
            "project_dir": str(project_dir),
            "session_id": session_id,
            "task_id": task_id,
            "event_journal_path": str(events_path),
            "state_snapshot_path": str(snapshots_path),
        },
        "result": {
            "clean_success": clean_success,
            "blockers": blockers,
            "verifier_passed": bool(verifier.get("passed")),
            "task_completed_event_present": event_summary["task_completed"],
            "task_failed_event_present": event_summary["task_failed"],
        },
        "verifier": verifier,
        "files": files,
        "touch_scope": scope,
        "required_events": required_events,
        "events": {
            **event_summary,
            "malformed": malformed_events,
        },
        "state_snapshots": {
            **snapshot_summary,
            "malformed": malformed_snapshots,
        },
        "metrics": {
            "retry_success": event_summary["retry_count"] > 0 and clean_success,
            "debug_repair_success": (
                event_summary["repair_events"]["debug_feedback_captured"] > 0
                and event_summary["repair_events"]["debug_repair_attempted"] > 0
                and clean_success
            ),
            "checkpoint_resume_success": (
                event_summary["checkpoint_events"]["checkpoint_loaded"] > 0
                and clean_success
            ),
            "repeated_failure_loop_signal": event_summary["divergence_detected"],
            "hallucination_signals": hallucination_signals,
            "prompt_byte_growth": {
                "first": snapshot_summary["prompt_byte_first"],
                "max": snapshot_summary["prompt_byte_max"],
                "final": snapshot_summary["prompt_byte_final"],
            },
        },
    }


def _write_report(path: Path, payload: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True, default=_json_default) + "\n",
        encoding="utf-8",
    )


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Score one existing orchestrator evaluation case."
    )
    parser.add_argument("--manifest", required=True, help="Benchmark manifest JSON")
    parser.add_argument("--case-id", required=True, help="Case id from manifest")
    parser.add_argument("--project-dir", required=True, help="Project workspace path")
    parser.add_argument("--session-id", type=int, required=True)
    parser.add_argument("--task-id", type=int, required=True)
    parser.add_argument(
        "--output",
        help="Optional report output path. If omitted, JSON is printed to stdout.",
    )
    args = parser.parse_args()

    manifest_path = Path(args.manifest)
    project_dir = Path(args.project_dir)
    if not project_dir.exists():
        raise SystemExit(f"Project directory not found: {project_dir}")
    manifest = _load_json(manifest_path)
    case = _select_case(manifest, args.case_id)
    report = _score_case(
        manifest_path=manifest_path,
        manifest=manifest,
        case=case,
        project_dir=project_dir,
        session_id=args.session_id,
        task_id=args.task_id,
    )
    if args.output:
        output_path = Path(args.output)
        _write_report(output_path, report)
        print(str(output_path))
    else:
        print(json.dumps(report, indent=2, sort_keys=True, default=_json_default))
    return 0 if report["result"]["clean_success"] else 1


if __name__ == "__main__":
    raise SystemExit(main())
