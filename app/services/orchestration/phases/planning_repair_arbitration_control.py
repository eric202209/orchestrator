"""Planning repair arbitration behavior controls."""

from __future__ import annotations

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
from app.services.orchestration.planning.source_api_contract import (
    build_source_api_contract_capsule,
)
from app.services.orchestration.state.persistence import append_orchestration_event
from app.services.orchestration.types import OrchestrationRunContext
from app.services.orchestration.validation.validator import ValidatorService
from app.services.prompt_templates import OrchestrationStatus


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
    arbitration = classify_planning_repair_candidate(
        previous_plan=previous_plan,
        repaired_plan=ctx.orchestration_state.plan,
        project_dir=ctx.orchestration_state.project_dir,
        source_api_capsule=source_api_capsule,
        immediate_repair_issues=immediate_repair_issues,
    )
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
    materialization_regression_paths = _materialization_regression_paths(arbitration)
    if materialization_regression_paths:
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
        is_first_ordered_task=getattr(ctx.task, "plan_position", None) == 1,
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


def _materialization_regression_paths(arbitration: dict[str, Any]) -> list[str]:
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
        if path not in repaired_paths and _is_required_source_materialization_path(path)
    ]


def _is_required_source_materialization_path(path: str) -> bool:
    normalized = str(path or "").strip().replace("\\", "/").lstrip("./")
    if not normalized:
        return False
    if normalized.startswith(("tests/", "test/")):
        return False
    return normalized.startswith("src/") and normalized.endswith(
        (".py", ".js", ".jsx", ".ts", ".tsx", ".css")
    )
