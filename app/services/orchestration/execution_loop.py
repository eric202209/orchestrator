"""Execution/debug loop for step-by-step orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from app.models import TaskStatus
from app.services import PromptTemplates
from app.services.orchestration.completion_flow import finalize_successful_task
from app.services.orchestration.execution_flow import (
    assess_step_execution,
    determine_step_timeout,
    repeated_tool_path_failure_decision,
)
from app.services.orchestration.executor import ExecutorService
from app.services.orchestration.persistence import (
    record_validation_verdict,
    save_orchestration_checkpoint,
    set_session_alert,
)
from app.services.orchestration.step_support import (
    coerce_execution_step_result,
    repair_step_commands_with_self_correction,
    step_needs_command_repair,
)
from app.services.orchestration.validator import ValidatorService
from app.services.prompt_templates import OrchestrationStatus, StepResult


def execute_step_loop(
    *,
    db: Any,
    session: Any,
    project: Any,
    task: Any,
    session_task_link: Any,
    session_id: int,
    task_id: int,
    prompt: str,
    timeout_seconds: int,
    execution_profile: str,
    validation_profile: str,
    orchestration_state: Any,
    openclaw_service: Any,
    task_service: Any,
    logger: logging.Logger,
    emit_live: Callable[..., None],
    error_handler: Any,
    extract_structured_text: Callable[[Any], str],
    normalize_step: Callable[..., Dict[str, Any]],
    normalize_plan_with_live_logging: Callable[..., Any],
    restore_workspace_snapshot_if_needed: Callable[[str], Optional[Dict[str, Any]]],
    workspace_violation_error_cls: type[Exception],
    get_next_pending_project_task_fn: Callable[..., Any],
    get_latest_session_task_link_fn: Callable[..., Any],
    execute_openclaw_task_delay_fn: Callable[..., Any],
    build_task_report_payload_fn: Callable[..., Dict[str, Any]],
    render_task_report_fn: Callable[..., Dict[str, Any]],
    write_project_state_snapshot_fn: Callable[..., None],
    record_live_log_fn: Callable[..., None],
) -> Dict[str, Any]:
    logger.info(
        "[ORCHESTRATION] Phase 2: EXECUTING - executing %s steps",
        len(orchestration_state.plan),
    )
    emit_live(
        "INFO",
        f"[ORCHESTRATION] Phase 2: EXECUTING - executing {len(orchestration_state.plan)} steps",
        metadata={"phase": "executing", "steps": len(orchestration_state.plan)},
    )

    for step_index in range(
        orchestration_state.current_step_index, len(orchestration_state.plan)
    ):
        step = orchestration_state.plan[step_index]
        db.refresh(session)
        if session.status in ["stopped", "paused"] or not session.is_active:
            logger.info(
                "[ORCHESTRATION] Session %s marked %s; stopping task execution before step %s",
                session_id,
                session.status,
                step_index + 1,
            )
            save_orchestration_checkpoint(
                db, session_id, task_id, prompt, orchestration_state
            )
            task.status = TaskStatus.CANCELLED
            task.completed_at = datetime.utcnow()
            if session_task_link:
                session_task_link.status = TaskStatus.CANCELLED
                session_task_link.completed_at = task.completed_at
            db.commit()
            restore_workspace_snapshot_if_needed(f"session {session.status}")
            write_project_state_snapshot_fn(db, project, task, session_id)
            return {
                "status": "cancelled",
                "task_id": task_id,
                "session_id": session_id,
                "reason": f"session_{session.status}",
            }

        orchestration_state.current_step_index = step_index
        task.current_step = step_index + 1
        save_orchestration_checkpoint(
            db, session_id, task_id, prompt, orchestration_state
        )
        db.commit()

        step_description = step.get("description", f"Step {step_index + 1}")
        step_commands = step.get("commands", [])
        verification_command = step.get("verification")
        rollback_command = step.get("rollback")
        expected_files = step.get("expected_files", [])

        if step_needs_command_repair(step):
            repaired_step = None
            for repair_attempt in range(1, 3):
                emit_live(
                    "WARN",
                    (
                        f"[ORCHESTRATION] Step {step_index + 1} has no runnable commands; "
                        f"attempting self-correction ({repair_attempt}/2)"
                    ),
                    metadata={
                        "phase": "step_validation",
                        "step_index": step_index + 1,
                        "attempt": repair_attempt,
                    },
                )
                repaired_step = repair_step_commands_with_self_correction(
                    openclaw_service=openclaw_service,
                    db=db,
                    session_id=session_id,
                    task_id=task_id,
                    session_instance_id=session.instance_id if session else None,
                    task_prompt=prompt,
                    step=step,
                    step_index=step_index,
                    project_dir=orchestration_state.project_dir,
                    prior_results_summary=orchestration_state.prior_results_summary(),
                    project_context=orchestration_state.project_context,
                    logger_obj=logger,
                    extract_structured_text=extract_structured_text,
                    normalize_step=normalize_step,
                    record_live_log=record_live_log_fn,
                )
                if repaired_step is not None:
                    orchestration_state.plan[step_index] = repaired_step
                    step = repaired_step
                    task.steps = json.dumps(orchestration_state.plan)
                    save_orchestration_checkpoint(
                        db, session_id, task_id, prompt, orchestration_state
                    )
                    db.commit()
                    break

            if repaired_step is None:
                manual_gate_message = (
                    f"Step {step_index + 1} generated empty or invalid commands twice. "
                    "Manual review is required before execution can continue."
                )
                orchestration_state.status = OrchestrationStatus.ABORTED
                orchestration_state.abort_reason = manual_gate_message
                task.status = TaskStatus.FAILED
                task.error_message = manual_gate_message
                if session_task_link:
                    session_task_link.status = TaskStatus.FAILED
                    session_task_link.completed_at = datetime.utcnow()
                session.status = "paused"
                session.is_active = False
                set_session_alert(session, "error", manual_gate_message)
                db.commit()
                restore_workspace_snapshot_if_needed("manual review gate")
                write_project_state_snapshot_fn(db, project, task, session_id)
                return {"status": "failed", "reason": "manual_review_required"}

            step_description = step.get("description", f"Step {step_index + 1}")
            step_commands = step.get("commands", [])
            verification_command = step.get("verification")
            rollback_command = step.get("rollback")
            expected_files = step.get("expected_files", [])

        logger.info(
            "[ORCHESTRATION] Executing step %s/%s: %s...",
            step_index + 1,
            len(orchestration_state.plan),
            step_description[:80],
        )
        emit_live(
            "INFO",
            f"[ORCHESTRATION] Executing step {step_index + 1}/{len(orchestration_state.plan)}: {step_description}",
            metadata={
                "phase": "executing",
                "step_index": step_index + 1,
                "step_total": len(orchestration_state.plan),
            },
        )

        logger.info(
            "[ORCHESTRATION] Step data: commands=%s, verification=%s",
            step_commands,
            verification_command,
        )

        execution_prompt = PromptTemplates.build_execution_prompt(
            step_description=step_description,
            step_commands=step_commands,
            project_dir=str(orchestration_state.project_dir),
            verification_command=verification_command,
            rollback_command=rollback_command,
            expected_files=expected_files,
            completed_steps_summary=orchestration_state.prior_results_summary(),
            project_context=orchestration_state.project_context,
            execution_profile=execution_profile,
        )

        step_timeout_seconds = determine_step_timeout(
            timeout_seconds=timeout_seconds,
            total_steps=len(orchestration_state.plan),
            execution_profile=execution_profile,
            step_description=step_description,
            task_prompt=prompt,
        )

        step_started_at = datetime.utcnow()
        step_result = asyncio.run(
            openclaw_service.execute_task(
                execution_prompt,
                timeout_seconds=step_timeout_seconds,
            )
        )
        step_result = coerce_execution_step_result(
            step_result,
            expected_files=expected_files,
            extract_structured_text=extract_structured_text,
        )

        step_debug_attempts = [
            da
            for da in orchestration_state.debug_attempts
            if da.get("attempt") is not None and da.get("step_index", -1) == step_index
        ]
        current_attempt = len(step_debug_attempts) + 1
        max_attempts = 3

        assessment = assess_step_execution(
            db=db,
            session_id=session_id,
            task_id=task_id,
            project_dir=orchestration_state.project_dir,
            step=step,
            step_result=step_result,
            step_started_at=step_started_at,
            validation_profile=validation_profile,
        )
        step_output = assessment.step_output
        step_status = assessment.step_status
        missing_files = assessment.missing_files
        tool_failures = assessment.tool_failures
        correction_hints = assessment.correction_hints
        step_result["error"] = assessment.error_message

        if missing_files:
            emit_live(
                "WARN",
                (
                    f"[ORCHESTRATION] Step {step_index + 1} reported success but "
                    f"did not materialize expected files: {', '.join(missing_files[:6])}"
                ),
                metadata={
                    "phase": "executing",
                    "step_index": step_index + 1,
                    "missing_expected_files": missing_files[:20],
                },
            )

        if tool_failures:
            emit_live(
                "WARN",
                (
                    f"[ORCHESTRATION] Step {step_index + 1} reported success but "
                    "task logs contain tool failures"
                ),
                metadata={
                    "phase": "executing",
                    "step_index": step_index + 1,
                    "tool_failures": tool_failures[:10],
                    "correction_hints": correction_hints[:10],
                },
            )

        if assessment.validation_verdict:
            record_validation_verdict(
                db,
                session_id,
                task_id,
                orchestration_state,
                assessment.validation_verdict,
                step_number=step_index + 1,
            )
            db.commit()
            if not assessment.validation_verdict.accepted:
                emit_live(
                    "WARN",
                    f"[ORCHESTRATION] Step {step_index + 1} failed validation after execution",
                    metadata={
                        "phase": "step_validation",
                        "step_index": step_index + 1,
                        "validation_status": assessment.validation_verdict.status,
                        "reasons": assessment.validation_verdict.reasons[:10],
                    },
                )

        step_record = StepResult(
            step_number=step_index + 1,
            status=step_status,
            output=step_output[:1000],
            verification_output=step_result.get("verification_output", ""),
            files_changed=step_result.get("files_changed", expected_files),
            error_message=step_result.get("error", ""),
            attempt=current_attempt,
        )

        if step_status == "success":
            orchestration_state.record_success(step_record)
            save_orchestration_checkpoint(
                db, session_id, task_id, prompt, orchestration_state
            )
            logger.info(
                "[ORCHESTRATION] Step %s completed successfully", step_index + 1
            )
            emit_live(
                "INFO",
                f"[ORCHESTRATION] Step {step_index + 1} completed successfully",
                metadata={"phase": "executing", "step_index": step_index + 1},
            )
            continue

        orchestration_state.record_failure(step_record)
        save_orchestration_checkpoint(
            db, session_id, task_id, prompt, orchestration_state
        )

        logger.info(
            "[ORCHESTRATION] Step %s failed, entering DEBUGGING phase",
            step_index + 1,
        )
        emit_live(
            "WARN",
            f"[ORCHESTRATION] Step {step_index + 1} failed, entering DEBUGGING phase",
            metadata={"phase": "debugging", "step_index": step_index + 1},
        )

        if ExecutorService.is_repeated_tool_path_failure(
            orchestration_state.debug_attempts, step_record.error_message
        ):
            decision = repeated_tool_path_failure_decision(
                step_index=step_index,
                execution_profile=execution_profile,
                validation_profile=validation_profile,
                expected_files=expected_files,
                step=step,
                project_dir=orchestration_state.project_dir,
                error_message=step_record.error_message,
            )
            if decision.action == "rewrite_step":
                rewritten_step = decision.rewritten_step or step
                orchestration_state.plan[step_index] = rewritten_step
                task.steps = json.dumps(orchestration_state.plan)
                orchestration_state.debug_attempts.append(
                    {
                        "attempt": len(orchestration_state.debug_attempts) + 1,
                        "fix_type": "command_fix",
                        "fix": "Rewrote inspection step into workspace discovery commands after repeated guessed-path failures",
                        "analysis": step_record.error_message[:500],
                        "confidence": "HIGH",
                        "error": step_record.error_message,
                    }
                )
                save_orchestration_checkpoint(
                    db, session_id, task_id, prompt, orchestration_state
                )
                db.commit()
                emit_live(
                    "WARN",
                    f"[ORCHESTRATION] {decision.message}",
                    metadata={
                        "phase": "debugging",
                        "step_index": step_index + 1,
                        "reason": "repeated_tool_path_failure_rewritten_step",
                    },
                )
                continue

            manual_gate_message = decision.message
            logger.warning("[ORCHESTRATION] %s", manual_gate_message)
            emit_live(
                "ERROR",
                f"[ORCHESTRATION] {manual_gate_message}",
                metadata={
                    "phase": "debugging",
                    "step_index": step_index + 1,
                    "manual_review_required": True,
                    "reason": "repeated_tool_path_failure",
                },
            )
            orchestration_state.status = OrchestrationStatus.ABORTED
            orchestration_state.abort_reason = manual_gate_message
            task.status = TaskStatus.FAILED
            task.error_message = manual_gate_message
            db.commit()
            set_session_alert(session, "error", manual_gate_message)
            restore_workspace_snapshot_if_needed("repeated tool/path failures")
            write_project_state_snapshot_fn(db, project, task, session_id)
            return {"status": "failed", "reason": "manual_review_required"}

        if current_attempt >= max_attempts:
            emit_live(
                "ERROR",
                f"[ORCHESTRATION] Step {step_index + 1} failed after {current_attempt} attempts, marking as failed",
                metadata={
                    "phase": "debugging",
                    "step_index": step_index + 1,
                    "max_attempts_reached": True,
                },
            )
            orchestration_state.status = OrchestrationStatus.ABORTED
            orchestration_state.abort_reason = (
                f"Step {step_index + 1} failed after {current_attempt} attempts"
            )
            task.status = TaskStatus.FAILED
            task.error_message = (
                f"Step failed after {current_attempt} attempts: "
                f"{step_record.error_message[:500]}"
            )
            db.commit()
            restore_workspace_snapshot_if_needed("max step attempts reached")
            write_project_state_snapshot_fn(db, project, task, session_id)
            return {"status": "failed", "reason": "max_attempts_reached"}

        debug_prompt = PromptTemplates.build_debugging_prompt(
            step_description=step_description,
            error_message=step_record.error_message,
            command_output=step_output,
            verification_output=step_record.verification_output,
            attempt_number=current_attempt,
            max_attempts=max_attempts,
            prior_debug_attempts=orchestration_state.debug_attempts,
            project_name=orchestration_state.project_name,
            workspace_root=str(orchestration_state.workspace_root),
            project_dir=str(orchestration_state.project_dir),
        )

        debug_result = asyncio.run(
            openclaw_service.execute_task(debug_prompt, timeout_seconds=180)
        )

        try:
            debug_output = debug_result.get("output", "{}")
            success, debug_data, strategy_info = error_handler.attempt_json_parsing(
                debug_output, context="debug"
            )
            if not success:
                raise ValueError(f"Failed to parse debug result: {strategy_info}")

            fix_type = debug_data.get("fix_type", "code_fix")
            logger.info("[DEBUG-PARSE] Using strategy: %s", strategy_info)

            if fix_type == "revise_plan":
                logger.info(
                    "[ORCHESTRATION] Plan revision needed, entering PLAN_REVISION phase"
                )
                emit_live(
                    "WARN",
                    "[ORCHESTRATION] Plan revision needed, entering PLAN_REVISION phase",
                    metadata={"phase": "plan_revision", "step_index": step_index + 1},
                )
                revise_prompt = PromptTemplates.build_plan_revision_prompt(
                    original_plan=orchestration_state.plan,
                    failed_steps=[step_record],
                    debug_analysis=debug_result.get("output", ""),
                    completed_steps=orchestration_state.completed_steps,
                    workspace_root=str(orchestration_state.workspace_root),
                    project_dir=str(orchestration_state.project_dir),
                )
                revise_result = asyncio.run(
                    openclaw_service.execute_task(revise_prompt, timeout_seconds=180)
                )
                revise_output = revise_result.get("output", "{}")
                success, revise_data, strategy_info = (
                    error_handler.attempt_json_parsing(
                        revise_output, context="revision"
                    )
                )
                if not success:
                    raise ValueError(f"Failed to parse revision: {strategy_info}")

                orchestration_state.plan = normalize_plan_with_live_logging(
                    db,
                    session_id,
                    task_id,
                    revise_data.get("revised_plan", orchestration_state.plan),
                    orchestration_state.project_dir,
                    logger,
                    session.instance_id,
                    "Plan revision",
                )
                revised_plan_verdict = ValidatorService.validate_plan(
                    orchestration_state.plan,
                    output_text=revise_output,
                    task_prompt=prompt,
                    execution_profile=execution_profile,
                    project_dir=orchestration_state.project_dir,
                    title=task.title if task else None,
                    description=task.description if task else None,
                )
                record_validation_verdict(
                    db,
                    session_id,
                    task_id,
                    orchestration_state,
                    revised_plan_verdict,
                )
                db.commit()
                if not revised_plan_verdict.accepted:
                    revised_plan_error = "Revised plan failed validation: " + "; ".join(
                        revised_plan_verdict.reasons[:3]
                    )
                    orchestration_state.status = OrchestrationStatus.ABORTED
                    orchestration_state.abort_reason = revised_plan_error
                    task.status = TaskStatus.FAILED
                    task.error_message = revised_plan_error
                    emit_live(
                        "ERROR",
                        "[ORCHESTRATION] Revised plan failed validation",
                        metadata={
                            "phase": "plan_revision",
                            "validation_status": revised_plan_verdict.status,
                            "reasons": revised_plan_verdict.reasons[:10],
                        },
                    )
                    db.commit()
                    restore_workspace_snapshot_if_needed(
                        "revised plan validation failure"
                    )
                    write_project_state_snapshot_fn(db, project, task, session_id)
                    return {
                        "status": "failed",
                        "reason": "revised_plan_validation_failed",
                    }

                logger.info(
                    "[ORCHESTRATION] Plan revised, %s steps",
                    len(orchestration_state.plan),
                )
                emit_live(
                    "INFO",
                    f"[ORCHESTRATION] Plan revised, {len(orchestration_state.plan)} steps",
                    metadata={
                        "phase": "plan_revision",
                        "steps": len(orchestration_state.plan),
                        "strategy": strategy_info,
                    },
                )
                continue

            if fix_type in {"code_fix", "command_fix"}:
                logger.info(
                    "[ORCHESTRATION] Applying %s before retrying step %s",
                    fix_type,
                    step_index + 1,
                )
                orchestration_state.debug_attempts.append(
                    {
                        "attempt": len(orchestration_state.debug_attempts) + 1,
                        "fix_type": fix_type,
                        "fix": debug_data.get("fix", ""),
                        "analysis": debug_data.get("analysis", ""),
                        "confidence": debug_data.get("confidence", "MEDIUM"),
                        "error": step_record.error_message,
                        "step_index": step_index,
                    }
                )
                if fix_type == "command_fix" and debug_data.get("fix"):
                    step["commands"] = [
                        debug_data.get("fix", step_commands[0] if step_commands else "")
                    ]
                    orchestration_state.plan[step_index] = step
                    task.steps = json.dumps(orchestration_state.plan)
                emit_live(
                    "INFO",
                    f"[ORCHESTRATION] Fix applied ({fix_type}), retrying step {step_index + 1}",
                    metadata={
                        "phase": "debugging",
                        "step_index": step_index + 1,
                        "fix_type": fix_type,
                    },
                )
                save_orchestration_checkpoint(
                    db, session_id, task_id, prompt, orchestration_state
                )
                db.commit()
                continue

        except workspace_violation_error_cls as exc:
            orchestration_state.status = OrchestrationStatus.ABORTED
            orchestration_state.abort_reason = f"Workspace isolation violation: {exc}"
            task.status = TaskStatus.FAILED
            task.error_message = str(exc)
            db.commit()
            restore_workspace_snapshot_if_needed("debug workspace isolation violation")
            return {"status": "failed", "reason": "workspace_isolation_violation"}
        except Exception as exc:
            logger.error("[ORCHESTRATION] Debug parsing failed: %s", exc)
            orchestration_state.status = OrchestrationStatus.ABORTED
            orchestration_state.abort_reason = f"Debug parse failed: {exc}"
            task.status = TaskStatus.FAILED
            task.error_message = str(exc)
            db.commit()
            restore_workspace_snapshot_if_needed("debug parse error")
            return {"status": "failed", "reason": "debug_parse_error"}

    return finalize_successful_task(
        db=db,
        openclaw_service=openclaw_service,
        task_service=task_service,
        session=session,
        project=project,
        task=task,
        session_task_link=session_task_link,
        session_id=session_id,
        task_id=task_id,
        prompt=prompt,
        execution_profile=execution_profile,
        validation_profile=validation_profile,
        orchestration_state=orchestration_state,
        emit_live=emit_live,
        logger=logger,
        write_project_state_snapshot_fn=write_project_state_snapshot_fn,
        get_next_pending_project_task_fn=get_next_pending_project_task_fn,
        get_latest_session_task_link_fn=get_latest_session_task_link_fn,
        execute_openclaw_task_delay_fn=execute_openclaw_task_delay_fn,
        build_task_report_payload_fn=build_task_report_payload_fn,
        render_task_report_fn=render_task_report_fn,
    )
