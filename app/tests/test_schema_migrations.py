"""Regression tests for tracked schema migrations."""

from __future__ import annotations

from pathlib import Path

from sqlalchemy import create_engine, inspect, text

from app.db_migrations import run_schema_migrations


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

    assert {"github_url", "branch", "workspace_path", "deleted_at"} <= project_columns
    assert {
        "plan_id",
        "plan_position",
        "execution_profile",
        "task_subfolder",
    } <= task_columns
    assert {"deleted_at", "instance_id", "execution_mode"} <= session_columns
    assert {"log_metadata", "session_instance_id"} <= log_columns
    assert "ix_sessions_project_name_active" in session_indexes
    assert {
        "planning_sessions",
        "planning_messages",
        "planning_artifacts",
    } <= planning_tables
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
