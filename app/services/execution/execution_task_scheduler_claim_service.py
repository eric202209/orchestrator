"""Ready-task selection and the durable scheduler claim boundary.

This module intentionally stops at scheduler ownership.  A successful claim
does not change ``ExecutionTask.status``, create ``TaskExecution``, allocate a
workspace, or call Celery.  Database uniqueness and the persisted fencing
token are the authority; Redis capacity signals and process timers are not.
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
    ExecutionTaskSchedulerClaim,
    PlanningSession,
    Project,
)
from app.services.execution.execution_eligibility_service import (
    ExecutionEligibilityDecision,
    ExecutionEligibilityError,
    ExecutionEligibilityService,
)
from app.services.execution.execution_task_transition_service import (
    ExecutionTaskTransitionError,
    ExecutionTaskTransitionService,
)
from app.services.planning.operator_review import canonical_json_hash


EXECUTION_SCHEDULER_CLAIM_SCHEMA_VERSION = "execution-scheduler-claim/1.0"
CLAIM_STATUS_ACTIVE = "active"
CLAIM_STATUS_RELEASED = "released"
CLAIM_STATUS_EXPIRED = "expired"
CLAIM_STATUS_CONSUMED = "consumed"
CLAIM_STATUSES = frozenset(
    {
        CLAIM_STATUS_ACTIVE,
        CLAIM_STATUS_RELEASED,
        CLAIM_STATUS_EXPIRED,
        CLAIM_STATUS_CONSUMED,
    }
)
MIN_CLAIM_LEASE_SECONDS = 5
DEFAULT_CLAIM_LEASE_SECONDS = 60
MAX_CLAIM_LEASE_SECONDS = 300
DEFAULT_SELECTION_LIMIT = 25
MAX_SELECTION_LIMIT = 1000
RELEASE_REASON_CODES = frozenset(
    {
        "dispatch_not_attempted",
        "scheduler_shutdown",
        "selection_abandoned",
        "task_no_longer_ready",
        "plan_inactive",
        "eligibility_changed",
        "operator_intervention",
        "claim_expired",
    }
)
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ExecutionSchedulerClaimError(Exception):
    """Bounded domain error for selection, claim, and release commands."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


# Friendly alias for callers that use the model's longer name.
ExecutionTaskSchedulerClaimError = ExecutionSchedulerClaimError


@dataclass(frozen=True)
class ReadyTaskSelectionScope:
    execution_plan_id: int | None = None
    project_id: int | None = None
    limit: int | None = None


@dataclass(frozen=True)
class ReadyTaskSelectionExclusion:
    execution_task_id: int
    execution_plan_id: int | None
    code: str


@dataclass(frozen=True)
class ReadyTaskCandidate:
    execution_plan_id: int
    execution_task_id: int
    project_id: int
    planning_session_id: int
    plan_generation: int
    plan_created_at: datetime | None
    plan_task_id: str
    task_state_version: int
    decision_hash: str
    graph_hash: str
    predecessor_fence_hash: str
    predecessor_fences: tuple[dict[str, object], ...]
    decision: ExecutionEligibilityDecision


@dataclass(frozen=True)
class ReadyTaskCandidateSet:
    candidates: tuple[ReadyTaskCandidate, ...]
    exclusions: tuple[ReadyTaskSelectionExclusion, ...] = ()

    @property
    def task_ids(self) -> tuple[int, ...]:
        return tuple(candidate.execution_task_id for candidate in self.candidates)


@dataclass(frozen=True)
class AcquireSchedulerClaimCommand:
    execution_task_id: int
    expected_task_state: str
    expected_task_state_version: int
    expected_eligibility_decision_hash: str
    scheduler_id: str
    idempotency_key: str
    lease_duration_seconds: int = DEFAULT_CLAIM_LEASE_SECONDS
    expected_graph_hash: str | None = None
    expected_predecessor_fence_hash: str | None = None


@dataclass(frozen=True)
class SchedulerClaimResult:
    claim: ExecutionTaskSchedulerClaim
    replayed: bool = False

    @property
    def claim_id(self) -> int:
        return self.claim.id

    @property
    def execution_task_id(self) -> int:
        return self.claim.execution_task_id

    @property
    def fencing_token(self) -> int:
        return self.claim.fencing_token


@dataclass(frozen=True)
class ReleaseSchedulerClaimResult:
    claim: ExecutionTaskSchedulerClaim
    replayed: bool = False


@dataclass(frozen=True)
class SelectAndClaimResult:
    code: str
    claim_result: SchedulerClaimResult | None
    candidates_considered: int
    skipped_task_ids: tuple[int, ...] = ()

    @property
    def claim(self) -> ExecutionTaskSchedulerClaim | None:
        return self.claim_result.claim if self.claim_result else None


@dataclass(frozen=True)
class SchedulerClaimIntegrityResult:
    execution_task_id: int
    execution_plan_id: int
    claim_count: int
    active_claim_count: int
    issues: tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        return not self.issues


@dataclass(frozen=True)
class SchedulerPlanClaimIntegrityResult:
    execution_plan_id: int
    task_results: tuple[SchedulerClaimIntegrityResult, ...]
    issues: tuple[str, ...] = ()

    @property
    def verified(self) -> bool:
        return not self.issues and all(result.verified for result in self.task_results)


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _text(value: object, field: str, limit: int) -> str:
    result = str(value or "").strip()
    if not result or len(result) > limit or _CONTROL_RE.search(result):
        raise ExecutionSchedulerClaimError(
            "invalid_claim_command", f"{field} is missing, malformed, or too long"
        )
    return result


def _hash(value: object, field: str) -> str:
    result = str(value or "").strip().lower()
    if not _HASH_RE.fullmatch(result):
        raise ExecutionSchedulerClaimError(
            "eligibility_decision_stale", f"{field} is not a canonical SHA-256 hash"
        )
    return result


def _valid_hash(value: object) -> bool:
    return bool(_HASH_RE.fullmatch(str(value or "").lower()))


def _scope(
    scope: ReadyTaskSelectionScope | Mapping[str, object] | None
) -> ReadyTaskSelectionScope:
    if scope is None:
        return ReadyTaskSelectionScope(limit=DEFAULT_SELECTION_LIMIT)
    if isinstance(scope, ReadyTaskSelectionScope):
        result = scope
    elif isinstance(scope, Mapping):
        result = ReadyTaskSelectionScope(
            execution_plan_id=scope.get("execution_plan_id"),
            project_id=scope.get("project_id"),
            limit=scope.get("limit"),
        )
    else:
        raise ExecutionSchedulerClaimError(
            "invalid_claim_command", "selection scope is invalid"
        )
    if result.execution_plan_id is not None and int(result.execution_plan_id) <= 0:
        raise ExecutionSchedulerClaimError(
            "invalid_claim_command", "execution_plan_id is invalid"
        )
    if result.project_id is not None and int(result.project_id) <= 0:
        raise ExecutionSchedulerClaimError(
            "invalid_claim_command", "project_id is invalid"
        )
    if result.limit is not None:
        try:
            limit = int(result.limit)
        except (TypeError, ValueError) as exc:
            raise ExecutionSchedulerClaimError(
                "invalid_claim_command", "selection limit is invalid"
            ) from exc
        if limit <= 0 or limit > MAX_SELECTION_LIMIT:
            raise ExecutionSchedulerClaimError(
                "invalid_claim_command", "selection limit is out of bounds"
            )
        result = ReadyTaskSelectionScope(
            execution_plan_id=result.execution_plan_id,
            project_id=result.project_id,
            limit=limit,
        )
    return result


def _predecessor_fences(
    decision: ExecutionEligibilityDecision,
) -> tuple[dict[str, object], ...]:
    fences = []
    for result in decision.dependency_results:
        if result.predecessor_state is None:
            continue
        fences.append(
            {
                "execution_task_id": result.prerequisite_execution_task_id,
                "plan_task_id": result.prerequisite_plan_task_id,
                "expected_state": result.predecessor_state,
                "expected_state_version": result.predecessor_state_version,
                "lifecycle_head_hash": result.predecessor_lifecycle_head_hash,
            }
        )
    return tuple(fences)


def _predecessor_fence_hash(fences: tuple[dict[str, object], ...]) -> str:
    return canonical_json_hash(
        {
            "schema_version": EXECUTION_SCHEDULER_CLAIM_SCHEMA_VERSION,
            "predecessor_fences": list(fences),
        }
    )


class ExecutionReadyTaskSelectionService:
    """Read-only deterministic projection of tasks safe to consider next."""

    def __init__(self, db: Session, *, now: Callable[[], datetime] | None = None):
        self.db = db
        self._now = now or (lambda: datetime.now(timezone.utc))

    def list_ready_candidates(
        self,
        execution_plan_id: int | None = None,
        project_id: int | None = None,
        limit: int | None = None,
    ) -> ReadyTaskCandidateSet:
        if limit is not None:
            scope = _scope(
                ReadyTaskSelectionScope(
                    execution_plan_id=execution_plan_id,
                    project_id=project_id,
                    limit=limit,
                )
            )
        else:
            scope = ReadyTaskSelectionScope(
                execution_plan_id=execution_plan_id, project_id=project_id
            )
        query = (
            self.db.query(ExecutionTask, ExecutionPlan)
            .join(ExecutionPlan, ExecutionPlan.id == ExecutionTask.execution_plan_id)
            .filter(
                ExecutionPlan.status == "active",
                ExecutionTask.status == "ready",
            )
        )
        if scope.execution_plan_id is not None:
            query = query.filter(ExecutionPlan.id == int(scope.execution_plan_id))
        if scope.project_id is not None:
            query = query.filter(ExecutionPlan.project_id == int(scope.project_id))
        rows = query.all()
        rows.sort(key=lambda pair: self._ordering_key(pair[0], pair[1]))

        candidates: list[ReadyTaskCandidate] = []
        exclusions: list[ReadyTaskSelectionExclusion] = []
        claim_service = ExecutionTaskSchedulerClaimService(self.db, now=self._now)
        eligibility = ExecutionEligibilityService(self.db)
        lifecycle = ExecutionTaskTransitionService(self.db)
        now = _utc(self._now())
        for task, plan in rows:
            code = self._candidate_exclusion(
                task, plan, now, claim_service, eligibility, lifecycle
            )
            if code is not None:
                exclusions.append(ReadyTaskSelectionExclusion(task.id, plan.id, code))
                continue
            try:
                decision = eligibility.evaluate_task(task.id)
            except ExecutionEligibilityError as exc:
                exclusions.append(
                    ReadyTaskSelectionExclusion(task.id, plan.id, exc.code)
                )
                continue
            fences = _predecessor_fences(decision)
            candidates.append(
                ReadyTaskCandidate(
                    execution_plan_id=plan.id,
                    execution_task_id=task.id,
                    project_id=plan.project_id,
                    planning_session_id=plan.planning_session_id,
                    plan_generation=int(plan.generation),
                    plan_created_at=_utc(plan.created_at),
                    plan_task_id=task.plan_task_id,
                    task_state_version=int(task.state_version),
                    decision_hash=decision.decision_hash,
                    graph_hash=decision.graph_hash,
                    predecessor_fence_hash=_predecessor_fence_hash(fences),
                    predecessor_fences=fences,
                    decision=decision,
                )
            )
            if scope.limit is not None and len(candidates) >= scope.limit:
                break
        return ReadyTaskCandidateSet(tuple(candidates), tuple(exclusions))

    @staticmethod
    def _ordering_key(task: ExecutionTask, plan: ExecutionPlan) -> tuple[Any, ...]:
        created_at = _utc(plan.created_at)
        return (
            created_at is None,
            created_at or datetime.max.replace(tzinfo=timezone.utc),
            int(plan.generation),
            int(plan.id),
            str(task.plan_task_id),
            int(task.id),
        )

    @staticmethod
    def _candidate_exclusion(
        task: ExecutionTask,
        plan: ExecutionPlan,
        now: datetime | None,
        claim_service: "ExecutionTaskSchedulerClaimService",
        eligibility: ExecutionEligibilityService,
        lifecycle: ExecutionTaskTransitionService,
    ) -> str | None:
        project = claim_service.db.get(Project, plan.project_id)
        session = claim_service.db.get(PlanningSession, plan.planning_session_id)
        if (
            project is None
            or project.deleted_at is not None
            or session is None
            or session.project_id != plan.project_id
        ):
            return "project_identity_inconsistent"
        try:
            lifecycle.verify_task_lifecycle_integrity(task.id)
        except ExecutionTaskTransitionError:
            return "lifecycle_integrity_failure"
        try:
            decision = eligibility.evaluate_task(task.id)
        except ExecutionEligibilityError as exc:
            return exc.code
        if not decision.eligible:
            if decision.reason_code in {
                "graph_integrity_failure",
                "lifecycle_integrity_failure",
                "unknown_dependency_type",
                "unknown_gate_type",
            }:
                return "eligibility_integrity_failure"
            return "eligibility_decision_stale"
        active_claims = claim_service._active_claims(task.id, now)
        if len(active_claims) > 1:
            return "claim_integrity_failure"
        if active_claims:
            return "task_already_claimed"
        # No dispatch-boundary table exists in Phase 29C-3.  This condition is
        # therefore vacuously satisfied; Phase 29D must add its own fence.
        return None


class ExecutionTaskSchedulerClaimService:
    """Atomic durable acquisition/release and bounded select-and-claim."""

    def __init__(self, db: Session, *, now: Callable[[], datetime] | None = None):
        self.db = db
        self._now = now or (lambda: datetime.now(timezone.utc))

    def acquire_claim(
        self, command: AcquireSchedulerClaimCommand
    ) -> SchedulerClaimResult:
        command = self._normalize_acquire(command)
        command_hash = canonical_json_hash(_acquire_payload(command))
        prior = self._claim_for_idempotency_key(command.idempotency_key)
        if prior is not None:
            if (
                prior.scheduler_id != command.scheduler_id
                or prior.canonical_command_hash != command_hash
            ):
                raise ExecutionSchedulerClaimError(
                    "claim_idempotency_conflict",
                    "idempotency key is bound to a different scheduler claim command",
                )
            self._assert_claim_record_shape(prior)
            return SchedulerClaimResult(prior, replayed=True)

        now = _utc(self._now())
        task = (
            self.db.query(ExecutionTask)
            .filter(ExecutionTask.id == command.execution_task_id)
            .with_for_update()
            .one_or_none()
        )
        if task is None:
            raise ExecutionSchedulerClaimError(
                "execution_task_not_found", "Execution Task was not found"
            )
        plan = (
            self.db.query(ExecutionPlan)
            .filter(ExecutionPlan.id == task.execution_plan_id)
            .with_for_update()
            .one_or_none()
        )
        if plan is None:
            raise ExecutionSchedulerClaimError(
                "eligibility_integrity_failure", "Execution Task parent plan is missing"
            )
        if plan.status != "active":
            raise ExecutionSchedulerClaimError(
                "execution_plan_inactive", "scheduler claims require an active plan"
            )
        if task.status != "ready":
            raise ExecutionSchedulerClaimError(
                "task_not_ready", "scheduler claims require a ready task"
            )
        if int(task.state_version) != command.expected_task_state_version:
            raise ExecutionSchedulerClaimError(
                "task_version_stale", "task state version changed before acquisition"
            )
        self._assert_plan_identity(plan)
        try:
            ExecutionTaskTransitionService(self.db).verify_task_lifecycle_integrity(
                task.id
            )
            decision = ExecutionEligibilityService(self.db).evaluate_task(task.id)
        except (ExecutionTaskTransitionError, ExecutionEligibilityError) as exc:
            code = getattr(exc, "code", "eligibility_integrity_failure")
            if code not in {"execution_task_not_found"}:
                code = "eligibility_integrity_failure"
            raise ExecutionSchedulerClaimError(
                code, "eligibility authority failed verification"
            ) from exc
        if not decision.eligible:
            if decision.reason_code in {
                "graph_integrity_failure",
                "lifecycle_integrity_failure",
                "unknown_dependency_type",
                "unknown_gate_type",
            }:
                raise ExecutionSchedulerClaimError(
                    "eligibility_integrity_failure",
                    "eligibility authority is not intact",
                )
            raise ExecutionSchedulerClaimError(
                "eligibility_decision_stale", "task is no longer eligible"
            )
        fences = _predecessor_fences(decision)
        fence_hash = _predecessor_fence_hash(fences)
        if command.expected_eligibility_decision_hash != decision.decision_hash:
            raise ExecutionSchedulerClaimError(
                "eligibility_decision_stale",
                "eligibility decision changed before acquisition",
            )
        if (
            command.expected_graph_hash is not None
            and command.expected_graph_hash != decision.graph_hash
        ):
            raise ExecutionSchedulerClaimError(
                "eligibility_decision_stale",
                "eligibility graph changed before acquisition",
            )
        if (
            command.expected_predecessor_fence_hash is not None
            and command.expected_predecessor_fence_hash != fence_hash
        ):
            raise ExecutionSchedulerClaimError(
                "eligibility_decision_stale",
                "predecessor fence changed before acquisition",
            )

        active_claims = self._active_claims(task.id, now)
        if len(active_claims) > 1:
            raise ExecutionSchedulerClaimError(
                "claim_integrity_failure",
                "more than one active claim exists for the task",
            )
        active = active_claims[0] if active_claims else None
        all_active_rows = (
            self.db.query(ExecutionTaskSchedulerClaim)
            .filter(
                ExecutionTaskSchedulerClaim.execution_task_id == task.id,
                ExecutionTaskSchedulerClaim.claim_status == CLAIM_STATUS_ACTIVE,
            )
            .order_by(ExecutionTaskSchedulerClaim.id.asc())
            .all()
        )
        if len(all_active_rows) > 1:
            raise ExecutionSchedulerClaimError(
                "claim_integrity_failure",
                "more than one active claim exists for the task",
            )
        if all_active_rows and active is None:
            active = all_active_rows[0]
            if _utc(active.expires_at) is None or _utc(active.expires_at) > now:
                raise ExecutionSchedulerClaimError(
                    "task_already_claimed", "task has an active scheduler claim"
                )

        try:
            with self.db.begin_nested():
                if active is not None:
                    expired = self.db.execute(
                        update(ExecutionTaskSchedulerClaim)
                        .where(
                            ExecutionTaskSchedulerClaim.id == active.id,
                            ExecutionTaskSchedulerClaim.claim_status
                            == CLAIM_STATUS_ACTIVE,
                            ExecutionTaskSchedulerClaim.expires_at <= now,
                        )
                        .values(
                            claim_status=CLAIM_STATUS_EXPIRED,
                            released_at=now,
                            release_reason="claim_expired",
                            updated_at=now,
                        )
                        .execution_options(synchronize_session=False)
                    )
                    if expired.rowcount != 1:
                        raise ExecutionSchedulerClaimError(
                            "task_already_claimed",
                            "claim changed during stale-claim replacement",
                        )
                    self.db.expire(active)
                previous_fence = (
                    self.db.query(func.max(ExecutionTaskSchedulerClaim.fencing_token))
                    .filter(ExecutionTaskSchedulerClaim.execution_task_id == task.id)
                    .scalar()
                    or 0
                )
                claim = ExecutionTaskSchedulerClaim(
                    execution_plan_id=plan.id,
                    execution_task_id=task.id,
                    project_id=plan.project_id,
                    planning_session_id=plan.planning_session_id,
                    scheduler_id=command.scheduler_id,
                    idempotency_key=command.idempotency_key,
                    command_payload=_acquire_payload(command),
                    canonical_command_hash=command_hash,
                    fencing_token=int(previous_fence) + 1,
                    claimed_task_state="ready",
                    claimed_task_state_version=int(task.state_version),
                    claimed_eligibility_decision_hash=decision.decision_hash,
                    claimed_graph_hash=decision.graph_hash,
                    predecessor_fence_hash=fence_hash,
                    predecessor_fences=list(fences),
                    claim_status=CLAIM_STATUS_ACTIVE,
                    lease_duration_seconds=command.lease_duration_seconds,
                    acquired_at=now,
                    expires_at=now + timedelta(seconds=command.lease_duration_seconds),
                    created_at=now,
                    updated_at=now,
                )
                self.db.add(claim)
                self.db.flush()
        except ExecutionSchedulerClaimError:
            raise
        except IntegrityError as exc:
            # The partial unique index is authoritative under a race.  A
            # savepoint keeps the caller's transaction usable for bounded
            # domain mapping and does not consume a fence on rollback.
            competing = self._active_claims(task.id, now)
            if competing:
                raise ExecutionSchedulerClaimError(
                    "task_already_claimed", "another scheduler acquired the task"
                ) from exc
            prior = self._claim_for_idempotency_key(command.idempotency_key)
            if prior is not None and prior.canonical_command_hash == command_hash:
                return SchedulerClaimResult(prior, replayed=True)
            raise ExecutionSchedulerClaimError(
                "claim_integrity_failure", "claim uniqueness could not be persisted"
            ) from exc
        except OperationalError as exc:
            # SQLite can surface the same concurrent write race as a bounded
            # database-lock error instead of a unique violation.  Reset only
            # this failed claim transaction before translating it; callers
            # must retry their surrounding unit of work after this path.
            self.db.rollback()
            competing = self._active_claims(task.id, now)
            if competing:
                raise ExecutionSchedulerClaimError(
                    "task_already_claimed", "another scheduler acquired the task"
                ) from exc
            raise ExecutionSchedulerClaimError(
                "claim_integrity_failure",
                "claim persistence encountered a database contention failure",
            ) from exc
        return SchedulerClaimResult(claim, replayed=False)

    def release_claim(
        self,
        claim_id: int,
        scheduler_id: str,
        fencing_token: int,
        reason_code: str,
        idempotency_key: str,
    ) -> ReleaseSchedulerClaimResult:
        scheduler = _text(scheduler_id, "scheduler_id", 255)
        key = _text(idempotency_key, "idempotency_key", 128)
        try:
            requested_claim_id = int(claim_id)
        except (TypeError, ValueError) as exc:
            raise ExecutionSchedulerClaimError(
                "claim_not_found", "scheduler claim identifier is invalid"
            ) from exc
        reason = str(reason_code or "").strip()
        if reason not in RELEASE_REASON_CODES:
            raise ExecutionSchedulerClaimError(
                "invalid_release_reason", "release reason is not supported"
            )
        try:
            fence = int(fencing_token)
        except (TypeError, ValueError) as exc:
            raise ExecutionSchedulerClaimError(
                "claim_fence_stale", "fencing token is invalid"
            ) from exc
        claim = self._claim_for_release_key(key)
        if claim is not None:
            release_hash = _release_hash(
                requested_claim_id, scheduler, fence, reason, key
            )
            if (
                claim.id != requested_claim_id
                or claim.canonical_release_hash != release_hash
            ):
                raise ExecutionSchedulerClaimError(
                    "claim_idempotency_conflict",
                    "release key is bound to a different release",
                )
            return ReleaseSchedulerClaimResult(claim, replayed=True)

        claim = (
            self.db.query(ExecutionTaskSchedulerClaim)
            .filter(ExecutionTaskSchedulerClaim.id == requested_claim_id)
            .with_for_update()
            .one_or_none()
        )
        if claim is None:
            raise ExecutionSchedulerClaimError(
                "claim_not_found", "scheduler claim was not found"
            )
        if claim.scheduler_id != scheduler:
            raise ExecutionSchedulerClaimError(
                "claim_owner_mismatch", "scheduler does not own the claim"
            )
        if int(claim.fencing_token) != fence:
            raise ExecutionSchedulerClaimError(
                "claim_fence_stale", "fencing token does not match the claim"
            )
        if claim.claim_status != CLAIM_STATUS_ACTIVE:
            raise ExecutionSchedulerClaimError(
                (
                    "claim_expired"
                    if claim.claim_status == CLAIM_STATUS_EXPIRED
                    else "claim_not_found"
                ),
                "claim is no longer active",
            )
        now = _utc(self._now())
        if _utc(claim.expires_at) is None or _utc(claim.expires_at) <= now:
            raise ExecutionSchedulerClaimError(
                "claim_expired", "claim lease has expired"
            )
        release_hash = _release_hash(claim.id, scheduler, fence, reason, key)
        claim.claim_status = CLAIM_STATUS_RELEASED
        claim.released_at = now
        claim.release_reason = reason
        claim.released_by_scheduler_id = scheduler
        claim.release_idempotency_key = key
        claim.canonical_release_hash = release_hash
        claim.updated_at = now
        self.db.flush()
        return ReleaseSchedulerClaimResult(claim, replayed=False)

    def select_and_claim_next(
        self,
        scheduler_id: str,
        idempotency_key: str,
        scope: ReadyTaskSelectionScope | Mapping[str, object] | None = None,
    ) -> SelectAndClaimResult:
        normalized_scope = _scope(scope)
        candidates = ExecutionReadyTaskSelectionService(
            self.db, now=self._now
        ).list_ready_candidates(
            execution_plan_id=normalized_scope.execution_plan_id,
            project_id=normalized_scope.project_id,
            limit=normalized_scope.limit or DEFAULT_SELECTION_LIMIT,
        )
        skipped: list[int] = []
        for candidate in candidates.candidates:
            command = AcquireSchedulerClaimCommand(
                execution_task_id=candidate.execution_task_id,
                expected_task_state="ready",
                expected_task_state_version=candidate.task_state_version,
                expected_eligibility_decision_hash=candidate.decision_hash,
                scheduler_id=scheduler_id,
                idempotency_key=idempotency_key,
                lease_duration_seconds=DEFAULT_CLAIM_LEASE_SECONDS,
                expected_graph_hash=candidate.graph_hash,
                expected_predecessor_fence_hash=candidate.predecessor_fence_hash,
            )
            try:
                result = self.acquire_claim(command)
            except ExecutionSchedulerClaimError as exc:
                if exc.code in {
                    "task_already_claimed",
                    "task_not_ready",
                    "task_state_stale",
                    "task_version_stale",
                    "eligibility_decision_stale",
                }:
                    skipped.append(candidate.execution_task_id)
                    continue
                raise
            return SelectAndClaimResult(
                code="claimed",
                claim_result=result,
                candidates_considered=len(skipped) + 1,
                skipped_task_ids=tuple(skipped),
            )
        return SelectAndClaimResult(
            code="no_candidate_available",
            claim_result=None,
            candidates_considered=len(candidates.candidates),
            skipped_task_ids=tuple(skipped),
        )

    def verify_scheduler_claim_integrity(
        self, execution_task_id: int
    ) -> SchedulerClaimIntegrityResult:
        task = self.db.get(ExecutionTask, int(execution_task_id))
        if task is None:
            raise ExecutionSchedulerClaimError(
                "execution_task_not_found", "Execution Task was not found"
            )
        plan = self.db.get(ExecutionPlan, task.execution_plan_id)
        claims = (
            self.db.query(ExecutionTaskSchedulerClaim)
            .filter(ExecutionTaskSchedulerClaim.execution_task_id == task.id)
            .order_by(
                ExecutionTaskSchedulerClaim.fencing_token.asc(),
                ExecutionTaskSchedulerClaim.id.asc(),
            )
            .all()
        )
        issues: list[str] = []
        if plan is None:
            issues.append("task_plan_mismatch")
        active = [
            claim for claim in claims if claim.claim_status == CLAIM_STATUS_ACTIVE
        ]
        if len(active) > 1:
            issues.append("duplicate_active_claim")
        previous_fence = 0
        seen_command_keys: set[str] = set()
        seen_release_keys: set[str] = set()
        for claim in claims:
            if claim.fencing_token <= previous_fence:
                issues.append("non_monotonic_fencing_token")
            previous_fence = int(claim.fencing_token)
            if claim.execution_plan_id != task.execution_plan_id:
                issues.append("task_plan_mismatch")
            if plan is not None and (
                claim.project_id != plan.project_id
                or claim.planning_session_id != plan.planning_session_id
            ):
                issues.append("claim_identity_mismatch")
            if claim.idempotency_key in seen_command_keys:
                issues.append("duplicate_claim_idempotency_binding")
            seen_command_keys.add(claim.idempotency_key)
            if claim.release_idempotency_key is not None:
                if claim.release_idempotency_key in seen_release_keys:
                    issues.append("duplicate_release_idempotency_binding")
                seen_release_keys.add(claim.release_idempotency_key)
            if claim.claim_status not in CLAIM_STATUSES:
                issues.append("invalid_claim_status")
            if (
                claim.claimed_task_state != "ready"
                or claim.claimed_task_state_version < 0
            ):
                issues.append("claimed_task_fence_malformed")
            for field in (
                claim.claimed_eligibility_decision_hash,
                claim.claimed_graph_hash,
                claim.predecessor_fence_hash,
                claim.canonical_command_hash,
            ):
                if not _valid_hash(field):
                    issues.append("malformed_claim_hash")
            if not isinstance(claim.predecessor_fences, list):
                issues.append("malformed_predecessor_fences")
            elif (
                _predecessor_fence_hash(tuple(claim.predecessor_fences))
                != claim.predecessor_fence_hash
            ):
                issues.append("predecessor_fence_hash_mismatch")
            if not claim.scheduler_id:
                issues.append("claim_owner_missing")
            if not isinstance(claim.command_payload, dict):
                issues.append("malformed_claim_command")
            elif (
                canonical_json_hash(claim.command_payload)
                != claim.canonical_command_hash
            ):
                issues.append("claim_command_hash_mismatch")
            if (
                _utc(claim.expires_at) is None
                or _utc(claim.acquired_at) is None
                or _utc(claim.expires_at) <= _utc(claim.acquired_at)
            ):
                issues.append("invalid_expiry_ordering")
            if claim.claim_status == CLAIM_STATUS_ACTIVE:
                if claim.released_at is not None or claim.release_reason is not None:
                    issues.append("active_claim_release_fields_present")
                if task.status != "ready":
                    issues.append("active_claim_for_non_ready_task")
                if int(task.state_version) != int(claim.claimed_task_state_version):
                    issues.append("active_claim_task_fence_stale")
            elif claim.claim_status == CLAIM_STATUS_RELEASED:
                if (
                    claim.released_at is None
                    or claim.release_reason not in RELEASE_REASON_CODES
                ):
                    issues.append("released_claim_fields_invalid")
                if claim.released_by_scheduler_id != claim.scheduler_id:
                    issues.append("release_owner_mismatch")
                if not claim.release_idempotency_key or not _valid_hash(
                    claim.canonical_release_hash
                ):
                    issues.append("release_idempotency_missing")
                elif (
                    _release_hash(
                        claim.id,
                        claim.scheduler_id,
                        claim.fencing_token,
                        claim.release_reason,
                        claim.release_idempotency_key,
                    )
                    != claim.canonical_release_hash
                ):
                    issues.append("release_hash_mismatch")
            elif claim.claim_status == CLAIM_STATUS_EXPIRED:
                if claim.released_at is None or claim.release_reason != "claim_expired":
                    issues.append("expired_claim_fields_invalid")
        return SchedulerClaimIntegrityResult(
            execution_task_id=task.id,
            execution_plan_id=task.execution_plan_id,
            claim_count=len(claims),
            active_claim_count=len(active),
            issues=tuple(sorted(set(issues))),
        )

    def verify_plan_scheduler_claim_integrity(
        self, execution_plan_id: int
    ) -> SchedulerPlanClaimIntegrityResult:
        plan = self.db.get(ExecutionPlan, int(execution_plan_id))
        if plan is None:
            raise ExecutionSchedulerClaimError(
                "execution_plan_not_found", "Execution Plan was not found"
            )
        tasks = (
            self.db.query(ExecutionTask)
            .filter(ExecutionTask.execution_plan_id == plan.id)
            .order_by(ExecutionTask.id.asc())
            .all()
        )
        task_results = tuple(
            self.verify_scheduler_claim_integrity(task.id) for task in tasks
        )
        issues: list[str] = []
        known_task_ids = {task.id for task in tasks}
        orphan_claims = (
            self.db.query(ExecutionTaskSchedulerClaim)
            .filter(ExecutionTaskSchedulerClaim.execution_plan_id == plan.id)
            .all()
        )
        if any(
            claim.execution_task_id not in known_task_ids for claim in orphan_claims
        ):
            issues.append("claim_task_plan_mismatch")
        return SchedulerPlanClaimIntegrityResult(plan.id, task_results, tuple(issues))

    # Explicit aliases keep the plan-level verifier discoverable by both
    # execution-plan and scheduler terminology.
    verify_execution_plan_claim_integrity = verify_plan_scheduler_claim_integrity

    def _normalize_acquire(
        self, command: AcquireSchedulerClaimCommand
    ) -> AcquireSchedulerClaimCommand:
        if not isinstance(command, AcquireSchedulerClaimCommand):
            raise ExecutionSchedulerClaimError(
                "invalid_claim_command", "claim command is invalid"
            )
        if command.expected_task_state != "ready":
            raise ExecutionSchedulerClaimError(
                "task_not_ready", "scheduler claims require expected_task_state='ready'"
            )
        try:
            version = int(command.expected_task_state_version)
        except (TypeError, ValueError) as exc:
            raise ExecutionSchedulerClaimError(
                "task_version_stale", "task version is invalid"
            ) from exc
        if version < 0:
            raise ExecutionSchedulerClaimError(
                "task_version_stale", "task version is invalid"
            )
        try:
            duration = int(command.lease_duration_seconds)
        except (TypeError, ValueError) as exc:
            raise ExecutionSchedulerClaimError(
                "invalid_claim_duration", "lease duration is invalid"
            ) from exc
        if duration < MIN_CLAIM_LEASE_SECONDS or duration > MAX_CLAIM_LEASE_SECONDS:
            raise ExecutionSchedulerClaimError(
                "invalid_claim_duration", "lease duration is out of bounds"
            )
        return AcquireSchedulerClaimCommand(
            execution_task_id=int(command.execution_task_id),
            expected_task_state="ready",
            expected_task_state_version=version,
            expected_eligibility_decision_hash=_hash(
                command.expected_eligibility_decision_hash,
                "expected_eligibility_decision_hash",
            ),
            scheduler_id=_text(command.scheduler_id, "scheduler_id", 255),
            idempotency_key=_text(command.idempotency_key, "idempotency_key", 128),
            lease_duration_seconds=duration,
            expected_graph_hash=(
                _hash(command.expected_graph_hash, "expected_graph_hash")
                if command.expected_graph_hash is not None
                else None
            ),
            expected_predecessor_fence_hash=(
                _hash(
                    command.expected_predecessor_fence_hash,
                    "expected_predecessor_fence_hash",
                )
                if command.expected_predecessor_fence_hash is not None
                else None
            ),
        )

    def _assert_plan_identity(self, plan: ExecutionPlan) -> None:
        project = self.db.get(Project, plan.project_id)
        session = self.db.get(PlanningSession, plan.planning_session_id)
        if (
            project is None
            or project.deleted_at is not None
            or session is None
            or session.project_id != plan.project_id
        ):
            raise ExecutionSchedulerClaimError(
                "eligibility_integrity_failure",
                "plan project/session identity is inconsistent",
            )

    def _claim_for_idempotency_key(
        self, key: str
    ) -> ExecutionTaskSchedulerClaim | None:
        return (
            self.db.query(ExecutionTaskSchedulerClaim)
            .filter(ExecutionTaskSchedulerClaim.idempotency_key == key)
            .one_or_none()
        )

    def _claim_for_release_key(self, key: str) -> ExecutionTaskSchedulerClaim | None:
        return (
            self.db.query(ExecutionTaskSchedulerClaim)
            .filter(ExecutionTaskSchedulerClaim.release_idempotency_key == key)
            .one_or_none()
        )

    def _active_claims(
        self, execution_task_id: int, now: datetime | None
    ) -> list[ExecutionTaskSchedulerClaim]:
        if now is None:
            now = _utc(self._now())
        return (
            self.db.query(ExecutionTaskSchedulerClaim)
            .filter(
                ExecutionTaskSchedulerClaim.execution_task_id == int(execution_task_id),
                ExecutionTaskSchedulerClaim.claim_status == CLAIM_STATUS_ACTIVE,
                ExecutionTaskSchedulerClaim.expires_at > now,
            )
            .order_by(ExecutionTaskSchedulerClaim.id.asc())
            .all()
        )

    @staticmethod
    def _assert_claim_record_shape(claim: ExecutionTaskSchedulerClaim) -> None:
        if claim.claim_status not in CLAIM_STATUSES or not _valid_hash(
            claim.canonical_command_hash
        ):
            raise ExecutionSchedulerClaimError(
                "claim_integrity_failure", "persisted scheduler claim is malformed"
            )


def _acquire_payload(command: AcquireSchedulerClaimCommand) -> dict[str, object]:
    return {
        "schema_version": EXECUTION_SCHEDULER_CLAIM_SCHEMA_VERSION,
        "execution_task_id": int(command.execution_task_id),
        "expected_task_state": command.expected_task_state,
        "expected_task_state_version": int(command.expected_task_state_version),
        "expected_eligibility_decision_hash": command.expected_eligibility_decision_hash,
        "expected_graph_hash": command.expected_graph_hash,
        "expected_predecessor_fence_hash": command.expected_predecessor_fence_hash,
        "scheduler_id": command.scheduler_id,
        "idempotency_key": command.idempotency_key,
        "lease_duration_seconds": int(command.lease_duration_seconds),
    }


def _release_hash(
    claim_id: int,
    scheduler_id: str,
    fencing_token: int,
    reason_code: str | None,
    idempotency_key: str | None,
) -> str:
    return canonical_json_hash(
        {
            "schema_version": EXECUTION_SCHEDULER_CLAIM_SCHEMA_VERSION,
            "claim_id": int(claim_id),
            "scheduler_id": scheduler_id,
            "fencing_token": int(fencing_token),
            "reason_code": reason_code,
            "idempotency_key": idempotency_key,
        }
    )


__all__ = [
    "AcquireSchedulerClaimCommand",
    "DEFAULT_CLAIM_LEASE_SECONDS",
    "ExecutionReadyTaskSelectionService",
    "ExecutionSchedulerClaimError",
    "ExecutionTaskSchedulerClaimError",
    "MAX_CLAIM_LEASE_SECONDS",
    "MAX_SELECTION_LIMIT",
    "MIN_CLAIM_LEASE_SECONDS",
    "ReadyTaskCandidate",
    "ReadyTaskCandidateSet",
    "ReadyTaskSelectionScope",
    "ReleaseSchedulerClaimResult",
    "RELEASE_REASON_CODES",
    "SchedulerClaimIntegrityResult",
    "SchedulerClaimResult",
    "SchedulerPlanClaimIntegrityResult",
    "SelectAndClaimResult",
    "ExecutionTaskSchedulerClaimService",
]
