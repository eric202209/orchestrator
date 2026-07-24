"""Focused Phase 29D-1 ChangeSet / Controlled Apply authorization tests."""

from __future__ import annotations

from datetime import datetime, timezone
import json

import pytest
from sqlalchemy import inspect

from app.db_migrations import MIGRATIONS, run_schema_migrations
from app.models import (
    Base,
    ExecutionPlan,
    ExecutionTaskAcceptanceDecision,
    ExecutionTaskApplyAuthorization,
    ExecutionTaskCandidateContent,
    ExecutionTaskChangeSet,
    ExecutionTaskChangeSetOperation,
)
from app.services.execution.apply_authorization import (
    ApplyAuthorizationError,
    ApplyAuthorizationService,
    APPLY_POLICY_ID,
    APPLY_POLICY_VERSION,
    AuthorizeApplyCommand,
    BASE_STATE_AUTHORITY_UNAVAILABLE_REASON,
    evaluate_apply_policy,
    verify_apply_authorization_integrity,
)
from app.services.execution.candidate_content import (
    CandidateContentIngestionService,
    CandidateContentIntegrityResult,
    IngestCandidateContentCommand,
    LocalContentAddressedStore,
)
from app.services.execution.changeset import (
    CHANGESET_FORMAT,
    CHANGESET_MEDIA_TYPE,
    ChangeSetError,
    ChangeSetIngestionService,
    IngestChangeSetCommand,
    validate_changeset_path,
    verify_change_set_integrity,
)
from app.services.execution.execution_evidence import (
    ExecutionEvidenceIngestionService,
    IngestExecutionEvidenceCommand,
)
from app.services.planning.operator_review import canonical_json_hash

from test_phase29c6b_runtime_evidence import _owned, _record_command, _start_command
from test_phase29c7b_evidence_validator import _contract as primitive_contract
from test_phase29c7b_evidence_validator import _structured_runtime
from test_phase29c7c_validation_run_acceptance import (
    _rebind_contract,
    _validation_command,
)
from app.services.execution.validation_run import ValidationRunService


def _accepted(db_session, *, ingest_real_content: bool = False, tmp_path=None):
    """Build one fully accepted candidate outcome and, optionally, its own
    real (application/json) candidate content ingested before validation."""

    task, created, outcome, specification = _structured_runtime(db_session)
    real_content = None
    store = None
    if ingest_real_content:
        store = LocalContentAddressedStore(tmp_path)
        real_content = (
            CandidateContentIngestionService(db_session, store=store)
            .ingest(
                IngestCandidateContentCommand(
                    execution_plan_id=task.execution_plan_id,
                    execution_task_id=task.id,
                    execution_task_attempt_id=created.attempt.id,
                    attempt_generation=created.attempt.attempt_generation,
                    candidate_outcome_id=outcome.id,
                    content=b"candidate",
                    media_type="text/plain",
                    ingestion_idempotency_key=f"real-content-{task.id}",
                )
            )
            .content
        )
        db_session.commit()
    contract = primitive_contract("output_reference_exists")
    _rebind_contract(db_session, task, specification, contract)
    db_session.commit()
    service = ValidationRunService(db_session, content_store=store)
    service.execute_validation_run(_validation_command(task, outcome, specification))
    db_session.commit()
    decision = db_session.query(ExecutionTaskAcceptanceDecision).one()
    return task, created, outcome, decision, real_content


def _fabricate_changeset_source_content(
    db_session, task, created, outcome, *, payload: dict, store, key: str
) -> ExecutionTaskCandidateContent:
    """Directly construct a candidate-content row bearing the ChangeSet media
    type.  The live runtime cannot produce this media type yet (Phase 29C-9
    accepts only application/json, text/plain, and application/octet-stream),
    so this fixture simulates the future producer without broadening C9."""

    encoded = json.dumps(payload).encode("utf-8")
    stored = store.put(encoded)
    metadata_payload = {
        "schema_version": "execution-task-candidate-content/1.0",
        "candidate_content_id": None,
        "execution_plan_id": task.execution_plan_id,
        "execution_task_id": task.id,
        "execution_task_attempt_id": created.attempt.id,
        "attempt_generation": created.attempt.attempt_generation,
        "candidate_outcome_id": outcome.id,
        "content_sha256": stored.content_sha256,
        "declared_sha256": None,
        "byte_length": stored.byte_length,
        "media_type": CHANGESET_MEDIA_TYPE,
        "storage_backend_id": stored.backend_id,
        "storage_backend_version": stored.backend_version,
        "storage_key": stored.storage_key,
        "content_projection_hash": None,
        "content_projection_version": None,
    }
    ingestion_payload = {
        "schema_version": "execution-task-candidate-content/1.0",
        "execution_plan_id": task.execution_plan_id,
        "execution_task_id": task.id,
        "execution_task_attempt_id": created.attempt.id,
        "attempt_generation": created.attempt.attempt_generation,
        "candidate_outcome_id": outcome.id,
        "content_sha256": stored.content_sha256,
        "declared_sha256": None,
        "byte_length": stored.byte_length,
        "media_type": CHANGESET_MEDIA_TYPE,
        "ingestion_idempotency_key": key,
        "creation_actor_type": "future-runtime",
        "creation_actor_id": "future-runtime",
    }
    row = ExecutionTaskCandidateContent(
        execution_plan_id=task.execution_plan_id,
        execution_task_id=task.id,
        execution_task_attempt_id=created.attempt.id,
        attempt_generation=created.attempt.attempt_generation,
        candidate_outcome_id=outcome.id,
        content_sha256=stored.content_sha256,
        declared_sha256=None,
        byte_length=stored.byte_length,
        media_type=CHANGESET_MEDIA_TYPE,
        storage_backend_id=stored.backend_id,
        storage_backend_version=stored.backend_version,
        storage_key=stored.storage_key,
        ingestion_idempotency_key=key,
        canonical_ingestion_command_payload=ingestion_payload,
        canonical_ingestion_command_hash=canonical_json_hash(ingestion_payload),
        canonical_metadata_payload=metadata_payload,
        canonical_metadata_hash=canonical_json_hash(metadata_payload),
        content_projection=None,
        content_projection_hash=None,
        content_projection_version=None,
        creation_actor_type="future-runtime",
        creation_actor_id="future-runtime",
        created_at=datetime.now(timezone.utc),
    )
    db_session.add(row)
    db_session.flush()
    return row


def _bypass_source_integrity(monkeypatch):
    """Stand in for the C9 media-type broadening this phase intentionally
    defers; only the source-content gate is bypassed, never the ChangeSet's
    own re-derived hashes/paths/operations."""

    def _always_verified(db, content_id, *, store=None):
        return CandidateContentIntegrityResult(None, None, True, ())

    monkeypatch.setattr(
        "app.services.execution.changeset.verify_candidate_content_integrity",
        _always_verified,
    )


def _minimal_changeset_payload(project_id: int, *, content_reference: str) -> dict:
    return {
        "format": CHANGESET_FORMAT,
        "base_state": {"project_id": project_id},
        "operations": [
            {
                "operation": "create_file",
                "path": "src/example.py",
                "content_reference": content_reference,
            }
        ],
    }


def _evidence_reference(db_session, task, created, *, key: str, store=None) -> str:
    result = ExecutionEvidenceIngestionService(db_session, store=store).ingest(
        IngestExecutionEvidenceCommand(
            execution_plan_id=task.execution_plan_id,
            execution_task_id=task.id,
            execution_task_attempt_id=created.attempt.id,
            attempt_generation=created.attempt.attempt_generation,
            evidence_kind="command",
            producer_id="command-runner",
            producer_version="1",
            content=b'{"ok":true}',
            media_type="application/json",
            ingestion_idempotency_key=key,
        )
    )
    db_session.commit()
    return f"execution-evidence://{result.evidence.id}"


# ---------------------------------------------------------------------------
# Path safety
# ---------------------------------------------------------------------------


def test_path_validation_accepts_clean_relative_paths():
    assert validate_changeset_path("src/app/example.py") == "src/app/example.py"


@pytest.mark.parametrize(
    "path",
    [
        "",
        "/abs/path",
        "a\\b",
        "http://example.com/x",
        "../escape",
        "a/../b",
        "a//b",
        "a/",
        "~/home",
        "C:/windows",
        "a" * 2000,
    ],
)
def test_path_validation_rejects_unsafe_paths(path):
    with pytest.raises(ChangeSetError) as excinfo:
        validate_changeset_path(path)
    assert excinfo.value.code == "changeset_path_invalid"


@pytest.mark.parametrize("path", [".git/config", ".git", ".orchestrator/state.json"])
def test_path_validation_rejects_protected_paths(path):
    with pytest.raises(ChangeSetError) as excinfo:
        validate_changeset_path(path)
    assert excinfo.value.code == "changeset_path_protected"


# ---------------------------------------------------------------------------
# Structured-source requirement
# ---------------------------------------------------------------------------


def test_ingestion_rejects_wrong_media_type(db_session, tmp_path):
    task, created, outcome, decision, real_content = _accepted(
        db_session, ingest_real_content=True, tmp_path=tmp_path
    )
    service = ChangeSetIngestionService(
        db_session, store=LocalContentAddressedStore(tmp_path)
    )
    command = IngestChangeSetCommand(
        execution_plan_id=task.execution_plan_id,
        execution_task_id=task.id,
        execution_task_attempt_id=created.attempt.id,
        attempt_generation=created.attempt.attempt_generation,
        candidate_outcome_id=outcome.id,
        acceptance_decision_id=decision.id,
        source_candidate_content_id=real_content.id,
        ingestion_idempotency_key="cs-media-type-1",
    )
    with pytest.raises(ChangeSetError) as excinfo:
        service.ingest(command)
    assert excinfo.value.code == "changeset_media_type_unsupported"
    assert db_session.query(ExecutionTaskChangeSet).count() == 0


def test_ingestion_rejects_unaccepted_candidate(db_session, tmp_path, monkeypatch):
    task, created, outcome, decision, _ = _accepted(db_session)
    store = LocalContentAddressedStore(tmp_path)
    payload = _minimal_changeset_payload(
        task.execution_plan.project_id, content_reference="candidate-content://1"
    )
    content = _fabricate_changeset_source_content(
        db_session, task, created, outcome, payload=payload, store=store, key="cs-1"
    )
    _bypass_source_integrity(monkeypatch)
    decision.decision_status = "rejected"
    db_session.flush()
    service = ChangeSetIngestionService(db_session, store=store)
    command = IngestChangeSetCommand(
        execution_plan_id=task.execution_plan_id,
        execution_task_id=task.id,
        execution_task_attempt_id=created.attempt.id,
        attempt_generation=created.attempt.attempt_generation,
        candidate_outcome_id=outcome.id,
        acceptance_decision_id=decision.id,
        source_candidate_content_id=content.id,
        ingestion_idempotency_key="cs-2",
    )
    with pytest.raises(ChangeSetError) as excinfo:
        service.ingest(command)
    assert excinfo.value.code == "changeset_candidate_not_accepted"


# ---------------------------------------------------------------------------
# Positive path: parse, canonicalize, persist, replay
# ---------------------------------------------------------------------------


def test_ingestion_persists_operations_and_is_idempotent(
    db_session, tmp_path, monkeypatch
):
    task, created, outcome, decision, _ = _accepted(db_session)
    store = LocalContentAddressedStore(tmp_path)
    evidence_ref = _evidence_reference(
        db_session, task, created, key="ev-1", store=store
    )
    payload = {
        "format": CHANGESET_FORMAT,
        "base_state": {
            "project_id": task.execution_plan.project_id,
            "workspace_identity": "workspace-sha256:" + "a" * 64,
        },
        "operations": [
            {
                "operation": "create_file",
                "path": "src/new_module.py",
                "content_reference": evidence_ref,
            },
            {
                "operation": "delete_file",
                "path": "obsolete.txt",
                "expected_previous_sha256": "b" * 64,
            },
        ],
    }
    content = _fabricate_changeset_source_content(
        db_session, task, created, outcome, payload=payload, store=store, key="cs-3"
    )
    _bypass_source_integrity(monkeypatch)
    service = ChangeSetIngestionService(db_session, store=store)
    command = IngestChangeSetCommand(
        execution_plan_id=task.execution_plan_id,
        execution_task_id=task.id,
        execution_task_attempt_id=created.attempt.id,
        attempt_generation=created.attempt.attempt_generation,
        candidate_outcome_id=outcome.id,
        acceptance_decision_id=decision.id,
        source_candidate_content_id=content.id,
        ingestion_idempotency_key="cs-changeset-1",
    )
    result = service.ingest(command)
    db_session.commit()

    assert result.replayed is False
    change_set = result.change_set
    assert change_set.operation_count == 2
    assert change_set.changeset_format == CHANGESET_FORMAT
    assert change_set.media_type == CHANGESET_MEDIA_TYPE
    assert change_set.target_project_id == task.execution_plan.project_id
    operations = (
        db_session.query(ExecutionTaskChangeSetOperation)
        .filter_by(change_set_id=change_set.id)
        .order_by(ExecutionTaskChangeSetOperation.operation_index)
        .all()
    )
    assert [item.operation for item in operations] == ["create_file", "delete_file"]
    assert operations[0].content_reference == evidence_ref
    assert operations[1].expected_previous_sha256 == "b" * 64

    integrity = verify_change_set_integrity(db_session, change_set.id, store=store)
    assert integrity.verified is True

    replay = service.ingest(command)
    assert replay.replayed is True
    assert replay.change_set.id == change_set.id
    assert db_session.query(ExecutionTaskChangeSet).count() == 1

    with pytest.raises(ChangeSetError) as excinfo:
        service.ingest(
            IngestChangeSetCommand(
                execution_plan_id=task.execution_plan_id,
                execution_task_id=task.id,
                execution_task_attempt_id=created.attempt.id,
                attempt_generation=created.attempt.attempt_generation,
                candidate_outcome_id=outcome.id,
                acceptance_decision_id=decision.id,
                source_candidate_content_id=content.id,
                ingestion_idempotency_key="cs-changeset-1",
                creation_actor_id="different-actor",
            )
        )
    assert excinfo.value.code == "changeset_idempotency_conflict"


def test_ingestion_rejects_duplicate_operation_paths(db_session, tmp_path, monkeypatch):
    task, created, outcome, decision, _ = _accepted(db_session)
    store = LocalContentAddressedStore(tmp_path)
    evidence_ref = _evidence_reference(
        db_session, task, created, key="ev-2", store=store
    )
    payload = {
        "format": CHANGESET_FORMAT,
        "base_state": {"project_id": task.execution_plan.project_id},
        "operations": [
            {
                "operation": "create_file",
                "path": "src/dup.py",
                "content_reference": evidence_ref,
            },
            {
                "operation": "delete_file",
                "path": "src/dup.py",
                "expected_previous_sha256": "c" * 64,
            },
        ],
    }
    content = _fabricate_changeset_source_content(
        db_session, task, created, outcome, payload=payload, store=store, key="cs-4"
    )
    _bypass_source_integrity(monkeypatch)
    service = ChangeSetIngestionService(db_session, store=store)
    command = IngestChangeSetCommand(
        execution_plan_id=task.execution_plan_id,
        execution_task_id=task.id,
        execution_task_attempt_id=created.attempt.id,
        attempt_generation=created.attempt.attempt_generation,
        candidate_outcome_id=outcome.id,
        acceptance_decision_id=decision.id,
        source_candidate_content_id=content.id,
        ingestion_idempotency_key="cs-dup-1",
    )
    with pytest.raises(ChangeSetError) as excinfo:
        service.ingest(command)
    assert excinfo.value.code == "changeset_operation_duplicate_path"


def test_ingestion_rejects_unsupported_format_and_base_state_mismatch(
    db_session, tmp_path, monkeypatch
):
    task, created, outcome, decision, _ = _accepted(db_session)
    store = LocalContentAddressedStore(tmp_path)
    evidence_ref = _evidence_reference(
        db_session, task, created, key="ev-3", store=store
    )
    bad_format_payload = {
        "format": "orchestrator-changeset/2",
        "base_state": {"project_id": task.execution_plan.project_id},
        "operations": [
            {
                "operation": "create_file",
                "path": "a.py",
                "content_reference": evidence_ref,
            }
        ],
    }
    content = _fabricate_changeset_source_content(
        db_session,
        task,
        created,
        outcome,
        payload=bad_format_payload,
        store=store,
        key="cs-5",
    )
    _bypass_source_integrity(monkeypatch)
    service = ChangeSetIngestionService(db_session, store=store)
    command = IngestChangeSetCommand(
        execution_plan_id=task.execution_plan_id,
        execution_task_id=task.id,
        execution_task_attempt_id=created.attempt.id,
        attempt_generation=created.attempt.attempt_generation,
        candidate_outcome_id=outcome.id,
        acceptance_decision_id=decision.id,
        source_candidate_content_id=content.id,
        ingestion_idempotency_key="cs-format-1",
    )
    with pytest.raises(ChangeSetError) as excinfo:
        service.ingest(command)
    assert excinfo.value.code == "changeset_format_unsupported"


def test_ingestion_rejects_cross_project_base_state(db_session, tmp_path, monkeypatch):
    task, created, outcome, decision, _ = _accepted(db_session)
    store = LocalContentAddressedStore(tmp_path)
    evidence_ref = _evidence_reference(
        db_session, task, created, key="ev-4", store=store
    )
    payload = _minimal_changeset_payload(
        task.execution_plan.project_id + 999, content_reference=evidence_ref
    )
    content = _fabricate_changeset_source_content(
        db_session, task, created, outcome, payload=payload, store=store, key="cs-6"
    )
    _bypass_source_integrity(monkeypatch)
    service = ChangeSetIngestionService(db_session, store=store)
    command = IngestChangeSetCommand(
        execution_plan_id=task.execution_plan_id,
        execution_task_id=task.id,
        execution_task_attempt_id=created.attempt.id,
        attempt_generation=created.attempt.attempt_generation,
        candidate_outcome_id=outcome.id,
        acceptance_decision_id=decision.id,
        source_candidate_content_id=content.id,
        ingestion_idempotency_key="cs-project-1",
    )
    with pytest.raises(ChangeSetError) as excinfo:
        service.ingest(command)
    assert excinfo.value.code == "changeset_base_state_project_mismatch"


# ---------------------------------------------------------------------------
# Controlled Apply authorization policy
# ---------------------------------------------------------------------------


def _authorized_changeset(db_session, tmp_path, monkeypatch):
    task, created, outcome, decision, _ = _accepted(db_session)
    store = LocalContentAddressedStore(tmp_path)
    evidence_ref = _evidence_reference(
        db_session, task, created, key="ev-apply-1", store=store
    )
    payload = _minimal_changeset_payload(
        task.execution_plan.project_id, content_reference=evidence_ref
    )
    content = _fabricate_changeset_source_content(
        db_session,
        task,
        created,
        outcome,
        payload=payload,
        store=store,
        key="cs-apply-1",
    )
    _bypass_source_integrity(monkeypatch)
    service = ChangeSetIngestionService(db_session, store=store)
    result = service.ingest(
        IngestChangeSetCommand(
            execution_plan_id=task.execution_plan_id,
            execution_task_id=task.id,
            execution_task_attempt_id=created.attempt.id,
            attempt_generation=created.attempt.attempt_generation,
            candidate_outcome_id=outcome.id,
            acceptance_decision_id=decision.id,
            source_candidate_content_id=content.id,
            ingestion_idempotency_key="cs-apply-changeset-1",
        )
    )
    db_session.commit()
    return task, result.change_set, store


def test_apply_policy_blocks_on_missing_base_state_authority(
    db_session, tmp_path, monkeypatch
):
    task, change_set, store = _authorized_changeset(db_session, tmp_path, monkeypatch)
    decision = evaluate_apply_policy(db_session, change_set, store=store)
    assert decision.status == "blocked"
    assert decision.reason == BASE_STATE_AUTHORITY_UNAVAILABLE_REASON


def test_apply_policy_denies_on_superseded_plan(db_session, tmp_path, monkeypatch):
    task, change_set, store = _authorized_changeset(db_session, tmp_path, monkeypatch)
    plan = db_session.get(ExecutionPlan, change_set.execution_plan_id)
    other = ExecutionPlan(
        project_id=plan.project_id,
        planning_session_id=plan.planning_session_id,
        planning_commit_manifest_id=(
            plan.planning_commit_manifest_id + 1
            if plan.planning_commit_manifest_id
            else None
        ),
        generation=plan.generation + 1,
        protocol_version="v2",
        source_commit_identity=plan.source_commit_identity,
        source_plan_checkpoint_id=plan.source_plan_checkpoint_id,
        source_plan_hash=plan.source_plan_hash,
        status="active",
    )
    plan.status = "superseded"
    db_session.flush()
    decision = evaluate_apply_policy(db_session, change_set, store=store)
    assert decision.status == "denied"
    assert decision.reason == "superseded_plan"


def test_authorization_service_persists_replays_and_conflicts(
    db_session, tmp_path, monkeypatch
):
    task, change_set, store = _authorized_changeset(db_session, tmp_path, monkeypatch)
    service = ApplyAuthorizationService(db_session, store=store)
    command = AuthorizeApplyCommand(
        change_set_id=change_set.id,
        authorization_idempotency_key="auth-1",
    )
    first = service.authorize(command)
    db_session.commit()
    assert first.replayed is False
    assert first.authorization.authorization_status == "blocked"
    assert first.authorization.apply_policy_id == APPLY_POLICY_ID
    assert first.authorization.apply_policy_version == APPLY_POLICY_VERSION
    assert db_session.query(ExecutionTaskApplyAuthorization).count() == 1

    replay = service.authorize(command)
    assert replay.replayed is True
    assert replay.authorization.id == first.authorization.id

    with pytest.raises(ApplyAuthorizationError) as excinfo:
        service.authorize(
            AuthorizeApplyCommand(
                change_set_id=change_set.id,
                authorization_idempotency_key="auth-2",
            )
        )
    assert excinfo.value.code == "apply_authorization_conflict"

    integrity = verify_apply_authorization_integrity(
        db_session, first.authorization.id, store=store
    )
    assert integrity.verified is True

    first.authorization.canonical_decision_hash = "tampered"
    db_session.flush()
    tampered = verify_apply_authorization_integrity(
        db_session, first.authorization.id, store=store
    )
    assert tampered.verified is False
    assert "apply_authorization_decision_hash_mismatch" in tampered.issues


# ---------------------------------------------------------------------------
# Migration
# ---------------------------------------------------------------------------


def test_migration_046_is_additive_replay_safe_and_empty(tmp_path):
    from sqlalchemy import create_engine, text

    engine = create_engine(f"sqlite:///{tmp_path / 'phase29d1.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        for table in (
            "execution_task_apply_authorizations",
            "execution_task_change_set_operations",
            "execution_task_change_sets",
        ):
            connection.execute(text(f"DROP TABLE {table}"))
    pre_phase = tuple(
        migration
        for migration in MIGRATIONS
        if migration.version < "046_execution_task_changeset_apply_authorization"
    )
    run_schema_migrations(engine, pre_phase)
    run_schema_migrations(engine, MIGRATIONS)
    run_schema_migrations(engine, MIGRATIONS)

    inspector = inspect(engine)
    table_names = set(inspector.get_table_names())
    for table in (
        "execution_task_change_sets",
        "execution_task_change_set_operations",
        "execution_task_apply_authorizations",
    ):
        assert table in table_names

    with engine.connect() as connection:
        for table in (
            "execution_task_change_sets",
            "execution_task_change_set_operations",
            "execution_task_apply_authorizations",
        ):
            count = connection.execute(text(f"SELECT COUNT(*) FROM {table}")).scalar()
            assert count == 0
    engine.dispose()
