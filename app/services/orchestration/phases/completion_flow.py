"""Task completion and finalization flow."""

import asyncio
import json
import os
import subprocess
import time
from pathlib import Path
from datetime import UTC, datetime
from typing import Any, Callable, Dict, Optional

from app.models import TaskExecution, TaskStatus
from app.config import settings
from app.services.orchestration.error_handler import error_handler
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.events.telemetry import emit_phase_event
from app.services.orchestration.diagnostics.debug_feedback import (
    build_debug_feedback_envelope,
    persist_debug_feedback_envelope,
)
from app.services.orchestration.diagnostics.signature_guard import (
    COMPLETION_REPAIR_POST_DIFF_VIOLATION_REASON,
    COMPLETION_REPAIR_SIGNATURE_VIOLATION_REASON,
    check_completion_repair_signature_contract,
    check_post_execution_signature_drift,
    completion_repair_signature_violation_event_details,
    snapshot_public_python_signatures,
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
from app.services.orchestration.state.execution_states import (
    OrchestrationPhase,
    TerminalReason,
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
from app.services.orchestration.prompt_templates import OrchestrationStatus, StepResult
from app.services.orchestration.phases.completion_repair import (
    _apply_completion_repair_ops_direct,
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
from app.services.orchestration.recovery.execution_recovery_evidence import (
    build_completion_recovery_evidence,
)
from app.services.orchestration.recovery.execution_recovery_service import (
    ExecutionRecoveryService,
)

__all__ = [
    "_attempt_completion_repair",
    "_augment_completion_verification_command",
    "_classify_completion_verification_failure",
    "_execute_completion_verification",
    "_run_evaluator",
    "finalize_successful_task",
]


def _create_completion_repair_runtime(db, session_id, task_id):
    """Create a fast runtime for completion-repair generation. Deferred import avoids circular dependency."""
    from app.services.agents.agent_runtime import BackendRole, create_agent_runtime

    return create_agent_runtime(
        db, session_id, task_id, role=BackendRole.COMPLETION_REPAIR
    )


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
        phase=OrchestrationPhase.COMPLETION_REPAIR,
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
                "phase": OrchestrationPhase.COMPLETION_REPAIR,
                "repair_churn_trigger": churn_trigger,
                "completion_repair_attempts": orchestration_state.completion_repair_attempts,
            },
        )
        emit_phase_event(
            ctx.orchestration_state,
            ctx.emit_live,
            level="ERROR",
            phase=OrchestrationPhase.COMPLETION_REPAIR,
            message="Repair churn limit reached — operator review required",
            details={"repair_churn_trigger": churn_trigger},
        )
        return {
            "status": "failed",
            "reason": TerminalReason.COMPLETION_REPAIR_CHURN_LIMIT,
        }

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
                "phase": OrchestrationPhase.COMPLETION_REPAIR,
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
                    "phase": OrchestrationPhase.COMPLETION_REPAIR,
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
                "phase": OrchestrationPhase.COMPLETION_REPAIR,
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
            "phase": OrchestrationPhase.COMPLETION_REPAIR,
            "attempt": orchestration_state.completion_repair_attempts,
            "reasons": completion_validation.reasons[:10],
        },
    )
    repair_generated_details = attach_failure_envelope(
        {
            "phase": OrchestrationPhase.COMPLETION_REPAIR,
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
    _cr_prompt_chars = len(repair_prompt)
    _cr_started_at = datetime.now(UTC).isoformat()
    _cr_start_mono = time.monotonic()
    _cr_fast_runtime = None
    _cr_fast_profile = None
    _cr_fast_fallback = False

    _cr_configured_backend = getattr(settings, "COMPLETION_REPAIR_BACKEND", None)
    if str(runtime_metadata.get("backend") or "").strip().lower() == "fake":
        _cr_configured_backend = None
    if _cr_configured_backend:
        try:
            _cr_fast_runtime = _create_completion_repair_runtime(
                ctx.db, ctx.session_id, ctx.task_id
            )
            _cr_fast_profile = _cr_configured_backend
        except Exception as _cr_fast_err:
            logger.warning(
                "[COMPLETION_REPAIR] Fast runtime unavailable, falling back to default: %s",
                _cr_fast_err,
            )
            _cr_fast_fallback = True

    _cr_active_runtime = _cr_fast_runtime if _cr_fast_runtime else ctx.runtime_service
    _cr_active_profile = _cr_fast_profile if _cr_fast_profile else "default"

    try:
        repair_plan_result = asyncio.run(
            _cr_active_runtime.execute_task(
                repair_prompt, timeout_seconds=COMPLETION_REPAIR_TIMEOUT_SECONDS
            )
        )
        _cr_duration = round(time.monotonic() - _cr_start_mono, 2)
        _cr_output_chars = len(str(repair_plan_result.get("output", "") or ""))
        emit_live(
            "INFO",
            "[COMPLETION_REPAIR] LLM generation completed",
            metadata={
                "phase": OrchestrationPhase.COMPLETION_REPAIR,
                "completion_repair_prompt_chars": _cr_prompt_chars,
                "completion_repair_timeout_seconds": COMPLETION_REPAIR_TIMEOUT_SECONDS,
                "completion_repair_runtime_profile": _cr_active_profile,
                "completion_repair_started_at": _cr_started_at,
                "completion_repair_duration_seconds": _cr_duration,
                "completion_repair_timed_out": False,
                "completion_repair_output_chars": _cr_output_chars,
                "completion_repair_fast_profile_selected": bool(_cr_fast_runtime),
                "completion_repair_fast_profile_fallback": _cr_fast_fallback,
            },
        )
    except Exception as _cr_exc:
        _cr_duration = round(time.monotonic() - _cr_start_mono, 2)
        _cr_exc_type = type(_cr_exc).__name__
        _cr_timed_out = (
            isinstance(_cr_exc, asyncio.TimeoutError)
            or "timeout" in _cr_exc_type.lower()
        )
        emit_live(
            "ERROR",
            "[COMPLETION_REPAIR] LLM generation failed",
            metadata={
                "phase": OrchestrationPhase.COMPLETION_REPAIR,
                "completion_repair_prompt_chars": _cr_prompt_chars,
                "completion_repair_timeout_seconds": COMPLETION_REPAIR_TIMEOUT_SECONDS,
                "completion_repair_runtime_profile": _cr_active_profile,
                "completion_repair_started_at": _cr_started_at,
                "completion_repair_duration_seconds": _cr_duration,
                "completion_repair_timed_out": _cr_timed_out,
                "completion_repair_exception_type": _cr_exc_type,
                "completion_repair_fast_profile_selected": bool(_cr_fast_runtime),
                "completion_repair_fast_profile_fallback": _cr_fast_fallback,
            },
        )
        raise

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

    if not repair_step.get("commands") and not repair_step.get("ops"):
        return {"status": "failed", "reason": "repair_step_missing_commands_or_ops"}

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
                "phase": OrchestrationPhase.COMPLETION_REPAIR,
                "invalid_paths": invalid_paths[:10],
            },
        )
        append_orchestration_event(
            project_dir=orchestration_state.project_dir,
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            event_type=EventType.REPAIR_REJECTED,
            details={
                "phase": OrchestrationPhase.COMPLETION_REPAIR,
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
                    "phase": OrchestrationPhase.COMPLETION_REPAIR,
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

    signature_guard_result = check_completion_repair_signature_contract(
        project_dir=Path(orchestration_state.project_dir),
        ops=repair_step.get("ops"),
    )
    signature_guard_details = completion_repair_signature_violation_event_details(
        signature_guard_result
    )
    if signature_guard_result.violations:
        logger.warning(
            "[ORCHESTRATION] Completion repair rejected by signature guard: %s",
            signature_guard_details["completion_repair_signature_violation_types"],
        )
        append_orchestration_event(
            project_dir=orchestration_state.project_dir,
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            event_type=EventType.REPAIR_REJECTED,
            details={
                "phase": OrchestrationPhase.COMPLETION_REPAIR,
                "reason": COMPLETION_REPAIR_SIGNATURE_VIOLATION_REASON,
                **signature_guard_details,
            },
        )
        orchestration_state.status = OrchestrationStatus.ABORTED
        orchestration_state.abort_reason = COMPLETION_REPAIR_SIGNATURE_VIOLATION_REASON
        task.error_message = COMPLETION_REPAIR_SIGNATURE_VIOLATION_REASON
        task_execution = (
            db.query(TaskExecution)
            .filter(TaskExecution.id == ctx.task_execution_id)
            .first()
            if ctx.task_execution_id
            else None
        )
        mark_task_attempt_failed(
            task=task,
            session_task_link=ctx.session_task_link,
            task_execution=task_execution,
            error_message=COMPLETION_REPAIR_SIGNATURE_VIOLATION_REASON,
            completed_at=datetime.now(UTC),
            workspace_status="blocked",
        )
        if session:
            mark_session_paused(
                session,
                alert_level="error",
                alert_message=COMPLETION_REPAIR_SIGNATURE_VIOLATION_REASON,
            )
        save_orchestration_checkpoint_fn(
            db, ctx.session_id, ctx.task_id, ctx.prompt, orchestration_state
        )
        db.commit()
        emit_live(
            "ERROR",
            "[ORCHESTRATION] Completion repair rejected before execution by signature contract guard",
            metadata={
                "phase": OrchestrationPhase.COMPLETION_REPAIR,
                "reason": COMPLETION_REPAIR_SIGNATURE_VIOLATION_REASON,
                **signature_guard_details,
            },
        )
        return {
            "status": "failed",
            "reason": COMPLETION_REPAIR_SIGNATURE_VIOLATION_REASON,
        }

    emit_live(
        "INFO",
        "[ORCHESTRATION] Completion repair signature guard completed before execution",
        metadata={
            "phase": OrchestrationPhase.COMPLETION_REPAIR,
            **signature_guard_details,
        },
    )

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
            "phase": OrchestrationPhase.COMPLETION_REPAIR,
            "step_index": next_step_number,
            "strategy": strategy_info,
        },
    )

    step_started_at = datetime.now(UTC)
    _pre_sig_snapshot: dict = {}
    if repair_step.get("ops"):
        # Direct ops application: apply structured file ops in-process, bypass OpenClaw.
        _ops_result = _apply_completion_repair_ops_direct(
            repair_step["ops"], Path(orchestration_state.project_dir)
        )
        if not repair_step.get("expected_files"):
            repair_step["expected_files"] = _ops_result["applied"]
        repair_exec_result: dict = {
            "status": "completed" if _ops_result["success"] else "failed",
            "output": (
                "Direct ops applied: " + ", ".join(_ops_result["applied"])
                if _ops_result["success"]
                else "Ops application errors: " + "; ".join(_ops_result["errors"][:5])
            ),
            "files_changed": _ops_result["applied"],
        }
        if not _ops_result["success"]:
            repair_exec_result["error"] = "; ".join(_ops_result["errors"][:5])
    else:
        # Command-only path: pre-snapshot Python signatures for post-diff guard.
        _post_diff_py_paths = [
            str(f or "").strip().lstrip("./")
            for f in (repair_step.get("expected_files") or [])
            if str(f or "").strip().endswith(".py")
        ]
        if signature_guard_result.candidate_unavailable and _post_diff_py_paths:
            _pre_sig_snapshot = snapshot_public_python_signatures(
                Path(orchestration_state.project_dir), _post_diff_py_paths
            )

        execution_prompt = assemble_execution_prompt(ctx, repair_step)
        step_timeout_seconds = determine_step_timeout(
            timeout_seconds=ctx.timeout_seconds,
            total_steps=len(orchestration_state.plan),
            execution_profile=ctx.execution_profile,
            step_description=repair_step["description"],
            task_prompt=ctx.prompt,
        )
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

        if _pre_sig_snapshot:
            _post_diff_result = check_post_execution_signature_drift(
                _pre_sig_snapshot, Path(orchestration_state.project_dir)
            )
            _post_diff_event_details = (
                completion_repair_signature_violation_event_details(_post_diff_result)
            )
            emit_live(
                "ERROR" if _post_diff_result.violations else "INFO",
                "[COMPLETION_REPAIR] Post-execution signature diff guard completed",
                metadata={
                    "phase": OrchestrationPhase.COMPLETION_REPAIR,
                    **_post_diff_event_details,
                },
            )
            if _post_diff_result.violations:
                logger.warning(
                    "[ORCHESTRATION] Command-only completion repair rejected by post-execution signature diff guard: %s",
                    _post_diff_event_details[
                        "completion_repair_signature_violation_types"
                    ],
                )
                append_orchestration_event(
                    project_dir=orchestration_state.project_dir,
                    session_id=ctx.session_id,
                    task_id=ctx.task_id,
                    event_type=EventType.REPAIR_REJECTED,
                    details={
                        "phase": OrchestrationPhase.COMPLETION_REPAIR,
                        "reason": COMPLETION_REPAIR_POST_DIFF_VIOLATION_REASON,
                        **_post_diff_event_details,
                    },
                )
                orchestration_state.status = OrchestrationStatus.ABORTED
                orchestration_state.abort_reason = (
                    COMPLETION_REPAIR_POST_DIFF_VIOLATION_REASON
                )
                task.error_message = COMPLETION_REPAIR_POST_DIFF_VIOLATION_REASON
                _post_diff_task_execution = (
                    db.query(TaskExecution)
                    .filter(TaskExecution.id == ctx.task_execution_id)
                    .first()
                    if ctx.task_execution_id
                    else None
                )
                mark_task_attempt_failed(
                    task=task,
                    session_task_link=ctx.session_task_link,
                    task_execution=_post_diff_task_execution,
                    error_message=COMPLETION_REPAIR_POST_DIFF_VIOLATION_REASON,
                    completed_at=datetime.now(UTC),
                    workspace_status="blocked",
                )
                if session:
                    mark_session_paused(
                        session,
                        alert_level="error",
                        alert_message=COMPLETION_REPAIR_POST_DIFF_VIOLATION_REASON,
                    )
                save_orchestration_checkpoint_fn(
                    db, ctx.session_id, ctx.task_id, ctx.prompt, orchestration_state
                )
                db.commit()
                emit_live(
                    "ERROR",
                    "[ORCHESTRATION] Command-only completion repair rejected after execution by post-diff signature drift guard",
                    metadata={
                        "phase": OrchestrationPhase.COMPLETION_REPAIR,
                        "reason": COMPLETION_REPAIR_POST_DIFF_VIOLATION_REASON,
                        **_post_diff_event_details,
                    },
                )
                return {
                    "status": "failed",
                    "reason": COMPLETION_REPAIR_POST_DIFF_VIOLATION_REASON,
                }

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
            metadata={
                "phase": OrchestrationPhase.COMPLETION_REPAIR,
                "step_index": next_step_number,
            },
        )
        append_orchestration_event(
            project_dir=orchestration_state.project_dir,
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            event_type=EventType.REPAIR_APPLIED,
            details={
                "phase": OrchestrationPhase.COMPLETION_REPAIR,
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
            "phase": OrchestrationPhase.COMPLETION_REPAIR,
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
            "phase": OrchestrationPhase.COMPLETION_REPAIR,
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
    """Shim — completion lifecycle is owned by CompletionCoordinator."""
    from app.services.orchestration.coordinators.completion_coordinator import (
        CompletionCoordinator,
    )

    return CompletionCoordinator().complete_task(
        ctx=ctx,
        write_project_state_snapshot_fn=write_project_state_snapshot_fn,
        save_orchestration_checkpoint_fn=save_orchestration_checkpoint_fn,
        get_next_pending_project_task_fn=get_next_pending_project_task_fn,
        get_latest_session_task_link_fn=get_latest_session_task_link_fn,
        execute_orchestration_task_delay_fn=execute_orchestration_task_delay_fn,
        build_task_report_payload_fn=build_task_report_payload_fn,
        render_task_report_fn=render_task_report_fn,
    )
