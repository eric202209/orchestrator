"""Provider-free regression coverage for the Phase 39 F1a foundation."""

from __future__ import annotations

import ast
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import hashlib
import importlib
import io
from pathlib import Path
import re
import sqlite3
import tarfile
import uuid

import pytest
from sqlalchemy import create_engine, event, inspect
from sqlalchemy.exc import IntegrityError, SQLAlchemyError
from sqlalchemy.schema import CreateTable
from sqlalchemy.orm import sessionmaker

from app.config import Settings, settings
from app.db_migrations import MIGRATIONS, run_schema_migrations
from app.models import (
    Base,
    ExecutorLaunchIntent,
    OneShotExecutionAuthorization,
    Project,
    Session as SessionModel,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.supervisor.oci_executor import SUPPORTED_SCOPE_SHA256
from app.services.execution.one_shot_authorization import (
    IDENTITY_SCHEMA_VERSION,
    POLICY_VERSION,
    SUPPORTED_PROFILE_ID,
    SUPPORTED_PROFILE_VERSION,
    AmbiguousExecutionMapping,
    AuthorizationAlreadyExists,
    AuthorizationAssociationMismatch,
    AuthorizationIntegrityError,
    AuthorizationStateConflict,
    InvalidAuthorizationIdentity,
    OneShotAuthorizationIdentity,
    OneShotFeatureDisabled,
    UnsupportedExecutionProfile,
    canonical_identity_bytes,
    canonical_identity_sha256,
    inspect_authorization,
    issue_one_shot_authorization,
    revoke_authorization,
)

CANDIDATE_FIXTURE_BYTES = b"print('candidate executed')\n"
_SNAPSHOT_BUFFER = io.BytesIO()
with tarfile.open(fileobj=_SNAPSHOT_BUFFER, mode="w") as _snapshot_archive:
    _snapshot_data = b"disposable source snapshot\n"
    _snapshot_info = tarfile.TarInfo("README.md")
    _snapshot_info.size = len(_snapshot_data)
    _snapshot_archive.addfile(_snapshot_info, io.BytesIO(_snapshot_data))
SOURCE_SNAPSHOT_FIXTURE_BYTES = _SNAPSHOT_BUFFER.getvalue()


MIGRATION_057 = next(
    migration
    for migration in MIGRATIONS
    if migration.version == "057_one_shot_execution_authorization"
)


def _sqlite_engine(path: Path):
    """Temporary SQLite engine with production's foreign-key enforcement."""

    engine = create_engine(f"sqlite:///{path}")

    @event.listens_for(engine, "connect")
    def _enforce_foreign_keys(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    return engine


@pytest.fixture
def f1a_database(tmp_path: Path):
    engine = _sqlite_engine(tmp_path / "f1a.db")
    Base.metadata.create_all(bind=engine)
    run_schema_migrations(engine, (MIGRATION_057,))
    factory = sessionmaker(autoflush=False, bind=engine)
    try:
        yield engine, factory
    finally:
        engine.dispose()


def _graph(db, *, execution_profile: str = "executor-contained-planning/v1"):
    project = Project(name=f"F1a Project {uuid.uuid4()}")
    db.add(project)
    db.flush()
    session = SessionModel(
        project_id=project.id,
        name=f"F1a Session {uuid.uuid4()}",
        status="pending",
        instance_id=str(uuid.uuid4()),
    )
    task = Task(
        project_id=project.id,
        title="F1a authorization fixture",
        status=TaskStatus.PENDING,
        execution_profile=execution_profile,
    )
    db.add_all([session, task])
    db.flush()
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.PENDING,
    )
    db.add(execution)
    db.commit()
    return project, session, task, execution


def _identity(project, session, task, execution, **overrides):
    issued_at = datetime.now(timezone.utc).replace(microsecond=0)
    values = {
        "identity_schema_version": IDENTITY_SCHEMA_VERSION,
        "policy_version": POLICY_VERSION,
        "authorization_uuid": str(uuid.uuid4()),
        "project_id": project.id,
        "session_id": session.id,
        "session_instance_id": session.instance_id,
        "task_id": task.id,
        "task_execution_id": execution.id,
        "source_revision_identity": "git:9d5a1c0e",
        "source_snapshot_digest": hashlib.sha256(
            SOURCE_SNAPSHOT_FIXTURE_BYTES
        ).hexdigest(),
        "candidate_uuid": str(uuid.uuid4()),
        "candidate_initial_digest": hashlib.sha256(CANDIDATE_FIXTURE_BYTES).hexdigest(),
        "execution_profile_id": SUPPORTED_PROFILE_ID,
        "execution_profile_version": SUPPORTED_PROFILE_VERSION,
        "operation_scope_id": "restricted-python-workspace",
        "operation_scope_version": "1",
        "operation_scope_digest": SUPPORTED_SCOPE_SHA256,
        "operator_approval_id": f"approval:{uuid.uuid4()}",
        "operator_identity": "operator@example.test",
        "operator_approval_decision": "APPROVED",
        "operator_approved_at": issued_at - timedelta(seconds=1),
        "issued_at": issued_at,
        "expires_at": issued_at + timedelta(minutes=10),
    }
    values.update(overrides)
    return OneShotAuthorizationIdentity(**values)


def _enable(monkeypatch):
    monkeypatch.setattr(settings, "ONE_SHOT_EXECUTION_ENABLED", True)


def _counts(db):
    return (
        db.query(OneShotExecutionAuthorization).count(),
        db.query(ExecutorLaunchIntent).count(),
    )


def test_feature_gate_is_false_by_default(monkeypatch):
    monkeypatch.delenv("ONE_SHOT_EXECUTION_ENABLED", raising=False)
    assert Settings(_env_file=None).ONE_SHOT_EXECUTION_ENABLED is False


def test_disabled_issuance_has_no_one_shot_or_task_execution_changes(
    f1a_database, monkeypatch
):
    _, factory = f1a_database
    db = factory()
    try:
        project, session, task, execution = _graph(db)
        identity = _identity(project, session, task, execution)
        before_execution_count = db.query(TaskExecution).count()
        with pytest.raises(OneShotFeatureDisabled):
            issue_one_shot_authorization(db, identity)
        assert db.query(TaskExecution).count() == before_execution_count
        assert _counts(db) == (0, 0)
        assert db.get(TaskExecution, execution.id).status == TaskStatus.PENDING
    finally:
        db.close()


def test_ordinary_task_execution_creation_remains_available_when_gate_is_off(
    f1a_database,
):
    _, factory = f1a_database
    db = factory()
    try:
        project, session, task, _ = _graph(db, execution_profile="full_lifecycle")
        ordinary = TaskExecution(
            session_id=session.id,
            task_id=task.id,
            attempt_number=2,
            status=TaskStatus.PENDING,
        )
        db.add(ordinary)
        db.commit()
        assert ordinary.id is not None
        assert _counts(db) == (0, 0)
    finally:
        db.close()


def test_issue_persists_one_authorization_and_non_dispatchable_intent_atomically(
    f1a_database, monkeypatch
):
    _enable(monkeypatch)
    _, factory = f1a_database
    db = factory()
    try:
        project, session, task, execution = _graph(db)
        result = issue_one_shot_authorization(
            db, _identity(project, session, task, execution)
        )
        assert result.state == "AUTHORIZED"
        assert result.launch_state == "NOT_DISPATCHABLE"
        assert len(result.identity_sha256) == 64
        assert len(result.request_payload_sha256) == 64
        assert _counts(db) == (1, 1)
    finally:
        db.close()


def test_missing_task_execution_is_rejected_without_partial_rows(
    f1a_database, monkeypatch
):
    _enable(monkeypatch)
    _, factory = f1a_database
    db = factory()
    try:
        project, session, task, execution = _graph(db)
        identity = _identity(
            project, session, task, execution, task_execution_id=999999
        )
        with pytest.raises(AuthorizationAssociationMismatch):
            issue_one_shot_authorization(db, identity)
        assert _counts(db) == (0, 0)
    finally:
        db.close()


def test_wrong_session_generation_is_rejected(f1a_database, monkeypatch):
    _enable(monkeypatch)
    _, factory = f1a_database
    db = factory()
    try:
        project, session, task, execution = _graph(db)
        identity = _identity(
            project,
            session,
            task,
            execution,
            session_instance_id=str(uuid.uuid4()),
        )
        with pytest.raises(AuthorizationAssociationMismatch):
            issue_one_shot_authorization(db, identity)
        assert _counts(db) == (0, 0)
    finally:
        db.close()


def test_cross_project_identity_is_rejected(f1a_database, monkeypatch):
    _enable(monkeypatch)
    _, factory = f1a_database
    db = factory()
    try:
        project, session, task, execution = _graph(db)
        other_project = Project(name="Different Project")
        db.add(other_project)
        db.commit()
        identity = _identity(
            project, session, task, execution, project_id=other_project.id
        )
        with pytest.raises(AuthorizationAssociationMismatch):
            issue_one_shot_authorization(db, identity)
        assert _counts(db) == (0, 0)
    finally:
        db.close()


def test_runtime_lease_mapping_is_rejected(f1a_database, tmp_path, monkeypatch):
    _enable(monkeypatch)
    _, factory = f1a_database
    db = factory()
    try:
        project, session, task, execution = _graph(db)
        # A real Phase 29 lease needs ~10 parent tables; only this fixture
        # write uses a raw connection without FK enforcement. Issuance itself
        # still runs on the FK-enforced engine.
        with sqlite3.connect(tmp_path / "f1a.db") as raw:
            raw.execute(
                "UPDATE task_executions SET runtime_lease_id = 91 WHERE id = ?",
                (execution.id,),
            )
        db.expire_all()
        with pytest.raises(AmbiguousExecutionMapping):
            issue_one_shot_authorization(
                db, _identity(project, session, task, execution)
            )
        assert _counts(db) == (0, 0)
    finally:
        db.close()


@pytest.mark.parametrize(
    ("field", "value", "exception"),
    [
        ("source_revision_identity", "", InvalidAuthorizationIdentity),
        ("source_snapshot_digest", "", InvalidAuthorizationIdentity),
        ("candidate_uuid", "", InvalidAuthorizationIdentity),
        ("candidate_initial_digest", "", InvalidAuthorizationIdentity),
        ("execution_profile_id", "legacy-profile", UnsupportedExecutionProfile),
        ("execution_profile_version", "v9", UnsupportedExecutionProfile),
        ("operation_scope_id", "", InvalidAuthorizationIdentity),
        ("operation_scope_version", "", InvalidAuthorizationIdentity),
        ("operation_scope_digest", "", InvalidAuthorizationIdentity),
        ("operator_identity", "", InvalidAuthorizationIdentity),
        ("operator_approval_decision", "PENDING", InvalidAuthorizationIdentity),
    ],
)
def test_incomplete_or_unsupported_identity_is_rejected(
    f1a_database, monkeypatch, field, value, exception
):
    _enable(monkeypatch)
    _, factory = f1a_database
    db = factory()
    try:
        project, session, task, execution = _graph(db)
        identity = _identity(project, session, task, execution)
        with pytest.raises(exception):
            replace(identity, **{field: value})
        assert _counts(db) == (0, 0)
    finally:
        db.close()


def test_canonical_identity_hash_is_deterministic_and_versioned(f1a_database):
    _, factory = f1a_database
    db = factory()
    try:
        project, session, task, execution = _graph(db)
        identity = _identity(project, session, task, execution)
        same_identity = replace(identity)
        assert canonical_identity_bytes(identity) == canonical_identity_bytes(
            same_identity
        )
        assert canonical_identity_sha256(identity) == canonical_identity_sha256(
            same_identity
        )
        assert len(canonical_identity_sha256(identity)) == 64
    finally:
        db.close()


def test_conflicting_duplicate_and_exact_duplicate_issuance_are_rejected(
    f1a_database, monkeypatch
):
    _enable(monkeypatch)
    _, factory = f1a_database
    db = factory()
    try:
        project, session, task, execution = _graph(db)
        identity = _identity(project, session, task, execution)
        issue_one_shot_authorization(db, identity)
        with pytest.raises(AuthorizationAlreadyExists):
            issue_one_shot_authorization(db, identity)
        conflicting = _identity(
            project,
            session,
            task,
            execution,
            operation_scope_digest="d" * 64,
        )
        with pytest.raises(AuthorizationAlreadyExists):
            issue_one_shot_authorization(db, conflicting)
        assert _counts(db) == (1, 1)
    finally:
        db.close()


def test_failed_intent_insert_rolls_back_authorization_insert(
    f1a_database, monkeypatch
):
    _enable(monkeypatch)
    _, factory = f1a_database
    db = factory()
    module = importlib.import_module("app.services.execution.one_shot_authorization")
    try:
        first = _graph(db)
        first_identity = _identity(*first)
        issue_one_shot_authorization(db, first_identity)

        second = _graph(db)
        second_identity = _identity(*second)
        original = module._intent_identity
        _, first_key, _ = original(
            first_identity, canonical_identity_sha256(first_identity)
        )

        def colliding_intent(identity, identity_sha):
            intent_uuid, _, request_sha = original(identity, identity_sha)
            return intent_uuid, first_key, request_sha

        monkeypatch.setattr(module, "_intent_identity", colliding_intent)
        with pytest.raises(AuthorizationAlreadyExists):
            issue_one_shot_authorization(db, second_identity)
        db.rollback()
        assert _counts(db) == (1, 1)
        assert (
            db.get(OneShotExecutionAuthorization, second_identity.authorization_uuid)
            is None
        )
    finally:
        db.close()


def test_f1a_test_connections_enforce_foreign_keys(f1a_database):
    _, factory = f1a_database
    db = factory()
    try:
        assert db.connection().exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
    finally:
        db.close()


def test_issuance_inserts_parent_before_intent_and_commits_both(
    f1a_database, monkeypatch, tmp_path
):
    _enable(monkeypatch)
    engine, factory = f1a_database
    db = factory()
    inserts: list[str] = []

    @event.listens_for(engine, "before_cursor_execute")
    def _record(conn, cursor, statement, parameters, context, executemany):
        if statement.startswith("INSERT INTO"):
            inserts.append(statement.split()[2])

    try:
        identity = _identity(*_graph(db))
        inserts.clear()
        result = issue_one_shot_authorization(db, identity)
    finally:
        event.remove(engine, "before_cursor_execute", _record)
        db.close()

    assert inserts == ["one_shot_execution_authorizations", "executor_launch_intents"]
    with sqlite3.connect(tmp_path / "f1a.db") as raw:
        assert raw.execute(
            "SELECT state FROM one_shot_execution_authorizations "
            "WHERE authorization_uuid = ?",
            (result.authorization_uuid,),
        ).fetchall() == [("AUTHORIZED",)]
        assert raw.execute(
            "SELECT state FROM executor_launch_intents WHERE authorization_uuid = ?",
            (result.authorization_uuid,),
        ).fetchall() == [("NOT_DISPATCHABLE",)]


def test_missing_reference_is_classified_as_association_mismatch(
    f1a_database, monkeypatch
):
    _enable(monkeypatch)
    _, factory = f1a_database
    db = factory()
    module = importlib.import_module("app.services.execution.one_shot_authorization")
    try:
        project, session, task, execution = _graph(db)
        monkeypatch.setattr(module, "_verify_existing_associations", lambda *_: None)
        identity = _identity(
            project, session, task, type("Missing", (), {"id": 987654})()
        )
        with pytest.raises(AuthorizationAssociationMismatch):
            issue_one_shot_authorization(db, identity)
        assert _counts(db) == (0, 0)
    finally:
        db.close()


def test_check_violation_is_classified_as_integrity_error(f1a_database, monkeypatch):
    _enable(monkeypatch)
    _, factory = f1a_database
    db = factory()
    module = importlib.import_module("app.services.execution.one_shot_authorization")
    try:
        identity = _identity(*_graph(db))
        original = module._intent_identity

        def uppercase_key(identity, identity_sha):
            intent_uuid, key, request_sha = original(identity, identity_sha)
            return intent_uuid, key.upper(), request_sha

        monkeypatch.setattr(module, "_intent_identity", uppercase_key)
        with pytest.raises(AuthorizationIntegrityError) as raised:
            issue_one_shot_authorization(db, identity)
        assert not isinstance(raised.value, AuthorizationAlreadyExists)
        assert _counts(db) == (0, 0)
    finally:
        db.close()


def test_launch_intent_has_unique_authorization_and_attempt_key(
    f1a_database, monkeypatch
):
    _enable(monkeypatch)
    _, factory = f1a_database
    db = factory()
    try:
        project, session, task, execution = _graph(db)
        identity = _identity(project, session, task, execution)
        result = issue_one_shot_authorization(db, identity)
        duplicate_intent = ExecutorLaunchIntent(
            intent_uuid=str(uuid.uuid4()),
            authorization_uuid=result.authorization_uuid,
            project_id=project.id,
            session_id=session.id,
            session_instance_id=session.instance_id,
            task_id=task.id,
            task_execution_id=execution.id,
            attempt_idempotency_key="e" * 64,
            request_payload_sha256="f" * 64,
            state="NOT_DISPATCHABLE",
            created_at=identity.issued_at,
        )
        db.add(duplicate_intent)
        with pytest.raises(IntegrityError):
            db.flush()
        db.rollback()
        assert _counts(db) == (1, 1)
    finally:
        db.close()


def test_orm_and_sqlite_guards_protect_identity_and_launch_intent(
    f1a_database, monkeypatch
):
    _enable(monkeypatch)
    engine, factory = f1a_database
    db = factory()
    try:
        project, session, task, execution = _graph(db)
        identity = _identity(project, session, task, execution)
        result = issue_one_shot_authorization(db, identity)
        authorization = db.get(OneShotExecutionAuthorization, result.authorization_uuid)
        authorization.candidate_initial_digest = "d" * 64
        with pytest.raises(RuntimeError, match="identity is immutable"):
            db.flush()
        db.rollback()

        with pytest.raises(SQLAlchemyError):
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "UPDATE one_shot_execution_authorizations "
                    "SET source_snapshot_digest = ? WHERE authorization_uuid = ?",
                    ("e" * 64, result.authorization_uuid),
                )
        with pytest.raises(SQLAlchemyError):
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "UPDATE executor_launch_intents SET state = 'READY' "
                    "WHERE authorization_uuid = ?",
                    (result.authorization_uuid,),
                )
        assert (
            db.query(OneShotExecutionAuthorization).one().candidate_initial_digest
            == hashlib.sha256(CANDIDATE_FIXTURE_BYTES).hexdigest()
        )
    finally:
        db.close()


def test_expiry_and_revocation_are_terminal_and_inspection_is_idempotent(
    f1a_database, monkeypatch
):
    _enable(monkeypatch)
    _, factory = f1a_database
    db = factory()
    try:
        project, session, task, execution = _graph(db)
        identity = _identity(project, session, task, execution)
        result = issue_one_shot_authorization(db, identity)
        expired_at = identity.expires_at + timedelta(seconds=1)
        expired = inspect_authorization(db, result.authorization_uuid, now=expired_at)
        assert expired.state == "EXPIRED"
        again = inspect_authorization(db, result.authorization_uuid, now=expired_at)
        assert again.state == "EXPIRED"
        with pytest.raises(RuntimeError, match="state is terminal"):
            authorization = db.get(
                OneShotExecutionAuthorization, result.authorization_uuid
            )
            authorization.state = "AUTHORIZED"
            db.flush()
        db.rollback()
    finally:
        db.close()


def test_revocation_is_explicit_idempotent_and_never_reactivates(
    f1a_database, monkeypatch
):
    _enable(monkeypatch)
    _, factory = f1a_database
    db = factory()
    try:
        project, session, task, execution = _graph(db)
        identity = _identity(project, session, task, execution)
        result = issue_one_shot_authorization(db, identity)
        revoked = revoke_authorization(
            db,
            result.authorization_uuid,
            operator_identity="operator@example.test",
            now=identity.issued_at + timedelta(seconds=2),
        )
        repeated = revoke_authorization(
            db,
            result.authorization_uuid,
            operator_identity="different-operator@example.test",
            now=identity.issued_at + timedelta(seconds=3),
        )
        assert revoked.state == "REVOKED"
        assert repeated.state == "REVOKED"
        assert (
            db.get(OneShotExecutionAuthorization, result.authorization_uuid).revoked_by
            == "operator@example.test"
        )
    finally:
        db.close()


def test_expired_revoke_records_expiry_and_claim_transition_is_unavailable(
    f1a_database, monkeypatch
):
    _enable(monkeypatch)
    engine, factory = f1a_database
    db = factory()
    try:
        project, session, task, execution = _graph(db)
        identity = _identity(project, session, task, execution)
        result = issue_one_shot_authorization(db, identity)
        expired = revoke_authorization(
            db,
            result.authorization_uuid,
            operator_identity="operator@example.test",
            now=identity.expires_at + timedelta(seconds=1),
        )
        assert expired.state == "EXPIRED"
        with pytest.raises(SQLAlchemyError):
            with engine.begin() as connection:
                connection.exec_driver_sql(
                    "UPDATE one_shot_execution_authorizations "
                    "SET state = 'CLAIMED', state_changed_at = ? "
                    "WHERE authorization_uuid = ?",
                    (datetime.now(timezone.utc).isoformat(), result.authorization_uuid),
                )
    finally:
        db.close()


def test_f1a_service_exposes_no_claim_dispatch_or_external_imports():
    module = importlib.import_module("app.services.execution.one_shot_authorization")
    assert not hasattr(module, "claim_authorization")
    assert not hasattr(module, "dispatch_launch_intent")
    syntax = ast.parse(Path(module.__file__).read_text(encoding="utf-8"))
    imported = {
        alias.name
        for node in ast.walk(syntax)
        if isinstance(node, ast.Import)
        for alias in node.names
    } | {
        node.module or ""
        for node in ast.walk(syntax)
        if isinstance(node, ast.ImportFrom)
    }
    assert not any(
        name.startswith(("celery", "app.tasks", "app.providers", "app.supervisor"))
        for name in imported
    )


def test_fresh_create_all_then_migration_verifies_complete_schema(f1a_database):
    engine, _ = f1a_database
    inspector = inspect(engine)
    assert {
        "one_shot_execution_authorizations",
        "executor_launch_intents",
    } <= set(inspector.get_table_names())
    assert "uq_one_shot_auth_task_execution" in {
        constraint["name"]
        for constraint in inspector.get_unique_constraints(
            "one_shot_execution_authorizations"
        )
    }


def test_existing_schema_migration_adds_new_tables(tmp_path: Path):
    engine = _sqlite_engine(tmp_path / "existing-schema.db")
    new_tables = {
        "one_shot_execution_authorizations",
        "executor_launch_intents",
    }
    existing_tables = [
        table for table in Base.metadata.sorted_tables if table.name not in new_tables
    ]
    Base.metadata.create_all(engine, tables=existing_tables)
    try:
        run_schema_migrations(engine, (MIGRATION_057,))
        assert new_tables <= set(inspect(engine).get_table_names())
    finally:
        engine.dispose()


def test_repeated_migration_is_idempotent_and_reverifies_constraints(f1a_database):
    engine, _ = f1a_database
    run_schema_migrations(engine, (MIGRATION_057,))
    run_schema_migrations(engine, (MIGRATION_057,))
    with engine.begin() as connection:
        applied = connection.exec_driver_sql(
            "SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
            ("057_one_shot_execution_authorization",),
        ).scalar_one()
        connection.exec_driver_sql("DROP INDEX ix_one_shot_auth_candidate")
    assert applied == 1
    with pytest.raises(RuntimeError, match="missing indexes"):
        run_schema_migrations(engine, (MIGRATION_057,))


def test_partial_schema_is_rejected_without_recording_success(tmp_path: Path):
    engine = _sqlite_engine(tmp_path / "partial-schema.db")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE one_shot_execution_authorizations "
            "(authorization_uuid VARCHAR(36) PRIMARY KEY)"
        )
    try:
        with pytest.raises((RuntimeError, SQLAlchemyError)):
            run_schema_migrations(engine, (MIGRATION_057,))
        with engine.begin() as connection:
            applied = connection.exec_driver_sql(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
                ("057_one_shot_execution_authorization",),
            ).scalar_one()
        assert applied == 0
    finally:
        engine.dispose()


def test_missing_check_constraint_is_rejected_without_recording_success(
    tmp_path: Path,
):
    engine = _sqlite_engine(tmp_path / "missing-check.db")
    new_tables = {
        "one_shot_execution_authorizations",
        "executor_launch_intents",
    }
    existing_tables = [
        table for table in Base.metadata.sorted_tables if table.name not in new_tables
    ]
    Base.metadata.create_all(engine, tables=existing_tables)
    auth_table = OneShotExecutionAuthorization.__table__
    ddl = str(CreateTable(auth_table).compile(engine))
    ddl = re.sub(
        r"\s*CONSTRAINT ck_one_shot_auth_approved CHECK "
        r"\(operator_approval_decision = 'APPROVED'\),?",
        "",
        ddl,
    )
    ddl = re.sub(r",\s*,", ",", ddl)
    ddl = re.sub(r",\s*\)", "\n)", ddl)
    with engine.begin() as connection:
        connection.exec_driver_sql(ddl)
    for index in auth_table.indexes:
        index.create(engine, checkfirst=True)
    try:
        with pytest.raises(RuntimeError, match="missing checks"):
            run_schema_migrations(engine, (MIGRATION_057,))
        with engine.begin() as connection:
            applied = connection.exec_driver_sql(
                "SELECT COUNT(*) FROM schema_migrations WHERE version = ?",
                ("057_one_shot_execution_authorization",),
            ).scalar_one()
        assert applied == 0
    finally:
        engine.dispose()
