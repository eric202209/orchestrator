"""Centralized orchestration policy knobs and thresholds."""

from __future__ import annotations

PLANNING_TIMEOUT_MIN_SECONDS = 180
PLANNING_TIMEOUT_MAX_SECONDS = 240
MINIMAL_PLANNING_TIMEOUT_SECONDS = 120
PLANNING_REPAIR_TIMEOUT_SECONDS = 60
ULTRA_MINIMAL_PLANNING_TIMEOUT_SECONDS = 45
STALE_RUN_GUARD_SECONDS = 300
MAX_STEP_ATTEMPTS = 3
DEBUG_TIMEOUT_SECONDS = 180
SUMMARY_TIMEOUT_SECONDS = 60
COMPLETION_VERIFICATION_TIMEOUT_SECONDS = 180
# Reasons that trigger an automatic rollback to the pre-run snapshot.
# Isolation violations always restore (dangerous partial writes).
# Most execution failures also restore so phantom / empty files do not
# accumulate between runs.
WORKSPACE_RESTORE_ALLOWED_REASON_MARKERS = (
    # Isolation / path escapes — always restore
    "workspace isolation violation",
    "debug workspace isolation violation",
    # Planning failures — restore so stale artefacts don't skew re-plan
    "planning json parse failure",  # planning_flow: "planning JSON parse failure"
    "planning parse error",  # planning_flow: "planning parse error"
    "planning validation failure",  # planning_flow: "planning validation failure"
    "truncated multi-step plan",  # planning_flow: "truncated multi-step plan"
    # Execution failures — restore so half-written files don't persist
    "max step attempts reached",
    "repeated tool/path failures",
    "debug parse error",  # execution_loop: "debug parse error"
    "manual review gate",  # execution_loop: "manual review gate"
    # Unhandled exceptions — safest to roll back
    "task exception",
)

# Reasons where we explicitly PRESERVE the workspace (user stopped mid-flight
# and likely wants to resume from the current state).
WORKSPACE_PRESERVE_REASON_MARKERS = (
    "session paused",
    "session stopped",
    "resume preserve workspace",
    "user requested stop",
)


def clamp_planning_timeout(timeout_seconds: int) -> int:
    """Bound planning time so dense tasks fail faster and more predictably."""

    return max(
        PLANNING_TIMEOUT_MIN_SECONDS,
        min(timeout_seconds, PLANNING_TIMEOUT_MAX_SECONDS),
    )


def should_restore_workspace_on_failure(reason: str) -> bool:
    """
    Return True when the workspace should be rolled back to the pre-run
    snapshot.  Preservation takes priority over restoration — if a reason
    matches both lists, it is preserved (safe for resume).
    """
    normalized_reason = str(reason or "").strip().lower()

    # Explicit preserve signals beat everything.
    if any(marker in normalized_reason for marker in WORKSPACE_PRESERVE_REASON_MARKERS):
        return False

    return any(
        marker in normalized_reason
        for marker in WORKSPACE_RESTORE_ALLOWED_REASON_MARKERS
    )
