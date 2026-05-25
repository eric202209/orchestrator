"""Checkpoint application helpers for orchestration workers."""

import json
from typing import Any, Dict

from app.services.orchestration import ValidatorService
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.state.persistence import (
    append_orchestration_event as _append_orchestration_event,
    restore_step_result as _restore_step_result,
)
from app.services.prompt_templates import OrchestrationStatus


def _apply_checkpoint_payload(
    checkpoint_payload: Dict[str, Any],
    *,
    orchestration_state: Any,
    task: Any,
    session_id: int,
    task_id: int,
    prompt: str,
    emit_live: Any,
) -> tuple:
    """Apply checkpoint data to orchestration_state. Returns (updated_prompt, compatibility)."""
    checkpoint_context = checkpoint_payload.get("context", {}) or {}
    checkpoint_state = checkpoint_payload.get("orchestration_state", {}) or {}

    prompt = checkpoint_context.get("task_description", prompt) or prompt
    orchestration_state.project_name = checkpoint_context.get(
        "project_name", orchestration_state.project_name
    )
    orchestration_state.project_context = checkpoint_context.get(
        "project_context", orchestration_state.project_context
    )
    if checkpoint_context.get("task_subfolder"):
        orchestration_state._task_subfolder_override = checkpoint_context.get(
            "task_subfolder"
        )

    orchestration_state.plan = checkpoint_state.get("plan", []) or []
    orchestration_state.reasoning_artifact = checkpoint_state.get("reasoning_artifact")
    orchestration_state.current_step_index = (
        checkpoint_state.get(
            "current_step_index",
            checkpoint_payload.get("current_step_index", 0) or 0,
        )
        or 0
    )
    orchestration_state.debug_attempts = (
        checkpoint_state.get("debug_attempts", []) or []
    )
    orchestration_state.changed_files = checkpoint_state.get("changed_files", []) or []
    orchestration_state.validation_history = (
        checkpoint_state.get("validation_history", []) or []
    )
    orchestration_state.phase_history = checkpoint_state.get("phase_history", []) or []
    orchestration_state.last_plan_validation = checkpoint_state.get(
        "last_plan_validation"
    )
    orchestration_state.last_completion_validation = checkpoint_state.get(
        "last_completion_validation"
    )
    orchestration_state.relaxed_mode = bool(checkpoint_state.get("relaxed_mode", False))
    orchestration_state.completion_repair_attempts = int(
        checkpoint_state.get("completion_repair_attempts", 0) or 0
    )
    orchestration_state.debug_repair_task_execution_ids = [
        int(item)
        for item in checkpoint_state.get("debug_repair_task_execution_ids", []) or []
        if str(item).isdigit()
    ]
    orchestration_state.execution_results = [
        _restore_step_result(item)
        for item in checkpoint_state.get(
            "execution_results", checkpoint_payload.get("step_results", [])
        )
    ]
    restored_result_count = len(orchestration_state.execution_results)
    restored_cursor = int(orchestration_state.current_step_index or 0)
    plan_length = len(orchestration_state.plan or [])
    reconciled_cursor = max(0, min(restored_result_count, plan_length))
    if restored_cursor != reconciled_cursor:
        orchestration_state.current_step_index = reconciled_cursor
        reconciliation_details = {
            "checkpoint_name": checkpoint_payload.get("_resolved_checkpoint_name")
            or checkpoint_payload.get("checkpoint_name"),
            "requested_checkpoint_name": checkpoint_payload.get(
                "_requested_checkpoint_name"
            ),
            "original_current_step_index": restored_cursor,
            "reconciled_current_step_index": reconciled_cursor,
            "execution_results_count": restored_result_count,
            "plan_length": plan_length,
            "reason": "checkpoint_cursor_execution_results_mismatch",
        }
        _append_orchestration_event(
            project_dir=orchestration_state.project_dir,
            session_id=session_id,
            task_id=task_id,
            event_type=EventType.CHECKPOINT_CURSOR_RECONCILED,
            details=reconciliation_details,
        )
        emit_live(
            "WARN",
            "[ORCHESTRATION] Resume checkpoint cursor did not match saved execution results; reconciled to the durable result boundary",
            metadata={"phase": "resume", **reconciliation_details},
        )
    raw_status = checkpoint_state.get("status", "")
    if raw_status in ("executing", "debugging", "revising_plan"):
        try:
            orchestration_state.status = OrchestrationStatus(raw_status)
        except ValueError:
            pass
    elif orchestration_state.plan and orchestration_state.current_step_index < len(
        orchestration_state.plan
    ):
        orchestration_state.status = OrchestrationStatus.EXECUTING
    _append_orchestration_event(
        project_dir=orchestration_state.project_dir,
        session_id=session_id,
        task_id=task_id,
        event_type=EventType.CHECKPOINT_LOADED,
        details={
            "checkpoint_name": checkpoint_payload.get("_resolved_checkpoint_name")
            or checkpoint_payload.get("checkpoint_name"),
            "requested_checkpoint_name": checkpoint_payload.get(
                "_requested_checkpoint_name"
            ),
        },
    )
    if (
        orchestration_state.completion_repair_attempts > 0
        and orchestration_state.plan
        and len(orchestration_state.execution_results)
        == int(orchestration_state.current_step_index or 0)
        and int(orchestration_state.current_step_index or 0)
        == len(orchestration_state.plan) - 1
    ):
        stale_repair_step = orchestration_state.plan[-1]
        orchestration_state.plan = orchestration_state.plan[
            : orchestration_state.current_step_index
        ]
        orchestration_state.completion_repair_attempts = 0
        task.steps = json.dumps(orchestration_state.plan)
        emit_live(
            "WARN",
            "[ORCHESTRATION] Dropped a stale pending completion-repair step from the resume checkpoint and reset the repair budget",
            metadata={
                "phase": "resume",
                "stale_completion_repair_step": stale_repair_step.get(
                    "description", ""
                ),
            },
        )
    completed_step_count = max(
        len(orchestration_state.execution_results),
        int(orchestration_state.current_step_index or 0),
    )
    compatibility = (
        ValidatorService.assess_plan_workspace_compatibility(
            project_dir=orchestration_state.project_dir,
            plan=orchestration_state.plan,
            completed_step_count=completed_step_count,
        )
        if orchestration_state.plan
        else {"compatible": True}
    )
    if orchestration_state.plan and orchestration_state.current_step_index >= len(
        orchestration_state.plan
    ):
        orchestration_state.completion_repair_attempts = 0
    return prompt, compatibility
