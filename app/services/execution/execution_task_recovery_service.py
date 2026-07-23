"""Phase 29C-8 recovery input, policy, authorization, and re-entry boundary.

This module is deliberately separate from the legacy Task/Session recovery
stack.  It evaluates only immutable Phase 29 runtime/validation evidence,
freezes a bounded strategy for a future attempt, and never invokes a provider,
model, scheduler, workspace, or repository operation.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timezone
import re
from typing import Any

from sqlalchemy import func
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ExecutionPlan,
    ExecutionTask,
    ExecutionTaskAcceptanceDecision,
    ExecutionTaskAttempt,
    ExecutionTaskAttemptOutcome,
    ExecutionTaskDispatchIntent,
    ExecutionTaskRecoveryAuthorization,
    ExecutionTaskRecoveryInput,
    ExecutionTaskRuntimeLease,
    ExecutionTaskRuntimeStart,
    ExecutionTaskTransition,
    ExecutionTaskValidationPredicateResult,
    ExecutionTaskValidationRun,
)
from app.services.execution.execution_task_transition_service import (
    ExecutionTaskTransitionCommand,
    ExecutionTaskTransitionError,
    ExecutionTaskTransitionService,
)
from app.services.planning.operator_review import canonical_json_hash


RECOVERY_INPUT_SCHEMA_VERSION = "execution-task-recovery-input/1"
RECOVERY_AUTHORIZATION_SCHEMA_VERSION = "execution-task-recovery-authorization/1"
DEFAULT_RECOVERY_POLICY_ID = "standard_execution_recovery"
DEFAULT_RECOVERY_POLICY_VERSION = 1
RECOVERY_SOURCES = frozenset({"runtime_attempt_failed", "validation_rejected"})
AUTHORIZATION_STATUSES = frozenset(
    {
        "authorized",
        "operator_required",
        "exhausted",
        "non_retryable",
        "blocked",
        "error",
        "cancelled",
    }
)
_HASH_RE = re.compile(r"^[0-9a-f]{64}$")
_CONTROL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")


class ExecutionTaskRecoveryError(RuntimeError):
    """Bounded error at the Phase 29 recovery authority."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class RecoveryPolicySpecification:
    policy_id: str = DEFAULT_RECOVERY_POLICY_ID
    policy_version: int = DEFAULT_RECOVERY_POLICY_VERSION
    max_attempts: int = 3
    allowed_sources: tuple[str, ...] = tuple(sorted(RECOVERY_SOURCES))
    strategy_order: tuple[str, ...] = ("same_input_retry",)
    non_retryable_failure_codes: tuple[str, ...] = (
        "invalid_request",
        "invalid_task_input",
        "permission_denied",
        "unsupported_task",
        "safety_blocked",
        "task_cancelled",
        "contract_invalid",
        "validation_contract_invalid",
        "candidate_contract_violation",
    )
    operator_required_failure_codes: tuple[str, ...] = (
        "operator_required",
        "manual_intervention_required",
    )
    retryable_runtime_categories: tuple[str, ...] = (
        "execution_timeout",
        "backend_timeout",
        "provider_timeout",
        "provider_unavailable",
        "provider_protocol_error",
        "runtime_exception",
        "worker_lost",
    )
    retryable_validation_codes: tuple[str, ...] = ("validation_predicate_failed",)
    backoff_policy_id: str | None = None
    backoff_policy_version: int | None = None

    def __post_init__(self) -> None:
        if not self.policy_id or self.policy_version < 1 or self.max_attempts < 1:
            raise ValueError("recovery policy identity and max_attempts are invalid")
        if not self.allowed_sources:
            raise ValueError("recovery policy must name at least one source")

    @property
    def canonical_payload(self) -> dict[str, object]:
        return {
            "schema_version": "recovery-policy/1",
            "policy_id": self.policy_id,
            "policy_version": int(self.policy_version),
            "max_attempts": int(self.max_attempts),
            "allowed_sources": list(self.allowed_sources),
            "strategy_order": list(self.strategy_order),
            "non_retryable_failure_codes": list(self.non_retryable_failure_codes),
            "operator_required_failure_codes": list(
                self.operator_required_failure_codes
            ),
            "retryable_runtime_categories": list(self.retryable_runtime_categories),
            "retryable_validation_codes": list(self.retryable_validation_codes),
            "backoff_policy_id": self.backoff_policy_id,
            "backoff_policy_version": self.backoff_policy_version,
        }

    @property
    def canonical_hash(self) -> str:
        return canonical_json_hash(self.canonical_payload)


@dataclass(frozen=True)
class RecoveryStrategySpecification:
    strategy_id: str
    strategy_version: int
    supported: bool = True
    provider_dependent: bool = False
    workspace_mutating: bool = False

    def freeze_parameters(
        self, parameters: Mapping[str, object] | None = None
    ) -> dict[str, object]:
        supplied = dict(parameters or {})
        if self.strategy_id == "same_input_retry":
            if supplied and supplied != {"input_mode": "same_released_task"}:
                raise ExecutionTaskRecoveryError(
                    "recovery_strategy_not_authorized",
                    "same_input_retry accepts no semantic input changes",
                )
            return {"input_mode": "same_released_task", "content_mutation": "none"}
        if self.strategy_id == "operator_intervention":
            return {"operator_authorization": "separate_exact_recovery_decision"}
        if self.strategy_id == "terminal_no_retry":
            return {"terminal": "policy_authorized_no_replacement"}
        raise ExecutionTaskRecoveryError(
            "recovery_strategy_unsupported", "strategy has no Phase 29 freeze semantics"
        )


class RecoveryStrategyRegistry:
    """Versioned freeze-only registry; it has no provider execution method."""

    def __init__(self, strategies: Sequence[RecoveryStrategySpecification] = ()):
        self._strategies: dict[tuple[str, int], RecoveryStrategySpecification] = {}
        for strategy in strategies:
            self.register(strategy)

    def register(self, strategy: RecoveryStrategySpecification) -> None:
        if not strategy.strategy_id or strategy.strategy_version < 1:
            raise ExecutionTaskRecoveryError(
                "recovery_strategy_unsupported", "strategy id and version are required"
            )
        key = (strategy.strategy_id, int(strategy.strategy_version))
        if key in self._strategies:
            raise ExecutionTaskRecoveryError(
                "recovery_strategy_duplicate", "strategy identity is already registered"
            )
        self._strategies[key] = strategy

    def resolve(
        self, strategy_id: str, strategy_version: int = 1
    ) -> RecoveryStrategySpecification:
        if not strategy_id or int(strategy_version) < 1:
            raise ExecutionTaskRecoveryError(
                "recovery_strategy_unsupported",
                "versioned strategy identity is required",
            )
        strategy = self._strategies.get((strategy_id, int(strategy_version)))
        if strategy is None or not strategy.supported:
            raise ExecutionTaskRecoveryError(
                "recovery_strategy_unsupported", "strategy is not supported by Phase 29"
            )
        if strategy.provider_dependent or strategy.workspace_mutating:
            raise ExecutionTaskRecoveryError(
                "recovery_strategy_not_authorized",
                "provider- or workspace-dependent strategy cannot authorize here",
            )
        return strategy


def build_default_recovery_strategy_registry() -> RecoveryStrategyRegistry:
    return RecoveryStrategyRegistry(
        (
            RecoveryStrategySpecification("same_input_retry", 1),
            RecoveryStrategySpecification("operator_intervention", 1),
            RecoveryStrategySpecification("terminal_no_retry", 1),
        )
    )


@dataclass(frozen=True)
class RecoveryPolicyEvaluation:
    status: str
    classification: str
    reason: str
    retry_budget_before: int
    retry_budget_after: int
    next_attempt_generation: int | None
    strategy_id: str | None = None
    strategy_version: int | None = None
    strategy_parameters: dict[str, object] | None = None
    operator_required: bool = False
    not_before: datetime | None = None


def classify_recovery_input(
    recovery_input: ExecutionTaskRecoveryInput,
    policy: RecoveryPolicySpecification,
) -> str:
    code = str(recovery_input.failure_code or "")
    category = str(recovery_input.failure_category or "")
    if code in policy.operator_required_failure_codes:
        return "operator_required"
    if code in policy.non_retryable_failure_codes:
        return (
            "non_retryable_runtime_failure"
            if recovery_input.recovery_source == "runtime_attempt_failed"
            else "validation_rejection_non_retryable"
        )
    if recovery_input.recovery_source == "runtime_attempt_failed":
        if category in {"provider_unavailable", "worker_lost"}:
            return "infrastructure_failure"
        if category in policy.retryable_runtime_categories:
            return "retryable_runtime_failure"
        return "unknown_failure"
    if recovery_input.recovery_source == "validation_rejected":
        if code in policy.retryable_validation_codes or code in {
            "",
            "validation_rejection",
            "validation_predicate_failed",
        }:
            return "validation_rejection_retryable"
        return "unknown_failure"
    return "unknown_failure"


def evaluate_recovery_policy(
    recovery_input: ExecutionTaskRecoveryInput,
    policy: RecoveryPolicySpecification,
    *,
    registry: RecoveryStrategyRegistry | None = None,
) -> RecoveryPolicyEvaluation:
    registry = registry or build_default_recovery_strategy_registry()
    generation = int(recovery_input.attempt_generation)
    before = max(int(policy.max_attempts) - generation, 0)
    if recovery_input.recovery_source not in policy.allowed_sources:
        return RecoveryPolicyEvaluation(
            "policy_blocked",
            "unknown_failure",
            "recovery_source_unsupported",
            before,
            before,
            None,
        )
    classification = classify_recovery_input(recovery_input, policy)
    if classification == "operator_required":
        strategy = registry.resolve("operator_intervention", 1)
        parameters = strategy.freeze_parameters()
        return RecoveryPolicyEvaluation(
            "operator_required",
            classification,
            "operator_required",
            before,
            before,
            None,
            strategy.strategy_id,
            strategy.strategy_version,
            parameters,
            True,
        )
    if classification == "unknown_failure":
        return RecoveryPolicyEvaluation(
            "policy_blocked", classification, "unknown_failure", before, before, None
        )
    if classification.startswith("non_retryable"):
        strategy = registry.resolve("terminal_no_retry", 1)
        parameters = strategy.freeze_parameters()
        return RecoveryPolicyEvaluation(
            "non_retryable",
            classification,
            "failure_non_retryable",
            before,
            before,
            None,
            strategy.strategy_id,
            strategy.strategy_version,
            parameters,
        )
    if before <= 0:
        strategy = registry.resolve("terminal_no_retry", 1)
        parameters = strategy.freeze_parameters()
        return RecoveryPolicyEvaluation(
            "exhausted",
            classification,
            "retry_budget_exhausted",
            before,
            before,
            None,
            strategy.strategy_id,
            strategy.strategy_version,
            parameters,
        )
    for strategy_id in policy.strategy_order:
        try:
            strategy = registry.resolve(strategy_id, 1)
            parameters = strategy.freeze_parameters()
        except ExecutionTaskRecoveryError:
            continue
        next_generation = generation + 1
        return RecoveryPolicyEvaluation(
            "retry_authorized",
            classification,
            "retry_authorized",
            before,
            max(int(policy.max_attempts) - next_generation, 0),
            next_generation,
            strategy.strategy_id,
            strategy.strategy_version,
            parameters,
        )
    return RecoveryPolicyEvaluation(
        "policy_blocked",
        classification,
        "strategy_unsupported",
        before,
        before,
        None,
    )


@dataclass(frozen=True)
class CreateRecoveryInputCommand:
    execution_task_id: int
    failed_attempt_id: int
    recovery_source: str
    expected_task_state: str = "awaiting_recovery"
    expected_task_state_version: int | None = None
    runtime_outcome_id: int | None = None
    validation_run_id: int | None = None
    acceptance_decision_id: int | None = None
    input_idempotency_key: str = ""
    creation_actor_type: str = "recovery"
    creation_actor_id: str = "recovery-service"


@dataclass(frozen=True)
class AuthorizeRecoveryCommand:
    recovery_input_id: int
    expected_task_state: str = "awaiting_recovery"
    expected_task_state_version: int | None = None
    authorization_idempotency_key: str = ""
    expected_policy_id: str | None = None
    expected_policy_version: int | None = None
    decision_actor_type: str = "recovery"
    decision_actor_id: str = "recovery-service"


@dataclass(frozen=True)
class RecoveryInputResult:
    recovery_input: ExecutionTaskRecoveryInput
    replayed: bool = False


@dataclass(frozen=True)
class RecoveryAuthorizationResult:
    authorization: ExecutionTaskRecoveryAuthorization
    replacement_attempt: ExecutionTaskAttempt | None = None
    transition: ExecutionTaskTransition | None = None
    replayed: bool = False


@dataclass(frozen=True)
class RecoveryIntegrityResult:
    execution_plan_id: int | None
    execution_task_id: int | None
    verified: bool
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class RecoveryInspectionProjection:
    execution_plan_id: int
    execution_task_id: int
    state: str
    recovery_input_id: int | None
    authorization_id: int | None
    replacement_attempt_id: int | None
    reasons: tuple[str, ...] = ()

    def to_dict(self) -> dict[str, object]:
        return {
            "execution_plan_id": self.execution_plan_id,
            "execution_task_id": self.execution_task_id,
            "state": self.state,
            "recovery_input_id": self.recovery_input_id,
            "authorization_id": self.authorization_id,
            "replacement_attempt_id": self.replacement_attempt_id,
            "reasons": list(self.reasons),
        }


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _text(value: object, field: str, limit: int) -> str:
    result = str(value or "").strip()
    if not result or len(result) > limit or _CONTROL_RE.search(result):
        raise ExecutionTaskRecoveryError(
            "recovery_integrity_failure", f"{field} is missing or malformed"
        )
    return result


def _bounded_id(value: object | None, limit: int = 255) -> str | None:
    if value is None:
        return None
    result = str(value).strip()
    if not result or len(result) > limit or _CONTROL_RE.search(result):
        return None
    return result


def _valid_hash(value: object | None) -> bool:
    return bool(_HASH_RE.fullmatch(str(value or "").lower()))


class ExecutionTaskRecoveryService:
    """Own Phase 29 recovery authority, without provider execution."""

    def __init__(
        self,
        db: Session,
        *,
        now=None,
        policies: Sequence[RecoveryPolicySpecification] = (),
        strategy_registry: RecoveryStrategyRegistry | None = None,
    ):
        self.db = db
        self._now = now or (lambda: datetime.now(timezone.utc))
        self._policies = {
            (policy.policy_id, int(policy.policy_version)): policy
            for policy in policies
        }
        default = RecoveryPolicySpecification()
        self._policies.setdefault((default.policy_id, default.policy_version), default)
        self.strategy_registry = (
            strategy_registry or build_default_recovery_strategy_registry()
        )

    # -- immutable input -------------------------------------------------

    def create_recovery_input(
        self, command: CreateRecoveryInputCommand
    ) -> RecoveryInputResult:
        key = _text(command.input_idempotency_key, "input_idempotency_key", 128)
        source = _text(command.recovery_source, "recovery_source", 64)
        if source not in RECOVERY_SOURCES:
            raise ExecutionTaskRecoveryError(
                "recovery_source_unsupported",
                "source is not an authoritative Phase 29 source",
            )
        prior = (
            self.db.query(ExecutionTaskRecoveryInput)
            .filter(ExecutionTaskRecoveryInput.input_idempotency_key == key)
            .one_or_none()
        )
        task = (
            self.db.query(ExecutionTask)
            .filter(ExecutionTask.id == int(command.execution_task_id))
            .with_for_update()
            .one_or_none()
        )
        if task is None:
            raise ExecutionTaskRecoveryError(
                "execution_task_not_found", "Execution Task was not found"
            )
        plan = (
            self.db.query(ExecutionPlan)
            .filter(ExecutionPlan.id == task.execution_plan_id)
            .with_for_update()
            .one_or_none()
        )
        if plan is None:
            raise ExecutionTaskRecoveryError(
                "recovery_integrity_failure", "Execution Plan was not found"
            )
        if plan.status != "active":
            raise ExecutionTaskRecoveryError(
                "execution_plan_inactive", "recovery requires an active plan"
            )
        if task.status != command.expected_task_state:
            raise ExecutionTaskRecoveryError(
                "task_not_awaiting_recovery", "task is not awaiting recovery"
            )
        if command.expected_task_state_version is not None and int(
            task.state_version
        ) != int(command.expected_task_state_version):
            raise ExecutionTaskRecoveryError(
                "task_state_version_stale", "task state version is stale"
            )
        attempt = self.db.get(ExecutionTaskAttempt, int(command.failed_attempt_id))
        if (
            attempt is None
            or attempt.execution_task_id != task.id
            or attempt.execution_plan_id != plan.id
        ):
            raise ExecutionTaskRecoveryError(
                "recovery_source_not_authoritative", "failed attempt is not task-bound"
            )
        transition = self._authoritative_recovery_transition(task, attempt, source)
        source_fields = self._source_fields(command, task, attempt, source)
        generation = int(attempt.attempt_number)
        existing_generation = (
            self.db.query(ExecutionTaskRecoveryInput)
            .filter(
                ExecutionTaskRecoveryInput.execution_task_id == task.id,
                ExecutionTaskRecoveryInput.recovery_generation == generation,
            )
            .one_or_none()
        )
        payload = self._input_payload(
            plan, task, attempt, transition, source, generation, source_fields
        )
        payload_hash = canonical_json_hash(payload)
        if prior is not None:
            if prior.canonical_input_hash != payload_hash:
                raise ExecutionTaskRecoveryError(
                    "recovery_input_idempotency_conflict",
                    "input idempotency key is bound to different source evidence",
                )
            self.verify_recovery_input_integrity(prior.id, raise_on_error=True)
            return RecoveryInputResult(prior, replayed=True)
        if existing_generation is not None:
            raise ExecutionTaskRecoveryError(
                "recovery_input_already_exists",
                "recovery generation already has an input",
            )
        prior_auth = (
            self.db.query(ExecutionTaskRecoveryAuthorization)
            .filter(
                ExecutionTaskRecoveryAuthorization.execution_task_id == task.id,
                ExecutionTaskRecoveryAuthorization.recovery_generation < generation,
            )
            .order_by(ExecutionTaskRecoveryAuthorization.recovery_generation.desc())
            .first()
        )
        row = ExecutionTaskRecoveryInput(
            execution_plan_id=plan.id,
            execution_task_id=task.id,
            failed_attempt_id=attempt.id,
            attempt_generation=generation,
            runtime_outcome_id=source_fields.get("runtime_outcome_id"),
            validation_run_id=source_fields.get("validation_run_id"),
            acceptance_decision_id=source_fields.get("acceptance_decision_id"),
            recovery_source=source,
            failure_category=str(source_fields["failure_category"]),
            failure_code=source_fields.get("failure_code"),
            exception_type=source_fields.get("exception_type"),
            provider_request_id=source_fields.get("provider_request_id"),
            failed_predicate_summary=source_fields.get("failed_predicate_summary"),
            aggregate_evidence_hash=source_fields.get("aggregate_evidence_hash"),
            aggregate_predicate_result_hash=source_fields.get(
                "aggregate_predicate_result_hash"
            ),
            lifecycle_transition_id=transition.id,
            lifecycle_transition_sequence=transition.sequence,
            task_state_at_creation=task.status,
            task_state_version_at_creation=int(task.state_version),
            prior_recovery_authorization_id=prior_auth.id if prior_auth else None,
            retry_count=max(generation - 1, 0),
            recovery_generation=generation,
            canonical_input_payload=payload,
            canonical_input_hash=payload_hash,
            input_idempotency_key=key,
            creation_actor_type=_text(
                command.creation_actor_type, "creation_actor_type", 64
            ),
            creation_actor_id=_text(
                command.creation_actor_id, "creation_actor_id", 255
            ),
            created_at=_utc(self._now()),
        )
        try:
            with self.db.begin_nested():
                self.db.add(row)
                self.db.flush()
        except IntegrityError as exc:
            raise ExecutionTaskRecoveryError(
                "recovery_input_already_exists", "recovery input uniqueness was fenced"
            ) from exc
        return RecoveryInputResult(row)

    def _authoritative_recovery_transition(
        self, task: ExecutionTask, attempt: ExecutionTaskAttempt, source: str
    ) -> ExecutionTaskTransition:
        transition = (
            self.db.query(ExecutionTaskTransition)
            .filter(
                ExecutionTaskTransition.execution_task_id == task.id,
                ExecutionTaskTransition.to_state == "awaiting_recovery",
            )
            .order_by(ExecutionTaskTransition.sequence.desc())
            .first()
        )
        expected_reason = (
            "runtime_attempt_failed"
            if source == "runtime_attempt_failed"
            else "validation_rejected"
        )
        if transition is None or transition.sequence != int(task.state_version):
            raise ExecutionTaskRecoveryError(
                "recovery_source_not_authoritative",
                "exact awaiting_recovery transition is missing",
            )
        expected_from_state = (
            "running" if source == "runtime_attempt_failed" else "awaiting_validation"
        )
        if transition.from_state != expected_from_state:
            raise ExecutionTaskRecoveryError(
                "recovery_source_not_authoritative",
                "awaiting_recovery transition has the wrong predecessor state",
            )
        if transition.reason_code != expected_reason:
            raise ExecutionTaskRecoveryError(
                "recovery_source_not_authoritative",
                "transition reason is not an accepted recovery source",
            )
        if (
            source == "runtime_attempt_failed"
            and transition.runtime_attempt_id != attempt.id
        ):
            raise ExecutionTaskRecoveryError(
                "runtime_failure_integrity_invalid",
                "runtime transition does not bind the failed attempt",
            )
        return transition

    def _source_fields(
        self,
        command: CreateRecoveryInputCommand,
        task: ExecutionTask,
        attempt: ExecutionTaskAttempt,
        source: str,
    ) -> dict[str, Any]:
        if source == "runtime_attempt_failed":
            if command.runtime_outcome_id is None:
                raise ExecutionTaskRecoveryError(
                    "runtime_failure_integrity_invalid",
                    "runtime failure requires an outcome id",
                )
            outcome = self.db.get(
                ExecutionTaskAttemptOutcome, int(command.runtime_outcome_id)
            )
            if (
                outcome is None
                or outcome.execution_task_id != task.id
                or outcome.execution_task_attempt_id != attempt.id
            ):
                raise ExecutionTaskRecoveryError(
                    "runtime_failure_integrity_invalid",
                    "runtime outcome identity is invalid",
                )
            if (
                outcome.outcome_status != "attempt_failed"
                or attempt.attempt_status != "failed"
            ):
                raise ExecutionTaskRecoveryError(
                    "recovery_source_not_authoritative",
                    "outcome is not an authoritative attempt failure",
                )
            from app.services.execution.execution_task_runtime_execution_service import (
                ExecutionTaskRuntimeExecutionService,
            )

            integrity = ExecutionTaskRuntimeExecutionService(
                self.db
            ).verify_attempt_outcome_integrity(outcome.id)
            if not integrity.verified:
                raise ExecutionTaskRecoveryError(
                    "runtime_failure_integrity_invalid",
                    "runtime failure evidence failed integrity verification",
                )
            return {
                "runtime_outcome_id": outcome.id,
                "failure_category": outcome.failure_category or "unknown_failure",
                "failure_code": outcome.failure_code,
                "exception_type": outcome.exception_type,
                "provider_request_id": _bounded_id(outcome.provider_request_id),
            }
        if command.validation_run_id is None or command.acceptance_decision_id is None:
            raise ExecutionTaskRecoveryError(
                "validation_rejection_integrity_invalid",
                "validation rejection requires validation run and acceptance decision",
            )
        run = self.db.get(ExecutionTaskValidationRun, int(command.validation_run_id))
        decision = self.db.get(
            ExecutionTaskAcceptanceDecision, int(command.acceptance_decision_id)
        )
        if (
            run is None
            or decision is None
            or run.execution_task_id != task.id
            or decision.execution_task_id != task.id
        ):
            raise ExecutionTaskRecoveryError(
                "validation_rejection_integrity_invalid",
                "validation evidence identity is invalid",
            )
        if (
            run.execution_task_attempt_id != attempt.id
            or decision.execution_task_attempt_id != attempt.id
            or decision.validation_run_id != run.id
            or run.final_validation_classification != "rejected"
            or decision.decision_status != "rejected"
            or run.pass_policy_result != "failed"
        ):
            raise ExecutionTaskRecoveryError(
                "recovery_source_not_authoritative",
                "validation row is not an authoritative rejection",
            )
        from app.services.execution.validation_run import ValidationRunService

        integrity = ValidationRunService(self.db).verify_acceptance_decision_integrity(
            decision.id
        )
        if not integrity.verified:
            raise ExecutionTaskRecoveryError(
                "validation_rejection_integrity_invalid",
                "validation rejection failed integrity verification",
            )
        failed = (
            self.db.query(ExecutionTaskValidationPredicateResult)
            .filter(
                ExecutionTaskValidationPredicateResult.candidate_outcome_id
                == run.candidate_outcome_id,
                ExecutionTaskValidationPredicateResult.validation_specification_id
                == run.validation_specification_id,
                ExecutionTaskValidationPredicateResult.result_status == "failed",
            )
            .order_by(
                ExecutionTaskValidationPredicateResult.predicate_order.asc(),
                ExecutionTaskValidationPredicateResult.id.asc(),
            )
            .limit(32)
            .all()
        )
        return {
            "validation_run_id": run.id,
            "acceptance_decision_id": decision.id,
            "failure_category": "validation_rejection",
            "failure_code": "validation_predicate_failed",
            "failed_predicate_summary": [
                {
                    "predicate_id": item.predicate_id,
                    "predicate_version": int(item.predicate_version),
                    "result_code": item.result_code,
                }
                for item in failed
            ],
            "aggregate_evidence_hash": run.aggregate_evidence_hash,
            "aggregate_predicate_result_hash": run.aggregate_predicate_result_hash,
        }

    @staticmethod
    def _input_payload(
        plan: ExecutionPlan,
        task: ExecutionTask,
        attempt: ExecutionTaskAttempt,
        transition: ExecutionTaskTransition,
        source: str,
        generation: int,
        source_fields: Mapping[str, Any],
    ) -> dict[str, object]:
        return {
            "schema_version": RECOVERY_INPUT_SCHEMA_VERSION,
            "execution_plan_id": int(plan.id),
            "execution_task_id": int(task.id),
            "failed_attempt_id": int(attempt.id),
            "attempt_generation": generation,
            "recovery_generation": generation,
            "recovery_source": source,
            "runtime_outcome_id": source_fields.get("runtime_outcome_id"),
            "validation_run_id": source_fields.get("validation_run_id"),
            "acceptance_decision_id": source_fields.get("acceptance_decision_id"),
            "failure_category": source_fields.get("failure_category"),
            "failure_code": source_fields.get("failure_code"),
            "exception_type": source_fields.get("exception_type"),
            "provider_request_id": source_fields.get("provider_request_id"),
            "failed_predicate_summary": source_fields.get("failed_predicate_summary"),
            "aggregate_evidence_hash": source_fields.get("aggregate_evidence_hash"),
            "aggregate_predicate_result_hash": source_fields.get(
                "aggregate_predicate_result_hash"
            ),
            "lifecycle_transition_id": int(transition.id),
            "lifecycle_transition_sequence": int(transition.sequence),
            "task_state": task.status,
            "task_state_version": int(task.state_version),
            "retry_count": max(generation - 1, 0),
        }

    # -- policy and authorization ---------------------------------------

    def resolve_policy(self, plan: ExecutionPlan) -> RecoveryPolicySpecification:
        policy_id = plan.recovery_policy_id or DEFAULT_RECOVERY_POLICY_ID
        version = int(plan.recovery_policy_version or DEFAULT_RECOVERY_POLICY_VERSION)
        policy = self._policies.get((policy_id, version))
        if policy is None:
            raise ExecutionTaskRecoveryError(
                "recovery_policy_version_unsupported",
                "frozen recovery policy is unavailable",
            )
        return policy

    def authorize_recovery(
        self, command: AuthorizeRecoveryCommand
    ) -> RecoveryAuthorizationResult:
        key = _text(
            command.authorization_idempotency_key,
            "authorization_idempotency_key",
            128,
        )
        recovery_input = self.db.get(
            ExecutionTaskRecoveryInput, int(command.recovery_input_id)
        )
        if recovery_input is None:
            raise ExecutionTaskRecoveryError(
                "recovery_input_not_found", "recovery input was not found"
            )
        task = (
            self.db.query(ExecutionTask)
            .filter(ExecutionTask.id == recovery_input.execution_task_id)
            .with_for_update()
            .one_or_none()
        )
        if task is None:
            raise ExecutionTaskRecoveryError(
                "execution_task_not_found", "Execution Task was not found"
            )
        plan = (
            self.db.query(ExecutionPlan)
            .filter(ExecutionPlan.id == task.execution_plan_id)
            .with_for_update()
            .one_or_none()
        )
        if plan is None or plan.status != "active":
            raise ExecutionTaskRecoveryError(
                "execution_plan_inactive", "recovery requires an active plan"
            )
        input_integrity = self.verify_recovery_input_integrity(recovery_input.id)
        if not input_integrity.verified:
            raise ExecutionTaskRecoveryError(
                "recovery_integrity_failure",
                "recovery input failed integrity verification",
            )
        policy = self.resolve_policy(plan)
        if (
            command.expected_policy_id is not None
            and command.expected_policy_id != policy.policy_id
        ):
            raise ExecutionTaskRecoveryError(
                "recovery_policy_missing", "policy identity does not match"
            )
        if command.expected_policy_version is not None and int(
            command.expected_policy_version
        ) != int(policy.policy_version):
            raise ExecutionTaskRecoveryError(
                "recovery_policy_version_unsupported", "policy version does not match"
            )
        evaluation = evaluate_recovery_policy(
            recovery_input, policy, registry=self.strategy_registry
        )
        authorization_status = (
            "authorized"
            if evaluation.status == "retry_authorized"
            else (
                "blocked"
                if evaluation.status == "policy_blocked"
                else evaluation.status
            )
        )
        parameters = evaluation.strategy_parameters
        parameter_hash = (
            canonical_json_hash(parameters) if parameters is not None else None
        )
        command_payload = {
            "schema_version": RECOVERY_AUTHORIZATION_SCHEMA_VERSION,
            "recovery_input_id": int(recovery_input.id),
            "recovery_input_hash": recovery_input.canonical_input_hash,
            "execution_plan_id": int(plan.id),
            "execution_task_id": int(task.id),
            "expected_task_state": command.expected_task_state,
            "expected_task_state_version": (
                int(command.expected_task_state_version)
                if command.expected_task_state_version is not None
                else int(task.state_version)
            ),
            "policy_id": policy.policy_id,
            "policy_version": int(policy.policy_version),
            "strategy_id": evaluation.strategy_id,
            "strategy_version": evaluation.strategy_version,
            "authorization_status": evaluation.status,
            "decision_classification": evaluation.classification,
            "decision_reason": evaluation.reason,
            "retry_budget_before": evaluation.retry_budget_before,
            "retry_budget_after": evaluation.retry_budget_after,
            "next_attempt_generation": evaluation.next_attempt_generation,
            "strategy_parameters": parameters,
            "strategy_parameter_hash": parameter_hash,
            "operator_required": evaluation.operator_required,
            "decision_actor_type": command.decision_actor_type,
            "decision_actor_id": command.decision_actor_id,
        }
        command_hash = canonical_json_hash(command_payload)
        existing_key = (
            self.db.query(ExecutionTaskRecoveryAuthorization)
            .filter(
                ExecutionTaskRecoveryAuthorization.authorization_idempotency_key == key
            )
            .one_or_none()
        )
        if existing_key is not None:
            if existing_key.canonical_authorization_command_hash != command_hash:
                raise ExecutionTaskRecoveryError(
                    "recovery_authorization_idempotency_conflict",
                    "authorization key is bound to another decision",
                )
            self.verify_recovery_authorization_integrity(
                existing_key.id, raise_on_error=True
            )
            return RecoveryAuthorizationResult(
                existing_key,
                existing_key.replacement_attempt,
                existing_key.lifecycle_transition,
                replayed=True,
            )
        existing_input = (
            self.db.query(ExecutionTaskRecoveryAuthorization)
            .filter(
                ExecutionTaskRecoveryAuthorization.recovery_input_id
                == recovery_input.id
            )
            .one_or_none()
        )
        if existing_input is not None:
            raise ExecutionTaskRecoveryError(
                "recovery_authorization_already_exists",
                "recovery generation already has a canonical authorization",
            )
        if task.status != command.expected_task_state:
            raise ExecutionTaskRecoveryError(
                "task_not_awaiting_recovery", "task is not awaiting recovery"
            )
        expected_version = (
            int(command.expected_task_state_version)
            if command.expected_task_state_version is not None
            else int(task.state_version)
        )
        if int(task.state_version) != expected_version:
            raise ExecutionTaskRecoveryError(
                "task_state_version_stale", "task state version is stale"
            )
        result_state = task.status
        result_version = int(task.state_version)
        authorization_payload = {
            "schema_version": RECOVERY_AUTHORIZATION_SCHEMA_VERSION,
            "recovery_input_hash": recovery_input.canonical_input_hash,
            "policy_id": policy.policy_id,
            "policy_version": int(policy.policy_version),
            "strategy_id": evaluation.strategy_id,
            "strategy_version": evaluation.strategy_version,
            "authorization_status": evaluation.status,
            "decision_classification": evaluation.classification,
            "decision_reason": evaluation.reason,
            "retry_budget_before": evaluation.retry_budget_before,
            "retry_budget_after": evaluation.retry_budget_after,
            "next_attempt_generation": evaluation.next_attempt_generation,
            "strategy_parameters": parameters,
            "strategy_parameter_hash": parameter_hash,
            "operator_required": evaluation.operator_required,
            "resulting_task_state": result_state,
            "resulting_task_state_version": result_version,
        }
        authorization = ExecutionTaskRecoveryAuthorization(
            execution_plan_id=plan.id,
            execution_task_id=task.id,
            recovery_input_id=recovery_input.id,
            failed_attempt_id=recovery_input.failed_attempt_id,
            recovery_generation=recovery_input.recovery_generation,
            policy_id=policy.policy_id,
            policy_version=policy.policy_version,
            strategy_id=evaluation.strategy_id,
            strategy_version=evaluation.strategy_version,
            authorization_status=authorization_status,
            decision_classification=evaluation.classification,
            decision_reason=evaluation.reason,
            retry_budget_before=evaluation.retry_budget_before,
            retry_budget_after=evaluation.retry_budget_after,
            next_attempt_generation=evaluation.next_attempt_generation,
            strategy_parameters=parameters,
            strategy_parameter_hash=parameter_hash,
            not_before=evaluation.not_before,
            backoff_policy_id=policy.backoff_policy_id,
            backoff_policy_version=policy.backoff_policy_version,
            operator_required=evaluation.operator_required,
            authorization_idempotency_key=key,
            deterministic_authorization_command_id=f"execution-recovery-command-{task.id}-{recovery_input.recovery_generation}-{command_hash[:24]}",
            canonical_authorization_command_payload=command_payload,
            canonical_authorization_command_hash=command_hash,
            canonical_authorization_payload=authorization_payload,
            canonical_authorization_hash=canonical_json_hash(authorization_payload),
            resulting_task_state=result_state,
            resulting_task_state_version=result_version,
            decision_actor_type=_text(
                command.decision_actor_type, "decision_actor_type", 64
            ),
            decision_actor_id=_text(
                command.decision_actor_id, "decision_actor_id", 255
            ),
            authorized_at=_utc(self._now()),
            created_at=_utc(self._now()),
        )
        replacement: ExecutionTaskAttempt | None = None
        transition: ExecutionTaskTransition | None = None
        try:
            with self.db.begin_nested():
                # Persist the authorization row first so the replacement
                # attempt can bind its immutable lineage to a real authority
                # id.  The savepoint makes the complete sequence atomic.
                self.db.add(authorization)
                self.db.flush()
                if evaluation.status == "retry_authorized":
                    self._assert_attempt_generation(task, recovery_input)
                    replacement = self._create_replacement_attempt(
                        task, recovery_input, authorization, evaluation
                    )
                    result_state = "ready"
                    result_version = int(task.state_version) + 1
                    authorization.resulting_task_state = result_state
                    authorization.resulting_task_state_version = result_version
                    transition_result = ExecutionTaskTransitionService(
                        self.db, now=self._now
                    ).transition(
                        ExecutionTaskTransitionCommand(
                            execution_task_id=task.id,
                            execution_plan_id=plan.id,
                            expected_from_state="awaiting_recovery",
                            expected_state_version=expected_version,
                            to_state="ready",
                            reason_code="recovery_retry_authorized",
                            reason_detail=f"recovery_authorization_id={authorization.id};replacement_attempt_id={replacement.id}",
                            actor_type="recovery",
                            actor_id=authorization.decision_actor_id,
                            idempotency_key=(
                                f"recovery-lifecycle-{task.id}-"
                                f"{recovery_input.recovery_generation}-{command_hash[:24]}"
                            ),
                        )
                    )
                    transition = self.db.get(
                        ExecutionTaskTransition, transition_result.event_id
                    )
                elif evaluation.status in {"exhausted", "non_retryable"}:
                    result_state = "failed"
                    result_version = int(task.state_version) + 1
                    authorization.resulting_task_state = result_state
                    authorization.resulting_task_state_version = result_version
                    reason = (
                        "recovery_exhausted"
                        if evaluation.status == "exhausted"
                        else "recovery_non_retryable"
                    )
                    transition_result = ExecutionTaskTransitionService(
                        self.db, now=self._now
                    ).transition(
                        ExecutionTaskTransitionCommand(
                            execution_task_id=task.id,
                            execution_plan_id=plan.id,
                            expected_from_state="awaiting_recovery",
                            expected_state_version=expected_version,
                            to_state="failed",
                            reason_code=reason,
                            reason_detail=f"recovery_authorization_id={authorization.id}",
                            actor_type="recovery",
                            actor_id=authorization.decision_actor_id,
                            idempotency_key=(
                                f"recovery-lifecycle-{task.id}-"
                                f"{recovery_input.recovery_generation}-{command_hash[:24]}"
                            ),
                        )
                    )
                    transition = self.db.get(
                        ExecutionTaskTransition, transition_result.event_id
                    )
                authorization.canonical_authorization_payload = {
                    **authorization_payload,
                    "resulting_task_state": result_state,
                    "resulting_task_state_version": result_version,
                }
                authorization.canonical_authorization_hash = canonical_json_hash(
                    authorization.canonical_authorization_payload
                )
                authorization.lifecycle_transition_id = (
                    transition.id if transition else None
                )
                authorization.lifecycle_transition_sequence = (
                    transition.sequence if transition else None
                )
                authorization.replacement_attempt_id = (
                    replacement.id if replacement else None
                )
                self.db.flush()
        except ExecutionTaskTransitionError as exc:
            raise ExecutionTaskRecoveryError(
                "recovery_decision_conflict", "recovery lifecycle finalization failed"
            ) from exc
        except IntegrityError as exc:
            raise ExecutionTaskRecoveryError(
                "recovery_decision_conflict",
                "recovery authorization uniqueness was fenced",
            ) from exc
        return RecoveryAuthorizationResult(authorization, replacement, transition)

    def _assert_attempt_generation(
        self, task: ExecutionTask, recovery_input: ExecutionTaskRecoveryInput
    ) -> None:
        attempts = (
            self.db.query(ExecutionTaskAttempt)
            .filter(ExecutionTaskAttempt.execution_task_id == task.id)
            .order_by(ExecutionTaskAttempt.attempt_number.asc())
            .all()
        )
        numbers = [int(attempt.attempt_number) for attempt in attempts]
        if numbers != list(range(1, len(numbers) + 1)):
            raise ExecutionTaskRecoveryError(
                "attempt_generation_conflict", "attempt generations are not contiguous"
            )
        if not numbers or numbers[-1] != int(recovery_input.attempt_generation):
            raise ExecutionTaskRecoveryError(
                "attempt_generation_conflict",
                "recovery input is not for the current attempt generation",
            )
        unresolved_dispatch = (
            self.db.query(ExecutionTaskDispatchIntent)
            .filter(
                ExecutionTaskDispatchIntent.execution_task_id == task.id,
                ExecutionTaskDispatchIntent.dispatch_status.in_(
                    {"pending_submission", "submitting"}
                ),
            )
            .count()
        )
        active_leases = (
            self.db.query(ExecutionTaskRuntimeLease)
            .filter(
                ExecutionTaskRuntimeLease.execution_task_id == task.id,
                ExecutionTaskRuntimeLease.lease_status == "active",
            )
            .count()
        )
        active_starts = (
            self.db.query(ExecutionTaskRuntimeStart)
            .join(
                ExecutionTaskRuntimeLease,
                ExecutionTaskRuntimeLease.id
                == ExecutionTaskRuntimeStart.runtime_lease_id,
            )
            .filter(
                ExecutionTaskRuntimeStart.execution_task_id == task.id,
                ExecutionTaskRuntimeLease.lease_status == "active",
            )
            .count()
        )
        if unresolved_dispatch or active_leases or active_starts:
            raise ExecutionTaskRecoveryError(
                "recovery_integrity_failure",
                "unresolved dispatch or runtime ownership remains",
            )

    def _create_replacement_attempt(
        self,
        task: ExecutionTask,
        recovery_input: ExecutionTaskRecoveryInput,
        authorization: ExecutionTaskRecoveryAuthorization,
        evaluation: RecoveryPolicyEvaluation,
    ) -> ExecutionTaskAttempt:
        next_generation = int(evaluation.next_attempt_generation or 0)
        if next_generation <= 0:
            raise ExecutionTaskRecoveryError(
                "attempt_generation_conflict",
                "authorized recovery has no next generation",
            )
        existing = (
            self.db.query(ExecutionTaskAttempt)
            .filter(
                ExecutionTaskAttempt.execution_task_id == task.id,
                ExecutionTaskAttempt.attempt_number == next_generation,
            )
            .one_or_none()
        )
        if existing is not None:
            raise ExecutionTaskRecoveryError(
                "replacement_attempt_already_exists",
                "replacement attempt generation already exists",
            )
        now = _utc(self._now())
        parameter_hash = (
            canonical_json_hash(evaluation.strategy_parameters)
            if evaluation.strategy_parameters is not None
            else None
        )
        attempt = ExecutionTaskAttempt(
            execution_plan_id=task.execution_plan_id,
            execution_task_id=task.id,
            dispatch_intent_id=None,
            attempt_number=next_generation,
            attempt_identity=f"execution-recovery-attempt-{task.id}-{next_generation}-{authorization.canonical_authorization_hash[:24]}",
            broker_task_id=None,
            predecessor_attempt_id=recovery_input.failed_attempt_id,
            recovery_authorization_id=authorization.id,
            recovery_generation=recovery_input.recovery_generation,
            replacement_reason="recovery_retry_authorized",
            strategy_id=evaluation.strategy_id,
            strategy_version=evaluation.strategy_version,
            strategy_parameter_hash=parameter_hash,
            attempt_status="created",
            created_at=now,
            updated_at=now,
        )
        self.db.add(attempt)
        self.db.flush()
        return attempt

    # -- integrity and inspection --------------------------------------

    def verify_recovery_input_integrity(
        self, recovery_input_id: int, *, raise_on_error: bool = False
    ) -> RecoveryIntegrityResult:
        row = self.db.get(ExecutionTaskRecoveryInput, int(recovery_input_id))
        if row is None:
            result = RecoveryIntegrityResult(
                None, None, False, ("recovery_input_missing",)
            )
            if raise_on_error:
                raise ExecutionTaskRecoveryError(
                    "recovery_integrity_failure", result.issues[0]
                )
            return result
        issues: list[str] = []
        task = self.db.get(ExecutionTask, row.execution_task_id)
        attempt = self.db.get(ExecutionTaskAttempt, row.failed_attempt_id)
        transition = self.db.get(ExecutionTaskTransition, row.lifecycle_transition_id)
        if task is None or attempt is None or transition is None:
            issues.append("recovery_input_authority_missing")
        if attempt is not None and (
            attempt.execution_task_id != row.execution_task_id
            or int(attempt.attempt_number) != int(row.attempt_generation)
        ):
            issues.append("recovery_input_attempt_mismatch")
        if transition is not None and (
            transition.execution_task_id != row.execution_task_id
            or transition.to_state != "awaiting_recovery"
            or transition.sequence != row.lifecycle_transition_sequence
        ):
            issues.append("recovery_input_lifecycle_mismatch")
        try:
            if (
                canonical_json_hash(row.canonical_input_payload)
                != row.canonical_input_hash
            ):
                issues.append("recovery_input_hash_mismatch")
        except (TypeError, ValueError):
            issues.append("recovery_input_payload_malformed")
        if not _valid_hash(row.canonical_input_hash):
            issues.append("recovery_input_hash_malformed")
        if row.recovery_source not in RECOVERY_SOURCES:
            issues.append("recovery_source_unsupported")
        if row.recovery_source == "runtime_attempt_failed":
            outcome = self.db.get(ExecutionTaskAttemptOutcome, row.runtime_outcome_id)
            if outcome is None or outcome.outcome_status != "attempt_failed":
                issues.append("runtime_failure_integrity_invalid")
        elif row.recovery_source == "validation_rejected":
            decision = self.db.get(
                ExecutionTaskAcceptanceDecision, row.acceptance_decision_id
            )
            run = self.db.get(ExecutionTaskValidationRun, row.validation_run_id)
            if (
                decision is None
                or run is None
                or decision.decision_status != "rejected"
                or run.final_validation_classification != "rejected"
            ):
                issues.append("validation_rejection_integrity_invalid")
        result = RecoveryIntegrityResult(
            row.execution_plan_id,
            row.execution_task_id,
            not issues,
            tuple(sorted(set(issues))),
        )
        if raise_on_error and not result.verified:
            raise ExecutionTaskRecoveryError(
                "recovery_integrity_failure", result.issues[0]
            )
        return result

    def verify_recovery_authorization_integrity(
        self, authorization_id: int, *, raise_on_error: bool = False
    ) -> RecoveryIntegrityResult:
        row = self.db.get(ExecutionTaskRecoveryAuthorization, int(authorization_id))
        if row is None:
            result = RecoveryIntegrityResult(
                None, None, False, ("recovery_authorization_missing",)
            )
            if raise_on_error:
                raise ExecutionTaskRecoveryError(
                    "recovery_integrity_failure", result.issues[0]
                )
            return result
        issues: list[str] = []
        task = self.db.get(ExecutionTask, row.execution_task_id)
        recovery_input = self.db.get(ExecutionTaskRecoveryInput, row.recovery_input_id)
        if task is None or recovery_input is None:
            issues.append("recovery_authorization_authority_missing")
        else:
            issues.extend(
                self.verify_recovery_input_integrity(recovery_input.id).issues
            )
            if recovery_input.canonical_input_hash not in {
                (
                    row.canonical_authorization_payload.get("recovery_input_hash")
                    if isinstance(row.canonical_authorization_payload, dict)
                    else None
                )
            }:
                issues.append("recovery_authorization_input_hash_mismatch")
        for payload, expected, code in (
            (
                row.canonical_authorization_command_payload,
                row.canonical_authorization_command_hash,
                "recovery_authorization_command_hash_mismatch",
            ),
            (
                row.canonical_authorization_payload,
                row.canonical_authorization_hash,
                "recovery_authorization_hash_mismatch",
            ),
        ):
            try:
                if canonical_json_hash(payload) != expected:
                    issues.append(code)
            except (TypeError, ValueError):
                issues.append("recovery_authorization_payload_malformed")
        if row.authorization_status not in AUTHORIZATION_STATUSES:
            issues.append("recovery_authorization_status_invalid")
        if row.authorization_status == "authorized":
            replacement = self.db.get(ExecutionTaskAttempt, row.replacement_attempt_id)
            if replacement is None:
                issues.append("replacement_attempt_missing")
            else:
                if (
                    replacement.predecessor_attempt_id != row.failed_attempt_id
                    or replacement.recovery_authorization_id != row.id
                    or replacement.attempt_number != row.next_attempt_generation
                    or replacement.runtime_outcome is not None
                ):
                    issues.append("replacement_attempt_integrity_failure")
        elif row.replacement_attempt_id is not None:
            issues.append("non_authorized_replacement_present")
        if row.lifecycle_transition_id is not None:
            transition = self.db.get(
                ExecutionTaskTransition, row.lifecycle_transition_id
            )
            if transition is None:
                issues.append("recovery_lifecycle_transition_missing")
            else:
                expected_reason = {
                    "authorized": "recovery_retry_authorized",
                    "exhausted": "recovery_exhausted",
                    "non_retryable": "recovery_non_retryable",
                }.get(row.authorization_status)
                if (
                    transition.execution_task_id != row.execution_task_id
                    or transition.to_state != row.resulting_task_state
                    or expected_reason is None
                    or transition.reason_code != expected_reason
                    or transition.sequence != row.lifecycle_transition_sequence
                ):
                    issues.append("recovery_lifecycle_transition_mismatch")
        elif row.authorization_status in {"authorized", "exhausted", "non_retryable"}:
            issues.append("recovery_lifecycle_transition_missing")
        result = RecoveryIntegrityResult(
            row.execution_plan_id,
            row.execution_task_id,
            not issues,
            tuple(sorted(set(issues))),
        )
        if raise_on_error and not result.verified:
            raise ExecutionTaskRecoveryError(
                "recovery_integrity_failure", result.issues[0]
            )
        return result

    def verify_replacement_attempt_integrity(
        self, attempt_id: int
    ) -> RecoveryIntegrityResult:
        attempt = self.db.get(ExecutionTaskAttempt, int(attempt_id))
        if attempt is None:
            return RecoveryIntegrityResult(
                None, None, False, ("replacement_attempt_missing",)
            )
        issues: list[str] = []
        if attempt.recovery_authorization_id is None:
            issues.append("replacement_attempt_without_authorization")
        if attempt.predecessor_attempt_id is None:
            issues.append("replacement_attempt_without_predecessor")
        if attempt.dispatch_intent_id is not None:
            intent = self.db.get(
                ExecutionTaskDispatchIntent, attempt.dispatch_intent_id
            )
            if intent is None or intent.runtime_attempt_id != attempt.id:
                issues.append("replacement_attempt_dispatch_mismatch")
        if attempt.runtime_start is not None or attempt.runtime_outcome is not None:
            issues.append("replacement_attempt_copied_runtime_authority")
        result = RecoveryIntegrityResult(
            attempt.execution_plan_id,
            attempt.execution_task_id,
            not issues,
            tuple(sorted(set(issues))),
        )
        return result

    def verify_execution_task_recovery_integrity(
        self, execution_task_id: int
    ) -> RecoveryIntegrityResult:
        task = self.db.get(ExecutionTask, int(execution_task_id))
        if task is None:
            return RecoveryIntegrityResult(
                None, int(execution_task_id), False, ("execution_task_not_found",)
            )
        issues: list[str] = []
        attempts = (
            self.db.query(ExecutionTaskAttempt)
            .filter(ExecutionTaskAttempt.execution_task_id == task.id)
            .order_by(ExecutionTaskAttempt.attempt_number.asc())
            .all()
        )
        numbers = [int(item.attempt_number) for item in attempts]
        if numbers != list(range(1, len(numbers) + 1)):
            issues.append("attempt_generation_gap")
        inputs = (
            self.db.query(ExecutionTaskRecoveryInput)
            .filter(ExecutionTaskRecoveryInput.execution_task_id == task.id)
            .all()
        )
        authorizations = (
            self.db.query(ExecutionTaskRecoveryAuthorization)
            .filter(ExecutionTaskRecoveryAuthorization.execution_task_id == task.id)
            .all()
        )
        for row in inputs:
            issues.extend(self.verify_recovery_input_integrity(row.id).issues)
        for row in authorizations:
            issues.extend(self.verify_recovery_authorization_integrity(row.id).issues)
        for attempt in attempts:
            if attempt.recovery_authorization_id is not None:
                issues.extend(
                    self.verify_replacement_attempt_integrity(attempt.id).issues
                )
        if task.status == "ready":
            current = attempts[-1] if attempts else None
            if (
                current is not None
                and current.recovery_authorization_id is not None
                and not any(
                    row.authorization_status == "authorized" for row in authorizations
                )
            ):
                issues.append("ready_retry_without_authorization")
        if task.status == "failed" and not any(
            row.authorization_status in {"exhausted", "non_retryable"}
            for row in authorizations
        ):
            issues.append("failed_task_without_terminal_recovery_decision")
        if any(
            int(row.retry_budget_after) < 0
            or int(row.retry_budget_before) < int(row.retry_budget_after)
            for row in authorizations
        ):
            issues.append("retry_budget_overrun")
        result = RecoveryIntegrityResult(
            task.execution_plan_id,
            task.id,
            not issues,
            tuple(sorted(set(issues))),
        )
        return result

    def verify_execution_plan_recovery_integrity(
        self, execution_plan_id: int
    ) -> RecoveryIntegrityResult:
        plan = self.db.get(ExecutionPlan, int(execution_plan_id))
        if plan is None:
            return RecoveryIntegrityResult(
                int(execution_plan_id), None, False, ("execution_plan_not_found",)
            )
        issues: list[str] = []
        for task in (
            self.db.query(ExecutionTask)
            .filter(ExecutionTask.execution_plan_id == plan.id)
            .order_by(ExecutionTask.plan_task_id.asc())
            .all()
        ):
            issues.extend(
                f"task:{task.id}:{issue}"
                for issue in self.verify_execution_task_recovery_integrity(
                    task.id
                ).issues
            )
        return RecoveryIntegrityResult(
            plan.id, None, not issues, tuple(sorted(set(issues)))
        )

    def inspect_execution_task_recovery(
        self, execution_task_id: int
    ) -> RecoveryInspectionProjection:
        task = self.db.get(ExecutionTask, int(execution_task_id))
        if task is None:
            raise ExecutionTaskRecoveryError(
                "execution_task_not_found", "Execution Task was not found"
            )
        recovery_input = (
            self.db.query(ExecutionTaskRecoveryInput)
            .filter(ExecutionTaskRecoveryInput.execution_task_id == task.id)
            .order_by(ExecutionTaskRecoveryInput.id.desc())
            .first()
        )
        if recovery_input is None:
            state = (
                "recovery_not_started"
                if task.status != "awaiting_recovery"
                else "recovery_input_ready"
            )
            return RecoveryInspectionProjection(
                task.execution_plan_id, task.id, state, None, None, None
            )
        authorization = (
            self.db.query(ExecutionTaskRecoveryAuthorization)
            .filter(
                ExecutionTaskRecoveryAuthorization.recovery_input_id
                == recovery_input.id
            )
            .one_or_none()
        )
        if authorization is None:
            return RecoveryInspectionProjection(
                task.execution_plan_id,
                task.id,
                "recovery_input_ready",
                recovery_input.id,
                None,
                None,
            )
        integrity = self.verify_recovery_authorization_integrity(authorization.id)
        if not integrity.verified:
            state = "recovery_lifecycle_mismatch"
        elif authorization.authorization_status == "authorized":
            state = (
                "replacement_attempt_dispatch_pending"
                if authorization.replacement_attempt_id is not None
                and authorization.replacement_attempt.dispatch_intent_id is None
                else "replacement_attempt_created"
            )
        else:
            state = {
                "operator_required": "recovery_operator_required",
                "exhausted": "recovery_exhausted",
                "non_retryable": "recovery_non_retryable",
                "blocked": "recovery_blocked",
                "error": "recovery_error",
            }.get(authorization.authorization_status, "recovery_error")
        reasons = tuple(item for item in (authorization.decision_reason,) if item)
        return RecoveryInspectionProjection(
            task.execution_plan_id,
            task.id,
            state,
            recovery_input.id,
            authorization.id,
            authorization.replacement_attempt_id,
            reasons,
        )


def verify_recovery_input_integrity(
    db: Session, recovery_input_id: int
) -> RecoveryIntegrityResult:
    return ExecutionTaskRecoveryService(db).verify_recovery_input_integrity(
        recovery_input_id
    )


def verify_recovery_authorization_integrity(
    db: Session, authorization_id: int
) -> RecoveryIntegrityResult:
    return ExecutionTaskRecoveryService(db).verify_recovery_authorization_integrity(
        authorization_id
    )


def verify_replacement_attempt_integrity(
    db: Session, attempt_id: int
) -> RecoveryIntegrityResult:
    return ExecutionTaskRecoveryService(db).verify_replacement_attempt_integrity(
        attempt_id
    )


def verify_execution_task_recovery_integrity(
    db: Session, execution_task_id: int
) -> RecoveryIntegrityResult:
    return ExecutionTaskRecoveryService(db).verify_execution_task_recovery_integrity(
        execution_task_id
    )


def verify_execution_plan_recovery_integrity(
    db: Session, execution_plan_id: int
) -> RecoveryIntegrityResult:
    return ExecutionTaskRecoveryService(db).verify_execution_plan_recovery_integrity(
        execution_plan_id
    )
