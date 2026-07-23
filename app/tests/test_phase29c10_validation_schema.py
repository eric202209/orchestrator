"""Focused Phase 29C-10 immutable schema and JSON Schema boundary tests."""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import create_engine, inspect, text

from app.db_migrations import run_schema_migrations
from app.config import settings
from app.models import Base, ExecutionPlan, ExecutionTaskValidationSpecification
from app.services.execution.candidate_content import (
    CandidateContentIngestionService,
    LocalContentAddressedStore,
)
from app.services.execution.candidate_evidence import (
    DeterministicValidatorService,
    EvaluateCandidatePredicateCommand,
    ResolveCandidateEvidenceCommand,
    CandidateEvidenceResolverService,
)
from app.services.execution.validation_run import ValidationRunService
from app.services.execution.validation_schema import (
    CreateValidationSchemaCommand,
    ExecutionValidationSchemaService,
    MAX_SCHEMA_ENCODED_BYTES,
    SUPPORTED_SCHEMA_DIALECT,
    ValidationSchemaError,
    canonicalize_validation_schema,
    schema_reference_for_id,
)
from app.services.planning.validation_contract import (
    StructuredValidationContract,
    VALIDATION_CONTRACT_SCHEMA_VERSION,
    VALIDATION_ENVIRONMENT_SCHEMA_VERSION,
)

from test_phase29c7c_validation_run_acceptance import (
    _plan_contract_hash,
    _rebind_contract,
    _validation_command,
)
from test_phase29c9_candidate_content import _completed, _ingest_command


ENVIRONMENT_HASH = "a" * 64


def _schema():
    return {
        "$schema": SUPPORTED_SCHEMA_DIALECT,
        "type": "object",
        "properties": {
            "result": {
                "type": "object",
                "properties": {"id": {"type": "integer"}},
                "required": ["id"],
                "additionalProperties": False,
            }
        },
        "required": ["result"],
        "additionalProperties": False,
    }


def _schema_contract(schema_id: str, schema_hash: str) -> StructuredValidationContract:
    return StructuredValidationContract.from_mapping(
        {
            "schema_version": VALIDATION_CONTRACT_SCHEMA_VERSION,
            "status": "structured_executable",
            "predicates": [
                {
                    "predicate_id": "json_schema_matches",
                    "predicate_version": 1,
                    "evidence_key": "primary_content",
                    "parameters": {
                        "schema_reference": schema_reference_for_id(schema_id),
                        "schema_hash": schema_hash,
                        "schema_dialect": SUPPORTED_SCHEMA_DIALECT,
                    },
                    "required": True,
                    "order": 10,
                }
            ],
            "evidence_descriptors": [
                {
                    "evidence_key": "primary_content",
                    "evidence_type": "candidate_content",
                    "source": "candidate_content",
                    "required": True,
                    "expected_media_type": "application/json",
                    "expected_hash_algorithm": "sha256",
                    "resolver_version": "candidate-evidence-resolver/1",
                }
            ],
            "pass_policy": {
                "policy_id": "all_required",
                "policy_version": 1,
                "optional_predicate_behavior": "ignore",
                "missing_evidence": "fail",
                "validator_error": "fail",
                "short_circuit": False,
                "review_separate_requirement": True,
            },
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


def _bind_schema_contract(db_session, task, specification, contract, schema):
    _rebind_contract(db_session, task, specification, contract)
    specification.validation_schema_id = schema.id
    specification.validation_schema_reference = schema_reference_for_id(
        schema.schema_id
    )
    specification.validation_schema_hash = schema.schema_sha256
    specification.validation_schema_dialect = schema.dialect
    plan = db_session.get(ExecutionPlan, task.execution_plan_id)
    plan.validation_contract_set_hash = _plan_contract_hash(db_session, plan.id)
    db_session.flush()


def test_schema_is_canonical_content_identity_and_replay_safe(db_session):
    service = ExecutionValidationSchemaService(db_session)
    first = service.create(
        CreateValidationSchemaCommand(
            schema={"$schema": SUPPORTED_SCHEMA_DIALECT, "type": "object"},
            idempotency_key="schema-create-1",
        )
    )
    equivalent = service.create(
        CreateValidationSchemaCommand(
            schema={"type": "object", "$schema": SUPPORTED_SCHEMA_DIALECT},
            idempotency_key="schema-create-1",
        )
    )
    assert equivalent.replayed
    assert first.schema.schema_id.startswith("sha256:")
    assert first.schema.schema_id == first.schema.schema_sha256
    assert service.verify_integrity(first.schema.id).verified

    with pytest.raises(ValidationSchemaError) as conflict:
        service.create(
            CreateValidationSchemaCommand(
                schema={"$schema": SUPPORTED_SCHEMA_DIALECT, "type": "array"},
                idempotency_key="schema-create-1",
            )
        )
    assert conflict.value.code == "validation_schema_idempotency_conflict"


@pytest.mark.parametrize(
    "schema,code",
    [
        (
            {"$schema": SUPPORTED_SCHEMA_DIALECT, "$ref": "https://example.invalid/x"},
            "validation_schema_external_reference",
        ),
        (
            {"$schema": SUPPORTED_SCHEMA_DIALECT, "format": "email"},
            "validation_schema_keyword_unsupported",
        ),
    ],
)
def test_schema_safety_rejects_external_references_and_unsupported_keywords(
    schema, code
):
    with pytest.raises(ValidationSchemaError) as exc_info:
        canonicalize_validation_schema(schema)
    assert exc_info.value.code == code


def test_schema_size_bound_is_enforced():
    with pytest.raises(ValidationSchemaError) as exc_info:
        canonicalize_validation_schema(
            {
                "$schema": SUPPORTED_SCHEMA_DIALECT,
                "description": "x" * MAX_SCHEMA_ENCODED_BYTES,
            }
        )
    assert exc_info.value.code == "validation_schema_bound_exceeded"


def test_release_binding_and_byte_backed_json_schema_pass_fail(db_session, tmp_path):
    schema = (
        ExecutionValidationSchemaService(db_session)
        .create(
            CreateValidationSchemaCommand(
                schema=_schema(), idempotency_key="schema-run-1"
            )
        )
        .schema
    )
    task, created, outcome = _completed(db_session)
    store = LocalContentAddressedStore(tmp_path / "content")
    body = b'{"result":{"id":7}}'
    content = (
        CandidateContentIngestionService(db_session, store=store)
        .ingest(
            _ingest_command(
                task,
                created,
                outcome,
                body,
                key="schema-content-1",
                media_type="application/json",
            )
        )
        .content
    )
    specification = db_session.get(
        ExecutionTaskValidationSpecification, task.validation_contract_id
    )
    contract = _schema_contract(schema.schema_id, schema.schema_sha256)
    _bind_schema_contract(db_session, task, specification, contract, schema)
    db_session.commit()

    assert specification.validation_schema_id == schema.id
    assert (
        specification.validation_schema_hash
        in specification.canonical_payload["structured_contract"]["predicates"][0][
            "parameters"
        ].values()
    )

    evidence = (
        CandidateEvidenceResolverService(db_session, content_store=store)
        .resolve(
            ResolveCandidateEvidenceCommand(
                execution_plan_id=task.execution_plan_id,
                execution_task_id=task.id,
                execution_task_attempt_id=created.attempt.id,
                candidate_outcome_id=outcome.id,
                validation_specification_id=specification.id,
                validation_specification_hash=specification.canonical_specification_hash,
                evidence_key="primary_content",
                evidence_type="candidate_content",
                evidence_source="candidate_content",
                expected_reference=f"candidate-content://{content.id}",
                expected_content_hash=hashlib.sha256(body).hexdigest(),
                expected_hash_algorithm="sha256",
                environment_configuration_hash=ENVIRONMENT_HASH,
                resolution_idempotency_key="schema-evidence-1",
                deterministic_resolution_command_id="schema-evidence-command-1",
            )
        )
        .evidence
    )
    result = (
        DeterministicValidatorService(db_session, content_store=store)
        .validate(
            EvaluateCandidatePredicateCommand(
                execution_plan_id=task.execution_plan_id,
                execution_task_id=task.id,
                execution_task_attempt_id=created.attempt.id,
                candidate_outcome_id=outcome.id,
                validation_specification_id=specification.id,
                validation_specification_hash=specification.canonical_specification_hash,
                predicate_id="json_schema_matches",
                predicate_version=1,
                predicate_order=10,
                evidence_snapshot_id=evidence.id,
                evidence_key="primary_content",
                validator_id="json_schema_matches_draft202012",
                validator_version=1,
                validator_set_id="deterministic_readonly",
                validator_set_version="1",
                environment_configuration_hash=ENVIRONMENT_HASH,
                validator_idempotency_key="schema-result-1",
                deterministic_validator_command_id="schema-result-command-1",
            )
        )
        .result
    )
    assert result.result_status == "passed"
    assert result.diagnostics["projection_hash"] == content.content_projection_hash
    assert result.diagnostics["validator_implementation_version"]


def test_byte_backed_json_schema_fail_is_bounded(db_session, tmp_path):
    schema = (
        ExecutionValidationSchemaService(db_session)
        .create(
            CreateValidationSchemaCommand(
                schema=_schema(), idempotency_key="schema-fail"
            )
        )
        .schema
    )
    task, created, outcome = _completed(db_session)
    store = LocalContentAddressedStore(tmp_path / "content")
    body = b'{"result":{"id":"wrong"}}'
    content = (
        CandidateContentIngestionService(db_session, store=store)
        .ingest(
            _ingest_command(
                task,
                created,
                outcome,
                body,
                key="schema-fail-content",
                media_type="application/json",
            )
        )
        .content
    )
    specification = db_session.get(
        ExecutionTaskValidationSpecification, task.validation_contract_id
    )
    contract = _schema_contract(schema.schema_id, schema.schema_sha256)
    _bind_schema_contract(db_session, task, specification, contract, schema)
    db_session.commit()
    evidence = (
        CandidateEvidenceResolverService(db_session, content_store=store)
        .resolve(
            ResolveCandidateEvidenceCommand(
                execution_plan_id=task.execution_plan_id,
                execution_task_id=task.id,
                execution_task_attempt_id=created.attempt.id,
                candidate_outcome_id=outcome.id,
                validation_specification_id=specification.id,
                validation_specification_hash=specification.canonical_specification_hash,
                evidence_key="primary_content",
                evidence_type="candidate_content",
                evidence_source="candidate_content",
                expected_reference=f"candidate-content://{content.id}",
                expected_content_hash=hashlib.sha256(body).hexdigest(),
                expected_hash_algorithm="sha256",
                environment_configuration_hash=ENVIRONMENT_HASH,
                resolution_idempotency_key="schema-fail-evidence",
                deterministic_resolution_command_id="schema-fail-evidence-command",
            )
        )
        .evidence
    )
    failed = (
        DeterministicValidatorService(db_session, content_store=store)
        .validate(
            EvaluateCandidatePredicateCommand(
                execution_plan_id=task.execution_plan_id,
                execution_task_id=task.id,
                execution_task_attempt_id=created.attempt.id,
                candidate_outcome_id=outcome.id,
                validation_specification_id=specification.id,
                validation_specification_hash=specification.canonical_specification_hash,
                predicate_id="json_schema_matches",
                predicate_version=1,
                predicate_order=10,
                evidence_snapshot_id=evidence.id,
                evidence_key="primary_content",
                validator_id="json_schema_matches_draft202012",
                validator_version=1,
                validator_set_id="deterministic_readonly",
                validator_set_version="1",
                environment_configuration_hash=ENVIRONMENT_HASH,
                validator_idempotency_key="schema-fail-result",
                deterministic_validator_command_id="schema-fail-result-command",
            )
        )
        .result
    )
    assert failed.result_status == "failed"
    assert failed.diagnostics["violation_paths"]
    assert failed.diagnostics["keyword_names"]


def test_referenced_schema_cannot_be_deleted_and_unreferenced_can(db_session):
    schema = (
        ExecutionValidationSchemaService(db_session)
        .create(
            CreateValidationSchemaCommand(
                schema=_schema(), idempotency_key="schema-retain"
            )
        )
        .schema
    )
    task, _created, _outcome = _completed(db_session)
    specification = db_session.get(
        ExecutionTaskValidationSpecification, task.validation_contract_id
    )
    _bind_schema_contract(
        db_session,
        task,
        specification,
        _schema_contract(schema.schema_id, schema.schema_sha256),
        schema,
    )
    db_session.commit()
    with pytest.raises(ValidationSchemaError) as exc_info:
        ExecutionValidationSchemaService(db_session).delete_if_unreferenced(schema.id)
    assert exc_info.value.code == "validation_schema_referenced"


def test_json_schema_flows_through_normal_c7c_classification(
    db_session, tmp_path, monkeypatch
):
    monkeypatch.setattr(settings, "CANDIDATE_CONTENT_DIR", str(tmp_path / "content"))
    schema = (
        ExecutionValidationSchemaService(db_session)
        .create(
            CreateValidationSchemaCommand(
                schema=_schema(), idempotency_key="schema-c7c"
            )
        )
        .schema
    )
    task, created, outcome = _completed(db_session)
    store = LocalContentAddressedStore()
    body = b'{"result":{"id":7}}'
    content = (
        CandidateContentIngestionService(db_session, store=store)
        .ingest(
            _ingest_command(
                task,
                created,
                outcome,
                body,
                key="schema-c7c-content",
                media_type="application/json",
            )
        )
        .content
    )
    specification = db_session.get(
        ExecutionTaskValidationSpecification, task.validation_contract_id
    )
    _bind_schema_contract(
        db_session,
        task,
        specification,
        _schema_contract(schema.schema_id, schema.schema_sha256),
        schema,
    )
    db_session.commit()
    result = ValidationRunService(db_session).execute_validation_run(
        _validation_command(task, outcome, specification, key="schema-c7c-run")
    )
    assert result.run.run_status == "accepted"
    assert db_session.get(type(task), task.id).status == "succeeded"
    assert content.content_projection_hash


def test_schema_authority_migration_is_fresh_and_replay_safe(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'schema-migration.db'}")
    Base.metadata.create_all(engine)
    run_schema_migrations(engine)
    run_schema_migrations(engine)
    with engine.connect() as connection:
        assert "execution_validation_schemas" in inspect(engine).get_table_names()
        assert (
            connection.execute(
                text(
                    "SELECT COUNT(*) FROM schema_migrations "
                    "WHERE version = '043_execution_validation_schema_authority'"
                )
            ).scalar_one()
            == 1
        )
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM execution_validation_schemas")
            ).scalar_one()
            == 0
        )
    engine.dispose()
