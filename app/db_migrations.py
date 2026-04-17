"""Lightweight schema migration runner.

This replaces the old best-effort column backfill logic with explicit,
versioned migrations that are tracked in the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable

from sqlalchemy import inspect, text
from sqlalchemy.engine import Engine


MigrationFn = Callable[[Engine], None]


@dataclass(frozen=True)
class Migration:
    version: str
    description: str
    upgrade: MigrationFn


def _has_column(engine: Engine, table_name: str, column_name: str) -> bool:
    inspector = inspect(engine)
    return column_name in {
        column["name"] for column in inspector.get_columns(table_name)
    }


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
)


def run_schema_migrations(
    engine: Engine, migrations: Iterable[Migration] = MIGRATIONS
) -> None:
    applied_versions = _get_applied_versions(engine)
    for migration in migrations:
        if migration.version in applied_versions:
            continue
        migration.upgrade(engine)
        _record_migration(engine, migration)
