"""Focused persistence tests for Phase 28B Protocol v2 infrastructure."""

from __future__ import annotations

from pathlib import Path
import uuid

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.exc import IntegrityError

from app.db_migrations import run_schema_migrations
from app.models import PlanningSession, Project
from app.services.planning.protocol_persistence import (
    PlanningProtocolPersistenceService,
    ProtocolOwnershipError,
    ProtocolPersistenceError,
)


def _seed_owned_session(db, *, protocol_version: str = "v2"):
    project = Project(
        name=f"Protocol persistence {uuid.uuid4().hex[:8]}",
        workspace_path=f"protocol-persistence-{uuid.uuid4().hex[:8]}",
    )
    db.add(project)
    db.flush()
    session = PlanningSession(
        project_id=project.id,
        title="Protocol persistence test",
        prompt="Persist a bounded protocol state foundation.",
        status="active",
        protocol_version=protocol_version,
        generation_id=str(uuid.uuid4()),
        processing_token="fence-1",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return project, session


def _identity_kwargs(session):
    return {
        "planning_input": "Persist a bounded protocol state foundation.",
        "engineering_context_identity": "context-object-abc",
        "provider_identity": "local_openclaw",
        "model_configuration": {
            "planner_model": "qwen3.6:27B",
            "reasoning_profile": "planning_default",
            "configuration_fingerprint": "a" * 64,
        },
        "repository_identity": "path:/tmp/protocol-repository",
        "session_generation_id": session.generation_id,
    }


def test_current_planning_sessions_are_explicitly_protocol_v1(db_session, monkeypatch):
    from app.services.planning.planning_session_service import PlanningSessionService

    project = Project(name="Legacy protocol", workspace_path="legacy-protocol")
    db_session.add(project)
    db_session.flush()
    service = PlanningSessionService(db_session)
    monkeypatch.setattr(service, "schedule_processing", lambda *_args: None)

    session = service.start_session(
        project,
        "Keep current Planning behavior while recording protocol identity.",
        skip_clarification=True,
    )

    assert session.protocol_version == "v1"


def test_input_identity_is_non_secret_and_immutable(db_session):
    _, session = _seed_owned_session(db_session)
    service = PlanningProtocolPersistenceService(db_session)

    record = service.record_input_identity(
        session.id,
        **_identity_kwargs(session),
    )
    db_session.commit()
    assert record.input_hash
    assert record.model_configuration == {
        "planner_model": "qwen3.6:27B",
        "reasoning_profile": "planning_default",
        "configuration_fingerprint": "a" * 64,
    }

    same = service.record_input_identity(session.id, **_identity_kwargs(session))
    assert same.id == record.id
    with pytest.raises(ProtocolPersistenceError, match="immutable"):
        service.record_input_identity(
            session.id,
            **{
                **_identity_kwargs(session),
                "planning_input": "A changed planning input.",
            },
        )
    with pytest.raises(ProtocolPersistenceError, match="secret"):
        service.record_input_identity(
            session.id,
            **{
                **_identity_kwargs(session),
                "model_configuration": {"api_key": "must-not-persist"},
            },
        )


def test_checkpoint_dependencies_statuses_and_owner_fence(db_session):
    _, session = _seed_owned_session(db_session)
    service = PlanningProtocolPersistenceService(db_session)

    accepted = service.record_checkpoint(
        session.id,
        stage_name="brief",
        content="accepted brief",
        stage_generation_id="stage-1",
        attempt_id="attempt-1",
        fencing_token="fence-1",
        session_generation_id=session.generation_id,
        status="accepted",
    )
    failed = service.record_checkpoint(
        session.id,
        stage_name="brief",
        content="failed brief",
        stage_generation_id="stage-1",
        attempt_id="attempt-2",
        fencing_token="fence-1",
        session_generation_id=session.generation_id,
        status="failed",
        failure_reason="provider returned invalid output",
    )
    invalidated = service.record_checkpoint(
        session.id,
        stage_name="brief",
        content="invalidated brief",
        stage_generation_id="stage-1",
        attempt_id="attempt-3",
        fencing_token="fence-1",
        session_generation_id=session.generation_id,
        status="invalidated",
    )
    child = service.record_checkpoint(
        session.id,
        stage_name="task_plan",
        content="task plan",
        stage_generation_id="stage-2",
        attempt_id="attempt-1",
        fencing_token="fence-1",
        session_generation_id=session.generation_id,
        parent_checkpoint_ids=[accepted.id],
    )
    db_session.commit()

    assert accepted.accepted_at is not None
    assert failed.accepted_at is None
    assert failed.failure_reason == "provider returned invalid output"
    assert invalidated.invalidated_at is not None
    assert child.content_hash
    assert [edge.parent_checkpoint_id for edge in child.dependencies] == [accepted.id]

    with pytest.raises(ProtocolOwnershipError, match="fencing token"):
        service.record_checkpoint(
            session.id,
            stage_name="stale_stage",
            content="must not write",
            stage_generation_id="stage-stale",
            attempt_id="attempt-stale",
            fencing_token="old-fence",
            session_generation_id=session.generation_id,
        )


def test_checkpoint_attempt_uniqueness_is_concurrency_guard(db_session):
    _, session = _seed_owned_session(db_session)
    service = PlanningProtocolPersistenceService(db_session)
    kwargs = {
        "session_id": session.id,
        "stage_name": "brief",
        "content": "same attempt",
        "stage_generation_id": "stage-1",
        "attempt_id": "attempt-1",
        "fencing_token": "fence-1",
        "session_generation_id": session.generation_id,
    }
    service.record_checkpoint(**kwargs)
    db_session.commit()
    with pytest.raises(IntegrityError):
        service.record_checkpoint(**kwargs)
        db_session.flush()
    db_session.rollback()


def test_completion_and_commit_manifests_record_provenance(db_session):
    _, session = _seed_owned_session(db_session)
    service = PlanningProtocolPersistenceService(db_session)
    checkpoint = service.record_checkpoint(
        session.id,
        stage_name="task_plan",
        content="accepted task plan",
        stage_generation_id="stage-1",
        attempt_id="attempt-1",
        fencing_token="fence-1",
        session_generation_id=session.generation_id,
    )
    completion = service.record_completion_manifest(
        session.id,
        accepted_checkpoint_versions=[{"checkpoint_id": checkpoint.id}],
        dependency_hashes=["b" * 64, "a" * 64],
        fencing_token="fence-1",
        session_generation_id=session.generation_id,
    )
    commit = service.record_commit_manifest(
        session.id,
        completion_manifest_id=completion.id,
        task_provenance={"task_ids": [7], "source": "protocol-v2-plan"},
        fencing_token="fence-1",
        session_generation_id=session.generation_id,
    )
    db_session.commit()

    assert completion.protocol_version == "v2"
    assert (
        completion.accepted_checkpoint_versions[0]["content_hash"]
        == checkpoint.content_hash
    )
    assert completion.dependency_hashes == ["a" * 64, "b" * 64]
    assert commit.commit_identity
    assert commit.task_provenance["task_ids"] == [7]
    assert (
        service.record_commit_manifest(
            session.id,
            completion_manifest_id=completion.id,
            task_provenance={"task_ids": [7], "source": "protocol-v2-plan"},
            commit_identity=commit.commit_identity,
            fencing_token="fence-1",
            session_generation_id=session.generation_id,
        ).id
        == commit.id
    )
    with pytest.raises(ProtocolPersistenceError, match="immutable"):
        service.record_commit_manifest(
            session.id,
            completion_manifest_id=completion.id,
            task_provenance={"task_ids": [8], "source": "protocol-v2-plan"},
            commit_identity=commit.commit_identity,
            fencing_token="fence-1",
            session_generation_id=session.generation_id,
        )


def test_protocol_v2_migration_preserves_legacy_session_and_is_idempotent(
    tmp_path: Path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'phase28b-legacy.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE projects (id INTEGER PRIMARY KEY, name VARCHAR(255) NOT NULL, "
                "description TEXT, created_at DATETIME, updated_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE tasks (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, "
                "title VARCHAR(255) NOT NULL, description TEXT, status VARCHAR(50), "
                "priority INTEGER, steps TEXT, current_step INTEGER, error_message TEXT, "
                "created_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE sessions (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, "
                "name VARCHAR(255) NOT NULL, status VARCHAR(50), is_active BOOLEAN, "
                "created_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE log_entries (id INTEGER PRIMARY KEY, session_id INTEGER, "
                "task_id INTEGER, level VARCHAR(50), message TEXT, metadata TEXT, created_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "CREATE TABLE planning_sessions (id INTEGER PRIMARY KEY, project_id INTEGER NOT NULL, "
                "title VARCHAR(255) NOT NULL, prompt TEXT NOT NULL, status VARCHAR(50) NOT NULL, "
                "source_brain VARCHAR(50) NOT NULL, current_prompt_id VARCHAR(64), "
                "processing_token VARCHAR(64), processing_started_at DATETIME, "
                "finalized_plan_id INTEGER, committed_at DATETIME, committed_task_ids TEXT, "
                "last_error TEXT, completed_at DATETIME, created_at DATETIME, updated_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO planning_sessions "
                "(id, project_id, title, prompt, status, source_brain) "
                "VALUES (1, 1, 'legacy', 'legacy prompt', 'completed', 'local')"
            )
        )

    run_schema_migrations(engine)
    run_schema_migrations(engine)
    inspector = inspect(engine)
    assert "protocol_version" in {
        column["name"] for column in inspector.get_columns("planning_sessions")
    }
    assert {
        "planning_protocol_inputs",
        "planning_checkpoints",
        "planning_checkpoint_dependencies",
        "planning_completion_manifests",
        "planning_commit_manifests",
        "planning_review_events",
    } <= set(inspector.get_table_names())
    with engine.connect() as connection:
        row = connection.execute(
            text("SELECT protocol_version FROM planning_sessions WHERE id = 1")
        ).scalar_one()
        applied = connection.execute(
            text(
                "SELECT COUNT(*) FROM schema_migrations "
                "WHERE version = '027_protocol_v2_persistence'"
            )
        ).scalar_one()
    assert row == "v1"
    assert applied == 1
    engine.dispose()
