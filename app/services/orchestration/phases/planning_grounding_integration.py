"""Pre-prompt source-grounding integration used by the Planning phase."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict

from app.config import settings
from app.services.orchestration.planning.grounding import (
    GroundingCoordinator,
    GroundingExecutor,
    GroundingRunConfig,
    GroundingTaskReference,
    GroundingTerminalReason,
    apply_grounding_result_to_planning_context,
)
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.read_only_discovery import (
    DiscoveryContractError,
    fail_closed_discovery,
    prepare_discovery_context,
)
from app.services.orchestration.prompt_templates import OrchestrationStatus
from app.services.orchestration.state.persistence import append_orchestration_event
from app.services.orchestration.types import OrchestrationRunContext


def run_typed_grounding_for_planning(
    ctx: OrchestrationRunContext,
    *,
    append_event: Callable[..., Any] = append_orchestration_event,
):
    """Run only the provider-injected coordinator selected by the PGI3 flag."""

    provider = getattr(ctx, "grounding_decision_provider", None)
    max_steps = getattr(ctx, "grounding_max_steps", None)
    if max_steps is None:
        max_steps = settings.TYPED_GROUNDING_MAX_STEPS
    max_provider_requests = getattr(ctx, "grounding_max_provider_requests", None)
    if max_provider_requests is None:
        max_provider_requests = settings.TYPED_GROUNDING_MAX_PROVIDER_REQUESTS
    if provider is None or max_steps is None or max_provider_requests is None:
        raise ValueError(
            "typed grounding requires an injected decision provider and explicit run limits"
        )

    project_dir = Path(ctx.orchestration_state.project_dir).resolve()
    workspace_identity = str(project_dir)
    snapshot_identity = str(
        getattr(ctx, "grounding_snapshot_identity", None) or workspace_identity
    )
    run_id = (
        f"planning-grounding-{ctx.session_id}-{ctx.task_id}-"
        f"{getattr(ctx, 'task_execution_id', None) or 'current'}"
    )

    def event_sink(event_type: str, details: dict[str, Any]) -> None:
        try:
            append_event(
                project_dir=ctx.control_state_location,
                session_id=ctx.session_id,
                task_id=ctx.task_id,
                event_type=event_type,
                details=details,
                phase="planning",
                coordinator="grounding_coordinator",
            )
        except Exception:
            ctx.logger.debug(
                "[ORCHESTRATION] Grounding event persistence failed: %s", event_type
            )

    config = GroundingRunConfig(
        grounding_run_id=run_id,
        task_reference=GroundingTaskReference(
            task_id=str(ctx.task_id),
            task_execution_id=(
                str(ctx.task_execution_id)
                if ctx.task_execution_id is not None
                else None
            ),
        ),
        workspace_identity=workspace_identity,
        snapshot_identity=snapshot_identity,
        max_steps=int(max_steps),
        max_provider_requests=int(max_provider_requests),
        operator_task=str(ctx.prompt or ""),
        provider_name="injected_grounding_provider",
    )
    coordinator = GroundingCoordinator(
        executor=GroundingExecutor(
            project_dir,
            snapshot_identity=snapshot_identity,
        ),
        provider=provider,
        config=config,
        event_sink=event_sink,
    )
    return coordinator.run()


def fail_closed_typed_grounding(
    *,
    ctx: OrchestrationRunContext,
    reason: str,
    detail: str,
    emit_phase_event: Callable[..., Any],
    finalize_failure: Callable[..., Any],
) -> Dict[str, Any]:
    ctx.orchestration_state.status = OrchestrationStatus.ABORTED
    ctx.orchestration_state.abort_reason = detail
    emit_phase_event(
        ctx.orchestration_state,
        ctx.emit_live,
        level="ERROR",
        phase="planning",
        message="[ORCHESTRATION] Typed grounding failed closed",
        details={"stage": "typed_grounding", "reason": reason, "detail": detail},
    )
    finalize_failure(
        ctx=ctx,
        failure_type=reason,
        failure_reason=detail,
    )
    return {"status": "failed", "reason": reason}


def prepare_planning_source_context(
    *,
    ctx: OrchestrationRunContext,
    planning_timeout_seconds: int,
    extract_structured_text: Callable[[Any], str],
    planner_service: type[PlannerService],
    emit_phase_event: Callable[..., Any],
    materialize: Callable[..., Any],
    finalize_failure: Callable[..., Any],
    run_typed_grounding: Callable[[OrchestrationRunContext], Any],
    fail_typed_grounding: Callable[..., Dict[str, Any]],
    prepare_discovery: Callable[..., Any] = prepare_discovery_context,
) -> Dict[str, Any] | None:
    """Prepare either typed-grounding or legacy discovery source context."""

    if settings.ENABLE_TYPED_GROUNDING_COORDINATOR:
        try:
            grounding_result = run_typed_grounding(ctx)
        except (TypeError, ValueError, OSError) as exc:
            return fail_typed_grounding(
                ctx=ctx,
                reason=GroundingTerminalReason.INVALID_MODEL_REQUEST.value,
                detail=str(exc),
                emit_phase_event=emit_phase_event,
            )
        if grounding_result.terminal_reason is not GroundingTerminalReason.SUFFICIENT:
            return fail_typed_grounding(
                ctx=ctx,
                reason=grounding_result.terminal_reason.value,
                detail=(
                    "typed grounding terminated before Planning: "
                    + grounding_result.terminal_reason.value
                ),
                emit_phase_event=emit_phase_event,
            )
        apply_grounding_result_to_planning_context(ctx, grounding_result)
        try:
            from app.services.orchestration.planning.workspace_identity import (
                planner_workspace_identity_for_context,
            )

            ctx.planner_source_materialization = materialize(
                project_dir=Path(ctx.orchestration_state.project_dir),
                task_description=ctx.prompt,
                planner_contract=ctx.planner_contract,
                supporting_paths=grounding_result.cited_source_paths,
                workspace_identity=planner_workspace_identity_for_context(ctx),
            )
        except (OSError, ValueError) as exc:
            return fail_typed_grounding(
                ctx=ctx,
                reason=GroundingTerminalReason.EXECUTOR_FAILURE.value,
                detail=str(exc),
                emit_phase_event=emit_phase_event,
            )
        return None

    try:
        prepare_discovery(
            ctx=ctx,
            planning_timeout_seconds=planning_timeout_seconds,
            extract_structured_text=extract_structured_text,
            planner_service=planner_service,
            emit_phase_event=emit_phase_event,
            materialize=materialize,
            intent_mode=getattr(ctx, "intent_mode", "default"),
        )
    except (DiscoveryContractError, TimeoutError, OSError) as exc:
        return fail_closed_discovery(
            ctx=ctx,
            reason="read_only_discovery_failed_closed",
            detail=str(exc),
            aborted_status=OrchestrationStatus.ABORTED,
            emit_phase_event=emit_phase_event,
            finalize_failure=finalize_failure,
        )
    if not ctx.planner_source_materialization.available:
        return fail_closed_discovery(
            ctx=ctx,
            reason="planning_source_materialization_unavailable",
            detail=", ".join(
                ctx.planner_source_materialization.unavailable_reasons[:8]
            ),
            aborted_status=OrchestrationStatus.ABORTED,
            emit_phase_event=emit_phase_event,
            finalize_failure=finalize_failure,
        )
    return None
