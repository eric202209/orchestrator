"""CompletionCoordinator — owns the completion lifecycle orchestration.

Phase 14B-1: Extracts the completion lifecycle from completion_flow.py into a
single, owned orchestration surface.

Orchestration decisions live here. Algorithms are delegated to helpers,
validators, and lifecycle services.
"""

from __future__ import annotations

import asyncio
import json
import os
import subprocess
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from app.config import settings
from app.models import TaskExecution, TaskStatus
from app.services.orchestration.context.assembly import assemble_task_summary_prompt
from app.services.orchestration.diagnostics.debug_feedback import (
    build_debug_feedback_envelope,
    persist_debug_feedback_envelope,
)
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.events.telemetry import emit_phase_event
from app.services.orchestration.execution.runtime import (
    workspace_snapshot_key,
    write_project_state_snapshot,
)
from app.services.orchestration.lifecycle.completion import TaskCompletionFinalizer
from app.services.orchestration.phases.completion_summary import (
    _generate_task_summary_with_fallback,
)
from app.services.orchestration.phases.completion_workspace import (
    _scope_workspace_consistency_to_task_changes,
)
from app.services.orchestration.recovery.execution_recovery_evidence import (
    build_completion_recovery_evidence,
)
from app.services.orchestration.recovery.recovery_context import RecoveryContext
from app.services.orchestration.recovery.recovery_strategy_registry import (
    RecoveryStrategyRegistry,
)
from app.services.orchestration.review_policy import decide_change_set_review
from app.services.orchestration.run_state import mark_task_attempt_failed
from app.services.orchestration.state.execution_states import (
    OrchestrationPhase,
    TerminalReason,
)
from app.services.orchestration.state.persistence import (
    append_orchestration_event,
    attach_failure_envelope,
    record_validation_verdict,
    save_orchestration_checkpoint,
)
from app.services.orchestration.state.session_state import mark_session_paused
from app.services.orchestration.types import OrchestrationRunContext, ValidationVerdict
from app.services.orchestration.validation.integrity import (
    capture_baseline_result,
    compare_baseline,
)
from app.services.orchestration.validation.parsing import extract_structured_text
from app.services.orchestration.validation.validator import ValidatorService
from app.services.prompt_templates import OrchestrationStatus
from app.services.workspace.workspace_paths import TASK_REPORT_ROOT


@dataclass
class CompletionOutcome:
    """Typed result from CompletionCoordinator.complete_task.

    Exposes orchestration intent (what happened and why) rather than raw dict
    keys. The coordinator converts this to the externally observable dict on
    return so no callers are affected.
    """

    status: str
    terminal_reason: Optional[str] = None
    verification_passed: bool = False
    repair_attempted: bool = False
    repair_succeeded: bool = False
    task_id: Optional[int] = None
    session_id: Optional[int] = None
    steps_completed: int = 0
    debug_attempts: int = 0
    summary: str = ""
    events: list = field(default_factory=list)


class CompletionCoordinator:
    """Owns the completion lifecycle orchestration for a task.

    Orchestration decisions (validate, repair, verify, abort, succeed) are made
    here. Algorithms (validation logic, repair step generation, summary
    generation) are delegated to helpers and services.
    """

    def complete_task(
        self,
        *,
        ctx: OrchestrationRunContext,
        write_project_state_snapshot_fn: Callable[
            ..., None
        ] = write_project_state_snapshot,
        save_orchestration_checkpoint_fn: Callable[
            ..., None
        ] = save_orchestration_checkpoint,
        get_next_pending_project_task_fn: Optional[Callable[..., Any]] = None,
        get_latest_session_task_link_fn: Optional[Callable[..., Any]] = None,
        execute_orchestration_task_delay_fn: Optional[Callable[..., Any]] = None,
        build_task_report_payload_fn: Optional[Callable[..., Dict[str, Any]]] = None,
        render_task_report_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    ) -> Dict[str, Any]:
        """Execute the completion lifecycle and return a result dict.

        Owns: validation routing, repair routing, verification routing, terminal
        outcome decision.
        Delegates: summary generation, validators, repair helpers, finalizer.
        """
        # Deferred imports from completion_flow so that test patches on
        # completion_flow.* are respected at call time.
        from app.services.orchestration.phases.completion_flow import (
            _attempt_completion_repair,
            _classify_completion_verification_failure,
            _detect_completion_verification_command,
            _execute_completion_verification,
            _resolve_template_review_policy,
            _run_evaluator,
            _write_progress_notes,
            get_effective_workspace_review_policy,
        )

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
            phase="task_summary",
            coordinator="CompletionCoordinator",
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
                        "phase": OrchestrationPhase.COMPLETION_REPAIR,
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
                    phase="task_summary",
                    coordinator="CompletionCoordinator",
                )
                write_project_state_snapshot_fn(db, project, task, session_id)
                return {
                    "status": "failed",
                    "reason": TerminalReason.COMPLETION_REPAIR_FAILED,
                }

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
                failure_phase=OrchestrationPhase.COMPLETION_VALIDATION,
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
                phase="task_summary",
                coordinator="CompletionCoordinator",
            )
            # Phase 13B-S3: bounded execution recovery before aborting.
            # Routes to real recovery only when failure_class=="missing_requested_symbol".
            # All other failures fall through to ABORT unchanged.
            _completion_recovery_evidence = build_completion_recovery_evidence(
                completion_validation=completion_validation,
                debug_feedback_envelope=debug_feedback_envelope,
                orchestration_state=orchestration_state,
                task_title=getattr(task, "title", "") or "",
                task_prompt=prompt,
            )

            _completion_recovery_timeout = 90

            def _completion_recovery_llm_callable(_prompt_text: str) -> str:
                try:
                    _result = asyncio.run(
                        runtime_service.execute_task(
                            _prompt_text,
                            timeout_seconds=_completion_recovery_timeout,
                        )
                    )
                    return extract_structured_text(_result.get("output", ""))
                except Exception:
                    return ""

            def _completion_recovery_command_runner(_cmd: str) -> tuple:
                try:
                    _proc = subprocess.run(
                        _cmd,
                        shell=True,
                        capture_output=True,
                        text=True,
                        cwd=str(orchestration_state.project_dir),
                        timeout=120,
                    )
                    return _proc.returncode, _proc.stdout, _proc.stderr
                except subprocess.TimeoutExpired:
                    return -1, "", "Recovery rerun timed out"
                except Exception as _exc:
                    return -1, "", f"Recovery rerun error: {_exc}"

            def _completion_recovery_validator_callable(_patch_path: str) -> tuple:
                try:
                    _verdict = ValidatorService.validate_task_completion(
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
                    if not _verdict.accepted:
                        return False, " | ".join(_verdict.reasons[:3])
                    return True, ""
                except Exception as _exc:
                    return False, f"validator_exception:{_exc}"

            _completion_recovery_context = RecoveryContext(
                project_dir=orchestration_state.project_dir,
                session_id=session_id,
                task_id=task_id,
                evidence=_completion_recovery_evidence,
                orchestration_state=orchestration_state,
                scope="completion",
                step_index=None,
                llm_callable=_completion_recovery_llm_callable,
                command_runner=_completion_recovery_command_runner,
                validator_callable=_completion_recovery_validator_callable,
            )
            _completion_recovery_result = RecoveryStrategyRegistry.execute_recovery(
                context=_completion_recovery_context,
            )

            # S3: if recovery succeeded, re-run the authoritative completion validator.
            if _completion_recovery_result.get("status") == "success":
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
                    db, session_id, task_id, orchestration_state, completion_validation
                )
                db.commit()

            # ABORT path — fires when original validation failed and recovery did not
            # succeed, OR when recovery succeeded but re-validation still rejected.
            if not completion_validation.accepted:
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
                        session,
                        alert_level="error",
                        alert_message=completion_error[:2000],
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
                return {
                    "status": "failed",
                    "reason": TerminalReason.COMPLETION_VALIDATION_FAILED,
                }
            # else: recovery succeeded and re-validation accepted — fall through to
            # success path.

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
                verification_failure_verdict = (
                    _classify_completion_verification_failure(
                        command=completion_verification_command,
                        source=completion_verification_source,
                        verification_output=str(
                            completion_verification.get("output") or ""
                        ),
                        completion_validation=completion_validation,
                    )
                )
                if (
                    verification_failure_verdict
                    and verification_failure_verdict.repairable
                ):
                    completion_verification_before_repair = dict(
                        completion_verification
                    )
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
                                "phase": OrchestrationPhase.COMPLETION_REPAIR,
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
                                "phase": OrchestrationPhase.COMPLETION_REPAIR,
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
                                "task_status": str(
                                    task.status.value if task else "failed"
                                ),
                            },
                            phase="task_summary",
                            coordinator="CompletionCoordinator",
                        )
                        write_project_state_snapshot_fn(db, project, task, session_id)
                        return {
                            "status": "failed",
                            "reason": TerminalReason.COMPLETION_REPAIR_FAILED,
                        }

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
                                    "phase": OrchestrationPhase.COMPLETION_REPAIR,
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
                                    returncode=completion_verification.get(
                                        "returncode"
                                    ),
                                    stderr=str(
                                        completion_verification.get("output") or ""
                                    ),
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
                                "output": str(
                                    completion_verification.get("output") or ""
                                )[:2000],
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
                            phase="task_summary",
                            coordinator="CompletionCoordinator",
                        )
                        write_project_state_snapshot_fn(db, project, task, session_id)
                        return {
                            "status": "failed",
                            "reason": TerminalReason.COMPLETION_VERIFICATION_FAILED,
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
                    "execution_results_count": len(
                        orchestration_state.execution_results
                    ),
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
                    phase="task_summary",
                    coordinator="CompletionCoordinator",
                )
                write_project_state_snapshot_fn(db, project, task, session_id)
                return {
                    "status": "failed",
                    "reason": TerminalReason.VERIFICATION_INTEGRITY_FAILED,
                }

        nontrivial_change_flags = list(
            (task_change_set or {}).get("warning_flags") or []
        )
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
                        "changed_count": (task_change_set or {}).get(
                            "changed_count", 0
                        ),
                        "workspace_review_policy": workspace_review_policy,
                    },
                )
            else:
                baseline_publish_result = task_service.auto_publish_task_into_baseline(
                    project, task
                )
                baseline_publish_result["workspace_review_policy"] = (
                    workspace_review_policy
                )
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
                baseline_publish_validation = (
                    ValidatorService.validate_baseline_publish(
                        validation_profile=validation_profile,
                        baseline_path=baseline_materialization.get("baseline_path")
                        or "",
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
            disposition_record = (
                task_service.mark_task_execution_change_set_disposition(
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

        run_post_write_check_if_enabled(
            ctx, reported_changed_files=reported_changed_files
        )

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
