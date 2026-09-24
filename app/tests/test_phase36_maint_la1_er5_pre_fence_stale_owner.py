"""Provider-free ER5 pre-fence stale-owner regressions.

Phase 36 Maintenance LA1-ER5.  A typed terminal-attempt handoff from an older
generation must not mutate a successor-owned Session before, during, or after
the ER4 expected-identity fence rejects it.

Unlike ER4 E3-R8, these cases run with a real ``OrchestrationState`` and the
real ``record_live_log`` so the coordinator's intermediate commit boundary is
exercised rather than stubbed.  The checkpoint file writer and project
snapshot writer stay stubbed: neither commits the database session.
"""

from __future__ import annotations

from datetime import timedelta

import pytest

from app.models import (
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.orchestration.coordinators.failure_coordinator import (
    FailureCoordinator,
)
from app.services.orchestration.lifecycle.terminal_handoff import (
    TerminalAttemptHandoffError,
)
from app.services.orchestration.lifecycle.transitions import (
    enter_recovering,
    resolve_continuation_identity,
    schedule_continuation,
)
from app.services.orchestration.prompt_templates import OrchestrationState
from app.services.orchestration.state.persistence import record_live_log
from app.tests.test_phase36_maint_la1_er4_terminal_ownership_transfer import (
    T0,
    _commit_attempt_evidence,
    _ctx,
    _handoff,
    _provider_free_failure_setup,
    _RetryTask,
    _seed,
)

_SESSION_FIELDS = (
    "status",
    "is_active",
    "instance_id",
    "continuation_task_id",
    "continuation_kind",
    "continuation_retry_count",
    "continuation_retry_eta",
    "paused_at",
    "last_alert_level",
    "last_alert_message",
)
_EXECUTION_FIELDS = (
    "status",
    "worker_pid",
    "worker_hostname",
    "worker_process_start_identity",
    "heartbeat_at",
)


def _real_state(tmp_path, session, task):
    state = OrchestrationState(
        session_id=str(session.id),
        task_description=task.description,
        project_name="ER5 Project",
        task_id=task.id,
    )
    project_dir = tmp_path / "er5-project-dir"
    project_dir.mkdir(parents=True, exist_ok=True)
    state._project_dir_override = str(project_dir)
    return state


def _real_log_kwargs(link):
    return {
        "get_latest_session_task_link_fn": lambda *_args, **_kwargs: link,
        "write_project_state_snapshot_fn": lambda *_args, **_kwargs: None,
        "save_orchestration_checkpoint_fn": lambda *_args, **_kwargs: None,
        "record_live_log_fn": record_live_log,
    }


def _stale_handoff_against_successor(db, tmp_path, monkeypatch):
    """G1/E1 commits Planning attempt evidence and freezes its handoff."""

    project, session, task, link, execution = _seed(db, tmp_path)
    ctx = _ctx(db, project, session, task, link, execution)
    ctx.orchestration_state = _real_state(tmp_path, session, task)
    _provider_free_failure_setup(monkeypatch)
    _commit_attempt_evidence(
        ctx,
        failure_type="discovery_output_not_json",
        failure_reason="read_only_discovery_failed_closed",
    )
    handoff = _handoff(session, execution)
    assert handoff.expected_session_instance_id == "er4-generation-1"
    return ctx, session, task, link, execution, handoff


def _snapshot(db, session_id, execution_ids):
    db.expire_all()
    session = db.query(SessionModel).filter_by(id=session_id).one()
    executions = {
        execution_id: {
            name: getattr(
                db.query(TaskExecution).filter_by(id=execution_id).one(), name
            )
            for name in _EXECUTION_FIELDS
        }
        for execution_id in execution_ids
    }
    return {name: getattr(session, name) for name in _SESSION_FIELDS}, executions


def _run_stale_handoff(ctx, link, handoff):
    with pytest.raises(TerminalAttemptHandoffError):
        FailureCoordinator().handle_failure(
            self_task=_RetryTask(),
            ctx=ctx,
            exc=handoff,
            **_real_log_kwargs(link),
        )


# ---------------------------------------------------------------------------
# ER5-R1  successor RUNNING, no successor execution yet (resume window)
# ---------------------------------------------------------------------------


def test_er5_r1_stale_handoff_cannot_pause_running_successor(
    db_session, tmp_path, monkeypatch
):
    ctx, session, task, link, execution, handoff = _stale_handoff_against_successor(
        db_session, tmp_path, monkeypatch
    )

    # resume_session_lifecycle rotates the generation and commits ``running``
    # before its dispatch creates the successor TaskExecution.
    session.instance_id = "er5-generation-2"
    session.status = "running"
    session.is_active = True
    db_session.commit()
    before, _ = _snapshot(db_session, session.id, [])

    _run_stale_handoff(ctx, link, handoff)

    after, _ = _snapshot(db_session, session.id, [])
    assert after == before
    old_execution = db_session.query(TaskExecution).filter_by(id=execution.id).one()
    assert old_execution.status == TaskStatus.FAILED
    assert old_execution.worker_pid is None


# ---------------------------------------------------------------------------
# ER5-R2  successor RETRY_PENDING with a durable continuation
# ---------------------------------------------------------------------------


def test_er5_r2_stale_handoff_cannot_hide_successor_retry_pending(
    db_session, tmp_path, monkeypatch
):
    ctx, session, task, link, execution, handoff = _stale_handoff_against_successor(
        db_session, tmp_path, monkeypatch
    )

    session.instance_id = "er5-generation-2"
    session.status = "running"
    session.is_active = True
    successor = TaskExecution(
        session=session,
        task=task,
        attempt_number=2,
        status=TaskStatus.RUNNING,
        worker_pid=525252,
        worker_hostname="successor-host",
        worker_process_start_identity="successor-process-start",
        heartbeat_at=T0 + timedelta(seconds=30),
    )
    db_session.add(successor)
    db_session.commit()
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
    before, executions_before = _snapshot(
        db_session, session.id, [identity_before.task_execution_id]
    )
    assert before["status"] == "retry_pending"
    task_before = db_session.query(Task).filter_by(id=task.id).one().status
    link_before = db_session.query(SessionTask).filter_by(id=link.id).one().status

    _run_stale_handoff(ctx, link, handoff)

    after, executions_after = _snapshot(
        db_session, session.id, [identity_before.task_execution_id]
    )
    assert after == before
    assert executions_after == executions_before
    assert db_session.query(Task).filter_by(id=task.id).one().status == task_before
    assert (
        db_session.query(SessionTask).filter_by(id=link.id).one().status == link_before
    )
    session = db_session.query(SessionModel).filter_by(id=session.id).one()
    assert resolve_continuation_identity(db_session, session) == identity_before


# ---------------------------------------------------------------------------
# ER5-R3  successor RUNNING with an active successor execution
# ---------------------------------------------------------------------------


def test_er5_r3_stale_handoff_leaves_active_successor_untouched(
    db_session, tmp_path, monkeypatch
):
    ctx, session, task, link, execution, handoff = _stale_handoff_against_successor(
        db_session, tmp_path, monkeypatch
    )

    session.instance_id = "er5-generation-2"
    session.status = "running"
    session.is_active = True
    successor = TaskExecution(
        session=session,
        task=task,
        attempt_number=2,
        status=TaskStatus.RUNNING,
        worker_pid=535353,
        worker_hostname="successor-host",
        worker_process_start_identity="successor-process-start",
        heartbeat_at=T0 + timedelta(seconds=30),
    )
    db_session.add(successor)
    db_session.commit()
    before, executions_before = _snapshot(db_session, session.id, [successor.id])

    _run_stale_handoff(ctx, link, handoff)

    after, executions_after = _snapshot(db_session, session.id, [successor.id])
    assert after == before
    assert executions_after == executions_before
    old_execution = db_session.query(TaskExecution).filter_by(id=execution.id).one()
    assert old_execution.status == TaskStatus.FAILED
    assert old_execution.worker_pid is None


# ---------------------------------------------------------------------------
# ER5-R4  current owner still terminalizes through the fence
# ---------------------------------------------------------------------------


def test_er5_r4_current_owner_handoff_still_terminalizes_with_alert(
    db_session, tmp_path, monkeypatch
):
    ctx, session, task, link, execution, handoff = _stale_handoff_against_successor(
        db_session, tmp_path, monkeypatch
    )

    _run_stale_handoff(ctx, link, handoff)

    db_session.expire_all()
    session = db_session.query(SessionModel).filter_by(id=session.id).one()
    old_execution = db_session.query(TaskExecution).filter_by(id=execution.id).one()
    assert session.status == "failed"
    assert session.is_active is False
    assert session.instance_id != "er4-generation-1"
    assert session.continuation_task_id is None
    assert session.last_alert_level == "error"
    assert "discovery_output_not_json" in session.last_alert_message
    assert db_session.query(Task).filter_by(id=task.id).one().status == (
        TaskStatus.FAILED
    )
    assert old_execution.status == TaskStatus.FAILED
    assert old_execution.worker_pid is None
