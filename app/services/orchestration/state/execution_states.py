"""Explicit execution state machine vocabulary for the orchestrator.

Phase 14A-1: documentation-as-code only.

No runtime enforcement — this module names states and reasons that already
exist implicitly in planning, execution, debug repair, completion repair,
and failure handling code.

String values are identical to the literals already emitted by the codebase
so that StrEnum equality works transparently in comparisons and JSON payloads.
"""

from __future__ import annotations

from enum import StrEnum


class OrchestrationPhase(StrEnum):
    # Planning phase
    PLANNING = "planning"
    PLANNING_REPAIR = "planning_repair"

    # Execution phase
    STEP_EXECUTING = "step_executing"
    DEBUG_REPAIR = "debug_repair"
    PLAN_REVISION = "plan_revision"
    AWAITING_INPUT = "awaiting_input"

    # Completion phase
    COMPLETION_VALIDATION = "completion_validation"
    VERIFICATION = "verification"
    COMPLETION_REPAIR = "completion_repair"

    # Terminal states
    DONE = "done"
    FAILED = "failed"
    CANCELLED = "cancelled"


class TerminalReason(StrEnum):
    # Planning terminals (planning_flow.py; planning_support.py)
    PLANNING_REPAIR_OSCILLATION = "planning_repair_oscillation"
    PLANNING_REPAIR_BUDGET_EXHAUSTED = "planning_repair_budget_exhausted"
    PLANNING_FAILED = "planning_failed"

    # Execution terminals (execution_loop.py)
    MAX_ATTEMPTS_REACHED = "max_attempts_reached"
    DEBUG_REPAIR_BUDGET_EXHAUSTED = "debug_repair_budget_exhausted"
    DEBUG_PARSE_ERROR = "debug_parse_error"
    PLAN_REVISION_CAP_REACHED = "plan_revision_cap_reached"
    OP_CONTRACT_VIOLATION = "op_contract_violation"
    WORKSPACE_ISOLATION_VIOLATION = "workspace_isolation_violation"
    MANUAL_REVIEW_REQUIRED = "manual_review_required"
    REASONING_ARTIFACT_GATE_FAILED = "reasoning_artifact_gate_failed"
    REVISED_PLAN_VALIDATION_FAILED = "revised_plan_validation_failed"

    # Completion terminals (completion_flow.py)
    COMPLETION_VALIDATION_FAILED = "completion_validation_failed"
    COMPLETION_REPAIR_FAILED = "completion_repair_failed"
    # Actual code values (not the names in the original proposal):
    COMPLETION_REPAIR_CHURN_LIMIT = "repair_churn_limit"
    COMPLETION_REPAIR_ATTEMPT_LIMIT = "repair_attempt_limit_reached"
    COMPLETION_VERIFICATION_FAILED = "completion_verification_failed"
    VERIFICATION_INTEGRITY_FAILED = "verification_integrity_failed"
    REPAIR_STEP_MISSING_COMMANDS_OR_OPS = "repair_step_missing_commands_or_ops"
    VERIFICATION_FAILED = "verification_failed"

    # Session-driven terminals
    SESSION_STOPPED = "session_stopped"
    SESSION_PAUSED = "session_paused"
