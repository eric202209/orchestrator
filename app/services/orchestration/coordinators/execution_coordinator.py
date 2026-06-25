"""ExecutionCoordinator — owns the execution lifecycle boundary."""

from __future__ import annotations

from typing import Any, Callable, Dict


class ExecutionCoordinator:
    """Thin orchestration boundary for the step execution phase."""

    def run_execution(
        self,
        *,
        ctx: Any,
        extract_structured_text: Callable[[Any], str],
        normalize_step: Callable[..., Dict[str, Any]],
        normalize_plan_with_live_logging: Callable[..., Any],
        workspace_violation_error_cls: type,
        write_project_state_snapshot_fn: Callable[..., None],
        record_live_log_fn: Callable[..., None],
    ) -> Dict[str, Any]:
        from app.services.orchestration.phases.execution_loop import execute_step_loop

        return execute_step_loop(
            ctx=ctx,
            extract_structured_text=extract_structured_text,
            normalize_step=normalize_step,
            normalize_plan_with_live_logging=normalize_plan_with_live_logging,
            workspace_violation_error_cls=workspace_violation_error_cls,
            write_project_state_snapshot_fn=write_project_state_snapshot_fn,
            record_live_log_fn=record_live_log_fn,
        )
