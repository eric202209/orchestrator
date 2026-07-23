"""Lightweight schema migration runner.

This replaces the old best-effort column backfill logic with explicit,
versioned migrations that are tracked in the database.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Callable, Iterable
import hashlib
import json
import unicodedata
import uuid

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
    with engine.begin() as connection:
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
    with engine.begin() as connection:
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
    with engine.begin() as connection:
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
