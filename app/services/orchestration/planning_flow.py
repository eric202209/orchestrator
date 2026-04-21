"""Planning-phase orchestration flow."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Dict

from app.models import TaskStatus
from app.services.orchestration.context_assembly import assemble_planning_prompt
from app.services.orchestration.persistence import (
    append_orchestration_event,
    record_validation_verdict,
)
from app.services.orchestration.planner import PlannerService
from app.services.orchestration.policy import clamp_planning_timeout
from app.services.orchestration.telemetry import emit_phase_event
from app.services.orchestration.types import OrchestrationRunContext
from app.services.orchestration.validator import ValidatorService
from app.services.prompt_templates import OrchestrationStatus, estimate_token_count


def execute_planning_phase(
    *,
    ctx: OrchestrationRunContext,
    workspace_review: Dict[str, Any],
    extract_structured_text: Callable[[Any], str],
    extract_plan_steps: Callable[[Any], Any],
    looks_like_truncated_multistep_plan: Callable[[str, Any], bool],
    normalize_plan_with_live_logging: Callable[..., Any],
    workspace_violation_error_cls: type[Exception],
) -> Dict[str, Any]:
    ctx.logger.info("[ORCHESTRATION] Phase 1: PLANNING - generating step plan")
    emit_phase_event(
        ctx.orchestration_state,
        ctx.emit_live,
        level="INFO",
        phase="planning",
        message="[ORCHESTRATION] Phase 1: PLANNING - generating step plan",
        details={
            "project_context_chars": len(ctx.orchestration_state.project_context or ""),
            "task_chars": len(ctx.prompt or ""),
        },
    )
    try:
        append_orchestration_event(
            project_dir=ctx.orchestration_state.project_dir,
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            event_type="phase_started",
            details={"phase": "planning"},
        )
    except Exception:
        pass

    planning_prompt = (
        assemble_planning_prompt(ctx, workspace_review)
        if ctx.openclaw_service
        else None
    )
    planning_prompt_tokens = estimate_token_count(planning_prompt or "")

    planning_timeout_seconds = clamp_planning_timeout(ctx.timeout_seconds)
    start_with_minimal_planning_prompt = (
        PlannerService.should_start_with_minimal_prompt(
            ctx.prompt,
            ctx.orchestration_state.project_context,
        )
    )
    if workspace_review.get("has_existing_files"):
        start_with_minimal_planning_prompt = True
    # Qwen3.5-35B has a 128 k context window; 2200 tokens is far too
    # conservative.  Use 8000 tokens (~6000 words) as the boundary
    # so normal project contexts get the full planning prompt.
    MINIMAL_PROMPT_TOKEN_THRESHOLD = 8000
    if planning_prompt_tokens > MINIMAL_PROMPT_TOKEN_THRESHOLD:
        start_with_minimal_planning_prompt = True
    used_minimal_planning_prompt = start_with_minimal_planning_prompt

    if start_with_minimal_planning_prompt:
        emit_phase_event(
            ctx.orchestration_state,
            ctx.emit_live,
            level="WARN",
            phase="planning",
            message="[ORCHESTRATION] Planning context is dense; starting with minimal prompt",
            details={
                "strategy": "minimal_prompt_first",
                "project_context_length": len(
                    ctx.orchestration_state.project_context or ""
                ),
                "estimated_prompt_tokens": planning_prompt_tokens,
            },
        )
        planning_result = PlannerService.retry_with_minimal_prompt(
            openclaw_service=ctx.openclaw_service,
            task_description=ctx.prompt,
            project_dir=ctx.orchestration_state.project_dir,
            timeout_seconds=planning_timeout_seconds,
            logger=ctx.logger,
            emit_live=ctx.emit_live,
            reason="dense_planning_context",
        )
    else:
        planning_result = asyncio.run(
            ctx.openclaw_service.execute_task(
                planning_prompt, timeout_seconds=planning_timeout_seconds
            )
        )

    initial_output_text = __coerce_output_text(
        ctx=ctx,
        planning_result=planning_result,
        output_result=planning_result.get("output", ""),
        extract_structured_text=extract_structured_text,
    )
    if PlannerService.should_retry_with_minimal_prompt(
        planning_result, initial_output_text
    ) and not PlannerService.looks_salvageable_planning_output(initial_output_text):
        ctx.logger.warning(
            "[ORCHESTRATION] Planning failed on the first pass; retrying with minimal prompt"
        )
        emit_phase_event(
            ctx.orchestration_state,
            ctx.emit_live,
            level="WARN",
            phase="planning",
            message="[ORCHESTRATION] Planning needed a fallback; retrying with minimal prompt",
            details={
                "retry": "minimal_prompt",
                "reason": (planning_result.get("error") or initial_output_text)[:240],
            },
        )
        planning_result = PlannerService.retry_with_minimal_prompt(
            openclaw_service=ctx.openclaw_service,
            task_description=ctx.prompt,
            project_dir=ctx.orchestration_state.project_dir,
            timeout_seconds=planning_timeout_seconds,
            logger=ctx.logger,
            emit_live=ctx.emit_live,
            reason=(planning_result.get("error") or initial_output_text),
        )
        used_minimal_planning_prompt = True

    try:
        used_planning_repair_prompt = False
        while True:
            output_result = planning_result.get("output", {})
            output_text = __coerce_output_text(
                ctx=ctx,
                planning_result=planning_result,
                output_result=output_result,
                extract_structured_text=extract_structured_text,
            )

            success, plan_data, strategy_info = ctx.error_handler.attempt_json_parsing(
                output_text, context="planning"
            )

            if (
                PlannerService.should_retry_with_minimal_prompt(
                    planning_result, output_text
                )
                and not success
                and not PlannerService.looks_salvageable_planning_output(output_text)
            ):
                raise TimeoutError(
                    f"Planning timed out or exceeded context after {planning_timeout_seconds}s"
                )

            if not success and not used_minimal_planning_prompt:
                planning_result = __retry_with_minimal_prompt(
                    ctx=ctx,
                    planning_timeout_seconds=planning_timeout_seconds,
                    reason=f"json_parse_failed: {output_text[:240]}",
                )
                used_minimal_planning_prompt = True
                continue

            if not success and not used_planning_repair_prompt:
                emit_phase_event(
                    ctx.orchestration_state,
                    ctx.emit_live,
                    level="WARN",
                    phase="planning",
                    message="[ORCHESTRATION] Planning response was malformed or truncated; starting repair pass",
                    details={
                        "reason": f"json_parse_failed_after_minimal: {strategy_info}"[
                            :240
                        ],
                        "output_chars": len(output_text or ""),
                    },
                )
                planning_result = __repair_planning_output(
                    ctx=ctx,
                    planning_timeout_seconds=planning_timeout_seconds,
                    malformed_output=output_text,
                    reason=f"json_parse_failed_after_minimal: {strategy_info}",
                )
                used_planning_repair_prompt = True
                continue

            if not success:
                ctx.orchestration_state.status = OrchestrationStatus.ABORTED
                ctx.orchestration_state.abort_reason = (
                    f"Planning JSON parse failed: {strategy_info}"
                )
                emit_phase_event(
                    ctx.orchestration_state,
                    ctx.emit_live,
                    level="ERROR",
                    phase="planning",
                    message=f"[ORCHESTRATION] Planning JSON parse failed: {strategy_info}",
                    details={"reason": "planning_json_error"},
                )
                ctx.task.status = TaskStatus.FAILED
                ctx.task.error_message = (
                    f"Planning JSON parse failed: {strategy_info}. "
                    f"Raw output: {output_text[:500]}"
                )
                ctx.db.commit()
                if ctx.restore_workspace_snapshot_if_needed:
                    ctx.restore_workspace_snapshot_if_needed(
                        "planning JSON parse failure"
                    )
                return {"status": "failed", "reason": "planning_json_error"}

            extracted_plan = extract_plan_steps(plan_data)
            if extracted_plan is None and not used_minimal_planning_prompt:
                planning_result = __retry_with_minimal_prompt(
                    ctx=ctx,
                    planning_timeout_seconds=planning_timeout_seconds,
                    reason=f"unexpected_plan_shape: {str(plan_data)[:240]}",
                )
                used_minimal_planning_prompt = True
                continue

            if extracted_plan is None and not used_planning_repair_prompt:
                planning_result = __repair_planning_output(
                    ctx=ctx,
                    planning_timeout_seconds=planning_timeout_seconds,
                    malformed_output=output_text,
                    reason="unexpected_plan_shape_after_minimal",
                )
                used_planning_repair_prompt = True
                continue

            if (
                looks_like_truncated_multistep_plan(output_text, extracted_plan)
                and not used_minimal_planning_prompt
            ):
                planning_result = __retry_with_minimal_prompt(
                    ctx=ctx,
                    planning_timeout_seconds=planning_timeout_seconds,
                    reason="truncated_multistep_plan_detected",
                )
                used_minimal_planning_prompt = True
                continue

            if (
                looks_like_truncated_multistep_plan(output_text, extracted_plan)
                and not used_planning_repair_prompt
            ):
                planning_result = __repair_planning_output(
                    ctx=ctx,
                    planning_timeout_seconds=planning_timeout_seconds,
                    malformed_output=output_text,
                    reason="truncated_multistep_plan_after_minimal",
                )
                used_planning_repair_prompt = True
                continue

            if looks_like_truncated_multistep_plan(output_text, extracted_plan):
                ctx.orchestration_state.status = OrchestrationStatus.ABORTED
                ctx.orchestration_state.abort_reason = (
                    "Planning output collapsed a multi-step plan into a single step"
                )
                emit_phase_event(
                    ctx.orchestration_state,
                    ctx.emit_live,
                    level="ERROR",
                    phase="planning",
                    message="[ORCHESTRATION] Planning output was truncated into a single-step plan",
                    details={"reason": "truncated_multistep_plan_after_retry"},
                )
                ctx.task.status = TaskStatus.FAILED
                ctx.task.error_message = (
                    "Planning output collapsed a multi-step plan into a single "
                    "step after retry. The run was stopped to avoid a false success."
                )
                ctx.db.commit()
                if ctx.restore_workspace_snapshot_if_needed:
                    ctx.restore_workspace_snapshot_if_needed(
                        "truncated multi-step plan"
                    )
                return {
                    "status": "failed",
                    "reason": "truncated_multistep_plan_after_retry",
                }

            if extracted_plan is None:
                plan_shape = type(plan_data).__name__
                plan_keys = (
                    sorted(plan_data.keys()) if isinstance(plan_data, dict) else []
                )
                raise ValueError(
                    "Planning result is not a recognized list of steps "
                    f"(type={plan_shape}, keys={plan_keys}, preview={str(plan_data)[:240]})"
                )

            ctx.orchestration_state.plan = normalize_plan_with_live_logging(
                ctx.db,
                ctx.session_id,
                ctx.task_id,
                extracted_plan,
                ctx.orchestration_state.project_dir,
                ctx.logger,
                ctx.session_instance_id,
                "Planning output",
            )
            plan_verdict = ValidatorService.validate_plan(
                ctx.orchestration_state.plan,
                output_text=output_text,
                task_prompt=ctx.prompt,
                execution_profile=ctx.execution_profile,
                project_dir=ctx.orchestration_state.project_dir,
                title=ctx.task.title if ctx.task else None,
                description=ctx.task.description if ctx.task else None,
            )
            record_validation_verdict(
                ctx.db,
                ctx.session_id,
                ctx.task_id,
                ctx.orchestration_state,
                plan_verdict,
            )
            ctx.db.commit()
            try:
                append_orchestration_event(
                    project_dir=ctx.orchestration_state.project_dir,
                    session_id=ctx.session_id,
                    task_id=ctx.task_id,
                    event_type="phase_finished",
                    details={
                        "phase": "planning",
                        "status": plan_verdict.status,
                        "step_count": len(ctx.orchestration_state.plan),
                    },
                )
            except Exception:
                pass

            if not plan_verdict.accepted and not used_planning_repair_prompt:
                planning_result = __repair_planning_output(
                    ctx=ctx,
                    planning_timeout_seconds=planning_timeout_seconds,
                    malformed_output=output_text,
                    reason="plan_validation_failed: "
                    + "; ".join(plan_verdict.reasons[:3]),
                    rejection_reasons=plan_verdict.reasons,
                )
                used_planning_repair_prompt = True
                continue

            if not plan_verdict.accepted:
                ctx.orchestration_state.status = OrchestrationStatus.ABORTED
                ctx.orchestration_state.abort_reason = (
                    "Planning output failed validation: "
                    + "; ".join(plan_verdict.reasons[:3])
                )
                emit_phase_event(
                    ctx.orchestration_state,
                    ctx.emit_live,
                    level="ERROR",
                    phase="planning",
                    message="[ORCHESTRATION] Planning output failed validation",
                    details={
                        "reason": "planning_validation_failed",
                        "validation_status": plan_verdict.status,
                        "reasons": plan_verdict.reasons[:10],
                    },
                )
                ctx.task.status = TaskStatus.FAILED
                ctx.task.error_message = "Planning failed validation: " + "; ".join(
                    plan_verdict.reasons[:5]
                )
                ctx.db.commit()
                if ctx.restore_workspace_snapshot_if_needed:
                    ctx.restore_workspace_snapshot_if_needed(
                        "planning validation failure"
                    )
                return {"status": "failed", "reason": "planning_validation_failed"}

            ctx.logger.info(
                "[ORCHESTRATION] Generated %s steps in plan (using %s)",
                len(ctx.orchestration_state.plan),
                strategy_info,
            )
            emit_phase_event(
                ctx.orchestration_state,
                ctx.emit_live,
                level="INFO",
                phase="planning",
                message=f"[ORCHESTRATION] Generated {len(ctx.orchestration_state.plan)} steps in plan",
                details={
                    "steps": len(ctx.orchestration_state.plan),
                    "strategy": strategy_info,
                },
            )
            ctx.task.steps = json.dumps(ctx.orchestration_state.plan)
            ctx.task.current_step = 0
            ctx.db.commit()
            return {"status": "completed"}
    except workspace_violation_error_cls as exc:
        ctx.orchestration_state.status = OrchestrationStatus.ABORTED
        ctx.orchestration_state.abort_reason = f"Workspace isolation violation: {exc}"
        emit_phase_event(
            ctx.orchestration_state,
            ctx.emit_live,
            level="ERROR",
            phase="planning",
            message=f"[ORCHESTRATION] Planning output blocked: {exc}",
            details={"reason": "workspace_isolation_violation"},
        )
        ctx.task.status = TaskStatus.FAILED
        ctx.task.error_message = str(exc)
        ctx.db.commit()
        if ctx.restore_workspace_snapshot_if_needed:
            ctx.restore_workspace_snapshot_if_needed("workspace isolation violation")
        return {"status": "failed", "reason": "workspace_isolation_violation"}
    except Exception as exc:
        ctx.logger.error("[ORCHESTRATION] Failed to parse planning result: %s", exc)
        ctx.orchestration_state.status = OrchestrationStatus.ABORTED
        ctx.orchestration_state.abort_reason = f"Planning parse failed: {exc}"
        emit_phase_event(
            ctx.orchestration_state,
            ctx.emit_live,
            level="ERROR",
            phase="planning",
            message=f"[ORCHESTRATION] Failed to parse planning result: {exc}",
            details={"reason": "planning_parse_error"},
        )
        ctx.task.status = TaskStatus.FAILED
        ctx.task.error_message = str(exc)
        ctx.db.commit()
        if ctx.restore_workspace_snapshot_if_needed:
            ctx.restore_workspace_snapshot_if_needed("planning parse error")
        return {"status": "failed", "reason": "planning_parse_error"}


def __retry_with_minimal_prompt(
    *,
    ctx: OrchestrationRunContext,
    planning_timeout_seconds: int,
    reason: str,
) -> Dict[str, Any]:
    return PlannerService.retry_with_minimal_prompt(
        openclaw_service=ctx.openclaw_service,
        task_description=ctx.prompt,
        project_dir=ctx.orchestration_state.project_dir,
        timeout_seconds=planning_timeout_seconds,
        logger=ctx.logger,
        emit_live=ctx.emit_live,
        reason=reason,
    )


def __repair_planning_output(
    *,
    ctx: OrchestrationRunContext,
    planning_timeout_seconds: int,
    malformed_output: str,
    reason: str,
    rejection_reasons: list[str] | None = None,
) -> Dict[str, Any]:
    return PlannerService.repair_output(
        openclaw_service=ctx.openclaw_service,
        task_description=ctx.prompt,
        malformed_output=malformed_output,
        project_dir=ctx.orchestration_state.project_dir,
        timeout_seconds=planning_timeout_seconds,
        logger=ctx.logger,
        emit_live=ctx.emit_live,
        reason=reason,
        rejection_reasons=rejection_reasons,
    )


def __coerce_output_text(
    *,
    ctx: OrchestrationRunContext,
    planning_result: Any,
    output_result: Any,
    extract_structured_text: Callable[[Any], str],
) -> str:
    ctx.logger.info(
        "[ORCHESTRATION] Planning output type: %s, preview: %s",
        type(output_result),
        str(output_result)[:300],
    )
    output_text = extract_structured_text(output_result)
    if not output_text.strip() and isinstance(output_result, dict):
        output_text = json.dumps(output_result)
        ctx.logger.info(
            "[ORCHESTRATION] Structured text extraction empty; using full JSON"
        )
    if not output_text.strip():
        fallback_text = extract_structured_text(planning_result)
        if fallback_text.strip():
            output_text = fallback_text
            ctx.logger.info(
                "[ORCHESTRATION] Output field was empty; recovered planning text from full result payload"
            )
    if (
        not output_text.strip()
        and isinstance(planning_result, dict)
        and planning_result
    ):
        output_text = json.dumps(planning_result)
        ctx.logger.info(
            "[ORCHESTRATION] Using serialized full planning result payload as final fallback"
        )
    elif isinstance(output_result, str):
        ctx.logger.info("[ORCHESTRATION] Raw string response")
    else:
        ctx.logger.info(
            "[ORCHESTRATION] Structured text extracted from %s",
            type(output_result),
        )
    if isinstance(output_text, str):
        output_text = __strip_markdown_fences(output_text)
    return output_text


def __build_planning_prompt(
    *, prompt: str, orchestration_state: Any, execution_profile: str
) -> str:
    from app.services import PromptTemplates

    return PromptTemplates.build_planning_prompt(
        task_description=prompt,
        project_context=orchestration_state.project_context,
        workspace_root=str(orchestration_state.workspace_root),
        project_dir=str(orchestration_state.project_dir),
        execution_profile=execution_profile,
    )


def __strip_markdown_fences(output_text: str) -> str:
    import re

    markdown_pattern = r"^\s*```(?:json)?\s*|\s*```$"
    return re.sub(markdown_pattern, "", output_text.strip())
