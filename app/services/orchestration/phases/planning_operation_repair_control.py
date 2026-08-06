"""Bounded operation-level Planning repair orchestration control."""

from __future__ import annotations

import json
from typing import Any

from app.services.orchestration.events.telemetry import emit_phase_event
from app.services.orchestration.phases.planning_support import (
    BLOCKING_IMMEDIATE_REPAIR_ISSUE_KEYS,
    _PlanningRetryState,
    _finalize_planning_terminal_failure,
    _terminal_planning_root_cause,
    _validate_planning_plan,
)
from app.services.orchestration.planning.operation_repair import (
    OperationRepairError,
    build_operation_repair_prompt,
    merge_and_validate_operation_repairs,
    select_operation_repair_route,
)
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.prompt_templates import OrchestrationStatus
from app.services.orchestration.types import OrchestrationRunContext


def attempt_operation_level_repair(
    *,
    ctx: OrchestrationRunContext,
    retry_state: _PlanningRetryState,
    blocking_repair_issues: dict[str, Any],
    output_text: str,
    planning_timeout_seconds: int,
) -> dict[str, Any]:
    """Attempt the bounded operation lane or leave complete-plan fallback intact."""
    operation_plan_verdict = _validate_planning_plan(
        ctx, ctx.orchestration_state.plan, output_text
    )
    operation_findings = list(
        (operation_plan_verdict.details or {}).get("source_operation_findings") or []
    )
    operation_route = select_operation_repair_route(
        findings=operation_findings,
        source_materialization=ctx.planner_source_materialization,
        project_dir=ctx.orchestration_state.project_dir,
        structural_failure=(
            set(blocking_repair_issues) != {"stale_replace_ops_steps"}
            or len(operation_findings)
            != len(blocking_repair_issues.get("stale_replace_ops_steps", []))
        ),
    )
    if operation_route.lane != "operation_level":
        return {"action": "fallback"}

    try:
        operation_prompt = build_operation_repair_prompt(
            task_constraints=ctx.prompt,
            original_plan=ctx.orchestration_state.plan,
            rejected_findings=operation_findings,
            source_materialization=ctx.planner_source_materialization,
            project_dir=ctx.orchestration_state.project_dir,
        )
    except OperationRepairError:
        return {"action": "fallback"}

    emit_phase_event(
        ctx.orchestration_state,
        ctx.emit_live,
        level="WARN",
        phase="planning",
        message=(
            "[ORCHESTRATION] Starting one bounded operation-level "
            "Planning repair request"
        ),
        details={
            "repair_lane": "operation_level",
            "rejected_operation_identities": list(operation_route.rejected_identities),
            "repair_prompt_chars": len(operation_prompt),
            "repair_prompt_estimated_tokens": (len(operation_prompt) + 3) // 4,
            "max_output_tokens": 2048,
            "provider_call_limit": 1,
        },
    )
    try:
        operation_provider_result = PlannerService.repair_operations(
            runtime_service=ctx.runtime_service,
            repair_prompt=operation_prompt,
            timeout_seconds=planning_timeout_seconds,
        )
        operation_merge_result = merge_and_validate_operation_repairs(
            original_plan=ctx.orchestration_state.plan,
            rejected_findings=operation_findings,
            response_text=str(operation_provider_result.get("output") or ""),
            source_materialization=ctx.planner_source_materialization,
            project_dir=ctx.orchestration_state.project_dir,
            validate_complete_plan=lambda plan: _validate_planning_plan(
                ctx, plan, json.dumps(plan)
            ),
        )
    except Exception as exc:
        ctx.orchestration_state.status = OrchestrationStatus.ABORTED
        ctx.orchestration_state.abort_reason = (
            "Operation-level Planning repair failed closed"
        )
        _finalize_planning_terminal_failure(
            ctx=ctx,
            failure_type="planning_operation_repair_failed",
            failure_reason=(
                "Operation-level Planning repair failed closed after at most one "
                f"provider request: {type(exc).__name__}: {str(exc)[:500]}"
            ),
            planning_root_cause=_terminal_planning_root_cause(retry_state),
        )
        return {
            "action": "return",
            "result": {
                "status": "failed",
                "reason": "planning_operation_repair_failed",
            },
        }

    ctx.orchestration_state.plan = operation_merge_result.plan
    merged_output_text = json.dumps(operation_merge_result.plan)
    retry_state.repair_prompt_used = True
    setattr(retry_state, "operation_repair_succeeded", True)
    immediate_repair_issues = PlannerService.find_immediate_repair_step_issues(
        ctx.orchestration_state.plan,
        project_dir=ctx.orchestration_state.project_dir,
        source_materialization=ctx.planner_source_materialization,
    )
    remaining_blocking_issues = {
        key: value
        for key, value in immediate_repair_issues.items()
        if key in BLOCKING_IMMEDIATE_REPAIR_ISSUE_KEYS and value
    }
    if remaining_blocking_issues:
        _finalize_planning_terminal_failure(
            ctx=ctx,
            failure_type="planning_operation_repair_failed",
            failure_reason=(
                "Merged operation repair retained blocking findings; "
                "no second repair request is permitted"
            ),
            planning_root_cause=_terminal_planning_root_cause(retry_state),
        )
        return {
            "action": "return",
            "result": {
                "status": "failed",
                "reason": "planning_operation_repair_failed",
            },
        }

    emit_phase_event(
        ctx.orchestration_state,
        ctx.emit_live,
        level="INFO",
        phase="planning",
        message=(
            "[ORCHESTRATION] Operation-level repair merged and "
            "complete Plan validation accepted"
        ),
        details={
            "repair_lane": "operation_level",
            "provider_call_count": 1,
            "repair_prompt_chars": len(operation_prompt),
            "repair_output_chars": len(
                str(operation_provider_result.get("output") or "")
            ),
            "validator_status": getattr(
                operation_merge_result.validator_verdict, "status", None
            ),
        },
    )
    return {
        "action": "continue",
        "planning_result": {
            "status": "completed",
            "output": merged_output_text,
        },
    }
