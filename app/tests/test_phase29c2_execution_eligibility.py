"""Focused Phase 29C-2 eligibility projection and reconciliation tests."""

from __future__ import annotations

from dataclasses import replace

import pytest

import app.services.planning.protocol_persistence as protocol_persistence

from app.models import (
    ExecutionDependencyEdge,
    ExecutionGroupMember,
    ExecutionTask,
    ExecutionTaskTransition,
    Task,
    TaskExecution,
)
from app.services.execution.execution_eligibility_service import (
    ExecutionEligibilityError,
    ExecutionEligibilityService,
)
from app.services.execution.execution_plan_commit_service import (
    ExecutionPlanCommitService,
)
from app.services.execution.execution_task_transition_service import (
    ExecutionTaskTransitionCommand,
    ExecutionTaskTransitionService,
)

from test_phase29b1_execution_plan_commit_service import (
    _build_accepted_commit_authority,
)
import test_phase29b1_execution_plan_commit_service as phase29b1


def _build_context(db_session):
    project, session, plan, checkpoint, completion, commit_manifest = (
        _build_accepted_commit_authority(db_session)
    )
    execution_plan = ExecutionPlanCommitService(db_session).commit(commit_manifest.id)
    db_session.commit()
    db_session.refresh(execution_plan)
    tasks = (
        db_session.query(ExecutionTask)
        .filter(ExecutionTask.execution_plan_id == execution_plan.id)
        .order_by(ExecutionTask.plan_task_id.asc())
        .all()
    )
    return {
        "db": db_session,
        "project": project,
        "session": session,
        "plan": plan,
        "checkpoint": checkpoint,
        "completion": completion,
        "commit_manifest": commit_manifest,
        "execution_plan": execution_plan,
        "tasks": tasks,
    }


@pytest.fixture
def eligibility_context(db_session):
    return _build_context(db_session)


@pytest.fixture
def review_eligibility_context(db_session, monkeypatch):
    original_plan = phase29b1._plan
    original_validate = phase29b1.validate_structured_task_plan
    original_persistence_validate = protocol_persistence.validate_structured_task_plan

    def review_plan(**kwargs):
        plan = original_plan(**kwargs)
        tasks = list(plan.tasks)
        tasks[1] = replace(tasks[1], blocking_state="review_required")
        return replace(plan, tasks=tuple(tasks))

    def review_validate(*args, **kwargs):
        validation = original_validate(*args, **kwargs)
        return replace(validation, protocol_acceptable=True)

    def review_persistence_validate(*args, **kwargs):
        validation = original_persistence_validate(*args, **kwargs)
        return replace(validation, protocol_acceptable=True)

    monkeypatch.setattr(phase29b1, "_plan", review_plan)
    monkeypatch.setattr(phase29b1, "validate_structured_task_plan", review_validate)
    monkeypatch.setattr(
        protocol_persistence,
        "validate_structured_task_plan",
        review_persistence_validate,
    )
    return _build_context(db_session)


def _transition(
    db,
    task,
    to_state,
    *,
    key=None,
    expected_state=None,
    expected_version=None,
    actor_type="test",
    actor_id="eligibility-test",
):
    return ExecutionTaskTransitionService(db).transition(
        ExecutionTaskTransitionCommand(
            execution_task_id=task.id,
            execution_plan_id=task.execution_plan_id,
            expected_from_state=expected_state or task.status,
            expected_state_version=(
                task.state_version if expected_version is None else expected_version
            ),
            to_state=to_state,
            reason_code="system_reconciliation",
            actor_type=actor_type,
            actor_id=actor_id,
            idempotency_key=key or f"{task.id}-{to_state}-{task.state_version}",
        )
    )


def _set_predecessor_state(db, task, state):
    paths = {
        "pending": (),
        "ready": ("ready",),
        "running": ("ready", "running"),
        "succeeded": ("ready", "running", "succeeded"),
        "failed": ("ready", "running", "failed"),
        "blocked": ("blocked",),
        "paused": ("ready", "paused"),
        "cancelled": ("cancelled",),
        "skipped": ("skipped",),
    }
    for index, target in enumerate(paths[state]):
        _transition(db, task, target, key=f"predecessor-{task.id}-{state}-{index}")
    db.flush()


def _dependent(context):
    return context["tasks"][1]


def _predecessor(context):
    return context["tasks"][0]


def test_root_pending_task_is_eligible(eligibility_context):
    decision = ExecutionEligibilityService(eligibility_context["db"]).evaluate_task(
        _predecessor(eligibility_context).id
    )
    assert decision.eligible is True
    assert decision.reason_code == "eligible_root_task"
    assert decision.recommended_state == "ready"
    assert decision.dependency_results == ()


@pytest.mark.parametrize(
    ("state", "expected_result", "expected_reason"),
    (
        ("pending", "waiting", "waiting_on_dependencies"),
        ("ready", "waiting", "waiting_on_dependencies"),
        ("running", "waiting", "waiting_on_dependencies"),
        ("blocked", "waiting", "waiting_on_dependencies"),
        ("paused", "waiting", "waiting_on_dependencies"),
        ("failed", "failed", "dependency_failed"),
        ("cancelled", "failed", "dependency_cancelled"),
        ("skipped", "not_satisfied", "dependency_skipped"),
    ),
)
def test_dependency_state_matrix(
    eligibility_context, state, expected_result, expected_reason
):
    db = eligibility_context["db"]
    predecessor = _predecessor(eligibility_context)
    _set_predecessor_state(db, predecessor, state)
    decision = ExecutionEligibilityService(db).evaluate_task(
        _dependent(eligibility_context).id
    )
    dependency = decision.dependency_results[0]
    assert dependency.result == expected_result
    assert dependency.reason_code == expected_reason
    assert decision.eligible is False
    assert decision.reason_code == expected_reason


def test_succeeded_predecessor_satisfies_dependency(eligibility_context):
    db = eligibility_context["db"]
    _set_predecessor_state(db, _predecessor(eligibility_context), "succeeded")
    decision = ExecutionEligibilityService(db).evaluate_task(
        _dependent(eligibility_context).id
    )
    assert decision.eligible is True
    assert decision.reason_code == "eligible_dependencies_satisfied"
    assert decision.dependency_results[0].result == "satisfied"


def test_multiple_blockers_have_stable_order(eligibility_context):
    db = eligibility_context["db"]
    target = _dependent(eligibility_context)
    decision = ExecutionEligibilityService(db).evaluate_task(target.id)
    assert decision.reason_code == "waiting_on_dependencies"
    assert decision.blockers == ("waiting_on_dependencies",)


def test_unknown_edge_type_fails_closed(eligibility_context):
    db = eligibility_context["db"]
    edge = (
        db.query(ExecutionDependencyEdge)
        .filter(
            ExecutionDependencyEdge.execution_plan_id
            == eligibility_context["execution_plan"].id
        )
        .one()
    )
    edge.source_dependency_type = "future_unknown"
    db.flush()
    decision = ExecutionEligibilityService(db).evaluate_task(
        _dependent(eligibility_context).id
    )
    assert decision.eligible is False
    assert decision.reason_code == "unknown_dependency_type"


def test_cross_plan_dependency_endpoint_fails_integrity(eligibility_context):
    db = eligibility_context["db"]
    _, _, _, _, _, other_manifest = _build_accepted_commit_authority(db)
    other_plan = ExecutionPlanCommitService(db).commit(other_manifest.id)
    db.flush()
    other_task = (
        db.query(ExecutionTask)
        .filter(ExecutionTask.execution_plan_id == other_plan.id)
        .order_by(ExecutionTask.plan_task_id.asc())
        .first()
    )
    edge = (
        db.query(ExecutionDependencyEdge)
        .filter(
            ExecutionDependencyEdge.execution_plan_id
            == eligibility_context["execution_plan"].id
        )
        .one()
    )
    edge.prerequisite_execution_task_id = other_task.id
    db.flush()
    decision = ExecutionEligibilityService(db).evaluate_task(
        _dependent(eligibility_context).id
    )
    assert decision.eligible is False
    assert decision.reason_code == "graph_integrity_failure"


def test_review_required_task_is_bounded_gate(review_eligibility_context):
    db = review_eligibility_context["db"]
    target = _dependent(review_eligibility_context)
    _set_predecessor_state(db, _predecessor(review_eligibility_context), "succeeded")
    decision = ExecutionEligibilityService(db).evaluate_task(target.id)
    assert decision.eligible is False
    assert decision.reason_code == "review_gate_pending"
    assert decision.gate_results[0].gate_type == "review_gate"


def test_groups_are_explanation_metadata_not_gates(eligibility_context):
    db = eligibility_context["db"]
    _set_predecessor_state(db, _predecessor(eligibility_context), "succeeded")
    decision = ExecutionEligibilityService(db).evaluate_task(
        _dependent(eligibility_context).id
    )
    group_results = [
        result
        for result in decision.gate_results
        if result.gate_type == "execution_group"
    ]
    assert group_results
    assert group_results[0].result == "metadata_only"
    assert decision.eligible is True


@pytest.mark.parametrize(
    "state", ("running", "paused", "failed", "succeeded", "cancelled", "skipped")
)
def test_non_reconcilable_states_are_read_only(eligibility_context, state):
    db = eligibility_context["db"]
    task = _predecessor(eligibility_context)
    _set_predecessor_state(db, task, state)
    before = db.query(ExecutionTaskTransition).count()
    decision = ExecutionEligibilityService(db).evaluate_task(task.id)
    assert decision.reason_code == "task_state_not_reconcilable"
    assert decision.eligible is False
    result = ExecutionEligibilityService(db).reconcile_task(
        task.id, state, task.state_version, "operator", f"read-only-{state}"
    )
    assert result.no_op is True
    assert db.query(ExecutionTaskTransition).count() == before


def test_pending_eligible_reconciles_to_ready(eligibility_context):
    db = eligibility_context["db"]
    task = _predecessor(eligibility_context)
    result = ExecutionEligibilityService(db).reconcile_task(
        task.id, "pending", 0, "scheduler-test", "root-ready"
    )
    assert result.transition is not None
    assert result.transition.to_state == "ready"
    assert task.status == "ready"
    assert task.state_version == 1
    assert result.decision.reason_code == "eligible_root_task"
    assert result.transition.event_hash


def test_pending_dependent_reconciles_to_blocked(eligibility_context):
    db = eligibility_context["db"]
    task = _dependent(eligibility_context)
    result = ExecutionEligibilityService(db).reconcile_task(
        task.id, "pending", 0, "scheduler-test", "dependent-blocked"
    )
    assert result.transition is not None
    assert result.transition.to_state == "blocked"
    assert task.status == "blocked"
    assert task.state_version == 1
    assert result.decision.reason_code == "waiting_on_dependencies"


def test_blocked_dependent_reconciles_to_ready_after_success(eligibility_context):
    db = eligibility_context["db"]
    target = _dependent(eligibility_context)
    _transition(db, target, "blocked", key="initial-block")
    _set_predecessor_state(db, _predecessor(eligibility_context), "succeeded")
    result = ExecutionEligibilityService(db).reconcile_task(
        target.id, "blocked", 1, "scheduler-test", "unblock-after-success"
    )
    assert result.transition.to_state == "ready"
    assert target.status == "ready"
    assert target.state_version == 2


def test_blocked_task_still_blocked_is_no_op(eligibility_context):
    db = eligibility_context["db"]
    task = _dependent(eligibility_context)
    _transition(db, task, "blocked", key="still-blocked")
    before = db.query(ExecutionTaskTransition).count()
    result = ExecutionEligibilityService(db).reconcile_task(
        task.id, "blocked", 1, "scheduler-test", "still-blocked-reconcile"
    )
    assert result.no_op is True
    assert result.transition is None
    assert db.query(ExecutionTaskTransition).count() == before
    assert task.state_version == 1


def test_ready_dependency_regression_returns_to_blocked(eligibility_context):
    db = eligibility_context["db"]
    target = _dependent(eligibility_context)
    predecessor = _predecessor(eligibility_context)
    _set_predecessor_state(db, predecessor, "ready")
    _transition(db, target, "ready", key="ready-before-regression")
    # A predecessor may regress before the dependent starts; the dependent's
    # ready projection is therefore fenced back to blocked.
    _transition(db, predecessor, "blocked", key="predecessor-regresses")
    result = ExecutionEligibilityService(db).reconcile_task(
        target.id, "ready", 1, "scheduler-test", "regression-to-blocked"
    )
    assert result.transition.to_state == "blocked"
    assert target.status == "blocked"


def test_reconciliation_reason_contains_decision_evidence(eligibility_context):
    db = eligibility_context["db"]
    task = _predecessor(eligibility_context)
    result = ExecutionEligibilityService(db).reconcile_task(
        task.id, "pending", 0, "scheduler-test", "evidence-key"
    )
    event = db.query(ExecutionTaskTransition).one()
    assert result.decision.decision_hash in event.reason_detail
    assert result.decision.graph_hash in event.reason_detail


def test_reconciliation_replay_returns_same_event(eligibility_context):
    db = eligibility_context["db"]
    task = _predecessor(eligibility_context)
    service = ExecutionEligibilityService(db)
    first = service.reconcile_task(task.id, "pending", 0, "scheduler-test", "replay")
    db.commit()
    second = service.reconcile_task(task.id, "pending", 0, "scheduler-test", "replay")
    assert second.replayed is True
    assert second.transition.event_id == first.transition.event_id
    assert second.transition.event_hash == first.transition.event_hash
    assert db.query(ExecutionTaskTransition).count() == 1


def test_stale_target_state_and_version_fail_before_event(eligibility_context):
    db = eligibility_context["db"]
    task = _predecessor(eligibility_context)
    _transition(db, task, "ready", key="make-ready")
    for expected_state, expected_version, code in (
        ("pending", 1, "eligibility_target_state_stale"),
        ("ready", 0, "eligibility_target_version_stale"),
    ):
        with pytest.raises(ExecutionEligibilityError) as exc_info:
            ExecutionEligibilityService(db).reconcile_task(
                task.id,
                expected_state,
                expected_version,
                "scheduler-test",
                f"stale-{code}",
            )
        assert exc_info.value.code == code
    assert db.query(ExecutionTaskTransition).count() == 1


def test_predecessor_fence_rejects_changed_projection(eligibility_context, monkeypatch):
    db = eligibility_context["db"]
    predecessor = _predecessor(eligibility_context)
    target = _dependent(eligibility_context)
    _set_predecessor_state(db, predecessor, "ready")
    _transition(db, target, "ready", key="ready-before-race")
    service = ExecutionEligibilityService(db)
    original = service._transition.transition

    def race(command):
        _transition(db, predecessor, "blocked", key="race-predecessor")
        return original(command)

    monkeypatch.setattr(service._transition, "transition", race)
    with pytest.raises(ExecutionEligibilityError) as exc_info:
        service.reconcile_task(
            target.id, "ready", 1, "scheduler-test", "predecessor-race"
        )
    assert exc_info.value.code == "eligibility_predecessor_stale"
    assert target.status == "ready"


def test_decision_hash_changes_when_predecessor_version_changes(eligibility_context):
    db = eligibility_context["db"]
    target = _dependent(eligibility_context)
    first = ExecutionEligibilityService(db).evaluate_task(target.id)
    _transition(
        db, _predecessor(eligibility_context), "ready", key="predecessor-version"
    )
    second = ExecutionEligibilityService(db).evaluate_task(target.id)
    assert first.decision_hash != second.decision_hash
    assert first.lifecycle_head_hashes != second.lifecycle_head_hashes


def test_evaluation_isolated_from_legacy_runtime_rows(eligibility_context):
    db = eligibility_context["db"]
    before = {
        "tasks": db.query(Task).count(),
        "executions": db.query(TaskExecution).count(),
        "transitions": db.query(ExecutionTaskTransition).count(),
    }
    ExecutionEligibilityService(db).evaluate_plan(
        eligibility_context["execution_plan"].id
    )
    assert db.query(Task).count() == before["tasks"]
    assert db.query(TaskExecution).count() == before["executions"]
    assert db.query(ExecutionTaskTransition).count() == before["transitions"]


def test_plan_wide_evaluation_is_deterministic(eligibility_context):
    service = ExecutionEligibilityService(eligibility_context["db"])
    first = service.evaluate_plan(eligibility_context["execution_plan"].id)
    second = service.evaluate_plan(eligibility_context["execution_plan"].id)
    assert [item.plan_task_id for item in first] == ["TASK-001", "TASK-002"]
    assert [item.decision_hash for item in first] == [
        item.decision_hash for item in second
    ]
    assert first[0].eligible is True
    assert first[1].eligible is False


def test_plan_inactive_does_not_reconcile(eligibility_context):
    db = eligibility_context["db"]
    plan = eligibility_context["execution_plan"]
    plan.status = "superseded"
    db.flush()
    task = _predecessor(eligibility_context)
    decision = ExecutionEligibilityService(db).evaluate_task(task.id)
    assert decision.reason_code == "execution_plan_inactive"
    result = ExecutionEligibilityService(db).reconcile_task(
        task.id, "pending", 0, "scheduler-test", "inactive"
    )
    assert result.no_op is True
    assert db.query(ExecutionTaskTransition).count() == 0


def test_group_membership_tamper_fails_closed(eligibility_context):
    db = eligibility_context["db"]
    members = (
        db.query(ExecutionGroupMember)
        .join(ExecutionGroupMember.execution_group)
        .filter(ExecutionGroupMember.execution_group_id.isnot(None))
        .order_by(ExecutionGroupMember.id.asc())
        .all()
    )
    members[1].member_order = members[0].member_order
    db.flush()
    decision = ExecutionEligibilityService(db).evaluate_task(
        _dependent(eligibility_context).id
    )
    assert decision.reason_code == "graph_integrity_failure"


def test_unknown_persisted_gate_fails_closed_with_bounded_reason(eligibility_context):
    db = eligibility_context["db"]
    task = _predecessor(eligibility_context)
    task.blocking_state = "future_gate"
    db.flush()
    decision = ExecutionEligibilityService(db).evaluate_task(task.id)
    assert decision.eligible is False
    assert decision.reason_code == "unknown_gate_type"
    assert decision.blockers == ("unknown_gate_type",)


def test_ready_eligible_is_a_no_op(eligibility_context):
    db = eligibility_context["db"]
    task = _predecessor(eligibility_context)
    _transition(db, task, "ready", key="ready-no-op")
    before_events = db.query(ExecutionTaskTransition).count()
    result = ExecutionEligibilityService(db).reconcile_task(
        task.id, "ready", 1, "scheduler-test", "ready-no-op-reconcile"
    )
    assert result.no_op is True
    assert result.transition is None
    assert task.status == "ready"
    assert task.state_version == 1
    assert db.query(ExecutionTaskTransition).count() == before_events


def test_no_op_replay_is_stable(eligibility_context):
    db = eligibility_context["db"]
    task = _dependent(eligibility_context)
    _transition(db, task, "blocked", key="no-op-block")
    service = ExecutionEligibilityService(db)
    first = service.reconcile_task(
        task.id, "blocked", 1, "scheduler-test", "no-op-replay"
    )
    second = service.reconcile_task(
        task.id, "blocked", 1, "scheduler-test", "no-op-replay"
    )
    assert first.no_op is True
    assert second.no_op is True
    assert first.decision.decision_hash == second.decision.decision_hash
    assert db.query(ExecutionTaskTransition).count() == 1


def test_same_key_different_task_conflicts(eligibility_context):
    db = eligibility_context["db"]
    tasks = eligibility_context["tasks"]
    service = ExecutionEligibilityService(db)
    service.reconcile_task(tasks[0].id, "pending", 0, "scheduler-test", "same-key")
    with pytest.raises(ExecutionEligibilityError) as exc_info:
        service.reconcile_task(tasks[1].id, "pending", 0, "scheduler-test", "same-key")
    assert exc_info.value.code == "eligibility_idempotency_conflict"


def test_same_key_different_expected_version_conflicts(eligibility_context):
    db = eligibility_context["db"]
    task = _predecessor(eligibility_context)
    service = ExecutionEligibilityService(db)
    service.reconcile_task(task.id, "pending", 0, "scheduler-test", "version-key")
    with pytest.raises(ExecutionEligibilityError) as exc_info:
        service.reconcile_task(task.id, "pending", 1, "scheduler-test", "version-key")
    assert exc_info.value.code == "eligibility_idempotency_conflict"


def test_transition_reason_for_failed_dependency_is_bounded(eligibility_context):
    db = eligibility_context["db"]
    predecessor = _predecessor(eligibility_context)
    target = _dependent(eligibility_context)
    _set_predecessor_state(db, predecessor, "failed")
    result = ExecutionEligibilityService(db).reconcile_task(
        target.id, "pending", 0, "scheduler-test", "failed-block"
    )
    event = (
        db.query(ExecutionTaskTransition).filter_by(execution_task_id=target.id).one()
    )
    assert result.transition.to_state == "blocked"
    assert event.reason_code == "dependency_failed"


def test_reconciliation_changes_only_target_lifecycle(eligibility_context):
    db = eligibility_context["db"]
    predecessor = _predecessor(eligibility_context)
    target = _dependent(eligibility_context)
    before = (predecessor.status, predecessor.state_version)
    result = ExecutionEligibilityService(db).reconcile_task(
        target.id, "pending", 0, "scheduler-test", "target-only"
    )
    assert result.transition.to_state == "blocked"
    assert (predecessor.status, predecessor.state_version) == before
    assert (
        db.query(ExecutionTaskTransition)
        .filter_by(execution_task_id=predecessor.id)
        .count()
        == 0
    )
    assert (
        db.query(ExecutionTaskTransition).filter_by(execution_task_id=target.id).count()
        == 1
    )


def test_immutable_graph_and_task_spec_are_not_changed_by_reconciliation(
    eligibility_context,
):
    db = eligibility_context["db"]
    target = _dependent(eligibility_context)
    edge_before = (
        db.query(ExecutionDependencyEdge)
        .filter(ExecutionDependencyEdge.execution_plan_id == target.execution_plan_id)
        .one()
    )
    graph_before = (
        edge_before.plan_dependency_id,
        edge_before.prerequisite_execution_task_id,
        edge_before.dependent_execution_task_id,
        edge_before.source_dependency_type,
        edge_before.runtime_class,
    )
    task_spec_before = target.task_spec
    done_when_before = target.done_when
    _ = ExecutionEligibilityService(db).reconcile_task(
        target.id, "pending", 0, "scheduler-test", "immutable-inputs"
    )
    db.refresh(edge_before)
    db.refresh(target)
    assert (
        edge_before.plan_dependency_id,
        edge_before.prerequisite_execution_task_id,
        edge_before.dependent_execution_task_id,
        edge_before.source_dependency_type,
        edge_before.runtime_class,
    ) == graph_before
    assert target.task_spec == task_spec_before
    assert target.done_when == done_when_before


def test_target_lifecycle_integrity_failure_is_not_reconciled(eligibility_context):
    db = eligibility_context["db"]
    task = _predecessor(eligibility_context)
    task.status = "ready"
    task.state_version = 1
    db.commit()
    decision = ExecutionEligibilityService(db).evaluate_task(task.id)
    assert decision.reason_code == "lifecycle_integrity_failure"
    result = ExecutionEligibilityService(db).reconcile_task(
        task.id, "ready", 1, "scheduler-test", "tampered-target"
    )
    assert result.no_op is True
    assert db.query(ExecutionTaskTransition).count() == 0


def test_predecessor_lifecycle_integrity_failure_blocks_dependent(eligibility_context):
    db = eligibility_context["db"]
    predecessor = _predecessor(eligibility_context)
    _set_predecessor_state(db, predecessor, "ready")
    event = (
        db.query(ExecutionTaskTransition)
        .filter_by(execution_task_id=predecessor.id)
        .one()
    )
    event.event_hash = "f" * 64
    db.flush()
    decision = ExecutionEligibilityService(db).evaluate_task(
        _dependent(eligibility_context).id
    )
    assert decision.reason_code == "lifecycle_integrity_failure"
    assert decision.eligible is False


def test_missing_dependency_endpoint_is_detected(eligibility_context):
    db = eligibility_context["db"]
    edge = (
        db.query(ExecutionDependencyEdge)
        .filter(
            ExecutionDependencyEdge.execution_plan_id
            == eligibility_context["execution_plan"].id
        )
        .one()
    )
    edge.prerequisite_execution_task_id = 999999
    db.flush()
    decision = ExecutionEligibilityService(db).evaluate_task(
        _dependent(eligibility_context).id
    )
    assert decision.reason_code == "graph_integrity_failure"
    assert decision.dependency_results[0].result == "invalid"


def test_identical_decision_hash_survives_reload(eligibility_context):
    db = eligibility_context["db"]
    task = _dependent(eligibility_context)
    service = ExecutionEligibilityService(db)
    first = service.evaluate_task(task.id)
    db.expire_all()
    second = service.evaluate_task(task.id)
    assert first.payload() == second.payload()
    assert first.decision_hash == second.decision_hash


def test_transition_version_and_event_count_increment_once(eligibility_context):
    db = eligibility_context["db"]
    task = _predecessor(eligibility_context)
    result = ExecutionEligibilityService(db).reconcile_task(
        task.id, "pending", 0, "scheduler-test", "one-transition"
    )
    assert result.transition.resulting_version == 1
    assert task.state_version == 1
    assert (
        db.query(ExecutionTaskTransition).filter_by(execution_task_id=task.id).count()
        == 1
    )


@pytest.mark.parametrize(
    "state",
    ("running", "paused", "failed", "succeeded", "cancelled", "skipped"),
)
def test_terminal_and_runtime_states_never_transition_automatically(
    eligibility_context, state
):
    db = eligibility_context["db"]
    task = _predecessor(eligibility_context)
    _set_predecessor_state(db, task, state)
    before = (
        task.status,
        task.state_version,
        db.query(ExecutionTaskTransition).count(),
    )
    result = ExecutionEligibilityService(db).reconcile_task(
        task.id, state, task.state_version, "scheduler-test", f"runtime-{state}"
    )
    assert result.transition is None
    assert result.no_op is True
    assert (
        task.status,
        task.state_version,
        db.query(ExecutionTaskTransition).count(),
    ) == before


def test_no_legacy_runtime_side_effects(eligibility_context):
    db = eligibility_context["db"]
    before = (db.query(Task).count(), db.query(TaskExecution).count())
    service = ExecutionEligibilityService(db)
    service.reconcile_task(
        _dependent(eligibility_context).id,
        "pending",
        0,
        "scheduler-test",
        "no-runtime-side-effect",
    )
    assert (db.query(Task).count(), db.query(TaskExecution).count()) == before


def test_dependency_result_carries_predecessor_version_and_head(eligibility_context):
    db = eligibility_context["db"]
    predecessor = _predecessor(eligibility_context)
    _transition(db, predecessor, "ready", key="dependency-fence-input")
    decision = ExecutionEligibilityService(db).evaluate_task(
        _dependent(eligibility_context).id
    )
    dependency = decision.dependency_results[0]
    assert dependency.predecessor_state == "ready"
    assert dependency.predecessor_state_version == 1
    assert dependency.predecessor_lifecycle_head_hash is not None


def test_decision_binds_exact_execution_plan_and_task(eligibility_context):
    decision = ExecutionEligibilityService(eligibility_context["db"]).evaluate_task(
        _dependent(eligibility_context).id
    )
    assert decision.execution_plan_id == eligibility_context["execution_plan"].id
    assert decision.execution_task_id == _dependent(eligibility_context).id
    assert decision.plan_task_id == "TASK-002"


def test_group_explanation_order_is_canonical(eligibility_context):
    service = ExecutionEligibilityService(eligibility_context["db"])
    first = service.evaluate_task(_dependent(eligibility_context).id)
    second = service.evaluate_task(_dependent(eligibility_context).id)
    assert [item.gate_id for item in first.gate_results] == [
        item.gate_id for item in second.gate_results
    ]


def test_pending_review_gate_reconciles_to_blocked(review_eligibility_context):
    db = review_eligibility_context["db"]
    predecessor = _predecessor(review_eligibility_context)
    target = _dependent(review_eligibility_context)
    _set_predecessor_state(db, predecessor, "succeeded")
    result = ExecutionEligibilityService(db).reconcile_task(
        target.id, "pending", 0, "scheduler-test", "review-block"
    )
    assert result.decision.reason_code == "review_gate_pending"
    assert result.transition.to_state == "blocked"


def test_reconciliation_replay_does_not_require_a_second_transition(
    eligibility_context,
):
    db = eligibility_context["db"]
    task = _predecessor(eligibility_context)
    service = ExecutionEligibilityService(db)
    first = service.reconcile_task(
        task.id, "pending", 0, "scheduler-test", "replay-once"
    )
    db.commit()
    count = db.query(ExecutionTaskTransition).count()
    replay = service.reconcile_task(
        task.id, "pending", 0, "scheduler-test", "replay-once"
    )
    assert replay.replayed is True
    assert replay.transition.event_hash == first.transition.event_hash
    assert db.query(ExecutionTaskTransition).count() == count


def test_different_key_against_stale_version_fails(eligibility_context):
    db = eligibility_context["db"]
    task = _predecessor(eligibility_context)
    _transition(db, task, "ready", key="stale-baseline")
    with pytest.raises(ExecutionEligibilityError) as exc_info:
        ExecutionEligibilityService(db).reconcile_task(
            task.id, "pending", 0, "scheduler-test", "different-key"
        )
    assert exc_info.value.code == "eligibility_target_state_stale"


def test_blocked_eligible_has_single_ready_transition(eligibility_context):
    db = eligibility_context["db"]
    predecessor = _predecessor(eligibility_context)
    target = _dependent(eligibility_context)
    _transition(db, target, "blocked", key="blocked-baseline")
    _set_predecessor_state(db, predecessor, "succeeded")
    result = ExecutionEligibilityService(db).reconcile_task(
        target.id, "blocked", 1, "scheduler-test", "blocked-ready"
    )
    assert result.transition.to_state == "ready"
    assert (
        db.query(ExecutionTaskTransition).filter_by(execution_task_id=target.id).count()
        == 2
    )


def test_pending_integrity_failure_does_not_become_blocked(eligibility_context):
    db = eligibility_context["db"]
    edge = (
        db.query(ExecutionDependencyEdge)
        .filter(
            ExecutionDependencyEdge.execution_plan_id
            == eligibility_context["execution_plan"].id
        )
        .one()
    )
    edge.prerequisite_execution_task_id = 999999
    db.flush()
    target = _dependent(eligibility_context)
    result = ExecutionEligibilityService(db).reconcile_task(
        target.id, "pending", 0, "scheduler-test", "integrity-no-write"
    )
    assert result.no_op is True
    assert target.status == "pending"
    assert db.query(ExecutionTaskTransition).count() == 0


def test_plan_wide_results_have_complete_dependency_explanations(eligibility_context):
    results = ExecutionEligibilityService(eligibility_context["db"]).evaluate_plan(
        eligibility_context["execution_plan"].id
    )
    assert len(results) == len(eligibility_context["tasks"])
    assert results[0].dependency_results == ()
    assert len(results[1].dependency_results) == 1


def test_reconciliation_decision_hash_is_in_reason_detail(eligibility_context):
    db = eligibility_context["db"]
    task = _dependent(eligibility_context)
    result = ExecutionEligibilityService(db).reconcile_task(
        task.id, "pending", 0, "scheduler-test", "hash-detail"
    )
    event = db.query(ExecutionTaskTransition).filter_by(execution_task_id=task.id).one()
    assert f'"decision_hash":"{result.decision.decision_hash}"' in event.reason_detail


def test_evaluation_does_not_create_transition_events(eligibility_context):
    db = eligibility_context["db"]
    service = ExecutionEligibilityService(db)
    service.evaluate_task(_predecessor(eligibility_context).id)
    service.evaluate_task(_dependent(eligibility_context).id)
    assert db.query(ExecutionTaskTransition).count() == 0
