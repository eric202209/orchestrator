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
