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
WORKSPACE_RESTORE_ALLOWED_REASON_MARKERS = (
    "workspace isolation violation",
    "debug workspace isolation violation",
)


def clamp_planning_timeout(timeout_seconds: int) -> int:
    """Bound planning time so dense tasks fail faster and more predictably."""

    return max(
        PLANNING_TIMEOUT_MIN_SECONDS,
        min(timeout_seconds, PLANNING_TIMEOUT_MAX_SECONDS),
    )


def should_restore_workspace_on_failure(reason: str) -> bool:
    """Only destructive/isolation failures should automatically roll back files."""

    normalized_reason = str(reason or "").strip().lower()
    return any(
        marker in normalized_reason
        for marker in WORKSPACE_RESTORE_ALLOWED_REASON_MARKERS
    )
