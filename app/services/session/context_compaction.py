"""Deterministic checkpoint compaction for low-resource task dispatch."""

from __future__ import annotations

import json
from typing import Any, Dict, List, Optional

_MAX_PROJECT_CONTEXT_CHARS = 200
_DEFAULT_MAX_PLAN_STEPS = 3
_MAX_CHANGED_FILES = 10
_MAX_VALIDATION_HISTORY = 2
_MAX_FAILURE_CHARS = 500


def compact_checkpoint_payload(
    payload: Dict[str, Any],
    max_plan_steps: int = _DEFAULT_MAX_PLAN_STEPS,
) -> Dict[str, Any]:
    """Return a compacted copy of a CheckpointData-shaped dict."""
    try:
        return _do_compact(payload, max_plan_steps)
    except Exception:
        return payload


def estimate_tokens(payload: Dict[str, Any]) -> int:
    """Rough token count: four JSON chars per token."""
    try:
        return len(json.dumps(payload, default=str)) // 4
    except Exception:
        return 0


def needs_compaction(payload: Dict[str, Any], token_limit: int) -> bool:
    """Return True when payload exceeds 75 percent of the target context."""
    return estimate_tokens(payload) > int(token_limit * 0.75)


def _do_compact(payload: Dict[str, Any], max_plan_steps: int) -> Dict[str, Any]:
    ctx = dict(payload.get("context") or {})
    orch = dict(payload.get("orchestration_state") or {})
    step_results: List[Dict[str, Any]] = list(payload.get("step_results") or [])

    return {
        "session_id": payload.get("session_id"),
        "checkpoint_name": payload.get("checkpoint_name"),
        "context": _compact_context(ctx),
        "orchestration_state": _compact_orchestration_state(orch, max_plan_steps),
        "current_step_index": payload.get("current_step_index"),
        "step_results": _compact_step_results(step_results),
        "created_at": payload.get("created_at"),
        "_compacted": True,
    }


def _compact_context(ctx: Dict[str, Any]) -> Dict[str, Any]:
    project_context = str(ctx.get("project_context") or "")
    if len(project_context) > _MAX_PROJECT_CONTEXT_CHARS:
        project_context = project_context[:_MAX_PROJECT_CONTEXT_CHARS] + "..."

    return {
        "task_id": ctx.get("task_id"),
        "task_description": ctx.get("task_description"),
        "project_name": ctx.get("project_name"),
        "project_context": project_context or None,
        "task_subfolder": ctx.get("task_subfolder"),
        "workspace_path_override": ctx.get("workspace_path_override"),
        "human_guidance": ctx.get("human_guidance") or None,
    }


def _compact_orchestration_state(
    orch: Dict[str, Any], max_plan_steps: int
) -> Dict[str, Any]:
    plan = list(orch.get("plan") or [])
    truncated = len(plan) > max_plan_steps
    plan = plan[:max_plan_steps]

    changed_files = list(dict.fromkeys(orch.get("changed_files") or []))
    changed_files = changed_files[:_MAX_CHANGED_FILES]

    validation_history = list(orch.get("validation_history") or [])
    compact_validation = [
        {"stage": v.get("stage"), "status": v.get("status")}
        for v in validation_history[-_MAX_VALIDATION_HISTORY:]
        if isinstance(v, dict)
    ]

    debug_attempts = list(orch.get("debug_attempts") or [])
    phase_history = list(orch.get("phase_history") or [])
    phase_summary = (
        {"count": len(phase_history), "last": phase_history[-1]}
        if phase_history
        else None
    )

    last_plan_validation = orch.get("last_plan_validation") or {}
    compact_last_plan_validation = (
        {"status": last_plan_validation.get("status")}
        if isinstance(last_plan_validation, dict) and last_plan_validation
        else None
    )

    return {
        "status": orch.get("status"),
        "plan": plan,
        "plan_truncated": truncated,
        "current_step_index": orch.get("current_step_index"),
        "changed_files": changed_files,
        "validation_history": compact_validation,
        "debug_attempt_count": len(debug_attempts),
        "phase_summary": phase_summary,
        "completion_repair_attempts": orch.get("completion_repair_attempts", 0),
        "last_plan_validation": compact_last_plan_validation,
        "relaxed_mode": orch.get("relaxed_mode", False),
    }


def _compact_step_results(step_results: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
    last_failed: Optional[Dict[str, Any]] = None
    for result in reversed(step_results):
        if result.get("status") not in ("success", "completed", "done"):
            last_failed = result
            break

    if last_failed is None:
        return []

    return [
        {
            "step_number": last_failed.get("step_number"),
            "status": last_failed.get("status"),
            "error_message": str(last_failed.get("error_message") or "")[
                :_MAX_FAILURE_CHARS
            ],
        }
    ]
