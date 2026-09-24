"""Provider-free ER6 stale-owner workspace restore regressions.

Phase 36 Maintenance LA1-ER6.  A typed terminal-attempt handoff from an older
generation must not restore its pre-run snapshot over a task workspace a
successor generation has since changed, and a failed restore must not pause or
revoke the successor.

The workspace is a real task subfolder under ``tmp_path`` (the non-canonical
mode, where no dispatch-wide project mutation lock is held).  Snapshot capture,
``_restore_workspace_snapshot_if_needed``, the project mutation lock and
``record_live_log`` are all real; assertions are on SHA-256 file manifests.
"""

from __future__ import annotations

import hashlib
from datetime import timedelta
from pathlib import Path

from app.models import Session as SessionModel, TaskExecution, TaskStatus
from app.services.orchestration.execution.runtime import (
    snapshot_workspace_before_run,
    workspace_snapshot_key,
)
from app.services.orchestration.lifecycle.transitions import (
    enter_recovering,
    resolve_continuation_identity,
    schedule_continuation,
)
from app.services.tasks.service import TaskService
from app.services.workspace.project_mutation_lock import project_mutation_lock
from app.tasks.worker_support.workspace import _restore_workspace_snapshot_if_needed
from app.tests.test_phase36_maint_la1_er4_terminal_ownership_transfer import (
    T0,
    _commit_attempt_evidence,
    _ctx,
    _handoff,
    _provider_free_failure_setup,
    _seed,
)
from app.tests.test_phase36_maint_la1_er5_pre_fence_stale_owner import (
    _run_stale_handoff,
    _real_state,
    _snapshot,
)


def _manifest(root: Path) -> dict[str, str]:
    """Relative path -> SHA-256 for workspace content and snapshots.

    Lock files and the control-state event journal (``.agent/events``, stale
    owner provenance carried outside ER6) are excluded.
    """

    return {
        str(path.relative_to(root)): hashlib.sha256(path.read_bytes()).hexdigest()
        for path in sorted(root.rglob("*"))
        if path.is_file()
        and "locks" not in path.relative_to(root).parts
        and path.relative_to(root).parts[:2] != (".agent", "events")
    }


def _workspace_fixture(db, tmp_path, monkeypatch):
    """G1/E1 owns a task-subfolder workspace with pre-run snapshot S1."""

    project, session, task, link, execution = _seed(db, tmp_path)
    ctx = _ctx(db, project, session, task, link, execution)
    state = _real_state(tmp_path, session, task)
    task_service = TaskService(db)
    project_root = task_service.get_project_root(project)
    workspace = project_root / task.task_subfolder
    workspace.mkdir(parents=True, exist_ok=True)
    state._project_dir_override = str(workspace)
    ctx.orchestration_state = state

    (workspace / "file.txt").write_text("generation-one")
    snapshot = snapshot_workspace_before_run(
        task_service,
        project,
        task.id,
        workspace,
        task_execution_id=execution.id,
        preserve_project_root_rules=False,
    )
    assert snapshot["files_copied"] == 1

    # Same closure shape as worker.py for a non-canonical dispatch.
    ctx.restore_workspace_snapshot_if_needed = (
        lambda reason, force_restore=False: _restore_workspace_snapshot_if_needed(
            reason,
            project=project,
            session_id=session.id,
            task_id=task.id,
            task_execution_id=execution.id,
            orchestration_state=state,
            policy_profile_name="balanced",
            runs_in_canonical_baseline=False,
            task_service=task_service,
            emit_live=lambda *_args, **_kwargs: None,
            force_restore=force_restore,
            lock_already_held=False,
        )
    )
    _provider_free_failure_setup(monkeypatch)
    return ctx, project, project_root, workspace, session, task, link, execution


def _g1_attempt_fails(ctx, workspace, session, execution):
    """G1 writes partial output, then Planning commits terminal evidence."""

    (workspace / "file.txt").write_text("generation-one-partial")
    (workspace / "g1-partial.txt").write_text("g1 partial output")
    _commit_attempt_evidence(
        ctx,
        failure_type="discovery_output_not_json",
        failure_reason="read_only_discovery_failed_closed",
    )
    return _handoff(session, execution)


def _successor_takes_over(db, project, workspace, session, task):
    """G2 is admitted, snapshots its own attempt, and changes the workspace."""

    session.instance_id = "er6-generation-2"
    session.status = "running"
    session.is_active = True
    successor = TaskExecution(
        session=session,
        task=task,
        attempt_number=2,
        status=TaskStatus.RUNNING,
        worker_pid=626262,
        worker_hostname="successor-host",
        worker_process_start_identity="successor-process-start",
        heartbeat_at=T0 + timedelta(seconds=30),
    )
    db.add(successor)
    db.commit()
    snapshot_workspace_before_run(
        TaskService(db),
        project,
        task.id,
        workspace,
        task_execution_id=successor.id,
        preserve_project_root_rules=False,
    )
    (workspace / "g1-partial.txt").unlink()
    (workspace / "file.txt").write_text("generation-two")
    (workspace / "successor-only.txt").write_text("must-survive")
    return successor


def _execution_fields(db, execution_id):
    db.expire_all()
    row = db.query(TaskExecution).filter_by(id=execution_id).one()
    return (
        row.status,
        row.worker_pid,
        row.worker_hostname,
        row.worker_process_start_identity,
        row.heartbeat_at,
    )


# ---------------------------------------------------------------------------
# ER6-R1  stale restore over a running successor's workspace
# ---------------------------------------------------------------------------


def test_er6_r1_stale_handoff_cannot_restore_over_successor_workspace(
    db_session, tmp_path, monkeypatch
):
    ctx, project, _root, workspace, session, task, link, execution = _workspace_fixture(
        db_session, tmp_path, monkeypatch
    )
    handoff = _g1_attempt_fails(ctx, workspace, session, execution)
    successor = _successor_takes_over(db_session, project, workspace, session, task)

    workspace_before = _manifest(workspace)
    lifecycle_before, _ = _snapshot(db_session, session.id, [])
    successor_before = _execution_fields(db_session, successor.id)

    _run_stale_handoff(ctx, link, handoff)

    assert _manifest(workspace) == workspace_before
    assert (workspace / "file.txt").read_text() == "generation-two"
    assert (workspace / "successor-only.txt").read_text() == "must-survive"
    assert not (workspace / "g1-partial.txt").exists()
    lifecycle_after, _ = _snapshot(db_session, session.id, [])
    assert lifecycle_after == lifecycle_before
    assert _execution_fields(db_session, successor.id) == successor_before


# ---------------------------------------------------------------------------
# ER6-R2  stale restore failure cannot pause the successor
# ---------------------------------------------------------------------------


def test_er6_r2_stale_restore_failure_cannot_pause_successor(
    db_session, tmp_path, monkeypatch
):
    ctx, project, project_root, workspace, session, task, link, execution = (
        _workspace_fixture(db_session, tmp_path, monkeypatch)
    )
    handoff = _g1_attempt_fails(ctx, workspace, session, execution)
    _successor_takes_over(db_session, project, workspace, session, task)

    workspace_before = _manifest(workspace)
    lifecycle_before, _ = _snapshot(db_session, session.id, [])

    # A successor-side canonical mutation holds the project lock, so the
    # stale restore fails through the real ProjectMutationLockError boundary.
    with project_mutation_lock(
        project_id=project.id,
        project_root=project_root,
        operation="successor_canonical_mutation",
        owner="session:successor",
    ):
        _run_stale_handoff(ctx, link, handoff)

    assert _manifest(workspace) == workspace_before
    lifecycle_after, _ = _snapshot(db_session, session.id, [])
    assert lifecycle_after == lifecycle_before


# ---------------------------------------------------------------------------
# ER6-R3  current owner restore and restore-failure policy are preserved
# ---------------------------------------------------------------------------


def test_er6_r3a_current_owner_still_restores_its_snapshot(
    db_session, tmp_path, monkeypatch
):
    ctx, _project, _root, workspace, session, _task, link, execution = (
        _workspace_fixture(db_session, tmp_path, monkeypatch)
    )
    handoff = _g1_attempt_fails(ctx, workspace, session, execution)

    _run_stale_handoff(ctx, link, handoff)

    assert (workspace / "file.txt").read_text() == "generation-one"
    assert not (workspace / "g1-partial.txt").exists()
    db_session.expire_all()
    session = db_session.query(SessionModel).filter_by(id=session.id).one()
    assert session.status == "failed"
    assert session.instance_id != "er4-generation-1"


def test_er6_r3b_current_owner_restore_failure_still_pauses(
    db_session, tmp_path, monkeypatch
):
    ctx, project, project_root, workspace, session, _task, link, execution = (
        _workspace_fixture(db_session, tmp_path, monkeypatch)
    )
    handoff = _g1_attempt_fails(ctx, workspace, session, execution)
    workspace_before = _manifest(workspace)

    with project_mutation_lock(
        project_id=project.id,
        project_root=project_root,
        operation="concurrent_canonical_mutation",
        owner="session:other",
    ):
        _run_stale_handoff(ctx, link, handoff)

    assert _manifest(workspace) == workspace_before
    db_session.expire_all()
    session = db_session.query(SessionModel).filter_by(id=session.id).one()
    assert session.status == "paused"
    assert session.instance_id == "er4-generation-1"
    assert session.last_alert_message == (
        "Workspace restore failed; operator review required"
    )


# ---------------------------------------------------------------------------
# ER6-R4  successor that changed the workspace and is now retry_pending
# ---------------------------------------------------------------------------


def test_er6_r4_stale_handoff_preserves_retry_pending_successor_workspace(
    db_session, tmp_path, monkeypatch
):
    ctx, project, _root, workspace, session, task, link, execution = _workspace_fixture(
        db_session, tmp_path, monkeypatch
    )
    handoff = _g1_attempt_fails(ctx, workspace, session, execution)
    successor = _successor_takes_over(db_session, project, workspace, session, task)
    enter_recovering(
        db_session,
        session,
        task_execution=successor,
        continuation_kind="celery_retry",
        retry_count=0,
        failure_reason="successor retryable failure",
        commit=True,
    )
    identity_before = schedule_continuation(
        db_session,
        session,
        task_execution=successor,
        continuation_kind="celery_retry",
        retry_count=1,
        retry_eta=T0 + timedelta(seconds=60),
        commit=True,
    )

    workspace_before = _manifest(workspace)
    lifecycle_before, _ = _snapshot(db_session, session.id, [])
    successor_before = _execution_fields(db_session, successor.id)
    assert (
        workspace
        / ".agent/auto-snapshots"
        / workspace_snapshot_key(task.id, successor.id)
    ).is_dir()

    _run_stale_handoff(ctx, link, handoff)

    assert _manifest(workspace) == workspace_before
    lifecycle_after, _ = _snapshot(db_session, session.id, [])
    assert lifecycle_after == lifecycle_before
    assert _execution_fields(db_session, successor.id) == successor_before
    session = db_session.query(SessionModel).filter_by(id=session.id).one()
    assert resolve_continuation_identity(db_session, session) == identity_before
