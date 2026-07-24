"""Read-only integrity authority for execution runtime evidence.

This module contains the integrity checks shared by runtime execution and
candidate-content verification.  It deliberately has no candidate-content or
runtime-execution service dependency, so those authorities can depend on one
another's persisted facts without forming an import cycle.
"""

from __future__ import annotations

from datetime import datetime, timezone
import re

from sqlalchemy.orm import Session

from app.models import (
    ExecutionPlan,
    ExecutionTask,
    ExecutionTaskAcceptanceDecision,
    ExecutionTaskAttempt,
    ExecutionTaskAttemptOutcome,
    ExecutionTaskDispatchIntent,
    ExecutionTaskRuntimeLease,
    ExecutionTaskRuntimeStart,
    ExecutionTaskTransition,
)
from app.services.execution.execution_task_runtime_ownership_service import (
    RUNTIME_LEASE_STATUS_COMPLETED,
    RuntimeIntegrityResult,
)
from app.services.execution.runtime_execution_adapter import RUNTIME_OUTCOME_STATUSES
from app.services.planning.operator_review import canonical_json_hash


RUNTIME_OUTCOME_ACTOR_TYPE = "worker"
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")


class ExecutionRuntimeEvidenceError(Exception):
    """Bounded error at the runtime evidence boundary."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def verify_runtime_start_integrity(
    db: Session, runtime_start_id: int
) -> RuntimeIntegrityResult:
    start = db.get(ExecutionTaskRuntimeStart, int(runtime_start_id))
    if start is None:
        raise ExecutionRuntimeEvidenceError(
            "runtime_start_not_found", "runtime start was not found"
        )
    issues: list[str] = []
    starts = (
        db.query(ExecutionTaskRuntimeStart)
        .filter(
            ExecutionTaskRuntimeStart.execution_task_attempt_id
            == start.execution_task_attempt_id
        )
        .all()
    )
    if len(starts) != 1:
        issues.append("duplicate_runtime_start")
    plan = db.get(ExecutionPlan, start.execution_plan_id)
    task = db.get(ExecutionTask, start.execution_task_id)
    attempt = db.get(ExecutionTaskAttempt, start.execution_task_attempt_id)
    intent = db.get(ExecutionTaskDispatchIntent, start.dispatch_intent_id)
    lease = db.get(ExecutionTaskRuntimeLease, start.runtime_lease_id)
    if any(item is None for item in (plan, task, attempt, intent, lease)):
        issues.append("runtime_start_authority_missing")
    if plan is not None and plan.status != "active":
        issues.append("runtime_start_plan_inactive")
    if plan is not None and task is not None and task.execution_plan_id != plan.id:
        issues.append("runtime_start_plan_task_mismatch")
    if task is not None and (
        task.id != start.execution_task_id
        or task.execution_plan_id != start.execution_plan_id
    ):
        issues.append("runtime_start_task_mismatch")
    if attempt is not None and (
        attempt.execution_task_id != start.execution_task_id
        or attempt.execution_plan_id != start.execution_plan_id
        or attempt.dispatch_intent_id != start.dispatch_intent_id
        or attempt.broker_task_id != start.broker_task_id
    ):
        issues.append("runtime_start_attempt_mismatch")
    if intent is not None and (
        intent.execution_task_id != start.execution_task_id
        or intent.execution_plan_id != start.execution_plan_id
        or intent.runtime_attempt_id != start.execution_task_attempt_id
        or intent.broker_task_id != start.broker_task_id
    ):
        issues.append("runtime_start_intent_mismatch")
    if lease is not None and (
        lease.execution_task_id != start.execution_task_id
        or lease.execution_task_attempt_id != start.execution_task_attempt_id
        or lease.dispatch_intent_id != start.dispatch_intent_id
        or lease.broker_task_id != start.broker_task_id
        or lease.worker_instance_id != start.worker_instance_id
        or lease.ownership_fencing_token != start.ownership_fencing_token
    ):
        issues.append("runtime_start_lease_mismatch")
    if start.lifecycle_state_at_start != "running":
        issues.append("runtime_start_state_not_running")
    if start.lifecycle_state_version_at_start < 0:
        issues.append("runtime_start_state_version_invalid")
    if not _HASH_RE.fullmatch(str(start.configuration_hash or "")):
        issues.append("runtime_start_configuration_hash_invalid")
    if not _HASH_RE.fullmatch(str(start.canonical_start_command_hash or "")):
        issues.append("runtime_start_command_hash_malformed")
    elif not isinstance(start.canonical_start_command_payload, dict) or (
        canonical_json_hash(start.canonical_start_command_payload)
        != start.canonical_start_command_hash
    ):
        issues.append("runtime_start_command_hash_mismatch")
    if start.deterministic_start_command_id != (
        f"runtime-start-command-{start.execution_task_attempt_id}"
    ):
        issues.append("runtime_start_command_id_mismatch")
    started = _utc(start.started_at)
    acquired = _utc(lease.acquired_at) if lease is not None else None
    if started is None or acquired is None or started < acquired:
        issues.append("runtime_start_timestamp_order_invalid")
    if lease is not None:
        event = (
            db.query(ExecutionTaskTransition)
            .filter(
                ExecutionTaskTransition.execution_task_id == start.execution_task_id,
                ExecutionTaskTransition.to_state == "running",
                ExecutionTaskTransition.resulting_version
                == start.lifecycle_state_version_at_start,
            )
            .one_or_none()
        )
        if event is None:
            issues.append("runtime_start_lifecycle_reference_missing")
        elif (
            event.runtime_attempt_id != start.execution_task_attempt_id
            or event.runtime_lease_id != start.runtime_lease_id
            or event.runtime_ownership_fence != start.ownership_fencing_token
        ):
            issues.append("runtime_start_lifecycle_reference_mismatch")
    return RuntimeIntegrityResult(
        execution_plan_id=start.execution_plan_id,
        execution_task_id=start.execution_task_id,
        execution_task_attempt_id=start.execution_task_attempt_id,
        runtime_lease_id=start.runtime_lease_id,
        verified=not issues,
        issues=tuple(sorted(set(issues))),
    )


def verify_attempt_outcome_integrity(
    db: Session, outcome_id: int
) -> RuntimeIntegrityResult:
    outcome = db.get(ExecutionTaskAttemptOutcome, int(outcome_id))
    if outcome is None:
        raise ExecutionRuntimeEvidenceError(
            "runtime_outcome_not_found", "runtime outcome was not found"
        )
    issues: list[str] = []
    outcomes = (
        db.query(ExecutionTaskAttemptOutcome)
        .filter(
            ExecutionTaskAttemptOutcome.execution_task_attempt_id
            == outcome.execution_task_attempt_id
        )
        .all()
    )
    if len(outcomes) != 1:
        issues.append("duplicate_runtime_outcome")
    start = db.get(ExecutionTaskRuntimeStart, outcome.runtime_start_id)
    attempt = db.get(ExecutionTaskAttempt, outcome.execution_task_attempt_id)
    task = db.get(ExecutionTask, outcome.execution_task_id)
    lease = db.get(ExecutionTaskRuntimeLease, outcome.runtime_lease_id)
    event = (
        db.get(ExecutionTaskTransition, outcome.lifecycle_transition_id)
        if outcome.lifecycle_transition_id
        else None
    )
    if any(item is None for item in (start, attempt, task, lease, event)):
        issues.append("runtime_outcome_authority_missing")
    if start is not None:
        start_integrity = verify_runtime_start_integrity(db, start.id)
        issues.extend(start_integrity.issues)
        if (
            start.execution_task_id != outcome.execution_task_id
            or start.execution_task_attempt_id != outcome.execution_task_attempt_id
            or start.runtime_lease_id != outcome.runtime_lease_id
        ):
            issues.append("runtime_outcome_start_mismatch")
    if attempt is not None:
        expected_attempt = (
            "candidate_completed"
            if outcome.outcome_status == "candidate_completed"
            else "failed"
        )
        if attempt.attempt_status != expected_attempt:
            issues.append("runtime_outcome_attempt_status_mismatch")
    rejected_validation = None
    if outcome.outcome_status == "candidate_completed":
        rejected_validation = (
            db.query(ExecutionTaskAcceptanceDecision)
            .filter(
                ExecutionTaskAcceptanceDecision.candidate_outcome_id == outcome.id,
                ExecutionTaskAcceptanceDecision.decision_status == "rejected",
            )
            .one_or_none()
        )
    expected_state = (
        "awaiting_recovery"
        if rejected_validation is not None
        else (
            "awaiting_validation"
            if outcome.outcome_status == "candidate_completed"
            else "awaiting_recovery"
        )
    )
    expected_reason = (
        "validation_rejected"
        if rejected_validation is not None
        else (
            "runtime_candidate_completed"
            if outcome.outcome_status == "candidate_completed"
            else "runtime_attempt_failed"
        )
    )
    runtime_event_state = (
        "awaiting_validation"
        if outcome.outcome_status == "candidate_completed"
        else "awaiting_recovery"
    )
    runtime_event_reason = (
        "runtime_candidate_completed"
        if outcome.outcome_status == "candidate_completed"
        else "runtime_attempt_failed"
    )
    if outcome.outcome_status not in RUNTIME_OUTCOME_STATUSES:
        issues.append("runtime_outcome_status_invalid")
    expected_states = {expected_state}
    if expected_state == "awaiting_validation":
        accepted_decision = (
            db.query(ExecutionTaskAcceptanceDecision)
            .filter(
                ExecutionTaskAcceptanceDecision.candidate_outcome_id == outcome.id,
                ExecutionTaskAcceptanceDecision.decision_status == "accepted",
            )
            .one_or_none()
        )
        if accepted_decision is not None:
            expected_states |= {"awaiting_apply", "succeeded"}
    if task is not None and task.status not in expected_states:
        issues.append("outcome_lifecycle_mismatch")
    if event is not None and (
        event.to_state != runtime_event_state
        or event.reason_code != runtime_event_reason
        or event.actor_type != RUNTIME_OUTCOME_ACTOR_TYPE
        or event.runtime_attempt_id != outcome.execution_task_attempt_id
        or event.runtime_lease_id != outcome.runtime_lease_id
        or event.runtime_ownership_fence != outcome.ownership_fencing_token
        or event.sequence != outcome.lifecycle_transition_sequence
        or event.resulting_version != outcome.lifecycle_resulting_state_version
    ):
        issues.append("outcome_lifecycle_reference_mismatch")
    if task is not None and task.status in {"succeeded", "failed"}:
        issues.append("runtime_outcome_direct_terminal_transition")
    if not _HASH_RE.fullmatch(str(outcome.canonical_outcome_command_hash or "")):
        issues.append("runtime_outcome_command_hash_malformed")
    elif not isinstance(outcome.canonical_outcome_command_payload, dict):
        issues.append("runtime_outcome_command_payload_malformed")
    else:
        if (
            canonical_json_hash(outcome.canonical_outcome_command_payload)
            != outcome.canonical_outcome_command_hash
        ):
            issues.append("runtime_outcome_command_hash_mismatch")
        if (
            outcome.canonical_outcome_command_payload.get("output_hash")
            != outcome.output_hash
            or outcome.canonical_outcome_command_payload.get("output_reference")
            != outcome.output_reference
        ):
            issues.append("output_hash_tampered")
    if outcome.deterministic_outcome_command_id != (
        f"runtime-outcome-command-{outcome.execution_task_attempt_id}"
    ):
        issues.append("runtime_outcome_command_id_mismatch")
    if outcome.output_hash is not None and not _HASH_RE.fullmatch(outcome.output_hash):
        issues.append("output_hash_invalid")
    if outcome.runtime_duration_seconds < 0:
        issues.append("runtime_duration_invalid")
    completed = _utc(outcome.completed_at)
    started = _utc(start.started_at) if start is not None else None
    if completed is None or (started is not None and completed < started):
        issues.append("runtime_outcome_timestamp_order_invalid")
    if lease is not None:
        if (
            lease.lease_status != RUNTIME_LEASE_STATUS_COMPLETED
            or lease.closed_outcome_id != outcome.id
            or lease.closed_worker_instance_id != outcome.worker_instance_id
            or lease.closed_ownership_fencing_token != outcome.ownership_fencing_token
        ):
            issues.append("lease_outcome_mismatch")
        if outcome.lease_closed_at != lease.closed_at:
            issues.append("lease_close_timestamp_mismatch")
        if outcome.lease_closure_reason != lease.closure_reason:
            issues.append("lease_close_reason_mismatch")
        if outcome.lease_closure_hash != lease.canonical_closure_hash:
            issues.append("lease_close_hash_mismatch")
        if lease.closed_at is not None and _utc(lease.last_heartbeat_at) > _utc(
            lease.closed_at
        ):
            issues.append("heartbeat_after_outcome")
    return RuntimeIntegrityResult(
        execution_plan_id=outcome.execution_plan_id,
        execution_task_id=outcome.execution_task_id,
        execution_task_attempt_id=outcome.execution_task_attempt_id,
        runtime_lease_id=outcome.runtime_lease_id,
        verified=not issues,
        issues=tuple(sorted(set(issues))),
    )


__all__ = [
    "ExecutionRuntimeEvidenceError",
    "verify_attempt_outcome_integrity",
    "verify_runtime_start_integrity",
]
