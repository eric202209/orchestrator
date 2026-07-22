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
        "succeeded",
        "failed",
        "blocked",
        "paused",
        "cancelled",
        "skipped",
    }
)
TERMINAL_EXECUTION_TASK_STATES = frozenset({"succeeded", "cancelled", "skipped"})
ALLOWED_EXECUTION_TASK_TRANSITIONS: Mapping[str, frozenset[str]] = {
    "pending": frozenset({"ready", "blocked", "cancelled", "skipped"}),
    "ready": frozenset({"running", "blocked", "paused", "cancelled", "skipped"}),
    "running": frozenset({"succeeded", "failed", "paused", "cancelled"}),
    "blocked": frozenset({"ready", "cancelled", "skipped"}),
    "paused": frozenset({"ready", "running", "cancelled"}),
    "failed": frozenset({"ready", "cancelled"}),
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
        "execution_started",
        "execution_succeeded",
        "execution_failed",
        "retry_authorized",
        "operator_paused",
        "operator_cancelled",
        "operator_skipped",
        "resume_authorized",
        "review_gate_pending",
        "resource_unavailable",
        "system_reconciliation",
    }
)
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
    return {
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
    }


def _event_payload(event: ExecutionTaskTransition) -> dict[str, object]:
    return {
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


class ExecutionTaskTransitionService:
    """Validate and persist all lifecycle changes for an Execution Task."""

    def __init__(self, db: Session):
        self.db = db

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
        allowed = ALLOWED_EXECUTION_TASK_TRANSITIONS.get(task.status)
        if allowed is None or command.to_state not in allowed:
            raise ExecutionTaskTransitionError(
                "transition_not_allowed",
                f"transition {task.status!r} -> {command.to_state!r} is not allowed",
            )

        sequence = events[-1].sequence + 1 if events else 1
        previous_event_hash = (
            events[-1].event_hash if events else GENESIS_PREVIOUS_EVENT_HASH
        )
        resulting_version = command.expected_state_version + 1
        command_hash = canonical_json_hash(_command_payload(command))
        created_at = datetime.now(timezone.utc)
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
        )
