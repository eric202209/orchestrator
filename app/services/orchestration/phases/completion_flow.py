"""Task completion and finalization flow."""

import asyncio
import json
import os
from pathlib import Path
from datetime import UTC, datetime
from typing import Any, Callable, Dict, Optional

from app.models import TaskExecution, TaskStatus
from app.config import settings
from app.services.error_handler import error_handler
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.events.telemetry import emit_phase_event
from app.services.orchestration.diagnostics.debug_feedback import (
    build_debug_feedback_envelope,
    persist_debug_feedback_envelope,
)
from app.services.orchestration.diagnostics.evidence_capsule import (
    collect_workspace_evidence,
)
from app.services.orchestration.phases.completion_repair_capsule import (
    build_bounded_completion_repair_prompt,
    build_completion_repair_capsule,
)
from app.services.orchestration.context.assembly import (
    assemble_execution_prompt,
    assemble_task_summary_prompt,
    render_adapted_runtime_prompt,
)
from app.services.workspace.workspace_paths import TASK_REPORT_ROOT
from app.services.orchestration.execution.execution_flow import (
    assess_step_execution,
    determine_step_timeout,
)
from app.services.orchestration.execution.runtime import (
    workspace_snapshot_key,
    write_project_state_snapshot,
)
from app.services.orchestration.execution.step_support import (
    coerce_execution_step_result,
)
from app.services.orchestration.execution.repair_governor import check_repair_churn
from app.services.orchestration.lifecycle.completion import TaskCompletionFinalizer
from app.services.orchestration.state.persistence import (
    append_orchestration_event,
    attach_failure_envelope,
    record_validation_verdict,
    save_orchestration_checkpoint,
)
from app.services.orchestration.policy import (
    COMPLETION_REPAIR_TIMEOUT_SECONDS,
)
from app.runtime_naming import (
    completion_repair_prompt_mode_alias_details,
)
from app.services.orchestration.review_policy import decide_change_set_review
from app.services.orchestration.run_state import (
    mark_task_attempt_failed,
)
from app.services.orchestration.state.session_state import (
    mark_session_paused,
)
from app.services.orchestration.types import (
    FailureEnvelope,
    OrchestrationRunContext,
    ValidationVerdict,
)
from app.services.orchestration.validation.parsing import (
    build_json_compliance_retry_prompt,
    extract_structured_text,
)
from app.services.orchestration.validation.validator import ValidatorService
from app.services.orchestration.validation.integrity import (
    capture_baseline_result,
    compare_baseline,
)
from app.services.workspace.permissions import ensure_shared_permissions
from app.services.workspace.system_settings import get_effective_workspace_review_policy
from app.services.prompt_templates import OrchestrationStatus, StepResult
from app.services.orchestration.phases.completion_repair import (
    _augment_completion_verification_command,
    _classify_completion_verification_failure,
    _completion_failure_signature,
    _completion_repair_invalid_paths,
    _detect_completion_verification_command,
    _execute_completion_verification,
    _extract_completion_repair_step,
    _extract_reported_changed_files,
    _repeats_prior_completion_failure,
    _salvage_completion_repair_json_text,
)
from app.services.orchestration.phases.completion_summary import (
    _deterministic_task_summary,
    _generate_task_summary_with_fallback,
)
from app.services.orchestration.phases.completion_workspace import (
    _completion_expected_paths,
    _scope_workspace_consistency_to_task_changes,
    _stack_set_for_paths,
)

__all__ = [
    "_attempt_completion_repair",
    "_augment_completion_verification_command",
    "_classify_completion_verification_failure",
    "_execute_completion_verification",
    "_run_evaluator",
    "finalize_successful_task",
]


_OPENCLAW_DIAGNOSTIC_KEYS = {
    "aborted",
    "source",
    "generatedAt",
    "workspaceDir",
    "systemPrompt",
    "sandbox",
    "bootstrapMaxChars",
}
_VISIBLE_TEXT_KEYS = {
    "finalAssistantVisibleText",
    "final_assistant_visible_text",
    "text",
    "output_text",
    "content_text",
}


def _resolve_template_review_policy(task: Any) -> Optional[dict]:
    template_id = getattr(task, "template_id", None)
    if not template_id:
        return None
    try:
        from app.services.orchestration.workflow_templates import get_template

        tmpl = get_template(template_id)
        if not tmpl:
            return None
        policy = dict(tmpl.review_policy)
        policy["auto_promote_eligible"] = tmpl.auto_promote_eligible
        policy["allowed_ops"] = tmpl.allowed_ops
        return policy
    except Exception:
        return None


def _extract_completion_repair_json_text(value: Any) -> str:
    """Preserve direct repair JSON while still unwrapping OpenClaw payloads."""

    if not isinstance(value, str):
        return extract_structured_text(value)

    stripped = value.strip()
    if not stripped:
        return ""

    try:
        parsed = json.loads(stripped)
    except json.JSONDecodeError:
        return extract_structured_text(value)

    if isinstance(parsed, (dict, list)):
        if isinstance(parsed, dict) and (
            _VISIBLE_TEXT_KEYS.intersection(parsed.keys())
            or _OPENCLAW_DIAGNOSTIC_KEYS.intersection(parsed.keys())
        ):
            return extract_structured_text(value)
        return stripped

    return extract_structured_text(value)


def _attempt_completion_repair(
    *,
    ctx: OrchestrationRunContext,
    completion_validation: Any,
    save_orchestration_checkpoint_fn: Callable[..., None],
) -> Dict[str, Any]:
    orchestration_state = ctx.orchestration_state
    emit_live = ctx.emit_live
    logger = ctx.logger
    task = ctx.task
    db = ctx.db
    session = ctx.session
    runtime_metadata = (
        ctx.runtime_service.get_backend_metadata()
        if ctx.runtime_service and hasattr(ctx.runtime_service, "get_backend_metadata")
        else {}
    )
    failure_envelope = FailureEnvelope(
        session_id=ctx.session_id,
        task_id=ctx.task_id,
        phase="completion_repair",
        step_index=len(orchestration_state.plan) + 1,
        model_id=":".join(
            part
            for part in [
                str(runtime_metadata.get("backend") or "").strip(),
                str(runtime_metadata.get("model_family") or "").strip(),
            ]
            if part
        ),
        input={
            "expected_core_files": list(
                (getattr(completion_validation, "details", {}) or {}).get(
                    "expected_core_files", []
                )[:20]
            ),
            "reasons": list(getattr(completion_validation, "reasons", []) or [])[:10],
        },
        output={
            "validation_status": str(getattr(completion_validation, "status", "")),
            "details": dict(getattr(completion_validation, "details", {}) or {}),
        },
        stderr=str(
            (getattr(completion_validation, "details", {}) or {}).get(
                "verification_output_preview"
            )
            or ""
        )[:1200],
        root_cause="validation_failure",
    )
    debug_feedback_envelope = build_debug_feedback_envelope(
        task_execution_id=ctx.task_execution_id,
        task_id=ctx.task_id,
        step_index=len(orchestration_state.plan) + 1,
        failure_phase=str(getattr(completion_validation, "stage", "completion")),
        failed_command=str(
            (getattr(completion_validation, "details", {}) or {}).get(
                "verification_command"
            )
            or ""
        ),
        stdout="",
        stderr=str(
            (getattr(completion_validation, "details", {}) or {}).get(
                "verification_output_preview"
            )
            or ""
        ),
        validator_reasons=list(getattr(completion_validation, "reasons", []) or [])[
            :10
        ],
        changed_files=list(getattr(orchestration_state, "changed_files", []) or [])[
            :20
        ],
        workspace_path=orchestration_state.project_dir,
    )
    next_attempt = orchestration_state.completion_repair_attempts + 1
    if next_attempt > ctx.completion_repair_budget:
        return {"status": "skipped", "reason": "repair_attempt_limit_reached"}

    churn_stop, churn_trigger = check_repair_churn(
        db,
        session_id=ctx.session_id,
        task_id=ctx.task_id,
        completion_repair_attempts=orchestration_state.completion_repair_attempts,
        model_lane_label=getattr(session, "model_lane_label", None),
    )
    if churn_stop:
        session.repair_churn_stopped = True
        session.repair_churn_trigger = churn_trigger
        try:
            db.commit()
        except Exception:
            db.rollback()
        emit_live(
            "ERROR",
            f"[ORCHESTRATION] Repair churn limit reached ({churn_trigger}); routing to operator review",
            metadata={
                "phase": "completion_repair",
                "repair_churn_trigger": churn_trigger,
                "completion_repair_attempts": orchestration_state.completion_repair_attempts,
            },
        )
        emit_phase_event(
            ctx.orchestration_state,
            ctx.emit_live,
            level="ERROR",
            phase="completion_repair",
            message="Repair churn limit reached — operator review required",
            details={"repair_churn_trigger": churn_trigger},
        )
        return {"status": "failed", "reason": "repair_churn_limit"}

    if (
        orchestration_state.completion_repair_attempts > 0
        and _repeats_prior_completion_failure(
            orchestration_state, completion_validation
        )
    ):
        repeated_signature = _completion_failure_signature(completion_validation)
        emit_live(
            "ERROR",
            "[ORCHESTRATION] Completion validation failed with the same root-cause signature after a prior repair; stopping instead of looping",
            metadata={
                "phase": "completion_repair",
                "failure_signature": repeated_signature,
                "attempt": orchestration_state.completion_repair_attempts,
            },
        )
        append_orchestration_event(
            project_dir=orchestration_state.project_dir,
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            event_type=EventType.REPAIR_REJECTED,
            details=attach_failure_envelope(
                {
                    "phase": "completion_repair",
                    "reason": "repeat_completion_failure_signature",
                    "failure_signature": repeated_signature,
                },
                failure_envelope,
            ),
        )
        return {
            "status": "failed",
            "reason": "repeat_completion_failure_signature",
        }

    orchestration_state.completion_repair_attempts = next_attempt
    next_step_number = len(orchestration_state.plan) + 1
    repair_capsule = build_completion_repair_capsule(
        task_prompt=ctx.prompt,
        completion_validation=completion_validation,
        orchestration_state=orchestration_state,
    )
    _evidence_capsule = collect_workspace_evidence(
        debug_feedback_envelope.failure_class,
        orchestration_state.project_dir,
        failure_context=debug_feedback_envelope.stderr_excerpt,
    )
    persist_debug_feedback_envelope(
        db=db,
        session_id=ctx.session_id,
        task_id=ctx.task_id,
        session_instance_id=ctx.session_instance_id,
        project_dir=orchestration_state.project_dir,
        envelope=debug_feedback_envelope,
        evidence_capsule=_evidence_capsule,
    )
    if _evidence_capsule and not _evidence_capsule.is_empty():
        append_orchestration_event(
            project_dir=orchestration_state.project_dir,
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            event_type=EventType.WORKSPACE_EVIDENCE_COLLECTED,
            details={
                "phase": "completion_repair",
                "failure_class": debug_feedback_envelope.failure_class,
                "evidence_chars_total": _evidence_capsule.total_chars,
                "evidence_files_inspected": _evidence_capsule.files_inspected,
                "evidence_matched_lines": _evidence_capsule.matched_line_count,
                "commands_run": _evidence_capsule.commands_run,
            },
        )
    db.commit()

    emit_live(
        "WARN",
        "[ORCHESTRATION] Completion validation is repairable; generating a minimal repair step",
        metadata={
            "phase": "completion_repair",
            "attempt": orchestration_state.completion_repair_attempts,
            "reasons": completion_validation.reasons[:10],
        },
    )
    repair_generated_details = attach_failure_envelope(
        {
            "phase": "completion_repair",
            "attempt": orchestration_state.completion_repair_attempts,
            "reasons": completion_validation.reasons[:10],
            **completion_repair_prompt_mode_alias_details(),
            "capsule_relevant_file_count": len(repair_capsule.relevant_files),
            "capsule_last_step_present": bool(repair_capsule.last_step_summary),
            "envelope_mode": "direct_capsule",
            "compliance_retry_attempted": False,
            "compliance_retry_succeeded": False,
            "completion_repair_source": (
                (getattr(completion_validation, "details", {}) or {}).get(
                    "completion_repair_source"
                )
            ),
            "verification_command": (
                (getattr(completion_validation, "details", {}) or {}).get(
                    "verification_command"
                )
            ),
            "failure_class": (
                (getattr(completion_validation, "details", {}) or {}).get(
                    "failure_class"
                )
            ),
        },
        failure_envelope,
    )
    append_orchestration_event(
        project_dir=orchestration_state.project_dir,
        session_id=ctx.session_id,
        task_id=ctx.task_id,
        event_type=EventType.DEBUG_REPAIR_ATTEMPTED,
        details={
            "phase": "completion",
            "debug_repair_attempted": True,
            "debug_repair_used": True,
            "debug_failure_class": debug_feedback_envelope.failure_class,
            "debug_repair_step_count": 1,
            "debug_repair_validator_reasons": list(
                getattr(completion_validation, "reasons", []) or []
            )[:10],
            "task_execution_id": ctx.task_execution_id,
            "allowed": (
                debug_feedback_envelope.eligible_for_debug_repair
                and next_attempt <= ctx.completion_repair_budget
            ),
            "allowed_reason": "eligible completion failure class within budget",
            "envelope_mode": "direct_capsule",
            "compliance_retry_attempted": False,
            "compliance_retry_succeeded": False,
        },
    )

    raw_repair_prompt = build_bounded_completion_repair_prompt(
        repair_capsule,
        next_step_number,
        _evidence_capsule,
    )
    repair_prompt = render_adapted_runtime_prompt(
        ctx.db,
        objective="Generate a minimal repair step that resolves task-completion validation failures.",
        execution_mode="completion_repair",
        prompt_body=raw_repair_prompt,
        instructions=[
            "Return one machine-runnable repair step only.",
            "Use only inventory-confirmed paths or create new files explicitly.",
        ],
        context={
            "Project Directory": str(orchestration_state.project_dir),
            "Repair Attempt": orchestration_state.completion_repair_attempts,
            "Next Step Number": next_step_number,
        },
        expected_output="JSON object describing one repair step.",
        direct=True,
    )
    repair_plan_result = asyncio.run(
        ctx.runtime_service.execute_task(
            repair_prompt, timeout_seconds=COMPLETION_REPAIR_TIMEOUT_SECONDS
        )
    )
    repair_output = _extract_completion_repair_json_text(
        repair_plan_result.get("output", "{}")
    )
    repair_output = _salvage_completion_repair_json_text(repair_output)
    success, repair_data, strategy_info = error_handler.attempt_json_parsing(
        repair_output, context="completion_repair"
    )
    if not success:
        fallback_output = extract_structured_text(repair_plan_result)
        if fallback_output and fallback_output != repair_output:
            fallback_output = _salvage_completion_repair_json_text(fallback_output)
            success, repair_data, strategy_info = error_handler.attempt_json_parsing(
                fallback_output, context="completion_repair"
            )

    if not success:
        repair_generated_details["compliance_retry_attempted"] = True
        compliance_prompt = build_json_compliance_retry_prompt(
            repair_output,
            expected_shape="object",
        )
        try:
            compliance_result = asyncio.run(
                ctx.runtime_service.execute_task(
                    compliance_prompt,
                    timeout_seconds=COMPLETION_REPAIR_TIMEOUT_SECONDS,
                )
            )
            compliance_output = _extract_completion_repair_json_text(
                compliance_result.get("output", "{}")
            )
            compliance_output = _salvage_completion_repair_json_text(compliance_output)
            success, repair_data, strategy_info = error_handler.attempt_json_parsing(
                compliance_output, context="completion_repair_compliance_retry"
            )
        except Exception as compliance_error:
            success = False
            repair_data = None
            strategy_info = f"Compliance retry failed: {str(compliance_error)[:200]}"
        repair_generated_details["compliance_retry_succeeded"] = bool(success)

    append_orchestration_event(
        project_dir=orchestration_state.project_dir,
        session_id=ctx.session_id,
        task_id=ctx.task_id,
        event_type=EventType.REPAIR_GENERATED,
        details=repair_generated_details,
    )

    if not success:
        logger.warning(
            "[ORCHESTRATION] Completion repair step generation failed to parse: %s",
            strategy_info,
        )
        return {
            "status": "failed",
            "reason": f"repair_step_parse_failed:{strategy_info}",
        }

    repair_step = _extract_completion_repair_step(repair_data, next_step_number)
    if repair_step is None:
        logger.warning(
            "[ORCHESTRATION] Completion repair parse succeeded but no usable step object was found"
        )
        return {
            "status": "failed",
            "reason": "repair_step_missing_step_object",
        }

    if not repair_step.get("commands"):
        return {"status": "failed", "reason": "repair_step_missing_commands"}

    invalid_paths = _completion_repair_invalid_paths(
        repair_step=repair_step,
        project_dir=Path(orchestration_state.project_dir),
        completion_validation=completion_validation,
    )
    if invalid_paths:
        logger.warning(
            "[ORCHESTRATION] Completion repair step referenced inventory-missing paths: %s",
            invalid_paths[:10],
        )
        emit_live(
            "WARN",
            "[ORCHESTRATION] Completion repair step referenced paths that are not present in the current workspace inventory; requesting one guarded retry",
            metadata={
                "phase": "completion_repair",
                "invalid_paths": invalid_paths[:10],
            },
        )
        append_orchestration_event(
            project_dir=orchestration_state.project_dir,
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            event_type=EventType.REPAIR_REJECTED,
            details={
                "phase": "completion_repair",
                "reason": "inventory_guard",
                "invalid_paths": invalid_paths[:10],
            },
        )
        guarded_retry_prompt = (
            repair_prompt
            + "\n\nThe previous repair step was invalid because it referenced these paths that are not present in the workspace inventory or not created by the repair step:\n"
            + json.dumps(invalid_paths[:20], indent=2)
            + "\nReturn a replacement repair step that uses only inventory-confirmed paths or creates the referenced files first."
        )
        guarded_retry_result = asyncio.run(
            ctx.runtime_service.execute_task(
                guarded_retry_prompt, timeout_seconds=COMPLETION_REPAIR_TIMEOUT_SECONDS
            )
        )
        guarded_retry_output = extract_structured_text(
            guarded_retry_result.get("output", "{}")
        )
        guarded_retry_output = _salvage_completion_repair_json_text(
            guarded_retry_output
        )
        retry_success, retry_data, retry_strategy_info = (
            error_handler.attempt_json_parsing(
                guarded_retry_output, context="completion_repair"
            )
        )
        if not retry_success:
            fallback_output = extract_structured_text(guarded_retry_result)
            if fallback_output and fallback_output != guarded_retry_output:
                fallback_output = _salvage_completion_repair_json_text(fallback_output)
                retry_success, retry_data, retry_strategy_info = (
                    error_handler.attempt_json_parsing(
                        fallback_output, context="completion_repair"
                    )
                )
        if not retry_success:
            return {
                "status": "failed",
                "reason": f"repair_step_inventory_guard_parse_failed:{retry_strategy_info}",
            }
        repair_step = _extract_completion_repair_step(retry_data, next_step_number)
        if not repair_step or not repair_step.get("commands"):
            return {
                "status": "failed",
                "reason": "repair_step_inventory_guard_missing_commands",
            }
        invalid_paths = _completion_repair_invalid_paths(
            repair_step=repair_step,
            project_dir=Path(orchestration_state.project_dir),
            completion_validation=completion_validation,
        )
        if invalid_paths:
            append_orchestration_event(
                project_dir=orchestration_state.project_dir,
                session_id=ctx.session_id,
                task_id=ctx.task_id,
                event_type=EventType.REPAIR_REJECTED,
                details={
                    "phase": "completion_repair",
                    "reason": "inventory_guard_retry_rejected",
                    "invalid_paths": invalid_paths[:10],
                },
            )
            return {
                "status": "failed",
                "reason": "repair_step_inventory_guard_rejected:"
                + ", ".join(invalid_paths[:10]),
            }
        strategy_info = retry_strategy_info

    orchestration_state.plan.append(repair_step)
    task.steps = json.dumps(orchestration_state.plan)
    task.current_step = next_step_number
    save_orchestration_checkpoint_fn(
        db, ctx.session_id, ctx.task_id, ctx.prompt, orchestration_state
    )
    db.commit()

    emit_live(
        "INFO",
        f"[ORCHESTRATION] Executing completion repair step {next_step_number}: {repair_step['description']}",
        metadata={
            "phase": "completion_repair",
            "step_index": next_step_number,
            "strategy": strategy_info,
        },
    )

    execution_prompt = assemble_execution_prompt(ctx, repair_step)
    step_timeout_seconds = determine_step_timeout(
        timeout_seconds=ctx.timeout_seconds,
        total_steps=len(orchestration_state.plan),
        execution_profile=ctx.execution_profile,
        step_description=repair_step["description"],
        task_prompt=ctx.prompt,
    )
    step_started_at = datetime.now(UTC)
    repair_exec_result = asyncio.run(
        ctx.runtime_service.execute_task(
            execution_prompt,
            timeout_seconds=step_timeout_seconds,
        )
    )
    repair_exec_result = coerce_execution_step_result(
        repair_exec_result,
        expected_files=repair_step.get("expected_files", []),
        extract_structured_text=extract_structured_text,
    )
    reported_changed_files = _extract_reported_changed_files(
        str(repair_exec_result.get("output", "")),
        Path(orchestration_state.project_dir),
    )
    if reported_changed_files:
        repair_exec_result["files_changed"] = reported_changed_files
        repair_step["expected_files"] = reported_changed_files
    assessment = assess_step_execution(
        db=db,
        session_id=ctx.session_id,
        task_id=ctx.task_id,
        project_dir=orchestration_state.project_dir,
        step=repair_step,
        step_result=repair_exec_result,
        step_started_at=step_started_at,
        validation_profile=ctx.validation_profile,
        validation_severity=ctx.validation_severity,
        relaxed_mode=orchestration_state.relaxed_mode,
    )
    if assessment.validation_verdict:
        record_validation_verdict(
            db,
            ctx.session_id,
            ctx.task_id,
            orchestration_state,
            assessment.validation_verdict,
            step_number=next_step_number,
        )
        db.commit()

    step_record = StepResult(
        step_number=next_step_number,
        status=assessment.step_status,
        output=assessment.step_output[:1000],
        verification_output=repair_exec_result.get("verification_output", ""),
        files_changed=repair_exec_result.get(
            "files_changed", repair_step.get("expected_files", [])
        ),
        error_message=assessment.error_message,
        attempt=1,
    )

    if assessment.step_status == "success":
        orchestration_state.record_success(step_record)
        task.current_step = len(orchestration_state.plan)
        save_orchestration_checkpoint_fn(
            db, ctx.session_id, ctx.task_id, ctx.prompt, orchestration_state
        )
        db.commit()
        emit_live(
            "INFO",
            f"[ORCHESTRATION] Completion repair step {next_step_number} completed successfully",
            metadata={"phase": "completion_repair", "step_index": next_step_number},
        )
        append_orchestration_event(
            project_dir=orchestration_state.project_dir,
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            event_type=EventType.REPAIR_APPLIED,
            details={
                "phase": "completion_repair",
                "step_index": next_step_number,
                "expected_files": repair_step.get("expected_files", [])[:20],
            },
        )
        return {"status": "success", "step": repair_step}

    orchestration_state.record_failure(step_record)
    task.error_message = assessment.error_message[:2000]
    if session:
        mark_session_paused(
            session,
            alert_level="error",
            alert_message=f"Completion repair failed: {assessment.error_message[:1800]}",
        )
    save_orchestration_checkpoint_fn(
        db, ctx.session_id, ctx.task_id, ctx.prompt, orchestration_state
    )
    db.commit()
    emit_live(
        "ERROR",
        f"[ORCHESTRATION] Completion repair step {next_step_number} failed",
        metadata={
            "phase": "completion_repair",
            "step_index": next_step_number,
            "error": assessment.error_message[:1000],
        },
    )
    append_orchestration_event(
        project_dir=orchestration_state.project_dir,
        session_id=ctx.session_id,
        task_id=ctx.task_id,
        event_type=EventType.REPAIR_REJECTED,
        details={
            "phase": "completion_repair",
            "reason": assessment.error_message[:400],
            "step_index": next_step_number,
        },
    )
    return {"status": "failed", "reason": assessment.error_message}


def _run_evaluator(
    *,
    runtime_service: Any,
    orchestration_state: Any,
    prompt: str,
    summary: str,
    emit_live: Any,
    logger: Any,
) -> Dict[str, Any]:
    """Run an independent QA evaluation pass after structural validation passes.

    The evaluator is intentionally separate from the generator: it receives the
    task goal, the execution record, and the summary, then grades the result
    against concrete criteria. A NEEDS_REVIEW grade is surfaced to callers so
    auto-publish paths can hold the workspace for review before promotion.
    """
    try:
        reasoning_artifact = (
            getattr(orchestration_state, "reasoning_artifact", None) or {}
        )
        reasoning_summary = json.dumps(
            {
                "intent": reasoning_artifact.get("intent"),
                "planned_actions": list(
                    reasoning_artifact.get("planned_actions") or []
                )[:6],
                "verification_plan": list(
                    reasoning_artifact.get("verification_plan") or []
                )[:6],
            },
            ensure_ascii=True,
            indent=2,
        )
        steps_text = "\n".join(
            (
                f"- {r.get('step_title', r.get('step', ''))}: {r.get('status', '')}"
                if isinstance(r, dict)
                else f"- {r}"
            )
            for r in (orchestration_state.execution_results or [])
        )
        changed_files_text = "\n".join(
            f"- {f}"
            for f in (getattr(orchestration_state, "changed_files", []) or [])[:30]
        )
        evaluator_prompt = (
            "You are an independent QA evaluator. Grade the following completed task.\n\n"
            f"## Task goal\n{prompt}\n\n"
            f"## Control-plane reasoning artifact\n{reasoning_summary}\n\n"
            f"## Steps executed\n{steps_text or '(none recorded)'}\n\n"
            f"## Files changed\n{changed_files_text or '(none recorded)'}\n\n"
            f"## Agent summary\n{summary[:600] or '(no summary)'}\n\n"
            "## Evaluation criteria\n"
            "1. **Goal coverage** – Does the work address the full task goal? (0–3)\n"
            "   Check alignment with the reasoning artifact intent and planned actions.\n"
            "2. **No regressions** – Are there signs of broken functionality? (0–2)\n"
            "3. **Code quality** – Is the implementation complete, not stubbed? (0–2)\n"
            "4. **File correctness** – Do the changed files match what the task requires? (0–3)\n\n"
            "Respond in this exact format:\n"
            "SCORES: goal=X/3 regressions=X/2 quality=X/2 files=X/3\n"
            "TOTAL: X/10\n"
            "VERDICT: PASS or NEEDS_REVIEW\n"
            "NOTES: one-sentence rationale\n"
        )
        eval_result = asyncio.run(
            runtime_service.execute_task(
                evaluator_prompt, timeout_seconds=COMPLETION_REPAIR_TIMEOUT_SECONDS
            )
        )
        eval_output = (
            eval_result.get("output", "")
            if isinstance(eval_result, dict)
            else str(eval_result)
        )
        verdict = "PASS"
        if "VERDICT: NEEDS_REVIEW" in eval_output.upper():
            verdict = "NEEDS_REVIEW"
        judge_verdict = None
        if settings.JUDGE_AGENT_ENABLED:
            judge_prompt = (
                "You are a control-plane judge. Review whether the finished task still "
                "matches the accepted reasoning artifact.\n\n"
                f"## Reasoning artifact\n{reasoning_summary}\n\n"
                f"## Evaluator output\n{eval_output[:1200]}\n\n"
                "Respond exactly with:\n"
                "JUDGE: ACCEPT or WARN or REJECT\n"
                "RATIONALE: one sentence\n"
            )
            judge_result = asyncio.run(
                runtime_service.execute_task(judge_prompt, timeout_seconds=90)
            )
            judge_output = (
                judge_result.get("output", "")
                if isinstance(judge_result, dict)
                else str(judge_result)
            )
            if "JUDGE: REJECT" in judge_output.upper():
                judge_verdict = "REJECT"
            elif "JUDGE: WARN" in judge_output.upper():
                judge_verdict = "WARN"
            else:
                judge_verdict = "ACCEPT"
        log_level = "INFO" if verdict == "PASS" else "WARN"
        emit_live(
            log_level,
            f"[EVALUATOR] QA verdict: {verdict}",
            metadata={
                "phase": "evaluation",
                "verdict": verdict,
                "judge_verdict": judge_verdict,
                "eval_output": eval_output[:800],
            },
        )
        append_orchestration_event(
            project_dir=orchestration_state.project_dir,
            session_id=getattr(orchestration_state, "session_id", None),
            task_id=getattr(orchestration_state, "task_id", None),
            event_type=EventType.EVALUATOR_RESULT,
            details={
                "verdict": verdict,
                "judge_verdict": judge_verdict,
                "judge_enabled": bool(settings.JUDGE_AGENT_ENABLED),
                "reasoning_artifact_used": bool(reasoning_artifact),
                "reasoning_intent": reasoning_artifact.get("intent"),
                "output": eval_output[:800],
            },
        )
        return {
            "verdict": verdict,
            "judge_verdict": judge_verdict,
            "output": eval_output[:800],
        }
    except Exception as e:
        logger.warning("[EVALUATOR] QA evaluation failed (non-blocking): %s", e)
        return {"verdict": "ERROR", "error": str(e)}


_PROGRESS_NOTES_COMMANDS_CAP = 10
_PROGRESS_NOTES_COMMAND_MAX_CHARS = 120


def _extract_progress_notes_commands(orchestration_state: Any) -> list:
    """Return deduplicated command strings from successfully completed plan steps.

    Correlates plan step commands with execution_results (success-only) by
    step_number. Caps at _PROGRESS_NOTES_COMMANDS_CAP entries.
    """
    successful_step_numbers: set = set()
    for r in getattr(orchestration_state, "execution_results", None) or []:
        n = (
            r.step_number
            if hasattr(r, "step_number")
            else (r.get("step_number") if isinstance(r, dict) else None)
        )
        if n is not None:
            successful_step_numbers.add(n)

    seen: set = set()
    commands: list = []
    for step in getattr(orchestration_state, "plan", None) or []:
        if not isinstance(step, dict):
            continue
        if step.get("step_number") not in successful_step_numbers:
            continue
        for cmd in step.get("commands") or []:
            cmd = str(cmd or "").strip()
            if cmd and cmd not in seen:
                seen.add(cmd)
                commands.append(cmd)
                if len(commands) >= _PROGRESS_NOTES_COMMANDS_CAP:
                    return commands
    return commands


def _write_progress_notes(
    *,
    orchestration_state: Any,
    task: Any,
    prompt: str,
    summary: str,
    logger: Any,
) -> None:
    """Append a structured completion entry to .agent/progress_notes.md.

    This replaces git commits as the session artifact bridge when the project is
    not version-controlled.  The orient phase in worker.py reads this file before
    planning to give the next run full context on what was already done.
    """
    try:
        project_dir = getattr(orchestration_state, "project_dir", None)
        if not project_dir:
            return
        notes_dir = Path(project_dir) / ".agent"
        notes_dir.mkdir(parents=True, exist_ok=True)
        ensure_shared_permissions(notes_dir)
        notes_path = notes_dir / "progress_notes.md"

        completed_steps = [
            r.get("step_title", r.get("step", "")) if isinstance(r, dict) else str(r)
            for r in (orchestration_state.execution_results or [])
        ]
        changed_files = getattr(orchestration_state, "changed_files", []) or []
        task_title = getattr(task, "title", "") or prompt[:80]

        entry_lines = [
            f"\n## {datetime.now(UTC).strftime('%Y-%m-%d %H:%M UTC')} — {task_title}",
            "",
            f"**Steps completed ({len(completed_steps)}):**",
        ]
        for step in completed_steps[:20]:
            entry_lines.append(f"- {step}")
        if changed_files:
            entry_lines.append("")
            entry_lines.append(f"**Files changed ({len(changed_files)}):**")
            for f in changed_files[:30]:
                entry_lines.append(f"- {f}")
        if summary:
            entry_lines.append("")
            entry_lines.append("**Summary:**")
            entry_lines.append(summary[:800])
        known_good_cmds = _extract_progress_notes_commands(orchestration_state)
        if known_good_cmds:
            entry_lines.append("")
            entry_lines.append("**Known good commands:**")
            for cmd in known_good_cmds:
                entry_lines.append(f"- {cmd[:_PROGRESS_NOTES_COMMAND_MAX_CHARS]}")
        entry_lines.append("")

        with open(notes_path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(entry_lines))
        ensure_shared_permissions(notes_path)
        logger.info("[PROGRESS] Progress notes written to %s", notes_path)
    except Exception as e:
        logger.warning("[PROGRESS] Failed to write progress notes: %s", e)

    try:
        from app.services.orchestration.repo_memory import write_repo_memory

        write_repo_memory(
            project_dir=getattr(orchestration_state, "project_dir", None),
            _logger=logger,
        )
    except Exception as exc:
        logger.warning("[REPO_MEMORY] Unexpected error during write: %s", exc)


def finalize_successful_task(
    *,
    ctx: OrchestrationRunContext,
    write_project_state_snapshot_fn: Callable[..., None] = write_project_state_snapshot,
    save_orchestration_checkpoint_fn: Callable[
        ..., None
    ] = save_orchestration_checkpoint,
    get_next_pending_project_task_fn: Optional[Callable[..., Any]] = None,
    get_latest_session_task_link_fn: Optional[Callable[..., Any]] = None,
    execute_orchestration_task_delay_fn: Optional[Callable[..., Any]] = None,
    build_task_report_payload_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    render_task_report_fn: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    db = ctx.db
    runtime_service = ctx.runtime_service
    task_service = ctx.task_service
    session = ctx.session
    project = ctx.project
    task = ctx.task
    session_task_link = ctx.session_task_link
    session_id = ctx.session_id
    task_id = ctx.task_id
    prompt = ctx.prompt
    execution_profile = ctx.execution_profile
    validation_profile = ctx.validation_profile
    runs_in_canonical_baseline = ctx.runs_in_canonical_baseline
    orchestration_state = ctx.orchestration_state
    emit_live = ctx.emit_live
    logger = ctx.logger

    logger.info("[ORCHESTRATION] Phase 5: TASK_SUMMARY - summarizing completion")
    emit_phase_event(
        orchestration_state,
        emit_live,
        level="INFO",
        phase="task_summary",
        message="[ORCHESTRATION] Phase 5: TASK_SUMMARY - summarizing completion",
    )
    append_orchestration_event(
        project_dir=orchestration_state.project_dir,
        session_id=session_id,
        task_id=task_id,
        event_type=EventType.PHASE_STARTED,
        details={"phase": "task_summary"},
    )

    summary_prompt = assemble_task_summary_prompt(ctx)
    summary_result = _generate_task_summary_with_fallback(
        ctx=ctx,
        summary_prompt=summary_prompt,
    )
    wm_summary = summary_result.get("output", "")
    pn_summary = summary_result.get("pn_summary", wm_summary)
    reported_changed_files = list(
        dict.fromkeys(
            path
            for result in (orchestration_state.execution_results or [])
            for path in (getattr(result, "files_changed", []) or [])
            if str(path).strip()
        )
    )
    workspace_consistency = task_service.analyze_workspace_consistency(
        orchestration_state.project_dir
    )
    workspace_consistency = _scope_workspace_consistency_to_task_changes(
        workspace_consistency,
        plan=orchestration_state.plan,
        reported_changed_files=reported_changed_files,
    )

    completion_validation = ValidatorService.validate_task_completion(
        project_dir=orchestration_state.project_dir,
        plan=orchestration_state.plan,
        task_prompt=prompt,
        execution_profile=execution_profile,
        workspace_consistency=workspace_consistency,
        title=task.title if task else None,
        description=task.description if task else None,
        relaxed_mode=orchestration_state.relaxed_mode,
        completion_evidence={
            "summary_generated": bool(summary_result),
            "execution_results_count": len(orchestration_state.execution_results),
            "reported_changed_files": reported_changed_files,
        },
        validation_severity=ctx.validation_severity,
        workflow_stage=ctx.workflow_stage,
        is_first_ordered_task=bool(task and task.plan_position == 1),
    )
    record_validation_verdict(
        db,
        session_id,
        task_id,
        orchestration_state,
        completion_validation,
    )
    db.commit()

    # 10K-c: Emit LogEntry when symbol verification failed
    _sym_check = completion_validation.details.get("symbol_verification") or {}
    if (
        _sym_check.get("applicable")
        and not _sym_check.get("passed")
        and _sym_check.get("missing")
    ):
        try:
            import json as _json

            from app.models import LogEntry

            db.add(
                LogEntry(
                    session_id=session_id,
                    task_id=task_id,
                    level="WARNING",
                    message=(
                        f"[COMPLETION_SYMBOL_VERIFICATION_FAILED]"
                        f" task={task_id}"
                        f" missing_symbols={_sym_check['missing'][:8]}"
                    ),
                    log_metadata=_json.dumps(
                        {
                            "missing_symbols": _sym_check["missing"][:8],
                            "required_symbols": _sym_check.get("required", [])[:8],
                            "task_id": task_id,
                            "reason": "requested_symbol_missing_from_workspace",
                        }
                    ),
                )
            )
            db.commit()
        except Exception as _exc:
            logger.warning(
                "[SYMBOL_VERIFICATION] LogEntry write failed (non-fatal): %s", _exc
            )

    if completion_validation.repairable:
        repair_result = _attempt_completion_repair(
            ctx=ctx,
            completion_validation=completion_validation,
            save_orchestration_checkpoint_fn=save_orchestration_checkpoint_fn,
        )
        if repair_result.get("status") == "success":
            completion_validation = ValidatorService.validate_task_completion(
                project_dir=orchestration_state.project_dir,
                plan=orchestration_state.plan,
                task_prompt=prompt,
                execution_profile=execution_profile,
                workspace_consistency=workspace_consistency,
                title=task.title if task else None,
                description=task.description if task else None,
                relaxed_mode=orchestration_state.relaxed_mode,
                completion_evidence={
                    "summary_generated": bool(summary_result),
                    "execution_results_count": len(
                        orchestration_state.execution_results
                    ),
                    "reported_changed_files": reported_changed_files,
                },
                validation_severity=ctx.validation_severity,
                workflow_stage=ctx.workflow_stage,
                is_first_ordered_task=bool(task and task.plan_position == 1),
            )
            record_validation_verdict(
                db,
                session_id,
                task_id,
                orchestration_state,
                completion_validation,
            )
            db.commit()
        else:
            completion_error = "Completion repair failed: " + str(
                repair_result.get("reason") or "unknown reason"
            )
            completion_failure_reason = str(
                repair_result.get("reason") or "unknown reason"
            )
            orchestration_state.status = OrchestrationStatus.ABORTED
            orchestration_state.abort_reason = completion_error
            task_execution = (
                db.query(TaskExecution)
                .filter(TaskExecution.id == ctx.task_execution_id)
                .first()
                if ctx.task_execution_id
                else None
            )
            mark_task_attempt_failed(
                task=task,
                session_task_link=session_task_link,
                task_execution=task_execution,
                error_message=completion_error,
                completed_at=datetime.now(UTC),
                workspace_status="blocked",
            )
            task.current_step = len(orchestration_state.plan)
            if session:
                mark_session_paused(
                    session,
                    alert_level="error",
                    alert_message=completion_error[:2000],
                )
            db.commit()
            emit_live(
                "ERROR",
                f"[ORCHESTRATION] Completion repair failed: {completion_failure_reason}",
                metadata={
                    "phase": "completion_repair",
                    "reason": completion_failure_reason,
                },
            )
            save_orchestration_checkpoint_fn(
                db, session_id, task_id, prompt, orchestration_state
            )
            append_orchestration_event(
                project_dir=orchestration_state.project_dir,
                session_id=session_id,
                task_id=task_id,
                event_type=EventType.PHASE_FINISHED,
                details={
                    "phase": "task_summary",
                    "status": "repair_failed",
                    "task_status": str(task.status.value if task else "failed"),
                },
            )
            write_project_state_snapshot_fn(db, project, task, session_id)
            return {"status": "failed", "reason": "completion_repair_failed"}

    if completion_validation.warning:
        emit_live(
            "WARN",
            "[ORCHESTRATION] Task completion passed with validator warnings",
            metadata={
                "phase": "task_validation",
                "validation_status": completion_validation.status,
                "reasons": completion_validation.reasons[:10],
                "relaxed_mode": orchestration_state.relaxed_mode,
            },
        )

    if not completion_validation.accepted:
        debug_feedback_envelope = build_debug_feedback_envelope(
            task_execution_id=ctx.task_execution_id,
            task_id=task_id,
            step_index=len(orchestration_state.plan),
            failure_phase="completion_validation",
            failed_command="",
            stdout="",
            stderr="; ".join(completion_validation.reasons[:10]),
            validator_reasons=completion_validation.reasons[:10],
            changed_files=reported_changed_files[:20],
            workspace_path=orchestration_state.project_dir,
        )
        persist_debug_feedback_envelope(
            db=db,
            session_id=session_id,
            task_id=task_id,
            session_instance_id=ctx.session_instance_id,
            project_dir=orchestration_state.project_dir,
            envelope=debug_feedback_envelope,
        )
        append_orchestration_event(
            project_dir=orchestration_state.project_dir,
            session_id=session_id,
            task_id=task_id,
            event_type=EventType.COMPLETION_EVIDENCE_FAILED,
            details={
                "session_instance_id": ctx.session_instance_id,
                **runtime_service.get_backend_metadata(),
                "project_dir": str(orchestration_state.project_dir),
                "validation_status": completion_validation.status,
                "reasons": completion_validation.reasons[:10],
                "reported_changed_files": reported_changed_files[:20],
            },
        )
        completion_error = "Completion validation failed: " + "; ".join(
            completion_validation.reasons[:5]
        )
        orchestration_state.status = OrchestrationStatus.ABORTED
        orchestration_state.abort_reason = completion_error
        task_execution = (
            db.query(TaskExecution)
            .filter(TaskExecution.id == ctx.task_execution_id)
            .first()
            if ctx.task_execution_id
            else None
        )
        mark_task_attempt_failed(
            task=task,
            session_task_link=session_task_link,
            task_execution=task_execution,
            error_message=completion_error,
            completed_at=datetime.now(UTC),
            workspace_status="blocked",
        )
        task.current_step = len(orchestration_state.plan)
        if session:
            mark_session_paused(
                session, alert_level="error", alert_message=completion_error[:2000]
            )
        db.commit()
        emit_live(
            "ERROR",
            "[ORCHESTRATION] Task completion failed validation",
            metadata={
                "phase": "task_validation",
                "validation_status": completion_validation.status,
                "profile": completion_validation.profile,
                "reasons": completion_validation.reasons[:10],
            },
        )
        save_orchestration_checkpoint_fn(
            db, session_id, task_id, prompt, orchestration_state
        )
        write_project_state_snapshot_fn(db, project, task, session_id)
        return {"status": "failed", "reason": "completion_validation_failed"}

    completion_verification_command, completion_verification_source = (
        _detect_completion_verification_command(orchestration_state.project_dir)
    )
    behavior_baseline_result = None
    if completion_verification_command:
        emit_live(
            "INFO",
            f"[ORCHESTRATION] Running completion verification: {completion_verification_command}",
            metadata={
                "phase": "task_verification",
                "command": completion_verification_command,
                "source": completion_verification_source,
            },
        )
        completion_verification = _execute_completion_verification(
            project_dir=orchestration_state.project_dir,
            command=completion_verification_command,
        )
        if not completion_verification.get("success", False):
            verification_failure_verdict = _classify_completion_verification_failure(
                command=completion_verification_command,
                source=completion_verification_source,
                verification_output=str(completion_verification.get("output") or ""),
                completion_validation=completion_validation,
            )
            if verification_failure_verdict and verification_failure_verdict.repairable:
                completion_verification_before_repair = dict(completion_verification)
                record_validation_verdict(
                    db,
                    session_id,
                    task_id,
                    orchestration_state,
                    verification_failure_verdict,
                )
                db.commit()
                repair_result = _attempt_completion_repair(
                    ctx=ctx,
                    completion_validation=verification_failure_verdict,
                    save_orchestration_checkpoint_fn=save_orchestration_checkpoint_fn,
                )
                if repair_result.get("status") == "success":
                    emit_live(
                        "INFO",
                        "[ORCHESTRATION] Completion verification repair applied, rerunning verification",
                        metadata={
                            "phase": "completion_repair",
                            "command": completion_verification_command,
                        },
                    )
                    completion_verification = _execute_completion_verification(
                        project_dir=orchestration_state.project_dir,
                        command=completion_verification_command,
                    )
                    behavior_baseline_result = compare_baseline(
                        capture_baseline_result(
                            command=completion_verification_command,
                            returncode=completion_verification_before_repair.get(
                                "returncode"
                            ),
                            stderr=str(
                                completion_verification_before_repair.get("output")
                                or ""
                            ),
                        ),
                        capture_baseline_result(
                            command=completion_verification_command,
                            returncode=completion_verification.get("returncode"),
                            stderr=str(completion_verification.get("output") or ""),
                        ),
                        policy="pass_fail_transition",
                    )
                else:
                    completion_error = "Completion repair failed: " + str(
                        repair_result.get("reason") or "unknown reason"
                    )
                    completion_failure_reason = str(
                        repair_result.get("reason") or "unknown reason"
                    )
                    orchestration_state.status = OrchestrationStatus.ABORTED
                    orchestration_state.abort_reason = completion_error
                    task_execution = (
                        db.query(TaskExecution)
                        .filter(TaskExecution.id == ctx.task_execution_id)
                        .first()
                        if ctx.task_execution_id
                        else None
                    )
                    mark_task_attempt_failed(
                        task=task,
                        session_task_link=session_task_link,
                        task_execution=task_execution,
                        error_message=completion_error,
                        completed_at=datetime.now(UTC),
                        workspace_status="blocked",
                    )
                    task.current_step = len(orchestration_state.plan)
                    if session:
                        mark_session_paused(
                            session,
                            alert_level="error",
                            alert_message=completion_error[:2000],
                        )
                    db.commit()
                    emit_live(
                        "ERROR",
                        f"[ORCHESTRATION] Completion repair failed: {completion_failure_reason}",
                        metadata={
                            "phase": "completion_repair",
                            "reason": completion_failure_reason,
                        },
                    )
                    save_orchestration_checkpoint_fn(
                        db, session_id, task_id, prompt, orchestration_state
                    )
                    append_orchestration_event(
                        project_dir=orchestration_state.project_dir,
                        session_id=session_id,
                        task_id=task_id,
                        event_type=EventType.PHASE_FINISHED,
                        details={
                            "phase": "task_summary",
                            "status": "repair_failed",
                            "task_status": str(task.status.value if task else "failed"),
                        },
                    )
                    write_project_state_snapshot_fn(db, project, task, session_id)
                    return {"status": "failed", "reason": "completion_repair_failed"}

            if not completion_verification.get("success", False):
                verification_error = (
                    "Completion verification failed: "
                    f"`{completion_verification_command}` "
                    f"({completion_verification_source or 'auto-detected'})"
                )
                debug_feedback_envelope = build_debug_feedback_envelope(
                    task_execution_id=ctx.task_execution_id,
                    task_id=task_id,
                    step_index=len(orchestration_state.plan),
                    failure_phase="completion_verification",
                    failed_command=completion_verification_command,
                    return_code=completion_verification.get("returncode"),
                    stdout="",
                    stderr=str(completion_verification.get("output") or ""),
                    validator_reasons=[verification_error],
                    changed_files=reported_changed_files[:20],
                    workspace_path=orchestration_state.project_dir,
                )
                persist_debug_feedback_envelope(
                    db=db,
                    session_id=session_id,
                    task_id=task_id,
                    session_instance_id=ctx.session_instance_id,
                    project_dir=orchestration_state.project_dir,
                    envelope=debug_feedback_envelope,
                )

                task_execution_id = (
                    int(ctx.task_execution_id)
                    if ctx.task_execution_id is not None
                    else None
                )
                if (
                    debug_feedback_envelope.eligible_for_debug_repair
                    and task_execution_id is not None
                ):
                    fallback_verdict = ValidationVerdict(
                        stage="completion_verification",
                        status="repair_required",
                        profile=(
                            getattr(completion_validation, "profile", None)
                            or "implementation"
                        ),
                        reasons=[
                            verification_error
                            + ": "
                            + str(completion_verification.get("output") or "")[:400]
                        ],
                        details={
                            "expected_core_files": reported_changed_files[:20],
                            "verification_command": completion_verification_command,
                            "verification_source": (
                                completion_verification_source or "auto-detected"
                            ),
                            "verification_output_preview": str(
                                completion_verification.get("output") or ""
                            )[:400],
                            "completion_repair_source": (
                                "final_completion_verification"
                            ),
                            "failure_class": debug_feedback_envelope.failure_class,
                        },
                    )
                    record_validation_verdict(
                        db,
                        session_id,
                        task_id,
                        orchestration_state,
                        fallback_verdict,
                    )
                    db.commit()
                    repair_result = _attempt_completion_repair(
                        ctx=ctx,
                        completion_validation=fallback_verdict,
                        save_orchestration_checkpoint_fn=(
                            save_orchestration_checkpoint_fn
                        ),
                    )
                    save_orchestration_checkpoint_fn(
                        db, session_id, task_id, prompt, orchestration_state
                    )
                    if repair_result.get("status") == "success":
                        emit_live(
                            "INFO",
                            "[ORCHESTRATION] Final verification repair applied, rerunning verification",
                            metadata={
                                "phase": "completion_repair",
                                "completion_repair_source": (
                                    "final_completion_verification"
                                ),
                                "command": completion_verification_command,
                                "failure_class": (
                                    debug_feedback_envelope.failure_class
                                ),
                            },
                        )
                        completion_verification = _execute_completion_verification(
                            project_dir=orchestration_state.project_dir,
                            command=completion_verification_command,
                        )
                        behavior_baseline_result = compare_baseline(
                            capture_baseline_result(
                                command=completion_verification_command,
                                returncode=debug_feedback_envelope.return_code,
                                stderr=debug_feedback_envelope.stderr_excerpt,
                            ),
                            capture_baseline_result(
                                command=completion_verification_command,
                                returncode=completion_verification.get("returncode"),
                                stderr=str(completion_verification.get("output") or ""),
                            ),
                            policy="pass_fail_transition",
                        )
                    else:
                        verification_error = "Completion repair failed: " + str(
                            repair_result.get("reason") or "unknown reason"
                        )

                if not completion_verification.get("success", False):
                    verification_error_message = (
                        verification_error
                        + ": "
                        + str(completion_verification.get("output") or "")[:1500]
                    )
                    task_execution = (
                        db.query(TaskExecution)
                        .filter(TaskExecution.id == ctx.task_execution_id)
                        .first()
                        if ctx.task_execution_id
                        else None
                    )
                    mark_task_attempt_failed(
                        task=task,
                        session_task_link=session_task_link,
                        task_execution=task_execution,
                        error_message=verification_error_message,
                        completed_at=datetime.now(UTC),
                        workspace_status="blocked",
                    )
                    task.current_step = len(orchestration_state.plan)
                    orchestration_state.status = OrchestrationStatus.ABORTED
                    orchestration_state.abort_reason = verification_error
                    if session:
                        mark_session_paused(
                            session,
                            alert_level="error",
                            alert_message=task.error_message[:2000],
                        )
                    db.commit()
                    emit_live(
                        "ERROR",
                        "[ORCHESTRATION] Task completion verification failed",
                        metadata={
                            "phase": "task_verification",
                            "command": completion_verification_command,
                            "source": completion_verification_source,
                            "output": str(completion_verification.get("output") or "")[
                                :2000
                            ],
                        },
                    )
                    save_orchestration_checkpoint_fn(
                        db, session_id, task_id, prompt, orchestration_state
                    )
                    append_orchestration_event(
                        project_dir=orchestration_state.project_dir,
                        session_id=session_id,
                        task_id=task_id,
                        event_type=EventType.PHASE_FINISHED,
                        details={
                            "phase": "task_summary",
                            "status": "verification_failed",
                            "verification_command": completion_verification_command,
                        },
                    )
                    write_project_state_snapshot_fn(db, project, task, session_id)
                    return {
                        "status": "failed",
                        "reason": "completion_verification_failed",
                    }

    task_change_set = None
    workspace_review_policy = get_effective_workspace_review_policy(
        settings.WORKSPACE_REVIEW_POLICY, db=db
    )
    if (
        project
        and task
        and ctx.task_execution_id
        and hasattr(task_service, "persist_task_execution_change_set")
    ):
        task_change_set = task_service.persist_task_execution_change_set(
            project,
            task,
            session_id=session_id,
            task_execution_id=ctx.task_execution_id,
            snapshot_key=workspace_snapshot_key(task_id, ctx.task_execution_id),
            target_dir=Path(orchestration_state.project_dir),
            preserve_project_root_rules=runs_in_canonical_baseline,
            status=TaskStatus.DONE.value,
            workspace_review_policy=workspace_review_policy,
            workflow_profile=getattr(ctx, "workflow_profile", None),
            commit=False,
        )

    if task_change_set:
        completion_validation = ValidatorService.validate_task_completion(
            project_dir=orchestration_state.project_dir,
            plan=orchestration_state.plan,
            task_prompt=prompt,
            execution_profile=execution_profile,
            workspace_consistency=workspace_consistency,
            title=task.title if task else None,
            description=task.description if task else None,
            relaxed_mode=orchestration_state.relaxed_mode,
            completion_evidence={
                "summary_generated": bool(summary_result),
                "execution_results_count": len(orchestration_state.execution_results),
                "reported_changed_files": reported_changed_files,
                "change_set": task_change_set,
                "completion_verification_command": completion_verification_command,
                "completion_verification_source": completion_verification_source,
                "behavior_baseline": behavior_baseline_result,
            },
            validation_severity=ctx.validation_severity,
            workflow_stage=ctx.workflow_stage,
            is_first_ordered_task=bool(task and task.plan_position == 1),
        )
        record_validation_verdict(
            db,
            session_id,
            task_id,
            orchestration_state,
            completion_validation,
        )
        db.commit()
        if not completion_validation.accepted:
            integrity_error = (
                "Completion validation failed after change-set integrity checks: "
                + "; ".join(completion_validation.reasons[:5])
            )
            orchestration_state.status = OrchestrationStatus.ABORTED
            orchestration_state.abort_reason = integrity_error
            task_execution = (
                db.query(TaskExecution)
                .filter(TaskExecution.id == ctx.task_execution_id)
                .first()
                if ctx.task_execution_id
                else None
            )
            mark_task_attempt_failed(
                task=task,
                session_task_link=session_task_link,
                task_execution=task_execution,
                error_message=integrity_error,
                completed_at=datetime.now(UTC),
                workspace_status="blocked",
            )
            task.current_step = len(orchestration_state.plan)
            if session:
                mark_session_paused(
                    session,
                    alert_level="error",
                    alert_message=integrity_error[:2000],
                )
            db.commit()
            emit_live(
                "ERROR",
                "[ORCHESTRATION] Completion failed verification integrity checks",
                metadata={
                    "phase": "task_summary",
                    "validation_status": completion_validation.status,
                    "reasons": completion_validation.reasons[:10],
                    "validation_evidence": completion_validation.details.get(
                        "validation_evidence"
                    ),
                },
            )
            append_orchestration_event(
                project_dir=orchestration_state.project_dir,
                session_id=session_id,
                task_id=task_id,
                event_type=EventType.PHASE_FINISHED,
                details={
                    "phase": "task_summary",
                    "status": "verification_integrity_failed",
                    "task_status": str(task.status.value if task else "failed"),
                },
            )
            write_project_state_snapshot_fn(db, project, task, session_id)
            return {
                "status": "failed",
                "reason": "verification_integrity_failed",
            }

    nontrivial_change_flags = list((task_change_set or {}).get("warning_flags") or [])
    _tmpl_review_policy = _resolve_template_review_policy(task)
    if hasattr(task_service, "change_set_review_decision"):
        review_decision = task_service.change_set_review_decision(
            task_change_set,
            workspace_review_policy=workspace_review_policy,
            workflow_profile=getattr(ctx, "workflow_profile", None),
            template_review_policy=_tmpl_review_policy,
        )
    else:
        review_decision = decide_change_set_review(
            task_change_set,
            workspace_review_policy=workspace_review_policy,
            workflow_profile=getattr(ctx, "workflow_profile", None),
            template_review_policy=_tmpl_review_policy,
        )
    should_hold_for_review = bool(review_decision["held_for_review"])
    evaluator_result = None
    if (
        task_change_set
        and ctx.task_execution_id
        and not should_hold_for_review
        and review_decision.get("outcome") == "auto_promote"
    ):
        evaluator_result = _run_evaluator(
            runtime_service=runtime_service,
            orchestration_state=orchestration_state,
            prompt=prompt,
            summary=wm_summary,
            emit_live=emit_live,
            logger=logger,
        )
        if (evaluator_result or {}).get("verdict") == "NEEDS_REVIEW":
            should_hold_for_review = True
            review_decision = {
                **review_decision,
                "outcome": "hold_for_review",
                "held_for_review": True,
                "reason": "evaluator_needs_review",
                "evaluator_verdict": "NEEDS_REVIEW",
            }
            emit_live(
                "WARN",
                "[ORCHESTRATION] Evaluator requested review; holding workspace instead of auto-publishing",
                metadata={
                    "phase": "evaluation",
                    "verdict": "NEEDS_REVIEW",
                    "reason": "evaluator_needs_review",
                },
            )
    baseline_publish_result = None
    baseline_publish_validation = None
    if project and task.task_subfolder and not runs_in_canonical_baseline:
        if should_hold_for_review:
            baseline_publish_result = {
                "auto_publish_skipped": True,
                "reason": review_decision["reason"],
                "held_for_review": True,
                "review_decision": review_decision,
                "files_copied": 0,
                "accepted_change_set": task_change_set,
                "warning_flags": nontrivial_change_flags,
                "workspace_review_policy": workspace_review_policy,
            }
            emit_live(
                "WARN",
                "[ORCHESTRATION] Task change set recorded; holding workspace for manual review instead of auto-publishing",
                metadata={
                    "phase": "baseline_publish",
                    "reason": review_decision["reason"],
                    "held_for_review": True,
                    "warning_flags": nontrivial_change_flags,
                    "changed_count": (task_change_set or {}).get("changed_count", 0),
                    "workspace_review_policy": workspace_review_policy,
                },
            )
        else:
            baseline_publish_result = task_service.auto_publish_task_into_baseline(
                project, task
            )
            baseline_publish_result["workspace_review_policy"] = workspace_review_policy
            baseline_publish_result["held_for_review"] = False
            baseline_publish_result["review_decision"] = review_decision
            if task_change_set:
                baseline_publish_result["accepted_change_set"] = {
                    "task_execution_id": ctx.task_execution_id,
                    "change_set": task_change_set,
                }
            baseline_materialization = (
                task_service.validate_task_baseline_materialization(project, task)
            )
            baseline_overview = task_service.validate_project_baseline(
                project, current_task=task
            )
            baseline_publish_validation = ValidatorService.validate_baseline_publish(
                validation_profile=validation_profile,
                baseline_path=baseline_materialization.get("baseline_path") or "",
                baseline_file_count=baseline_materialization.get(
                    "baseline_file_count", 0
                ),
                missing_task_expected_files=baseline_materialization.get(
                    "missing_expected_files", []
                ),
                missing_prior_expected_files=baseline_overview.get(
                    "missing_expected_files", []
                ),
                consistency_issues=baseline_materialization.get(
                    "consistency_issues", []
                ),
                consistency_details=baseline_materialization.get("consistency"),
                relaxed_mode=orchestration_state.relaxed_mode,
                validation_severity=ctx.validation_severity,
            )
            record_validation_verdict(
                db,
                session_id,
                task_id,
                orchestration_state,
                baseline_publish_validation,
            )
            db.commit()
            if baseline_publish_validation.warning:
                emit_live(
                    "WARN",
                    "[ORCHESTRATION] Baseline publish passed with validator warnings",
                    metadata={
                        "phase": "baseline_publish",
                        "validation_status": baseline_publish_validation.status,
                        "reasons": baseline_publish_validation.reasons[:10],
                        "relaxed_mode": orchestration_state.relaxed_mode,
                    },
                )

            if not baseline_publish_validation.accepted:
                baseline_error = "Baseline publish validation failed: " + "; ".join(
                    baseline_publish_validation.reasons[:5]
                )
                orchestration_state.status = OrchestrationStatus.ABORTED
                orchestration_state.abort_reason = baseline_error
                task_execution = (
                    db.query(TaskExecution)
                    .filter(TaskExecution.id == ctx.task_execution_id)
                    .first()
                    if ctx.task_execution_id
                    else None
                )
                mark_task_attempt_failed(
                    task=task,
                    session_task_link=session_task_link,
                    task_execution=task_execution,
                    error_message=baseline_error,
                    completed_at=datetime.now(UTC),
                    workspace_status="blocked",
                )
                task.current_step = len(orchestration_state.plan)
                if session:
                    mark_session_paused(
                        session,
                        alert_level="error",
                        alert_message=baseline_error[:2000],
                    )
                db.commit()
                emit_live(
                    "ERROR",
                    "[ORCHESTRATION] Baseline publish failed validation",
                    metadata={
                        "phase": "baseline_publish",
                        "validation_status": baseline_publish_validation.status,
                        "reasons": baseline_publish_validation.reasons[:10],
                    },
                )
                save_orchestration_checkpoint_fn(
                    db, session_id, task_id, prompt, orchestration_state
                )
                write_project_state_snapshot_fn(db, project, task, session_id)
                return {
                    "status": "failed",
                    "reason": "baseline_publish_validation_failed",
                }

    if (
        task_change_set
        and ctx.task_execution_id
        and not should_hold_for_review
        and review_decision.get("outcome") == "auto_promote"
        and hasattr(task_service, "mark_task_execution_change_set_disposition")
    ):
        disposition_record = task_service.mark_task_execution_change_set_disposition(
            task_execution_id=ctx.task_execution_id,
            disposition="promoted",
            reason=review_decision.get("reason") or "auto_promote",
            metadata={
                "action": "auto_promote",
                "task_execution_id": ctx.task_execution_id,
                "workspace_review_policy": workspace_review_policy,
                "review_decision": review_decision,
            },
            commit=False,
        )
        if disposition_record and baseline_publish_result:
            baseline_publish_result["accepted_change_set_disposition"] = (
                task_service.get_task_execution_change_set(
                    task_execution_id=ctx.task_execution_id
                )
                if hasattr(task_service, "get_task_execution_change_set")
                else None
            )

    def _pn_write_fn(*, orchestration_state, task, prompt, summary, logger):
        return _write_progress_notes(
            orchestration_state=orchestration_state,
            task=task,
            prompt=prompt,
            summary=pn_summary,
            logger=logger,
        )

    finalization = TaskCompletionFinalizer(
        db=db,
        task_service=task_service,
    ).finalize_success(
        ctx=ctx,
        summary=wm_summary,
        baseline_publish_result=baseline_publish_result,
        completion_validation=completion_validation,
        write_project_state_snapshot_fn=write_project_state_snapshot_fn,
        write_progress_notes_fn=_pn_write_fn,
        get_next_pending_project_task_fn=get_next_pending_project_task_fn,
        get_latest_session_task_link_fn=get_latest_session_task_link_fn,
        execute_orchestration_task_delay_fn=execute_orchestration_task_delay_fn,
    )
    from app.services.orchestration.working_memory import write_working_memory

    write_working_memory(
        orchestration_state=orchestration_state,
        task=task,
        summary=wm_summary,
        logger=logger,
        db=db,
        guidance_backend=ctx.guidance_backend,
        guidance_model_family=ctx.guidance_model_family,
    )

    from app.services.human_guidance_post_write_checker import (
        run_post_write_check_if_enabled,
    )

    run_post_write_check_if_enabled(ctx, reported_changed_files=reported_changed_files)

    promoted_workspace_archive_result = finalization.get(
        "promoted_workspace_archive_result"
    )

    logger.info(
        "[ORCHESTRATION] Task %s completed successfully with %s steps",
        task_id,
        len(orchestration_state.plan),
    )
    emit_live(
        "INFO",
        f"[ORCHESTRATION] Task {task_id} completed successfully with {len(orchestration_state.plan)} steps",
        metadata={
            "phase": "completed",
            "steps": len(orchestration_state.plan),
            "baseline_publish_result": baseline_publish_result,
            "promoted_workspace_archive_result": promoted_workspace_archive_result,
        },
    )

    if build_task_report_payload_fn and render_task_report_fn:
        try:
            report_payload = build_task_report_payload_fn(db, task_id)
            report_result = render_task_report_fn(
                report_payload, output_format="markdown"
            )
            if report_result and "report" in report_result:
                report_content = report_result["report"]
                report_path = (
                    orchestration_state.project_dir
                    / TASK_REPORT_ROOT
                    / f"task_report_{task_id}.md"
                )
                os.makedirs(report_path.parent, exist_ok=True)
                report_path.parent.chmod(0o777)
                with open(report_path, "w", encoding="utf-8") as handle:
                    handle.write(report_content)
                report_path.chmod(0o666)
                logger.info("[REPORT] Task report saved to: %s", report_path)
        except Exception as report_error:
            logger.error(
                "[REPORT] Failed to generate task report: %s", str(report_error)
            )

    return {
        "status": "completed",
        "task_id": task_id,
        "session_id": session_id,
        "steps_completed": len(orchestration_state.plan),
        "debug_attempts": len(orchestration_state.debug_attempts),
        "summary": wm_summary[:500],
    }
