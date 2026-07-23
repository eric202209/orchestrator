"""Focused Phase 29C-9 immutable candidate-content authority tests."""

from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib

import pytest
from sqlalchemy import create_engine, inspect, text

from app.models import (
    Base,
    ExecutionTaskCandidateContent,
    ExecutionTaskValidationSpecification,
)
from app.db_migrations import MIGRATIONS, run_schema_migrations
from app.services.execution.candidate_content import (
    CandidateContentError,
    CandidateContentIngestionService,
    CandidateContentIntegrityResult,
    IngestCandidateContentCommand,
    LocalContentAddressedStore,
    MAX_CANDIDATE_CONTENT_BYTES,
    cleanup_unlinked_candidate_content,
    verify_candidate_content_integrity,
    verify_candidate_content_store_integrity,
)
from app.services.execution.candidate_evidence import (
    CandidateEvidenceError,
    CandidateEvidenceResolverService,
    DeterministicValidatorService,
    EvaluateCandidatePredicateCommand,
    ResolveCandidateEvidenceCommand,
    ValidationPrimitiveService,
    normalize_evidence_reference,
    parse_evidence_reference,
)
from app.services.execution.execution_task_runtime_execution_service import (
    ExecutionTaskRuntimeExecutionService,
)
from app.services.execution.runtime_execution_adapter import (
    DeterministicExecutionRuntimeAdapter,
    RuntimeExecutionResult,
)
from app.services.planning.validation_contract import (
    StructuredValidationContract,
    VALIDATION_CONTRACT_SCHEMA_VERSION,
    VALIDATION_ENVIRONMENT_SCHEMA_VERSION,
)
from app.services.execution.validation_contract import ValidationContractService
from app.services.planning.structured_task_plan import Task

from test_phase29c6b_runtime_evidence import (
    _owned,
    _record_command,
    _start_command,
)
from test_phase29c7a_validation_contract import _pass_policy


ENVIRONMENT_HASH = "a" * 64


def _content_contract() -> StructuredValidationContract:
    predicates = [
        ("content_exists", 10),
        ("content_hash_matches", 20),
        ("content_size_within_limit", 30),
        ("media_type_matches", 40),
    ]
    return StructuredValidationContract.from_mapping(
        {
            "schema_version": VALIDATION_CONTRACT_SCHEMA_VERSION,
            "status": "structured_executable",
            "predicates": [
                {
                    "predicate_id": predicate_id,
                    "predicate_version": 1,
                    "evidence_key": "primary_content",
                    "parameters": {},
                    "required": True,
                    "order": order,
                }
                for predicate_id, order in predicates
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


def _completed(db_session, *, output_hash: str | None = None):
    task, created, ownership, _ = _owned(db_session)
    runtime = ExecutionTaskRuntimeExecutionService(db_session)
    start = runtime.mark_runtime_execution_started(
        _start_command(task, created, ownership, key=f"content-start-{task.id}")
    )
    db_session.commit()
    outcome = runtime.record_runtime_attempt_outcome(
        _record_command(
            task,
            created,
            ownership,
            start.start,
            status="candidate_completed",
            key=f"content-outcome-{task.id}",
            output_hash=output_hash,
        )
    )
    db_session.commit()
    return task, created, outcome.outcome


def _ingest_command(task, created, outcome, content, *, key, **overrides):
    values = {
        "execution_plan_id": task.execution_plan_id,
        "execution_task_id": task.id,
        "execution_task_attempt_id": created.attempt.id,
        "attempt_generation": created.attempt.attempt_generation,
        "candidate_outcome_id": outcome.id,
        "content": content,
        "ingestion_idempotency_key": key,
        "creation_actor_id": "test-runtime",
    }
    values.update(overrides)
    return IngestCandidateContentCommand(**values)


def _project_content_contract(db_session, task):
    authored = Task(**task.task_spec)
    authored = replace(
        authored,
        work_items=tuple(
            replace(item, validation_contract=_content_contract())
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
    db_session.commit()
    return specification


def test_ingestion_recomputes_hash_persists_authority_and_projection(
    db_session, tmp_path
):
    body = b'{"result":{"id":7}}'
    digest = hashlib.sha256(body).hexdigest()
    task, created, outcome = _completed(db_session, output_hash=digest)
    store = LocalContentAddressedStore(tmp_path / "content")
    result = CandidateContentIngestionService(db_session, store=store).ingest(
        _ingest_command(
            task,
            created,
            outcome,
            body,
            key="content-ingest-1",
            media_type="application/json",
        )
    )
    db_session.commit()

    row = result.content
    assert row.content_sha256 == digest
    assert row.declared_sha256 == digest
    assert row.byte_length == len(body)
    assert row.media_type == "application/json"
    assert row.storage_key == f"sha256/{digest[:2]}/{digest}"
    assert row.content_projection == {"result": {"id": 7}}
    assert store.read(row.storage_key) == body
    assert verify_candidate_content_integrity(db_session, row.id, store=store).verified
    assert verify_candidate_content_store_integrity(db_session, store=store).verified


def test_hash_mismatch_media_and_size_fail_closed(db_session, tmp_path):
    task, created, outcome = _completed(db_session)
    store = LocalContentAddressedStore(tmp_path / "content", max_bytes=4)
    with pytest.raises(CandidateContentError) as mismatch:
        CandidateContentIngestionService(db_session, store=store).ingest(
            _ingest_command(
                task,
                created,
                outcome,
                b"candidate",
                key="content-mismatch",
                declared_sha256="0" * 64,
            )
        )
    assert mismatch.value.code == "candidate_content_hash_mismatch"
    assert db_session.query(ExecutionTaskCandidateContent).count() == 0
    with pytest.raises(CandidateContentError) as too_large:
        CandidateContentIngestionService(db_session, store=store).ingest(
            _ingest_command(task, created, outcome, b"12345", key="content-large")
        )
    assert too_large.value.code == "candidate_content_too_large"
    with pytest.raises(CandidateContentError) as media:
        CandidateContentIngestionService(
            db_session, store=LocalContentAddressedStore(tmp_path / "media")
        ).ingest(
            _ingest_command(
                task,
                created,
                outcome,
                b"candidate",
                key="content-media",
                media_type="image/png",
            )
        )
    assert media.value.code == "candidate_content_media_type_invalid"


def test_ingestion_replay_and_changed_key_conflict_are_exact(db_session, tmp_path):
    body = b"same bytes"
    task, created, outcome = _completed(db_session)
    store = LocalContentAddressedStore(tmp_path / "content")
    command = _ingest_command(task, created, outcome, body, key="content-replay")
    service = CandidateContentIngestionService(db_session, store=store)
    first = service.ingest(command)
    db_session.commit()
    replay = service.ingest(command)
    assert replay.replayed is True
    assert replay.content.id == first.content.id
    with pytest.raises(CandidateContentError) as conflict:
        service.ingest(
            _ingest_command(task, created, outcome, b"different", key="content-replay")
        )
    assert conflict.value.code == "candidate_content_idempotency_conflict"
    assert db_session.query(ExecutionTaskCandidateContent).count() == 1


def test_runtime_completion_contract_ingests_direct_bytes_in_same_authority_boundary(
    db_session, tmp_path
):
    body = b"runtime bytes"
    digest = hashlib.sha256(body).hexdigest()
    task, created, ownership, _ = _owned(db_session)
    store = LocalContentAddressedStore(tmp_path / "content")
    service = ExecutionTaskRuntimeExecutionService(
        db_session, candidate_content_store=store
    )
    result = service.execute_owned_runtime_attempt(
        _start_command(task, created, ownership, key="adapter-byte-start"),
        DeterministicExecutionRuntimeAdapter(
            result=RuntimeExecutionResult(
                completion_kind="candidate_completed",
                output_reference="runtime://opaque-pointer",
                output_hash=digest,
                candidate_bytes=body,
                candidate_media_type="text/plain",
            )
        ),
    )
    assert result.rejected is False
    assert result.outcome is not None
    assert result.candidate_content is not None
    assert result.candidate_content.content.content_sha256 == digest
    assert db_session.query(ExecutionTaskCandidateContent).count() == 1
    assert task.status == "awaiting_validation"


def test_runtime_completion_hash_mismatch_is_rejected_before_candidate_commit(
    db_session, tmp_path
):
    task, created, ownership, _ = _owned(db_session)
    service = ExecutionTaskRuntimeExecutionService(
        db_session,
        candidate_content_store=LocalContentAddressedStore(tmp_path / "content"),
    )
    result = service.execute_owned_runtime_attempt(
        _start_command(task, created, ownership, key="adapter-mismatch-start"),
        DeterministicExecutionRuntimeAdapter(
            result=RuntimeExecutionResult(
                completion_kind="candidate_completed",
                output_reference="runtime://opaque-pointer",
                output_hash="0" * 64,
                candidate_bytes=b"actual bytes",
                candidate_media_type="text/plain",
            )
        ),
    )
    assert result.rejected is True
    assert result.error_code == "candidate_content_hash_mismatch"
    assert result.outcome is None
    assert db_session.query(ExecutionTaskCandidateContent).count() == 0
    assert task.status == "running"


def test_store_collision_and_tamper_are_not_silent(tmp_path, monkeypatch):
    store = LocalContentAddressedStore(tmp_path / "content")
    digest = hashlib.sha256(b"one").hexdigest()
    key = f"sha256/{digest[:2]}/{digest}"
    path = store._path_for_key(key)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"two")
    path.chmod(0o444)
    with pytest.raises(CandidateContentError) as collision:
        store.put(b"one")
    assert collision.value.code == "candidate_content_hash_collision"

    # CI runs as a non-root user; temporarily make the fixture writable to
    # model an on-disk tamper, then restore the store's immutable mode.
    path.chmod(0o644)
    path.write_bytes(b"one")
    path.chmod(0o444)
    path.chmod(0o644)
    path.write_bytes(b"tampered")
    path.chmod(0o444)
    with pytest.raises(CandidateContentError) as tamper:
        store.read(key)
    assert tamper.value.code == "candidate_content_storage_tampered"


def test_content_reference_grammar_rejects_paths_and_accepts_task_scoped_hashes():
    assert (
        normalize_evidence_reference("CANDIDATE-CONTENT://42")
        == "candidate-content://42"
    )
    digest = "a" * 64
    assert parse_evidence_reference(f"content-sha256://{digest}").identifier == digest
    for reference in (
        "candidate-content://1/../2",
        "candidate-content://user@1",
        "candidate-content://1?x=1",
        "https://example.test/candidate",
        "file:///tmp/candidate",
        "content-sha256://not-a-hash",
        "candidate-content://0",
    ):
        with pytest.raises(CandidateEvidenceError):
            parse_evidence_reference(reference)


def test_c7b_resolves_immutable_content_and_predicates_distinguish_strength(
    db_session, tmp_path
):
    body = b'{"result":{"id":7}}'
    digest = hashlib.sha256(body).hexdigest()
    task, created, outcome = _completed(db_session, output_hash=digest)
    store = LocalContentAddressedStore(tmp_path / "content")
    content = (
        CandidateContentIngestionService(db_session, store=store)
        .ingest(
            _ingest_command(
                task,
                created,
                outcome,
                body,
                key="content-resolve",
                media_type="application/json",
            )
        )
        .content
    )
    db_session.commit()
    specification = _project_content_contract(db_session, task)
    resolver = CandidateEvidenceResolverService(db_session, content_store=store)
    evidence = resolver.resolve(
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
            expected_content_hash=digest,
            expected_hash_algorithm="sha256",
            environment_configuration_hash=ENVIRONMENT_HASH,
            resolution_idempotency_key="resolve-content-1",
            deterministic_resolution_command_id="resolve-content-command-1",
        )
    ).evidence
    assert evidence.actual_hash == digest
    assert evidence.byte_size == len(body)
    assert evidence.media_type == "application/json"
    assert evidence.content_addressed_reference == f"candidate-content://{content.id}"
    assert evidence.content_projection == {"result": {"id": 7}}
    assert (
        evidence.structured_metadata_summary["verification_level"]
        == "independently_recomputed_bytes"
    )
    assert (
        ValidationPrimitiveService(db_session, content_store=store)
        .verify_resolved_validation_evidence_integrity(evidence.id)
        .verified
    )

    predicate_ids = (
        "content_exists",
        "content_hash_matches",
        "content_size_within_limit",
        "media_type_matches",
    )
    for index, predicate_id in enumerate(predicate_ids):
        result = DeterministicValidatorService(
            db_session,
            content_store=store,
        ).validate(
            EvaluateCandidatePredicateCommand(
                execution_plan_id=task.execution_plan_id,
                execution_task_id=task.id,
                execution_task_attempt_id=created.attempt.id,
                candidate_outcome_id=outcome.id,
                validation_specification_id=specification.id,
                validation_specification_hash=specification.canonical_specification_hash,
                predicate_id=predicate_id,
                predicate_version=1,
                predicate_order=(index + 1) * 10,
                evidence_snapshot_id=evidence.id,
                evidence_key="primary_content",
                validator_id=predicate_id,
                validator_version=1,
                validator_set_id="deterministic_readonly",
                validator_set_version="1",
                environment_configuration_hash=ENVIRONMENT_HASH,
                validator_idempotency_key=f"predicate-content-{predicate_id}",
                deterministic_validator_command_id=f"predicate-content-command-{predicate_id}",
            )
        )
        assert result.result.result_status == "passed"
    assert task.status == "awaiting_validation"


def test_retention_removes_unlinked_blob_only_after_metadata_is_gone(
    db_session, tmp_path
):
    body = b"retained until linkage deletion"
    task, created, outcome = _completed(db_session)
    store = LocalContentAddressedStore(tmp_path / "content")
    row = (
        CandidateContentIngestionService(db_session, store=store)
        .ingest(_ingest_command(task, created, outcome, body, key="content-retention"))
        .content
    )
    db_session.commit()
    assert store.list_storage_keys()
    db_session.delete(row)
    db_session.commit()
    assert store.list_storage_keys()
    removed = cleanup_unlinked_candidate_content(db_session, store=store)
    assert removed
    assert store.list_storage_keys() == ()


def test_candidate_content_migration_is_additive_replay_safe_and_empty(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'migration.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.execute(text("DROP TABLE execution_task_candidate_contents"))
    pre_phase = tuple(
        migration
        for migration in MIGRATIONS
        if migration.version < "042_execution_task_candidate_content_boundary"
    )
    run_schema_migrations(engine, pre_phase)
    run_schema_migrations(engine)
    run_schema_migrations(engine)
    assert "execution_task_candidate_contents" in inspect(engine).get_table_names()
    with engine.connect() as connection:
        assert (
            connection.execute(
                text("SELECT COUNT(*) FROM execution_task_candidate_contents")
            ).scalar_one()
            == 0
        )
        assert (
            connection.execute(
                text(
                    "SELECT COUNT(*) FROM schema_migrations "
                    "WHERE version = '042_execution_task_candidate_content_boundary'"
                )
            ).scalar_one()
            == 1
        )
    engine.dispose()
