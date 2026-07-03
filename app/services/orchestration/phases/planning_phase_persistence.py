"""Planning phase persistence helpers."""

from __future__ import annotations

from typing import Any

from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.state.persistence import (
    append_orchestration_event,
    write_orchestration_state_snapshot,
)
from app.services.orchestration.types import OrchestrationRunContext


def emit_planning_phase_finished(
    ctx: OrchestrationRunContext,
    *,
    plan_verdict: Any,
    planning_phase_event: dict[str, Any] | None,
) -> None:
    try:
        phase_finished_event = append_orchestration_event(
            project_dir=ctx.orchestration_state.project_dir,
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            event_type=EventType.PHASE_FINISHED,
            parent_event_id=(planning_phase_event or {}).get("event_id"),
            details={
                "phase": "planning",
                "status": plan_verdict.status,
                "step_count": len(ctx.orchestration_state.plan),
            },
        )
        write_orchestration_state_snapshot(
            project_dir=ctx.orchestration_state.project_dir,
            session_id=ctx.session_id,
            task_id=ctx.task_id,
            orchestration_state=ctx.orchestration_state,
            trigger="phase_finished",
            related_event_id=phase_finished_event.get("event_id"),
        )
    except Exception as exc:
        ctx.logger.debug(
            "[ORCHESTRATION] Failed to persist planning phase finish event/snapshot: %s",
            exc,
        )
