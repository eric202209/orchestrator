"""Focused Phase 29C-3 ready selection and scheduler-claim tests."""

from __future__ import annotations

from datetime import datetime, timedelta, timezone
from dataclasses import replace
import threading

import pytest
from sqlalchemy import create_engine, inspect, text
from sqlalchemy.orm import sessionmaker

from app.db_migrations import MIGRATIONS, run_schema_migrations
from app.models import Base

from app.models import ExecutionTask, ExecutionTaskSchedulerClaim, TaskExecution
from app.services.execution.execution_eligibility_service import (
    ExecutionEligibilityService,
)
from app.services.execution.execution_task_scheduler_claim_service import (
    AcquireSchedulerClaimCommand,
    ExecutionReadyTaskSelectionService,
    ExecutionSchedulerClaimError,
    ExecutionTaskSchedulerClaimService,
    ReadyTaskSelectionScope,
)
from app.services.execution.execution_task_transition_service import (
    ExecutionTaskTransitionCommand,
    ExecutionTaskTransitionService,
)

from test_phase29c2_execution_eligibility import _build_context


def _transition(db, task, to_state, key):
    return ExecutionTaskTransitionService(db).transition(
        ExecutionTaskTransitionCommand(
            execution_task_id=task.id,
            execution_plan_id=task.execution_plan_id,
            expected_from_state=task.status,
            expected_state_version=int(task.state_version),
            to_state=to_state,
            reason_code=(
                "dependencies_satisfied"
                if to_state == "ready"
                else (
                    "execution_started"
                    if to_state == "running"
                    else "execution_succeeded"
                )
            ),
            actor_type="test",
            actor_id="phase29c3",
            idempotency_key=key,
        )
    )


def _ready_root(context):
    task = context["tasks"][0]
    _transition(context["db"], task, "ready", f"ready-{task.id}")
    context["db"].flush()
    return task


def _ready_dependent(context):
    db = context["db"]
    predecessor = context["tasks"][0]
    if predecessor.status == "pending":
        _transition(db, predecessor, "ready", f"ready-{predecessor.id}")
    _transition(db, predecessor, "running", f"running-{predecessor.id}")
    _transition(db, predecessor, "succeeded", f"succeeded-{predecessor.id}")
    dependent = context["tasks"][1]
    _transition(db, dependent, "ready", f"ready-{dependent.id}")
    db.flush()
    return dependent


def _command(db, task, scheduler_id="scheduler-a", key="claim-key", **overrides):
    decision = ExecutionEligibilityService(db).evaluate_task(task.id)
    candidate = (
        ExecutionReadyTaskSelectionService(db)
        .list_ready_candidates(execution_plan_id=task.execution_plan_id)
        .candidates[0]
    )
    values = {
        "execution_task_id": task.id,
        "expected_task_state": "ready",
        "expected_task_state_version": int(task.state_version),
        "expected_eligibility_decision_hash": decision.decision_hash,
        "scheduler_id": scheduler_id,
        "idempotency_key": key,
        "lease_duration_seconds": 60,
        "expected_graph_hash": candidate.graph_hash,
        "expected_predecessor_fence_hash": candidate.predecessor_fence_hash,
    }
    values.update(overrides)
    return AcquireSchedulerClaimCommand(**values)


def test_candidate_projection_requires_ready_active_eligible_task(db_session):
    context = _build_context(db_session)
    service = ExecutionReadyTaskSelectionService(db_session)
    assert service.list_ready_candidates().task_ids == ()

    root = _ready_root(context)
    candidates = service.list_ready_candidates()
    assert candidates.task_ids == (root.id,)
    assert all(item.execution_task_id == root.id for item in candidates.candidates)

    _transition(db_session, root, "blocked", f"blocked-{root.id}")
    db_session.flush()
    assert service.list_ready_candidates().task_ids == ()


def test_candidate_projection_excludes_inactive_and_soft_deleted_owners(db_session):
    context = _build_context(db_session)
    root = _ready_root(context)
    service = ExecutionReadyTaskSelectionService(db_session)
    context["execution_plan"].status = "superseded"
    db_session.flush()
    assert service.list_ready_candidates().task_ids == ()

    context["execution_plan"].status = "active"
    context["project"].deleted_at = datetime.now(timezone.utc)
    db_session.flush()
    result = service.list_ready_candidates()
    assert result.task_ids == ()
    assert any(item.execution_task_id == root.id for item in result.exclusions)


def test_candidate_order_is_deterministic_and_canonical_task_ordered(db_session):
    first = _build_context(db_session)
    second = _build_context(db_session)
    _ready_root(first)
    _ready_dependent(first)
    _ready_root(second)
    db_session.commit()

    service = ExecutionReadyTaskSelectionService(db_session)
    one = service.list_ready_candidates()
    two = service.list_ready_candidates()
    assert one.task_ids == two.task_ids
    assert one.task_ids[:2] == (first["tasks"][1].id, second["tasks"][0].id)
    assert one.candidates[0].plan_created_at <= one.candidates[1].plan_created_at


def test_acquire_binds_fences_and_does_not_change_task_or_create_execution(db_session):
    context = _build_context(db_session)
    task = _ready_root(context)
    old_status = task.status
    old_version = int(task.state_version)
    command = _command(db_session, task)

    result = ExecutionTaskSchedulerClaimService(db_session).acquire_claim(command)
    claim = result.claim
    assert claim.claim_status == "active"
    assert claim.scheduler_id == "scheduler-a"
    assert claim.claimed_task_state == "ready"
    assert claim.claimed_task_state_version == old_version
    assert len(claim.claimed_eligibility_decision_hash) == 64
    assert len(claim.claimed_graph_hash) == 64
    assert len(claim.predecessor_fence_hash) == 64
    assert claim.fencing_token == 1
    assert claim.expires_at > claim.acquired_at
    db_session.refresh(task)
    assert task.status == old_status == "ready"
    assert int(task.state_version) == old_version
    assert db_session.query(TaskExecution).count() == 0


def test_acquire_is_idempotent_and_cross_scheduler_key_reuse_conflicts(db_session):
    context = _build_context(db_session)
    task = _ready_root(context)
    service = ExecutionTaskSchedulerClaimService(db_session)
    command = _command(db_session, task)
    first = service.acquire_claim(command)
    replay = service.acquire_claim(command)
    assert replay.replayed is True
    assert replay.claim_id == first.claim_id
    assert (
        db_session.query(ExecutionTaskSchedulerClaim)
        .filter(ExecutionTaskSchedulerClaim.execution_task_id == task.id)
        .count()
        == 1
    )

    with pytest.raises(ExecutionSchedulerClaimError) as different_scheduler:
        service.acquire_claim(replace(command, scheduler_id="scheduler-b"))
    assert different_scheduler.value.code == "claim_idempotency_conflict"

    with pytest.raises(ExecutionSchedulerClaimError) as other_claim:
        service.acquire_claim(replace(command, idempotency_key="other-key"))
    assert other_claim.value.code == "task_already_claimed"


def test_stale_version_and_decision_are_rejected_before_claim(db_session):
    context = _build_context(db_session)
    task = _ready_root(context)
    service = ExecutionTaskSchedulerClaimService(db_session)
    command = _command(db_session, task)
    with pytest.raises(ExecutionSchedulerClaimError) as version:
        service.acquire_claim(
            AcquireSchedulerClaimCommand(
                **{**command.__dict__, "expected_task_state_version": 99}
            )
        )
    assert version.value.code == "task_version_stale"
    with pytest.raises(ExecutionSchedulerClaimError) as decision:
        service.acquire_claim(
            AcquireSchedulerClaimCommand(
                **{
                    **command.__dict__,
                    "expected_eligibility_decision_hash": "0" * 64,
                }
            )
        )
    assert decision.value.code == "eligibility_decision_stale"
    assert db_session.query(ExecutionTaskSchedulerClaim).count() == 0


def test_expired_claim_is_replaced_with_higher_fence_and_history_retained(db_session):
    context = _build_context(db_session)
    task = _ready_root(context)
    current = [datetime(2026, 7, 22, 12, 0, tzinfo=timezone.utc)]

    def clock():
        return current[0]

    service = ExecutionTaskSchedulerClaimService(db_session, now=clock)
    first = service.acquire_claim(_command(db_session, task, key="first"))
    current[0] += timedelta(seconds=61)
    replacement = service.acquire_claim(
        _command(db_session, task, scheduler_id="scheduler-b", key="second")
    )
    assert replacement.claim.fencing_token > first.fencing_token
    assert (
        db_session.get(ExecutionTaskSchedulerClaim, first.claim_id).claim_status
        == "expired"
    )
    assert db_session.query(ExecutionTaskSchedulerClaim).count() == 2


def test_release_requires_owner_and_fence_and_is_replayable(db_session):
    context = _build_context(db_session)
    task = _ready_root(context)
    service = ExecutionTaskSchedulerClaimService(db_session)
    acquired = service.acquire_claim(_command(db_session, task))
    with pytest.raises(ExecutionSchedulerClaimError) as owner:
        service.release_claim(
            acquired.claim_id,
            "scheduler-b",
            acquired.fencing_token,
            "scheduler_shutdown",
            "release",
        )
    assert owner.value.code == "claim_owner_mismatch"
    with pytest.raises(ExecutionSchedulerClaimError) as fence:
        service.release_claim(
            acquired.claim_id,
            "scheduler-a",
            acquired.fencing_token + 1,
            "scheduler_shutdown",
            "release",
        )
    assert fence.value.code == "claim_fence_stale"
    released = service.release_claim(
        acquired.claim_id,
        "scheduler-a",
        acquired.fencing_token,
        "dispatch_not_attempted",
        "release",
    )
    replay = service.release_claim(
        acquired.claim_id,
        "scheduler-a",
        acquired.fencing_token,
        "dispatch_not_attempted",
        "release",
    )
    assert released.claim.claim_status == "released"
    assert replay.replayed is True
    assert ExecutionReadyTaskSelectionService(
        db_session
    ).list_ready_candidates().task_ids == (task.id,)


def test_select_and_claim_is_bounded_and_never_dispatches(db_session):
    context = _build_context(db_session)
    task = _ready_root(context)
    service = ExecutionTaskSchedulerClaimService(db_session)
    result = service.select_and_claim_next(
        "scheduler-a", "select-key", ReadyTaskSelectionScope(limit=1)
    )
    assert result.code == "claimed"
    assert result.claim.execution_task_id == task.id
    assert db_session.query(TaskExecution).count() == 0
    assert db_session.get(ExecutionTask, task.id).status == "ready"

    no_candidate = service.select_and_claim_next(
        "scheduler-b", "select-key-2", ReadyTaskSelectionScope(limit=1)
    )
    assert no_candidate.code == "no_candidate_available"
    assert no_candidate.claim is None


def test_claim_integrity_and_plan_verifier_detect_tampering(db_session):
    context = _build_context(db_session)
    task = _ready_root(context)
    service = ExecutionTaskSchedulerClaimService(db_session)
    acquired = service.acquire_claim(_command(db_session, task))
    clean = service.verify_scheduler_claim_integrity(task.id)
    assert clean.verified is True
    assert (
        service.verify_plan_scheduler_claim_integrity(
            context["execution_plan"].id
        ).verified
        is True
    )

    acquired.claim.scheduler_id = ""
    db_session.flush()
    tampered = service.verify_scheduler_claim_integrity(task.id)
    assert tampered.verified is False
    assert "claim_owner_missing" in tampered.issues


def test_active_claim_for_non_ready_task_is_reported_without_repair(db_session):
    context = _build_context(db_session)
    task = _ready_root(context)
    service = ExecutionTaskSchedulerClaimService(db_session)
    service.acquire_claim(_command(db_session, task))
    _transition(db_session, task, "blocked", "block-after-claim")
    result = service.verify_scheduler_claim_integrity(task.id)
    assert result.verified is False
    assert "active_claim_for_non_ready_task" in result.issues
    assert task.status == "blocked"


def test_claim_does_not_modify_immutable_task_specification(db_session):
    context = _build_context(db_session)
    task = _ready_root(context)
    spec = task.task_spec
    done_when = task.done_when
    service = ExecutionTaskSchedulerClaimService(db_session)
    service.acquire_claim(_command(db_session, task))
    db_session.refresh(task)
    assert task.task_spec == spec
    assert task.done_when == done_when


def test_two_scheduler_sessions_race_to_one_active_claim(tmp_path):
    engine = create_engine(
        f"sqlite:///{tmp_path / 'phase29c3-race.db'}",
        connect_args={"check_same_thread": False, "timeout": 5},
    )
    SessionLocal = sessionmaker(autoflush=False, bind=engine)
    try:
        Base.metadata.create_all(engine)
        seed = SessionLocal()
        context = _build_context(seed)
        task = _ready_root(context)
        candidate = (
            ExecutionReadyTaskSelectionService(seed)
            .list_ready_candidates(execution_plan_id=context["execution_plan"].id)
            .candidates[0]
        )
        seed.commit()
        task_id = task.id
        barrier = threading.Barrier(2)
        outcomes = []
        outcome_lock = threading.Lock()

        def race(scheduler_id):
            session = SessionLocal()
            outcome = ("thread_failed", None)
            try:
                barrier.wait(timeout=5)
                command = AcquireSchedulerClaimCommand(
                    execution_task_id=task_id,
                    expected_task_state="ready",
                    expected_task_state_version=candidate.task_state_version,
                    expected_eligibility_decision_hash=candidate.decision_hash,
                    expected_graph_hash=candidate.graph_hash,
                    expected_predecessor_fence_hash=candidate.predecessor_fence_hash,
                    scheduler_id=scheduler_id,
                    idempotency_key=f"race-{scheduler_id}",
                )
                result = ExecutionTaskSchedulerClaimService(session).acquire_claim(
                    command
                )
                session.commit()
                outcome = ("claimed", result.claim.fencing_token)
            except ExecutionSchedulerClaimError as exc:
                session.rollback()
                outcome = (exc.code, None)
            finally:
                with outcome_lock:
                    outcomes.append(outcome)
                session.close()

        threads = [
            threading.Thread(target=race, args=("scheduler-a",)),
            threading.Thread(target=race, args=("scheduler-b",)),
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=20)
        assert all(not thread.is_alive() for thread in threads)
        assert sorted(item[0] for item in outcomes) == [
            "claimed",
            "task_already_claimed",
        ]
        check = SessionLocal()
        try:
            assert (
                check.query(ExecutionTaskSchedulerClaim)
                .filter(
                    ExecutionTaskSchedulerClaim.execution_task_id == task_id,
                    ExecutionTaskSchedulerClaim.claim_status == "active",
                )
                .count()
                == 1
            )
        finally:
            check.close()
    finally:
        seed.close()
        Base.metadata.drop_all(engine)
        engine.dispose()


def test_scheduler_claim_migration_replays_and_matches_fresh_schema(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'phase29c3.db'}")
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE execution_task_scheduler_claims"))
        # Preserve the C3 fixture boundary through C4 while excluding C5/C6B.
        run_schema_migrations(engine, MIGRATIONS[:-3])
        run_schema_migrations(engine)
        run_schema_migrations(engine)
        columns = {
            column["name"]
            for column in inspect(engine).get_columns("execution_task_scheduler_claims")
        }
        assert {
            "id",
            "execution_plan_id",
            "execution_task_id",
            "project_id",
            "planning_session_id",
            "scheduler_id",
            "idempotency_key",
            "command_payload",
            "canonical_command_hash",
            "fencing_token",
            "claim_status",
            "expires_at",
            "release_idempotency_key",
        } <= columns
        assert inspect(engine).has_table("execution_task_scheduler_claims")
        with engine.connect() as connection:
            applied = connection.execute(
                text(
                    "SELECT COUNT(*) FROM schema_migrations "
                    "WHERE version = '034_execution_task_scheduler_claim'"
                )
            ).scalar_one()
        assert applied == 1
    finally:
        engine.dispose()
