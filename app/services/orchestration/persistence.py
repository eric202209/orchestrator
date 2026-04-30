"""Checkpoint, logging, validation, and event persistence helpers for orchestration."""

from __future__ import annotations

import json
import hashlib
import os
import re
import time
import uuid
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional

from sqlalchemy.orm import Session

from app.models import LogEntry, Session as SessionModel, TaskCheckpoint
from app.services.workspace.checkpoint_service import CheckpointService
from app.services.prompt_templates import OrchestrationState, StepResult

from .events.event_types import EventType
from .policy import MAX_STEP_ATTEMPTS
from .types import FailureEnvelope, ValidationVerdict


def _orchestration_event_log_path(
    project_dir: Any, session_id: int, task_id: int
) -> Path:
    return (
        Path(project_dir)
        / ".openclaw"
        / "events"
        / f"session_{session_id}_task_{task_id}.jsonl"
    )


def _session_fingerprint_index_path(workspace_path: Any, session_id: int) -> Path:
    return (
        Path(workspace_path)
        / ".openclaw"
        / "fingerprints"
        / f"session_{session_id}.json"
    )


def write_session_fingerprint_index(
    workspace_path: Any,
    session_id: int,
    fingerprint: Dict[str, Any],
) -> None:
    path = _session_fingerprint_index_path(workspace_path, session_id)
    path.parent.mkdir(parents=True, exist_ok=True)
    payload = {**fingerprint, "indexed_at": datetime.now(UTC).isoformat()}
    try:
        _write_json_payload_atomic(path, payload)
    except OSError:
        pass


def read_session_fingerprint_index(
    workspace_path: Any,
    session_id: int,
    *,
    max_age_seconds: int = 300,
) -> Optional[Dict[str, Any]]:
    path = _session_fingerprint_index_path(workspace_path, session_id)
    if not path.exists():
        return None
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
        indexed_at_str = data.get("indexed_at")
        if indexed_at_str and max_age_seconds > 0:
            try:
                indexed_at = datetime.fromisoformat(indexed_at_str)
                if indexed_at.tzinfo is None:
                    indexed_at = indexed_at.replace(tzinfo=UTC)
                age = (datetime.now(UTC) - indexed_at).total_seconds()
                if age > max_age_seconds:
                    return None
            except ValueError:
                return None
        return data
    except (OSError, json.JSONDecodeError):
        return None


def _apply_counterfactual_overrides_to_checkpoint(
    checkpoint: Dict[str, Any],
    *,
    overrides: Dict[str, Any],
) -> tuple[Dict[str, Any], Dict[str, Any], Dict[str, Any]]:
    """Apply variable overrides to a checkpoint payload for counterfactual replay.

    Returns (modified_checkpoint, applied_overrides, deferred_overrides).
    applied_overrides: overrides woven directly into checkpoint state.
    deferred_overrides: overrides stored in context.replay_overrides for the executor.
    """
    import copy

    modified = copy.deepcopy(checkpoint)
    applied: Dict[str, Any] = {}
    deferred: Dict[str, Any] = {}

    orchestration_state = modified.get("orchestration_state") or {}

    step_from_index = overrides.get("step_from_index")
    if step_from_index is not None:
        plan = orchestration_state.get("plan") or []
        clamped = max(0, min(int(step_from_index), max(0, len(plan) - 1)))
        orchestration_state["current_step_index"] = clamped
        orchestration_state["debug_attempts"] = []
        orchestration_state["execution_results"] = [
            r
            for r in (orchestration_state.get("execution_results") or [])
            if (int(r.get("step_number") or 1) - 1) < clamped
        ]
        modified["orchestration_state"] = orchestration_state
        modified["current_step_index"] = clamped
        modified["step_results"] = [
            r
            for r in (modified.get("step_results") or [])
            if (int(r.get("step_number") or 1) - 1) < clamped
        ]
        applied["step_from_index"] = clamped

    replay_overrides: Dict[str, Any] = {}
    for key in ("policy_profile", "model_family", "adaptation_profile"):
        val = overrides.get(key)
        if val is not None:
            replay_overrides[key] = val
            deferred[key] = val
    if replay_overrides:
        context = dict(modified.get("context") or {})
        context["replay_overrides"] = replay_overrides
        modified["context"] = context

    return modified, applied, deferred


def _orchestration_state_snapshot_log_path(
    project_dir: Any, session_id: int, task_id: int
) -> Path:
    return (
        Path(project_dir)
        / ".openclaw"
        / "events"
        / f"session_{session_id}_task_{task_id}_state_snapshots.jsonl"
    )


def _safe_jsonl_length(log_path: Path) -> int:
    if not log_path.exists():
        return 0
    try:
        with log_path.open("r", encoding="utf-8") as handle:
            return sum(1 for _ in handle)
    except OSError:
        return 0


_WORKSPACE_HASH_CACHE: Dict[str, tuple[float, Optional[str]]] = {}
_WORKSPACE_HASH_CACHE_TTL_SECONDS = 1.0


def _write_json_payload_atomic(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with tempfile.NamedTemporaryFile(
        "w",
        encoding="utf-8",
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    ) as handle:
        temp_path = Path(handle.name)
        handle.write(json.dumps(payload, default=str))
        handle.flush()
        os.fsync(handle.fileno())
    temp_path.replace(path)


def _append_jsonl_line(log_path: Path, payload: Dict[str, Any]) -> None:
    import fcntl

    log_path.parent.mkdir(parents=True, exist_ok=True)
    lock_path = log_path.with_suffix(f"{log_path.suffix}.lock")
    with lock_path.open("a+", encoding="utf-8") as lock_handle:
        fcntl.flock(lock_handle.fileno(), fcntl.LOCK_EX)
        try:
            with log_path.open("a", encoding="utf-8") as handle:
                handle.write(json.dumps(payload, default=str) + "\n")
                handle.flush()
                os.fsync(handle.fileno())
        finally:
            fcntl.flock(lock_handle.fileno(), fcntl.LOCK_UN)


def _compute_workspace_hash(project_dir: Any) -> Optional[str]:
    path = Path(project_dir)
    if not path.exists() or not path.is_dir():
        return None
    cache_key = str(path.resolve())
    now = time.monotonic()
    cached = _WORKSPACE_HASH_CACHE.get(cache_key)
    if cached and (now - cached[0]) < _WORKSPACE_HASH_CACHE_TTL_SECONDS:
        return cached[1]

    digest = hashlib.sha256()
    try:
        for file_path in sorted(
            item
            for item in path.rglob("*")
            if item.is_file() and ".openclaw" not in item.parts
        ):
            rel_path = file_path.relative_to(path)
            stat = file_path.stat()
            digest.update(str(rel_path).encode("utf-8", errors="ignore"))
            digest.update(str(stat.st_size).encode("utf-8"))
            digest.update(str(int(stat.st_mtime_ns)).encode("utf-8"))
    except OSError:
        return None
    result = digest.hexdigest()
    _WORKSPACE_HASH_CACHE[cache_key] = (now, result)
    return result


def _build_health_inputs(events: List[Dict[str, Any]]) -> Dict[str, Any]:
    non_health_events = [
        event
        for event in events
        if event.get("event_type") != EventType.HEALTH_SCORE_UPDATED
    ]
    recent = non_health_events[-25:]
    tool_failures = sum(
        1 for event in recent if event.get("event_type") == EventType.TOOL_FAILED
    )
    retries = sum(
        1 for event in recent if event.get("event_type") == EventType.RETRY_ENTERED
    )
    repairs = sum(
        1
        for event in recent
        if event.get("event_type")
        in {
            EventType.REPAIR_GENERATED,
            EventType.REPAIR_APPLIED,
            EventType.REPAIR_REJECTED,
        }
    )
    validation_warnings = 0
    validation_failures = 0
    same_tool_streak = 0
    current_same_tool_streak = 1
    last_tool_name: Optional[str] = None
    for event in recent:
        if event.get("event_type") == EventType.VALIDATION_RESULT:
            status = ((event.get("details") or {}).get("status") or "").lower()
            if status == "warning":
                validation_warnings += 1
            elif status in {"rejected", "repair_required"}:
                validation_failures += 1

        if event.get("event_type") != EventType.TOOL_INVOKED:
            continue
        tool_name = str((event.get("details") or {}).get("tool_name") or "").strip()
        if not tool_name:
            continue
        if tool_name == last_tool_name:
            current_same_tool_streak += 1
        else:
            current_same_tool_streak = 1
            last_tool_name = tool_name
        same_tool_streak = max(same_tool_streak, current_same_tool_streak)

    score = 100
    score -= tool_failures * 18
    score -= retries * 10
    score -= repairs * 8
    score -= validation_warnings * 6
    score -= validation_failures * 12
    score -= max(0, same_tool_streak - 2) * 5

    return {
        "score": max(0, min(100, score)),
        "inputs": {
            "tool_failures": tool_failures,
            "retries": retries,
            "repairs": repairs,
            "validation_warnings": validation_warnings,
            "validation_failures": validation_failures,
            "same_tool_streak": same_tool_streak,
            "window_event_count": len(recent),
        },
    }


def _extract_declared_intent(step_description: str) -> str:
    text = str(step_description or "").strip()
    if not text:
        return "unknown"
    lowered = text.lower()
    for marker in ("create ", "update ", "edit ", "fix ", "verify ", "test "):
        if marker in lowered:
            start = lowered.find(marker)
            return text[start : start + 120].strip()
    return text[:120]


def _normalize_text_tokens(values: List[str]) -> set[str]:
    tokens: set[str] = set()
    for value in values:
        for token in re.findall(r"[a-z0-9_./-]+", str(value or "").lower()):
            if len(token) >= 3:
                tokens.add(token)
    return tokens


def _append_health_score_update(
    *,
    project_dir: Any,
    session_id: int,
    task_id: int,
) -> Optional[Dict[str, Any]]:
    events = read_orchestration_events(project_dir, session_id, task_id)
    if not events:
        return None

    health_events = [
        event
        for event in events
        if event.get("event_type") == EventType.HEALTH_SCORE_UPDATED
    ]
    latest_health = health_events[-1] if health_events else None
    health_inputs = _build_health_inputs(events)
    score = int(health_inputs["score"])
    previous_score = None
    if latest_health:
        previous_score = (latest_health.get("details") or {}).get("score")
    if previous_score == score:
        return latest_health

    slope = None
    if isinstance(previous_score, int):
        slope = score - previous_score

    return append_orchestration_event(
        project_dir=project_dir,
        session_id=session_id,
        task_id=task_id,
        event_type=EventType.HEALTH_SCORE_UPDATED,
        details={
            "score": score,
            "slope": slope,
            "previous_score": previous_score,
            **health_inputs["inputs"],
        },
    )


def emit_intent_outcome_mismatch(
    *,
    project_dir: Any,
    session_id: int,
    task_id: int,
    step_index: int,
    step_description: str,
    expected_files: List[str],
    actual_files: List[str],
    actual_tool_calls: List[str],
    parent_event_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    expected_files = list(dict.fromkeys(expected_files or []))
    actual_files = list(dict.fromkeys(actual_files or []))
    actual_tool_calls = list(dict.fromkeys(actual_tool_calls or []))
    expected_tokens = _normalize_text_tokens(
        expected_files + [step_description, _extract_declared_intent(step_description)]
    )
    actual_tokens = _normalize_text_tokens(actual_files + actual_tool_calls)
    missing_expected_files = sorted(set(expected_files) - set(actual_files))
    overlap = len(expected_tokens & actual_tokens)
    expected_signal = max(1, len(expected_files) + min(5, len(expected_tokens)))
    mismatch_score = max(
        0,
        min(
            100,
            int(
                (len(missing_expected_files) * 25)
                + (30 if actual_tool_calls and overlap == 0 else 0)
                + max(0, 20 - overlap * 4)
            ),
        ),
    )
    if mismatch_score < 40:
        return None

    last_mismatch = find_latest_orchestration_event(
        project_dir,
        session_id,
        task_id,
        event_types={EventType.INTENT_OUTCOME_MISMATCH},
    )
    last_details = (last_mismatch or {}).get("details") or {}
    if (
        last_details.get("step_index") == step_index
        and last_details.get("mismatch_score") == mismatch_score
    ):
        return None

    return append_orchestration_event(
        project_dir=project_dir,
        session_id=session_id,
        task_id=task_id,
        event_type=EventType.INTENT_OUTCOME_MISMATCH,
        parent_event_id=parent_event_id,
        details={
            "step_index": step_index,
            "declared_intent": _extract_declared_intent(step_description),
            "expected_artifacts": expected_files[:10],
            "actual_files": actual_files[:10],
            "actual_tool_calls": actual_tool_calls[:10],
            "missing_expected_files": missing_expected_files[:10],
            "mismatch_score": mismatch_score,
            "overlap_signals": overlap,
            "expected_signal_count": expected_signal,
        },
    )


def maybe_emit_divergence_detected(
    *,
    project_dir: Any,
    session_id: int,
    task_id: int,
    parent_event_id: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    events = read_orchestration_events(project_dir, session_id, task_id)
    if len(events) < 2:
        return None

    latest_divergence = None
    for event in reversed(events):
        if event.get("event_type") == EventType.DIVERGENCE_DETECTED:
            latest_divergence = event
            break

    problem_event: Optional[Dict[str, Any]] = None
    reason = None

    recent_non_health = [
        event
        for event in events[-8:]
        if event.get("event_type") != EventType.HEALTH_SCORE_UPDATED
    ]
    recent_retries = [
        event
        for event in recent_non_health
        if event.get("event_type") == EventType.RETRY_ENTERED
    ]
    if len(recent_retries) >= 2:
        problem_event = recent_retries[0]
        reason = "retry_cluster"

    latest_health = next(
        (
            event
            for event in reversed(events)
            if event.get("event_type") == EventType.HEALTH_SCORE_UPDATED
        ),
        None,
    )
    if latest_health and isinstance(
        ((latest_health.get("details") or {}).get("slope")), int
    ):
        if int((latest_health.get("details") or {}).get("slope") or 0) <= -20:
            problem_event = latest_health
            reason = "health_drop"

    latest_validation = next(
        (
            event
            for event in reversed(recent_non_health)
            if event.get("event_type") == EventType.VALIDATION_RESULT
        ),
        None,
    )
    if latest_validation:
        validation_status = str(
            ((latest_validation.get("details") or {}).get("status") or "")
        ).lower()
        prior_success = any(
            str(((event.get("details") or {}).get("status") or "")).lower()
            in {"accepted", "warning"}
            for event in recent_non_health
            if event is not latest_validation
            and event.get("event_type") == EventType.VALIDATION_RESULT
        )
        if prior_success and validation_status in {
            "warning",
            "repair_required",
            "rejected",
        }:
            problem_event = latest_validation
            reason = "validation_regression"

    if not problem_event or not reason:
        return None

    last_known_good_event = None
    problem_index = next(
        (index for index, event in enumerate(events) if event is problem_event),
        len(events) - 1,
    )
    for candidate in reversed(events[:problem_index]):
        if candidate.get("event_type") in {
            EventType.STEP_FINISHED,
            EventType.VALIDATION_RESULT,
            EventType.CHECKPOINT_SAVED,
            EventType.PHASE_FINISHED,
        }:
            candidate_details = candidate.get("details") or {}
            status = str(candidate_details.get("status") or "").lower()
            if (
                candidate.get("event_type") == EventType.STEP_FINISHED
                and status != "success"
            ):
                continue
            if candidate.get(
                "event_type"
            ) == EventType.VALIDATION_RESULT and status not in {
                "accepted",
                "warning",
            }:
                continue
            last_known_good_event = candidate
            break

    problem_event_id = problem_event.get("event_id")
    if latest_divergence and (
        (latest_divergence.get("details") or {}).get("problem_event_id")
        == problem_event_id
    ):
        return None

    return append_orchestration_event(
        project_dir=project_dir,
        session_id=session_id,
        task_id=task_id,
        event_type=EventType.DIVERGENCE_DETECTED,
        parent_event_id=parent_event_id,
        details={
            "reason": reason,
            "problem_event_id": problem_event_id,
            "last_known_good_event_id": (last_known_good_event or {}).get("event_id"),
            "problem_event_type": problem_event.get("event_type"),
            "problem_timestamp": problem_event.get("timestamp"),
        },
    )


def build_orchestration_state_snapshot(
    *,
    session_id: int,
    task_id: int,
    orchestration_state: OrchestrationState,
    checkpoint_name: Optional[str] = None,
    trigger: str,
    related_event_id: Optional[str] = None,
) -> Dict[str, Any]:
    validation_history = list(orchestration_state.validation_history or [])
    validation_verdicts = [
        {
            "stage": item.get("stage"),
            "status": item.get("status"),
            "profile": item.get("profile"),
        }
        for item in validation_history[-10:]
    ]
    retry_budget_remaining = max(
        0,
        MAX_STEP_ATTEMPTS
        + (1 if getattr(orchestration_state, "relaxed_mode", False) else 0)
        - len(getattr(orchestration_state, "debug_attempts", []) or []),
    )
    return {
        "timestamp": datetime.now(UTC).isoformat(),
        "session_id": session_id,
        "task_id": task_id,
        "trigger": trigger,
        "checkpoint_name": checkpoint_name,
        "related_event_id": related_event_id,
        "status": orchestration_state.status.value,
        "plan_steps": list(orchestration_state.plan or []),
        "reasoning_artifact_present": bool(
            getattr(orchestration_state, "reasoning_artifact", None)
        ),
        "current_step_index": int(orchestration_state.current_step_index or 0),
        "retry_budget_remaining": retry_budget_remaining,
        "validation_verdicts": validation_verdicts,
        "files_touched": list(dict.fromkeys(orchestration_state.changed_files or [])),
        "prompt_byte_estimate": len(
            str(orchestration_state.task_description or "").encode("utf-8")
        )
        + len(str(orchestration_state.project_context or "").encode("utf-8")),
        "workspace_hash": _compute_workspace_hash(orchestration_state.project_dir),
        "completion_repair_attempts": int(
            getattr(orchestration_state, "completion_repair_attempts", 0) or 0
        ),
    }


def write_checkpoint_state_snapshot(
    *,
    project_dir: Any,
    session_id: int,
    task_id: int,
    checkpoint_payload: Dict[str, Any],
    trigger: str,
    related_event_id: Optional[str] = None,
) -> Dict[str, Any]:
    orchestration_state = checkpoint_payload.get("orchestration_state", {}) or {}
    context = checkpoint_payload.get("context", {}) or {}
    validation_history = list(orchestration_state.get("validation_history", []) or [])
    validation_verdicts = [
        {
            "stage": item.get("stage"),
            "status": item.get("status"),
            "profile": item.get("profile"),
        }
        for item in validation_history[-10:]
    ]
    retry_budget_remaining = max(
        0,
        MAX_STEP_ATTEMPTS
        + (1 if orchestration_state.get("relaxed_mode") else 0)
        - len(orchestration_state.get("debug_attempts", []) or []),
    )
    log_path = _orchestration_state_snapshot_log_path(project_dir, session_id, task_id)
    payload = {
        "timestamp": datetime.now(UTC).isoformat(),
        "session_id": session_id,
        "task_id": task_id,
        "trigger": trigger,
        "checkpoint_name": checkpoint_payload.get("checkpoint_name"),
        "related_event_id": related_event_id,
        "status": orchestration_state.get("status"),
        "plan_steps": list(orchestration_state.get("plan", []) or []),
        "reasoning_artifact_present": bool(
            orchestration_state.get("reasoning_artifact")
        ),
        "current_step_index": int(
            orchestration_state.get(
                "current_step_index", checkpoint_payload.get("current_step_index", 0)
            )
            or 0
        ),
        "retry_budget_remaining": retry_budget_remaining,
        "validation_verdicts": validation_verdicts,
        "files_touched": list(
            dict.fromkeys(orchestration_state.get("changed_files", []) or [])
        ),
        "prompt_byte_estimate": len(
            str(context.get("task_description", "")).encode("utf-8")
        )
        + len(str(context.get("project_context", "")).encode("utf-8")),
        "workspace_hash": _compute_workspace_hash(project_dir),
        "completion_repair_attempts": int(
            orchestration_state.get("completion_repair_attempts", 0) or 0
        ),
        "snapshot_index": _safe_jsonl_length(log_path),
    }
    _append_jsonl_line(log_path, payload)
    return payload


def write_orchestration_state_snapshot(
    *,
    project_dir: Any,
    session_id: int,
    task_id: int,
    orchestration_state: OrchestrationState,
    checkpoint_name: Optional[str] = None,
    trigger: str,
    related_event_id: Optional[str] = None,
) -> Dict[str, Any]:
    log_path = _orchestration_state_snapshot_log_path(project_dir, session_id, task_id)
    payload = build_orchestration_state_snapshot(
        session_id=session_id,
        task_id=task_id,
        orchestration_state=orchestration_state,
        checkpoint_name=checkpoint_name,
        trigger=trigger,
        related_event_id=related_event_id,
    )
    payload["snapshot_index"] = _safe_jsonl_length(log_path)
    _append_jsonl_line(log_path, payload)
    return payload


def append_orchestration_event(
    *,
    project_dir: Any,
    session_id: int,
    task_id: int,
    event_type: str,
    details: Optional[Dict[str, Any]] = None,
    parent_event_id: Optional[str] = None,
) -> Dict[str, Any]:
    payload = {
        "event_id": str(uuid.uuid4()),
        "timestamp": datetime.now(UTC).isoformat(),
        "event_type": event_type,
        "session_id": session_id,
        "task_id": task_id,
        "parent_event_id": parent_event_id,
        "details": details or {},
    }
    log_path = _orchestration_event_log_path(project_dir, session_id, task_id)
    _append_jsonl_line(log_path, payload)
    suppress_health_update_for = {
        EventType.HEALTH_SCORE_UPDATED,
        EventType.TASK_QUEUED,
        EventType.WAITING_FOR_INPUT,
    }
    if event_type not in suppress_health_update_for:
        try:
            _append_health_score_update(
                project_dir=project_dir,
                session_id=session_id,
                task_id=task_id,
            )
        except Exception:
            pass
    return payload


def attach_failure_envelope(
    details: Optional[Dict[str, Any]],
    envelope: Optional[FailureEnvelope],
) -> Dict[str, Any]:
    payload = dict(details or {})
    if envelope is not None:
        payload["failure_envelope"] = envelope.to_dict()
    return payload


def set_session_alert(
    session: Optional[SessionModel],
    level: Optional[str] = None,
    message: Optional[str] = None,
) -> None:
    if not session:
        return
    session.last_alert_level = level
    session.last_alert_message = message
    session.last_alert_at = datetime.now(UTC) if message else None


def serialize_step_result(step_result: StepResult) -> Dict[str, Any]:
    return {
        "step_number": step_result.step_number,
        "status": step_result.status,
        "output": step_result.output,
        "verification_output": step_result.verification_output,
        "files_changed": step_result.files_changed,
        "error_message": step_result.error_message,
        "attempt": step_result.attempt,
    }


def restore_step_result(data: Dict[str, Any]) -> StepResult:
    return StepResult(
        step_number=data.get("step_number", 0),
        status=data.get("status", "failed"),
        output=data.get("output", ""),
        verification_output=data.get("verification_output", ""),
        files_changed=data.get("files_changed", []) or [],
        error_message=data.get("error_message", ""),
        attempt=data.get("attempt", 1),
    )


def save_orchestration_checkpoint(
    db: Session,
    session_id: int,
    task_id: int,
    prompt: str,
    orchestration_state: OrchestrationState,
    checkpoint_name: str = "autosave_latest",
) -> None:
    checkpoint_service = CheckpointService(db)
    checkpoint_service.save_checkpoint(
        session_id=session_id,
        checkpoint_name=checkpoint_name,
        context_data={
            "task_id": task_id,
            "task_description": prompt,
            "project_name": orchestration_state.project_name,
            "project_context": orchestration_state.project_context,
            "task_subfolder": orchestration_state.task_subfolder,
            "workspace_path_override": (
                str(orchestration_state._workspace_path_override)
                if orchestration_state._workspace_path_override
                else None
            ),
        },
        orchestration_state={
            "status": orchestration_state.status.value,
            "plan": orchestration_state.plan,
            "reasoning_artifact": orchestration_state.reasoning_artifact,
            "current_step_index": orchestration_state.current_step_index,
            "debug_attempts": orchestration_state.debug_attempts,
            "changed_files": orchestration_state.changed_files,
            "validation_history": orchestration_state.validation_history,
            "phase_history": orchestration_state.phase_history,
            "last_plan_validation": orchestration_state.last_plan_validation,
            "last_completion_validation": orchestration_state.last_completion_validation,
            "relaxed_mode": orchestration_state.relaxed_mode,
            "completion_repair_attempts": orchestration_state.completion_repair_attempts,
            "execution_results": [
                serialize_step_result(r) for r in orchestration_state.execution_results
            ],
        },
        current_step_index=orchestration_state.current_step_index,
        step_results=[
            serialize_step_result(r) for r in orchestration_state.execution_results
        ],
    )
    try:
        event = append_orchestration_event(
            project_dir=orchestration_state.project_dir,
            session_id=session_id,
            task_id=task_id,
            event_type=EventType.CHECKPOINT_SAVED,
            details={
                "checkpoint_name": checkpoint_name,
                "current_step_index": orchestration_state.current_step_index,
                "status": orchestration_state.status.value,
            },
        )
        write_orchestration_state_snapshot(
            project_dir=orchestration_state.project_dir,
            session_id=session_id,
            task_id=task_id,
            orchestration_state=orchestration_state,
            checkpoint_name=checkpoint_name,
            trigger="checkpoint_saved",
            related_event_id=event.get("event_id"),
        )
    except Exception:
        pass


def record_validation_verdict(
    db: Session,
    session_id: int,
    task_id: int,
    orchestration_state: OrchestrationState,
    verdict: ValidationVerdict,
    *,
    step_number: Optional[int] = None,
    parent_event_id: Optional[str] = None,
) -> None:
    db.add(
        TaskCheckpoint(
            task_id=task_id,
            session_id=session_id,
            checkpoint_type=f"validation_{verdict.stage}",
            step_number=step_number,
            description=f"{verdict.stage}:{verdict.status}",
            state_snapshot=json.dumps(verdict.to_dict()),
        )
    )
    verdict_payload = verdict.to_dict()
    orchestration_state.validation_history.append(verdict_payload)
    try:
        event = append_orchestration_event(
            project_dir=orchestration_state.project_dir,
            session_id=session_id,
            task_id=task_id,
            event_type=EventType.VALIDATION_RESULT,
            parent_event_id=parent_event_id,
            details={
                "stage": verdict.stage,
                "status": verdict.status,
                "profile": verdict.profile,
                "step_number": step_number,
                "reasons": verdict.reasons[:10],
                "confidence": verdict.confidence,
            },
        )
        write_orchestration_state_snapshot(
            project_dir=orchestration_state.project_dir,
            session_id=session_id,
            task_id=task_id,
            orchestration_state=orchestration_state,
            trigger=f"validation_{verdict.stage}",
            related_event_id=event.get("event_id"),
        )
    except Exception:
        pass
    if verdict.stage == "plan":
        orchestration_state.last_plan_validation = verdict_payload
    elif verdict.stage == "task_completion":
        orchestration_state.last_completion_validation = verdict_payload


def read_orchestration_events(
    project_dir: Any,
    session_id: int,
    task_id: int,
    *,
    event_type_filter: Optional[str] = None,
) -> List[Dict[str, Any]]:
    """Read the append-only event journal for a session/task pair.

    Returns events in chronological order.  Pass ``event_type_filter`` to
    restrict results to a single event type.
    """
    log_path = _orchestration_event_log_path(project_dir, session_id, task_id)
    if not log_path.exists():
        return []

    events: List[Dict[str, Any]] = []
    try:
        with log_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if event_type_filter and event.get("event_type") != event_type_filter:
                    continue
                events.append(event)
    except OSError:
        pass
    return events


def find_latest_orchestration_event(
    project_dir: Any,
    session_id: int,
    task_id: int,
    *,
    event_types: Optional[set[str]] = None,
) -> Optional[Dict[str, Any]]:
    events = read_orchestration_events(project_dir, session_id, task_id)
    for event in reversed(events):
        if event_types and event.get("event_type") not in event_types:
            continue
        return event
    return None


def read_orchestration_state_snapshots(
    project_dir: Any,
    session_id: int,
    task_id: int,
) -> List[Dict[str, Any]]:
    log_path = _orchestration_state_snapshot_log_path(project_dir, session_id, task_id)
    if not log_path.exists():
        return []

    snapshots: List[Dict[str, Any]] = []
    try:
        with log_path.open("r", encoding="utf-8") as handle:
            for line in handle:
                line = line.strip()
                if not line:
                    continue
                try:
                    snapshots.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
    except OSError:
        pass
    return snapshots


def diff_orchestration_state_snapshots(
    project_dir: Any,
    session_id: int,
    task_id: int,
    *,
    from_checkpoint: Optional[int] = None,
    to_checkpoint: Optional[int] = None,
) -> Dict[str, Any]:
    snapshots = read_orchestration_state_snapshots(project_dir, session_id, task_id)
    if not snapshots:
        raise ValueError("No orchestration state snapshots found")
    if to_checkpoint is None:
        to_checkpoint = len(snapshots) - 1
    if from_checkpoint is None:
        from_checkpoint = max(0, to_checkpoint - 1)
    if from_checkpoint < 0 or to_checkpoint < 0:
        raise ValueError("Checkpoint indexes must be non-negative")
    if from_checkpoint >= len(snapshots) or to_checkpoint >= len(snapshots):
        raise ValueError("Checkpoint index is out of range")

    start = snapshots[from_checkpoint]
    end = snapshots[to_checkpoint]
    start_plan = list(start.get("plan_steps") or [])
    end_plan = list(end.get("plan_steps") or [])
    start_files = set(start.get("files_touched") or [])
    end_files = set(end.get("files_touched") or [])
    start_validations = list(start.get("validation_verdicts") or [])
    end_validations = list(end.get("validation_verdicts") or [])

    return {
        "session_id": session_id,
        "task_id": task_id,
        "from_checkpoint": from_checkpoint,
        "to_checkpoint": to_checkpoint,
        "from_snapshot": start,
        "to_snapshot": end,
        "delta": {
            "current_step_index": {
                "from": start.get("current_step_index"),
                "to": end.get("current_step_index"),
                "change": (end.get("current_step_index") or 0)
                - (start.get("current_step_index") or 0),
            },
            "retry_budget_remaining": {
                "from": start.get("retry_budget_remaining"),
                "to": end.get("retry_budget_remaining"),
                "change": (end.get("retry_budget_remaining") or 0)
                - (start.get("retry_budget_remaining") or 0),
            },
            "completion_repair_attempts": {
                "from": start.get("completion_repair_attempts"),
                "to": end.get("completion_repair_attempts"),
                "change": (end.get("completion_repair_attempts") or 0)
                - (start.get("completion_repair_attempts") or 0),
            },
            "status": {
                "from": start.get("status"),
                "to": end.get("status"),
            },
            "plan_step_count": {
                "from": len(start_plan),
                "to": len(end_plan),
                "change": len(end_plan) - len(start_plan),
            },
            "validation_verdicts": {
                "from_count": len(start_validations),
                "to_count": len(end_validations),
                "new_entries": end_validations[len(start_validations) :],
            },
            "files_touched": {
                "from_count": len(start_files),
                "to_count": len(end_files),
                "added": sorted(end_files - start_files),
                "removed": sorted(start_files - end_files),
            },
            "prompt_byte_estimate": {
                "from": start.get("prompt_byte_estimate"),
                "to": end.get("prompt_byte_estimate"),
                "change": (end.get("prompt_byte_estimate") or 0)
                - (start.get("prompt_byte_estimate") or 0),
            },
            "workspace_hash_changed": start.get("workspace_hash")
            != end.get("workspace_hash"),
        },
    }


def record_live_log(
    db: Session,
    session_id: int,
    task_id: Optional[int],
    level: str,
    message: str,
    session_instance_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    db.add(
        LogEntry(
            session_id=session_id,
            task_id=task_id,
            level=level,
            message=message,
            session_instance_id=session_instance_id,
            log_metadata=json.dumps(metadata) if metadata else None,
        )
    )
    db.commit()
