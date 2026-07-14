"""Regression coverage for immutable planning/execution identity snapshots."""

from __future__ import annotations

import json

from sqlalchemy import create_engine, inspect, text

from app.config import settings
from app.db_migrations import (
    Migration,
    _migration_024_planning_identity_metadata,
    _migration_025_task_execution_planner_provenance,
)
from app.models import (
    Plan,
    PlanningSession,
    Project,
    Session as SessionModel,
    Task,
    TaskStatus,
)
from app.services.observability.planning_identity import (
    active_execution_identity,
    active_planning_identity,
)
from app.services.planning.planning_session_service import PlanningSessionService
from app.services.tasks.execution import create_task_execution


def _project_with_task_and_session(db_session):
    project = Project(name="identity-metadata-project")
    db_session.add(project)
    db_session.flush()
    task = Task(project_id=project.id, title="identity-metadata-task")
    session = SessionModel(project_id=project.id, name="identity-metadata-session")
    db_session.add_all([task, session])
    db_session.commit()
    return project, task, session


def _planning_session_for_task(db_session, project, task):
    plan = Plan(
        project_id=project.id,
        title="identity plan",
        source_brain="local",
        requirement="preserve provenance",
        markdown="# Plan",
        status="draft",
    )
    db_session.add(plan)
    db_session.flush()
    task.plan_id = plan.id
    planning_session = PlanningSession(
        project_id=project.id,
        title="originating planning session",
        prompt="Preserve provenance",
        status="completed",
        source_brain="local",
        planning_backend="origin-planning-backend",
        planner_model="origin-planner-model",
        reasoning_profile="origin-reasoning-profile",
        configuration_fingerprint="a" * 64,
        finalized_plan_id=plan.id,
        committed_task_ids=json.dumps([task.id]),
    )
    db_session.add(planning_session)
    db_session.commit()
    return planning_session


def test_planning_session_snapshots_active_planner_identity(db_session, monkeypatch):
    project, _, _ = _project_with_task_and_session(db_session)
    monkeypatch.setattr(PlanningSessionService, "schedule_processing", lambda *_: None)
    expected = active_planning_identity(db_session)

    planning_session = PlanningSessionService(db_session).start_session(
        project, "Persist planning identity"
    )

    assert planning_session.planning_backend == expected["planning_backend"]
    assert planning_session.planner_model == expected["planner_model"]
    assert planning_session.reasoning_profile == expected["reasoning_profile"]
    assert (
        planning_session.configuration_fingerprint
        == expected["configuration_fingerprint"]
    )
    payload = PlanningSessionService(db_session).build_session_payload(planning_session)
    assert payload["planning_backend"] == expected["planning_backend"]
    assert payload["configuration_fingerprint"] == expected["configuration_fingerprint"]


def test_task_execution_snapshots_lanes_and_ignores_later_config_changes(
    db_session, monkeypatch
):
    _, task, session = _project_with_task_and_session(db_session)
    expected = active_execution_identity(db_session)
    execution = create_task_execution(
        db_session, session_id=session.id, task_id=task.id
    )
    db_session.commit()

    monkeypatch.setattr(settings, "PLANNING_BACKEND", "changed-planning-backend")
    monkeypatch.setattr(settings, "EXECUTION_BACKEND", "changed-execution-backend")
    monkeypatch.setattr(settings, "PLANNER_MODEL", "changed-planner-model")
    monkeypatch.setattr(settings, "EXECUTION_MODEL", "changed-executor-model")
    changed = active_execution_identity(db_session)
    db_session.refresh(execution)

    assert execution.planning_backend == expected["planning_backend"]
    assert execution.execution_backend == expected["execution_backend"]
    assert execution.planner_model == expected["planner_model"]
    assert execution.executor_model == expected["executor_model"]
    assert execution.configuration_fingerprint == expected["configuration_fingerprint"]
    assert execution.configuration_fingerprint != changed["configuration_fingerprint"]


def test_task_execution_preserves_originating_planning_session_on_retries(
    db_session, monkeypatch
):
    project, task, session = _project_with_task_and_session(db_session)
    planning_session = _planning_session_for_task(db_session, project, task)

    first = create_task_execution(db_session, session_id=session.id, task_id=task.id)
    db_session.commit()
    monkeypatch.setattr(settings, "PLANNING_BACKEND", "later-planning-backend")
    monkeypatch.setattr(settings, "PLANNER_MODEL", "later-planner-model")
    second = create_task_execution(db_session, session_id=session.id, task_id=task.id)
    db_session.commit()

    for execution in (first, second):
        assert execution.planning_session_id == planning_session.id
        assert execution.planning_backend == planning_session.planning_backend
        assert execution.planner_model == planning_session.planner_model
        assert execution.reasoning_profile == planning_session.reasoning_profile
        assert (
            execution.configuration_fingerprint
            == planning_session.configuration_fingerprint
        )
    assert first.attempt_number == 1
    assert second.attempt_number == 2


def test_existing_reads_and_trace_export_expose_complete_planner_provenance(
    authenticated_client, db_session
):
    project, task, session = _project_with_task_and_session(db_session)
    planning_session = _planning_session_for_task(db_session, project, task)
    session.status = "stopped"
    session.is_active = False
    task.status = TaskStatus.FAILED
    execution = create_task_execution(
        db_session,
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.FAILED,
    )
    db_session.commit()

    expected = {
        "task_execution_id": execution.id,
        "planning_session_id": planning_session.id,
        "planning_backend": planning_session.planning_backend,
        "execution_backend": execution.execution_backend,
        "planner_model": planning_session.planner_model,
        "executor_model": execution.executor_model,
        "reasoning_profile": planning_session.reasoning_profile,
        "configuration_fingerprint": planning_session.configuration_fingerprint,
    }
    task_payload = authenticated_client.get(f"/api/v1/tasks/{task.id}")
    failure_payload = authenticated_client.get(
        f"/api/v1/sessions/{session.id}/failure-summary?enrich=false"
    )
    export_payload = authenticated_client.get(
        f"/api/v1/sessions/{session.id}/trace-export"
    )

    assert task_payload.status_code == 200
    assert failure_payload.status_code == 200
    assert export_payload.status_code == 200
    assert task_payload.json()["latest_execution_identity"] == expected
    assert failure_payload.json()["latest_execution_identity"] == expected
    assert export_payload.json()["latest_execution_identity"] == expected


def test_identity_migration_is_additive_and_preserves_existing_rows():
    engine = create_engine("sqlite://")
    try:
        with engine.begin() as connection:
            connection.execute(
                text(
                    "CREATE TABLE planning_sessions (id INTEGER PRIMARY KEY, title VARCHAR(255))"
                )
            )
            connection.execute(
                text(
                    "CREATE TABLE task_executions (id INTEGER PRIMARY KEY, attempt_number INTEGER)"
                )
            )
            connection.execute(
                text("INSERT INTO planning_sessions (id, title) VALUES (1, 'legacy')")
            )
            connection.execute(
                text("INSERT INTO task_executions (id, attempt_number) VALUES (1, 1)")
            )

        _migration_024_planning_identity_metadata(engine)
        _migration_025_task_execution_planner_provenance(engine)
        inspector = inspect(engine)
        planning_columns = {
            column["name"] for column in inspector.get_columns("planning_sessions")
        }
        execution_columns = {
            column["name"] for column in inspector.get_columns("task_executions")
        }
        assert {
            "planning_backend",
            "planner_model",
            "reasoning_profile",
            "configuration_fingerprint",
        } <= planning_columns
        assert {
            "planning_backend",
            "execution_backend",
            "planner_model",
            "executor_model",
            "planning_session_id",
            "reasoning_profile",
            "configuration_fingerprint",
        } <= execution_columns

        with engine.connect() as connection:
            planning_row = (
                connection.execute(text("SELECT * FROM planning_sessions WHERE id = 1"))
                .mappings()
                .one()
            )
            execution_row = (
                connection.execute(text("SELECT * FROM task_executions WHERE id = 1"))
                .mappings()
                .one()
            )
        assert planning_row["title"] == "legacy"
        assert planning_row["planning_backend"] is None
        assert execution_row["attempt_number"] == 1
        assert execution_row["execution_backend"] is None
        assert execution_row["planning_session_id"] is None
        assert execution_row["reasoning_profile"] is None
    finally:
        engine.dispose()
