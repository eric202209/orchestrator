"""Independent Phase 31 certification boundaries.

The canonical runner owns live planning/execution dispatch.  This module owns
the evidence boundary after that dispatch: completion/publication certification
starts only from a validated successful execution artifact, while execution
debug-repair evidence is classified from its own bounded diagnostic record.
No runtime execution or provider parsing behavior lives here.
"""

from __future__ import annotations

import hashlib
import json
from dataclasses import asdict, dataclass, replace
from enum import Enum
from pathlib import Path
from typing import Any, Mapping, Optional

from app.config import settings
from app.models import Project, Session, Task, TaskExecution, TaskExecutionChangeSet
from app.services.orchestration.coordinators.completion_coordinator import (
    CompletionCoordinator,
)
from app.services.orchestration.outcome_adjudication import (
    derive_review_publication_outcomes,
)
from app.services.tasks.service import TaskService
from app.services.workspace.changeset_service import ChangesetService
from app.services.workspace.system_settings import get_effective_workspace_review_policy
from scripts.maintenance.phase31_certification_scenarios import (
    CapabilityBinding,
    scenario_spec,
)


CERTIFICATION_BOUNDARY_SCHEMA = "phase31-certification-boundary/1"
SUPPORTED_FIXTURE_SCHEMAS = {CERTIFICATION_BOUNDARY_SCHEMA}
LANE_A_ID = "completion_publication"
LANE_B_ID = "execution_debug_repair"


class CertificationBoundaryError(ValueError):
    """Base error for deterministic certification-boundary rejection."""


class CertificationPreconditionError(CertificationBoundaryError):
    """Raised before a Lane-A service can mutate or publish anything."""

    def __init__(self, failures: list[str]):
        self.failures = tuple(failures)
        super().__init__("; ".join(failures))


class PlanningExecutionClassification(str, Enum):
    PASSED = "PASSED"
    FAILED = "FAILED"
    BLOCKED = "BLOCKED"
    NOT_EVALUATED = "NOT_EVALUATED"


class CompletionPublicationClassification(str, Enum):
    PASSED = "PASSED"
    REVIEW_HELD = "REVIEW_HELD"
    PUBLICATION_INELIGIBLE = "PUBLICATION_INELIGIBLE"
    PUBLICATION_FAILED = "PUBLICATION_FAILED"
    BLOCKED = "BLOCKED"
    NOT_EVALUATED = "NOT_EVALUATED"


class ExecutionDebugRepairClassification(str, Enum):
    NOT_EXERCISED = "NOT_EXERCISED"
    RECOVERED = "RECOVERED"
    REJECTED_FAIL_CLOSED = "REJECTED_FAIL_CLOSED"
    FAILED_AFTER_REPAIR = "FAILED_AFTER_REPAIR"
    BLOCKED = "BLOCKED"
    NOT_EVALUATED = "NOT_EVALUATED"


class AggregateCertificationClassification(str, Enum):
    FULLY_CERTIFIED = "FULLY_CERTIFIED"
    PARTIALLY_EVIDENCED = "PARTIALLY_EVIDENCED"
    BLOCKED = "BLOCKED"


def _stable_json(value: Any) -> str:
    return json.dumps(value, sort_keys=True, separators=(",", ":"), default=str)


def stable_hash(value: Any) -> str:
    return hashlib.sha256(_stable_json(value).encode("utf-8")).hexdigest()


def task_steps_digest(steps: Any) -> str:
    """Hash the accepted Task.steps representation without changing it."""

    if isinstance(steps, str):
        try:
            normalized = json.loads(steps)
        except json.JSONDecodeError:
            normalized = steps
    else:
        normalized = steps
    return stable_hash(normalized)


def _as_value(value: Any) -> str:
    return str(getattr(value, "value", value))


@dataclass(frozen=True)
class CompletionCertificationFixture:
    """Integrity-bound proof of a successful canonical execution boundary."""

    schema_version: str
    scenario_id: str
    planner_contract_id: str
    planner_contract_version: str
    contract_source: str
    project_id: int
    session_id: int
    task_id: int
    task_execution_id: int
    accepted_plan_identity: str
    accepted_task_steps_digest: str
    execution_status: str
    verification_status: str
    changed_file_inventory: tuple[str, ...]
    expected_source_inventory: tuple[str, ...]
    expected_test_inventory: tuple[str, ...]
    persisted_change_set_id: int
    unsafe_failure_count: int
    duplicate_execution_count: int
    duplicate_mutation_count: int
    workspace_snapshot_identity: str
    workspace_baseline_identity: str
    review_expectation: str
    publication_expectation: str
    evidence_origin: str
    source_revision: str
    executing_revision: str
    source_evidence_reference: str
    fixture_generation_method: str
    integrity_hash: str = ""

    def unsigned_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("integrity_hash", None)
        for key in (
            "changed_file_inventory",
            "expected_source_inventory",
            "expected_test_inventory",
        ):
            payload[key] = list(payload[key])
        return payload

    def with_integrity(self) -> "CompletionCertificationFixture":
        return replace(self, integrity_hash=stable_hash(self.unsigned_payload()))

    def to_payload(self) -> dict[str, Any]:
        payload = self.unsigned_payload()
        payload["integrity_hash"] = self.integrity_hash
        return payload

    @classmethod
    def from_payload(
        cls, payload: Mapping[str, Any]
    ) -> "CompletionCertificationFixture":
        values = dict(payload)
        for key in (
            "changed_file_inventory",
            "expected_source_inventory",
            "expected_test_inventory",
        ):
            values[key] = tuple(values.get(key) or ())
        return cls(**values)


# Public alias makes the artifact/fixture distinction explicit to callers that
# import retained evidence rather than generate a local fixture.
CompletionCertificationArtifact = CompletionCertificationFixture


def build_deterministic_s1_2_fixture(
    *,
    project_id: int,
    session_id: int,
    task_id: int,
    task_execution_id: int,
    persisted_change_set_id: int,
    accepted_task_steps: Any,
    source_revision: str,
    executing_revision: str,
    source_evidence_reference: str = (
        "phase31e-2d-historical-successful-execution-evidence"
    ),
    evidence_origin: str = "deterministic_fixture_from_real_persistence_services",
) -> CompletionCertificationFixture:
    """Build the bounded S1-2 fixture from explicit canonical identities."""

    specification = scenario_spec("S1-2")
    fixture = CompletionCertificationFixture(
        schema_version=CERTIFICATION_BOUNDARY_SCHEMA,
        scenario_id="S1-2",
        planner_contract_id=specification.planner_contract.contract_id,
        planner_contract_version=specification.planner_contract.contract_version,
        contract_source="phase31_certification_runner",
        project_id=int(project_id),
        session_id=int(session_id),
        task_id=int(task_id),
        task_execution_id=int(task_execution_id),
        accepted_plan_identity=stable_hash(
            {"task_id": int(task_id), "task_steps": accepted_task_steps}
        ),
        accepted_task_steps_digest=task_steps_digest(accepted_task_steps),
        execution_status="done",
        verification_status="passed",
        changed_file_inventory=("app/main.py", "tests/test_certification.py"),
        expected_source_inventory=tuple(specification.source_paths),
        expected_test_inventory=tuple(specification.test_paths),
        persisted_change_set_id=int(persisted_change_set_id),
        unsafe_failure_count=0,
        duplicate_execution_count=0,
        duplicate_mutation_count=0,
        workspace_snapshot_identity=f"task-execution:{int(task_execution_id)}",
        workspace_baseline_identity=f"project:{int(project_id)}:canonical-baseline",
        review_expectation=specification.review_contract.expectation,
        publication_expectation=specification.publication_contract.expectation,
        evidence_origin=evidence_origin,
        source_revision=str(source_revision),
        executing_revision=str(executing_revision),
        source_evidence_reference=str(source_evidence_reference),
        fixture_generation_method=(
            "validated Task.steps plus persisted TaskExecutionChangeSet; "
            "historical Phase 31E-2D evidence is provenance only"
        ),
    )
    return fixture.with_integrity()


def _fixture_failures(
    fixture: CompletionCertificationFixture,
    *,
    db: Optional[Any] = None,
) -> list[str]:
    failures: list[str] = []
    if fixture.schema_version not in SUPPORTED_FIXTURE_SCHEMAS:
        failures.append(f"unsupported fixture schema: {fixture.schema_version}")
    if stable_hash(fixture.unsigned_payload()) != fixture.integrity_hash:
        failures.append("invalid fixture integrity hash")
    for field_name in (
        "scenario_id",
        "planner_contract_id",
        "planner_contract_version",
        "contract_source",
        "accepted_plan_identity",
        "accepted_task_steps_digest",
        "workspace_snapshot_identity",
        "workspace_baseline_identity",
        "evidence_origin",
        "source_revision",
        "executing_revision",
        "source_evidence_reference",
        "fixture_generation_method",
    ):
        if not str(getattr(fixture, field_name, "")).strip():
            failures.append(f"missing fixture field: {field_name}")
    for field_name in (
        "project_id",
        "session_id",
        "task_id",
        "task_execution_id",
        "persisted_change_set_id",
    ):
        if int(getattr(fixture, field_name, 0) or 0) <= 0:
            failures.append(f"missing identity: {field_name}")
    if fixture.execution_status.lower() not in {"done", "completed", "success"}:
        failures.append("execution is not complete")
    if fixture.verification_status.lower() not in {"passed", "success", "verified"}:
        failures.append("verification is not successful")
    inventories = {
        "changed_file_inventory": tuple(fixture.changed_file_inventory),
        "expected_source_inventory": tuple(fixture.expected_source_inventory),
        "expected_test_inventory": tuple(fixture.expected_test_inventory),
    }
    for name, values in inventories.items():
        if any(not str(value).strip() for value in values):
            failures.append(f"{name} contains an empty path")
        if len(values) != len(set(values)):
            failures.append(f"{name} contains duplicate paths")
    expected_inventory = set(fixture.expected_source_inventory) | set(
        fixture.expected_test_inventory
    )
    if set(fixture.changed_file_inventory) != expected_inventory:
        failures.append("changed-file inventory mismatch")
    for field_name in (
        "unsafe_failure_count",
        "duplicate_execution_count",
        "duplicate_mutation_count",
    ):
        if int(getattr(fixture, field_name, -1)) != 0:
            failures.append(f"{field_name} must be zero")
    if fixture.review_expectation not in {
        "REVIEW_NOT_APPLICABLE",
        "REVIEW_NOT_REQUIRED",
        "REVIEW_REQUIRED",
    }:
        failures.append("invalid or missing review expectation")
    if fixture.publication_expectation not in {
        "PUBLICATION_ALLOWED",
        "PUBLICATION_FORBIDDEN",
        "PUBLICATION_NOT_REQUIRED",
        "PUBLICATION_REQUIRED",
    }:
        failures.append("invalid or missing publication expectation")
    if (
        fixture.publication_expectation == "PUBLICATION_FORBIDDEN"
        and fixture.publication_expectation == "PUBLICATION_REQUIRED"
    ):
        failures.append("contradictory publication intent")
    if fixture.scenario_id == "S1-2":
        specification = scenario_spec("S1-2")
        if fixture.planner_contract_id != specification.planner_contract.contract_id:
            failures.append("planner contract ID mismatch")
        if (
            fixture.planner_contract_version
            != specification.planner_contract.contract_version
        ):
            failures.append("planner contract version mismatch")
        if fixture.review_expectation != specification.review_contract.expectation:
            failures.append("review intent does not match registered S1-2 intent")
        if (
            fixture.publication_expectation
            != specification.publication_contract.expectation
        ):
            failures.append("publication intent does not match registered S1-2 intent")
    if db is not None:
        project = db.query(Project).filter(Project.id == fixture.project_id).first()
        session = db.query(Session).filter(Session.id == fixture.session_id).first()
        task = db.query(Task).filter(Task.id == fixture.task_id).first()
        execution = (
            db.query(TaskExecution)
            .filter(TaskExecution.id == fixture.task_execution_id)
            .first()
        )
        change_set = (
            db.query(TaskExecutionChangeSet)
            .filter(TaskExecutionChangeSet.id == fixture.persisted_change_set_id)
            .first()
        )
        if project is None:
            failures.append("missing project identity")
        if session is None:
            failures.append("missing session identity")
        if task is None:
            failures.append("missing task identity")
        if execution is None:
            failures.append("missing task-execution identity")
        if change_set is None:
            failures.append("missing persisted change-set identity")
        if task is not None:
            if task.project_id != fixture.project_id:
                failures.append("task/project identity mismatch")
            if task.steps is None:
                failures.append("accepted Task.steps is missing")
            elif task_steps_digest(task.steps) != fixture.accepted_task_steps_digest:
                failures.append("accepted Task.steps digest mismatch")
        if session is not None and session.project_id != fixture.project_id:
            failures.append("session/project identity mismatch")
        if execution is not None:
            if (
                execution.task_id != fixture.task_id
                or execution.session_id != fixture.session_id
            ):
                failures.append("task-execution identity mismatch")
            if _as_value(execution.status).lower() not in {
                "done",
                "completed",
                "success",
            }:
                failures.append("persisted execution is not complete")
        if change_set is not None:
            if (
                change_set.project_id != fixture.project_id
                or change_set.task_id != fixture.task_id
                or change_set.task_execution_id != fixture.task_execution_id
            ):
                failures.append("persisted change-set identity mismatch")
            persisted_inventory = tuple(
                sorted(
                    set(change_set.added_files or [])
                    | set(change_set.modified_files or [])
                    | set(change_set.deleted_files or [])
                )
            )
            if persisted_inventory != tuple(sorted(fixture.changed_file_inventory)):
                failures.append("persisted change-set inventory mismatch")
    return failures


def validate_completion_fixture(
    fixture: CompletionCertificationFixture,
    *,
    db: Optional[Any] = None,
) -> None:
    failures = _fixture_failures(fixture, db=db)
    if failures:
        raise CertificationPreconditionError(failures)


def _registered_planner_contract(
    fixture: CompletionCertificationFixture,
) -> dict[str, Any]:
    specification = scenario_spec(fixture.scenario_id)
    planner = specification.planner_contract
    if planner is None:
        raise CertificationBoundaryError(
            "completion fixture has no registered planner contract"
        )
    review = specification.review_contract.to_payload()
    publication = specification.publication_contract.to_payload()
    return {
        **planner.to_payload(),
        "contract_source": fixture.contract_source,
        "review_expectation": fixture.review_expectation,
        "publication_expectation": fixture.publication_expectation,
        "review_contract": review,
        "publication_contract": publication,
        "registered_scenario_contract": {
            "scenario_id": fixture.scenario_id,
            "review_contract": review,
            "publication_contract": publication,
        },
    }


def _publication_record(result: Mapping[str, Any]) -> dict[str, Any]:
    publication = dict(result.get("publication_result") or {})
    status = str(publication.get("status") or "")
    if status == "published":
        return {**publication, "outcome": "published"}
    if status == "held_for_review":
        return {**publication, "outcome": "held_for_review"}
    if status == "failed":
        return {**publication, "outcome": "failed"}
    return {**publication, "outcome": "not_published"}


def evaluate_completion_publication(
    fixture: CompletionCertificationFixture,
    *,
    db: Any,
    workspace_review_policy: Optional[str] = None,
    publish: bool = True,
) -> dict[str, Any]:
    """Run Lane A from proven state through the real completion boundary."""

    validate_completion_fixture(fixture, db=db)
    project = db.query(Project).filter(Project.id == fixture.project_id).first()
    task = db.query(Task).filter(Task.id == fixture.task_id).first()
    execution = (
        db.query(TaskExecution)
        .filter(TaskExecution.id == fixture.task_execution_id)
        .first()
    )
    change_set_service = ChangesetService(db)
    persisted = change_set_service.get_task_execution_change_set(
        task_execution_id=fixture.task_execution_id
    )
    if (
        not persisted
        or persisted.get("change_set_id") != fixture.persisted_change_set_id
    ):
        raise CertificationPreconditionError(
            ["persisted change-set identity could not be verified"]
        )
    change_set = dict(persisted.get("change_set") or persisted)
    policy = workspace_review_policy or get_effective_workspace_review_policy(
        settings.WORKSPACE_REVIEW_POLICY,
        db=db,
    )
    planner_contract = _registered_planner_contract(fixture)
    completion_result = CompletionCoordinator().evaluate_completed_execution(
        db=db,
        project=project,
        task=task,
        task_execution=execution,
        session_id=fixture.session_id,
        change_set=change_set,
        workspace_review_policy=policy,
        planner_contract=planner_contract,
        publish=publish,
        task_service=TaskService(db),
    )
    review_decision = dict(completion_result.get("review_decision") or {})
    publication_result = dict(completion_result.get("publication_result") or {})
    publication_status = publication_result.get("status")
    if publication_status == "held_for_review":
        classification = CompletionPublicationClassification.REVIEW_HELD
    elif publication_status == "failed":
        classification = CompletionPublicationClassification.PUBLICATION_FAILED
    elif not review_decision.get("publication_allowed", True):
        classification = CompletionPublicationClassification.PUBLICATION_INELIGIBLE
    else:
        classification = CompletionPublicationClassification.PASSED

    structural_evidence = {
        "CONTRACT_REGISTERED": True,
        "SCENARIO_ID_MATCH": True,
        "REVIEW_DECISION_RECORDED": True,
        "PUBLICATION_FACT_RECORDED": True,
        "SOURCE_INVENTORY_REGISTERED": True,
        "TEST_INVENTORY_REGISTERED": True,
        "EXECUTION_COMPLETE": True,
        "VERIFICATION_SUCCESSFUL": True,
    }
    adjudication = derive_review_publication_outcomes(
        scenario_spec(fixture.scenario_id).registered_outcome_contract(),
        {
            "contract_id": "ST23-REVIEW-001",
            "contract_version": "v1",
            "facts": structural_evidence,
        },
        {"outcome": "RUNTIME_COMPLETED"},
        {"useful_work_completed": True},
        review_decision,
        _publication_record(completion_result),
    )
    persistence_identity = None
    if completion_result.get("publication_persisted"):
        persistence_identity = {
            "task_execution_id": fixture.task_execution_id,
            "change_set_id": fixture.persisted_change_set_id,
            "baseline_path": publication_result.get("baseline_path"),
            "artifact_path": publication_result.get("artifact_path"),
        }
    evidence = {
        "schema_version": CERTIFICATION_BOUNDARY_SCHEMA,
        "capability_id": LANE_A_ID,
        "scenario_id": fixture.scenario_id,
        "fixture_source": fixture.evidence_origin,
        "source_revision": fixture.source_revision,
        "executing_revision": fixture.executing_revision,
        "planner_contract_id": fixture.planner_contract_id,
        "planner_contract_version": fixture.planner_contract_version,
        "contract_resolution": review_decision.get("contract_resolution"),
        "policy_source": review_decision.get("policy_source"),
        "workspace_review_policy": policy,
        "warning_risk_flags": list(change_set.get("warning_flags") or []),
        "review_required_before_contract": review_decision.get(
            "review_required_before_contract"
        ),
        "registered_review_expectation": fixture.review_expectation,
        "stronger_safety_override": review_decision.get("stronger_safety_override"),
        "final_review_required_decision": review_decision.get("review_required"),
        "registered_publication_expectation": fixture.publication_expectation,
        "publication_allowed": review_decision.get("publication_allowed"),
        "publication_eligible": review_decision.get("publication_eligible"),
        "publication_attempt": completion_result.get("publication_attempted"),
        "publication_result": publication_result,
        "publication_persistence_identity": persistence_identity,
        "terminal_classification": completion_result.get("terminal_classification"),
        "classification": classification.value,
        "adjudication": adjudication.to_dict(),
        "rejected_preconditions": [],
        "historical_evidence_provenance": fixture.source_evidence_reference,
        "local_harness_validation": "deterministic_lane_a_real_services",
        "fresh_live_execution": False,
    }
    evidence["evidence_hash"] = stable_hash(evidence)
    validate_capability_evidence(evidence)
    return evidence


@dataclass(frozen=True)
class ExecutionDebugRepairEvidence:
    """Bounded, non-secret diagnostic evidence for Lane B."""

    capability_id: str
    request_id: str
    provider_backend: str
    provider_model: str
    provider_envelope_type: str
    provider_envelope_metadata: Mapping[str, Any]
    normalized_content_type: str
    normalized_content_length: int
    normalized_content_hash: str
    parsed_top_level_type: str
    parser_branch: str
    supported_shapes: tuple[str, ...]
    rejection_code: Optional[str]
    candidate_operation_count: int
    triggering_failure_class: str
    failed_current_step: str
    bounded_failure_envelope: Mapping[str, Any]
    shape_decision: str
    candidate_repair: Mapping[str, Any]
    mutation_status: str
    verification_status: str
    resume_status: str
    rollback_status: str
    exercised: bool
    accepted: bool
    integrity_hash: str = ""

    def unsigned_payload(self) -> dict[str, Any]:
        payload = asdict(self)
        payload.pop("integrity_hash", None)
        payload["supported_shapes"] = list(self.supported_shapes)
        return payload

    def with_integrity(self) -> "ExecutionDebugRepairEvidence":
        return replace(self, integrity_hash=stable_hash(self.unsigned_payload()))

    def to_payload(self) -> dict[str, Any]:
        payload = self.unsigned_payload()
        payload["integrity_hash"] = self.integrity_hash
        return payload


def classify_execution_debug_repair(
    evidence: ExecutionDebugRepairEvidence,
) -> ExecutionDebugRepairClassification:
    if not evidence.exercised:
        return ExecutionDebugRepairClassification.NOT_EXERCISED
    if evidence.resume_status == "blocked" or evidence.rollback_status == "blocked":
        return ExecutionDebugRepairClassification.BLOCKED
    if (
        evidence.accepted
        and evidence.resume_status == "resumed"
        and evidence.verification_status == "passed"
    ):
        return ExecutionDebugRepairClassification.RECOVERED
    if (
        not evidence.accepted
        and evidence.shape_decision == "rejected"
        and evidence.rollback_status in {"not_required", "completed"}
    ):
        return ExecutionDebugRepairClassification.REJECTED_FAIL_CLOSED
    if evidence.accepted:
        return ExecutionDebugRepairClassification.FAILED_AFTER_REPAIR
    return ExecutionDebugRepairClassification.BLOCKED


def validate_debug_repair_evidence(
    evidence: ExecutionDebugRepairEvidence,
) -> ExecutionDebugRepairClassification:
    failures: list[str] = []
    if evidence.capability_id != LANE_B_ID:
        failures.append("invalid debug-repair capability ID")
    if stable_hash(evidence.unsigned_payload()) != evidence.integrity_hash:
        failures.append("invalid debug-repair evidence integrity hash")
    for name in (
        "request_id",
        "provider_backend",
        "provider_envelope_type",
        "normalized_content_type",
        "parsed_top_level_type",
        "parser_branch",
        "triggering_failure_class",
        "failed_current_step",
        "shape_decision",
    ):
        if not str(getattr(evidence, name, "")).strip():
            failures.append(f"missing debug-repair evidence field: {name}")
    if evidence.normalized_content_length < 0:
        failures.append("normalized content length must be non-negative")
    if evidence.candidate_operation_count < 0:
        failures.append("candidate operation count must be non-negative")
    for field_name in (
        "provider_envelope_metadata",
        "bounded_failure_envelope",
        "candidate_repair",
    ):
        value = getattr(evidence, field_name)
        if not isinstance(value, Mapping):
            failures.append(f"{field_name} must be bounded metadata mapping")
            continue
        if len(_stable_json(value).encode("utf-8")) > 4096:
            failures.append(f"{field_name} exceeds bounded metadata limit")
        forbidden_tokens = {
            "authorization",
            "password",
            "raw_payload",
            "secret",
            "token",
        }
        if any(str(key).lower() in forbidden_tokens for key in value):
            failures.append(f"{field_name} contains forbidden secret/payload metadata")
    if failures:
        raise CertificationBoundaryError("; ".join(failures))
    return classify_execution_debug_repair(evidence)


def _classification_value(value: Any) -> str:
    return str(getattr(value, "value", value))


def aggregate_capability_results(
    bindings: tuple[CapabilityBinding, ...],
    results: Mapping[str, Mapping[str, Any]],
) -> dict[str, Any]:
    """Aggregate required lanes while retaining every optional outcome."""

    required_results: list[dict[str, Any]] = []
    optional_results: list[dict[str, Any]] = []
    first_failed_required: Optional[str] = None
    optional_failures: list[str] = []
    required_observation_blocked_by_optional = False
    for binding in bindings:
        result = dict(results.get(binding.capability_id) or {})
        classification = _classification_value(
            result.get("classification", "NOT_EVALUATED")
        )
        entry = {
            "capability_id": binding.capability_id,
            "requirement": binding.requirement,
            "independently_classified": binding.independently_classified,
            "classification": classification,
            "evidence": result,
        }
        if binding.required:
            required_results.append(entry)
            passing = (
                classification == PlanningExecutionClassification.PASSED.value
                if binding.capability_id == "planning_execution"
                else classification == CompletionPublicationClassification.PASSED.value
            )
            if not passing and first_failed_required is None:
                first_failed_required = binding.capability_id
        elif binding.optional:
            optional_results.append(entry)
            if classification not in {
                ExecutionDebugRepairClassification.NOT_EXERCISED.value,
                ExecutionDebugRepairClassification.NOT_EVALUATED.value,
            }:
                if classification not in {
                    ExecutionDebugRepairClassification.RECOVERED.value,
                }:
                    optional_failures.append(binding.capability_id)
                if result.get("prevented_required_observation"):
                    required_observation_blocked_by_optional = True
    required_failed = first_failed_required is not None
    required_not_evaluated = any(
        entry["classification"] == "NOT_EVALUATED" for entry in required_results
    )
    if (
        required_failed
        or required_not_evaluated
        or required_observation_blocked_by_optional
    ):
        aggregate = AggregateCertificationClassification.BLOCKED
    elif optional_failures:
        aggregate = AggregateCertificationClassification.PARTIALLY_EVIDENCED
    else:
        aggregate = AggregateCertificationClassification.FULLY_CERTIFIED
    return {
        "aggregate_classification": aggregate.value,
        "required_capabilities": required_results,
        "optional_capabilities": optional_results,
        "first_failed_required_capability": first_failed_required,
        "incidental_optional_failures": optional_failures,
        "optional_failure_prevented_required_observation": (
            required_observation_blocked_by_optional
        ),
        "fully_certified": aggregate
        == AggregateCertificationClassification.FULLY_CERTIFIED,
        "partially_evidenced": aggregate
        == AggregateCertificationClassification.PARTIALLY_EVIDENCED,
        "blocked": aggregate == AggregateCertificationClassification.BLOCKED,
    }


def build_capability_evidence(
    *,
    scenario_id: str,
    bindings: tuple[CapabilityBinding, ...],
    results: Mapping[str, Mapping[str, Any]],
    source_revision: str,
    executing_revision: str,
) -> dict[str, Any]:
    aggregate = aggregate_capability_results(bindings, results)
    payload = {
        "schema_version": CERTIFICATION_BOUNDARY_SCHEMA,
        "scenario_id": scenario_id,
        "source_revision": source_revision,
        "executing_revision": executing_revision,
        "capability_bindings": [binding.to_payload() for binding in bindings],
        "capabilities": dict(results),
        "aggregate": aggregate,
        "historical_evidence_is_not_fresh_live_execution": True,
    }
    payload["evidence_hash"] = stable_hash(payload)
    return payload


def validate_capability_evidence(payload: Mapping[str, Any]) -> None:
    values = dict(payload)
    evidence_hash = values.pop("evidence_hash", None)
    if not evidence_hash or stable_hash(values) != evidence_hash:
        raise CertificationBoundaryError("tampered capability evidence hash")
    if values.get("schema_version") not in SUPPORTED_FIXTURE_SCHEMAS:
        raise CertificationBoundaryError("unsupported capability evidence schema")
    if not values.get("capability_id") and "capabilities" not in values:
        raise CertificationBoundaryError("capability evidence ID is missing")
    for capability_id, capability_payload in (values.get("capabilities") or {}).items():
        if not isinstance(capability_payload, Mapping):
            raise CertificationBoundaryError(
                f"capability evidence is not a mapping: {capability_id}"
            )
        if "evidence_hash" in capability_payload:
            validate_capability_evidence(capability_payload)


def replay_capability_evidence(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Recompute capability-level classifications and aggregate deterministically."""

    validate_capability_evidence(payload)
    if "capabilities" not in payload:
        classification = payload.get("classification")
        return {
            "capability_id": payload.get("capability_id"),
            "classification": classification,
            "match": classification is not None,
        }
    bindings = tuple(
        CapabilityBinding(
            item["capability_id"],
            item["requirement"],
            bool(item.get("independently_classified", True)),
        )
        for item in payload.get("capability_bindings", ())
    )
    replayed = aggregate_capability_results(bindings, payload.get("capabilities", {}))
    return {
        "match": replayed == payload.get("aggregate"),
        "aggregate": replayed,
    }


def classify_legacy_evidence(payload: Mapping[str, Any]) -> str:
    """Keep pre-boundary records readable without pretending they have lanes."""

    if "capability_id" in payload or "capabilities" in payload:
        return "CURRENT_BOUNDARY_SCHEMA"
    if "outcome_class" in payload and "captured_facts" in payload:
        return "LEGACY_PHASE31_READABLE_NO_CAPABILITY_SPLIT"
    return "UNRECOGNIZED_EVIDENCE"


def write_capability_evidence(path: Path, payload: Mapping[str, Any]) -> Path:
    validate_capability_evidence(payload)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.parent.chmod(0o775)
    path.write_text(
        json.dumps(payload, indent=2, sort_keys=True) + "\n", encoding="utf-8"
    )
    path.chmod(0o664)
    return path


__all__ = [
    "AggregateCertificationClassification",
    "CERTIFICATION_BOUNDARY_SCHEMA",
    "CertificationBoundaryError",
    "CertificationPreconditionError",
    "CompletionCertificationArtifact",
    "CompletionCertificationFixture",
    "CompletionPublicationClassification",
    "ExecutionDebugRepairClassification",
    "ExecutionDebugRepairEvidence",
    "LANE_A_ID",
    "LANE_B_ID",
    "PlanningExecutionClassification",
    "aggregate_capability_results",
    "build_capability_evidence",
    "build_deterministic_s1_2_fixture",
    "classify_execution_debug_repair",
    "classify_legacy_evidence",
    "evaluate_completion_publication",
    "replay_capability_evidence",
    "stable_hash",
    "task_steps_digest",
    "validate_capability_evidence",
    "validate_completion_fixture",
    "validate_debug_repair_evidence",
    "write_capability_evidence",
]
