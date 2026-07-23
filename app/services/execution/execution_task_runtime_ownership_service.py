"""Phase 29C-5 worker receipt and fenced runtime ownership boundary."""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any, Callable

from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError, OperationalError, SQLAlchemyError
from sqlalchemy.orm import Session

from app.models import (
    ExecutionPlan,
    ExecutionTask,
    ExecutionTaskAttempt,
    ExecutionTaskDispatchIntent,
    ExecutionTaskAttemptOutcome,
    ExecutionTaskRuntimeStart,
    ExecutionTaskRuntimeLease,
    ExecutionTaskTransition,
)
from app.services.execution.execution_eligibility_service import (
    ExecutionEligibilityError,
    ExecutionEligibilityService,
)
from app.services.execution.execution_plan_commit_service import (
    ExecutionPlanCommitError,
    ExecutionPlanCommitService,
)
from app.services.execution.execution_task_dispatch_service import (
    ExecutionDispatchError,
    ExecutionTaskDispatchService,
)
from app.services.execution.execution_task_transition_service import (
    ExecutionTaskTransitionCommand,
    ExecutionTaskTransitionError,
    ExecutionTaskTransitionService,
)
from app.services.planning.operator_review import canonical_json_hash
from app.services.execution.runtime_execution_adapter import RUNTIME_PROGRESS_STATES


RUNTIME_OWNERSHIP_SCHEMA_VERSION = "execution-task-runtime-ownership/1.0"
RUNTIME_LEASE_STATUS_ACTIVE = "active"
RUNTIME_LEASE_STATUS_RELEASED = "released"
RUNTIME_LEASE_STATUS_EXPIRED = "expired"
RUNTIME_LEASE_STATUS_COMPLETED = "completed"
RUNTIME_LEASE_STATUS_REVOKED = "revoked"
RUNTIME_LEASE_STATUSES = frozenset(
    {
        RUNTIME_LEASE_STATUS_ACTIVE,
        RUNTIME_LEASE_STATUS_RELEASED,
        RUNTIME_LEASE_STATUS_EXPIRED,
        RUNTIME_LEASE_STATUS_COMPLETED,
        RUNTIME_LEASE_STATUS_REVOKED,
    }
)
MIN_LEASE_SECONDS = 10
DEFAULT_LEASE_SECONDS = 60
MAX_LEASE_SECONDS = 300
MAX_TEXT = 255
MAX_ERROR_DETAIL = 1024
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ExecutionRuntimeOwnershipError(Exception):
    """Bounded domain error for worker receipt and runtime ownership."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class AcquireRuntimeOwnershipCommand:
    dispatch_intent_id: int
    execution_task_attempt_id: int
    execution_task_id: int
    broker_task_id: str
    worker_id: str
    worker_hostname: str
    worker_pid: int
    worker_process_start_identity: str
    worker_instance_id: str
    ownership_idempotency_key: str
    lease_seconds: int = DEFAULT_LEASE_SECONDS
    worker_payload_hash: str | None = None


@dataclass(frozen=True)
class HeartbeatRuntimeOwnershipCommand:
    runtime_lease_id: int
    worker_instance_id: str
    fencing_token: int
    lease_seconds: int = DEFAULT_LEASE_SECONDS
    progress_state: str | None = None
    progress_sequence: int | None = None
    provider_request_id: str | None = None


@dataclass(frozen=True)
class RuntimeOwnershipResult:
    lease: ExecutionTaskRuntimeLease
    transition: Any
    replayed: bool = False


@dataclass(frozen=True)
class RuntimeOwnershipHeartbeatResult:
    lease_id: int
    lease_expires_at: datetime
    last_heartbeat_at: datetime


@dataclass(frozen=True)
class RuntimeIntegrityResult:
    execution_plan_id: int
    execution_task_id: int
    execution_task_attempt_id: int | None
    runtime_lease_id: int | None
    verified: bool
    issues: tuple[str, ...] = ()


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _text(value: object, field: str, limit: int = MAX_TEXT) -> str:
    result = str(value or "").strip()
    if not result or len(result) > limit or _CONTROL_RE.search(result):
        raise ExecutionRuntimeOwnershipError(
            "worker_instance_invalid", f"{field} is invalid"
        )
    return result


def _hash(value: object, field: str) -> str:
    result = str(value or "").strip().lower()
    if not _HASH_RE.fullmatch(result):
        raise ExecutionRuntimeOwnershipError(
            "runtime_ownership_integrity_failure", f"{field} is not a SHA-256 hash"
        )
    return result


def _bounded_error(value: object) -> str:
    return _CONTROL_RE.sub(" ", str(value or "").replace("\n", " ").replace("\r", " "))[
        :MAX_ERROR_DETAIL
    ]


def _command_payload(command: AcquireRuntimeOwnershipCommand) -> dict[str, object]:
    return {
        "schema_version": RUNTIME_OWNERSHIP_SCHEMA_VERSION,
        "dispatch_intent_id": int(command.dispatch_intent_id),
        "execution_task_attempt_id": int(command.execution_task_attempt_id),
        "execution_task_id": int(command.execution_task_id),
        "broker_task_id": command.broker_task_id,
        "worker_id": command.worker_id,
        "worker_hostname": command.worker_hostname,
        "worker_pid": int(command.worker_pid),
        "worker_process_start_identity": command.worker_process_start_identity,
        "worker_instance_id": command.worker_instance_id,
        "ownership_idempotency_key": command.ownership_idempotency_key,
        "lease_seconds": int(command.lease_seconds),
        "worker_payload_hash": command.worker_payload_hash,
    }


class ExecutionTaskRuntimeOwnershipService:
    """Atomically acquire worker ownership and start one submitted attempt."""

    def __init__(
        self,
        db: Session,
        *,
        now: Callable[[], datetime] | None = None,
    ):
        self.db = db
        self._now = now or (lambda: datetime.now(timezone.utc))

    def acquire(
        self, command: AcquireRuntimeOwnershipCommand
    ) -> RuntimeOwnershipResult:
        command = self._normalize_acquire(command)
        command_payload = _command_payload(command)
        command_hash = canonical_json_hash(command_payload)

        existing = (
            self.db.query(ExecutionTaskRuntimeLease)
            .filter(
                ExecutionTaskRuntimeLease.ownership_idempotency_key
                == command.ownership_idempotency_key
            )
            .one_or_none()
        )
        if existing is not None:
            if existing.canonical_ownership_command_hash != command_hash:
                raise ExecutionRuntimeOwnershipError(
                    "runtime_ownership_idempotency_conflict",
                    "ownership idempotency key is bound to another command",
                )
            integrity = self.verify_runtime_ownership_integrity(existing.id)
            if not integrity.verified:
                raise ExecutionRuntimeOwnershipError(
                    "runtime_ownership_integrity_failure",
                    "replayed runtime ownership failed integrity verification",
                )
            return self._result_from_lease(existing, replayed=True)

        intent = self.db.get(ExecutionTaskDispatchIntent, command.dispatch_intent_id)
        if intent is None:
            raise ExecutionRuntimeOwnershipError(
                "dispatch_intent_not_found", "dispatch intent was not found"
            )
        attempt = self.db.get(ExecutionTaskAttempt, command.execution_task_attempt_id)
        if attempt is None:
            raise ExecutionRuntimeOwnershipError(
                "runtime_attempt_not_found", "runtime attempt was not found"
            )
        task = self.db.get(ExecutionTask, command.execution_task_id)
        if task is None:
            raise ExecutionRuntimeOwnershipError(
                "runtime_attempt_not_found", "Execution Task was not found"
            )
        plan = self.db.get(ExecutionPlan, task.execution_plan_id)
        if plan is None:
            raise ExecutionRuntimeOwnershipError(
                "graph_integrity_failure", "Execution Plan was not found"
            )

        now = _utc(self._now()) or datetime.now(timezone.utc)
        active = self._active_lease(attempt.id)
        if active is not None:
            if (_utc(active.lease_expires_at) or now) <= now:
                raise ExecutionRuntimeOwnershipError(
                    "runtime_ownership_expired",
                    "runtime ownership lease has expired",
                )
            raise ExecutionRuntimeOwnershipError(
                "runtime_ownership_conflict",
                "runtime attempt is already owned by another worker",
            )
        if attempt.attempt_status == "running":
            raise ExecutionRuntimeOwnershipError(
                "runtime_attempt_already_running",
                "runtime attempt is already running",
            )
        self._validate_authority(command, intent, attempt, task, plan)
        historical = (
            self.db.query(ExecutionTaskRuntimeLease)
            .filter(ExecutionTaskRuntimeLease.execution_task_attempt_id == attempt.id)
            .order_by(ExecutionTaskRuntimeLease.ownership_fencing_token.desc())
            .first()
        )
        if historical is not None:
            raise ExecutionRuntimeOwnershipError(
                "runtime_ownership_conflict",
                "runtime ownership history requires explicit recovery",
            )

        previous_fence = (
            self.db.query(func.max(ExecutionTaskRuntimeLease.ownership_fencing_token))
            .filter(ExecutionTaskRuntimeLease.execution_task_attempt_id == attempt.id)
            .scalar()
        )
        fencing_token = int(previous_fence or 0) + 1
        lease = ExecutionTaskRuntimeLease(
            execution_plan_id=plan.id,
            execution_task_id=task.id,
            execution_task_attempt_id=attempt.id,
            dispatch_intent_id=intent.id,
            broker_task_id=command.broker_task_id,
            worker_id=command.worker_id,
            worker_hostname=command.worker_hostname,
            worker_pid=command.worker_pid,
            worker_process_start_identity=command.worker_process_start_identity,
            worker_instance_id=command.worker_instance_id,
            ownership_fencing_token=fencing_token,
            lease_status=RUNTIME_LEASE_STATUS_ACTIVE,
            lease_duration_seconds=command.lease_seconds,
            acquired_at=now,
            lease_expires_at=now + timedelta(seconds=command.lease_seconds),
            last_heartbeat_at=now,
            ownership_idempotency_key=command.ownership_idempotency_key,
            canonical_ownership_command_payload=command_payload,
            canonical_ownership_command_hash=command_hash,
            runtime_started_at=now,
            runtime_start_evidence={
                "schema_version": RUNTIME_OWNERSHIP_SCHEMA_VERSION,
                "status": "lifecycle_transition_pending",
                "attempt_id": attempt.id,
                "dispatch_intent_id": intent.id,
                "broker_task_id": command.broker_task_id,
                "ownership_fence": fencing_token,
                "worker_instance_id": command.worker_instance_id,
            },
        )
        self.db.add(lease)
        try:
            self.db.flush()
        except (IntegrityError, OperationalError) as exc:
            self.db.rollback()
            raise ExecutionRuntimeOwnershipError(
                "runtime_ownership_conflict",
                "another worker acquired runtime ownership",
            ) from exc

        try:
            updated = self.db.execute(
                update(ExecutionTaskAttempt)
                .where(
                    ExecutionTaskAttempt.id == attempt.id,
                    ExecutionTaskAttempt.attempt_status == "submitted",
                )
                .values(attempt_status="running", started_at=now, updated_at=now)
            )
        except SQLAlchemyError as exc:
            raise ExecutionRuntimeOwnershipError(
                "runtime_start_transition_failed",
                "runtime attempt start could not be persisted",
            ) from exc
        if updated.rowcount != 1:
            self.db.rollback()
            raise ExecutionRuntimeOwnershipError(
                "runtime_attempt_already_running",
                "runtime attempt changed before it could start",
            )

        reason_detail = (
            f"attempt_id={attempt.id};dispatch_intent_id={intent.id};"
            f"broker_task_id={command.broker_task_id};runtime_lease_id={lease.id};"
            f"ownership_fence={fencing_token};"
            f"worker_instance_id={command.worker_instance_id}"
        )
        try:
            transition = ExecutionTaskTransitionService(
                self.db, now=lambda: now
            ).transition(
                ExecutionTaskTransitionCommand(
                    execution_task_id=task.id,
                    execution_plan_id=plan.id,
                    expected_from_state="ready",
                    expected_state_version=int(intent.claimed_task_state_version),
                    to_state="running",
                    reason_code="execution_started",
                    reason_detail=reason_detail,
                    actor_type="worker",
                    actor_id=command.worker_instance_id,
                    idempotency_key=command.ownership_idempotency_key,
                    runtime_attempt_id=attempt.id,
                    runtime_lease_id=lease.id,
                    runtime_ownership_fence=fencing_token,
                )
            )
        except ExecutionTaskTransitionError as exc:
            raise ExecutionRuntimeOwnershipError(
                "runtime_start_transition_failed",
                "Execution Task ready-to-running transition failed",
            ) from exc
        except SQLAlchemyError as exc:
            raise ExecutionRuntimeOwnershipError(
                "runtime_start_transition_failed",
                "Execution Task ready-to-running transition could not be persisted",
            ) from exc

        lease.lifecycle_transition_id = transition.event_id
        lease.lifecycle_transition_sequence = transition.sequence
        lease.lifecycle_resulting_state_version = transition.resulting_version
        lease.runtime_start_evidence = {
            "schema_version": RUNTIME_OWNERSHIP_SCHEMA_VERSION,
            "status": "RUNTIME_OWNERSHIP_ACQUIRED",
            "attempt_id": attempt.id,
            "dispatch_intent_id": intent.id,
            "broker_task_id": command.broker_task_id,
            "runtime_lease_id": lease.id,
            "ownership_fence": fencing_token,
            "worker_instance_id": command.worker_instance_id,
            "lifecycle_transition_id": transition.event_id,
            "lifecycle_transition_sequence": transition.sequence,
            "lifecycle_resulting_state_version": transition.resulting_version,
            "runtime_started_at": now.isoformat(),
        }
        try:
            self.db.flush()
        except SQLAlchemyError as exc:
            raise ExecutionRuntimeOwnershipError(
                "runtime_start_transition_failed",
                "runtime start evidence could not be persisted",
            ) from exc
        return RuntimeOwnershipResult(lease=lease, transition=transition)

    def receive_worker_dispatch(
        self,
        payload: Mapping[str, object],
        broker_task_id: str,
        *,
        worker_id: str,
        worker_hostname: str,
        worker_pid: int,
        worker_process_start_identity: str,
        worker_instance_id: str,
        lease_seconds: int = DEFAULT_LEASE_SECONDS,
    ) -> RuntimeOwnershipResult:
        intent_id = (
            payload.get("dispatch_intent_id") if isinstance(payload, Mapping) else None
        )
        intent = (
            self.db.get(ExecutionTaskDispatchIntent, intent_id) if intent_id else None
        )
        if intent is not None:
            if str(broker_task_id) != intent.broker_task_id:
                raise ExecutionRuntimeOwnershipError(
                    "broker_task_id_mismatch", "broker task identity does not match"
                )
            if dict(payload) != dict(intent.worker_command_payload):
                raise ExecutionRuntimeOwnershipError(
                    "worker_payload_mismatch", "worker payload does not match authority"
                )
        try:
            entry = ExecutionTaskDispatchService(self.db).validate_worker_entry(
                payload, str(broker_task_id)
            )
        except ExecutionDispatchError as exc:
            code = exc.code
            if code == "dispatch_intent_integrity_failure":
                code = "worker_payload_mismatch"
            raise ExecutionRuntimeOwnershipError(
                code, _bounded_error(exc.message)
            ) from exc
        worker_payload_hash = canonical_json_hash(dict(payload))
        ownership_key = (
            f"runtime-start-{entry.runtime_attempt_id}-"
            f"{canonical_json_hash({'worker_instance_id': worker_instance_id})[:32]}"
        )
        return self.acquire(
            AcquireRuntimeOwnershipCommand(
                dispatch_intent_id=entry.dispatch_intent_id,
                execution_task_attempt_id=entry.runtime_attempt_id,
                execution_task_id=entry.execution_task_id,
                broker_task_id=entry.broker_task_id,
                worker_id=worker_id,
                worker_hostname=worker_hostname,
                worker_pid=worker_pid,
                worker_process_start_identity=worker_process_start_identity,
                worker_instance_id=worker_instance_id,
                ownership_idempotency_key=ownership_key,
                lease_seconds=lease_seconds,
                worker_payload_hash=worker_payload_hash,
            )
        )

    def heartbeat(
        self, command: HeartbeatRuntimeOwnershipCommand
    ) -> RuntimeOwnershipHeartbeatResult:
        try:
            lease_id = int(command.runtime_lease_id)
            fence = int(command.fencing_token)
            lease_seconds = int(command.lease_seconds)
        except (TypeError, ValueError) as exc:
            raise ExecutionRuntimeOwnershipError(
                "runtime_ownership_integrity_failure", "heartbeat command is invalid"
            ) from exc
        if (
            lease_id < 1
            or fence < 1
            or not MIN_LEASE_SECONDS <= lease_seconds <= MAX_LEASE_SECONDS
        ):
            raise ExecutionRuntimeOwnershipError(
                "runtime_ownership_integrity_failure", "heartbeat command is invalid"
            )
        progress_state = command.progress_state
        if progress_state is not None:
            progress_state = str(progress_state).strip()
            if progress_state not in RUNTIME_PROGRESS_STATES:
                raise ExecutionRuntimeOwnershipError(
                    "runtime_progress_state_invalid",
                    "runtime progress state is not recognized",
                )
        progress_sequence = command.progress_sequence
        if progress_sequence is not None:
            try:
                progress_sequence = int(progress_sequence)
            except (TypeError, ValueError) as exc:
                raise ExecutionRuntimeOwnershipError(
                    "runtime_progress_sequence_invalid",
                    "runtime progress sequence is invalid",
                ) from exc
            if progress_sequence < 1:
                raise ExecutionRuntimeOwnershipError(
                    "runtime_progress_sequence_invalid",
                    "runtime progress sequence is invalid",
                )
        worker_instance_id = _text(command.worker_instance_id, "worker_instance_id")
        lease = self.db.get(ExecutionTaskRuntimeLease, lease_id)
        if lease is None:
            raise ExecutionRuntimeOwnershipError(
                "runtime_ownership_not_found", "runtime ownership was not found"
            )
        if lease.worker_instance_id != worker_instance_id:
            raise ExecutionRuntimeOwnershipError(
                "runtime_ownership_owner_mismatch", "worker does not own the lease"
            )
        if int(lease.ownership_fencing_token) != fence:
            raise ExecutionRuntimeOwnershipError(
                "runtime_ownership_fence_stale", "runtime ownership fence is stale"
            )
        now = _utc(self._now()) or datetime.now(timezone.utc)
        if lease.lease_status != RUNTIME_LEASE_STATUS_ACTIVE:
            raise ExecutionRuntimeOwnershipError(
                "runtime_ownership_expired",
                "runtime ownership is not active",
            )
        if (_utc(lease.lease_expires_at) or now) <= now:
            raise ExecutionRuntimeOwnershipError(
                "runtime_ownership_expired", "runtime ownership lease has expired"
            )
        attempt = self.db.get(ExecutionTaskAttempt, lease.execution_task_attempt_id)
        task = self.db.get(ExecutionTask, lease.execution_task_id)
        if (
            attempt is None
            or task is None
            or attempt.attempt_status != "running"
            or task.status != "running"
        ):
            raise ExecutionRuntimeOwnershipError(
                "runtime_ownership_integrity_failure",
                "heartbeat requires a running task and attempt",
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
        # C5 ownership heartbeats remain valid before the adapter handoff for
        # compatibility.  Once a canonical start exists, every heartbeat is
        # start-bound; progress-bearing heartbeats are never accepted before
        # that row exists.
        if progress_state is not None or progress_sequence is not None:
            if start is None or start.runtime_lease_id != lease.id:
                raise ExecutionRuntimeOwnershipError(
                    "runtime_start_not_found",
                    "progress heartbeat requires canonical runtime start evidence",
                )
        if start is not None and start.runtime_lease_id != lease.id:
            raise ExecutionRuntimeOwnershipError(
                "runtime_start_integrity_failure",
                "heartbeat start evidence is bound to another lease",
            )
        if outcome is not None:
            raise ExecutionRuntimeOwnershipError(
                "runtime_outcome_already_exists",
                "heartbeat is not allowed after an attempt outcome",
            )
        if progress_sequence is not None and progress_sequence <= int(
            lease.progress_sequence or 0
        ):
            raise ExecutionRuntimeOwnershipError(
                "runtime_progress_sequence_stale",
                "runtime progress sequence must increase monotonically",
            )
        lease.last_heartbeat_at = now
        lease.lease_expires_at = now + timedelta(seconds=lease_seconds)
        lease.lease_duration_seconds = lease_seconds
        if progress_state is not None:
            lease.progress_state = progress_state
        if progress_sequence is not None:
            lease.progress_sequence = progress_sequence
        lease.updated_at = now
        self.db.flush()
        return RuntimeOwnershipHeartbeatResult(
            lease_id=lease.id,
            lease_expires_at=lease.lease_expires_at,
            last_heartbeat_at=lease.last_heartbeat_at,
        )

    def expire_runtime_ownership(self, runtime_lease_id: int) -> RuntimeIntegrityResult:
        """Explicitly mark an expired lease stale; never restart its attempt."""

        lease = self.db.get(ExecutionTaskRuntimeLease, int(runtime_lease_id))
        if lease is None:
            raise ExecutionRuntimeOwnershipError(
                "runtime_ownership_not_found", "runtime ownership was not found"
            )
        now = _utc(self._now()) or datetime.now(timezone.utc)
        if lease.lease_status != RUNTIME_LEASE_STATUS_ACTIVE:
            return self.verify_runtime_ownership_integrity(lease.id)
        if (_utc(lease.lease_expires_at) or now) > now:
            raise ExecutionRuntimeOwnershipError(
                "runtime_ownership_conflict", "runtime ownership lease is not expired"
            )
        lease.lease_status = RUNTIME_LEASE_STATUS_EXPIRED
        lease.released_at = now
        lease.release_reason = "lease_expired"
        lease.closed_at = now
        lease.closure_reason = "lease_expired"
        lease.closed_worker_instance_id = lease.worker_instance_id
        lease.closed_ownership_fencing_token = lease.ownership_fencing_token
        lease.updated_at = now
        self.db.flush()
        return self.verify_runtime_ownership_integrity(lease.id)

    def verify_runtime_ownership_integrity(
        self, runtime_lease_id: int
    ) -> RuntimeIntegrityResult:
        lease = self.db.get(ExecutionTaskRuntimeLease, int(runtime_lease_id))
        if lease is None:
            raise ExecutionRuntimeOwnershipError(
                "runtime_ownership_not_found", "runtime ownership was not found"
            )
        return self._integrity_for_lease(lease)

    def verify_execution_task_runtime_integrity(
        self, execution_task_id: int
    ) -> RuntimeIntegrityResult:
        task = self.db.get(ExecutionTask, int(execution_task_id))
        if task is None:
            raise ExecutionRuntimeOwnershipError(
                "runtime_attempt_not_found", "Execution Task was not found"
            )
        attempts = (
            self.db.query(ExecutionTaskAttempt)
            .filter(ExecutionTaskAttempt.execution_task_id == task.id)
            .order_by(ExecutionTaskAttempt.id.asc())
            .all()
        )
        issues: list[str] = []
        for attempt in attempts:
            leases = (
                self.db.query(ExecutionTaskRuntimeLease)
                .filter(
                    ExecutionTaskRuntimeLease.execution_task_attempt_id == attempt.id
                )
                .order_by(ExecutionTaskRuntimeLease.ownership_fencing_token.asc())
                .all()
            )
            active = [lease for lease in leases if lease.lease_status == "active"]
            if len(active) > 1:
                issues.append("duplicate_active_owner")
            for lease in leases:
                issues.extend(self._integrity_for_lease(lease).issues)
            if attempt.attempt_status == "running" and len(active) != 1:
                issues.append("running_attempt_without_active_owner")
        if (
            task.status == "running"
            and attempts
            and not any(
                lease.lease_status == "active"
                for attempt in attempts
                for lease in self._leases_for_attempt(attempt.id)
            )
        ):
            issues.append("running_task_without_active_owner")
        return RuntimeIntegrityResult(
            execution_plan_id=task.execution_plan_id,
            execution_task_id=task.id,
            execution_task_attempt_id=None,
            runtime_lease_id=None,
            verified=not issues,
            issues=tuple(sorted(set(issues))),
        )

    def verify_execution_plan_runtime_integrity(
        self, execution_plan_id: int
    ) -> RuntimeIntegrityResult:
        plan = self.db.get(ExecutionPlan, int(execution_plan_id))
        if plan is None:
            raise ExecutionRuntimeOwnershipError(
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
            issues.extend(self.verify_execution_task_runtime_integrity(task.id).issues)
        leases = (
            self.db.query(ExecutionTaskRuntimeLease)
            .filter(ExecutionTaskRuntimeLease.execution_plan_id == plan.id)
            .all()
        )
        known_task_ids = {task.id for task in tasks}
        if any(lease.execution_task_id not in known_task_ids for lease in leases):
            issues.append("ownership_plan_task_mismatch")
        return RuntimeIntegrityResult(
            execution_plan_id=plan.id,
            execution_task_id=0,
            execution_task_attempt_id=None,
            runtime_lease_id=None,
            verified=not issues,
            issues=tuple(sorted(set(issues))),
        )

    def _validate_authority(
        self,
        command: AcquireRuntimeOwnershipCommand,
        intent: ExecutionTaskDispatchIntent,
        attempt: ExecutionTaskAttempt,
        task: ExecutionTask,
        plan: ExecutionPlan,
    ) -> None:
        if intent.execution_plan_id != plan.id or intent.execution_task_id != task.id:
            raise ExecutionRuntimeOwnershipError(
                "runtime_ownership_integrity_failure",
                "dispatch intent does not belong to the task and plan",
            )
        if (
            attempt.dispatch_intent_id != intent.id
            or attempt.execution_task_id != task.id
            or attempt.execution_plan_id != plan.id
            or intent.runtime_attempt_id != attempt.id
        ):
            raise ExecutionRuntimeOwnershipError(
                "runtime_ownership_integrity_failure",
                "runtime attempt identity is inconsistent",
            )
        if (
            command.broker_task_id != intent.broker_task_id
            or command.broker_task_id != attempt.broker_task_id
        ):
            raise ExecutionRuntimeOwnershipError(
                "broker_task_id_mismatch", "broker task identity does not match"
            )
        if intent.dispatch_status == "cancelled":
            raise ExecutionRuntimeOwnershipError(
                "dispatch_intent_cancelled", "dispatch intent is cancelled"
            )
        if intent.dispatch_status != "submitted":
            raise ExecutionRuntimeOwnershipError(
                "dispatch_intent_not_submitted", "dispatch intent is not submitted"
            )
        if attempt.attempt_status == "cancelled":
            raise ExecutionRuntimeOwnershipError(
                "runtime_attempt_cancelled", "runtime attempt is cancelled"
            )
        if attempt.attempt_status != "submitted":
            raise ExecutionRuntimeOwnershipError(
                "runtime_attempt_not_submitted", "runtime attempt is not submitted"
            )
        if plan.status != "active":
            raise ExecutionRuntimeOwnershipError(
                "execution_plan_inactive", "Execution Plan is not active"
            )
        if task.status != "ready":
            raise ExecutionRuntimeOwnershipError(
                "task_not_ready", "Execution Task is not ready"
            )
        if int(task.state_version) != int(intent.claimed_task_state_version):
            raise ExecutionRuntimeOwnershipError(
                "task_version_stale", "Execution Task state version is stale"
            )
        dispatch_integrity = ExecutionTaskDispatchService(
            self.db
        ).verify_dispatch_intent_integrity(intent.id)
        if not dispatch_integrity.verified:
            raise ExecutionRuntimeOwnershipError(
                "runtime_ownership_integrity_failure",
                "dispatch authority integrity failed",
            )
        try:
            lifecycle = ExecutionTaskTransitionService(self.db)
            lifecycle.verify_task_lifecycle_integrity(task.id)
            ExecutionPlanCommitService(self.db).verify_integrity(plan.id)
        except ExecutionTaskTransitionError as exc:
            raise ExecutionRuntimeOwnershipError(
                "lifecycle_integrity_failure", "lifecycle authority integrity failed"
            ) from exc
        except ExecutionPlanCommitError as exc:
            raise ExecutionRuntimeOwnershipError(
                "graph_integrity_failure", "Execution Plan graph integrity failed"
            ) from exc
        try:
            decision = ExecutionEligibilityService(self.db).evaluate_task(task.id)
        except ExecutionEligibilityError as exc:
            code = (
                "graph_integrity_failure"
                if exc.code == "graph_integrity_failure"
                else (
                    "lifecycle_integrity_failure"
                    if exc.code == "lifecycle_integrity_failure"
                    else "eligibility_decision_stale"
                )
            )
            raise ExecutionRuntimeOwnershipError(
                code, _bounded_error(exc.message)
            ) from exc
        if (
            not decision.eligible
            or decision.decision_hash != intent.claim_eligibility_decision_hash
            or decision.evaluated_state != intent.claimed_task_state
            or int(decision.evaluated_state_version)
            != int(intent.claimed_task_state_version)
            or decision.graph_hash != intent.claim_graph_hash
        ):
            raise ExecutionRuntimeOwnershipError(
                "eligibility_decision_stale",
                "current eligibility no longer matches the submitted authority",
            )
        if (
            command.worker_payload_hash is not None
            and command.worker_payload_hash != intent.worker_command_hash
        ):
            raise ExecutionRuntimeOwnershipError(
                "worker_payload_mismatch",
                "worker payload hash does not match authority",
            )

    def _normalize_acquire(
        self, command: AcquireRuntimeOwnershipCommand
    ) -> AcquireRuntimeOwnershipCommand:
        if not isinstance(command, AcquireRuntimeOwnershipCommand):
            raise ExecutionRuntimeOwnershipError(
                "runtime_ownership_integrity_failure", "ownership command is invalid"
            )
        try:
            values = {
                "dispatch_intent_id": int(command.dispatch_intent_id),
                "execution_task_attempt_id": int(command.execution_task_attempt_id),
                "execution_task_id": int(command.execution_task_id),
                "worker_pid": int(command.worker_pid),
                "lease_seconds": int(command.lease_seconds),
            }
        except (TypeError, ValueError) as exc:
            raise ExecutionRuntimeOwnershipError(
                "runtime_ownership_integrity_failure",
                "ownership identifiers are invalid",
            ) from exc
        if any(
            values[key] < 1
            for key in (
                "dispatch_intent_id",
                "execution_task_attempt_id",
                "execution_task_id",
                "worker_pid",
            )
        ):
            raise ExecutionRuntimeOwnershipError(
                "runtime_ownership_integrity_failure",
                "ownership identifiers are invalid",
            )
        if not MIN_LEASE_SECONDS <= values["lease_seconds"] <= MAX_LEASE_SECONDS:
            raise ExecutionRuntimeOwnershipError(
                "runtime_ownership_integrity_failure",
                "lease duration is outside bounds",
            )
        payload_hash = (
            _hash(command.worker_payload_hash, "worker_payload_hash")
            if command.worker_payload_hash is not None
            else None
        )
        return AcquireRuntimeOwnershipCommand(
            dispatch_intent_id=values["dispatch_intent_id"],
            execution_task_attempt_id=values["execution_task_attempt_id"],
            execution_task_id=values["execution_task_id"],
            broker_task_id=_text(command.broker_task_id, "broker_task_id"),
            worker_id=_text(command.worker_id, "worker_id"),
            worker_hostname=_text(command.worker_hostname, "worker_hostname"),
            worker_pid=values["worker_pid"],
            worker_process_start_identity=_text(
                command.worker_process_start_identity, "worker_process_start_identity"
            ),
            worker_instance_id=_text(command.worker_instance_id, "worker_instance_id"),
            ownership_idempotency_key=_text(
                command.ownership_idempotency_key, "ownership_idempotency_key", 128
            ),
            lease_seconds=values["lease_seconds"],
            worker_payload_hash=payload_hash,
        )

    def _active_lease(self, attempt_id: int) -> ExecutionTaskRuntimeLease | None:
        return (
            self.db.query(ExecutionTaskRuntimeLease)
            .filter(
                ExecutionTaskRuntimeLease.execution_task_attempt_id == int(attempt_id),
                ExecutionTaskRuntimeLease.lease_status == RUNTIME_LEASE_STATUS_ACTIVE,
            )
            .order_by(ExecutionTaskRuntimeLease.id.asc())
            .first()
        )

    def _leases_for_attempt(self, attempt_id: int) -> list[ExecutionTaskRuntimeLease]:
        return (
            self.db.query(ExecutionTaskRuntimeLease)
            .filter(
                ExecutionTaskRuntimeLease.execution_task_attempt_id == int(attempt_id)
            )
            .order_by(ExecutionTaskRuntimeLease.ownership_fencing_token.asc())
            .all()
        )

    def _result_from_lease(
        self, lease: ExecutionTaskRuntimeLease, *, replayed: bool
    ) -> RuntimeOwnershipResult:
        transition = (
            self.db.get(ExecutionTaskTransition, lease.lifecycle_transition_id)
            if lease.lifecycle_transition_id
            else None
        )
        if transition is None:
            raise ExecutionRuntimeOwnershipError(
                "runtime_ownership_integrity_failure",
                "runtime ownership has no lifecycle start evidence",
            )
        return RuntimeOwnershipResult(
            lease=lease, transition=transition, replayed=replayed
        )

    def _integrity_for_lease(
        self, lease: ExecutionTaskRuntimeLease
    ) -> RuntimeIntegrityResult:
        issues: list[str] = []
        plan = self.db.get(ExecutionPlan, lease.execution_plan_id)
        task = self.db.get(ExecutionTask, lease.execution_task_id)
        attempt = self.db.get(ExecutionTaskAttempt, lease.execution_task_attempt_id)
        intent = self.db.get(ExecutionTaskDispatchIntent, lease.dispatch_intent_id)
        if plan is None or task is None or attempt is None or intent is None:
            issues.append("ownership_authority_missing")
        if plan is not None and task is not None and task.execution_plan_id != plan.id:
            issues.append("ownership_plan_task_mismatch")
        if attempt is not None and (
            attempt.execution_task_id != lease.execution_task_id
            or attempt.execution_plan_id != lease.execution_plan_id
        ):
            issues.append("ownership_attempt_mismatch")
        if intent is not None and (
            intent.execution_task_id != lease.execution_task_id
            or intent.execution_plan_id != lease.execution_plan_id
            or intent.runtime_attempt_id != lease.execution_task_attempt_id
            or intent.broker_task_id != lease.broker_task_id
        ):
            issues.append("ownership_intent_mismatch")
        if attempt is not None and attempt.broker_task_id != lease.broker_task_id:
            issues.append("ownership_broker_id_mismatch")
        if lease.lease_status not in RUNTIME_LEASE_STATUSES:
            issues.append("invalid_ownership_status")
        if (
            lease.progress_state is not None
            and lease.progress_state not in RUNTIME_PROGRESS_STATES
        ):
            issues.append("progress_state_invalid")
        if int(lease.progress_sequence or 0) < 0:
            issues.append("progress_sequence_invalid")
        if lease.ownership_fencing_token < 1:
            issues.append("fencing_token_invalid")
        leases = self._leases_for_attempt(lease.execution_task_attempt_id)
        fences = [item.ownership_fencing_token for item in leases]
        if any(left >= right for left, right in zip(fences, fences[1:])):
            issues.append("fencing_tokens_not_monotonic")
        if (
            not lease.worker_id
            or not lease.worker_hostname
            or not lease.worker_instance_id
            or not lease.worker_process_start_identity
        ):
            issues.append("worker_instance_identity_missing")
        acquired = _utc(lease.acquired_at)
        expires = _utc(lease.lease_expires_at)
        heartbeat = _utc(lease.last_heartbeat_at)
        released = _utc(lease.released_at)
        started = _utc(lease.runtime_started_at)
        closed = _utc(lease.closed_at)
        if acquired is None or expires is None or heartbeat is None or started is None:
            issues.append("ownership_timestamp_missing")
        else:
            if expires <= acquired:
                issues.append("ownership_expiry_invalid")
            if heartbeat < acquired or started < acquired:
                issues.append("ownership_timestamp_order_invalid")
            if released is not None and released < heartbeat:
                issues.append("ownership_release_timestamp_invalid")
            if closed is not None and closed < acquired:
                issues.append("ownership_close_timestamp_invalid")
            if closed is not None and heartbeat > closed:
                issues.append("heartbeat_after_ownership_close")
        if lease.lease_status == RUNTIME_LEASE_STATUS_ACTIVE:
            now = _utc(self._now()) or datetime.now(timezone.utc)
            if expires is not None and expires <= now:
                issues.append("active_ownership_expired")
            if attempt is None or attempt.attempt_status != "running":
                issues.append("active_owner_on_non_running_attempt")
            if task is None or task.status != "running":
                issues.append("active_owner_on_non_running_task")
        elif released is None or not lease.release_reason:
            issues.append("historical_ownership_release_evidence_missing")
        if lease.lease_status != RUNTIME_LEASE_STATUS_ACTIVE:
            if closed is None or not lease.closure_reason:
                issues.append("ownership_close_evidence_missing")
            if lease.closed_worker_instance_id != lease.worker_instance_id:
                issues.append("ownership_close_worker_mismatch")
            if lease.closed_ownership_fencing_token != lease.ownership_fencing_token:
                issues.append("ownership_close_fence_mismatch")
        if intent is not None and intent.dispatch_status == "cancelled":
            issues.append("ownership_on_cancelled_intent")
        if not isinstance(
            lease.canonical_ownership_command_payload, dict
        ) or not _HASH_RE.fullmatch(str(lease.canonical_ownership_command_hash or "")):
            issues.append("ownership_command_hash_malformed")
        elif (
            canonical_json_hash(lease.canonical_ownership_command_payload)
            != lease.canonical_ownership_command_hash
        ):
            issues.append("ownership_command_hash_mismatch")
        if lease.lifecycle_transition_id is None:
            issues.append("lifecycle_transition_reference_missing")
        else:
            event = self.db.get(ExecutionTaskTransition, lease.lifecycle_transition_id)
            if event is None:
                issues.append("lifecycle_transition_reference_missing")
            elif (
                event.execution_task_id != lease.execution_task_id
                or event.execution_plan_id != lease.execution_plan_id
                or event.to_state != "running"
                or event.runtime_attempt_id != lease.execution_task_attempt_id
                or event.runtime_lease_id != lease.id
                or event.runtime_ownership_fence != lease.ownership_fencing_token
                or event.sequence != lease.lifecycle_transition_sequence
                or event.resulting_version != lease.lifecycle_resulting_state_version
                or event.actor_id != lease.worker_instance_id
            ):
                issues.append("lifecycle_transition_reference_mismatch")
        return RuntimeIntegrityResult(
            execution_plan_id=lease.execution_plan_id,
            execution_task_id=lease.execution_task_id,
            execution_task_attempt_id=lease.execution_task_attempt_id,
            runtime_lease_id=lease.id,
            verified=not issues,
            issues=tuple(sorted(set(issues))),
        )


__all__ = [
    "AcquireRuntimeOwnershipCommand",
    "DEFAULT_LEASE_SECONDS",
    "ExecutionRuntimeOwnershipError",
    "ExecutionTaskRuntimeOwnershipService",
    "HeartbeatRuntimeOwnershipCommand",
    "MAX_LEASE_SECONDS",
    "MIN_LEASE_SECONDS",
    "RuntimeIntegrityResult",
    "RuntimeOwnershipHeartbeatResult",
    "RuntimeOwnershipResult",
]
