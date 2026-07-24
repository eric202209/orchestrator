"""The single write boundary for Phase 29C-1 Execution Task lifecycle state.

Execution graph fields remain immutable after materialization. This service
owns the only production lifecycle mutation: it validates a fenced command,
appends one hash-chained transition event, and advances the current-state
projection and optimistic version in the caller's transaction.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, replace
from datetime import datetime, timezone
import re
import unicodedata

from sqlalchemy import update
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ExecutionPlan,
    ExecutionTask,
    ExecutionTaskTransition,
)
from app.services.planning.operator_review import canonical_json_hash


EXECUTION_TASK_STATE_SCHEMA_VERSION = "execution-task-lifecycle/1.0"
EXECUTION_TASK_STATES = frozenset(
    {
        "pending",
        "ready",
        "running",
        "awaiting_validation",
        "awaiting_recovery",
        "awaiting_apply",
        "succeeded",
        "failed",
        "blocked",
        "paused",
        "cancelled",
        "skipped",
    }
)
TERMINAL_EXECUTION_TASK_STATES = frozenset(
    {"succeeded", "failed", "cancelled", "skipped"}
)
READY_EXECUTION_TASK_STATES = frozenset({"ready"})
RUNNING_EXECUTION_TASK_STATES = frozenset({"running"})
SUCCESSFUL_EXECUTION_TASK_STATES = frozenset({"succeeded"})
DEPENDENCY_SATISFYING_EXECUTION_TASK_STATES = frozenset({"succeeded"})
ALLOWED_EXECUTION_TASK_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "pending": frozenset({"ready", "blocked", "cancelled", "skipped"}),
    "ready": frozenset({"running", "blocked", "paused", "cancelled", "skipped"}),
    "running": frozenset(
        {
            "succeeded",
            "failed",
            "awaiting_validation",
            "awaiting_recovery",
            "paused",
            "cancelled",
        }
    ),
    "awaiting_validation": frozenset(
        {"succeeded", "awaiting_apply", "awaiting_recovery", "paused", "cancelled"}
    ),
    "awaiting_apply": frozenset(
        {"succeeded", "awaiting_recovery", "paused", "cancelled"}
    ),
    "awaiting_recovery": frozenset({"ready", "failed", "paused", "cancelled"}),
    "blocked": frozenset({"ready", "cancelled", "skipped"}),
    "paused": frozenset({"ready", "running", "cancelled"}),
    "failed": frozenset(),
    "succeeded": frozenset(),
    "cancelled": frozenset(),
    "skipped": frozenset(),
}
EXECUTION_TASK_ACTOR_TYPES = frozenset(
    {"system", "scheduler", "worker", "recovery", "operator", "test"}
)
EXECUTION_TASK_REASON_CODES = frozenset(
    {
        "dependencies_satisfied",
        "dependency_blocked",
        "dependency_failed",
        "dependency_cancelled",
        "dependency_skipped",
        "execution_started",
        "execution_succeeded",
        "execution_failed",
        "retry_authorized",
        "runtime_candidate_completed",
        "runtime_attempt_failed",
        "validation_accepted",
        "validation_rejected",
        "controlled_apply_verified",
        "controlled_apply_failed",
        "recovery_retry_authorized",
        "recovery_exhausted",
        "recovery_non_retryable",
        "operator_paused",
        "operator_cancelled",
        "operator_skipped",
        "resume_authorized",
        "review_gate_pending",
        "manual_gate_pending",
        "resource_unavailable",
        "resource_gate_pending",
        "group_gate_pending",
        "system_reconciliation",
    }
)
TERMINAL_SUCCESS_AUTHORIZATION_REASON = "validation_accepted"
TERMINAL_FAILURE_AUTHORIZATION_REASON = "recovery_exhausted"
TERMINAL_AUTHORITY_ACTOR_TYPES = frozenset({"system", "recovery"})
GENESIS_PREVIOUS_EVENT_HASH: str | None = None
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ExecutionTaskTransitionError(Exception):
    """Bounded domain error returned by the lifecycle boundary."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


class ExecutionTaskTransitionIntegrityError(ExecutionTaskTransitionError):
    """Persisted lifecycle authority is internally inconsistent."""

    def __init__(self, message: str):
        super().__init__("transition_integrity_failure", message)


@dataclass(frozen=True)
class ExecutionTaskTransitionCommand:
    execution_task_id: int
    expected_from_state: str
    expected_state_version: int
    to_state: str
    reason_code: str
    actor_type: str
    actor_id: str | None
    idempotency_key: str
    reason_detail: str | None = None
    execution_plan_id: int | None = None
    guarded_task_fences: tuple["ExecutionTaskLifecycleFence", ...] = ()
    runtime_attempt_id: int | None = None
    runtime_lease_id: int | None = None
    runtime_ownership_fence: int | None = None


@dataclass(frozen=True)
class ExecutionTaskLifecycleFence:
    """A predecessor lifecycle projection fence for a guarded transition."""

    execution_task_id: int
    expected_state: str
    expected_state_version: int
    lifecycle_head_hash: str | None = None


@dataclass(frozen=True)
class ExecutionTaskTransitionResult:
    execution_task_id: int
    execution_plan_id: int
    plan_task_id: str
    event_id: int
    sequence: int
    from_state: str
    to_state: str
    expected_version: int
    resulting_version: int
    event_hash: str
    replayed: bool


@dataclass(frozen=True)
class ExecutionTaskLifecycleIntegrityResult:
    execution_task_id: int
    execution_plan_id: int
    event_count: int
    current_state: str
    state_version: int
    verified: bool = True


@dataclass(frozen=True)
class ExecutionPlanLifecycleIntegrityResult:
    execution_plan_id: int
    task_count: int
    verified: bool = True


def is_terminal_execution_task_state(state: str) -> bool:
    return state in TERMINAL_EXECUTION_TASK_STATES


def satisfies_execution_task_dependency(state: str) -> bool:
    return state in DEPENDENCY_SATISFYING_EXECUTION_TASK_STATES


def _validate_transition_reason_contract(
    *, from_state: str, to_state: str, reason_code: str, actor_type: str
) -> None:
    """Keep runtime evidence distinct from acceptance/recovery authority.

    The legacy direct terminal edges remain structurally available for
    compatibility.  Production callers must use the amended authority reason
    for terminalization; the test actor is retained solely for existing
    contract fixtures that exercise the structural graph.
    """

    required_reason = {
        ("running", "awaiting_validation"): "runtime_candidate_completed",
        ("running", "awaiting_recovery"): "runtime_attempt_failed",
        ("awaiting_validation", "succeeded"): TERMINAL_SUCCESS_AUTHORIZATION_REASON,
        (
            "awaiting_validation",
            "awaiting_apply",
        ): TERMINAL_SUCCESS_AUTHORIZATION_REASON,
        ("awaiting_validation", "awaiting_recovery"): "validation_rejected",
        # Reserved for Phase 29D-4: post-apply verification/failure routing.
        # Not invoked by Phase 29D-3B.
        ("awaiting_apply", "succeeded"): "controlled_apply_verified",
        ("awaiting_apply", "awaiting_recovery"): "controlled_apply_failed",
        ("awaiting_recovery", "ready"): "recovery_retry_authorized",
        ("awaiting_recovery", "failed"): TERMINAL_FAILURE_AUTHORIZATION_REASON,
    }.get((from_state, to_state))
    if required_reason is not None and reason_code != required_reason:
        if (from_state, to_state) == (
            "awaiting_recovery",
            "failed",
        ) and reason_code == "recovery_non_retryable":
            required_reason = reason_code
        else:
            raise ExecutionTaskTransitionError(
                "transition_reason_not_authorized",
                f"{from_state!r} -> {to_state!r} requires reason {required_reason!r}",
            )

    if to_state == "succeeded" and actor_type not in (
        TERMINAL_AUTHORITY_ACTOR_TYPES | {"test"}
    ):
        raise ExecutionTaskTransitionError(
            "terminal_acceptance_actor_required",
            "succeeded requires validation authority, not a runtime worker",
        )
    if to_state == "failed" and actor_type not in (
        TERMINAL_AUTHORITY_ACTOR_TYPES | {"test"}
    ):
        raise ExecutionTaskTransitionError(
            "terminal_recovery_actor_required",
            "failed requires recovery authority, not a runtime worker",
        )

    if from_state == "running" and to_state == "succeeded":
        if (
            actor_type != "test"
            and reason_code != TERMINAL_SUCCESS_AUTHORIZATION_REASON
        ):
            raise ExecutionTaskTransitionError(
                "terminal_acceptance_authority_required",
                "succeeded requires validation acceptance authority",
            )
    elif from_state == "running" and to_state == "failed":
        if (
            actor_type != "test"
            and reason_code != TERMINAL_FAILURE_AUTHORIZATION_REASON
        ):
            raise ExecutionTaskTransitionError(
                "terminal_recovery_authority_required",
                "failed requires recovery exhaustion authority",
            )


def _bounded_text(
    value: object,
    field_name: str,
    limit: int,
    *,
    required: bool,
) -> str:
    text = unicodedata.normalize("NFC", str(value or "").strip())
    if required and not text:
        raise ExecutionTaskTransitionError(
            "invalid_command", f"{field_name} is required"
        )
    if len(text) > limit:
        raise ExecutionTaskTransitionError(
            "invalid_command", f"{field_name} exceeds {limit} characters"
        )
    if _CONTROL_RE.search(text):
        raise ExecutionTaskTransitionError(
            "invalid_command", f"{field_name} contains control characters"
        )
    return text


def _timestamp(value: datetime) -> str:
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).isoformat()


def _command_payload(command: ExecutionTaskTransitionCommand) -> dict[str, object]:
    payload = {
        "schema_version": EXECUTION_TASK_STATE_SCHEMA_VERSION,
        "execution_plan_id": command.execution_plan_id,
        "execution_task_id": command.execution_task_id,
        "expected_from_state": command.expected_from_state,
        "expected_state_version": command.expected_state_version,
        "to_state": command.to_state,
        "reason_code": command.reason_code,
        "reason_detail": command.reason_detail,
        "actor_type": command.actor_type,
        "actor_id": command.actor_id,
        "idempotency_key": command.idempotency_key,
        "guarded_task_fences": [
            {
                "execution_task_id": fence.execution_task_id,
                "expected_state": fence.expected_state,
                "expected_state_version": fence.expected_state_version,
                "lifecycle_head_hash": fence.lifecycle_head_hash,
            }
            for fence in command.guarded_task_fences
        ],
    }
    if command.runtime_attempt_id is not None:
        payload["runtime_attempt_id"] = command.runtime_attempt_id
    if command.runtime_lease_id is not None:
        payload["runtime_lease_id"] = command.runtime_lease_id
    if command.runtime_ownership_fence is not None:
        payload["runtime_ownership_fence"] = command.runtime_ownership_fence
    return payload


def _event_payload(event: ExecutionTaskTransition) -> dict[str, object]:
    payload = {
        "schema_version": EXECUTION_TASK_STATE_SCHEMA_VERSION,
        "execution_plan_id": event.execution_plan_id,
        "execution_task_id": event.execution_task_id,
        "plan_task_id": event.plan_task_id,
        "sequence": event.sequence,
        "from_state": event.from_state,
        "to_state": event.to_state,
        "reason_code": event.reason_code,
        "reason_detail": event.reason_detail,
        "actor_type": event.actor_type,
        "actor_id": event.actor_id,
        "command_id": event.command_id,
        "expected_version": event.expected_version,
        "resulting_version": event.resulting_version,
        "canonical_command_hash": event.canonical_command_hash,
        "previous_event_hash": event.previous_event_hash,
        "created_at": _timestamp(event.created_at),
    }
    if event.runtime_attempt_id is not None:
        payload["runtime_attempt_id"] = event.runtime_attempt_id
    if event.runtime_lease_id is not None:
        payload["runtime_lease_id"] = event.runtime_lease_id
    if event.runtime_ownership_fence is not None:
        payload["runtime_ownership_fence"] = event.runtime_ownership_fence
    return payload


class ExecutionTaskTransitionService:
    """Validate and persist all lifecycle changes for an Execution Task."""

    def __init__(self, db: Session, *, now=None):
        self.db = db
        self._now = now or (lambda: datetime.now(timezone.utc))

    def transition(
        self,
        command: ExecutionTaskTransitionCommand,
    ) -> ExecutionTaskTransitionResult:
        command = self._normalize_command(command)
        task = (
            self.db.query(ExecutionTask)
            .filter(ExecutionTask.id == command.execution_task_id)
            .with_for_update()
            .one_or_none()
        )
        if task is None:
            raise ExecutionTaskTransitionError(
                "execution_task_not_found", "Execution Task was not found"
            )
        plan = (
            self.db.query(ExecutionPlan)
            .filter(ExecutionPlan.id == task.execution_plan_id)
            .with_for_update()
            .one_or_none()
        )
        if plan is None:
            raise ExecutionTaskTransitionIntegrityError(
                "Execution Task parent Execution Plan is missing"
            )
        if (
            command.execution_plan_id is not None
            and command.execution_plan_id != plan.id
        ):
            raise ExecutionTaskTransitionError(
                "execution_task_plan_mismatch",
                "Execution Task does not belong to the supplied Execution Plan",
            )
        if plan.status != "active":
            raise ExecutionTaskTransitionError(
                "execution_plan_inactive",
                "lifecycle transitions require an active Execution Plan",
            )
        command = replace(command, execution_plan_id=plan.id)

        existing = self._existing_event(task.id, command)
        if existing is not None:
            # A replay is allowed to return an old result only after the full
            # persisted chain and projection have been checked.
            self.verify_task_lifecycle_integrity(task.id)
            command_hash = canonical_json_hash(_command_payload(command))
            if command_hash != existing.canonical_command_hash:
                raise ExecutionTaskTransitionError(
                    "transition_idempotency_conflict",
                    "idempotency key is bound to a different command",
                )
            return self._result(existing, replayed=True)

        events = self._events(task.id)
        if events:
            self.verify_task_lifecycle_integrity(task.id)
        elif task.status not in EXECUTION_TASK_STATES:
            raise ExecutionTaskTransitionError(
                "invalid_current_state",
                f"stored current state is not recognized: {task.status!r}",
            )
        elif task.status != "pending" or int(task.state_version or 0) != 0:
            raise ExecutionTaskTransitionIntegrityError(
                "task has non-genesis projection without transition history"
            )

        if task.status != command.expected_from_state:
            raise ExecutionTaskTransitionError(
                "transition_state_stale",
                "expected current state does not match the persisted state",
            )
        if int(task.state_version) != command.expected_state_version:
            raise ExecutionTaskTransitionError(
                "transition_version_stale",
                "expected state version does not match the persisted version",
            )
        self._verify_guarded_task_fences(task, command.guarded_task_fences)
        allowed = ALLOWED_EXECUTION_TASK_TRANSITIONS.get(task.status)
        if allowed is None or command.to_state not in allowed:
            raise ExecutionTaskTransitionError(
                "transition_not_allowed",
                f"transition {task.status!r} -> {command.to_state!r} is not allowed",
            )
        _validate_transition_reason_contract(
            from_state=task.status,
            to_state=command.to_state,
            reason_code=command.reason_code,
            actor_type=command.actor_type,
        )

        sequence = events[-1].sequence + 1 if events else 1
        previous_event_hash = (
            events[-1].event_hash if events else GENESIS_PREVIOUS_EVENT_HASH
        )
        resulting_version = command.expected_state_version + 1
        command_hash = canonical_json_hash(_command_payload(command))
        created_at = self._now()
        event = ExecutionTaskTransition(
            execution_plan_id=plan.id,
            execution_task_id=task.id,
            plan_task_id=task.plan_task_id,
            sequence=sequence,
            from_state=task.status,
            to_state=command.to_state,
            reason_code=command.reason_code,
            reason_detail=command.reason_detail,
            actor_type=command.actor_type,
            actor_id=command.actor_id or command.actor_type,
            command_id=command.idempotency_key,
            expected_version=command.expected_state_version,
            resulting_version=resulting_version,
            canonical_command_hash=command_hash,
            previous_event_hash=previous_event_hash,
            event_hash="",
            canonical_payload_hash="",
            runtime_attempt_id=command.runtime_attempt_id,
            runtime_lease_id=command.runtime_lease_id,
            runtime_ownership_fence=command.runtime_ownership_fence,
            created_at=created_at,
        )
        payload_hash = canonical_json_hash(_event_payload(event))
        event.canonical_payload_hash = payload_hash
        event.event_hash = payload_hash

        # This conditional UPDATE is the SQLite-compatible optimistic fence.
        # with_for_update is not sufficient on SQLite, so rowcount remains
        # mandatory even on databases that support row locks.
        updated = self.db.execute(
            update(ExecutionTask)
            .where(
                ExecutionTask.id == task.id,
                ExecutionTask.status == command.expected_from_state,
                ExecutionTask.state_version == command.expected_state_version,
            )
            .values(status=command.to_state, state_version=resulting_version)
        )
        if updated.rowcount != 1:
            self.db.expire(task)
            self.db.refresh(task)
            if task.status != command.expected_from_state:
                raise ExecutionTaskTransitionError(
                    "transition_state_stale",
                    "concurrent transition changed the current state",
                )
            raise ExecutionTaskTransitionError(
                "transition_version_stale",
                "concurrent transition changed the state version",
            )

        self.db.add(event)
        try:
            self.db.flush()
        except IntegrityError as exc:
            self.db.rollback()
            existing = self._existing_event(task.id, command)
            if existing is not None:
                self.verify_task_lifecycle_integrity(task.id)
                if existing.canonical_command_hash == command_hash:
                    return self._result(existing, replayed=True)
                raise ExecutionTaskTransitionError(
                    "transition_idempotency_conflict",
                    "idempotency key is bound to a different command",
                ) from exc
            raise ExecutionTaskTransitionError(
                "transition_state_stale",
                "concurrent transition could not be persisted",
            ) from exc

        self.db.refresh(task)
        return self._result(event, replayed=False)

    def verify_task_lifecycle_integrity(
        self, execution_task_id: int
    ) -> ExecutionTaskLifecycleIntegrityResult:
        task = self.db.get(ExecutionTask, int(execution_task_id))
        if task is None:
            raise ExecutionTaskTransitionError(
                "execution_task_not_found", "Execution Task was not found"
            )
        if self.db.get(ExecutionPlan, task.execution_plan_id) is None:
            raise ExecutionTaskTransitionIntegrityError(
                "Execution Task parent Execution Plan is missing"
            )

        events = self._events(task.id)
        derived_state = "pending"
        derived_version = 0
        previous_hash = GENESIS_PREVIOUS_EVENT_HASH
        for expected_sequence, event in enumerate(events, start=1):
            if event.sequence != expected_sequence:
                raise ExecutionTaskTransitionIntegrityError(
                    "lifecycle event sequence is missing or duplicated"
                )
            if (
                event.execution_plan_id != task.execution_plan_id
                or event.execution_task_id != task.id
                or event.plan_task_id != task.plan_task_id
            ):
                raise ExecutionTaskTransitionIntegrityError(
                    "lifecycle event is attached to the wrong task or plan"
                )
            if event.previous_event_hash != previous_hash:
                raise ExecutionTaskTransitionIntegrityError(
                    "lifecycle event previous hash link is broken"
                )
            if (
                event.from_state != derived_state
                or event.expected_version != derived_version
                or event.resulting_version != derived_version + 1
            ):
                raise ExecutionTaskTransitionIntegrityError(
                    "lifecycle event state/version replay binding is invalid"
                )
            if event.from_state not in EXECUTION_TASK_STATES:
                raise ExecutionTaskTransitionIntegrityError(
                    "lifecycle event contains an unknown source state"
                )
            if event.to_state not in EXECUTION_TASK_STATES:
                raise ExecutionTaskTransitionIntegrityError(
                    "lifecycle event contains an unknown target state"
                )
            if (
                event.to_state
                not in ALLOWED_EXECUTION_TASK_TRANSITIONS[event.from_state]
            ):
                raise ExecutionTaskTransitionIntegrityError(
                    "lifecycle event contains an illegal transition edge"
                )
            if event.actor_type not in EXECUTION_TASK_ACTOR_TYPES:
                raise ExecutionTaskTransitionIntegrityError(
                    "lifecycle event contains an unknown actor type"
                )
            if event.reason_code not in EXECUTION_TASK_REASON_CODES:
                raise ExecutionTaskTransitionIntegrityError(
                    "lifecycle event contains an unknown reason code"
                )
            try:
                _validate_transition_reason_contract(
                    from_state=event.from_state,
                    to_state=event.to_state,
                    reason_code=event.reason_code,
                    actor_type=event.actor_type,
                )
            except ExecutionTaskTransitionError as exc:
                # Existing Phase 29C-1 test-scoped structural histories are
                # valid compatibility fixtures; all amended production edges
                # still require their bounded authority reason.
                legacy_structural_terminal = (
                    event.actor_type == "test"
                    and event.from_state == "running"
                    and event.to_state in {"succeeded", "failed"}
                )
                if not legacy_structural_terminal:
                    raise ExecutionTaskTransitionIntegrityError(exc.message) from exc
            command = ExecutionTaskTransitionCommand(
                execution_task_id=task.id,
                execution_plan_id=task.execution_plan_id,
                expected_from_state=event.from_state,
                expected_state_version=event.expected_version,
                to_state=event.to_state,
                reason_code=event.reason_code,
                reason_detail=event.reason_detail,
                actor_type=event.actor_type,
                actor_id=event.actor_id,
                idempotency_key=event.command_id,
                runtime_attempt_id=event.runtime_attempt_id,
                runtime_lease_id=event.runtime_lease_id,
                runtime_ownership_fence=event.runtime_ownership_fence,
            )
            if event.canonical_command_hash != canonical_json_hash(
                _command_payload(command)
            ):
                raise ExecutionTaskTransitionIntegrityError(
                    "lifecycle event canonical command hash is invalid"
                )
            payload_hash = canonical_json_hash(_event_payload(event))
            if (
                event.canonical_payload_hash != payload_hash
                or event.event_hash != payload_hash
            ):
                raise ExecutionTaskTransitionIntegrityError(
                    "lifecycle event payload or event hash is invalid"
                )
            derived_state = event.to_state
            derived_version = event.resulting_version
            previous_hash = event.event_hash

        if task.status not in EXECUTION_TASK_STATES:
            raise ExecutionTaskTransitionIntegrityError(
                "Execution Task projection contains an unknown state"
            )
        if task.status != derived_state:
            raise ExecutionTaskTransitionIntegrityError(
                "Execution Task status projection does not match lifecycle replay"
            )
        if int(task.state_version) != derived_version:
            raise ExecutionTaskTransitionIntegrityError(
                "Execution Task version projection does not match lifecycle replay"
            )
        return ExecutionTaskLifecycleIntegrityResult(
            execution_task_id=task.id,
            execution_plan_id=task.execution_plan_id,
            event_count=len(events),
            current_state=derived_state,
            state_version=derived_version,
        )

    def verify_execution_plan_lifecycle_integrity(
        self, execution_plan_id: int
    ) -> ExecutionPlanLifecycleIntegrityResult:
        plan = self.db.get(ExecutionPlan, int(execution_plan_id))
        if plan is None:
            raise ExecutionTaskTransitionError(
                "execution_plan_not_found", "Execution Plan was not found"
            )
        tasks = (
            self.db.query(ExecutionTask)
            .filter(ExecutionTask.execution_plan_id == plan.id)
            .order_by(ExecutionTask.id.asc())
            .all()
        )
        for task in tasks:
            self.verify_task_lifecycle_integrity(task.id)
        return ExecutionPlanLifecycleIntegrityResult(
            execution_plan_id=plan.id,
            task_count=len(tasks),
        )

    def verify_plan_lifecycle_integrity(
        self, execution_plan_id: int
    ) -> ExecutionPlanLifecycleIntegrityResult:
        """Compatibility spelling for future plan-level callers."""

        return self.verify_execution_plan_lifecycle_integrity(execution_plan_id)

    def _events(self, execution_task_id: int) -> list[ExecutionTaskTransition]:
        return (
            self.db.query(ExecutionTaskTransition)
            .filter(ExecutionTaskTransition.execution_task_id == execution_task_id)
            .order_by(ExecutionTaskTransition.sequence.asc())
            .all()
        )

    def _verify_guarded_task_fences(
        self,
        target: ExecutionTask,
        fences: tuple[ExecutionTaskLifecycleFence, ...],
    ) -> None:
        for fence in fences:
            predecessor = self.db.get(ExecutionTask, fence.execution_task_id)
            if (
                predecessor is None
                or predecessor.execution_plan_id != target.execution_plan_id
                or predecessor.status != fence.expected_state
                or int(predecessor.state_version) != fence.expected_state_version
            ):
                raise ExecutionTaskTransitionError(
                    "transition_dependency_stale",
                    "a guarded predecessor lifecycle projection changed",
                )
            self.verify_task_lifecycle_integrity(predecessor.id)
            events = self._events(predecessor.id)
            current_head = (
                events[-1].event_hash if events else GENESIS_PREVIOUS_EVENT_HASH
            )
            if current_head != fence.lifecycle_head_hash:
                raise ExecutionTaskTransitionError(
                    "transition_dependency_stale",
                    "a guarded predecessor lifecycle head changed",
                )

    def _existing_event(
        self,
        execution_task_id: int,
        command: ExecutionTaskTransitionCommand,
    ) -> ExecutionTaskTransition | None:
        return (
            self.db.query(ExecutionTaskTransition)
            .filter(
                ExecutionTaskTransition.execution_task_id == execution_task_id,
                ExecutionTaskTransition.actor_type == command.actor_type,
                ExecutionTaskTransition.actor_id
                == (command.actor_id or command.actor_type),
                ExecutionTaskTransition.command_id == command.idempotency_key,
            )
            .one_or_none()
        )

    @staticmethod
    def _result(
        event: ExecutionTaskTransition, *, replayed: bool
    ) -> ExecutionTaskTransitionResult:
        return ExecutionTaskTransitionResult(
            execution_task_id=event.execution_task_id,
            execution_plan_id=event.execution_plan_id,
            plan_task_id=event.plan_task_id,
            event_id=event.id,
            sequence=event.sequence,
            from_state=event.from_state,
            to_state=event.to_state,
            expected_version=event.expected_version,
            resulting_version=event.resulting_version,
            event_hash=event.event_hash,
            replayed=replayed,
        )

    @staticmethod
    def _normalize_command(
        command: ExecutionTaskTransitionCommand,
    ) -> ExecutionTaskTransitionCommand:
        try:
            task_id = int(command.execution_task_id)
            expected_version = int(command.expected_state_version)
            plan_id = (
                int(command.execution_plan_id)
                if command.execution_plan_id is not None
                else None
            )
        except (TypeError, ValueError) as exc:
            raise ExecutionTaskTransitionError(
                "invalid_command",
                "task, plan, and version identifiers must be integers",
            ) from exc
        if task_id < 1 or expected_version < 0 or (plan_id is not None and plan_id < 1):
            raise ExecutionTaskTransitionError(
                "invalid_command", "task and plan identifiers must be positive"
            )
        expected_state = _bounded_text(
            command.expected_from_state, "expected_from_state", 20, required=True
        )
        if expected_state not in EXECUTION_TASK_STATES:
            raise ExecutionTaskTransitionError(
                "invalid_current_state",
                f"expected current state is not recognized: {expected_state!r}",
            )
        to_state = _bounded_text(command.to_state, "to_state", 20, required=True)
        if to_state not in EXECUTION_TASK_STATES:
            raise ExecutionTaskTransitionError(
                "invalid_requested_state",
                f"requested state is not recognized: {to_state!r}",
            )
        actor_type = _bounded_text(command.actor_type, "actor_type", 32, required=True)
        if actor_type not in EXECUTION_TASK_ACTOR_TYPES:
            raise ExecutionTaskTransitionError(
                "invalid_actor", f"actor type is not recognized: {actor_type!r}"
            )
        actor_id = _bounded_text(command.actor_id, "actor_id", 255, required=False)
        if not actor_id:
            actor_id = actor_type
        reason_code = _bounded_text(
            command.reason_code, "reason_code", 64, required=True
        )
        if reason_code not in EXECUTION_TASK_REASON_CODES:
            raise ExecutionTaskTransitionError(
                "invalid_reason", f"reason code is not recognized: {reason_code!r}"
            )
        reason_detail = (
            _bounded_text(command.reason_detail, "reason_detail", 1024, required=False)
            or None
        )
        idempotency_key = _bounded_text(
            command.idempotency_key, "idempotency_key", 128, required=True
        )
        fences: list[ExecutionTaskLifecycleFence] = []
        for raw_fence in command.guarded_task_fences or ():
            if isinstance(raw_fence, ExecutionTaskLifecycleFence):
                fence = raw_fence
            elif isinstance(raw_fence, Mapping):
                try:
                    fence = ExecutionTaskLifecycleFence(
                        execution_task_id=int(raw_fence["execution_task_id"]),
                        expected_state=str(raw_fence["expected_state"]),
                        expected_state_version=int(raw_fence["expected_state_version"]),
                        lifecycle_head_hash=raw_fence.get("lifecycle_head_hash"),
                    )
                except (KeyError, TypeError, ValueError) as exc:
                    raise ExecutionTaskTransitionError(
                        "invalid_command", "guarded task fence is malformed"
                    ) from exc
            else:
                raise ExecutionTaskTransitionError(
                    "invalid_command", "guarded task fence is malformed"
                )
            if (
                fence.execution_task_id < 1
                or fence.expected_state_version < 0
                or fence.expected_state not in EXECUTION_TASK_STATES
                or (
                    fence.lifecycle_head_hash is not None
                    and (
                        not isinstance(fence.lifecycle_head_hash, str)
                        or len(fence.lifecycle_head_hash) != 64
                    )
                )
            ):
                raise ExecutionTaskTransitionError(
                    "invalid_command", "guarded task fence is invalid"
                )
            fences.append(fence)
        if len({fence.execution_task_id for fence in fences}) != len(fences):
            raise ExecutionTaskTransitionError(
                "invalid_command", "guarded task fences contain duplicates"
            )
        fences.sort(key=lambda item: item.execution_task_id)
        try:
            runtime_attempt_id = (
                int(command.runtime_attempt_id)
                if command.runtime_attempt_id is not None
                else None
            )
            runtime_lease_id = (
                int(command.runtime_lease_id)
                if command.runtime_lease_id is not None
                else None
            )
            runtime_ownership_fence = (
                int(command.runtime_ownership_fence)
                if command.runtime_ownership_fence is not None
                else None
            )
        except (TypeError, ValueError) as exc:
            raise ExecutionTaskTransitionError(
                "invalid_command", "runtime ownership references are invalid"
            ) from exc
        if (
            (runtime_attempt_id is not None and runtime_attempt_id < 1)
            or (runtime_lease_id is not None and runtime_lease_id < 1)
            or (runtime_ownership_fence is not None and runtime_ownership_fence < 1)
        ):
            raise ExecutionTaskTransitionError(
                "invalid_command", "runtime ownership references are invalid"
            )
        return ExecutionTaskTransitionCommand(
            execution_task_id=task_id,
            execution_plan_id=plan_id,
            expected_from_state=expected_state,
            expected_state_version=expected_version,
            to_state=to_state,
            reason_code=reason_code,
            reason_detail=reason_detail,
            actor_type=actor_type,
            actor_id=actor_id,
            idempotency_key=idempotency_key,
            guarded_task_fences=tuple(fences),
            runtime_attempt_id=runtime_attempt_id,
            runtime_lease_id=runtime_lease_id,
            runtime_ownership_fence=runtime_ownership_fence,
        )
