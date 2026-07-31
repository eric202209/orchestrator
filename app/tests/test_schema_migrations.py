"""Regression tests for tracked schema migrations."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.orm import sessionmaker

from app.db_migrations import (
    MIGRATIONS,
    _migration_036_execution_task_runtime_ownership,
    _migration_037_execution_task_runtime_evidence,
    _migration_053_project_retirement,
    run_schema_migrations,
)


def _legacy_engine(tmp_path: Path):
    db_path = tmp_path / "legacy.db"
    engine = create_engine(f"sqlite:///{db_path}")
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE projects (
                    id INTEGER PRIMARY KEY,
                    name VARCHAR(255) NOT NULL,
                    description TEXT,
                    created_at DATETIME,
                    updated_at DATETIME
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE tasks (
                    id INTEGER PRIMARY KEY,
                    project_id INTEGER NOT NULL,
                    title VARCHAR(255) NOT NULL,
                    description TEXT,
                    status VARCHAR(50),
                    priority INTEGER,
                    steps TEXT,
                    current_step INTEGER,
                    error_message TEXT,
                    created_at DATETIME
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE sessions (
                    id INTEGER PRIMARY KEY,
                    project_id INTEGER NOT NULL,
                    name VARCHAR(255) NOT NULL,
                    description TEXT,
                    status VARCHAR(50),
                    is_active BOOLEAN,
                    created_at DATETIME
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE log_entries (
                    id INTEGER PRIMARY KEY,
                    session_id INTEGER,
                    task_id INTEGER,
                    level VARCHAR(50),
                    message TEXT,
                    metadata TEXT,
                    created_at DATETIME
                )
                """
            )
        )
    return engine


def test_schema_migrations_add_required_columns_and_indexes(tmp_path):
    engine = _legacy_engine(tmp_path)

    run_schema_migrations(engine)

    inspector = inspect(engine)
    project_columns = {column["name"] for column in inspector.get_columns("projects")}
    task_columns = {column["name"] for column in inspector.get_columns("tasks")}
    session_columns = {column["name"] for column in inspector.get_columns("sessions")}
    log_columns = {column["name"] for column in inspector.get_columns("log_entries")}
    session_indexes = {index["name"] for index in inspector.get_indexes("sessions")}
    planning_tables = set(inspector.get_table_names())
    planning_columns = {
        column["name"] for column in inspector.get_columns("planning_sessions")
    }

    assert {
        "github_url",
        "branch",
        "workspace_path",
        "deleted_at",
        "retired_at",
        "retirement_reason",
        "retired_by_user_id",
    } <= project_columns
    assert {
        "plan_id",
        "plan_position",
        "execution_profile",
        "workflow_stage",
        "task_subfolder",
        "template_id",
    } <= task_columns
    assert {
        "deleted_at",
        "instance_id",
        "execution_mode",
        "model_lane_label",
        "model_lane_metadata",
    } <= session_columns
    assert {"log_metadata", "session_instance_id"} <= log_columns
    assert "ix_sessions_project_name_active" in session_indexes
    assert {
        "planning_sessions",
        "planning_messages",
        "planning_artifacts",
    } <= planning_tables
    assert {"generation_id", "processing_task_id"} <= planning_columns
    assert "ux_planning_sessions_one_active" in {
        index["name"] for index in inspector.get_indexes("planning_sessions")
    }


def test_schema_migrations_rename_deleted_session_names(tmp_path):
    engine = _legacy_engine(tmp_path)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                ALTER TABLE sessions ADD COLUMN deleted_at DATETIME
                """
            )
        )
        connection.execute(
            text(
                """
                INSERT INTO sessions (id, project_id, name, status, is_active, deleted_at)
                VALUES (1, 7, 'retry-session', 'deleted', 0, '2026-04-17T00:00:00')
                """
            )
        )

    run_schema_migrations(engine)

    with engine.begin() as connection:
        renamed = connection.execute(
            text("SELECT name FROM sessions WHERE id = 1")
        ).scalar_one()

    assert renamed == "retry-session__deleted__1"


def test_schema_migrations_add_template_id_when_runtime_migration_already_applied(
    tmp_path,
):
    engine = _legacy_engine(tmp_path)
    run_schema_migrations(engine)

    with engine.begin() as connection:
        connection.execute(text("ALTER TABLE tasks DROP COLUMN template_id"))
        connection.execute(
            text(
                """
                DELETE FROM schema_migrations
                WHERE version = '012_task_template_id'
                """
            )
        )

    run_schema_migrations(engine)

    task_columns = {column["name"] for column in inspect(engine).get_columns("tasks")}
    assert "template_id" in task_columns


def test_project_retirement_migration_preserves_existing_project_and_history_rows(
    tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'project-retirement.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE projects (id INTEGER PRIMARY KEY, name VARCHAR(255), deleted_at DATETIME)"
            )
        )
        connection.execute(
            text("CREATE TABLE tasks (id INTEGER PRIMARY KEY, project_id INTEGER)")
        )
        connection.execute(
            text(
                "INSERT INTO projects (id, name, deleted_at) VALUES "
                "(2, 'active duplicate', NULL), (13, 'deleted history', '2026-07-10')"
            )
        )
        connection.execute(text("INSERT INTO tasks (id, project_id) VALUES (1, 2)"))

    _migration_053_project_retirement(engine)

    project_columns = {
        column["name"] for column in inspect(engine).get_columns("projects")
    }
    assert {"retired_at", "retirement_reason", "retired_by_user_id"} <= project_columns
    with engine.connect() as connection:
        rows = connection.execute(
            text("SELECT id, deleted_at, retired_at FROM projects ORDER BY id")
        ).all()
        task_count = connection.execute(text("SELECT COUNT(*) FROM tasks")).scalar_one()
    assert rows == [(2, None, None), (13, "2026-07-10", None)]
    assert task_count == 1
    assert "053_project_retirement" in {migration.version for migration in MIGRATIONS}


def test_attempt_rebuild_preserves_child_foreign_keys_with_sqlite_enforcement(
    tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'attempt-rebuild.db'}")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    try:
        with engine.begin() as connection:
            connection.execute(
                text("CREATE TABLE execution_plans (id INTEGER PRIMARY KEY)")
            )
            connection.execute(
                text("CREATE TABLE execution_tasks (id INTEGER PRIMARY KEY)")
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE execution_task_dispatch_intents (
                        id INTEGER PRIMARY KEY,
                        execution_task_id INTEGER,
                        dispatch_status VARCHAR(32),
                        submission_lease_expires_at DATETIME,
                        dispatch_command_id INTEGER
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE execution_task_scheduler_claims (
                        consumed_dispatch_intent_id INTEGER
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE execution_task_attempts (
                        id INTEGER PRIMARY KEY,
                        execution_plan_id INTEGER NOT NULL,
                        execution_task_id INTEGER NOT NULL,
                        dispatch_intent_id INTEGER NOT NULL,
                        attempt_number INTEGER NOT NULL,
                        attempt_identity VARCHAR(128) NOT NULL,
                        broker_task_id VARCHAR(255) NOT NULL,
                        attempt_status VARCHAR(24) NOT NULL DEFAULT 'created',
                        created_at DATETIME NOT NULL,
                        submitted_at DATETIME,
                        cancelled_at DATETIME,
                        updated_at DATETIME,
                        CHECK (attempt_status IN ('created', 'submitted', 'cancelled'))
                    )
                    """
                )
            )

        _migration_036_execution_task_runtime_ownership(engine)
        _migration_037_execution_task_runtime_evidence(engine)

        referred_tables = {
            foreign_key["referred_table"]
            for foreign_key in inspect(engine).get_foreign_keys(
                "execution_task_runtime_leases"
            )
        }
        assert "execution_task_attempts" in referred_tables
        assert "execution_task_attempts_phase29c5_old" not in referred_tables
        with engine.connect() as connection:
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1
            assert connection.exec_driver_sql("PRAGMA legacy_alter_table").scalar() == 0
    finally:
        engine.dispose()


def test_canonical_init_db_bootstraps_and_replays_empty_sqlite(
    tmp_path,
    monkeypatch,
):
    from app import database
    from app.models import Base

    engine = create_engine(f"sqlite:///{tmp_path / 'empty.sqlite'}")

    @event.listens_for(engine, "connect")
    def _enable_foreign_keys(dbapi_connection, _):
        dbapi_connection.execute("PRAGMA foreign_keys=ON")

    monkeypatch.setattr(database, "engine", engine)
    monkeypatch.setattr(database, "SessionLocal", sessionmaker(bind=engine))
    try:
        database.init_db()
        with engine.connect() as connection:
            first_versions = tuple(
                row[0]
                for row in connection.execute(
                    text("SELECT version FROM schema_migrations ORDER BY version")
                )
            )
            first_tables = set(inspect(engine).get_table_names())
            assert connection.exec_driver_sql("PRAGMA foreign_keys").scalar() == 1

        assert first_versions == tuple(migration.version for migration in MIGRATIONS)
        assert {
            "projects",
            "tasks",
            "sessions",
            "execution_task_attempts",
            "execution_task_post_apply_validations",
        } <= first_tables

        database.init_db()
        with engine.connect() as connection:
            second_versions = tuple(
                row[0]
                for row in connection.execute(
                    text("SELECT version FROM schema_migrations ORDER BY version")
                )
            )
            second_tables = set(inspect(engine).get_table_names())

        assert second_versions == first_versions
        assert second_tables == first_tables
        db = database.get_db_session()
        try:
            assert db.execute(text("SELECT COUNT(*) FROM projects")).scalar_one() == 0
        finally:
            db.close()
    finally:
        engine.dispose()
