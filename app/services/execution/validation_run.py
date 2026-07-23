"""Phase 29C-7C validation-run and acceptance authority.

This module is the boundary between the immutable C7A contract/C7B
primitive rows and the Execution Task lifecycle.  It never reads candidate
bytes, opens references, invokes providers, runs commands, mutates a
workspace, creates attempts, or performs recovery.  Evidence and predicate
execution are delegated to the C7B resolver/registry services only.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Callable

from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import Session

from app.models import (
    ExecutionPlan,
    ExecutionTask,
    ExecutionTaskAcceptanceDecision,
    ExecutionTaskAttempt,
    ExecutionTaskAttemptOutcome,
    ExecutionTaskCandidateContent,
    ExecutionTaskResolvedValidationEvidence,
    ExecutionTaskValidationPredicateResult,
    ExecutionTaskValidationRun,
    ExecutionTaskValidationSpecification,
    ExecutionTaskTransition,
)
from app.services.execution.candidate_evidence import (
    CandidateEvidenceError,
    CandidateEvidenceResolverService,
    DeterministicValidatorRegistry,
    DeterministicValidatorService,
    EvaluateCandidatePredicateCommand,
    ResolveCandidateEvidenceCommand,
    ValidationPrimitiveService,
    build_default_validator_registry,
)
from app.services.execution.execution_task_runtime_execution_service import (
    ExecutionTaskRuntimeExecutionService,
)
from app.services.execution.execution_task_transition_service import (
    ExecutionTaskTransitionCommand,
    ExecutionTaskTransitionError,
    ExecutionTaskTransitionService,
)
from app.services.execution.validation_contract import ValidationContractService
from app.services.planning.operator_review import canonical_json_hash
from app.services.planning.validation_contract import (
    StructuredValidationContract,
    ValidationContractError,
    ValidationPredicate,
)


RESOLVER_CONTRACT_VERSION = "candidate-evidence-resolver/1"
VALIDATION_RUN_SCHEMA_VERSION = "execution-task-validation-run/1"
ACCEPTANCE_DECISION_SCHEMA_VERSION = "execution-task-acceptance-decision/1"
DEFAULT_ACTOR_TYPE = "system"
DEFAULT_ACTOR_ID = "validation-service"

RUN_FINAL_STATUSES = frozenset(
    {"accepted", "rejected", "blocked", "validation_error", "review_required"}
)
DECISION_STATUSES = frozenset(RUN_FINAL_STATUSES)
BLOCKED_RESULT_CODES = frozenset(
    {
        "missing_evidence",
        "unsupported",
        "invalid_evidence",
        "validation_required_evidence_missing",
        "validation_required_evidence_unsupported",
        "validation_required_predicate_unsupported",
        "validation_environment_mismatch",
        "validation_contract_unavailable",
        "immutable_candidate_bytes_unavailable",
        "review_authority_missing",
    }
)


class ValidationRunError(RuntimeError):
    """Bounded validation-run or acceptance error."""

    def __init__(self, code: str, message: str):
        self.code = code
        self.message = message
        super().__init__(f"{code}: {message}")


@dataclass(frozen=True)
class StartExecutionTaskValidationCommand:
    execution_plan_id: int
    execution_task_id: int
    execution_task_attempt_id: int
    candidate_outcome_id: int
    validation_specification_id: int
    validation_specification_hash: str
    expected_task_state: str
    expected_task_state_version: int
    validator_set_id: str
    validator_set_version: str
    environment_configuration_hash: str
    validation_idempotency_key: str
    creation_actor_type: str = DEFAULT_ACTOR_TYPE
    creation_actor_id: str = DEFAULT_ACTOR_ID
    validation_run_generation: int | None = None


@dataclass(frozen=True)
class ValidationRunResult:
    run: ExecutionTaskValidationRun
    replayed: bool = False


@dataclass(frozen=True)
class FinalizeExecutionTaskValidationCommand:
    validation_run_id: int
    expected_task_state: str
    expected_task_state_version: int
    decision_idempotency_key: str
    expected_classification: str | None = None
    decision_actor_type: str = DEFAULT_ACTOR_TYPE
    decision_actor_id: str = DEFAULT_ACTOR_ID


@dataclass(frozen=True)
class AcceptanceDecisionResult:
    decision: ExecutionTaskAcceptanceDecision
    replayed: bool = False


@dataclass(frozen=True)
class PassPolicyEvaluation:
    status: str
    relevant_predicate_ids: tuple[str, ...]
    blocker_codes: tuple[str, ...] = ()
    failed_predicate_ids: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValidationIntegrityResult:
    execution_plan_id: int | None
    execution_task_id: int | None
    verified: bool
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class EvidenceStrengthProjection:
    codes: tuple[str, ...]

    def to_dict(self) -> dict[str, Any]:
        return {"codes": list(self.codes)}


@dataclass(frozen=True)
class ValidationInspectionProjection:
    execution_plan_id: int
    execution_task_id: int
    validation_state: str
    run_id: int | None
    run_status: str | None
    decision_status: str | None
    blocker_reasons: tuple[str, ...] = ()
    evidence_strength: EvidenceStrengthProjection | None = None

    def to_dict(self) -> dict[str, Any]:
        return {
            "execution_plan_id": self.execution_plan_id,
            "execution_task_id": self.execution_task_id,
            "validation_state": self.validation_state,
            "run_id": self.run_id,
            "run_status": self.run_status,
            "decision_status": self.decision_status,
            "blocker_reasons": list(self.blocker_reasons),
            "evidence_strength": (
                self.evidence_strength.to_dict() if self.evidence_strength else None
            ),
        }


def _utc(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc)


def _hash(value: Any) -> str:
    if not isinstance(value, str) or len(value) != 64:
        raise ValidationRunError("validation_command_invalid", "hash is invalid")
    try:
        int(value, 16)
    except ValueError as exc:
        raise ValidationRunError(
            "validation_command_invalid", "hash is invalid"
        ) from exc
    return value.lower()


def _text(value: Any, field: str, limit: int = 255) -> str:
    if not isinstance(value, str) or not value.strip() or len(value) > limit:
        raise ValidationRunError("validation_command_invalid", f"{field} is invalid")
    return value.strip()


def _now() -> datetime:
    return datetime.now(timezone.utc)


def _contract(
    specification: ExecutionTaskValidationSpecification,
) -> StructuredValidationContract:
    try:
        return StructuredValidationContract.from_mapping(
            specification.canonical_payload["structured_contract"]
        )
    except (KeyError, TypeError, ValidationContractError) as exc:
        raise ValidationRunError(
            "validation_run_integrity_failure",
            "structured validation contract is invalid",
        ) from exc


def _start_payload(
    command: StartExecutionTaskValidationCommand,
    *,
    generation: int,
    deterministic_command_id: str,
) -> dict[str, Any]:
    return {
        "schema_version": VALIDATION_RUN_SCHEMA_VERSION,
        "execution_plan_id": int(command.execution_plan_id),
        "execution_task_id": int(command.execution_task_id),
        "execution_task_attempt_id": int(command.execution_task_attempt_id),
        "candidate_outcome_id": int(command.candidate_outcome_id),
        "validation_specification_id": int(command.validation_specification_id),
        "validation_specification_hash": _hash(command.validation_specification_hash),
        "expected_task_state": _text(
            command.expected_task_state, "expected task state", 20
        ),
        "expected_task_state_version": int(command.expected_task_state_version),
        "validation_run_generation": int(generation),
        "validator_set_id": _text(command.validator_set_id, "validator set id", 128),
        "validator_set_version": _text(
            command.validator_set_version, "validator set version", 64
        ),
        "environment_configuration_hash": _hash(command.environment_configuration_hash),
        "validation_idempotency_key": _text(
            command.validation_idempotency_key, "validation idempotency key", 128
        ),
        "deterministic_validation_command_id": deterministic_command_id,
        "creation_actor_type": _text(
            command.creation_actor_type, "creation actor type", 64
        ),
        "creation_actor_id": _text(command.creation_actor_id, "creation actor id", 255),
    }


def _decision_command_payload(
    command: FinalizeExecutionTaskValidationCommand,
    run: ExecutionTaskValidationRun,
    classification: str,
) -> dict[str, Any]:
    return {
        "schema_version": ACCEPTANCE_DECISION_SCHEMA_VERSION,
        "validation_run_id": int(run.id),
        "execution_plan_id": int(run.execution_plan_id),
        "execution_task_id": int(run.execution_task_id),
        "candidate_outcome_id": int(run.candidate_outcome_id),
        "validation_specification_id": int(run.validation_specification_id),
        "expected_task_state": _text(
            command.expected_task_state, "expected task state", 20
        ),
        "expected_task_state_version": int(command.expected_task_state_version),
        "expected_classification": classification,
        "decision_idempotency_key": _text(
            command.decision_idempotency_key, "decision idempotency key", 128
        ),
        "decision_actor_type": _text(
            command.decision_actor_type, "decision actor type", 64
        ),
        "decision_actor_id": _text(command.decision_actor_id, "decision actor id", 255),
    }


def _status_for_result(result_status: str) -> str:
    if result_status == "failed":
        return "failed"
    if result_status == "validator_error":
        return "validation_error"
    if result_status in {"missing_evidence", "unsupported", "invalid_evidence"}:
        return "blocked"
    return "passed"


def _policy_relevant(
    contract: StructuredValidationContract, *, include_optional: bool
) -> tuple[ValidationPredicate, ...]:
    return tuple(
        predicate
        for predicate in contract.predicates
        if include_optional or predicate.required
    )


def evaluate_pass_policy(
    contract: StructuredValidationContract,
    results: Mapping[tuple[str, int], ExecutionTaskValidationPredicateResult],
) -> PassPolicyEvaluation:
    """Pure, versioned, fail-closed evaluation of the frozen pass policy."""

    policy = contract.pass_policy
    if policy is None:
        return PassPolicyEvaluation(
            "blocked", (), ("validation_pass_policy_unsupported",)
        )
    relevant = _policy_relevant(
        contract, include_optional=policy.policy_id == "all_predicates"
    )
    if not relevant:
        return PassPolicyEvaluation("blocked", (), ("validation_pass_policy_blocked",))

    blocker_codes: list[str] = []
    failed: list[str] = []
    passed: list[str] = []
    for predicate in relevant:
        row = results.get((predicate.predicate_id, predicate.predicate_version))
        if row is None:
            blocker_codes.append("validation_primitives_incomplete")
            continue
        classification = _status_for_result(row.result_status)
        if classification == "blocked":
            blocker_codes.append(
                {
                    "missing_evidence": "required_evidence_missing",
                    "unsupported": "required_predicate_unsupported",
                    "invalid_evidence": "primitive_integrity_failure",
                }.get(row.result_status, "validation_pass_policy_blocked")
            )
        elif classification == "validation_error":
            blocker_codes.append("validation_validator_error")
        elif classification == "failed":
            failed.append(predicate.predicate_id)
        elif row.passed:
            passed.append(predicate.predicate_id)

    if blocker_codes:
        # Validator errors are infrastructure errors; unavailable evidence and
        # unsupported predicates are lifecycle-neutral blockers.
        if any(code == "validation_validator_error" for code in blocker_codes):
            status = "validation_error"
        else:
            status = "blocked"
        return PassPolicyEvaluation(
            status,
            tuple(item.predicate_id for item in relevant),
            tuple(sorted(set(blocker_codes))),
            tuple(sorted(set(failed))),
        )

    if policy.policy_id == "any_required_group":
        status = "passed" if passed else "failed"
    else:
        status = "passed" if not failed else "failed"
    return PassPolicyEvaluation(
        status,
        tuple(item.predicate_id for item in relevant),
        (),
        tuple(sorted(set(failed))),
    )


def _evidence_strength(
    contract: StructuredValidationContract,
) -> EvidenceStrengthProjection:
    """Describe capability, without presenting claims as byte verification."""

    predicate_ids = {item.predicate_id for item in contract.predicates}
    codes = {
        "byte_level_validation_unavailable",
        "content_semantics_not_verified",
        "test_execution_not_verified",
        "artifact_bytes_not_verified",
    }
    if "test_suite_result_passed" in predicate_ids:
        codes.add("test_execution_not_verified")
    if "artifact_exists" in predicate_ids or "artifact_hash_matches" in predicate_ids:
        codes.add("artifact_bytes_not_verified")
    if predicate_ids.intersection(
        {
            "content_exists",
            "content_hash_matches",
            "content_size_within_limit",
            "media_type_matches",
        }
    ):
        codes.discard("byte_level_validation_unavailable")
        codes.add("independently_recomputed_bytes")
    return EvidenceStrengthProjection(tuple(sorted(codes)))


def _result_payload(
    run: ExecutionTaskValidationRun,
    contract: StructuredValidationContract,
    evaluation: PassPolicyEvaluation,
    review: Mapping[str, Any],
    classification: str,
    evidence_hash: str,
    predicate_hash: str,
) -> dict[str, Any]:
    return {
        "schema_version": VALIDATION_RUN_SCHEMA_VERSION,
        "run_id": run.id,
        "execution_plan_id": run.execution_plan_id,
        "execution_task_id": run.execution_task_id,
        "execution_task_attempt_id": run.execution_task_attempt_id,
        "candidate_outcome_id": run.candidate_outcome_id,
        "validation_specification_id": run.validation_specification_id,
        "validation_specification_hash": run.validation_specification_hash,
        "validation_contract_set_hash": run.validation_contract_set_hash,
        "aggregate_evidence_hash": evidence_hash,
        "aggregate_predicate_result_hash": predicate_hash,
        "pass_policy_id": (
            contract.pass_policy.policy_id if contract.pass_policy else None
        ),
        "pass_policy_version": (
            contract.pass_policy.policy_version if contract.pass_policy else None
        ),
        "pass_policy_result": evaluation.status,
        "review_requirement": run.review_requirement,
        "review_result": review,
        "classification": classification,
        "evidence_strength": _evidence_strength(contract).to_dict(),
    }


class ValidationRunService:
    """Start, execute, finalize, inspect, and verify validation authority."""

    def __init__(
        self,
        db: Session,
        *,
        now: Callable[[], datetime] | None = None,
        resolver: CandidateEvidenceResolverService | None = None,
        validator: DeterministicValidatorService | None = None,
    ):
        self.db = db
        self._now = now or _now
        self._resolver = resolver
        self._validator = validator

    def _load_run(
        self, run_id: int, *, lock: bool = False
    ) -> ExecutionTaskValidationRun:
        query = self.db.query(ExecutionTaskValidationRun).filter(
            ExecutionTaskValidationRun.id == int(run_id)
        )
        if lock:
            query = query.with_for_update()
        run = query.one_or_none()
        if run is None:
            raise ValidationRunError(
                "validation_run_not_found", "validation run was not found"
            )
        return run

    def start_validation_run(
        self, command: StartExecutionTaskValidationCommand
    ) -> ValidationRunResult:
        """Create the run authority only; no resolver or validator is called."""

        plan = self.db.get(ExecutionPlan, int(command.execution_plan_id))
        task = self.db.get(ExecutionTask, int(command.execution_task_id))
        specification = self.db.get(
            ExecutionTaskValidationSpecification,
            int(command.validation_specification_id),
        )
        outcome = self.db.get(
            ExecutionTaskAttemptOutcome, int(command.candidate_outcome_id)
        )
        attempt = self.db.get(
            ExecutionTaskAttempt, int(command.execution_task_attempt_id)
        )
        if any(item is None for item in (plan, task, specification, outcome, attempt)):
            raise ValidationRunError(
                "validation_run_integrity_failure", "validation authority is incomplete"
            )
        assert plan is not None and task is not None and specification is not None
        assert outcome is not None and attempt is not None
        generation = int(
            command.validation_run_generation or specification.release_generation
        )
        if generation != specification.release_generation:
            raise ValidationRunError(
                "validation_run_integrity_failure",
                "validation generation is not frozen",
            )
        deterministic_id = (
            f"validation-run-command-{outcome.id}-{specification.id}-{generation}"
        )
        payload = _start_payload(
            command, generation=generation, deterministic_command_id=deterministic_id
        )
        command_hash = canonical_json_hash(payload)

        existing = (
            self.db.query(ExecutionTaskValidationRun)
            .filter(
                ExecutionTaskValidationRun.validation_idempotency_key
                == payload["validation_idempotency_key"]
            )
            .one_or_none()
        )
        if existing is not None:
            if existing.canonical_validation_command_hash != command_hash:
                raise ValidationRunError(
                    "validation_run_idempotency_conflict",
                    "validation idempotency key is bound to another command",
                )
            self.verify_validation_run_integrity(existing.id)
            return ValidationRunResult(existing, replayed=True)

        duplicates = (
            self.db.query(ExecutionTaskValidationRun)
            .filter(
                ExecutionTaskValidationRun.candidate_outcome_id == outcome.id,
                ExecutionTaskValidationRun.validation_specification_id
                == specification.id,
            )
            .all()
        )
        if len(duplicates) > 1:
            raise ValidationRunError(
                "validation_run_integrity_failure",
                "candidate outcome has duplicate validation runs",
            )
        if duplicates:
            raise ValidationRunError(
                "validation_run_already_exists",
                "candidate outcome already has a validation run for this specification",
            )
        contract = self._authorize_start(
            command, plan, task, specification, outcome, attempt
        )
        command_duplicate = (
            self.db.query(ExecutionTaskValidationRun)
            .filter(
                ExecutionTaskValidationRun.deterministic_validation_command_id
                == deterministic_id
            )
            .one_or_none()
        )
        if command_duplicate is not None:
            raise ValidationRunError(
                "validation_run_already_exists",
                "deterministic validation command already exists",
            )

        row = ExecutionTaskValidationRun(
            execution_plan_id=plan.id,
            execution_task_id=task.id,
            execution_task_attempt_id=attempt.id,
            candidate_outcome_id=outcome.id,
            validation_specification_id=specification.id,
            validation_specification_hash=specification.canonical_specification_hash,
            validation_contract_set_hash=plan.validation_contract_set_hash,
            task_state_at_start=task.status,
            task_state_version_at_start=task.state_version,
            validation_run_generation=generation,
            validation_idempotency_key=payload["validation_idempotency_key"],
            deterministic_validation_command_id=deterministic_id,
            canonical_validation_command_payload=payload,
            canonical_validation_command_hash=command_hash,
            validator_set_id=contract.environment.validator_set_id,
            validator_set_version=contract.environment.validator_set_version,
            environment_configuration_hash=contract.environment.configuration_hash,
            resolver_contract_version=contract.environment.resolver_version,
            started_at=self._now(),
            run_status="pending",
            required_evidence_count=sum(
                item.required for item in contract.evidence_descriptors
            ),
            required_predicate_count=sum(item.required for item in contract.predicates),
            creation_actor_type=payload["creation_actor_type"],
            creation_actor_id=payload["creation_actor_id"],
        )
        try:
            with self.db.begin_nested():
                self.db.add(row)
                self.db.flush()
        except IntegrityError as exc:
            replay = (
                self.db.query(ExecutionTaskValidationRun)
                .filter(
                    ExecutionTaskValidationRun.validation_idempotency_key
                    == payload["validation_idempotency_key"]
                )
                .one_or_none()
            )
            if (
                replay is not None
                and replay.canonical_validation_command_hash == command_hash
            ):
                return ValidationRunResult(replay, replayed=True)
            raise ValidationRunError(
                "validation_run_already_exists",
                "validation run conflicts with canonical authority",
            ) from exc
        return ValidationRunResult(row)

    def _authorize_start(
        self,
        command: StartExecutionTaskValidationCommand,
        plan: ExecutionPlan,
        task: ExecutionTask,
        specification: ExecutionTaskValidationSpecification,
        outcome: ExecutionTaskAttemptOutcome,
        attempt: ExecutionTaskAttempt,
    ) -> StructuredValidationContract:
        if plan.status != "active" or plan.superseded_by_execution_plan_id is not None:
            raise ValidationRunError(
                "execution_plan_inactive", "execution plan is not active"
            )
        if (
            task.execution_plan_id != plan.id
            or specification.execution_plan_id != plan.id
        ):
            raise ValidationRunError(
                "validation_run_integrity_failure", "task and plan linkage is invalid"
            )
        if specification.execution_task_id != task.id:
            raise ValidationRunError(
                "validation_run_integrity_failure",
                "validation specification task linkage is invalid",
            )
        if (
            command.expected_task_state != "awaiting_validation"
            or task.status != "awaiting_validation"
        ):
            raise ValidationRunError(
                "task_not_awaiting_validation", "task is not awaiting validation"
            )
        if task.state_version != int(command.expected_task_state_version):
            raise ValidationRunError(
                "task_state_version_stale", "task state version is stale"
            )
        if (
            outcome.execution_plan_id != plan.id
            or outcome.execution_task_id != task.id
            or outcome.execution_task_attempt_id != attempt.id
        ):
            raise ValidationRunError(
                "validation_run_integrity_failure",
                "candidate outcome linkage is invalid",
            )
        if (
            outcome.outcome_status != "candidate_completed"
            or attempt.attempt_status != "candidate_completed"
        ):
            raise ValidationRunError(
                "validation_required_evidence_missing",
                "candidate outcome is not completed",
            )
        if task.validation_contract_status != "structured_executable":
            if task.validation_contract_status == "validation_not_required":
                raise ValidationRunError(
                    "validation_not_required", "task does not require validation"
                )
            raise ValidationRunError(
                "validation_contract_unavailable",
                "task has no executable validation contract",
            )
        if task.validation_contract_id != specification.id:
            raise ValidationRunError(
                "validation_run_integrity_failure",
                "task specification reference is invalid",
            )
        if specification.canonical_specification_hash != _hash(
            command.validation_specification_hash
        ):
            raise ValidationRunError(
                "validation_run_integrity_failure",
                "validation specification hash does not match",
            )
        contract_integrity = ValidationContractService(
            self.db
        ).verify_validation_contract_integrity(specification.id)
        if not contract_integrity.verified:
            raise ValidationRunError(
                "validation_run_integrity_failure",
                "validation specification integrity failed",
            )
        plan_integrity = ValidationContractService(
            self.db
        ).verify_execution_plan_validation_contract_integrity(plan.id)
        if (
            not plan_integrity.verified
            or plan.validation_contract_set_hash != plan_integrity.specification_hash
        ):
            raise ValidationRunError(
                "validation_run_integrity_failure",
                "validation contract set integrity failed",
            )
        runtime_integrity = ExecutionTaskRuntimeExecutionService(
            self.db
        ).verify_attempt_outcome_integrity(outcome.id)
        if not runtime_integrity.verified:
            raise ValidationRunError(
                "validation_run_integrity_failure", "runtime evidence integrity failed"
            )
        try:
            ExecutionTaskTransitionService(self.db).verify_task_lifecycle_integrity(
                task.id
            )
        except ExecutionTaskTransitionError as exc:
            raise ValidationRunError(
                "validation_run_integrity_failure", "task lifecycle integrity failed"
            ) from exc
        contract = _contract(specification)
        environment = contract.environment
        if (
            command.validator_set_id != environment.validator_set_id
            or command.validator_set_version != environment.validator_set_version
        ):
            raise ValidationRunError(
                "validation_environment_mismatch",
                "validator set identity does not match frozen environment",
            )
        if command.environment_configuration_hash != environment.configuration_hash:
            raise ValidationRunError(
                "validation_environment_mismatch",
                "validator environment does not match frozen environment",
            )
        if environment.resolver_version != RESOLVER_CONTRACT_VERSION:
            raise ValidationRunError(
                "validation_environment_mismatch", "resolver contract is unavailable"
            )
        return contract

    def _mark_running(self, run: ExecutionTaskValidationRun) -> None:
        if run.run_status == "pending":
            run.run_status = "running"
            self.db.flush()
        elif run.run_status not in {"running", *RUN_FINAL_STATUSES}:
            raise ValidationRunError(
                "validation_run_integrity_failure", "validation run status is invalid"
            )

    def _registry(
        self, contract: StructuredValidationContract
    ) -> DeterministicValidatorRegistry:
        environment = contract.environment
        if (
            environment.validator_set_id == "deterministic_readonly"
            and environment.validator_set_version == "1"
        ):
            return build_default_validator_registry(
                configuration_hash=environment.configuration_hash
            )
        return DeterministicValidatorRegistry(
            validator_set_id=environment.validator_set_id,
            validator_set_version=environment.validator_set_version,
            configuration_hash=environment.configuration_hash,
        )

    def execute_validation_run(
        self,
        command: StartExecutionTaskValidationCommand,
        *,
        registry: DeterministicValidatorRegistry | None = None,
    ) -> ValidationRunResult:
        """Start/commit, then orchestrate C7B primitives and finalize once."""

        started = self.start_validation_run(command)
        self.db.commit()
        run_id = started.run.id
        run = self._load_run(run_id)
        if run.run_status in RUN_FINAL_STATUSES:
            return ValidationRunResult(run, replayed=started.replayed)
        try:
            self._mark_running(run)
            specification = self.db.get(
                ExecutionTaskValidationSpecification, run.validation_specification_id
            )
            if specification is None:
                raise ValidationRunError(
                    "validation_run_integrity_failure",
                    "validation specification disappeared",
                )
            contract = _contract(specification)
            active_registry = registry or self._registry(contract)
            resolver = self._resolver or CandidateEvidenceResolverService(self.db)
            validator = self._validator or DeterministicValidatorService(
                self.db, registry=active_registry
            )
            evidence_rows: dict[str, Any] = {}
            for descriptor in contract.evidence_descriptors:
                candidate_content = (
                    self.db.query(ExecutionTaskCandidateContent)
                    .filter(
                        ExecutionTaskCandidateContent.candidate_outcome_id
                        == run.candidate_outcome_id
                    )
                    .one_or_none()
                    if descriptor.source == "candidate_content"
                    else None
                )
                expected_reference = (
                    f"candidate-content://{candidate_content.id}"
                    if candidate_content is not None
                    else f"candidate-output://{run.candidate_outcome_id}"
                )
                resolution = resolver.resolve(
                    ResolveCandidateEvidenceCommand(
                        execution_plan_id=run.execution_plan_id,
                        execution_task_id=run.execution_task_id,
                        execution_task_attempt_id=run.execution_task_attempt_id,
                        candidate_outcome_id=run.candidate_outcome_id,
                        validation_specification_id=run.validation_specification_id,
                        validation_specification_hash=run.validation_specification_hash,
                        evidence_key=descriptor.evidence_key,
                        evidence_type=descriptor.evidence_type,
                        evidence_source=descriptor.source,
                        expected_reference=expected_reference,
                        expected_hash_algorithm=descriptor.expected_hash_algorithm,
                        resolver_version=descriptor.resolver_version,
                        environment_configuration_hash=run.environment_configuration_hash,
                        resolution_idempotency_key=f"validation-evidence-{run.id}-{descriptor.evidence_key}",
                        deterministic_resolution_command_id=f"validation-evidence-command-{run.id}-{descriptor.evidence_key}",
                    )
                )
                evidence_rows[descriptor.evidence_key] = resolution.evidence

            for predicate in contract.predicates:
                evidence = evidence_rows.get(predicate.evidence_key)
                if evidence is None:
                    continue
                registration = active_registry.registration(
                    predicate.predicate_id, predicate.predicate_version
                )
                validator_id = (
                    registration.validator_id
                    if registration
                    else predicate.predicate_id
                )
                validator_version = (
                    registration.validator_version if registration else 1
                )
                validator.validate(
                    EvaluateCandidatePredicateCommand(
                        execution_plan_id=run.execution_plan_id,
                        execution_task_id=run.execution_task_id,
                        execution_task_attempt_id=run.execution_task_attempt_id,
                        candidate_outcome_id=run.candidate_outcome_id,
                        validation_specification_id=run.validation_specification_id,
                        validation_specification_hash=run.validation_specification_hash,
                        predicate_id=predicate.predicate_id,
                        predicate_version=predicate.predicate_version,
                        predicate_order=predicate.order,
                        evidence_snapshot_id=evidence.id,
                        evidence_key=predicate.evidence_key,
                        validator_id=validator_id,
                        validator_version=validator_version,
                        validator_set_id=run.validator_set_id,
                        validator_set_version=run.validator_set_version,
                        environment_configuration_hash=run.environment_configuration_hash,
                        validator_idempotency_key=f"validation-predicate-{run.id}-{predicate.predicate_id}-{predicate.predicate_version}",
                        deterministic_validator_command_id=f"validation-predicate-command-{run.id}-{predicate.predicate_id}-{predicate.predicate_version}",
                    )
                )
            classification, aggregation = self._classify_run(run, contract)
            self._persist_run_result(run, contract, classification, aggregation)
            decision = self.finalize_validation_run(
                FinalizeExecutionTaskValidationCommand(
                    validation_run_id=run.id,
                    expected_task_state=run.task_state_at_start,
                    expected_task_state_version=run.task_state_version_at_start,
                    decision_idempotency_key=f"validation-decision-{run.id}",
                    expected_classification=classification,
                )
            )
            self.db.commit()
            return ValidationRunResult(
                run, replayed=decision.replayed and started.replayed
            )
        except ValidationRunError as exc:
            return self._record_unexpected_terminal(
                run_id,
                "blocked" if exc.code in BLOCKED_RESULT_CODES else "validation_error",
                exc.code,
            )
        except CandidateEvidenceError as exc:
            classification = (
                "blocked" if exc.code in BLOCKED_RESULT_CODES else "validation_error"
            )
            return self._record_unexpected_terminal(run_id, classification, exc.code)
        except Exception:
            return self._record_unexpected_terminal(
                run_id, "validation_error", "validation_validator_error"
            )

    def _classify_run(
        self, run: ExecutionTaskValidationRun, contract: StructuredValidationContract
    ) -> tuple[str, dict[str, Any]]:
        snapshots = {
            item.evidence_key: item
            for item in self.db.query(ExecutionTaskResolvedValidationEvidence)
            .filter(
                ExecutionTaskResolvedValidationEvidence.candidate_outcome_id
                == run.candidate_outcome_id,
                ExecutionTaskResolvedValidationEvidence.validation_specification_id
                == run.validation_specification_id,
            )
            .all()
        }
        results = {
            (item.predicate_id, item.predicate_version): item
            for item in self.db.query(ExecutionTaskValidationPredicateResult)
            .filter(
                ExecutionTaskValidationPredicateResult.candidate_outcome_id
                == run.candidate_outcome_id,
                ExecutionTaskValidationPredicateResult.validation_specification_id
                == run.validation_specification_id,
            )
            .all()
        }
        evaluation = evaluate_pass_policy(contract, results)
        required_evidence_blockers = []
        for descriptor in contract.evidence_descriptors:
            if not descriptor.required:
                continue
            snapshot = snapshots.get(descriptor.evidence_key)
            if snapshot is None or snapshot.resolution_status == "missing":
                required_evidence_blockers.append("required_evidence_missing")
            elif snapshot.resolution_status == "unsupported":
                required_evidence_blockers.append("required_evidence_unsupported")
            elif snapshot.resolution_status != "resolved":
                required_evidence_blockers.append("primitive_integrity_failure")
        if required_evidence_blockers and evaluation.status not in {
            "validation_error",
        }:
            evaluation = PassPolicyEvaluation(
                "blocked",
                evaluation.relevant_predicate_ids,
                tuple(sorted(set(required_evidence_blockers))),
                evaluation.failed_predicate_ids,
            )
        review_requirement = contract.review_requirement.requirement
        if evaluation.status == "passed":
            if review_requirement == "none":
                classification = "accepted"
                review = {"status": "satisfied", "requirement": "none"}
            elif review_requirement in {"operator_required", "policy_required"}:
                classification = "review_required"
                review = {
                    "status": "required",
                    "requirement": review_requirement,
                    "reason": "review_authority_missing",
                    "scope": "execution_task_candidate",
                }
            else:
                classification = "blocked"
                review = {"status": "unsupported", "requirement": review_requirement}
        else:
            classification = (
                "rejected" if evaluation.status == "failed" else evaluation.status
            )
            review = {"status": "not_evaluated", "requirement": review_requirement}
        return classification, {
            "snapshots": snapshots,
            "results": results,
            "evaluation": evaluation,
            "review_requirement": review_requirement,
            "review": review,
        }

    def _aggregate_hashes(
        self,
        run: ExecutionTaskValidationRun,
        contract: StructuredValidationContract,
        snapshots: Mapping[str, ExecutionTaskResolvedValidationEvidence],
        results: Mapping[tuple[str, int], ExecutionTaskValidationPredicateResult],
    ) -> tuple[str, str]:
        evidence_items = []
        for descriptor in contract.evidence_descriptors:
            row = snapshots.get(descriptor.evidence_key)
            evidence_items.append(
                {
                    "evidence_key": descriptor.evidence_key,
                    "required": descriptor.required,
                    "snapshot_id": row.id if row else None,
                    "resolution_status": (
                        row.resolution_status if row else "missing_snapshot"
                    ),
                    "canonical_evidence_payload_hash": (
                        row.canonical_evidence_payload_hash if row else None
                    ),
                    "source_authority_id": row.source_authority_id if row else None,
                    "actual_hash": row.actual_hash if row else None,
                    "resolver_id": row.resolver_id if row else None,
                    "resolver_version": row.resolver_version if row else None,
                }
            )
        predicate_items = []
        for predicate in contract.predicates:
            row = results.get((predicate.predicate_id, predicate.predicate_version))
            predicate_items.append(
                {
                    "predicate_id": predicate.predicate_id,
                    "predicate_version": predicate.predicate_version,
                    "predicate_order": predicate.order,
                    "required": predicate.required,
                    "evidence_snapshot_id": row.evidence_snapshot_id if row else None,
                    "evidence_key": row.evidence_key if row else predicate.evidence_key,
                    "result_status": row.result_status if row else "missing_result",
                    "passed": bool(row.passed) if row else False,
                    "result_payload_hash": row.canonical_result_hash if row else None,
                    "validator_id": row.validator_id if row else None,
                    "validator_version": row.validator_version if row else None,
                    "environment_configuration_hash": (
                        row.environment_configuration_hash
                        if row
                        else run.environment_configuration_hash
                    ),
                }
            )
        return canonical_json_hash(evidence_items), canonical_json_hash(predicate_items)

    def _persist_run_result(
        self,
        run: ExecutionTaskValidationRun,
        contract: StructuredValidationContract,
        classification: str,
        aggregation: Mapping[str, Any],
    ) -> None:
        snapshots = aggregation["snapshots"]
        results = aggregation["results"]
        evidence_hash, predicate_hash = self._aggregate_hashes(
            run, contract, snapshots, results
        )
        evaluation: PassPolicyEvaluation = aggregation["evaluation"]
        review = aggregation["review"]
        counts = {
            "resolved": 0,
            "evaluated": 0,
            "passed": 0,
            "failed": 0,
            "missing": 0,
            "unsupported": 0,
            "validator_error": 0,
            "invalid": 0,
        }
        for row in snapshots.values():
            counts["resolved"] += row.resolution_status == "resolved"
        for row in results.values():
            counts["evaluated"] += 1
            counts["passed"] += row.result_status == "passed"
            counts["failed"] += row.result_status == "failed"
            counts["missing"] += row.result_status == "missing_evidence"
            counts["unsupported"] += row.result_status == "unsupported"
            counts["validator_error"] += row.result_status == "validator_error"
            counts["invalid"] += row.result_status == "invalid_evidence"
        run.resolved_evidence_count = counts["resolved"]
        run.evaluated_predicate_count = counts["evaluated"]
        run.passed_predicate_count = counts["passed"]
        run.failed_predicate_count = counts["failed"]
        run.missing_predicate_count = counts["missing"]
        run.unsupported_predicate_count = counts["unsupported"]
        run.validator_error_count = counts["validator_error"]
        run.invalid_evidence_count = counts["invalid"]
        run.pass_policy_result = evaluation.status
        run.review_requirement = aggregation["review_requirement"]
        run.review_result = review
        run.final_validation_classification = classification
        run.aggregate_evidence_hash = evidence_hash
        run.aggregate_predicate_result_hash = predicate_hash
        run.bounded_reason = (
            "validation_accepted"
            if classification == "accepted"
            else (
                "validation_rejected"
                if classification == "rejected"
                else (
                    evaluation.blocker_codes[0]
                    if evaluation.blocker_codes
                    else (
                        "validation_review_required"
                        if classification == "review_required"
                        else "validation_validator_error"
                    )
                )
            )
        )
        run.bounded_detail = (
            ",".join(evaluation.blocker_codes or evaluation.failed_predicate_ids)[:1024]
            or None
        )
        result_payload = _result_payload(
            run,
            contract,
            evaluation,
            review,
            classification,
            evidence_hash,
            predicate_hash,
        )
        run.canonical_result_payload = result_payload
        run.canonical_result_hash = canonical_json_hash(result_payload)
        run.run_status = classification
        run.completed_at = self._now()
        self.db.flush()

    def _record_unexpected_terminal(
        self, run_id: int, classification: str, reason: str
    ) -> ValidationRunResult:
        reason = (
            reason
            if reason
            in {
                "validation_environment_mismatch",
                "validation_required_evidence_missing",
                "validation_required_evidence_unsupported",
                "validation_required_predicate_unsupported",
                "immutable_candidate_bytes_unavailable",
                "review_authority_missing",
                "validation_validator_error",
            }
            else "validation_validator_error"
        )
        self.db.rollback()
        run = self._load_run(run_id)
        if run.run_status in RUN_FINAL_STATUSES:
            return ValidationRunResult(run, replayed=True)
        specification = self.db.get(
            ExecutionTaskValidationSpecification, run.validation_specification_id
        )
        if specification is None:
            raise ValidationRunError(
                "validation_run_integrity_failure",
                "validation specification disappeared",
            )
        contract = _contract(specification)
        self._mark_running(run)
        aggregation = self._classify_run(run, contract)[1]
        evaluation = PassPolicyEvaluation(
            classification,
            tuple(item.predicate_id for item in contract.predicates),
            (reason,),
        )
        aggregation = dict(aggregation)
        aggregation["evaluation"] = evaluation
        aggregation["review"] = {
            "status": "not_evaluated",
            "requirement": contract.review_requirement.requirement,
        }
        self._persist_run_result(run, contract, classification, aggregation)
        self.finalize_validation_run(
            FinalizeExecutionTaskValidationCommand(
                validation_run_id=run.id,
                expected_task_state=run.task_state_at_start,
                expected_task_state_version=run.task_state_version_at_start,
                decision_idempotency_key=f"validation-decision-{run.id}",
                expected_classification=classification,
            )
        )
        self.db.commit()
        return ValidationRunResult(run)

    def finalize_validation_run(
        self, command: FinalizeExecutionTaskValidationCommand
    ) -> AcceptanceDecisionResult:
        run = self._load_run(command.validation_run_id, lock=True)
        task = (
            self.db.query(ExecutionTask)
            .filter(ExecutionTask.id == run.execution_task_id)
            .with_for_update()
            .one_or_none()
        )
        if task is None:
            raise ValidationRunError(
                "acceptance_integrity_failure", "execution task was not found"
            )
        classification = run.final_validation_classification or run.run_status
        if classification not in DECISION_STATUSES:
            raise ValidationRunError(
                "acceptance_conditions_not_met",
                "validation run has no final classification",
            )
        if (
            command.expected_classification is not None
            and command.expected_classification != classification
        ):
            raise ValidationRunError(
                "acceptance_decision_idempotency_conflict",
                "decision classification does not match run",
            )
        payload = _decision_command_payload(command, run, classification)
        command_hash = canonical_json_hash(payload)
        existing_by_key = (
            self.db.query(ExecutionTaskAcceptanceDecision)
            .filter(
                ExecutionTaskAcceptanceDecision.decision_idempotency_key
                == payload["decision_idempotency_key"]
            )
            .one_or_none()
        )
        if existing_by_key is not None:
            if existing_by_key.canonical_decision_command_hash != command_hash:
                raise ValidationRunError(
                    "acceptance_decision_idempotency_conflict",
                    "decision idempotency key is bound to another command",
                )
            return AcceptanceDecisionResult(existing_by_key, replayed=True)
        existing = (
            self.db.query(ExecutionTaskAcceptanceDecision)
            .filter(ExecutionTaskAcceptanceDecision.validation_run_id == run.id)
            .one_or_none()
        )
        if existing is not None:
            raise ValidationRunError(
                "acceptance_decision_conflict",
                "validation run already has a different decision",
            )
        if task.status != command.expected_task_state or task.state_version != int(
            command.expected_task_state_version
        ):
            raise ValidationRunError(
                "task_state_version_stale", "task state/version is stale"
            )
        if task.status != "awaiting_validation":
            raise ValidationRunError(
                "task_not_awaiting_validation", "task is not awaiting validation"
            )
        if (
            run.task_state_at_start != command.expected_task_state
            or run.task_state_version_at_start
            != int(command.expected_task_state_version)
        ):
            raise ValidationRunError(
                "task_state_version_stale", "validation run start fence is stale"
            )
        if (
            not run.aggregate_evidence_hash
            or not run.aggregate_predicate_result_hash
            or not run.canonical_result_hash
        ):
            raise ValidationRunError(
                "validation_primitives_incomplete",
                "validation run aggregates are incomplete",
            )
        self._verify_finalization_authority(run, task, classification)

        transition = None
        if classification in {"accepted", "rejected"}:
            transition = ExecutionTaskTransitionService(self.db).transition(
                ExecutionTaskTransitionCommand(
                    execution_task_id=task.id,
                    execution_plan_id=task.execution_plan_id,
                    expected_from_state="awaiting_validation",
                    expected_state_version=task.state_version,
                    to_state=(
                        "succeeded"
                        if classification == "accepted"
                        else "awaiting_recovery"
                    ),
                    reason_code=(
                        "validation_accepted"
                        if classification == "accepted"
                        else "validation_rejected"
                    ),
                    reason_detail=run.bounded_detail,
                    actor_type=command.decision_actor_type,
                    actor_id=command.decision_actor_id,
                    idempotency_key=f"validation-lifecycle-{run.id}",
                )
            )
        resulting_state = task.status
        resulting_version = task.state_version
        if transition is not None:
            resulting_state = transition.to_state
            resulting_version = transition.resulting_version
        specification = self.db.get(
            ExecutionTaskValidationSpecification, run.validation_specification_id
        )
        contract = _contract(specification) if specification else None
        if contract is None or contract.pass_policy is None:
            raise ValidationRunError(
                "acceptance_integrity_failure", "pass policy authority is missing"
            )
        review_result = run.review_result or {
            "status": "not_evaluated",
            "requirement": contract.review_requirement.requirement,
        }
        decision_payload = {
            "schema_version": ACCEPTANCE_DECISION_SCHEMA_VERSION,
            "validation_run_id": run.id,
            "execution_plan_id": run.execution_plan_id,
            "execution_task_id": run.execution_task_id,
            "candidate_outcome_id": run.candidate_outcome_id,
            "validation_specification_id": run.validation_specification_id,
            "validation_specification_hash": run.validation_specification_hash,
            "validation_run_result_hash": run.canonical_result_hash,
            "aggregate_evidence_hash": run.aggregate_evidence_hash,
            "aggregate_predicate_result_hash": run.aggregate_predicate_result_hash,
            "pass_policy_id": contract.pass_policy.policy_id,
            "pass_policy_version": contract.pass_policy.policy_version,
            "pass_policy_result": run.pass_policy_result,
            "review_requirement": run.review_requirement,
            "review_result": review_result,
            "decision_status": classification,
            "decision_reason": (
                "validation_accepted"
                if classification == "accepted"
                else (
                    "validation_rejected"
                    if classification == "rejected"
                    else run.bounded_reason
                )
            ),
            "resulting_task_state": resulting_state,
            "resulting_task_state_version": resulting_version,
            "lifecycle_transition_id": transition.event_id if transition else None,
            "lifecycle_transition_sequence": (
                transition.sequence if transition else None
            ),
            "evidence_strength": _evidence_strength(contract).to_dict(),
        }
        row = ExecutionTaskAcceptanceDecision(
            execution_plan_id=run.execution_plan_id,
            execution_task_id=run.execution_task_id,
            execution_task_attempt_id=run.execution_task_attempt_id,
            candidate_outcome_id=run.candidate_outcome_id,
            validation_specification_id=run.validation_specification_id,
            validation_specification_hash=run.validation_specification_hash,
            validation_run_id=run.id,
            validation_run_result_hash=run.canonical_result_hash,
            aggregate_evidence_hash=run.aggregate_evidence_hash,
            aggregate_predicate_result_hash=run.aggregate_predicate_result_hash,
            pass_policy_id=contract.pass_policy.policy_id,
            pass_policy_version=contract.pass_policy.policy_version,
            pass_policy_result=run.pass_policy_result or "blocked",
            review_requirement=run.review_requirement
            or contract.review_requirement.requirement,
            review_result=review_result,
            review_reference=None,
            decision_status=classification,
            decision_idempotency_key=payload["decision_idempotency_key"],
            deterministic_decision_command_id=f"validation-decision-command-{run.id}",
            canonical_decision_command_payload=payload,
            canonical_decision_command_hash=command_hash,
            canonical_decision_payload=decision_payload,
            canonical_decision_hash=canonical_json_hash(decision_payload),
            decision_reason=decision_payload["decision_reason"],
            bounded_detail=run.bounded_detail,
            decision_actor_type=command.decision_actor_type,
            decision_actor_id=command.decision_actor_id,
            decided_at=self._now(),
            lifecycle_transition_id=transition.event_id if transition else None,
            lifecycle_transition_sequence=transition.sequence if transition else None,
            resulting_task_state=resulting_state,
            resulting_task_state_version=resulting_version,
        )
        self.db.add(row)
        self.db.flush()
        run.acceptance_decision_id = row.id
        run.lifecycle_transition_id = row.lifecycle_transition_id
        run.lifecycle_transition_sequence = row.lifecycle_transition_sequence
        run.run_status = classification
        self.db.flush()
        return AcceptanceDecisionResult(row)

    def _verify_finalization_authority(
        self, run: ExecutionTaskValidationRun, task: ExecutionTask, classification: str
    ) -> None:
        runtime = self.db.get(ExecutionTaskAttemptOutcome, run.candidate_outcome_id)
        if runtime is None or runtime.outcome_status != "candidate_completed":
            raise ValidationRunError(
                "acceptance_integrity_failure",
                "candidate runtime evidence is not complete",
            )
        runtime_integrity = ExecutionTaskRuntimeExecutionService(
            self.db
        ).verify_attempt_outcome_integrity(runtime.id)
        if not runtime_integrity.verified:
            raise ValidationRunError(
                "acceptance_integrity_failure",
                "candidate runtime evidence integrity failed",
            )
        contract_integrity = ValidationContractService(
            self.db
        ).verify_validation_contract_integrity(run.validation_specification_id)
        if not contract_integrity.verified:
            raise ValidationRunError(
                "acceptance_integrity_failure", "validation contract integrity failed"
            )
        run_integrity = self.verify_validation_run_integrity(run.id)
        if not run_integrity.verified:
            raise ValidationRunError(
                "acceptance_integrity_failure", "validation run integrity failed"
            )
        if classification == "accepted":
            if run.pass_policy_result != "passed" or (
                run.review_requirement != "none"
                and (run.review_result or {}).get("status") != "satisfied"
            ):
                raise ValidationRunError(
                    "acceptance_conditions_not_met",
                    "acceptance conditions are not satisfied",
                )
        elif classification == "rejected":
            if run.pass_policy_result != "failed":
                raise ValidationRunError(
                    "acceptance_conditions_not_met",
                    "rejection requires an authoritative failed policy",
                )
            if any(
                (
                    run.unsupported_predicate_count,
                    run.validator_error_count,
                    run.invalid_evidence_count,
                    run.missing_predicate_count,
                )
            ):
                raise ValidationRunError(
                    "acceptance_conditions_not_met", "rejection has an authority gap"
                )
        elif classification not in {"blocked", "validation_error", "review_required"}:
            raise ValidationRunError(
                "acceptance_conditions_not_met", "unknown validation classification"
            )

    def verify_validation_run_integrity(
        self, validation_run_id: int
    ) -> ValidationIntegrityResult:
        run = self.db.get(ExecutionTaskValidationRun, int(validation_run_id))
        if run is None:
            return ValidationIntegrityResult(
                None, None, False, ("validation_run_missing",)
            )
        issues: list[str] = []
        plan = self.db.get(ExecutionPlan, run.execution_plan_id)
        task = self.db.get(ExecutionTask, run.execution_task_id)
        specification = self.db.get(
            ExecutionTaskValidationSpecification, run.validation_specification_id
        )
        attempt = self.db.get(ExecutionTaskAttempt, run.execution_task_attempt_id)
        outcome = self.db.get(ExecutionTaskAttemptOutcome, run.candidate_outcome_id)
        if any(item is None for item in (plan, task, specification, attempt, outcome)):
            issues.append("validation_run_authority_missing")
            return ValidationIntegrityResult(
                run.execution_plan_id, run.execution_task_id, False, tuple(issues)
            )
        assert plan is not None and task is not None and specification is not None
        assert attempt is not None and outcome is not None
        if (
            task.execution_plan_id != plan.id
            or run.validation_specification_id != task.validation_contract_id
            or specification.execution_task_id != task.id
        ):
            issues.append("validation_run_identity_mismatch")
        if (
            outcome.execution_plan_id != plan.id
            or outcome.execution_task_id != task.id
            or outcome.execution_task_attempt_id != attempt.id
            or outcome.outcome_status != "candidate_completed"
        ):
            issues.append("validation_run_candidate_mismatch")
        if (
            run.validation_specification_hash
            != specification.canonical_specification_hash
        ):
            issues.append("validation_run_specification_hash_mismatch")
        if run.validation_contract_set_hash != plan.validation_contract_set_hash:
            issues.append("validation_run_contract_set_hash_mismatch")
        contract_integrity = ValidationContractService(
            self.db
        ).verify_validation_contract_integrity(specification.id)
        issues.extend(contract_integrity.issues)
        try:
            contract = _contract(specification)
        except ValidationRunError:
            return ValidationIntegrityResult(
                run.execution_plan_id,
                run.execution_task_id,
                False,
                tuple(sorted(set(issues + ["validation_run_contract_invalid"]))),
            )
        if (
            run.validator_set_id != contract.environment.validator_set_id
            or run.validator_set_version != contract.environment.validator_set_version
            or run.environment_configuration_hash
            != contract.environment.configuration_hash
            or run.resolver_contract_version != contract.environment.resolver_version
        ):
            issues.append("validation_run_environment_mismatch")
        snapshots = {
            item.evidence_key: item
            for item in self.db.query(ExecutionTaskResolvedValidationEvidence)
            .filter(
                ExecutionTaskResolvedValidationEvidence.candidate_outcome_id
                == run.candidate_outcome_id,
                ExecutionTaskResolvedValidationEvidence.validation_specification_id
                == run.validation_specification_id,
            )
            .all()
        }
        results = {
            (item.predicate_id, item.predicate_version): item
            for item in self.db.query(ExecutionTaskValidationPredicateResult)
            .filter(
                ExecutionTaskValidationPredicateResult.candidate_outcome_id
                == run.candidate_outcome_id,
                ExecutionTaskValidationPredicateResult.validation_specification_id
                == run.validation_specification_id,
            )
            .all()
        }
        for row in snapshots.values():
            issues.extend(
                ValidationPrimitiveService(self.db)
                .verify_resolved_validation_evidence_integrity(row.id)
                .issues
            )
        for row in results.values():
            issues.extend(
                ValidationPrimitiveService(self.db)
                .verify_validation_predicate_result_integrity(row.id)
                .issues
            )
        evidence_hash, predicate_hash = self._aggregate_hashes(
            run, contract, snapshots, results
        )
        if (
            run.aggregate_evidence_hash is not None
            and run.aggregate_evidence_hash != evidence_hash
        ):
            issues.append("validation_run_aggregate_evidence_hash_mismatch")
        if (
            run.aggregate_predicate_result_hash is not None
            and run.aggregate_predicate_result_hash != predicate_hash
        ):
            issues.append("validation_run_aggregate_predicate_hash_mismatch")
        expected_counts = {
            "required_evidence_count": sum(
                item.required for item in contract.evidence_descriptors
            ),
            "resolved_evidence_count": sum(
                item.resolution_status == "resolved" for item in snapshots.values()
            ),
            "required_predicate_count": sum(
                item.required for item in contract.predicates
            ),
            "evaluated_predicate_count": len(results),
            "passed_predicate_count": sum(
                item.result_status == "passed" for item in results.values()
            ),
            "failed_predicate_count": sum(
                item.result_status == "failed" for item in results.values()
            ),
            "missing_predicate_count": sum(
                item.result_status == "missing_evidence" for item in results.values()
            ),
            "unsupported_predicate_count": sum(
                item.result_status == "unsupported" for item in results.values()
            ),
            "validator_error_count": sum(
                item.result_status == "validator_error" for item in results.values()
            ),
            "invalid_evidence_count": sum(
                item.result_status == "invalid_evidence" for item in results.values()
            ),
        }
        for field, expected in expected_counts.items():
            if int(getattr(run, field)) != expected:
                issues.append(f"validation_run_{field}_mismatch")
        evaluation = evaluate_pass_policy(contract, results)
        if (
            run.pass_policy_result is not None
            and run.pass_policy_result != evaluation.status
            and not (
                run.run_status == "validation_error"
                and run.pass_policy_result == "validation_error"
            )
        ):
            issues.append("validation_run_pass_policy_result_mismatch")
        effective_evaluation = evaluation
        if (
            run.run_status == "validation_error"
            and run.pass_policy_result == "validation_error"
        ):
            effective_evaluation = PassPolicyEvaluation(
                "validation_error",
                tuple(item.predicate_id for item in contract.predicates),
                ("validation_validator_error",),
            )
        if run.canonical_result_payload is None or run.canonical_result_hash is None:
            if run.final_validation_classification is not None:
                issues.append("validation_run_result_payload_missing")
        else:
            expected_payload = _result_payload(
                run,
                contract,
                effective_evaluation,
                run.review_result
                or {
                    "status": "not_evaluated",
                    "requirement": contract.review_requirement.requirement,
                },
                run.final_validation_classification or run.run_status,
                evidence_hash,
                predicate_hash,
            )
            if run.canonical_result_payload != expected_payload:
                issues.append("validation_run_result_payload_tampered")
            if (
                canonical_json_hash(run.canonical_result_payload)
                != run.canonical_result_hash
            ):
                issues.append("validation_run_result_hash_mismatch")
        if run.final_validation_classification not in {None, *RUN_FINAL_STATUSES}:
            issues.append("validation_run_classification_invalid")
        if (
            run.completed_at is not None
            and run.started_at
            and (_utc(run.completed_at) < _utc(run.started_at))
        ):
            issues.append("validation_run_timestamp_order_invalid")
        if (
            run.final_validation_classification in {"accepted", "rejected"}
            and run.lifecycle_transition_id is None
            and run.acceptance_decision_id is not None
        ):
            issues.append("validation_run_lifecycle_reference_missing")
        if (
            run.final_validation_classification
            in {"blocked", "validation_error", "review_required"}
            and run.lifecycle_transition_id is not None
        ):
            issues.append("validation_run_lifecycle_reference_unexpected")
        return ValidationIntegrityResult(
            run.execution_plan_id,
            run.execution_task_id,
            not issues,
            tuple(sorted(set(issues))),
        )

    def verify_acceptance_decision_integrity(
        self, decision_id: int
    ) -> ValidationIntegrityResult:
        decision = self.db.get(ExecutionTaskAcceptanceDecision, int(decision_id))
        if decision is None:
            return ValidationIntegrityResult(
                None, None, False, ("acceptance_decision_missing",)
            )
        run_result = self.verify_validation_run_integrity(decision.validation_run_id)
        issues = list(run_result.issues)
        run = self.db.get(ExecutionTaskValidationRun, decision.validation_run_id)
        task = self.db.get(ExecutionTask, decision.execution_task_id)
        specification = self.db.get(
            ExecutionTaskValidationSpecification, decision.validation_specification_id
        )
        if run is None or task is None or specification is None:
            issues.append("acceptance_decision_authority_missing")
            return ValidationIntegrityResult(
                decision.execution_plan_id,
                decision.execution_task_id,
                False,
                tuple(sorted(set(issues))),
            )
        if run.acceptance_decision_id != decision.id:
            issues.append("acceptance_decision_run_reference_mismatch")
        if (
            decision.execution_plan_id != run.execution_plan_id
            or decision.execution_task_id != run.execution_task_id
            or decision.candidate_outcome_id != run.candidate_outcome_id
            or decision.validation_specification_id != run.validation_specification_id
        ):
            issues.append("acceptance_decision_identity_mismatch")
        if (
            decision.validation_run_result_hash != run.canonical_result_hash
            or decision.aggregate_evidence_hash != run.aggregate_evidence_hash
            or decision.aggregate_predicate_result_hash
            != run.aggregate_predicate_result_hash
        ):
            issues.append("acceptance_decision_run_hash_mismatch")
        if (
            decision.validation_specification_hash
            != specification.canonical_specification_hash
        ):
            issues.append("acceptance_decision_specification_hash_mismatch")
        if decision.decision_status != run.final_validation_classification:
            issues.append("acceptance_decision_run_status_mismatch")
        if decision.decision_status == "accepted":
            if (
                run.pass_policy_result != "passed"
                or task.status != "succeeded"
                or decision.resulting_task_state != "succeeded"
                or decision.lifecycle_transition_id is None
            ):
                issues.append("accepted_lifecycle_mismatch")
        elif decision.decision_status == "rejected":
            if (
                run.pass_policy_result != "failed"
                or task.status != "awaiting_recovery"
                or decision.resulting_task_state != "awaiting_recovery"
                or decision.lifecycle_transition_id is None
            ):
                issues.append("rejected_lifecycle_mismatch")
        elif decision.lifecycle_transition_id is not None:
            issues.append("lifecycle_neutral_decision_has_transition")
        if decision.lifecycle_transition_id is not None:
            transition = self.db.get(
                ExecutionTaskTransition, decision.lifecycle_transition_id
            )
            expected_state = (
                "succeeded"
                if decision.decision_status == "accepted"
                else "awaiting_recovery"
            )
            expected_reason = (
                "validation_accepted"
                if decision.decision_status == "accepted"
                else "validation_rejected"
            )
            if transition is None:
                issues.append("acceptance_lifecycle_transition_missing")
            elif (
                transition.execution_plan_id != run.execution_plan_id
                or transition.execution_task_id != run.execution_task_id
                or transition.from_state != "awaiting_validation"
                or transition.to_state != expected_state
                or transition.reason_code != expected_reason
                or transition.sequence != decision.lifecycle_transition_sequence
                or transition.resulting_version != decision.resulting_task_state_version
            ):
                issues.append("acceptance_lifecycle_transition_mismatch")
        try:
            if (
                canonical_json_hash(decision.canonical_decision_payload)
                != decision.canonical_decision_hash
            ):
                issues.append("acceptance_decision_hash_mismatch")
            if (
                canonical_json_hash(decision.canonical_decision_command_payload)
                != decision.canonical_decision_command_hash
            ):
                issues.append("acceptance_decision_command_hash_mismatch")
        except (TypeError, ValueError):
            issues.append("acceptance_decision_payload_malformed")
        return ValidationIntegrityResult(
            decision.execution_plan_id,
            decision.execution_task_id,
            not issues,
            tuple(sorted(set(issues))),
        )

    def verify_execution_task_validation_acceptance_integrity(
        self, execution_task_id: int
    ) -> ValidationIntegrityResult:
        task = self.db.get(ExecutionTask, int(execution_task_id))
        if task is None:
            return ValidationIntegrityResult(
                None, int(execution_task_id), False, ("execution_task_not_found",)
            )
        runs = (
            self.db.query(ExecutionTaskValidationRun)
            .filter(ExecutionTaskValidationRun.execution_task_id == task.id)
            .all()
        )
        decisions = (
            self.db.query(ExecutionTaskAcceptanceDecision)
            .filter(ExecutionTaskAcceptanceDecision.execution_task_id == task.id)
            .all()
        )
        issues: list[str] = []
        if len(runs) > 1:
            issues.append("duplicate_validation_run")
        if len(decisions) > 1:
            issues.append("duplicate_acceptance_decision")
        for run in runs:
            issues.extend(self.verify_validation_run_integrity(run.id).issues)
        for decision in decisions:
            issues.extend(self.verify_acceptance_decision_integrity(decision.id).issues)
        if task.status == "succeeded" and not any(
            item.decision_status == "accepted" for item in decisions
        ):
            issues.append("succeeded_task_without_accepted_decision")
        if (
            task.status == "awaiting_recovery"
            and any(item.run_status == "rejected" for item in runs)
            and not any(item.decision_status == "rejected" for item in decisions)
        ):
            issues.append("awaiting_recovery_without_rejected_decision")
        return ValidationIntegrityResult(
            task.execution_plan_id, task.id, not issues, tuple(sorted(set(issues)))
        )

    def verify_execution_plan_validation_acceptance_integrity(
        self, execution_plan_id: int
    ) -> ValidationIntegrityResult:
        plan = self.db.get(ExecutionPlan, int(execution_plan_id))
        if plan is None:
            return ValidationIntegrityResult(
                int(execution_plan_id), None, False, ("execution_plan_not_found",)
            )
        issues: list[str] = []
        for task in (
            self.db.query(ExecutionTask)
            .filter(ExecutionTask.execution_plan_id == plan.id)
            .order_by(ExecutionTask.plan_task_id)
            .all()
        ):
            issues.extend(
                f"task:{task.id}:{issue}"
                for issue in self.verify_execution_task_validation_acceptance_integrity(
                    task.id
                ).issues
            )
        return ValidationIntegrityResult(
            plan.id, None, not issues, tuple(sorted(set(issues)))
        )

    def inspect_execution_task_validation(
        self, execution_task_id: int
    ) -> ValidationInspectionProjection:
        task = self.db.get(ExecutionTask, int(execution_task_id))
        if task is None:
            raise ValidationRunError(
                "execution_task_not_found", "Execution Task was not found"
            )
        run = (
            self.db.query(ExecutionTaskValidationRun)
            .filter(ExecutionTaskValidationRun.execution_task_id == task.id)
            .order_by(ExecutionTaskValidationRun.id.desc())
            .first()
        )
        if run is None:
            return ValidationInspectionProjection(
                task.execution_plan_id,
                task.id,
                "validation_not_started",
                None,
                None,
                None,
            )
        decision = (
            self.db.query(ExecutionTaskAcceptanceDecision)
            .filter(ExecutionTaskAcceptanceDecision.validation_run_id == run.id)
            .one_or_none()
        )
        if decision is not None and decision.decision_status in {
            "accepted",
            "rejected",
        }:
            expected = (
                "succeeded"
                if decision.decision_status == "accepted"
                else "awaiting_recovery"
            )
            acceptance_integrity = self.verify_acceptance_decision_integrity(
                decision.id
            )
            if (
                not acceptance_integrity.verified
                or task.status != expected
                or task.state_version != decision.resulting_task_state_version
            ):
                state = "decision_lifecycle_mismatch"
            elif decision.decision_status == "accepted":
                state = "accepted_dependency_released"
            else:
                state = "validation_rejected"
        else:
            state = {
                "pending": "validation_running",
                "running": "validation_running",
                "blocked": "validation_blocked",
                "validation_error": "validation_error",
                "review_required": "validation_passed_review_required",
                "accepted": "validation_accepted",
                "rejected": "validation_rejected",
            }.get(run.run_status, "validation_primitives_incomplete")
        blockers = tuple(
            item for item in (run.bounded_reason, run.bounded_detail) if item
        )
        strength = None
        specification = self.db.get(
            ExecutionTaskValidationSpecification, run.validation_specification_id
        )
        if (
            specification is not None
            and task.validation_contract_status == "structured_executable"
        ):
            strength = _evidence_strength(_contract(specification))
        return ValidationInspectionProjection(
            task.execution_plan_id,
            task.id,
            state,
            run.id,
            run.run_status,
            decision.decision_status if decision else None,
            blockers,
            strength,
        )


class PassPolicyEvaluator:
    """Small injectable facade for the pure pass-policy function."""

    @staticmethod
    def evaluate(
        contract: StructuredValidationContract,
        results: Mapping[tuple[str, int], ExecutionTaskValidationPredicateResult],
    ) -> PassPolicyEvaluation:
        return evaluate_pass_policy(contract, results)


def execute_validation_run(
    db: Session,
    command: StartExecutionTaskValidationCommand,
    *,
    registry: DeterministicValidatorRegistry | None = None,
) -> ValidationRunResult:
    return ValidationRunService(db).execute_validation_run(command, registry=registry)


def verify_validation_run_integrity(
    db: Session, validation_run_id: int
) -> ValidationIntegrityResult:
    return ValidationRunService(db).verify_validation_run_integrity(validation_run_id)


def verify_acceptance_decision_integrity(
    db: Session, decision_id: int
) -> ValidationIntegrityResult:
    return ValidationRunService(db).verify_acceptance_decision_integrity(decision_id)


def verify_execution_task_validation_acceptance_integrity(
    db: Session, execution_task_id: int
) -> ValidationIntegrityResult:
    return ValidationRunService(
        db
    ).verify_execution_task_validation_acceptance_integrity(execution_task_id)


def verify_execution_plan_validation_acceptance_integrity(
    db: Session, execution_plan_id: int
) -> ValidationIntegrityResult:
    return ValidationRunService(
        db
    ).verify_execution_plan_validation_acceptance_integrity(execution_plan_id)


__all__ = [
    "AcceptanceDecisionResult",
    "EvidenceStrengthProjection",
    "FinalizeExecutionTaskValidationCommand",
    "PassPolicyEvaluation",
    "PassPolicyEvaluator",
    "StartExecutionTaskValidationCommand",
    "ValidationInspectionProjection",
    "ValidationIntegrityResult",
    "ValidationRunError",
    "ValidationRunResult",
    "ValidationRunService",
    "execute_validation_run",
    "evaluate_pass_policy",
    "verify_acceptance_decision_integrity",
    "verify_execution_plan_validation_acceptance_integrity",
    "verify_execution_task_validation_acceptance_integrity",
    "verify_validation_run_integrity",
]
