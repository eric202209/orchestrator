"""Provider-free BR1 tests for bounded lifecycle control and outcome correctness.

Every case proves one rule: a failed or pending attempt is attempt evidence,
while a pending continuation owns the logical execution.  No fresh admission,
direct runtime start, replan, legacy compatibility endpoint, retained-workspace
cleanup, or Product outcome metric may reinterpret that intermediate attempt
state as logical completion.
"""

from __future__ import annotations

import asyncio
from pathlib import Path
from types import SimpleNamespace

import pytest
from fastapi import HTTPException

from app.models import (
    PlanningSession,
    Project,
    Session as SessionModel,
    SessionState,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.orchestration.lifecycle.authority import (
    derive_lifecycle_authority,
)
from app.services.orchestration.lifecycle.transitions import (
    ContinuationIdentity,
    admit_fresh_logical_execution,
    autonomous_continuation_owns_generation,
    claim_continuation,
    enter_recovering,
    finalize_logical_failure,
    finalize_logical_success,
    schedule_continuation,
)
from app.services.session.replan_service import trigger_replan
from app.services.session.resume_service import ResumeSessionService
from app.services.session.session_execution_service import start_session_payload
from app.services.session.session_runtime_service import queue_task_for_session
from app.services.workspace.baseline_promotion_service import (
    BaselinePromotionService,
)

from scripts.session_and_replay.failure_taxonomy import (
    outcome_class,
    terminal_class,
)


# --------------------------------------------------------------------------
# Fixtures and lifecycle-condition builders
# --------------------------------------------------------------------------

CONTINUATION_KINDS = ("celery_retry", "automatic_recovery", "backend_capacity")


def _seed(
    db,
    tmp_path: Path,
    *,
    session_status: str = "running",
    task_status: TaskStatus = TaskStatus.RUNNING,
    execution_status: TaskStatus = TaskStatus.RUNNING,
    instance_id: str = "br1-generation-1",
    workspace_status: str = "isolated",
):
    project = Project(
        name="BR1 Project",
        workspace_path=str(tmp_path / "project-workspace"),
    )
    session = SessionModel(
        project=project,
        name="BR1 Session",
        status=session_status,
        execution_mode="manual",
        is_active=session_status in {"running", "recovering", "retry_pending"},
        instance_id=instance_id,
    )
    task = Task(
        project=project,
        title="BR1 Task",
        description="Exercise bounded lifecycle control",
        status=task_status,
        task_subfolder="task-br1",
        workspace_status=workspace_status,
    )
    link = SessionTask(session=session, task=task, status=task_status)
    execution = TaskExecution(
        session=session,
        task=task,
        attempt_number=1,
        status=execution_status,
    )
    db.add_all([project, session, task, link, execution])
    db.commit()
    for row in (session, task, link, execution):
        db.refresh(row)
    return project, session, task, link, execution


def _make_recovering(db, tmp_path, **kwargs):
    project, session, task, link, execution = _seed(db, tmp_path, **kwargs)
    enter_recovering(
        db,
        session,
        task_execution=execution,
        continuation_kind="automatic_recovery",
        retry_count=0,
        failure_reason="attempt failed",
        commit=True,
    )
    db.refresh(session)
    return project, session, task, link, execution


def _make_retry_pending(db, tmp_path, kind: str, **kwargs):
    project, session, task, link, execution = _make_recovering(db, tmp_path, **kwargs)
    identity = schedule_continuation(
        db,
        session,
        continuation_kind=kind,
        retry_count=1,
        commit=True,
    )
    db.refresh(session)
    return project, session, task, link, execution, identity


def _build(db, tmp_path, condition: str):
    """Build one row of the lifecycle control matrix."""

    if condition == "running":
        return _seed(db, tmp_path)[:5]
    if condition == "recovering":
        return _make_recovering(db, tmp_path)
    if condition.startswith("retry_pending/"):
        kind = condition.split("/", 1)[1]
        return _make_retry_pending(db, tmp_path, kind)[:5]
    if condition == "paused":
        return _seed(db, tmp_path, session_status="paused")
    if condition == "completed":
        return _seed(
            db,
            tmp_path,
            session_status="completed",
            task_status=TaskStatus.DONE,
            execution_status=TaskStatus.DONE,
        )
    if condition == "stable_failed":
        return _seed(
            db,
            tmp_path,
            session_status="failed",
            task_status=TaskStatus.FAILED,
            execution_status=TaskStatus.FAILED,
        )
    if condition == "cancelled":
        return _seed(
            db,
            tmp_path,
            session_status="cancelled",
            task_status=TaskStatus.CANCELLED,
            execution_status=TaskStatus.CANCELLED,
        )
    raise AssertionError(f"unknown lifecycle condition: {condition}")


CONTINUATION_CONDITIONS = (
    "recovering",
    "retry_pending/celery_retry",
    "retry_pending/automatic_recovery",
    "retry_pending/backend_capacity",
)
STABLE_CONDITIONS = ("paused", "completed", "stable_failed", "cancelled")
ALL_CONDITIONS = ("running",) + CONTINUATION_CONDITIONS + STABLE_CONDITIONS


class _Broker:
    """Deterministic stand-in for the Celery publication boundary."""

    def __init__(self):
        self.calls: list[dict] = []

    def delay(self, **kwargs):
        self.calls.append(kwargs)
        return SimpleNamespace(id=f"br1-celery-{len(self.calls)}")


@pytest.fixture
def no_replayable_checkpoint(monkeypatch):
    """Pin the legacy-resume cases to the fresh-requeue dispatch path.

    Checkpoints are keyed by session id on disk, so residue from an unrelated
    test in the same workspace root must not decide which dispatch mode these
    lifecycle assertions exercise.
    """

    from app.services.workspace.checkpoint_service import (
        CheckpointError,
        CheckpointService,
    )

    def _none(self, session_id, checkpoint_name=None):
        raise CheckpointError(f"No checkpoints found for session {session_id}")

    monkeypatch.setattr(CheckpointService, "load_resume_checkpoint", _none)


@pytest.fixture
def broker(monkeypatch):
    published = _Broker()
    monkeypatch.setattr("app.tasks.worker.execute_orchestration_task", published)
    return published


# --------------------------------------------------------------------------
# Shared canonical predicate
# --------------------------------------------------------------------------


@pytest.mark.parametrize("condition", CONTINUATION_CONDITIONS)
def test_br1_shared_predicate_sees_continuation_ownership(
    db_session, tmp_path, condition
):
    _project, session, *_rest = _build(db_session, tmp_path, condition)
    assert autonomous_continuation_owns_generation(session) is True
    authority = derive_lifecycle_authority(db_session, session)
    assert authority.logical_terminal is False


@pytest.mark.parametrize("condition", ("running",) + STABLE_CONDITIONS)
def test_br1_shared_predicate_ignores_non_continuation_states(
    db_session, tmp_path, condition
):
    _project, session, *_rest = _build(db_session, tmp_path, condition)
    assert autonomous_continuation_owns_generation(session) is False


# --------------------------------------------------------------------------
# Bug A — fresh-work admission
# --------------------------------------------------------------------------


@pytest.mark.parametrize("condition", CONTINUATION_CONDITIONS)
def test_br1_fresh_task_admission_rejected_during_continuation(
    db_session, tmp_path, broker, condition
):
    _project, session, task, _link, _execution = _build(db_session, tmp_path, condition)
    before_instance = session.instance_id
    before_kind = session.continuation_kind
    executions_before = db_session.query(TaskExecution).count()

    with pytest.raises(HTTPException) as excinfo:
        queue_task_for_session(db=db_session, session=session, task_id=task.id)

    assert excinfo.value.status_code == 409
    assert "autonomous continuation" in excinfo.value.detail
    assert broker.calls == []
    db_session.refresh(session)
    assert session.instance_id == before_instance
    assert session.continuation_kind == before_kind
    assert db_session.query(TaskExecution).count() == executions_before


def test_br1_fresh_task_admission_proceeds_for_pending_session(
    db_session, tmp_path, broker
):
    _project, session, task, _link, execution = _seed(
        db_session,
        tmp_path,
        session_status="pending",
        task_status=TaskStatus.PENDING,
        execution_status=TaskStatus.DONE,
    )

    queued = queue_task_for_session(db=db_session, session=session, task_id=task.id)

    assert queued["task_execution_id"] is not None
    assert len(broker.calls) == 1
    db_session.refresh(session)
    assert session.status == "running"
    assert execution is not None


def test_br1_cross_session_active_link_covers_continuation_phases(
    db_session, tmp_path, broker
):
    """A task running under a recovering Session blocks a second Session."""

    project, owner, task, link, execution = _make_recovering(db_session, tmp_path)
    link.status = TaskStatus.RUNNING
    competitor = SessionModel(
        project_id=project.id,
        name="BR1 competing session",
        status="pending",
        instance_id="br1-generation-competitor",
    )
    db_session.add(competitor)
    db_session.commit()
    task.status = TaskStatus.PENDING
    db_session.commit()

    with pytest.raises(HTTPException) as excinfo:
        queue_task_for_session(db=db_session, session=competitor, task_id=task.id)

    assert excinfo.value.status_code == 409
    assert str(owner.id) in excinfo.value.detail
    assert broker.calls == []
    assert execution is not None


def test_br1_two_competing_fresh_admissions_cannot_both_win(
    db_session, db_session_factory, tmp_path
):
    project = Project(name="BR1 race", workspace_path=str(tmp_path / "race"))
    db_session.add(project)
    db_session.flush()
    session = SessionModel(
        project_id=project.id,
        name="BR1 race session",
        status="pending",
        instance_id="br1-race-generation",
    )
    db_session.add(session)
    db_session.commit()

    first_db = db_session_factory()
    second_db = db_session_factory()
    try:
        # Both callers observe the same generation before either commits.
        first_session = first_db.get(SessionModel, session.id)
        second_session = second_db.get(SessionModel, session.id)
        first = admit_fresh_logical_execution(first_db, first_session)
        first_db.commit()
        second = admit_fresh_logical_execution(second_db, second_session)

        assert [first.accepted, second.accepted].count(True) == 1
        assert second.reason == "fresh_admission_race_lost"
    finally:
        first_db.close()
        second_db.close()


@pytest.mark.parametrize("condition", CONTINUATION_CONDITIONS)
def test_br1_fresh_admission_primitive_rejects_continuation(
    db_session, tmp_path, condition
):
    _project, session, *_rest = _build(db_session, tmp_path, condition)
    result = admit_fresh_logical_execution(db_session, session)
    assert result.accepted is False
    assert result.reason == "autonomous_continuation_owns_generation"


# --------------------------------------------------------------------------
# Bug B — direct runtime start
# --------------------------------------------------------------------------


@pytest.mark.parametrize("condition", CONTINUATION_CONDITIONS)
def test_br1_direct_runtime_start_rejected_during_continuation(
    db_session, tmp_path, monkeypatch, condition
):
    _project, session, *_rest = _build(db_session, tmp_path, condition)

    def _explode(*_args, **_kwargs):
        raise AssertionError("runtime must not be created during a continuation")

    monkeypatch.setattr(
        "app.services.session.session_execution_service.create_agent_runtime",
        _explode,
    )

    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            start_session_payload(db_session, session.id, task_description="direct")
        )

    assert excinfo.value.status_code == 409
    assert "autonomous continuation" in excinfo.value.detail
    db_session.refresh(session)
    assert session.status == (
        "recovering" if condition == "recovering" else "retry_pending"
    )


def test_br1_direct_runtime_start_still_rejects_running_session(db_session, tmp_path):
    _project, session, *_rest = _seed(db_session, tmp_path)
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(
            start_session_payload(db_session, session.id, task_description="direct")
        )
    assert excinfo.value.status_code == 409
    assert "active execution in progress" in excinfo.value.detail


def test_br1_direct_runtime_start_allowed_for_pending_session(
    db_session, tmp_path, monkeypatch
):
    _project, session, *_rest = _seed(
        db_session,
        tmp_path,
        session_status="pending",
        task_status=TaskStatus.PENDING,
        execution_status=TaskStatus.PENDING,
    )

    class _Runtime:
        async def create_session(self, description):
            return f"session-key::{description}"

    monkeypatch.setattr(
        "app.services.session.session_execution_service.create_agent_runtime",
        lambda *_a, **_k: _Runtime(),
    )

    payload = asyncio.run(
        start_session_payload(db_session, session.id, task_description="direct")
    )
    assert payload["status"] == "started"


# --------------------------------------------------------------------------
# Bug C — operator replan
# --------------------------------------------------------------------------


@pytest.mark.parametrize("condition", CONTINUATION_CONDITIONS)
def test_br1_replan_rejected_during_continuation(
    db_session, tmp_path, monkeypatch, condition
):
    _project, session, _task, _link, _execution = _build(
        db_session, tmp_path, condition
    )
    before = (
        session.instance_id,
        session.continuation_kind,
        session.continuation_task_id,
        session.continuation_retry_count,
    )

    def _explode(*_args, **_kwargs):
        raise AssertionError("Planning must not start during a continuation")

    monkeypatch.setattr(
        "app.services.planning.planning_session_service.PlanningSessionService",
        _explode,
    )

    with pytest.raises(HTTPException) as excinfo:
        trigger_replan(db_session, session.id)

    assert excinfo.value.status_code == 409
    assert db_session.query(PlanningSession).count() == 0
    db_session.refresh(session)
    assert (
        session.instance_id,
        session.continuation_kind,
        session.continuation_task_id,
        session.continuation_retry_count,
    ) == before
    authority = derive_lifecycle_authority(db_session, session)
    assert authority.continuation_pending is True
    assert authority.logical_terminal is False


def test_br1_replan_still_available_after_stable_failure(
    db_session, tmp_path, monkeypatch
):
    _project, session, *_rest = _build(db_session, tmp_path, "stable_failed")
    started: list[str] = []

    class _PlanningService:
        def __init__(self, _db):
            pass

        def start_session(self, _project, prompt, **_kwargs):
            started.append(prompt)
            return SimpleNamespace(id=4321)

    monkeypatch.setattr(
        "app.services.planning.planning_session_service.PlanningSessionService",
        _PlanningService,
    )

    result = trigger_replan(db_session, session.id)
    assert result["planning_session_id"] == 4321
    assert len(started) == 1


# --------------------------------------------------------------------------
# Bug D — legacy resume / retry-step compatibility endpoints
# --------------------------------------------------------------------------


def _with_resume_state(db, session, *, current_step=1, total_steps=3):
    db.add(
        SessionState(
            session_id=session.id,
            project_id=session.project_id,
            current_step=current_step,
            total_steps=total_steps,
            plan='[{"step": 1}, {"step": 2}, {"step": 3}]',
            execution_results="[]",
            debug_attempts="[]",
            changed_files="[]",
        )
    )
    db.commit()


def test_br1_legacy_resume_uses_accepted_generation_and_dispatch(
    db_session, tmp_path, broker, no_replayable_checkpoint
):
    _project, session, task, link, _execution = _seed(
        db_session,
        tmp_path,
        session_status="paused",
        task_status=TaskStatus.PENDING,
        execution_status=TaskStatus.FAILED,
    )
    link.status = TaskStatus.PENDING
    db_session.commit()
    _with_resume_state(db_session, session)
    before_instance = session.instance_id

    service = ResumeSessionService(db_session, session.id)
    result = asyncio.run(service.resume_session())

    assert result["success"] is True
    assert result["status"] == "running"
    db_session.refresh(session)
    assert session.status == "running"
    # The accepted generation was rotated and real work was dispatched.
    assert session.instance_id != before_instance
    assert len(broker.calls) == 1
    assert broker.calls[0]["expected_session_instance_id"] == session.instance_id
    assert broker.calls[0]["task_id"] == task.id


def test_br1_legacy_resume_rejects_stale_prior_generation_delivery(
    db_session, tmp_path, broker, no_replayable_checkpoint
):
    _project, session, task, link, execution = _make_retry_pending(
        db_session, tmp_path, "celery_retry"
    )[:5]
    stale = ContinuationIdentity(
        session_id=session.id,
        instance_id=session.instance_id,
        continuation_task_id=task.id,
        continuation_kind="celery_retry",
        task_execution_id=session.continuation_task_id and execution.id,
        retry_count=1,
    )

    # Operator revokes the continuation and resumes through the legacy path.
    from app.services.orchestration.lifecycle.transitions import (
        revoke_autonomous_continuation,
    )

    revoke_autonomous_continuation(
        db_session,
        session,
        resulting_status="paused",
        reason="operator pause",
        commit=True,
    )
    db_session.refresh(session)
    link.status = TaskStatus.PENDING
    task.status = TaskStatus.PENDING
    db_session.commit()
    _with_resume_state(db_session, session)

    service = ResumeSessionService(db_session, session.id)
    asyncio.run(service.resume_session())
    db_session.refresh(session)

    rejected = claim_continuation(db_session, stale)
    assert rejected.accepted is False
    assert session.instance_id != stale.instance_id


@pytest.mark.parametrize("condition", CONTINUATION_CONDITIONS)
def test_br1_legacy_resume_cannot_bypass_continuation_ownership(
    db_session, tmp_path, broker, condition
):
    _project, session, *_rest = _build(db_session, tmp_path, condition)
    _with_resume_state(db_session, session)
    before_status = session.status
    before_instance = session.instance_id

    service = ResumeSessionService(db_session, session.id)
    with pytest.raises(HTTPException) as excinfo:
        asyncio.run(service.resume_session())

    assert excinfo.value.status_code == 409
    assert broker.calls == []
    db_session.refresh(session)
    assert session.status == before_status
    assert session.instance_id == before_instance


@pytest.mark.parametrize("condition", CONTINUATION_CONDITIONS)
def test_br1_legacy_retry_step_cannot_bypass_continuation_ownership(
    db_session, tmp_path, condition
):
    _project, session, *_rest = _build(db_session, tmp_path, condition)
    _with_resume_state(db_session, session)
    before_status = session.status

    service = ResumeSessionService(db_session, session.id)
    with pytest.raises(HTTPException) as excinfo:
        service.retry_failed_step(1)

    assert excinfo.value.status_code == 409
    db_session.refresh(session)
    assert session.status == before_status


def test_br1_legacy_retry_step_no_longer_manufactures_running(db_session, tmp_path):
    _project, session, *_rest = _seed(
        db_session,
        tmp_path,
        session_status="paused",
        task_status=TaskStatus.FAILED,
        execution_status=TaskStatus.FAILED,
    )
    _with_resume_state(db_session, session)

    service = ResumeSessionService(db_session, session.id)
    result = service.retry_failed_step(1)

    assert result["success"] is True
    assert result["retry_step"] == 1
    db_session.refresh(session)
    # No accepted dispatch happened, so no running status may be manufactured.
    assert session.status == "paused"


# --------------------------------------------------------------------------
# Bug E — retained-workspace cleanup
# --------------------------------------------------------------------------


def _cleanup(db, project, task, tmp_path, *, workspace_status="blocked"):
    project_root = Path(project.workspace_path)
    (project_root / task.task_subfolder).mkdir(parents=True, exist_ok=True)
    task.workspace_status = workspace_status
    db.commit()
    return BaselinePromotionService(db).cleanup_retained_task_workspaces(
        project,
        dry_run=True,
        include_blocked=True,
        include_ready=True,
        include_changes_requested=True,
    )


def _reasons(report, task_id):
    return {row["reason"] for row in report["skipped"] if row["task_id"] == task_id}


def test_br1_cleanup_retains_workspace_for_failed_attempt_under_recovery(
    db_session, tmp_path
):
    project, _session, task, _link, _execution = _make_recovering(
        db_session,
        tmp_path,
        task_status=TaskStatus.FAILED,
        execution_status=TaskStatus.FAILED,
    )
    report = _cleanup(db_session, project, task, tmp_path)
    assert report["candidate_count"] == 0
    assert "session_continuation_live" in _reasons(report, task.id)


@pytest.mark.parametrize("kind", CONTINUATION_KINDS)
def test_br1_cleanup_retains_workspace_for_pending_attempt_under_retry(
    db_session, tmp_path, kind
):
    project, _session, task, _link, _execution, _identity = _make_retry_pending(
        db_session,
        tmp_path,
        kind,
        task_status=TaskStatus.PENDING,
        execution_status=TaskStatus.PENDING,
    )
    report = _cleanup(db_session, project, task, tmp_path)
    assert report["candidate_count"] == 0
    assert "session_continuation_live" in _reasons(report, task.id)


def test_br1_cleanup_still_eligible_after_stable_terminal_outcome(db_session, tmp_path):
    project, session, task, _link, _execution = _build(
        db_session, tmp_path, "stable_failed"
    )
    report = _cleanup(db_session, project, task, tmp_path)
    assert [row["task_id"] for row in report["candidates"]] == [task.id]
    assert autonomous_continuation_owns_generation(session) is False


def test_br1_cleanup_preserves_behavior_for_unrelated_workspace(db_session, tmp_path):
    project, _session, _task, _link, _execution = _make_recovering(
        db_session,
        tmp_path,
        task_status=TaskStatus.FAILED,
        execution_status=TaskStatus.FAILED,
    )
    unrelated = Task(
        project=project,
        title="Unrelated",
        description="not linked to any session",
        status=TaskStatus.FAILED,
        task_subfolder="task-unrelated",
        workspace_status="blocked",
    )
    db_session.add(unrelated)
    db_session.commit()

    report = _cleanup(db_session, project, unrelated, tmp_path)
    assert [row["task_id"] for row in report["candidates"]] == [unrelated.id]


# --------------------------------------------------------------------------
# Bug F — admin outcome / failure analytics
# --------------------------------------------------------------------------


def _session_row(session, **overrides):
    row = {
        "id": session.id,
        "status": session.status,
        "is_active": session.is_active,
        "started_at": None,
        "continuation_task_id": session.continuation_task_id,
        "continuation_kind": session.continuation_kind,
        "continuation_retry_count": session.continuation_retry_count,
        "continuation_retry_eta": session.continuation_retry_eta,
    }
    row.update(overrides)
    return row


FAILED_ATTEMPT_ROWS = [{"id": 1, "task_id": 1, "attempt_number": 1, "status": "failed"}]


@pytest.mark.parametrize("condition", CONTINUATION_CONDITIONS)
def test_br1_failed_attempt_is_not_a_final_failed_outcome(
    db_session, tmp_path, condition
):
    _project, session, *_rest = _build(db_session, tmp_path, condition)
    row = _session_row(session)

    assert outcome_class(row, FAILED_ATTEMPT_ROWS, []) == "in_progress"
    assert (
        terminal_class(
            session=row, task_executions=FAILED_ATTEMPT_ROWS, metadata_rows=[]
        )
        != "task_execution_failed"
    )


def test_br1_final_failure_still_counts_as_a_failed_outcome(db_session, tmp_path):
    _project, session, _task, _link, execution = _seed(
        db_session,
        tmp_path,
        task_status=TaskStatus.FAILED,
        execution_status=TaskStatus.FAILED,
    )
    finalize_logical_failure(
        db_session,
        session,
        task_execution=execution,
        failure_reason="max_attempts_reached",
        commit=True,
    )
    db_session.refresh(session)
    authority = derive_lifecycle_authority(db_session, session)
    assert authority.logical_terminal is True

    row = _session_row(session)
    metadata_rows = [{"log_metadata": '{"reason": "max_attempts_reached"}'}]
    assert outcome_class(row, FAILED_ATTEMPT_ROWS, metadata_rows) == (
        "failed_but_actionable"
    )
    assert (
        terminal_class(
            session=row,
            task_executions=FAILED_ATTEMPT_ROWS,
            metadata_rows=metadata_rows,
        )
        == "max_attempts_reached"
    )


def test_br1_completed_session_outcome_is_unchanged(db_session, tmp_path):
    _project, session, _task, _link, execution = _seed(
        db_session,
        tmp_path,
        task_status=TaskStatus.DONE,
        execution_status=TaskStatus.DONE,
    )
    finalize_logical_success(db_session, session, task_execution=execution, commit=True)
    db_session.refresh(session)

    row = _session_row(session)
    done_rows = [{"id": 1, "task_id": 1, "attempt_number": 1, "status": "done"}]
    assert outcome_class(row, done_rows, []) == "first_pass_success"
    assert terminal_class(session=row, task_executions=done_rows, metadata_rows=[]) == (
        "DONE"
    )


# --------------------------------------------------------------------------
# Lifecycle control matrix
# --------------------------------------------------------------------------


@pytest.mark.parametrize("condition", ALL_CONDITIONS)
def test_br1_lifecycle_control_matrix_final_outcome_column(
    db_session, tmp_path, condition
):
    _project, session, *_rest = _build(db_session, tmp_path, condition)
    authority = derive_lifecycle_authority(db_session, session)
    expected_terminal = condition in ("completed", "stable_failed", "cancelled")
    assert authority.logical_terminal is expected_terminal
    assert authority.is_terminal is authority.logical_terminal

    row = _session_row(session)
    classified = outcome_class(row, FAILED_ATTEMPT_ROWS, [])
    if not expected_terminal:
        assert classified == "in_progress"


@pytest.mark.parametrize("condition", ALL_CONDITIONS)
def test_br1_lifecycle_control_matrix_admission_column(db_session, tmp_path, condition):
    _project, session, *_rest = _build(db_session, tmp_path, condition)
    result = admit_fresh_logical_execution(db_session, session)
    if condition in CONTINUATION_CONDITIONS:
        assert result.accepted is False
        assert result.reason == "autonomous_continuation_owns_generation"
    else:
        # Every other cell keeps its existing per-path Product policy.
        assert result.accepted is True


# --------------------------------------------------------------------------
# Primary RER-02A cross-subsystem regression
# --------------------------------------------------------------------------


def test_br1_rer02a_cross_subsystem_continuation_protection(
    db_session, tmp_path, broker, monkeypatch
):
    project, session, task, link, execution = _seed(
        db_session,
        tmp_path,
        task_status=TaskStatus.FAILED,
        execution_status=TaskStatus.FAILED,
    )
    (Path(project.workspace_path) / task.task_subfolder).mkdir(parents=True)
    task.workspace_status = "blocked"
    db_session.commit()

    monkeypatch.setattr(
        "app.services.planning.planning_session_service.PlanningSessionService",
        lambda *_a, **_k: pytest.fail("Planning must not start"),
    )

    def _assert_protected(expected_status: str):
        db_session.refresh(session)
        authority = derive_lifecycle_authority(db_session, session, task_id=task.id)
        assert session.status == expected_status
        assert authority.continuation_pending is True
        assert authority.logical_terminal is False

        with pytest.raises(HTTPException) as fresh:
            queue_task_for_session(db=db_session, session=session, task_id=task.id)
        assert fresh.value.status_code == 409

        with pytest.raises(HTTPException) as direct:
            asyncio.run(
                start_session_payload(db_session, session.id, task_description="x")
            )
        assert direct.value.status_code == 409

        with pytest.raises(HTTPException) as replan:
            trigger_replan(db_session, session.id)
        assert replan.value.status_code == 409
        assert db_session.query(PlanningSession).count() == 0

        report = BaselinePromotionService(db_session).cleanup_retained_task_workspaces(
            project, dry_run=True, include_blocked=True
        )
        assert report["candidate_count"] == 0
        assert "session_continuation_live" in _reasons(report, task.id)

        row = _session_row(session)
        assert outcome_class(row, FAILED_ATTEMPT_ROWS, []) == "in_progress"

    # attempt fails -> Session recovering
    enter_recovering(
        db_session,
        session,
        task_execution=execution,
        continuation_kind="automatic_recovery",
        retry_count=0,
        failure_reason="attempt failed",
        commit=True,
    )
    _assert_protected("recovering")

    # -> schedule retry_pending; the same protections remain
    identity = schedule_continuation(
        db_session,
        session,
        continuation_kind="automatic_recovery",
        retry_count=1,
        commit=True,
    )
    _assert_protected("retry_pending")

    # -> strict claim -> running, with no duplicate logical execution
    claimed = claim_continuation(db_session, identity, commit=True)
    assert claimed.accepted is True
    db_session.refresh(session)
    assert session.status == "running"
    assert (
        db_session.query(TaskExecution)
        .filter(
            TaskExecution.session_id == session.id,
            TaskExecution.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]),
        )
        .count()
        == 1
    )
    assert broker.calls == []
    assert link is not None
