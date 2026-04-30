"""Planning-phase orchestration flow."""

from __future__ import annotations

import asyncio
import json
import logging
from typing import Any, Callable, Dict

from celery.exceptions import SoftTimeLimitExceeded

from app.models import TaskStatus
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
    write_orchestration_state_snapshot,
)
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.policy import (
    PLANNING_REPAIR_TIMEOUT_SECONDS,
    clamp_planning_timeout,
)
from app.services.orchestration.task_rules import get_workflow_profile
from app.services.orchestration.workflow_profiles import get_workflow_phases
from app.services.orchestration.types import OrchestrationRunContext
from app.services.orchestration.types import ReasoningArtifact
from app.services.orchestration.validation.parsing import (
    extract_plan_steps_from_summary_text,
)
from app.services.orchestration.validation.validator import ValidatorService
from app.services.prompt_templates import OrchestrationStatus, estimate_token_count

# Circuit breaker: abort planning after this many consecutive validation failures
# to prevent infinite retry loops that hang the session.
MAX_PLANNING_RETRIES = 3


def _compress_project_context_for_planning(
    orchestration_state: Any,
    *,
    max_chars: int = 2800,
) -> str:
    current_context = str(getattr(orchestration_state, "project_context", "") or "")
    if len(current_context) <= max_chars:
        return current_context

    compact_state_summary = compress_orchestration_context(
        orchestration_state, max_chars=max_chars // 2
    )
    if compact_state_summary and compact_state_summary != "Step progress: 0/0":
        return compact_state_summary

    normalized = " ".join(current_context.split())
    if len(normalized) <= max_chars:
        return normalized

    head = normalized[: max_chars // 2].rstrip()
    tail = normalized[-(max_chars // 3) :].lstrip()
    return f"{head}\n...\n{tail}"


def _build_reasoning_artifact(
    *,
    ctx: OrchestrationRunContext,
    workspace_review: Dict[str, Any],
) -> Dict[str, Any]:
    plan = list(ctx.orchestration_state.plan or [])
    workspace_facts = [
        f"project_dir={ctx.orchestration_state.project_dir}",
        f"execution_profile={ctx.execution_profile}",
        f"workflow_profile={getattr(ctx, 'workflow_profile', 'default')}",
    ]
    if workspace_review.get("has_existing_files"):
        workspace_facts.append("workspace already contains project files")
    file_count = int(workspace_review.get("file_count") or 0)
    source_file_count = int(workspace_review.get("source_file_count") or 0)
    if file_count or source_file_count:
        workspace_facts.append(
            f"workspace inventory shows {file_count} files and {source_file_count} source files"
        )
    review_summary = str(workspace_review.get("summary") or "").strip()
    if review_summary:
        workspace_facts.append(review_summary[:180])

    planned_actions = [
        str(step.get("description") or f"Step {index + 1}").strip()
        for index, step in enumerate(plan[:8])
        if str(step.get("description") or "").strip()
    ]
    verification_plan = []
    for step in plan[:8]:
        verification = str(step.get("verification") or "").strip()
        if verification:
            verification_plan.append(verification)
        elif step.get("expected_files"):
            verification_plan.append(
                "materialize expected files: "
                + ", ".join(
                    str(item) for item in (step.get("expected_files") or [])[:4]
                )
            )

    artifact = ReasoningArtifact(
        intent=" ".join(str(ctx.prompt or "").split())[:220],
        workspace_facts=workspace_facts[:8],
        planned_actions=planned_actions[:8],
        verification_plan=verification_plan[:8] or ["verify each planned step outcome"],
    )
    return artifact.to_dict()


def _should_repair_truncated_single_step_plan(
    *,
    prompt_profile: str,
    extracted_plan: list[dict[str, Any]] | None,
    execution_profile: str,
) -> bool:
    """Route compressed local-Qwen plans into repair instead of execution."""

    return (
        prompt_profile == "local_qwen_json_array"
        and execution_profile == "full_lifecycle"
        and isinstance(extracted_plan, list)
        and len(extracted_plan) == 1
    )


class _PlanningRetryState:
    """Track retry/repair attempts to implement circuit breaking."""

    def __init__(self):
        self.consecutive_failures = 0
        self.minimal_prompt_used = False
        self.repair_prompt_used = False

    @property
    def circuit_open(self) -> bool:
        return self.consecutive_failures >= MAX_PLANNING_RETRIES


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
    planning_prompt = (
        assemble_planning_prompt(ctx, workspace_review) if ctx.runtime_service else None
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
            workflow_profile=getattr(ctx, "workflow_profile", "default"),
            workflow_phases=getattr(ctx, "workflow_phases", []),
            workspace_has_existing_files=getattr(
                ctx, "workspace_has_existing_files", False
            ),
        )
    else:
        planning_result = asyncio.run(
            ctx.runtime_service.execute_task(
                planning_prompt, timeout_seconds=planning_timeout_seconds
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
            workflow_profile=getattr(ctx, "workflow_profile", "default"),
            workflow_phases=getattr(ctx, "workflow_phases", []),
            workspace_has_existing_files=getattr(
                ctx, "workspace_has_existing_files", False
            ),
        )
        used_minimal_planning_prompt = True

    try:
        retry_state = _PlanningRetryState()
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
                ctx.task.status = TaskStatus.FAILED
                ctx.task.error_message = (
                    f"Planning failed {MAX_PLANNING_RETRIES} consecutive times. "
                    "The agent was unable to produce a valid execution plan."
                )
                ctx.db.commit()
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
                    success = True
                    plan_data = extracted_summary_plan
                    strategy_info = "Recovered plan from prose summary text"
                    ctx.logger.info(
                        "[ORCHESTRATION] Recovered planning steps from prose summary output"
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

            if not success and not retry_state.minimal_prompt_used:
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
                ctx.logger.info(
                    "[ORCHESTRATION] Calling repair pass for planning output"
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
            if extracted_plan is None and not retry_state.minimal_prompt_used:
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
                ctx.logger.info(
                    "[ORCHESTRATION] Plan extraction failed, calling repair"
                )
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
                    planning_result = __repair_planning_output(
                        ctx=ctx,
                        planning_timeout_seconds=planning_timeout_seconds,
                        malformed_output=output_text,
                        reason="truncated_multistep_plan_detected",
                        prompt_profile=prompt_profile,
                    )
                    retry_state.repair_prompt_used = True
                    retry_state.consecutive_failures += 1
                    continue
                else:
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
                planning_result = __repair_planning_output(
                    ctx=ctx,
                    planning_timeout_seconds=planning_timeout_seconds,
                    malformed_output=output_text,
                    reason="truncated_multistep_plan_after_minimal",
                    prompt_profile=prompt_profile,
                )
                retry_state.repair_prompt_used = True
                retry_state.consecutive_failures += 1
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

            sanitized_plan = PlannerService.sanitize_common_plan_issues(extracted_plan)
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
            immediate_repair_issues = PlannerService.find_immediate_repair_step_issues(
                ctx.orchestration_state.plan
            )
            blocking_issue_keys = (
                "non_runnable_steps",
                "background_process_steps",
                "placeholder_only_steps",
            )
            blocking_repair_issues = {
                key: value
                for key, value in immediate_repair_issues.items()
                if key in blocking_issue_keys and value
            }
            if blocking_repair_issues and not retry_state.repair_prompt_used:
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
                ctx.orchestration_state.status = OrchestrationStatus.ABORTED
                ctx.orchestration_state.abort_reason = "Planning repair still produced non-runnable or long-running commands"
                ctx.task.status = TaskStatus.FAILED
                ctx.task.error_message = (
                    "Planning repair still produced invalid commands: "
                    + "; ".join(
                        f"{key}={value[:5]}"
                        for key, value in blocking_repair_issues.items()
                    )
                )
                ctx.db.commit()
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
                workflow_profile=getattr(ctx, "workflow_profile", None),
            )
            record_validation_verdict(
                ctx.db,
                ctx.session_id,
                ctx.task_id,
                ctx.orchestration_state,
                plan_verdict,
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
                ctx.logger.warning(
                    "[ORCHESTRATION] Plan validation failed, calling repair (failure_count=%d)",
                    retry_state.consecutive_failures,
                )
                planning_result = __repair_planning_output(
                    ctx=ctx,
                    planning_timeout_seconds=planning_timeout_seconds,
                    malformed_output=output_text,
                    reason="plan_validation_failed: "
                    + "; ".join(plan_verdict.reasons[:3]),
                    rejection_reasons=plan_verdict.reasons,
                    prompt_profile=prompt_profile,
                )
                retry_state.repair_prompt_used = True
                retry_state.consecutive_failures += 1
                continue

            if not plan_verdict.accepted:
                planning_result = __repair_planning_output(
                    ctx=ctx,
                    planning_timeout_seconds=planning_timeout_seconds,
                    malformed_output=output_text,
                    reason="plan_validation_failed_after_repair: "
                    + "; ".join(plan_verdict.reasons[:3]),
                    rejection_reasons=plan_verdict.reasons,
                    prompt_profile=prompt_profile,
                )
                ctx.logger.warning(
                    "[ORCHESTRATION] Plan validation failed after repair; requesting a fresh planning result (failure_count=%d)",
                    retry_state.consecutive_failures,
                )
                retry_state.consecutive_failures += 1
                retry_state.repair_prompt_used = True
                continue

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
                ctx.task.status = TaskStatus.FAILED
                ctx.task.error_message = (
                    "Structured reasoning artifact failed validation: "
                    + "; ".join(reasoning_verdict.reasons[:4])
                )
                ctx.db.commit()
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
    except TimeoutError as exc:
        ctx.logger.error(
            "[ORCHESTRATION] Planning timed out or exceeded context before a valid plan was produced: %s",
            exc,
        )
        ctx.orchestration_state.status = OrchestrationStatus.ABORTED
        ctx.orchestration_state.abort_reason = (
            f"Planning timed out or exceeded context: {exc}"
        )
        emit_phase_event(
            ctx.orchestration_state,
            ctx.emit_live,
            level="ERROR",
            phase="planning",
            message=f"[ORCHESTRATION] Planning timed out or exceeded context: {exc}",
            details={"reason": "planning_timeout_or_context_overflow"},
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
                    "status": "timeout_or_context_overflow",
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
        ctx.task.status = TaskStatus.FAILED
        ctx.task.error_message = str(exc)
        ctx.db.commit()
        if ctx.restore_workspace_snapshot_if_needed:
            ctx.restore_workspace_snapshot_if_needed(
                "planning timeout or context overflow"
            )
        return {"status": "failed", "reason": "planning_timeout_or_context_overflow"}
    except SoftTimeLimitExceeded as exc:
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
            details={"reason": "planning_soft_time_limit_exceeded"},
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
        ctx.task.status = TaskStatus.FAILED
        ctx.task.error_message = "Planning exceeded the worker soft time limit before a valid plan was produced"
        ctx.db.commit()
        if ctx.restore_workspace_snapshot_if_needed:
            ctx.restore_workspace_snapshot_if_needed(
                "planning soft time limit exceeded"
            )
        return {"status": "failed", "reason": "planning_soft_time_limit_exceeded"}
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
        workflow_profile=getattr(ctx, "workflow_profile", "default"),
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
        workflow_profile=getattr(ctx, "workflow_profile", "default"),
        workflow_phases=getattr(ctx, "workflow_phases", []),
        workspace_has_existing_files=getattr(
            ctx, "workspace_has_existing_files", False
        ),
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
