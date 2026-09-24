"""Provider-free ER7 Planning-side stale workspace restore adjudication.

Phase 36 Maintenance LA1-ER7.  The successor generation is admitted through
the real operator path -- ``pause_session_lifecycle`` then
``resume_session_lifecycle`` -- while the predecessor worker is still inside
Planning.  Only transport is stubbed: the Celery revoke broadcast (pause sends
SIGTERM asynchronously and does not wait for it) and the Celery publish.  The
Planning finalizer, restore closure, snapshot service, and admission checks are
real; assertions are on SHA-256 file manifests.
"""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import pytest

from app.models import Session as SessionModel, TaskExecution, TaskStatus
from app.services.orchestration.phases.planning_support import (
    _finalize_planning_terminal_failure,
)
from app.services.orchestration.planning.read_only_discovery import (
    fail_closed_discovery,
)
from app.services.orchestration.prompt_templates import OrchestrationStatus
from app.services.session.session_lifecycle_service import (
    pause_session_lifecycle,
    resume_session_lifecycle,
)
from app.services.tasks.service import TaskService
from app.services.workspace.checkpoint_service import CheckpointError
from app.tasks.worker_support.workspace import build_dispatch_workspace_restore
from app.tests.test_phase36_maint_la1_er6_stale_owner_workspace_restore import (
    _manifest,
    _workspace_fixture,
)


class _CheckpointStore:
    """In-memory stand-in for the on-disk checkpoint store."""

    def __init__(self, db):
        self.db = db

    def load_checkpoint(self, session_id, checkpoint_name=None):
        return {"context": {}, "orchestration_state": {}, "step_results": []}

    def save_checkpoint(self, session_id, checkpoint_name="manual", **_kwargs):
        return {"success": True}

    def load_resume_checkpoint(self, session_id, checkpoint_name=None):
        raise CheckpointError(f"No checkpoints found for session {session_id}")


class _Transport:
    def __init__(self):
        self.revoked: list[int] = []
        self.published: list[dict] = []

    def revoke(self, _db, session_id, terminate=True):
        # Pause broadcasts SIGTERM and returns without waiting for delivery.
        self.revoked.append(session_id)
        return ["g1-planning-celery-task"]

    def delay(self, **kwargs):
        self.published.append(kwargs)
        return SimpleNamespace(id=f"er7-celery-{len(self.published)}")


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


def _dispatch_fixture(db, tmp_path, monkeypatch):
    """ER6 workspace fixture wired to the worker's real dispatch closure."""

    ctx, project, root, workspace, session, task, link, execution = _workspace_fixture(
        db, tmp_path, monkeypatch
    )
    ctx.restore_workspace_snapshot_if_needed = build_dispatch_workspace_restore(
        db=db,
        session=session,
        expected_session_instance_id=session.instance_id,
        project=project,
        session_id=session.id,
        task_id=task.id,
        task_execution_id=execution.id,
        orchestration_state=ctx.orchestration_state,
        policy_profile_name="balanced",
        runs_in_canonical_baseline=False,
        task_service=TaskService(db),
        emit_live=lambda *_args, **_kwargs: None,
        lock_already_held=False,
    )
    return ctx, project, root, workspace, session, task, link, execution


def _admit_successor(db, session_id, transport):
    """Real operator pause, then real resume, while G1 is still in Planning."""

    paused = asyncio.run(pause_session_lifecycle(db, session_id))
    resumed = asyncio.run(resume_session_lifecycle(db, session_id))
    db.expire_all()
    session = db.query(SessionModel).filter_by(id=session_id).one()
    return paused, resumed, session


def _successor_writes(workspace):
    (workspace / "file.txt").write_text("generation-two")
    (workspace / "successor-only.txt").write_text("must-survive")
    partial = workspace / "g1-partial.txt"
    if partial.exists():
        partial.unlink()


def _g1_planning_writes(workspace):
    (workspace / "file.txt").write_text("generation-one-partial")
    (workspace / "g1-partial.txt").write_text("g1 partial output")


# ---------------------------------------------------------------------------
# ER7-R1  real admission while G1 still owns an active Planning attempt
# ---------------------------------------------------------------------------


def test_er7_r1_real_pause_resume_admits_successor_during_planning(
    db_session, tmp_path, monkeypatch, transport
):
    ctx, _project, _root, workspace, session, task, _link, execution = (
        _dispatch_fixture(db_session, tmp_path, monkeypatch)
    )
    _g1_planning_writes(workspace)
    g1_generation = session.instance_id
    assert execution.status == TaskStatus.RUNNING
    assert execution.worker_pid is not None

    paused, resumed, session = _admit_successor(db_session, session.id, transport)

    assert paused["status"] == "paused"
    assert resumed["status"] == "resumed"
    assert transport.revoked == [session.id]
    old = db_session.query(TaskExecution).filter_by(id=execution.id).one()
    # Pause normalized the live Planning attempt in the database only.
    assert old.status == TaskStatus.CANCELLED
    assert old.worker_pid is None
    assert session.status == "running"
    assert session.instance_id != g1_generation
    successor = (
        db_session.query(TaskExecution)
        .filter(TaskExecution.session_id == session.id)
        .filter(TaskExecution.id != execution.id)
        .one()
    )
    assert successor.status == TaskStatus.PENDING
    assert len(transport.published) == 1


# ---------------------------------------------------------------------------
# ER7-R2  still-running G1 Planning restores over the admitted successor
# ---------------------------------------------------------------------------


def test_er7_r2_stale_planning_restore_cannot_destroy_successor_workspace(
    db_session, tmp_path, monkeypatch, transport
):
    ctx, _project, _root, workspace, session, _task, _link, _execution = (
        _dispatch_fixture(db_session, tmp_path, monkeypatch)
    )
    _g1_planning_writes(workspace)
    _admit_successor(db_session, session.id, transport)
    _successor_writes(workspace)
    before = _manifest(workspace)

    # The shared tail of every destructive Planning terminal branch
    # (planning_flow.py:2189 / planning_repair_arbitration_control.py:549).
    _finalize_planning_terminal_failure(
        ctx=ctx,
        failure_type="planning_validation_failed_after_repair",
        failure_reason="Plan validation failed after repair",
    )
    result = ctx.restore_workspace_snapshot_if_needed("planning validation failure")

    assert result["reason"] == "stale_generation_restore_refused"
    assert _manifest(workspace) == before
    assert (workspace / "file.txt").read_text() == "generation-two"
    assert (workspace / "successor-only.txt").read_text() == "must-survive"


# ---------------------------------------------------------------------------
# ER7-R3  read-only discovery fail-closed path
# ---------------------------------------------------------------------------


def test_er7_r3_read_only_discovery_restore_preserves_successor_workspace(
    db_session, tmp_path, monkeypatch, transport
):
    ctx, _project, _root, workspace, session, _task, _link, _execution = (
        _dispatch_fixture(db_session, tmp_path, monkeypatch)
    )
    _g1_planning_writes(workspace)

    def _finalize_then_successor(**kwargs):
        _finalize_planning_terminal_failure(**kwargs)
        _admit_successor(db_session, session.id, transport)
        _successor_writes(workspace)
        snapshots.append(_manifest(workspace))

    snapshots: list[dict] = []
    result = fail_closed_discovery(
        ctx=ctx,
        reason="discovery_output_not_json",
        detail="read_only_discovery_failed_closed",
        aborted_status=OrchestrationStatus.ABORTED,
        emit_phase_event=lambda *_args, **_kwargs: None,
        finalize_failure=_finalize_then_successor,
    )

    assert result["terminal_failure"] is True
    assert _manifest(workspace) == snapshots[0]


# ---------------------------------------------------------------------------
# ER7-R4  post-finalizer window: E1 FAILED and released, restore still ahead
# ---------------------------------------------------------------------------


def test_er7_r4_post_finalizer_window_cannot_destroy_successor_workspace(
    db_session, tmp_path, monkeypatch, transport
):
    ctx, _project, _root, workspace, session, _task, _link, execution = (
        _dispatch_fixture(db_session, tmp_path, monkeypatch)
    )
    _g1_planning_writes(workspace)
    _finalize_planning_terminal_failure(
        ctx=ctx,
        failure_type="planning_validation_failed_after_repair",
        failure_reason="Plan validation failed after repair",
    )
    old = db_session.query(TaskExecution).filter_by(id=execution.id).one()
    assert old.status == TaskStatus.FAILED
    assert old.worker_pid is None

    _admit_successor(db_session, session.id, transport)
    _successor_writes(workspace)
    before = _manifest(workspace)

    result = ctx.restore_workspace_snapshot_if_needed("planning validation failure")

    assert result["reason"] == "stale_generation_restore_refused"
    assert _manifest(workspace) == before


# ---------------------------------------------------------------------------
# Current owner: Planning restore still rolls back its own attempt
# ---------------------------------------------------------------------------


def test_er7_current_owner_planning_restore_still_rolls_back(
    db_session, tmp_path, monkeypatch, transport
):
    ctx, _project, _root, workspace, _session, _task, _link, _execution = (
        _dispatch_fixture(db_session, tmp_path, monkeypatch)
    )
    _g1_planning_writes(workspace)
    _finalize_planning_terminal_failure(
        ctx=ctx,
        failure_type="planning_validation_failed_after_repair",
        failure_reason="Plan validation failed after repair",
    )

    ctx.restore_workspace_snapshot_if_needed("planning validation failure")

    assert (workspace / "file.txt").read_text() == "generation-one"
    assert not (workspace / "g1-partial.txt").exists()
    assert transport.revoked == []
