"""Pre-prompt source-grounding integration used by the Planning phase."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Callable, Dict

from app.config import settings
from app.services.orchestration.planning.grounding import (
    GroundingCoordinator,
    GroundingExecutor,
    GroundingHandoffError,
    GroundingRunConfig,
    GroundingTaskReference,
    GroundingTerminalReason,
    GROUNDING_PROVIDER_TIMEOUT_SECONDS,
    PlanningGroundingProviderAdapter,
    build_grounding_planning_context,
)
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.read_only_discovery import (
    DiscoveryContractError,
    fail_closed_discovery,
    prepare_discovery_context,
)
from app.services.orchestration.planning.source_materialization import (
    SELECTION_HEAD_FALLBACK,
    SOURCE_STATUS_EXISTING,
    SOURCE_STATUS_NEW,
)
from app.services.orchestration.planning.repository_orientation import (
    derive_repository_orientation,
)
from app.task_intent import TaskIntentMode, normalize_task_intent


class _MechanicalSkipProvider:
    def decide(self, _context: Any) -> Any:
        raise AssertionError("mechanical skip must not invoke a provider")


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
    max_exploration_provider_requests = getattr(
        ctx, "grounding_max_provider_requests", None
    )
    if max_exploration_provider_requests is None:
        max_exploration_provider_requests = (
            settings.TYPED_GROUNDING_MAX_PROVIDER_REQUESTS
        )
    mechanical_skip = bool(getattr(ctx, "grounding_mechanical_skip", False))
    if max_steps is None or max_exploration_provider_requests is None:
        raise ValueError("typed grounding requires explicit run limits")

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

    if provider is None and not mechanical_skip:
        from app.services.planning.providers import create_planning_provider

        planning_provider = create_planning_provider(ctx.db)
        configured_timeout = int(
            getattr(ctx, "timeout_seconds", GROUNDING_PROVIDER_TIMEOUT_SECONDS)
            or GROUNDING_PROVIDER_TIMEOUT_SECONDS
        )
        provider = PlanningGroundingProviderAdapter(
            planning_provider,
            timeout_seconds=min(
                max(1, configured_timeout), GROUNDING_PROVIDER_TIMEOUT_SECONDS
            ),
            event_sink=event_sink,
        )
    if provider is None and not mechanical_skip:
        raise ValueError("typed grounding provider could not be selected")

    provider_name = str(
        getattr(provider, "provider_name", "injected_grounding_provider")
    )
    model_name = str(getattr(provider, "model_name", "unbound"))

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
        max_exploration_provider_requests=int(max_exploration_provider_requests),
        operator_task=str(ctx.prompt or ""),
        provider_name=provider_name,
        model_name=model_name,
        orientation_advisory=(
            # The provider projection, not the event projection: the counters
            # alone describe candidate paths the provider never gets to see.
            derive_repository_orientation(
                project_dir, str(ctx.prompt or "")
            ).as_provider_advisory()
            if not mechanical_skip
            else {}
        ),
        mechanical_skip=mechanical_skip,
    )
    coordinator = GroundingCoordinator(
        executor=GroundingExecutor(
            project_dir,
            snapshot_identity=snapshot_identity,
        ),
        provider=provider or _MechanicalSkipProvider(),
        config=config,
        event_sink=event_sink,
    )
    return coordinator.run()


def _mechanical_grounding_skip(materialization: Any, intent_mode: str) -> bool:
    """Return only the narrow Slice 2 skip cases; no target-hint semantics."""

    expected = tuple(
        item
        for item in getattr(materialization, "files", ())
        if bool(getattr(item, "expected", False))
    )
    if not expected:
        return False
    if normalize_task_intent(intent_mode) == TaskIntentMode.CREATE_ONLY.value:
        return all(
            getattr(item, "status", None) == SOURCE_STATUS_NEW
            and bool(getattr(item, "creation_authorized", False))
            for item in expected
        )
    return all(
        getattr(item, "status", None) == SOURCE_STATUS_EXISTING
        and bool(getattr(item, "version_identity", None))
        and bool(getattr(item, "content_hash", None))
        and getattr(item, "content", None) is not None
        and not bool(getattr(item, "truncated", False))
        and getattr(item, "selection_strategy", None) != SELECTION_HEAD_FALLBACK
        for item in expected
    )


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
            initial_materialization = materialize(
                project_dir=Path(ctx.orchestration_state.project_dir),
                task_description=ctx.prompt,
                planner_contract=ctx.planner_contract,
                supporting_paths=(),
            )
            ctx.grounding_mechanical_skip = _mechanical_grounding_skip(
                initial_materialization,
                getattr(ctx, "intent_mode", TaskIntentMode.DEFAULT.value),
            )
        except (OSError, ValueError) as exc:
            return fail_typed_grounding(
                ctx=ctx,
                reason=GroundingTerminalReason.EXECUTOR_FAILURE.value,
                detail=str(exc),
                emit_phase_event=emit_phase_event,
            )
        try:
            grounding_result = run_typed_grounding(ctx)
        except (TypeError, ValueError, OSError) as exc:
            return fail_typed_grounding(
                ctx=ctx,
                reason=GroundingTerminalReason.INVALID_MODEL_REQUEST.value,
                detail=str(exc),
                emit_phase_event=emit_phase_event,
            )
        ctx.grounding_result = grounding_result
        if grounding_result.terminal_reason is GroundingTerminalReason.SKIPPED:
            ctx.planner_source_materialization = initial_materialization
            return None
        if grounding_result.terminal_state.value == "SUFFICIENT":
            try:
                grounding_context = build_grounding_planning_context(
                    grounding_result,
                    project_dir=Path(ctx.orchestration_state.project_dir),
                    operator_task=str(ctx.prompt or ""),
                    planner_contract=ctx.planner_contract,
                    source_cache={},
                )
            except (GroundingHandoffError, OSError, ValueError) as exc:
                return fail_typed_grounding(
                    ctx=ctx,
                    reason="planning_grounding_handoff_failed",
                    detail=f"typed grounding handoff failed closed: {exc}",
                    emit_phase_event=emit_phase_event,
                )
            ctx.grounding_planning_context = grounding_context
            ctx.planning_grounding_context = grounding_context.rendered_prompt_sections
            ctx.planner_source_materialization = (
                grounding_context.source_materialization
            )
            emit_phase_event(
                ctx.orchestration_state,
                ctx.emit_live,
                level="INFO",
                phase="planning",
                message="[ORCHESTRATION] Typed grounding handed off to Planning",
                details={
                    "stage": "typed_grounding_handoff",
                    "grounding_run_id": grounding_result.grounding_run_id,
                    "terminal_state": grounding_result.terminal_state.value,
                    "cited_observation_ids": list(
                        grounding_result.cited_observation_ids
                    ),
                    "source_versions": dict(grounding_result.source_versions),
                    "grounding_evidence_bytes": grounding_result.budget_snapshot.source_evidence_bytes,
                    "handoff": "legacy_planning_context",
                },
            )
            return None
        failure_reason = (
            "planning_grounding_insufficient"
            if grounding_result.terminal_state.value == "INSUFFICIENT"
            else "planning_grounding_failed"
        )
        return fail_typed_grounding(
            ctx=ctx,
            reason=failure_reason,
            detail=(
                "typed grounding terminated before Planning: "
                + grounding_result.terminal_reason.value
            ),
            emit_phase_event=emit_phase_event,
        )

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
