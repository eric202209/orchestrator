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
    REASONING_ARTIFACT_GENERATED = "reasoning_artifact_generated"

    # ── Human-in-the-loop ─────────────────────────────────────────────────────
    WAITING_FOR_INPUT = "waiting_for_input"
    HUMAN_INTERVENTION_REQUESTED = "human_intervention_requested"
    HUMAN_INTERVENTION_REPLIED = "human_intervention_replied"

    # ── Validation ────────────────────────────────────────────────────────────
    VALIDATION_RESULT = "validation_result"

    # ── Checkpoints ──────────────────────────────────────────────────────────
    CHECKPOINT_SAVED = "checkpoint_saved"
    CHECKPOINT_LOADED = "checkpoint_loaded"
    CHECKPOINT_REDIRECTED = "checkpoint_redirected"
    HEALTH_SCORE_UPDATED = "health_score_updated"
    DIVERGENCE_DETECTED = "divergence_detected"
    INTENT_OUTCOME_MISMATCH = "intent_outcome_mismatch"

    # ── Completion / repair ───────────────────────────────────────────────────
    REPAIR_GENERATED = "repair_generated"
    REPAIR_APPLIED = "repair_applied"
    REPAIR_REJECTED = "repair_rejected"
    EVALUATOR_RESULT = "evaluator_result"

    # ── Tier 3 — Counterfactual replay ───────────────────────────────────────
    COUNTERFACTUAL_REPLAY_STARTED = "counterfactual_replay_started"

    # ── Workspace ─────────────────────────────────────────────────────────────
    WORKSPACE_RESTORE_SKIPPED = "workspace_restore_skipped"
    WORKSPACE_PRESERVED = "workspace_preserved"
    RESUME_WORKSPACE_DRIFT = "resume_workspace_drift"
    WORKSPACE_CONTRACT_FAILED = "workspace_contract_failed"

    # ── Reliability / evidence ───────────────────────────────────────────────
    COMPLETION_EVIDENCE_FAILED = "completion_evidence_failed"


_ALL_EVENT_TYPES: frozenset[str] = frozenset(
    v for k, v in EventType.__dict__.items() if not k.startswith("_")
)


def is_known_event_type(event_type: str) -> bool:
    """Return True if ``event_type`` is one of the canonical constants."""
    return event_type in _ALL_EVENT_TYPES
