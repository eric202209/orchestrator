"""PlanningCoordinator — owns the planning lifecycle orchestration.

Phase 14B-3: Extracts the planning entry point from worker.py into a single,
owned orchestration surface.

Orchestration decisions live here. Planning algorithms are delegated to
execute_planning_phase and its helpers, which remain in planning_flow.py.
"""

from __future__ import annotations

from typing import Any, Callable, Dict, Optional


class PlanningCoordinator:
    """Single orchestration boundary for the planning phase.

    Owns: runtime-service swap for the planning lane, delegation to
    execute_planning_phase, and result passthrough.
    Delegates: all planning algorithms, repair logic, validation, arbitration,
    and bootstrap behavior to planning_flow.execute_planning_phase.
    """

    def run_planning(
        self,
        *,
        ctx: Any,
        workspace_review: Dict[str, Any],
        extract_structured_text: Callable[[Any], str],
        extract_plan_steps: Callable[[Any], Any],
        looks_like_truncated_multistep_plan: Callable[[str, Any], bool],
        normalize_plan_with_live_logging: Callable[..., Any],
        workspace_violation_error_cls: type,
        planning_runtime_service: Optional[Any] = None,
    ) -> Dict[str, Any]:
        """Execute the planning lifecycle and return a result dict.

        Owns: runtime-service swap for the planning lane.
        Delegates: plan generation, repair, validation, arbitration to
        planning_flow.execute_planning_phase.

        Deferred import of execute_planning_phase preserves test monkeypatching
        on planning_flow.* attributes.
        """
        from app.services.orchestration.phases.planning_flow import (
            execute_planning_phase,
        )

        original_runtime_service = ctx.runtime_service
        if planning_runtime_service is not None:
            ctx.runtime_service = planning_runtime_service
        try:
            return execute_planning_phase(
                ctx=ctx,
                workspace_review=workspace_review,
                extract_structured_text=extract_structured_text,
                extract_plan_steps=extract_plan_steps,
                looks_like_truncated_multistep_plan=looks_like_truncated_multistep_plan,
                normalize_plan_with_live_logging=normalize_plan_with_live_logging,
                workspace_violation_error_cls=workspace_violation_error_cls,
            )
        finally:
            ctx.runtime_service = original_runtime_service
