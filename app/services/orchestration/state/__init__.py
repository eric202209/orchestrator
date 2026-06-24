"""State persistence and session-state helpers for orchestration."""

from .execution_states import OrchestrationPhase, TerminalReason
from .transition_table import LEGAL_TRANSITIONS, TERMINAL_PHASES
from .persistence import (
    CheckpointContext,
    CheckpointData,
    append_orchestration_event,
    diff_orchestration_state_snapshots,
    emit_intent_outcome_mismatch,
    find_latest_orchestration_event,
    maybe_emit_divergence_detected,
    read_orchestration_events,
    read_orchestration_state_snapshots,
    record_live_log,
    record_validation_verdict,
    restore_step_result,
    save_orchestration_checkpoint,
    set_session_alert,
    write_checkpoint_state_snapshot,
    write_orchestration_state_snapshot,
)
from .session_state import (
    clear_session_alert,
    mark_session_completed,
    mark_session_paused,
    mark_session_resumed,
    mark_session_running,
    mark_session_stopped,
)

__all__ = [
    "LEGAL_TRANSITIONS",
    "OrchestrationPhase",
    "TERMINAL_PHASES",
    "TerminalReason",
    "CheckpointContext",
    "CheckpointData",
    "append_orchestration_event",
    "clear_session_alert",
    "diff_orchestration_state_snapshots",
    "emit_intent_outcome_mismatch",
    "find_latest_orchestration_event",
    "mark_session_completed",
    "mark_session_paused",
    "mark_session_resumed",
    "mark_session_running",
    "mark_session_stopped",
    "maybe_emit_divergence_detected",
    "read_orchestration_events",
    "read_orchestration_state_snapshots",
    "record_live_log",
    "record_validation_verdict",
    "restore_step_result",
    "save_orchestration_checkpoint",
    "set_session_alert",
    "write_checkpoint_state_snapshot",
    "write_orchestration_state_snapshot",
]
