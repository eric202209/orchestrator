"""Release-boundary persistence and integrity for validation contracts.

This service creates and verifies immutable contract records only.  It never
reads candidate output, resolves evidence, invokes a predicate, makes an
acceptance decision, or changes an Execution Task lifecycle state.
"""

from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
from typing import Any

from sqlalchemy.orm import Session

from app.models import (
    ExecutionPlan,
    ExecutionTask,
    ExecutionTaskValidationSpecification,
)
from app.services.planning.structured_task_plan import Task
from app.services.planning.validation_contract import (
    RELEASE_CONTRACT_STATUSES,
    StructuredValidationContract,
    TaskValidationContractProjection,
    ValidationContractError,
    build_task_validation_contract,
    canonical_validation_hash,
)


class ExecutionValidationContractError(RuntimeError):
    """Bounded persistence/integrity error for the release boundary."""

    def __init__(self, code: str, message: str):
        super().__init__(f"{code}: {message}")
        self.code = code
        self.message = message


@dataclass(frozen=True)
class ValidationContractIntegrityResult:
    execution_plan_id: int | None
    execution_task_id: int | None
    contract_status: str | None
    specification_hash: str | None
    verified: bool
    issues: tuple[str, ...] = ()


@dataclass(frozen=True)
class ValidationContractInspection:
    execution_plan_id: int
    execution_task_id: int
    contract_status: str
    specification_hash: str | None
    blocker_code: str | None
    predicate_count: int
    evidence_descriptor_count: int
    review_requirement: str | None
    integrity_verified: bool


def _task_from_snapshot(task: ExecutionTask) -> Task:
    if not isinstance(task.task_spec, Mapping):
        raise ValidationContractError(
            "validation_contract_integrity_failure", "task snapshot is invalid"
        )
    try:
        return Task(**dict(task.task_spec))
    except (TypeError, ValueError, ValidationContractError) as exc:
        raise ValidationContractError(
            "validation_contract_integrity_failure", "task snapshot is invalid"
        ) from exc


def _projection_for_snapshot(
    task: ExecutionTask,
) -> TaskValidationContractProjection:
    return build_task_validation_contract(_task_from_snapshot(task))


def _contract_set_hash(
    task_and_specifications: list[
        tuple[ExecutionTask, ExecutionTaskValidationSpecification]
    ],
) -> str:
    values = [
        {
            "plan_task_id": task.plan_task_id,
            "contract_status": specification.contract_status,
            "specification_hash": specification.canonical_specification_hash,
        }
        for task, specification in sorted(
            task_and_specifications, key=lambda item: item[0].plan_task_id
        )
    ]
    return canonical_validation_hash(values)


class ValidationContractService:
    """Build, persist, inspect, and verify release-bound contract records."""

    def __init__(self, db: Session):
        self.db = db

    @staticmethod
    def preflight_task_plan(task_plan: Any) -> None:
        """Validate authored contracts before Planning Transaction A starts."""

        for task in task_plan.tasks:
            try:
                build_task_validation_contract(task)
            except ValidationContractError:
                raise
            except (TypeError, ValueError) as exc:
                raise ValidationContractError(
                    "validation_contract_parameters_invalid",
                    "validation contract is malformed",
                ) from exc

    @staticmethod
    def projection_for_task(task: Any) -> TaskValidationContractProjection:
        return build_task_validation_contract(task)

    def create_for_task(
        self,
        *,
        execution_plan: ExecutionPlan,
        execution_task: ExecutionTask,
        authored_task: Any,
    ) -> ExecutionTaskValidationSpecification:
        try:
            projection = build_task_validation_contract(authored_task)
        except ValidationContractError:
            raise
        except (TypeError, ValueError) as exc:
            raise ExecutionValidationContractError(
                "validation_contract_parameters_invalid",
                "validation contract is malformed",
            ) from exc

        structured_contract = projection.canonical_payload.get("structured_contract")
        source = (
            projection.structured_contract.specification_source
            if projection.structured_contract is not None
            else "legacy_compatibility"
        )
        row = ExecutionTaskValidationSpecification(
            execution_plan_id=execution_plan.id,
            execution_task_id=execution_task.id,
            release_generation=execution_plan.generation,
            contract_status=projection.contract_status,
            schema_version=projection.canonical_payload["schema_version"],
            original_done_when=list(projection.original_done_when),
            structured_contract=structured_contract,
            pass_policy=(
                structured_contract.get("pass_policy")
                if isinstance(structured_contract, Mapping)
                else None
            ),
            review_requirement=(
                structured_contract.get("review_requirement")
                if isinstance(structured_contract, Mapping)
                else None
            ),
            environment_identity=(
                structured_contract.get("environment")
                if isinstance(structured_contract, Mapping)
                else None
            ),
            validator_set_identity=(
                structured_contract.get("environment", {}).get("validator_set_id")
                if isinstance(structured_contract, Mapping)
                else None
            ),
            canonical_payload=projection.canonical_payload,
            canonical_specification_hash=projection.canonical_hash,
            hash_algorithm="sha256",
            specification_source=source,
            release_authority_reference=execution_plan.source_commit_identity,
            creation_actor_type="execution_plan_release",
            creation_actor_id=execution_plan.source_commit_identity,
        )
        self.db.add(row)
        self.db.flush()
        return row

    def verify_validation_contract_integrity(
        self, specification_id: int
    ) -> ValidationContractIntegrityResult:
        specification = self.db.get(
            ExecutionTaskValidationSpecification, int(specification_id)
        )
        if specification is None:
            return ValidationContractIntegrityResult(
                execution_plan_id=None,
                execution_task_id=None,
                contract_status=None,
                specification_hash=None,
                verified=False,
                issues=("validation_contract_missing",),
            )
        task = self.db.get(ExecutionTask, specification.execution_task_id)
        if task is None:
            return ValidationContractIntegrityResult(
                execution_plan_id=specification.execution_plan_id,
                execution_task_id=specification.execution_task_id,
                contract_status=specification.contract_status,
                specification_hash=specification.canonical_specification_hash,
                verified=False,
                issues=("validation_contract_task_identity_mismatch",),
            )
        issues: list[str] = []
        plan = self.db.get(ExecutionPlan, task.execution_plan_id)
        if plan is None or specification.execution_plan_id != task.execution_plan_id:
            issues.append("validation_contract_task_identity_mismatch")
        if specification.execution_task_id != task.id:
            issues.append("validation_contract_task_identity_mismatch")
        if task.validation_contract_id != specification.id:
            issues.append("validation_contract_release_reference_mismatch")
        if task.validation_contract_status != specification.contract_status:
            issues.append("validation_contract_post_release_mutation")
        if specification.contract_status not in RELEASE_CONTRACT_STATUSES:
            issues.append("validation_contract_schema_unsupported")
        if specification.hash_algorithm != "sha256":
            issues.append("validation_contract_hash_mismatch")
        try:
            projection = _projection_for_snapshot(task)
        except ValidationContractError:
            projection = None
            issues.append("validation_contract_integrity_failure")
        if projection is not None:
            if specification.original_done_when != list(projection.original_done_when):
                issues.append("validation_contract_post_release_mutation")
            if specification.contract_status != projection.contract_status:
                issues.append("validation_contract_post_release_mutation")
            if specification.canonical_payload != projection.canonical_payload:
                issues.append("validation_contract_post_release_mutation")
            if specification.canonical_specification_hash != projection.canonical_hash:
                issues.append("validation_contract_hash_mismatch")
        if not isinstance(specification.canonical_payload, Mapping):
            issues.append("validation_contract_hash_mismatch")
        elif (
            canonical_validation_hash(specification.canonical_payload)
            != specification.canonical_specification_hash
        ):
            issues.append("validation_contract_hash_mismatch")
        if plan is not None:
            if specification.release_generation != plan.generation:
                issues.append("validation_contract_release_reference_mismatch")
            if specification.release_authority_reference != plan.source_commit_identity:
                issues.append("validation_contract_release_reference_mismatch")
        return ValidationContractIntegrityResult(
            execution_plan_id=specification.execution_plan_id,
            execution_task_id=specification.execution_task_id,
            contract_status=specification.contract_status,
            specification_hash=specification.canonical_specification_hash,
            verified=not issues,
            issues=tuple(sorted(set(issues))),
        )

    def verify_execution_task_validation_contract_integrity(
        self, execution_task_id: int
    ) -> ValidationContractIntegrityResult:
        task = self.db.get(ExecutionTask, int(execution_task_id))
        if task is None:
            return ValidationContractIntegrityResult(
                execution_plan_id=None,
                execution_task_id=int(execution_task_id),
                contract_status=None,
                specification_hash=None,
                verified=False,
                issues=("validation_contract_missing",),
            )
        specifications = (
            self.db.query(ExecutionTaskValidationSpecification)
            .filter(ExecutionTaskValidationSpecification.execution_task_id == task.id)
            .all()
        )
        if len(specifications) != 1:
            return ValidationContractIntegrityResult(
                execution_plan_id=task.execution_plan_id,
                execution_task_id=task.id,
                contract_status=task.validation_contract_status,
                specification_hash=None,
                verified=False,
                issues=("validation_contract_missing",),
            )
        return self.verify_validation_contract_integrity(specifications[0].id)

    def verify_execution_plan_validation_contract_integrity(
        self, execution_plan_id: int
    ) -> ValidationContractIntegrityResult:
        plan = self.db.get(ExecutionPlan, int(execution_plan_id))
        if plan is None:
            return ValidationContractIntegrityResult(
                execution_plan_id=int(execution_plan_id),
                execution_task_id=None,
                contract_status=None,
                specification_hash=None,
                verified=False,
                issues=("validation_contract_missing",),
            )
        tasks = (
            self.db.query(ExecutionTask)
            .filter(ExecutionTask.execution_plan_id == plan.id)
            .order_by(ExecutionTask.plan_task_id.asc())
            .all()
        )
        issues: list[str] = []
        pairs: list[tuple[ExecutionTask, ExecutionTaskValidationSpecification]] = []
        for task in tasks:
            result = self.verify_execution_task_validation_contract_integrity(task.id)
            issues.extend(result.issues)
            specification = (
                self.db.query(ExecutionTaskValidationSpecification)
                .filter(
                    ExecutionTaskValidationSpecification.execution_task_id == task.id
                )
                .one_or_none()
            )
            if specification is not None:
                pairs.append((task, specification))
        if len(pairs) != len(tasks):
            issues.append("validation_contract_missing")
        if plan.validation_contract_set_hash != _contract_set_hash(pairs):
            issues.append("validation_contract_hash_mismatch")
        return ValidationContractIntegrityResult(
            execution_plan_id=plan.id,
            execution_task_id=None,
            contract_status=None,
            specification_hash=plan.validation_contract_set_hash,
            verified=not issues,
            issues=tuple(sorted(set(issues))),
        )

    def inspect_execution_task(
        self, execution_task_id: int
    ) -> ValidationContractInspection:
        task = self.db.get(ExecutionTask, int(execution_task_id))
        if task is None:
            raise ExecutionValidationContractError(
                "validation_contract_missing", "Execution Task was not found"
            )
        plan = self.db.get(ExecutionPlan, task.execution_plan_id)
        if plan is None:
            raise ExecutionValidationContractError(
                "validation_contract_missing", "Execution Plan was not found"
            )
        specification = (
            self.db.query(ExecutionTaskValidationSpecification)
            .filter(ExecutionTaskValidationSpecification.execution_task_id == task.id)
            .one_or_none()
        )
        integrity = self.verify_execution_task_validation_contract_integrity(task.id)
        if specification is None:
            status = task.validation_contract_status or "legacy_unstructured"
            return ValidationContractInspection(
                execution_plan_id=plan.id,
                execution_task_id=task.id,
                contract_status=status,
                specification_hash=None,
                blocker_code="validation_contract_unavailable",
                predicate_count=0,
                evidence_descriptor_count=0,
                review_requirement=None,
                integrity_verified=False,
            )
        payload = specification.canonical_payload
        contract = (
            payload.get("structured_contract") if isinstance(payload, Mapping) else None
        )
        blocker = None
        status = specification.contract_status
        if plan.superseded_by_execution_plan_id is not None:
            status = "superseded_contract"
        elif status == "legacy_unstructured":
            blocker = "validation_contract_unavailable"
        elif status == "unsupported":
            blocker = "validation_contract_unavailable"
        return ValidationContractInspection(
            execution_plan_id=plan.id,
            execution_task_id=task.id,
            contract_status=status,
            specification_hash=specification.canonical_specification_hash,
            blocker_code=blocker,
            predicate_count=(
                len(contract.get("predicates", []))
                if isinstance(contract, Mapping)
                else 0
            ),
            evidence_descriptor_count=(
                len(contract.get("evidence_descriptors", []))
                if isinstance(contract, Mapping)
                else 0
            ),
            review_requirement=(
                contract.get("review_requirement", {}).get("requirement")
                if isinstance(contract, Mapping)
                and isinstance(contract.get("review_requirement"), Mapping)
                else None
            ),
            integrity_verified=integrity.verified,
        )


def verify_validation_contract_integrity(
    db: Session, specification_id: int
) -> ValidationContractIntegrityResult:
    return ValidationContractService(db).verify_validation_contract_integrity(
        specification_id
    )


def verify_execution_task_validation_contract_integrity(
    db: Session, execution_task_id: int
) -> ValidationContractIntegrityResult:
    return ValidationContractService(
        db
    ).verify_execution_task_validation_contract_integrity(execution_task_id)


def verify_execution_plan_validation_contract_integrity(
    db: Session, execution_plan_id: int
) -> ValidationContractIntegrityResult:
    return ValidationContractService(
        db
    ).verify_execution_plan_validation_contract_integrity(execution_plan_id)


__all__ = [
    "ExecutionValidationContractError",
    "ValidationContractInspection",
    "ValidationContractIntegrityResult",
    "ValidationContractService",
    "verify_execution_plan_validation_contract_integrity",
    "verify_execution_task_validation_contract_integrity",
    "verify_validation_contract_integrity",
]
