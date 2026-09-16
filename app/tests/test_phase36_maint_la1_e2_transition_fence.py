"""Focused E2 tests for lifecycle transitions and Session generation fencing."""

from __future__ import annotations

from app.models import (
    Base,
    Project,
    Session as SessionModel,
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
    admit_autonomous_execution,
    begin_new_generation,
    claim_continuation,
    enter_recovering,
    finalize_logical_failure,
    finalize_logical_success,
    revoke_autonomous_continuation,
    schedule_continuation,
)


def _graph(db, *, status: str = "running", instance_id: str = "generation-a"):
    project = Project(name=f"e2-project-{id(db)}", workspace_path="/tmp/e2")
    db.add(project)
    db.flush()
    session = SessionModel(
        project_id=project.id,
        name=f"e2-session-{id(db)}",
        status=status,
        is_active=status in {"running", "recovering", "retry_pending"},
        instance_id=instance_id,
    )
    task = Task(
        project_id=project.id,
        title="E2 task",
        description="transition test",
        status=TaskStatus.RUNNING,
    )
    db.add_all([session, task])
    db.flush()
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
    )
    link = SessionTask(
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.RUNNING,
    )
    db.add_all([execution, link])
    db.commit()
    db.refresh(session)
    db.refresh(task)
    db.refresh(execution)
    return session, task, execution


def _authority(db, session):
    db.refresh(session)
    return derive_lifecycle_authority(db, session)


def _retry_pending(db):
    session, task, execution = _graph(db)
    enter_recovering(
        db,
        session,
        task_execution=execution,
        continuation_kind="automatic_recovery",
        retry_count=0,
    )
    db.flush()
    identity = schedule_continuation(
        db,
        session,
        continuation_kind="automatic_recovery",
        retry_count=1,
    )
    db.commit()
    db.refresh(session)
    db.refresh(task)
    db.refresh(execution)
    pending_execution = db.get(TaskExecution, identity.task_execution_id)
    return session, task, execution, pending_execution, identity


def test_single_autonomous_admission_blocks_distinct_task(db_session):
    project = Project(name="admission-project", workspace_path="/tmp/e2-admission")
    db_session.add(project)
    db_session.flush()
    session = SessionModel(
        project_id=project.id,
        name="admission-session",
        status="pending",
        instance_id="admission-generation",
    )
    first = Task(project_id=project.id, title="first", status=TaskStatus.PENDING)
    second = Task(project_id=project.id, title="second", status=TaskStatus.PENDING)
    db_session.add_all([session, first, second])
    db_session.commit()
    db_session.refresh(session)

    winner = admit_autonomous_execution(db_session, session, task=first)
    db_session.commit()
    loser = admit_autonomous_execution(db_session, session, task=second)

    assert winner.accepted is True
    assert loser.accepted is False
    assert loser.reason == "autonomous_execution_already_active"
    assert db_session.query(TaskExecution).filter_by(session_id=session.id).count() == 1


def test_same_admission_claim_is_idempotent_when_execution_identity_matches(db_session):
    project = Project(name="idempotent-project", workspace_path="/tmp/e2-idempotent")
    db_session.add(project)
    db_session.flush()
    session = SessionModel(
        project_id=project.id,
        name="idempotent-session",
        status="pending",
        instance_id="idempotent-generation",
    )
    task = Task(project_id=project.id, title="same", status=TaskStatus.PENDING)
    db_session.add_all([session, task])
    db_session.commit()
    db_session.refresh(session)
    db_session.refresh(task)

    first = admit_autonomous_execution(db_session, session, task=task)
    db_session.commit()
    second = admit_autonomous_execution(
        db_session,
        session,
        task=task,
        expected_task_execution_id=first.task_execution_id,
    )

    assert first.accepted is True
    assert second.accepted is True
    assert second.reason == "already_admitted"


def test_concurrent_admission_boundary_is_conditional_and_single_winner(
    db_session, db_session_factory
):
    """SQLite cannot demonstrate row-lock blocking; this proves its atomic gate.

    Production PostgreSQL uses the same conditional UPDATE under the row lock.
    The second transaction is evaluated after the first committed reservation
    and deterministically loses without a timing sleep.
    """

    project = Project(name="race-project", workspace_path="/tmp/e2-race")
    db_session.add(project)
    db_session.flush()
    session = SessionModel(
        project_id=project.id,
        name="race-session",
        status="pending",
        instance_id="race-generation",
    )
    first = Task(project_id=project.id, title="first", status=TaskStatus.PENDING)
    second = Task(project_id=project.id, title="second", status=TaskStatus.PENDING)
    db_session.add_all([session, first, second])
    db_session.commit()
    db_session.refresh(session)

    first_db = db_session_factory()
    second_db = db_session_factory()
    try:
        first_session = first_db.get(SessionModel, session.id)
        first_task = first_db.get(Task, first.id)
        second_session = second_db.get(SessionModel, session.id)
        second_task = second_db.get(Task, second.id)
        first_result = admit_autonomous_execution(
            first_db, first_session, task=first_task
        )
        first_db.commit()
        second_result = admit_autonomous_execution(
            second_db, second_session, task=second_task
        )

        assert [first_result.accepted, second_result.accepted].count(True) == 1
        assert second_result.reason == "autonomous_admission_race_lost"
    finally:
        first_db.close()
        second_db.close()


def test_recovering_marks_attempt_failed_but_not_logically_terminal(db_session):
    session, task, execution = _graph(db_session)

    identity = enter_recovering(
        db_session,
        session,
        task_execution=execution,
        continuation_kind="automatic_recovery",
        retry_count=0,
    )
    db_session.flush()
    authority = _authority(db_session, session)

    assert execution.status == TaskStatus.FAILED
    assert identity.instance_id == "generation-a"
    assert authority.attempt_status == "failed"
    assert authority.continuation_pending is True
    assert authority.logical_terminal is False
    assert authority.quiescent is False


def test_retry_pending_persists_marker_and_preserves_generation(db_session):
    session, _, _, pending_execution, identity = _retry_pending(db_session)
    authority = _authority(db_session, session)

    assert session.status == "retry_pending"
    assert session.instance_id == "generation-a"
    assert pending_execution.status == TaskStatus.PENDING
    assert session.continuation_task_id == identity.continuation_task_id
    assert session.continuation_kind == "automatic_recovery"
    assert session.continuation_retry_count == 1
    assert authority.continuation_pending is True
    assert authority.logical_terminal is False
    assert authority.quiescent is False


def test_valid_claim_sets_running_and_preserves_generation(db_session):
    session, _, _, pending_execution, identity = _retry_pending(db_session)

    result = claim_continuation(db_session, identity)
    db_session.commit()
    authority = _authority(db_session, session)

    assert result.accepted is True
    assert session.status == "running"
    assert session.instance_id == "generation-a"
    assert pending_execution.status == TaskStatus.RUNNING
    assert session.continuation_task_id is None
    assert authority.continuation_pending is False
    assert authority.logical_terminal is False
    assert authority.quiescent is False


def test_duplicate_claim_is_refused_without_mutation(db_session):
    session, _, _, pending_execution, identity = _retry_pending(db_session)
    assert claim_continuation(db_session, identity).accepted is True
    db_session.commit()
    before = (session.status, session.instance_id, pending_execution.status)

    duplicate = claim_continuation(db_session, identity)
    db_session.commit()

    assert duplicate.accepted is False
    assert duplicate.reason == "continuation_attempt_not_pending"
    assert (session.status, session.instance_id, pending_execution.status) == before


def test_stale_claim_after_pause_is_refused_and_quiescent(db_session):
    session, _, _, _, identity = _retry_pending(db_session)
    old_instance = session.instance_id
    new_instance = revoke_autonomous_continuation(
        db_session, session, resulting_status="paused"
    )
    db_session.commit()

    result = claim_continuation(db_session, identity)
    authority = _authority(db_session, session)
    assert result.accepted is False
    assert new_instance != old_instance
    assert session.status == "paused"
    assert authority.logical_terminal is False
    assert authority.quiescent is True


def test_claim_ordered_after_pause_in_another_transaction_is_fenced(
    db_session, db_session_factory
):
    session, _, _, _, identity = _retry_pending(db_session)
    operator_db = db_session_factory()
    delivery_db = db_session_factory()
    try:
        operator_session = operator_db.get(SessionModel, session.id)
        revoke_autonomous_continuation(
            operator_db, operator_session, resulting_status="paused"
        )
        operator_db.commit()

        result = claim_continuation(delivery_db, identity)
        delivery_db.commit()
        current = delivery_db.get(SessionModel, session.id)
        assert result.accepted is False
        assert current.status == "paused"
    finally:
        operator_db.close()
        delivery_db.close()


def test_stale_claim_after_stop_is_refused_and_terminal(db_session):
    session, _, _, _, identity = _retry_pending(db_session)
    old_instance = session.instance_id
    revoke_autonomous_continuation(db_session, session, resulting_status="stopped")
    db_session.commit()

    result = claim_continuation(db_session, identity)
    authority = _authority(db_session, session)
    assert result.accepted is False
    assert session.instance_id != old_instance
    assert session.status == "stopped"
    assert authority.logical_terminal is True


def test_stale_claim_after_cancel_is_refused_and_terminal(db_session):
    session, _, _, _, identity = _retry_pending(db_session)
    revoke_autonomous_continuation(db_session, session, resulting_status="cancelled")
    db_session.commit()

    result = claim_continuation(db_session, identity)
    assert result.accepted is False
    assert session.status == "cancelled"
    assert _authority(db_session, session).logical_terminal is True


def test_stale_claim_after_success_cannot_resurrect_completed_session(db_session):
    session, _, _, pending_execution, identity = _retry_pending(db_session)
    old_instance = session.instance_id
    finalize_logical_success(db_session, session)
    db_session.commit()

    result = claim_continuation(db_session, identity)
    authority = _authority(db_session, session)
    assert result.accepted is False
    assert session.status == "completed"
    assert session.instance_id != old_instance
    assert pending_execution.status == TaskStatus.CANCELLED
    assert authority.logical_terminal is True
    assert authority.quiescent is True


def test_claim_after_final_failure_is_rejected_without_resurrection(db_session):
    session, _, _, _, identity = _retry_pending(db_session)
    finalize_logical_failure(db_session, session, failure_reason="unrecoverable")
    db_session.commit()

    result = claim_continuation(db_session, identity)
    authority = _authority(db_session, session)
    assert result.accepted is False
    assert session.status == "failed"
    assert authority.logical_terminal is True
    assert authority.quiescent is True


def test_claim_ordered_after_finalization_in_another_transaction_is_fenced(
    db_session, db_session_factory
):
    session, _, _, _, identity = _retry_pending(db_session)
    finalizer_db = db_session_factory()
    delivery_db = db_session_factory()
    try:
        finalizer_session = finalizer_db.get(SessionModel, session.id)
        finalize_logical_failure(
            finalizer_db,
            finalizer_session,
            failure_reason="unrecoverable",
        )
        finalizer_db.commit()

        result = claim_continuation(delivery_db, identity)
        delivery_db.commit()
        current = delivery_db.get(SessionModel, session.id)
        assert result.accepted is False
        assert current.status == "failed"
    finally:
        finalizer_db.close()
        delivery_db.close()


def test_final_failure_clears_marker_fences_generation_and_is_terminal(db_session):
    session, _, _, pending_execution, _ = _retry_pending(db_session)
    old_instance = session.instance_id
    finalize_logical_failure(db_session, session, failure_reason="budget_exhausted")
    db_session.commit()

    authority = _authority(db_session, session)
    assert session.status == "failed"
    assert session.instance_id != old_instance
    assert session.continuation_task_id is None
    assert pending_execution.status == TaskStatus.CANCELLED
    assert authority.logical_terminal is True
    assert authority.continuation_pending is False
    assert authority.quiescent is True


def test_explicit_new_generation_rotates_instance_id(db_session):
    session, _, _, _, _ = _retry_pending(db_session)
    old_instance = session.instance_id
    new_instance = begin_new_generation(db_session, session)
    db_session.commit()

    assert new_instance != old_instance
    assert session.status == "pending"
    assert session.instance_id == new_instance
    assert session.continuation_task_id is None


def test_autonomous_continuation_preserves_instance_across_all_steps(db_session):
    session, _, execution = _graph(db_session)
    original = session.instance_id
    enter_recovering(db_session, session, task_execution=execution)
    identity = schedule_continuation(
        db_session, session, continuation_kind="celery_retry", retry_count=2
    )
    result = claim_continuation(db_session, identity)

    assert result.accepted is True
    assert session.instance_id == original
    assert session.status == "running"


def test_historical_completed_and_failed_attempts_do_not_block_new_admission(
    db_session,
):
    project = Project(name="historical-project", workspace_path="/tmp/e2-history")
    db_session.add(project)
    db_session.flush()
    session = SessionModel(
        project_id=project.id,
        name="historical-session",
        status="pending",
        instance_id="new-generation",
    )
    old_task = Task(project_id=project.id, title="old", status=TaskStatus.FAILED)
    new_task = Task(project_id=project.id, title="new", status=TaskStatus.PENDING)
    db_session.add_all([session, old_task, new_task])
    db_session.flush()
    db_session.add_all(
        [
            TaskExecution(
                session_id=session.id,
                task_id=old_task.id,
                attempt_number=1,
                status=TaskStatus.FAILED,
            ),
            TaskExecution(
                session_id=session.id,
                task_id=old_task.id,
                attempt_number=2,
                status=TaskStatus.DONE,
            ),
        ]
    )
    db_session.commit()

    result = admit_autonomous_execution(db_session, session, task=new_task)
    assert result.accepted is True


def test_malformed_continuation_identity_is_rejected(db_session):
    session, _, _, _, identity = _retry_pending(db_session)
    malformed = ContinuationIdentity(
        session_id=identity.session_id,
        instance_id=identity.instance_id,
        continuation_task_id=identity.continuation_task_id,
        continuation_kind="not-a-supported-kind",
        task_execution_id=identity.task_execution_id,
        retry_count=identity.retry_count,
    )

    result = claim_continuation(db_session, malformed)
    assert result.accepted is False
    assert session.status == "retry_pending"


def test_malformed_durable_continuation_marker_is_not_claimable(db_session):
    session, _, _, _, identity = _retry_pending(db_session)
    session.continuation_kind = None
    db_session.commit()

    result = claim_continuation(db_session, identity)
    authority = _authority(db_session, session)
    assert result.accepted is False
    assert authority.continuation_pending is False
    assert authority.logical_terminal is False
    assert authority.quiescent is False


def test_final_success_marks_attempt_done_and_authority_terminal(db_session):
    session, _, execution = _graph(db_session)
    finalize_logical_success(db_session, session, task_execution=execution)
    db_session.commit()

    authority = _authority(db_session, session)
    assert execution.status == TaskStatus.DONE
    assert session.status == "completed"
    assert authority.logical_terminal is True
    assert authority.continuation_pending is False
    assert authority.quiescent is True
