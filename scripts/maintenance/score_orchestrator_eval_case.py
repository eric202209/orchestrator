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
import os
from pathlib import Path
import shlex
import subprocess
import sys
from typing import Any


EVENT_DIR = ".agent/events"

_TIMEOUT_ENV_KEYS = (
    "PLANNING_REPAIR_TIMEOUT_SECONDS",
    "PLANNING_SYNTHESIS_TIMEOUT_SECONDS",
    "REPLAN_SYNTHESIS_TIMEOUT_SECONDS",
    "OLLAMA_PLANNING_TIMEOUT_SECONDS",
    "PLANNING_DIRECT_LOCAL_OPENCLAW_TIMEOUT_SECONDS",
    "ORCHESTRATOR_OPENCLAW_PLANNING_LOCK_ACQUIRE_TIMEOUT_SECONDS",
)
_ABORTED_STATUSES = frozenset({"paused", "stopped", "failed", "cancelled", "canceled"})


def _json_default(value: Any) -> str:
    return str(value)


def _is_session_aborted(session_status: str | None, failure_category: str | None) -> bool:
    """Return True when the API reports a definitively-failed terminal state."""
    return (
        str(session_status or "").lower() in _ABORTED_STATUSES
        and bool(str(failure_category or "").strip())
    )


def _scorer_env_summary(project_dir: Path) -> dict[str, Any]:
    """Collect environment metadata at scoring time for the report."""
    git_sha: str | None = None
    try:
        repo_root = Path(__file__).resolve().parents[1]
        result = subprocess.run(
            ["git", "rev-parse", "HEAD"],
            cwd=repo_root,
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if result.returncode == 0:
            git_sha = result.stdout.strip() or None
    except (OSError, subprocess.TimeoutExpired):
        pass

    worker_pid: int | None = None
    try:
        result = subprocess.run(
            ["pgrep", "-f", "celery"],
            capture_output=True,
            text=True,
            check=False,
            timeout=5,
        )
        if result.returncode == 0:
            pids = [p.strip() for p in result.stdout.splitlines() if p.strip()]
            worker_pid = int(pids[0]) if pids else None
    except (OSError, subprocess.TimeoutExpired, ValueError):
        pass

    timeout_env = {
        k: os.environ[k] for k in _TIMEOUT_ENV_KEYS if k in os.environ
    }
    backend = (
        os.environ.get("ORCHESTRATOR_AGENT_BACKEND")
        or os.environ.get("AGENT_BACKEND")
    )
    model = (
        os.environ.get("ORCHESTRATOR_AGENT_MODEL_FAMILY")
        or os.environ.get("AGENT_MODEL")
        or os.environ.get("OLLAMA_AGENT_MODEL")
    )
    runtime_profile = (
        os.environ.get("ORCHESTRATOR_RUNTIME_PROFILE")
        or os.environ.get("RUNTIME_PROFILE")
    )
    return {
        "git_sha": git_sha,
        "backend": backend,
        "model": model,
        "worker_pid": worker_pid,
        "workspace_root": str(project_dir.parent),
        "runtime_profile": runtime_profile,
        "timeout_env": timeout_env,
    }


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
            if path.startswith(".agent/"):
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


def _active_python_executable() -> str:
    """Return the Python executable used to run the scorer."""

    return sys.executable or "python3"


def _resolve_verifier_command(command: str, python_executable: str | None) -> str:
    """Replace a leading python/python3 token with the configured interpreter."""

    selected_python = (python_executable or _active_python_executable()).strip()
    if not selected_python:
        return command
    try:
        tokens = shlex.split(command, posix=True)
    except ValueError:
        return command
    if not tokens or tokens[0] not in {"python", "python3"}:
        return command
    tokens[0] = selected_python
    return shlex.join(tokens)


def _verifier_python_executable(
    *,
    command: str,
    original_command: str,
    python_executable: str | None,
) -> str | None:
    if command == original_command:
        return None
    return python_executable or _active_python_executable()


def _run_verifier(
    project_dir: Path,
    case: dict[str, Any],
    *,
    python_executable: str | None = None,
) -> dict[str, Any]:
    verifier = case.get("verifier") or {}
    if not isinstance(verifier, dict) or not verifier.get("command"):
        return {"available": False, "reason": "verifier_missing"}
    original_command = str(verifier["command"])
    command = _resolve_verifier_command(original_command, python_executable)
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
            "original_command": original_command,
            "python_executable": _verifier_python_executable(
                command=command,
                original_command=original_command,
                python_executable=python_executable,
            ),
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
            "original_command": original_command,
            "python_executable": _verifier_python_executable(
                command=command,
                original_command=original_command,
                python_executable=python_executable,
            ),
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


def _details_text(value: Any) -> str:
    try:
        return json.dumps(value, sort_keys=True, default=_json_default).lower()
    except TypeError:
        return str(value).lower()


def _event_details(event: dict[str, Any]) -> dict[str, Any]:
    details = event.get("details") or {}
    return details if isinstance(details, dict) else {}


def _phase_started(events: list[dict[str, Any]], phase: str) -> bool:
    for event in events:
        if event.get("event_type") != "phase_started":
            continue
        if str(_event_details(event).get("phase") or "").lower() == phase:
            return True
    return False


def _planning_validation_blocked(events: list[dict[str, Any]]) -> bool:
    blocked_statuses = {"failed", "failure", "repair_required", "rejected"}
    for event in events:
        details = _event_details(event)
        if details.get("cross_stage_convergence_class") == "root_cause_oscillation":
            return True
        if str(details.get("reason") or "") == "root_cause_oscillation_no_progress":
            return True
        if event.get("event_type") != "validation_result":
            continue
        if str(details.get("stage") or "").lower() != "plan":
            continue
        if str(details.get("status") or "").lower() in blocked_statuses:
            return True
    return False


def _planning_root_cause_from_details(details: dict[str, Any]) -> str | None:
    root_cause = str(details.get("planning_root_cause") or "").strip()
    if root_cause:
        return root_cause
    text = _details_text(details)
    if "python_source_syntax_invalid" in text or "invalid_python" in text:
        return "invalid_python"
    if "missing_source_materialization" in text:
        return "missing_source_materialization"
    if "missing verification" in text or "missing_verification" in text:
        return "missing_verification"
    if "stale_replace" in text or "patch_strategy_fallback_required" in text:
        return "stale_replace"
    if "framework_mismatch" in text or "undefined decorator" in text:
        return "framework_mismatch"
    if "repair_timeout" in text:
        return "repair_timeout"
    return None


def _planning_terminal_attribution(
    events: list[dict[str, Any]],
) -> dict[str, str | None]:
    terminal_state: str | None = None
    root_cause: str | None = None
    for event in events:
        details = _event_details(event)
        if str(details.get("phase") or "").lower() == "planning":
            status = str(details.get("status") or "").strip()
            if status:
                terminal_state = status
        reason = str(details.get("terminal_state") or details.get("reason") or "")
        if reason.startswith("planning_") or reason in {
            "repair_removed_materialization",
            "op_contract_violation",
            "workspace_isolation_violation",
            "reasoning_artifact_validation_failed",
        }:
            terminal_state = reason
        candidate_root_cause = _planning_root_cause_from_details(details)
        if candidate_root_cause:
            root_cause = candidate_root_cause
    if str(terminal_state or "").lower() in {"accepted", "completed", "success"}:
        root_cause = None
    if terminal_state == "planning_circuit_breaker_opened" and not root_cause:
        root_cause = "retry_exhausted"
    return {
        "terminal_state": terminal_state,
        "planning_root_cause": root_cause or "unknown",
    }


def _planning_issue_classes(events: list[dict[str, Any]]) -> list[str]:
    classes: list[str] = []
    for event in events:
        details = _event_details(event)
        if event.get("event_type") == "validation_result" and str(
            details.get("stage") or ""
        ).lower() != "plan":
            continue
        text = _details_text(details)
        root_cause = _planning_root_cause_from_details(details)
        if root_cause:
            classes.append(root_cause)
        if "source_api_regression" in text or "missing_required_symbols" in text:
            classes.append("source_api_regression")
        if "placeholder or stub" in text or "placeholder_only_steps" in text:
            classes.append("placeholder_stub")
    return list(dict.fromkeys(item for item in classes if item != "unknown"))


def _has_cross_stage_contract_regression(events: list[dict[str, Any]]) -> bool:
    for event in events:
        details = _event_details(event)
        if event.get("event_type") == "planning_repair_arbitration":
            text = _details_text(details)
            if "source_api_regression" in text or "test_rewrite" in text:
                return True
        if event.get("event_type") in {
            "debug_feedback_captured",
            "step_finished",
            "repair_rejected",
        }:
            text = _details_text(details)
            if "cannot import name" in text or "missing_required_symbols" in text:
                return True
    return False


def _cross_stage_convergence_class(
    *,
    events: list[dict[str, Any]],
    clean_success: bool,
    primary_failure_phase: str | None,
    execution_reached: bool,
    debug_repair_reached: bool,
    planning_terminal_state: str | None,
) -> str:
    if clean_success:
        return "clean_success"
    for event in events:
        details = _event_details(event)
        explicit_class = str(details.get("cross_stage_convergence_class") or "")
        if explicit_class == "root_cause_oscillation":
            return "root_cause_oscillation"
    planning_accepted = str(planning_terminal_state or "").lower() in {
        "accepted",
        "completed",
        "success",
    }
    if planning_accepted and debug_repair_reached:
        if _has_cross_stage_contract_regression(events):
            return "cross_stage_contract_regression"
        return "debug_repair_after_accepted_planning"
    planning_issues = _planning_issue_classes(events)
    if (
        primary_failure_phase == "planning_validation"
        and not execution_reached
        and len(planning_issues) > 1
    ):
        return "root_cause_oscillation"
    if primary_failure_phase == "planning_validation" and not execution_reached:
        return "planning_only_blocker"
    if execution_reached:
        return "stage_progression_without_success"
    return "unknown"


def _phase7f_used(events: list[dict[str, Any]]) -> bool:
    for event in events:
        details = _event_details(event)
        mode_architecture = str(
            details.get("debug_prompt_mode_architecture") or ""
        ).lower()
        mode = str(details.get("debug_prompt_mode") or "").lower()
        if mode_architecture:
            if "bounded_execution_debug_repair" in mode_architecture:
                return True
            continue
        if "phase7f" in mode or "bounded_debug_repair" in mode:
            return True
        text = _details_text(details)
        if "phase7f" in text or "bounded_execution_debug_repair" in text:
            return True
    return False


def _phase7g_used(events: list[dict[str, Any]]) -> bool:
    for event in events:
        details = _event_details(event)
        mode = str(details.get("debug_prompt_mode") or "").lower()
        mode_architecture = str(
            details.get("debug_prompt_mode_architecture") or ""
        ).lower()
        if mode_architecture:
            if "diff_scoped_debug_repair" in mode_architecture:
                return True
            continue
        if "phase7g" in mode:
            return True
        if isinstance(details.get("diff_capsule_line_count"), int) and (
            details["diff_capsule_line_count"] > 0
        ):
            return True
        if details.get("diff_capsule_primary_file"):
            return True
        text = _details_text(details)
        if "phase7g" in text:
            return True
        if "diff_scoped_debug_repair" in text:
            return True
    return False


def _case_intended_path_observed(
    *,
    case: dict[str, Any],
    existing_path_observed: bool,
    execution_reached: bool,
    debug_repair_reached: bool,
    repair_rejected_count: int,
    checkpoint_loaded: bool,
    clean_success: bool,
    files: dict[str, Any],
    scope: dict[str, Any],
    planning_root_cause: str | None,
) -> bool:
    category = str(case.get("category") or "").lower()
    case_id = str(case.get("case_id") or "").lower()
    required_events = set(_as_list(case.get("required_events")))
    success_criteria = set(_as_list(case.get("success_criteria")))
    expected = required_events | success_criteria

    if "repair_rejection" in category:
        return (
            clean_success
            or repair_rejected_count > 0
            or planning_root_cause == "stale_replace"
        )
    if "completion_validation" in category:
        return clean_success or (
            execution_reached and bool(files["missing_required_files"])
        )
    if "verification_artifact_guard" in category:
        return clean_success or (
            execution_reached
            and bool(
                files["present_forbidden_existing_files"]
                or scope["forbidden_touched_files"]
            )
        )
    if "checkpoint" in category or "checkpoint_loaded" in expected:
        return checkpoint_loaded
    if (
        "debug" in category
        or {
            "debug_feedback_captured",
            "debug_repair_attempted",
        }
        & expected
    ):
        return debug_repair_reached
    if case_id == "python_cli_small_feature" or category == "baseline_success":
        return execution_reached
    return existing_path_observed


def _primary_failure_phase(
    *,
    case: dict[str, Any],
    events: list[dict[str, Any]],
    verifier: dict[str, Any],
    clean_success: bool,
    planning_reached: bool,
    execution_reached: bool,
    debug_repair_reached: bool,
    checkpoint_loaded: bool,
    intended_path_observed: bool,
) -> str | None:
    if clean_success:
        return None
    category = str(case.get("category") or "").lower()
    if (
        "checkpoint" in category
        or "checkpoint_loaded" in _as_list(case.get("required_events"))
    ) and not checkpoint_loaded:
        return "checkpoint_resume"
    if _planning_validation_blocked(events) and not execution_reached:
        return "planning_validation"
    if debug_repair_reached:
        return "debug_repair"
    if execution_reached:
        return "execution"
    if planning_reached:
        return "planning"
    if not intended_path_observed:
        return "checkpoint_resume" if "checkpoint" in category else "planning"
    if verifier.get("available") and not verifier.get("passed"):
        return "verifier"
    return "unknown"


def _path_observability(
    *,
    case: dict[str, Any],
    events: list[dict[str, Any]],
    snapshots: list[dict[str, Any]],
    event_summary: dict[str, Any],
    verifier: dict[str, Any],
    clean_success: bool,
    required_events: dict[str, Any],
    files: dict[str, Any] | None = None,
    scope: dict[str, Any] | None = None,
) -> dict[str, Any]:
    files = files or {
        "missing_required_files": [],
        "present_forbidden_existing_files": [],
    }
    scope = scope or {"forbidden_touched_files": []}
    counts = event_summary["event_type_counts"]
    repair_events = event_summary["repair_events"]
    checkpoint_events = event_summary["checkpoint_events"]
    final_statuses = {
        str(snapshot.get("status") or "").lower() for snapshot in snapshots
    }

    planning_reached = (
        _phase_started(events, "planning")
        or _planning_validation_blocked(events)
        or "planning" in final_statuses
    )
    step_started_count = int(counts.get("step_started", 0))
    execution_reached = (
        _phase_started(events, "execution")
        or step_started_count > 0
        or counts.get("step_finished", 0) > 0
        or repair_events["debug_feedback_captured"] > 0
        or any(status in {"executing", "execution"} for status in final_statuses)
    )
    debug_repair_reached = any(value > 0 for value in repair_events.values())
    phase7f_used = _phase7f_used(events)
    phase7g_used = _phase7g_used(events)
    checkpoint_loaded = checkpoint_events["checkpoint_loaded"] > 0
    planning_attribution = _planning_terminal_attribution(events)
    intended_path_observed = _case_intended_path_observed(
        case=case,
        existing_path_observed=bool(required_events["path_observed"]),
        execution_reached=execution_reached,
        debug_repair_reached=debug_repair_reached,
        repair_rejected_count=repair_events["repair_rejected"],
        checkpoint_loaded=checkpoint_loaded,
        clean_success=clean_success,
        files=files,
        scope=scope,
        planning_root_cause=planning_attribution["planning_root_cause"],
    )
    primary_failure_phase = _primary_failure_phase(
        case=case,
        events=events,
        verifier=verifier,
        clean_success=clean_success,
        planning_reached=planning_reached,
        execution_reached=execution_reached,
        debug_repair_reached=debug_repair_reached,
        checkpoint_loaded=checkpoint_loaded,
        intended_path_observed=intended_path_observed,
    )
    cross_stage_class = _cross_stage_convergence_class(
        events=events,
        clean_success=clean_success,
        primary_failure_phase=primary_failure_phase,
        execution_reached=execution_reached,
        debug_repair_reached=debug_repair_reached,
        planning_terminal_state=planning_attribution["terminal_state"],
    )
    return {
        "planning_reached": planning_reached,
        "execution_reached": execution_reached,
        "step_started_count": step_started_count,
        "debug_repair_reached": debug_repair_reached,
        "phase7f_used": phase7f_used,
        "bounded_execution_debug_repair_used": phase7f_used,
        "phase7g_used": phase7g_used,
        "diff_scoped_debug_repair_used": phase7g_used,
        "repair_rejected_count": repair_events["repair_rejected"],
        "checkpoint_loaded": checkpoint_loaded,
        "intended_path_observed": intended_path_observed,
        "primary_failure_phase": primary_failure_phase,
        "planning_terminal_state": planning_attribution["terminal_state"],
        "planning_root_cause": planning_attribution["planning_root_cause"],
        "cross_stage_convergence_class": cross_stage_class,
    }


def _required_event_results(
    case: dict[str, Any], counts: dict[str, int]
) -> dict[str, Any]:
    required_events = _as_list(case.get("required_events"))
    present = {event: counts.get(event, 0) > 0 for event in required_events}
    missing = [event for event, found in present.items() if not found]
    return {
        "required_events": present,
        "missing_required_events": missing,
        "expected_signals": present,
        "missing_expected_signals": missing,
        "expected_signals_met": not missing,
        "path_observed": not missing,
    }


def _derive_clean_success(
    *,
    case: dict[str, Any],
    verifier: dict[str, Any],
    files: dict[str, Any],
    scope: dict[str, Any],
    event_summary: dict[str, Any],
    snapshot_summary: dict[str, Any],
    session_status: str | None = None,
    failure_category: str | None = None,
) -> tuple[bool, list[str]]:
    if _is_session_aborted(session_status, failure_category):
        category = str(failure_category or "").strip()
        return False, [f"session_aborted:{category}"]
    blockers: list[str] = []
    if not verifier.get("passed"):
        blockers.append("verifier_failed")
    if files["missing_required_files"]:
        blockers.append("required_files_missing")
    if files["present_forbidden_existing_files"]:
        blockers.append("forbidden_files_present")
    if scope["forbidden_touched_files"]:
        blockers.append("forbidden_files_touched")
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
    python_executable: str | None = None,
    session_status: str | None = None,
    failure_category: str | None = None,
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
    verifier = _run_verifier(
        project_dir,
        case,
        python_executable=python_executable,
    )
    required_events = _required_event_results(case, event_summary["event_type_counts"])
    session_aborted = _is_session_aborted(session_status, failure_category)
    clean_success, blockers = _derive_clean_success(
        case=case,
        verifier=verifier,
        files=files,
        scope=scope,
        event_summary=event_summary,
        snapshot_summary=snapshot_summary,
        session_status=session_status,
        failure_category=failure_category,
    )
    path_observability = _path_observability(
        case=case,
        events=events,
        snapshots=snapshots,
        event_summary=event_summary,
        verifier=verifier,
        clean_success=clean_success,
        required_events=required_events,
        files=files,
        scope=scope,
    )
    if session_aborted:
        path_observability = {
            **path_observability,
            "primary_failure_phase": "session_aborted",
        }
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
        "env_summary": _scorer_env_summary(project_dir),
        "result": {
            "clean_success": clean_success,
            "blockers": blockers,
            "verifier_passed": bool(verifier.get("passed")),
            "task_completed_event_present": event_summary["task_completed"],
            "task_failed_event_present": event_summary["task_failed"],
            "expected_signals_met": required_events["expected_signals_met"],
            "path_observed": required_events["path_observed"],
            "planning_terminal_state": path_observability[
                "planning_terminal_state"
            ],
            "planning_root_cause": path_observability["planning_root_cause"],
            "cross_stage_convergence_class": path_observability[
                "cross_stage_convergence_class"
            ],
            "session_status": session_status,
            "session_failure_category": failure_category,
            "session_aborted": session_aborted,
        },
        "path_observability": path_observability,
        "verifier": verifier,
        "files": files,
        "touch_scope": scope,
        "required_events": required_events,
        "expected_signals": {
            "signals": required_events["expected_signals"],
            "missing": required_events["missing_expected_signals"],
            "expected_signals_met": required_events["expected_signals_met"],
            "path_observed": required_events["path_observed"],
        },
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
        "--python",
        "--venv-python",
        dest="python_executable",
        help=(
            "Python executable for verifier commands that start with python/python3. "
            "Defaults to the interpreter running this scorer."
        ),
    )
    parser.add_argument(
        "--output",
        help="Optional report output path. If omitted, JSON is printed to stdout.",
    )
    parser.add_argument(
        "--session-status",
        dest="session_status",
        default=None,
        help="Terminal session status as reported by the API (e.g. paused, completed).",
    )
    parser.add_argument(
        "--failure-category",
        dest="failure_category",
        default=None,
        help="Session failure_category from the API (e.g. backend_timeout).",
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
        python_executable=args.python_executable,
        session_status=args.session_status,
        failure_category=args.failure_category,
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
