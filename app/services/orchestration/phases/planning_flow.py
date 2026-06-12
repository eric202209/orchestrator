"""Planning-phase orchestration flow."""

from __future__ import annotations

import asyncio
import hashlib
import json
from pathlib import Path
from typing import Any, Callable, Dict

from celery.exceptions import SoftTimeLimitExceeded

from app.schemas.knowledge import KnowledgeContext
from app.services.orchestration.context.assembly import (
    assemble_planning_prompt,
    compress_orchestration_context,
)
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.events.telemetry import emit_phase_event
from app.services.orchestration.state.persistence import (
    append_orchestration_event,
    maybe_emit_divergence_detected,
    record_validation_verdict,
    write_orchestration_state_snapshot,
)
from app.services.orchestration.planning.planner import (
    PlannerService,
    PlanningRepairBudgetExceeded,
    PlanningRepairOutputContractViolation,
)
from app.services.orchestration.planning.source_materialization import (
    repair_context_requires_source_materialization as _repair_context_requires_source_materialization,
    repair_removed_source_materialization as _repair_removed_source_materialization,
)
from app.services.orchestration.planning.normalization import (
    normalize_existing_file_target_plan,
    normalize_stale_replace_ops_to_small_file_writes,
)
from app.services.orchestration.policy import clamp_planning_timeout
from app.services.orchestration.task_rules import get_workflow_profile
from app.services.orchestration.workflow_profiles import get_workflow_phases
from app.services.orchestration.types import OrchestrationRunContext
from app.services.orchestration.validation.parsing import (
    extract_plan_steps_from_summary_text,
)
from app.services.orchestration.validation.validator import (
    ValidatorService,
)
from app.services.orchestration.validation.workspace_guard import (
    TaskOperationContractViolation,
)
from app.services.prompt_templates import OrchestrationStatus, estimate_token_count
from app.services.orchestration.phases.planning_verification import (
    _commands_are_weak_expected_file_verification,
    _grep_quiet_verification_target,
    _python_exists_verification_command,
    _python_file_contains_verification_command,
    _strengthen_weak_expected_file_verifications,
)
from app.services.orchestration.phases.planning_knowledge import (
    _log_knowledge_usage,
    _looks_like_verification_only_task,
    _retrieve_knowledge,
    _retrieve_validation_repair_knowledge,
)
from app.services.orchestration.phases.read_only_fallbacks import (
    _read_only_stage_fallback_plan,
)
from app.services.orchestration.phases.planning_repair_arbitration_control import (
    arbitrate_planning_repair_candidate,
)
from app.services.orchestration.phases.planning_plan_shape import (
    prune_unmaterialized_expected_files as _prune_unmaterialized_expected_files,
    split_repaired_single_step_full_lifecycle_plan as _split_repaired_single_step_full_lifecycle_plan,
)
from app.services.orchestration.phases.planning_task1_bootstrap import (
    emit_task1_bootstrap_contract_event as _emit_task1_bootstrap_contract_event,
    is_first_ordered_task as _is_first_ordered_task,
    normalize_task1_bootstrap_plan_for_json_stability as _normalize_task1_bootstrap_plan_for_json_stability,
    normalize_task1_python_src_layout_verification as _normalize_task1_python_src_layout_verification,
    task1_bootstrap_contract_passed as _task1_bootstrap_contract_passed,
    task1_plan_failed_only_brittle_command_shape as _task1_plan_failed_only_brittle_command_shape,
)


from app.services.orchestration.phases.planning_support import (
    BLOCKING_IMMEDIATE_REPAIR_ISSUE_KEYS,
    MAX_PLANNING_RETRIES,
    TRUNCATED_PLAN_REPAIR_REJECTION_REASON,
    _PlanningRetryState,
    _abort_missing_source_materialization_repair,
    _abort_repeated_physical_src_import_repair,
    _abort_root_cause_oscillation_repair_loop,
    _build_reasoning_artifact,
    _build_repair_rejection_reasons,
    _classify_planning_timeout_failure,
    _compress_project_context_for_planning,
    _count_prior_failed_planning_executions,
    _emit_planning_diagnostics_contract_violation,
    _finalize_planning_terminal_failure,
    _finalize_planning_timeout_failure,
    _get_targeted_second_repair_reason,
    _is_repairable_malformed_shell_quoting_violation,
    _last_plan_output_snippet,
    _extract_stale_old_text_from_plan,
    _model_lane_limitation_for_invalid_planning_commands,
    _plan_contract_diagnostics,
    _planning_validation_profile,
    _planning_root_cause_from_immediate_repair_issues,
    _planning_root_cause_from_issue_key,
    _planning_root_cause_from_plan_verdict,
    _record_planning_root_cause,
    _record_repair_root_cause,
    _repair_root_cause_from_plan_verdict,
    _semantic_codes_for_immediate_repair_issues,
    _should_repair_truncated_single_step_plan,
    _terminal_validation_failure_details,
    _terminal_planning_root_cause,
    _task1_bootstrap_second_repair_rejection_reasons,
    _truncated_multistep_collapse_diagnostics,
    _usable_knowledge_context,
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
    # Template workflow_profile overrides the heuristic-derived value.
    _tmpl_id = getattr(ctx.task, "template_id", None)
    if _tmpl_id:
        try:
            from app.services.orchestration.workflow_templates import (
                get_template as _gt,
            )

            _tmpl = _gt(_tmpl_id)
            if _tmpl:
                ctx.workflow_profile = _tmpl.workflow_profile
        except Exception:
            pass
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
            knowledge_context=_usable_knowledge_context(planning_knowledge_ctx),
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
    model_capability_label = PlannerService.model_capability_label(
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
    planning_prompt_ref = None
    if planning_prompt:
        planning_prompt_ref = {
            "kind": "sha256",
            "sha256": hashlib.sha256(planning_prompt.encode("utf-8")).hexdigest(),
            "chars": len(planning_prompt),
            "estimated_tokens": planning_prompt_tokens,
            "prompt_profile": prompt_profile,
        }
    # 10H-C: emit context budget on every planning attempt so it appears in
    # session debug output regardless of dense-prompt detection.
    emit_phase_event(
        ctx.orchestration_state,
        ctx.emit_live,
        level="INFO",
        phase="planning",
        message=(
            f"[ORCHESTRATION] Planning context budget: "
            f"~{planning_prompt_tokens} tokens "
            f"(project_context={len(ctx.orchestration_state.project_context or '')}c "
            f"task={len(ctx.prompt or '')}c)"
        ),
        details={
            "planning_prompt_tokens": planning_prompt_tokens,
            "project_context_chars": len(ctx.orchestration_state.project_context or ""),
            "task_chars": len(ctx.prompt or ""),
            "prompt_profile": prompt_profile,
            "planning_prompt_ref": planning_prompt_ref,
            "model_capability_label": model_capability_label,
            "context_budget_status": (
                "dense" if planning_prompt_tokens > 8000 else "normal"
            ),
        },
    )

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
        # Always compress project_context when the prompt is dense.
        # Later tasks in a session accumulate workspace files that inflate the
        # project_context even when no debug/completed steps exist, causing
        # dense_planning_context failures for beta-python and gamma-docs tasks.
        _compressed = _compress_project_context_for_planning(ctx.orchestration_state)
        if _compressed:
            ctx.orchestration_state.project_context = _compressed
            ctx.logger.info(
                "[ORCHESTRATION] Context compressed for dense planning (%d chars)",
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
        planning_result = __retry_with_minimal_prompt(
            ctx=ctx,
            planning_timeout_seconds=planning_timeout_seconds,
            reason="dense_planning_context",
            prompt_profile=prompt_profile,
            knowledge_context=planning_knowledge_ctx,
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
        planning_result = __retry_with_minimal_prompt(
            ctx=ctx,
            planning_timeout_seconds=planning_timeout_seconds,
            reason=(planning_result.get("error") or initial_output_text),
            prompt_profile=prompt_profile,
            knowledge_context=planning_knowledge_ctx,
        )
        used_minimal_planning_prompt = True

    persisted_failures = _count_prior_failed_planning_executions(ctx)
    retry_state = _PlanningRetryState(persisted_failures=persisted_failures)
    retry_state.minimal_prompt_used = used_minimal_planning_prompt
    if persisted_failures > 0:
        ctx.logger.warning(
            "[ORCHESTRATION] session_id=%s task_id=%s "
            "persisted_planning_failures=%d — circuit breaker seeded from DB",
            ctx.session_id,
            ctx.task_id,
            persisted_failures,
        )
    try:
        while True:
            # Circuit breaker: abort after too many consecutive validation failures.
            # Combined count includes persisted failures from prior executions so
            # a worker restart cannot reset the counter to zero (F12).
            if retry_state.circuit_open:
                has_persisted = retry_state.persisted_failures > 0
                cb_reason = (
                    "planning_circuit_breaker_opened_persisted_attempts"
                    if has_persisted
                    else "planning_circuit_breaker_opened"
                )
                total_failures = (
                    retry_state.consecutive_failures + retry_state.persisted_failures
                )
                root_cause = _terminal_planning_root_cause(retry_state)
                ctx.orchestration_state.status = OrchestrationStatus.ABORTED
                ctx.orchestration_state.abort_reason = (
                    f"Planning failed {total_failures} time(s) "
                    f"({retry_state.persisted_failures} prior + "
                    f"{retry_state.consecutive_failures} this run); "
                    "circuit breaker opened to prevent infinite retry loop"
                )
                emit_phase_event(
                    ctx.orchestration_state,
                    ctx.emit_live,
                    level="ERROR",
                    phase="planning",
                    message=(
                        f"[ORCHESTRATION] Planning circuit breaker opened after "
                        f"{total_failures} failure(s) "
                        f"({retry_state.persisted_failures} persisted + "
                        f"{retry_state.consecutive_failures} in-session)"
                    ),
                    details={
                        "reason": cb_reason,
                        "persisted_failures": retry_state.persisted_failures,
                        "consecutive_failures": retry_state.consecutive_failures,
                        "total_failures": total_failures,
                        "terminal_state": cb_reason,
                        "planning_root_cause": root_cause,
                    },
                )
                last_snippet = _last_plan_output_snippet(planning_result)
                last_repair = retry_state.last_repair_reason or "none"
                cb_failure_reason = (
                    f"Planning failed {total_failures} time(s) "
                    f"({retry_state.persisted_failures} prior + "
                    f"{retry_state.consecutive_failures} this run). "
                    f"Last repair reason: {last_repair}. "
                    + (
                        f"Last plan output: {last_snippet}"
                        if last_snippet
                        else "No plan output was produced."
                    )
                )
                _finalize_planning_terminal_failure(
                    ctx=ctx,
                    failure_type=cb_reason,
                    failure_reason=cb_failure_reason,
                    generate_failure_summary=True,
                    planning_root_cause=root_cause,
                )
                if ctx.restore_workspace_snapshot_if_needed:
                    ctx.restore_workspace_snapshot_if_needed(
                        "planning circuit breaker opened"
                    )
                return {
                    "status": "failed",
                    "reason": cb_reason,
                    "planning_root_cause": root_cause,
                }

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
                    knowledge_context=planning_knowledge_ctx,
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
                    knowledge_context=planning_knowledge_ctx,
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

            repair_shrank_multistep_plan = False
            single_step_full_lifecycle_plan = False
            verification_only_task = _looks_like_verification_only_task(
                getattr(ctx.task, "title", None),
                getattr(ctx.task, "description", None),
            )
            if isinstance(extracted_plan, list):
                if len(extracted_plan) > 1:
                    retry_state.last_multistep_plan_step_count = max(
                        retry_state.last_multistep_plan_step_count,
                        len(extracted_plan),
                    )
                elif (
                    len(extracted_plan) == 1
                    and retry_state.repair_prompt_used
                    and retry_state.last_multistep_plan_step_count > 1
                ):
                    repair_shrank_multistep_plan = True
                elif (
                    len(extracted_plan) == 1
                    and ctx.execution_profile == "full_lifecycle"
                    and bool(getattr(ctx, "workspace_has_existing_files", False))
                    and not verification_only_task
                ):
                    single_step_full_lifecycle_plan = True

            if single_step_full_lifecycle_plan and not retry_state.repair_prompt_used:
                # Attempt deterministic lifecycle expansion before consuming a repair
                # token. When the original single-step plan already has ops or clear
                # artifact commands (manifest.json, config files, etc.), splitting
                # into inspect/implement/verify deterministically prevents repair from
                # drifting into unrelated solution.py implementations.
                normalized_plan = _split_repaired_single_step_full_lifecycle_plan(
                    extracted_plan
                )
                if normalized_plan:
                    emit_phase_event(
                        ctx.orchestration_state,
                        ctx.emit_live,
                        level="INFO",
                        phase="planning",
                        message=(
                            "[ORCHESTRATION] Normalized original single-step artifact "
                            "plan into deterministic lifecycle steps (skipping repair)"
                        ),
                        details={
                            "reason": "single_step_artifact_plan_normalized",
                            "step_count": len(normalized_plan),
                        },
                    )
                    extracted_plan = normalized_plan
                    output_text = json.dumps(normalized_plan)
                    single_step_full_lifecycle_plan = False
                    # Fall through to validation without consuming a repair attempt
                else:
                    contract_violations = [
                        "Full-lifecycle planning must return separate inspect, implementation, and verification steps"
                    ]
                    _emit_planning_diagnostics_contract_violation(
                        ctx,
                        reason="single_step_full_lifecycle_plan",
                        contract_violations=contract_violations,
                        output_text=output_text,
                        strategy_info="single_step_full_lifecycle_plan",
                    )
                    retry_state.last_repair_reason = "single_step_full_lifecycle_plan"
                    planning_result = __repair_planning_output(
                        ctx=ctx,
                        planning_timeout_seconds=planning_timeout_seconds,
                        malformed_output=output_text,
                        reason="single_step_full_lifecycle_plan",
                        rejection_reasons=[
                            "Return 3 or 4 separate step objects: inspect current workspace, implement the requested change, and verify behavior/content. Do not merge the full task into one step."
                        ],
                        prompt_profile=prompt_profile,
                        knowledge_context=planning_knowledge_ctx,
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
                if (
                    _should_repair_truncated_single_step_plan(
                        prompt_profile=prompt_profile,
                        extracted_plan=extracted_plan,
                        execution_profile=ctx.execution_profile,
                    )
                    and not verification_only_task
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
                        knowledge_context=planning_knowledge_ctx,
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

            if (
                looks_like_truncated_multistep_plan(output_text, extracted_plan)
                or repair_shrank_multistep_plan
                or (single_step_full_lifecycle_plan and retry_state.repair_prompt_used)
            ):
                normalized_plan = None
                if (
                    retry_state.repair_prompt_used
                    and isinstance(extracted_plan, list)
                    and len(extracted_plan) == 1
                ):
                    removed_source_paths = _repair_removed_source_materialization(
                        ctx.orchestration_state.plan, extracted_plan
                    )
                    if removed_source_paths:
                        failure_type = "repair_removed_materialization"
                        ctx.orchestration_state.status = OrchestrationStatus.ABORTED
                        ctx.orchestration_state.abort_reason = (
                            "Planning repair removed source materialization"
                        )
                        emit_phase_event(
                            ctx.orchestration_state,
                            ctx.emit_live,
                            level="ERROR",
                            phase="planning",
                            message=(
                                "[ORCHESTRATION] Planning repair removed concrete "
                                "source materialization from the rejected plan"
                            ),
                            details={
                                "reason": failure_type,
                                "removed_source_materialization_paths": (
                                    removed_source_paths[:8]
                                ),
                            },
                        )
                        _emit_planning_diagnostics_contract_violation(
                            ctx,
                            reason=failure_type,
                            contract_violations=[
                                "repair_removed_materialization: repaired/salvaged "
                                "plan removed concrete source write operations from "
                                "the rejected plan"
                            ],
                            contract_diagnostics={
                                "removed_source_materialization_paths": (
                                    removed_source_paths[:8]
                                ),
                            },
                            output_text=output_text,
                            strategy_info=failure_type,
                        )
                        _finalize_planning_terminal_failure(
                            ctx=ctx,
                            failure_type=failure_type,
                            failure_reason=(
                                "Planning repair removed materializing source "
                                "operations from the rejected plan: "
                                + ", ".join(removed_source_paths[:4])
                            ),
                        )
                        if ctx.restore_workspace_snapshot_if_needed:
                            ctx.restore_workspace_snapshot_if_needed(
                                "planning repair removed materialization"
                            )
                        return {
                            "status": "failed",
                            "reason": failure_type,
                        }
                    else:
                        normalized_plan = (
                            _split_repaired_single_step_full_lifecycle_plan(
                                extracted_plan
                            )
                        )
                if normalized_plan:
                    ctx.logger.warning(
                        "[ORCHESTRATION] Normalized repaired single-step full-lifecycle plan into %d deterministic steps",
                        len(normalized_plan),
                    )
                    emit_phase_event(
                        ctx.orchestration_state,
                        ctx.emit_live,
                        level="WARN",
                        phase="planning",
                        message=(
                            "[ORCHESTRATION] Normalized repaired single-step "
                            "full-lifecycle plan into deterministic steps"
                        ),
                        details={
                            "reason": "single_step_full_lifecycle_plan_normalized",
                            "step_count": len(normalized_plan),
                        },
                    )
                    extracted_plan = normalized_plan
                    output_text = json.dumps(normalized_plan)
                    repair_shrank_multistep_plan = False
                    single_step_full_lifecycle_plan = False
                else:
                    truncated_diagnostics = _truncated_multistep_collapse_diagnostics(
                        output_text=output_text,
                        extracted_plan=extracted_plan,
                        repair_stage="after_first_repair",
                    )
                    if repair_shrank_multistep_plan:
                        truncated_diagnostics[
                            "truncated_multistep_original_step_count"
                        ] = retry_state.last_multistep_plan_step_count
                        truncated_diagnostics["truncated_multistep_subcodes"] = list(
                            dict.fromkeys(
                                list(
                                    truncated_diagnostics.get(
                                        "truncated_multistep_subcodes"
                                    )
                                    or []
                                )
                                + [
                                    "repair_shrank_previously_valid_multistep_plan",
                                    (
                                        "original_steps_detected_"
                                        f"{retry_state.last_multistep_plan_step_count}"
                                    ),
                                ]
                            )
                        )
                    failure_type = (
                        "single_step_full_lifecycle_plan_after_repair"
                        if (
                            single_step_full_lifecycle_plan
                            and retry_state.repair_prompt_used
                        )
                        else "truncated_multistep_plan_after_retry"
                    )
                    ctx.orchestration_state.status = OrchestrationStatus.ABORTED
                    ctx.orchestration_state.abort_reason = (
                        "Planning output collapsed into a single-step plan"
                    )
                    emit_phase_event(
                        ctx.orchestration_state,
                        ctx.emit_live,
                        level="ERROR",
                        phase="planning",
                        message="[ORCHESTRATION] Planning output was still a single-step plan after repair",
                        details={"reason": failure_type},
                    )
                    _emit_planning_diagnostics_contract_violation(
                        ctx,
                        reason=failure_type,
                        contract_violations=[
                            "full-lifecycle planning returned a single-step plan after repair"
                        ],
                        contract_diagnostics=truncated_diagnostics,
                        output_text=output_text,
                        strategy_info=failure_type,
                    )
                    _finalize_planning_terminal_failure(
                        ctx=ctx,
                        failure_type=failure_type,
                        failure_reason=(
                            "Planning output was still a single-step plan after repair. "
                            "The run was stopped to avoid a false success."
                        ),
                    )
                    if ctx.restore_workspace_snapshot_if_needed:
                        ctx.restore_workspace_snapshot_if_needed(
                            "single-step full-lifecycle plan"
                        )
                    return {
                        "status": "failed",
                        "reason": failure_type,
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

            previous_plan_for_repair_arbitration = (
                list(ctx.orchestration_state.plan or [])
                if retry_state.repair_prompt_used
                and isinstance(ctx.orchestration_state.plan, list)
                else []
            )
            sanitized_plan = PlannerService.sanitize_common_plan_issues(
                extracted_plan, task_prompt=ctx.prompt
            )
            sanitized_plan = _strengthen_weak_expected_file_verifications(
                sanitized_plan
            )
            sanitized_plan, file_target_normalization = (
                normalize_existing_file_target_plan(
                    sanitized_plan,
                    project_dir=ctx.orchestration_state.project_dir,
                )
            )
            if file_target_normalization.get("changed"):
                ctx.logger.info(
                    "[ORCHESTRATION] Normalized plan file targets to existing workspace paths: %s",
                    file_target_normalization,
                )
                emit_phase_event(
                    ctx.orchestration_state,
                    ctx.emit_live,
                    level="INFO",
                    phase="planning",
                    message="[ORCHESTRATION] Normalized plan file targets to existing workspace paths",
                    details=file_target_normalization,
                )
            sanitized_plan, stale_replace_normalization = (
                normalize_stale_replace_ops_to_small_file_writes(
                    sanitized_plan,
                    project_dir=ctx.orchestration_state.project_dir,
                )
            )
            if stale_replace_normalization.get("changed"):
                ctx.logger.info(
                    "[ORCHESTRATION] Converted stale replace ops to guarded small-file writes: %s",
                    stale_replace_normalization,
                )
                emit_phase_event(
                    ctx.orchestration_state,
                    ctx.emit_live,
                    level="INFO",
                    phase="planning",
                    message="[ORCHESTRATION] Converted stale replace ops to guarded small-file writes",
                    details=stale_replace_normalization,
                )
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
                            project_dir=ctx.orchestration_state.project_dir,
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
            materialization_result = _abort_missing_source_materialization_repair(
                ctx=ctx,
                retry_state=retry_state,
                output_text=output_text,
            )
            if materialization_result:
                return materialization_result
            immediate_repair_issues = PlannerService.find_immediate_repair_step_issues(
                ctx.orchestration_state.plan,
                project_dir=ctx.orchestration_state.project_dir,
            )
            if retry_state.repair_prompt_used:
                arbitration_control = arbitrate_planning_repair_candidate(
                    ctx=ctx,
                    retry_state=retry_state,
                    previous_plan=previous_plan_for_repair_arbitration,
                    immediate_repair_issues=immediate_repair_issues,
                    planning_phase_event=planning_phase_event,
                    output_text=output_text,
                    planning_timeout_seconds=planning_timeout_seconds,
                    prompt_profile=prompt_profile,
                    repair_planning_output=__repair_planning_output,
                )
                if arbitration_control.get("action") == "continue":
                    planning_result = arbitration_control.get("planning_result")
                    continue
                if arbitration_control.get("action") == "return":
                    return arbitration_control["result"]
                if arbitration_control.get("action") == "replace":
                    ctx.orchestration_state.plan = arbitration_control["plan"]
                    output_text = json.dumps(ctx.orchestration_state.plan)
                    immediate_repair_issues = (
                        PlannerService.find_immediate_repair_step_issues(
                            ctx.orchestration_state.plan,
                            project_dir=ctx.orchestration_state.project_dir,
                        )
                    )
            blocking_repair_issues = {
                key: value
                for key, value in immediate_repair_issues.items()
                if key in BLOCKING_IMMEDIATE_REPAIR_ISSUE_KEYS and value
            }
            if blocking_repair_issues:
                _record_repair_root_cause(
                    retry_state,
                    root_cause=_planning_root_cause_from_immediate_repair_issues(
                        blocking_repair_issues
                    ),
                    stage="planning_immediate_repair_issue",
                )
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
                if blocking_repair_issues.get("stale_replace_ops_steps"):
                    issue_fragments.append(
                        "replace_in_file old text not found in workspace in steps "
                        f"{blocking_repair_issues['stale_replace_ops_steps'][:5]}"
                    )
                    issue_fragments.extend(
                        PlannerService.stale_replace_repair_hints(
                            ctx.orchestration_state.plan,
                            ctx.orchestration_state.project_dir,
                        )
                    )
                if blocking_repair_issues.get("empty_replace_old_text_steps"):
                    issue_fragments.append(
                        "replace_in_file without old text in steps "
                        f"{blocking_repair_issues['empty_replace_old_text_steps'][:5]}"
                    )
                if blocking_repair_issues.get("test_assertion_loss_ops_steps"):
                    issue_fragments.append(
                        "test file rewrite would remove existing assertions in steps "
                        f"{blocking_repair_issues['test_assertion_loss_ops_steps'][:5]}; "
                        "preserve existing tests and assertion intent"
                    )
                if blocking_repair_issues.get("test_deletion_ops_steps"):
                    issue_fragments.append(
                        "test file deletion in steps "
                        f"{blocking_repair_issues['test_deletion_ops_steps'][:5]}; "
                        "do not delete existing tests during fallback repair"
                    )
                retry_state.last_repair_reason = "plan_contains_immediate_repair_issues"
                semantic_violation_codes = _semantic_codes_for_immediate_repair_issues(
                    blocking_repair_issues
                )
                validation_knowledge_ctx = _retrieve_validation_repair_knowledge(
                    ctx,
                    query="Plan immediate repair issue: "
                    + "; ".join(issue_fragments[:4]),
                    failure_signature=(
                        "; ".join(semantic_violation_codes)
                        if semantic_violation_codes
                        else "plan_contains_immediate_repair_issues"
                    ),
                    retrieve_knowledge=_retrieve_knowledge,
                    log_knowledge_usage=_log_knowledge_usage,
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
                    retry_state=retry_state,
                    planning_timeout_seconds=planning_timeout_seconds,
                    malformed_output=output_text,
                    reason="plan_contains_immediate_repair_issues: "
                    + "; ".join(issue_fragments),
                    rejection_reasons=issue_fragments,
                    prompt_profile=prompt_profile,
                    knowledge_context=_usable_knowledge_context(
                        validation_knowledge_ctx
                    ),
                )
                retry_state.repair_prompt_used = True
                retry_state.consecutive_failures += 1
                continue
            if blocking_repair_issues:
                blocking_plan_verdict = ValidatorService.validate_plan(
                    ctx.orchestration_state.plan,
                    output_text=output_text,
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
                second_repair_reason = _get_targeted_second_repair_reason(
                    retry_state=retry_state,
                    blocking_repair_issues=blocking_repair_issues,
                    plan_verdict=blocking_plan_verdict,
                    project_dir=ctx.orchestration_state.project_dir,
                )
                if second_repair_reason and not second_repair_reason.cap_used:
                    _record_repair_root_cause(
                        retry_state,
                        root_cause=_planning_root_cause_from_issue_key(
                            second_repair_reason.issue_key
                        ),
                        stage=second_repair_reason.event_reason,
                    )
                    oscillation_result = _abort_root_cause_oscillation_repair_loop(
                        ctx=ctx,
                        retry_state=retry_state,
                    )
                    if oscillation_result:
                        return oscillation_result
                    issue_fragments = [second_repair_reason.rejection_text]
                    if second_repair_reason.issue_key in (
                        "stale_replace_ops_steps",
                        "empty_replace_old_text_steps",
                    ):
                        issue_fragments.extend(
                            PlannerService.stale_replace_fallback_hints(
                                ctx.orchestration_state.plan,
                                ctx.orchestration_state.project_dir,
                            )
                        )
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
                            "fallback_strategy": (
                                "structured_rewrite_or_preserved_write_file"
                                if second_repair_reason.issue_key
                                == "stale_replace_ops_steps"
                                else None
                            ),
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
                    validation_knowledge_ctx = _retrieve_validation_repair_knowledge(
                        ctx,
                        query="Plan immediate repair still failed after repair: "
                        + "; ".join(issue_fragments[:4]),
                        failure_signature=second_repair_reason.semantic_violation_code,
                        retrieve_knowledge=_retrieve_knowledge,
                        log_knowledge_usage=_log_knowledge_usage,
                    )
                    retry_state.last_repair_reason = second_repair_reason.event_reason
                    planning_result = __repair_planning_output(
                        ctx=ctx,
                        retry_state=retry_state,
                        planning_timeout_seconds=planning_timeout_seconds,
                        malformed_output=output_text,
                        reason=f"{second_repair_reason.retry_reason}: "
                        + "; ".join(issue_fragments),
                        rejection_reasons=issue_fragments,
                        prompt_profile=prompt_profile,
                        knowledge_context=_usable_knowledge_context(
                            validation_knowledge_ctx
                        ),
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
                model_lane_limitation = (
                    _model_lane_limitation_for_invalid_planning_commands(
                        blocking_repair_issues
                    )
                )
                if model_lane_limitation:
                    failure_reason = (
                        failure_reason
                        + "; model_lane_limitation="
                        + str(model_lane_limitation["model_lane_limitation"])
                        + "; runtime_rewrite_added=false"
                    )
                    emit_phase_event(
                        ctx.orchestration_state,
                        ctx.emit_live,
                        level="ERROR",
                        phase="planning",
                        message=(
                            "[ORCHESTRATION] Planning repair repeated stale exact "
                            "patches after bounded repair; recording model-lane limitation"
                        ),
                        details={
                            "reason": "planning_invalid_commands_after_repair",
                            "blocking_repair_issues": blocking_repair_issues,
                            "planning_root_cause": _terminal_planning_root_cause(
                                retry_state
                            ),
                            "stale_old_text": _extract_stale_old_text_from_plan(
                                ctx.orchestration_state.plan,
                                (blocking_repair_issues or {}).get(
                                    "stale_replace_ops_steps"
                                ),
                            ),
                            **model_lane_limitation,
                        },
                    )
                _finalize_planning_terminal_failure(
                    ctx=ctx,
                    failure_type="planning_invalid_commands_after_repair",
                    failure_reason=failure_reason,
                    planning_root_cause=_terminal_planning_root_cause(retry_state),
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
                workflow_stage=ctx.workflow_stage,
                is_first_ordered_task=_is_first_ordered_task(ctx.task),
            )
            if not plan_verdict.accepted and (plan_verdict.details or {}).get(
                "unmaterialized_expected_files"
            ):
                pruned_plan, prune_details = _prune_unmaterialized_expected_files(
                    ctx.orchestration_state.plan,
                    (plan_verdict.details or {}).get("unmaterialized_expected_files")
                    or [],
                )
                if prune_details.get("changed"):
                    ctx.orchestration_state.plan = pruned_plan
                    output_text = json.dumps(pruned_plan)
                    ctx.logger.info(
                        "[ORCHESTRATION] Pruned unmaterialized expected_files from plan: %s",
                        prune_details,
                    )
                    emit_phase_event(
                        ctx.orchestration_state,
                        ctx.emit_live,
                        level="INFO",
                        phase="planning",
                        message="[ORCHESTRATION] Pruned unmaterialized expected_files from plan",
                        details=prune_details,
                    )
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
                        workflow_stage=ctx.workflow_stage,
                        is_first_ordered_task=_is_first_ordered_task(ctx.task),
                    )
            if not plan_verdict.accepted and (
                (plan_verdict.details or {}).get("read_only_stage_mutation_steps")
                or (plan_verdict.details or {}).get("missing_workspace_expected_files")
                or (plan_verdict.details or {}).get("unmaterialized_expected_files")
                or (plan_verdict.details or {}).get(
                    "read_only_stage_failable_probe_steps"
                )
                or (plan_verdict.details or {}).get("weak_verification_steps")
            ):
                fallback_plan = _read_only_stage_fallback_plan(ctx)
                if fallback_plan:
                    verdict_details = plan_verdict.details or {}
                    emit_phase_event(
                        ctx.orchestration_state,
                        ctx.emit_live,
                        level="WARN",
                        phase="planning",
                        message=(
                            "[ORCHESTRATION] Replaced mutating read-only stage plan "
                            "with deterministic inspection plan"
                        ),
                        details={
                            "reason": "read_only_stage_plan_normalized",
                            "workflow_stage": ctx.workflow_stage,
                            "mutating_steps": verdict_details.get(
                                "read_only_stage_mutation_steps"
                            ),
                            "missing_workspace_expected_files": verdict_details.get(
                                "missing_workspace_expected_files"
                            ),
                            "unmaterialized_expected_files": verdict_details.get(
                                "unmaterialized_expected_files"
                            ),
                            "failable_probe_steps": verdict_details.get(
                                "read_only_stage_failable_probe_steps"
                            ),
                            "weak_verification_steps": verdict_details.get(
                                "weak_verification_steps"
                            ),
                        },
                    )
                    ctx.orchestration_state.plan = fallback_plan
                    output_text = json.dumps(fallback_plan)
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
                        workflow_stage=ctx.workflow_stage,
                        is_first_ordered_task=_is_first_ordered_task(ctx.task),
                    )
            if (
                _is_first_ordered_task(ctx.task)
                and not plan_verdict.accepted
                and _task1_bootstrap_contract_passed(plan_verdict)
                and _task1_plan_failed_only_brittle_command_shape(plan_verdict)
            ):
                normalized_plan = _normalize_task1_bootstrap_plan_for_json_stability(
                    ctx.orchestration_state.plan
                )
                if normalized_plan != ctx.orchestration_state.plan:
                    emit_phase_event(
                        ctx.orchestration_state,
                        ctx.emit_live,
                        level="INFO",
                        phase="planning",
                        message=(
                            "[ORCHESTRATION] Normalized Task 1 bootstrap plan "
                            "before repair by preferring typed file ops over "
                            "malformed shell command text"
                        ),
                        details={
                            "reason": "task1_bootstrap_json_stability_normalized",
                            "step_count": len(normalized_plan),
                        },
                    )
                    ctx.orchestration_state.plan = normalized_plan
                    output_text = json.dumps(normalized_plan)
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
                        workflow_stage=ctx.workflow_stage,
                        is_first_ordered_task=_is_first_ordered_task(ctx.task),
                    )
            if _is_first_ordered_task(ctx.task) and _task1_bootstrap_contract_passed(
                plan_verdict
            ):
                normalized_plan = _normalize_task1_python_src_layout_verification(
                    ctx.orchestration_state.plan, plan_verdict
                )
                if normalized_plan != ctx.orchestration_state.plan:
                    ctx.orchestration_state.plan = normalized_plan
                    output_text = json.dumps(normalized_plan)
            record_validation_verdict(
                ctx.db,
                ctx.session_id,
                ctx.task_id,
                ctx.orchestration_state,
                plan_verdict.verdict,
                parent_event_id=(planning_phase_event or {}).get("event_id"),
            )
            _emit_task1_bootstrap_contract_event(ctx, plan_verdict)
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
            if not plan_verdict.accepted:
                _record_repair_root_cause(
                    retry_state,
                    root_cause=_repair_root_cause_from_plan_verdict(plan_verdict),
                    stage="planning_validation",
                )
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
                    knowledge_types=[
                        "failure_memory",
                        "format_guide",
                        "debug_case",
                    ],
                    query="Plan validation failed: "
                    + "; ".join(plan_verdict.reasons[:3]),
                    failure_signature=(
                        plan_verdict.reasons[0] if plan_verdict.reasons else None
                    ),
                )
                if validation_knowledge_ctx:
                    _log_knowledge_usage(
                        ctx, validation_knowledge_ctx, used_in_prompt=True
                    )
                retry_state.last_repair_reason = "plan_validation_failed"
                if "verification_mutates_source_assets" in semantic_violation_codes:
                    retry_state.vma_repair_triggered = True
                retry_state.task1_bootstrap_rejection_contract = (
                    plan_verdict.details or {}
                ).get("task1_bootstrap_contract")
                repair_rejection_reasons = _build_repair_rejection_reasons(
                    plan_verdict.reasons,
                    plan_verdict.details,
                )
                planning_result = __repair_planning_output(
                    ctx=ctx,
                    retry_state=retry_state,
                    planning_timeout_seconds=planning_timeout_seconds,
                    malformed_output=output_text,
                    reason="plan_validation_failed: "
                    + "; ".join(plan_verdict.reasons[:3]),
                    rejection_reasons=repair_rejection_reasons,
                    prompt_profile=prompt_profile,
                    knowledge_context=_usable_knowledge_context(
                        validation_knowledge_ctx
                    ),
                )
                retry_state.repair_prompt_used = True
                retry_state.consecutive_failures += 1
                continue

            if not plan_verdict.accepted:
                if retry_state.repair_prompt_used:
                    repeated_src_import_result = (
                        _abort_repeated_physical_src_import_repair(
                            ctx=ctx,
                            plan_verdict=plan_verdict,
                            output_text=output_text,
                        )
                    )
                    if repeated_src_import_result:
                        return repeated_src_import_result
                    oscillation_result = _abort_root_cause_oscillation_repair_loop(
                        ctx=ctx,
                        retry_state=retry_state,
                    )
                    if oscillation_result:
                        return oscillation_result

                second_repair_reason = _get_targeted_second_repair_reason(
                    retry_state=retry_state,
                    blocking_repair_issues=blocking_repair_issues,
                    plan_verdict=plan_verdict,
                    project_dir=ctx.orchestration_state.project_dir,
                )
                if second_repair_reason and not second_repair_reason.cap_used:
                    _record_repair_root_cause(
                        retry_state,
                        root_cause=_planning_root_cause_from_issue_key(
                            second_repair_reason.issue_key
                        ),
                        stage=second_repair_reason.event_reason,
                    )
                    oscillation_result = _abort_root_cause_oscillation_repair_loop(
                        ctx=ctx,
                        retry_state=retry_state,
                    )
                    if oscillation_result:
                        return oscillation_result
                    if second_repair_reason.issue_key == "task1_bootstrap_contract":
                        issue_fragments = (
                            _task1_bootstrap_second_repair_rejection_reasons(
                                retry_state=retry_state,
                                plan_verdict=plan_verdict,
                                rejection_text=second_repair_reason.rejection_text,
                            )
                        )
                    else:
                        issue_fragments = [second_repair_reason.rejection_text]
                    if second_repair_reason.issue_key in (
                        "stale_replace_ops_steps",
                        "empty_replace_old_text_steps",
                    ):
                        issue_fragments.extend(
                            PlannerService.stale_replace_fallback_hints(
                                ctx.orchestration_state.plan,
                                ctx.orchestration_state.project_dir,
                            )
                        )
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
                            "[ORCHESTRATION] Planning repair still failed a "
                            "targeted validator issue; starting one targeted "
                            "second repair pass"
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
                        "[ORCHESTRATION] Planning repair still failed %s in steps %s; "
                        "starting one targeted second repair pass",
                        second_repair_reason.issue_label,
                        second_repair_reason.step_numbers,
                    )
                    validation_knowledge_ctx = _retrieve_knowledge(
                        ctx,
                        trigger_phase="validation",
                        knowledge_types=[
                            "failure_memory",
                            "format_guide",
                            "debug_case",
                        ],
                        query="Plan validation failed after repair: "
                        + "; ".join(plan_verdict.reasons[:3]),
                        failure_signature=(
                            plan_verdict.reasons[0] if plan_verdict.reasons else None
                        ),
                    )
                    if validation_knowledge_ctx:
                        _log_knowledge_usage(
                            ctx, validation_knowledge_ctx, used_in_prompt=True
                        )
                    retry_state.last_repair_reason = second_repair_reason.event_reason
                    planning_result = __repair_planning_output(
                        ctx=ctx,
                        retry_state=retry_state,
                        planning_timeout_seconds=planning_timeout_seconds,
                        malformed_output=output_text,
                        reason=f"{second_repair_reason.retry_reason}: "
                        + "; ".join(issue_fragments),
                        rejection_reasons=issue_fragments,
                        prompt_profile=prompt_profile,
                        knowledge_context=_usable_knowledge_context(
                            validation_knowledge_ctx
                        ),
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
                    details={
                        **_terminal_validation_failure_details(plan_verdict),
                        "planning_root_cause": _terminal_planning_root_cause(
                            retry_state
                        ),
                    },
                )
                failure_reason = "Plan validation failed after repair: " + "; ".join(
                    plan_verdict.reasons[:4]
                )
                _finalize_planning_terminal_failure(
                    ctx=ctx,
                    failure_type="planning_validation_failed_after_repair",
                    failure_reason=failure_reason,
                    planning_root_cause=_terminal_planning_root_cause(retry_state),
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
    except TaskOperationContractViolation as exc:
        failure_type = "op_contract_violation"
        ctx.orchestration_state.status = OrchestrationStatus.ABORTED
        ctx.orchestration_state.abort_reason = f"Operation contract violation: {exc}"
        emit_phase_event(
            ctx.orchestration_state,
            ctx.emit_live,
            level="ERROR",
            phase="planning",
            message=f"[ORCHESTRATION] Planning output blocked: {exc}",
            details={"reason": failure_type},
        )
        _finalize_planning_terminal_failure(
            ctx=ctx,
            failure_type=failure_type,
            failure_reason=str(exc),
        )
        if ctx.restore_workspace_snapshot_if_needed:
            ctx.restore_workspace_snapshot_if_needed("operation contract violation")
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
        root_cause = "repair_timeout" if is_repair_timeout else "unknown"
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
            details={"reason": failure_type, "planning_root_cause": root_cause},
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
                    "planning_root_cause": root_cause,
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
            planning_root_cause=root_cause,
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
        root_cause = "repair_timeout" if "repair" in failure_type else "unknown"
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
            details={"reason": failure_type, "planning_root_cause": root_cause},
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
                    "planning_root_cause": root_cause,
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
            planning_root_cause=root_cause,
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
    knowledge_context: KnowledgeContext | None = None,
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
        knowledge_context=_usable_knowledge_context(knowledge_context),
        validation_profile=_planning_validation_profile(ctx),
    )


def __repair_planning_output(
    *,
    ctx: OrchestrationRunContext,
    retry_state: _PlanningRetryState | None = None,
    planning_timeout_seconds: int,
    malformed_output: str,
    reason: str,
    rejection_reasons: list[str] | None = None,
    prompt_profile: str = "default",
    knowledge_context: KnowledgeContext | None = None,
) -> Dict[str, Any]:
    if retry_state and _repair_context_requires_source_materialization(
        execution_profile=ctx.execution_profile,
        reason=reason,
        rejection_reasons=rejection_reasons,
    ):
        retry_state.source_materialization_required_after_repair = True
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
        str(output_result)[:1500],
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
