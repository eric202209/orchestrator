"""Helpers for diagnosing orchestration runs that appear stuck at planning."""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
import re
from typing import Optional

PLANNING_RESPONSE_MESSAGES = {
    "[ORCHESTRATION] Planning response received; parsing and validating plan",
    "[OPENCLAW] Request returned output; awaiting orchestration validation",
    "[OPENCLAW] stdout was empty; recovered structured response from stderr",
}

PLANNING_START_MESSAGES = {
    "[ORCHESTRATION] Phase 1: PLANNING - generating step plan",
}

QUEUE_MESSAGES = ("Queued task ",)
CLAIM_MESSAGES = ("[ORCHESTRATION] Worker claimed queued task dispatch",)

PLANNING_RETRY_MARKERS = (
    "Planning output needed a strict JSON retry",
    "Planning response was malformed or truncated; starting repair pass",
    "Planning repair attempt is now running",
    "Planning attempt 2 is now running with the minimal prompt",
    "Planning attempt 3 is now running with the ultra-minimal prompt",
)


@dataclass(frozen=True)
class PlanningLogSnapshot:
    created_at: Optional[datetime]
    message: str
    level: str = "INFO"


@dataclass(frozen=True)
class ValidationCheckpointSnapshot:
    checkpoint_type: str
    description: str
    created_at: Optional[datetime]


@dataclass(frozen=True)
class PlanningRunSnapshot:
    session_id: int
    task_id: int
    session_status: str
    session_is_active: bool
    task_status: str
    task_current_step: int
    task_error_message: str | None = None
    latest_logs: tuple[PlanningLogSnapshot, ...] = ()
    validation_checkpoints: tuple[ValidationCheckpointSnapshot, ...] = ()


@dataclass(frozen=True)
class PlanningDiagnosis:
    state: str
    summary: str
    evidence: tuple[str, ...] = field(default_factory=tuple)
    recommendations: tuple[str, ...] = field(default_factory=tuple)


def extract_plan_preview_from_logs(logs: tuple[PlanningLogSnapshot, ...]) -> str:
    """Pull the most useful inline plan preview from noisy stderr-derived logs."""

    for log in logs:
        message = str(log.message or "")
        if '"finalAssistantVisibleText":' in message or '"text": "' in message:
            return message
    return ""


def detect_duplicate_workspace_roots(plan_preview: str) -> tuple[str, ...]:
    """Flag obvious duplicated-root paths inside returned planning text."""

    text = plan_preview or ""
    patterns = (
        r"frontend/src/frontend/src",
        r"backend/src/backend/src",
        r"apps/frontend/apps/frontend",
        r"apps/backend/apps/backend",
        r"vault/projects/[^/\s]+/vault/projects/[^/\s]+",
    )
    findings = []
    for pattern in patterns:
        match = re.search(pattern, text)
        if match:
            findings.append(match.group(0))
    return tuple(findings)


def diagnose_planning_stuck(
    snapshot: PlanningRunSnapshot,
    *,
    now: Optional[datetime] = None,
    orphaned_after_seconds: int = 120,
    in_flight_after_seconds: int = 360,
) -> PlanningDiagnosis:
    """Classify the most likely planning-stuck state from lightweight signals."""

    current_time = now or datetime.utcnow()
    latest_log = snapshot.latest_logs[0] if snapshot.latest_logs else None
    latest_message = (latest_log.message if latest_log else "").strip()
    latest_age_seconds: Optional[float] = None
    if latest_log and latest_log.created_at:
        latest_age_seconds = (current_time - latest_log.created_at).total_seconds()

    latest_checkpoint = (
        snapshot.validation_checkpoints[0] if snapshot.validation_checkpoints else None
    )
    plan_preview = extract_plan_preview_from_logs(snapshot.latest_logs)
    duplicate_roots = detect_duplicate_workspace_roots(plan_preview)

    if duplicate_roots:
        return PlanningDiagnosis(
            state="returned_plan_contains_duplicated_workspace_roots",
            summary=(
                "The model returned a plan, but the plan preview already shows "
                "duplicated/nested workspace roots that will likely trigger repair "
                "or validation failure again."
            ),
            evidence=tuple(
                [
                    f"Detected duplicated path fragment: {fragment}"
                    for fragment in duplicate_roots
                ]
            )
            + (
                "The latest logs include a recovered plan preview from stderr-derived output.",
            ),
            recommendations=(
                "Tighten the planner repair prompt to explicitly forbid repeated path roots.",
                "Add/strengthen validator rules for duplicated path segments in commands as well as expected_files.",
                "Treat this as a bad returned plan, not a phantom stuck UI state.",
            ),
        )

    if (
        snapshot.task_status.upper() == "PENDING"
        and snapshot.task_current_step == 0
        and any(marker in latest_message for marker in QUEUE_MESSAGES)
        and latest_age_seconds is not None
        and latest_age_seconds >= orphaned_after_seconds
        and not any(
            any(claim_marker in (log.message or "") for claim_marker in CLAIM_MESSAGES)
            for log in snapshot.latest_logs
        )
    ):
        return PlanningDiagnosis(
            state="queued_but_unclaimed",
            summary=(
                "The task was queued for execution, but no worker claim has landed "
                "within the expected handoff window."
            ),
            evidence=(
                f"Task {snapshot.task_id} is still PENDING at step 0.",
                f"Latest log is queue submission and is {int(latest_age_seconds)}s old.",
                "No worker-claim log was recorded after queueing.",
            ),
            recommendations=(
                "Check Celery worker health and task routing before treating this as a planner bug.",
                "Use dispatch watchdog data or orchestration events to confirm whether the task is stale in queue.",
                "If the session instance changed, expect the original dispatch to be rejected rather than claimed.",
            ),
        )

    if (
        snapshot.task_status.upper() == "RUNNING"
        and snapshot.task_current_step == 0
        and latest_message in PLANNING_RESPONSE_MESSAGES
        and latest_age_seconds is not None
        and latest_age_seconds >= orphaned_after_seconds
    ):
        return PlanningDiagnosis(
            state="orphaned_after_planning_response",
            summary=(
                "The model returned a planning response, but the run appears to "
                "have stalled before validation/repair advanced the task."
            ),
            evidence=(
                f"Task {snapshot.task_id} is still RUNNING at step 0.",
                f"Latest log is '{latest_message}' and is {int(latest_age_seconds)}s old.",
                "No newer execution, validation-result, or failure logs were recorded.",
            ),
            recommendations=(
                "Recover the task to PENDING and stop the session before restarting.",
                "Inspect the planner/validation branch immediately after planning-response handling.",
                "Treat old validation_task checkpoints as historical evidence, not replay state.",
            ),
        )

    if (
        snapshot.task_status.upper() == "RUNNING"
        and snapshot.task_current_step == 0
        and any(marker in latest_message for marker in PLANNING_RETRY_MARKERS)
        and latest_age_seconds is not None
        and latest_age_seconds >= in_flight_after_seconds
    ):
        return PlanningDiagnosis(
            state="planning_retry_loop_or_slow_repair",
            summary=(
                "The run is in a planning retry/repair path and has exceeded the "
                "normal wait window without producing a validated plan."
            ),
            evidence=(
                f"Latest log indicates retry/repair activity: '{latest_message}'.",
                f"The latest planning-retry log is {int(latest_age_seconds)}s old.",
            ),
            recommendations=(
                "Inspect whether the repair prompt is still too strict for the current model output.",
                "Check whether validation keeps rejecting nested-workspace or malformed plans.",
                "If no newer logs arrive, recover the task and rerun with the latest planner fixes.",
            ),
        )

    if (
        snapshot.task_status.upper() == "RUNNING"
        and snapshot.task_current_step == 0
        and latest_message in PLANNING_START_MESSAGES
        and latest_age_seconds is not None
        and latest_age_seconds >= in_flight_after_seconds
    ):
        return PlanningDiagnosis(
            state="planning_request_inflight_or_slow",
            summary=(
                "The run looks stuck before the first planning response returned."
            ),
            evidence=(
                f"Latest log is planning start and is {int(latest_age_seconds)}s old.",
                "No planning-response or retry log has been recorded yet.",
            ),
            recommendations=(
                "Check the local model/OpenClaw runtime responsiveness.",
                "Confirm the worker was restarted with the latest planning timeout settings.",
            ),
        )

    if (
        snapshot.task_status.upper() in {"PENDING", "FAILED"}
        and latest_checkpoint is not None
        and latest_checkpoint.checkpoint_type == "validation_plan"
        and "repair_required" in latest_checkpoint.description
    ):
        return PlanningDiagnosis(
            state="stale_validation_artifact_only",
            summary=(
                "The visible validation checkpoint is a historical artifact, not a "
                "live replay checkpoint for the current run."
            ),
            evidence=(
                f"Latest validation checkpoint is {latest_checkpoint.description}.",
                "Task is not actively progressing past planning right now.",
            ),
            recommendations=(
                "Use session/checkpoint JSON files and recent logs to diagnose replay behavior.",
                "Do not assume validation_plan rows alone are driving the current execution.",
            ),
        )

    return PlanningDiagnosis(
        state="active_or_unknown",
        summary=(
            "The snapshot does not match a known planning-stuck signature with high confidence."
        ),
        evidence=(
            f"Session status={snapshot.session_status}, task status={snapshot.task_status}, step={snapshot.task_current_step}.",
            f"Latest log={latest_message or 'none'}.",
        ),
        recommendations=(
            "Inspect the most recent session logs and orchestration events together.",
            "If the run keeps repeating the same planning logs, capture the last 20-30 rows after the newest planning-response log.",
        ),
    )
