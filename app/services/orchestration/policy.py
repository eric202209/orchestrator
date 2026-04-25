"""Centralized orchestration policy knobs and thresholds."""

from __future__ import annotations

from dataclasses import asdict, dataclass
from typing import Any

PLANNING_TIMEOUT_MIN_SECONDS = 180
PLANNING_TIMEOUT_MAX_SECONDS = 360
MINIMAL_PLANNING_TIMEOUT_SECONDS = 180
PLANNING_REPAIR_TIMEOUT_SECONDS = 180
ULTRA_MINIMAL_PLANNING_TIMEOUT_SECONDS = 180
STALE_RUN_GUARD_SECONDS = 300
MAX_STEP_ATTEMPTS = 3
DEBUG_TIMEOUT_SECONDS = 180
SUMMARY_TIMEOUT_SECONDS = 180
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
ISOLATION_RESTORE_REASON_MARKERS = (
    "workspace isolation violation",
    "debug workspace isolation violation",
)


@dataclass(frozen=True)
class PolicyProfile:
    """Operator-visible policy profile for validation and recovery posture."""

    name: str
    display_name: str
    description: str
    validation_severity: str
    completion_repair_budget: int
    workspace_restore_mode: str
    planning_mode: str
    retry_mode: str
    restore_behavior_label: str

    def effect_summary(self) -> dict[str, Any]:
        return {
            "planning_mode": self.planning_mode,
            "validation_severity": self.validation_severity,
            "completion_repair_budget": self.completion_repair_budget,
            "retry_mode": self.retry_mode,
            "workspace_restore_mode": self.workspace_restore_mode,
            "restore_behavior_label": self.restore_behavior_label,
        }

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["effects"] = self.effect_summary()
        return payload


_POLICY_PROFILES = {
    "balanced": PolicyProfile(
        name="balanced",
        display_name="Balanced",
        description="Default safety and repair posture for routine implementation work.",
        validation_severity="standard",
        completion_repair_budget=1,
        workspace_restore_mode="preserve_resume_restore_failures",
        planning_mode="balanced",
        retry_mode="single_repair_then_relax",
        restore_behavior_label="Restore only workspace-isolation failures by default",
    ),
    "strict": PolicyProfile(
        name="strict",
        display_name="Strict",
        description="Bias toward hard validation gates and conservative recovery.",
        validation_severity="high",
        completion_repair_budget=0,
        workspace_restore_mode="restore_most_failures",
        planning_mode="strict",
        retry_mode="fail_fast",
        restore_behavior_label="Restore most orchestration failures to the pre-run snapshot",
    ),
    "recovery_friendly": PolicyProfile(
        name="recovery_friendly",
        display_name="Recovery Friendly",
        description="Allow one extra repair loop and preserve more state for operator replay.",
        validation_severity="standard",
        completion_repair_budget=2,
        workspace_restore_mode="preserve_for_resume",
        planning_mode="recovery_friendly",
        retry_mode="extra_repair_budget",
        restore_behavior_label="Preserve more workspace state to support replay and operator recovery",
    ),
}


def clamp_planning_timeout(timeout_seconds: int) -> int:
    """Bound planning time so dense tasks fail faster and more predictably."""

    return max(
        PLANNING_TIMEOUT_MIN_SECONDS,
        min(timeout_seconds, PLANNING_TIMEOUT_MAX_SECONDS),
    )


def apply_validation_policy(status: str, *, severity: str, stage: str) -> str:
    """Escalate validator outcomes according to the active policy severity."""

    normalized_status = str(status or "").strip().lower()
    normalized_severity = str(severity or "standard").strip().lower()
    if normalized_severity != "high":
        return normalized_status
    if stage in {"plan", "task_completion", "baseline_publish"}:
        if normalized_status in {"warning", "repair_required"}:
            return "rejected"
    return normalized_status


def should_restore_workspace_on_failure(
    reason: str, policy_profile: str | None = "balanced"
) -> bool:
    """
    Return True when the workspace should be rolled back to the pre-run
    snapshot.  Preservation takes priority over restoration — if a reason
    matches both lists, it is preserved (safe for resume).
    """
    normalized_reason = str(reason or "").strip().lower()
    profile = get_policy_profile(policy_profile)

    # Explicit preserve signals beat everything.
    if any(marker in normalized_reason for marker in WORKSPACE_PRESERVE_REASON_MARKERS):
        return False

    if any(marker in normalized_reason for marker in ISOLATION_RESTORE_REASON_MARKERS):
        return True

    if profile.workspace_restore_mode == "restore_most_failures":
        return (
            any(
                marker in normalized_reason
                for marker in WORKSPACE_RESTORE_ALLOWED_REASON_MARKERS
            )
            or "completion validation failed" in normalized_reason
        )

    return False


def list_policy_profiles() -> list[PolicyProfile]:
    """Return supported operator policy profiles."""

    return list(_POLICY_PROFILES.values())


def get_policy_profile(name: str | None) -> PolicyProfile:
    """Resolve a requested policy profile with a balanced fallback."""

    normalized = (name or "balanced").strip().lower()
    return _POLICY_PROFILES.get(normalized, _POLICY_PROFILES["balanced"])
