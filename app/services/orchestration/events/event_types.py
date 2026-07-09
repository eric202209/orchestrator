"""Canonical orchestration event type constants.

All calls to ``append_orchestration_event`` should use these constants rather
than raw string literals so the full event vocabulary is enumerable in one place.
"""

from __future__ import annotations


class EventType:
    """Canonical event type names for the orchestration event journal."""

    # ── Phase lifecycle ───────────────────────────────────────────────────────
    PHASE_STARTED = "phase_started"
    PHASE_FINISHED = "phase_finished"

    # ── Step execution ────────────────────────────────────────────────────────
    STEP_STARTED = "step_started"
    STEP_FINISHED = "step_finished"
    RETRY_ENTERED = "retry_entered"

    # ── Tool execution ────────────────────────────────────────────────────────
    TOOL_INVOKED = "tool_invoked"
    TOOL_FAILED = "tool_failed"

    # ── Task lifecycle ────────────────────────────────────────────────────────
    TASK_STARTED = "task_started"
    TASK_QUEUED = "task_queued"
    TASK_CLAIMED = "task_claimed"
    TASK_QUEUE_STALE = "task_queue_stale"
    TASK_DISPATCH_REJECTED = "task_dispatch_rejected"
    TASK_COMPLETED = "task_completed"
    TASK_FAILED = "task_failed"

    # ── Planning ──────────────────────────────────────────────────────────────
    PLAN_REVISED = "plan_revised"
    PLANNING_REPAIR_ARBITRATION = "planning_repair_arbitration"
    PLAN_CANDIDATE_CREATED = "plan_candidate_created"
    PLAN_CANDIDATE_VALIDATED = "plan_candidate_validated"
    PLAN_CANDIDATE_SELECTED = "plan_candidate_selected"
    PLAN_CANDIDATE_REJECTED = "plan_candidate_rejected"
    PLAN_SLOT_MERGED = "plan_slot_merged"
    PLAN_CANDIDATE_EXHAUSTED = "plan_candidate_exhausted"
    CROSS_STAGE_CONVERGENCE = "cross_stage_convergence"
    REASONING_ARTIFACT_GENERATED = "reasoning_artifact_generated"
    LANE_ESCALATION_TRIGGERED = "lane_escalation_triggered"
    LANE_ESCALATION_RESULT = "lane_escalation_result"

    # Low-resource execution
    CONTEXT_COMPACTED = "context_compacted"
    PLAN_TRUNCATED = "plan_truncated"

    # ── Human-in-the-loop ─────────────────────────────────────────────────────
    WAITING_FOR_INPUT = "waiting_for_input"
    HUMAN_INTERVENTION_REQUESTED = "human_intervention_requested"
    HUMAN_INTERVENTION_REPLIED = "human_intervention_replied"

    # ── Validation ────────────────────────────────────────────────────────────
    VALIDATION_RESULT = "validation_result"

    # ── Checkpoints ──────────────────────────────────────────────────────────
    CHECKPOINT_SAVED = "checkpoint_saved"
    CHECKPOINT_LOADED = "checkpoint_loaded"
    CHECKPOINT_CURSOR_RECONCILED = "checkpoint_cursor_reconciled"
    CHECKPOINT_REDIRECTED = "checkpoint_redirected"
    HEALTH_SCORE_UPDATED = "health_score_updated"
    DIVERGENCE_DETECTED = "divergence_detected"
    INTENT_OUTCOME_MISMATCH = "intent_outcome_mismatch"

    # ── Completion / repair ───────────────────────────────────────────────────
    DEBUG_FEEDBACK_CAPTURED = "debug_feedback_captured"
    DEBUG_REPAIR_ATTEMPTED = "debug_repair_attempted"
    REPAIR_GENERATED = "repair_generated"
    REPAIR_APPLIED = "repair_applied"
    REPAIR_REJECTED = "repair_rejected"
    EVALUATOR_RESULT = "evaluator_result"

    # ── Phase 13B: bounded execution recovery ────────────────────────────────
    EXECUTION_RECOVERY_ATTEMPTED = "execution_recovery_attempted"
    EXECUTION_RECOVERY_SUCCEEDED = "execution_recovery_succeeded"
    EXECUTION_RECOVERY_FAILED = "execution_recovery_failed"
    EXECUTION_RECOVERY_SKIPPED = "execution_recovery_skipped"

    # ── Tier 3 — Counterfactual replay ───────────────────────────────────────
    COUNTERFACTUAL_REPLAY_STARTED = "counterfactual_replay_started"

    # ── Workspace ─────────────────────────────────────────────────────────────
    WORKSPACE_RESTORE_SKIPPED = "workspace_restore_skipped"
    WORKSPACE_PRESERVED = "workspace_preserved"
    WORKSPACE_RETRY_DIRTY = "workspace_retry_dirty"
    RESUME_WORKSPACE_DRIFT = "resume_workspace_drift"
    WORKSPACE_CONTRACT_FAILED = "workspace_contract_failed"
    RUNTIME_WORKSPACE_ALLOCATED = "runtime_workspace_allocated"
    RUNTIME_WORKSPACE_DISPOSED = "runtime_workspace_disposed"

    # ── Reliability / evidence ───────────────────────────────────────────────
    COMPLETION_EVIDENCE_FAILED = "completion_evidence_failed"
    WORKSPACE_EVIDENCE_COLLECTED = "workspace_evidence_collected"

    # ── Diagnostics ──────────────────────────────────────────────────────────
    PLANNING_CONTEXT_PROVENANCE = "planning_context_provenance"

    # ── Incremental execution (Slice J) ───────────────────────────────────────
    INCREMENTAL_ATTEMPTED = "incremental_attempted"
    INCREMENTAL_SUCCEEDED = "incremental_succeeded"
    INCREMENTAL_FALLBACK_TO_PLANNING = "incremental_fallback_to_planning"
    INCREMENTAL_FALSE_POSITIVE = "incremental_false_positive"

    # ── Phase 17A: recovery infrastructure ───────────────────────────────────
    RECOVERY_DECISION_ROUTED = "recovery_decision_routed"
    RECOVERY_NOISE_ANNOTATED = "recovery_noise_annotated"

    # ── Phase 17C: active recovery lifecycle ─────────────────────────────────
    RECOVERY_STARTED = "recovery_started"
    RECOVERY_COMPLETED = "recovery_completed"
    RECOVERY_RESUMED = "recovery_resumed"
    RECOVERY_FAILED = "recovery_failed"

    # ── Phase 17B: reflection retry ───────────────────────────────────────────
    RECOVERY_REFLECTION_STARTED = "recovery_reflection_started"
    RECOVERY_REFLECTION_COMPLETED = "recovery_reflection_completed"
    RECOVERY_REFLECTION_SKIPPED = "recovery_reflection_skipped"
    RECOVERY_REFLECTION_FAILED = "recovery_reflection_failed"


_ALL_EVENT_TYPES: frozenset[str] = frozenset(
    v for k, v in EventType.__dict__.items() if not k.startswith("_")
)


def is_known_event_type(event_type: str) -> bool:
    """Return True if ``event_type`` is one of the canonical constants."""
    return event_type in _ALL_EVENT_TYPES
