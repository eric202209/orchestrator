"""Phase 29C-4 dispatch-intent and canonical-attempt boundary.

The service stops at durable intent creation and bounded broker publication.
It never changes ``ExecutionTask.status`` or creates legacy ``TaskExecution``
rows.  Broker retries reuse the persisted broker task id and worker payload.
"""

from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import re
from typing import Any

from sqlalchemy import func, update
from sqlalchemy.exc import IntegrityError, OperationalError
from sqlalchemy.orm import Session

from app.models import (
    ExecutionPlan,
    ExecutionTask,
    ExecutionTaskAttempt,
    ExecutionTaskDispatchIntent,
    ExecutionTaskSchedulerClaim,
    PlanningSession,
    Project,
)
from app.services.execution.execution_eligibility_service import (
    ExecutionEligibilityError,
    ExecutionEligibilityService,
)
from app.services.execution.execution_task_scheduler_claim_service import (
    CLAIM_STATUS_ACTIVE,
    CLAIM_STATUS_CONSUMED,
    _predecessor_fence_hash as _scheduler_predecessor_fence_hash,
    _predecessor_fences as _scheduler_predecessor_fences,
)
from app.services.execution.execution_task_transition_service import (
    ExecutionTaskTransitionError,
    ExecutionTaskTransitionService,
)
from app.services.planning.operator_review import canonical_json_hash


DISPATCH_INTENT_SCHEMA_VERSION = "execution-dispatch-intent/1.0"
WORKER_COMMAND_SCHEMA_VERSION = "execution-dispatch-worker-command/1.0"
DISPATCH_STATUS_PENDING = "pending_submission"
DISPATCH_STATUS_SUBMITTING = "submitting"
DISPATCH_STATUS_SUBMITTED = "submitted"
DISPATCH_STATUS_FAILED = "submission_failed"
DISPATCH_STATUS_CANCELLED = "cancelled"
DISPATCH_STATUSES = frozenset(
    {
        DISPATCH_STATUS_PENDING,
        DISPATCH_STATUS_SUBMITTING,
        DISPATCH_STATUS_SUBMITTED,
        DISPATCH_STATUS_FAILED,
        DISPATCH_STATUS_CANCELLED,
    }
)
PRESTART_CANCELLATION_REASONS = frozenset(
    {
        "task_no_longer_ready",
        "task_version_changed",
        "plan_inactive",
        "eligibility_changed",
        "graph_integrity_failure",
        "lifecycle_integrity_failure",
        "operator_cancelled",
    }
)
SUBMISSION_LEASE_SECONDS = 60
MAX_ERROR_DETAIL = 1024
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ExecutionDispatchError(Exception):
    """Bounded domain error for the Phase 29C-4 dispatch boundary."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class CreateDispatchIntentCommand:
    execution_task_id: int
    scheduler_claim_id: int
    scheduler_id: str
    claim_fencing_token: int
    expected_task_state: str
    expected_task_state_version: int
    expected_eligibility_decision_hash: str
    dispatch_idempotency_key: str
    creation_actor_type: str = "scheduler"
    creation_actor_id: str | None = None


@dataclass(frozen=True)
class DispatchIntentResult:
    intent: ExecutionTaskDispatchIntent
    attempt: ExecutionTaskAttempt
    replayed: bool = False

    @property
    def dispatch_intent_id(self) -> int:
        return self.intent.id

    @property
    def runtime_attempt_id(self) -> int:
        return self.attempt.id

    @property
    def broker_task_id(self) -> str:
        return self.intent.broker_task_id


@dataclass(frozen=True)
class DispatchSubmissionResult:
    intent: ExecutionTaskDispatchIntent
    attempt: ExecutionTaskAttempt
    status: str
    replayed: bool = False
    error_code: str | None = None

    @property
    def broker_task_id(self) -> str:
        return self.intent.broker_task_id


@dataclass(frozen=True)
class DispatchIntegrityResult:
    dispatch_intent_id: int
    execution_task_id: int
    execution_plan_id: int
    attempt_id: int | None
    verified: bool
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class WorkerEntryResult:
    dispatch_intent_id: int
    runtime_attempt_id: int
    execution_task_id: int
    broker_task_id: str
    duplicate_delivery: bool = False


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _text(value: object, field: str, limit: int, *, required: bool = True) -> str:
    result = str(value or "").strip()
    if required and not result:
        raise ExecutionDispatchError(
            "dispatch_intent_integrity_failure", f"{field} is required"
        )
    if len(result) > limit or _CONTROL_RE.search(result):
        raise ExecutionDispatchError(
            "dispatch_intent_integrity_failure", f"{field} is malformed or too long"
        )
    return result


def _hash(value: object, field: str) -> str:
    result = str(value or "").strip().lower()
    if not _HASH_RE.fullmatch(result):
        raise ExecutionDispatchError(
            "dispatch_intent_integrity_failure", f"{field} is not a SHA-256 hash"
        )
    return result


def _safe_detail(value: object) -> str:
    detail = str(value or "").replace("\n", " ").replace("\r", " ").strip()
    detail = _CONTROL_RE.sub(" ", detail)
    return detail[:MAX_ERROR_DETAIL]


def _command_payload(
    command: CreateDispatchIntentCommand,
    claim: ExecutionTaskSchedulerClaim,
    plan: ExecutionPlan,
    attempt_number: int,
) -> dict[str, object]:
    return {
        "schema_version": DISPATCH_INTENT_SCHEMA_VERSION,
        "execution_plan_id": int(plan.id),
        "execution_task_id": int(command.execution_task_id),
        "scheduler_claim_id": int(claim.id),
        "scheduler_id": command.scheduler_id,
        "claim_fencing_token": int(command.claim_fencing_token),
        "claim_eligibility_decision_hash": command.expected_eligibility_decision_hash,
        "claim_graph_hash": claim.claimed_graph_hash,
        "claim_predecessor_fence_hash": claim.predecessor_fence_hash,
        "claim_predecessor_fences": claim.predecessor_fences,
        "expected_task_state": command.expected_task_state,
        "expected_task_state_version": int(command.expected_task_state_version),
        "dispatch_idempotency_key": command.dispatch_idempotency_key,
        "attempt_number": int(attempt_number),
        "creation_actor_type": command.creation_actor_type,
        "creation_actor_id": command.creation_actor_id,
    }


def _attempt_identity(task_id: int, attempt_number: int, command_hash: str) -> str:
    return f"execution-attempt-{int(task_id)}-{int(attempt_number)}-{command_hash[:32]}"


def _broker_task_id(task_id: int, attempt_number: int, command_hash: str) -> str:
    return (
        f"execution-dispatch-{int(task_id)}-{int(attempt_number)}-{command_hash[:32]}"
    )


def _dispatch_command_id(task_id: int, attempt_number: int, command_hash: str) -> str:
    return f"execution-command-{int(task_id)}-{int(attempt_number)}-{command_hash[:32]}"


def _requested_fields(payload: Mapping[str, object]) -> tuple[object, ...]:
    return tuple(
        payload.get(key)
        for key in (
            "execution_task_id",
            "scheduler_claim_id",
            "scheduler_id",
            "claim_fencing_token",
            "expected_task_state",
            "expected_task_state_version",
            "claim_eligibility_decision_hash",
            "dispatch_idempotency_key",
            "creation_actor_type",
            "creation_actor_id",
        )
    )


class ExecutionTaskDispatchService:
    """Create, submit, recover, and verify Phase 29C-4 dispatch records."""

    def __init__(
        self,
        db: Session,
        *,
        now: Callable[[], datetime] | None = None,
        submitter_id: str = "dispatch-service",
        publisher: Callable[[str, Mapping[str, object], str], object] | None = None,
        submission_lease_seconds: int = SUBMISSION_LEASE_SECONDS,
    ):
        self.db = db
        self._now = now or (lambda: datetime.now(timezone.utc))
        self.submitter_id = _text(submitter_id, "submitter_id", 255)
        self._publisher = publisher or self._publish_to_celery
        if int(submission_lease_seconds) < 5 or int(submission_lease_seconds) > 300:
            raise ExecutionDispatchError(
                "dispatch_submission_stale", "submission lease is out of bounds"
            )
        self.submission_lease_seconds = int(submission_lease_seconds)

    # -- creation ---------------------------------------------------------

    def create_dispatch_intent(
        self, command: CreateDispatchIntentCommand
    ) -> DispatchIntentResult:
        command = self._normalize_create(command)
        prior = self._intent_for_key(command.dispatch_idempotency_key)
        if prior is not None:
            self._assert_requested_match(prior, command)
            return self._replay_result(prior)

        claim = (
            self.db.query(ExecutionTaskSchedulerClaim)
            .filter(ExecutionTaskSchedulerClaim.id == int(command.scheduler_claim_id))
            .with_for_update()
            .one_or_none()
        )
        if claim is None:
            raise ExecutionDispatchError(
                "scheduler_claim_not_found", "scheduler claim was not found"
            )

        existing_for_claim = self._intent_for_claim(claim.id)
        if existing_for_claim is not None:
            self._assert_requested_match(existing_for_claim, command)
            return self._replay_result(existing_for_claim)

        now = _utc(self._now()) or datetime.now(timezone.utc)
        self._assert_active_claim(claim, command, now)

        task = (
            self.db.query(ExecutionTask)
            .filter(ExecutionTask.id == int(command.execution_task_id))
            .with_for_update()
            .one_or_none()
        )
        if task is None:
            raise ExecutionDispatchError(
                "task_not_ready", "Execution Task was not found"
            )
        plan = (
            self.db.query(ExecutionPlan)
            .filter(ExecutionPlan.id == task.execution_plan_id)
            .with_for_update()
            .one_or_none()
        )
        if plan is None or claim.execution_plan_id != task.execution_plan_id:
            raise ExecutionDispatchError(
                "graph_integrity_failure",
                "claim, task, and plan identities do not match",
            )
        self._assert_plan_identity(plan)
        if plan.status != "active":
            raise ExecutionDispatchError(
                "execution_plan_inactive", "dispatch requires an active Execution Plan"
            )
        if task.status != "ready":
            raise ExecutionDispatchError(
                "task_not_ready", "dispatch requires a ready task"
            )
        if int(task.state_version) != int(command.expected_task_state_version):
            raise ExecutionDispatchError(
                "task_version_stale", "task lifecycle version changed before dispatch"
            )

        decision = self._evaluate_authority(task.id)
        if not decision.eligible:
            raise ExecutionDispatchError(
                "eligibility_decision_stale", "task is no longer eligible for dispatch"
            )
        if decision.decision_hash != command.expected_eligibility_decision_hash:
            raise ExecutionDispatchError(
                "eligibility_decision_stale",
                "eligibility decision changed before dispatch",
            )
        if decision.graph_hash != claim.claimed_graph_hash:
            raise ExecutionDispatchError(
                "graph_integrity_failure", "eligibility graph changed before dispatch"
            )
        predecessor_fences = _scheduler_predecessor_fences(decision)
        if (
            _scheduler_predecessor_fence_hash(predecessor_fences)
            != claim.predecessor_fence_hash
        ):
            raise ExecutionDispatchError(
                "eligibility_decision_stale",
                "predecessor fence changed before dispatch",
            )

        prior_task_intents = (
            self.db.query(ExecutionTaskDispatchIntent)
            .filter(ExecutionTaskDispatchIntent.execution_task_id == task.id)
            .order_by(ExecutionTaskDispatchIntent.id.asc())
            .all()
        )
        if any(
            intent.dispatch_status != DISPATCH_STATUS_CANCELLED
            for intent in prior_task_intents
        ):
            raise ExecutionDispatchError(
                "dispatch_intent_already_exists",
                "Execution Task already has a non-cancelled dispatch intent",
            )

        attempt_number = self._next_attempt_number(task.id)
        if prior_task_intents and int(task.state_version) <= max(
            int(intent.claimed_task_state_version) for intent in prior_task_intents
        ):
            raise ExecutionDispatchError(
                "runtime_attempt_conflict",
                "a cancelled attempt requires a newer reconciled task version",
            )
        payload = _command_payload(command, claim, plan, attempt_number)
        command_hash = canonical_json_hash(payload)
        command_id = _dispatch_command_id(task.id, attempt_number, command_hash)
        broker_id = _broker_task_id(task.id, attempt_number, command_hash)
        attempt_identity = _attempt_identity(task.id, attempt_number, command_hash)

        try:
            with self.db.begin_nested():
                intent = ExecutionTaskDispatchIntent(
                    execution_plan_id=plan.id,
                    execution_task_id=task.id,
                    scheduler_claim_id=claim.id,
                    scheduler_id=command.scheduler_id,
                    claim_fencing_token=int(command.claim_fencing_token),
                    claim_eligibility_decision_hash=decision.decision_hash,
                    claim_graph_hash=decision.graph_hash,
                    claim_predecessor_fence_hash=claim.predecessor_fence_hash,
                    claim_predecessor_fences=list(predecessor_fences),
                    claimed_task_state="ready",
                    claimed_task_state_version=int(task.state_version),
                    dispatch_idempotency_key=command.dispatch_idempotency_key,
                    dispatch_command_id=command_id,
                    canonical_command_payload=payload,
                    canonical_command_hash=command_hash,
                    worker_command_payload={},
                    worker_command_hash="0" * 64,
                    broker_task_id=broker_id,
                    dispatch_status=DISPATCH_STATUS_PENDING,
                    created_at=now,
                    submission_count=0,
                    submission_attempt_number=0,
                    submission_fencing_token=0,
                    creation_actor_type=command.creation_actor_type,
                    creation_actor_id=command.creation_actor_id or command.scheduler_id,
                    created_by_idempotency_key=command.dispatch_idempotency_key,
                    updated_at=now,
                )
                self.db.add(intent)
                self.db.flush()
                attempt = ExecutionTaskAttempt(
                    execution_plan_id=plan.id,
                    execution_task_id=task.id,
                    dispatch_intent_id=intent.id,
                    attempt_number=attempt_number,
                    attempt_identity=attempt_identity,
                    broker_task_id=broker_id,
                    attempt_status="created",
                    created_at=now,
                    updated_at=now,
                )
                self.db.add(attempt)
                self.db.flush()
                worker_payload = self._worker_payload(intent, attempt)
                intent.runtime_attempt_id = attempt.id
                intent.worker_command_payload = worker_payload
                intent.worker_command_hash = canonical_json_hash(worker_payload)
                consumed = self.db.execute(
                    update(ExecutionTaskSchedulerClaim)
                    .where(
                        ExecutionTaskSchedulerClaim.id == claim.id,
                        ExecutionTaskSchedulerClaim.claim_status == CLAIM_STATUS_ACTIVE,
                        ExecutionTaskSchedulerClaim.scheduler_id
                        == command.scheduler_id,
                        ExecutionTaskSchedulerClaim.fencing_token
                        == int(command.claim_fencing_token),
                        ExecutionTaskSchedulerClaim.expires_at > now,
                    )
                    .values(
                        claim_status=CLAIM_STATUS_CONSUMED,
                        consumed_at=now,
                        consumed_dispatch_intent_id=intent.id,
                        updated_at=now,
                    )
                    .execution_options(synchronize_session=False)
                )
                if consumed.rowcount != 1:
                    raise self._claim_race_error(claim.id)
                self.db.flush()
                self.db.expire(claim)
                self.db.refresh(claim)
        except ExecutionDispatchError:
            raise
        except IntegrityError as exc:
            current = self._intent_for_key(command.dispatch_idempotency_key)
            if current is not None:
                self._assert_requested_match(current, command)
                return self._replay_result(current)
            consumed_intent = self._intent_for_claim(claim.id)
            if consumed_intent is not None:
                self._assert_requested_match(consumed_intent, command)
                return self._replay_result(consumed_intent)
            raise ExecutionDispatchError(
                "runtime_attempt_conflict",
                "dispatch intent or attempt uniqueness conflicted",
            ) from exc
        except OperationalError as exc:
            self.db.rollback()
            current = self._intent_for_key(command.dispatch_idempotency_key)
            if current is not None:
                self._assert_requested_match(current, command)
                return self._replay_result(current)
            raise ExecutionDispatchError(
                "dispatch_intent_integrity_failure",
                "dispatch intent persistence encountered database contention",
            ) from exc
        except Exception as exc:
            raise ExecutionDispatchError(
                "dispatch_intent_integrity_failure",
                "dispatch intent transaction could not be completed",
            ) from exc
        return DispatchIntentResult(intent, attempt, replayed=False)

    # -- submission -------------------------------------------------------

    def submit_dispatch_intent(
        self,
        dispatch_intent_id: int,
        expected_status: str,
        submission_idempotency_key: str,
    ) -> DispatchSubmissionResult:
        key = _text(submission_idempotency_key, "submission_idempotency_key", 128)
        expected = _text(expected_status, "expected_status", 24)
        if expected not in DISPATCH_STATUSES:
            raise ExecutionDispatchError(
                "dispatch_intent_not_submittable", "expected dispatch status is invalid"
            )

        intent = (
            self.db.query(ExecutionTaskDispatchIntent)
            .filter(ExecutionTaskDispatchIntent.id == int(dispatch_intent_id))
            .with_for_update()
            .one_or_none()
        )
        if intent is None:
            raise ExecutionDispatchError(
                "dispatch_intent_not_found", "dispatch intent was not found"
            )
        integrity = self.verify_dispatch_intent_integrity(intent.id)
        if not integrity.verified:
            raise ExecutionDispatchError(
                "dispatch_intent_integrity_failure", "dispatch intent integrity failed"
            )
        attempt = self._attempt_for_intent(intent)
        if intent.dispatch_status == DISPATCH_STATUS_SUBMITTED:
            return DispatchSubmissionResult(
                intent, attempt, DISPATCH_STATUS_SUBMITTED, replayed=True
            )
        if intent.dispatch_status == DISPATCH_STATUS_CANCELLED:
            raise ExecutionDispatchError(
                "dispatch_intent_not_submittable",
                "cancelled dispatch intent cannot submit",
            )
        if intent.dispatch_status not in {expected, DISPATCH_STATUS_SUBMITTING}:
            raise ExecutionDispatchError(
                "dispatch_intent_not_submittable",
                "dispatch intent status is not submittable",
            )

        now = _utc(self._now()) or datetime.now(timezone.utc)
        lease_expiry = _utc(intent.submission_lease_expires_at)
        if (
            intent.dispatch_status == DISPATCH_STATUS_SUBMITTING
            and lease_expiry is not None
            and lease_expiry > now
        ):
            raise ExecutionDispatchError(
                "dispatch_submission_in_progress",
                "another submitter owns the dispatch lease",
            )

        reason = self._prestart_invalidation_reason(intent)
        if reason is not None:
            self._cancel_intent(intent, attempt, reason, now)
            self.db.commit()
            return DispatchSubmissionResult(
                intent,
                attempt,
                DISPATCH_STATUS_CANCELLED,
                replayed=False,
                error_code=reason,
            )

        token = int(intent.submission_fencing_token) + 1
        intent.dispatch_status = DISPATCH_STATUS_SUBMITTING
        intent.submission_started_at = now
        intent.failed_at = None
        intent.last_submission_error_code = None
        intent.last_submission_detail = None
        intent.submission_attempt_number = int(intent.submission_attempt_number) + 1
        intent.submission_count = int(intent.submission_count) + 1
        intent.submission_idempotency_key = key
        intent.submitter_id = self.submitter_id
        intent.submission_fencing_token = token
        intent.submission_lease_expires_at = now + timedelta(
            seconds=self.submission_lease_seconds
        )
        intent.updated_at = now
        self.db.flush()
        broker_task_id = intent.broker_task_id
        worker_payload = dict(intent.worker_command_payload)
        # The marker is committed before publication.  A crash after this
        # commit is recovered by the stale-submission reset below and replay
        # uses the same deterministic broker task id.
        self.db.commit()

        try:
            returned = self._publisher(
                broker_task_id,
                worker_payload,
                "app.tasks.worker.receive_execution_task_dispatch",
            )
            returned_id = getattr(
                returned, "id", returned if isinstance(returned, str) else None
            )
            if returned_id is not None and str(returned_id) != broker_task_id:
                raise RuntimeError("broker returned a non-deterministic task id")
        except Exception as exc:
            self._record_submission_failure(
                intent.id,
                token,
                key,
                "broker_submission_error",
                _safe_detail(exc),
            )
            current = self.db.get(ExecutionTaskDispatchIntent, intent.id)
            current_attempt = self._attempt_for_intent(current)
            return DispatchSubmissionResult(
                current,
                current_attempt,
                DISPATCH_STATUS_FAILED,
                replayed=False,
                error_code="broker_submission_error",
            )

        current = self.db.get(ExecutionTaskDispatchIntent, intent.id)
        if (
            current is None
            or current.dispatch_status != DISPATCH_STATUS_SUBMITTING
            or int(current.submission_fencing_token) != token
            or current.submitter_id != self.submitter_id
        ):
            raise ExecutionDispatchError(
                "dispatch_submission_stale",
                "submitter no longer owns the dispatch lease",
            )
        submitted_at = _utc(self._now()) or datetime.now(timezone.utc)
        current.dispatch_status = DISPATCH_STATUS_SUBMITTED
        current.submitted_at = submitted_at
        current.acknowledged_at = submitted_at
        current.broker_returned_task_id = current.broker_task_id
        current.submission_lease_expires_at = None
        current.last_submission_error_code = None
        current.last_submission_detail = None
        current.updated_at = submitted_at
        current_attempt = self._attempt_for_intent(current)
        current_attempt.attempt_status = "submitted"
        current_attempt.submitted_at = submitted_at
        current_attempt.updated_at = submitted_at
        self.db.commit()
        return DispatchSubmissionResult(
            current, current_attempt, DISPATCH_STATUS_SUBMITTED, replayed=False
        )

    def recover_stale_submission_intents(self) -> int:
        """Reset only expired publication ownership; never create a new attempt."""

        now = _utc(self._now()) or datetime.now(timezone.utc)
        intents = (
            self.db.query(ExecutionTaskDispatchIntent)
            .filter(
                ExecutionTaskDispatchIntent.dispatch_status
                == DISPATCH_STATUS_SUBMITTING,
                ExecutionTaskDispatchIntent.submission_lease_expires_at <= now,
            )
            .with_for_update()
            .all()
        )
        for intent in intents:
            intent.dispatch_status = DISPATCH_STATUS_PENDING
            intent.submitter_id = None
            intent.submission_lease_expires_at = None
            intent.last_submission_error_code = "dispatch_submission_stale"
            intent.last_submission_detail = (
                "publication lease expired; safe replay remains available"
            )
            intent.updated_at = now
        self.db.commit()
        return len(intents)

    # -- worker-entry boundary -------------------------------------------

    def validate_worker_entry(
        self, payload: Mapping[str, object], broker_task_id: str
    ) -> WorkerEntryResult:
        if not isinstance(payload, Mapping):
            raise ExecutionDispatchError(
                "dispatch_intent_integrity_failure",
                "worker dispatch payload is invalid",
            )
        try:
            intent_id = int(payload["dispatch_intent_id"])
            attempt_id = int(payload["runtime_attempt_id"])
            task_id = int(payload["execution_task_id"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ExecutionDispatchError(
                "dispatch_intent_integrity_failure",
                "worker dispatch identity is invalid",
            ) from exc
        intent = self.db.get(ExecutionTaskDispatchIntent, intent_id)
        if intent is None:
            raise ExecutionDispatchError(
                "dispatch_intent_not_found",
                "worker referenced an unknown dispatch intent",
            )
        integrity = self.verify_dispatch_intent_integrity(intent_id)
        if not integrity.verified:
            raise ExecutionDispatchError(
                "dispatch_intent_integrity_failure",
                "worker dispatch authority is invalid",
            )
        attempt = self._attempt_for_intent(intent)
        if intent.dispatch_status == DISPATCH_STATUS_CANCELLED:
            raise ExecutionDispatchError(
                "dispatch_intent_not_submittable",
                "worker referenced a cancelled dispatch intent",
            )
        if (
            attempt.id != attempt_id
            or intent.execution_task_id != task_id
            or intent.broker_task_id != str(broker_task_id)
            or dict(payload) != dict(intent.worker_command_payload)
        ):
            raise ExecutionDispatchError(
                "dispatch_intent_integrity_failure",
                "worker dispatch identity does not match authority",
            )
        return WorkerEntryResult(
            dispatch_intent_id=intent.id,
            runtime_attempt_id=attempt.id,
            execution_task_id=task_id,
            broker_task_id=intent.broker_task_id,
            duplicate_delivery=intent.dispatch_status == DISPATCH_STATUS_SUBMITTED,
        )

    # -- integrity --------------------------------------------------------

    def verify_dispatch_intent_integrity(
        self, dispatch_intent_id: int
    ) -> DispatchIntegrityResult:
        intent = self.db.get(ExecutionTaskDispatchIntent, int(dispatch_intent_id))
        if intent is None:
            raise ExecutionDispatchError(
                "dispatch_intent_not_found", "dispatch intent was not found"
            )
        issues: list[str] = []
        task = self.db.get(ExecutionTask, intent.execution_task_id)
        plan = self.db.get(ExecutionPlan, intent.execution_plan_id)
        claim = self.db.get(ExecutionTaskSchedulerClaim, intent.scheduler_claim_id)
        attempt = self._attempt_for_intent(intent, required=False)
        if task is None or plan is None:
            issues.append("task_plan_missing")
        if claim is None:
            issues.append("claim_missing")
        if attempt is None:
            issues.append("attempt_missing")
        if plan is not None and task is not None:
            if task.execution_plan_id != plan.id:
                issues.append("task_plan_mismatch")
            project = self.db.get(Project, plan.project_id)
            session = self.db.get(PlanningSession, plan.planning_session_id)
            if (
                project is None
                or session is None
                or session.project_id != plan.project_id
            ):
                issues.append("project_session_mismatch")
        if claim is not None:
            if (
                claim.execution_task_id != intent.execution_task_id
                or claim.execution_plan_id != intent.execution_plan_id
                or claim.scheduler_id != intent.scheduler_id
                or int(claim.fencing_token) != int(intent.claim_fencing_token)
                or claim.claim_status != CLAIM_STATUS_CONSUMED
                or claim.consumed_dispatch_intent_id != intent.id
            ):
                issues.append("claim_intent_mismatch")
            if self._active_claims_for_task(intent.execution_task_id):
                issues.append("active_claim_for_consumed_intent")
        if not _valid_hash(intent.canonical_command_hash):
            issues.append("malformed_command_hash")
        elif not isinstance(intent.canonical_command_payload, dict):
            issues.append("malformed_command_payload")
        elif (
            canonical_json_hash(intent.canonical_command_payload)
            != intent.canonical_command_hash
        ):
            issues.append("command_hash_mismatch")
        elif (
            intent.canonical_command_payload.get("execution_plan_id")
            != intent.execution_plan_id
            or intent.canonical_command_payload.get("execution_task_id")
            != intent.execution_task_id
            or intent.canonical_command_payload.get("scheduler_claim_id")
            != intent.scheduler_claim_id
            or intent.canonical_command_payload.get("scheduler_id")
            != intent.scheduler_id
            or intent.canonical_command_payload.get("claim_fencing_token")
            != intent.claim_fencing_token
            or intent.canonical_command_payload.get("expected_task_state")
            != intent.claimed_task_state
            or intent.canonical_command_payload.get("expected_task_state_version")
            != intent.claimed_task_state_version
            or intent.canonical_command_payload.get("claim_eligibility_decision_hash")
            != intent.claim_eligibility_decision_hash
            or intent.canonical_command_payload.get("dispatch_idempotency_key")
            != intent.dispatch_idempotency_key
        ):
            issues.append("command_authority_binding_mismatch")
        if not isinstance(intent.claim_predecessor_fences, list) or not _valid_hash(
            intent.claim_predecessor_fence_hash
        ):
            issues.append("malformed_predecessor_fence_evidence")
        elif (
            _scheduler_predecessor_fence_hash(tuple(intent.claim_predecessor_fences))
            != intent.claim_predecessor_fence_hash
        ):
            issues.append("predecessor_fence_hash_mismatch")
        if not _valid_hash(intent.worker_command_hash) or not isinstance(
            intent.worker_command_payload, dict
        ):
            issues.append("malformed_worker_command")
        elif (
            canonical_json_hash(intent.worker_command_payload)
            != intent.worker_command_hash
        ):
            issues.append("worker_command_hash_mismatch")
        if task is not None and attempt is not None:
            if (
                attempt.execution_task_id != task.id
                or attempt.execution_plan_id != intent.execution_plan_id
                or attempt.dispatch_intent_id != intent.id
                or intent.runtime_attempt_id != attempt.id
                or attempt.broker_task_id != intent.broker_task_id
                or attempt.attempt_number <= 0
            ):
                issues.append("attempt_identity_mismatch")
            expected_identity = _attempt_identity(
                task.id, attempt.attempt_number, intent.canonical_command_hash
            )
            expected_broker = _broker_task_id(
                task.id, attempt.attempt_number, intent.canonical_command_hash
            )
            expected_command_id = _dispatch_command_id(
                task.id, attempt.attempt_number, intent.canonical_command_hash
            )
            if attempt.attempt_identity != expected_identity:
                issues.append("attempt_identity_tampered")
            if intent.broker_task_id != expected_broker:
                issues.append("broker_task_id_tampered")
            if intent.dispatch_command_id != expected_command_id:
                issues.append("dispatch_command_id_tampered")
            if isinstance(
                intent.worker_command_payload, dict
            ) and intent.worker_command_payload != self._worker_payload(
                intent, attempt
            ):
                issues.append("worker_command_binding_mismatch")
        if intent.dispatch_status not in DISPATCH_STATUSES:
            issues.append("invalid_dispatch_status")
        if intent.dispatch_status == DISPATCH_STATUS_SUBMITTED:
            if (
                intent.submitted_at is None
                or intent.acknowledged_at is None
                or intent.broker_returned_task_id != intent.broker_task_id
            ):
                issues.append("submitted_evidence_missing")
        if intent.dispatch_status == DISPATCH_STATUS_FAILED:
            if intent.failed_at is None or not intent.last_submission_error_code:
                issues.append("failed_evidence_missing")
        if intent.dispatch_status == DISPATCH_STATUS_CANCELLED:
            if (
                intent.cancelled_at is None
                or intent.cancellation_reason not in PRESTART_CANCELLATION_REASONS
            ):
                issues.append("cancellation_evidence_missing")
        if intent.submission_count < 0 or intent.submission_attempt_number < 0:
            issues.append("submission_counts_invalid")
        if intent.submission_fencing_token < 0:
            issues.append("submission_fence_invalid")
        timestamps = [
            _utc(intent.created_at),
            _utc(intent.submission_started_at),
            _utc(intent.submitted_at),
            _utc(intent.acknowledged_at),
            _utc(intent.failed_at),
            _utc(intent.cancelled_at),
        ]
        present = [value for value in timestamps if value is not None]
        if present and present != sorted(present):
            issues.append("timestamp_ordering_invalid")
        return DispatchIntegrityResult(
            dispatch_intent_id=intent.id,
            execution_task_id=intent.execution_task_id,
            execution_plan_id=intent.execution_plan_id,
            attempt_id=attempt.id if attempt is not None else None,
            verified=not issues,
            issues=tuple(sorted(set(issues))),
        )

    def verify_execution_task_dispatch_integrity(
        self, execution_task_id: int
    ) -> tuple[DispatchIntegrityResult, ...]:
        intents = (
            self.db.query(ExecutionTaskDispatchIntent)
            .filter(
                ExecutionTaskDispatchIntent.execution_task_id == int(execution_task_id)
            )
            .order_by(ExecutionTaskDispatchIntent.id.asc())
            .all()
        )
        results = tuple(
            self.verify_dispatch_intent_integrity(intent.id) for intent in intents
        )
        attempt_without_intent = (
            self.db.query(ExecutionTaskAttempt)
            .filter(ExecutionTaskAttempt.execution_task_id == int(execution_task_id))
            .all()
        )
        known = {result.attempt_id for result in results}
        if any(attempt.id not in known for attempt in attempt_without_intent):
            return results + (
                DispatchIntegrityResult(
                    dispatch_intent_id=0,
                    execution_task_id=int(execution_task_id),
                    execution_plan_id=0,
                    attempt_id=None,
                    verified=False,
                    issues=("attempt_without_dispatch_intent",),
                ),
            )
        return results

    def verify_execution_plan_dispatch_integrity(
        self, execution_plan_id: int
    ) -> tuple[DispatchIntegrityResult, ...]:
        intents = (
            self.db.query(ExecutionTaskDispatchIntent)
            .filter(
                ExecutionTaskDispatchIntent.execution_plan_id == int(execution_plan_id)
            )
            .order_by(ExecutionTaskDispatchIntent.id.asc())
            .all()
        )
        results = [
            self.verify_dispatch_intent_integrity(intent.id) for intent in intents
        ]
        task_ids = {
            task_id
            for (task_id,) in self.db.query(ExecutionTask.id)
            .filter(ExecutionTask.execution_plan_id == int(execution_plan_id))
            .all()
        }
        for task_id in sorted(task_ids):
            results.extend(self.verify_execution_task_dispatch_integrity(task_id))
        orphan_attempts = (
            self.db.query(ExecutionTaskAttempt)
            .filter(ExecutionTaskAttempt.execution_plan_id == int(execution_plan_id))
            .all()
        )
        if any(
            attempt.execution_task_id not in task_ids for attempt in orphan_attempts
        ):
            results.append(
                DispatchIntegrityResult(
                    dispatch_intent_id=0,
                    execution_task_id=0,
                    execution_plan_id=int(execution_plan_id),
                    attempt_id=None,
                    verified=False,
                    issues=("attempt_plan_mismatch",),
                )
            )
        return tuple(results)

    # -- internal helpers -------------------------------------------------

    def _normalize_create(
        self, command: CreateDispatchIntentCommand
    ) -> CreateDispatchIntentCommand:
        if not isinstance(command, CreateDispatchIntentCommand):
            raise ExecutionDispatchError(
                "dispatch_intent_integrity_failure",
                "dispatch intent command is invalid",
            )
        try:
            task_id = int(command.execution_task_id)
            claim_id = int(command.scheduler_claim_id)
            fence = int(command.claim_fencing_token)
            version = int(command.expected_task_state_version)
        except (TypeError, ValueError) as exc:
            raise ExecutionDispatchError(
                "dispatch_intent_integrity_failure",
                "dispatch intent identifiers are invalid",
            ) from exc
        if task_id <= 0 or claim_id <= 0 or fence <= 0 or version < 0:
            raise ExecutionDispatchError(
                "dispatch_intent_integrity_failure",
                "dispatch intent identifiers are invalid",
            )
        if command.expected_task_state != "ready":
            raise ExecutionDispatchError(
                "task_not_ready", "dispatch requires expected_task_state='ready'"
            )
        return CreateDispatchIntentCommand(
            execution_task_id=task_id,
            scheduler_claim_id=claim_id,
            scheduler_id=_text(command.scheduler_id, "scheduler_id", 255),
            claim_fencing_token=fence,
            expected_task_state="ready",
            expected_task_state_version=version,
            expected_eligibility_decision_hash=_hash(
                command.expected_eligibility_decision_hash,
                "expected_eligibility_decision_hash",
            ),
            dispatch_idempotency_key=_text(
                command.dispatch_idempotency_key, "dispatch_idempotency_key", 128
            ),
            creation_actor_type=_text(
                command.creation_actor_type, "creation_actor_type", 32
            ),
            creation_actor_id=(
                _text(command.creation_actor_id, "creation_actor_id", 255)
                if command.creation_actor_id is not None
                else None
            ),
        )

    def _assert_active_claim(
        self,
        claim: ExecutionTaskSchedulerClaim,
        command: CreateDispatchIntentCommand,
        now: datetime,
    ) -> None:
        if claim.claim_status == CLAIM_STATUS_CONSUMED:
            raise ExecutionDispatchError(
                "scheduler_claim_already_consumed",
                "scheduler claim was already consumed",
            )
        if claim.claim_status != CLAIM_STATUS_ACTIVE:
            code = (
                "scheduler_claim_expired"
                if claim.claim_status == "expired"
                else "scheduler_claim_not_active"
            )
            raise ExecutionDispatchError(code, "scheduler claim is not active")
        if claim.scheduler_id != command.scheduler_id:
            raise ExecutionDispatchError(
                "scheduler_claim_owner_mismatch", "scheduler does not own the claim"
            )
        if int(claim.fencing_token) != int(command.claim_fencing_token):
            raise ExecutionDispatchError(
                "scheduler_claim_fence_stale", "scheduler claim fencing token is stale"
            )
        if _utc(claim.expires_at) is None or _utc(claim.expires_at) <= now:
            raise ExecutionDispatchError(
                "scheduler_claim_expired", "scheduler claim lease has expired"
            )
        if claim.execution_task_id != int(command.execution_task_id):
            raise ExecutionDispatchError(
                "dispatch_intent_integrity_failure",
                "claim does not belong to the requested task",
            )

    def _claim_race_error(self, claim_id: int) -> ExecutionDispatchError:
        claim = self.db.get(ExecutionTaskSchedulerClaim, claim_id)
        if claim is not None and claim.claim_status == CLAIM_STATUS_CONSUMED:
            return ExecutionDispatchError(
                "scheduler_claim_already_consumed",
                "scheduler claim changed before consumption",
            )
        return ExecutionDispatchError(
            "scheduler_claim_expired",
            "scheduler claim changed or expired before consumption",
        )

    def _assert_plan_identity(self, plan: ExecutionPlan) -> None:
        project = self.db.get(Project, plan.project_id)
        session = self.db.get(PlanningSession, plan.planning_session_id)
        if project is None or session is None or session.project_id != plan.project_id:
            raise ExecutionDispatchError(
                "graph_integrity_failure",
                "Execution Plan project/session identity is inconsistent",
            )

    def _evaluate_authority(self, task_id: int):
        try:
            ExecutionTaskTransitionService(self.db).verify_task_lifecycle_integrity(
                task_id
            )
            decision = ExecutionEligibilityService(self.db).evaluate_task(task_id)
        except ExecutionTaskTransitionError as exc:
            raise ExecutionDispatchError(
                "lifecycle_integrity_failure",
                "Execution Task lifecycle integrity failed",
            ) from exc
        except ExecutionEligibilityError as exc:
            code = (
                "graph_integrity_failure"
                if getattr(exc, "code", "") == "graph_integrity_failure"
                else "eligibility_decision_stale"
            )
            raise ExecutionDispatchError(
                code, "Execution Task eligibility could not be verified"
            ) from exc
        if decision.reason_code in {
            "graph_integrity_failure",
            "lifecycle_integrity_failure",
            "unknown_dependency_type",
            "unknown_gate_type",
        }:
            raise ExecutionDispatchError(
                "graph_integrity_failure",
                "Execution Plan graph or lifecycle authority is not intact",
            )
        return decision

    def _prestart_invalidation_reason(
        self, intent: ExecutionTaskDispatchIntent
    ) -> str | None:
        task = self.db.get(ExecutionTask, intent.execution_task_id)
        plan = self.db.get(ExecutionPlan, intent.execution_plan_id)
        if task is None or plan is None:
            return "graph_integrity_failure"
        if plan.status != "active":
            return "plan_inactive"
        if task.status != "ready":
            return "task_no_longer_ready"
        if int(task.state_version) != int(intent.claimed_task_state_version):
            return "task_version_changed"
        try:
            decision = self._evaluate_authority(task.id)
        except ExecutionDispatchError as exc:
            return (
                exc.code
                if exc.code in PRESTART_CANCELLATION_REASONS
                else "graph_integrity_failure"
            )
        if (
            decision.decision_hash != intent.claim_eligibility_decision_hash
            or decision.graph_hash != intent.claim_graph_hash
            or _scheduler_predecessor_fence_hash(
                _scheduler_predecessor_fences(decision)
            )
            != intent.claim_predecessor_fence_hash
        ):
            return "eligibility_changed"
        return None

    def _cancel_intent(
        self,
        intent: ExecutionTaskDispatchIntent,
        attempt: ExecutionTaskAttempt,
        reason: str,
        now: datetime,
    ) -> None:
        if reason not in PRESTART_CANCELLATION_REASONS:
            reason = "graph_integrity_failure"
        intent.dispatch_status = DISPATCH_STATUS_CANCELLED
        intent.cancelled_at = now
        intent.cancellation_reason = reason
        intent.submission_lease_expires_at = None
        intent.submitter_id = None
        intent.updated_at = now
        attempt.attempt_status = "cancelled"
        attempt.cancelled_at = now
        attempt.updated_at = now

    def _record_submission_failure(
        self,
        intent_id: int,
        token: int,
        key: str,
        code: str,
        detail: str,
    ) -> None:
        current = self.db.get(ExecutionTaskDispatchIntent, int(intent_id))
        if (
            current is None
            or current.dispatch_status != DISPATCH_STATUS_SUBMITTING
            or int(current.submission_fencing_token) != int(token)
            or current.submission_idempotency_key != key
        ):
            raise ExecutionDispatchError(
                "dispatch_submission_stale",
                "submission failure cannot overwrite a newer owner",
            )
        now = _utc(self._now()) or datetime.now(timezone.utc)
        current.dispatch_status = DISPATCH_STATUS_FAILED
        current.failed_at = now
        current.last_submission_error_code = _text(code, "error_code", 64)
        current.last_submission_detail = _safe_detail(detail)
        current.submission_lease_expires_at = None
        current.submitter_id = None
        current.updated_at = now
        self.db.commit()

    def _worker_payload(
        self, intent: ExecutionTaskDispatchIntent, attempt: ExecutionTaskAttempt
    ) -> dict[str, object]:
        return {
            "schema_version": WORKER_COMMAND_SCHEMA_VERSION,
            "dispatch_intent_id": int(intent.id),
            "runtime_attempt_id": int(attempt.id),
            "execution_plan_id": int(intent.execution_plan_id),
            "execution_task_id": int(intent.execution_task_id),
            "broker_task_id": intent.broker_task_id,
            "attempt_number": int(attempt.attempt_number),
            "authority_hash": intent.canonical_command_hash,
            "claim_fencing_token": int(intent.claim_fencing_token),
        }

    @staticmethod
    def _publish_to_celery(
        broker_task_id: str, payload: Mapping[str, object], task_name: str
    ) -> object:
        from app.celery_app import celery_app

        return celery_app.send_task(
            task_name,
            args=[dict(payload)],
            task_id=broker_task_id,
            queue="celery",
        )

    def _next_attempt_number(self, execution_task_id: int) -> int:
        latest = (
            self.db.query(func.max(ExecutionTaskAttempt.attempt_number))
            .filter(ExecutionTaskAttempt.execution_task_id == int(execution_task_id))
            .scalar()
        )
        return int(latest or 0) + 1

    def _attempt_for_intent(
        self, intent: ExecutionTaskDispatchIntent, *, required: bool = True
    ) -> ExecutionTaskAttempt | None:
        attempt = (
            self.db.query(ExecutionTaskAttempt)
            .filter(ExecutionTaskAttempt.dispatch_intent_id == intent.id)
            .one_or_none()
        )
        if attempt is None and required:
            raise ExecutionDispatchError(
                "runtime_attempt_integrity_failure",
                "dispatch intent has no canonical attempt",
            )
        return attempt

    def _intent_for_key(self, key: str) -> ExecutionTaskDispatchIntent | None:
        return (
            self.db.query(ExecutionTaskDispatchIntent)
            .filter(ExecutionTaskDispatchIntent.dispatch_idempotency_key == key)
            .one_or_none()
        )

    def _intent_for_claim(self, claim_id: int) -> ExecutionTaskDispatchIntent | None:
        return (
            self.db.query(ExecutionTaskDispatchIntent)
            .filter(ExecutionTaskDispatchIntent.scheduler_claim_id == int(claim_id))
            .one_or_none()
        )

    def _replay_result(
        self, intent: ExecutionTaskDispatchIntent
    ) -> DispatchIntentResult:
        integrity = self.verify_dispatch_intent_integrity(intent.id)
        if not integrity.verified:
            raise ExecutionDispatchError(
                "dispatch_intent_integrity_failure",
                "persisted dispatch intent failed integrity verification",
            )
        return DispatchIntentResult(
            intent, self._attempt_for_intent(intent), replayed=True
        )

    def _assert_requested_match(
        self, intent: ExecutionTaskDispatchIntent, command: CreateDispatchIntentCommand
    ) -> None:
        payload = intent.canonical_command_payload
        if not isinstance(payload, dict):
            raise ExecutionDispatchError(
                "dispatch_intent_integrity_failure",
                "persisted dispatch command payload is malformed",
            )
        candidate = _requested_fields(payload)
        requested = (
            command.execution_task_id,
            command.scheduler_claim_id,
            command.scheduler_id,
            command.claim_fencing_token,
            command.expected_task_state,
            command.expected_task_state_version,
            command.expected_eligibility_decision_hash,
            command.dispatch_idempotency_key,
            command.creation_actor_type,
            command.creation_actor_id,
        )
        if candidate != requested:
            raise ExecutionDispatchError(
                "dispatch_idempotency_conflict",
                "dispatch idempotency key is bound to another command",
            )

    def _active_claims_for_task(
        self, task_id: int
    ) -> list[ExecutionTaskSchedulerClaim]:
        now = _utc(self._now()) or datetime.now(timezone.utc)
        return (
            self.db.query(ExecutionTaskSchedulerClaim)
            .filter(
                ExecutionTaskSchedulerClaim.execution_task_id == int(task_id),
                ExecutionTaskSchedulerClaim.claim_status == CLAIM_STATUS_ACTIVE,
                ExecutionTaskSchedulerClaim.expires_at > now,
            )
            .all()
        )


def _valid_hash(value: object) -> bool:
    return bool(_HASH_RE.fullmatch(str(value or "").lower()))


__all__ = [
    "CreateDispatchIntentCommand",
    "DISPATCH_STATUS_CANCELLED",
    "DISPATCH_STATUS_FAILED",
    "DISPATCH_STATUS_PENDING",
    "DISPATCH_STATUS_SUBMITTED",
    "DISPATCH_STATUS_SUBMITTING",
    "DispatchIntegrityResult",
    "DispatchIntentResult",
    "DispatchSubmissionResult",
    "ExecutionDispatchError",
    "ExecutionTaskDispatchService",
    "WorkerEntryResult",
]
