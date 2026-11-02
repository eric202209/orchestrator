from __future__ import annotations

import json
from types import SimpleNamespace

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.models import (
    Base,
    Plan,
    PlanningArtifact,
    PlanningSession,
    Project,
    Task,
    TaskStatus,
    User,
)
from app.schemas import PlannerTaskCandidate, TaskCreate
from app.api.v1.endpoints import tasks as task_endpoints
from app.services.planning.plan_commit_service import (
    PlanCommitPositionConflictError,
    PlanCommitService,
)
from app.services.planning.planning_session_service import PlanningSessionService
from app.services.planning.task_position_integrity import (
    UnsupportedTaskPositionDialectError,
    acquire_sqlite_write_reservation,
)


MOBILE_KEY = "writer-integration-mobile-key"
MOBILE_HEADERS = {"X-OpenClaw-API-Key": MOBILE_KEY}


def _create_project(client, name: str = "Writer Project") -> dict:
    response = client.post(
        "/api/v1/projects",
        json={"name": name, "description": "Task position writer integration"},
    )
    assert response.status_code == 201
    return response.json()


def _persist_project(db, name: str = "Writer Project") -> Project:
    project = Project(name=name, workspace_path=f"/tmp/{name}", user_id=1)
    db.add(project)
    db.commit()
    db.refresh(project)
    return project


def _persist_plan(db, project_id: int, title: str = "Plan") -> Plan:
    plan = Plan(
        project_id=project_id,
        title=title,
        source_brain="local",
        requirement="requirement",
        markdown="# plan",
        status="committed",
    )
    db.add(plan)
    db.commit()
    db.refresh(plan)
    return plan


def _persist_task(
    db,
    project_id: int,
    position: int | None,
    *,
    plan_id: int | None = None,
    title: str = "Existing Task",
) -> Task:
    task = Task(
        project_id=project_id,
        plan_id=plan_id,
        title=title,
        plan_position=position,
        status=TaskStatus.PENDING,
    )
    db.add(task)
    db.commit()
    db.refresh(task)
    return task


def _candidate(title: str, position: int | None) -> PlannerTaskCandidate:
    return PlannerTaskCandidate(title=title, plan_position=position)


def _complete_planning_session(client, project: dict, monkeypatch) -> dict:
    payload = {
        "requirements": "# Requirements\n\n- Keep Task order stable",
        "design": "# Design\n\n- Use the guarded persistence path",
        "implementation_plan": "# Implementation Plan\n\n1. Preserve positions",
        "planner_markdown": "\n".join(
            [
                f"# Project: {project['name']}",
                "",
                "## Task List",
                "- [ ] TASK_START: First task | Keep position one | order=1 | P1",
                "- [ ] TASK_START: Second task | Keep position two | order=2 | P1",
            ]
        ),
    }
    monkeypatch.setattr(
        PlanningSessionService,
        "_run_openclaw",
        lambda self, prompt, *args, **kwargs: {
            "status": "completed",
            "output": json.dumps(payload),
        },
    )
    response = client.post(
        "/api/v1/planning/sessions",
        json={
            "project_id": project["id"],
            "prompt": "Keep project tasks in a stable order",
            "skip_clarification": True,
        },
    )
    assert response.status_code == 201
    assert response.json()["status"] == "completed"
    return response.json()


def test_plan_commit_preserves_selected_order_and_commit_ownership(db_session):
    project = _persist_project(db_session)
    service = PlanCommitService(db_session)

    plan, tasks = service.create_plan_tasks(
        project,
        [_candidate("Second", 2), _candidate("First", 1)],
        markdown="# Selected order",
        commit=False,
    )

    assert [task.plan_position for task in tasks] == [2, 1]
    assert plan is not None
    assert db_session.in_transaction()
    db_session.commit()
    db_session.expire_all()
    assert db_session.query(Task).filter(Task.project_id == project.id).count() == 2


@pytest.mark.parametrize(
    ("positions", "expected"),
    [([2, 1], [2, 1]), ([2, 4], [2, 4]), ([0, None, -4], [1, 2, -4])],
)
def test_plan_commit_keeps_gaps_order_zero_fallback_and_negative_positions(
    db_session, positions, expected
):
    project = _persist_project(db_session, "Position policy")
    candidates = [
        _candidate(f"Task {index}", position)
        for index, position in enumerate(positions, start=1)
    ]

    _, tasks = PlanCommitService(db_session).create_plan_tasks(project, candidates)

    assert [task.plan_position for task in tasks] == expected


def test_plan_commit_duplicate_candidates_roll_back_created_plan(db_session):
    project = _persist_project(db_session)

    with pytest.raises(PlanCommitPositionConflictError) as raised:
        PlanCommitService(db_session).create_plan_tasks(
            project,
            [_candidate("One", 3), _candidate("Two", 3)],
            markdown="# Invalid duplicate plan",
        )

    assert raised.value.candidate_only is True
    db_session.expire_all()
    assert db_session.query(Plan).filter(Plan.project_id == project.id).count() == 0
    assert db_session.query(Task).filter(Task.project_id == project.id).count() == 0


def test_plan_commit_tolerates_legacy_conflicts_and_distinct_plans_share_position(
    db_session,
):
    project = _persist_project(db_session)
    legacy_plan = _persist_plan(db_session, project.id, "Legacy plan")
    other_plan = _persist_plan(db_session, project.id, "Other plan")
    _persist_task(db_session, project.id, 1, plan_id=legacy_plan.id, title="Legacy A")
    _persist_task(db_session, project.id, 1, plan_id=legacy_plan.id, title="Legacy B")

    _, created = PlanCommitService(db_session).create_plan_tasks(
        project, [_candidate("Other plan task", 1)], plan=other_plan
    )

    assert created[0].plan_position == 1
    assert created[0].plan_id == other_plan.id
    assert db_session.query(Task).filter(Task.project_id == project.id).count() == 3


def test_plan_commit_rejects_plan_project_mismatch(db_session):
    project = _persist_project(db_session, "First Project")
    other = _persist_project(db_session, "Second Project")
    foreign_plan = _persist_plan(db_session, other.id, "Foreign plan")

    with pytest.raises(ValueError, match="does not belong"):
        PlanCommitService(db_session).create_plan_tasks(
            project, [_candidate("Must not persist", 1)], plan=foreign_plan
        )

    db_session.expire_all()
    assert db_session.query(Task).filter(Task.project_id == project.id).count() == 0


def test_batch_api_duplicate_candidates_return_422_and_valid_order_is_kept(
    authenticated_client,
):
    project = _create_project(authenticated_client)
    duplicate = authenticated_client.post(
        f"/api/v1/projects/{project['id']}/batch-tasks",
        json={
            "markdown": "# Invalid batch",
            "tasks": [
                {"title": "Duplicate A", "plan_position": 4},
                {"title": "Duplicate B", "plan_position": 4},
            ],
        },
    )
    assert duplicate.status_code == 422
    assert duplicate.json()["detail"]["code"] == "INVALID_TASK_POSITIONS"

    valid = authenticated_client.post(
        f"/api/v1/projects/{project['id']}/batch-tasks",
        json={
            "markdown": "# Valid batch",
            "tasks": [
                {"title": "Position two", "plan_position": 2},
                {"title": "Position one", "plan_position": 1},
            ],
        },
    )
    assert valid.status_code == 201
    assert [task["plan_position"] for task in valid.json()["tasks"]] == [2, 1]


@pytest.mark.parametrize("collision", ["append", "unassigned-planned"])
def test_batch_api_persisted_collisions_return_409(
    authenticated_client, db_session, collision
):
    project = _create_project(authenticated_client)
    plan = _persist_plan(db_session, project["id"], "Existing plan")
    _persist_task(db_session, project["id"], 1, plan_id=plan.id)
    body = {
        "tasks": [{"title": "Colliding candidate", "plan_position": 1}],
    }
    if collision == "append":
        body["plan_id"] = plan.id
    response = authenticated_client.post(
        f"/api/v1/projects/{project['id']}/batch-tasks", json=body
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "TASK_POSITION_CONFLICT"


def test_batch_api_rejects_plan_from_another_project(authenticated_client, db_session):
    first = _create_project(authenticated_client, "Batch owner")
    second = _create_project(authenticated_client, "Batch requester")
    foreign_plan = _persist_plan(db_session, first["id"], "Foreign")

    response = authenticated_client.post(
        f"/api/v1/projects/{second['id']}/batch-tasks",
        json={
            "plan_id": foreign_plan.id,
            "tasks": [{"title": "Invalid association", "plan_position": 1}],
        },
    )

    assert response.status_code == 404


def test_planning_session_commit_success_uses_guarded_plan_commit(
    authenticated_client, monkeypatch
):
    project = _create_project(authenticated_client, "Planning commit success")
    session = _complete_planning_session(authenticated_client, project, monkeypatch)

    response = authenticated_client.post(
        f"/api/v1/planning/sessions/{session['id']}/commit", json={}
    )

    assert response.status_code == 200
    assert len(response.json()["tasks"]) == 2
    assert response.json()["plan"]["project_id"] == project["id"]


def test_planning_session_position_conflict_rolls_back_all_commit_state(
    authenticated_client, db_session, monkeypatch
):
    project = _create_project(authenticated_client, "Planning conflict rollback")
    session_payload = _complete_planning_session(
        authenticated_client, project, monkeypatch
    )
    session_id = session_payload["id"]
    artifacts_before = (
        db_session.query(PlanningArtifact)
        .filter(
            PlanningArtifact.planning_session_id == session_id,
            PlanningArtifact.artifact_type == "planner_markdown",
        )
        .count()
    )

    response = authenticated_client.post(
        f"/api/v1/planning/sessions/{session_id}/commit",
        json={
            "planner_markdown": "# Edited candidate markdown",
            "selected_tasks": [
                {"title": "Duplicate A", "plan_position": 1},
                {"title": "Duplicate B", "plan_position": 1},
            ],
        },
    )

    assert response.status_code == 422
    assert response.json()["detail"]["code"] == "INVALID_TASK_POSITIONS"
    db_session.expire_all()
    session = db_session.query(PlanningSession).filter_by(id=session_id).one()
    assert session.committed_at is None
    assert session.finalized_plan_id is None
    assert db_session.query(Plan).filter(Plan.project_id == project["id"]).count() == 0
    assert db_session.query(Task).filter(Task.project_id == project["id"]).count() == 0
    assert (
        db_session.query(PlanningArtifact)
        .filter(
            PlanningArtifact.planning_session_id == session_id,
            PlanningArtifact.artifact_type == "planner_markdown",
        )
        .count()
        == artifacts_before
    )


def test_manual_post_rejects_pending_collision_without_persisting_task(
    authenticated_client, db_session
):
    project = _create_project(authenticated_client, "Manual collision")
    _persist_task(db_session, project["id"], 4)

    response = authenticated_client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "Pending collision",
            "plan_position": 4,
        },
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "TASK_POSITION_CONFLICT"
    db_session.expire_all()
    assert db_session.query(Task).filter(Task.project_id == project["id"]).count() == 1


def test_manual_post_fallback_allocation_tolerates_legacy_pairs(
    authenticated_client, db_session
):
    project = _create_project(authenticated_client, "Manual fallback")
    _persist_task(db_session, project["id"], 1, title="Legacy A")
    _persist_task(db_session, project["id"], 1, title="Legacy B")

    response = authenticated_client.post(
        "/api/v1/tasks",
        json={"project_id": project["id"], "title": "Fallback task"},
    )

    assert response.status_code == 201
    assert response.json()["plan_position"] == 2


@pytest.mark.parametrize("position", [0, -3])
def test_manual_post_preserves_explicit_zero_and_negative_positions(
    authenticated_client, position
):
    project = _create_project(authenticated_client, f"Manual explicit {position}")

    response = authenticated_client.post(
        "/api/v1/tasks",
        json={
            "project_id": project["id"],
            "title": "Explicit position",
            "plan_position": position,
        },
    )

    assert response.status_code == 201
    assert response.json()["plan_position"] == position


@pytest.mark.parametrize("collision", ["unassigned", "unassigned-planned"])
def test_mobile_patch_conflict_rolls_back_original_position(
    api_client, db_session, monkeypatch, collision
):
    monkeypatch.setattr(settings, "MOBILE_GATEWAY_API_KEY", MOBILE_KEY)
    project = _persist_project(db_session, f"Mobile {collision}")
    plan = _persist_plan(db_session, project.id, "Mobile plan")
    target = _persist_task(
        db_session,
        project.id,
        2,
        plan_id=plan.id if collision == "unassigned-planned" else None,
        title="Move target",
    )
    _persist_task(
        db_session,
        project.id,
        1,
        plan_id=None,
        title="Unassigned collision partner",
    )

    response = api_client.patch(
        f"/api/v1/mobile/tasks/{target.id}/position",
        json={"plan_position": 1},
        headers=MOBILE_HEADERS,
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "TASK_POSITION_CONFLICT"
    db_session.expire_all()
    assert db_session.query(Task).filter(Task.id == target.id).one().plan_position == 2


def test_mobile_patch_safe_move_and_legacy_conflicts_are_allowed(
    api_client, db_session, monkeypatch
):
    monkeypatch.setattr(settings, "MOBILE_GATEWAY_API_KEY", MOBILE_KEY)
    project = _persist_project(db_session, "Mobile safe move")
    plan = _persist_plan(db_session, project.id, "Legacy plan")
    _persist_task(db_session, project.id, 1, plan_id=plan.id, title="Legacy A")
    _persist_task(db_session, project.id, 1, plan_id=plan.id, title="Legacy B")
    target = _persist_task(db_session, project.id, 3, plan_id=plan.id, title="Move")

    response = api_client.patch(
        f"/api/v1/mobile/tasks/{target.id}/position",
        json={"plan_position": 5},
        headers=MOBILE_HEADERS,
    )

    assert response.status_code == 200
    assert response.json()["plan_position"] == 5


def test_plan_delete_detach_conflict_rolls_back_plan_and_associations(
    authenticated_client, db_session
):
    project = _create_project(authenticated_client, "Delete conflict")
    delete_plan = _persist_plan(db_session, project["id"], "Delete plan")
    keep_plan = _persist_plan(db_session, project["id"], "Keep plan")
    detached = _persist_task(db_session, project["id"], 1, plan_id=delete_plan.id)
    collision = _persist_task(db_session, project["id"], 1, plan_id=keep_plan.id)

    response = authenticated_client.delete(
        f"/api/v1/projects/{project['id']}/plans/{delete_plan.id}"
    )

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "PLAN_DETACH_POSITION_CONFLICT"
    db_session.expire_all()
    assert db_session.query(Plan).filter(Plan.id == delete_plan.id).one()
    assert (
        db_session.query(Task).filter(Task.id == detached.id).one().plan_id
        == delete_plan.id
    )
    assert (
        db_session.query(Task).filter(Task.id == collision.id).one().plan_id
        == keep_plan.id
    )


def test_plan_delete_succeeds_with_unrelated_legacy_conflict(
    authenticated_client, db_session
):
    project = _create_project(authenticated_client, "Delete safe")
    delete_plan = _persist_plan(db_session, project["id"], "Delete plan")
    legacy_plan = _persist_plan(db_session, project["id"], "Legacy plan")
    detached = _persist_task(db_session, project["id"], 2, plan_id=delete_plan.id)
    delete_plan_id = delete_plan.id
    _persist_task(
        db_session, project["id"], 1, plan_id=legacy_plan.id, title="Legacy A"
    )
    _persist_task(
        db_session, project["id"], 1, plan_id=legacy_plan.id, title="Legacy B"
    )

    response = authenticated_client.delete(
        f"/api/v1/projects/{project['id']}/plans/{delete_plan.id}"
    )

    assert response.status_code == 204
    db_session.expire_all()
    assert db_session.query(Plan).filter(Plan.id == delete_plan_id).count() == 0
    assert db_session.query(Task).filter(Task.id == detached.id).one().plan_id is None


def test_sqlite_contention_maps_to_retryable_manual_post_response(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'writer-contention.db'}",
        connect_args={"check_same_thread": False, "timeout": 0.01},
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    holder = factory()
    contender = factory()
    try:
        user = User(
            id=1, email="writer@example.test", hashed_password="unused", is_active=True
        )
        project = Project(
            name="Contended project", workspace_path="/tmp/contended", user_id=1
        )
        holder.add_all([user, project])
        holder.commit()
        acquire_sqlite_write_reservation(holder)

        with pytest.raises(HTTPException) as raised:
            task_endpoints.create_task(
                TaskCreate(
                    project_id=project.id,
                    title="Must retry",
                    description="Contended writer",
                ),
                db=contender,
                current_user=SimpleNamespace(
                    id=1, email="writer@example.test", is_active=True
                ),
            )

        assert raised.value.status_code == 503
        assert raised.value.detail == {
            "code": "TASK_POSITION_WRITE_BUSY",
            "retryable": True,
        }
        contender.rollback()
        assert contender.query(Task).filter(Task.project_id == project.id).count() == 0
        holder.rollback()
        acquire_sqlite_write_reservation(contender)
        contender.rollback()
    finally:
        holder.close()
        contender.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_unsupported_writer_dialect_maps_to_stable_server_error(
    db_session, monkeypatch
):
    project = _persist_project(db_session, "Unsupported dialect")

    def reject_dialect(_db):
        raise UnsupportedTaskPositionDialectError("private dialect details")

    monkeypatch.setattr(
        task_endpoints,
        "acquire_sqlite_write_reservation",
        reject_dialect,
    )

    with pytest.raises(HTTPException) as raised:
        task_endpoints.create_task(
            TaskCreate(project_id=project.id, title="Must fail closed"),
            db=db_session,
            current_user=SimpleNamespace(
                id=1, email="regression@example.com", is_active=True
            ),
        )

    assert raised.value.status_code == 500
    assert raised.value.detail == {"code": "TASK_POSITION_WRITE_UNAVAILABLE"}


def test_plan_commit_commit_and_conflict_rollback_release_sqlite_reservation(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'writer-release.db'}",
        connect_args={"check_same_thread": False, "timeout": 0.01},
    )
    with engine.connect() as connection:
        connection.exec_driver_sql("PRAGMA journal_mode=WAL")
    Base.metadata.create_all(engine)
    factory = sessionmaker(bind=engine, autoflush=False, expire_on_commit=False)
    writer = factory()
    verifier = factory()
    try:
        project = Project(name="Release project", workspace_path="/tmp/release")
        writer.add(project)
        writer.commit()
        PlanCommitService(writer).create_plan_tasks(
            project, [_candidate("Committed", 1)], markdown="# Committed"
        )
        acquire_sqlite_write_reservation(verifier)
        verifier.rollback()

        with pytest.raises(PlanCommitPositionConflictError):
            PlanCommitService(writer).create_plan_tasks(
                project,
                [_candidate("Duplicate A", 1), _candidate("Duplicate B", 1)],
                markdown="# Rolled back",
            )
        acquire_sqlite_write_reservation(verifier)
        verifier.rollback()
    finally:
        writer.close()
        verifier.close()
        Base.metadata.drop_all(engine)
        engine.dispose()
