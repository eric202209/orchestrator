"""Provider-free E1 lifecycle persistence and canonical projection tests."""

from __future__ import annotations

from datetime import datetime, timezone

from sqlalchemy import create_engine, inspect, text

from app.db_migrations import (
    Migration,
    _migration_056_session_lifecycle_authority,
    run_schema_migrations,
)
from app.models import (
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.orchestration.lifecycle.authority import (
    derive_continuation_pending,
    derive_logical_terminal,
    derive_quiescent,
)
from app.services.session.session_inspection_service import (
    derive_orchestration_state_block,
)


def _make_project(db):
    project = Project(name="LA1 E1", workspace_path="/tmp/la1-e1")
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _make_session(db, project, *, status: str):
    session = SessionModel(
        project_id=project.id,
        name=f"LA1 {status}",
        description="E1 lifecycle authority test",
        status=status,
        is_active=status in {"running", "awaiting_input"},
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _make_task(db, project, *, status: TaskStatus = TaskStatus.PENDING):
    task = Task(
        project_id=project.id,
        title="E1 task",
        description="exercise lifecycle projection",
        status=status,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _make_execution(db, session, task, *, status: TaskStatus, failure=None):
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=status,
        failure_category=failure,
    )
    db.add(execution)
    db.commit()
    db.refresh(execution)
    return execution


def test_e1_t1_historical_failed_is_terminal_without_continuation(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="failed")

    block = derive_orchestration_state_block(db_session, session)

    assert block["logical_terminal"] is True
    assert block["is_terminal"] is True
    assert block["continuation_pending"] is False
    assert block["quiescent"] is True


def test_e1_t2_recovering_preserves_failed_attempt_as_nonterminal(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="recovering")
    task = _make_task(db_session, project)
    session.continuation_task_id = task.id
    session.continuation_kind = "automatic_recovery"
    session.continuation_retry_count = 0
    db_session.commit()
    execution = _make_execution(
        db_session, session, task, status=TaskStatus.FAILED, failure="worker_lost"
    )

    block = derive_orchestration_state_block(
        db_session, session, latest_task_execution=execution
    )

    assert block["current_phase"] == "recovering"
    assert block["attempt_status"] == "failed"
    assert block["attempt_failure_reason"] == "worker_lost"
    assert block["continuation_pending"] is True
    assert block["logical_terminal"] is False
    assert block["quiescent"] is False
    assert block["terminal_reason"] is None


def test_e1_t3_retry_pending_is_nonterminal_and_nonquiescent(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="retry_pending")
    task = _make_task(db_session, project)
    eta = datetime(2026, 9, 15, 21, 0, tzinfo=timezone.utc)
    session.continuation_task_id = task.id
    session.continuation_kind = "celery_retry"
    session.continuation_retry_count = 2
    session.continuation_retry_eta = eta
    db_session.commit()
    execution = _make_execution(db_session, session, task, status=TaskStatus.PENDING)

    block = derive_orchestration_state_block(
        db_session, session, latest_task_execution=execution
    )

    assert block["current_phase"] == "retry_pending"
    assert block["attempt_status"] == "pending"
    assert block["continuation_pending"] is True
    assert block["continuation_kind"] == "celery_retry"
    assert block["retry_count"] == 2
    assert block["retry_eta"].replace(tzinfo=None) == eta.replace(tzinfo=None)
    assert block["logical_terminal"] is False
    assert block["quiescent"] is False


def test_e1_t4_completed_is_terminal_without_continuation(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="completed")

    block = derive_orchestration_state_block(db_session, session)

    assert block["logical_terminal"] is True
    assert block["is_terminal"] == block["logical_terminal"]
    assert block["continuation_pending"] is False


def test_e1_t5_paused_without_active_work_is_quiescent(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="paused")

    block = derive_orchestration_state_block(db_session, session)

    assert block["logical_terminal"] is False
    assert block["continuation_pending"] is False
    assert block["quiescent"] is True


def test_e1_t6_paused_with_session_task_residue_is_not_quiescent(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="paused")
    task = _make_task(db_session, project)
    db_session.add(
        SessionTask(session_id=session.id, task_id=task.id, status=TaskStatus.RUNNING)
    )
    db_session.commit()

    block = derive_orchestration_state_block(db_session, session)

    assert block["logical_terminal"] is False
    assert block["continuation_pending"] is False
    assert block["quiescent"] is False


def test_e1_t6b_linked_running_task_residue_is_not_quiescent(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="paused")
    task = _make_task(db_session, project, status=TaskStatus.RUNNING)
    db_session.add(
        SessionTask(session_id=session.id, task_id=task.id, status=TaskStatus.PENDING)
    )
    db_session.commit()

    block = derive_orchestration_state_block(db_session, session)

    assert block["quiescent"] is False


def test_e1_t7_awaiting_input_without_active_work_is_quiescent(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="awaiting_input")

    block = derive_orchestration_state_block(db_session, session)

    assert block["logical_terminal"] is False
    assert block["continuation_pending"] is False
    assert block["quiescent"] is True


def test_e1_t8_unknown_state_fails_closed(db_session):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="legacy_future_state")

    block = derive_orchestration_state_block(db_session, session)

    assert block["current_phase"] is None
    assert block["logical_terminal"] is False
    assert block["is_terminal"] is False
    assert block["quiescent"] is False


def test_e1_t9_malformed_retry_pending_never_becomes_terminal_or_quiescent(
    db_session,
):
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="retry_pending")
    task = _make_task(db_session, project)
    session.continuation_task_id = task.id
    session.continuation_retry_count = 1
    # continuation_kind is required to make the marker internally consistent.
    db_session.commit()

    block = derive_orchestration_state_block(db_session, session)

    assert derive_continuation_pending(session) is False
    assert derive_logical_terminal(session) is False
    assert derive_quiescent(db_session, session) is False
    assert block["logical_terminal"] is False
    assert block["is_terminal"] is False
    assert block["quiescent"] is False


def test_e1_t10_api_projection_exposes_additive_fields_and_terminal_alias(
    authenticated_client,
):
    project_response = authenticated_client.post(
        "/api/v1/projects",
        json={"name": "LA1 API", "workspace_path": "/tmp/la1-api"},
    )
    assert project_response.status_code == 201
    session_response = authenticated_client.post(
        "/api/v1/sessions",
        json={"project_id": project_response.json()["id"], "name": "E1 API"},
    )
    assert session_response.status_code == 201

    response = authenticated_client.get(
        f"/api/v1/sessions/{session_response.json()['id']}"
    )
    assert response.status_code == 200
    state = response.json()["orchestration_state"]
    assert {
        "attempt_status",
        "attempt_failure_reason",
        "continuation_pending",
        "continuation_kind",
        "retry_count",
        "retry_eta",
        "logical_terminal",
        "quiescent",
        "last_transition_at",
        "is_terminal",
    } <= set(state)
    assert state["is_terminal"] == state["logical_terminal"]


def test_e1_t11_migration_is_idempotent_and_backfills_conservative_defaults(
    tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'la1-e1.db'}")
    with engine.begin() as connection:
        connection.execute(
            text(
                "CREATE TABLE sessions ("
                "id INTEGER PRIMARY KEY, status VARCHAR(50), "
                "created_at DATETIME, updated_at DATETIME)"
            )
        )
        connection.execute(
            text(
                "INSERT INTO sessions (id, status, created_at, updated_at) VALUES "
                "(1, 'paused', '2026-09-15 20:00:00', '2026-09-15 20:30:00'), "
                "(2, 'failed', '2026-09-15 20:01:00', NULL)"
            )
        )

    migration = Migration(
        version="056_session_lifecycle_authority",
        description="E1 test migration",
        upgrade=_migration_056_session_lifecycle_authority,
    )
    run_schema_migrations(engine, (migration,))
    run_schema_migrations(engine, (migration,))

    columns = {column["name"] for column in inspect(engine).get_columns("sessions")}
    assert {
        "continuation_task_id",
        "continuation_kind",
        "continuation_retry_count",
        "continuation_retry_eta",
        "lifecycle_updated_at",
    } <= columns
    with engine.connect() as connection:
        rows = (
            connection.execute(
                text(
                    "SELECT id, continuation_task_id, continuation_kind, "
                    "continuation_retry_count, continuation_retry_eta, "
                    "lifecycle_updated_at FROM sessions ORDER BY id"
                )
            )
            .mappings()
            .all()
        )
        applied = connection.execute(
            text(
                "SELECT COUNT(*) FROM schema_migrations "
                "WHERE version = '056_session_lifecycle_authority'"
            )
        ).scalar_one()

    assert applied == 1
    assert rows[0]["continuation_task_id"] is None
    assert rows[0]["continuation_kind"] is None
    assert rows[0]["continuation_retry_count"] == 0
    assert rows[0]["continuation_retry_eta"] is None
    assert rows[0]["lifecycle_updated_at"] == "2026-09-15 20:30:00"
    assert rows[1]["lifecycle_updated_at"] == "2026-09-15 20:01:00"
    engine.dispose()


def test_e1_t12_legacy_api_fields_remain_present(authenticated_client):
    project_response = authenticated_client.post(
        "/api/v1/projects",
        json={"name": "LA1 Compatibility", "workspace_path": "/tmp/la1-compat"},
    )
    assert project_response.status_code == 201
    session_response = authenticated_client.post(
        "/api/v1/sessions",
        json={
            "project_id": project_response.json()["id"],
            "name": "E1 Compatibility",
        },
    )
    assert session_response.status_code == 201
    session_id = session_response.json()["id"]

    body = authenticated_client.get(f"/api/v1/sessions/{session_id}").json()
    assert {
        "id",
        "project_id",
        "task_id",
        "status",
        "execution_mode",
        "is_active",
        "created_at",
        "updated_at",
        "orchestration_state",
    } <= set(body)

    list_response = authenticated_client.get("/api/v1/sessions")
    assert list_response.status_code == 200
    assert any(item["id"] == session_id for item in list_response.json())
