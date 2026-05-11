"""Planning-phase orchestration flow."""

from __future__ import annotations

import asyncio
import json
from datetime import UTC, datetime
from typing import Any, Callable, Dict

from celery.exceptions import SoftTimeLimitExceeded

from app.models import TaskExecution, TaskStatus
from app.schemas.knowledge import KnowledgeContext
from app.services.orchestration.context_assembly import (
    assemble_planning_prompt,
    compress_orchestration_context,
)
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.events.telemetry import emit_phase_event
from app.services.orchestration.persistence import (
    append_orchestration_event,
    maybe_emit_divergence_detected,
    record_validation_verdict,
    set_session_alert,
    write_orchestration_state_snapshot,
)
from app.services.orchestration.planning.planner import (
    PlannerService,
    PlanningRepairBudgetExceeded,
    PlanningRepairOutputContractViolation,
)
from app.services.orchestration.policy import clamp_planning_timeout
from app.services.orchestration.task_rules import get_workflow_profile
from app.services.orchestration.workflow_profiles import get_workflow_phases
from app.services.orchestration.types import OrchestrationRunContext
from app.services.orchestration.validation.parsing import (
    extract_plan_steps_from_summary_text,
)
from app.services.orchestration.validation.validator import ValidatorService
from app.services.prompt_templates import OrchestrationStatus, estimate_token_count

# Circuit breaker: abort planning after this many consecutive validation failures
# to prevent infinite retry loops that hang the session.
from app.services.orchestration.phases.planning_support import (
    MAX_PLANNING_RETRIES,
    TRUNCATED_PLAN_REPAIR_REJECTION_REASON,
    _PlanningRetryState,
    _build_reasoning_artifact,
    _build_repair_rejection_reasons,
    _classify_planning_timeout_failure,
    _compress_project_context_for_planning,
    _emit_planning_diagnostics_contract_violation,
    _get_targeted_second_repair_reason,
    _is_repairable_malformed_shell_quoting_violation,
    _plan_contract_diagnostics,
    _semantic_codes_for_immediate_repair_issues,
    _should_repair_truncated_single_step_plan,
    _terminal_validation_failure_details,
    _truncated_multistep_collapse_diagnostics,
)


def _finalize_planning_terminal_failure(
    *,
    ctx: OrchestrationRunContext,
    failure_type: str,
    failure_reason: str,
    generate_failure_summary: bool = False,
) -> bool:
    completed_at = datetime.now(UTC)
    if ctx.task:
        ctx.task.status = TaskStatus.FAILED
        ctx.task.error_message = failure_reason
        ctx.task.completed_at = completed_at
    if ctx.session_task_link:
        ctx.session_task_link.status = TaskStatus.FAILED
        ctx.session_task_link.completed_at = completed_at
    if ctx.session:
        ctx.session.status = "paused"
        ctx.session.is_active = False
        ctx.session.paused_at = completed_at
        set_session_alert(ctx.session, "error", failure_reason[:2000])
    if ctx.task_execution_id:
        task_execution = (
            ctx.db.query(TaskExecution)
            .filter(TaskExecution.id == ctx.task_execution_id)
            .first()
        )
        if task_execution:
            task_execution.status = TaskStatus.FAILED
            task_execution.completed_at = task_execution.completed_at or completed_at
    ctx.db.commit()
    if generate_failure_summary:
        try:
            from app.services.session.replan_service import (
                get_or_generate_failure_summary,
            )

            get_or_generate_failure_summary(ctx.db, ctx.session_id)
        except Exception as summary_exc:
            ctx.logger.debug(
                "[ORCHESTRATION] Failed to create/update failure summary for session=%s: %s",
                ctx.session_id,
                summary_exc,
            )

    knowledge_recorded = False
    try:
        from app.services.orchestration.phases.failure_flow import (
            record_failure_knowledge_for_stopped_session,
        )

        knowledge_recorded = bool(
            record_failure_knowledge_for_stopped_session(
                db=ctx.db,
                session_id=ctx.session_id,
                task_id=ctx.task_id,
                failure_reason=failure_type,
                logger=ctx.logger,
            )
        )
    except Exception as knowledge_exc:
        ctx.logger.warning(
            "[ORCHESTRATION] session_id=%s task_id=%s failure_type=%s "
            "handle_task_failure_called=False knowledge_recorded=False error=%s",
            ctx.session_id,
            ctx.task_id,
            failure_type,
            knowledge_exc,
        )
        return False

    ctx.logger.warning(
        "[ORCHESTRATION] session_id=%s task_id=%s failure_type=%s "
        "handle_task_failure_called=False knowledge_recorded=%s",
        ctx.session_id,
        ctx.task_id,
        failure_type,
        knowledge_recorded,
    )
    return knowledge_recorded


def _finalize_planning_timeout_failure(
    *,
    ctx: OrchestrationRunContext,
    failure_type: str,
    failure_reason: str,
) -> bool:
    return _finalize_planning_terminal_failure(
        ctx=ctx,
        failure_type=failure_type,
        failure_reason=failure_reason,
        generate_failure_summary=True,
    )


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
    planning_phase_event = None
    try:
        planning_phase_event = append_orchestration_event(
            project_dir=ctx.orchestration_state.project_dir,
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            event_type=EventType.PHASE_STARTED,
            details={"phase": "planning"},
        )
        write_orchestration_state_snapshot(
            project_dir=ctx.orchestration_state.project_dir,
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            orchestration_state=ctx.orchestration_state,
            trigger="phase_started",
            related_event_id=planning_phase_event.get("event_id"),
        )
    except Exception as exc:
        ctx.logger.debug(
            "[ORCHESTRATION] Failed to persist planning phase start event/snapshot: %s",
            exc,
        )

    ctx.workflow_profile = get_workflow_profile(
        ctx.execution_profile,
        getattr(ctx.task, "title", None),
        getattr(ctx.task, "description", None),
    )
    ctx.workflow_phases = get_workflow_phases(ctx.workflow_profile)
    ctx.workspace_has_existing_files = bool(workspace_review.get("has_existing_files"))
    if len(ctx.orchestration_state.project_context or "") > 3500:
        compressed_context = _compress_project_context_for_planning(
            ctx.orchestration_state
        )
        if compressed_context != (ctx.orchestration_state.project_context or ""):
            ctx.orchestration_state.project_context = compressed_context
            ctx.logger.info(
                "[ORCHESTRATION] Compressed oversized planning context before first prompt (%d chars)",
                len(compressed_context),
            )
    planning_knowledge_ctx = _retrieve_knowledge(
        ctx, trigger_phase="planning", knowledge_types=["format_guide", "task_example"]
    )
    planning_prompt = (
        assemble_planning_prompt(
            ctx,
            workspace_review,
            knowledge_context=(
                planning_knowledge_ctx
                if planning_knowledge_ctx and planning_knowledge_ctx.retrieved_items
                else None
            ),
        )
        if ctx.runtime_service
        else None
    )
    runtime_metadata = (
        ctx.runtime_service.get_backend_metadata()
        if ctx.runtime_service and hasattr(ctx.runtime_service, "get_backend_metadata")
        else {}
    )
    prompt_profile = PlannerService.select_prompt_profile(
        runtime_metadata.get("backend"),
        runtime_metadata.get("model_family"),
    )
    if planning_prompt:
        planning_prompt = PlannerService.apply_prompt_profile(
            planning_prompt,
            prompt_profile=prompt_profile,
        )
    if planning_knowledge_ctx:
        _log_knowledge_usage(
            ctx, planning_knowledge_ctx, used_in_prompt=bool(planning_prompt)
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
        # Context compression: if mid-execution state exists, replace the full
        # project_context with a compact snapshot so the planner can recover
        # without blowing the context window again.
        if getattr(ctx.orchestration_state, "debug_attempts", None) or getattr(
            ctx.orchestration_state, "completed_steps", None
        ):
            _compressed = compress_orchestration_context(ctx.orchestration_state)
            if _compressed:
                ctx.orchestration_state.project_context = _compressed
                ctx.logger.info(
                    "[ORCHESTRATION] Context compressed for dense replanning (%d chars)",
                    len(_compressed),
                )
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
            runtime_service=ctx.runtime_service,
            task_description=ctx.prompt,
            project_dir=ctx.orchestration_state.project_dir,
            timeout_seconds=planning_timeout_seconds,
            logger=ctx.logger,
            emit_live=ctx.emit_live,
            reason="dense_planning_context",
            prompt_profile=prompt_profile,
            workflow_profile=ctx.workflow_profile,
            workflow_phases=getattr(ctx, "workflow_phases", []),
            workspace_has_existing_files=getattr(
                ctx, "workspace_has_existing_files", False
            ),
        )
    else:
        planning_result = asyncio.run(
            PlannerService._execute_task_with_planning_lock(
                ctx.runtime_service,
                planning_prompt,
                timeout_seconds=planning_timeout_seconds,
                reuse_task_session=False,
                diagnostic_label="PLANNING",
                diagnostic_metadata={
                    "session_id": ctx.session_id,
                    "task_id": ctx.task_id,
                    "task_execution_id": ctx.task_execution_id,
                    "workflow_profile": ctx.workflow_profile,
                    "prompt_profile": prompt_profile,
                    "planning_prompt_tokens": planning_prompt_tokens,
                    "planning_attempt": "initial",
                },
            )
        )

    emit_phase_event(
        ctx.orchestration_state,
        ctx.emit_live,
        level="INFO",
        phase="planning",
        message="[ORCHESTRATION] Planning response received; parsing and validating plan",
        details={"phase_state": "planning_response_received"},
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
        if used_minimal_planning_prompt:
            if PlannerService.is_openclaw_lock_contention(planning_result):
                failure_type = "planning_openclaw_lock_contention"
                failure_reason = (
                    "OpenClaw planning failed because the local session file was locked"
                )
                restore_reason = "planning OpenClaw lock contention"
            else:
                failure_type = "planning_timeout"
                failure_reason = (
                    "Planning timed out or exceeded context after "
                    f"{planning_timeout_seconds}s"
                )
                restore_reason = "planning timeout or context overflow"
            ctx.logger.error(
                "[ORCHESTRATION] Planning timed out before a salvageable response was produced: %s",
                failure_reason,
            )
            ctx.orchestration_state.status = OrchestrationStatus.ABORTED
            ctx.orchestration_state.abort_reason = failure_reason
            emit_phase_event(
                ctx.orchestration_state,
                ctx.emit_live,
                level="ERROR",
                phase="planning",
                message=(
                    "[ORCHESTRATION] Planning terminalized before a valid plan "
                    f"was produced: {failure_reason}"
                ),
                details={"reason": failure_type},
            )
            _finalize_planning_timeout_failure(
                ctx=ctx,
                failure_type=failure_type,
                failure_reason=failure_reason,
            )
            if ctx.restore_workspace_snapshot_if_needed:
                ctx.restore_workspace_snapshot_if_needed(restore_reason)
            return {"status": "failed", "reason": failure_type}
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
            runtime_service=ctx.runtime_service,
            task_description=ctx.prompt,
            project_dir=ctx.orchestration_state.project_dir,
            timeout_seconds=planning_timeout_seconds,
            logger=ctx.logger,
            emit_live=ctx.emit_live,
            reason=(planning_result.get("error") or initial_output_text),
            prompt_profile=prompt_profile,
            workflow_profile=ctx.workflow_profile,
            workflow_phases=getattr(ctx, "workflow_phases", []),
            workspace_has_existing_files=getattr(
                ctx, "workspace_has_existing_files", False
            ),
        )
        used_minimal_planning_prompt = True

    retry_state = _PlanningRetryState()
    try:
        while True:
            # Circuit breaker: abort after too many consecutive validation failures
            if retry_state.circuit_open:
                ctx.orchestration_state.status = OrchestrationStatus.ABORTED
                ctx.orchestration_state.abort_reason = (
                    f"Planning failed {MAX_PLANNING_RETRIES} consecutive times; "
                    "circuit breaker opened to prevent infinite retry loop"
                )
                emit_phase_event(
                    ctx.orchestration_state,
                    ctx.emit_live,
                    level="ERROR",
                    phase="planning",
                    message=(
                        f"[ORCHESTRATION] Planning circuit breaker opened after "
                        f"{MAX_PLANNING_RETRIES} consecutive failures"
                    ),
                    details={"reason": "planning_circuit_breaker_opened"},
                )
                _finalize_planning_terminal_failure(
                    ctx=ctx,
                    failure_type="planning_circuit_breaker_opened",
                    failure_reason=(
                        f"Planning failed {MAX_PLANNING_RETRIES} consecutive times. "
                        "The agent was unable to produce a valid execution plan."
                    ),
                )
                if ctx.restore_workspace_snapshot_if_needed:
                    ctx.restore_workspace_snapshot_if_needed(
                        "planning circuit breaker opened"
                    )
                return {"status": "failed", "reason": "planning_circuit_breaker_opened"}

            output_result = planning_result.get("output", {})
            ctx.logger.info(
                "[ORCHESTRATION] Attempting JSON parse (failure_count=%d)",
                retry_state.consecutive_failures,
            )
            output_text = __coerce_output_text(
                ctx=ctx,
                planning_result=planning_result,
                output_result=output_result,
                extract_structured_text=extract_structured_text,
            )

            success, plan_data, strategy_info = ctx.error_handler.attempt_json_parsing(
                output_text, context="planning"
            )
            if not success:
                ctx.logger.warning(
                    "[ORCHESTRATION] JSON parse failed: %s", strategy_info
                )
                extracted_summary_plan = extract_plan_steps_from_summary_text(
                    output_text
                )
                if extracted_summary_plan is not None:
                    strategy_info = (
                        "planning_contract_violation: multi_step_prose_summary"
                    )
                    ctx.logger.warning(
                        "[ORCHESTRATION] Planning output was multi-step prose instead of JSON; routing to fallback/repair"
                    )
                workspace_plan = PlannerService.maybe_load_workspace_plan(
                    output_text=output_text,
                    project_dir=ctx.orchestration_state.project_dir,
                    logger=ctx.logger,
                )
                if workspace_plan is not None:
                    success = True
                    plan_data = workspace_plan
                    strategy_info = "Recovered plan from workspace plan.json"
                    ctx.logger.info(
                        "[ORCHESTRATION] Planning output referenced plan.json; using workspace file instead of strict JSON retry"
                    )

            if (
                PlannerService.should_retry_with_minimal_prompt(
                    planning_result, output_text
                )
                and not success
                and not PlannerService.looks_salvageable_planning_output(output_text)
            ):
                if PlannerService.is_openclaw_lock_contention(planning_result):
                    raise TimeoutError(
                        "OpenClaw planning failed because the local session file "
                        "was locked"
                    )
                raise TimeoutError(
                    f"Planning timed out or exceeded context after {planning_timeout_seconds}s"
                )

            if not success and not retry_state.minimal_prompt_used:
                contract_violations = (
                    PlannerService.describe_planning_contract_violations(
                        output_text=output_text,
                        parse_success=False,
                        strategy_info=strategy_info,
                    )
                )
                ctx.logger.warning(
                    "[ORCHESTRATION] Planning contract violation before minimal retry: %s",
                    "; ".join(contract_violations),
                )
                _emit_planning_diagnostics_contract_violation(
                    ctx,
                    reason="json_parse_failed_before_minimal",
                    contract_violations=contract_violations,
                    output_text=output_text,
                    strategy_info=strategy_info,
                )
                ctx.logger.info(
                    "[ORCHESTRATION] JSON parse failed, switching to minimal prompt"
                )
                planning_result = __retry_with_minimal_prompt(
                    ctx=ctx,
                    planning_timeout_seconds=planning_timeout_seconds,
                    reason=f"json_parse_failed: {output_text[:240]}",
                    prompt_profile=prompt_profile,
                )
                retry_state.minimal_prompt_used = True
                retry_state.consecutive_failures += 1
                continue

            if not success and not retry_state.repair_prompt_used:
                contract_violations = (
                    PlannerService.describe_planning_contract_violations(
                        output_text=output_text,
                        parse_success=False,
                        strategy_info=strategy_info,
                    )
                )
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
                        "contract_violations": contract_violations[:8],
                    },
                )
                ctx.logger.warning(
                    "[ORCHESTRATION] Planning contract violation before repair: %s",
                    "; ".join(contract_violations),
                )
                _emit_planning_diagnostics_contract_violation(
                    ctx,
                    reason="json_parse_failed_after_minimal",
                    contract_violations=contract_violations,
                    output_text=output_text,
                    strategy_info=strategy_info,
                )
                ctx.logger.info(
                    "[ORCHESTRATION] Calling repair pass for planning output"
                )
                retry_state.last_repair_reason = (
                    f"json_parse_failed_after_minimal: {strategy_info}"
                )
                planning_result = __repair_planning_output(
                    ctx=ctx,
                    planning_timeout_seconds=planning_timeout_seconds,
                    malformed_output=output_text,
                    reason=f"json_parse_failed_after_minimal: {strategy_info}",
                    prompt_profile=prompt_profile,
                )
                retry_state.repair_prompt_used = True
                retry_state.consecutive_failures += 1
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
                _finalize_planning_terminal_failure(
                    ctx=ctx,
                    failure_type="planning_json_error",
                    failure_reason=(
                        f"Planning JSON parse failed: {strategy_info}. "
                        f"Raw output: {output_text[:500]}"
                    ),
                )
                if ctx.restore_workspace_snapshot_if_needed:
                    ctx.restore_workspace_snapshot_if_needed(
                        "planning JSON parse failure"
                    )
                return {"status": "failed", "reason": "planning_json_error"}

            extracted_plan = extract_plan_steps(plan_data)
            if extracted_plan is None and not retry_state.minimal_prompt_used:
                contract_violations = (
                    PlannerService.describe_planning_contract_violations(
                        output_text=output_text,
                        parse_success=True,
                        strategy_info="unexpected_plan_shape",
                        plan_data=plan_data,
                    )
                )
                ctx.logger.warning(
                    "[ORCHESTRATION] Planning contract violation before minimal retry: %s",
                    "; ".join(contract_violations),
                )
                _emit_planning_diagnostics_contract_violation(
                    ctx,
                    reason="unexpected_plan_shape_before_minimal",
                    contract_violations=contract_violations,
                    output_text=output_text,
                    strategy_info="unexpected_plan_shape",
                )
                ctx.logger.info(
                    "[ORCHESTRATION] Plan extraction failed, switching to minimal prompt"
                )
                planning_result = __retry_with_minimal_prompt(
                    ctx=ctx,
                    planning_timeout_seconds=planning_timeout_seconds,
                    reason=f"unexpected_plan_shape: {str(plan_data)[:240]}",
                    prompt_profile=prompt_profile,
                )
                retry_state.minimal_prompt_used = True
                retry_state.consecutive_failures += 1
                continue

            if extracted_plan is None and not retry_state.repair_prompt_used:
                contract_violations = (
                    PlannerService.describe_planning_contract_violations(
                        output_text=output_text,
                        parse_success=True,
                        strategy_info="unexpected_plan_shape_after_minimal",
                        plan_data=plan_data,
                    )
                )
                ctx.logger.warning(
                    "[ORCHESTRATION] Planning contract violation before repair: %s",
                    "; ".join(contract_violations),
                )
                _emit_planning_diagnostics_contract_violation(
                    ctx,
                    reason="unexpected_plan_shape_after_minimal",
                    contract_violations=contract_violations,
                    output_text=output_text,
                    strategy_info="unexpected_plan_shape_after_minimal",
                )
                ctx.logger.info(
                    "[ORCHESTRATION] Plan extraction failed, calling repair"
                )
                retry_state.last_repair_reason = "unexpected_plan_shape_after_minimal"
                planning_result = __repair_planning_output(
                    ctx=ctx,
                    planning_timeout_seconds=planning_timeout_seconds,
                    malformed_output=output_text,
                    reason="unexpected_plan_shape_after_minimal",
                    prompt_profile=prompt_profile,
                )
                retry_state.repair_prompt_used = True
                retry_state.consecutive_failures += 1
                continue

            if (
                looks_like_truncated_multistep_plan(output_text, extracted_plan)
                and not retry_state.minimal_prompt_used
            ):
                truncated_diagnostics = _truncated_multistep_collapse_diagnostics(
                    output_text=output_text,
                    extracted_plan=extracted_plan,
                    repair_stage=(
                        "after_first_repair"
                        if retry_state.repair_prompt_used
                        else "before_first_repair"
                    ),
                )
                if _should_repair_truncated_single_step_plan(
                    prompt_profile=prompt_profile,
                    extracted_plan=extracted_plan,
                    execution_profile=ctx.execution_profile,
                ):
                    emit_phase_event(
                        ctx.orchestration_state,
                        ctx.emit_live,
                        level="WARN",
                        phase="planning",
                        message=(
                            "[ORCHESTRATION] Planning output collapsed into a "
                            "single step on the local model path; starting a "
                            "repair pass instead of executing it as-is"
                        ),
                        details={
                            "reason": "truncated_multistep_plan_repair_requested",
                            "model_profile": prompt_profile,
                        },
                    )
                    _emit_planning_diagnostics_contract_violation(
                        ctx,
                        reason="truncated_multistep_plan_detected",
                        contract_violations=[
                            "truncated multi-step plan collapsed into a single step"
                        ],
                        contract_diagnostics=truncated_diagnostics,
                        output_text=output_text,
                        strategy_info="truncated_multistep_plan_repair_requested",
                    )
                    retry_state.last_repair_reason = "truncated_multistep_plan_detected"
                    planning_result = __repair_planning_output(
                        ctx=ctx,
                        planning_timeout_seconds=planning_timeout_seconds,
                        malformed_output=output_text,
                        reason="truncated_multistep_plan_detected",
                        rejection_reasons=_build_repair_rejection_reasons(
                            [TRUNCATED_PLAN_REPAIR_REJECTION_REASON],
                            truncated_diagnostics,
                        ),
                        prompt_profile=prompt_profile,
                    )
                    retry_state.repair_prompt_used = True
                    retry_state.consecutive_failures += 1
                    continue
                else:
                    _emit_planning_diagnostics_contract_violation(
                        ctx,
                        reason="truncated_multistep_plan_detected",
                        contract_violations=[
                            "truncated multi-step plan collapsed into a single step"
                        ],
                        contract_diagnostics=truncated_diagnostics,
                        output_text=output_text,
                        strategy_info="truncated_multistep_plan_minimal_retry",
                    )
                    planning_result = __retry_with_minimal_prompt(
                        ctx=ctx,
                        planning_timeout_seconds=planning_timeout_seconds,
                        reason="truncated_multistep_plan_detected",
                        prompt_profile=prompt_profile,
                    )
                    retry_state.minimal_prompt_used = True
                    retry_state.consecutive_failures += 1
                    continue

            if (
                looks_like_truncated_multistep_plan(output_text, extracted_plan)
                and not retry_state.repair_prompt_used
            ):
                truncated_diagnostics = _truncated_multistep_collapse_diagnostics(
                    output_text=output_text,
                    extracted_plan=extracted_plan,
                    repair_stage="before_first_repair",
                )
                _emit_planning_diagnostics_contract_violation(
                    ctx,
                    reason="truncated_multistep_plan_after_minimal",
                    contract_violations=[
                        "truncated multi-step plan collapsed into a single step"
                    ],
                    contract_diagnostics=truncated_diagnostics,
                    output_text=output_text,
                    strategy_info="truncated_multistep_plan_after_minimal",
                )
                retry_state.last_repair_reason = (
                    "truncated_multistep_plan_after_minimal"
                )
                planning_result = __repair_planning_output(
                    ctx=ctx,
                    planning_timeout_seconds=planning_timeout_seconds,
                    malformed_output=output_text,
                    reason="truncated_multistep_plan_after_minimal",
                    rejection_reasons=_build_repair_rejection_reasons(
                        [TRUNCATED_PLAN_REPAIR_REJECTION_REASON],
                        truncated_diagnostics,
                    ),
                    prompt_profile=prompt_profile,
                )
                retry_state.repair_prompt_used = True
                retry_state.consecutive_failures += 1
                continue

            if looks_like_truncated_multistep_plan(output_text, extracted_plan):
                truncated_diagnostics = _truncated_multistep_collapse_diagnostics(
                    output_text=output_text,
                    extracted_plan=extracted_plan,
                    repair_stage="after_first_repair",
                )
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
                _emit_planning_diagnostics_contract_violation(
                    ctx,
                    reason="truncated_multistep_plan_after_retry",
                    contract_violations=[
                        "truncated multi-step plan collapsed into a single step"
                    ],
                    contract_diagnostics=truncated_diagnostics,
                    output_text=output_text,
                    strategy_info="truncated_multistep_plan_after_retry",
                )
                _finalize_planning_terminal_failure(
                    ctx=ctx,
                    failure_type="truncated_multistep_plan_after_retry",
                    failure_reason=(
                        "Planning output collapsed a multi-step plan into a single "
                        "step after retry. The run was stopped to avoid a false success."
                    ),
                )
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

            sanitized_plan = PlannerService.sanitize_common_plan_issues(extracted_plan)
            try:
                ctx.orchestration_state.plan = normalize_plan_with_live_logging(
                    ctx.db,
                    ctx.session_id,
                    ctx.task_id,
                    sanitized_plan,
                    ctx.orchestration_state.project_dir,
                    ctx.logger,
                    ctx.session_instance_id,
                    "Planning output",
                )
            except workspace_violation_error_cls as exc:
                if _is_repairable_malformed_shell_quoting_violation(exc):
                    second_repair_reason = None
                    if retry_state.repair_prompt_used:
                        second_repair_reason = _get_targeted_second_repair_reason(
                            retry_state=retry_state,
                            malformed_shell_quoting_violation=True,
                        )
                        if not second_repair_reason or second_repair_reason.cap_used:
                            raise
                    contract_violations = [
                        "Plan contains malformed shell quoting in runnable commands"
                    ]
                    repair_reason = (
                        second_repair_reason.retry_reason
                        if second_repair_reason
                        else "malformed_shell_quoting"
                    )
                    rejection_reasons = (
                        [second_repair_reason.rejection_text]
                        if second_repair_reason
                        else [
                            "Malformed shell quoting: emit one valid shell command "
                            "string; avoid unmatched quotes, mixed quote escaping, "
                            "and python -c snippets with nested quotes"
                        ]
                    )
                    retry_state.last_repair_reason = repair_reason
                    _emit_planning_diagnostics_contract_violation(
                        ctx,
                        reason=repair_reason,
                        contract_violations=contract_violations,
                        semantic_violation_codes=["malformed_shell_quoting"],
                        output_text=output_text,
                        strategy_info="workspace_guard_malformed_shell_quoting",
                    )
                    planning_result = __repair_planning_output(
                        ctx=ctx,
                        planning_timeout_seconds=planning_timeout_seconds,
                        malformed_output=output_text,
                        reason=f"{repair_reason}: " + str(exc)[:300],
                        rejection_reasons=rejection_reasons,
                        prompt_profile=prompt_profile,
                    )
                    retry_state.repair_prompt_used = True
                    if second_repair_reason:
                        setattr(
                            retry_state,
                            second_repair_reason.cap_attribute,
                            True,
                        )
                    retry_state.consecutive_failures += 1
                    continue
                raise
            immediate_repair_issues = PlannerService.find_immediate_repair_step_issues(
                ctx.orchestration_state.plan
            )
            blocking_issue_keys = (
                "non_runnable_steps",
                "background_process_steps",
                "placeholder_only_steps",
                "weak_verification_steps",
            )
            blocking_repair_issues = {
                key: value
                for key, value in immediate_repair_issues.items()
                if key in blocking_issue_keys and value
            }
            if blocking_repair_issues and not retry_state.repair_prompt_used:
                contract_violations = (
                    PlannerService.describe_planning_contract_violations(
                        output_text=output_text,
                        parse_success=True,
                        strategy_info="plan_contains_immediate_repair_issues",
                        plan_data=plan_data,
                        extracted_plan=ctx.orchestration_state.plan,
                        immediate_repair_issues=blocking_repair_issues,
                    )
                )
                issue_fragments = []
                if blocking_repair_issues.get("non_runnable_steps"):
                    issue_fragments.append(
                        "non-runnable pseudo-commands in steps "
                        f"{blocking_repair_issues['non_runnable_steps'][:5]}"
                    )
                if blocking_repair_issues.get("background_process_steps"):
                    issue_fragments.append(
                        "background processes in steps "
                        f"{blocking_repair_issues['background_process_steps'][:5]}"
                    )
                if blocking_repair_issues.get("placeholder_only_steps"):
                    issue_fragments.append(
                        "placeholder-only implementation steps in steps "
                        f"{blocking_repair_issues['placeholder_only_steps'][:5]}"
                    )
                if blocking_repair_issues.get("weak_verification_steps"):
                    issue_fragments.append(
                        "weak verification commands in steps "
                        f"{blocking_repair_issues['weak_verification_steps'][:5]}"
                    )
                retry_state.last_repair_reason = "plan_contains_immediate_repair_issues"
                semantic_violation_codes = _semantic_codes_for_immediate_repair_issues(
                    blocking_repair_issues
                )
                emit_phase_event(
                    ctx.orchestration_state,
                    ctx.emit_live,
                    level="WARN",
                    phase="planning",
                    message="[ORCHESTRATION] Planning output violated the runnable-step contract; starting repair pass",
                    details={
                        "reason": "plan_contains_immediate_repair_issues",
                        "contract_violations": contract_violations[:8],
                        "semantic_violation_codes": semantic_violation_codes,
                    },
                )
                ctx.logger.warning(
                    "[ORCHESTRATION] Planning contract violation before repair: %s",
                    "; ".join(contract_violations),
                )
                _emit_planning_diagnostics_contract_violation(
                    ctx,
                    reason="plan_contains_immediate_repair_issues",
                    contract_violations=contract_violations,
                    semantic_violation_codes=semantic_violation_codes,
                    output_text=output_text,
                    strategy_info="plan_contains_immediate_repair_issues",
                )
                planning_result = __repair_planning_output(
                    ctx=ctx,
                    planning_timeout_seconds=planning_timeout_seconds,
                    malformed_output=output_text,
                    reason="plan_contains_immediate_repair_issues: "
                    + "; ".join(issue_fragments),
                    rejection_reasons=issue_fragments,
                    prompt_profile=prompt_profile,
                )
                retry_state.repair_prompt_used = True
                retry_state.consecutive_failures += 1
                continue
            if blocking_repair_issues:
                second_repair_reason = _get_targeted_second_repair_reason(
                    retry_state=retry_state,
                    blocking_repair_issues=blocking_repair_issues,
                )
                if second_repair_reason and not second_repair_reason.cap_used:
                    issue_fragments = [second_repair_reason.rejection_text]
                    contract_violations = (
                        PlannerService.describe_planning_contract_violations(
                            output_text=output_text,
                            parse_success=True,
                            strategy_info=second_repair_reason.retry_reason,
                            plan_data=plan_data,
                            extracted_plan=ctx.orchestration_state.plan,
                            immediate_repair_issues=blocking_repair_issues,
                        )
                    )
                    emit_phase_event(
                        ctx.orchestration_state,
                        ctx.emit_live,
                        level="WARN",
                        phase="planning",
                        message=(
                            "[ORCHESTRATION] Planning repair still had a targeted "
                            "blocking issue; starting one targeted second repair pass"
                        ),
                        details={
                            "reason": second_repair_reason.event_reason,
                            second_repair_reason.issue_key: (
                                second_repair_reason.step_numbers
                            ),
                            "contract_violations": contract_violations[:8],
                            "repair_attempts": retry_state.consecutive_failures + 1,
                        },
                    )
                    ctx.logger.warning(
                        "[ORCHESTRATION] Planning repair still had %s in steps %s; "
                        "starting one targeted second repair pass",
                        second_repair_reason.issue_label,
                        second_repair_reason.step_numbers,
                    )
                    _emit_planning_diagnostics_contract_violation(
                        ctx,
                        reason=second_repair_reason.event_reason,
                        contract_violations=contract_violations,
                        semantic_violation_codes=[
                            second_repair_reason.semantic_violation_code
                        ],
                        output_text=output_text,
                        strategy_info=second_repair_reason.event_reason,
                    )
                    retry_state.last_repair_reason = second_repair_reason.event_reason
                    planning_result = __repair_planning_output(
                        ctx=ctx,
                        planning_timeout_seconds=planning_timeout_seconds,
                        malformed_output=output_text,
                        reason=f"{second_repair_reason.retry_reason}: "
                        + "; ".join(issue_fragments),
                        rejection_reasons=issue_fragments,
                        prompt_profile=prompt_profile,
                    )
                    setattr(
                        retry_state,
                        second_repair_reason.cap_attribute,
                        True,
                    )
                    retry_state.consecutive_failures += 1
                    continue

                ctx.orchestration_state.status = OrchestrationStatus.ABORTED
                ctx.orchestration_state.abort_reason = "Planning repair still produced non-runnable or long-running commands"
                failure_reason = (
                    "Planning repair still produced invalid commands: "
                    + "; ".join(
                        f"{key}={value[:5]}"
                        for key, value in blocking_repair_issues.items()
                    )
                )
                _finalize_planning_terminal_failure(
                    ctx=ctx,
                    failure_type="planning_invalid_commands_after_repair",
                    failure_reason=failure_reason,
                )
                return {
                    "status": "failed",
                    "reason": "planning_invalid_commands_after_repair",
                }
            plan_verdict = ValidatorService.validate_plan(
                ctx.orchestration_state.plan,
                output_text=output_text,
                task_prompt=ctx.prompt,
                execution_profile=ctx.execution_profile,
                project_dir=ctx.orchestration_state.project_dir,
                title=ctx.task.title if ctx.task else None,
                description=ctx.task.description if ctx.task else None,
                validation_severity=ctx.validation_severity,
                workflow_profile=ctx.workflow_profile,
            )
            record_validation_verdict(
                ctx.db,
                ctx.session_id,
                ctx.task_id,
                ctx.orchestration_state,
                plan_verdict.verdict,
                parent_event_id=(planning_phase_event or {}).get("event_id"),
            )
            if not plan_verdict.accepted or plan_verdict.warning:
                try:
                    maybe_emit_divergence_detected(
                        project_dir=ctx.orchestration_state.project_dir,
                        session_id=ctx.session_id,
                        task_id=ctx.task_id,
                        parent_event_id=(planning_phase_event or {}).get("event_id"),
                    )
                except Exception as exc:
                    ctx.logger.debug(
                        "[ORCHESTRATION] Failed to emit planning divergence signal: %s",
                        exc,
                    )
            ctx.db.commit()
            schema_validation = (plan_verdict.details or {}).get("plan_schema") or {}
            if not schema_validation.get("valid", True):
                ctx.logger.warning(
                    "[ORCHESTRATION] Planning schema mismatch session_id=%s task_id=%s "
                    "errors=%s details=%s",
                    ctx.session_id,
                    ctx.task_id,
                    schema_validation.get("errors", []),
                    schema_validation.get("details", {}),
                )
            try:
                phase_finished_event = append_orchestration_event(
                    project_dir=ctx.orchestration_state.project_dir,
                    session_id=ctx.session_id,
                    task_id=ctx.task_id,
                    event_type=EventType.PHASE_FINISHED,
                    parent_event_id=(planning_phase_event or {}).get("event_id"),
                    details={
                        "phase": "planning",
                        "status": plan_verdict.status,
                        "step_count": len(ctx.orchestration_state.plan),
                    },
                )
                write_orchestration_state_snapshot(
                    project_dir=ctx.orchestration_state.project_dir,
                    session_id=ctx.session_id,
                    task_id=ctx.task_id,
                    orchestration_state=ctx.orchestration_state,
                    trigger="phase_finished",
                    related_event_id=phase_finished_event.get("event_id"),
                )
            except Exception as exc:
                ctx.logger.debug(
                    "[ORCHESTRATION] Failed to persist planning phase finish event/snapshot: %s",
                    exc,
                )

            if not plan_verdict.accepted and not retry_state.repair_prompt_used:
                contract_diagnostics = _plan_contract_diagnostics(plan_verdict.details)
                semantic_violation_codes = list(
                    (plan_verdict.details or {}).get("semantic_violation_codes") or []
                )
                _emit_planning_diagnostics_contract_violation(
                    ctx,
                    reason="plan_validation_failed",
                    contract_violations=plan_verdict.reasons,
                    semantic_violation_codes=semantic_violation_codes,
                    contract_diagnostics=contract_diagnostics,
                    output_text=output_text,
                    strategy_info="plan_validation_failed",
                )
                ctx.logger.warning(
                    "[ORCHESTRATION] Plan validation failed, calling repair (failure_count=%d)",
                    retry_state.consecutive_failures,
                )
                validation_knowledge_ctx = _retrieve_knowledge(
                    ctx,
                    trigger_phase="validation",
                    knowledge_types=["format_guide", "debug_case"],
                )
                if validation_knowledge_ctx:
                    _log_knowledge_usage(
                        ctx, validation_knowledge_ctx, used_in_prompt=True
                    )
                retry_state.last_repair_reason = "plan_validation_failed"
                planning_result = __repair_planning_output(
                    ctx=ctx,
                    planning_timeout_seconds=planning_timeout_seconds,
                    malformed_output=output_text,
                    reason="plan_validation_failed: "
                    + "; ".join(plan_verdict.reasons[:3]),
                    rejection_reasons=_build_repair_rejection_reasons(
                        plan_verdict.reasons,
                        plan_verdict.details,
                    ),
                    prompt_profile=prompt_profile,
                    knowledge_context=(
                        validation_knowledge_ctx
                        if (
                            validation_knowledge_ctx
                            and validation_knowledge_ctx.retrieved_items
                        )
                        else None
                    ),
                )
                retry_state.repair_prompt_used = True
                retry_state.consecutive_failures += 1
                continue

            if not plan_verdict.accepted:
                second_repair_reason = _get_targeted_second_repair_reason(
                    retry_state=retry_state,
                    plan_verdict=plan_verdict,
                )
                if second_repair_reason and not second_repair_reason.cap_used:
                    issue_fragments = [second_repair_reason.rejection_text]
                    contract_diagnostics = _plan_contract_diagnostics(
                        plan_verdict.details
                    )
                    _emit_planning_diagnostics_contract_violation(
                        ctx,
                        reason=second_repair_reason.event_reason,
                        contract_violations=plan_verdict.reasons,
                        semantic_violation_codes=[
                            second_repair_reason.semantic_violation_code
                        ],
                        contract_diagnostics=contract_diagnostics,
                        output_text=output_text,
                        strategy_info=second_repair_reason.event_reason,
                    )
                    emit_phase_event(
                        ctx.orchestration_state,
                        ctx.emit_live,
                        level="WARN",
                        phase="planning",
                        message=(
                            "[ORCHESTRATION] Planning repair still missed "
                            "verification; starting one targeted second repair pass"
                        ),
                        details={
                            "reason": second_repair_reason.event_reason,
                            second_repair_reason.issue_key: (
                                second_repair_reason.step_numbers
                            ),
                            "validation_reasons": list(plan_verdict.reasons or [])[:5],
                            "repair_attempts": retry_state.consecutive_failures + 1,
                        },
                    )
                    ctx.logger.warning(
                        "[ORCHESTRATION] Planning repair still missed verification "
                        "in steps %s; starting one targeted second repair pass",
                        second_repair_reason.step_numbers,
                    )
                    retry_state.last_repair_reason = second_repair_reason.event_reason
                    planning_result = __repair_planning_output(
                        ctx=ctx,
                        planning_timeout_seconds=planning_timeout_seconds,
                        malformed_output=output_text,
                        reason=f"{second_repair_reason.retry_reason}: "
                        + "; ".join(issue_fragments),
                        rejection_reasons=issue_fragments,
                        prompt_profile=prompt_profile,
                    )
                    setattr(
                        retry_state,
                        second_repair_reason.cap_attribute,
                        True,
                    )
                    retry_state.consecutive_failures += 1
                    continue

                # Repair already attempted and validation still fails — abort
                # instead of looping through more repair calls (prevents the
                # plan→error→repair→retry chain from burning minutes of budget).
                ctx.orchestration_state.status = OrchestrationStatus.ABORTED
                ctx.orchestration_state.abort_reason = (
                    "Planning validation failed after repair: "
                    + "; ".join(plan_verdict.reasons[:3])
                )
                ctx.logger.warning(
                    "[ORCHESTRATION] Plan validation still failing after repair; aborting to prevent retry loop (failure_count=%d)",
                    retry_state.consecutive_failures,
                )
                emit_phase_event(
                    ctx.orchestration_state,
                    ctx.emit_live,
                    level="ERROR",
                    phase="planning",
                    message="[ORCHESTRATION] Plan validation failed after repair",
                    details=_terminal_validation_failure_details(plan_verdict),
                )
                failure_reason = "Plan validation failed after repair: " + "; ".join(
                    plan_verdict.reasons[:4]
                )
                _finalize_planning_terminal_failure(
                    ctx=ctx,
                    failure_type="planning_validation_failed_after_repair",
                    failure_reason=failure_reason,
                )
                if ctx.restore_workspace_snapshot_if_needed:
                    ctx.restore_workspace_snapshot_if_needed(
                        "planning validation failure"
                    )
                return {
                    "status": "failed",
                    "reason": "planning_validation_failed_after_repair",
                }

            reasoning_artifact = _build_reasoning_artifact(
                ctx=ctx,
                workspace_review=workspace_review,
            )
            reasoning_verdict = ValidatorService.validate_reasoning_artifact(
                reasoning_artifact,
                plan=ctx.orchestration_state.plan,
                validation_severity=ctx.validation_severity,
            )
            record_validation_verdict(
                ctx.db,
                ctx.session_id,
                ctx.task_id,
                ctx.orchestration_state,
                reasoning_verdict,
                parent_event_id=(planning_phase_event or {}).get("event_id"),
            )
            if not reasoning_verdict.accepted:
                ctx.logger.warning(
                    "[ORCHESTRATION] Reasoning artifact validation failed after plan acceptance"
                )
                ctx.orchestration_state.status = OrchestrationStatus.ABORTED
                ctx.orchestration_state.abort_reason = (
                    "Structured reasoning artifact failed validation before execution"
                )
                emit_phase_event(
                    ctx.orchestration_state,
                    ctx.emit_live,
                    level="ERROR",
                    phase="planning",
                    message="[ORCHESTRATION] Structured reasoning artifact failed validation before execution",
                    details={
                        "reason": "reasoning_artifact_validation_failed",
                        "validation_status": reasoning_verdict.status,
                    },
                )
                failure_reason = (
                    "Structured reasoning artifact failed validation: "
                    + "; ".join(reasoning_verdict.reasons[:4])
                )
                _finalize_planning_terminal_failure(
                    ctx=ctx,
                    failure_type="reasoning_artifact_validation_failed",
                    failure_reason=failure_reason,
                )
                if ctx.restore_workspace_snapshot_if_needed:
                    ctx.restore_workspace_snapshot_if_needed(
                        "reasoning artifact validation failed"
                    )
                return {
                    "status": "failed",
                    "reason": "reasoning_artifact_validation_failed",
                }

            ctx.orchestration_state.reasoning_artifact = reasoning_artifact
            try:
                append_orchestration_event(
                    project_dir=ctx.orchestration_state.project_dir,
                    session_id=ctx.session_id,
                    task_id=ctx.task_id,
                    event_type=EventType.REASONING_ARTIFACT_GENERATED,
                    parent_event_id=(planning_phase_event or {}).get("event_id"),
                    details={
                        "intent": reasoning_artifact.get("intent"),
                        "workspace_fact_count": len(
                            reasoning_artifact.get("workspace_facts", [])
                        ),
                        "planned_action_count": len(
                            reasoning_artifact.get("planned_actions", [])
                        ),
                        "verification_count": len(
                            reasoning_artifact.get("verification_plan", [])
                        ),
                        "validation_status": reasoning_verdict.status,
                    },
                )
            except Exception as exc:
                ctx.logger.debug(
                    "[ORCHESTRATION] Failed to persist reasoning artifact event: %s",
                    exc,
                )

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
    except PlanningRepairBudgetExceeded as exc:
        failure_type = "planning_repair_prompt_too_large"
        ctx.logger.error(
            "[ORCHESTRATION] Planning repair was skipped because the repair prompt exceeded the safe budget: %s",
            exc,
        )
        ctx.orchestration_state.status = OrchestrationStatus.ABORTED
        ctx.orchestration_state.abort_reason = str(exc)
        emit_phase_event(
            ctx.orchestration_state,
            ctx.emit_live,
            level="ERROR",
            phase="planning",
            message=f"[ORCHESTRATION] Planning repair prompt exceeded safe budget: {exc}",
            details={"reason": failure_type},
        )
        _finalize_planning_timeout_failure(
            ctx=ctx,
            failure_type=failure_type,
            failure_reason=str(exc),
        )
        if ctx.restore_workspace_snapshot_if_needed:
            ctx.restore_workspace_snapshot_if_needed(
                "planning repair prompt budget exceeded"
            )
        return {"status": "failed", "reason": failure_type}
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
        _finalize_planning_terminal_failure(
            ctx=ctx,
            failure_type="workspace_isolation_violation",
            failure_reason=str(exc),
        )
        if ctx.restore_workspace_snapshot_if_needed:
            ctx.restore_workspace_snapshot_if_needed("workspace isolation violation")
        return {"status": "failed", "reason": "workspace_isolation_violation"}
    except PlanningRepairOutputContractViolation as exc:
        failure_type = "repair_output_contract_violation"
        ctx.logger.error(
            "[ORCHESTRATION] Planning repair output contract violation: %s",
            exc,
        )
        ctx.orchestration_state.status = OrchestrationStatus.ABORTED
        ctx.orchestration_state.abort_reason = str(exc)
        emit_phase_event(
            ctx.orchestration_state,
            ctx.emit_live,
            level="ERROR",
            phase="planning",
            message=f"[ORCHESTRATION] Planning repair output contract violation: {exc}",
            details={"reason": failure_type},
        )
        try:
            phase_finished_event = append_orchestration_event(
                project_dir=ctx.orchestration_state.project_dir,
                session_id=ctx.session_id,
                task_id=ctx.task_id,
                event_type=EventType.PHASE_FINISHED,
                parent_event_id=(planning_phase_event or {}).get("event_id"),
                details={
                    "phase": "planning",
                    "status": "repair_output_contract_violation",
                },
            )
            write_orchestration_state_snapshot(
                project_dir=ctx.orchestration_state.project_dir,
                session_id=ctx.session_id,
                task_id=ctx.task_id,
                orchestration_state=ctx.orchestration_state,
                trigger="phase_finished",
                related_event_id=phase_finished_event.get("event_id"),
            )
        except Exception as snap_exc:
            ctx.logger.debug(
                "[ORCHESTRATION] Failed to persist repair output contract violation phase-finish snapshot: %s",
                snap_exc,
            )
        _finalize_planning_terminal_failure(
            ctx=ctx,
            failure_type=failure_type,
            failure_reason=str(exc),
        )
        if ctx.restore_workspace_snapshot_if_needed:
            ctx.restore_workspace_snapshot_if_needed("repair output contract violation")
        return {"status": "failed", "reason": failure_type}
    except TimeoutError as exc:
        timeout_exc = exc
        failure_type = _classify_planning_timeout_failure(exc, retry_state)
        is_repair_timeout = (
            "repair_timeout" in failure_type
            or failure_type == "planning_repair_no_output_timeout"
        )
        if is_repair_timeout:
            ctx.logger.error(
                "[ORCHESTRATION] Planning repair timed out before a valid plan was produced: %s",
                timeout_exc,
            )
        else:
            ctx.logger.error(
                "[ORCHESTRATION] Planning timed out or exceeded context before a valid plan was produced: %s",
                timeout_exc,
            )
        ctx.orchestration_state.status = OrchestrationStatus.ABORTED
        ctx.orchestration_state.abort_reason = (
            f"Planning repair timed out: {timeout_exc}"
            if is_repair_timeout
            else f"Planning timed out or exceeded context: {timeout_exc}"
        )
        failure_message = (
            f"[ORCHESTRATION] Planning repair timed out: {timeout_exc}"
            if is_repair_timeout
            else (
                "[ORCHESTRATION] Planning timed out or exceeded context: "
                f"{timeout_exc}"
            )
        )
        emit_phase_event(
            ctx.orchestration_state,
            ctx.emit_live,
            level="ERROR",
            phase="planning",
            message=failure_message,
            details={"reason": failure_type},
        )
        try:
            phase_finished_event = append_orchestration_event(
                project_dir=ctx.orchestration_state.project_dir,
                session_id=ctx.session_id,
                task_id=ctx.task_id,
                event_type=EventType.PHASE_FINISHED,
                parent_event_id=(planning_phase_event or {}).get("event_id"),
                details={
                    "phase": "planning",
                    "status": (
                        "repair_timeout"
                        if is_repair_timeout
                        else "timeout_or_context_overflow"
                    ),
                },
            )
            write_orchestration_state_snapshot(
                project_dir=ctx.orchestration_state.project_dir,
                session_id=ctx.session_id,
                task_id=ctx.task_id,
                orchestration_state=ctx.orchestration_state,
                trigger="phase_finished",
                related_event_id=phase_finished_event.get("event_id"),
            )
        except Exception as exc:
            ctx.logger.debug(
                "[ORCHESTRATION] Failed to persist planning timeout phase-finish snapshot: %s",
                exc,
            )
        _finalize_planning_timeout_failure(
            ctx=ctx,
            failure_type=failure_type,
            failure_reason=str(timeout_exc),
        )
        if ctx.restore_workspace_snapshot_if_needed:
            ctx.restore_workspace_snapshot_if_needed(
                "planning repair timeout"
                if is_repair_timeout
                else "planning timeout or context overflow"
            )
        return {"status": "failed", "reason": failure_type}
    except SoftTimeLimitExceeded as exc:
        failure_type = _classify_planning_timeout_failure(exc, retry_state)
        ctx.logger.error(
            "[ORCHESTRATION] Planning was interrupted by the Celery soft time limit: %s",
            exc,
        )
        ctx.orchestration_state.status = OrchestrationStatus.ABORTED
        ctx.orchestration_state.abort_reason = "Planning exceeded the worker soft time limit before a valid plan was produced"
        emit_phase_event(
            ctx.orchestration_state,
            ctx.emit_live,
            level="ERROR",
            phase="planning",
            message=(
                "[ORCHESTRATION] Planning exceeded the worker soft time limit "
                "before a valid plan was produced"
            ),
            details={"reason": failure_type},
        )
        try:
            phase_finished_event = append_orchestration_event(
                project_dir=ctx.orchestration_state.project_dir,
                session_id=ctx.session_id,
                task_id=ctx.task_id,
                event_type=EventType.PHASE_FINISHED,
                parent_event_id=(planning_phase_event or {}).get("event_id"),
                details={
                    "phase": "planning",
                    "status": "soft_time_limit_exceeded",
                },
            )
            write_orchestration_state_snapshot(
                project_dir=ctx.orchestration_state.project_dir,
                session_id=ctx.session_id,
                task_id=ctx.task_id,
                orchestration_state=ctx.orchestration_state,
                trigger="phase_finished",
                related_event_id=phase_finished_event.get("event_id"),
            )
        except Exception as exc:
            ctx.logger.debug(
                "[ORCHESTRATION] Failed to persist planning soft-time-limit phase-finish snapshot: %s",
                exc,
            )
        _finalize_planning_timeout_failure(
            ctx=ctx,
            failure_type=failure_type,
            failure_reason=(
                "Planning exceeded the worker soft time limit before a valid plan "
                "was produced"
            ),
        )
        if ctx.restore_workspace_snapshot_if_needed:
            ctx.restore_workspace_snapshot_if_needed(
                "planning soft time limit exceeded"
            )
        return {"status": "failed", "reason": failure_type}
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
        try:
            phase_finished_event = append_orchestration_event(
                project_dir=ctx.orchestration_state.project_dir,
                session_id=ctx.session_id,
                task_id=ctx.task_id,
                event_type=EventType.PHASE_FINISHED,
                parent_event_id=(planning_phase_event or {}).get("event_id"),
                details={
                    "phase": "planning",
                    "status": "parse_error",
                },
            )
            write_orchestration_state_snapshot(
                project_dir=ctx.orchestration_state.project_dir,
                session_id=ctx.session_id,
                task_id=ctx.task_id,
                orchestration_state=ctx.orchestration_state,
                trigger="phase_finished",
                related_event_id=phase_finished_event.get("event_id"),
            )
        except Exception as exc:
            ctx.logger.debug(
                "[ORCHESTRATION] Failed to persist planning parse-error phase-finish snapshot: %s",
                exc,
            )
        _finalize_planning_terminal_failure(
            ctx=ctx,
            failure_type="planning_parse_error",
            failure_reason=str(exc),
        )
        if ctx.restore_workspace_snapshot_if_needed:
            ctx.restore_workspace_snapshot_if_needed("planning parse error")
        return {"status": "failed", "reason": "planning_parse_error"}


def __retry_with_minimal_prompt(
    *,
    ctx: OrchestrationRunContext,
    planning_timeout_seconds: int,
    reason: str,
    prompt_profile: str = "default",
) -> Dict[str, Any]:
    return PlannerService.retry_with_minimal_prompt(
        runtime_service=ctx.runtime_service,
        task_description=ctx.prompt,
        project_dir=ctx.orchestration_state.project_dir,
        timeout_seconds=planning_timeout_seconds,
        logger=ctx.logger,
        emit_live=ctx.emit_live,
        reason=reason,
        prompt_profile=prompt_profile,
        workflow_profile=ctx.workflow_profile,
        workflow_phases=getattr(ctx, "workflow_phases", []),
        workspace_has_existing_files=getattr(
            ctx, "workspace_has_existing_files", False
        ),
    )


def __repair_planning_output(
    *,
    ctx: OrchestrationRunContext,
    planning_timeout_seconds: int,
    malformed_output: str,
    reason: str,
    rejection_reasons: list[str] | None = None,
    prompt_profile: str = "default",
    knowledge_context: KnowledgeContext | None = None,
) -> Dict[str, Any]:
    return PlannerService.repair_output(
        runtime_service=ctx.runtime_service,
        task_description=ctx.prompt,
        malformed_output=malformed_output,
        project_dir=ctx.orchestration_state.project_dir,
        timeout_seconds=planning_timeout_seconds,
        logger=ctx.logger,
        emit_live=ctx.emit_live,
        reason=reason,
        rejection_reasons=rejection_reasons,
        prompt_profile=prompt_profile,
        workflow_profile=ctx.workflow_profile,
        workflow_phases=getattr(ctx, "workflow_phases", []),
        workspace_has_existing_files=getattr(
            ctx, "workspace_has_existing_files", False
        ),
        knowledge_context=knowledge_context,
        session_id=ctx.session_id,
        task_id=ctx.task_id,
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


def _retrieve_knowledge(
    ctx: OrchestrationRunContext,
    trigger_phase: str,
    knowledge_types: list[str],
) -> KnowledgeContext | None:
    """Retrieve knowledge context; returns None on any error so failures don't break the flow."""
    try:
        from app.config import settings
        from app.services.knowledge.knowledge_service import KnowledgeService

        svc = KnowledgeService(
            qdrant_url=settings.QDRANT_URL,
            collection_name=settings.QDRANT_COLLECTION_NAME,
            embedding_model=settings.OPENAI_EMBEDDING_MODEL,
        )
        return svc.retrieve(
            query=ctx.prompt or "",
            trigger_phase=trigger_phase,
            knowledge_types=knowledge_types,
            db=ctx.db,
        )
    except Exception as exc:
        ctx.logger.debug("[KNOWLEDGE] Retrieval skipped (%s): %s", trigger_phase, exc)
        return None


def _log_knowledge_usage(
    ctx: OrchestrationRunContext,
    knowledge_ctx: KnowledgeContext,
    *,
    used_in_prompt: bool,
) -> None:
    try:
        from app.services.knowledge import usage_log_service

        usage_log_service.log_usage(
            context=knowledge_ctx,
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            used_in_prompt=used_in_prompt,
            db=ctx.db,
        )
    except Exception as exc:
        ctx.logger.debug("[KNOWLEDGE] Usage log skipped: %s", exc)
