"""Phase 29D-4 Controlled Apply lifecycle closure.

The single orchestration boundary that, given one immutable Apply Result,
runs post-apply validation (for an ``applied`` result), derives a recovery
decision and attempts recovery when required, and performs the one
authorized canonical transition out of ``awaiting_apply``:

    awaiting_apply -> succeeded          (controlled_apply_verified)
    awaiting_apply -> awaiting_recovery  (controlled_apply_failed)

This module never mutates the workspace itself (that remains
``apply_execution``/``apply_recovery``'s job) and never invents a lifecycle
edge outside ``ExecutionTaskTransitionService``'s existing, reserved
contract.  Dependency release is not performed here: it is already
pull-based (``ExecutionEligibilityService.evaluate_task`` reads
``ExecutionTask.status`` live), so a verified ``succeeded`` transition is
sufficient for dependents to become eligible on their next evaluation.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models import ExecutionTask, ExecutionTaskApplyResult, ExecutionTaskTransition
from app.services.execution.apply_recovery import (
    DecideRecoveryCommand,
    ExecuteRecoveryCommand,
    RecoveryDecisionService,
    RecoveryExecutionService,
)
from app.services.execution.candidate_content import CandidateContentStore
from app.services.execution.execution_task_transition_service import (
    ExecutionTaskTransitionCommand,
    ExecutionTaskTransitionService,
)
from app.services.execution.post_apply_validation import (
    PostApplyValidationService,
    ValidatePostApplyCommand,
)


class ApplyLifecycleError(RuntimeError):
    """A bounded lifecycle-closure failure."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


LIFECYCLE_ACTOR_TYPE = "system"
LIFECYCLE_ACTOR_ID = "controlled-apply-lifecycle-v1"


@dataclass(frozen=True)
class CompleteControlledApplyCommand:
    apply_result_id: int
    actor_type: str = LIFECYCLE_ACTOR_TYPE
    actor_id: str = LIFECYCLE_ACTOR_ID


@dataclass(frozen=True)
class ApplyLifecycleOutcome:
    execution_task_id: int
    to_state: str
    transition_replayed: bool
    post_apply_validation_id: int | None
    recovery_decision_id: int | None
    recovery_result_id: int | None


class ExecutionTaskApplyLifecycleService:
    """Complete the Controlled Apply lifecycle for one Apply Result."""

    def __init__(
        self,
        db: Session,
        *,
        store: CandidateContentStore | None = None,
        now: Any = None,
    ):
        self.db = db
        self.store = store
        self._now = now

    def complete(
        self, command: CompleteControlledApplyCommand
    ) -> ApplyLifecycleOutcome:
        result = self.db.get(ExecutionTaskApplyResult, int(command.apply_result_id))
        if result is None:
            raise ApplyLifecycleError(
                "apply_result_missing", "apply result does not exist"
            )
        task = self.db.get(ExecutionTask, result.execution_task_id)
        if task is None:
            raise ApplyLifecycleError(
                "execution_task_missing", "owning execution task does not exist"
            )

        validation_id: int | None = None
        if result.status == "applied":
            validation = (
                PostApplyValidationService(self.db, store=self.store, now=self._now)
                .validate(ValidatePostApplyCommand(apply_result_id=result.id))
                .validation
            )
            validation_id = validation.id
            if validation.status == "passed":
                transition = self._transition(
                    task,
                    result_id=result.id,
                    to_state="succeeded",
                    reason_code="controlled_apply_verified",
                    command=command,
                )
                return ApplyLifecycleOutcome(
                    execution_task_id=task.id,
                    to_state=transition.to_state,
                    transition_replayed=transition.replayed,
                    post_apply_validation_id=validation_id,
                    recovery_decision_id=None,
                    recovery_result_id=None,
                )

        decision = (
            RecoveryDecisionService(self.db, now=self._now)
            .decide(DecideRecoveryCommand(apply_result_id=result.id))
            .decision
        )
        recovery_result = (
            RecoveryExecutionService(self.db, store=self.store, now=self._now)
            .execute(ExecuteRecoveryCommand(recovery_decision_id=decision.id))
            .result
        )
        transition = self._transition(
            task,
            result_id=result.id,
            to_state="awaiting_recovery",
            reason_code="controlled_apply_failed",
            command=command,
        )
        return ApplyLifecycleOutcome(
            execution_task_id=task.id,
            to_state=transition.to_state,
            transition_replayed=transition.replayed,
            post_apply_validation_id=validation_id,
            recovery_decision_id=decision.id,
            recovery_result_id=recovery_result.id,
        )

    def _transition(
        self,
        task: ExecutionTask,
        *,
        result_id: int,
        to_state: str,
        reason_code: str,
        command: CompleteControlledApplyCommand,
    ):
        idempotency_key = f"controlled-apply-lifecycle:{result_id}"
        service = ExecutionTaskTransitionService(self.db, now=self._now)
        existing = (
            self.db.query(ExecutionTaskTransition)
            .filter(
                ExecutionTaskTransition.execution_task_id == task.id,
                ExecutionTaskTransition.actor_type == command.actor_type,
                ExecutionTaskTransition.actor_id == command.actor_id,
                ExecutionTaskTransition.command_id == idempotency_key,
            )
            .one_or_none()
        )
        if existing is not None:
            return service.transition(
                ExecutionTaskTransitionCommand(
                    execution_task_id=task.id,
                    execution_plan_id=existing.execution_plan_id,
                    expected_from_state=existing.from_state,
                    expected_state_version=existing.expected_version,
                    to_state=existing.to_state,
                    reason_code=existing.reason_code,
                    reason_detail=existing.reason_detail,
                    actor_type=existing.actor_type,
                    actor_id=existing.actor_id,
                    idempotency_key=idempotency_key,
                )
            )
        if task.status != "awaiting_apply":
            raise ApplyLifecycleError(
                "execution_task_not_awaiting_apply",
                "controlled apply lifecycle closure requires an execution "
                "task in awaiting_apply",
            )
        return service.transition(
            ExecutionTaskTransitionCommand(
                execution_task_id=task.id,
                execution_plan_id=task.execution_plan_id,
                expected_from_state=task.status,
                expected_state_version=task.state_version,
                to_state=to_state,
                reason_code=reason_code,
                actor_type=command.actor_type,
                actor_id=command.actor_id,
                idempotency_key=idempotency_key,
            )
        )


__all__ = [
    "ApplyLifecycleError",
    "ApplyLifecycleOutcome",
    "CompleteControlledApplyCommand",
    "ExecutionTaskApplyLifecycleService",
    "LIFECYCLE_ACTOR_ID",
    "LIFECYCLE_ACTOR_TYPE",
]
