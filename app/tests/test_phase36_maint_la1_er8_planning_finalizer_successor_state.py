"""Provider-free ER8 Planning finalizer successor-state adjudication.

Phase 36 Maintenance LA1-ER8.  The successor generation is admitted through
the real operator path (ER7): ``pause_session_lifecycle`` then
``resume_session_lifecycle`` -> ``queue_task_for_session``, while the old G1
worker is still inside Planning.  Only transport is stubbed (revoke broadcast
and Celery publish).  The old worker keeps its own DB session whose ORM rows
were loaded before the pause, exactly like a surviving worker process, and
then runs the real ``_finalize_planning_terminal_failure``.
"""

from __future__ import annotations

import dataclasses

import pytest

from app.models import (
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.orchestration.phases.planning_support import (
    _finalize_planning_terminal_failure,
)
from app.services.session.session_execution_service import mark_execution_running
from app.tasks.worker_support.dispatch import _claim_queued_task_for_worker
from app.tests.test_phase36_maint_la1_er7_planning_stale_restore import (
    _admit_successor,
    _CheckpointStore,
    _dispatch_fixture,
    _Transport,
)

FAILURE_REASON = "Plan validation failed after repair"


@pytest.fixture
def transport(monkeypatch):
    stub = _Transport()
    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.CheckpointService",
        _CheckpointStore,
    )
    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.revoke_session_celery_tasks",
        stub.revoke,
    )
    monkeypatch.setattr("app.tasks.worker.execute_orchestration_task", stub)
    return stub


def _g1_worker(db_session_factory, db, tmp_path, monkeypatch):
    """G1/E1 in Planning; the worker holds its own session and claimed rows."""

    ctx, _project, _root, _workspace, session, task, link, execution = (
        _dispatch_fixture(db, tmp_path, monkeypatch)
    )
    worker_db = db_session_factory()
    g1_ctx = dataclasses.replace(
        ctx,
        db=worker_db,
        session=worker_db.get(SessionModel, session.id),
        task=worker_db.get(Task, task.id),
        session_task_link=worker_db.get(SessionTask, link.id),
        claimed_session_instance_id=session.instance_id,
    )
    return g1_ctx, session.id, task.id, execution.id


def _finalize(ctx):
    # The shared tail of every Planning terminal branch.
    return _finalize_planning_terminal_failure(
        ctx=ctx,
        failure_type="planning_validation_failed_after_repair",
        failure_reason=FAILURE_REASON,
    )


def _execution_facts(execution):
    if execution is None:
        return None
    return {
        "id": execution.id,
        "status": execution.status,
        "failure_category": execution.failure_category,
        "worker_pid": execution.worker_pid,
        "worker_hostname": execution.worker_hostname,
        "heartbeat_at": execution.heartbeat_at,
        "completed_at": execution.completed_at,
    }


def _facts(db, session_id, task_id, e1_id):
    db.expire_all()
    session = db.get(SessionModel, session_id)
    task = db.get(Task, task_id)
    link = (
        db.query(SessionTask)
        .filter(SessionTask.session_id == session_id, SessionTask.task_id == task_id)
        .order_by(SessionTask.id.desc())
        .first()
    )
    e2 = (
        db.query(TaskExecution)
        .filter(TaskExecution.session_id == session_id, TaskExecution.id != e1_id)
        .order_by(TaskExecution.id.desc())
        .first()
    )
    return {
        "session": {
            "status": session.status,
            "instance_id": session.instance_id,
            "is_active": session.is_active,
            "continuation_task_id": session.continuation_task_id,
            "continuation_kind": session.continuation_kind,
        },
        "task": {
            "status": task.status,
            "error_message": task.error_message,
            "completed_at": task.completed_at,
            "workspace_status": task.workspace_status,
        },
        "session_task": {
            "id": link.id,
            "status": link.status,
            "completed_at": link.completed_at,
        },
        "e1": _execution_facts(db.get(TaskExecution, e1_id)),
        "e2": _execution_facts(e2),
    }


# ---------------------------------------------------------------------------
# ER8-R1  real pause -> resume -> admission baseline (no G1 finalizer yet)
# ---------------------------------------------------------------------------


def test_er8_r1_real_admission_establishes_successor_baseline(
    db_session, db_session_factory, tmp_path, monkeypatch, transport
):
    g1_ctx, session_id, task_id, e1_id = _g1_worker(
        db_session_factory, db_session, tmp_path, monkeypatch
    )

    paused, resumed, _session = _admit_successor(db_session, session_id, transport)
    before = _facts(db_session, session_id, task_id, e1_id)

    assert paused["status"] == "paused"
    assert resumed["status"] == "resumed"
    assert before["session"]["status"] == "running"
    assert before["session"]["instance_id"] != g1_ctx.claimed_session_instance_id
    assert before["session"]["continuation_task_id"] is None
    assert before["task"]["status"] == TaskStatus.PENDING
    assert before["task"]["error_message"] is None
    assert before["session_task"]["status"] == TaskStatus.PENDING
    assert before["e1"]["status"] == TaskStatus.CANCELLED
    assert before["e1"]["failure_category"] == "manual_stop"
    assert before["e1"]["worker_pid"] is None
    assert before["e2"]["status"] == TaskStatus.PENDING
    assert before["e2"]["worker_pid"] is None
    assert len(transport.published) == 1


# ---------------------------------------------------------------------------
# ER8-R2  surviving G1 finalizer after G2/E2 admission
# ---------------------------------------------------------------------------


def test_er8_r2_stale_finalizer_cannot_regress_admitted_successor(
    db_session, db_session_factory, tmp_path, monkeypatch, transport
):
    g1_ctx, session_id, task_id, e1_id = _g1_worker(
        db_session_factory, db_session, tmp_path, monkeypatch
    )
    _admit_successor(db_session, session_id, transport)
    before = _facts(db_session, session_id, task_id, e1_id)

    _finalize(g1_ctx)
    after = _facts(db_session, session_id, task_id, e1_id)

    assert after["session"] == before["session"]
    assert after["task"] == before["task"]
    assert after["session_task"] == before["session_task"]
    assert after["e2"] == before["e2"]
    # E1 is G1's own attempt: its terminal Planning outcome is still recorded.
    assert after["e1"]["status"] == TaskStatus.FAILED
    assert after["e1"]["worker_pid"] is None
    assert after["e1"]["completed_at"] == before["e1"]["completed_at"]

    # The admitted E2 dispatch can still claim the successor task.
    session = db_session.get(SessionModel, session_id)
    claim_ok, claim_reason, _started, _link = _claim_queued_task_for_worker(
        db=db_session,
        session=session,
        task=db_session.get(Task, task_id),
        session_task_link=None,
        expected_session_instance_id=session.instance_id,
    )
    assert (claim_ok, claim_reason) == (True, "claimed")
    g1_ctx.db.close()


# ---------------------------------------------------------------------------
# ER8-R3  current owner: no successor admitted
# ---------------------------------------------------------------------------


def test_er8_r3_current_owner_finalizer_marks_attempt_failed(
    db_session, db_session_factory, tmp_path, monkeypatch, transport
):
    g1_ctx, session_id, task_id, e1_id = _g1_worker(
        db_session_factory, db_session, tmp_path, monkeypatch
    )
    before = _facts(db_session, session_id, task_id, e1_id)

    _finalize(g1_ctx)
    after = _facts(db_session, session_id, task_id, e1_id)

    assert after["session"] == before["session"]
    assert after["session"]["instance_id"] == g1_ctx.claimed_session_instance_id
    assert after["task"]["status"] == TaskStatus.FAILED
    assert after["task"]["error_message"] == FAILURE_REASON
    assert after["task"]["completed_at"] is not None
    assert after["session_task"]["status"] == TaskStatus.FAILED
    assert after["session_task"]["completed_at"] is not None
    assert after["e1"]["status"] == TaskStatus.FAILED
    assert after["e1"]["worker_pid"] is None
    assert after["e1"]["heartbeat_at"] is None
    assert after["e2"] is None
    assert transport.revoked == []
    g1_ctx.db.close()


# ---------------------------------------------------------------------------
# ER8-R4  E2 already claimed and running when stale G1 finalizes
# ---------------------------------------------------------------------------


def test_er8_r4_stale_finalizer_cannot_regress_running_successor(
    db_session, db_session_factory, tmp_path, monkeypatch, transport
):
    g1_ctx, session_id, task_id, e1_id = _g1_worker(
        db_session_factory, db_session, tmp_path, monkeypatch
    )
    _admit_successor(db_session, session_id, transport)
    admitted = _facts(db_session, session_id, task_id, e1_id)

    # Real worker claim + running transition for the E2 dispatch.
    session = db_session.get(SessionModel, session_id)
    task = db_session.get(Task, task_id)
    claim_ok, _reason, started_at, link = _claim_queued_task_for_worker(
        db=db_session,
        session=session,
        task=task,
        session_task_link=None,
        expected_session_instance_id=session.instance_id,
    )
    assert claim_ok
    mark_execution_running(
        task=task,
        session_task_link=link,
        task_execution=db_session.get(TaskExecution, admitted["e2"]["id"]),
        started_at=started_at,
    )
    db_session.commit()
    before = _facts(db_session, session_id, task_id, e1_id)
    assert before["task"]["status"] == TaskStatus.RUNNING
    assert before["session_task"]["status"] == TaskStatus.RUNNING
    assert before["e2"]["status"] == TaskStatus.RUNNING

    _finalize(g1_ctx)
    after = _facts(db_session, session_id, task_id, e1_id)

    assert after["session"] == before["session"]
    assert after["task"] == before["task"]
    assert after["session_task"] == before["session_task"]
    assert after["e2"] == before["e2"]
    assert after["e1"]["status"] == TaskStatus.FAILED
    g1_ctx.db.close()
