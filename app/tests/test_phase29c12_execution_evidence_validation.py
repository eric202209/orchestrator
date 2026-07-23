"""Focused Phase 29C-12 execution-evidence validation boundary tests."""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import create_engine, inspect

from app.models import (
    Base,
    ExecutionEvidence,
    ExecutionTaskValidationSpecification,
)
from app.db_migrations import MIGRATIONS, run_schema_migrations
from app.services.execution.candidate_evidence import (
    CandidateEvidenceResolverService,
    DeterministicValidatorService,
    EvaluateCandidatePredicateCommand,
    ResolveCandidateEvidenceCommand,
    ValidationPrimitiveService,
    build_default_validator_registry,
    parse_evidence_reference,
)
from app.services.execution.execution_evidence import (
    EVIDENCE_KIND_PRODUCERS,
    ExecutionEvidenceIngestionService,
    IngestExecutionEvidenceCommand,
)
from app.services.execution.validation_contract import ValidationContractService
from app.services.planning.structured_task_plan import Task
from app.services.planning.validation_contract import (
    StructuredValidationContract,
    ValidationContractError,
    ValidationEvidenceDescriptor,
    VALIDATION_CONTRACT_SCHEMA_VERSION,
    VALIDATION_ENVIRONMENT_SCHEMA_VERSION,
    canonical_validation_hash,
)

from test_phase29c6b_runtime_evidence import _owned, _record_command, _start_command
from test_phase29c7a_validation_contract import _pass_policy

ENVIRONMENT_HASH = "a" * 64


def _execution_evidence_contract(
    predicate_id: str,
    *,
    expected_kind: str = "test",
    expected_media_type: str | None = None,
):
    expected_producer = EVIDENCE_KIND_PRODUCERS[expected_kind]
    return StructuredValidationContract.from_mapping(
        {
            "schema_version": VALIDATION_CONTRACT_SCHEMA_VERSION,
            "status": "structured_executable",
            "predicates": [
                {
                    "predicate_id": predicate_id,
                    "predicate_version": 1,
                    "evidence_key": "exec_evidence",
                    "parameters": {},
                    "required": True,
                    "order": 10,
                }
            ],
            "evidence_descriptors": [
                {
                    "evidence_key": "exec_evidence",
                    "evidence_type": "execution_evidence",
                    "source": "execution_evidence",
                    "required": True,
                    "expected_media_type": expected_media_type,
                    "expected_hash_algorithm": None,
                    "resolver_version": "candidate-evidence-resolver/1",
                    "expected_evidence_kind": expected_kind,
                    "expected_producer": expected_producer,
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


def _structured_runtime_with_evidence(
    db_session,
    *,
    predicate_id: str,
    expected_kind: str = "test",
    expected_media_type: str | None = None,
    ingest_kind: str | None = None,
    ingest_content: bytes = b'{"ok": true}',
    ingest_key: str = "exec-evidence-1",
    ingest: bool = True,
):
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
        )
    )
    db_session.commit()

    authored = Task(**task.task_spec)
    authored = replace(
        authored,
        work_items=tuple(
            replace(
                item,
                validation_contract=_execution_evidence_contract(
                    predicate_id,
                    expected_kind=expected_kind,
                    expected_media_type=expected_media_type,
                ),
            )
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

    evidence_row = None
    if ingest:
        kind = ingest_kind or expected_kind
        result = ExecutionEvidenceIngestionService(db_session).ingest(
            IngestExecutionEvidenceCommand(
                execution_plan_id=task.execution_plan_id,
                execution_task_id=task.id,
                execution_task_attempt_id=created.attempt.id,
                attempt_generation=created.attempt.attempt_generation,
                evidence_kind=kind,
                producer_id=EVIDENCE_KIND_PRODUCERS[kind],
                producer_version="1",
                content=ingest_content,
                media_type="application/json",
                ingestion_idempotency_key=ingest_key,
                creation_actor_id="test-producer",
            )
        )
        db_session.commit()
        evidence_row = result.evidence

    return task, created, outcome.outcome, specification, evidence_row


def _resolve_command(
    task, outcome, specification, evidence_row, *, key="resolve-1", **overrides
):
    reference = (
        f"execution-evidence://{evidence_row.id}"
        if evidence_row is not None
        else "execution-evidence://999999"
    )
    values = {
        "execution_plan_id": task.execution_plan_id,
        "execution_task_id": task.id,
        "execution_task_attempt_id": outcome.execution_task_attempt_id,
        "candidate_outcome_id": outcome.id,
        "validation_specification_id": specification.id,
        "validation_specification_hash": specification.canonical_specification_hash,
        "evidence_key": "exec_evidence",
        "evidence_type": "execution_evidence",
        "evidence_source": "execution_evidence",
        "expected_reference": reference,
        "expected_output_reference": None,
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
    task,
    outcome,
    specification,
    evidence,
    predicate_id,
    *,
    key="predicate-1",
    **overrides,
):
    values = {
        "execution_plan_id": task.execution_plan_id,
        "execution_task_id": task.id,
        "execution_task_attempt_id": outcome.execution_task_attempt_id,
        "candidate_outcome_id": outcome.id,
        "validation_specification_id": specification.id,
        "validation_specification_hash": specification.canonical_specification_hash,
        "predicate_id": predicate_id,
        "predicate_version": 1,
        "predicate_order": 10,
        "evidence_snapshot_id": evidence.id,
        "evidence_key": "exec_evidence",
        "validator_id": predicate_id,
        "validator_version": 1,
        "validator_set_id": "deterministic_readonly",
        "validator_set_version": "1",
        "environment_configuration_hash": ENVIRONMENT_HASH,
        "validator_idempotency_key": key,
        "deterministic_validator_command_id": f"validator-command-{key}",
    }
    values.update(overrides)
    return EvaluateCandidatePredicateCommand(**values)


# ---------------------------------------------------------------------------
# Contract binding, hashing, and historical compatibility
# ---------------------------------------------------------------------------


def test_descriptor_requires_matching_producer_for_kind():
    with pytest.raises(ValidationContractError) as excinfo:
        ValidationEvidenceDescriptor(
            evidence_key="exec_evidence",
            evidence_type="execution_evidence",
            source="execution_evidence",
            expected_evidence_kind="test",
            expected_producer="runtime",
        )
    assert excinfo.value.code == "validation_evidence_descriptor_invalid"


def test_descriptor_forbids_kind_binding_outside_execution_evidence_source():
    with pytest.raises(ValidationContractError):
        ValidationEvidenceDescriptor(
            evidence_key="primary_output",
            evidence_type="candidate_output_reference",
            source="candidate_outcome",
            expected_evidence_kind="test",
            expected_producer="test-runner",
        )


def test_media_type_predicate_requires_expected_media_type():
    with pytest.raises(ValidationContractError):
        _execution_evidence_contract(
            "execution_evidence_media_type_matches", expected_media_type=None
        )


def test_execution_evidence_predicate_requires_execution_evidence_descriptor():
    with pytest.raises(ValidationContractError):
        StructuredValidationContract.from_mapping(
            {
                "schema_version": VALIDATION_CONTRACT_SCHEMA_VERSION,
                "status": "structured_executable",
                "predicates": [
                    {
                        "predicate_id": "execution_evidence_exists",
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
                        "evidence_type": "candidate_output_reference",
                        "source": "candidate_outcome",
                        "required": True,
                        "expected_media_type": None,
                        "expected_hash_algorithm": None,
                        "resolver_version": "candidate-evidence-resolver/1",
                    }
                ],
                "pass_policy": _pass_policy(),
                "review_requirement": {
                    "requirement": "none",
                    "requirement_version": 1,
                },
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


def test_contract_hash_includes_evidence_authority_parameters():
    test_contract = _execution_evidence_contract(
        "execution_evidence_kind_matches", expected_kind="test"
    )
    lint_contract = _execution_evidence_contract(
        "execution_evidence_kind_matches", expected_kind="lint"
    )
    assert canonical_validation_hash(
        test_contract.to_dict()
    ) != canonical_validation_hash(lint_contract.to_dict())


def test_historical_descriptor_payload_without_new_fields_still_parses():
    legacy_payload = {
        "evidence_key": "primary_output",
        "evidence_type": "candidate_output_reference",
        "source": "candidate_outcome",
        "required": True,
        "expected_media_type": None,
        "expected_hash_algorithm": None,
        "resolver_version": "candidate-evidence-resolver/1",
    }
    descriptor = ValidationEvidenceDescriptor.from_mapping(legacy_payload)
    assert descriptor.expected_evidence_kind is None
    assert descriptor.expected_producer is None


# ---------------------------------------------------------------------------
# Resolution and predicate classification
# ---------------------------------------------------------------------------


def test_execution_evidence_exists_passes_when_evidence_resolved(db_session):
    task, created, outcome, specification, evidence_row = (
        _structured_runtime_with_evidence(
            db_session, predicate_id="execution_evidence_exists"
        )
    )
    resolved = CandidateEvidenceResolverService(db_session).resolve(
        _resolve_command(task, outcome, specification, evidence_row)
    )
    db_session.commit()
    assert resolved.evidence.resolution_status == "resolved"
    integrity = ValidationPrimitiveService(
        db_session
    ).verify_resolved_validation_evidence_integrity(resolved.evidence.id)
    assert integrity.verified, integrity.issues

    result = DeterministicValidatorService(db_session).validate(
        _predicate_command(
            task,
            outcome,
            specification,
            resolved.evidence,
            "execution_evidence_exists",
        )
    )
    db_session.commit()
    assert result.result.result_status == "passed"
    assert result.result.passed is True


def test_missing_execution_evidence_classifies_as_missing_evidence(db_session):
    task, created, outcome, specification, evidence_row = (
        _structured_runtime_with_evidence(
            db_session, predicate_id="execution_evidence_exists", ingest=False
        )
    )
    resolved = CandidateEvidenceResolverService(db_session).resolve(
        _resolve_command(task, outcome, specification, evidence_row=None)
    )
    db_session.commit()
    assert resolved.evidence.resolution_status == "missing"

    result = DeterministicValidatorService(db_session).validate(
        _predicate_command(
            task,
            outcome,
            specification,
            resolved.evidence,
            "execution_evidence_exists",
        )
    )
    db_session.commit()
    assert result.result.result_status == "missing_evidence"
    assert result.result.passed is False


def test_kind_matches_fails_on_mismatched_kind(db_session):
    task, created, outcome, specification, evidence_row = (
        _structured_runtime_with_evidence(
            db_session,
            predicate_id="execution_evidence_kind_matches",
            expected_kind="test",
            ingest_kind="lint",
            ingest_content=b"lint output",
            ingest_key="exec-evidence-kind-mismatch",
        )
    )
    resolved = CandidateEvidenceResolverService(db_session).resolve(
        _resolve_command(task, outcome, specification, evidence_row)
    )
    db_session.commit()
    assert resolved.evidence.resolution_status == "resolved"

    result = DeterministicValidatorService(db_session).validate(
        _predicate_command(
            task,
            outcome,
            specification,
            resolved.evidence,
            "execution_evidence_kind_matches",
        )
    )
    db_session.commit()
    assert result.result.result_status == "failed"
    assert result.result.result_code == "execution_evidence_kind_mismatch"


def test_producer_matches_passes_for_bound_producer(db_session):
    task, created, outcome, specification, evidence_row = (
        _structured_runtime_with_evidence(
            db_session,
            predicate_id="execution_evidence_producer_matches",
            expected_kind="command",
        )
    )
    resolved = CandidateEvidenceResolverService(db_session).resolve(
        _resolve_command(task, outcome, specification, evidence_row)
    )
    db_session.commit()

    result = DeterministicValidatorService(db_session).validate(
        _predicate_command(
            task,
            outcome,
            specification,
            resolved.evidence,
            "execution_evidence_producer_matches",
        )
    )
    db_session.commit()
    assert result.result.result_status == "passed"


def test_media_type_matches_fails_on_mismatch(db_session):
    task, created, outcome, specification, evidence_row = (
        _structured_runtime_with_evidence(
            db_session,
            predicate_id="execution_evidence_media_type_matches",
            expected_media_type="text/plain",
        )
    )
    resolved = CandidateEvidenceResolverService(db_session).resolve(
        _resolve_command(task, outcome, specification, evidence_row)
    )
    db_session.commit()

    result = DeterministicValidatorService(db_session).validate(
        _predicate_command(
            task,
            outcome,
            specification,
            resolved.evidence,
            "execution_evidence_media_type_matches",
        )
    )
    db_session.commit()
    assert result.result.result_status == "failed"
    assert result.result.result_code == "execution_evidence_media_type_mismatch"


def test_hash_matches_passes_for_verified_evidence(db_session):
    task, created, outcome, specification, evidence_row = (
        _structured_runtime_with_evidence(
            db_session, predicate_id="execution_evidence_hash_matches"
        )
    )
    resolved = CandidateEvidenceResolverService(db_session).resolve(
        _resolve_command(task, outcome, specification, evidence_row)
    )
    db_session.commit()

    result = DeterministicValidatorService(db_session).validate(
        _predicate_command(
            task,
            outcome,
            specification,
            resolved.evidence,
            "execution_evidence_hash_matches",
        )
    )
    db_session.commit()
    assert result.result.result_status == "passed"
    assert result.result.actual_summary["hash"] == evidence_row.content_sha256


def test_tampered_execution_evidence_hash_fails_integrity_and_classifies_invalid(
    db_session,
):
    task, created, outcome, specification, evidence_row = (
        _structured_runtime_with_evidence(
            db_session, predicate_id="execution_evidence_hash_matches"
        )
    )
    # Simulate authority tampering: the recorded hash no longer matches the
    # independently-stored bytes.  This is a read-only inspection scenario --
    # nothing here re-executes ingestion or mutates the blob store.
    stored = db_session.get(ExecutionEvidence, evidence_row.id)
    stored.content_sha256 = "0" * 64
    db_session.flush()

    resolved = CandidateEvidenceResolverService(db_session).resolve(
        _resolve_command(task, outcome, specification, evidence_row)
    )
    db_session.commit()
    assert resolved.evidence.resolution_status == "invalid_content"

    result = DeterministicValidatorService(db_session).validate(
        _predicate_command(
            task,
            outcome,
            specification,
            resolved.evidence,
            "execution_evidence_hash_matches",
        )
    )
    db_session.commit()
    assert result.result.result_status == "invalid_evidence"
    assert result.result.passed is False


def test_resolution_is_idempotent_on_replay(db_session):
    task, created, outcome, specification, evidence_row = (
        _structured_runtime_with_evidence(
            db_session, predicate_id="execution_evidence_exists"
        )
    )
    command = _resolve_command(task, outcome, specification, evidence_row)
    first = CandidateEvidenceResolverService(db_session).resolve(command)
    db_session.commit()
    second = CandidateEvidenceResolverService(db_session).resolve(command)
    db_session.commit()
    assert second.replayed is True
    assert second.evidence.id == first.evidence.id


# ---------------------------------------------------------------------------
# Migration replay
# ---------------------------------------------------------------------------


def test_migration_045_is_additive_and_replay_safe():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    run_schema_migrations(engine, MIGRATIONS)
    run_schema_migrations(engine, MIGRATIONS)  # replay must be a no-op
    inspector = inspect(engine)
    index_names = {
        row["name"]
        for row in inspector.get_indexes("execution_task_resolved_validation_evidence")
    }
    assert "ix_execution_task_resolved_evidence_source_key" in index_names
    # C9/C10/C11 tables remain untouched by this migration.
    for table in (
        "execution_task_candidate_contents",
        "execution_validation_schemas",
        "execution_evidence",
    ):
        assert table in inspector.get_table_names()


def test_default_registry_has_all_generic_execution_evidence_predicates():
    registry = build_default_validator_registry()
    for predicate_id in (
        "execution_evidence_exists",
        "execution_evidence_kind_matches",
        "execution_evidence_media_type_matches",
        "execution_evidence_hash_matches",
        "execution_evidence_producer_matches",
    ):
        assert registry.registration(predicate_id, 1) is not None
