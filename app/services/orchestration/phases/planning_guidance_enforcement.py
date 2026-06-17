"""HG-P2b guidance enforcement hook for the planning loop.

Extracted from planning_flow.py to keep that file under the 2600-line gate.
Called once per planning iteration, before ValidatorService.validate_plan().
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from app.services.human_guidance_plan_validator import (
    check_plan_guidance_violations_if_enabled as _check_plan_violations,
)
from app.services.orchestration.events.telemetry import emit_phase_event

logger = logging.getLogger(__name__)


def emit_hg_p2b_worker_coverage(
    *,
    execution_backend: str,
    resolved_planning_backend: Optional[str],
    use_configured_planning_runtime: bool,
    hg_table_enabled: bool,
    logger: Any,
) -> None:
    """Emit one [HG_COVERAGE] line at worker level before execute_planning_phase.

    Purely diagnostic — no side effects.
    """
    if execution_backend == "local_openclaw" and not use_configured_planning_runtime:
        eligible = False
        reason = "backend_bypasses_python_planning"
    elif not hg_table_enabled:
        eligible = False
        reason = "flags_off"
    else:
        eligible = True
        reason = "structured_plan_expected"
    logger.info(
        "[HG_COVERAGE] execution_backend=%s planning_backend=%s"
        " separate_runtime=%s hg_p2b_eligible=%s reason=%s",
        execution_backend,
        resolved_planning_backend or execution_backend,
        use_configured_planning_runtime,
        eligible,
        reason,
    )


def collect_repair_guidance_block(ctx: Any) -> str:
    """Return the active guidance block for inclusion in planning repair prompts.

    Returns empty string when HG is disabled, no guidance exists, or any error occurs.
    """
    from app.services.human_guidance_plan_validator import (
        render_active_guidance_for_repair as _render,
    )

    return _render(
        ctx.db,
        project_id=getattr(ctx.project, "id", None),
        session_id=ctx.session_id,
        task_id=ctx.task_id,
        user_id=getattr(ctx.project, "user_id", None),
    )


def run_guidance_plan_enforcement(
    ctx: Any,
    *,
    retry_state: Any,
    output_text: str,
    planning_timeout_seconds: int,
    prompt_profile: str,
    repair_fn: Callable[..., Any],
    emit_diagnostics_fn: Callable[..., Any],
) -> Optional[Dict[str, Any]]:
    """Check active guidance against the current plan.

    Returns the new planning_result dict if a repair was triggered and the
    caller must `continue` the planning loop.  Returns None if no action is
    needed (compliant plan or post-repair warning-only pass).
    """
    plan_steps = ctx.orchestration_state.plan or []

    if not plan_steps:
        ctx.logger.info(
            "[HG_P2B_COVERAGE] skip reason=no_structured_plan"
            " project_id=%s task_id=%s",
            getattr(ctx.project, "id", None),
            ctx.task_id,
        )
        return None

    violations: List[str] = _check_plan_violations(
        ctx.db,
        project_id=getattr(ctx.project, "id", None),
        session_id=ctx.session_id,
        task_id=ctx.task_id,
        user_id=getattr(ctx.project, "user_id", None),
        plan_steps=plan_steps,
    )

    if not retry_state.repair_prompt_used:
        if not violations:
            return None
        ctx.logger.warning(
            "[GUIDANCE_PLAN_VALIDATION] Plan violates active guidance (%d rule(s));"
            " triggering repair: %s",
            len(violations),
            "; ".join(violations[:2]),
        )
        emit_phase_event(
            ctx.orchestration_state,
            ctx.emit_live,
            level="WARN",
            phase="planning",
            message="[ORCHESTRATION] Plan violates active Operator Guidance; triggering repair",
            details={"reason": "guidance_violation", "violations": violations[:4]},
        )
        emit_diagnostics_fn(
            ctx,
            reason="guidance_violation",
            contract_violations=violations,
            output_text=output_text,
            strategy_info="guidance_violation",
        )
        retry_state.last_repair_reason = "guidance_violation"
        planning_result = repair_fn(
            ctx=ctx,
            retry_state=retry_state,
            planning_timeout_seconds=planning_timeout_seconds,
            malformed_output=output_text,
            reason="guidance_violation: " + "; ".join(violations[:2]),
            rejection_reasons=violations,
            prompt_profile=prompt_profile,
        )
        retry_state.repair_prompt_used = True
        retry_state.consecutive_failures += 1
        return planning_result
    else:
        if violations:
            ctx.logger.warning(
                "[GUIDANCE_PLAN_VALIDATION] Plan still violates guidance after repair"
                " (%d rule(s)); proceeding: %s",
                len(violations),
                "; ".join(violations[:2]),
            )
        return None
