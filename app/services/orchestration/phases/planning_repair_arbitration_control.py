"""Planning repair arbitration behavior controls."""

from __future__ import annotations

import copy
import os
from pathlib import Path
from typing import Any, Callable

from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.events.telemetry import emit_phase_event
from app.services.orchestration.phases.planning_knowledge import (
    _log_knowledge_usage,
    _retrieve_knowledge,
)
from app.services.orchestration.phases.planning_support import (
    _PlanningRetryState,
    _emit_planning_diagnostics_contract_violation,
    _finalize_planning_terminal_failure,
    _get_targeted_second_repair_reason,
    _plan_contract_diagnostics,
    _record_planning_root_cause,
    _record_repair_root_cause,
    _repair_root_cause_from_arbitration,
    _task1_bootstrap_second_repair_rejection_reasons,
    _terminal_validation_failure_details,
    _terminal_planning_root_cause,
)
from app.services.orchestration.phases.planning_task1_bootstrap import (
    is_first_ordered_task as _is_first_ordered_task,
)
from app.services.orchestration.planning.repair_arbitration import (
    classify_planning_repair_candidate,
)
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.phases.planning_support import (
    BLOCKING_IMMEDIATE_REPAIR_ISSUE_KEYS,
)
from app.services.orchestration.planning.repair_evidence import (
    write_failed_planning_repair_triplet,
)
from app.services.orchestration.planning.slot_repair import (
    SlotRepairError,
    SlotRepairTaskContext,
    compile_slots_to_typed_plan,
    extract_plan_slots,
    merge_repair_slots,
)
from app.services.orchestration.planning.source_api_contract import (
    build_source_api_contract_capsule,
)
from app.services.orchestration.planning.source_materialization import (
    is_concrete_source_materialization_path,
)
from app.services.orchestration.state.persistence import append_orchestration_event
from app.services.orchestration.types import OrchestrationRunContext
from app.services.orchestration.validation.validator import ValidatorService
from app.services.orchestration.prompt_templates import OrchestrationStatus


_SLOT_REPAIR_EXPERIMENT_ENV = "SLOT_BASED_PLANNING_REPAIR_EXPERIMENT"
_SLOT_REPAIR_VERIFY = "python3 -m pytest -q"


def arbitrate_planning_repair_candidate(
    *,
    ctx: OrchestrationRunContext,
    retry_state: _PlanningRetryState,
    previous_plan: Any,
    immediate_repair_issues: dict[str, list[int]],
    planning_phase_event: dict[str, Any] | None,
    output_text: str,
    planning_timeout_seconds: int,
    prompt_profile: str | None,
    repair_planning_output: Callable[..., Any],
) -> dict[str, Any]:
    source_api_capsule = None
    try:
        source_api_capsule = build_source_api_contract_capsule(
            ctx.orchestration_state.project_dir,
        )
    except Exception as exc:
        ctx.logger.debug(
            "[ORCHESTRATION] Failed to build source/API capsule for "
            "planning repair arbitration: %s",
            exc,
        )
    slot_repair_diagnostics = _maybe_apply_slot_repair_experiment(
        ctx=ctx,
        retry_state=retry_state,
        previous_plan=previous_plan,
        output_text=output_text,
    )
    arbitration = classify_planning_repair_candidate(
        previous_plan=previous_plan,
        repaired_plan=ctx.orchestration_state.plan,
        project_dir=ctx.orchestration_state.project_dir,
        source_api_capsule=source_api_capsule,
        immediate_repair_issues=immediate_repair_issues,
    )
    arbitration["slot_repair_experiment"] = {
        **slot_repair_diagnostics,
        "arbitration_result": arbitration.get("outcome") or "classified",
        "arbitration_source_materialization_status": (
            (arbitration.get("source_materialization") or {}).get("status")
            if isinstance(arbitration.get("source_materialization"), dict)
            else None
        ),
    }
    arbitration["repair_reason"] = retry_state.last_repair_reason
    arbitration["repair_attempts"] = retry_state.consecutive_failures
    invalid_python_repair_candidate = "invalid_output" in arbitration.get(
        "regression_labels", []
    ) and (arbitration.get("python_syntax") or {}).get("status") in {
        "regressed",
        "still_invalid",
    }
    arbitration["invalid_output"] = invalid_python_repair_candidate
    arbitration["arbitration_action"] = "none"
    _record_repair_root_cause(
        retry_state,
        root_cause=_repair_root_cause_from_arbitration(arbitration),
        stage="planning_repair_arbitration",
    )
    # Attempt weak-verification preservation before accepting a regressed repair.
    # Task-1 bootstrap plans use obligation loss as the damage signal. Later
    # implementation tasks use placeholder-only repaired steps as the signal.
    preserved_weak_verification_plan = _preserve_regressed_weak_verification_plan(
        ctx=ctx,
        previous_plan=previous_plan,
        arbitration=arbitration,
    )
    if preserved_weak_verification_plan is not None:
        ctx.orchestration_state.plan = preserved_weak_verification_plan
        arbitration["reason"] = "regressed_weak_verification_repair_preserved_original"
        arbitration["arbitration_action"] = (
            "preserve_original_replace_weak_verification"
        )
        _emit_planning_repair_arbitration(
            ctx,
            arbitration=arbitration,
            planning_phase_event=planning_phase_event,
        )
        return {
            "action": "replace",
            "plan": preserved_weak_verification_plan,
        }

    materialization_regression_paths = _materialization_regression_paths(
        arbitration,
        ctx.orchestration_state.project_dir,
    )
    # C-1: VMA repairs are expected to remove source-write ops — the repair prompt
    # explicitly instructs the model to do exactly that.  Triggering the terminal
    # abort here would punish a correct repair.  Skip for VMA-triggered repairs;
    # implementation-profile behaviour is unchanged.
    if materialization_regression_paths and not retry_state.vma_repair_triggered:
        root_cause = _record_planning_root_cause(
            retry_state,
            "missing_source_materialization",
        )
        arbitration["reason"] = "planning_repair_materialization_regression"
        arbitration["arbitration_action"] = "reject_materialization_regression"
        arbitration["planning_root_cause"] = root_cause
        arbitration["materialization_regression_paths"] = (
            materialization_regression_paths[:20]
        )
        _attach_failed_repair_triplet_evidence(
            ctx=ctx,
            arbitration=arbitration,
            previous_plan=previous_plan,
            output_text=output_text,
        )
        _emit_planning_repair_arbitration(
            ctx,
            arbitration=arbitration,
            planning_phase_event=planning_phase_event,
        )
        ctx.orchestration_state.status = OrchestrationStatus.ABORTED
        ctx.orchestration_state.abort_reason = (
            "Planning repair moved or removed required source materialization"
        )
        failure_reason = (
            "Planning repair moved or removed required source materialization: "
            + ", ".join(materialization_regression_paths[:4])
        )
        emit_phase_event(
            ctx.orchestration_state,
            ctx.emit_live,
            level="ERROR",
            phase="planning",
            message=(
                "[ORCHESTRATION] Planning repair moved or removed required "
                "source materialization"
            ),
            details={
                "reason": "planning_repair_materialization_regression",
                "planning_root_cause": root_cause,
                "materialization_regression_paths": (
                    materialization_regression_paths[:20]
                ),
                "planning_repair_arbitration": arbitration,
            },
        )
        _finalize_planning_terminal_failure(
            ctx=ctx,
            failure_type="planning_repair_materialization_regression",
            failure_reason=failure_reason,
            planning_root_cause=root_cause,
        )
        if ctx.restore_workspace_snapshot_if_needed:
            ctx.restore_workspace_snapshot_if_needed(
                "planning repair materialization regression"
            )
        return {
            "action": "return",
            "result": {
                "status": "failed",
                "reason": "planning_repair_materialization_regression",
            },
        }
    if not invalid_python_repair_candidate:
        # Acceptance definition: accepted progress = repair improved the plan
        # AND repair produced a Bootstrap Contract-valid plan.
        # Bootstrap Contract must be satisfied before a candidate is classified
        # as accepted progress — not checked separately afterward.
        if _is_first_ordered_task(ctx.task):
            try:
                bootstrap_verdict = ValidatorService.validate_plan(
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
                    is_first_ordered_task=True,
                )
            except Exception as exc:
                ctx.logger.warning(
                    "[ORCHESTRATION] Bootstrap Contract pre-check in arbitration "
                    "raised an exception; falling through to accept: %s",
                    exc,
                )
                bootstrap_verdict = None
            if bootstrap_verdict is not None:
                bootstrap_contract = (bootstrap_verdict.details or {}).get(
                    "task1_bootstrap_contract"
                )
                if (
                    isinstance(bootstrap_contract, dict)
                    and bootstrap_contract.get("passed") is False
                ):
                    return _reject_repair_candidate_by_bootstrap_contract(
                        ctx=ctx,
                        retry_state=retry_state,
                        arbitration=arbitration,
                        bootstrap_verdict=bootstrap_verdict,
                        planning_phase_event=planning_phase_event,
                        output_text=output_text,
                        planning_timeout_seconds=planning_timeout_seconds,
                        prompt_profile=prompt_profile,
                        repair_planning_output=repair_planning_output,
                    )
        _emit_planning_repair_arbitration(
            ctx,
            arbitration=arbitration,
            planning_phase_event=planning_phase_event,
        )
        return {"action": "none"}

    arbitration["reason"] = "invalid_python_repair_candidate"
    _record_planning_root_cause(retry_state, "invalid_python")
    arbitration["planning_root_cause"] = _terminal_planning_root_cause(retry_state)
    arbitration_plan_verdict = ValidatorService.validate_plan(
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
        plan_verdict=arbitration_plan_verdict,
        project_dir=ctx.orchestration_state.project_dir,
    )
    if second_repair_reason and not second_repair_reason.cap_used:
        arbitration["arbitration_action"] = "syntax_retry"
        _emit_planning_repair_arbitration(
            ctx,
            arbitration=arbitration,
            planning_phase_event=planning_phase_event,
        )
        issue_fragments = [second_repair_reason.rejection_text]
        contract_diagnostics = _plan_contract_diagnostics(
            arbitration_plan_verdict.details
        )
        _emit_planning_diagnostics_contract_violation(
            ctx,
            reason=second_repair_reason.event_reason,
            contract_violations=arbitration_plan_verdict.reasons,
            semantic_violation_codes=[second_repair_reason.semantic_violation_code],
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
                "[ORCHESTRATION] Planning repair arbitration rejected invalid "
                "Python candidate; starting syntax second repair pass"
            ),
            details={
                "reason": "invalid_python_repair_candidate",
                "arbitration_action": "syntax_retry",
                "planning_root_cause": _terminal_planning_root_cause(retry_state),
                "python_syntax": arbitration.get("python_syntax"),
                "validation_reasons": list(arbitration_plan_verdict.reasons or [])[:5],
                "repair_attempts": retry_state.consecutive_failures + 1,
            },
        )
        validation_knowledge_ctx = _retrieve_knowledge(
            ctx,
            trigger_phase="validation",
            knowledge_types=["failure_memory", "format_guide", "debug_case"],
            query="Plan validation failed after repair: "
            + "; ".join(arbitration_plan_verdict.reasons[:3]),
            failure_signature=(
                arbitration_plan_verdict.reasons[0]
                if arbitration_plan_verdict.reasons
                else None
            ),
        )
        if validation_knowledge_ctx:
            _log_knowledge_usage(ctx, validation_knowledge_ctx, used_in_prompt=True)
        retry_state.last_repair_reason = second_repair_reason.event_reason
        planning_result = repair_planning_output(
            ctx=ctx,
            retry_state=retry_state,
            planning_timeout_seconds=planning_timeout_seconds,
            malformed_output=output_text,
            reason=f"{second_repair_reason.retry_reason}: "
            + "; ".join(issue_fragments),
            rejection_reasons=issue_fragments,
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
        setattr(retry_state, second_repair_reason.cap_attribute, True)
        retry_state.consecutive_failures += 1
        return {"action": "continue", "planning_result": planning_result}

    arbitration["arbitration_action"] = "reject_after_retry"
    _attach_failed_repair_triplet_evidence(
        ctx=ctx,
        arbitration=arbitration,
        previous_plan=previous_plan,
        output_text=output_text,
    )
    _emit_planning_repair_arbitration(
        ctx,
        arbitration=arbitration,
        planning_phase_event=planning_phase_event,
    )
    ctx.orchestration_state.status = OrchestrationStatus.ABORTED
    ctx.orchestration_state.abort_reason = (
        "Planning validation failed after repair: "
        + "; ".join(arbitration_plan_verdict.reasons[:3])
    )
    ctx.logger.warning(
        "[ORCHESTRATION] Planning repair arbitration rejected invalid Python "
        "candidate after syntax retry was exhausted"
    )
    failure_details = _terminal_validation_failure_details(arbitration_plan_verdict)
    failure_details["planning_repair_arbitration"] = arbitration
    failure_details["planning_root_cause"] = _terminal_planning_root_cause(retry_state)
    emit_phase_event(
        ctx.orchestration_state,
        ctx.emit_live,
        level="ERROR",
        phase="planning",
        message="[ORCHESTRATION] Plan validation failed after repair",
        details=failure_details,
    )
    failure_reason = "Plan validation failed after repair: " + "; ".join(
        arbitration_plan_verdict.reasons[:4]
    )
    _finalize_planning_terminal_failure(
        ctx=ctx,
        failure_type="planning_validation_failed_after_repair",
        failure_reason=failure_reason,
        planning_root_cause=_terminal_planning_root_cause(retry_state),
    )
    if ctx.restore_workspace_snapshot_if_needed:
        ctx.restore_workspace_snapshot_if_needed("planning validation failure")
    return {
        "action": "return",
        "result": {
            "status": "failed",
            "reason": "planning_validation_failed_after_repair",
        },
    }


def _preserve_regressed_weak_verification_plan(
    *,
    ctx: OrchestrationRunContext,
    previous_plan: Any,
    arbitration: dict[str, Any],
) -> list[dict[str, Any]] | None:
    if not isinstance(previous_plan, list):
        return None
    bootstrap_regression = _is_first_ordered_task(
        ctx.task
    ) and _repair_drops_bootstrap_obligations(
        previous_plan, ctx.orchestration_state.plan
    )
    placeholder_steps = (arbitration.get("immediate_repair_issues") or {}).get(
        "placeholder_only_steps"
    ) or []
    non_bootstrap_placeholder_regression = not _is_first_ordered_task(
        ctx.task
    ) and bool(placeholder_steps)
    if not bootstrap_regression and not non_bootstrap_placeholder_regression:
        return None

    original_issues = PlannerService.find_immediate_repair_step_issues(
        previous_plan,
        project_dir=ctx.orchestration_state.project_dir,
    )
    blocking_original_issues = {
        key: value
        for key, value in original_issues.items()
        if key in BLOCKING_IMMEDIATE_REPAIR_ISSUE_KEYS and value
    }
    weak_steps = list(blocking_original_issues.get("weak_verification_steps") or [])
    if not weak_steps or set(blocking_original_issues) != {"weak_verification_steps"}:
        return None

    candidate_plan = ctx.orchestration_state.plan
    if not isinstance(candidate_plan, list):
        return None
    candidate_steps = [
        step
        for step in candidate_plan
        if isinstance(step, dict) and str(step.get("verification") or "").strip()
    ]
    candidate_steps.sort(
        key=lambda step: (
            not bool(step.get("expected_files")),
            not bool(step.get("ops")),
        )
    )
    candidate_verifications = [
        str(step.get("verification") or "").strip() for step in candidate_steps
    ]
    if not candidate_verifications:
        return None

    preserved_plan = copy.deepcopy(previous_plan)
    for weak_step_number in weak_steps:
        original_step = next(
            (
                step
                for step in preserved_plan
                if isinstance(step, dict)
                and step.get("step_number") == weak_step_number
            ),
            None,
        )
        if original_step is None:
            return None
        replacement_found = False
        matching_candidate = next(
            (
                step
                for step in candidate_steps
                if step.get("step_number") == weak_step_number
            ),
            None,
        )
        matching_verification = str(
            (matching_candidate or {}).get("verification") or ""
        ).strip()
        matching_verification_is_grounded = bool(
            matching_candidate
            and (
                matching_candidate.get("expected_files")
                or matching_candidate.get("ops")
            )
        )
        ordered_verifications = list(
            dict.fromkeys(
                (
                    [matching_verification]
                    if matching_verification and matching_verification_is_grounded
                    else []
                )
                + candidate_verifications
            )
        )
        for verification in ordered_verifications:
            trial_plan = copy.deepcopy(preserved_plan)
            trial_step = next(
                (
                    step
                    for step in trial_plan
                    if isinstance(step, dict)
                    and step.get("step_number") == weak_step_number
                ),
                None,
            )
            if trial_step is None:
                continue
            trial_step["verification"] = verification
            trial_issues = PlannerService.find_immediate_repair_step_issues(
                trial_plan,
                project_dir=ctx.orchestration_state.project_dir,
            )
            if weak_step_number not in (
                trial_issues.get("weak_verification_steps") or []
            ):
                preserved_plan = trial_plan
                replacement_found = True
                break
        if not replacement_found:
            return None
    return preserved_plan


def _repair_drops_bootstrap_obligations(
    previous_plan: Any,
    candidate_plan: Any,
) -> bool:
    if not isinstance(previous_plan, list) or not isinstance(candidate_plan, list):
        return False

    def _expected_files(plan: list[Any]) -> set[str]:
        return {
            str(path).strip().lstrip("./")
            for step in plan
            if isinstance(step, dict)
            for path in (step.get("expected_files") or [])
            if str(path).strip()
        }

    def _lifecycle_obligations(plan: list[Any]) -> set[str]:
        commands = "\n".join(
            str(command or "").lower()
            for step in plan
            if isinstance(step, dict)
            for command in (step.get("commands") or [])
        )
        obligations: set[str] = set()
        if " -m venv " in f" {commands} ":
            obligations.add("venv")
        if "pip install" in commands:
            obligations.add("install")
        if "pytest" in commands:
            obligations.add("pytest")
        return obligations

    return bool(
        _expected_files(previous_plan) - _expected_files(candidate_plan)
        or _lifecycle_obligations(previous_plan)
        - _lifecycle_obligations(candidate_plan)
    )


def _reject_repair_candidate_by_bootstrap_contract(
    *,
    ctx: OrchestrationRunContext,
    retry_state: _PlanningRetryState,
    arbitration: dict[str, Any],
    bootstrap_verdict: Any,
    planning_phase_event: dict[str, Any] | None,
    output_text: str,
    planning_timeout_seconds: int,
    prompt_profile: str | None,
    repair_planning_output: Callable[..., Any],
) -> dict[str, Any]:
    """Arbitration rejection path: repair candidate fails Bootstrap Contract.

    Emits the repair_candidate_rejected_by_bootstrap_contract diagnostic, then
    either triggers a targeted Bootstrap Contract repair pass (if budget remains)
    or terminates planning with a specific failure reason.
    """
    bootstrap_contract = (bootstrap_verdict.details or {}).get(
        "task1_bootstrap_contract"
    ) or {}
    failed_requirements = bootstrap_contract.get("violation_codes") or []
    bootstrap_task_type = bootstrap_contract.get("bootstrap_task_type")
    expected_test_reason = bootstrap_contract.get("expected_test_reason")

    emit_phase_event(
        ctx.orchestration_state,
        ctx.emit_live,
        level="WARN",
        phase="planning",
        message=(
            "[ORCHESTRATION] Repair candidate rejected by Bootstrap Contract; "
            "not classified as accepted progress"
        ),
        details={
            "event": "repair_candidate_rejected_by_bootstrap_contract",
            "bootstrap_contract_passed": bootstrap_contract.get("passed"),
            "bootstrap_task_type": bootstrap_task_type,
            "classification_evidence": bootstrap_contract.get("classification_evidence")
            or {},
            "violations": list(bootstrap_contract.get("violations") or [])[:8],
            "failed_requirements": failed_requirements,
            "expected_test_reason": expected_test_reason,
            "required_artifacts": list(
                bootstrap_contract.get("required_artifacts") or []
            )[:20],
            "required_source_files": list(
                bootstrap_contract.get("required_source_files") or []
            )[:20],
            "required_test_files": list(
                bootstrap_contract.get("required_test_files") or []
            )[:20],
            "required_verification": list(
                bootstrap_contract.get("required_verification") or []
            )[:8],
        },
    )

    second_repair_reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        plan_verdict=bootstrap_verdict,
        project_dir=ctx.orchestration_state.project_dir,
    )
    if second_repair_reason and not second_repair_reason.cap_used:
        issue_fragments = _task1_bootstrap_second_repair_rejection_reasons(
            retry_state=retry_state,
            plan_verdict=bootstrap_verdict,
            rejection_text=second_repair_reason.rejection_text,
        )
        arbitration["arbitration_action"] = "bootstrap_contract_repair"
        arbitration["reason"] = "repair_candidate_rejected_by_bootstrap_contract"
        try:
            append_orchestration_event(
                project_dir=ctx.orchestration_state.project_dir,
                session_id=ctx.session_id,
                task_id=ctx.task_id,
                event_type=EventType.PLANNING_REPAIR_ARBITRATION,
                parent_event_id=(planning_phase_event or {}).get("event_id"),
                details=arbitration,
            )
        except Exception as exc:
            ctx.logger.warning(
                "[ORCHESTRATION] Failed to persist Bootstrap Contract "
                "rejection arbitration event: %s",
                exc,
            )
        emit_phase_event(
            ctx.orchestration_state,
            ctx.emit_live,
            level="WARN",
            phase="planning",
            message=(
                "[ORCHESTRATION] Planning repair arbitration starting targeted "
                "Bootstrap Contract repair pass"
            ),
            details={
                "reason": second_repair_reason.event_reason,
                "bootstrap_task_type": bootstrap_task_type,
                "failed_requirements": failed_requirements,
                "expected_test_reason": expected_test_reason,
                "repair_attempts": retry_state.consecutive_failures + 1,
            },
        )
        validation_knowledge_ctx = _retrieve_knowledge(
            ctx,
            trigger_phase="validation",
            knowledge_types=["failure_memory", "format_guide", "debug_case"],
            query="Task 1 Bootstrap Contract failed after repair: "
            + "; ".join(str(f) for f in failed_requirements[:3]),
            failure_signature=(
                second_repair_reason.semantic_violation_code
                or "task1_bootstrap_contract"
            ),
        )
        if validation_knowledge_ctx:
            _log_knowledge_usage(ctx, validation_knowledge_ctx, used_in_prompt=True)
        retry_state.last_repair_reason = second_repair_reason.event_reason
        planning_result = repair_planning_output(
            ctx=ctx,
            retry_state=retry_state,
            planning_timeout_seconds=planning_timeout_seconds,
            malformed_output=output_text,
            reason=f"{second_repair_reason.retry_reason}: "
            + "; ".join(str(f) for f in issue_fragments[:4]),
            rejection_reasons=issue_fragments,
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
        setattr(retry_state, second_repair_reason.cap_attribute, True)
        retry_state.consecutive_failures += 1
        return {"action": "continue", "planning_result": planning_result}

    # No repair budget for Bootstrap Contract — terminate with specific reason.
    arbitration["arbitration_action"] = "reject_bootstrap_contract_no_budget"
    arbitration["reason"] = "repair_candidate_rejected_by_bootstrap_contract"
    _attach_failed_repair_triplet_evidence(
        ctx=ctx,
        arbitration=arbitration,
        previous_plan=[],
        output_text=output_text,
    )
    try:
        append_orchestration_event(
            project_dir=ctx.orchestration_state.project_dir,
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            event_type=EventType.PLANNING_REPAIR_ARBITRATION,
            parent_event_id=(planning_phase_event or {}).get("event_id"),
            details=arbitration,
        )
    except Exception as exc:
        ctx.logger.warning(
            "[ORCHESTRATION] Failed to persist Bootstrap Contract "
            "no-budget rejection event: %s",
            exc,
        )
    ctx.orchestration_state.status = OrchestrationStatus.ABORTED
    ctx.orchestration_state.abort_reason = (
        "Repair candidate rejected by Bootstrap Contract: "
        + "; ".join(str(f) for f in failed_requirements[:3])
    )
    failure_reason = (
        "Planning repair produced a Bootstrap Contract-invalid candidate: "
        + "; ".join(str(f) for f in failed_requirements[:4])
    )
    _finalize_planning_terminal_failure(
        ctx=ctx,
        failure_type="repair_candidate_rejected_by_bootstrap_contract",
        failure_reason=failure_reason,
        planning_root_cause=_terminal_planning_root_cause(retry_state),
    )
    if ctx.restore_workspace_snapshot_if_needed:
        ctx.restore_workspace_snapshot_if_needed(
            "repair candidate rejected by Bootstrap Contract"
        )
    return {
        "action": "return",
        "result": {
            "status": "failed",
            "reason": "repair_candidate_rejected_by_bootstrap_contract",
        },
    }


def _attach_failed_repair_triplet_evidence(
    *,
    ctx: OrchestrationRunContext,
    arbitration: dict[str, Any],
    previous_plan: Any,
    output_text: str,
) -> None:
    try:
        artifact_ref = write_failed_planning_repair_triplet(
            project_dir=ctx.orchestration_state.project_dir,
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            repair_attempt=int(arbitration.get("repair_attempts") or 1),
            previous_plan=previous_plan,
            repaired_plan=ctx.orchestration_state.plan,
            repaired_output_text=output_text,
            arbitration=arbitration,
        )
    except Exception as exc:
        ctx.logger.warning(
            "[ORCHESTRATION] Failed to persist planning repair triplet evidence: %s",
            exc,
        )
        return
    if artifact_ref:
        arbitration["planning_repair_evidence"] = artifact_ref


def _emit_planning_repair_arbitration(
    ctx: OrchestrationRunContext,
    *,
    arbitration: dict[str, Any],
    planning_phase_event: dict[str, Any] | None,
) -> None:
    emit_phase_event(
        ctx.orchestration_state,
        ctx.emit_live,
        level="INFO",
        phase="planning",
        message=(
            "[ORCHESTRATION] Planning repair arbitration classified "
            "candidate progress"
        ),
        details=arbitration,
    )
    try:
        append_orchestration_event(
            project_dir=ctx.orchestration_state.project_dir,
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            event_type=EventType.PLANNING_REPAIR_ARBITRATION,
            parent_event_id=(planning_phase_event or {}).get("event_id"),
            details=arbitration,
        )
    except Exception as exc:
        ctx.logger.warning(
            "[ORCHESTRATION] Failed to persist planning repair "
            "arbitration event: %s",
            exc,
        )


def _maybe_apply_slot_repair_experiment(
    *,
    ctx: OrchestrationRunContext,
    retry_state: _PlanningRetryState,
    previous_plan: Any,
    output_text: str,
) -> dict[str, Any]:
    enabled = _env_flag_enabled(_SLOT_REPAIR_EXPERIMENT_ENV)
    diagnostics: dict[str, Any] = {
        "slot_repair_experiment_enabled": enabled,
        "slot_merge_attempted": False,
        "slot_merge_applied": False,
        "slot_merge_rejected": False,
        "slot_merge_rejected_reason": None,
        "preserved_source_materialization": False,
        "preserved_verification": False,
        "compiled_plan_validator_result": "not_run",
    }
    if not enabled:
        return diagnostics

    diagnostics["slot_merge_attempted"] = True
    eligibility = _slot_repair_experiment_eligibility(ctx)
    diagnostics["eligibility"] = eligibility
    if not eligibility.get("eligible"):
        diagnostics["slot_merge_rejected"] = True
        diagnostics["slot_merge_rejected_reason"] = eligibility.get("reason")
        return diagnostics

    try:
        task_context = _slot_repair_task_context(ctx)
        previous_slots = extract_plan_slots(previous_plan, task_context)
        candidate_slots = extract_plan_slots(ctx.orchestration_state.plan, task_context)
        diagnostics.update(
            {
                "previous_slots_rejected": previous_slots.rejected,
                "previous_slot_rejection_reasons": list(
                    previous_slots.rejection_reasons
                )[:8],
                "candidate_slots_rejected": candidate_slots.rejected,
                "candidate_slot_rejection_reasons": list(
                    candidate_slots.rejection_reasons
                )[:8],
                "previous_source_materialization": previous_slots.target_file,
                "candidate_source_materialization": candidate_slots.target_file,
                "previous_verification": previous_slots.verification_command,
                "candidate_verification": candidate_slots.verification_command,
            }
        )
        merged_slots = merge_repair_slots(
            previous_slots,
            candidate_slots,
            retry_state.last_repair_reason or "",
        )
        compiled_plan = compile_slots_to_typed_plan(merged_slots)
        diagnostics["preserved_source_materialization"] = bool(
            previous_slots.source_op
            and merged_slots.source_op
            and previous_slots.source_op.get("path")
            == merged_slots.source_op.get("path")
        )
        diagnostics["preserved_verification"] = bool(
            previous_slots.verification_command
            and merged_slots.verification_command == previous_slots.verification_command
        )
    except SlotRepairError as exc:
        diagnostics["slot_merge_rejected"] = True
        diagnostics["slot_merge_rejected_reason"] = str(exc)
        return diagnostics
    except Exception as exc:
        diagnostics["slot_merge_rejected"] = True
        diagnostics["slot_merge_rejected_reason"] = f"slot_repair_error: {exc}"
        return diagnostics

    verdict = ValidatorService.validate_plan(
        compiled_plan,
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
    diagnostics["compiled_plan_validator_result"] = (
        "accepted" if verdict.accepted else "rejected"
    )
    diagnostics["compiled_plan_validator_reasons"] = list(verdict.reasons or [])[:8]
    if not verdict.accepted:
        diagnostics["slot_merge_rejected"] = True
        diagnostics["slot_merge_rejected_reason"] = "compiled_plan_validator_rejected"
        return diagnostics

    ctx.orchestration_state.plan = compiled_plan
    diagnostics["slot_merge_applied"] = True
    return diagnostics


def _env_flag_enabled(name: str) -> bool:
    return str(os.environ.get(name) or "").strip().lower() in {"1", "true", "yes", "on"}


def _slot_repair_experiment_eligibility(
    ctx: OrchestrationRunContext,
) -> dict[str, Any]:
    project_dir = Path(str(ctx.orchestration_state.project_dir or ""))
    prompt = str(ctx.prompt or "")
    if _SLOT_REPAIR_VERIFY not in prompt:
        return {"eligible": False, "reason": "prompt_missing_known_verification"}
    required_phrases = (
        "Edit only that source file",
        "Do not create new files",
        "Do not edit tests",
    )
    missing_phrases = [phrase for phrase in required_phrases if phrase not in prompt]
    if missing_phrases:
        return {
            "eligible": False,
            "reason": "prompt_missing_constrained_task_language",
            "missing_phrases": missing_phrases,
        }
    try:
        source_files = sorted(
            path.relative_to(project_dir).as_posix()
            for path in (project_dir / "src").rglob("*.py")
            if path.name != "__init__.py"
        )
        test_files = sorted(
            path.relative_to(project_dir).as_posix()
            for path in (project_dir / "tests").rglob("test_*.py")
        )
    except OSError as exc:
        return {"eligible": False, "reason": f"workspace_shape_scan_failed: {exc}"}
    if len(source_files) != 1:
        return {
            "eligible": False,
            "reason": "source_shape_not_one_existing_file",
            "source_files": source_files[:20],
        }
    if not test_files:
        return {"eligible": False, "reason": "existing_tests_missing"}
    target_file = source_files[0]
    if target_file not in prompt:
        return {"eligible": False, "reason": "prompt_missing_allowed_target"}
    return {
        "eligible": True,
        "reason": "constrained_one_file_source_rewrite",
        "allowed_target_files": [target_file],
        "allowed_test_files": test_files,
        "allowed_verification_commands": [_SLOT_REPAIR_VERIFY],
    }


def _slot_repair_task_context(ctx: OrchestrationRunContext) -> SlotRepairTaskContext:
    eligibility = _slot_repair_experiment_eligibility(ctx)
    target_file = str((eligibility.get("allowed_target_files") or [""])[0])
    test_files = tuple(
        str(path) for path in eligibility.get("allowed_test_files") or ()
    )
    project_dir = Path(str(ctx.orchestration_state.project_dir or ""))
    current_content = (project_dir / target_file).read_text(encoding="utf-8")
    return SlotRepairTaskContext(
        allowed_target_files=(target_file,),
        allowed_verification_commands=(_SLOT_REPAIR_VERIFY,),
        allow_test_changes=False,
        current_file_contents={target_file: current_content},
        bootstrap_required_source_files=(target_file,),
        bootstrap_required_test_files=test_files,
        bootstrap_required_verification=(_SLOT_REPAIR_VERIFY,),
    )


def _materialization_regression_paths(
    arbitration: dict[str, Any],
    project_dir: str | Path | None = None,
) -> list[str]:
    materialization = arbitration.get("source_materialization")
    if not isinstance(materialization, dict):
        return []
    if materialization.get("status") not in {"removed", "moved"}:
        return []
    previous_paths = [
        str(path).strip()
        for path in (materialization.get("previous_paths") or [])
        if str(path).strip()
    ]
    repaired_paths = {
        str(path).strip()
        for path in (materialization.get("repaired_paths") or [])
        if str(path).strip()
    }
    if not previous_paths:
        return []
    return [
        path
        for path in previous_paths
        if path not in repaired_paths
        and _is_required_source_materialization_path(path, project_dir)
    ]


def _is_required_source_materialization_path(
    path: str,
    project_dir: str | Path | None = None,
) -> bool:
    normalized = str(path or "").strip()
    if not normalized:
        return False
    root = Path(str(project_dir or "."))
    return is_concrete_source_materialization_path(normalized, root)
