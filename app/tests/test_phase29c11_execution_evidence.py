"""Focused Phase 29C-11 immutable execution-evidence authority tests."""

from __future__ import annotations

import hashlib

import pytest
from sqlalchemy import create_engine, inspect

from app.models import Base, ExecutionEvidence, ExecutionTaskCandidateContent
from app.db_migrations import MIGRATIONS, run_schema_migrations
from app.services.execution.candidate_content import (
    CandidateContentIngestionService,
    IngestCandidateContentCommand,
    LocalContentAddressedStore,
    cleanup_unlinked_candidate_content,
    verify_candidate_content_integrity,
)
from app.services.execution.execution_evidence import (
    EVIDENCE_KIND_PRODUCERS,
    ExecutionEvidenceError,
    ExecutionEvidenceIngestionService,
    IngestExecutionEvidenceCommand,
    cleanup_unlinked_execution_evidence,
    evidence_reference_for_id,
    normalize_execution_evidence_reference,
    parse_execution_evidence_reference,
    resolve_execution_evidence_reference,
    verify_execution_evidence_integrity,
)
from app.services.execution.execution_task_runtime_execution_service import (
    ExecutionTaskRuntimeExecutionService,
)

from test_phase29c6b_runtime_evidence import _owned, _record_command, _start_command


def _evidence_command(task, created, content, *, kind, key, **overrides):
    values = {
        "execution_plan_id": task.execution_plan_id,
        "execution_task_id": task.id,
        "execution_task_attempt_id": created.attempt.id,
        "attempt_generation": created.attempt.attempt_generation,
        "evidence_kind": kind,
        "producer_id": EVIDENCE_KIND_PRODUCERS.get(kind, "unregistered-producer"),
        "producer_version": "1",
        "content": content,
        "ingestion_idempotency_key": key,
        "creation_actor_id": "test-producer",
    }
    values.update(overrides)
    return IngestExecutionEvidenceCommand(**values)


def test_migration_creates_bounded_replay_safe_table():
    engine = create_engine("sqlite://")
    Base.metadata.create_all(engine)
    run_schema_migrations(engine, MIGRATIONS)
    run_schema_migrations(engine, MIGRATIONS)  # replay must be a no-op

    inspector = inspect(engine)
    assert "execution_evidence" in inspector.get_table_names()
    columns = {col["name"] for col in inspector.get_columns("execution_evidence")}
    assert {
        "evidence_kind",
        "producer_id",
        "producer_version",
        "content_sha256",
        "storage_backend_id",
        "storage_key",
        "canonical_metadata_hash",
    }.issubset(columns)


def test_ingestion_persists_immutable_metadata_and_reuses_blob_store(
    db_session, tmp_path
):
    task, created, ownership, _ = _owned(db_session)
    body = b'{"tests_passed": 12}'
    digest = hashlib.sha256(body).hexdigest()
    store = LocalContentAddressedStore(tmp_path / "evidence")
    service = ExecutionEvidenceIngestionService(db_session, store=store)

    result = service.ingest(
        _evidence_command(
            task,
            created,
            body,
            kind="test",
            key="evidence-test-1",
            media_type="application/json",
        )
    )
    db_session.commit()

    row = result.evidence
    assert row.evidence_kind == "test"
    assert row.producer_id == "test-runner"
    assert row.content_sha256 == digest
    assert row.storage_key == f"sha256/{digest[:2]}/{digest}"
    assert store.read(row.storage_key) == body
    assert verify_execution_evidence_integrity(db_session, row.id, store=store).verified

    reference = evidence_reference_for_id(row.id)
    assert reference == f"execution-evidence://{row.id}"
    assert normalize_execution_evidence_reference(reference) == reference


def test_blob_store_is_shared_with_candidate_content_and_retention_is_reference_aware(
    db_session, tmp_path
):
    task, created, ownership, _ = _owned(db_session)
    runtime = ExecutionTaskRuntimeExecutionService(db_session)
    start = runtime.mark_runtime_execution_started(
        _start_command(task, created, ownership, key="evidence-shared-start")
    )
    db_session.commit()
    body = b'{"shared": true}'
    digest = hashlib.sha256(body).hexdigest()
    outcome = runtime.record_runtime_attempt_outcome(
        _record_command(
            task,
            created,
            ownership,
            start.start,
            status="candidate_completed",
            key="evidence-shared-outcome",
            output_hash=digest,
        )
    )
    db_session.commit()

    store = LocalContentAddressedStore(tmp_path / "shared")
    candidate_result = CandidateContentIngestionService(db_session, store=store).ingest(
        IngestCandidateContentCommand(
            execution_plan_id=task.execution_plan_id,
            execution_task_id=task.id,
            execution_task_attempt_id=created.attempt.id,
            attempt_generation=created.attempt.attempt_generation,
            candidate_outcome_id=outcome.outcome.id,
            content=body,
            ingestion_idempotency_key="evidence-shared-candidate",
            media_type="application/json",
        )
    )
    db_session.commit()

    evidence_service = ExecutionEvidenceIngestionService(db_session, store=store)
    evidence_result = evidence_service.ingest(
        _evidence_command(
            task,
            created,
            body,
            kind="candidate",
            key="evidence-shared-evidence",
            media_type="application/json",
        )
    )
    db_session.commit()

    assert candidate_result.content.storage_key == evidence_result.evidence.storage_key

    # Deleting the execution-evidence row must not orphan the shared blob:
    # candidate content still references the same key.
    db_session.delete(evidence_result.evidence)
    db_session.commit()
    removed = cleanup_unlinked_execution_evidence(db_session, store=store)
    assert removed == ()
    assert store.read(candidate_result.content.storage_key) == body
    assert verify_candidate_content_integrity(
        db_session, candidate_result.content.id, store=store
    ).verified

    removed_by_candidate_side = cleanup_unlinked_candidate_content(
        db_session, store=store
    )
    assert removed_by_candidate_side == ()


def test_producer_and_kind_validation_fail_closed(db_session, tmp_path):
    task, created, ownership, _ = _owned(db_session)
    store = LocalContentAddressedStore(tmp_path / "evidence")
    service = ExecutionEvidenceIngestionService(db_session, store=store)

    with pytest.raises(ExecutionEvidenceError) as unsupported_kind:
        service.ingest(
            _evidence_command(
                task,
                created,
                b"data",
                kind="coverage",
                key="evidence-bad-kind",
                producer_id="coverage-runner",
            )
        )
    assert unsupported_kind.value.code == "execution_evidence_kind_unsupported"

    with pytest.raises(ExecutionEvidenceError) as unsupported_producer:
        service.ingest(
            _evidence_command(
                task,
                created,
                b"data",
                kind="test",
                key="evidence-bad-producer",
                producer_id="not-a-producer",
            )
        )
    assert unsupported_producer.value.code == "execution_evidence_producer_unsupported"

    with pytest.raises(ExecutionEvidenceError) as mismatch:
        service.ingest(
            _evidence_command(
                task,
                created,
                b"data",
                kind="test",
                key="evidence-mismatch",
                producer_id="lint-runner",
            )
        )
    assert mismatch.value.code == "execution_evidence_producer_kind_mismatch"
    assert db_session.query(ExecutionEvidence).count() == 0


def test_duplicate_idempotency_key_conflict_and_replay_are_exact(db_session, tmp_path):
    task, created, ownership, _ = _owned(db_session)
    store = LocalContentAddressedStore(tmp_path / "evidence")
    service = ExecutionEvidenceIngestionService(db_session, store=store)
    command = _evidence_command(
        task, created, b"lint clean", kind="lint", key="evidence-replay"
    )

    first = service.ingest(command)
    db_session.commit()
    replay = service.ingest(command)
    assert replay.replayed is True
    assert replay.evidence.id == first.evidence.id

    with pytest.raises(ExecutionEvidenceError) as conflict:
        service.ingest(
            _evidence_command(
                task, created, b"different bytes", kind="lint", key="evidence-replay"
            )
        )
    assert conflict.value.code == "execution_evidence_idempotency_conflict"
    assert db_session.query(ExecutionEvidence).count() == 1


def test_reference_resolution_covers_missing_and_ambiguous_and_by_id(
    db_session, tmp_path
):
    task, created, ownership, _ = _owned(db_session)
    store = LocalContentAddressedStore(tmp_path / "evidence")
    service = ExecutionEvidenceIngestionService(db_session, store=store)
    same_bytes = b"command output identical"

    first = service.ingest(
        _evidence_command(
            task, created, same_bytes, kind="command", key="evidence-ref-1"
        )
    )
    db_session.commit()

    resolved = resolve_execution_evidence_reference(
        db_session, evidence_reference_for_id(first.evidence.id), store=store
    )
    assert resolved.verified is True
    assert resolved.evidence_kind == "command"
    assert resolved.byte_length == len(same_bytes)

    missing = resolve_execution_evidence_reference(
        db_session, "execution-evidence://999999", store=store
    )
    assert missing.resolution_status == "missing"

    digest = hashlib.sha256(same_bytes).hexdigest()
    second = service.ingest(
        _evidence_command(
            task,
            created,
            same_bytes,
            kind="lint",
            key="evidence-ref-2",
        )
    )
    db_session.commit()
    assert second.evidence.content_sha256 == digest

    ambiguous = resolve_execution_evidence_reference(
        db_session, f"execution-evidence-sha256://{digest}", store=store
    )
    assert ambiguous.resolution_status == "ambiguous_reference"
    assert ambiguous.issues == ("execution_evidence_reference_ambiguous",)


def test_reference_grammar_rejects_unsupported_schemes():
    with pytest.raises(ExecutionEvidenceError) as invalid:
        parse_execution_evidence_reference("candidate-content://1")
    assert invalid.value.code == "execution_evidence_scheme_unsupported"

    with pytest.raises(ExecutionEvidenceError) as malformed:
        parse_execution_evidence_reference("execution-evidence://not-a-number")
    assert malformed.value.code == "execution_evidence_scheme_unsupported"


def test_integrity_detects_storage_tamper(db_session, tmp_path):
    task, created, ownership, _ = _owned(db_session)
    store = LocalContentAddressedStore(tmp_path / "evidence")
    service = ExecutionEvidenceIngestionService(db_session, store=store)
    body = b"lint output"
    result = service.ingest(
        _evidence_command(task, created, body, kind="lint", key="evidence-tamper")
    )
    db_session.commit()

    path = store._path_for_key(result.evidence.storage_key)
    import os
    import stat

    os.chmod(path, stat.S_IWUSR | stat.S_IRUSR)
    path.write_bytes(b"tampered bytes!!")

    integrity = verify_execution_evidence_integrity(
        db_session, result.evidence.id, store=store
    )
    assert integrity.verified is False
    assert "candidate_content_storage_mutable" in integrity.issues
