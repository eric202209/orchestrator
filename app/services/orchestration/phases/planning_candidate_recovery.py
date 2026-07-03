"""Candidate Recovery glue for the planning phase."""

from __future__ import annotations

import asyncio
import json
from typing import Any, Callable

from app.config import settings
from app.services.orchestration.planning.normalization import (
    normalize_existing_file_target_plan,
    normalize_stale_replace_ops_to_small_file_writes,
)
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.recovery.execution_recovery_evidence import (
    ExecutionRecoveryEvidence,
)
from app.services.orchestration.recovery.recovery_context import RecoveryContext
from app.services.orchestration.recovery.recovery_strategy_registry import (
    RecoveryStrategyRegistry,
)
from app.services.orchestration.state.persistence import record_validation_verdict
from app.services.orchestration.types import OrchestrationRunContext
from app.services.orchestration.validation.validator import ValidatorService
from app.services.prompt_templates import OrchestrationStatus
from app.services.orchestration.phases.planning_support import (
    _finalize_planning_terminal_failure,
    _terminal_planning_root_cause,
)
from app.services.orchestration.phases.planning_task1_bootstrap import (
    is_first_ordered_task as _is_first_ordered_task,
)
from app.services.orchestration.phases.planning_verification import (
    _strengthen_weak_expected_file_verifications,
)
from app.services.planning.candidate_recovery import (
    CandidateRecoveryRequest,
    execute_single_sibling_candidate_recovery,
    planning_failure_signature,
)


def candidate_recovery_precheck(
    ctx: OrchestrationRunContext, plan_verdict: Any
) -> bool:
    if not settings.CANDIDATE_RECOVERY_ENABLED:
        return False
    if settings.RUNTIME_PROFILE != "standard":
        return False
    if getattr(plan_verdict, "accepted", False):
        return False
    status = str(getattr(plan_verdict, "status", "") or "")
    return status in {"repair_required", "rejected"}


def try_candidate_recovery_after_validation(
    *,
    ctx: OrchestrationRunContext,
    plan_verdict: Any,
    output_text: str,
    planning_prompt: str,
    planning_timeout_seconds: int,
    prompt_profile: str,
    planning_phase_event: Any,
    extract_structured_text: Callable[[Any], str],
    extract_plan_steps: Callable[[Any], Any],
    normalize_plan_with_live_logging: Callable[..., Any],
    coerce_output_text: Callable[..., str],
) -> Any:
    """Run Machine-A Candidate Recovery at the first validation failure only."""

    if not candidate_recovery_precheck(ctx, plan_verdict):
        return None

    original_plan = list(ctx.orchestration_state.plan or [])
    signature = planning_failure_signature(tuple(plan_verdict.reasons or ()))

    def _validate_candidate(candidate_plan: list[dict[str, Any]], text: str) -> Any:
        return ValidatorService.validate_plan(
            candidate_plan,
            output_text=text,
            task_prompt=ctx.prompt,
            execution_profile=ctx.execution_profile,
            project_dir=ctx.orchestration_state.project_dir,
            title=ctx.task.title if ctx.task else None,
            description=ctx.task.description if ctx.task else None,
            validation_severity=ctx.validation_severity,
            workflow_profile=ctx.workflow_profile,
            workflow_stage=ctx.workflow_stage,
            is_first_ordered_task=_is_first_ordered_task(ctx.task),
        )

    def _generate_sibling() -> tuple[list[dict[str, Any]], str]:
        sibling_result = asyncio.run(
            PlannerService._execute_task_with_planning_lock(
                ctx.runtime_service,
                planning_prompt,
                timeout_seconds=planning_timeout_seconds,
                reuse_task_session=False,
                diagnostic_label="CANDIDATE_PLANNING",
                diagnostic_metadata={
                    "session_id": ctx.session_id,
                    "task_id": ctx.task_id,
                    "task_execution_id": ctx.task_execution_id,
                    "workflow_profile": ctx.workflow_profile,
                    "prompt_profile": prompt_profile,
                    "planning_attempt": "candidate_sibling_1",
                },
            )
        )
        sibling_output_text = coerce_output_text(
            ctx=ctx,
            planning_result=sibling_result,
            output_result=sibling_result.get("output", ""),
            extract_structured_text=extract_structured_text,
        )
        success, plan_data, strategy_info = ctx.error_handler.attempt_json_parsing(
            sibling_output_text, context="planning"
        )
        if not success:
            raise RuntimeError(f"candidate sibling parse failed: {strategy_info}")
        extracted_plan = extract_plan_steps(plan_data)
        if extracted_plan is None:
            raise RuntimeError("candidate sibling did not produce a step list")
        sanitized_plan = PlannerService.sanitize_common_plan_issues(
            extracted_plan, task_prompt=ctx.prompt
        )
        sanitized_plan = _strengthen_weak_expected_file_verifications(sanitized_plan)
        sanitized_plan, _ = normalize_existing_file_target_plan(
            sanitized_plan,
            project_dir=ctx.orchestration_state.project_dir,
        )
        sanitized_plan, _ = normalize_stale_replace_ops_to_small_file_writes(
            sanitized_plan,
            project_dir=ctx.orchestration_state.project_dir,
        )
        normalized_plan = normalize_plan_with_live_logging(
            ctx.db,
            ctx.session_id,
            ctx.task_id,
            sanitized_plan,
            ctx.orchestration_state.project_dir,
            ctx.logger,
            ctx.session_instance_id,
            "Candidate planning output",
        )
        return normalized_plan, json.dumps(normalized_plan)

    runtime_result_holder: dict[str, Any] = {}

    def _candidate_executor() -> Any:
        runtime_result = execute_single_sibling_candidate_recovery(
            CandidateRecoveryRequest(
                project_dir=ctx.orchestration_state.project_dir,
                session_id=ctx.session_id,
                task_id=ctx.task_id,
                original_plan=original_plan,
                original_output_text=output_text,
                original_verdict=plan_verdict,
                runtime_profile=settings.RUNTIME_PROFILE,
                parent_event_id=(planning_phase_event or {}).get("event_id"),
                generate_sibling=_generate_sibling,
                validate_candidate=_validate_candidate,
            )
        )
        runtime_result_holder["result"] = runtime_result
        return runtime_result

    evidence = ExecutionRecoveryEvidence(
        task_title=str(getattr(ctx.task, "title", "") or "")[:200],
        task_description=str(ctx.prompt or "")[:400],
        failed_command="planning_validation",
        exit_code=None,
        stdout_excerpt="",
        stderr_excerpt="; ".join(plan_verdict.reasons[:5]),
        traceback_excerpt="",
        validator_rejection_reason="; ".join(plan_verdict.reasons[:5]),
        failure_class="planning_validation_failed",
    )
    recovery_context = RecoveryContext(
        project_dir=ctx.orchestration_state.project_dir,
        session_id=ctx.session_id,
        task_id=ctx.task_id,
        scope="planning",
        evidence=evidence,
        orchestration_state=ctx.orchestration_state,
        recovery_metadata={
            "planning_failure_signature": signature,
            "candidate_executor": _candidate_executor,
        },
    )
    outcome = RecoveryStrategyRegistry.execute_candidate_planning(
        context=recovery_context
    )
    if not outcome.succeeded:
        if outcome.get("status") == "skipped":
            return None
        return {
            "outcome": outcome,
            "runtime_result": runtime_result_holder.get("result"),
        }
    return {"outcome": outcome, "runtime_result": runtime_result_holder["result"]}


def apply_candidate_recovery_after_validation(
    *,
    ctx: OrchestrationRunContext,
    retry_state: Any,
    plan_verdict: Any,
    output_text: str,
    recovery_hooks: dict[str, Any],
) -> Any:
    if getattr(retry_state, "repair_prompt_used", False):
        return None
    candidate_recovery = try_candidate_recovery_after_validation(
        ctx=ctx,
        plan_verdict=plan_verdict,
        output_text=output_text,
        planning_prompt=recovery_hooks["planning_prompt"] or "",
        planning_timeout_seconds=recovery_hooks["planning_timeout_seconds"],
        prompt_profile=recovery_hooks["prompt_profile"],
        planning_phase_event=recovery_hooks["planning_phase_event"],
        extract_structured_text=recovery_hooks["extract_structured_text"],
        extract_plan_steps=recovery_hooks["extract_plan_steps"],
        normalize_plan_with_live_logging=recovery_hooks[
            "normalize_plan_with_live_logging"
        ],
        coerce_output_text=recovery_hooks["coerce_output_text"],
    )
    if candidate_recovery is None:
        return None
    candidate_outcome = candidate_recovery["outcome"]
    candidate_runtime_result = candidate_recovery.get("runtime_result")
    if candidate_outcome.succeeded and candidate_runtime_result:
        ctx.orchestration_state.plan = candidate_runtime_result.selected_plan
        record_validation_verdict(
            ctx.db,
            ctx.session_id,
            ctx.task_id,
            ctx.orchestration_state,
            candidate_runtime_result.selected_verdict.verdict,
            parent_event_id=(recovery_hooks["planning_phase_event"] or {}).get(
                "event_id"
            ),
        )
        return {
            "plan_verdict": candidate_runtime_result.selected_verdict,
            "output_text": candidate_runtime_result.selected_output_text,
        }

    ctx.orchestration_state.status = OrchestrationStatus.ABORTED
    ctx.orchestration_state.abort_reason = (
        "Candidate planning exhausted without an accepted plan"
    )
    _finalize_planning_terminal_failure(
        ctx=ctx,
        failure_type="candidate_planning_exhausted",
        failure_reason=(
            "Candidate planning exhausted after original plus one sibling candidate"
        ),
        planning_root_cause=_terminal_planning_root_cause(retry_state),
    )
    if ctx.restore_workspace_snapshot_if_needed:
        ctx.restore_workspace_snapshot_if_needed("candidate planning exhausted")
    return {
        "return_result": {
            "status": "failed",
            "reason": "candidate_planning_exhausted",
        }
    }
