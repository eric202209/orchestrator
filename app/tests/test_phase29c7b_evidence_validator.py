"""Focused Phase 29C-7B evidence and deterministic-validator tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
from types import MappingProxyType

import hashlib
import pytest
from sqlalchemy import create_engine, inspect

from app.models import (
    ExecutionPlan,
    ExecutionTaskResolvedValidationEvidence,
    ExecutionTaskTransition,
    ExecutionTaskValidationPredicateResult,
    ExecutionTaskValidationSpecification,
    Base,
    TaskExecution,
)
from app.services.execution.candidate_evidence import (
    CandidateEvidenceError,
    CandidateEvidenceResolverService,
    CandidatePredicateResult,
    DeterministicValidatorRegistry,
    DeterministicValidatorService,
    EvaluateCandidatePredicateCommand,
    ResolveCandidateEvidenceCommand,
    ResolvedCandidateEvidence,
    ValidationPrimitiveService,
    ValidatorExecutionContext,
    build_default_validator_registry,
    normalize_evidence_reference,
    parse_evidence_reference,
)
from app.db_migrations import _migration_039_execution_task_validation_primitives
from app.services.execution.validation_contract import ValidationContractService
from app.services.planning.structured_task_plan import Task
from app.services.planning.validation_contract import (
    StructuredValidationContract,
    VALIDATION_CONTRACT_SCHEMA_VERSION,
    VALIDATION_ENVIRONMENT_SCHEMA_VERSION,
)

from test_phase29c6b_runtime_evidence import (
    _owned,
    _record_command,
    _start_command,
)
from test_phase29c7a_validation_contract import _pass_policy


ENVIRONMENT_HASH = "a" * 64
OUTPUT_HASH = hashlib.sha256(b"candidate").hexdigest()


def _contract(predicate_id: str = "output_reference_exists"):
    evidence_type = (
        "candidate_output_hash"
        if predicate_id == "output_hash_matches"
        else "candidate_output_reference"
    )
    return StructuredValidationContract.from_mapping(
        {
            "schema_version": VALIDATION_CONTRACT_SCHEMA_VERSION,
            "status": "structured_executable",
            "predicates": [
                {
                    "predicate_id": predicate_id,
                    "predicate_version": 1,
                    "evidence_key": "primary_output",
                    "parameters": {},
                    "required": True,
                    "order": 10,
                }
            ],
            "evidence_descriptors": [
                {
                    "evidence_key": "primary_output",
                    "evidence_type": evidence_type,
                    "source": "candidate_outcome",
                    "required": True,
                    "expected_media_type": None,
                    "expected_hash_algorithm": (
                        "sha256" if evidence_type == "candidate_output_hash" else None
                    ),
                    "resolver_version": "candidate-evidence-resolver/1",
                }
            ],
            "pass_policy": _pass_policy(),
            "review_requirement": {"requirement": "none", "requirement_version": 1},
            "environment": {
                "schema_version": VALIDATION_ENVIRONMENT_SCHEMA_VERSION,
                "validator_set_id": "deterministic_readonly",
                "validator_set_version": "1",
                "configuration_hash": ENVIRONMENT_HASH,
                "resolver_version": "candidate-evidence-resolver/1",
                "toolchain_identity": "test-toolchain",
                "timezone": "UTC",
                "locale": "C",
            },
            "specification_source": "authored",
        }
    )


def _structured_runtime(db_session, *, predicate_id="output_reference_exists"):
    task, created, ownership, _ = _owned(db_session)
    from app.services.execution.execution_task_runtime_execution_service import (
        ExecutionTaskRuntimeExecutionService,
    )

    start = ExecutionTaskRuntimeExecutionService(
        db_session
    ).mark_runtime_execution_started(_start_command(task, created, ownership))
    db_session.commit()
    outcome = ExecutionTaskRuntimeExecutionService(
        db_session
    ).record_runtime_attempt_outcome(
        _record_command(
            task,
            created,
            ownership,
            start.start,
            status="candidate_completed",
            key=f"candidate-evidence-{task.id}",
            output_hash=OUTPUT_HASH,
        )
    )
    db_session.commit()

    # The real release boundary freezes task_spec before runtime ownership.
    # This fixture starts from the existing C6B legacy fixture, records its
    # candidate outcome, then projects the same authored task into a structured
    # specification for primitive-only tests.  The runtime row itself remains
    # unchanged; resolver authority still verifies its canonical evidence.
    authored = Task(**task.task_spec)
    authored = replace(
        authored,
        work_items=tuple(
            replace(item, validation_contract=_contract(predicate_id))
            for item in authored.work_items
        ),
    )
    projection = ValidationContractService.projection_for_task(authored)
    specification = db_session.get(
        ExecutionTaskValidationSpecification, task.validation_contract_id
    )
    assert specification is not None
    structured = projection.canonical_payload["structured_contract"]
    specification.contract_status = projection.contract_status
    specification.schema_version = projection.canonical_payload["schema_version"]
    specification.original_done_when = list(projection.original_done_when)
    specification.structured_contract = structured
    specification.pass_policy = structured["pass_policy"]
    specification.review_requirement = structured["review_requirement"]
    specification.environment_identity = structured["environment"]
    specification.validator_set_identity = structured["environment"]["validator_set_id"]
    specification.canonical_payload = projection.canonical_payload
    specification.canonical_specification_hash = projection.canonical_hash
    task.task_spec = authored.to_dict()
    task.done_when = [item.done_when for item in authored.work_items]
    task.validation_contract_status = "structured_executable"
    task.validation_contract_id = specification.id
    db_session.flush()
    return task, created, outcome.outcome, specification


def _resolve_command(task, outcome, specification, *, key="resolve-1", **overrides):
    values = {
        "execution_plan_id": task.execution_plan_id,
        "execution_task_id": task.id,
        "execution_task_attempt_id": outcome.execution_task_attempt_id,
        "candidate_outcome_id": outcome.id,
        "validation_specification_id": specification.id,
        "validation_specification_hash": specification.canonical_specification_hash,
        "evidence_key": "primary_output",
        "evidence_type": "candidate_output_reference",
        "evidence_source": "candidate_outcome",
        "expected_reference": f"candidate-output://{outcome.id}",
        "expected_output_reference": "runtime://test-output",
        "expected_content_hash": None,
        "expected_hash_algorithm": None,
        "resolver_version": "candidate-evidence-resolver/1",
        "environment_configuration_hash": ENVIRONMENT_HASH,
        "resolution_idempotency_key": key,
        "deterministic_resolution_command_id": f"resolve-command-{key}",
    }
    values.update(overrides)
    return ResolveCandidateEvidenceCommand(**values)


def _predicate_command(
    task, outcome, specification, evidence, *, key="predicate-1", **overrides
):
    values = {
        "execution_plan_id": task.execution_plan_id,
        "execution_task_id": task.id,
        "execution_task_attempt_id": outcome.execution_task_attempt_id,
        "candidate_outcome_id": outcome.id,
        "validation_specification_id": specification.id,
        "validation_specification_hash": specification.canonical_specification_hash,
        "predicate_id": "output_reference_exists",
        "predicate_version": 1,
        "predicate_order": 10,
        "evidence_snapshot_id": evidence.id,
        "evidence_key": "primary_output",
        "validator_id": "output_reference_exists",
        "validator_version": 1,
        "validator_set_id": "deterministic_readonly",
        "validator_set_version": "1",
        "environment_configuration_hash": ENVIRONMENT_HASH,
        "validator_idempotency_key": key,
        "deterministic_validator_command_id": f"validator-command-{key}",
    }
    values.update(overrides)
    return EvaluateCandidatePredicateCommand(**values)


@pytest.mark.parametrize(
    "reference",
    [
        "relative/path",
        "/absolute/path",
        "file:///tmp/x",
        "https://example.test/x",
        "candidate-output://1/../2",
        "candidate-output://1?secret=x",
        "candidate-output://user@1",
        "candidate-output://0001",
    ],
)
def test_reference_grammar_rejects_unsafe_references(reference):
    with pytest.raises(CandidateEvidenceError):
        parse_evidence_reference(reference)


def test_reference_grammar_is_versioned_and_normalized():
    parsed = parse_evidence_reference("CANDIDATE-OUTPUT://42")
    assert parsed.grammar_version == "candidate-evidence-reference/1"
    assert parsed.normalized == "candidate-output://42"
    assert normalize_evidence_reference(parsed.normalized) == parsed.normalized


def test_reference_grammar_rejects_unknown_scheme_and_oversized_reference():
    with pytest.raises(CandidateEvidenceError) as unsupported:
        parse_evidence_reference("artifact://1")
    assert unsupported.value.code == "candidate_evidence_scheme_unsupported"
    with pytest.raises(CandidateEvidenceError) as oversized:
        parse_evidence_reference("candidate-output://" + "1" * 300)
    assert oversized.value.code == "candidate_evidence_reference_invalid"


def _evidence(
    *,
    status="resolved",
    expected_hash=OUTPUT_HASH,
    actual_hash=OUTPUT_HASH,
    content=None,
):
    return ResolvedCandidateEvidence(
        id=1,
        execution_plan_id=1,
        execution_task_id=1,
        execution_task_attempt_id=1,
        candidate_outcome_id=1,
        validation_specification_id=1,
        validation_specification_hash="b" * 64,
        evidence_key="primary_output",
        evidence_type="candidate_output_reference",
        source="candidate_outcome",
        normalized_reference="candidate-output://1",
        source_authority_id="execution-task-attempt-outcome:1",
        resolver_id="sql-candidate-outcome",
        resolver_version="candidate-evidence-resolver/1",
        resolver_contract_version="candidate-evidence-resolver/1",
        environment_configuration_hash=ENVIRONMENT_HASH,
        expected_hash_algorithm="sha256",
        expected_hash=expected_hash,
        actual_hash=actual_hash,
        media_type=None,
        byte_size=None,
        structured_metadata_summary=MappingProxyType({}),
        content_projection=content,
        resolution_status=status,
        canonical_evidence_payload_hash="c" * 64,
        resolved_at=datetime.now(timezone.utc),
    )


def test_registry_is_bounded_and_deterministic():
    registry = build_default_validator_registry(configuration_hash=ENVIRONMENT_HASH)
    context = ValidatorExecutionContext(
        1,
        "b" * 64,
        "deterministic_readonly",
        "1",
        ENVIRONMENT_HASH,
        "candidate-evidence-resolver/1",
    )
    predicate = __import__(
        "app.services.planning.validation_contract",
        fromlist=["ValidationPredicate"],
    ).ValidationPredicate("output_reference_exists", 1, "primary_output", {}, True, 10)
    first = registry.resolve(predicate, context)
    second = registry.resolve(predicate, context)
    assert first.validator_id == second.validator_id == "output_reference_exists"
    with pytest.raises(CandidateEvidenceError):
        registry.register(
            predicate_id="output_reference_exists",
            predicate_version=1,
            validator_id="duplicate",
            validator_version=1,
            validator=first.validator,
        )


def test_registry_rejects_environment_and_unregistered_predicates():
    registry = build_default_validator_registry(configuration_hash=ENVIRONMENT_HASH)
    context = ValidatorExecutionContext(
        1,
        "b" * 64,
        "deterministic_readonly",
        "1",
        "f" * 64,
        "candidate-evidence-resolver/1",
    )
    from app.services.planning.validation_contract import ValidationPredicate

    predicate = ValidationPredicate(
        "json_schema_matches",
        1,
        "primary_output",
        {"schema_evidence_key": "schema"},
        True,
        10,
    )
    with pytest.raises(CandidateEvidenceError) as environment:
        registry.resolve(predicate, context)
    assert environment.value.code == "validation_environment_mismatch"
    matching = replace(context, environment_configuration_hash=ENVIRONMENT_HASH)
    with pytest.raises(CandidateEvidenceError) as unsupported:
        registry.resolve(predicate, matching)
    assert unsupported.value.code == "validation_validator_not_registered"


def test_pure_validators_distinguish_pass_failure_and_evidence_errors():
    registry = build_default_validator_registry(configuration_hash=ENVIRONMENT_HASH)
    context = ValidatorExecutionContext(
        1,
        "b" * 64,
        "deterministic_readonly",
        "1",
        ENVIRONMENT_HASH,
        "candidate-evidence-resolver/1",
    )
    reference = __import__(
        "app.services.planning.validation_contract",
        fromlist=["ValidationPredicate"],
    ).ValidationPredicate("output_reference_exists", 1, "primary_output", {}, True, 10)
    registration = registry.resolve(reference, context)
    passed = registration.validator.validate(reference, _evidence(), context)
    failed = registration.validator.validate(
        reference, _evidence(status="missing"), context
    )
    assert passed.result_status == "passed" and passed.passed is True
    assert failed.result_status == "missing_evidence" and failed.passed is False


def test_output_hash_validator_passes_and_fails_without_aggregation():
    registry = build_default_validator_registry(configuration_hash=ENVIRONMENT_HASH)
    context = ValidatorExecutionContext(
        1,
        "b" * 64,
        "deterministic_readonly",
        "1",
        ENVIRONMENT_HASH,
        "candidate-evidence-resolver/1",
    )
    from app.services.planning.validation_contract import ValidationPredicate

    predicate = ValidationPredicate(
        "output_hash_matches", 1, "primary_output", {}, True, 10
    )
    validator = registry.resolve(predicate, context).validator
    assert validator.validate(predicate, _evidence(), context).result_status == "passed"
    assert (
        validator.validate(
            predicate, _evidence(actual_hash="d" * 64), context
        ).result_status
        == "failed"
    )


def test_migration_039_is_replay_safe_and_creates_exact_tables(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'fresh.db'}")
    Base.metadata.create_all(bind=engine)
    _migration_039_execution_task_validation_primitives(engine)
    _migration_039_execution_task_validation_primitives(engine)
    inspector = inspect(engine)
    assert inspector.has_table("execution_task_resolved_validation_evidence")
    assert inspector.has_table("execution_task_validation_predicate_results")
    assert "uq_execution_task_resolved_evidence_candidate_spec_key" in {
        item["name"]
        for item in inspector.get_unique_constraints(
            "execution_task_resolved_validation_evidence"
        )
    }
    assert "uq_execution_task_validation_result_candidate_spec_predicate" in {
        item["name"]
        for item in inspector.get_unique_constraints(
            "execution_task_validation_predicate_results"
        )
    }


def test_required_fields_validator_requires_bounded_structured_projection():
    registry = build_default_validator_registry(configuration_hash=ENVIRONMENT_HASH)
    context = ValidatorExecutionContext(
        1,
        "b" * 64,
        "deterministic_readonly",
        "1",
        ENVIRONMENT_HASH,
        "candidate-evidence-resolver/1",
    )
    predicate = __import__(
        "app.services.planning.validation_contract",
        fromlist=["ValidationPredicate"],
    ).ValidationPredicate(
        "required_fields_present",
        1,
        "primary_output",
        {"fields": ["result.id"]},
        True,
        10,
    )
    registration = registry.resolve(predicate, context)
    passed = registration.validator.validate(
        predicate,
        _evidence(content=MappingProxyType({"result": MappingProxyType({"id": 7})})),
        context,
    )
    invalid = registration.validator.validate(predicate, _evidence(), context)
    assert passed.result_status == "passed"
    assert invalid.result_status == "invalid_evidence"


def test_structured_runtime_resolves_metadata_only_and_replays_without_reading_source(
    db_session,
):
    task, _created, outcome, specification = _structured_runtime(db_session)
    service = CandidateEvidenceResolverService(db_session)
    first = service.resolve(_resolve_command(task, outcome, specification))
    db_session.commit()
    assert first.evidence.resolution_status == "resolved"
    assert first.evidence.content_projection is None
    assert first.evidence.byte_size is None
    assert db_session.query(ExecutionTaskResolvedValidationEvidence).count() == 1

    class MustNotRead:
        def read(self, candidate_outcome_id):
            raise AssertionError("replay reread the source")

    replay = CandidateEvidenceResolverService(db_session, source=MustNotRead()).resolve(
        _resolve_command(task, outcome, specification)
    )
    assert replay.replayed is True
    assert replay.evidence.id == first.evidence.id
    assert replay.evidence.resolved_at == first.evidence.resolved_at


def test_resolution_conflicts_and_hash_mismatch_are_bounded(db_session):
    task, _created, outcome, specification = _structured_runtime(db_session)
    service = CandidateEvidenceResolverService(db_session)
    service.resolve(_resolve_command(task, outcome, specification))
    with pytest.raises(CandidateEvidenceError) as same_key:
        service.resolve(
            _resolve_command(
                task,
                outcome,
                specification,
                expected_output_reference="runtime://changed",
            )
        )
    assert same_key.value.code == "candidate_evidence_idempotency_conflict"
    with pytest.raises(CandidateEvidenceError) as other_key:
        service.resolve(_resolve_command(task, outcome, specification, key="resolve-2"))
    assert other_key.value.code == "candidate_evidence_resolution_conflict"


def test_resolution_hash_mismatch_is_persisted_deterministically(db_session):
    task, _created, outcome, specification = _structured_runtime(db_session)
    mismatch = CandidateEvidenceResolverService(db_session).resolve(
        _resolve_command(task, outcome, specification, expected_content_hash="d" * 64)
    )
    db_session.commit()
    assert mismatch.evidence.resolution_status == "hash_mismatch"


def test_validator_result_is_idempotent_and_does_not_change_lifecycle(db_session):
    task, _created, outcome, specification = _structured_runtime(db_session)
    evidence = (
        CandidateEvidenceResolverService(db_session)
        .resolve(_resolve_command(task, outcome, specification))
        .evidence
    )
    db_session.commit()
    before = (
        task.status,
        task.state_version,
        db_session.query(ExecutionTaskTransition).count(),
        db_session.query(TaskExecution).count(),
    )
    registry = build_default_validator_registry(configuration_hash=ENVIRONMENT_HASH)
    service = DeterministicValidatorService(db_session, registry=registry)
    first = service.validate(_predicate_command(task, outcome, specification, evidence))
    db_session.commit()
    replay = service.validate(
        _predicate_command(task, outcome, specification, evidence)
    )
    assert replay.replayed is True
    assert first.result.id == replay.result.id
    assert first.result.canonical_result_hash == replay.result.canonical_result_hash
    assert (
        task.status,
        task.state_version,
        db_session.query(ExecutionTaskTransition).count(),
        db_session.query(TaskExecution).count(),
    ) == before
    assert db_session.query(ExecutionTaskValidationPredicateResult).count() == 1


def test_validator_result_changed_key_and_integrity_tampering_are_detected(db_session):
    task, _created, outcome, specification = _structured_runtime(db_session)
    evidence = (
        CandidateEvidenceResolverService(db_session)
        .resolve(_resolve_command(task, outcome, specification))
        .evidence
    )
    db_session.commit()
    registry = build_default_validator_registry(configuration_hash=ENVIRONMENT_HASH)
    service = DeterministicValidatorService(db_session, registry=registry)
    first = service.validate(_predicate_command(task, outcome, specification, evidence))
    db_session.commit()
    with pytest.raises(CandidateEvidenceError) as duplicate:
        service.validate(
            _predicate_command(
                task, outcome, specification, evidence, key="predicate-2"
            )
        )
    assert duplicate.value.code == "validation_predicate_result_conflict"
    integrity = ValidationPrimitiveService(
        db_session, registry=registry
    ).verify_validation_predicate_result_integrity(first.result.id)
    assert integrity.verified is True
    first.result.canonical_result_hash = "f" * 64
    db_session.commit()
    tampered = ValidationPrimitiveService(
        db_session, registry=registry
    ).verify_validation_predicate_result_integrity(first.result.id)
    assert tampered.verified is False
    assert "validation_predicate_result_hash_mismatch" in tampered.issues
