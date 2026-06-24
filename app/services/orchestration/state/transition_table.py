"""Legal state transition graph for the orchestration state machine.

Phase 14A-1: documentation-as-code only.

This table does not enforce transitions at runtime. It names every legal arc
derived from the execution paths in planning_flow, execution_loop, and
completion_flow so that the full graph can be read in one place.

Any change to orchestration routing logic should be reflected here.
"""

from __future__ import annotations

from app.services.orchestration.state.execution_states import OrchestrationPhase

TERMINAL_PHASES: frozenset[OrchestrationPhase] = frozenset(
    {
        OrchestrationPhase.DONE,
        OrchestrationPhase.FAILED,
        OrchestrationPhase.CANCELLED,
    }
)

LEGAL_TRANSITIONS: dict[OrchestrationPhase, frozenset[OrchestrationPhase]] = {
    OrchestrationPhase.PLANNING: frozenset(
        {
            OrchestrationPhase.PLANNING_REPAIR,
            OrchestrationPhase.STEP_EXECUTING,
            OrchestrationPhase.FAILED,
            OrchestrationPhase.CANCELLED,
        }
    ),
    OrchestrationPhase.PLANNING_REPAIR: frozenset(
        {
            OrchestrationPhase.STEP_EXECUTING,
            OrchestrationPhase.FAILED,
            OrchestrationPhase.CANCELLED,
        }
    ),
    OrchestrationPhase.STEP_EXECUTING: frozenset(
        {
            OrchestrationPhase.DEBUG_REPAIR,
            OrchestrationPhase.PLAN_REVISION,
            OrchestrationPhase.AWAITING_INPUT,
            OrchestrationPhase.COMPLETION_VALIDATION,
            OrchestrationPhase.FAILED,
            OrchestrationPhase.CANCELLED,
        }
    ),
    OrchestrationPhase.DEBUG_REPAIR: frozenset(
        {
            OrchestrationPhase.STEP_EXECUTING,
            OrchestrationPhase.FAILED,
            OrchestrationPhase.CANCELLED,
        }
    ),
    OrchestrationPhase.PLAN_REVISION: frozenset(
        {
            OrchestrationPhase.STEP_EXECUTING,
            OrchestrationPhase.FAILED,
            OrchestrationPhase.CANCELLED,
        }
    ),
    OrchestrationPhase.AWAITING_INPUT: frozenset(
        {
            OrchestrationPhase.STEP_EXECUTING,
            OrchestrationPhase.CANCELLED,
        }
    ),
    OrchestrationPhase.COMPLETION_VALIDATION: frozenset(
        {
            OrchestrationPhase.VERIFICATION,
            OrchestrationPhase.COMPLETION_REPAIR,
            OrchestrationPhase.FAILED,
            OrchestrationPhase.CANCELLED,
        }
    ),
    OrchestrationPhase.VERIFICATION: frozenset(
        {
            OrchestrationPhase.COMPLETION_REPAIR,
            OrchestrationPhase.DONE,
            OrchestrationPhase.FAILED,
            OrchestrationPhase.CANCELLED,
        }
    ),
    OrchestrationPhase.COMPLETION_REPAIR: frozenset(
        {
            OrchestrationPhase.VERIFICATION,
            OrchestrationPhase.FAILED,
            OrchestrationPhase.CANCELLED,
        }
    ),
}
