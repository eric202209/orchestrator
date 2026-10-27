"""Lightweight schema migration runner.

This replaces the old best-effort column backfill logic with explicit,
versioned migrations that are tracked in the database.
"""

from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable
import hashlib
import json
import re
import unicodedata
import uuid

from sqlalchemy import CheckConstraint, UniqueConstraint, inspect, text
from sqlalchemy.engine import Engine

MigrationFn = Callable[[Engine], None]


@dataclass(frozen=True)
class Migration:
    version: str
    description: str
    upgrade: MigrationFn
    verify: MigrationFn | None = None


def _has_column(engine: Engine, table_name: str, column_name: str) -> bool:
    inspector = inspect(engine)
    return column_name in {
        column["name"] for column in inspector.get_columns(table_name)
    }


def _column_nullable(engine: Engine, table_name: str, column_name: str) -> bool:
    inspector = inspect(engine)
    for column in inspector.get_columns(table_name):
        if column["name"] == column_name:
            return bool(column.get("nullable", True))
    return True


def _has_index(engine: Engine, table_name: str, index_name: str) -> bool:
    inspector = inspect(engine)
    return index_name in {index["name"] for index in inspector.get_indexes(table_name)}


def _table_names(engine: Engine) -> set[str]:
    return set(inspect(engine).get_table_names())


def _ensure_migrations_table(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS schema_migrations (
                    version VARCHAR(64) PRIMARY KEY,
                    description VARCHAR(255) NOT NULL,
                    applied_at DATETIME NOT NULL
                )
                """
            )
        )


def _get_applied_versions(engine: Engine) -> set[str]:
    _ensure_migrations_table(engine)
    with engine.begin() as connection:
        rows = connection.execute(
            text("SELECT version FROM schema_migrations")
        ).fetchall()
    return {str(row[0]) for row in rows}


def _record_migration(engine: Engine, migration: Migration) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                INSERT INTO schema_migrations (version, description, applied_at)
                VALUES (:version, :description, :applied_at)
                """
            ),
            {
                "version": migration.version,
                "description": migration.description,
                "applied_at": datetime.now(timezone.utc).isoformat(),
            },
        )


@contextmanager
def _migration_transaction(engine: Engine, *, table_rebuild: bool = False):
    """Run one migration transaction with bounded SQLite rebuild settings.

    SQLite rewrites child foreign-key declarations to a temporary table name
    when a referenced table is renamed.  During a table rebuild that leaves
    child authorities pointing at the temporary name after the old table is
    dropped.  Keep foreign-key enforcement enabled for normal application and
    migration work, but use SQLite's legacy rename behavior only for the
    bounded rebuild transaction and restore both connection settings before
    releasing the connection.
    """

    if not table_rebuild or engine.dialect.name != "sqlite":
        with engine.begin() as connection:
            yield connection
        return

    with engine.connect() as connection:
        foreign_keys = int(
            connection.exec_driver_sql("PRAGMA foreign_keys").scalar() or 0
        )
        legacy_alter_table = int(
            connection.exec_driver_sql("PRAGMA legacy_alter_table").scalar() or 0
        )
        connection.commit()
        connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
        connection.exec_driver_sql("PRAGMA legacy_alter_table=ON")
        connection.commit()
        try:
            with connection.begin():
                yield connection
        finally:
            if connection.in_transaction():
                connection.rollback()
            connection.exec_driver_sql(
                f"PRAGMA foreign_keys={'ON' if foreign_keys else 'OFF'}"
            )
            connection.exec_driver_sql(
                "PRAGMA legacy_alter_table=" f"{'ON' if legacy_alter_table else 'OFF'}"
            )
            connection.commit()


def _migration_001_runtime_columns(engine: Engine) -> None:
    table_names = _table_names(engine)

    if "projects" in table_names:
        statements: list[str] = []
        if not _has_column(engine, "projects", "github_url"):
            statements.append("ALTER TABLE projects ADD COLUMN github_url VARCHAR(512)")
        if not _has_column(engine, "projects", "branch"):
            statements.append(
                "ALTER TABLE projects ADD COLUMN branch VARCHAR(255) DEFAULT 'main'"
            )
        if not _has_column(engine, "projects", "workspace_path"):
            statements.append(
                "ALTER TABLE projects ADD COLUMN workspace_path VARCHAR(512)"
            )
        if not _has_column(engine, "projects", "deleted_at"):
            statements.append("ALTER TABLE projects ADD COLUMN deleted_at DATETIME")
        if statements:
            with engine.begin() as connection:
                for statement in statements:
                    connection.execute(text(statement))

    if "tasks" in table_names:
        statements = []
        for column_name, ddl in [
            ("plan_id", "ALTER TABLE tasks ADD COLUMN plan_id INTEGER"),
            (
                "estimated_effort",
                "ALTER TABLE tasks ADD COLUMN estimated_effort VARCHAR(50)",
            ),
            ("plan_position", "ALTER TABLE tasks ADD COLUMN plan_position INTEGER"),
            (
                "execution_profile",
                "ALTER TABLE tasks ADD COLUMN execution_profile VARCHAR(30) DEFAULT 'full_lifecycle'",
            ),
            (
                "workflow_stage",
                "ALTER TABLE tasks ADD COLUMN workflow_stage VARCHAR(30)",
            ),
            (
                "workspace_status",
                "ALTER TABLE tasks ADD COLUMN workspace_status VARCHAR(30) DEFAULT 'isolated'",
            ),
            ("promotion_note", "ALTER TABLE tasks ADD COLUMN promotion_note TEXT"),
            ("promoted_at", "ALTER TABLE tasks ADD COLUMN promoted_at DATETIME"),
            (
                "task_subfolder",
                "ALTER TABLE tasks ADD COLUMN task_subfolder VARCHAR(255)",
            ),
            ("started_at", "ALTER TABLE tasks ADD COLUMN started_at DATETIME"),
            ("completed_at", "ALTER TABLE tasks ADD COLUMN completed_at DATETIME"),
            ("updated_at", "ALTER TABLE tasks ADD COLUMN updated_at DATETIME"),
            (
                "template_id",
                "ALTER TABLE tasks ADD COLUMN template_id VARCHAR(50)",
            ),
        ]:
            if not _has_column(engine, "tasks", column_name):
                statements.append(ddl)
        if statements:
            with engine.begin() as connection:
                for statement in statements:
                    connection.execute(text(statement))
        with engine.begin() as connection:
            if not _has_index(engine, "tasks", "ix_tasks_plan_id"):
                connection.execute(
                    text("CREATE INDEX ix_tasks_plan_id ON tasks (plan_id)")
                )
            if not _has_index(engine, "tasks", "ix_tasks_plan_position"):
                connection.execute(
                    text("CREATE INDEX ix_tasks_plan_position ON tasks (plan_position)")
                )

    if "sessions" in table_names:
        statements = []
        for column_name, ddl in [
            (
                "execution_mode",
                "ALTER TABLE sessions ADD COLUMN execution_mode VARCHAR(20) DEFAULT 'automatic'",
            ),
            (
                "default_execution_profile",
                "ALTER TABLE sessions ADD COLUMN default_execution_profile VARCHAR(30) DEFAULT 'full_lifecycle'",
            ),
            (
                "last_alert_level",
                "ALTER TABLE sessions ADD COLUMN last_alert_level VARCHAR(20)",
            ),
            (
                "last_alert_message",
                "ALTER TABLE sessions ADD COLUMN last_alert_message TEXT",
            ),
            ("last_alert_at", "ALTER TABLE sessions ADD COLUMN last_alert_at DATETIME"),
            ("deleted_at", "ALTER TABLE sessions ADD COLUMN deleted_at DATETIME"),
            ("instance_id", "ALTER TABLE sessions ADD COLUMN instance_id VARCHAR(36)"),
            ("paused_at", "ALTER TABLE sessions ADD COLUMN paused_at DATETIME"),
            ("resumed_at", "ALTER TABLE sessions ADD COLUMN resumed_at DATETIME"),
            ("stopped_at", "ALTER TABLE sessions ADD COLUMN stopped_at DATETIME"),
            ("updated_at", "ALTER TABLE sessions ADD COLUMN updated_at DATETIME"),
        ]:
            if not _has_column(engine, "sessions", column_name):
                statements.append(ddl)
        if statements:
            with engine.begin() as connection:
                for statement in statements:
                    connection.execute(text(statement))
        with engine.begin() as connection:
            if not _has_index(engine, "sessions", "ix_sessions_instance_id"):
                connection.execute(
                    text(
                        "CREATE INDEX ix_sessions_instance_id ON sessions (instance_id)"
                    )
                )
            if not _has_index(engine, "sessions", "ix_sessions_deleted_instance"):
                connection.execute(
                    text(
                        "CREATE INDEX ix_sessions_deleted_instance ON sessions (deleted_at, instance_id)"
                    )
                )

    if "log_entries" in table_names:
        existing_columns = {
            column["name"] for column in inspect(engine).get_columns("log_entries")
        }
        statements = []
        if "log_metadata" not in existing_columns:
            statements.append("ALTER TABLE log_entries ADD COLUMN log_metadata TEXT")
        if "session_instance_id" not in existing_columns:
            statements.append(
                "ALTER TABLE log_entries ADD COLUMN session_instance_id VARCHAR(36)"
            )
        if statements:
            with engine.begin() as connection:
                for statement in statements:
                    connection.execute(text(statement))
                if "metadata" in existing_columns:
                    connection.execute(
                        text(
                            """
                            UPDATE log_entries
                            SET log_metadata = metadata
                            WHERE log_metadata IS NULL AND metadata IS NOT NULL
                            """
                        )
                    )
        with engine.begin() as connection:
            if not _has_index(
                engine, "log_entries", "ix_log_entries_session_instance_id"
            ):
                connection.execute(
                    text(
                        """
                        CREATE INDEX ix_log_entries_session_instance_id
                        ON log_entries (session_instance_id)
                        """
                    )
                )


def _migration_002_session_name_soft_delete(engine: Engine) -> None:
    if "sessions" not in _table_names(engine):
        return

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                UPDATE sessions
                SET name = name || '__deleted__' || id
                WHERE deleted_at IS NOT NULL
                  AND name NOT LIKE '%__deleted__%'
                """
            )
        )

        if not _has_index(engine, "sessions", "ix_sessions_project_name_active"):
            connection.execute(
                text(
                    """
                    CREATE UNIQUE INDEX ix_sessions_project_name_active
                    ON sessions (project_id, name)
                    WHERE deleted_at IS NULL
                    """
                )
            )


def _migration_003_planning_sessions(engine: Engine) -> None:
    table_names = _table_names(engine)

    with engine.begin() as connection:
        if "planning_sessions" not in table_names:
            connection.execute(
                text(
                    """
                    CREATE TABLE planning_sessions (
                        id INTEGER PRIMARY KEY,
                        project_id INTEGER NOT NULL,
                        title VARCHAR(255) NOT NULL,
                        prompt TEXT NOT NULL,
                        status VARCHAR(50) NOT NULL DEFAULT 'active',
                        source_brain VARCHAR(50) NOT NULL DEFAULT 'local',
                        current_prompt_id VARCHAR(64),
                        processing_token VARCHAR(64),
                        processing_started_at DATETIME,
                        finalized_plan_id INTEGER,
                        committed_at DATETIME,
                        committed_task_ids TEXT,
                        last_error TEXT,
                        completed_at DATETIME,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME,
                        FOREIGN KEY(project_id) REFERENCES projects (id),
                        FOREIGN KEY(finalized_plan_id) REFERENCES plans (id)
                    )
                    """
                )
            )

        if "planning_messages" not in table_names:
            connection.execute(
                text(
                    """
                    CREATE TABLE planning_messages (
                        id INTEGER PRIMARY KEY,
                        planning_session_id INTEGER NOT NULL,
                        role VARCHAR(20) NOT NULL,
                        prompt_id VARCHAR(64),
                        content TEXT NOT NULL,
                        metadata_json JSON,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(planning_session_id) REFERENCES planning_sessions (id)
                    )
                    """
                )
            )

        if "planning_artifacts" not in table_names:
            connection.execute(
                text(
                    """
                    CREATE TABLE planning_artifacts (
                        id INTEGER PRIMARY KEY,
                        planning_session_id INTEGER NOT NULL,
                        artifact_type VARCHAR(50) NOT NULL,
                        filename VARCHAR(255) NOT NULL,
                        content TEXT NOT NULL,
                        version INTEGER NOT NULL DEFAULT 1,
                        is_latest BOOLEAN NOT NULL DEFAULT 1,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        FOREIGN KEY(planning_session_id) REFERENCES planning_sessions (id)
                    )
                    """
                )
            )

        if not _has_index(
            engine, "planning_sessions", "ix_planning_sessions_project_id"
        ):
            connection.execute(
                text(
                    "CREATE INDEX ix_planning_sessions_project_id ON planning_sessions (project_id)"
                )
            )
        if not _has_index(engine, "planning_sessions", "ix_planning_sessions_status"):
            connection.execute(
                text(
                    "CREATE INDEX ix_planning_sessions_status ON planning_sessions (status)"
                )
            )
        if not _has_index(
            engine, "planning_sessions", "ix_planning_sessions_finalized_plan_id"
        ):
            connection.execute(
                text(
                    "CREATE INDEX ix_planning_sessions_finalized_plan_id ON planning_sessions (finalized_plan_id)"
                )
            )
        if not _has_index(
            engine, "planning_sessions", "ix_planning_sessions_processing_token"
        ):
            connection.execute(
                text(
                    "CREATE INDEX ix_planning_sessions_processing_token ON planning_sessions (processing_token)"
                )
            )
        if not _has_index(
            engine, "planning_sessions", "ux_planning_sessions_one_active"
        ):
            connection.execute(
                text(
                    """
                    CREATE UNIQUE INDEX ux_planning_sessions_one_active
                    ON planning_sessions (project_id)
                    WHERE status IN ('active', 'waiting_for_input')
                    """
                )
            )
        if not _has_index(
            engine, "planning_messages", "ix_planning_messages_planning_session_id"
        ):
            connection.execute(
                text(
                    "CREATE INDEX ix_planning_messages_planning_session_id ON planning_messages (planning_session_id)"
                )
            )
        if not _has_index(
            engine, "planning_messages", "ix_planning_messages_prompt_id"
        ):
            connection.execute(
                text(
                    "CREATE INDEX ix_planning_messages_prompt_id ON planning_messages (prompt_id)"
                )
            )
        if not _has_index(
            engine, "planning_artifacts", "ix_planning_artifacts_planning_session_id"
        ):
            connection.execute(
                text(
                    "CREATE INDEX ix_planning_artifacts_planning_session_id ON planning_artifacts (planning_session_id)"
                )
            )
        if not _has_index(
            engine, "planning_artifacts", "ix_planning_artifacts_is_latest"
        ):
            connection.execute(
                text(
                    "CREATE INDEX ix_planning_artifacts_is_latest ON planning_artifacts (is_latest)"
                )
            )


def _migration_005_planning_processing_lease(engine: Engine) -> None:
    if "planning_sessions" not in _table_names(engine):
        return

    statements: list[str] = []
    if not _has_column(engine, "planning_sessions", "processing_token"):
        statements.append(
            "ALTER TABLE planning_sessions ADD COLUMN processing_token VARCHAR(64)"
        )
    if not _has_column(engine, "planning_sessions", "processing_started_at"):
        statements.append(
            "ALTER TABLE planning_sessions ADD COLUMN processing_started_at DATETIME"
        )

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
        if not _has_index(
            engine, "planning_sessions", "ix_planning_sessions_processing_token"
        ):
            connection.execute(
                text(
                    "CREATE INDEX ix_planning_sessions_processing_token ON planning_sessions (processing_token)"
                )
            )


def _migration_006_planning_artifact_versioning(engine: Engine) -> None:
    if "planning_artifacts" not in _table_names(engine):
        return

    statements: list[str] = []
    if not _has_column(engine, "planning_artifacts", "version"):
        statements.append(
            "ALTER TABLE planning_artifacts ADD COLUMN version INTEGER NOT NULL DEFAULT 1"
        )
    if not _has_column(engine, "planning_artifacts", "is_latest"):
        statements.append(
            "ALTER TABLE planning_artifacts ADD COLUMN is_latest BOOLEAN NOT NULL DEFAULT 1"
        )

    with engine.begin() as connection:
        for statement in statements:
            connection.execute(text(statement))
        if not _has_index(
            engine, "planning_artifacts", "ix_planning_artifacts_is_latest"
        ):
            connection.execute(
                text(
                    "CREATE INDEX ix_planning_artifacts_is_latest ON planning_artifacts (is_latest)"
                )
            )
        connection.execute(
            text(
                """
                UPDATE planning_artifacts
                SET is_latest = 1
                WHERE id IN (
                    SELECT MAX(id)
                    FROM planning_artifacts
                    GROUP BY planning_session_id, artifact_type
                )
                """
            )
        )
        connection.execute(
            text(
                """
                UPDATE planning_artifacts
                SET is_latest = 0
                WHERE id NOT IN (
                    SELECT MAX(id)
                    FROM planning_artifacts
                    GROUP BY planning_session_id, artifact_type
                )
                """
            )
        )


def _migration_007_intervention_requests(engine: Engine) -> None:
    table_names = _table_names(engine)
    if "intervention_requests" not in table_names:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE intervention_requests (
                        id INTEGER PRIMARY KEY,
                        session_id INTEGER NOT NULL,
                        task_id INTEGER,
                        project_id INTEGER NOT NULL,
                        intervention_type VARCHAR(20) NOT NULL,
                        initiated_by VARCHAR(20) NOT NULL DEFAULT 'ai',
                        prompt TEXT NOT NULL,
                        context_snapshot TEXT,
                        status VARCHAR(20) NOT NULL DEFAULT 'pending',
                        operator_reply TEXT,
                        operator_id VARCHAR(255),
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        replied_at DATETIME,
                        expires_at DATETIME,
                        updated_at DATETIME,
                        FOREIGN KEY(session_id) REFERENCES sessions (id),
                        FOREIGN KEY(task_id) REFERENCES tasks (id),
                        FOREIGN KEY(project_id) REFERENCES projects (id)
                    )
                    """
                )
            )

    with engine.begin() as connection:
        if not _has_index(
            engine, "intervention_requests", "ix_intervention_requests_session_id"
        ):
            connection.execute(
                text(
                    "CREATE INDEX ix_intervention_requests_session_id ON intervention_requests (session_id)"
                )
            )
        if not _has_index(
            engine, "intervention_requests", "ix_intervention_requests_task_id"
        ):
            connection.execute(
                text(
                    "CREATE INDEX ix_intervention_requests_task_id ON intervention_requests (task_id)"
                )
            )
        if not _has_index(
            engine, "intervention_requests", "ix_intervention_requests_project_id"
        ):
            connection.execute(
                text(
                    "CREATE INDEX ix_intervention_requests_project_id ON intervention_requests (project_id)"
                )
            )
        if not _has_index(
            engine, "intervention_requests", "ix_intervention_requests_status"
        ):
            connection.execute(
                text(
                    "CREATE INDEX ix_intervention_requests_status ON intervention_requests (status)"
                )
            )
        if not _has_index(
            engine,
            "intervention_requests",
            "ix_intervention_requests_intervention_type",
        ):
            connection.execute(
                text(
                    "CREATE INDEX ix_intervention_requests_intervention_type ON intervention_requests (intervention_type)"
                )
            )


def _migration_008_intervention_initiated_by(engine: Engine) -> None:
    if "intervention_requests" not in _table_names(engine):
        return
    if not _has_column(engine, "intervention_requests", "initiated_by"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE intervention_requests ADD COLUMN initiated_by VARCHAR(20) NOT NULL DEFAULT 'ai'"
                )
            )


def _migration_009_execution_failure_summaries(engine: Engine) -> None:
    table_names = _table_names(engine)
    if "execution_failure_summaries" not in table_names:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE execution_failure_summaries (
                        id INTEGER PRIMARY KEY,
                        session_id INTEGER NOT NULL UNIQUE,
                        summary TEXT NOT NULL,
                        operator_feedback TEXT,
                        generated_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        feedback_at DATETIME,
                        replan_planning_session_id INTEGER,
                        FOREIGN KEY(session_id) REFERENCES sessions (id),
                        FOREIGN KEY(replan_planning_session_id) REFERENCES planning_sessions (id)
                    )
                    """
                )
            )
    with engine.begin() as connection:
        if not _has_index(
            engine,
            "execution_failure_summaries",
            "ix_execution_failure_summaries_session_id",
        ):
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX ix_execution_failure_summaries_session_id ON execution_failure_summaries (session_id)"
                )
            )


def _migration_010_rename_awaiting_input(engine: Engine) -> None:
    with engine.begin() as connection:
        connection.execute(
            text(
                "UPDATE sessions SET status = 'awaiting_input' WHERE status = 'waiting_for_human'"
            )
        )


def _migration_011_project_user_ownership(engine: Engine) -> None:
    if "projects" not in _table_names(engine):
        return

    with engine.begin() as connection:
        if not _has_column(engine, "projects", "user_id"):
            connection.execute(text("ALTER TABLE projects ADD COLUMN user_id INTEGER"))
        if not _has_index(engine, "projects", "ix_projects_user_id"):
            connection.execute(
                text("CREATE INDEX ix_projects_user_id ON projects (user_id)")
            )


def _migration_012_task_template_id(engine: Engine) -> None:
    if "tasks" not in _table_names(engine):
        return
    if not _has_column(engine, "tasks", "template_id"):
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE tasks ADD COLUMN template_id VARCHAR(50)")
            )


def _migration_013_failure_metadata(engine: Engine) -> None:
    table_names = _table_names(engine)
    if "task_executions" in table_names:
        statements: list[str] = []
        if not _has_column(engine, "task_executions", "failure_category"):
            statements.append(
                "ALTER TABLE task_executions ADD COLUMN failure_category VARCHAR(64)"
            )
        if not _has_column(engine, "task_executions", "backend_id"):
            statements.append(
                "ALTER TABLE task_executions ADD COLUMN backend_id VARCHAR(64)"
            )
        if statements:
            with engine.begin() as connection:
                for statement in statements:
                    connection.execute(text(statement))

    if "sessions" in table_names:
        if not _has_column(engine, "sessions", "escalation_backend_id"):
            with engine.begin() as connection:
                connection.execute(
                    text(
                        "ALTER TABLE sessions ADD COLUMN escalation_backend_id VARCHAR(64)"
                    )
                )


def _migration_023_task_execution_lease(engine: Engine) -> None:
    if "task_executions" not in _table_names(engine):
        return
    statements: list[str] = []
    for column_name, ddl in [
        ("worker_pid", "ALTER TABLE task_executions ADD COLUMN worker_pid INTEGER"),
        (
            "worker_hostname",
            "ALTER TABLE task_executions ADD COLUMN worker_hostname VARCHAR(255)",
        ),
        (
            "heartbeat_at",
            "ALTER TABLE task_executions ADD COLUMN heartbeat_at DATETIME",
        ),
    ]:
        if not _has_column(engine, "task_executions", column_name):
            statements.append(ddl)
    if statements:
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))


def _migration_024_planning_identity_metadata(engine: Engine) -> None:
    table_names = _table_names(engine)
    additions = {
        "planning_sessions": [
            ("planning_backend", "VARCHAR(64)"),
            ("planner_model", "VARCHAR(255)"),
            ("reasoning_profile", "VARCHAR(128)"),
            ("configuration_fingerprint", "VARCHAR(64)"),
        ],
        "task_executions": [
            ("planning_backend", "VARCHAR(64)"),
            ("execution_backend", "VARCHAR(64)"),
            ("planner_model", "VARCHAR(255)"),
            ("executor_model", "VARCHAR(255)"),
            ("configuration_fingerprint", "VARCHAR(64)"),
        ],
    }
    for table_name, columns in additions.items():
        if table_name not in table_names:
            continue
        statements = [
            f"ALTER TABLE {table_name} ADD COLUMN {column_name} {ddl}"
            for column_name, ddl in columns
            if not _has_column(engine, table_name, column_name)
        ]
        if statements:
            with engine.begin() as connection:
                for statement in statements:
                    connection.execute(text(statement))


def _migration_025_task_execution_planner_provenance(engine: Engine) -> None:
    if "task_executions" not in _table_names(engine):
        return
    statements = [
        f"ALTER TABLE task_executions ADD COLUMN {column_name} {ddl}"
        for column_name, ddl in [
            ("planning_session_id", "INTEGER"),
            ("reasoning_profile", "VARCHAR(128)"),
        ]
        if not _has_column(engine, "task_executions", column_name)
    ]
    if statements:
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_task_executions_planning_session_id "
                "ON task_executions (planning_session_id)"
            )
        )


def _migration_026_planning_generation_fence(engine: Engine) -> None:
    """Backfill immutable planning generations and task observations."""

    if "planning_sessions" not in _table_names(engine):
        return

    statements: list[str] = []
    if not _has_column(engine, "planning_sessions", "generation_id"):
        statements.append(
            "ALTER TABLE planning_sessions ADD COLUMN generation_id VARCHAR(36)"
        )
    if not _has_column(engine, "planning_sessions", "processing_task_id"):
        statements.append(
            "ALTER TABLE planning_sessions ADD COLUMN processing_task_id VARCHAR(255)"
        )
    if statements:
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))

    with engine.begin() as connection:
        rows = connection.execute(
            text(
                "SELECT id FROM planning_sessions "
                "WHERE generation_id IS NULL OR generation_id = ''"
            )
        ).fetchall()
        for (session_id,) in rows:
            connection.execute(
                text(
                    "UPDATE planning_sessions SET generation_id = :generation_id "
                    "WHERE id = :session_id"
                ),
                {"generation_id": str(uuid.uuid4()), "session_id": session_id},
            )
        if not _has_index(
            engine, "planning_sessions", "ux_planning_sessions_generation_id"
        ):
            connection.execute(
                text(
                    "CREATE UNIQUE INDEX ux_planning_sessions_generation_id "
                    "ON planning_sessions (generation_id)"
                )
            )
        if not _has_index(
            engine, "planning_sessions", "ix_planning_sessions_processing_task_id"
        ):
            connection.execute(
                text(
                    "CREATE INDEX ix_planning_sessions_processing_task_id "
                    "ON planning_sessions (processing_task_id)"
                )
            )


def _migration_027_protocol_v2_persistence(engine: Engine) -> None:
    """Add protocol identity, append-only stage state, and future manifests."""

    table_names = _table_names(engine)
    if "planning_sessions" in table_names and not _has_column(
        engine, "planning_sessions", "protocol_version"
    ):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE planning_sessions ADD COLUMN protocol_version "
                    "VARCHAR(16) NOT NULL DEFAULT 'v1'"
                )
            )
    if "planning_sessions" in table_names:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE planning_sessions SET protocol_version = 'v1' "
                    "WHERE protocol_version IS NULL OR protocol_version = ''"
                )
            )

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS planning_protocol_inputs (
                    id INTEGER PRIMARY KEY,
                    planning_session_id INTEGER NOT NULL UNIQUE,
                    protocol_version VARCHAR(16) NOT NULL,
                    session_generation_id VARCHAR(36) NOT NULL,
                    input_hash VARCHAR(64) NOT NULL,
                    engineering_context_identity VARCHAR(512) NOT NULL,
                    provider_identity VARCHAR(255) NOT NULL,
                    model_configuration JSON NOT NULL,
                    repository_identity VARCHAR(512) NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(planning_session_id)
                        REFERENCES planning_sessions (id) ON DELETE CASCADE
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS planning_checkpoints (
                    id INTEGER PRIMARY KEY,
                    planning_session_id INTEGER NOT NULL,
                    stage_name VARCHAR(100) NOT NULL,
                    checkpoint_version INTEGER NOT NULL DEFAULT 1,
                    protocol_version VARCHAR(16) NOT NULL,
                    session_generation_id VARCHAR(36) NOT NULL,
                    stage_generation_id VARCHAR(36) NOT NULL,
                    attempt_id VARCHAR(36) NOT NULL,
                    fencing_token VARCHAR(128) NOT NULL,
                    status VARCHAR(20) NOT NULL,
                    content_hash VARCHAR(64) NOT NULL,
                    content TEXT NOT NULL,
                    accepted_at DATETIME,
                    failure_reason TEXT,
                    invalidated_at DATETIME,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_planning_checkpoint_attempt UNIQUE (
                        planning_session_id, stage_name, checkpoint_version, attempt_id
                    ),
                    CONSTRAINT ck_planning_checkpoint_status CHECK (
                        status IN ('accepted', 'failed', 'invalidated')
                    ),
                    FOREIGN KEY(planning_session_id)
                        REFERENCES planning_sessions (id) ON DELETE CASCADE
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS planning_checkpoint_dependencies (
                    checkpoint_id INTEGER NOT NULL,
                    parent_checkpoint_id INTEGER NOT NULL,
                    PRIMARY KEY (checkpoint_id, parent_checkpoint_id),
                    CHECK (checkpoint_id <> parent_checkpoint_id),
                    FOREIGN KEY(checkpoint_id)
                        REFERENCES planning_checkpoints (id) ON DELETE CASCADE,
                    FOREIGN KEY(parent_checkpoint_id)
                        REFERENCES planning_checkpoints (id)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS planning_completion_manifests (
                    id INTEGER PRIMARY KEY,
                    planning_session_id INTEGER NOT NULL UNIQUE,
                    protocol_version VARCHAR(16) NOT NULL,
                    session_generation_id VARCHAR(36) NOT NULL,
                    accepted_checkpoint_versions JSON NOT NULL,
                    dependency_hashes JSON NOT NULL,
                    manifest_hash VARCHAR(64) NOT NULL UNIQUE,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(planning_session_id)
                        REFERENCES planning_sessions (id) ON DELETE CASCADE
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS planning_commit_manifests (
                    id INTEGER PRIMARY KEY,
                    planning_session_id INTEGER NOT NULL,
                    completion_manifest_id INTEGER,
                    plan_id INTEGER,
                    protocol_version VARCHAR(16) NOT NULL,
                    session_generation_id VARCHAR(36) NOT NULL,
                    commit_identity VARCHAR(128) NOT NULL UNIQUE,
                    task_provenance JSON NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    FOREIGN KEY(planning_session_id)
                        REFERENCES planning_sessions (id) ON DELETE CASCADE,
                    FOREIGN KEY(completion_manifest_id)
                        REFERENCES planning_completion_manifests (id),
                    FOREIGN KEY(plan_id) REFERENCES plans (id)
                )
                """
            )
        )

        indexes = {
            "ix_planning_protocol_inputs_input_hash": "CREATE INDEX IF NOT EXISTS ix_planning_protocol_inputs_input_hash "
            "ON planning_protocol_inputs (input_hash)",
            "ix_planning_checkpoints_session_stage": "CREATE INDEX IF NOT EXISTS ix_planning_checkpoints_session_stage "
            "ON planning_checkpoints (planning_session_id, stage_name, checkpoint_version)",
            "ix_planning_checkpoint_dependencies_parent": "CREATE INDEX IF NOT EXISTS ix_planning_checkpoint_dependencies_parent "
            "ON planning_checkpoint_dependencies (parent_checkpoint_id)",
            "ix_planning_commit_manifests_completion": "CREATE INDEX IF NOT EXISTS ix_planning_commit_manifests_completion "
            "ON planning_commit_manifests (completion_manifest_id)",
            "ix_planning_commit_manifests_plan": "CREATE INDEX IF NOT EXISTS ix_planning_commit_manifests_plan "
            "ON planning_commit_manifests (plan_id)",
        }
        for statement in indexes.values():
            connection.execute(text(statement))


def _migration_028_protocol_v2_input_manifest(engine: Engine) -> None:
    """Add the complete immutable Input Manifest to the 28B envelope."""

    if "planning_protocol_inputs" not in _table_names(engine):
        return
    statements: list[str] = []
    if not _has_column(engine, "planning_protocol_inputs", "manifest_id"):
        statements.append(
            "ALTER TABLE planning_protocol_inputs ADD COLUMN manifest_id VARCHAR(128)"
        )
    if not _has_column(engine, "planning_protocol_inputs", "manifest_schema_version"):
        statements.append(
            "ALTER TABLE planning_protocol_inputs ADD COLUMN manifest_schema_version VARCHAR(64)"
        )
    if not _has_column(engine, "planning_protocol_inputs", "manifest_hash"):
        statements.append(
            "ALTER TABLE planning_protocol_inputs ADD COLUMN manifest_hash VARCHAR(64)"
        )
    if not _has_column(engine, "planning_protocol_inputs", "manifest_json"):
        statements.append(
            "ALTER TABLE planning_protocol_inputs ADD COLUMN manifest_json JSON"
        )
    if statements:
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_planning_protocol_inputs_manifest_hash "
                "ON planning_protocol_inputs (manifest_hash)"
            )
        )

    # Phase 28B rows contain enough non-secret identity to form an explicit
    # compatibility manifest without consulting live project state.  This is
    # a one-time adapter; later recovery reads only manifest_json.
    from app.services.planning.input_manifest import InputManifestBuilder

    with engine.begin() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT id, planning_session_id, session_generation_id, input_hash, "
                    "engineering_context_identity, provider_identity, model_configuration, "
                    "repository_identity FROM planning_protocol_inputs "
                    "WHERE protocol_version = 'v2' AND manifest_json IS NULL"
                )
            )
            .mappings()
            .all()
        )
        for row in rows:
            raw_configuration = row["model_configuration"]
            if isinstance(raw_configuration, str):
                raw_configuration = json.loads(raw_configuration)
            manifest = InputManifestBuilder.from_compatibility_identity(
                session_id=int(row["planning_session_id"]),
                session_generation_id=str(row["session_generation_id"]),
                planning_input_hash=str(row["input_hash"]),
                engineering_context_identity=str(row["engineering_context_identity"]),
                provider_identity=str(row["provider_identity"]),
                model_configuration=dict(raw_configuration),
                repository_identity=str(row["repository_identity"]),
            )
            connection.execute(
                text(
                    "UPDATE planning_protocol_inputs SET input_hash = :input_hash, "
                    "manifest_id = :manifest_id, manifest_schema_version = :schema_version, "
                    "manifest_hash = :manifest_hash, manifest_json = :manifest_json "
                    "WHERE id = :id"
                ),
                {
                    "id": row["id"],
                    "input_hash": manifest.manifest_hash,
                    "manifest_id": manifest.manifest_id,
                    "schema_version": manifest.schema_version,
                    "manifest_hash": manifest.manifest_hash,
                    "manifest_json": json.dumps(manifest.to_dict(), ensure_ascii=False),
                },
            )


def _migration_029_planning_brief_checkpoint_metadata(engine: Engine) -> None:
    """Add canonical Planning Brief metadata beside immutable checkpoints."""

    if "planning_checkpoints" not in _table_names(engine):
        return
    statements: list[str] = []
    for column_name, ddl in (
        (
            "schema_version",
            "ALTER TABLE planning_checkpoints ADD COLUMN schema_version VARCHAR(64)",
        ),
        (
            "brief_hash",
            "ALTER TABLE planning_checkpoints ADD COLUMN brief_hash VARCHAR(64)",
        ),
        (
            "renderer_version",
            "ALTER TABLE planning_checkpoints ADD COLUMN renderer_version VARCHAR(64)",
        ),
        (
            "validator_version",
            "ALTER TABLE planning_checkpoints ADD COLUMN validator_version VARCHAR(64)",
        ),
        (
            "validation_json",
            "ALTER TABLE planning_checkpoints ADD COLUMN validation_json JSON",
        ),
    ):
        if not _has_column(engine, "planning_checkpoints", column_name):
            statements.append(ddl)
    if statements:
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS ix_planning_checkpoints_brief_hash "
                "ON planning_checkpoints (brief_hash)"
            )
        )


def _migration_030_protocol_v2_operator_review(engine: Engine) -> None:
    """Add the append-only Protocol v2 operator-review event stream."""

    if "planning_checkpoints" in _table_names(engine):
        statements = []
        if not _has_column(engine, "planning_checkpoints", "promotion_review_event_id"):
            statements.append(
                "ALTER TABLE planning_checkpoints ADD COLUMN "
                "promotion_review_event_id VARCHAR(128)"
            )
        if not _has_column(engine, "planning_checkpoints", "promotion_reason_code"):
            statements.append(
                "ALTER TABLE planning_checkpoints ADD COLUMN promotion_reason_code VARCHAR(128)"
            )
        if statements:
            with engine.begin() as connection:
                for statement in statements:
                    connection.execute(text(statement))

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS planning_review_events (
                    id INTEGER PRIMARY KEY,
                    event_id VARCHAR(128) NOT NULL UNIQUE,
                    review_id VARCHAR(128) NOT NULL,
                    event_sequence INTEGER NOT NULL,
                    event_type VARCHAR(40) NOT NULL,
                    schema_version VARCHAR(64) NOT NULL,
                    planning_session_id INTEGER NOT NULL,
                    project_id INTEGER NOT NULL,
                    protocol_version VARCHAR(16) NOT NULL,
                    stage_name VARCHAR(100) NOT NULL,
                    stage_version INTEGER NOT NULL,
                    stage_generation_id VARCHAR(128) NOT NULL,
                    candidate_checkpoint_id INTEGER NOT NULL,
                    candidate_checkpoint_version INTEGER NOT NULL,
                    candidate_content_hash VARCHAR(64) NOT NULL,
                    session_generation_id VARCHAR(128) NOT NULL,
                    input_manifest_id VARCHAR(128) NOT NULL,
                    input_manifest_hash VARCHAR(64) NOT NULL,
                    brief_checkpoint_id INTEGER,
                    brief_hash VARCHAR(64),
                    predecessor_json JSON NOT NULL,
                    configuration_fingerprint VARCHAR(64) NOT NULL,
                    candidate_attempt_id VARCHAR(128),
                    validator_version VARCHAR(128) NOT NULL,
                    validation_hash VARCHAR(64) NOT NULL,
                    validation_json JSON NOT NULL,
                    review_reason_codes JSON NOT NULL,
                    candidate_binding_json JSON NOT NULL,
                    operator_subject VARCHAR(255) NOT NULL,
                    operator_role VARCHAR(128) NOT NULL,
                    authority_basis VARCHAR(128) NOT NULL,
                    actor_kind VARCHAR(32) NOT NULL,
                    decision_type VARCHAR(40) NOT NULL,
                    decision_text TEXT,
                    command_identity VARCHAR(128),
                    amendment_id VARCHAR(128),
                    amendment_hash VARCHAR(64),
                    prior_review_head_sequence INTEGER NOT NULL,
                    resulting_sequence INTEGER NOT NULL,
                    review_concurrency_token VARCHAR(128) NOT NULL,
                    owner_fence_fingerprint VARCHAR(128),
                    idempotency_key VARCHAR(128) NOT NULL,
                    canonical_request_hash VARCHAR(64) NOT NULL,
                    previous_event_hash VARCHAR(64),
                    event_hash VARCHAR(64) NOT NULL,
                    promotion_checkpoint_id INTEGER,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_planning_review_event_sequence
                        UNIQUE (review_id, event_sequence),
                    CONSTRAINT uq_planning_review_event_idempotency
                        UNIQUE (operator_subject, idempotency_key),
                    CONSTRAINT ck_planning_review_protocol_v2
                        CHECK (protocol_version = 'v2'),
                    CONSTRAINT ck_planning_review_event_sequence_positive
                        CHECK (event_sequence >= 1 AND resulting_sequence = event_sequence),
                    CONSTRAINT ck_planning_review_event_type
                        CHECK (event_type IN ('review_opened','acknowledge_only',
                            'approve_unchanged','reject','request_regeneration',
                            'request_amendment','cancel_review')),
                    FOREIGN KEY(planning_session_id)
                        REFERENCES planning_sessions (id) ON DELETE CASCADE,
                    FOREIGN KEY(project_id)
                        REFERENCES projects (id) ON DELETE CASCADE,
                    FOREIGN KEY(candidate_checkpoint_id)
                        REFERENCES planning_checkpoints (id) ON DELETE RESTRICT
                )
                """
            )
        )
        for statement in (
            "CREATE INDEX IF NOT EXISTS ix_planning_review_events_review_id "
            "ON planning_review_events (review_id)",
            "CREATE INDEX IF NOT EXISTS ix_planning_review_events_session_stage "
            "ON planning_review_events (planning_session_id, stage_name, candidate_checkpoint_id)",
            "CREATE INDEX IF NOT EXISTS ix_planning_review_events_type "
            "ON planning_review_events (event_type)",
            "CREATE INDEX IF NOT EXISTS ix_planning_review_events_candidate_hash "
            "ON planning_review_events (candidate_content_hash)",
            "CREATE INDEX IF NOT EXISTS ix_planning_review_events_created_type "
            "ON planning_review_events (created_at, event_type)",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_planning_review_one_terminal "
            "ON planning_review_events (review_id) WHERE event_type IN "
            "('approve_unchanged','reject','request_regeneration','request_amendment','cancel_review')",
            "CREATE UNIQUE INDEX IF NOT EXISTS ux_planning_review_candidate_open "
            "ON planning_review_events (candidate_checkpoint_id) WHERE event_type = 'review_opened'",
            "CREATE INDEX IF NOT EXISTS ix_planning_checkpoints_promotion_event "
            "ON planning_checkpoints (promotion_review_event_id)",
        ):
            connection.execute(text(statement))


def _migration_031_execution_plan_persistence(engine: Engine) -> None:
    """Add the Phase 29B-1 Execution Plan graph tables.

    These tables are additive: they materialize one accepted Structured
    Task Plan into a durable, hash-bound runtime graph.  Nothing here
    changes Planning tables or behavior.
    """

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_plans (
                    id INTEGER PRIMARY KEY,
                    project_id INTEGER NOT NULL,
                    planning_session_id INTEGER NOT NULL,
                    planning_commit_manifest_id INTEGER NOT NULL UNIQUE,
                    generation INTEGER NOT NULL DEFAULT 1,
                    protocol_version VARCHAR(16) NOT NULL,
                    source_commit_identity VARCHAR(128) NOT NULL,
                    source_plan_checkpoint_id INTEGER NOT NULL,
                    source_plan_hash VARCHAR(64) NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'active',
                    superseded_by_execution_plan_id INTEGER,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME,
                    CONSTRAINT ck_execution_plans_generation_positive
                        CHECK (generation > 0),
                    CONSTRAINT ck_execution_plans_protocol_v2
                        CHECK (protocol_version = 'v2'),
                    FOREIGN KEY(project_id) REFERENCES projects (id),
                    FOREIGN KEY(planning_session_id)
                        REFERENCES planning_sessions (id),
                    FOREIGN KEY(planning_commit_manifest_id)
                        REFERENCES planning_commit_manifests (id),
                    FOREIGN KEY(source_plan_checkpoint_id)
                        REFERENCES planning_checkpoints (id),
                    FOREIGN KEY(superseded_by_execution_plan_id)
                        REFERENCES execution_plans (id)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_tasks (
                    id INTEGER PRIMARY KEY,
                    execution_plan_id INTEGER NOT NULL,
                    plan_task_id VARCHAR(32) NOT NULL,
                    title VARCHAR(255) NOT NULL,
                    blocking_state VARCHAR(32) NOT NULL,
                    task_spec JSON NOT NULL,
                    done_when JSON NOT NULL,
                    status VARCHAR(20) NOT NULL DEFAULT 'pending',
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME,
                    CONSTRAINT uq_execution_tasks_plan_task
                        UNIQUE (execution_plan_id, plan_task_id),
                    FOREIGN KEY(execution_plan_id)
                        REFERENCES execution_plans (id) ON DELETE CASCADE
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_dependency_edges (
                    id INTEGER PRIMARY KEY,
                    execution_plan_id INTEGER NOT NULL,
                    plan_dependency_id VARCHAR(32) NOT NULL,
                    prerequisite_execution_task_id INTEGER NOT NULL,
                    dependent_execution_task_id INTEGER NOT NULL,
                    source_dependency_type VARCHAR(32) NOT NULL,
                    runtime_class VARCHAR(32) NOT NULL,
                    rationale TEXT,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_execution_dependency_edges_plan_dep
                        UNIQUE (execution_plan_id, plan_dependency_id),
                    CONSTRAINT ck_execution_dependency_edges_not_self
                        CHECK (prerequisite_execution_task_id <>
                               dependent_execution_task_id),
                    FOREIGN KEY(execution_plan_id)
                        REFERENCES execution_plans (id) ON DELETE CASCADE,
                    FOREIGN KEY(prerequisite_execution_task_id)
                        REFERENCES execution_tasks (id),
                    FOREIGN KEY(dependent_execution_task_id)
                        REFERENCES execution_tasks (id)
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_groups (
                    id INTEGER PRIMARY KEY,
                    execution_plan_id INTEGER NOT NULL,
                    plan_group_id VARCHAR(32) NOT NULL,
                    kind VARCHAR(32) NOT NULL,
                    order_index INTEGER NOT NULL,
                    parallel_limit INTEGER NOT NULL,
                    skip_policy VARCHAR(32) NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_execution_groups_plan_group
                        UNIQUE (execution_plan_id, plan_group_id),
                    FOREIGN KEY(execution_plan_id)
                        REFERENCES execution_plans (id) ON DELETE CASCADE
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_group_members (
                    id INTEGER PRIMARY KEY,
                    execution_group_id INTEGER NOT NULL,
                    execution_task_id INTEGER NOT NULL,
                    member_order INTEGER NOT NULL,
                    CONSTRAINT uq_execution_group_members_unique_task
                        UNIQUE (execution_group_id, execution_task_id),
                    FOREIGN KEY(execution_group_id)
                        REFERENCES execution_groups (id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_task_id)
                        REFERENCES execution_tasks (id)
                )
                """
            )
        )

        indexes = {
            "ix_execution_plans_project_status": (
                "CREATE INDEX IF NOT EXISTS ix_execution_plans_project_status "
                "ON execution_plans (project_id, status)"
            ),
            "ix_execution_plans_session": (
                "CREATE INDEX IF NOT EXISTS ix_execution_plans_session "
                "ON execution_plans (planning_session_id)"
            ),
            "ix_execution_plans_source_plan_hash": (
                "CREATE INDEX IF NOT EXISTS ix_execution_plans_source_plan_hash "
                "ON execution_plans (source_plan_hash)"
            ),
            "ix_execution_tasks_plan": (
                "CREATE INDEX IF NOT EXISTS ix_execution_tasks_plan "
                "ON execution_tasks (execution_plan_id)"
            ),
            "ix_execution_dependency_edges_plan": (
                "CREATE INDEX IF NOT EXISTS ix_execution_dependency_edges_plan "
                "ON execution_dependency_edges (execution_plan_id)"
            ),
            "ix_execution_dependency_edges_prerequisite": (
                "CREATE INDEX IF NOT EXISTS ix_execution_dependency_edges_prerequisite "
                "ON execution_dependency_edges (prerequisite_execution_task_id)"
            ),
            "ix_execution_dependency_edges_dependent": (
                "CREATE INDEX IF NOT EXISTS ix_execution_dependency_edges_dependent "
                "ON execution_dependency_edges (dependent_execution_task_id)"
            ),
            "ix_execution_groups_plan": (
                "CREATE INDEX IF NOT EXISTS ix_execution_groups_plan "
                "ON execution_groups (execution_plan_id)"
            ),
            "ix_execution_group_members_group": (
                "CREATE INDEX IF NOT EXISTS ix_execution_group_members_group "
                "ON execution_group_members (execution_group_id)"
            ),
            "ix_execution_group_members_task": (
                "CREATE INDEX IF NOT EXISTS ix_execution_group_members_task "
                "ON execution_group_members (execution_task_id)"
            ),
        }
        for statement in indexes.values():
            connection.execute(text(statement))


def _migration_032_execution_commit_command(engine: Engine) -> None:
    """Add the Phase 29B-3 execution-commit idempotency command binding.

    This table is additive and holds only command-replay/control state; it
    never becomes a second authority source over Planning or Execution
    tables.
    """

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_commit_commands (
                    id INTEGER PRIMARY KEY,
                    planning_session_id INTEGER NOT NULL,
                    operator_subject VARCHAR(255) NOT NULL,
                    idempotency_key VARCHAR(128) NOT NULL,
                    canonical_request_hash VARCHAR(64) NOT NULL,
                    planning_commit_manifest_id INTEGER,
                    execution_plan_id INTEGER,
                    boundary_state VARCHAR(40) NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME,
                    CONSTRAINT uq_execution_commit_command_idempotency
                        UNIQUE (operator_subject, idempotency_key),
                    FOREIGN KEY(planning_session_id)
                        REFERENCES planning_sessions (id) ON DELETE CASCADE,
                    FOREIGN KEY(planning_commit_manifest_id)
                        REFERENCES planning_commit_manifests (id),
                    FOREIGN KEY(execution_plan_id)
                        REFERENCES execution_plans (id)
                )
                """
            )
        )
        indexes = {
            "ix_execution_commit_commands_session": (
                "CREATE INDEX IF NOT EXISTS ix_execution_commit_commands_session "
                "ON execution_commit_commands (planning_session_id)"
            ),
            "ix_execution_commit_commands_operator": (
                "CREATE INDEX IF NOT EXISTS ix_execution_commit_commands_operator "
                "ON execution_commit_commands (operator_subject)"
            ),
            "ix_execution_commit_commands_manifest": (
                "CREATE INDEX IF NOT EXISTS ix_execution_commit_commands_manifest "
                "ON execution_commit_commands (planning_commit_manifest_id)"
            ),
            "ix_execution_commit_commands_plan": (
                "CREATE INDEX IF NOT EXISTS ix_execution_commit_commands_plan "
                "ON execution_commit_commands (execution_plan_id)"
            ),
        }
        for statement in indexes.values():
            connection.execute(text(statement))


def _migration_033_execution_task_lifecycle(engine: Engine) -> None:
    """Add durable Phase 29C-1 task lifecycle state and transition events.

    Existing Phase 29B tasks are intentionally accepted only when they are
    still at the materialization state. No historical transition event is
    fabricated for those rows; their state version starts at zero.
    """

    with engine.begin() as connection:
        if not _has_column(engine, "execution_tasks", "state_version"):
            connection.execute(
                text(
                    "ALTER TABLE execution_tasks ADD COLUMN "
                    "state_version INTEGER NOT NULL DEFAULT 0"
                )
            )

        legacy_statuses = {
            str(value)
            for value in connection.execute(
                text(
                    "SELECT DISTINCT status FROM execution_tasks "
                    "WHERE status <> 'pending'"
                )
            ).scalars()
        }
        if legacy_statuses:
            raise RuntimeError(
                "Phase 29C-1 migration refuses non-pending existing "
                f"ExecutionTask statuses: {sorted(legacy_statuses)!r}"
            )
        legacy_versions = {
            int(value)
            for value in connection.execute(
                text(
                    "SELECT DISTINCT state_version FROM execution_tasks "
                    "WHERE state_version <> 0"
                )
            ).scalars()
        }
        if legacy_versions:
            raise RuntimeError(
                "Phase 29C-1 migration refuses existing ExecutionTask "
                f"versions without history: {sorted(legacy_versions)!r}"
            )

        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_task_transitions (
                    id INTEGER PRIMARY KEY,
                    execution_plan_id INTEGER NOT NULL,
                    execution_task_id INTEGER NOT NULL,
                    plan_task_id VARCHAR(32) NOT NULL,
                    sequence INTEGER NOT NULL,
                    from_state VARCHAR(20) NOT NULL,
                    to_state VARCHAR(20) NOT NULL,
                    reason_code VARCHAR(64) NOT NULL,
                    reason_detail VARCHAR(1024),
                    actor_type VARCHAR(32) NOT NULL,
                    actor_id VARCHAR(255) NOT NULL,
                    command_id VARCHAR(128) NOT NULL,
                    expected_version INTEGER NOT NULL,
                    resulting_version INTEGER NOT NULL,
                    canonical_command_hash VARCHAR(64) NOT NULL,
                    canonical_payload_hash VARCHAR(64) NOT NULL,
                    previous_event_hash VARCHAR(64),
                    event_hash VARCHAR(64) NOT NULL,
                    created_at DATETIME DEFAULT CURRENT_TIMESTAMP NOT NULL,
                    CONSTRAINT uq_execution_task_transition_sequence
                        UNIQUE (execution_task_id, sequence),
                    CONSTRAINT uq_execution_task_transition_idempotency
                        UNIQUE (execution_task_id, actor_type, actor_id, command_id),
                    FOREIGN KEY(execution_plan_id)
                        REFERENCES execution_plans (id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_task_id)
                        REFERENCES execution_tasks (id) ON DELETE CASCADE
                )
                """
            )
        )
        indexes = {
            "ix_execution_task_transitions_plan": (
                "CREATE INDEX IF NOT EXISTS ix_execution_task_transitions_plan "
                "ON execution_task_transitions (execution_plan_id)"
            ),
            "ix_execution_task_transitions_task": (
                "CREATE INDEX IF NOT EXISTS ix_execution_task_transitions_task "
                "ON execution_task_transitions (execution_task_id)"
            ),
            "ix_execution_task_transitions_plan_task": (
                "CREATE INDEX IF NOT EXISTS "
                "ix_execution_task_transitions_plan_task "
                "ON execution_task_transitions "
                "(execution_plan_id, execution_task_id)"
            ),
            "ix_execution_task_transitions_command": (
                "CREATE INDEX IF NOT EXISTS "
                "ix_execution_task_transitions_command "
                "ON execution_task_transitions "
                "(actor_type, actor_id, command_id)"
            ),
            "ix_execution_task_transitions_event_hash": (
                "CREATE INDEX IF NOT EXISTS "
                "ix_execution_task_transitions_event_hash "
                "ON execution_task_transitions (event_hash)"
            ),
        }
        for statement in indexes.values():
            connection.execute(text(statement))


def _migration_034_execution_task_scheduler_claim(engine: Engine) -> None:
    """Add the durable Phase 29C-3 scheduler claim boundary.

    Claims are audit-preserving control records owned by an Execution Plan.
    The partial unique index is the database authority for one active claim
    per task; no Redis or worker-side convention is used for correctness.
    """

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_task_scheduler_claims (
                    id INTEGER PRIMARY KEY,
                    execution_plan_id INTEGER NOT NULL,
                    execution_task_id INTEGER NOT NULL,
                    project_id INTEGER NOT NULL,
                    planning_session_id INTEGER NOT NULL,
                    scheduler_id VARCHAR(255) NOT NULL,
                    idempotency_key VARCHAR(128) NOT NULL,
                    command_payload JSON NOT NULL,
                    canonical_command_hash VARCHAR(64) NOT NULL,
                    fencing_token INTEGER NOT NULL,
                    claimed_task_state VARCHAR(20) NOT NULL,
                    claimed_task_state_version INTEGER NOT NULL,
                    claimed_eligibility_decision_hash VARCHAR(64) NOT NULL,
                    claimed_graph_hash VARCHAR(64) NOT NULL,
                    predecessor_fence_hash VARCHAR(64) NOT NULL,
                    predecessor_fences JSON NOT NULL,
                    claim_status VARCHAR(16) NOT NULL DEFAULT 'active',
                    lease_duration_seconds INTEGER NOT NULL,
                    acquired_at DATETIME NOT NULL,
                    expires_at DATETIME NOT NULL,
                    released_at DATETIME,
                    release_reason VARCHAR(64),
                    released_by_scheduler_id VARCHAR(255),
                    release_idempotency_key VARCHAR(128),
                    canonical_release_hash VARCHAR(64),
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME,
                    CONSTRAINT uq_execution_task_scheduler_claim_idempotency
                        UNIQUE (idempotency_key),
                    CONSTRAINT uq_execution_task_scheduler_claim_release_idempotency
                        UNIQUE (release_idempotency_key),
                    CONSTRAINT ck_execution_task_scheduler_claim_status
                        CHECK (claim_status IN ('active', 'released', 'expired', 'consumed')),
                    CONSTRAINT ck_execution_task_scheduler_claim_ready_state
                        CHECK (claimed_task_state = 'ready'),
                    CONSTRAINT ck_execution_task_scheduler_claim_fence_positive
                        CHECK (fencing_token > 0),
                    CONSTRAINT ck_execution_task_scheduler_claim_version_nonnegative
                        CHECK (claimed_task_state_version >= 0),
                    CONSTRAINT ck_execution_task_scheduler_claim_lease_bounds
                        CHECK (lease_duration_seconds >= 5 AND lease_duration_seconds <= 300),
                    CONSTRAINT ck_execution_task_scheduler_claim_expiry_after_acquire
                        CHECK (expires_at > acquired_at),
                    FOREIGN KEY(execution_plan_id)
                        REFERENCES execution_plans (id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_task_id)
                        REFERENCES execution_tasks (id) ON DELETE CASCADE,
                    FOREIGN KEY(project_id) REFERENCES projects (id),
                    FOREIGN KEY(planning_session_id) REFERENCES planning_sessions (id)
                )
                """
            )
        )
        indexes = {
            "uq_execution_task_scheduler_claim_active": (
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_execution_task_scheduler_claim_active "
                "ON execution_task_scheduler_claims (execution_task_id) "
                "WHERE claim_status = 'active'"
            ),
            "ix_execution_task_scheduler_claim_task_status_expiry": (
                "CREATE INDEX IF NOT EXISTS "
                "ix_execution_task_scheduler_claim_task_status_expiry "
                "ON execution_task_scheduler_claims "
                "(execution_task_id, claim_status, expires_at)"
            ),
            "ix_execution_task_scheduler_claim_scheduler_status": (
                "CREATE INDEX IF NOT EXISTS "
                "ix_execution_task_scheduler_claim_scheduler_status "
                "ON execution_task_scheduler_claims (scheduler_id, claim_status)"
            ),
            "ix_execution_task_scheduler_claim_expiry": (
                "CREATE INDEX IF NOT EXISTS "
                "ix_execution_task_scheduler_claim_expiry "
                "ON execution_task_scheduler_claims (claim_status, expires_at)"
            ),
            "ix_execution_task_scheduler_claim_plan_status_expiry": (
                "CREATE INDEX IF NOT EXISTS "
                "ix_execution_task_scheduler_claim_plan_status_expiry "
                "ON execution_task_scheduler_claims "
                "(execution_plan_id, claim_status, expires_at)"
            ),
            "ix_execution_task_scheduler_claim_project_status_expiry": (
                "CREATE INDEX IF NOT EXISTS "
                "ix_execution_task_scheduler_claim_project_status_expiry "
                "ON execution_task_scheduler_claims "
                "(project_id, claim_status, expires_at)"
            ),
            "ix_execution_task_scheduler_claim_idempotency": (
                "CREATE INDEX IF NOT EXISTS "
                "ix_execution_task_scheduler_claim_idempotency "
                "ON execution_task_scheduler_claims (idempotency_key)"
            ),
            "ix_execution_task_scheduler_claim_release_idempotency": (
                "CREATE INDEX IF NOT EXISTS "
                "ix_execution_task_scheduler_claim_release_idempotency "
                "ON execution_task_scheduler_claims (release_idempotency_key)"
            ),
        }
        for statement in indexes.values():
            connection.execute(text(statement))


def _migration_035_execution_task_dispatch_intent_attempt(engine: Engine) -> None:
    """Add Phase 29C-4 dispatch intents, canonical attempts, and claim binding."""

    with engine.begin() as connection:
        for column_name, ddl in (
            (
                "consumed_at",
                "ALTER TABLE execution_task_scheduler_claims "
                "ADD COLUMN consumed_at DATETIME",
            ),
            (
                "consumed_dispatch_intent_id",
                "ALTER TABLE execution_task_scheduler_claims "
                "ADD COLUMN consumed_dispatch_intent_id INTEGER",
            ),
        ):
            if not _has_column(engine, "execution_task_scheduler_claims", column_name):
                connection.execute(text(ddl))

        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_task_dispatch_intents (
                    id INTEGER PRIMARY KEY,
                    execution_plan_id INTEGER NOT NULL,
                    execution_task_id INTEGER NOT NULL,
                    scheduler_claim_id INTEGER NOT NULL,
                    scheduler_id VARCHAR(255) NOT NULL,
                    claim_fencing_token INTEGER NOT NULL,
                    claim_eligibility_decision_hash VARCHAR(64) NOT NULL,
                    claim_graph_hash VARCHAR(64) NOT NULL,
                    claim_predecessor_fence_hash VARCHAR(64) NOT NULL,
                    claim_predecessor_fences JSON NOT NULL,
                    claimed_task_state VARCHAR(20) NOT NULL,
                    claimed_task_state_version INTEGER NOT NULL,
                    dispatch_idempotency_key VARCHAR(128) NOT NULL,
                    dispatch_command_id VARCHAR(128) NOT NULL,
                    canonical_command_payload JSON NOT NULL,
                    canonical_command_hash VARCHAR(64) NOT NULL,
                    worker_command_payload JSON NOT NULL,
                    worker_command_hash VARCHAR(64) NOT NULL,
                    runtime_attempt_id INTEGER,
                    broker_task_id VARCHAR(255) NOT NULL,
                    dispatch_status VARCHAR(24) NOT NULL DEFAULT 'pending_submission',
                    created_at DATETIME NOT NULL,
                    submission_started_at DATETIME,
                    submitted_at DATETIME,
                    acknowledged_at DATETIME,
                    failed_at DATETIME,
                    cancelled_at DATETIME,
                    cancellation_reason VARCHAR(64),
                    last_submission_error_code VARCHAR(64),
                    last_submission_detail VARCHAR(1024),
                    submission_count INTEGER NOT NULL DEFAULT 0,
                    submission_attempt_number INTEGER NOT NULL DEFAULT 0,
                    submission_idempotency_key VARCHAR(128),
                    submitter_id VARCHAR(255),
                    submission_fencing_token INTEGER NOT NULL DEFAULT 0,
                    submission_lease_expires_at DATETIME,
                    broker_returned_task_id VARCHAR(255),
                    creation_actor_type VARCHAR(32) NOT NULL,
                    creation_actor_id VARCHAR(255) NOT NULL,
                    created_by_idempotency_key VARCHAR(128) NOT NULL,
                    updated_at DATETIME,
                    CONSTRAINT uq_execution_task_dispatch_intent_claim
                        UNIQUE (scheduler_claim_id),
                    CONSTRAINT uq_execution_task_dispatch_intent_key
                        UNIQUE (dispatch_idempotency_key),
                    CONSTRAINT uq_execution_task_dispatch_intent_command
                        UNIQUE (dispatch_command_id),
                    CONSTRAINT uq_execution_task_dispatch_intent_attempt
                        UNIQUE (runtime_attempt_id),
                    CONSTRAINT uq_execution_task_dispatch_intent_broker
                        UNIQUE (broker_task_id),
                    CONSTRAINT ck_execution_task_dispatch_intent_status
                        CHECK (dispatch_status IN (
                            'pending_submission', 'submitting', 'submitted',
                            'submission_failed', 'cancelled'
                        )),
                    CONSTRAINT ck_execution_task_dispatch_intent_fence_positive
                        CHECK (claim_fencing_token > 0),
                    CONSTRAINT ck_execution_task_dispatch_intent_task_fence
                        CHECK (
                            claimed_task_state = 'ready'
                            AND claimed_task_state_version >= 0
                        ),
                    CONSTRAINT ck_execution_task_dispatch_intent_submission_counts
                        CHECK (
                            submission_count >= 0
                            AND submission_attempt_number >= 0
                        ),
                    CONSTRAINT ck_execution_task_dispatch_intent_submission_fence
                        CHECK (submission_fencing_token >= 0),
                    FOREIGN KEY(execution_plan_id)
                        REFERENCES execution_plans (id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_task_id)
                        REFERENCES execution_tasks (id) ON DELETE CASCADE,
                    FOREIGN KEY(scheduler_claim_id)
                        REFERENCES execution_task_scheduler_claims (id)
                        ON DELETE CASCADE
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_task_attempts (
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
                    CONSTRAINT uq_execution_task_attempt_dispatch_intent
                        UNIQUE (dispatch_intent_id),
                    CONSTRAINT uq_execution_task_attempt_identity
                        UNIQUE (attempt_identity),
                    CONSTRAINT uq_execution_task_attempt_broker
                        UNIQUE (broker_task_id),
                    CONSTRAINT uq_execution_task_attempt_task_number
                        UNIQUE (execution_task_id, attempt_number),
                    CONSTRAINT ck_execution_task_attempt_number_positive
                        CHECK (attempt_number > 0),
                    CONSTRAINT ck_execution_task_attempt_status
                        CHECK (attempt_status IN ('created', 'submitted', 'cancelled')),
                    FOREIGN KEY(execution_plan_id)
                        REFERENCES execution_plans (id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_task_id)
                        REFERENCES execution_tasks (id) ON DELETE CASCADE,
                    FOREIGN KEY(dispatch_intent_id)
                        REFERENCES execution_task_dispatch_intents (id)
                        ON DELETE CASCADE
                )
                """
            )
        )
        indexes = {
            "uq_execution_task_scheduler_claim_consumed_intent": (
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_execution_task_scheduler_claim_consumed_intent "
                "ON execution_task_scheduler_claims (consumed_dispatch_intent_id)"
            ),
            "ix_execution_task_dispatch_intents_task_status": (
                "CREATE INDEX IF NOT EXISTS ix_execution_task_dispatch_intents_task_status "
                "ON execution_task_dispatch_intents (execution_task_id, dispatch_status)"
            ),
            "ix_execution_task_dispatch_intents_recovery": (
                "CREATE INDEX IF NOT EXISTS ix_execution_task_dispatch_intents_recovery "
                "ON execution_task_dispatch_intents "
                "(dispatch_status, submission_lease_expires_at)"
            ),
            "ix_execution_task_dispatch_intents_plan_status": (
                "CREATE INDEX IF NOT EXISTS ix_execution_task_dispatch_intents_plan_status "
                "ON execution_task_dispatch_intents (execution_plan_id, dispatch_status)"
            ),
            "ix_execution_task_dispatch_intents_command": (
                "CREATE INDEX IF NOT EXISTS ix_execution_task_dispatch_intents_command "
                "ON execution_task_dispatch_intents (dispatch_command_id)"
            ),
            "ix_execution_task_attempts_task_status": (
                "CREATE INDEX IF NOT EXISTS ix_execution_task_attempts_task_status "
                "ON execution_task_attempts (execution_task_id, attempt_status)"
            ),
            "ix_execution_task_attempts_identity": (
                "CREATE INDEX IF NOT EXISTS ix_execution_task_attempts_identity "
                "ON execution_task_attempts (attempt_identity)"
            ),
            "ix_execution_task_attempts_broker": (
                "CREATE INDEX IF NOT EXISTS ix_execution_task_attempts_broker "
                "ON execution_task_attempts (broker_task_id)"
            ),
        }
        for statement in indexes.values():
            connection.execute(text(statement))


def _migration_036_execution_task_runtime_ownership(engine: Engine) -> None:
    """Add Phase 29C-5 fenced runtime ownership and running evidence."""

    table_names = _table_names(engine)
    with _migration_transaction(engine, table_rebuild=True) as connection:
        if "execution_task_transitions" in table_names:
            for column_name, ddl in (
                (
                    "runtime_attempt_id",
                    "ALTER TABLE execution_task_transitions "
                    "ADD COLUMN runtime_attempt_id INTEGER",
                ),
                (
                    "runtime_lease_id",
                    "ALTER TABLE execution_task_transitions "
                    "ADD COLUMN runtime_lease_id INTEGER",
                ),
                (
                    "runtime_ownership_fence",
                    "ALTER TABLE execution_task_transitions "
                    "ADD COLUMN runtime_ownership_fence INTEGER",
                ),
            ):
                if not _has_column(engine, "execution_task_transitions", column_name):
                    connection.execute(text(ddl))

        if "execution_task_attempts" in table_names and not _has_column(
            engine, "execution_task_attempts", "started_at"
        ):
            # SQLite cannot alter a CHECK constraint in place.  Rebuild this
            # additive Phase 29C-4 table while preserving every existing row.
            connection.execute(
                text(
                    "ALTER TABLE execution_task_attempts "
                    "RENAME TO execution_task_attempts_phase29c4_old"
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
                        started_at DATETIME,
                        cancelled_at DATETIME,
                        updated_at DATETIME,
                        CONSTRAINT uq_execution_task_attempt_dispatch_intent
                            UNIQUE (dispatch_intent_id),
                        CONSTRAINT uq_execution_task_attempt_identity
                            UNIQUE (attempt_identity),
                        CONSTRAINT uq_execution_task_attempt_broker
                            UNIQUE (broker_task_id),
                        CONSTRAINT uq_execution_task_attempt_task_number
                            UNIQUE (execution_task_id, attempt_number),
                        CONSTRAINT ck_execution_task_attempt_number_positive
                            CHECK (attempt_number > 0),
                        CONSTRAINT ck_execution_task_attempt_status
                            CHECK (attempt_status IN (
                                'created', 'submitted', 'running', 'cancelled',
                                'failed', 'succeeded'
                            )),
                        FOREIGN KEY(execution_plan_id)
                            REFERENCES execution_plans (id) ON DELETE CASCADE,
                        FOREIGN KEY(execution_task_id)
                            REFERENCES execution_tasks (id) ON DELETE CASCADE,
                        FOREIGN KEY(dispatch_intent_id)
                            REFERENCES execution_task_dispatch_intents (id)
                            ON DELETE CASCADE
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO execution_task_attempts (
                        id, execution_plan_id, execution_task_id,
                        dispatch_intent_id, attempt_number, attempt_identity,
                        broker_task_id, attempt_status, created_at, submitted_at,
                        started_at, cancelled_at, updated_at
                    )
                    SELECT id, execution_plan_id, execution_task_id,
                        dispatch_intent_id, attempt_number, attempt_identity,
                        broker_task_id, attempt_status, created_at, submitted_at,
                        NULL, cancelled_at, updated_at
                    FROM execution_task_attempts_phase29c4_old
                    """
                )
            )
            connection.execute(text("DROP TABLE execution_task_attempts_phase29c4_old"))

        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_task_runtime_leases (
                    id INTEGER PRIMARY KEY,
                    execution_plan_id INTEGER NOT NULL,
                    execution_task_id INTEGER NOT NULL,
                    execution_task_attempt_id INTEGER NOT NULL,
                    dispatch_intent_id INTEGER NOT NULL,
                    broker_task_id VARCHAR(255) NOT NULL,
                    worker_id VARCHAR(255) NOT NULL,
                    worker_hostname VARCHAR(255) NOT NULL,
                    worker_pid INTEGER NOT NULL,
                    worker_process_start_identity VARCHAR(255) NOT NULL,
                    worker_instance_id VARCHAR(255) NOT NULL,
                    ownership_fencing_token INTEGER NOT NULL,
                    lease_status VARCHAR(16) NOT NULL DEFAULT 'active',
                    lease_duration_seconds INTEGER NOT NULL,
                    acquired_at DATETIME NOT NULL,
                    lease_expires_at DATETIME NOT NULL,
                    last_heartbeat_at DATETIME NOT NULL,
                    released_at DATETIME,
                    release_reason VARCHAR(64),
                    ownership_idempotency_key VARCHAR(128) NOT NULL,
                    canonical_ownership_command_payload JSON NOT NULL,
                    canonical_ownership_command_hash VARCHAR(64) NOT NULL,
                    lifecycle_transition_id INTEGER,
                    lifecycle_transition_sequence INTEGER,
                    lifecycle_resulting_state_version INTEGER,
                    runtime_started_at DATETIME NOT NULL,
                    runtime_start_evidence JSON NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    updated_at DATETIME,
                    CONSTRAINT uq_execution_task_runtime_lease_idempotency
                        UNIQUE (ownership_idempotency_key),
                    CONSTRAINT ck_execution_task_runtime_lease_status
                        CHECK (lease_status IN (
                            'active', 'released', 'expired', 'completed', 'revoked'
                        )),
                    CONSTRAINT ck_execution_task_runtime_lease_fence_positive
                        CHECK (ownership_fencing_token > 0),
                    CONSTRAINT ck_execution_task_runtime_lease_duration_bounds
                        CHECK (lease_duration_seconds >= 10
                            AND lease_duration_seconds <= 300),
                    CONSTRAINT ck_execution_task_runtime_lease_worker_pid_positive
                        CHECK (worker_pid > 0),
                    CONSTRAINT ck_execution_task_runtime_lease_expiry_after_acquire
                        CHECK (lease_expires_at > acquired_at),
                    FOREIGN KEY(execution_plan_id)
                        REFERENCES execution_plans (id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_task_id)
                        REFERENCES execution_tasks (id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_task_attempt_id)
                        REFERENCES execution_task_attempts (id) ON DELETE CASCADE,
                    FOREIGN KEY(dispatch_intent_id)
                        REFERENCES execution_task_dispatch_intents (id)
                        ON DELETE CASCADE
                )
                """
            )
        )
        indexes = {
            "uq_execution_task_runtime_lease_active": (
                "CREATE UNIQUE INDEX IF NOT EXISTS "
                "uq_execution_task_runtime_lease_active "
                "ON execution_task_runtime_leases (execution_task_attempt_id) "
                "WHERE lease_status = 'active'"
            ),
            "ix_execution_task_runtime_leases_attempt_status_expiry": (
                "CREATE INDEX IF NOT EXISTS "
                "ix_execution_task_runtime_leases_attempt_status_expiry "
                "ON execution_task_runtime_leases "
                "(execution_task_attempt_id, lease_status, lease_expires_at)"
            ),
            "ix_execution_task_runtime_leases_plan_status": (
                "CREATE INDEX IF NOT EXISTS "
                "ix_execution_task_runtime_leases_plan_status "
                "ON execution_task_runtime_leases (execution_plan_id, lease_status)"
            ),
            "ix_execution_task_runtime_leases_worker_instance": (
                "CREATE INDEX IF NOT EXISTS "
                "ix_execution_task_runtime_leases_worker_instance "
                "ON execution_task_runtime_leases (worker_instance_id)"
            ),
        }
        for statement in indexes.values():
            connection.execute(text(statement))


def _migration_037_execution_task_runtime_evidence(engine: Engine) -> None:
    """Add Phase 29C-6B start, progress, and canonical outcome evidence."""

    table_names = _table_names(engine)
    with _migration_transaction(engine, table_rebuild=True) as connection:
        if "execution_task_attempts" in table_names:
            # The Phase 29C-5 table has a status CHECK that predates the
            # attempt-local candidate_completed state.  Rebuild only this
            # additive authority table, preserving every existing value.
            connection.execute(
                text(
                    "ALTER TABLE execution_task_attempts "
                    "RENAME TO execution_task_attempts_phase29c5_old"
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
                        started_at DATETIME,
                        cancelled_at DATETIME,
                        updated_at DATETIME,
                        CONSTRAINT uq_execution_task_attempt_dispatch_intent
                            UNIQUE (dispatch_intent_id),
                        CONSTRAINT uq_execution_task_attempt_identity
                            UNIQUE (attempt_identity),
                        CONSTRAINT uq_execution_task_attempt_broker
                            UNIQUE (broker_task_id),
                        CONSTRAINT uq_execution_task_attempt_task_number
                            UNIQUE (execution_task_id, attempt_number),
                        CONSTRAINT ck_execution_task_attempt_number_positive
                            CHECK (attempt_number > 0),
                        CONSTRAINT ck_execution_task_attempt_status
                            CHECK (attempt_status IN (
                                'created', 'submitted', 'running',
                                'candidate_completed', 'cancelled', 'failed',
                                'succeeded'
                            )),
                        FOREIGN KEY(execution_plan_id)
                            REFERENCES execution_plans (id) ON DELETE CASCADE,
                        FOREIGN KEY(execution_task_id)
                            REFERENCES execution_tasks (id) ON DELETE CASCADE,
                        FOREIGN KEY(dispatch_intent_id)
                            REFERENCES execution_task_dispatch_intents (id)
                            ON DELETE CASCADE
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO execution_task_attempts (
                        id, execution_plan_id, execution_task_id,
                        dispatch_intent_id, attempt_number, attempt_identity,
                        broker_task_id, attempt_status, created_at, submitted_at,
                        started_at, cancelled_at, updated_at
                    )
                    SELECT id, execution_plan_id, execution_task_id,
                        dispatch_intent_id, attempt_number, attempt_identity,
                        broker_task_id, attempt_status, created_at, submitted_at,
                        started_at, cancelled_at, updated_at
                    FROM execution_task_attempts_phase29c5_old
                    """
                )
            )
            connection.execute(text("DROP TABLE execution_task_attempts_phase29c5_old"))

        if "execution_task_runtime_leases" in table_names:
            lease_columns = (
                (
                    "progress_state",
                    "ALTER TABLE execution_task_runtime_leases "
                    "ADD COLUMN progress_state VARCHAR(32)",
                ),
                (
                    "progress_sequence",
                    "ALTER TABLE execution_task_runtime_leases "
                    "ADD COLUMN progress_sequence INTEGER NOT NULL DEFAULT 0",
                ),
                (
                    "closed_at",
                    "ALTER TABLE execution_task_runtime_leases ADD COLUMN closed_at DATETIME",
                ),
                (
                    "closure_reason",
                    "ALTER TABLE execution_task_runtime_leases ADD COLUMN closure_reason VARCHAR(64)",
                ),
                (
                    "closed_outcome_id",
                    "ALTER TABLE execution_task_runtime_leases ADD COLUMN closed_outcome_id INTEGER",
                ),
                (
                    "closed_worker_instance_id",
                    "ALTER TABLE execution_task_runtime_leases ADD COLUMN closed_worker_instance_id VARCHAR(255)",
                ),
                (
                    "closed_ownership_fencing_token",
                    "ALTER TABLE execution_task_runtime_leases ADD COLUMN closed_ownership_fencing_token INTEGER",
                ),
                (
                    "canonical_closure_hash",
                    "ALTER TABLE execution_task_runtime_leases ADD COLUMN canonical_closure_hash VARCHAR(64)",
                ),
            )
            for column_name, ddl in lease_columns:
                if not _has_column(
                    engine, "execution_task_runtime_leases", column_name
                ):
                    connection.execute(text(ddl))

        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_task_runtime_starts (
                    id INTEGER PRIMARY KEY,
                    execution_plan_id INTEGER NOT NULL,
                    execution_task_id INTEGER NOT NULL,
                    execution_task_attempt_id INTEGER NOT NULL UNIQUE,
                    dispatch_intent_id INTEGER NOT NULL,
                    runtime_lease_id INTEGER NOT NULL UNIQUE,
                    broker_task_id VARCHAR(255) NOT NULL,
                    worker_instance_id VARCHAR(255) NOT NULL,
                    ownership_fencing_token INTEGER NOT NULL,
                    execution_start_idempotency_key VARCHAR(128) NOT NULL UNIQUE,
                    deterministic_start_command_id VARCHAR(128) NOT NULL UNIQUE,
                    canonical_start_command_payload JSON NOT NULL,
                    canonical_start_command_hash VARCHAR(64) NOT NULL,
                    runtime_adapter_name VARCHAR(64) NOT NULL,
                    adapter_version VARCHAR(64),
                    execution_mode VARCHAR(32) NOT NULL,
                    configuration_hash VARCHAR(64) NOT NULL,
                    provider_request_id VARCHAR(255),
                    started_at DATETIME NOT NULL,
                    lifecycle_state_at_start VARCHAR(20) NOT NULL,
                    lifecycle_state_version_at_start INTEGER NOT NULL,
                    creation_actor_type VARCHAR(32) NOT NULL,
                    creation_actor_id VARCHAR(255) NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT ck_execution_task_runtime_start_fence_positive
                        CHECK (ownership_fencing_token > 0),
                    CONSTRAINT ck_execution_task_runtime_start_state_version_nonnegative
                        CHECK (lifecycle_state_version_at_start >= 0),
                    FOREIGN KEY(execution_plan_id)
                        REFERENCES execution_plans (id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_task_id)
                        REFERENCES execution_tasks (id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_task_attempt_id)
                        REFERENCES execution_task_attempts (id) ON DELETE CASCADE,
                    FOREIGN KEY(dispatch_intent_id)
                        REFERENCES execution_task_dispatch_intents (id)
                        ON DELETE CASCADE,
                    FOREIGN KEY(runtime_lease_id)
                        REFERENCES execution_task_runtime_leases (id)
                        ON DELETE CASCADE
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_task_attempt_outcomes (
                    id INTEGER PRIMARY KEY,
                    execution_plan_id INTEGER NOT NULL,
                    execution_task_id INTEGER NOT NULL,
                    execution_task_attempt_id INTEGER NOT NULL UNIQUE,
                    dispatch_intent_id INTEGER NOT NULL,
                    runtime_lease_id INTEGER NOT NULL,
                    runtime_start_id INTEGER NOT NULL UNIQUE,
                    worker_instance_id VARCHAR(255) NOT NULL,
                    ownership_fencing_token INTEGER NOT NULL,
                    outcome_idempotency_key VARCHAR(128) NOT NULL UNIQUE,
                    deterministic_outcome_command_id VARCHAR(128) NOT NULL UNIQUE,
                    canonical_outcome_command_payload JSON NOT NULL,
                    canonical_outcome_command_hash VARCHAR(64) NOT NULL,
                    outcome_status VARCHAR(32) NOT NULL,
                    completed_at DATETIME NOT NULL,
                    runtime_duration_seconds FLOAT NOT NULL,
                    provider_request_id VARCHAR(255),
                    output_reference VARCHAR(512),
                    output_hash VARCHAR(64),
                    usage_summary JSON,
                    failure_category VARCHAR(64),
                    failure_code VARCHAR(64),
                    sanitized_failure_detail VARCHAR(1024),
                    exception_type VARCHAR(128),
                    diagnostics JSON,
                    lifecycle_transition_id INTEGER,
                    lifecycle_transition_sequence INTEGER,
                    lifecycle_resulting_state_version INTEGER,
                    lease_closed_at DATETIME,
                    lease_closure_reason VARCHAR(64),
                    lease_closure_hash VARCHAR(64),
                    creation_actor_type VARCHAR(32) NOT NULL,
                    creation_actor_id VARCHAR(255) NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT ck_execution_task_attempt_outcome_fence_positive
                        CHECK (ownership_fencing_token > 0),
                    CONSTRAINT ck_execution_task_attempt_outcome_status
                        CHECK (outcome_status IN (
                            'candidate_completed', 'attempt_failed',
                            'attempt_cancelled'
                        )),
                    CONSTRAINT ck_execution_task_attempt_outcome_duration_nonnegative
                        CHECK (runtime_duration_seconds >= 0),
                    FOREIGN KEY(execution_plan_id)
                        REFERENCES execution_plans (id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_task_id)
                        REFERENCES execution_tasks (id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_task_attempt_id)
                        REFERENCES execution_task_attempts (id) ON DELETE CASCADE,
                    FOREIGN KEY(dispatch_intent_id)
                        REFERENCES execution_task_dispatch_intents (id)
                        ON DELETE CASCADE,
                    FOREIGN KEY(runtime_lease_id)
                        REFERENCES execution_task_runtime_leases (id)
                        ON DELETE CASCADE,
                    FOREIGN KEY(runtime_start_id)
                        REFERENCES execution_task_runtime_starts (id)
                        ON DELETE CASCADE
                )
                """
            )
        )
        indexes = {
            "ix_execution_task_attempts_task_status": (
                "CREATE INDEX IF NOT EXISTS ix_execution_task_attempts_task_status "
                "ON execution_task_attempts (execution_task_id, attempt_status)"
            ),
            "ix_execution_task_runtime_leases_closed_outcome": (
                "CREATE INDEX IF NOT EXISTS ix_execution_task_runtime_leases_closed_outcome "
                "ON execution_task_runtime_leases (closed_outcome_id)"
            ),
            "ix_execution_task_runtime_starts_plan_task": (
                "CREATE INDEX IF NOT EXISTS ix_execution_task_runtime_starts_plan_task "
                "ON execution_task_runtime_starts (execution_plan_id, execution_task_id)"
            ),
            "ix_execution_task_runtime_starts_lease_worker": (
                "CREATE INDEX IF NOT EXISTS ix_execution_task_runtime_starts_lease_worker "
                "ON execution_task_runtime_starts (runtime_lease_id, worker_instance_id)"
            ),
            "ix_execution_task_attempt_outcomes_plan_status": (
                "CREATE INDEX IF NOT EXISTS ix_execution_task_attempt_outcomes_plan_status "
                "ON execution_task_attempt_outcomes (execution_plan_id, outcome_status)"
            ),
            "ix_execution_task_attempt_outcomes_task_completed": (
                "CREATE INDEX IF NOT EXISTS ix_execution_task_attempt_outcomes_task_completed "
                "ON execution_task_attempt_outcomes (execution_task_id, completed_at)"
            ),
        }
        for statement in indexes.values():
            connection.execute(text(statement))


def _migration_038_execution_task_validation_contract(engine: Engine) -> None:
    """Add immutable release-bound validation-contract authority.

    Existing tasks receive compatibility rows marked ``legacy_unstructured``.
    The migration copies only the already-persisted ``done_when`` value; it
    never creates predicates, validation runs, acceptance decisions, or
    lifecycle transitions.
    """

    table_names = _table_names(engine)
    if "execution_tasks" in table_names:
        with engine.begin() as connection:
            if not _has_column(engine, "execution_tasks", "validation_contract_status"):
                connection.execute(
                    text(
                        "ALTER TABLE execution_tasks ADD COLUMN "
                        "validation_contract_status VARCHAR(32) NOT NULL "
                        "DEFAULT 'legacy_unstructured'"
                    )
                )
            if not _has_column(engine, "execution_tasks", "validation_contract_id"):
                connection.execute(
                    text(
                        "ALTER TABLE execution_tasks ADD COLUMN "
                        "validation_contract_id INTEGER"
                    )
                )

    if "execution_plans" in table_names:
        with engine.begin() as connection:
            if not _has_column(
                engine, "execution_plans", "validation_contract_set_hash"
            ):
                connection.execute(
                    text(
                        "ALTER TABLE execution_plans ADD COLUMN "
                        "validation_contract_set_hash VARCHAR(64)"
                    )
                )

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_task_validation_specifications (
                    id INTEGER PRIMARY KEY,
                    execution_plan_id INTEGER NOT NULL,
                    execution_task_id INTEGER NOT NULL UNIQUE,
                    release_generation INTEGER NOT NULL,
                    contract_status VARCHAR(32) NOT NULL,
                    schema_version VARCHAR(96) NOT NULL,
                    original_done_when JSON NOT NULL,
                    structured_contract JSON,
                    pass_policy JSON,
                    review_requirement JSON,
                    environment_identity JSON,
                    validator_set_identity VARCHAR(128),
                    canonical_payload JSON NOT NULL,
                    canonical_specification_hash VARCHAR(64) NOT NULL,
                    hash_algorithm VARCHAR(16) NOT NULL DEFAULT 'sha256',
                    specification_source VARCHAR(64) NOT NULL,
                    release_authority_reference VARCHAR(128) NOT NULL,
                    creation_actor_type VARCHAR(64) NOT NULL,
                    creation_actor_id VARCHAR(255) NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_execution_task_validation_release_generation
                        UNIQUE (execution_plan_id, execution_task_id, release_generation),
                    CONSTRAINT ck_execution_task_validation_generation_positive
                        CHECK (release_generation > 0),
                    CONSTRAINT ck_execution_task_validation_status
                        CHECK (contract_status IN (
                            'structured_executable', 'legacy_unstructured',
                            'validation_not_required', 'unsupported'
                        )),
                    CONSTRAINT ck_execution_task_validation_hash_algorithm
                        CHECK (hash_algorithm = 'sha256'),
                    FOREIGN KEY(execution_plan_id)
                        REFERENCES execution_plans (id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_task_id)
                        REFERENCES execution_tasks (id) ON DELETE CASCADE
                )
                """
            )
        )
        indexes = {
            "ix_execution_task_validation_plan_status": (
                "CREATE INDEX IF NOT EXISTS ix_execution_task_validation_plan_status "
                "ON execution_task_validation_specifications "
                "(execution_plan_id, contract_status)"
            ),
            "ix_execution_task_validation_hash": (
                "CREATE INDEX IF NOT EXISTS ix_execution_task_validation_hash "
                "ON execution_task_validation_specifications "
                "(canonical_specification_hash)"
            ),
            "ix_execution_tasks_validation_contract_id": (
                "CREATE INDEX IF NOT EXISTS ix_execution_tasks_validation_contract_id "
                "ON execution_tasks (validation_contract_id)"
            ),
            "ix_execution_plans_validation_contract_set_hash": (
                "CREATE INDEX IF NOT EXISTS ix_execution_plans_validation_contract_set_hash "
                "ON execution_plans (validation_contract_set_hash)"
            ),
        }
        for statement in indexes.values():
            if (
                "ON execution_tasks " in statement
                and "execution_tasks" not in table_names
            ):
                continue
            if (
                "ON execution_plans " in statement
                and "execution_plans" not in table_names
            ):
                continue
            connection.execute(text(statement))

        if "execution_tasks" not in table_names:
            return

        task_rows = connection.execute(
            text(
                "SELECT id, execution_plan_id, done_when, "
                "validation_contract_id FROM execution_tasks ORDER BY id"
            )
        ).fetchall()
        for task_id, plan_id, done_when_raw, existing_spec_id in task_rows:
            if existing_spec_id is not None:
                continue
            try:
                done_when = (
                    json.loads(done_when_raw)
                    if isinstance(done_when_raw, str)
                    else done_when_raw
                )
            except (TypeError, ValueError):
                done_when = done_when_raw
            payload = {
                "canonicalization_version": "execution-task-validation-canonical/1",
                "schema_version": "execution-task-validation-contract/1.0",
                "contract_status": "legacy_unstructured",
                "original_done_when": done_when,
                "structured_contract": None,
            }
            canonical_bytes = json.dumps(
                _migration_038_normalize(payload),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            specification_hash = hashlib.sha256(canonical_bytes).hexdigest()
            connection.execute(
                text(
                    """
                    INSERT INTO execution_task_validation_specifications (
                        execution_plan_id, execution_task_id, release_generation,
                        contract_status, schema_version, original_done_when,
                        structured_contract, canonical_payload,
                        canonical_specification_hash, hash_algorithm,
                        specification_source, release_authority_reference,
                        creation_actor_type, creation_actor_id
                    )
                    SELECT id, :task_id,
                        COALESCE(generation, 1), 'legacy_unstructured',
                        'execution-task-validation-contract/1.0', :done_when,
                        NULL, :canonical_payload, :specification_hash, 'sha256',
                        'legacy_compatibility',
                        source_commit_identity, 'schema_migration', '038'
                    FROM execution_plans WHERE id = :plan_id
                    """
                ),
                {
                    "task_id": task_id,
                    "plan_id": plan_id,
                    "done_when": json.dumps(done_when, ensure_ascii=False),
                    "canonical_payload": json.dumps(payload, ensure_ascii=False),
                    "specification_hash": specification_hash,
                },
            )
            # A pre-authority fixture may contain an orphaned task row.  It
            # has no legal immutable plan binding, so leave it untouched and
            # fail closed rather than fabricate a contract authority.
            specification_id = connection.execute(
                text(
                    "SELECT id FROM execution_task_validation_specifications "
                    "WHERE execution_task_id = :task_id"
                ),
                {"task_id": task_id},
            ).scalar_one_or_none()
            if specification_id is None:
                continue
            connection.execute(
                text(
                    "UPDATE execution_tasks SET validation_contract_status = "
                    "'legacy_unstructured', validation_contract_id = :specification_id "
                    "WHERE id = :task_id"
                ),
                {"specification_id": specification_id, "task_id": task_id},
            )

        plan_rows = connection.execute(
            text("SELECT id FROM execution_plans ORDER BY id")
        ).fetchall()
        for (plan_id,) in plan_rows:
            contract_rows = connection.execute(
                text(
                    """
                    SELECT t.plan_task_id, s.contract_status,
                        s.canonical_specification_hash
                    FROM execution_tasks t
                    JOIN execution_task_validation_specifications s
                        ON s.execution_task_id = t.id
                    WHERE t.execution_plan_id = :plan_id
                    ORDER BY t.plan_task_id
                    """
                ),
                {"plan_id": plan_id},
            ).fetchall()
            set_payload = [
                {
                    "plan_task_id": str(row[0]),
                    "contract_status": str(row[1]),
                    "specification_hash": str(row[2]),
                }
                for row in contract_rows
            ]
            set_bytes = json.dumps(
                _migration_038_normalize(set_payload),
                ensure_ascii=False,
                sort_keys=True,
                separators=(",", ":"),
            ).encode("utf-8")
            connection.execute(
                text(
                    "UPDATE execution_plans SET validation_contract_set_hash = "
                    ":set_hash WHERE id = :plan_id"
                ),
                {
                    "set_hash": hashlib.sha256(set_bytes).hexdigest(),
                    "plan_id": plan_id,
                },
            )


def _migration_039_execution_task_validation_primitives(engine: Engine) -> None:
    """Add read-only evidence snapshots and deterministic predicate results.

    This migration creates empty authority tables only.  It deliberately does
    not resolve evidence, invoke validators, synthesize content, or alter task
    lifecycle state.
    """

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS
                    execution_task_resolved_validation_evidence (
                    id INTEGER PRIMARY KEY,
                    execution_plan_id INTEGER NOT NULL,
                    execution_task_id INTEGER NOT NULL,
                    execution_task_attempt_id INTEGER NOT NULL,
                    candidate_outcome_id INTEGER NOT NULL,
                    validation_specification_id INTEGER NOT NULL,
                    validation_specification_hash VARCHAR(64) NOT NULL,
                    evidence_key VARCHAR(64) NOT NULL,
                    evidence_type VARCHAR(64) NOT NULL,
                    source VARCHAR(64) NOT NULL,
                    normalized_reference VARCHAR(255) NOT NULL,
                    source_authority_id VARCHAR(128) NOT NULL,
                    resolver_id VARCHAR(64) NOT NULL,
                    resolver_version VARCHAR(64) NOT NULL,
                    resolver_contract_version VARCHAR(64) NOT NULL,
                    environment_configuration_hash VARCHAR(64) NOT NULL,
                    expected_hash_algorithm VARCHAR(16),
                    expected_hash VARCHAR(64),
                    actual_hash VARCHAR(64),
                    media_type VARCHAR(128),
                    byte_size INTEGER,
                    structured_metadata_summary JSON NOT NULL,
                    content_addressed_reference VARCHAR(255),
                    content_projection JSON,
                    expected_output_reference VARCHAR(512),
                    resolution_status VARCHAR(32) NOT NULL,
                    resolution_idempotency_key VARCHAR(128) NOT NULL UNIQUE,
                    deterministic_resolution_command_id VARCHAR(128) NOT NULL UNIQUE,
                    canonical_resolution_command_payload JSON NOT NULL,
                    canonical_resolution_command_hash VARCHAR(64) NOT NULL,
                    canonical_evidence_payload JSON NOT NULL,
                    canonical_evidence_payload_hash VARCHAR(64) NOT NULL,
                    task_state_at_resolution VARCHAR(20) NOT NULL,
                    task_state_version_at_resolution INTEGER NOT NULL,
                    resolved_at DATETIME NOT NULL,
                    creation_actor_type VARCHAR(64) NOT NULL,
                    creation_actor_id VARCHAR(255) NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT
                        uq_execution_task_resolved_evidence_candidate_spec_key
                        UNIQUE (
                            candidate_outcome_id,
                            validation_specification_id,
                            evidence_key
                        ),
                    CONSTRAINT ck_execution_task_resolved_evidence_status
                        CHECK (resolution_status IN (
                            'resolved', 'missing', 'hash_mismatch', 'unsupported',
                            'unavailable', 'invalid_reference', 'too_large',
                            'invalid_content'
                        )),
                    CONSTRAINT ck_execution_task_resolved_evidence_byte_size_nonnegative
                        CHECK (byte_size IS NULL OR byte_size >= 0),
                    CONSTRAINT ck_execution_task_resolved_evidence_state_version_nonnegative
                        CHECK (task_state_version_at_resolution >= 0),
                    FOREIGN KEY(execution_plan_id)
                        REFERENCES execution_plans (id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_task_id)
                        REFERENCES execution_tasks (id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_task_attempt_id)
                        REFERENCES execution_task_attempts (id) ON DELETE CASCADE,
                    FOREIGN KEY(candidate_outcome_id)
                        REFERENCES execution_task_attempt_outcomes (id)
                        ON DELETE CASCADE,
                    FOREIGN KEY(validation_specification_id)
                        REFERENCES execution_task_validation_specifications (id)
                        ON DELETE CASCADE
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS
                    execution_task_validation_predicate_results (
                    id INTEGER PRIMARY KEY,
                    execution_plan_id INTEGER NOT NULL,
                    execution_task_id INTEGER NOT NULL,
                    execution_task_attempt_id INTEGER NOT NULL,
                    candidate_outcome_id INTEGER NOT NULL,
                    validation_specification_id INTEGER NOT NULL,
                    validation_specification_hash VARCHAR(64) NOT NULL,
                    predicate_id VARCHAR(64) NOT NULL,
                    predicate_version INTEGER NOT NULL,
                    predicate_order INTEGER NOT NULL,
                    evidence_snapshot_id INTEGER NOT NULL,
                    evidence_key VARCHAR(64) NOT NULL,
                    validator_id VARCHAR(64) NOT NULL,
                    validator_version INTEGER NOT NULL,
                    validator_set_id VARCHAR(128) NOT NULL,
                    validator_set_version VARCHAR(64) NOT NULL,
                    environment_configuration_hash VARCHAR(64) NOT NULL,
                    result_status VARCHAR(32) NOT NULL,
                    passed BOOLEAN NOT NULL,
                    result_code VARCHAR(64) NOT NULL,
                    diagnostics JSON NOT NULL,
                    expected_summary JSON,
                    actual_summary JSON,
                    canonical_result_payload JSON NOT NULL,
                    canonical_result_hash VARCHAR(64) NOT NULL,
                    validator_idempotency_key VARCHAR(128) NOT NULL UNIQUE,
                    deterministic_validator_command_id VARCHAR(128) NOT NULL UNIQUE,
                    canonical_validator_command_payload JSON NOT NULL,
                    canonical_validator_command_hash VARCHAR(64) NOT NULL,
                    started_at DATETIME NOT NULL,
                    completed_at DATETIME NOT NULL,
                    creation_actor_type VARCHAR(64) NOT NULL,
                    creation_actor_id VARCHAR(255) NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT
                        uq_execution_task_validation_result_candidate_spec_predicate
                        UNIQUE (
                            candidate_outcome_id,
                            validation_specification_id,
                            predicate_id,
                            predicate_version
                        ),
                    CONSTRAINT
                        ck_execution_task_validation_result_predicate_version_positive
                        CHECK (predicate_version > 0),
                    CONSTRAINT
                        ck_execution_task_validation_result_validator_version_positive
                        CHECK (validator_version > 0),
                    CONSTRAINT ck_execution_task_validation_result_status
                        CHECK (result_status IN (
                            'passed', 'failed', 'missing_evidence',
                            'validator_error', 'unsupported', 'invalid_evidence'
                        )),
                    CONSTRAINT ck_execution_task_validation_result_passed_consistent
                        CHECK (
                            (result_status = 'passed' AND passed = 1)
                            OR (result_status <> 'passed' AND passed = 0)
                        ),
                    FOREIGN KEY(execution_plan_id)
                        REFERENCES execution_plans (id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_task_id)
                        REFERENCES execution_tasks (id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_task_attempt_id)
                        REFERENCES execution_task_attempts (id) ON DELETE CASCADE,
                    FOREIGN KEY(candidate_outcome_id)
                        REFERENCES execution_task_attempt_outcomes (id)
                        ON DELETE CASCADE,
                    FOREIGN KEY(validation_specification_id)
                        REFERENCES execution_task_validation_specifications (id)
                        ON DELETE CASCADE,
                    FOREIGN KEY(evidence_snapshot_id)
                        REFERENCES execution_task_resolved_validation_evidence (id)
                        ON DELETE CASCADE
                )
                """
            )
        )
        indexes = {
            "ix_execution_task_resolved_evidence_task_status": (
                "CREATE INDEX IF NOT EXISTS "
                "ix_execution_task_resolved_evidence_task_status "
                "ON execution_task_resolved_validation_evidence "
                "(execution_task_id, resolution_status)"
            ),
            "ix_execution_task_resolved_evidence_spec_key": (
                "CREATE INDEX IF NOT EXISTS "
                "ix_execution_task_resolved_evidence_spec_key "
                "ON execution_task_resolved_validation_evidence "
                "(validation_specification_id, evidence_key)"
            ),
            "ix_execution_task_validation_result_task_status": (
                "CREATE INDEX IF NOT EXISTS "
                "ix_execution_task_validation_result_task_status "
                "ON execution_task_validation_predicate_results "
                "(execution_task_id, result_status)"
            ),
            "ix_execution_task_validation_result_spec_predicate": (
                "CREATE INDEX IF NOT EXISTS "
                "ix_execution_task_validation_result_spec_predicate "
                "ON execution_task_validation_predicate_results "
                "(validation_specification_id, predicate_id, predicate_version)"
            ),
        }
        for statement in indexes.values():
            connection.execute(text(statement))


def _migration_040_execution_task_validation_runs_acceptance(engine: Engine) -> None:
    """Add empty validation-run and acceptance-decision authority tables.

    This migration is additive and deliberately creates no run, decision,
    evidence, predicate, lifecycle, retry, review, or dependency records.
    """

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_task_validation_runs (
                    id INTEGER PRIMARY KEY,
                    execution_plan_id INTEGER NOT NULL,
                    execution_task_id INTEGER NOT NULL,
                    execution_task_attempt_id INTEGER NOT NULL,
                    candidate_outcome_id INTEGER NOT NULL,
                    validation_specification_id INTEGER NOT NULL,
                    validation_specification_hash VARCHAR(64) NOT NULL,
                    validation_contract_set_hash VARCHAR(64) NOT NULL,
                    task_state_at_start VARCHAR(20) NOT NULL,
                    task_state_version_at_start INTEGER NOT NULL,
                    validation_run_generation INTEGER NOT NULL,
                    validation_idempotency_key VARCHAR(128) NOT NULL UNIQUE,
                    deterministic_validation_command_id VARCHAR(128) NOT NULL UNIQUE,
                    canonical_validation_command_payload JSON NOT NULL,
                    canonical_validation_command_hash VARCHAR(64) NOT NULL,
                    validator_set_id VARCHAR(128) NOT NULL,
                    validator_set_version VARCHAR(64) NOT NULL,
                    environment_configuration_hash VARCHAR(64) NOT NULL,
                    resolver_contract_version VARCHAR(64) NOT NULL,
                    started_at DATETIME NOT NULL,
                    completed_at DATETIME,
                    run_status VARCHAR(32) NOT NULL,
                    required_evidence_count INTEGER NOT NULL DEFAULT 0,
                    resolved_evidence_count INTEGER NOT NULL DEFAULT 0,
                    required_predicate_count INTEGER NOT NULL DEFAULT 0,
                    evaluated_predicate_count INTEGER NOT NULL DEFAULT 0,
                    passed_predicate_count INTEGER NOT NULL DEFAULT 0,
                    failed_predicate_count INTEGER NOT NULL DEFAULT 0,
                    missing_predicate_count INTEGER NOT NULL DEFAULT 0,
                    unsupported_predicate_count INTEGER NOT NULL DEFAULT 0,
                    validator_error_count INTEGER NOT NULL DEFAULT 0,
                    invalid_evidence_count INTEGER NOT NULL DEFAULT 0,
                    pass_policy_result VARCHAR(32),
                    review_requirement VARCHAR(32),
                    review_result JSON,
                    final_validation_classification VARCHAR(32),
                    aggregate_evidence_hash VARCHAR(64),
                    aggregate_predicate_result_hash VARCHAR(64),
                    canonical_result_payload JSON,
                    canonical_result_hash VARCHAR(64),
                    acceptance_decision_id INTEGER UNIQUE,
                    lifecycle_transition_id INTEGER,
                    lifecycle_transition_sequence INTEGER,
                    bounded_reason VARCHAR(64),
                    bounded_detail VARCHAR(1024),
                    creation_actor_type VARCHAR(64) NOT NULL,
                    creation_actor_id VARCHAR(255) NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_execution_task_validation_run_candidate_spec_generation
                        UNIQUE (
                            candidate_outcome_id,
                            validation_specification_id,
                            validation_run_generation
                        ),
                    CONSTRAINT ck_execution_task_validation_run_generation_positive
                        CHECK (validation_run_generation > 0),
                    CONSTRAINT ck_execution_task_validation_run_state_version_nonnegative
                        CHECK (task_state_version_at_start >= 0),
                    CONSTRAINT ck_execution_task_validation_run_status
                        CHECK (run_status IN (
                            'pending', 'running', 'completed', 'blocked',
                            'validation_error', 'review_required', 'accepted',
                            'rejected', 'cancelled'
                        )),
                    FOREIGN KEY(execution_plan_id)
                        REFERENCES execution_plans (id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_task_id)
                        REFERENCES execution_tasks (id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_task_attempt_id)
                        REFERENCES execution_task_attempts (id) ON DELETE CASCADE,
                    FOREIGN KEY(candidate_outcome_id)
                        REFERENCES execution_task_attempt_outcomes (id)
                        ON DELETE CASCADE,
                    FOREIGN KEY(validation_specification_id)
                        REFERENCES execution_task_validation_specifications (id)
                        ON DELETE CASCADE
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_task_acceptance_decisions (
                    id INTEGER PRIMARY KEY,
                    execution_plan_id INTEGER NOT NULL,
                    execution_task_id INTEGER NOT NULL,
                    execution_task_attempt_id INTEGER NOT NULL,
                    candidate_outcome_id INTEGER NOT NULL,
                    validation_specification_id INTEGER NOT NULL,
                    validation_specification_hash VARCHAR(64) NOT NULL,
                    validation_run_id INTEGER NOT NULL UNIQUE,
                    validation_run_result_hash VARCHAR(64) NOT NULL,
                    aggregate_evidence_hash VARCHAR(64) NOT NULL,
                    aggregate_predicate_result_hash VARCHAR(64) NOT NULL,
                    pass_policy_id VARCHAR(64) NOT NULL,
                    pass_policy_version INTEGER NOT NULL,
                    pass_policy_result VARCHAR(32) NOT NULL,
                    review_requirement VARCHAR(32) NOT NULL,
                    review_result JSON NOT NULL,
                    review_reference VARCHAR(128),
                    decision_status VARCHAR(32) NOT NULL,
                    decision_idempotency_key VARCHAR(128) NOT NULL UNIQUE,
                    deterministic_decision_command_id VARCHAR(128) NOT NULL UNIQUE,
                    canonical_decision_command_payload JSON NOT NULL,
                    canonical_decision_command_hash VARCHAR(64) NOT NULL,
                    canonical_decision_payload JSON NOT NULL,
                    canonical_decision_hash VARCHAR(64) NOT NULL,
                    decision_reason VARCHAR(64) NOT NULL,
                    bounded_detail VARCHAR(1024),
                    decision_actor_type VARCHAR(64) NOT NULL,
                    decision_actor_id VARCHAR(255) NOT NULL,
                    decided_at DATETIME NOT NULL,
                    lifecycle_transition_id INTEGER,
                    lifecycle_transition_sequence INTEGER,
                    resulting_task_state VARCHAR(20) NOT NULL,
                    resulting_task_state_version INTEGER NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_execution_task_acceptance_candidate_spec
                        UNIQUE (candidate_outcome_id, validation_specification_id),
                    CONSTRAINT ck_execution_task_acceptance_policy_version_positive
                        CHECK (pass_policy_version > 0),
                    CONSTRAINT ck_execution_task_acceptance_state_version_nonnegative
                        CHECK (resulting_task_state_version >= 0),
                    CONSTRAINT ck_execution_task_acceptance_decision_status
                        CHECK (decision_status IN (
                            'accepted', 'rejected', 'blocked',
                            'validation_error', 'review_required'
                        )),
                    FOREIGN KEY(execution_plan_id)
                        REFERENCES execution_plans (id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_task_id)
                        REFERENCES execution_tasks (id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_task_attempt_id)
                        REFERENCES execution_task_attempts (id) ON DELETE CASCADE,
                    FOREIGN KEY(candidate_outcome_id)
                        REFERENCES execution_task_attempt_outcomes (id)
                        ON DELETE CASCADE,
                    FOREIGN KEY(validation_specification_id)
                        REFERENCES execution_task_validation_specifications (id)
                        ON DELETE CASCADE,
                    FOREIGN KEY(validation_run_id)
                        REFERENCES execution_task_validation_runs (id)
                        ON DELETE CASCADE
                )
                """
            )
        )
        indexes = {
            "ix_execution_task_validation_runs_task_status": (
                "CREATE INDEX IF NOT EXISTS "
                "ix_execution_task_validation_runs_task_status "
                "ON execution_task_validation_runs (execution_task_id, run_status)"
            ),
            "ix_execution_task_validation_runs_candidate_spec": (
                "CREATE INDEX IF NOT EXISTS "
                "ix_execution_task_validation_runs_candidate_spec "
                "ON execution_task_validation_runs "
                "(candidate_outcome_id, validation_specification_id)"
            ),
            "ix_execution_task_acceptance_task_status": (
                "CREATE INDEX IF NOT EXISTS "
                "ix_execution_task_acceptance_task_status "
                "ON execution_task_acceptance_decisions "
                "(execution_task_id, decision_status)"
            ),
            "ix_execution_task_acceptance_plan_status": (
                "CREATE INDEX IF NOT EXISTS "
                "ix_execution_task_acceptance_plan_status "
                "ON execution_task_acceptance_decisions "
                "(execution_plan_id, decision_status)"
            ),
        }
        for statement in indexes.values():
            connection.execute(text(statement))


def _migration_041_execution_task_recovery_boundary(engine: Engine) -> None:
    """Add empty Phase 29C-8 recovery authority and pre-dispatch lineage.

    This migration is additive.  Existing attempts and all prior evidence are
    copied byte-for-byte into the rebuilt attempt table when SQLite needs a
    nullable dispatch link; no recovery history or lifecycle projection is
    inferred.
    """

    table_names = _table_names(engine)
    with _migration_transaction(engine, table_rebuild=True) as connection:
        if "execution_plans" in table_names:
            for column_name, ddl in (
                (
                    "recovery_policy_id",
                    "ALTER TABLE execution_plans ADD COLUMN recovery_policy_id VARCHAR(64)",
                ),
                (
                    "recovery_policy_version",
                    "ALTER TABLE execution_plans ADD COLUMN recovery_policy_version INTEGER",
                ),
            ):
                if not _has_column(engine, "execution_plans", column_name):
                    connection.execute(text(ddl))

        if "execution_task_attempts" in table_names and not _column_nullable(
            engine, "execution_task_attempts", "dispatch_intent_id"
        ):
            connection.execute(
                text(
                    "ALTER TABLE execution_task_attempts "
                    "RENAME TO execution_task_attempts_phase29c8_old"
                )
            )
            connection.execute(
                text(
                    """
                    CREATE TABLE execution_task_attempts (
                        id INTEGER PRIMARY KEY,
                        execution_plan_id INTEGER NOT NULL,
                        execution_task_id INTEGER NOT NULL,
                        dispatch_intent_id INTEGER,
                        attempt_number INTEGER NOT NULL,
                        attempt_identity VARCHAR(128) NOT NULL,
                        broker_task_id VARCHAR(255),
                        predecessor_attempt_id INTEGER,
                        recovery_authorization_id INTEGER,
                        recovery_generation INTEGER,
                        replacement_reason VARCHAR(64),
                        strategy_id VARCHAR(64),
                        strategy_version INTEGER,
                        strategy_parameter_hash VARCHAR(64),
                        attempt_status VARCHAR(24) NOT NULL DEFAULT 'created',
                        created_at DATETIME NOT NULL,
                        submitted_at DATETIME,
                        started_at DATETIME,
                        cancelled_at DATETIME,
                        updated_at DATETIME,
                        CONSTRAINT uq_execution_task_attempt_dispatch_intent
                            UNIQUE (dispatch_intent_id),
                        CONSTRAINT uq_execution_task_attempt_identity
                            UNIQUE (attempt_identity),
                        CONSTRAINT uq_execution_task_attempt_broker
                            UNIQUE (broker_task_id),
                        CONSTRAINT uq_execution_task_attempt_task_number
                            UNIQUE (execution_task_id, attempt_number),
                        CONSTRAINT uq_execution_task_attempt_recovery_authorization
                            UNIQUE (recovery_authorization_id),
                        CONSTRAINT ck_execution_task_attempt_number_positive
                            CHECK (attempt_number > 0),
                        CONSTRAINT ck_execution_task_attempt_status
                            CHECK (attempt_status IN (
                                'created', 'submitted', 'running',
                                'candidate_completed', 'cancelled', 'failed',
                                'succeeded'
                            )),
                        FOREIGN KEY(execution_plan_id)
                            REFERENCES execution_plans (id) ON DELETE CASCADE,
                        FOREIGN KEY(execution_task_id)
                            REFERENCES execution_tasks (id) ON DELETE CASCADE,
                        FOREIGN KEY(dispatch_intent_id)
                            REFERENCES execution_task_dispatch_intents (id)
                            ON DELETE CASCADE,
                    FOREIGN KEY(predecessor_attempt_id)
                        REFERENCES execution_task_attempts (id)
                            ON DELETE CASCADE
                    )
                    """
                )
            )
            connection.execute(
                text(
                    """
                    INSERT INTO execution_task_attempts (
                        id, execution_plan_id, execution_task_id,
                        dispatch_intent_id, attempt_number, attempt_identity,
                        broker_task_id, attempt_status, created_at, submitted_at,
                        started_at, cancelled_at, updated_at
                    )
                    SELECT id, execution_plan_id, execution_task_id,
                        dispatch_intent_id, attempt_number, attempt_identity,
                        broker_task_id, attempt_status, created_at, submitted_at,
                        started_at, cancelled_at, updated_at
                    FROM execution_task_attempts_phase29c8_old
                    """
                )
            )
            connection.execute(text("DROP TABLE execution_task_attempts_phase29c8_old"))

        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_task_recovery_inputs (
                    id INTEGER PRIMARY KEY,
                    execution_plan_id INTEGER NOT NULL,
                    execution_task_id INTEGER NOT NULL,
                    failed_attempt_id INTEGER NOT NULL,
                    attempt_generation INTEGER NOT NULL,
                    runtime_outcome_id INTEGER,
                    validation_run_id INTEGER,
                    acceptance_decision_id INTEGER,
                    recovery_source VARCHAR(64) NOT NULL,
                    failure_category VARCHAR(64) NOT NULL,
                    failure_code VARCHAR(64),
                    exception_type VARCHAR(128),
                    provider_request_id VARCHAR(255),
                    failed_predicate_summary JSON,
                    aggregate_evidence_hash VARCHAR(64),
                    aggregate_predicate_result_hash VARCHAR(64),
                    lifecycle_transition_id INTEGER NOT NULL,
                    lifecycle_transition_sequence INTEGER NOT NULL,
                    task_state_at_creation VARCHAR(20) NOT NULL,
                    task_state_version_at_creation INTEGER NOT NULL,
                    prior_recovery_authorization_id INTEGER,
                    retry_count INTEGER NOT NULL,
                    recovery_generation INTEGER NOT NULL,
                    canonical_input_payload JSON NOT NULL,
                    canonical_input_hash VARCHAR(64) NOT NULL,
                    input_idempotency_key VARCHAR(128) NOT NULL UNIQUE,
                    creation_actor_type VARCHAR(64) NOT NULL,
                    creation_actor_id VARCHAR(255) NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_execution_task_recovery_input_task_generation
                        UNIQUE (execution_task_id, recovery_generation),
                    CONSTRAINT uq_execution_task_recovery_input_transition
                        UNIQUE (lifecycle_transition_id),
                    CONSTRAINT ck_execution_task_recovery_input_generation_positive
                        CHECK (attempt_generation > 0 AND recovery_generation > 0),
                    CONSTRAINT ck_execution_task_recovery_input_counts_nonnegative
                        CHECK (retry_count >= 0 AND task_state_version_at_creation >= 0),
                    FOREIGN KEY(execution_plan_id)
                        REFERENCES execution_plans (id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_task_id)
                        REFERENCES execution_tasks (id) ON DELETE CASCADE,
                    FOREIGN KEY(failed_attempt_id)
                        REFERENCES execution_task_attempts (id) ON DELETE CASCADE,
                    FOREIGN KEY(runtime_outcome_id)
                        REFERENCES execution_task_attempt_outcomes (id) ON DELETE CASCADE,
                    FOREIGN KEY(validation_run_id)
                        REFERENCES execution_task_validation_runs (id) ON DELETE CASCADE,
                    FOREIGN KEY(acceptance_decision_id)
                        REFERENCES execution_task_acceptance_decisions (id) ON DELETE CASCADE,
                    FOREIGN KEY(lifecycle_transition_id)
                        REFERENCES execution_task_transitions (id) ON DELETE CASCADE
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_task_recovery_authorizations (
                    id INTEGER PRIMARY KEY,
                    execution_plan_id INTEGER NOT NULL,
                    execution_task_id INTEGER NOT NULL,
                    recovery_input_id INTEGER NOT NULL UNIQUE,
                    failed_attempt_id INTEGER NOT NULL,
                    recovery_generation INTEGER NOT NULL,
                    policy_id VARCHAR(64) NOT NULL,
                    policy_version INTEGER NOT NULL,
                    strategy_id VARCHAR(64),
                    strategy_version INTEGER,
                    authorization_status VARCHAR(32) NOT NULL,
                    decision_classification VARCHAR(64) NOT NULL,
                    decision_reason VARCHAR(64) NOT NULL,
                    retry_budget_before INTEGER NOT NULL,
                    retry_budget_after INTEGER NOT NULL,
                    next_attempt_generation INTEGER,
                    strategy_parameters JSON,
                    strategy_parameter_hash VARCHAR(64),
                    not_before DATETIME,
                    backoff_policy_id VARCHAR(64),
                    backoff_policy_version INTEGER,
                    operator_required BOOLEAN NOT NULL DEFAULT 0,
                    authorization_idempotency_key VARCHAR(128) NOT NULL UNIQUE,
                    deterministic_authorization_command_id VARCHAR(128) NOT NULL UNIQUE,
                    canonical_authorization_command_payload JSON NOT NULL,
                    canonical_authorization_command_hash VARCHAR(64) NOT NULL,
                    canonical_authorization_payload JSON NOT NULL,
                    canonical_authorization_hash VARCHAR(64) NOT NULL,
                    lifecycle_transition_id INTEGER,
                    lifecycle_transition_sequence INTEGER,
                    replacement_attempt_id INTEGER UNIQUE,
                    resulting_task_state VARCHAR(20) NOT NULL,
                    resulting_task_state_version INTEGER NOT NULL,
                    decision_actor_type VARCHAR(64) NOT NULL,
                    decision_actor_id VARCHAR(255) NOT NULL,
                    authorized_at DATETIME NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_execution_task_recovery_authorization_task_generation
                        UNIQUE (execution_task_id, recovery_generation),
                    CONSTRAINT ck_execution_task_recovery_authorization_generation_positive
                        CHECK (recovery_generation > 0 AND policy_version > 0),
                    CONSTRAINT ck_execution_task_recovery_authorization_budget_nonnegative
                        CHECK (retry_budget_before >= 0 AND retry_budget_after >= 0),
                    CONSTRAINT ck_execution_task_recovery_authorization_next_generation_positive
                        CHECK (next_attempt_generation IS NULL OR next_attempt_generation > 0),
                    CONSTRAINT ck_execution_task_recovery_authorization_status
                        CHECK (authorization_status IN (
                            'authorized', 'operator_required', 'exhausted',
                            'non_retryable', 'blocked', 'error', 'cancelled'
                        )),
                    FOREIGN KEY(execution_plan_id)
                        REFERENCES execution_plans (id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_task_id)
                        REFERENCES execution_tasks (id) ON DELETE CASCADE,
                    FOREIGN KEY(recovery_input_id)
                        REFERENCES execution_task_recovery_inputs (id) ON DELETE CASCADE,
                    FOREIGN KEY(failed_attempt_id)
                        REFERENCES execution_task_attempts (id) ON DELETE CASCADE,
                    FOREIGN KEY(lifecycle_transition_id)
                        REFERENCES execution_task_transitions (id) ON DELETE CASCADE,
                    FOREIGN KEY(replacement_attempt_id)
                        REFERENCES execution_task_attempts (id) ON DELETE CASCADE
                )
                """
            )
        )
        for statement in (
            "CREATE INDEX IF NOT EXISTS ix_execution_task_recovery_inputs_task_source "
            "ON execution_task_recovery_inputs (execution_task_id, recovery_source)",
            "CREATE INDEX IF NOT EXISTS ix_execution_task_recovery_inputs_task_generation "
            "ON execution_task_recovery_inputs (execution_task_id, recovery_generation)",
            "CREATE INDEX IF NOT EXISTS ix_execution_task_recovery_authorizations_task_status "
            "ON execution_task_recovery_authorizations (execution_task_id, authorization_status)",
            "CREATE INDEX IF NOT EXISTS ix_execution_task_recovery_authorizations_policy "
            "ON execution_task_recovery_authorizations (policy_id, policy_version)",
            "CREATE INDEX IF NOT EXISTS ix_execution_task_attempts_predecessor "
            "ON execution_task_attempts (predecessor_attempt_id)",
        ):
            connection.execute(text(statement))


def _migration_042_execution_task_candidate_content_boundary(engine: Engine) -> None:
    """Add empty Phase 29C-9 candidate-content authority.

    This migration deliberately creates only tables and indexes.  It never
    reads runtime references, fetches bytes, infers media types, recomputes
    historical hashes, or creates historical content rows.
    """

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_task_candidate_contents (
                    id INTEGER PRIMARY KEY,
                    execution_plan_id INTEGER NOT NULL,
                    execution_task_id INTEGER NOT NULL,
                    execution_task_attempt_id INTEGER NOT NULL,
                    attempt_generation INTEGER NOT NULL,
                    candidate_outcome_id INTEGER NOT NULL UNIQUE,
                    content_sha256 VARCHAR(64) NOT NULL,
                    declared_sha256 VARCHAR(64),
                    byte_length INTEGER NOT NULL,
                    media_type VARCHAR(64) NOT NULL,
                    storage_backend_id VARCHAR(64) NOT NULL,
                    storage_backend_version VARCHAR(32) NOT NULL,
                    storage_key VARCHAR(160) NOT NULL,
                    ingestion_idempotency_key VARCHAR(128) NOT NULL UNIQUE,
                    canonical_ingestion_command_payload JSON NOT NULL,
                    canonical_ingestion_command_hash VARCHAR(64) NOT NULL,
                    canonical_metadata_payload JSON NOT NULL,
                    canonical_metadata_hash VARCHAR(64) NOT NULL,
                    content_projection JSON,
                    content_projection_hash VARCHAR(64),
                    content_projection_version VARCHAR(64),
                    creation_actor_type VARCHAR(64) NOT NULL,
                    creation_actor_id VARCHAR(255) NOT NULL,
                    created_at DATETIME NOT NULL,
                    CONSTRAINT uq_execution_task_candidate_content_outcome
                        UNIQUE (execution_task_id, candidate_outcome_id),
                    CONSTRAINT ck_execution_task_candidate_content_generation_positive
                        CHECK (attempt_generation > 0),
                    CONSTRAINT ck_execution_task_candidate_content_length_nonnegative
                        CHECK (byte_length >= 0),
                    FOREIGN KEY(execution_plan_id)
                        REFERENCES execution_plans (id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_task_id)
                        REFERENCES execution_tasks (id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_task_attempt_id)
                        REFERENCES execution_task_attempts (id) ON DELETE CASCADE,
                    FOREIGN KEY(candidate_outcome_id)
                        REFERENCES execution_task_attempt_outcomes (id)
                        ON DELETE CASCADE
                )
                """
            )
        )
        for statement in (
            "CREATE INDEX IF NOT EXISTS ix_execution_task_candidate_contents_task_hash "
            "ON execution_task_candidate_contents (execution_task_id, content_sha256)",
            "CREATE INDEX IF NOT EXISTS ix_execution_task_candidate_contents_plan_created "
            "ON execution_task_candidate_contents (execution_plan_id, created_at)",
        ):
            connection.execute(text(statement))


def _migration_043_execution_validation_schema_authority(engine: Engine) -> None:
    """Add empty immutable schema authority and nullable release linkage.

    This migration creates no schemas, changes no existing contract, and
    performs no validation.  Nullable linkage preserves legacy and historical
    schema-free validation specifications exactly.
    """

    table_names = _table_names(engine)
    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_validation_schemas (
                    id INTEGER PRIMARY KEY,
                    schema_id VARCHAR(71) NOT NULL UNIQUE,
                    schema_type VARCHAR(32) NOT NULL,
                    schema_version VARCHAR(96) NOT NULL,
                    dialect VARCHAR(255) NOT NULL,
                    canonical_schema_payload JSON NOT NULL,
                    schema_sha256 VARCHAR(71) NOT NULL UNIQUE,
                    schema_size_bytes INTEGER NOT NULL,
                    schema_depth INTEGER NOT NULL,
                    schema_object_members INTEGER NOT NULL,
                    schema_array_length INTEGER NOT NULL,
                    schema_max_string_length INTEGER NOT NULL,
                    schema_reference_count INTEGER NOT NULL,
                    schema_regex_length INTEGER NOT NULL,
                    storage_backend_id VARCHAR(64) NOT NULL,
                    storage_backend_version VARCHAR(32) NOT NULL,
                    logical_name VARCHAR(128),
                    logical_version VARCHAR(64),
                    idempotency_key VARCHAR(128) NOT NULL UNIQUE,
                    canonical_command_payload JSON NOT NULL,
                    canonical_command_hash VARCHAR(64) NOT NULL,
                    canonical_metadata_payload JSON NOT NULL,
                    canonical_metadata_hash VARCHAR(64) NOT NULL,
                    creation_actor_type VARCHAR(64) NOT NULL,
                    creation_actor_id VARCHAR(255) NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT ck_execution_validation_schema_type
                        CHECK (schema_type = 'json_schema'),
                    CONSTRAINT ck_execution_validation_schema_bounds_nonnegative
                        CHECK (
                            schema_size_bytes >= 0 AND schema_depth >= 0
                            AND schema_object_members >= 0
                            AND schema_array_length >= 0
                            AND schema_max_string_length >= 0
                            AND schema_reference_count >= 0
                            AND schema_regex_length >= 0
                        )
                )
                """
            )
        )
        if "execution_task_validation_specifications" in table_names:
            for column_name, ddl in (
                (
                    "validation_schema_id",
                    "ALTER TABLE execution_task_validation_specifications "
                    "ADD COLUMN validation_schema_id INTEGER",
                ),
                (
                    "validation_schema_reference",
                    "ALTER TABLE execution_task_validation_specifications "
                    "ADD COLUMN validation_schema_reference VARCHAR(96)",
                ),
                (
                    "validation_schema_hash",
                    "ALTER TABLE execution_task_validation_specifications "
                    "ADD COLUMN validation_schema_hash VARCHAR(71)",
                ),
                (
                    "validation_schema_dialect",
                    "ALTER TABLE execution_task_validation_specifications "
                    "ADD COLUMN validation_schema_dialect VARCHAR(255)",
                ),
            ):
                if not _has_column(
                    engine, "execution_task_validation_specifications", column_name
                ):
                    connection.execute(text(ddl))
        for statement in (
            "CREATE INDEX IF NOT EXISTS ix_execution_validation_schemas_dialect "
            "ON execution_validation_schemas (dialect)",
            "CREATE INDEX IF NOT EXISTS "
            "ix_execution_task_validation_specifications_schema "
            "ON execution_task_validation_specifications "
            "(validation_schema_id, validation_schema_hash)",
        ):
            connection.execute(text(statement))


def _migration_044_execution_evidence_authority(engine: Engine) -> None:
    """Add empty Phase 29C-11 immutable execution evidence authority.

    This migration creates only the table and indexes.  It never fabricates
    evidence rows, executes commands or tests, or touches C9/C10 tables.
    """

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_evidence (
                    id INTEGER PRIMARY KEY,
                    execution_plan_id INTEGER NOT NULL,
                    execution_task_id INTEGER NOT NULL,
                    execution_task_attempt_id INTEGER NOT NULL,
                    attempt_generation INTEGER NOT NULL,
                    evidence_kind VARCHAR(32) NOT NULL,
                    producer_id VARCHAR(32) NOT NULL,
                    producer_version VARCHAR(64) NOT NULL,
                    content_sha256 VARCHAR(64) NOT NULL,
                    declared_sha256 VARCHAR(64),
                    byte_length INTEGER NOT NULL,
                    media_type VARCHAR(64) NOT NULL,
                    storage_backend_id VARCHAR(64) NOT NULL,
                    storage_backend_version VARCHAR(32) NOT NULL,
                    storage_key VARCHAR(160) NOT NULL,
                    ingestion_idempotency_key VARCHAR(128) NOT NULL UNIQUE,
                    canonical_ingestion_command_payload JSON NOT NULL,
                    canonical_ingestion_command_hash VARCHAR(64) NOT NULL,
                    canonical_metadata_payload JSON NOT NULL,
                    canonical_metadata_hash VARCHAR(64) NOT NULL,
                    creation_actor_type VARCHAR(64) NOT NULL,
                    creation_actor_id VARCHAR(255) NOT NULL,
                    created_at DATETIME NOT NULL,
                    CONSTRAINT ck_execution_evidence_kind_supported
                        CHECK (evidence_kind IN
                            ('candidate', 'command', 'test', 'lint')),
                    CONSTRAINT ck_execution_evidence_producer_supported
                        CHECK (producer_id IN
                            ('runtime', 'command-runner', 'test-runner',
                             'lint-runner')),
                    CONSTRAINT ck_execution_evidence_generation_positive
                        CHECK (attempt_generation > 0),
                    CONSTRAINT ck_execution_evidence_length_nonnegative
                        CHECK (byte_length >= 0),
                    FOREIGN KEY(execution_plan_id)
                        REFERENCES execution_plans (id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_task_id)
                        REFERENCES execution_tasks (id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_task_attempt_id)
                        REFERENCES execution_task_attempts (id) ON DELETE CASCADE
                )
                """
            )
        )
        for statement in (
            "CREATE INDEX IF NOT EXISTS ix_execution_evidence_task_kind "
            "ON execution_evidence (execution_task_id, evidence_kind)",
            "CREATE INDEX IF NOT EXISTS ix_execution_evidence_attempt_kind "
            "ON execution_evidence (execution_task_attempt_id, evidence_kind)",
            "CREATE INDEX IF NOT EXISTS ix_execution_evidence_plan_created "
            "ON execution_evidence (execution_plan_id, created_at)",
            "CREATE INDEX IF NOT EXISTS ix_execution_evidence_content_sha256 "
            "ON execution_evidence (content_sha256)",
        ):
            connection.execute(text(statement))


def _migration_045_execution_evidence_validation_boundary(engine: Engine) -> None:
    """Add Phase 29C-12 supporting indexes only.

    This migration creates no table and no column.  The C7B resolved-evidence
    and predicate-result tables (Phase 29C-7B/7C) and the execution evidence
    authority table (Phase 29C-11) already accept the classification values
    and free-form ``source``/``source_authority_id`` strings this boundary
    uses, so no schema shape changes.  It only adds an index that makes
    execution-evidence-sourced resolved-evidence lookups efficient.  It never
    fabricates evidence, results, or validation contracts, and it never
    touches C9/C10/C11 tables.
    """

    table_names = _table_names(engine)
    if "execution_task_resolved_validation_evidence" not in table_names:
        return
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE INDEX IF NOT EXISTS "
                "ix_execution_task_resolved_evidence_source_key "
                "ON execution_task_resolved_validation_evidence "
                "(source, evidence_key)"
            )
        )


def _migration_046_execution_task_changeset_apply_authorization(engine: Engine) -> None:
    """Add empty Phase 29D-1 ChangeSet and Controlled Apply authorities.

    This migration creates only tables and indexes.  It never infers a
    ChangeSet from historical candidate content, fabricates an authorization,
    mutates a workspace/repository, or touches any Phase 29 lifecycle field.
    """

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_task_change_sets (
                    id INTEGER PRIMARY KEY,
                    execution_plan_id INTEGER NOT NULL,
                    execution_task_id INTEGER NOT NULL,
                    execution_task_attempt_id INTEGER NOT NULL,
                    attempt_generation INTEGER NOT NULL,
                    candidate_outcome_id INTEGER NOT NULL,
                    source_candidate_content_id INTEGER NOT NULL,
                    source_candidate_content_sha256 VARCHAR(64) NOT NULL,
                    acceptance_decision_id INTEGER NOT NULL,
                    acceptance_decision_hash VARCHAR(64) NOT NULL,
                    changeset_format VARCHAR(64) NOT NULL,
                    media_type VARCHAR(96) NOT NULL,
                    target_project_id INTEGER NOT NULL,
                    target_workspace_identity VARCHAR(255),
                    base_state_payload JSON NOT NULL,
                    base_state_hash VARCHAR(64) NOT NULL,
                    operation_count INTEGER NOT NULL,
                    canonical_changeset_payload JSON NOT NULL,
                    changeset_sha256 VARCHAR(64) NOT NULL,
                    canonical_metadata_payload JSON NOT NULL,
                    canonical_metadata_hash VARCHAR(64) NOT NULL,
                    ingestion_idempotency_key VARCHAR(128) NOT NULL UNIQUE,
                    canonical_ingestion_command_payload JSON NOT NULL,
                    canonical_ingestion_command_hash VARCHAR(64) NOT NULL,
                    creation_actor_type VARCHAR(64) NOT NULL,
                    creation_actor_id VARCHAR(255) NOT NULL,
                    created_at DATETIME NOT NULL,
                    CONSTRAINT ck_execution_task_change_set_generation_positive
                        CHECK (attempt_generation > 0),
                    CONSTRAINT ck_execution_task_change_set_operation_count_positive
                        CHECK (operation_count > 0),
                    CONSTRAINT ck_execution_task_change_set_format_v1
                        CHECK (changeset_format = 'orchestrator-changeset/1'),
                    FOREIGN KEY(execution_plan_id)
                        REFERENCES execution_plans (id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_task_id)
                        REFERENCES execution_tasks (id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_task_attempt_id)
                        REFERENCES execution_task_attempts (id) ON DELETE CASCADE,
                    FOREIGN KEY(candidate_outcome_id)
                        REFERENCES execution_task_attempt_outcomes (id)
                        ON DELETE CASCADE,
                    FOREIGN KEY(source_candidate_content_id)
                        REFERENCES execution_task_candidate_contents (id)
                        ON DELETE CASCADE,
                    FOREIGN KEY(acceptance_decision_id)
                        REFERENCES execution_task_acceptance_decisions (id)
                        ON DELETE CASCADE,
                    FOREIGN KEY(target_project_id)
                        REFERENCES projects (id) ON DELETE CASCADE
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_task_change_set_operations (
                    id INTEGER PRIMARY KEY,
                    change_set_id INTEGER NOT NULL,
                    operation_index INTEGER NOT NULL,
                    operation VARCHAR(32) NOT NULL,
                    canonical_path VARCHAR(1024) NOT NULL,
                    expected_previous_sha256 VARCHAR(64),
                    content_reference VARCHAR(160),
                    content_reference_scheme VARCHAR(32),
                    content_reference_id INTEGER,
                    content_sha256 VARCHAR(64),
                    content_media_type VARCHAR(96),
                    content_byte_length INTEGER,
                    created_at DATETIME NOT NULL,
                    CONSTRAINT uq_execution_task_change_set_operation_index
                        UNIQUE (change_set_id, operation_index),
                    CONSTRAINT uq_execution_task_change_set_operation_path
                        UNIQUE (change_set_id, canonical_path),
                    CONSTRAINT ck_execution_task_change_set_operation_index_nonnegative
                        CHECK (operation_index >= 0),
                    CONSTRAINT ck_execution_task_change_set_operation_type
                        CHECK (operation IN
                            ('create_file', 'replace_file', 'delete_file')),
                    FOREIGN KEY(change_set_id)
                        REFERENCES execution_task_change_sets (id) ON DELETE CASCADE
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_task_apply_authorizations (
                    id INTEGER PRIMARY KEY,
                    execution_plan_id INTEGER NOT NULL,
                    execution_task_id INTEGER NOT NULL,
                    execution_task_attempt_id INTEGER NOT NULL,
                    attempt_generation INTEGER NOT NULL,
                    change_set_id INTEGER NOT NULL,
                    change_set_hash VARCHAR(64) NOT NULL,
                    acceptance_decision_id INTEGER NOT NULL,
                    acceptance_decision_hash VARCHAR(64) NOT NULL,
                    target_project_id INTEGER NOT NULL,
                    target_workspace_identity VARCHAR(255),
                    base_state_hash VARCHAR(64) NOT NULL,
                    apply_policy_id VARCHAR(64) NOT NULL,
                    apply_policy_version INTEGER NOT NULL,
                    authorization_status VARCHAR(32) NOT NULL,
                    decision_reason VARCHAR(64) NOT NULL,
                    bounded_detail VARCHAR(1024),
                    canonical_input_payload JSON NOT NULL,
                    canonical_input_hash VARCHAR(64) NOT NULL,
                    canonical_decision_payload JSON NOT NULL,
                    canonical_decision_hash VARCHAR(64) NOT NULL,
                    authorization_idempotency_key VARCHAR(128) NOT NULL UNIQUE,
                    deterministic_authorization_command_id VARCHAR(128) NOT NULL UNIQUE,
                    decision_actor_type VARCHAR(64) NOT NULL,
                    decision_actor_id VARCHAR(255) NOT NULL,
                    decided_at DATETIME NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT uq_execution_task_apply_authorization_changeset_policy
                        UNIQUE (change_set_id, apply_policy_id, apply_policy_version),
                    CONSTRAINT ck_execution_task_apply_authorization_generation_positive
                        CHECK (attempt_generation > 0 AND apply_policy_version > 0),
                    CONSTRAINT ck_execution_task_apply_authorization_status
                        CHECK (authorization_status IN
                            ('authorized', 'blocked', 'denied')),
                    FOREIGN KEY(execution_plan_id)
                        REFERENCES execution_plans (id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_task_id)
                        REFERENCES execution_tasks (id) ON DELETE CASCADE,
                    FOREIGN KEY(execution_task_attempt_id)
                        REFERENCES execution_task_attempts (id) ON DELETE CASCADE,
                    FOREIGN KEY(change_set_id)
                        REFERENCES execution_task_change_sets (id) ON DELETE CASCADE,
                    FOREIGN KEY(acceptance_decision_id)
                        REFERENCES execution_task_acceptance_decisions (id)
                        ON DELETE CASCADE,
                    FOREIGN KEY(target_project_id)
                        REFERENCES projects (id) ON DELETE CASCADE
                )
                """
            )
        )
        for statement in (
            "CREATE INDEX IF NOT EXISTS ix_execution_task_change_sets_task_created "
            "ON execution_task_change_sets (execution_task_id, created_at)",
            "CREATE INDEX IF NOT EXISTS ix_execution_task_change_sets_plan_created "
            "ON execution_task_change_sets (execution_plan_id, created_at)",
            "CREATE INDEX IF NOT EXISTS ix_execution_task_change_sets_changeset_sha256 "
            "ON execution_task_change_sets (changeset_sha256)",
            "CREATE INDEX IF NOT EXISTS "
            "ix_execution_task_change_set_operations_change_set "
            "ON execution_task_change_set_operations (change_set_id)",
            "CREATE INDEX IF NOT EXISTS "
            "ix_execution_task_apply_authorizations_task_status "
            "ON execution_task_apply_authorizations "
            "(execution_task_id, authorization_status)",
            "CREATE INDEX IF NOT EXISTS "
            "ix_execution_task_apply_authorizations_change_set "
            "ON execution_task_apply_authorizations (change_set_id)",
        ):
            connection.execute(text(statement))


def _migration_047_workspace_base_state_apply_attempt_boundary(engine: Engine) -> None:
    """Add empty Phase 29D-2 workspace/apply authorities.

    This migration is additive and replay-safe.  It never resolves a
    historical Project.workspace_path, inspects a workspace, fabricates a
    target/base/approval/attempt, or changes task lifecycle.  Existing D-1
    authorization rows are copied byte-for-byte when SQLite's old v1 unique
    constraint must be replaced so v2 can have one authorization per exact
    ChangeSet/base-state scope.
    """

    table_names = _table_names(engine)
    if "execution_task_apply_authorizations" in table_names and not _has_column(
        engine, "execution_task_apply_authorizations", "base_state_id"
    ):
        # Phase 29D-1's SQLite table used a v1-only unique constraint.  A
        # table rebuild preserves every historical value while permitting v2
        # observations to be independently authorized for refreshed bases.
        with engine.connect() as connection:
            connection.exec_driver_sql("PRAGMA foreign_keys=OFF")
            connection.commit()
            transaction = connection.begin()
            try:
                connection.execute(
                    text(
                        "ALTER TABLE execution_task_apply_authorizations "
                        "RENAME TO execution_task_apply_authorizations_046_legacy"
                    )
                )
                connection.execute(
                    text(
                        """
                        CREATE TABLE execution_task_apply_authorizations (
                            id INTEGER PRIMARY KEY,
                            execution_plan_id INTEGER NOT NULL,
                            execution_task_id INTEGER NOT NULL,
                            execution_task_attempt_id INTEGER NOT NULL,
                            attempt_generation INTEGER NOT NULL,
                            change_set_id INTEGER NOT NULL,
                            change_set_hash VARCHAR(64) NOT NULL,
                            acceptance_decision_id INTEGER NOT NULL,
                            acceptance_decision_hash VARCHAR(64) NOT NULL,
                            target_project_id INTEGER NOT NULL,
                            workspace_target_id INTEGER,
                            base_state_id INTEGER,
                            target_workspace_identity VARCHAR(255),
                            base_state_hash VARCHAR(64) NOT NULL,
                            apply_policy_id VARCHAR(64) NOT NULL,
                            apply_policy_version INTEGER NOT NULL,
                            authorization_status VARCHAR(32) NOT NULL,
                            decision_reason VARCHAR(64) NOT NULL,
                            bounded_detail VARCHAR(1024),
                            canonical_input_payload JSON NOT NULL,
                            canonical_input_hash VARCHAR(64) NOT NULL,
                            canonical_decision_payload JSON NOT NULL,
                            canonical_decision_hash VARCHAR(64) NOT NULL,
                            authorization_idempotency_key VARCHAR(128) NOT NULL UNIQUE,
                            deterministic_authorization_command_id VARCHAR(128) NOT NULL UNIQUE,
                            decision_actor_type VARCHAR(64) NOT NULL,
                            decision_actor_id VARCHAR(255) NOT NULL,
                            decided_at DATETIME NOT NULL,
                            created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                            CONSTRAINT ck_execution_task_apply_authorization_generation_positive
                                CHECK (attempt_generation > 0 AND apply_policy_version > 0),
                            CONSTRAINT ck_execution_task_apply_authorization_status
                                CHECK (authorization_status IN ('authorized', 'blocked', 'denied')),
                            FOREIGN KEY(execution_plan_id) REFERENCES execution_plans (id) ON DELETE CASCADE,
                            FOREIGN KEY(execution_task_id) REFERENCES execution_tasks (id) ON DELETE CASCADE,
                            FOREIGN KEY(execution_task_attempt_id) REFERENCES execution_task_attempts (id) ON DELETE CASCADE,
                            FOREIGN KEY(change_set_id) REFERENCES execution_task_change_sets (id) ON DELETE CASCADE,
                            FOREIGN KEY(acceptance_decision_id) REFERENCES execution_task_acceptance_decisions (id) ON DELETE CASCADE,
                            FOREIGN KEY(target_project_id) REFERENCES projects (id) ON DELETE CASCADE
                        )
                        """
                    )
                )
                connection.execute(
                    text(
                        """
                        INSERT INTO execution_task_apply_authorizations (
                            id, execution_plan_id, execution_task_id,
                            execution_task_attempt_id, attempt_generation,
                            change_set_id, change_set_hash, acceptance_decision_id,
                            acceptance_decision_hash, target_project_id,
                            workspace_target_id, base_state_id,
                            target_workspace_identity, base_state_hash,
                            apply_policy_id, apply_policy_version,
                            authorization_status, decision_reason, bounded_detail,
                            canonical_input_payload, canonical_input_hash,
                            canonical_decision_payload, canonical_decision_hash,
                            authorization_idempotency_key,
                            deterministic_authorization_command_id,
                            decision_actor_type, decision_actor_id, decided_at, created_at
                        )
                        SELECT id, execution_plan_id, execution_task_id,
                            execution_task_attempt_id, attempt_generation,
                            change_set_id, change_set_hash, acceptance_decision_id,
                            acceptance_decision_hash, target_project_id,
                            NULL, NULL, target_workspace_identity, base_state_hash,
                            apply_policy_id, apply_policy_version,
                            authorization_status, decision_reason, bounded_detail,
                            canonical_input_payload, canonical_input_hash,
                            canonical_decision_payload, canonical_decision_hash,
                            authorization_idempotency_key,
                            deterministic_authorization_command_id,
                            decision_actor_type, decision_actor_id, decided_at, created_at
                        FROM execution_task_apply_authorizations_046_legacy
                        """
                    )
                )
                connection.execute(
                    text("DROP TABLE execution_task_apply_authorizations_046_legacy")
                )
                transaction.commit()
            except Exception:
                transaction.rollback()
                raise
            finally:
                connection.exec_driver_sql("PRAGMA foreign_keys=ON")

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_workspace_targets (
                    id INTEGER PRIMARY KEY,
                    project_id INTEGER NOT NULL,
                    authority_version INTEGER NOT NULL,
                    target_status VARCHAR(24) NOT NULL,
                    configured_workspace_path VARCHAR(512) NOT NULL,
                    normalized_realpath VARCHAR(1024) NOT NULL,
                    filesystem_device VARCHAR(64),
                    filesystem_inode VARCHAR(64),
                    target_identity VARCHAR(255) NOT NULL UNIQUE,
                    repository_kind VARCHAR(32) NOT NULL,
                    repository_identity VARCHAR(255),
                    repository_root_realpath VARCHAR(1024),
                    repository_root_identity VARCHAR(255),
                    canonical_target_payload JSON NOT NULL,
                    canonical_target_hash VARCHAR(64) NOT NULL,
                    registration_idempotency_key VARCHAR(128) NOT NULL UNIQUE,
                    creation_actor_type VARCHAR(64) NOT NULL,
                    creation_actor_id VARCHAR(255) NOT NULL,
                    created_at DATETIME NOT NULL,
                    CONSTRAINT uq_execution_workspace_target_project_identity
                        UNIQUE (project_id, target_identity),
                    CONSTRAINT ck_execution_workspace_target_version_positive
                        CHECK (authority_version > 0),
                    CONSTRAINT ck_execution_workspace_target_status
                        CHECK (target_status IN ('active', 'superseded')),
                    FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_workspace_base_states (
                    id INTEGER PRIMARY KEY,
                    workspace_target_id INTEGER NOT NULL,
                    project_id INTEGER NOT NULL,
                    change_set_id INTEGER NOT NULL,
                    target_identity VARCHAR(255) NOT NULL,
                    repository_kind VARCHAR(32) NOT NULL,
                    repository_identity VARCHAR(255),
                    repository_root_identity VARCHAR(255),
                    repository_head VARCHAR(128) NOT NULL,
                    workspace_clean BOOLEAN NOT NULL,
                    dirty_state VARCHAR(32) NOT NULL,
                    dirty_path_count INTEGER NOT NULL,
                    dirty_paths JSON NOT NULL,
                    dirty_path_summary_hash VARCHAR(64) NOT NULL,
                    repository_operation_state JSON NOT NULL,
                    inspection_policy_id VARCHAR(64) NOT NULL,
                    inspection_policy_version INTEGER NOT NULL,
                    tool_identity VARCHAR(64) NOT NULL,
                    tool_version VARCHAR(64) NOT NULL,
                    path_observation_count INTEGER NOT NULL,
                    canonical_observation_payload JSON NOT NULL,
                    canonical_observation_hash VARCHAR(64) NOT NULL,
                    observation_idempotency_key VARCHAR(128) NOT NULL UNIQUE,
                    creation_actor_type VARCHAR(64) NOT NULL,
                    creation_actor_id VARCHAR(255) NOT NULL,
                    created_at DATETIME NOT NULL,
                    inspected_at DATETIME NOT NULL,
                    CONSTRAINT uq_execution_workspace_base_state_observation
                        UNIQUE (workspace_target_id, change_set_id, canonical_observation_hash),
                    CONSTRAINT ck_execution_workspace_base_state_bounds
                        CHECK (inspection_policy_version > 0 AND path_observation_count > 0),
                    CONSTRAINT ck_execution_workspace_base_state_dirty_state
                        CHECK (dirty_state IN ('clean', 'unrelated_dirty', 'conflicting_dirty')),
                    FOREIGN KEY(workspace_target_id) REFERENCES execution_workspace_targets (id) ON DELETE RESTRICT,
                    FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
                    FOREIGN KEY(change_set_id) REFERENCES execution_task_change_sets (id) ON DELETE RESTRICT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_workspace_path_observations (
                    id INTEGER PRIMARY KEY,
                    base_state_id INTEGER NOT NULL,
                    observation_index INTEGER NOT NULL,
                    operation VARCHAR(32) NOT NULL,
                    path VARCHAR(1024) NOT NULL,
                    "exists" BOOLEAN NOT NULL,
                    entry_type VARCHAR(32) NOT NULL,
                    content_sha256 VARCHAR(64),
                    byte_length INTEGER,
                    mode_classification VARCHAR(32),
                    symlink_status VARCHAR(32) NOT NULL,
                    canonical_observation_payload JSON NOT NULL,
                    canonical_observation_hash VARCHAR(64) NOT NULL,
                    CONSTRAINT uq_execution_workspace_path_observation_index
                        UNIQUE (base_state_id, observation_index),
                    CONSTRAINT uq_execution_workspace_path_observation_path
                        UNIQUE (base_state_id, path),
                    CONSTRAINT ck_execution_workspace_path_observation_index
                        CHECK (observation_index >= 0),
                    FOREIGN KEY(base_state_id) REFERENCES execution_workspace_base_states (id) ON DELETE CASCADE
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_task_apply_approvals (
                    id INTEGER PRIMARY KEY,
                    execution_plan_id INTEGER NOT NULL,
                    execution_task_id INTEGER NOT NULL,
                    execution_task_attempt_id INTEGER NOT NULL,
                    attempt_generation INTEGER NOT NULL,
                    change_set_id INTEGER NOT NULL,
                    change_set_hash VARCHAR(64) NOT NULL,
                    workspace_target_id INTEGER NOT NULL,
                    workspace_target_hash VARCHAR(64) NOT NULL,
                    base_state_id INTEGER NOT NULL,
                    base_state_hash VARCHAR(64) NOT NULL,
                    apply_policy_id VARCHAR(64) NOT NULL,
                    apply_policy_version INTEGER NOT NULL,
                    decision VARCHAR(16) NOT NULL,
                    approver_actor_type VARCHAR(64) NOT NULL,
                    approver_actor_id VARCHAR(255) NOT NULL,
                    reviewed_summary_payload JSON NOT NULL,
                    reviewed_summary_hash VARCHAR(64) NOT NULL,
                    canonical_approval_payload JSON NOT NULL,
                    canonical_approval_hash VARCHAR(64) NOT NULL,
                    approval_idempotency_key VARCHAR(128) NOT NULL UNIQUE,
                    decided_at DATETIME NOT NULL,
                    created_at DATETIME NOT NULL,
                    CONSTRAINT uq_execution_task_apply_approval_exact_scope
                        UNIQUE (change_set_id, base_state_id, apply_policy_id, apply_policy_version),
                    CONSTRAINT ck_execution_task_apply_approval_versions_positive
                        CHECK (attempt_generation > 0 AND apply_policy_version > 0),
                    CONSTRAINT ck_execution_task_apply_approval_decision
                        CHECK (decision IN ('approved', 'rejected')),
                    FOREIGN KEY(execution_plan_id) REFERENCES execution_plans (id) ON DELETE RESTRICT,
                    FOREIGN KEY(execution_task_id) REFERENCES execution_tasks (id) ON DELETE RESTRICT,
                    FOREIGN KEY(execution_task_attempt_id) REFERENCES execution_task_attempts (id) ON DELETE RESTRICT,
                    FOREIGN KEY(change_set_id) REFERENCES execution_task_change_sets (id) ON DELETE RESTRICT,
                    FOREIGN KEY(workspace_target_id) REFERENCES execution_workspace_targets (id) ON DELETE RESTRICT,
                    FOREIGN KEY(base_state_id) REFERENCES execution_workspace_base_states (id) ON DELETE RESTRICT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_task_apply_attempts (
                    id INTEGER PRIMARY KEY,
                    execution_plan_id INTEGER NOT NULL,
                    execution_task_id INTEGER NOT NULL,
                    execution_task_attempt_id INTEGER NOT NULL,
                    attempt_generation INTEGER NOT NULL,
                    change_set_id INTEGER NOT NULL,
                    change_set_hash VARCHAR(64) NOT NULL,
                    authorization_id INTEGER NOT NULL UNIQUE,
                    authorization_hash VARCHAR(64) NOT NULL,
                    approval_id INTEGER NOT NULL,
                    approval_hash VARCHAR(64) NOT NULL,
                    workspace_target_id INTEGER NOT NULL,
                    workspace_target_hash VARCHAR(64) NOT NULL,
                    base_state_id INTEGER NOT NULL,
                    base_state_hash VARCHAR(64) NOT NULL,
                    apply_policy_id VARCHAR(64) NOT NULL,
                    apply_policy_version INTEGER NOT NULL,
                    attempt_number INTEGER NOT NULL,
                    status VARCHAR(32) NOT NULL,
                    status_reason VARCHAR(64),
                    canonical_command_payload JSON NOT NULL,
                    canonical_command_hash VARCHAR(64) NOT NULL,
                    precondition_verification_hash VARCHAR(64),
                    apply_attempt_idempotency_key VARCHAR(128) NOT NULL UNIQUE,
                    creation_actor_type VARCHAR(64) NOT NULL,
                    creation_actor_id VARCHAR(255) NOT NULL,
                    created_at DATETIME NOT NULL,
                    CONSTRAINT uq_execution_task_apply_attempt_task_number
                        UNIQUE (execution_task_id, attempt_number),
                    CONSTRAINT ck_execution_task_apply_attempt_versions_positive
                        CHECK (attempt_generation > 0 AND attempt_number > 0 AND apply_policy_version > 0),
                    CONSTRAINT ck_execution_task_apply_attempt_status
                        CHECK (status IN ('created', 'precondition_verified', 'blocked', 'cancelled')),
                    FOREIGN KEY(execution_plan_id) REFERENCES execution_plans (id) ON DELETE RESTRICT,
                    FOREIGN KEY(execution_task_id) REFERENCES execution_tasks (id) ON DELETE RESTRICT,
                    FOREIGN KEY(execution_task_attempt_id) REFERENCES execution_task_attempts (id) ON DELETE RESTRICT,
                    FOREIGN KEY(change_set_id) REFERENCES execution_task_change_sets (id) ON DELETE RESTRICT,
                    FOREIGN KEY(authorization_id) REFERENCES execution_task_apply_authorizations (id) ON DELETE RESTRICT,
                    FOREIGN KEY(approval_id) REFERENCES execution_task_apply_approvals (id) ON DELETE RESTRICT,
                    FOREIGN KEY(workspace_target_id) REFERENCES execution_workspace_targets (id) ON DELETE RESTRICT,
                    FOREIGN KEY(base_state_id) REFERENCES execution_workspace_base_states (id) ON DELETE RESTRICT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_task_apply_precondition_verifications (
                    id INTEGER PRIMARY KEY,
                    apply_attempt_id INTEGER NOT NULL,
                    sequence INTEGER NOT NULL,
                    outcome VARCHAR(48) NOT NULL,
                    reason VARCHAR(64) NOT NULL,
                    authorized_base_state_id INTEGER NOT NULL,
                    authorized_base_state_hash VARCHAR(64) NOT NULL,
                    observed_target_identity VARCHAR(255),
                    observed_state_hash VARCHAR(64),
                    canonical_verification_payload JSON NOT NULL,
                    canonical_verification_hash VARCHAR(64) NOT NULL,
                    created_at DATETIME NOT NULL,
                    CONSTRAINT uq_execution_task_apply_precondition_verification_sequence
                        UNIQUE (apply_attempt_id, sequence),
                    CONSTRAINT ck_execution_task_apply_precondition_verification_sequence
                        CHECK (sequence > 0),
                    CONSTRAINT ck_execution_task_apply_precondition_verification_outcome
                        CHECK (outcome IN ('precondition_verified', 'blocked_workspace_changed',
                            'blocked_target_identity_changed', 'blocked_repository_head_changed',
                            'blocked_path_state_changed', 'blocked_dirty_state',
                            'blocked_approval_missing', 'blocked_integrity_failure')),
                    FOREIGN KEY(apply_attempt_id) REFERENCES execution_task_apply_attempts (id) ON DELETE CASCADE
                )
                """
            )
        )
        for statement in (
            "CREATE INDEX IF NOT EXISTS ix_execution_workspace_targets_project_status ON execution_workspace_targets (project_id, target_status)",
            "CREATE INDEX IF NOT EXISTS ix_execution_workspace_targets_realpath ON execution_workspace_targets (normalized_realpath)",
            "CREATE INDEX IF NOT EXISTS ix_execution_workspace_base_states_target_created ON execution_workspace_base_states (workspace_target_id, created_at)",
            "CREATE INDEX IF NOT EXISTS ix_execution_workspace_base_states_changeset_created ON execution_workspace_base_states (change_set_id, created_at)",
            "CREATE INDEX IF NOT EXISTS ix_execution_workspace_path_observations_base ON execution_workspace_path_observations (base_state_id, observation_index)",
            "CREATE INDEX IF NOT EXISTS ix_execution_task_apply_approvals_task_decision ON execution_task_apply_approvals (execution_task_id, decision)",
            "CREATE INDEX IF NOT EXISTS ix_execution_task_apply_attempts_task_status ON execution_task_apply_attempts (execution_task_id, status)",
            "CREATE INDEX IF NOT EXISTS ix_execution_task_apply_attempts_base_state ON execution_task_apply_attempts (base_state_id)",
            "CREATE INDEX IF NOT EXISTS ix_execution_task_apply_precondition_verifications_attempt ON execution_task_apply_precondition_verifications (apply_attempt_id, sequence)",
        ):
            connection.execute(text(statement))


def _migration_048_controlled_apply_result_authority(engine: Engine) -> None:
    """Add the empty immutable Phase 29D-3 Apply Result authority."""

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_task_apply_results (
                    id INTEGER PRIMARY KEY,
                    execution_plan_id INTEGER NOT NULL,
                    execution_task_id INTEGER NOT NULL,
                    execution_task_attempt_id INTEGER NOT NULL,
                    attempt_generation INTEGER NOT NULL,
                    apply_attempt_id INTEGER NOT NULL UNIQUE,
                    apply_attempt_hash VARCHAR(64) NOT NULL,
                    change_set_id INTEGER NOT NULL,
                    change_set_hash VARCHAR(64) NOT NULL,
                    authorization_id INTEGER NOT NULL,
                    authorization_hash VARCHAR(64) NOT NULL,
                    approval_id INTEGER NOT NULL,
                    approval_hash VARCHAR(64) NOT NULL,
                    workspace_target_id INTEGER NOT NULL,
                    workspace_target_hash VARCHAR(64) NOT NULL,
                    base_state_id INTEGER NOT NULL,
                    base_state_hash VARCHAR(64) NOT NULL,
                    status VARCHAR(16) NOT NULL,
                    failure_reason VARCHAR(64),
                    failure_detail VARCHAR(1024),
                    applied_operations JSON NOT NULL,
                    canonical_payload JSON NOT NULL,
                    canonical_sha256 VARCHAR(64) NOT NULL,
                    result_idempotency_key VARCHAR(128) NOT NULL UNIQUE,
                    started_at DATETIME NOT NULL,
                    ended_at DATETIME NOT NULL,
                    created_at DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
                    CONSTRAINT ck_execution_task_apply_result_generation_positive
                        CHECK (attempt_generation > 0),
                    CONSTRAINT ck_execution_task_apply_result_status
                        CHECK (status IN ('applied', 'blocked', 'failed')),
                    CONSTRAINT ck_execution_task_apply_result_failure_shape
                        CHECK ((status = 'applied' AND failure_reason IS NULL) OR
                            (status IN ('blocked', 'failed') AND failure_reason IS NOT NULL)),
                    FOREIGN KEY(execution_plan_id) REFERENCES execution_plans (id) ON DELETE RESTRICT,
                    FOREIGN KEY(execution_task_id) REFERENCES execution_tasks (id) ON DELETE RESTRICT,
                    FOREIGN KEY(execution_task_attempt_id) REFERENCES execution_task_attempts (id) ON DELETE RESTRICT,
                    FOREIGN KEY(apply_attempt_id) REFERENCES execution_task_apply_attempts (id) ON DELETE RESTRICT,
                    FOREIGN KEY(change_set_id) REFERENCES execution_task_change_sets (id) ON DELETE RESTRICT,
                    FOREIGN KEY(authorization_id) REFERENCES execution_task_apply_authorizations (id) ON DELETE RESTRICT,
                    FOREIGN KEY(approval_id) REFERENCES execution_task_apply_approvals (id) ON DELETE RESTRICT,
                    FOREIGN KEY(workspace_target_id) REFERENCES execution_workspace_targets (id) ON DELETE RESTRICT,
                    FOREIGN KEY(base_state_id) REFERENCES execution_workspace_base_states (id) ON DELETE RESTRICT
                )
                """
            )
        )
        for statement in (
            "CREATE INDEX IF NOT EXISTS ix_execution_task_apply_results_task_status "
            "ON execution_task_apply_results (execution_task_id, status)",
            "CREATE INDEX IF NOT EXISTS ix_execution_task_apply_results_attempt "
            "ON execution_task_apply_results (apply_attempt_id)",
            "CREATE INDEX IF NOT EXISTS ix_execution_task_apply_results_hash "
            "ON execution_task_apply_results (canonical_sha256)",
        ):
            connection.execute(text(statement))


def _migration_049_pre_apply_snapshot_authority(engine: Engine) -> None:
    """Add immutable Phase 29D-3A pre-apply bytes and scope authorities."""

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_task_pre_apply_snapshots (
                    id INTEGER PRIMARY KEY,
                    execution_plan_id INTEGER NOT NULL,
                    execution_task_id INTEGER NOT NULL,
                    execution_task_attempt_id INTEGER NOT NULL,
                    attempt_generation INTEGER NOT NULL,
                    apply_attempt_id INTEGER NOT NULL UNIQUE,
                    apply_attempt_hash VARCHAR(64) NOT NULL,
                    change_set_id INTEGER NOT NULL,
                    change_set_hash VARCHAR(64) NOT NULL,
                    authorization_id INTEGER NOT NULL,
                    authorization_hash VARCHAR(64) NOT NULL,
                    approval_id INTEGER NOT NULL,
                    approval_hash VARCHAR(64) NOT NULL,
                    workspace_target_id INTEGER NOT NULL,
                    workspace_target_hash VARCHAR(64) NOT NULL,
                    base_state_id INTEGER NOT NULL,
                    base_state_hash VARCHAR(64) NOT NULL,
                    final_precondition_verification_hash VARCHAR(64) NOT NULL,
                    capture_command_hash VARCHAR(64) NOT NULL,
                    status VARCHAR(16) NOT NULL,
                    failure_reason VARCHAR(64),
                    failure_detail VARCHAR(1024),
                    expected_entry_count INTEGER NOT NULL,
                    captured_entry_count INTEGER NOT NULL,
                    canonical_payload JSON NOT NULL,
                    canonical_sha256 VARCHAR(64) NOT NULL,
                    snapshot_idempotency_key VARCHAR(128) NOT NULL UNIQUE,
                    created_at DATETIME NOT NULL,
                    CONSTRAINT ck_execution_task_pre_apply_snapshot_bounds
                        CHECK (attempt_generation > 0 AND expected_entry_count > 0 AND
                            captured_entry_count >= 0 AND captured_entry_count <= expected_entry_count),
                    CONSTRAINT ck_execution_task_pre_apply_snapshot_status
                        CHECK (status IN ('captured', 'failed')),
                    CONSTRAINT ck_execution_task_pre_apply_snapshot_failure_shape
                        CHECK ((status = 'captured' AND failure_reason IS NULL AND
                            captured_entry_count = expected_entry_count) OR
                            (status = 'failed' AND failure_reason IS NOT NULL)),
                    FOREIGN KEY(execution_plan_id) REFERENCES execution_plans (id) ON DELETE RESTRICT,
                    FOREIGN KEY(execution_task_id) REFERENCES execution_tasks (id) ON DELETE RESTRICT,
                    FOREIGN KEY(execution_task_attempt_id) REFERENCES execution_task_attempts (id) ON DELETE RESTRICT,
                    FOREIGN KEY(apply_attempt_id) REFERENCES execution_task_apply_attempts (id) ON DELETE RESTRICT,
                    FOREIGN KEY(change_set_id) REFERENCES execution_task_change_sets (id) ON DELETE RESTRICT,
                    FOREIGN KEY(authorization_id) REFERENCES execution_task_apply_authorizations (id) ON DELETE RESTRICT,
                    FOREIGN KEY(approval_id) REFERENCES execution_task_apply_approvals (id) ON DELETE RESTRICT,
                    FOREIGN KEY(workspace_target_id) REFERENCES execution_workspace_targets (id) ON DELETE RESTRICT,
                    FOREIGN KEY(base_state_id) REFERENCES execution_workspace_base_states (id) ON DELETE RESTRICT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_task_pre_apply_snapshot_entries (
                    id INTEGER PRIMARY KEY,
                    snapshot_id INTEGER NOT NULL,
                    entry_index INTEGER NOT NULL,
                    operation VARCHAR(32) NOT NULL,
                    canonical_path VARCHAR(1024) NOT NULL,
                    previous_exists BOOLEAN NOT NULL,
                    previous_entry_type VARCHAR(32) NOT NULL,
                    previous_sha256 VARCHAR(64),
                    previous_byte_length INTEGER,
                    previous_content_reference VARCHAR(160),
                    previous_storage_key VARCHAR(160),
                    expected_post_apply_exists BOOLEAN NOT NULL,
                    expected_post_apply_sha256 VARCHAR(64),
                    canonical_entry_payload JSON NOT NULL,
                    canonical_entry_hash VARCHAR(64) NOT NULL,
                    CONSTRAINT uq_execution_task_pre_apply_snapshot_entry_index
                        UNIQUE (snapshot_id, entry_index),
                    CONSTRAINT uq_execution_task_pre_apply_snapshot_entry_path
                        UNIQUE (snapshot_id, canonical_path),
                    CONSTRAINT ck_execution_task_pre_apply_snapshot_entry_index
                        CHECK (entry_index >= 0),
                    CONSTRAINT ck_execution_task_pre_apply_snapshot_entry_operation
                        CHECK (operation IN ('create_file', 'replace_file', 'delete_file')),
                    CONSTRAINT ck_execution_task_pre_apply_snapshot_entry_previous_shape
                        CHECK ((previous_exists = 0 AND previous_entry_type = 'absent' AND
                            previous_sha256 IS NULL AND previous_byte_length IS NULL AND
                            previous_content_reference IS NULL AND previous_storage_key IS NULL) OR
                            (previous_exists = 1 AND previous_entry_type = 'regular_file' AND
                            previous_sha256 IS NOT NULL AND previous_byte_length IS NOT NULL AND
                            previous_content_reference IS NOT NULL AND previous_storage_key IS NOT NULL)),
                    CONSTRAINT ck_execution_task_pre_apply_snapshot_entry_post_shape
                        CHECK ((expected_post_apply_exists = 1 AND expected_post_apply_sha256 IS NOT NULL) OR
                            (expected_post_apply_exists = 0 AND expected_post_apply_sha256 IS NULL)),
                    FOREIGN KEY(snapshot_id) REFERENCES execution_task_pre_apply_snapshots (id) ON DELETE CASCADE
                )
                """
            )
        )
        if not _has_column(
            engine, "execution_task_apply_results", "pre_apply_snapshot_id"
        ):
            connection.execute(
                text(
                    "ALTER TABLE execution_task_apply_results ADD COLUMN "
                    "pre_apply_snapshot_id INTEGER REFERENCES execution_task_pre_apply_snapshots (id) ON DELETE RESTRICT"
                )
            )
        if not _has_column(
            engine, "execution_task_apply_results", "pre_apply_snapshot_hash"
        ):
            connection.execute(
                text(
                    "ALTER TABLE execution_task_apply_results ADD COLUMN "
                    "pre_apply_snapshot_hash VARCHAR(64)"
                )
            )
        for statement in (
            "CREATE INDEX IF NOT EXISTS ix_execution_task_pre_apply_snapshots_task_status "
            "ON execution_task_pre_apply_snapshots (execution_task_id, status)",
            "CREATE INDEX IF NOT EXISTS ix_execution_task_pre_apply_snapshots_hash "
            "ON execution_task_pre_apply_snapshots (canonical_sha256)",
            "CREATE INDEX IF NOT EXISTS ix_execution_task_pre_apply_snapshot_entries_snapshot "
            "ON execution_task_pre_apply_snapshot_entries (snapshot_id, entry_index)",
            "CREATE INDEX IF NOT EXISTS ix_execution_task_pre_apply_snapshot_entries_storage "
            "ON execution_task_pre_apply_snapshot_entries (previous_storage_key)",
            "CREATE UNIQUE INDEX IF NOT EXISTS uq_execution_task_apply_results_snapshot "
            "ON execution_task_apply_results (pre_apply_snapshot_id)",
        ):
            connection.execute(text(statement))


def _migration_038_normalize(value):
    if isinstance(value, str):
        return unicodedata.normalize("NFC", value)
    if isinstance(value, dict):
        return {
            unicodedata.normalize("NFC", str(key)): _migration_038_normalize(item)
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_migration_038_normalize(item) for item in value]
    return value


def _migration_014_task_workflow_stage(engine: Engine) -> None:
    if "tasks" not in _table_names(engine):
        return
    if not _has_column(engine, "tasks", "workflow_stage"):
        with engine.begin() as connection:
            connection.execute(
                text("ALTER TABLE tasks ADD COLUMN workflow_stage VARCHAR(30)")
            )


def _migration_015_session_model_lane(engine: Engine) -> None:
    if "sessions" not in _table_names(engine):
        return
    statements: list[str] = []
    if not _has_column(engine, "sessions", "model_lane_label"):
        statements.append(
            "ALTER TABLE sessions ADD COLUMN model_lane_label VARCHAR(64)"
        )
    if not _has_column(engine, "sessions", "model_lane_metadata"):
        statements.append("ALTER TABLE sessions ADD COLUMN model_lane_metadata JSON")
    if statements:
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))


def _migration_016_session_repair_churn(engine: Engine) -> None:
    if "sessions" not in _table_names(engine):
        return
    statements: list[str] = []
    if not _has_column(engine, "sessions", "repair_churn_stopped"):
        statements.append(
            "ALTER TABLE sessions ADD COLUMN repair_churn_stopped BOOLEAN DEFAULT FALSE"
        )
    if not _has_column(engine, "sessions", "repair_churn_trigger"):
        statements.append(
            "ALTER TABLE sessions ADD COLUMN repair_churn_trigger VARCHAR(64)"
        )
    if statements:
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))


def _migration_017_human_guidance_conflicts(engine: Engine) -> None:
    table_names = _table_names(engine)
    if "human_guidance_conflicts" not in table_names:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE human_guidance_conflicts (
                        id INTEGER PRIMARY KEY,
                        guidance_id INTEGER REFERENCES human_guidance(id),
                        project_id INTEGER,
                        session_id INTEGER,
                        task_id INTEGER,
                        task_title VARCHAR(512),
                        guidance_scope VARCHAR(50),
                        guidance_message TEXT NOT NULL,
                        conflict_excerpt TEXT NOT NULL DEFAULT '',
                        conflict_patterns TEXT,
                        severity VARCHAR(20) NOT NULL DEFAULT 'warning',
                        status VARCHAR(20) NOT NULL DEFAULT 'open',
                        detected_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        resolved_at DATETIME,
                        resolved_by VARCHAR(255),
                        resolution_note TEXT,
                        source VARCHAR(50) NOT NULL DEFAULT 'heuristic'
                    )
                    """
                )
            )

    with engine.begin() as connection:
        for index_name, ddl in [
            (
                "ix_hgc_guidance_id",
                "CREATE INDEX ix_hgc_guidance_id ON human_guidance_conflicts (guidance_id)",
            ),
            (
                "ix_hgc_project_id",
                "CREATE INDEX ix_hgc_project_id ON human_guidance_conflicts (project_id)",
            ),
            (
                "ix_hgc_session_id",
                "CREATE INDEX ix_hgc_session_id ON human_guidance_conflicts (session_id)",
            ),
            (
                "ix_hgc_task_id",
                "CREATE INDEX ix_hgc_task_id ON human_guidance_conflicts (task_id)",
            ),
            (
                "ix_hgc_status",
                "CREATE INDEX ix_hgc_status ON human_guidance_conflicts (status)",
            ),
            (
                "ix_hgc_detected_at",
                "CREATE INDEX ix_hgc_detected_at ON human_guidance_conflicts (detected_at)",
            ),
        ]:
            if not _has_index(engine, "human_guidance_conflicts", index_name):
                connection.execute(text(ddl))


def _migration_018_human_guidance_activations(engine: Engine) -> None:
    table_names = _table_names(engine)
    if "human_guidance_activations" not in table_names:
        with engine.begin() as connection:
            connection.execute(
                text(
                    """
                    CREATE TABLE human_guidance_activations (
                        id INTEGER PRIMARY KEY,
                        project_id INTEGER,
                        session_id INTEGER,
                        scope VARCHAR(20) NOT NULL,
                        table_enabled BOOLEAN NOT NULL DEFAULT 0,
                        persistence_enabled BOOLEAN NOT NULL DEFAULT 0,
                        render_enabled BOOLEAN NOT NULL DEFAULT 0,
                        injection_enabled BOOLEAN NOT NULL DEFAULT 0,
                        conflict_detection_enabled BOOLEAN NOT NULL DEFAULT 0,
                        created_at DATETIME DEFAULT CURRENT_TIMESTAMP,
                        updated_at DATETIME,
                        enabled_by VARCHAR(255),
                        disabled_at DATETIME,
                        disabled_by VARCHAR(255),
                        status VARCHAR(20) NOT NULL DEFAULT 'disabled'
                    )
                    """
                )
            )

    with engine.begin() as connection:
        for index_name, ddl in [
            (
                "ix_hga_project_id",
                "CREATE INDEX ix_hga_project_id ON human_guidance_activations (project_id)",
            ),
            (
                "ix_hga_session_id",
                "CREATE INDEX ix_hga_session_id ON human_guidance_activations (session_id)",
            ),
            (
                "ix_hga_scope",
                "CREATE INDEX ix_hga_scope ON human_guidance_activations (scope)",
            ),
            (
                "ix_hga_status",
                "CREATE INDEX ix_hga_status ON human_guidance_activations (status)",
            ),
        ]:
            if not _has_index(engine, "human_guidance_activations", index_name):
                connection.execute(text(ddl))


def _migration_019_human_guidance_usage_selection(engine: Engine) -> None:
    table_names = _table_names(engine)
    if "human_guidance_usage" not in table_names:
        return

    statements = []
    if not _has_column(engine, "human_guidance_usage", "selected"):
        statements.append(
            "ALTER TABLE human_guidance_usage ADD COLUMN selected BOOLEAN DEFAULT 0 NOT NULL"
        )
    if not _has_column(engine, "human_guidance_usage", "selection_score"):
        statements.append(
            "ALTER TABLE human_guidance_usage ADD COLUMN selection_score INTEGER"
        )

    if statements:
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))


def _migration_020_human_guidance_backend_targets(engine: Engine) -> None:
    if "human_guidance" not in _table_names(engine):
        return
    if not _has_column(engine, "human_guidance", "backend_targets"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE human_guidance"
                    " ADD COLUMN backend_targets TEXT DEFAULT '[\"all\"]'"
                )
            )
            connection.execute(
                text(
                    "UPDATE human_guidance SET backend_targets = '[\"all\"]'"
                    " WHERE backend_targets IS NULL"
                )
            )


def _migration_021_pagination_indexes(engine: Engine) -> None:
    """Add indexes required for efficient paginated list queries (Phase 15E-2)."""
    table_names = _table_names(engine)

    if "sessions" in table_names:
        with engine.begin() as connection:
            for index_name, ddl in [
                (
                    "ix_sessions_status",
                    "CREATE INDEX ix_sessions_status ON sessions (status)",
                ),
                (
                    "ix_sessions_created_at",
                    "CREATE INDEX ix_sessions_created_at ON sessions (created_at)",
                ),
                (
                    "ix_sessions_project_id_created_at",
                    "CREATE INDEX ix_sessions_project_id_created_at ON sessions (project_id, created_at)",
                ),
            ]:
                if not _has_index(engine, "sessions", index_name):
                    connection.execute(text(ddl))

    if "tasks" in table_names:
        with engine.begin() as connection:
            for index_name, ddl in [
                (
                    "ix_tasks_workspace_status",
                    "CREATE INDEX ix_tasks_workspace_status ON tasks (workspace_status)",
                ),
                (
                    "ix_tasks_created_at",
                    "CREATE INDEX ix_tasks_created_at ON tasks (created_at)",
                ),
                (
                    "ix_tasks_project_id_created_at",
                    "CREATE INDEX ix_tasks_project_id_created_at ON tasks (project_id, created_at)",
                ),
                (
                    "ix_tasks_project_id_plan_position",
                    "CREATE INDEX ix_tasks_project_id_plan_position ON tasks (project_id, plan_position)",
                ),
            ]:
                if not _has_index(engine, "tasks", index_name):
                    connection.execute(text(ddl))

    if "projects" in table_names:
        with engine.begin() as connection:
            for index_name, ddl in [
                (
                    "ix_projects_name",
                    "CREATE INDEX ix_projects_name ON projects (name)",
                ),
                (
                    "ix_projects_updated_at",
                    "CREATE INDEX ix_projects_updated_at ON projects (updated_at)",
                ),
            ]:
                if not _has_index(engine, "projects", index_name):
                    connection.execute(text(ddl))


def _migration_022_knowledge_sync_state(engine: Engine) -> None:
    if "knowledge_items" not in _table_names(engine):
        return
    statements: list[str] = []
    if not _has_column(engine, "knowledge_items", "sync_status"):
        statements.append(
            "ALTER TABLE knowledge_items ADD COLUMN sync_status VARCHAR(20) NOT NULL DEFAULT 'synced'"
        )
    if not _has_column(engine, "knowledge_items", "sync_required_at"):
        statements.append(
            "ALTER TABLE knowledge_items ADD COLUMN sync_required_at DATETIME"
        )
    if not _has_column(engine, "knowledge_items", "last_synced_at"):
        statements.append(
            "ALTER TABLE knowledge_items ADD COLUMN last_synced_at DATETIME"
        )
    if not _has_column(engine, "knowledge_items", "last_sync_error"):
        statements.append("ALTER TABLE knowledge_items ADD COLUMN last_sync_error TEXT")
    if statements:
        with engine.begin() as connection:
            for statement in statements:
                connection.execute(text(statement))


def _migration_050_post_apply_validation_recovery_lifecycle(engine: Engine) -> None:
    """Add immutable Phase 29D-4 post-apply validation and recovery authorities."""

    with engine.begin() as connection:
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_task_post_apply_validations (
                    id INTEGER PRIMARY KEY,
                    execution_plan_id INTEGER NOT NULL,
                    execution_task_id INTEGER NOT NULL,
                    execution_task_attempt_id INTEGER NOT NULL,
                    attempt_generation INTEGER NOT NULL,
                    apply_result_id INTEGER NOT NULL,
                    apply_result_hash VARCHAR(64) NOT NULL,
                    apply_attempt_id INTEGER NOT NULL,
                    apply_attempt_hash VARCHAR(64) NOT NULL,
                    change_set_id INTEGER NOT NULL,
                    change_set_hash VARCHAR(64) NOT NULL,
                    pre_apply_snapshot_id INTEGER,
                    pre_apply_snapshot_hash VARCHAR(64),
                    workspace_target_id INTEGER NOT NULL,
                    workspace_target_hash VARCHAR(64) NOT NULL,
                    base_state_id INTEGER NOT NULL,
                    base_state_hash VARCHAR(64) NOT NULL,
                    validation_policy_id VARCHAR(64) NOT NULL,
                    validation_policy_version INTEGER NOT NULL,
                    status VARCHAR(16) NOT NULL,
                    failure_reason VARCHAR(64),
                    failure_detail VARCHAR(1024),
                    checked_operation_count INTEGER NOT NULL,
                    canonical_payload JSON NOT NULL,
                    canonical_sha256 VARCHAR(64) NOT NULL,
                    validation_idempotency_key VARCHAR(160) NOT NULL UNIQUE,
                    created_at DATETIME NOT NULL,
                    CONSTRAINT uq_execution_task_post_apply_validation_result_policy
                        UNIQUE (apply_result_id, validation_policy_version),
                    CONSTRAINT ck_execution_task_post_apply_validation_bounds
                        CHECK (attempt_generation > 0 AND validation_policy_version > 0
                            AND checked_operation_count >= 0),
                    CONSTRAINT ck_execution_task_post_apply_validation_status
                        CHECK (status IN ('passed', 'failed', 'blocked', 'validation_error')),
                    CONSTRAINT ck_execution_task_post_apply_validation_failure_shape
                        CHECK ((status = 'passed' AND failure_reason IS NULL) OR
                            (status != 'passed' AND failure_reason IS NOT NULL)),
                    FOREIGN KEY(execution_plan_id) REFERENCES execution_plans (id) ON DELETE RESTRICT,
                    FOREIGN KEY(execution_task_id) REFERENCES execution_tasks (id) ON DELETE RESTRICT,
                    FOREIGN KEY(execution_task_attempt_id) REFERENCES execution_task_attempts (id) ON DELETE RESTRICT,
                    FOREIGN KEY(apply_result_id) REFERENCES execution_task_apply_results (id) ON DELETE RESTRICT,
                    FOREIGN KEY(apply_attempt_id) REFERENCES execution_task_apply_attempts (id) ON DELETE RESTRICT,
                    FOREIGN KEY(change_set_id) REFERENCES execution_task_change_sets (id) ON DELETE RESTRICT,
                    FOREIGN KEY(pre_apply_snapshot_id) REFERENCES execution_task_pre_apply_snapshots (id) ON DELETE RESTRICT,
                    FOREIGN KEY(workspace_target_id) REFERENCES execution_workspace_targets (id) ON DELETE RESTRICT,
                    FOREIGN KEY(base_state_id) REFERENCES execution_workspace_base_states (id) ON DELETE RESTRICT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_task_recovery_decisions (
                    id INTEGER PRIMARY KEY,
                    execution_plan_id INTEGER NOT NULL,
                    execution_task_id INTEGER NOT NULL,
                    execution_task_attempt_id INTEGER NOT NULL,
                    attempt_generation INTEGER NOT NULL,
                    apply_result_id INTEGER NOT NULL UNIQUE,
                    apply_result_hash VARCHAR(64) NOT NULL,
                    post_apply_validation_id INTEGER UNIQUE,
                    post_apply_validation_hash VARCHAR(64),
                    decision VARCHAR(32) NOT NULL,
                    decision_reason VARCHAR(64) NOT NULL,
                    decision_detail VARCHAR(1024),
                    canonical_payload JSON NOT NULL,
                    canonical_sha256 VARCHAR(64) NOT NULL,
                    decision_idempotency_key VARCHAR(160) NOT NULL UNIQUE,
                    created_at DATETIME NOT NULL,
                    CONSTRAINT ck_execution_task_recovery_decision_generation_positive
                        CHECK (attempt_generation > 0),
                    CONSTRAINT ck_execution_task_recovery_decision_outcome
                        CHECK (decision IN ('rollback_required', 'no_recovery_required',
                            'recovery_blocked', 'manual_intervention_required')),
                    FOREIGN KEY(execution_plan_id) REFERENCES execution_plans (id) ON DELETE RESTRICT,
                    FOREIGN KEY(execution_task_id) REFERENCES execution_tasks (id) ON DELETE RESTRICT,
                    FOREIGN KEY(execution_task_attempt_id) REFERENCES execution_task_attempts (id) ON DELETE RESTRICT,
                    FOREIGN KEY(apply_result_id) REFERENCES execution_task_apply_results (id) ON DELETE RESTRICT,
                    FOREIGN KEY(post_apply_validation_id) REFERENCES execution_task_post_apply_validations (id) ON DELETE RESTRICT
                )
                """
            )
        )
        connection.execute(
            text(
                """
                CREATE TABLE IF NOT EXISTS execution_task_recovery_results (
                    id INTEGER PRIMARY KEY,
                    execution_plan_id INTEGER NOT NULL,
                    execution_task_id INTEGER NOT NULL,
                    execution_task_attempt_id INTEGER NOT NULL,
                    attempt_generation INTEGER NOT NULL,
                    recovery_decision_id INTEGER NOT NULL UNIQUE,
                    recovery_decision_hash VARCHAR(64) NOT NULL,
                    apply_result_id INTEGER NOT NULL,
                    apply_result_hash VARCHAR(64) NOT NULL,
                    pre_apply_snapshot_id INTEGER,
                    pre_apply_snapshot_hash VARCHAR(64),
                    status VARCHAR(32) NOT NULL,
                    failure_reason VARCHAR(64),
                    failure_detail VARCHAR(1024),
                    rolled_back_operations JSON NOT NULL,
                    canonical_payload JSON NOT NULL,
                    canonical_sha256 VARCHAR(64) NOT NULL,
                    result_idempotency_key VARCHAR(160) NOT NULL UNIQUE,
                    started_at DATETIME NOT NULL,
                    ended_at DATETIME NOT NULL,
                    created_at DATETIME NOT NULL,
                    CONSTRAINT ck_execution_task_recovery_result_generation_positive
                        CHECK (attempt_generation > 0),
                    CONSTRAINT ck_execution_task_recovery_result_status
                        CHECK (status IN ('recovered', 'blocked', 'failed',
                            'manual_intervention_required')),
                    CONSTRAINT ck_execution_task_recovery_result_failure_shape
                        CHECK ((status = 'recovered' AND failure_reason IS NULL) OR
                            (status != 'recovered' AND failure_reason IS NOT NULL)),
                    FOREIGN KEY(execution_plan_id) REFERENCES execution_plans (id) ON DELETE RESTRICT,
                    FOREIGN KEY(execution_task_id) REFERENCES execution_tasks (id) ON DELETE RESTRICT,
                    FOREIGN KEY(execution_task_attempt_id) REFERENCES execution_task_attempts (id) ON DELETE RESTRICT,
                    FOREIGN KEY(recovery_decision_id) REFERENCES execution_task_recovery_decisions (id) ON DELETE RESTRICT,
                    FOREIGN KEY(apply_result_id) REFERENCES execution_task_apply_results (id) ON DELETE RESTRICT,
                    FOREIGN KEY(pre_apply_snapshot_id) REFERENCES execution_task_pre_apply_snapshots (id) ON DELETE RESTRICT
                )
                """
            )
        )
        for statement in (
            "CREATE INDEX IF NOT EXISTS ix_execution_task_post_apply_validations_task_status "
            "ON execution_task_post_apply_validations (execution_task_id, status)",
            "CREATE INDEX IF NOT EXISTS ix_execution_task_post_apply_validations_apply_result "
            "ON execution_task_post_apply_validations (apply_result_id)",
            "CREATE INDEX IF NOT EXISTS ix_execution_task_recovery_decisions_task_decision "
            "ON execution_task_recovery_decisions (execution_task_id, decision)",
            "CREATE INDEX IF NOT EXISTS ix_execution_task_recovery_results_task_status "
            "ON execution_task_recovery_results (execution_task_id, status)",
        ):
            connection.execute(text(statement))


def _migration_051_project_type_override(engine: Engine) -> None:
    if "projects" not in _table_names(engine):
        return
    if not _has_column(engine, "projects", "project_type_override"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE projects ADD COLUMN project_type_override VARCHAR(16)"
                )
            )


def _migration_052_orphan_ownership_observability(engine: Engine) -> None:
    """Add the legacy-to-canonical ownership bridge and process identity."""

    if "task_executions" not in _table_names(engine):
        return
    additions = [
        (
            "runtime_lease_id",
            "ALTER TABLE task_executions ADD COLUMN runtime_lease_id INTEGER",
        ),
        (
            "worker_process_start_identity",
            "ALTER TABLE task_executions ADD COLUMN worker_process_start_identity VARCHAR(255)",
        ),
    ]
    with engine.begin() as connection:
        for column_name, statement in additions:
            if not _has_column(engine, "task_executions", column_name):
                connection.execute(text(statement))
        if not _has_index(
            engine, "task_executions", "ix_task_executions_runtime_lease_id"
        ):
            connection.execute(
                text(
                    "CREATE INDEX ix_task_executions_runtime_lease_id "
                    "ON task_executions (runtime_lease_id)"
                )
            )


def _migration_053_project_retirement(engine: Engine) -> None:
    """Add non-destructive Project retirement metadata without touching rows."""

    if "projects" not in _table_names(engine):
        return
    additions = [
        ("retired_at", "ALTER TABLE projects ADD COLUMN retired_at DATETIME"),
        (
            "retirement_reason",
            "ALTER TABLE projects ADD COLUMN retirement_reason VARCHAR(255)",
        ),
        (
            "retired_by_user_id",
            "ALTER TABLE projects ADD COLUMN retired_by_user_id INTEGER",
        ),
    ]
    with engine.begin() as connection:
        for column_name, statement in additions:
            if not _has_column(engine, "projects", column_name):
                connection.execute(text(statement))


def _migration_054_dogfood_queue_isolation(engine: Engine) -> None:
    """Persist the admission authority used to isolate historical queue work."""

    if "sessions" not in _table_names(engine):
        return
    if not _has_column(engine, "sessions", "dogfood_admitted"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE sessions ADD COLUMN dogfood_admitted "
                    "BOOLEAN NOT NULL DEFAULT 0"
                )
            )


def _migration_055_task_intent_mode(engine: Engine) -> None:
    """Add the optional, backward-compatible typed Task intent constraint."""

    if "tasks" not in _table_names(engine):
        return
    if not _has_column(engine, "tasks", "intent_mode"):
        with engine.begin() as connection:
            connection.execute(
                text(
                    "ALTER TABLE tasks ADD COLUMN intent_mode "
                    "VARCHAR(20) NOT NULL DEFAULT 'default'"
                )
            )
    else:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "UPDATE tasks SET intent_mode = 'default' "
                    "WHERE intent_mode IS NULL OR intent_mode = ''"
                )
            )


def _migration_056_session_lifecycle_authority(engine: Engine) -> None:
    """Add additive Session lifecycle-authority metadata and safe backfills."""

    if "sessions" not in _table_names(engine):
        return

    existing_columns = {
        column["name"] for column in inspect(engine).get_columns("sessions")
    }
    additions = (
        (
            "continuation_task_id",
            "ALTER TABLE sessions ADD COLUMN continuation_task_id INTEGER",
        ),
        (
            "continuation_kind",
            "ALTER TABLE sessions ADD COLUMN continuation_kind VARCHAR(64)",
        ),
        (
            "continuation_retry_count",
            "ALTER TABLE sessions ADD COLUMN continuation_retry_count "
            "INTEGER NOT NULL DEFAULT 0",
        ),
        (
            "continuation_retry_eta",
            "ALTER TABLE sessions ADD COLUMN continuation_retry_eta DATETIME",
        ),
        (
            "lifecycle_updated_at",
            "ALTER TABLE sessions ADD COLUMN lifecycle_updated_at DATETIME",
        ),
    )

    with engine.begin() as connection:
        for column_name, statement in additions:
            if column_name not in existing_columns:
                connection.execute(text(statement))

        # Historical rows receive only deterministic defaults.  In particular,
        # paused status is not treated as evidence of a historical retry.
        connection.execute(
            text(
                "UPDATE sessions SET continuation_retry_count = 0 "
                "WHERE continuation_retry_count IS NULL"
            )
        )

        timestamp_sources = [
            column_name
            for column_name in ("updated_at", "created_at")
            if column_name in existing_columns
        ]
        if timestamp_sources:
            connection.execute(
                text(
                    "UPDATE sessions SET lifecycle_updated_at = COALESCE("
                    + ", ".join(timestamp_sources)
                    + ") WHERE lifecycle_updated_at IS NULL"
                )
            )


def _verify_057_one_shot_execution_authorization(engine: Engine) -> None:
    """Verify the complete additive F1a schema, including SQLite guards."""

    from app.models import ExecutorLaunchIntent, OneShotExecutionAuthorization

    inspector = inspect(engine)
    required_tables = {
        OneShotExecutionAuthorization.__tablename__,
        ExecutorLaunchIntent.__tablename__,
    }
    tables = _table_names(engine)
    if not required_tables <= tables:
        raise RuntimeError("057 one-shot schema is missing a required table")

    expected_columns = {
        OneShotExecutionAuthorization.__tablename__: {
            column.name for column in OneShotExecutionAuthorization.__table__.columns
        },
        ExecutorLaunchIntent.__tablename__: {
            column.name for column in ExecutorLaunchIntent.__table__.columns
        },
    }
    for table_name, expected in expected_columns.items():
        actual = {column["name"] for column in inspector.get_columns(table_name)}
        if not expected <= actual:
            raise RuntimeError(
                f"057 one-shot schema has incomplete {table_name} columns"
            )
        nullable = {
            column["name"]: bool(column.get("nullable", True))
            for column in inspector.get_columns(table_name)
        }
        for column in expected:
            if column not in {"state_changed_at", "revoked_by"} and nullable[column]:
                raise RuntimeError(
                    f"057 one-shot schema has nullable required column {table_name}.{column}"
                )

    expected_unique = {
        OneShotExecutionAuthorization.__tablename__: {
            "uq_one_shot_auth_task_execution": ("task_execution_id",),
            "uq_one_shot_auth_session_generation_task": (
                "session_id",
                "session_instance_id",
                "task_id",
            ),
        },
        ExecutorLaunchIntent.__tablename__: {
            "uq_executor_launch_intents_authorization": ("authorization_uuid",),
            "uq_executor_launch_intents_idempotency_key": ("attempt_idempotency_key",),
            "uq_executor_launch_intents_task_execution": ("task_execution_id",),
        },
    }
    for table_name, constraints in expected_unique.items():
        actual = {
            constraint.get("name"): tuple(constraint.get("column_names") or ())
            for constraint in inspector.get_unique_constraints(table_name)
        }
        if any(actual.get(name) != columns for name, columns in constraints.items()):
            raise RuntimeError(
                f"057 one-shot schema has incomplete uniqueness on {table_name}"
            )

    expected_checks = {
        OneShotExecutionAuthorization.__tablename__: {
            "ck_one_shot_auth_state",
            "ck_one_shot_auth_approved",
            "ck_one_shot_auth_expiry_after_issue",
            "ck_one_shot_auth_source_digest",
            "ck_one_shot_auth_candidate_digest",
            "ck_one_shot_auth_scope_digest",
            "ck_one_shot_auth_identity_digest",
            "ck_one_shot_auth_terminal_shape",
        },
        ExecutorLaunchIntent.__tablename__: {
            "ck_executor_launch_intents_state",
            "ck_executor_launch_intents_idempotency_key",
            "ck_executor_launch_intents_payload_digest",
        },
    }
    for table_name, names in expected_checks.items():
        actual = {
            constraint.get("name")
            for constraint in inspector.get_check_constraints(table_name)
        }
        if not names <= actual:
            raise RuntimeError(
                f"057 one-shot schema has missing checks on {table_name}"
            )

    expected_foreign_keys = {
        OneShotExecutionAuthorization.__tablename__: {
            ("project_id", "projects", "id"),
            ("session_id", "sessions", "id"),
            ("task_id", "tasks", "id"),
            ("task_execution_id", "task_executions", "id"),
        },
        ExecutorLaunchIntent.__tablename__: {
            (
                "authorization_uuid",
                "one_shot_execution_authorizations",
                "authorization_uuid",
            ),
            ("project_id", "projects", "id"),
            ("session_id", "sessions", "id"),
            ("task_id", "tasks", "id"),
            ("task_execution_id", "task_executions", "id"),
        },
    }
    for table_name, expected in expected_foreign_keys.items():
        foreign_keys = inspector.get_foreign_keys(table_name)
        actual = set()
        for foreign_key in foreign_keys:
            actual.add(
                (
                    (foreign_key.get("constrained_columns") or [None])[0],
                    foreign_key.get("referred_table"),
                    (foreign_key.get("referred_columns") or [None])[0],
                )
            )
            ondelete = (foreign_key.get("options") or {}).get("ondelete")
            if ondelete is None or ondelete.upper() != "RESTRICT":
                raise RuntimeError(
                    f"057 one-shot schema has a non-restrictive foreign key on {table_name}"
                )
        if not expected <= actual:
            raise RuntimeError(
                f"057 one-shot schema has incomplete foreign keys on {table_name}"
            )

    expected_indexes = {
        OneShotExecutionAuthorization.__tablename__: {
            "ix_one_shot_auth_state_expiry",
            "ix_one_shot_auth_project_session",
            "ix_one_shot_auth_candidate",
        },
        ExecutorLaunchIntent.__tablename__: {
            "ix_executor_launch_intents_state_created",
        },
    }
    for table_name, names in expected_indexes.items():
        actual = {index["name"] for index in inspector.get_indexes(table_name)}
        if not names <= actual:
            raise RuntimeError(
                f"057 one-shot schema has missing indexes on {table_name}"
            )

    if engine.dialect.name == "sqlite":
        with engine.connect() as connection:
            rows = connection.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
                "AND name IN (?, ?, ?, ?)",
                (
                    "trg_one_shot_auth_immutable_state_guard",
                    "trg_one_shot_auth_no_delete",
                    "trg_executor_launch_intent_immutable",
                    "trg_executor_launch_intent_no_delete",
                ),
            ).fetchall()
        definitions = {row[0]: (row[1] or "").lower() for row in rows}
        auth_trigger = definitions.get("trg_one_shot_auth_immutable_state_guard", "")
        auth_delete_trigger = definitions.get("trg_one_shot_auth_no_delete", "")
        intent_trigger = definitions.get("trg_executor_launch_intent_immutable", "")
        intent_delete_trigger = definitions.get(
            "trg_executor_launch_intent_no_delete", ""
        )
        if (
            "raise(abort" not in auth_trigger
            or "old.state = 'authorized'" not in auth_trigger
        ):
            raise RuntimeError(
                "057 one-shot authorization immutable-state trigger is missing or invalid"
            )
        for column in (
            "authorization_uuid",
            "identity_schema_version",
            "policy_version",
            "project_id",
            "session_id",
            "session_instance_id",
            "task_id",
            "task_execution_id",
            "source_revision_identity",
            "source_snapshot_digest",
            "candidate_uuid",
            "candidate_initial_digest",
            "execution_profile_id",
            "execution_profile_version",
            "operation_scope_id",
            "operation_scope_version",
            "operation_scope_digest",
            "operator_approval_id",
            "operator_identity",
            "operator_approval_decision",
            "operator_approved_at",
            "issued_at",
            "expires_at",
            "identity_sha256",
            "created_at",
        ):
            if (
                f"new.{column}" not in auth_trigger
                or f"old.{column}" not in auth_trigger
            ):
                raise RuntimeError(
                    "057 one-shot authorization trigger does not guard identity"
                )
        if "raise(abort" not in intent_trigger:
            raise RuntimeError(
                "057 launch-intent immutable trigger is missing or invalid"
            )
        if (
            "raise(abort" not in auth_delete_trigger
            or "raise(abort" not in intent_delete_trigger
        ):
            raise RuntimeError(
                "057 one-shot evidence-retention trigger is missing or invalid"
            )


def _migration_057_one_shot_execution_authorization(engine: Engine) -> None:
    """Create additive F1a tables and database-level SQLite immutability guards."""

    from app.models import ExecutorLaunchIntent, OneShotExecutionAuthorization

    with engine.begin() as connection:
        OneShotExecutionAuthorization.__table__.create(connection, checkfirst=True)
        ExecutorLaunchIntent.__table__.create(connection, checkfirst=True)
        if engine.dialect.name == "sqlite":
            connection.exec_driver_sql(
                """
                CREATE TRIGGER IF NOT EXISTS trg_one_shot_auth_immutable_state_guard
                BEFORE UPDATE ON one_shot_execution_authorizations
                WHEN
                    NEW.authorization_uuid IS NOT OLD.authorization_uuid OR
                    NEW.identity_schema_version IS NOT OLD.identity_schema_version OR
                    NEW.policy_version IS NOT OLD.policy_version OR
                    NEW.project_id IS NOT OLD.project_id OR
                    NEW.session_id IS NOT OLD.session_id OR
                    NEW.session_instance_id IS NOT OLD.session_instance_id OR
                    NEW.task_id IS NOT OLD.task_id OR
                    NEW.task_execution_id IS NOT OLD.task_execution_id OR
                    NEW.source_revision_identity IS NOT OLD.source_revision_identity OR
                    NEW.source_snapshot_digest IS NOT OLD.source_snapshot_digest OR
                    NEW.candidate_uuid IS NOT OLD.candidate_uuid OR
                    NEW.candidate_initial_digest IS NOT OLD.candidate_initial_digest OR
                    NEW.execution_profile_id IS NOT OLD.execution_profile_id OR
                    NEW.execution_profile_version IS NOT OLD.execution_profile_version OR
                    NEW.operation_scope_id IS NOT OLD.operation_scope_id OR
                    NEW.operation_scope_version IS NOT OLD.operation_scope_version OR
                    NEW.operation_scope_digest IS NOT OLD.operation_scope_digest OR
                    NEW.operator_approval_id IS NOT OLD.operator_approval_id OR
                    NEW.operator_identity IS NOT OLD.operator_identity OR
                    NEW.operator_approval_decision IS NOT OLD.operator_approval_decision OR
                    NEW.operator_approved_at IS NOT OLD.operator_approved_at OR
                    NEW.issued_at IS NOT OLD.issued_at OR
                    NEW.expires_at IS NOT OLD.expires_at OR
                    NEW.identity_sha256 IS NOT OLD.identity_sha256 OR
                    NEW.created_at IS NOT OLD.created_at OR
                    NOT (
                        (
                            NEW.state = OLD.state AND
                            NEW.state_changed_at IS OLD.state_changed_at AND
                            NEW.revoked_by IS OLD.revoked_by
                        ) OR (
                            OLD.state = 'AUTHORIZED' AND
                            NEW.state IN ('REVOKED', 'EXPIRED') AND
                            NEW.state_changed_at IS NOT NULL AND
                            (
                                (NEW.state = 'REVOKED' AND NEW.revoked_by IS NOT NULL) OR
                                (NEW.state = 'EXPIRED' AND NEW.revoked_by IS NULL)
                            )
                        )
                    )
                BEGIN
                    SELECT RAISE(ABORT, 'one-shot authorization identity/state is immutable');
                END
                """
            )
            connection.exec_driver_sql(
                """
                CREATE TRIGGER IF NOT EXISTS trg_one_shot_auth_no_delete
                BEFORE DELETE ON one_shot_execution_authorizations
                BEGIN
                    SELECT RAISE(ABORT, 'one-shot authorization evidence is retained');
                END
                """
            )
            connection.exec_driver_sql(
                """
                CREATE TRIGGER IF NOT EXISTS trg_executor_launch_intent_immutable
                BEFORE UPDATE ON executor_launch_intents
                BEGIN
                    SELECT RAISE(ABORT, 'executor launch intent is immutable in F1a');
                END
                """
            )
            connection.exec_driver_sql(
                """
                CREATE TRIGGER IF NOT EXISTS trg_executor_launch_intent_no_delete
                BEFORE DELETE ON executor_launch_intents
                BEGIN
                    SELECT RAISE(ABORT, 'executor launch intent evidence is retained');
                END
                """
            )
    _verify_057_one_shot_execution_authorization(engine)


_ONE_SHOT_AUTH_GUARDED_COLUMNS = (
    "authorization_uuid",
    "identity_schema_version",
    "policy_version",
    "project_id",
    "session_id",
    "session_instance_id",
    "task_id",
    "task_execution_id",
    "source_revision_identity",
    "source_snapshot_digest",
    "candidate_uuid",
    "candidate_initial_digest",
    "execution_profile_id",
    "execution_profile_version",
    "operation_scope_id",
    "operation_scope_version",
    "operation_scope_digest",
    "operator_approval_id",
    "operator_identity",
    "operator_approval_decision",
    "operator_approved_at",
    "issued_at",
    "expires_at",
    "identity_sha256",
    "created_at",
)
_LAUNCH_INTENT_GUARDED_COLUMNS = (
    "intent_uuid",
    "authorization_uuid",
    "project_id",
    "session_id",
    "session_instance_id",
    "task_id",
    "task_execution_id",
    "attempt_idempotency_key",
    "request_payload_sha256",
    "created_at",
)


# SQLAlchemy's fixed-width SQLite DATETIME text; exact lexical order holds.
_SQLITE_DATETIME_GLOB = (
    "[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9] "
    "[0-9][0-9]:[0-9][0-9]:[0-9][0-9].[0-9][0-9][0-9][0-9][0-9][0-9]"
)


def _identity_changed_sql(columns: tuple[str, ...]) -> str:
    return " OR\n".join(f"NEW.{column} IS NOT OLD.{column}" for column in columns)


def _058_trigger_statements() -> tuple[str, ...]:
    """F1b guards: CLAIMED/READY are the only new, irreversible transitions."""

    return (
        "DROP TRIGGER IF EXISTS trg_one_shot_auth_immutable_state_guard",
        f"""
        CREATE TRIGGER trg_one_shot_auth_immutable_state_guard
        BEFORE UPDATE ON one_shot_execution_authorizations
        WHEN
            {_identity_changed_sql(_ONE_SHOT_AUTH_GUARDED_COLUMNS)} OR
            NOT (
                (
                    NEW.state = OLD.state AND
                    NEW.state_changed_at IS OLD.state_changed_at AND
                    NEW.revoked_by IS OLD.revoked_by
                ) OR (
                    OLD.state = 'AUTHORIZED' AND
                    NEW.state IN ('REVOKED', 'EXPIRED', 'CLAIMED') AND
                    NEW.state_changed_at IS NOT NULL AND
                    (
                        (NEW.state = 'REVOKED' AND NEW.revoked_by IS NOT NULL) OR
                        (NEW.state = 'EXPIRED' AND NEW.revoked_by IS NULL) OR
                        (
                            NEW.state = 'CLAIMED' AND
                            NEW.revoked_by IS NULL AND
                            NEW.state_changed_at GLOB '{_SQLITE_DATETIME_GLOB}' AND
                            OLD.expires_at GLOB '{_SQLITE_DATETIME_GLOB}' AND
                            NEW.state_changed_at < OLD.expires_at
                        )
                    )
                )
            )
        BEGIN
            SELECT RAISE(ABORT, 'one-shot authorization identity/state is immutable');
        END
        """,
        "DROP TRIGGER IF EXISTS trg_executor_launch_intent_immutable",
        f"""
        CREATE TRIGGER trg_executor_launch_intent_immutable
        BEFORE UPDATE ON executor_launch_intents
        WHEN
            {_identity_changed_sql(_LAUNCH_INTENT_GUARDED_COLUMNS)} OR
            NOT (OLD.state = 'NOT_DISPATCHABLE' AND NEW.state = 'READY') OR
            NOT EXISTS (
                SELECT 1 FROM one_shot_execution_authorizations AS auth
                WHERE auth.authorization_uuid = NEW.authorization_uuid
                  AND auth.state = 'CLAIMED'
                  AND auth.task_execution_id = NEW.task_execution_id
                  AND auth.session_instance_id = NEW.session_instance_id
            )
        BEGIN
            SELECT RAISE(ABORT, 'executor launch intent identity/state is immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_executor_launch_intent_no_delete
        BEFORE DELETE ON executor_launch_intents
        BEGIN
            SELECT RAISE(ABORT, 'executor launch intent evidence is retained');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_one_shot_claim_requires_ready
        BEFORE INSERT ON one_shot_authorization_claims
        WHEN NOT EXISTS (
            SELECT 1 FROM one_shot_execution_authorizations AS auth
            JOIN executor_launch_intents AS li
              ON li.authorization_uuid = auth.authorization_uuid
            WHERE auth.authorization_uuid = NEW.authorization_uuid
              AND auth.state = 'CLAIMED'
              AND auth.identity_sha256 = NEW.identity_sha256
              AND auth.task_execution_id = NEW.task_execution_id
              AND li.intent_uuid = NEW.intent_uuid
              AND li.state = 'READY'
              AND li.task_execution_id = NEW.task_execution_id
              AND li.attempt_idempotency_key = NEW.attempt_idempotency_key
              AND li.request_payload_sha256 = NEW.request_payload_sha256
        )
        BEGIN
            SELECT RAISE(ABORT, 'one-shot claim receipt requires CLAIMED and READY');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_one_shot_claim_immutable
        BEFORE UPDATE ON one_shot_authorization_claims
        BEGIN
            SELECT RAISE(ABORT, 'one-shot claim receipt is immutable');
        END
        """,
        """
        CREATE TRIGGER IF NOT EXISTS trg_one_shot_claim_no_delete
        BEFORE DELETE ON one_shot_authorization_claims
        BEGIN
            SELECT RAISE(ABORT, 'one-shot claim receipt evidence is retained');
        END
        """,
    )


def _rebuild_058_launch_intents(connection, intent_sql: str) -> None:
    """Widen the 057 intent state CHECK, keeping the rest of the stored DDL.

    SQLite cannot alter a CHECK constraint in place. No table references the
    intents table except the (still empty) 058 claim receipts, so the copy,
    drop, and rename run inside the caller's single write transaction.
    """

    prefix = "CREATE TABLE executor_launch_intents ("
    old_check = "CHECK (state = 'NOT_DISPATCHABLE')"
    if not intent_sql.startswith(prefix) or intent_sql.count(old_check) != 1:
        raise RuntimeError("058 found an unexpected executor launch intent DDL")
    staging = "executor_launch_intents_058_staging"
    ddl = intent_sql.replace(
        old_check, "CHECK (state IN ('NOT_DISPATCHABLE', 'READY'))"
    )
    index_sql = [
        row[0]
        for row in connection.exec_driver_sql(
            "SELECT sql FROM sqlite_master WHERE type = 'index' "
            "AND tbl_name = 'executor_launch_intents' AND sql IS NOT NULL"
        )
    ]
    columns = ", ".join(
        row[1]
        for row in connection.exec_driver_sql(
            "PRAGMA table_info(executor_launch_intents)"
        )
    )
    connection.exec_driver_sql(f"CREATE TABLE {staging} (" + ddl[len(prefix) :])
    connection.exec_driver_sql(
        f"INSERT INTO {staging} ({columns}) "
        f"SELECT {columns} FROM executor_launch_intents"
    )
    connection.exec_driver_sql("DROP TABLE executor_launch_intents")
    connection.exec_driver_sql(
        f"ALTER TABLE {staging} RENAME TO executor_launch_intents"
    )
    for statement in index_sql:
        connection.exec_driver_sql(statement)


# F-05: the exact one-shot schema 058 guarantees. These definitions are frozen
# here (not compiled from the models) so that a later model or SQLAlchemy DDL
# change cannot silently redefine what an already-applied 058 must look like.
# Any later migration that changes these objects must update this verifier.
_058_EXPECTED_TABLES = (
    """
    CREATE TABLE one_shot_execution_authorizations (
        authorization_uuid VARCHAR(36) NOT NULL,
        identity_schema_version VARCHAR(64) NOT NULL,
        policy_version VARCHAR(64) NOT NULL,
        project_id INTEGER NOT NULL,
        session_id INTEGER NOT NULL,
        session_instance_id VARCHAR(36) NOT NULL,
        task_id INTEGER NOT NULL,
        task_execution_id INTEGER NOT NULL,
        source_revision_identity VARCHAR(255) NOT NULL,
        source_snapshot_digest VARCHAR(64) NOT NULL,
        candidate_uuid VARCHAR(36) NOT NULL,
        candidate_initial_digest VARCHAR(64) NOT NULL,
        execution_profile_id VARCHAR(128) NOT NULL,
        execution_profile_version VARCHAR(64) NOT NULL,
        operation_scope_id VARCHAR(128) NOT NULL,
        operation_scope_version VARCHAR(64) NOT NULL,
        operation_scope_digest VARCHAR(64) NOT NULL,
        operator_approval_id VARCHAR(255) NOT NULL,
        operator_identity VARCHAR(255) NOT NULL,
        operator_approval_decision VARCHAR(16) NOT NULL,
        operator_approved_at DATETIME NOT NULL,
        issued_at DATETIME NOT NULL,
        expires_at DATETIME NOT NULL,
        identity_sha256 VARCHAR(64) NOT NULL,
        state VARCHAR(16) NOT NULL,
        state_changed_at DATETIME,
        revoked_by VARCHAR(255),
        created_at DATETIME NOT NULL,
        PRIMARY KEY (authorization_uuid),
        CONSTRAINT uq_one_shot_auth_task_execution UNIQUE (task_execution_id),
        CONSTRAINT uq_one_shot_auth_session_generation_task UNIQUE (session_id, session_instance_id, task_id),
        CONSTRAINT ck_one_shot_auth_state CHECK (state IN ('AUTHORIZED', 'CLAIMED', 'REVOKED', 'EXPIRED')),
        CONSTRAINT ck_one_shot_auth_approved CHECK (operator_approval_decision = 'APPROVED'),
        CONSTRAINT ck_one_shot_auth_expiry_after_issue CHECK (expires_at > issued_at),
        CONSTRAINT ck_one_shot_auth_source_digest CHECK (length(source_snapshot_digest) = 64 AND source_snapshot_digest = lower(source_snapshot_digest)),
        CONSTRAINT ck_one_shot_auth_candidate_digest CHECK (length(candidate_initial_digest) = 64 AND candidate_initial_digest = lower(candidate_initial_digest)),
        CONSTRAINT ck_one_shot_auth_scope_digest CHECK (length(operation_scope_digest) = 64 AND operation_scope_digest = lower(operation_scope_digest)),
        CONSTRAINT ck_one_shot_auth_identity_digest CHECK (length(identity_sha256) = 64 AND identity_sha256 = lower(identity_sha256)),
        CONSTRAINT ck_one_shot_auth_terminal_shape CHECK ((state = 'AUTHORIZED' AND state_changed_at IS NULL AND revoked_by IS NULL) OR (state = 'REVOKED' AND state_changed_at IS NOT NULL AND revoked_by IS NOT NULL) OR (state = 'EXPIRED' AND state_changed_at IS NOT NULL AND revoked_by IS NULL) OR (state = 'CLAIMED' AND state_changed_at IS NOT NULL)),
        FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
        FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE RESTRICT,
        FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE RESTRICT,
        FOREIGN KEY(task_execution_id) REFERENCES task_executions (id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE executor_launch_intents (
        intent_uuid VARCHAR(36) NOT NULL,
        authorization_uuid VARCHAR(36) NOT NULL,
        project_id INTEGER NOT NULL,
        session_id INTEGER NOT NULL,
        session_instance_id VARCHAR(36) NOT NULL,
        task_id INTEGER NOT NULL,
        task_execution_id INTEGER NOT NULL,
        attempt_idempotency_key VARCHAR(64) NOT NULL,
        request_payload_sha256 VARCHAR(64) NOT NULL,
        state VARCHAR(32) NOT NULL,
        created_at DATETIME NOT NULL,
        PRIMARY KEY (intent_uuid),
        CONSTRAINT uq_executor_launch_intents_authorization UNIQUE (authorization_uuid),
        CONSTRAINT uq_executor_launch_intents_idempotency_key UNIQUE (attempt_idempotency_key),
        CONSTRAINT uq_executor_launch_intents_task_execution UNIQUE (task_execution_id),
        CONSTRAINT ck_executor_launch_intents_state CHECK (state IN ('NOT_DISPATCHABLE', 'READY')),
        CONSTRAINT ck_executor_launch_intents_idempotency_key CHECK (length(attempt_idempotency_key) = 64 AND attempt_idempotency_key = lower(attempt_idempotency_key)),
        CONSTRAINT ck_executor_launch_intents_payload_digest CHECK (length(request_payload_sha256) = 64 AND request_payload_sha256 = lower(request_payload_sha256)),
        FOREIGN KEY(authorization_uuid) REFERENCES one_shot_execution_authorizations (authorization_uuid) ON DELETE RESTRICT,
        FOREIGN KEY(project_id) REFERENCES projects (id) ON DELETE RESTRICT,
        FOREIGN KEY(session_id) REFERENCES sessions (id) ON DELETE RESTRICT,
        FOREIGN KEY(task_id) REFERENCES tasks (id) ON DELETE RESTRICT,
        FOREIGN KEY(task_execution_id) REFERENCES task_executions (id) ON DELETE RESTRICT
    )
    """,
    """
    CREATE TABLE one_shot_authorization_claims (
        claim_uuid VARCHAR(36) NOT NULL,
        authorization_uuid VARCHAR(36) NOT NULL,
        intent_uuid VARCHAR(36) NOT NULL,
        task_execution_id INTEGER NOT NULL,
        identity_sha256 VARCHAR(64) NOT NULL,
        attempt_idempotency_key VARCHAR(64) NOT NULL,
        request_payload_sha256 VARCHAR(64) NOT NULL,
        claimed_at DATETIME NOT NULL,
        PRIMARY KEY (claim_uuid),
        CONSTRAINT uq_one_shot_claims_authorization UNIQUE (authorization_uuid),
        CONSTRAINT uq_one_shot_claims_intent UNIQUE (intent_uuid),
        CONSTRAINT uq_one_shot_claims_task_execution UNIQUE (task_execution_id),
        CONSTRAINT uq_one_shot_claims_idempotency_key UNIQUE (attempt_idempotency_key),
        CONSTRAINT ck_one_shot_claims_identity_digest CHECK (length(identity_sha256) = 64 AND identity_sha256 = lower(identity_sha256)),
        CONSTRAINT ck_one_shot_claims_idempotency_key CHECK (length(attempt_idempotency_key) = 64 AND attempt_idempotency_key = lower(attempt_idempotency_key)),
        CONSTRAINT ck_one_shot_claims_payload_digest CHECK (length(request_payload_sha256) = 64 AND request_payload_sha256 = lower(request_payload_sha256)),
        FOREIGN KEY(authorization_uuid) REFERENCES one_shot_execution_authorizations (authorization_uuid) ON DELETE RESTRICT,
        FOREIGN KEY(intent_uuid) REFERENCES executor_launch_intents (intent_uuid) ON DELETE RESTRICT,
        FOREIGN KEY(task_execution_id) REFERENCES task_executions (id) ON DELETE RESTRICT
    )
    """,
)
_058_EXPECTED_INDEXES = (
    "CREATE INDEX ix_one_shot_auth_state_expiry ON one_shot_execution_authorizations (state, expires_at)",
    "CREATE INDEX ix_one_shot_auth_project_session ON one_shot_execution_authorizations (project_id, session_id)",
    "CREATE INDEX ix_one_shot_auth_candidate ON one_shot_execution_authorizations (candidate_uuid)",
    "CREATE INDEX ix_executor_launch_intents_state_created ON executor_launch_intents (state, created_at)",
)
# Created by 057 and never replaced by 058 (057's own SQL is inlined there).
_057_AUTH_NO_DELETE_TRIGGER = """
    CREATE TRIGGER trg_one_shot_auth_no_delete
    BEFORE DELETE ON one_shot_execution_authorizations
    BEGIN
        SELECT RAISE(ABORT, 'one-shot authorization evidence is retained');
    END
"""
# UNIQUE/PRIMARY KEY autoindexes, which are fully determined by the table DDL.
_058_EXPECTED_AUTOINDEXES = {
    "one_shot_execution_authorizations": 3,
    "executor_launch_intents": 4,
    "one_shot_authorization_claims": 5,
}

# Every character outside SQLite's SQL whitespace falls into exactly one token,
# so comparing token lists ignores only whitespace between tokens. String
# literals and quoted identifiers stay distinct, whole tokens.
_SQL_TOKEN = re.compile(
    r"""
    '(?:[^']|'')*'
    | "(?:[^"]|"")*"
    | `(?:[^`]|``)*`
    | \[[^\]]*\]
    | --[^\n]*
    | /\*.*?\*/
    | [A-Za-z_][A-Za-z0-9_$]*
    | [0-9]+(?:\.[0-9]*)?
    | <=|>=|<>|!=|==|\|\||<<|>>
    | [^ \t\n\f\r]
    """,
    re.VERBOSE | re.DOTALL,
)


def _sql_tokens(sql: str) -> list[str]:
    return _SQL_TOKEN.findall(sql)


def _058_stored_tokens(statement: str) -> list[str]:
    """Tokens of a CREATE statement as SQLite stores it in sqlite_master."""

    tokens = _sql_tokens(statement)
    if tokens[2:5] == ["IF", "NOT", "EXISTS"]:
        del tokens[2:5]
    return tokens


def _058_expected_schema() -> dict[tuple[str, str], tuple[str, list[str] | None]]:
    statements = (
        *_058_EXPECTED_TABLES,
        *_058_EXPECTED_INDEXES,
        _057_AUTH_NO_DELETE_TRIGGER,
        *(
            statement
            for statement in _058_trigger_statements()
            if statement.lstrip().startswith("CREATE")
        ),
    )
    expected: dict[tuple[str, str], tuple[str, list[str] | None]] = {}
    for statement in statements:
        tokens = _058_stored_tokens(statement)
        kind, name = tokens[1].lower(), tokens[2]
        table = name if kind == "table" else tokens[tokens.index("ON") + 1]
        expected[(kind, name)] = (table, tokens)
    for table, count in _058_EXPECTED_AUTOINDEXES.items():
        for number in range(1, count + 1):
            expected[("index", f"sqlite_autoindex_{table}_{number}")] = (table, None)
    return expected


def _verify_058_exact_sqlite_schema(engine: Engine) -> None:
    """Require every one-shot table, index, and trigger to match exactly.

    The only accepted variations are whitespace between tokens (057 and 058
    create the same trigger with different indentation) and the double-quoted
    table name SQLite writes when 058's rebuild renames the staging table.
    """

    expected = _058_expected_schema()
    protected = set(_058_EXPECTED_AUTOINDEXES)
    with engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT type, name, tbl_name, sql FROM sqlite_master "
            "WHERE type IN ('table', 'index', 'trigger', 'view')"
        ).fetchall()
    actual = {
        (row[0], row[1]): (row[2], row[3])
        for row in rows
        if row[2] in protected or (row[0], row[1]) in expected
    }
    for (kind, name), (table, tokens) in expected.items():
        stored_table, sql = actual.get((kind, name), (None, None))
        stored = None if sql is None else _sql_tokens(sql)
        if (
            kind == "table"
            and stored is not None
            and len(stored) > 2
            and stored[2] == f'"{name}"'
        ):
            stored[2] = name
        if stored_table != table or stored != tokens:
            raise RuntimeError(
                f"058 one-shot claim {kind} is missing or invalid: {name}"
            )
    unexpected = sorted(set(actual) - set(expected))
    if unexpected:
        kind, name = unexpected[0]
        raise RuntimeError(
            f"058 one-shot claim schema has an unexpected {kind}: {name}"
        )


def _verify_058_one_shot_atomic_claim(engine: Engine) -> None:
    """Verify the F1b claim receipt table, widened intent state, and guards."""

    from app.models import ExecutorLaunchIntent, OneShotAuthorizationClaim

    _verify_057_one_shot_execution_authorization(engine)
    inspector = inspect(engine)
    table_name = OneShotAuthorizationClaim.__tablename__
    if table_name not in _table_names(engine):
        raise RuntimeError("058 one-shot claim schema is missing a required table")

    columns = inspector.get_columns(table_name)
    expected = {column.name for column in OneShotAuthorizationClaim.__table__.columns}
    if not expected <= {column["name"] for column in columns}:
        raise RuntimeError("058 one-shot claim schema has incomplete columns")
    if any(column["nullable"] for column in columns if column["name"] in expected):
        raise RuntimeError("058 one-shot claim schema has a nullable column")

    expected_unique = {
        "uq_one_shot_claims_authorization": ("authorization_uuid",),
        "uq_one_shot_claims_intent": ("intent_uuid",),
        "uq_one_shot_claims_task_execution": ("task_execution_id",),
        "uq_one_shot_claims_idempotency_key": ("attempt_idempotency_key",),
    }
    actual_unique = {
        constraint.get("name"): tuple(constraint.get("column_names") or ())
        for constraint in inspector.get_unique_constraints(table_name)
    }
    if any(actual_unique.get(name) != cols for name, cols in expected_unique.items()):
        raise RuntimeError("058 one-shot claim schema has incomplete uniqueness")

    checks = {
        constraint.get("name"): str(constraint.get("sqltext") or "")
        for constraint in inspector.get_check_constraints(table_name)
    }
    if not {
        "ck_one_shot_claims_identity_digest",
        "ck_one_shot_claims_idempotency_key",
        "ck_one_shot_claims_payload_digest",
    } <= set(checks):
        raise RuntimeError("058 one-shot claim schema has missing checks")

    expected_foreign_keys = {
        (
            "authorization_uuid",
            "one_shot_execution_authorizations",
            "authorization_uuid",
        ),
        ("intent_uuid", "executor_launch_intents", "intent_uuid"),
        ("task_execution_id", "task_executions", "id"),
    }
    actual_foreign_keys = set()
    for foreign_key in inspector.get_foreign_keys(table_name):
        ondelete = (foreign_key.get("options") or {}).get("ondelete")
        if ondelete is None or ondelete.upper() != "RESTRICT":
            raise RuntimeError("058 one-shot claim schema has a non-restrictive FK")
        actual_foreign_keys.add(
            (
                (foreign_key.get("constrained_columns") or [None])[0],
                foreign_key.get("referred_table"),
                (foreign_key.get("referred_columns") or [None])[0],
            )
        )
    if not expected_foreign_keys <= actual_foreign_keys:
        raise RuntimeError("058 one-shot claim schema has incomplete foreign keys")

    intent_state_check = {
        constraint.get("name"): str(constraint.get("sqltext") or "")
        for constraint in inspector.get_check_constraints(
            ExecutorLaunchIntent.__tablename__
        )
    }.get("ck_executor_launch_intents_state", "")
    if "'READY'" not in intent_state_check:
        raise RuntimeError("058 launch-intent state check does not permit READY")

    if engine.dialect.name != "sqlite":
        return
    _verify_058_exact_sqlite_schema(engine)


def _migration_058_one_shot_atomic_claim(engine: Engine) -> None:
    """Permit AUTHORIZED→CLAIMED / NOT_DISPATCHABLE→READY and add claim receipts."""

    from app.models import (
        ExecutorLaunchIntent,
        OneShotAuthorizationClaim,
        OneShotExecutionAuthorization,
    )

    if not {
        OneShotExecutionAuthorization.__tablename__,
        ExecutorLaunchIntent.__tablename__,
    } <= _table_names(engine):
        raise RuntimeError("058 requires the 057 one-shot authorization schema")
    if engine.dialect.name != "sqlite":
        with engine.begin() as connection:
            OneShotAuthorizationClaim.__table__.create(connection, checkfirst=True)
        _verify_058_one_shot_atomic_claim(engine)
        return

    # pysqlite does not open a transaction for DDL, so the rebuild and guard
    # replacement run inside one explicit write transaction on this connection.
    with engine.connect() as connection:
        connection.execution_options(isolation_level="AUTOCOMMIT")
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            intent_sql = connection.exec_driver_sql(
                "SELECT sql FROM sqlite_master WHERE type = 'table' AND name = ?",
                (ExecutorLaunchIntent.__tablename__,),
            ).scalar_one()
            if "'ready'" not in intent_sql.lower():
                _rebuild_058_launch_intents(connection, intent_sql)
            OneShotAuthorizationClaim.__table__.create(connection, checkfirst=True)
            for statement in _058_trigger_statements():
                connection.exec_driver_sql(statement)
            if connection.exec_driver_sql(
                "PRAGMA foreign_key_check(executor_launch_intents)"
            ).first():
                raise RuntimeError("058 launch-intent rebuild violates a foreign key")
            connection.exec_driver_sql("COMMIT")
        except BaseException:
            try:
                if connection.connection.dbapi_connection.in_transaction:
                    connection.exec_driver_sql("ROLLBACK")
            except Exception:
                # Closing the DBAPI connection discards any open transaction;
                # keep the original migration error as the one raised.
                connection.invalidate()
            raise
    _verify_058_one_shot_atomic_claim(engine)


_WORKFLOW_BUDGET_GUARDED_COLUMNS = (
    "workflow_uuid",
    "workflow_kind",
    "project_id",
    "session_id",
    "session_instance_id",
    "task_id",
    "root_task_execution_id",
    "planning_session_id",
    "planning_generation_id",
    "policy_id",
    "policy_version",
    "enforcement_mode",
    "max_requests",
    "max_duration_seconds",
    "created_at",
    "deadline_at",
)
_RESERVATION_GUARDED_COLUMNS = (
    "reservation_uuid",
    "workflow_uuid",
    "request_ordinal",
    "idempotency_key",
    "stage",
    "runtime_role",
    "task_execution_id",
    "retry_of_reservation_uuid",
    "reserved_at",
)
_059_TABLES = (
    "workflow_budgets",
    "workflow_task_execution_bindings",
    "provider_request_reservations",
)


def _059_trigger_statements() -> dict[str, str]:
    """R3A guards: immutable policy/deadline, +1 counter, retained evidence.

    Guards reference only the three R3A tables. SQLite re-validates every
    trigger on ``ALTER TABLE``, so a trigger naming a core table that a
    partial historical schema lacks would break unrelated later migrations.
    Root/attempt existence is enforced by the RESTRICT foreign keys; matching
    the live Session generation and attempt number is the service's check.
    """

    glob = _SQLITE_DATETIME_GLOB
    return {
        "trg_workflow_budget_insert_guard": f"""
        CREATE TRIGGER trg_workflow_budget_insert_guard
        BEFORE INSERT ON workflow_budgets
        WHEN NOT (
            NEW.reserved_requests = 0 AND
            NEW.status = 'OPEN' AND
            NEW.created_at GLOB '{glob}' AND
            NEW.deadline_at GLOB '{glob}'
        )
        BEGIN
            SELECT RAISE(ABORT, 'workflow budget must start OPEN and unconsumed');
        END
        """,
        "trg_workflow_budget_update_guard": f"""
        CREATE TRIGGER trg_workflow_budget_update_guard
        BEFORE UPDATE ON workflow_budgets
        WHEN
            {_identity_changed_sql(_WORKFLOW_BUDGET_GUARDED_COLUMNS)} OR
            NOT (
                (
                    NEW.status = OLD.status AND
                    NEW.closed_at IS OLD.closed_at AND
                    OLD.status = 'OPEN' AND
                    NEW.reserved_requests = OLD.reserved_requests + 1
                ) OR (
                    OLD.status = 'OPEN' AND
                    NEW.status = 'CLOSED' AND
                    NEW.closed_at GLOB '{glob}' AND
                    NEW.reserved_requests = OLD.reserved_requests
                )
            )
        BEGIN
            SELECT RAISE(ABORT, 'workflow budget policy/deadline is immutable');
        END
        """,
        "trg_workflow_budget_no_delete": """
        CREATE TRIGGER trg_workflow_budget_no_delete
        BEFORE DELETE ON workflow_budgets
        BEGIN
            SELECT RAISE(ABORT, 'workflow budget evidence is retained');
        END
        """,
        "trg_workflow_binding_insert_guard": f"""
        CREATE TRIGGER trg_workflow_binding_insert_guard
        BEFORE INSERT ON workflow_task_execution_bindings
        WHEN NOT (
            NEW.bound_at GLOB '{glob}' AND
            EXISTS (
                SELECT 1 FROM workflow_budgets AS w
                WHERE w.workflow_uuid = NEW.workflow_uuid
                  AND w.workflow_kind = 'TASK_EXECUTION'
                  AND w.status = 'OPEN'
                  AND w.session_id = NEW.session_id
                  AND w.session_instance_id = NEW.session_instance_id
                  AND w.task_id = NEW.task_id
            ) AND
            (
                (
                    NEW.binding_kind = 'ROOT' AND
                    EXISTS (
                        SELECT 1 FROM workflow_budgets AS w
                        WHERE w.workflow_uuid = NEW.workflow_uuid
                          AND w.root_task_execution_id = NEW.task_execution_id
                    )
                ) OR (
                    NEW.binding_kind IN ('RETRY', 'RECOVERY') AND
                    EXISTS (
                        SELECT 1 FROM workflow_task_execution_bindings AS p
                        WHERE p.task_execution_id = NEW.parent_task_execution_id
                          AND p.workflow_uuid = NEW.workflow_uuid
                          AND p.attempt_number < NEW.attempt_number
                    )
                )
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'workflow attempt binding is invalid');
        END
        """,
        "trg_workflow_binding_immutable": """
        CREATE TRIGGER trg_workflow_binding_immutable
        BEFORE UPDATE ON workflow_task_execution_bindings
        BEGIN
            SELECT RAISE(ABORT, 'workflow attempt binding is immutable');
        END
        """,
        "trg_workflow_binding_no_delete": """
        CREATE TRIGGER trg_workflow_binding_no_delete
        BEFORE DELETE ON workflow_task_execution_bindings
        BEGIN
            SELECT RAISE(ABORT, 'workflow attempt binding evidence is retained');
        END
        """,
        "trg_provider_reservation_insert_guard": f"""
        CREATE TRIGGER trg_provider_reservation_insert_guard
        BEFORE INSERT ON provider_request_reservations
        WHEN NOT (
            NEW.status = 'RESERVED' AND
            NEW.dispatch_started_at IS NULL AND
            NEW.completed_at IS NULL AND
            NEW.outcome_code IS NULL AND
            NEW.reserved_at GLOB '{glob}' AND
            EXISTS (
                SELECT 1 FROM workflow_budgets AS w
                WHERE w.workflow_uuid = NEW.workflow_uuid
                  AND w.status = 'OPEN'
                  AND w.reserved_requests = NEW.request_ordinal
                  AND NEW.reserved_at < w.deadline_at
                  AND (
                      (
                          w.workflow_kind = 'TASK_EXECUTION' AND
                          EXISTS (
                              SELECT 1 FROM workflow_task_execution_bindings AS b
                              WHERE b.task_execution_id = NEW.task_execution_id
                                AND b.workflow_uuid = NEW.workflow_uuid
                          )
                      ) OR (
                          w.workflow_kind = 'PLANNING_SESSION' AND
                          NEW.task_execution_id IS NULL
                      )
                  )
            ) AND
            (
                NEW.retry_of_reservation_uuid IS NULL OR
                EXISTS (
                    SELECT 1 FROM provider_request_reservations AS r
                    WHERE r.reservation_uuid = NEW.retry_of_reservation_uuid
                      AND r.workflow_uuid = NEW.workflow_uuid
                )
            )
        )
        BEGIN
            SELECT RAISE(ABORT, 'provider request reservation was not consumed');
        END
        """,
        "trg_provider_reservation_transition_guard": f"""
        CREATE TRIGGER trg_provider_reservation_transition_guard
        BEFORE UPDATE ON provider_request_reservations
        WHEN
            {_identity_changed_sql(_RESERVATION_GUARDED_COLUMNS)} OR
            NOT (
                (
                    OLD.status = 'RESERVED' AND
                    NEW.status = 'DISPATCH_STARTED' AND
                    NEW.outcome_code IS NULL AND
                    NEW.completed_at IS NULL AND
                    NEW.dispatch_started_at GLOB '{glob}' AND
                    EXISTS (
                        SELECT 1 FROM workflow_budgets AS w
                        WHERE w.workflow_uuid = NEW.workflow_uuid
                          AND w.status = 'OPEN'
                          AND NEW.dispatch_started_at < w.deadline_at
                    )
                ) OR (
                    OLD.status = 'RESERVED' AND
                    NEW.status = 'CANCELLED' AND
                    NEW.dispatch_started_at IS NULL AND
                    NEW.completed_at GLOB '{glob}'
                ) OR (
                    OLD.status = 'DISPATCH_STARTED' AND
                    NEW.status IN (
                        'COMPLETED', 'FAILED', 'TIMED_OUT', 'CANCELLED',
                        'OUTCOME_UNKNOWN'
                    ) AND
                    NEW.dispatch_started_at IS OLD.dispatch_started_at AND
                    NEW.completed_at GLOB '{glob}'
                )
            )
        BEGIN
            SELECT RAISE(ABORT, 'provider request reservation transition is invalid');
        END
        """,
        "trg_provider_reservation_no_delete": """
        CREATE TRIGGER trg_provider_reservation_no_delete
        BEFORE DELETE ON provider_request_reservations
        BEGIN
            SELECT RAISE(ABORT, 'provider request reservation evidence is retained');
        END
        """,
    }


def _verify_059_tables(connection) -> None:
    from app.models import (
        ProviderRequestReservation,
        WorkflowBudget,
        WorkflowTaskExecutionBinding,
    )

    inspector = inspect(connection)
    if not set(_059_TABLES) <= set(inspector.get_table_names()):
        raise RuntimeError("059 workflow budget schema is missing a required table")
    for model in (
        WorkflowBudget,
        WorkflowTaskExecutionBinding,
        ProviderRequestReservation,
    ):
        table = model.__table__
        name = table.name
        columns = {column["name"]: column for column in inspector.get_columns(name)}
        for column in table.columns:
            stored = columns.get(column.name)
            if stored is None or bool(stored.get("nullable", True)) != bool(
                column.nullable and not column.primary_key
            ):
                raise RuntimeError(
                    f"059 schema has an invalid column {name}.{column.name}"
                )
        expected_unique = {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, UniqueConstraint)
        }
        actual_unique = {
            constraint.get("name")
            for constraint in inspector.get_unique_constraints(name)
        }
        expected_checks = {
            constraint.name
            for constraint in table.constraints
            if isinstance(constraint, CheckConstraint)
        }
        actual_checks = {
            constraint.get("name")
            for constraint in inspector.get_check_constraints(name)
        }
        expected_indexes = {index.name for index in table.indexes}
        actual_indexes = {index["name"] for index in inspector.get_indexes(name)}
        if not (
            expected_unique <= actual_unique
            and expected_checks <= actual_checks
            and expected_indexes <= actual_indexes
        ):
            raise RuntimeError(f"059 schema has missing constraints on {name}")
        expected_keys = {
            (fk.parent.name, fk.column.table.name, fk.column.name)
            for fk in table.foreign_keys
        }
        actual_keys = set()
        for foreign_key in inspector.get_foreign_keys(name):
            ondelete = (foreign_key.get("options") or {}).get("ondelete")
            if ondelete is None or ondelete.upper() != "RESTRICT":
                raise RuntimeError(f"059 schema has a non-restrictive key on {name}")
            actual_keys.add(
                (
                    (foreign_key.get("constrained_columns") or [None])[0],
                    foreign_key.get("referred_table"),
                    (foreign_key.get("referred_columns") or [None])[0],
                )
            )
        if actual_keys != expected_keys:
            raise RuntimeError(f"059 schema has incomplete foreign keys on {name}")


def _verify_059_workflow_provider_request_budget(engine: Engine) -> None:
    """Verify the R3A tables, constraints, RESTRICT keys, and exact guards."""

    with engine.connect() as connection:
        if engine.dialect.name == "sqlite":
            # SQLite PRAGMAs answer from a connection's cached schema without
            # checking the schema cookie; a pooled connection that last loaded
            # the schema before 059's DDL would report no indexes. A real read
            # of sqlite_master reloads it; all reflection uses this connection.
            connection.exec_driver_sql("SELECT COUNT(*) FROM sqlite_master").scalar()
        _verify_059_tables(connection)

    if engine.dialect.name != "sqlite":
        return
    expected = _059_trigger_statements()
    with engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT name, sql FROM sqlite_master WHERE type = 'trigger' "
            "AND tbl_name IN (?, ?, ?)",
            _059_TABLES,
        ).fetchall()
    stored = {row[0]: " ".join((row[1] or "").split()) for row in rows}
    if stored != {name: " ".join(sql.split()) for name, sql in expected.items()}:
        raise RuntimeError("059 workflow budget guard triggers are missing or invalid")


def _migration_059_workflow_provider_request_budget(engine: Engine) -> None:
    """Create the additive R3A budget tables and SQLite guards atomically."""

    from app.models import (
        ProviderRequestReservation,
        WorkflowBudget,
        WorkflowTaskExecutionBinding,
    )

    tables = (
        WorkflowBudget.__table__,
        WorkflowTaskExecutionBinding.__table__,
        ProviderRequestReservation.__table__,
    )
    if engine.dialect.name != "sqlite":
        with engine.begin() as connection:
            for table in tables:
                table.create(connection, checkfirst=True)
        _verify_059_workflow_provider_request_budget(engine)
        return

    # pysqlite does not open a transaction for DDL; one explicit write
    # transaction keeps a failed upgrade from leaving partial guards behind.
    with engine.connect() as connection:
        connection.execution_options(isolation_level="AUTOCOMMIT")
        connection.exec_driver_sql("BEGIN IMMEDIATE")
        try:
            for table in tables:
                table.create(connection, checkfirst=True)
            for statement in _059_trigger_statements().values():
                connection.exec_driver_sql(statement)
            connection.exec_driver_sql("COMMIT")
        except BaseException:
            try:
                if connection.connection.dbapi_connection.in_transaction:
                    connection.exec_driver_sql("ROLLBACK")
            except Exception:
                # Closing the DBAPI connection discards any open transaction;
                # keep the original migration error as the one raised.
                connection.invalidate()
            raise
    _verify_059_workflow_provider_request_budget(engine)


MIGRATIONS: tuple[Migration, ...] = (
    Migration(
        version="001_runtime_columns",
        description="Backfill runtime columns and supporting indexes",
        upgrade=_migration_001_runtime_columns,
    ),
    Migration(
        version="002_session_soft_delete_name_strategy",
        description="Rename deleted sessions and enforce active-name uniqueness",
        upgrade=_migration_002_session_name_soft_delete,
    ),
    Migration(
        version="003_planning_sessions",
        description="Create planning session, message, and artifact tables",
        upgrade=_migration_003_planning_sessions,
    ),
    Migration(
        version="004_planning_active_session_index",
        description="Enforce one active planning session per project",
        upgrade=lambda engine: _migration_003_planning_sessions(engine),
    ),
    Migration(
        version="005_planning_processing_lease",
        description="Add processing lease columns for background planning workers",
        upgrade=_migration_005_planning_processing_lease,
    ),
    Migration(
        version="006_planning_artifact_versioning",
        description="Preserve planning artifact history with latest-version markers",
        upgrade=_migration_006_planning_artifact_versioning,
    ),
    Migration(
        version="007_intervention_requests",
        description="Create intervention_requests table for human-in-the-loop orchestration",
        upgrade=_migration_007_intervention_requests,
    ),
    Migration(
        version="008_intervention_initiated_by",
        description="Add initiated_by column to intervention_requests",
        upgrade=_migration_008_intervention_initiated_by,
    ),
    Migration(
        version="009_execution_failure_summaries",
        description="Create execution_failure_summaries table for replan flow",
        upgrade=_migration_009_execution_failure_summaries,
    ),
    Migration(
        version="010_rename_awaiting_input",
        description="Rename session status waiting_for_human to awaiting_input",
        upgrade=_migration_010_rename_awaiting_input,
    ),
    Migration(
        version="011_project_user_ownership",
        description="Add nullable user ownership to projects",
        upgrade=_migration_011_project_user_ownership,
    ),
    Migration(
        version="012_task_template_id",
        description="Add optional workflow template id to tasks",
        upgrade=_migration_012_task_template_id,
    ),
    Migration(
        version="013_failure_metadata",
        description="Add failure_category/backend_id to task_executions and escalation_backend_id to sessions",
        upgrade=_migration_013_failure_metadata,
    ),
    Migration(
        version="014_task_workflow_stage",
        description="Add optional Project Architect workflow stage to tasks",
        upgrade=_migration_014_task_workflow_stage,
    ),
    Migration(
        version="015_session_model_lane",
        description="Add model-lane reporting metadata to sessions",
        upgrade=_migration_015_session_model_lane,
    ),
    Migration(
        version="016_session_repair_churn",
        description="Add repair churn stop flag and trigger to sessions",
        upgrade=_migration_016_session_repair_churn,
    ),
    Migration(
        version="017_human_guidance_conflicts",
        description="Create human_guidance_conflicts table for queryable conflict persistence",
        upgrade=_migration_017_human_guidance_conflicts,
    ),
    Migration(
        version="018_human_guidance_activations",
        description="Create human_guidance_activations table for per-project/session activation controls",
        upgrade=_migration_018_human_guidance_activations,
    ),
    Migration(
        version="019_human_guidance_usage_selection",
        description="Add selection metadata columns to human_guidance_usage",
        upgrade=_migration_019_human_guidance_usage_selection,
    ),
    Migration(
        version="020_human_guidance_backend_targets",
        description="Add backend_targets column to human_guidance for per-backend guidance targeting",
        upgrade=_migration_020_human_guidance_backend_targets,
    ),
    Migration(
        version="021_pagination_indexes",
        description="Add performance indexes for paginated list queries (Phase 15E-2)",
        upgrade=_migration_021_pagination_indexes,
    ),
    Migration(
        version="022_knowledge_sync_state",
        description="Add sync_status, sync_required_at, last_synced_at, last_sync_error to knowledge_items",
        upgrade=_migration_022_knowledge_sync_state,
    ),
    Migration(
        version="023_task_execution_lease",
        description="Add worker ownership and heartbeat fields to task executions",
        upgrade=_migration_023_task_execution_lease,
    ),
    Migration(
        version="024_planning_identity_metadata",
        description="Add creation-time planning and execution identity metadata",
        upgrade=_migration_024_planning_identity_metadata,
    ),
    Migration(
        version="025_task_execution_planner_provenance",
        description="Add immutable planner provenance to task executions",
        upgrade=_migration_025_task_execution_planner_provenance,
    ),
    Migration(
        version="026_planning_generation_fence",
        description="Add immutable Planning Session generations and task observations",
        upgrade=_migration_026_planning_generation_fence,
    ),
    Migration(
        version="027_protocol_v2_persistence",
        description="Add Protocol v2 input, checkpoint, ownership, and manifest persistence",
        upgrade=_migration_027_protocol_v2_persistence,
    ),
    Migration(
        version="028_protocol_v2_input_manifest",
        description="Persist canonical Protocol v2 Input Manifest provenance",
        upgrade=_migration_028_protocol_v2_input_manifest,
    ),
    Migration(
        version="029_planning_brief_checkpoint_metadata",
        description="Persist canonical Planning Brief checkpoint metadata",
        upgrade=_migration_029_planning_brief_checkpoint_metadata,
    ),
    Migration(
        version="030_protocol_v2_operator_review",
        description="Persist append-only Protocol v2 operator-review events and promotions",
        upgrade=_migration_030_protocol_v2_operator_review,
    ),
    Migration(
        version="031_execution_plan_persistence",
        description="Add the Phase 29B-1 Execution Plan graph tables",
        upgrade=_migration_031_execution_plan_persistence,
    ),
    Migration(
        version="032_execution_commit_command",
        description="Add the Phase 29B-3 execution-commit idempotency command binding",
        upgrade=_migration_032_execution_commit_command,
    ),
    Migration(
        version="033_execution_task_lifecycle",
        description="Add Phase 29C-1 Execution Task lifecycle state and transition events",
        upgrade=_migration_033_execution_task_lifecycle,
    ),
    Migration(
        version="034_execution_task_scheduler_claim",
        description="Add Phase 29C-3 durable Execution Task scheduler claims",
        upgrade=_migration_034_execution_task_scheduler_claim,
    ),
    Migration(
        version="035_execution_task_dispatch_intent_attempt",
        description="Add Phase 29C-4 dispatch intents and canonical runtime attempts",
        upgrade=_migration_035_execution_task_dispatch_intent_attempt,
    ),
    Migration(
        version="036_execution_task_runtime_ownership",
        description="Add Phase 29C-5 fenced runtime ownership and running evidence",
        upgrade=_migration_036_execution_task_runtime_ownership,
    ),
    Migration(
        version="037_execution_task_runtime_evidence",
        description="Add Phase 29C-6B runtime starts, progress, and attempt outcomes",
        upgrade=_migration_037_execution_task_runtime_evidence,
    ),
    Migration(
        version="038_execution_task_validation_contract",
        description="Add immutable release-bound validation contract authority",
        upgrade=_migration_038_execution_task_validation_contract,
    ),
    Migration(
        version="039_execution_task_validation_primitives",
        description="Add read-only evidence snapshots and deterministic predicate results",
        upgrade=_migration_039_execution_task_validation_primitives,
    ),
    Migration(
        version="040_execution_task_validation_runs_acceptance",
        description="Add canonical validation runs and acceptance decisions",
        upgrade=_migration_040_execution_task_validation_runs_acceptance,
    ),
    Migration(
        version="041_execution_task_recovery_boundary",
        description="Add Phase 29C-8 recovery authority and replacement-attempt lineage",
        upgrade=_migration_041_execution_task_recovery_boundary,
    ),
    Migration(
        version="042_execution_task_candidate_content_boundary",
        description="Add Phase 29C-9 immutable candidate content authority",
        upgrade=_migration_042_execution_task_candidate_content_boundary,
    ),
    Migration(
        version="043_execution_validation_schema_authority",
        description="Add Phase 29C-10 immutable JSON Schema authority and release linkage",
        upgrade=_migration_043_execution_validation_schema_authority,
    ),
    Migration(
        version="044_execution_evidence_authority",
        description="Add Phase 29C-11 immutable execution evidence authority",
        upgrade=_migration_044_execution_evidence_authority,
    ),
    Migration(
        version="045_execution_evidence_validation_boundary",
        description=("Add Phase 29C-12 execution evidence validation boundary index"),
        upgrade=_migration_045_execution_evidence_validation_boundary,
    ),
    Migration(
        version="046_execution_task_changeset_apply_authorization",
        description=(
            "Add Phase 29D-1 immutable ChangeSet and Controlled Apply "
            "authorization authorities"
        ),
        upgrade=_migration_046_execution_task_changeset_apply_authorization,
    ),
    Migration(
        version="047_workspace_base_state_apply_attempt_boundary",
        description=(
            "Add immutable workspace target/base-state, approval, apply-attempt, "
            "and precondition-verification authorities"
        ),
        upgrade=_migration_047_workspace_base_state_apply_attempt_boundary,
    ),
    Migration(
        version="048_controlled_apply_result_authority",
        description="Add immutable Phase 29D-3 Controlled Apply Result authority",
        upgrade=_migration_048_controlled_apply_result_authority,
    ),
    Migration(
        version="049_pre_apply_snapshot_authority",
        description="Add immutable Phase 29D-3A pre-apply snapshot authority",
        upgrade=_migration_049_pre_apply_snapshot_authority,
    ),
    Migration(
        version="050_post_apply_validation_recovery_lifecycle",
        description=(
            "Add Phase 29D-4 immutable post-apply validation, recovery "
            "decision, and recovery result authorities"
        ),
        upgrade=_migration_050_post_apply_validation_recovery_lifecycle,
    ),
    Migration(
        version="051_project_type_override",
        description="Add Phase 30C manual project-type override column to projects",
        upgrade=_migration_051_project_type_override,
    ),
    Migration(
        version="052_orphan_ownership_observability",
        description=(
            "Add canonical runtime-lease bridge and PID-reuse-safe process identity "
            "for orphan recovery"
        ),
        upgrade=_migration_052_orphan_ownership_observability,
    ),
    Migration(
        version="053_project_retirement",
        description="Add non-destructive Project retirement lifecycle metadata",
        upgrade=_migration_053_project_retirement,
    ),
    Migration(
        version="054_dogfood_queue_isolation",
        description="Persist bounded dogfood admission for historical queue isolation",
        upgrade=_migration_054_dogfood_queue_isolation,
    ),
    Migration(
        version="055_task_intent_mode",
        description="Add typed Task intent mode for bounded create-only planning",
        upgrade=_migration_055_task_intent_mode,
    ),
    Migration(
        version="056_session_lifecycle_authority",
        description="Add additive Session continuation and lifecycle timestamps",
        upgrade=_migration_056_session_lifecycle_authority,
    ),
    Migration(
        version="057_one_shot_execution_authorization",
        description=(
            "Add disabled-by-default one-shot authorization and non-dispatchable intent"
        ),
        upgrade=_migration_057_one_shot_execution_authorization,
        verify=_verify_057_one_shot_execution_authorization,
    ),
    Migration(
        version="058_one_shot_atomic_claim",
        description=(
            "Permit the atomic one-shot claim to READY and retain claim receipts"
        ),
        upgrade=_migration_058_one_shot_atomic_claim,
        verify=_verify_058_one_shot_atomic_claim,
    ),
    Migration(
        version="059_workflow_provider_request_budget",
        description=(
            "Add durable workflow budget owner, attempt bindings, and "
            "provider-request reservations"
        ),
        upgrade=_migration_059_workflow_provider_request_budget,
        verify=_verify_059_workflow_provider_request_budget,
    ),
)


def run_schema_migrations(
    engine: Engine, migrations: Iterable[Migration] = MIGRATIONS
) -> None:
    applied_versions = _get_applied_versions(engine)
    for migration in migrations:
        if migration.version in applied_versions:
            if migration.verify is not None:
                migration.verify(engine)
            continue
        migration.upgrade(engine)
        if migration.verify is not None:
            migration.verify(engine)
        _record_migration(engine, migration)
