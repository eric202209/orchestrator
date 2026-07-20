"""Focused Protocol v2 Input Manifest tests."""

from __future__ import annotations

from dataclasses import replace
import json
import uuid

import pytest
from sqlalchemy import create_engine, text

from app.db_migrations import _migration_028_protocol_v2_input_manifest
from app.models import PlanningSession, Project
from app.services.orchestration.stage_engine import (
    StageDefinition,
    StageExecutor,
    StageStatus,
)
from app.services.planning.input_manifest import (
    InputManifestBuilder,
    InputManifestValidationError,
    build_input_manifest,
    canonical_json_hash,
    validate_input_manifest,
)
from app.services.planning.planning_session_service import PlanningSessionService
from app.services.planning.protocol_persistence import (
    PlanningProtocolPersistenceService,
    ProtocolPersistenceError,
)


def _build_manifest(**overrides):
    values = {
        "session_id": 1,
        "session_generation_id": "generation-1",
        "planning_request": {
            "message_id": 42,
            "content": "Implement the accepted manifest.",
        },
        "clarification_messages": [
            {"id": 7, "role": "assistant", "content": "Which boundary matters?"},
            {"id": 8, "role": "user", "content": "Only Protocol v2."},
        ],
        "project_metadata": {
            "project_id": 9,
            "name": "Manifest project",
            "description": "A bounded test project",
        },
        "project_rules": "Preserve Protocol v1.",
        "repository": {
            "available": True,
            "identity": "https://example.test/repository",
            "workspace": "/workspace/project",
            "revision": "a" * 40,
            "dirty": False,
        },
        "engineering_context": {
            "object_id": "context:project-log-authorization:1",
            "subsystem_version": 1,
            "content_hash": "b" * 64,
            "repository_revision": "a" * 40,
            "freshness": "fresh",
            "selection_reason": "fresh_published_object",
        },
        "structural_information": {
            "object_id": "structural:context:1",
            "schema_version": 1,
            "algorithm_version": 1,
            "content_hash": "c" * 64,
            "freshness": "fresh",
        },
        "runtime_configuration": {
            "provider": "local",
            "backend": "stub_success",
            "model": "test-model",
            "reasoning_profile": "default",
        },
        "stage_configuration": {
            "stages": [{"identifier": "brief", "version": 1, "prerequisites": []}]
        },
        "selection_timestamps": {
            "engineering_context": "2026-07-20T17:00:00+00:00",
            "structural_information": "2026-07-20T17:00:00+00:00",
        },
        "manifest_built_at": "2026-07-20T17:01:00+00:00",
    }
    values.update(overrides)
    return build_input_manifest(**values)


def _seed_session(db_session, *, protocol_version: str = "v2"):
    project = Project(
        name=f"Manifest test {uuid.uuid4().hex[:8]}",
        workspace_path=f"manifest-test-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(project)
    db_session.flush()
    session = PlanningSession(
        project_id=project.id,
        title="Manifest test",
        prompt="Persist the canonical input manifest.",
        status="active",
        protocol_version=protocol_version,
        generation_id=str(uuid.uuid4()),
        processing_token="manifest-fence",
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)
    return project, session


def test_canonical_serialization_and_hash_are_deterministic_and_immutable():
    first = _build_manifest()
    second = _build_manifest()

    assert first.canonical_bytes() == second.canonical_bytes()
    assert first.manifest_id == second.manifest_id
    assert first.manifest_hash == second.manifest_hash
    assert json.loads(first.canonical_json()) == json.loads(second.canonical_json())
    with pytest.raises(AttributeError):
        first.manifest_hash = "f" * 64


def test_source_inventory_has_stable_ids_order_and_freshness_identity():
    manifest = _build_manifest()
    assert [source.ordinal for source in manifest.sources] == list(
        range(1, len(manifest.sources) + 1)
    )
    assert len({source.source_id for source in manifest.sources}) == len(
        manifest.sources
    )
    assert manifest.sources[0].source_type == "planning_request"
    assert manifest.sources[-2].source_type == "runtime_configuration"
    assert manifest.sources[-1].omission_reason == "not_replanning"
    assert manifest.repository_identity.revision == "a" * 40
    assert manifest.engineering_context_identity.freshness == "fresh"
    assert manifest.structural_information_identity.freshness == "fresh"
    assert manifest.freshness.manifest_built_at.endswith("+00:00")
    assert manifest.configuration_identity.stage_configuration_fingerprint


def test_redaction_is_typed_secret_free_and_hash_stable():
    one = _build_manifest(
        planning_request={
            "message_id": 42,
            "content": "api_key=first-secret-value",
        }
    )
    two = _build_manifest(
        planning_request={
            "message_id": 42,
            "content": "api_key=second-secret-value",
        }
    )

    assert one.manifest_hash == two.manifest_hash
    serialized = one.canonical_json()
    assert "first-secret-value" not in serialized
    assert "credential_shape" in serialized
    assert one.redaction.redacted_source_count >= 1


def test_manifest_validation_rejects_unsupported_schema_and_secret_leakage():
    manifest = _build_manifest()
    unsupported = replace(manifest, schema_version="protocol-v2-input-manifest/99.0")
    with pytest.raises(InputManifestValidationError, match="unsupported"):
        validate_input_manifest(unsupported)

    source = manifest.sources[0]
    leaked_source = replace(
        source,
        content="password=unredacted-secret",
        content_hash=canonical_json_hash("password=unredacted-secret"),
        redaction_state="none",
        redaction_classes=(),
    )
    leaked = replace(manifest, sources=(leaked_source, *manifest.sources[1:]))
    leaked = replace(
        leaked,
        manifest_hash=canonical_json_hash(leaked._canonical_payload()),
    )
    with pytest.raises(InputManifestValidationError, match="secret leakage"):
        validate_input_manifest(leaked)


def test_persistence_and_recovery_reload_the_complete_manifest(db_session):
    _, session = _seed_session(db_session)
    manifest = _build_manifest(
        session_id=session.id, session_generation_id=session.generation_id
    )
    persistence = PlanningProtocolPersistenceService(db_session)
    record = persistence.record_input_manifest(session.id, manifest=manifest)
    db_session.commit()

    loaded = persistence.load_input_manifest(session.id)
    assert loaded is not None
    assert loaded.manifest_hash == manifest.manifest_hash
    assert record.manifest_json["manifest_hash"] == manifest.manifest_hash
    assert (
        persistence.recovery_state(session.id)["input_manifest"].manifest_hash
        == manifest.manifest_hash
    )

    with pytest.raises(ProtocolPersistenceError, match="immutable"):
        persistence.record_input_manifest(
            session.id,
            manifest=_build_manifest(
                session_id=session.id,
                session_generation_id=session.generation_id,
                planning_request={"message_id": 42, "content": "changed"},
            ),
        )


def test_stage_context_exposes_persisted_manifest(db_session):
    _, session = _seed_session(db_session)
    manifest = _build_manifest(
        session_id=session.id, session_generation_id=session.generation_id
    )
    persistence = PlanningProtocolPersistenceService(db_session)
    persistence.record_input_manifest(session.id, manifest=manifest)
    db_session.commit()
    observed = {}

    engine = StageExecutor(
        db_session,
        [
            StageDefinition(
                "manifest-consumer",
                execute=lambda context: observed.setdefault(
                    "hash", context.input_manifest.manifest_hash
                ),
            )
        ],
    )
    result = engine.advance(
        session.id,
        session_generation_id=session.generation_id,
        fencing_token="manifest-fence",
    )
    assert result.status == StageStatus.COMPLETED
    assert observed["hash"] == manifest.manifest_hash


def test_protocol_v2_start_persists_full_manifest_and_v1_stays_legacy(
    db_session, monkeypatch
):
    project = Project(
        name="Protocol compatibility",
        workspace_path=f"protocol-compat-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(project)
    db_session.flush()
    service = PlanningSessionService(db_session)
    monkeypatch.setattr(service, "schedule_processing", lambda *_args: None)

    v2 = service.start_session(
        project,
        "Create a Protocol v2 manifest.",
        skip_clarification=True,
        protocol_version="v2",
    )
    assert v2.protocol_input is not None
    assert v2.protocol_input.manifest_json is not None
    assert v2.protocol_input.manifest_hash == v2.protocol_input.input_hash
    assert {
        source["source_type"] for source in v2.protocol_input.manifest_json["sources"]
    } >= {"planning_request", "project_metadata", "runtime_configuration"}

    v1_project = Project(name="Protocol v1 compatibility")
    db_session.add(v1_project)
    db_session.flush()
    v1 = service.start_session(
        v1_project,
        "Keep the legacy protocol unchanged.",
        skip_clarification=True,
    )
    assert v1.protocol_version == "v1"
    assert v1.protocol_input is None


def test_compatibility_adapter_is_deterministic():
    kwargs = {
        "session_id": 1,
        "session_generation_id": "generation-1",
        "planning_input_hash": "a" * 64,
        "engineering_context_identity": "context-1",
        "provider_identity": "local",
        "model_configuration": {
            "planner_model": "model",
            "reasoning_profile": "default",
            "configuration_fingerprint": "b" * 64,
        },
        "repository_identity": "repository-1",
    }
    first = InputManifestBuilder.from_compatibility_identity(**kwargs)
    second = InputManifestBuilder.from_compatibility_identity(**kwargs)
    assert first.manifest_hash == second.manifest_hash
    assert first.freshness.manifest_built_at.startswith("1970-")


def test_phase28b_rows_are_backfilled_without_live_state_reconstruction(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'coarse-input.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE planning_protocol_inputs ("
                "id INTEGER PRIMARY KEY, planning_session_id INTEGER NOT NULL, "
                "protocol_version VARCHAR(16) NOT NULL, session_generation_id VARCHAR(36) NOT NULL, "
                "input_hash VARCHAR(64) NOT NULL, engineering_context_identity VARCHAR(512) NOT NULL, "
                "provider_identity VARCHAR(255) NOT NULL, model_configuration JSON NOT NULL, "
                "repository_identity VARCHAR(512) NOT NULL)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO planning_protocol_inputs "
                "(id, planning_session_id, protocol_version, session_generation_id, input_hash, "
                "engineering_context_identity, provider_identity, model_configuration, repository_identity) "
                "VALUES (1, 11, 'v2', 'generation-11', :input_hash, 'context-11', 'provider-11', "
                ":configuration, 'repository-11')"
            ),
            {
                "input_hash": "d" * 64,
                "configuration": json.dumps(
                    {
                        "planner_model": "model-11",
                        "reasoning_profile": "default",
                        "configuration_fingerprint": "e" * 64,
                    }
                ),
            },
        )

    _migration_028_protocol_v2_input_manifest(engine)
    with engine.connect() as connection:
        row = (
            connection.execute(
                text(
                    "SELECT input_hash, manifest_hash, manifest_id, manifest_json "
                    "FROM planning_protocol_inputs WHERE id = 1"
                )
            )
            .mappings()
            .one()
        )
    assert row["manifest_id"].startswith("manifest:")
    assert row["manifest_hash"] == row["input_hash"]
    assert json.loads(row["manifest_json"])["manifest_hash"] == row["manifest_hash"]
    engine.dispose()
