"""Provider-free E5 public lifecycle, operator, and stream regressions."""

from __future__ import annotations

import pytest

from app.config import settings
from app.models import (
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.orchestration.lifecycle.authority import derive_lifecycle_authority
from app.services.orchestration.lifecycle.transitions import (
    claim_continuation,
    enter_recovering,
    finalize_logical_failure,
    finalize_logical_success,
    schedule_continuation,
)
from app.services.session.session_stream_service import (
    _stream_is_terminal,
    _stream_lifecycle_projection,
)


_MATRIX_ACTIONS = {
    "pending": ("view_logs", "view_timeline", "start_session"),
    "running": ("view_logs", "view_timeline", "pause_session", "stop_session"),
    "recovering": ("view_logs", "view_timeline"),
    "retry_pending": ("view_logs", "view_timeline"),
    "paused": ("view_logs", "view_timeline", "resume_session", "stop_session"),
    "awaiting_input": (
        "view_logs",
        "view_timeline",
        "submit_guidance",
        "stop_session",
    ),
    "completed": ("view_logs", "view_timeline", "start_session"),
    "failed": (
        "view_logs",
        "view_timeline",
        "resume_session",
        "retry_task",
        "start_session",
    ),
    "stopped": ("view_logs", "view_timeline", "resume_session", "start_session"),
    "cancelled": ("view_logs", "view_timeline", "resume_session", "start_session"),
}


def _make_project(db, name: str = "E5 project"):
    project = Project(name=name, workspace_path="/tmp/e5-provider-free")
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _make_session(db, project, *, status: str):
    session = SessionModel(
        project_id=project.id,
        name=f"E5 {status} {db.query(SessionModel).count()}",
        description="E5 public lifecycle test",
        status=status,
        is_active=status in {"running", "recovering", "retry_pending"},
        instance_id=f"e5-{status}",
    )
    db.add(session)
    db.commit()
    db.refresh(session)
    return session


def _add_attempt(db, session, project, *, status: TaskStatus):
    task = Task(
        project_id=project.id,
        title="E5 task",
        description="E5 attempt",
        status=status,
    )
    db.add(task)
    db.flush()
    link = SessionTask(session_id=session.id, task_id=task.id, status=status)
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=status,
        failure_category="worker_lost" if status == TaskStatus.FAILED else None,
    )
    db.add_all([link, execution])
    db.commit()
    db.refresh(task)
    db.refresh(execution)
    return task, execution


def _api_session(client, name: str) -> int:
    project_response = client.post(
        "/api/v1/projects",
        json={
            "name": name,
            "workspace_path": f"/tmp/{name.lower().replace(' ', '-')}",
        },
    )
    assert project_response.status_code == 201
    session_response = client.post(
        "/api/v1/sessions",
        json={"project_id": project_response.json()["id"], "name": name},
    )
    assert session_response.status_code == 201
    return session_response.json()["id"]


@pytest.mark.parametrize(
    ("status", "attempt_status", "continuation_kind", "expected"),
    [
        ("pending", None, None, (False, True, False)),
        ("running", TaskStatus.RUNNING, None, (False, False, False)),
        ("recovering", TaskStatus.FAILED, "automatic_recovery", (False, False, True)),
        ("retry_pending", TaskStatus.PENDING, "celery_retry", (False, False, True)),
        (
            "retry_pending",
            TaskStatus.PENDING,
            "automatic_recovery",
            (False, False, True),
        ),
        (
            "retry_pending",
            TaskStatus.PENDING,
            "backend_capacity",
            (False, False, True),
        ),
        ("paused", None, None, (False, True, False)),
        ("awaiting_input", None, None, (False, True, False)),
        ("completed", None, None, (True, True, False)),
        ("failed", TaskStatus.FAILED, None, (True, True, False)),
        ("stopped", None, None, (True, True, False)),
        ("cancelled", None, None, (True, True, False)),
    ],
)
def test_e5_allowed_actions_matrix_is_authority_owned(
    db_session, status, attempt_status, continuation_kind, expected
):
    project = _make_project(db_session, f"E5 matrix {status} {continuation_kind}")
    session = _make_session(db_session, project, status=status)
    if attempt_status is not None:
        task, _ = _add_attempt(db_session, session, project, status=attempt_status)
        if continuation_kind:
            session.continuation_task_id = task.id
            session.continuation_kind = continuation_kind
            session.continuation_retry_count = 1
            db_session.commit()

    authority = derive_lifecycle_authority(db_session, session)
    logical_terminal, quiescent, continuation_pending = expected

    assert authority.logical_terminal is logical_terminal
    assert authority.quiescent is quiescent
    assert authority.continuation_pending is continuation_pending
    assert authority.is_terminal is authority.logical_terminal
    assert authority.allowed_actions == _MATRIX_ACTIONS[status]
    if status == "paused":
        assert authority.current_phase == "paused"
    if status in {"recovering", "retry_pending"}:
        assert authority.terminal_reason is None


def test_e5_public_api_failed_attempt_recovery_retry_running_completed(
    authenticated_client, db_session
):
    session_id = _api_session(authenticated_client, "E5 lifecycle sequence")
    session = db_session.get(SessionModel, session_id)
    project = db_session.get(Project, session.project_id)
    session.status = "running"
    session.is_active = True
    task, execution = _add_attempt(
        db_session, session, project, status=TaskStatus.RUNNING
    )
    db_session.commit()

    def state():
        response = authenticated_client.get(f"/api/v1/sessions/{session_id}")
        assert response.status_code == 200
        return response.json()["orchestration_state"]

    enter_recovering(
        db_session,
        session,
        task_execution=execution,
        failure_reason="worker_lost",
        continuation_kind="automatic_recovery",
        retry_count=0,
    )
    db_session.commit()
    recovering = state()
    assert recovering["attempt_status"] == "failed"
    assert recovering["attempt_failure_reason"] == "worker_lost"
    assert recovering["logical_terminal"] is False
    assert recovering["continuation_pending"] is True
    assert recovering["terminal_reason"] is None

    identity = schedule_continuation(
        db_session,
        session,
        continuation_kind="celery_retry",
        retry_count=1,
    )
    db_session.commit()
    retry_pending = state()
    assert retry_pending["current_phase"] == "retry_pending"
    assert retry_pending["logical_terminal"] is False
    assert retry_pending["continuation_pending"] is True
    assert retry_pending["continuation_kind"] == "celery_retry"

    assert claim_continuation(db_session, identity).accepted is True
    db_session.commit()
    running = state()
    assert running["current_phase"] == "step_executing"
    assert running["logical_terminal"] is False
    assert running["continuation_pending"] is False

    pending_execution = db_session.get(TaskExecution, identity.task_execution_id)
    finalize_logical_success(db_session, session, task_execution=pending_execution)
    db_session.commit()
    completed = state()
    assert completed["logical_terminal"] is True
    assert completed["is_terminal"] is True
    assert completed["continuation_pending"] is False
    assert completed["terminal_reason"] is None


def test_e5_session_list_and_mobile_summary_share_authority(
    authenticated_client, db_session, monkeypatch
):
    session_id = _api_session(authenticated_client, "E5 mixed list")
    session = db_session.get(SessionModel, session_id)
    project = db_session.get(Project, session.project_id)
    session.status = "retry_pending"
    session.is_active = True
    task, _ = _add_attempt(db_session, session, project, status=TaskStatus.PENDING)
    session.continuation_task_id = task.id
    session.continuation_kind = "backend_capacity"
    session.continuation_retry_count = 1
    db_session.commit()

    list_response = authenticated_client.get("/api/v1/sessions")
    assert list_response.status_code == 200
    item = next(row for row in list_response.json() if row["id"] == session_id)
    assert item["orchestration_state"]["current_phase"] == "retry_pending"
    assert item["orchestration_state"]["logical_terminal"] is False
    assert item["orchestration_state"]["continuation_pending"] is True

    monkeypatch.setattr(settings, "MOBILE_GATEWAY_API_KEY", "e5-mobile-key")
    mobile_list = authenticated_client.get(
        "/api/v1/mobile/sessions",
        headers={"X-OpenClaw-API-Key": "e5-mobile-key"},
    )
    assert mobile_list.status_code == 200
    mobile_item = next(
        row for row in mobile_list.json()["sessions"] if row["id"] == session_id
    )
    assert mobile_item["orchestration_state"] == item["orchestration_state"]

    summary = authenticated_client.get(
        f"/api/v1/mobile/sessions/{session_id}/summary",
        headers={"X-OpenClaw-API-Key": "e5-mobile-key"},
    )
    assert summary.status_code == 200
    assert summary.json()["orchestration_state"] == item["orchestration_state"]


def test_e5_operator_pause_revokes_retry_and_public_projection_stays_quiescent(
    authenticated_client, db_session, monkeypatch
):
    session_id = _api_session(authenticated_client, "E5 operator pause")
    session = db_session.get(SessionModel, session_id)
    project = db_session.get(Project, session.project_id)
    session.status = "running"
    session.is_active = True
    task, execution = _add_attempt(
        db_session, session, project, status=TaskStatus.RUNNING
    )
    identity = enter_recovering(
        db_session,
        session,
        task_execution=execution,
        continuation_kind="automatic_recovery",
    )
    identity = schedule_continuation(
        db_session,
        session,
        continuation_kind="automatic_recovery",
        retry_count=1,
    )
    db_session.commit()
    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.revoke_session_celery_tasks",
        lambda *args, **kwargs: [],
    )

    response = authenticated_client.post(f"/api/v1/sessions/{session_id}/pause")
    assert response.status_code == 200, response.text
    state = response.json()["orchestration_state"]
    assert state["current_phase"] == "paused"
    assert state["logical_terminal"] is False
    assert state["continuation_pending"] is False
    assert state["quiescent"] is True

    result = claim_continuation(db_session, identity)
    assert result.accepted is False
    db_session.expire_all()
    stale = authenticated_client.get(f"/api/v1/sessions/{session_id}").json()[
        "orchestration_state"
    ]
    assert stale["current_phase"] == "paused"
    assert stale["logical_terminal"] is False
    assert stale["quiescent"] is True


def test_e5_operator_stop_revokes_retry_and_public_projection_is_terminal(
    authenticated_client, db_session, monkeypatch
):
    session_id = _api_session(authenticated_client, "E5 operator stop")
    session = db_session.get(SessionModel, session_id)
    project = db_session.get(Project, session.project_id)
    session.status = "running"
    session.is_active = True
    task, execution = _add_attempt(
        db_session, session, project, status=TaskStatus.RUNNING
    )
    enter_recovering(
        db_session,
        session,
        task_execution=execution,
        continuation_kind="automatic_recovery",
    )
    identity = schedule_continuation(
        db_session,
        session,
        continuation_kind="automatic_recovery",
        retry_count=1,
    )
    db_session.commit()
    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.revoke_session_celery_tasks",
        lambda *args, **kwargs: [],
    )
    response = authenticated_client.post(
        f"/api/v1/sessions/{session_id}/stop?force=true"
    )
    assert response.status_code == 200, response.text
    state = response.json()["orchestration_state"]
    assert state["current_phase"] == "cancelled"
    assert state["logical_terminal"] is True
    assert state["is_terminal"] is True
    assert state["continuation_pending"] is False

    assert claim_continuation(db_session, identity).accepted is False
    stale = authenticated_client.get(f"/api/v1/sessions/{session_id}").json()[
        "orchestration_state"
    ]
    assert stale["logical_terminal"] is True
    assert stale["continuation_pending"] is False


def test_e5_paused_failure_keeps_attempt_reason_out_of_terminal_reason(db_session):
    project = _make_project(db_session, "E5 paused attempt reason")
    session = _make_session(db_session, project, status="paused")
    _, execution = _add_attempt(db_session, session, project, status=TaskStatus.FAILED)

    authority = derive_lifecycle_authority(db_session, session)
    assert authority.attempt_failure_reason == execution.failure_category
    assert authority.terminal_reason is None
    assert authority.logical_terminal is False


def test_e5_capacity_and_final_failure_public_projections_are_terminality_safe(
    authenticated_client, db_session
):
    capacity_id = _api_session(authenticated_client, "E5 capacity public")
    capacity_session = db_session.get(SessionModel, capacity_id)
    capacity_project = db_session.get(Project, capacity_session.project_id)
    capacity_session.status = "pending"
    capacity_session.is_active = False
    task, _ = _add_attempt(
        db_session, capacity_session, capacity_project, status=TaskStatus.PENDING
    )
    schedule_continuation(
        db_session,
        capacity_session,
        task_id=task.id,
        continuation_kind="backend_capacity",
        retry_count=2,
    )
    db_session.commit()
    capacity = authenticated_client.get(f"/api/v1/sessions/{capacity_id}").json()[
        "orchestration_state"
    ]
    assert capacity["current_phase"] == "retry_pending"
    assert capacity["continuation_kind"] == "backend_capacity"
    assert capacity["logical_terminal"] is False
    assert capacity["continuation_pending"] is True
    assert capacity["quiescent"] is False

    failure_id = _api_session(authenticated_client, "E5 final failure public")
    failure_session = db_session.get(SessionModel, failure_id)
    failure_project = db_session.get(Project, failure_session.project_id)
    failure_session.status = "running"
    failure_session.is_active = True
    _, execution = _add_attempt(
        db_session, failure_session, failure_project, status=TaskStatus.RUNNING
    )
    enter_recovering(
        db_session,
        failure_session,
        task_execution=execution,
        continuation_kind="automatic_recovery",
    )
    stale_identity = schedule_continuation(
        db_session,
        failure_session,
        continuation_kind="automatic_recovery",
        retry_count=1,
    )
    finalize_logical_failure(
        db_session,
        failure_session,
        task_execution=execution,
        failure_reason="unrecoverable",
    )
    db_session.commit()
    assert claim_continuation(db_session, stale_identity).accepted is False
    failed = authenticated_client.get(f"/api/v1/sessions/{failure_id}").json()[
        "orchestration_state"
    ]
    assert failed["current_phase"] == "failed"
    assert failed["logical_terminal"] is True
    assert failed["is_terminal"] is True
    assert failed["continuation_pending"] is False
    assert failed["terminal_reason"] == "unrecoverable"


def test_e5_stream_terminality_uses_authority_for_recovering_retry_and_pause(
    db_session,
):
    project = _make_project(db_session, "E5 stream semantics")
    for status, attempt_status, kind in [
        ("recovering", TaskStatus.FAILED, "automatic_recovery"),
        ("retry_pending", TaskStatus.PENDING, "celery_retry"),
        ("retry_pending", TaskStatus.PENDING, "backend_capacity"),
    ]:
        session = _make_session(db_session, project, status=status)
        task, _ = _add_attempt(db_session, session, project, status=attempt_status)
        session.continuation_task_id = task.id
        session.continuation_kind = kind
        session.continuation_retry_count = 1
        db_session.commit()
        assert _stream_is_terminal(db_session, session) is False
        assert (
            _stream_lifecycle_projection(db_session, session)["logical_terminal"]
            is False
        )

    paused = _make_session(db_session, project, status="paused")
    assert _stream_is_terminal(db_session, paused) is False

    failed = _make_session(db_session, project, status="failed")
    assert _stream_is_terminal(db_session, failed) is True


def test_e5_openapi_keeps_additive_session_lifecycle_schema(authenticated_client):
    schema = authenticated_client.get("/openapi.json")
    assert schema.status_code == 200
    definitions = schema.json()["components"]["schemas"]
    lifecycle = definitions["OrchestrationStateResponse"]["properties"]
    assert {
        "current_phase",
        "terminal_reason",
        "attempt_status",
        "attempt_failure_reason",
        "continuation_pending",
        "continuation_kind",
        "retry_count",
        "retry_eta",
        "logical_terminal",
        "quiescent",
        "last_transition_at",
        "coordinator",
        "allowed_actions",
        "is_terminal",
    } <= set(lifecycle)
