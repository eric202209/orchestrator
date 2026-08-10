"""CompletionCoordinator — owns the completion lifecycle orchestration.

Phase 14B-1: Extracts the completion lifecycle from completion_flow.py into a
single, owned orchestration surface.

Orchestration decisions live here. Algorithms are delegated to helpers,
validators, and lifecycle services.
"""

from __future__ import annotations

import json
import os
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
from app.services.orchestration.diagnostics.outcome_observability import (
    bounded_exception_message,
    record_outcome_checkpoint,
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
from app.services.orchestration.phases.completion_repair_capsule import (
    CompletionRepairProgress,
    classify_completion_repair_progress,
    completion_repair_finding_signature,
)
from app.services.orchestration.phases.completion_workspace import (
    _scope_workspace_consistency_to_task_changes,
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
from app.services.orchestration.validation.candidate_checks import (
    candidate_delta_identity,
)
from app.services.orchestration.validation.validator import ValidatorService
from app.services.orchestration.prompt_templates import OrchestrationStatus
from app.services.workspace.project_isolation_service import (
    resolve_project_workspace_path,
)
from app.services.workspace.workspace_paths import TASK_REPORT_ROOT


def _completion_plan_identity(plan: Any) -> str:
    """Canonical in-memory identity for the already accepted Plan."""

    return json.dumps(plan, sort_keys=True, separators=(",", ":"))


def _completion_candidate_scope(validation: Any) -> tuple[str, ...]:
    details = getattr(validation, "details", {}) or {}
    return tuple(
        sorted(str(path) for path in details.get("candidate_authorized_paths", []))
    )


def _retain_completion_repair_verification_evidence(
    validation: Any, repair_result: dict[str, Any]
) -> None:
    """Carry provider verification evidence into the canonical verdict details."""

    provider_verification = repair_result.get("provider_verification") or {}
    validation.details.update(
        {
            key: provider_verification.get(key)
            for key in (
                "verification_command_valid",
                "verification_command",
                "verification_exit_code",
                "verification_passed",
                "verification_output_preview",
            )
        }
    )


def _annotate_completion_repair_progress(
    *,
    before_validation: Any,
    after_validation: Any,
    orchestration_state: Any,
    repair_budget: int,
    accepted_plan_identity: str,
    accepted_candidate_scope: tuple[str, ...],
) -> CompletionRepairProgress:
    """Classify one iteration and retain its bounded convergence evidence."""

    progress = classify_completion_repair_progress(
        before_validation,
        after_validation,
    )
    plan_unchanged = (
        _completion_plan_identity(orchestration_state.plan) == accepted_plan_identity
    )
    scope_unchanged = (
        _completion_candidate_scope(after_validation) == accepted_candidate_scope
    )
    if not plan_unchanged or not scope_unchanged:
        progress = CompletionRepairProgress.NO_PROGRESS_OR_REGRESSION

    budget_used = int(orchestration_state.completion_repair_attempts)
    after_validation.details.update(
        {
            "completion_repair_iteration": budget_used,
            "completion_repair_before_identity": before_validation.candidate_identity,
            "completion_repair_after_identity": after_validation.candidate_identity,
            "completion_repair_before_finding_signature": completion_repair_finding_signature(
                before_validation
            ),
            "completion_repair_after_finding_signature": completion_repair_finding_signature(
                after_validation
            ),
            "completion_repair_progress": progress.value,
            "canonical_progress": progress.value,
            "completion_repair_budget_used": budget_used,
            "completion_repair_budget_remaining": max(repair_budget - budget_used, 0),
            "completion_repair_plan_unchanged": plan_unchanged,
            "completion_repair_scope_unchanged": scope_unchanged,
        }
    )
    return progress


def _build_gating_change_set(
    *,
    task_service: Any,
    project: Any,
    task: Any,
    task_execution_id: Optional[int],
    project_dir: Any,
    preserve_project_root_rules: bool,
    logger: Any,
) -> Optional[Dict[str, Any]]:
    """Build the read-only change set the gating completion validation needs.

    Uses the existing non-persisting builder, so nothing is written and no
    change-set record is created here — the persisted change set is still built
    later, after completion verification. Returns None when the change set
    cannot be built, which restores the previous whole-file validation
    behaviour rather than failing the gate.
    """

    if not (
        project
        and task
        and task_execution_id
        and hasattr(task_service, "build_task_execution_change_set")
    ):
        return None
    try:
        change_set = task_service.build_task_execution_change_set(
            project,
            task,
            task_execution_id=task_execution_id,
            snapshot_key=workspace_snapshot_key(task.id, task_execution_id),
            target_dir=Path(project_dir),
            preserve_project_root_rules=preserve_project_root_rules,
        )
    except Exception as change_set_error:
        logger.warning(
            "[COMPLETION_VALIDATION] Gating change set unavailable, "
            "falling back to whole-file validation: %s",
            change_set_error,
        )
        return None
    return change_set if isinstance(change_set, dict) else None


class CompletionCoordinator:
    """Owns the completion lifecycle orchestration for a task.

    Orchestration decisions (validate, repair, verify, abort, succeed) are made
    here. Algorithms (validation logic, repair step generation, summary
    generation) are delegated to helpers and services.
    """

    def evaluate_completed_execution(
        self,
        *,
        db: Any,
        project: Any,
        task: Any,
        task_execution: Any,
        session_id: Optional[int],
        change_set: Dict[str, Any],
        workspace_review_policy: str,
        planner_contract: Optional[Dict[str, Any]],
        publish: bool = True,
        task_service: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Compatibility seam for the Phase 31 certification harness only."""

        if not change_set:
            raise ValueError("completed execution requires a persisted change set")

        if task_service is None:
            from app.services.tasks.service import TaskService

            task_service = TaskService(db)

        review_decision = task_service.change_set_review_decision(
            change_set,
            workspace_review_policy=workspace_review_policy,
            planner_contract=planner_contract,
        )
        publication_allowed = bool(review_decision.get("publication_allowed", True))
        publication_eligible = bool(
            publication_allowed and not review_decision.get("held_for_review", False)
        )
        review_decision = {
            **review_decision,
            "review_required": bool(review_decision.get("held_for_review", False)),
            "publication_eligible": publication_eligible,
        }

        publication_expectation = str(
            review_decision.get("registered_publication_expectation") or ""
        )
        publication_attempted = False
        publication_result: Dict[str, Any]
        if not publication_allowed:
            publication_result = {
                "status": "not_published",
                "reason": review_decision.get("reason") or "publication_forbidden",
                "publication_eligible": False,
            }
        elif not publication_eligible:
            publication_result = {
                "status": "held_for_review",
                "reason": review_decision.get("reason") or "review_required",
                "publication_eligible": False,
            }
        elif not publish:
            publication_result = {
                "status": "not_published",
                "reason": "publication_attempt_disabled_by_caller",
                "publication_eligible": True,
            }
        elif publication_expectation in {
            "PUBLICATION_REQUIRED",
            "PUBLICATION_ALLOWED",
        }:
            publication_attempted = True
            try:
                publication_result = task_service.promote_change_set_into_baseline(
                    project,
                    task,
                    change_set,
                )
                publication_result = {
                    **publication_result,
                    "status": "published",
                    "publication_eligible": True,
                }
            except Exception as exc:
                publication_result = {
                    "status": "failed",
                    "reason": f"{type(exc).__name__}: {str(exc)[:500]}",
                    "publication_eligible": True,
                }
        else:
            publication_result = {
                "status": "not_published",
                "reason": "publication_not_required",
                "publication_eligible": True,
            }

        if hasattr(db, "commit"):
            db.commit()

        publication_status = publication_result.get("status")
        terminal_classification = {
            "held_for_review": "REVIEW_HELD",
            "failed": "PUBLICATION_FAILED",
            "published": "COMPLETED_PUBLISHED",
            "not_published": "COMPLETED_NO_PUBLICATION",
        }.get(str(publication_status), "COMPLETION_UNCLASSIFIED")
        return {
            "status": "failed" if publication_status == "failed" else "completed",
            "terminal_classification": terminal_classification,
            "task_id": getattr(task, "id", None),
            "task_execution_id": getattr(task_execution, "id", None),
            "session_id": session_id,
            "review_decision": review_decision,
            "publication_result": publication_result,
            "publication_attempted": publication_attempted,
            "publication_persisted": publication_status == "published",
        }

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

        Owns: sequencing one candidate result through repair and terminal outcome.
        Delegates: summary generation, validators, repair helpers, finalizer.
        """
        # Deferred imports from completion_flow so that test patches on
        # completion_flow.* are respected at call time.
        from app.services.orchestration.phases.completion_flow import (
            _attempt_completion_repair,
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

        def _checkpoint(
            operation: str,
            status: str,
            details: Optional[dict[str, Any]] = None,
        ) -> None:
            record_outcome_checkpoint(
                ctx=ctx,
                operation=operation,
                status=status,
                append_orchestration_event_fn=append_orchestration_event,
                logger=logger,
                details=details,
            )

        def _checkpointed(operation: str, fn: Callable[[], Any]) -> Any:
            _checkpoint(operation, "entry")
            try:
                result = fn()
            except Exception as checkpointed_error:
                _checkpoint(
                    operation,
                    "failure",
                    {
                        "exception_type": type(checkpointed_error).__name__,
                        "exception_message": bounded_exception_message(
                            checkpointed_error
                        ),
                    },
                )
                raise
            _checkpoint(operation, "success")
            return result

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

        # Phase 32P-2: the delta-scoped placeholder rule resolves its baseline
        # from completion_evidence["change_set"]. Build that change set
        # read-only here — the persisted one is not created until after
        # verification — so the gating validation and its revalidations judge
        # candidate-changed lines instead of pre-existing baseline debt.
        _gating_change_set_cache: list[Any] = []

        def _gating_completion_evidence(*, rebuild: bool = False) -> Dict[str, Any]:
            if rebuild:
                _gating_change_set_cache.clear()
            if not _gating_change_set_cache:
                _gating_change_set_cache.append(
                    _build_gating_change_set(
                        task_service=task_service,
                        project=project,
                        task=task,
                        task_execution_id=ctx.task_execution_id,
                        project_dir=orchestration_state.project_dir,
                        preserve_project_root_rules=runs_in_canonical_baseline,
                        logger=logger,
                    )
                )
            evidence: Dict[str, Any] = {
                "summary_generated": bool(summary_result),
                "execution_results_count": len(orchestration_state.execution_results),
                "reported_changed_files": reported_changed_files,
                "candidate_delta_required": True,
                "run_candidate_checks": True,
                "include_static_checks": True,
            }
            if _gating_change_set_cache[0]:
                evidence["change_set"] = _gating_change_set_cache[0]
            return evidence

        completion_validation = ValidatorService.validate_task_completion(
            project_dir=orchestration_state.project_dir,
            plan=orchestration_state.plan,
            task_prompt=prompt,
            execution_profile=execution_profile,
            workspace_consistency=workspace_consistency,
            title=task.title if task else None,
            description=task.description if task else None,
            relaxed_mode=orchestration_state.relaxed_mode,
            completion_evidence=_gating_completion_evidence(),
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

        if completion_validation.repairable_findings:
            accepted_plan_identity = _completion_plan_identity(orchestration_state.plan)
            accepted_candidate_scope = _completion_candidate_scope(
                completion_validation
            )
            repair_result: Dict[str, Any] = {
                "status": "skipped",
                "reason": "no_completion_repair_iteration",
            }
            while completion_validation.repairable_findings:
                completion_validation_before_repair = completion_validation
                repair_result = _attempt_completion_repair(
                    ctx=ctx,
                    completion_validation=completion_validation,
                    save_orchestration_checkpoint_fn=save_orchestration_checkpoint_fn,
                )
                if repair_result.get("status") != "success":
                    break
                completion_validation = ValidatorService.validate_task_completion(
                    project_dir=orchestration_state.project_dir,
                    plan=orchestration_state.plan,
                    task_prompt=prompt,
                    execution_profile=execution_profile,
                    workspace_consistency=workspace_consistency,
                    title=task.title if task else None,
                    description=task.description if task else None,
                    relaxed_mode=orchestration_state.relaxed_mode,
                    completion_evidence=_gating_completion_evidence(rebuild=True),
                    validation_severity=ctx.validation_severity,
                    workflow_stage=ctx.workflow_stage,
                    is_first_ordered_task=bool(task and task.plan_position == 1),
                )
                _retain_completion_repair_verification_evidence(
                    completion_validation, repair_result
                )
                repair_progress = _annotate_completion_repair_progress(
                    before_validation=completion_validation_before_repair,
                    after_validation=completion_validation,
                    orchestration_state=orchestration_state,
                    repair_budget=ctx.completion_repair_budget,
                    accepted_plan_identity=accepted_plan_identity,
                    accepted_candidate_scope=accepted_candidate_scope,
                )
                record_validation_verdict(
                    db,
                    session_id,
                    task_id,
                    orchestration_state,
                    completion_validation,
                )
                db.commit()
                if (
                    repair_progress != CompletionRepairProgress.PARTIAL_PROGRESS
                    or orchestration_state.completion_repair_attempts
                    >= ctx.completion_repair_budget
                ):
                    break

            if repair_result.get("status") != "success":
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
            # Candidate Repair is the only automatic recovery path.
            # Non-repairable and unknown findings fail closed.

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
                    "reason": (
                        "completion_repair_partial_progress_budget_exhausted"
                        if completion_validation.details.get(
                            "completion_repair_progress"
                        )
                        == CompletionRepairProgress.PARTIAL_PROGRESS.value
                        else TerminalReason.COMPLETION_VALIDATION_FAILED
                    ),
                }
            # else: recovery succeeded and re-validation accepted — fall through to
            # success path.

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
            task_change_set = _checkpointed(
                "change_set_persisted",
                lambda: task_service.persist_task_execution_change_set(
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
                    planner_contract=getattr(ctx, "planner_contract", None),
                    commit=False,
                ),
            )

        if task_change_set:
            completion_validation = _checkpointed(
                "post_change_set_completion_validation",
                lambda: ValidatorService.validate_task_completion(
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
                        "candidate_delta_required": True,
                        "run_candidate_checks": True,
                        "include_static_checks": True,
                    },
                    validation_severity=ctx.validation_severity,
                    workflow_stage=ctx.workflow_stage,
                    is_first_ordered_task=bool(task and task.plan_position == 1),
                ),
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
            review_decision = _checkpointed(
                "review_decision",
                lambda: task_service.change_set_review_decision(
                    task_change_set,
                    workspace_review_policy=workspace_review_policy,
                    workflow_profile=getattr(ctx, "workflow_profile", None),
                    template_review_policy=_tmpl_review_policy,
                    planner_contract=getattr(ctx, "planner_contract", None),
                ),
            )
        else:
            review_decision = _checkpointed(
                "review_decision",
                lambda: decide_change_set_review(
                    task_change_set,
                    workspace_review_policy=workspace_review_policy,
                    workflow_profile=getattr(ctx, "workflow_profile", None),
                    template_review_policy=_tmpl_review_policy,
                    planner_contract=getattr(ctx, "planner_contract", None),
                ),
            )
        should_hold_for_review = bool(review_decision["held_for_review"])
        change_set_identity = (
            candidate_delta_identity(task_change_set) if task_change_set else None
        )
        if (
            task_change_set
            and completion_validation.accepted
            and not completion_validation.candidate_identity
        ):
            # Compatibility for injected/legacy accepted verdicts. Production
            # candidate validation sets this identity itself.
            completion_validation.candidate_identity = change_set_identity
        candidate_handoff_valid = bool(
            completion_validation.accepted
            and completion_validation.candidate_identity
            and completion_validation.candidate_identity == change_set_identity
        )
        publication_allowed = bool(
            review_decision.get("publication_allowed", True)
            and (not task_change_set or candidate_handoff_valid)
        )
        review_decision = {
            **review_decision,
            "review_required": should_hold_for_review,
            "publication_eligible": bool(
                publication_allowed and not should_hold_for_review
            ),
            "candidate_validation": {
                "status": completion_validation.status,
                "candidate_identity": completion_validation.candidate_identity,
                "change_set_identity": change_set_identity,
                "accepted_identity_match": candidate_handoff_valid,
            },
        }
        evaluator_result = None
        if (
            task_change_set
            and ctx.task_execution_id
            and not should_hold_for_review
            and publication_allowed
            and review_decision.get("outcome") == "auto_promote"
        ):
            evaluator_result = _checkpointed(
                "evaluator_completed",
                lambda: _run_evaluator(
                    runtime_service=runtime_service,
                    orchestration_state=orchestration_state,
                    prompt=prompt,
                    summary=wm_summary,
                    emit_live=emit_live,
                    logger=logger,
                ),
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
        if task_change_set and project and ctx.runtime_workspace_used:
            task_service.retain_workspace_snapshot(
                project,
                source_root=Path(orchestration_state.project_dir),
                snapshot_key=workspace_snapshot_key(task_id, ctx.task_execution_id),
            )
        baseline_publish_result = None
        baseline_publish_validation = None
        publish_captured_change_set = bool(
            project
            and task_change_set
            and runs_in_canonical_baseline
            and ctx.runtime_workspace_used
        )
        if project and (
            (task.task_subfolder and not runs_in_canonical_baseline)
            or publish_captured_change_set
        ):
            if not publication_allowed:
                baseline_publish_result = {
                    "auto_publish_skipped": True,
                    "reason": review_decision.get("reason")
                    or "publication_not_required",
                    "held_for_review": should_hold_for_review,
                    "review_decision": review_decision,
                    "files_copied": 0,
                    "accepted_change_set": task_change_set,
                    "warning_flags": nontrivial_change_flags,
                    "workspace_review_policy": workspace_review_policy,
                    "publication_eligible": False,
                }
                emit_live(
                    "INFO",
                    "[ORCHESTRATION] Task change set retained without automatic publication",
                    metadata={
                        "phase": "baseline_publish",
                        "reason": baseline_publish_result["reason"],
                        "held_for_review": should_hold_for_review,
                        "publication_allowed": False,
                        "publication_required": review_decision.get(
                            "publication_required"
                        ),
                        "policy_source": review_decision.get("policy_source"),
                    },
                )
            elif should_hold_for_review:
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
                        "publication_eligible": False,
                    },
                )
            else:
                # A rejected baseline publish must never be discovered only
                # after the captured Runtime Workspace candidate has been
                # copied into canonical.  The validator's repository-shape
                # semantics remain unchanged; this is an ordering preflight
                # against the untouched baseline.  The established
                # post-promotion validation below is retained for accepted
                # publication compatibility.
                preflight_materialization = (
                    task_service.validate_task_baseline_materialization(project, task)
                )
                preflight_overview = task_service.validate_project_baseline(
                    project, current_task=task
                )
                try:
                    baseline_publish_preflight = _checkpointed(
                        "baseline_publish_preflight",
                        lambda: ValidatorService.validate_baseline_publish(
                            validation_profile=validation_profile,
                            baseline_path=preflight_materialization.get("baseline_path")
                            or "",
                            baseline_file_count=preflight_materialization.get(
                                "baseline_file_count", 0
                            ),
                            missing_task_expected_files=preflight_materialization.get(
                                "missing_expected_files", []
                            ),
                            current_expected_files=preflight_materialization.get(
                                "expected_files", []
                            ),
                            missing_prior_expected_files=preflight_overview.get(
                                "missing_expected_files", []
                            ),
                            prior_expected_files=preflight_overview.get(
                                "prior_expected_files", []
                            ),
                            consistency_issues=preflight_materialization.get(
                                "consistency_issues", []
                            ),
                            consistency_details=preflight_materialization.get(
                                "consistency"
                            ),
                            relaxed_mode=orchestration_state.relaxed_mode,
                            validation_severity=ctx.validation_severity,
                            candidate_change_set=(
                                task_change_set if publish_captured_change_set else None
                            ),
                        ),
                    )
                except Exception as preflight_error:
                    baseline_publish_preflight = ValidationVerdict(
                        stage="baseline_publish",
                        status="rejected",
                        profile=validation_profile,
                        reasons=[
                            "Baseline publish preflight raised: "
                            + bounded_exception_message(preflight_error)
                        ],
                        details={"preflight_exception": type(preflight_error).__name__},
                    )
                record_validation_verdict(
                    db,
                    session_id,
                    task_id,
                    orchestration_state,
                    baseline_publish_preflight,
                )
                db.commit()
                if not baseline_publish_preflight.accepted:
                    baseline_error = "Baseline publish validation failed: " + "; ".join(
                        baseline_publish_preflight.reasons[:5]
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
                        "[ORCHESTRATION] Baseline publish preflight failed validation",
                        metadata={
                            "phase": "baseline_publish",
                            "validation_status": baseline_publish_preflight.status,
                            "reasons": baseline_publish_preflight.reasons[:10],
                            "preflight": True,
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
                if publish_captured_change_set:
                    baseline_publish_result = _checkpointed(
                        "baseline_promotion",
                        lambda: task_service.promote_change_set_into_baseline(
                            project,
                            task,
                            task_change_set,
                            lock_already_held=runs_in_canonical_baseline,
                        ),
                    )
                    baseline_publish_result["materialization_mode"] = (
                        "captured_change_set"
                    )
                else:
                    baseline_publish_result = _checkpointed(
                        "baseline_promotion",
                        lambda: task_service.auto_publish_task_into_baseline(
                            project, task
                        ),
                    )
                baseline_publish_result["workspace_review_policy"] = (
                    workspace_review_policy
                )
                baseline_publish_result["held_for_review"] = False
                baseline_publish_result["review_decision"] = review_decision
                baseline_publish_result["publication_eligible"] = True
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
                baseline_publish_validation = _checkpointed(
                    "baseline_publish_validation",
                    lambda: ValidatorService.validate_baseline_publish(
                        validation_profile=validation_profile,
                        baseline_path=baseline_materialization.get("baseline_path")
                        or "",
                        baseline_file_count=baseline_materialization.get(
                            "baseline_file_count", 0
                        ),
                        missing_task_expected_files=baseline_materialization.get(
                            "missing_expected_files", []
                        ),
                        current_expected_files=baseline_materialization.get(
                            "expected_files", []
                        ),
                        missing_prior_expected_files=baseline_overview.get(
                            "missing_expected_files", []
                        ),
                        prior_expected_files=baseline_overview.get(
                            "prior_expected_files", []
                        ),
                        consistency_issues=baseline_materialization.get(
                            "consistency_issues", []
                        ),
                        consistency_details=baseline_materialization.get("consistency"),
                        relaxed_mode=orchestration_state.relaxed_mode,
                        validation_severity=ctx.validation_severity,
                    ),
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
            and publication_allowed
            and review_decision.get("outcome") == "auto_promote"
            and (
                (
                    baseline_publish_result
                    and not baseline_publish_result.get("auto_publish_skipped")
                )
                or (runs_in_canonical_baseline and not ctx.runtime_workspace_used)
            )
            and hasattr(task_service, "mark_task_execution_change_set_disposition")
        ):
            disposition_record = _checkpointed(
                "change_set_disposition",
                lambda: task_service.mark_task_execution_change_set_disposition(
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
                ),
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

        finalization = _checkpointed(
            "task_finalization",
            lambda: TaskCompletionFinalizer(
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
            ),
        )
        from app.services.orchestration.working_memory import write_working_memory

        _checkpointed(
            "working_memory_write",
            lambda: write_working_memory(
                orchestration_state=orchestration_state,
                task=task,
                summary=wm_summary,
                logger=logger,
                db=db,
                guidance_backend=ctx.guidance_backend,
                guidance_model_family=ctx.guidance_model_family,
            ),
        )

        from app.services.human_guidance.post_write_checker import (
            run_post_write_check_if_enabled,
        )

        _checkpointed(
            "post_write_checker",
            lambda: run_post_write_check_if_enabled(
                ctx, reported_changed_files=reported_changed_files
            ),
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
            _checkpoint("task_report_generation", "entry")
            _report_completed = False
            try:
                report_payload = build_task_report_payload_fn(db, task_id)
                report_result = render_task_report_fn(
                    report_payload, output_format="markdown"
                )
                if report_result and "report" in report_result:
                    report_content = report_result["report"]
                    # The virtual merge gate resolves reports against the
                    # durable project root; under RUNTIME_WORKSPACE_ENABLED
                    # orchestration_state.project_dir is the disposable Task
                    # Execution Sandbox, so a report written there is lost on
                    # disposal (Phase 24B-7 live finding on tasks 47/48).
                    report_root = orchestration_state.project_dir
                    if project is not None:
                        try:
                            report_root = resolve_project_workspace_path(
                                project.workspace_path, project.name
                            )
                        except Exception:
                            report_root = orchestration_state.project_dir
                    report_path = (
                        report_root / TASK_REPORT_ROOT / f"task_report_{task_id}.md"
                    )
                    os.makedirs(report_path.parent, exist_ok=True)
                    report_path.parent.chmod(0o777)
                    with open(report_path, "w", encoding="utf-8") as handle:
                        handle.write(report_content)
                    report_path.chmod(0o666)
                    logger.info("[REPORT] Task report saved to: %s", report_path)
                _report_completed = True
            except Exception as report_error:
                _checkpoint(
                    "task_report_generation",
                    "failure",
                    {
                        "exception_type": type(report_error).__name__,
                        "exception_message": bounded_exception_message(report_error),
                    },
                )
                logger.error(
                    "[REPORT] Failed to generate task report: %s", str(report_error)
                )
            if _report_completed:
                _checkpoint("task_report_generation", "success")

        return {
            "status": "completed",
            "task_id": task_id,
            "session_id": session_id,
            "steps_completed": len(orchestration_state.plan),
            "debug_attempts": len(orchestration_state.debug_attempts),
            "summary": wm_summary[:500],
        }
