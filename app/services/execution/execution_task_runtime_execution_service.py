"""Phase 29C-6B runtime-start, heartbeat, and attempt-outcome authority.

This service owns only the boundary around one already-owned canonical
attempt.  It never evaluates ``done_when``, invokes validation/recovery
policy, creates a replacement attempt, or mutates a workspace.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
import json
import re
from typing import Any

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import (
    ExecutionPlan,
    ExecutionTask,
    ExecutionTaskAttempt,
    ExecutionTaskAttemptOutcome,
    ExecutionTaskDispatchIntent,
    ExecutionTaskRuntimeLease,
    ExecutionTaskRuntimeStart,
    ExecutionTaskTransition,
)
from app.services.execution.execution_task_dispatch_service import (
    ExecutionTaskDispatchService,
)
from app.services.execution.execution_task_runtime_ownership_service import (
    RUNTIME_LEASE_STATUS_ACTIVE,
    RUNTIME_LEASE_STATUS_COMPLETED,
    ExecutionRuntimeOwnershipError,
    ExecutionTaskRuntimeOwnershipService,
    HeartbeatRuntimeOwnershipCommand,
    RuntimeIntegrityResult,
)
from app.services.execution.execution_task_transition_service import (
    ExecutionTaskTransitionCommand,
    ExecutionTaskTransitionError,
    ExecutionTaskTransitionService,
)
from app.services.execution.runtime_execution_adapter import (
    MAX_ADAPTER_DIAGNOSTICS_BYTES,
    MAX_ADAPTER_REFERENCE,
    MAX_ADAPTER_TEXT,
    RUNTIME_OUTCOME_STATUSES,
    ExecutionRuntimeAdapter,
    RuntimeExecutionCommand,
    RuntimeExecutionResult,
    RuntimeProgress,
)
from app.services.orchestration.recovery.failure_classifier import FailureClassifier
from app.services.planning.operator_review import canonical_json_hash
from app.services.execution.execution_plan_commit_service import (
    ExecutionPlanCommitError,
    ExecutionPlanCommitService,
)


RUNTIME_START_SCHEMA_VERSION = "execution-task-runtime-start/1.0"
RUNTIME_OUTCOME_SCHEMA_VERSION = "execution-task-runtime-outcome/1.0"
RUNTIME_START_ACTOR_TYPE = "worker"
RUNTIME_OUTCOME_ACTOR_TYPE = "worker"
MAX_PROVIDER_REQUEST_ID = 255
MAX_FAILURE_CATEGORY = 64
MAX_FAILURE_CODE = 64
MAX_EXCEPTION_TYPE = 128
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

CANONICAL_FAILURE_CATEGORIES = frozenset(
    {
        "execution_timeout",
        "backend_timeout",
        "provider_timeout",
        "provider_unavailable",
        "provider_protocol_error",
        "runtime_exception",
        "caller_cancelled",
        "worker_lost",
        "unknown_failure",
    }
)
FAILURE_CATEGORY_BRIDGE = {
    "execution_timeout": "execution_timeout",
    "backend_timeout": "backend_timeout",
    "provider_timeout": "provider_timeout",
    "provider_unavailable": "provider_unavailable",
    "provider_protocol_error": "provider_protocol_error",
    "provider_process_failure": "provider_protocol_error",
    "provider_output_failure": "provider_protocol_error",
    "provider_result_missing": "provider_protocol_error",
    "provider_result_ambiguous": "provider_protocol_error",
    "execution_failure": "runtime_exception",
    "execution_failed": "runtime_exception",
    "runtime_exception": "runtime_exception",
    "caller_cancelled": "caller_cancelled",
    "worker_lost": "worker_lost",
    "unknown_failure": "unknown_failure",
}


class ExecutionRuntimeEvidenceError(Exception):
    """Bounded error at the runtime evidence boundary."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class MarkRuntimeExecutionStartedCommand:
    execution_task_id: int
    execution_task_attempt_id: int
    dispatch_intent_id: int
    runtime_lease_id: int
    worker_instance_id: str
    ownership_fencing_token: int
    execution_start_idempotency_key: str
    runtime_adapter_name: str
    execution_mode: str
    configuration_hash: str
    adapter_version: str | None = None
    provider_request_id: str | None = None
    creation_actor_type: str = RUNTIME_START_ACTOR_TYPE
    creation_actor_id: str | None = None


@dataclass(frozen=True)
class RuntimeStartResult:
    start: ExecutionTaskRuntimeStart
    replayed: bool = False


@dataclass(frozen=True)
class RecordRuntimeAttemptOutcomeCommand:
    execution_task_id: int
    execution_task_attempt_id: int
    runtime_start_id: int
    runtime_lease_id: int
    worker_instance_id: str
    ownership_fencing_token: int
    expected_task_state: str
    expected_task_state_version: int
    outcome_status: str
    outcome_idempotency_key: str
    provider_request_id: str | None = None
    output_reference: str | None = None
    output_hash: str | None = None
    usage_summary: Mapping[str, Any] | None = None
    failure_category: str | None = None
    failure_code: str | None = None
    sanitized_detail: str | None = None
    exception_type: str | None = None
    diagnostics: Mapping[str, Any] | None = None
    creation_actor_type: str = RUNTIME_OUTCOME_ACTOR_TYPE
    creation_actor_id: str | None = None


@dataclass(frozen=True)
class RuntimeOutcomeResult:
    outcome: ExecutionTaskAttemptOutcome
    transition: ExecutionTaskTransition
    replayed: bool = False


@dataclass(frozen=True)
class RuntimeExecutionOrchestrationResult:
    start: RuntimeStartResult
    outcome: RuntimeOutcomeResult | None
    adapter_result: RuntimeExecutionResult | None = None
    rejected: bool = False
    error_code: str | None = None


@dataclass(frozen=True)
class RuntimeInspectionProjection:
    execution_task_id: int
    state: str
    attempt_id: int | None
    runtime_lease_id: int | None
    runtime_start_id: int | None
    outcome_id: int | None
    heartbeat_current: bool | None
    issues: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_task_id": self.execution_task_id,
            "state": self.state,
            "attempt_id": self.attempt_id,
            "runtime_lease_id": self.runtime_lease_id,
            "runtime_start_id": self.runtime_start_id,
            "outcome_id": self.outcome_id,
            "heartbeat_current": self.heartbeat_current,
            "issues": list(self.issues),
        }


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _text(value: object, field: str, limit: int = MAX_ADAPTER_TEXT) -> str:
    result = str(value or "").strip()
    if not result or len(result) > limit or _CONTROL_RE.search(result):
        raise ExecutionRuntimeEvidenceError(
            "runtime_start_integrity_failure", f"{field} is invalid"
        )
    return result


def _optional_text(value: object, field: str, limit: int) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    result = str(value).strip()
    if len(result) > limit or _CONTROL_RE.search(result):
        raise ExecutionRuntimeEvidenceError(
            "runtime_outcome_status_invalid", f"{field} is invalid"
        )
    return result


def _hash(value: object, field: str) -> str:
    result = str(value or "").strip().lower()
    if not _HASH_RE.fullmatch(result):
        raise ExecutionRuntimeEvidenceError(
            "runtime_outcome_status_invalid", f"{field} is not a SHA-256 hash"
        )
    return result


def _optional_hash(value: object, field: str) -> str | None:
    if value is None or str(value).strip() == "":
        return None
    return _hash(value, field)


def _safe_detail(value: object, limit: int = 1024) -> str | None:
    if value is None:
        return None
    result = _CONTROL_RE.sub(" ", str(value).replace("\n", " ").replace("\r", " "))
    result = result.strip()
    return result[:limit] or None


def _bounded_mapping(
    value: Mapping[str, Any] | None, field: str
) -> dict[str, Any] | None:
    if value is None:
        return None
    if not isinstance(value, Mapping):
        raise ExecutionRuntimeEvidenceError(
            "runtime_outcome_status_invalid", f"{field} must be an object"
        )
    result = dict(value)
    try:
        encoded = json.dumps(result, sort_keys=True, separators=(",", ":"))
    except (TypeError, ValueError) as exc:
        raise ExecutionRuntimeEvidenceError(
            "runtime_outcome_status_invalid", f"{field} is not JSON-safe"
        ) from exc
    if len(encoded.encode("utf-8")) > MAX_ADAPTER_DIAGNOSTICS_BYTES:
        raise ExecutionRuntimeEvidenceError(
            "runtime_outcome_status_invalid", f"{field} is too large"
        )
    return result


def _failure_category(value: object) -> str:
    raw = str(value or "unknown_failure").strip().lower()
    return FAILURE_CATEGORY_BRIDGE.get(raw, "unknown_failure")


def _start_payload(command: MarkRuntimeExecutionStartedCommand) -> dict[str, object]:
    return {
        "schema_version": RUNTIME_START_SCHEMA_VERSION,
        "execution_task_id": int(command.execution_task_id),
        "execution_task_attempt_id": int(command.execution_task_attempt_id),
        "dispatch_intent_id": int(command.dispatch_intent_id),
        "runtime_lease_id": int(command.runtime_lease_id),
        "worker_instance_id": command.worker_instance_id,
        "ownership_fencing_token": int(command.ownership_fencing_token),
        "execution_start_idempotency_key": command.execution_start_idempotency_key,
        "runtime_adapter_name": command.runtime_adapter_name,
        "adapter_version": command.adapter_version,
        "execution_mode": command.execution_mode,
        "configuration_hash": command.configuration_hash,
        "provider_request_id": command.provider_request_id,
        "creation_actor_type": command.creation_actor_type,
        "creation_actor_id": command.creation_actor_id,
    }


def _outcome_payload(command: RecordRuntimeAttemptOutcomeCommand) -> dict[str, object]:
    return {
        "schema_version": RUNTIME_OUTCOME_SCHEMA_VERSION,
        "execution_task_id": int(command.execution_task_id),
        "execution_task_attempt_id": int(command.execution_task_attempt_id),
        "runtime_start_id": int(command.runtime_start_id),
        "runtime_lease_id": int(command.runtime_lease_id),
        "worker_instance_id": command.worker_instance_id,
        "ownership_fencing_token": int(command.ownership_fencing_token),
        "expected_task_state": command.expected_task_state,
        "expected_task_state_version": int(command.expected_task_state_version),
        "outcome_status": command.outcome_status,
        "outcome_idempotency_key": command.outcome_idempotency_key,
        "provider_request_id": command.provider_request_id,
        "output_reference": command.output_reference,
        "output_hash": command.output_hash,
        "usage_summary": command.usage_summary,
        "failure_category": command.failure_category,
        "failure_code": command.failure_code,
        "sanitized_detail": command.sanitized_detail,
        "exception_type": command.exception_type,
        "diagnostics": command.diagnostics,
        "creation_actor_type": command.creation_actor_type,
        "creation_actor_id": command.creation_actor_id,
    }


class ExecutionTaskRuntimeExecutionService:
    """Persist and execute one fenced Phase 29 runtime attempt."""

    def __init__(self, db: Session, *, now: Callable[[], datetime] | None = None):
        self.db = db
        self._now = now or (lambda: datetime.now(timezone.utc))

    def mark_runtime_execution_started(
        self, command: MarkRuntimeExecutionStartedCommand
    ) -> RuntimeStartResult:
        command = self._normalize_start(command)
        payload = _start_payload(command)
        command_hash = canonical_json_hash(payload)

        existing = (
            self.db.query(ExecutionTaskRuntimeStart)
            .filter(
                ExecutionTaskRuntimeStart.execution_start_idempotency_key
                == command.execution_start_idempotency_key
            )
            .one_or_none()
        )
        if existing is not None:
            if existing.canonical_start_command_hash != command_hash:
                raise ExecutionRuntimeEvidenceError(
                    "runtime_start_idempotency_conflict",
                    "execution-start key is bound to another command",
                )
            integrity = self.verify_runtime_start_integrity(existing.id)
            if not integrity.verified:
                raise ExecutionRuntimeEvidenceError(
                    "runtime_start_integrity_failure",
                    "replayed runtime start failed integrity verification",
                )
            return RuntimeStartResult(existing, replayed=True)

        attempt_start = (
            self.db.query(ExecutionTaskRuntimeStart)
            .filter(
                ExecutionTaskRuntimeStart.execution_task_attempt_id
                == int(command.execution_task_attempt_id)
            )
            .one_or_none()
        )
        if attempt_start is not None:
            raise ExecutionRuntimeEvidenceError(
                "runtime_start_already_exists",
                "the attempt already has canonical runtime-start evidence",
            )

        plan, task, attempt, intent, lease = self._require_active_owner(
            execution_task_id=command.execution_task_id,
            execution_task_attempt_id=command.execution_task_attempt_id,
            dispatch_intent_id=command.dispatch_intent_id,
            runtime_lease_id=command.runtime_lease_id,
            worker_instance_id=command.worker_instance_id,
            fencing_token=command.ownership_fencing_token,
            expected_state_version=None,
        )
        now = _utc(self._now()) or datetime.now(timezone.utc)
        start = ExecutionTaskRuntimeStart(
            execution_plan_id=plan.id,
            execution_task_id=task.id,
            execution_task_attempt_id=attempt.id,
            dispatch_intent_id=intent.id,
            runtime_lease_id=lease.id,
            broker_task_id=attempt.broker_task_id,
            worker_instance_id=lease.worker_instance_id,
            ownership_fencing_token=lease.ownership_fencing_token,
            execution_start_idempotency_key=command.execution_start_idempotency_key,
            deterministic_start_command_id=f"runtime-start-command-{attempt.id}",
            canonical_start_command_payload=payload,
            canonical_start_command_hash=command_hash,
            runtime_adapter_name=command.runtime_adapter_name,
            adapter_version=command.adapter_version,
            execution_mode=command.execution_mode,
            configuration_hash=command.configuration_hash,
            provider_request_id=command.provider_request_id,
            started_at=now,
            lifecycle_state_at_start=task.status,
            lifecycle_state_version_at_start=int(task.state_version),
            creation_actor_type=command.creation_actor_type,
            creation_actor_id=command.creation_actor_id or command.worker_instance_id,
        )
        self.db.add(start)
        try:
            self.db.flush()
        except IntegrityError as exc:
            raise ExecutionRuntimeEvidenceError(
                "runtime_start_already_exists",
                "another runtime-start row already exists for the attempt",
            ) from exc
        return RuntimeStartResult(start)

    def heartbeat(self, command: HeartbeatRuntimeOwnershipCommand):
        """Expose the C5 fenced heartbeat with C6B start/progress checks."""

        return ExecutionTaskRuntimeOwnershipService(self.db, now=self._now).heartbeat(
            command
        )

    def record_runtime_attempt_outcome(
        self, command: RecordRuntimeAttemptOutcomeCommand
    ) -> RuntimeOutcomeResult:
        command = self._normalize_outcome(command)
        payload = _outcome_payload(command)
        command_hash = canonical_json_hash(payload)

        existing = (
            self.db.query(ExecutionTaskAttemptOutcome)
            .filter(
                ExecutionTaskAttemptOutcome.outcome_idempotency_key
                == command.outcome_idempotency_key
            )
            .one_or_none()
        )
        if existing is not None:
            if existing.canonical_outcome_command_hash != command_hash:
                raise ExecutionRuntimeEvidenceError(
                    "runtime_outcome_idempotency_conflict",
                    "outcome idempotency key is bound to another command",
                )
            return self._replay_outcome(existing)

        attempt_outcome = (
            self.db.query(ExecutionTaskAttemptOutcome)
            .filter(
                ExecutionTaskAttemptOutcome.execution_task_attempt_id
                == int(command.execution_task_attempt_id)
            )
            .one_or_none()
        )
        if attempt_outcome is not None:
            raise ExecutionRuntimeEvidenceError(
                "runtime_outcome_conflict",
                "the attempt already has a canonical outcome",
            )

        start = self.db.get(ExecutionTaskRuntimeStart, command.runtime_start_id)
        if start is None:
            raise ExecutionRuntimeEvidenceError(
                "runtime_start_not_found", "runtime start was not found"
            )
        plan, task, attempt, intent, lease = self._require_active_owner(
            execution_task_id=command.execution_task_id,
            execution_task_attempt_id=command.execution_task_attempt_id,
            dispatch_intent_id=start.dispatch_intent_id,
            runtime_lease_id=command.runtime_lease_id,
            worker_instance_id=command.worker_instance_id,
            fencing_token=command.ownership_fencing_token,
            expected_state_version=command.expected_task_state_version,
        )
        if (
            start.execution_task_id != task.id
            or start.execution_task_attempt_id != attempt.id
            or start.runtime_lease_id != lease.id
            or start.dispatch_intent_id != intent.id
            or start.worker_instance_id != lease.worker_instance_id
            or start.ownership_fencing_token != lease.ownership_fencing_token
        ):
            raise ExecutionRuntimeEvidenceError(
                "runtime_outcome_integrity_failure",
                "runtime outcome is not bound to the canonical runtime start",
            )
        start_integrity = self.verify_runtime_start_integrity(start.id)
        if not start_integrity.verified:
            raise ExecutionRuntimeEvidenceError(
                "runtime_start_integrity_failure",
                "runtime start failed integrity verification",
            )
        now = _utc(self._now()) or datetime.now(timezone.utc)
        started_at = _utc(start.started_at) or now
        duration = max(0.0, (now - started_at).total_seconds())
        outcome = ExecutionTaskAttemptOutcome(
            execution_plan_id=plan.id,
            execution_task_id=task.id,
            execution_task_attempt_id=attempt.id,
            dispatch_intent_id=intent.id,
            runtime_lease_id=lease.id,
            runtime_start_id=start.id,
            worker_instance_id=lease.worker_instance_id,
            ownership_fencing_token=lease.ownership_fencing_token,
            outcome_idempotency_key=command.outcome_idempotency_key,
            deterministic_outcome_command_id=f"runtime-outcome-command-{attempt.id}",
            canonical_outcome_command_payload=payload,
            canonical_outcome_command_hash=command_hash,
            outcome_status=command.outcome_status,
            completed_at=now,
            runtime_duration_seconds=duration,
            provider_request_id=command.provider_request_id,
            output_reference=command.output_reference,
            output_hash=command.output_hash,
            usage_summary=command.usage_summary,
            failure_category=command.failure_category,
            failure_code=command.failure_code,
            sanitized_failure_detail=command.sanitized_detail,
            exception_type=command.exception_type,
            diagnostics=command.diagnostics,
            creation_actor_type=command.creation_actor_type,
            creation_actor_id=command.creation_actor_id or command.worker_instance_id,
        )
        self.db.add(outcome)
        self.db.flush()

        attempt_status = (
            "candidate_completed"
            if command.outcome_status == "candidate_completed"
            else "failed"
        )
        updated = self.db.execute(
            update(ExecutionTaskAttempt)
            .where(
                ExecutionTaskAttempt.id == attempt.id,
                ExecutionTaskAttempt.attempt_status == "running",
            )
            .values(attempt_status=attempt_status, updated_at=now)
        )
        if updated.rowcount != 1:
            raise ExecutionRuntimeEvidenceError(
                "runtime_outcome_integrity_failure",
                "attempt changed before outcome closure",
            )

        to_state = (
            "awaiting_validation"
            if command.outcome_status == "candidate_completed"
            else "awaiting_recovery"
        )
        reason_code = (
            "runtime_candidate_completed"
            if command.outcome_status == "candidate_completed"
            else "runtime_attempt_failed"
        )
        reason_detail = _safe_detail(
            f"attempt_id={attempt.id};runtime_start_id={start.id};"
            f"outcome_id={outcome.id};worker_instance_id={lease.worker_instance_id}"
        )
        try:
            transition_result = ExecutionTaskTransitionService(
                self.db, now=lambda: now
            ).transition(
                ExecutionTaskTransitionCommand(
                    execution_task_id=task.id,
                    execution_plan_id=plan.id,
                    expected_from_state=command.expected_task_state,
                    expected_state_version=command.expected_task_state_version,
                    to_state=to_state,
                    reason_code=reason_code,
                    reason_detail=reason_detail,
                    actor_type=RUNTIME_OUTCOME_ACTOR_TYPE,
                    actor_id=lease.worker_instance_id,
                    idempotency_key=f"runtime-transition-{attempt.id}",
                    runtime_attempt_id=attempt.id,
                    runtime_lease_id=lease.id,
                    runtime_ownership_fence=lease.ownership_fencing_token,
                )
            )
        except Exception as exc:
            raise ExecutionRuntimeEvidenceError(
                "runtime_outcome_integrity_failure",
                "runtime outcome lifecycle transition could not be persisted",
            ) from exc
        transition = self.db.get(ExecutionTaskTransition, transition_result.event_id)
        if transition is None:
            raise ExecutionRuntimeEvidenceError(
                "runtime_outcome_integrity_failure",
                "runtime outcome lifecycle transition disappeared",
            )

        lease_status = RUNTIME_LEASE_STATUS_COMPLETED
        closure_reason = (
            "runtime_candidate_completed"
            if command.outcome_status == "candidate_completed"
            else "runtime_attempt_failed"
        )
        closure_hash = canonical_json_hash(
            {
                "schema_version": RUNTIME_OUTCOME_SCHEMA_VERSION,
                "outcome_id": outcome.id,
                "lease_id": lease.id,
                "ownership_fencing_token": lease.ownership_fencing_token,
                "closure_reason": closure_reason,
            }
        )
        lease.lease_status = lease_status
        lease.released_at = now
        lease.release_reason = closure_reason
        lease.closed_at = now
        lease.closure_reason = closure_reason
        lease.closed_outcome_id = outcome.id
        lease.closed_worker_instance_id = lease.worker_instance_id
        lease.closed_ownership_fencing_token = lease.ownership_fencing_token
        lease.canonical_closure_hash = closure_hash
        lease.updated_at = now
        outcome.lifecycle_transition_id = transition_result.event_id
        outcome.lifecycle_transition_sequence = transition_result.sequence
        outcome.lifecycle_resulting_state_version = transition_result.resulting_version
        outcome.lease_closed_at = now
        outcome.lease_closure_reason = closure_reason
        outcome.lease_closure_hash = closure_hash
        self.db.flush()
        return RuntimeOutcomeResult(outcome, transition)

    def execute_owned_runtime_attempt(
        self,
        start_command: MarkRuntimeExecutionStartedCommand,
        adapter: ExecutionRuntimeAdapter,
    ) -> RuntimeExecutionOrchestrationResult:
        """Start, invoke, and close one owned attempt across short transactions."""

        start_result = self.mark_runtime_execution_started(start_command)
        self.db.commit()
        existing_outcome = (
            self.db.query(ExecutionTaskAttemptOutcome)
            .filter(
                ExecutionTaskAttemptOutcome.execution_task_attempt_id
                == start_result.start.execution_task_attempt_id
            )
            .one_or_none()
        )
        if existing_outcome is not None:
            replay = self._replay_outcome(existing_outcome)
            self.db.rollback()
            return RuntimeExecutionOrchestrationResult(start_result, replay)

        start = self.db.get(ExecutionTaskRuntimeStart, start_result.start.id)
        if start is None:
            self.db.rollback()
            raise ExecutionRuntimeEvidenceError(
                "runtime_start_integrity_failure", "runtime start disappeared"
            )
        command = self._adapter_command(start)
        self.db.rollback()

        def heartbeat(progress: RuntimeProgress) -> None:
            self.db.rollback()
            sequence = progress.sequence
            if sequence is None:
                lease = self.db.get(ExecutionTaskRuntimeLease, start.runtime_lease_id)
                sequence = int(lease.progress_sequence or 0) + 1 if lease else None
            self.db.rollback()
            self.heartbeat(
                HeartbeatRuntimeOwnershipCommand(
                    runtime_lease_id=start.runtime_lease_id,
                    worker_instance_id=start.worker_instance_id,
                    fencing_token=start.ownership_fencing_token,
                    progress_state=progress.state,
                    progress_sequence=sequence,
                    provider_request_id=progress.provider_request_id,
                )
            )
            self.db.commit()

        def authority_check() -> None:
            self.db.rollback()
            self._require_active_owner(
                execution_task_id=start.execution_task_id,
                execution_task_attempt_id=start.execution_task_attempt_id,
                dispatch_intent_id=start.dispatch_intent_id,
                runtime_lease_id=start.runtime_lease_id,
                worker_instance_id=start.worker_instance_id,
                fencing_token=start.ownership_fencing_token,
                expected_state_version=start.lifecycle_state_version_at_start,
            )
            self.db.rollback()

        def cancellation_check() -> bool:
            try:
                authority_check()
                return False
            except (ExecutionRuntimeEvidenceError, ExecutionRuntimeOwnershipError):
                self.db.rollback()
                return True

        adapter_result: RuntimeExecutionResult | None = None
        try:
            adapter_result = adapter.execute(
                command, heartbeat, authority_check, cancellation_check
            )
            if not isinstance(adapter_result, RuntimeExecutionResult):
                raise ExecutionRuntimeEvidenceError(
                    "runtime_outcome_status_invalid",
                    "adapter returned an unsupported result",
                )
        except ExecutionRuntimeOwnershipError as exc:
            self.db.rollback()
            return RuntimeExecutionOrchestrationResult(
                start_result,
                None,
                rejected=True,
                error_code=exc.code,
            )
        except ExecutionRuntimeEvidenceError as exc:
            self.db.rollback()
            return RuntimeExecutionOrchestrationResult(
                start_result,
                None,
                rejected=True,
                error_code=exc.code,
            )
        except Exception as exc:
            adapter_result = self._failure_result(exc)
        self.db.rollback()

        outcome_command = self._outcome_command_from_result(start, adapter_result)
        try:
            outcome = self.record_runtime_attempt_outcome(outcome_command)
            self.db.commit()
            return RuntimeExecutionOrchestrationResult(
                start_result, outcome, adapter_result=adapter_result
            )
        except ExecutionRuntimeOwnershipError as exc:
            self.db.rollback()
            return RuntimeExecutionOrchestrationResult(
                start_result,
                None,
                adapter_result=adapter_result,
                rejected=True,
                error_code=exc.code,
            )
        except ExecutionRuntimeEvidenceError as exc:
            self.db.rollback()
            return RuntimeExecutionOrchestrationResult(
                start_result,
                None,
                adapter_result=adapter_result,
                rejected=True,
                error_code=exc.code,
            )

    def inspect_unresolved_runtime(
        self, execution_task_id: int
    ) -> RuntimeInspectionProjection:
        task = self.db.get(ExecutionTask, int(execution_task_id))
        if task is None:
            raise ExecutionRuntimeEvidenceError(
                "runtime_attempt_not_found", "Execution Task was not found"
            )
        attempts = (
            self.db.query(ExecutionTaskAttempt)
            .filter(ExecutionTaskAttempt.execution_task_id == task.id)
            .order_by(ExecutionTaskAttempt.id.desc())
            .all()
        )
        if not attempts:
            return RuntimeInspectionProjection(
                task.id, "running_without_start_evidence", None, None, None, None, None
            )
        attempt = attempts[0]
        lease = (
            self.db.query(ExecutionTaskRuntimeLease)
            .filter(ExecutionTaskRuntimeLease.execution_task_attempt_id == attempt.id)
            .order_by(ExecutionTaskRuntimeLease.ownership_fencing_token.desc())
            .first()
        )
        start = (
            self.db.query(ExecutionTaskRuntimeStart)
            .filter(ExecutionTaskRuntimeStart.execution_task_attempt_id == attempt.id)
            .one_or_none()
        )
        outcome = (
            self.db.query(ExecutionTaskAttemptOutcome)
            .filter(ExecutionTaskAttemptOutcome.execution_task_attempt_id == attempt.id)
            .one_or_none()
        )
        if outcome is not None:
            state = (
                "candidate_completed"
                if outcome.outcome_status == "candidate_completed"
                else "attempt_failed"
            )
            if task.status not in {"awaiting_validation", "awaiting_recovery"}:
                state = "outcome_lifecycle_mismatch"
            return RuntimeInspectionProjection(
                task.id,
                state,
                attempt.id,
                lease.id if lease else None,
                start.id if start else None,
                outcome.id,
                False,
            )
        if lease is None or lease.lease_status != RUNTIME_LEASE_STATUS_ACTIVE:
            state = (
                "ownership_expired_without_outcome"
                if lease is not None
                else "running_without_live_owner"
            )
            return RuntimeInspectionProjection(
                task.id,
                state,
                attempt.id,
                lease.id if lease else None,
                start.id if start else None,
                None,
                False,
            )
        now = _utc(self._now()) or datetime.now(timezone.utc)
        heartbeat_current = (_utc(lease.lease_expires_at) or now) > now
        if start is None:
            state = "owned_not_started"
        elif not heartbeat_current:
            state = "started_heartbeat_stale"
        else:
            state = "started_heartbeat_current"
        return RuntimeInspectionProjection(
            task.id,
            state,
            attempt.id,
            lease.id,
            start.id if start else None,
            None,
            heartbeat_current,
        )

    def verify_runtime_start_integrity(
        self, runtime_start_id: int
    ) -> RuntimeIntegrityResult:
        start = self.db.get(ExecutionTaskRuntimeStart, int(runtime_start_id))
        if start is None:
            raise ExecutionRuntimeEvidenceError(
                "runtime_start_not_found", "runtime start was not found"
            )
        issues: list[str] = []
        starts = (
            self.db.query(ExecutionTaskRuntimeStart)
            .filter(
                ExecutionTaskRuntimeStart.execution_task_attempt_id
                == start.execution_task_attempt_id
            )
            .all()
        )
        if len(starts) != 1:
            issues.append("duplicate_runtime_start")
        plan = self.db.get(ExecutionPlan, start.execution_plan_id)
        task = self.db.get(ExecutionTask, start.execution_task_id)
        attempt = self.db.get(ExecutionTaskAttempt, start.execution_task_attempt_id)
        intent = self.db.get(ExecutionTaskDispatchIntent, start.dispatch_intent_id)
        lease = self.db.get(ExecutionTaskRuntimeLease, start.runtime_lease_id)
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
                self.db.query(ExecutionTaskTransition)
                .filter(
                    ExecutionTaskTransition.execution_task_id
                    == start.execution_task_id,
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
        self, outcome_id: int
    ) -> RuntimeIntegrityResult:
        outcome = self.db.get(ExecutionTaskAttemptOutcome, int(outcome_id))
        if outcome is None:
            raise ExecutionRuntimeEvidenceError(
                "runtime_outcome_not_found", "runtime outcome was not found"
            )
        issues: list[str] = []
        outcomes = (
            self.db.query(ExecutionTaskAttemptOutcome)
            .filter(
                ExecutionTaskAttemptOutcome.execution_task_attempt_id
                == outcome.execution_task_attempt_id
            )
            .all()
        )
        if len(outcomes) != 1:
            issues.append("duplicate_runtime_outcome")
        start = self.db.get(ExecutionTaskRuntimeStart, outcome.runtime_start_id)
        attempt = self.db.get(ExecutionTaskAttempt, outcome.execution_task_attempt_id)
        task = self.db.get(ExecutionTask, outcome.execution_task_id)
        lease = self.db.get(ExecutionTaskRuntimeLease, outcome.runtime_lease_id)
        event = (
            self.db.get(ExecutionTaskTransition, outcome.lifecycle_transition_id)
            if outcome.lifecycle_transition_id
            else None
        )
        if any(item is None for item in (start, attempt, task, lease, event)):
            issues.append("runtime_outcome_authority_missing")
        if start is not None:
            start_integrity = self.verify_runtime_start_integrity(start.id)
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
        expected_state = (
            "awaiting_validation"
            if outcome.outcome_status == "candidate_completed"
            else "awaiting_recovery"
        )
        expected_reason = (
            "runtime_candidate_completed"
            if outcome.outcome_status == "candidate_completed"
            else "runtime_attempt_failed"
        )
        if outcome.outcome_status not in RUNTIME_OUTCOME_STATUSES:
            issues.append("runtime_outcome_status_invalid")
        if task is not None and task.status != expected_state:
            issues.append("outcome_lifecycle_mismatch")
        if event is not None and (
            event.to_state != expected_state
            or event.reason_code != expected_reason
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
        if outcome.output_hash is not None and not _HASH_RE.fullmatch(
            outcome.output_hash
        ):
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
                or lease.closed_ownership_fencing_token
                != outcome.ownership_fencing_token
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

    def verify_execution_task_attempt_runtime_integrity(
        self, execution_task_id: int
    ) -> RuntimeIntegrityResult:
        task = self.db.get(ExecutionTask, int(execution_task_id))
        if task is None:
            raise ExecutionRuntimeEvidenceError(
                "runtime_attempt_not_found", "Execution Task was not found"
            )
        issues: list[str] = []
        attempts = (
            self.db.query(ExecutionTaskAttempt)
            .filter(ExecutionTaskAttempt.execution_task_id == task.id)
            .order_by(ExecutionTaskAttempt.id.asc())
            .all()
        )
        for attempt in attempts:
            leases = (
                self.db.query(ExecutionTaskRuntimeLease)
                .filter(
                    ExecutionTaskRuntimeLease.execution_task_attempt_id == attempt.id
                )
                .all()
            )
            starts = (
                self.db.query(ExecutionTaskRuntimeStart)
                .filter(
                    ExecutionTaskRuntimeStart.execution_task_attempt_id == attempt.id
                )
                .all()
            )
            outcomes = (
                self.db.query(ExecutionTaskAttemptOutcome)
                .filter(
                    ExecutionTaskAttemptOutcome.execution_task_attempt_id == attempt.id
                )
                .all()
            )
            if len(starts) > 1:
                issues.append("duplicate_runtime_start")
            if len(outcomes) > 1:
                issues.append("duplicate_runtime_outcome")
            for lease in leases:
                issues.extend(
                    ExecutionTaskRuntimeOwnershipService(self.db, now=self._now)
                    .verify_runtime_ownership_integrity(lease.id)
                    .issues
                )
            for start in starts:
                issues.extend(self.verify_runtime_start_integrity(start.id).issues)
            for outcome in outcomes:
                issues.extend(self.verify_attempt_outcome_integrity(outcome.id).issues)
            if (
                attempt.attempt_status in {"candidate_completed", "failed"}
                and not outcomes
            ):
                issues.append("terminal_attempt_without_outcome")
            if attempt.attempt_status == "running" and not leases:
                issues.append("running_attempt_without_live_owner")
            if attempt.attempt_status == "running" and leases and not starts:
                issues.append("running_without_start_evidence")
        if task.status == "running" and attempts:
            active = any(
                lease.lease_status == RUNTIME_LEASE_STATUS_ACTIVE
                for attempt in attempts
                for lease in attempt.runtime_leases
            )
            if not active:
                issues.append("running_task_without_live_owner")
        return RuntimeIntegrityResult(
            execution_plan_id=task.execution_plan_id,
            execution_task_id=task.id,
            execution_task_attempt_id=None,
            runtime_lease_id=None,
            verified=not issues,
            issues=tuple(sorted(set(issues))),
        )

    def verify_execution_plan_runtime_evidence_integrity(
        self, execution_plan_id: int
    ) -> RuntimeIntegrityResult:
        plan = self.db.get(ExecutionPlan, int(execution_plan_id))
        if plan is None:
            raise ExecutionRuntimeEvidenceError(
                "graph_integrity_failure", "Execution Plan was not found"
            )
        issues: list[str] = []
        tasks = (
            self.db.query(ExecutionTask)
            .filter(ExecutionTask.execution_plan_id == plan.id)
            .order_by(ExecutionTask.id.asc())
            .all()
        )
        for task in tasks:
            result = self.verify_execution_task_attempt_runtime_integrity(task.id)
            issues.extend(result.issues)
        return RuntimeIntegrityResult(
            execution_plan_id=plan.id,
            execution_task_id=None,
            execution_task_attempt_id=None,
            runtime_lease_id=None,
            verified=not issues,
            issues=tuple(sorted(set(issues))),
        )

    def _require_active_owner(
        self,
        *,
        execution_task_id: int,
        execution_task_attempt_id: int,
        dispatch_intent_id: int,
        runtime_lease_id: int,
        worker_instance_id: str,
        fencing_token: int,
        expected_state_version: int | None,
    ) -> tuple[
        ExecutionPlan,
        ExecutionTask,
        ExecutionTaskAttempt,
        ExecutionTaskDispatchIntent,
        ExecutionTaskRuntimeLease,
    ]:
        lease = self.db.get(ExecutionTaskRuntimeLease, int(runtime_lease_id))
        if lease is None:
            raise ExecutionRuntimeEvidenceError(
                "runtime_start_integrity_failure", "runtime lease was not found"
            )
        if lease.worker_instance_id != _text(worker_instance_id, "worker_instance_id"):
            raise ExecutionRuntimeEvidenceError(
                "runtime_start_owner_mismatch", "worker does not own the runtime lease"
            )
        if int(lease.ownership_fencing_token) != int(fencing_token):
            raise ExecutionRuntimeEvidenceError(
                "runtime_start_fence_stale", "runtime ownership fence is stale"
            )
        now = _utc(self._now()) or datetime.now(timezone.utc)
        if lease.lease_status != RUNTIME_LEASE_STATUS_ACTIVE:
            raise ExecutionRuntimeEvidenceError(
                "runtime_start_lease_expired", "runtime lease is not active"
            )
        if (_utc(lease.lease_expires_at) or now) <= now:
            raise ExecutionRuntimeEvidenceError(
                "runtime_start_lease_expired", "runtime lease has expired"
            )
        plan = self.db.get(ExecutionPlan, lease.execution_plan_id)
        task = self.db.get(ExecutionTask, lease.execution_task_id)
        attempt = self.db.get(ExecutionTaskAttempt, lease.execution_task_attempt_id)
        intent = self.db.get(ExecutionTaskDispatchIntent, lease.dispatch_intent_id)
        if any(item is None for item in (plan, task, attempt, intent)):
            raise ExecutionRuntimeEvidenceError(
                "runtime_start_integrity_failure", "runtime authority row is missing"
            )
        assert (
            plan is not None
            and task is not None
            and attempt is not None
            and intent is not None
        )
        if (
            lease.execution_task_id != int(execution_task_id)
            or lease.execution_task_attempt_id != int(execution_task_attempt_id)
            or lease.dispatch_intent_id != int(dispatch_intent_id)
            or task.execution_plan_id != plan.id
            or attempt.execution_task_id != task.id
            or attempt.execution_plan_id != plan.id
            or intent.execution_task_id != task.id
            or intent.execution_plan_id != plan.id
            or intent.runtime_attempt_id != attempt.id
            or intent.broker_task_id != attempt.broker_task_id
        ):
            raise ExecutionRuntimeEvidenceError(
                "runtime_start_integrity_failure",
                "runtime authority identity relationship is invalid",
            )
        if plan.status != "active":
            raise ExecutionRuntimeEvidenceError(
                "runtime_start_integrity_failure", "Execution Plan is not active"
            )
        if attempt.attempt_status != "running":
            raise ExecutionRuntimeEvidenceError(
                "runtime_start_integrity_failure", "runtime attempt is not running"
            )
        if task.status != "running":
            raise ExecutionRuntimeEvidenceError(
                "runtime_start_integrity_failure", "Execution Task is not running"
            )
        if expected_state_version is not None and int(task.state_version) != int(
            expected_state_version
        ):
            raise ExecutionRuntimeEvidenceError(
                "runtime_outcome_conflict", "Execution Task state version is stale"
            )
        ownership = ExecutionTaskRuntimeOwnershipService(
            self.db, now=self._now
        ).verify_runtime_ownership_integrity(lease.id)
        if not ownership.verified:
            raise ExecutionRuntimeEvidenceError(
                "runtime_start_integrity_failure", "runtime ownership integrity failed"
            )
        dispatch = ExecutionTaskDispatchService(
            self.db
        ).verify_dispatch_intent_integrity(intent.id)
        if not dispatch.verified:
            raise ExecutionRuntimeEvidenceError(
                "runtime_start_integrity_failure", "dispatch integrity failed"
            )
        try:
            ExecutionTaskTransitionService(self.db).verify_task_lifecycle_integrity(
                task.id
            )
            ExecutionPlanCommitService(self.db).verify_integrity(plan.id)
        except (ExecutionTaskTransitionError, ExecutionPlanCommitError) as exc:
            raise ExecutionRuntimeEvidenceError(
                "runtime_start_integrity_failure", "lifecycle or graph integrity failed"
            ) from exc
        return plan, task, attempt, intent, lease

    def _normalize_start(
        self, command: MarkRuntimeExecutionStartedCommand
    ) -> MarkRuntimeExecutionStartedCommand:
        if not isinstance(command, MarkRuntimeExecutionStartedCommand):
            raise ExecutionRuntimeEvidenceError(
                "runtime_start_integrity_failure", "runtime-start command is invalid"
            )
        try:
            ids = {
                "execution_task_id": int(command.execution_task_id),
                "execution_task_attempt_id": int(command.execution_task_attempt_id),
                "dispatch_intent_id": int(command.dispatch_intent_id),
                "runtime_lease_id": int(command.runtime_lease_id),
                "ownership_fencing_token": int(command.ownership_fencing_token),
            }
        except (TypeError, ValueError) as exc:
            raise ExecutionRuntimeEvidenceError(
                "runtime_start_integrity_failure",
                "runtime-start identifiers are invalid",
            ) from exc
        if any(value < 1 for value in ids.values()):
            raise ExecutionRuntimeEvidenceError(
                "runtime_start_integrity_failure",
                "runtime-start identifiers are invalid",
            )
        configuration_hash = _hash(command.configuration_hash, "configuration_hash")
        return MarkRuntimeExecutionStartedCommand(
            execution_task_id=ids["execution_task_id"],
            execution_task_attempt_id=ids["execution_task_attempt_id"],
            dispatch_intent_id=ids["dispatch_intent_id"],
            runtime_lease_id=ids["runtime_lease_id"],
            worker_instance_id=_text(command.worker_instance_id, "worker_instance_id"),
            ownership_fencing_token=ids["ownership_fencing_token"],
            execution_start_idempotency_key=_text(
                command.execution_start_idempotency_key,
                "execution_start_idempotency_key",
                128,
            ),
            runtime_adapter_name=_text(
                command.runtime_adapter_name, "runtime_adapter_name", 64
            ),
            execution_mode=_text(command.execution_mode, "execution_mode", 32),
            configuration_hash=configuration_hash,
            adapter_version=_optional_text(
                command.adapter_version, "adapter_version", 64
            ),
            provider_request_id=_optional_text(
                command.provider_request_id,
                "provider_request_id",
                MAX_PROVIDER_REQUEST_ID,
            ),
            creation_actor_type=_text(
                command.creation_actor_type, "creation_actor_type", 32
            ),
            creation_actor_id=_optional_text(
                command.creation_actor_id, "creation_actor_id", 255
            ),
        )

    def _normalize_outcome(
        self, command: RecordRuntimeAttemptOutcomeCommand
    ) -> RecordRuntimeAttemptOutcomeCommand:
        if not isinstance(command, RecordRuntimeAttemptOutcomeCommand):
            raise ExecutionRuntimeEvidenceError(
                "runtime_outcome_status_invalid", "runtime-outcome command is invalid"
            )
        if command.outcome_status not in RUNTIME_OUTCOME_STATUSES:
            raise ExecutionRuntimeEvidenceError(
                "runtime_outcome_status_invalid", "runtime outcome status is invalid"
            )
        if command.outcome_status == "attempt_cancelled":
            raise ExecutionRuntimeEvidenceError(
                "runtime_outcome_status_invalid",
                "cancellation outcome mapping is deferred to existing cancellation authority",
            )
        try:
            ids = {
                "execution_task_id": int(command.execution_task_id),
                "execution_task_attempt_id": int(command.execution_task_attempt_id),
                "runtime_start_id": int(command.runtime_start_id),
                "runtime_lease_id": int(command.runtime_lease_id),
                "ownership_fencing_token": int(command.ownership_fencing_token),
                "expected_task_state_version": int(command.expected_task_state_version),
            }
        except (TypeError, ValueError) as exc:
            raise ExecutionRuntimeEvidenceError(
                "runtime_outcome_status_invalid",
                "runtime-outcome identifiers are invalid",
            ) from exc
        if any(
            value < 1
            for key, value in ids.items()
            if key != "expected_task_state_version"
        ):
            raise ExecutionRuntimeEvidenceError(
                "runtime_outcome_status_invalid",
                "runtime-outcome identifiers are invalid",
            )
        if ids["expected_task_state_version"] < 0:
            raise ExecutionRuntimeEvidenceError(
                "runtime_outcome_status_invalid",
                "expected task state version is invalid",
            )
        output_reference = _optional_text(
            command.output_reference, "output_reference", MAX_ADAPTER_REFERENCE
        )
        output_hash = _optional_hash(command.output_hash, "output_hash")
        if command.outcome_status == "candidate_completed" and not (
            output_reference or output_hash
        ):
            raise ExecutionRuntimeEvidenceError(
                "runtime_outcome_status_invalid",
                "candidate completion requires bounded output evidence",
            )
        failure_category = None
        failure_code = None
        sanitized_detail = None
        exception_type = None
        if command.outcome_status == "attempt_failed":
            failure_category = _failure_category(command.failure_category)
            failure_code = _safe_detail(
                command.failure_code or command.failure_category or "runtime_failure",
                MAX_FAILURE_CODE,
            )
            sanitized_detail = _safe_detail(command.sanitized_detail)
            exception_type = _optional_text(
                command.exception_type, "exception_type", MAX_EXCEPTION_TYPE
            )
        return RecordRuntimeAttemptOutcomeCommand(
            execution_task_id=ids["execution_task_id"],
            execution_task_attempt_id=ids["execution_task_attempt_id"],
            runtime_start_id=ids["runtime_start_id"],
            runtime_lease_id=ids["runtime_lease_id"],
            worker_instance_id=_text(command.worker_instance_id, "worker_instance_id"),
            ownership_fencing_token=ids["ownership_fencing_token"],
            expected_task_state=_text(
                command.expected_task_state, "expected_task_state", 20
            ),
            expected_task_state_version=ids["expected_task_state_version"],
            outcome_status=command.outcome_status,
            outcome_idempotency_key=_text(
                command.outcome_idempotency_key, "outcome_idempotency_key", 128
            ),
            provider_request_id=_optional_text(
                command.provider_request_id,
                "provider_request_id",
                MAX_PROVIDER_REQUEST_ID,
            ),
            output_reference=output_reference,
            output_hash=output_hash,
            usage_summary=_bounded_mapping(command.usage_summary, "usage_summary"),
            failure_category=failure_category,
            failure_code=failure_code,
            sanitized_detail=sanitized_detail,
            exception_type=exception_type,
            diagnostics=_bounded_mapping(command.diagnostics, "diagnostics"),
            creation_actor_type=_text(
                command.creation_actor_type, "creation_actor_type", 32
            ),
            creation_actor_id=_optional_text(
                command.creation_actor_id, "creation_actor_id", 255
            ),
        )

    def _replay_outcome(
        self, outcome: ExecutionTaskAttemptOutcome
    ) -> RuntimeOutcomeResult:
        integrity = self.verify_attempt_outcome_integrity(outcome.id)
        if not integrity.verified:
            raise ExecutionRuntimeEvidenceError(
                "runtime_outcome_integrity_failure",
                "replayed runtime outcome failed integrity verification",
            )
        event = self.db.get(ExecutionTaskTransition, outcome.lifecycle_transition_id)
        if event is None:
            raise ExecutionRuntimeEvidenceError(
                "runtime_outcome_integrity_failure",
                "replayed runtime outcome has no lifecycle transition",
            )
        return RuntimeOutcomeResult(outcome, event, replayed=True)

    def _adapter_command(
        self, start: ExecutionTaskRuntimeStart
    ) -> RuntimeExecutionCommand:
        return RuntimeExecutionCommand(
            execution_plan_id=start.execution_plan_id,
            execution_task_id=start.execution_task_id,
            execution_task_attempt_id=start.execution_task_attempt_id,
            dispatch_intent_id=start.dispatch_intent_id,
            runtime_lease_id=start.runtime_lease_id,
            runtime_start_id=start.id,
            broker_task_id=start.broker_task_id,
            worker_instance_id=start.worker_instance_id,
            ownership_fencing_token=start.ownership_fencing_token,
            runtime_adapter_name=start.runtime_adapter_name,
            adapter_version=start.adapter_version,
            execution_mode=start.execution_mode,
            configuration_hash=start.configuration_hash,
            provider_request_id=start.provider_request_id,
        )

    def _outcome_command_from_result(
        self, start: ExecutionTaskRuntimeStart, result: RuntimeExecutionResult
    ) -> RecordRuntimeAttemptOutcomeCommand:
        task = self.db.get(ExecutionTask, start.execution_task_id)
        if task is None:
            raise ExecutionRuntimeEvidenceError(
                "runtime_outcome_integrity_failure", "Execution Task disappeared"
            )
        if result.completion_kind == "attempt_cancelled":
            raise ExecutionRuntimeEvidenceError(
                "runtime_outcome_status_invalid", "cancellation outcome is deferred"
            )
        return RecordRuntimeAttemptOutcomeCommand(
            execution_task_id=start.execution_task_id,
            execution_task_attempt_id=start.execution_task_attempt_id,
            runtime_start_id=start.id,
            runtime_lease_id=start.runtime_lease_id,
            worker_instance_id=start.worker_instance_id,
            ownership_fencing_token=start.ownership_fencing_token,
            expected_task_state=task.status,
            expected_task_state_version=int(task.state_version),
            outcome_status=result.completion_kind,
            outcome_idempotency_key=f"runtime-outcome-{start.execution_task_attempt_id}",
            provider_request_id=result.provider_request_id or start.provider_request_id,
            output_reference=result.output_reference,
            output_hash=result.output_hash,
            usage_summary=result.usage_summary,
            failure_category=result.failure_category,
            failure_code=result.failure_code,
            sanitized_detail=result.sanitized_detail,
            exception_type=result.exception_type,
            diagnostics=result.diagnostics,
            creation_actor_id=start.worker_instance_id,
        )

    @staticmethod
    def _failure_result(exc: Exception) -> RuntimeExecutionResult:
        provider_classification = getattr(exc, "provider_failure_classification", None)
        event = FailureClassifier.classify(exc, None)
        raw_category = provider_classification or event.failure_class
        category = _failure_category(raw_category)
        return RuntimeExecutionResult(
            completion_kind="attempt_failed",
            failure_category=category,
            failure_code=str(raw_category or category)[:MAX_FAILURE_CODE],
            sanitized_detail=_safe_detail(str(exc)),
            exception_type=type(exc).__name__[:MAX_EXCEPTION_TYPE],
        )


# Short aliases make the command boundary discoverable without importing the
# implementation class name into worker code.
ExecutionTaskRuntimeEvidenceService = ExecutionTaskRuntimeExecutionService


__all__ = [
    "ExecutionRuntimeEvidenceError",
    "ExecutionTaskRuntimeEvidenceService",
    "ExecutionTaskRuntimeExecutionService",
    "MarkRuntimeExecutionStartedCommand",
    "RecordRuntimeAttemptOutcomeCommand",
    "RuntimeExecutionOrchestrationResult",
    "RuntimeInspectionProjection",
    "RuntimeOutcomeResult",
    "RuntimeStartResult",
]
