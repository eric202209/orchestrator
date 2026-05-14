"""Task completion and finalization flow."""

import asyncio
import json
import os
from pathlib import Path
from datetime import UTC, datetime
from typing import Any, Callable, Dict, Optional

from app.models import LogEntry, SessionTask, Task, TaskExecution, TaskStatus
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
from app.services.orchestration.state.persistence import (
    append_orchestration_event,
    attach_failure_envelope,
    record_validation_verdict,
    save_orchestration_checkpoint,
)
from app.services.orchestration.policy import (
    SUMMARY_TIMEOUT_SECONDS,
)
from app.services.orchestration.run_state import (
    mark_task_attempt_done,
    mark_task_attempt_failed,
    mark_task_attempt_pending,
)
from app.services.orchestration.state.session_state import (
    clear_session_alert,
    mark_session_paused,
    mark_session_running,
    mark_session_stopped,
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
            "completion_repair_prompt_mode": "phase7h_capsule",
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
        ctx.runtime_service.execute_task(repair_prompt, timeout_seconds=120)
    )
    repair_output = _extract_completion_repair_json_text(
        repair_plan_result.get("output", "{}")
    )
    success, repair_data, strategy_info = error_handler.attempt_json_parsing(
        repair_output, context="completion_repair"
    )
    if not success:
        fallback_output = extract_structured_text(repair_plan_result)
        if fallback_output and fallback_output != repair_output:
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
                    timeout_seconds=120,
                )
            )
            compliance_output = _extract_completion_repair_json_text(
                compliance_result.get("output", "{}")
            )
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
            ctx.runtime_service.execute_task(guarded_retry_prompt, timeout_seconds=120)
        )
        guarded_retry_output = extract_structured_text(
            guarded_retry_result.get("output", "{}")
        )
        retry_success, retry_data, retry_strategy_info = (
            error_handler.attempt_json_parsing(
                guarded_retry_output, context="completion_repair"
            )
        )
        if not retry_success:
            fallback_output = extract_structured_text(guarded_retry_result)
            if fallback_output and fallback_output != guarded_retry_output:
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
        adjusted_expected_files = [
            path
            for path in reported_changed_files
            if path.startswith(("src/", "tests/"))
            or path
            in {
                "vitest.config.ts",
                "jest.config.js",
                "package.json",
                "tsconfig.json",
                ".env.example",
            }
        ]
        if adjusted_expected_files:
            repair_step["expected_files"] = adjusted_expected_files
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
) -> None:
    """Run an independent QA evaluation pass after structural validation passes.

    The evaluator is intentionally separate from the generator: it receives the
    task goal, the execution record, and the summary, then grades the result
    against concrete criteria.  A failing grade is logged as a warning (not a
    hard failure) so the task still completes, but the signal is surfaced in
    the live log and stored in the orchestration events for later review.
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
            runtime_service.execute_task(evaluator_prompt, timeout_seconds=120)
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
    except Exception as e:
        logger.warning("[EVALUATOR] QA evaluation failed (non-blocking): %s", e)


def _write_progress_notes(
    *,
    orchestration_state: Any,
    task: Any,
    prompt: str,
    summary: str,
    logger: Any,
) -> None:
    """Append a structured completion entry to .openclaw/progress_notes.md.

    This replaces git commits as the session artifact bridge when the project is
    not version-controlled.  The orient phase in worker.py reads this file before
    planning to give the next run full context on what was already done.
    """
    try:
        project_dir = getattr(orchestration_state, "project_dir", None)
        if not project_dir:
            return
        notes_dir = Path(project_dir) / ".openclaw"
        notes_dir.mkdir(parents=True, exist_ok=True)
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
        entry_lines.append("")

        with open(notes_path, "a", encoding="utf-8") as fh:
            fh.write("\n".join(entry_lines))
        logger.info("[PROGRESS] Progress notes written to %s", notes_path)
    except Exception as e:
        logger.warning("[PROGRESS] Failed to write progress notes: %s", e)


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
    summary_result = asyncio.run(
        runtime_service.execute_task(
            summary_prompt, timeout_seconds=SUMMARY_TIMEOUT_SECONDS
        )
    )
    reported_changed_files = list(
        dict.fromkeys(
            path
            for result in (orchestration_state.execution_results or [])
            for path in (getattr(result, "files_changed", []) or [])
            if str(path).strip()
        )
    )

    completion_validation = ValidatorService.validate_task_completion(
        project_dir=orchestration_state.project_dir,
        plan=orchestration_state.plan,
        task_prompt=prompt,
        execution_profile=execution_profile,
        workspace_consistency=task_service.analyze_workspace_consistency(
            orchestration_state.project_dir
        ),
        title=task.title if task else None,
        description=task.description if task else None,
        relaxed_mode=orchestration_state.relaxed_mode,
        completion_evidence={
            "summary_generated": bool(summary_result),
            "execution_results_count": len(orchestration_state.execution_results),
            "reported_changed_files": reported_changed_files,
        },
        validation_severity=ctx.validation_severity,
    )
    record_validation_verdict(
        db,
        session_id,
        task_id,
        orchestration_state,
        completion_validation,
    )
    db.commit()

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
                workspace_consistency=task_service.analyze_workspace_consistency(
                    orchestration_state.project_dir
                ),
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
            commit=False,
        )

    nontrivial_change_flags = list((task_change_set or {}).get("warning_flags") or [])
    if hasattr(task_service, "change_set_review_decision"):
        review_decision = task_service.change_set_review_decision(
            task_change_set,
            workspace_review_policy=workspace_review_policy,
        )
    else:
        held_for_review = workspace_review_policy == "hold_all" or (
            workspace_review_policy == "hold_nontrivial"
            and bool(nontrivial_change_flags)
        )
        review_decision = {
            "workspace_review_policy": workspace_review_policy,
            "held_for_review": held_for_review,
            "reason": (
                "hold_all_review_required"
                if workspace_review_policy == "hold_all"
                else (
                    "nontrivial_change_set_review_required" if held_for_review else None
                )
            ),
            "changed_count": int((task_change_set or {}).get("changed_count") or 0),
            "warning_flags": nontrivial_change_flags,
        }
    should_hold_for_review = bool(review_decision["held_for_review"])
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

        _run_evaluator(
            runtime_service=runtime_service,
            orchestration_state=orchestration_state,
            prompt=prompt,
            summary=summary_result.get("output", ""),
            emit_live=emit_live,
            logger=logger,
        )

    task_execution = (
        db.query(TaskExecution)
        .filter(TaskExecution.id == ctx.task_execution_id)
        .first()
        if ctx.task_execution_id
        else None
    )
    completed_at = mark_task_attempt_done(
        task=task,
        session_task_link=session_task_link,
        task_execution=task_execution,
        completed_at=datetime.now(UTC),
    )
    task.summary = summary_result.get("output", "")[:2000]
    task.current_step = len(orchestration_state.plan)
    promoted_workspace_archive_result = None
    if (
        baseline_publish_result
        and not baseline_publish_result.get("auto_publish_skipped")
        and project
        and task.task_subfolder
    ):
        promoted_workspace_archive_result = (
            task_service.archive_promoted_task_workspace(project, task)
        )
        baseline_publish_result["promoted_workspace_archive_result"] = (
            promoted_workspace_archive_result
        )
    else:
        task.workspace_status = "ready" if task.task_subfolder else "not_created"
    task.completed_at = completed_at
    append_orchestration_event(
        project_dir=orchestration_state.project_dir,
        session_id=session_id,
        task_id=task_id,
        event_type=EventType.TASK_COMPLETED,
        details={
            "steps_completed": len(orchestration_state.plan),
            "execution_profile": execution_profile,
        },
    )

    _write_progress_notes(
        orchestration_state=orchestration_state,
        task=task,
        prompt=prompt,
        summary=summary_result.get("output", ""),
        logger=logger,
    )

    clear_session_alert(session)
    db.flush()

    next_task = None
    blocked_pending_task = None
    if (
        session
        and session.execution_mode == "automatic"
        and get_next_pending_project_task_fn
    ):
        next_task = get_next_pending_project_task_fn(db, session.project_id)
        if not next_task and session.project_id:
            blocked_pending_task = (
                db.query(Task)
                .filter(
                    Task.project_id == session.project_id,
                    Task.status == TaskStatus.PENDING,
                )
                .order_by(
                    Task.plan_position.asc().nullslast(),
                    Task.priority.desc(),
                    Task.created_at.asc().nullslast(),
                    Task.id.asc(),
                )
                .first()
            )

    if session:
        if next_task:
            mark_session_running(session)
        elif blocked_pending_task:
            mark_session_paused(session)
            blockers = type(task_service)(db).get_blocking_prior_tasks(
                blocked_pending_task
            )
            if blockers:
                blocking_summary = ", ".join(
                    f"#{item.plan_position} {item.title} ({item.status.value})"
                    for item in blockers[:3]
                )
                mark_session_paused(
                    session,
                    alert_level="warning",
                    alert_message=(
                        "Automatic execution is paused because an earlier ordered task "
                        f"is incomplete: {blocking_summary}"
                    )[:2000],
                )
        else:
            mark_session_stopped(session)

    db.commit()
    write_project_state_snapshot_fn(db, project, task, session_id)

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

    if baseline_publish_result:
        publish_skipped = bool(baseline_publish_result.get("auto_publish_skipped"))
        db.add(
            LogEntry(
                session_id=session_id,
                session_instance_id=session.instance_id,
                task_id=task_id,
                level="INFO",
                message=(
                    "[ORCHESTRATION] Held task workspace for manual review"
                    if publish_skipped
                    else (
                        "[ORCHESTRATION] Published task workspace into canonical project baseline "
                        f"({baseline_publish_result.get('files_copied', 0)} files)"
                    )
                ),
                log_metadata=json.dumps(baseline_publish_result),
            )
        )
        db.commit()

    if (
        session
        and next_task
        and get_latest_session_task_link_fn
        and execute_orchestration_task_delay_fn
    ):
        next_session_task_link = get_latest_session_task_link_fn(
            db, session_id, next_task.id
        )
        if not next_session_task_link:
            next_session_task_link = SessionTask(
                session_id=session_id,
                task_id=next_task.id,
                status=TaskStatus.PENDING,
                started_at=None,
            )
            db.add(next_session_task_link)
        else:
            mark_task_attempt_pending(
                task=None,
                session_task_link=next_session_task_link,
                reset_started_at=True,
            )

        mark_task_attempt_pending(
            task=next_task,
            reset_started_at=True,
            error_message=None,
        )
        from app.services.task_execution_service import create_task_execution

        next_task_execution = create_task_execution(
            db,
            session_id=session_id,
            task_id=next_task.id,
            status=TaskStatus.PENDING,
            started_at=None,
        )

        db.add(
            LogEntry(
                session_id=session_id,
                session_instance_id=session.instance_id,
                task_id=next_task.id,
                task_execution_id=next_task_execution.id,
                level="INFO",
                message=(
                    f"[ORCHESTRATION] Auto-advancing to next task {next_task.id}: {next_task.title}"
                ),
                log_metadata=json.dumps(
                    {
                        "auto_advance": True,
                        "task_execution_id": next_task_execution.id,
                        "plan_position": getattr(next_task, "plan_position", None),
                    }
                ),
            )
        )
        db.commit()
        execute_orchestration_task_delay_fn(
            session_id=session_id,
            task_id=next_task.id,
            prompt=next_task.description or next_task.title,
            timeout_seconds=900,
            task_execution_id=next_task_execution.id,
        )

    if build_task_report_payload_fn and render_task_report_fn:
        try:
            report_payload = build_task_report_payload_fn(db, task_id)
            report_result = render_task_report_fn(
                report_payload, output_format="markdown"
            )
            if report_result and "report" in report_result:
                report_content = report_result["report"]
                report_filename = f"task_report_{task_id}.md"
                report_path = orchestration_state.project_dir / report_filename
                os.makedirs(orchestration_state.project_dir, exist_ok=True)
                with open(report_path, "w", encoding="utf-8") as handle:
                    handle.write(report_content)
                logger.info("[REPORT] Task report saved to: %s", report_path)
        except Exception as report_error:
            logger.error(
                "[REPORT] Failed to generate task report: %s", str(report_error)
            )

    append_orchestration_event(
        project_dir=orchestration_state.project_dir,
        session_id=session_id,
        task_id=task_id,
        event_type=EventType.PHASE_FINISHED,
        details={
            "phase": "task_summary",
            "status": completion_validation.status,
            "task_status": str(task.status.value if task else "done"),
        },
    )

    return {
        "status": "completed",
        "task_id": task_id,
        "session_id": session_id,
        "steps_completed": len(orchestration_state.plan),
        "debug_attempts": len(orchestration_state.debug_attempts),
        "summary": summary_result.get("output", "")[:500],
    }
