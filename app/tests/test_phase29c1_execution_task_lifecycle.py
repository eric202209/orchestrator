"""Focused Phase 29C-1 Execution Task lifecycle tests."""

from __future__ import annotations

import ast
from pathlib import Path

import pytest
from sqlalchemy import create_engine, inspect, text

from app.db_migrations import MIGRATIONS, run_schema_migrations
from app.models import (
    Base,
    ExecutionDependencyEdge,
    ExecutionGroup,
    ExecutionGroupMember,
    ExecutionPlan,
    ExecutionTask,
    ExecutionTaskTransition,
    PlanningCommitManifest,
    Session,
    Task,
    TaskExecution,
)
from app.services.execution.execution_plan_commit_service import (
    ExecutionPlanCommitService,
)
from app.services.execution.execution_task_transition_service import (
    ALLOWED_EXECUTION_TASK_TRANSITIONS,
    EXECUTION_TASK_REASON_CODES,
    ExecutionTaskTransitionCommand,
    ExecutionTaskTransitionError,
    ExecutionTaskTransitionIntegrityError,
    ExecutionTaskTransitionService,
    _event_payload,
)
from app.services.planning.operator_review import canonical_json_hash

from test_phase29b1_execution_plan_commit_service import (
    _build_accepted_commit_authority,
)


@pytest.fixture
def execution_context(db_session):
    project, session, plan, checkpoint, completion, commit_manifest = (
        _build_accepted_commit_authority(db_session)
    )
    execution_plan = ExecutionPlanCommitService(db_session).commit(commit_manifest.id)
    db_session.commit()
    db_session.refresh(execution_plan)
    tasks = (
        db_session.query(ExecutionTask)
        .filter(ExecutionTask.execution_plan_id == execution_plan.id)
        .order_by(ExecutionTask.id.asc())
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


def _command(
    task: ExecutionTask,
    *,
    to_state: str,
    expected_from_state: str | None = None,
    expected_version: int | None = None,
    key: str = "command-1",
    actor_type: str = "test",
    actor_id: str | None = "test-actor",
    reason_code: str = "system_reconciliation",
    reason_detail: str | None = "focused test",
    plan_id: int | None = None,
) -> ExecutionTaskTransitionCommand:
    source_state = expected_from_state or task.status
    if reason_code == "system_reconciliation":
        reason_code = {
            ("running", "awaiting_validation"): "runtime_candidate_completed",
            ("running", "awaiting_recovery"): "runtime_attempt_failed",
            ("awaiting_validation", "succeeded"): "validation_accepted",
            ("awaiting_validation", "awaiting_recovery"): "validation_rejected",
            ("awaiting_recovery", "ready"): "recovery_retry_authorized",
            ("awaiting_recovery", "failed"): "recovery_exhausted",
        }.get((source_state, to_state), reason_code)
    return ExecutionTaskTransitionCommand(
        execution_task_id=task.id,
        execution_plan_id=plan_id,
        expected_from_state=source_state,
        expected_state_version=(
            task.state_version if expected_version is None else expected_version
        ),
        to_state=to_state,
        reason_code=reason_code,
        reason_detail=reason_detail,
        actor_type=actor_type,
        actor_id=actor_id,
        idempotency_key=key,
    )


_PATHS_TO_STATE = {
    "pending": (),
    "ready": ("ready",),
    "running": ("ready", "running"),
    "awaiting_validation": ("ready", "running", "awaiting_validation"),
    "awaiting_recovery": ("ready", "running", "awaiting_recovery"),
    "succeeded": ("ready", "running", "succeeded"),
    "failed": ("ready", "running", "failed"),
    "blocked": ("blocked",),
    "paused": ("ready", "paused"),
    "cancelled": ("cancelled",),
    "skipped": ("skipped",),
}


def _drive_to(service, task, state: str, *, key_prefix: str = "path"):
    for index, target in enumerate(_PATHS_TO_STATE[state]):
        service.transition(
            _command(task, to_state=target, key=f"{key_prefix}-{state}-{index}")
        )
    assert task.status == state
    assert task.state_version == len(_PATHS_TO_STATE[state])


def test_new_execution_task_starts_pending_version_zero(execution_context, db_session):
    task = execution_context["tasks"][0]

    assert task.status == "pending"
    assert task.state_version == 0
    assert db_session.query(ExecutionTaskTransition).count() == 0


@pytest.mark.parametrize(
    "source,target",
    tuple(
        (source, target)
        for source, targets in sorted(ALLOWED_EXECUTION_TASK_TRANSITIONS.items())
        for target in sorted(targets)
    ),
)
def test_every_allowed_transition_succeeds(execution_context, source, target):
    task = execution_context["tasks"][0]
    service = ExecutionTaskTransitionService(execution_context["db"])
    _drive_to(service, task, source)

    result = service.transition(
        _command(task, to_state=target, key=f"allowed-{source}-{target}")
    )

    assert result.from_state == source
    assert result.to_state == target
    assert result.resulting_version == len(_PATHS_TO_STATE[source]) + 1
    assert task.status == target
    assert task.state_version == result.resulting_version


def test_every_disallowed_transition_fails_closed(execution_context):
    db = execution_context["db"]
    service = ExecutionTaskTransitionService(db)
    task = execution_context["tasks"][0]
    task_id = task.id
    for source in sorted(_PATHS_TO_STATE):
        db.rollback()
        db.expunge_all()
        task = db.get(ExecutionTask, task_id)
        assert task.status == "pending"
        assert task.state_version == 0
        _drive_to(service, task, source, key_prefix="invalid-source")
        allowed = ALLOWED_EXECUTION_TASK_TRANSITIONS[source]
        for target in sorted(set(ALLOWED_EXECUTION_TASK_TRANSITIONS) - set(allowed)):
            with pytest.raises(ExecutionTaskTransitionError) as exc_info:
                service.transition(
                    _command(
                        task,
                        to_state=target,
                        key=f"disallowed-{source}-{target}",
                    )
                )
            assert exc_info.value.code == "transition_not_allowed"
        db.rollback()
        db.expunge_all()
        task = db.get(ExecutionTask, task_id)


def test_running_to_blocked_fails(execution_context):
    task = execution_context["tasks"][0]
    service = ExecutionTaskTransitionService(execution_context["db"])
    _drive_to(service, task, "running")

    with pytest.raises(ExecutionTaskTransitionError) as exc_info:
        service.transition(_command(task, to_state="blocked", key="running-blocked"))
    assert exc_info.value.code == "transition_not_allowed"
    assert task.status == "running"
    assert task.state_version == 2


@pytest.mark.parametrize("terminal", ("succeeded", "cancelled", "skipped"))
def test_terminal_states_reject_outgoing_transitions(execution_context, terminal):
    task = execution_context["tasks"][0]
    service = ExecutionTaskTransitionService(execution_context["db"])
    _drive_to(service, task, terminal)

    with pytest.raises(ExecutionTaskTransitionError) as exc_info:
        service.transition(
            _command(task, to_state="ready", key=f"terminal-{terminal}-ready")
        )
    assert exc_info.value.code == "transition_not_allowed"


def test_unknown_stored_current_state_fails_closed(execution_context):
    db = execution_context["db"]
    task = execution_context["tasks"][0]
    task.status = "unknown-state"
    db.commit()

    with pytest.raises(ExecutionTaskTransitionError) as exc_info:
        ExecutionTaskTransitionService(db).transition(
            _command(task, to_state="ready", expected_from_state="pending")
        )
    assert exc_info.value.code == "invalid_current_state"


def test_unknown_requested_state_fails_closed(execution_context):
    task = execution_context["tasks"][0]

    with pytest.raises(ExecutionTaskTransitionError) as exc_info:
        ExecutionTaskTransitionService(execution_context["db"]).transition(
            _command(task, to_state="unknown-state")
        )
    assert exc_info.value.code == "invalid_requested_state"


def test_wrong_expected_state_fails(execution_context):
    task = execution_context["tasks"][0]

    with pytest.raises(ExecutionTaskTransitionError) as exc_info:
        ExecutionTaskTransitionService(execution_context["db"]).transition(
            _command(
                task,
                to_state="ready",
                expected_from_state="blocked",
                key="wrong-state",
            )
        )
    assert exc_info.value.code == "transition_state_stale"


def test_wrong_expected_version_fails(execution_context):
    task = execution_context["tasks"][0]
    service = ExecutionTaskTransitionService(execution_context["db"])

    with pytest.raises(ExecutionTaskTransitionError) as exc_info:
        service.transition(
            _command(task, to_state="ready", expected_version=4, key="wrong-version")
        )
    assert exc_info.value.code == "transition_version_stale"


def test_first_valid_command_increments_version_once(execution_context):
    db = execution_context["db"]
    task = execution_context["tasks"][0]
    result = ExecutionTaskTransitionService(db).transition(
        _command(task, to_state="ready", key="first-valid")
    )

    assert result.resulting_version == 1
    assert task.state_version == 1
    assert db.query(ExecutionTaskTransition).count() == 1


def test_stale_concurrent_command_cannot_overwrite_newer_state(
    execution_context, db_session_factory
):
    db = execution_context["db"]
    task = execution_context["tasks"][0]
    ExecutionTaskTransitionService(db).transition(
        _command(task, to_state="ready", key="concurrent-first")
    )
    db.commit()

    other_db = db_session_factory()
    try:
        other_task = other_db.get(ExecutionTask, task.id)
        with pytest.raises(ExecutionTaskTransitionError) as exc_info:
            ExecutionTaskTransitionService(other_db).transition(
                _command(
                    other_task,
                    to_state="running",
                    expected_from_state="ready",
                    expected_version=0,
                    key="concurrent-stale",
                )
            )
        assert exc_info.value.code == "transition_version_stale"
        other_db.rollback()
    finally:
        other_db.close()
    db.refresh(task)
    assert task.status == "ready"
    assert task.state_version == 1


def test_rollback_leaves_state_version_and_event_count_unchanged(execution_context):
    db = execution_context["db"]
    task = execution_context["tasks"][0]
    ExecutionTaskTransitionService(db).transition(
        _command(task, to_state="ready", key="rolled-back")
    )
    db.rollback()
    db.refresh(task)

    assert task.status == "pending"
    assert task.state_version == 0
    assert db.query(ExecutionTaskTransition).count() == 0


def test_same_command_key_replays_same_event_without_increment(execution_context):
    db = execution_context["db"]
    task = execution_context["tasks"][0]
    command = _command(task, to_state="ready", key="replay-me")
    service = ExecutionTaskTransitionService(db)
    first = service.transition(command)
    db.commit()
    second = service.transition(command)

    assert first.event_id == second.event_id
    assert first.event_hash == second.event_hash
    assert second.replayed is True
    assert task.state_version == 1
    assert db.query(ExecutionTaskTransition).count() == 1


def test_same_key_with_different_target_conflicts(execution_context):
    db = execution_context["db"]
    task = execution_context["tasks"][0]
    service = ExecutionTaskTransitionService(db)
    service.transition(_command(task, to_state="ready", key="same-key-target"))
    db.commit()

    with pytest.raises(ExecutionTaskTransitionError) as exc_info:
        service.transition(
            _command(
                task,
                to_state="blocked",
                expected_from_state="pending",
                expected_version=0,
                key="same-key-target",
            )
        )
    assert exc_info.value.code == "transition_idempotency_conflict"


def test_same_key_with_different_expected_version_conflicts(execution_context):
    db = execution_context["db"]
    task = execution_context["tasks"][0]
    service = ExecutionTaskTransitionService(db)
    service.transition(_command(task, to_state="ready", key="same-key-version"))
    db.commit()

    with pytest.raises(ExecutionTaskTransitionError) as exc_info:
        service.transition(
            _command(
                task,
                to_state="ready",
                expected_from_state="pending",
                expected_version=1,
                key="same-key-version",
            )
        )
    assert exc_info.value.code == "transition_idempotency_conflict"


def test_different_key_with_stale_version_fails(execution_context):
    db = execution_context["db"]
    task = execution_context["tasks"][0]
    service = ExecutionTaskTransitionService(db)
    service.transition(_command(task, to_state="ready", key="fresh-key"))
    db.commit()

    with pytest.raises(ExecutionTaskTransitionError) as exc_info:
        service.transition(
            _command(
                task,
                to_state="running",
                expected_from_state="pending",
                expected_version=0,
                key="different-stale-key",
            )
        )
    assert exc_info.value.code == "transition_state_stale"


def test_event_persists_complete_binding_and_deterministic_hashes(execution_context):
    db = execution_context["db"]
    task = execution_context["tasks"][0]
    service = ExecutionTaskTransitionService(db)
    service.transition(
        _command(
            task,
            to_state="ready",
            key="event-binding",
            actor_type="scheduler",
            actor_id="scheduler-1",
            reason_code="dependencies_satisfied",
            reason_detail="all applicable prerequisites were observed",
        )
    )
    service.transition(
        _command(
            task,
            to_state="running",
            key="event-binding-2",
            actor_type="worker",
            actor_id="worker-1",
            reason_code="execution_started",
        )
    )
    db.commit()
    events = (
        db.query(ExecutionTaskTransition)
        .filter(ExecutionTaskTransition.execution_task_id == task.id)
        .order_by(ExecutionTaskTransition.sequence.asc())
        .all()
    )

    assert [event.sequence for event in events] == [1, 2]
    assert events[0].execution_plan_id == execution_context["execution_plan"].id
    assert events[0].plan_task_id == task.plan_task_id
    assert events[0].actor_type == "scheduler"
    assert events[0].reason_code == "dependencies_satisfied"
    assert events[0].expected_version == 0
    assert events[0].resulting_version == 1
    assert events[0].previous_event_hash is None
    assert events[1].previous_event_hash == events[0].event_hash
    assert events[0].event_hash == events[0].canonical_payload_hash
    assert events[0].event_hash == canonical_json_hash(_event_payload(events[0]))


def test_event_rows_have_no_mutation_api_and_are_not_written_by_other_services(
    execution_context,
):
    db = execution_context["db"]
    task = execution_context["tasks"][0]
    service = ExecutionTaskTransitionService(db)
    service.transition(_command(task, to_state="ready", key="only-service"))
    db.commit()

    assert not hasattr(service, "update_transition")
    assert not hasattr(service, "delete_transition")
    assert db.query(ExecutionTaskTransition).count() == 1


def test_plan_delete_cascades_lifecycle_events_downward(execution_context):
    db = execution_context["db"]
    db.connection().exec_driver_sql("PRAGMA foreign_keys = ON")
    task = execution_context["tasks"][0]
    execution_plan = execution_context["execution_plan"]
    ExecutionTaskTransitionService(db).transition(
        _command(task, to_state="ready", key="delete-downward")
    )
    db.commit()

    db.delete(execution_plan)
    db.commit()
    assert db.query(ExecutionTaskTransition).count() == 0
    assert db.query(ExecutionTask).filter(ExecutionTask.id == task.id).count() == 0
    assert (
        db.query(PlanningCommitManifest)
        .filter(PlanningCommitManifest.id == execution_context["commit_manifest"].id)
        .count()
        == 1
    )


def test_deleting_lifecycle_event_does_not_cascade_upward(execution_context):
    db = execution_context["db"]
    task = execution_context["tasks"][0]
    execution_plan = execution_context["execution_plan"]
    service = ExecutionTaskTransitionService(db)
    service.transition(_command(task, to_state="ready", key="delete-event"))
    db.commit()
    event = db.query(ExecutionTaskTransition).one()
    db.delete(event)
    db.commit()

    assert db.get(ExecutionTask, task.id) is not None
    assert db.get(ExecutionPlan, execution_plan.id) is not None
    assert db.get(PlanningCommitManifest, execution_context["commit_manifest"].id)


def test_integrity_verifier_accepts_clean_chain_and_plan(execution_context):
    db = execution_context["db"]
    task = execution_context["tasks"][0]
    service = ExecutionTaskTransitionService(db)
    service.transition(_command(task, to_state="ready", key="clean-1"))
    service.transition(_command(task, to_state="running", key="clean-2"))
    db.commit()

    result = service.verify_task_lifecycle_integrity(task.id)
    plan_result = service.verify_execution_plan_lifecycle_integrity(
        execution_context["execution_plan"].id
    )
    assert result.verified is True
    assert result.event_count == 2
    assert result.current_state == "running"
    assert result.state_version == 2
    assert plan_result.task_count == 2


@pytest.mark.parametrize(
    "tamper",
    ("payload", "previous", "sequence", "illegal", "status", "version", "binding"),
)
def test_integrity_verifier_detects_each_tamper(execution_context, tamper):
    db = execution_context["db"]
    task = execution_context["tasks"][0]
    service = ExecutionTaskTransitionService(db)
    service.transition(_command(task, to_state="ready", key="tamper-1"))
    service.transition(_command(task, to_state="running", key="tamper-2"))
    db.commit()
    events = (
        db.query(ExecutionTaskTransition)
        .filter(ExecutionTaskTransition.execution_task_id == task.id)
        .order_by(ExecutionTaskTransition.sequence.asc())
        .all()
    )
    if tamper == "payload":
        events[0].reason_detail = "mutated"
    elif tamper == "previous":
        events[1].previous_event_hash = "0" * 64
    elif tamper == "sequence":
        events[1].sequence = 3
    elif tamper == "illegal":
        events[1].to_state = "blocked"
    elif tamper == "status":
        task.status = "ready"
    elif tamper == "version":
        task.state_version = 99
    else:
        events[0].execution_plan_id = execution_context["execution_plan"].id + 100
    db.commit()

    with pytest.raises(ExecutionTaskTransitionIntegrityError) as exc_info:
        service.verify_task_lifecycle_integrity(task.id)
    assert exc_info.value.code == "transition_integrity_failure"


def test_integrity_verifier_detects_event_attached_to_another_task(execution_context):
    db = execution_context["db"]
    first, second = execution_context["tasks"]
    service = ExecutionTaskTransitionService(db)
    service.transition(_command(first, to_state="ready", key="wrong-task"))
    db.commit()
    event = db.query(ExecutionTaskTransition).one()
    event.execution_task_id = second.id
    db.commit()

    with pytest.raises(ExecutionTaskTransitionIntegrityError):
        service.verify_execution_plan_lifecycle_integrity(
            execution_context["execution_plan"].id
        )


def test_lifecycle_chain_isolated_per_task(execution_context):
    db = execution_context["db"]
    first, second = execution_context["tasks"]
    service = ExecutionTaskTransitionService(db)
    service.transition(_command(first, to_state="ready", key="first-only"))
    db.commit()

    assert second.status == "pending"
    assert second.state_version == 0
    assert (
        db.query(ExecutionTaskTransition)
        .filter(ExecutionTaskTransition.execution_task_id == second.id)
        .count()
        == 0
    )
    assert service.verify_task_lifecycle_integrity(second.id).event_count == 0


def test_transition_does_not_modify_immutable_graph_or_runtime_rows(
    execution_context,
):
    db = execution_context["db"]
    task = execution_context["tasks"][0]
    execution_plan = execution_context["execution_plan"]
    before = {
        "title": task.title,
        "blocking_state": task.blocking_state,
        "task_spec": task.task_spec,
        "done_when": task.done_when,
        "edges": [
            (
                edge.plan_dependency_id,
                edge.prerequisite_execution_task_id,
                edge.dependent_execution_task_id,
            )
            for edge in db.query(ExecutionDependencyEdge)
            .filter(ExecutionDependencyEdge.execution_plan_id == execution_plan.id)
            .all()
        ],
        "groups": db.query(ExecutionGroup)
        .filter(ExecutionGroup.execution_plan_id == execution_plan.id)
        .count(),
        "members": db.query(ExecutionGroupMember).count(),
    }
    counts_before = {
        "tasks": db.query(Task).count(),
        "sessions": db.query(Session).count(),
        "task_executions": db.query(TaskExecution).count(),
    }

    ExecutionTaskTransitionService(db).transition(
        _command(task, to_state="ready", key="isolation")
    )
    db.commit()
    db.refresh(task)
    after_edges = [
        (
            edge.plan_dependency_id,
            edge.prerequisite_execution_task_id,
            edge.dependent_execution_task_id,
        )
        for edge in db.query(ExecutionDependencyEdge)
        .filter(ExecutionDependencyEdge.execution_plan_id == execution_plan.id)
        .all()
    ]

    assert {
        "title": task.title,
        "blocking_state": task.blocking_state,
        "task_spec": task.task_spec,
        "done_when": task.done_when,
    } == {
        key: before[key]
        for key in ("title", "blocking_state", "task_spec", "done_when")
    }
    assert after_edges == before["edges"]
    assert (
        db.query(ExecutionGroup)
        .filter(ExecutionGroup.execution_plan_id == execution_plan.id)
        .count()
        == before["groups"]
    )
    assert db.query(ExecutionGroupMember).count() == before["members"]
    assert {
        "tasks": db.query(Task).count(),
        "sessions": db.query(Session).count(),
        "task_executions": db.query(TaskExecution).count(),
    } == counts_before


def test_transition_on_inactive_parent_plan_fails(execution_context):
    db = execution_context["db"]
    task = execution_context["tasks"][0]
    execution_plan = execution_context["execution_plan"]
    execution_plan.status = "superseded"
    db.commit()

    with pytest.raises(ExecutionTaskTransitionError) as exc_info:
        ExecutionTaskTransitionService(db).transition(
            _command(task, to_state="ready", plan_id=execution_plan.id)
        )
    assert exc_info.value.code == "execution_plan_inactive"


def test_reason_and_actor_vocabulary_is_bounded(execution_context):
    task = execution_context["tasks"][0]
    service = ExecutionTaskTransitionService(execution_context["db"])
    with pytest.raises(ExecutionTaskTransitionError) as reason_error:
        service.transition(
            _command(task, to_state="ready", reason_code="made-up-reason")
        )
    assert reason_error.value.code == "invalid_reason"
    with pytest.raises(ExecutionTaskTransitionError) as actor_error:
        service.transition(_command(task, to_state="ready", actor_type="made-up-actor"))
    assert actor_error.value.code == "invalid_actor"
    assert "system_reconciliation" in EXECUTION_TASK_REASON_CODES


def test_production_execution_task_lifecycle_writes_are_boundary_local():
    app_root = Path(__file__).parents[1]
    for path in app_root.rglob("*.py"):
        if "/tests/" in str(path):
            continue
        source = path.read_text(encoding="utf-8")
        if "ExecutionTask" not in source:
            continue
        tree = ast.parse(source, filename=str(path))
        for node in ast.walk(tree):
            if isinstance(node, ast.Assign):
                targets = node.targets
            elif isinstance(node, (ast.AnnAssign, ast.AugAssign)):
                targets = [node.target]
            else:
                continue
            for target in targets:
                if isinstance(target, ast.Attribute) and target.attr in {
                    "status",
                    "state_version",
                }:
                    raise AssertionError(
                        f"direct ExecutionTask lifecycle assignment in {path}: "
                        f"{target.attr}"
                    )


def test_migration_adds_lifecycle_column_and_table_to_phase29b_schema(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'phase29b.db'}")
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE execution_task_transitions"))
            connection.execute(
                text("ALTER TABLE execution_tasks DROP COLUMN state_version")
            )
            connection.execute(
                text(
                    "ALTER TABLE execution_tasks DROP COLUMN "
                    "validation_contract_status"
                )
            )
            connection.execute(
                text(
                    "DROP INDEX IF EXISTS " "ix_execution_tasks_validation_contract_id"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE execution_tasks DROP COLUMN " "validation_contract_id"
                )
            )
            connection.execute(
                text(
                    "DROP INDEX IF EXISTS "
                    "ix_execution_plans_validation_contract_set_hash"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE execution_plans DROP COLUMN validation_contract_set_hash"
                )
            )
            connection.execute(
                text("DROP TABLE execution_task_validation_specifications")
            )
            run_schema_migrations(engine, MIGRATIONS[:-6])
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO execution_tasks "
                    "(execution_plan_id, plan_task_id, title, blocking_state, "
                    "task_spec, done_when, status) VALUES "
                    "(1, 'task-1', 'Task', 'blocking', '{}', '[]', 'pending')"
                )
            )
        run_schema_migrations(engine)
        run_schema_migrations(engine)
        columns = {
            column["name"] for column in inspect(engine).get_columns("execution_tasks")
        }
        assert "state_version" in columns
        assert inspect(engine).has_table("execution_task_transitions")
        with engine.connect() as connection:
            row = connection.execute(
                text("SELECT status, state_version FROM execution_tasks")
            ).one()
            event_count = connection.execute(
                text("SELECT COUNT(*) FROM execution_task_transitions")
            ).scalar_one()
        assert tuple(row) == ("pending", 0)
        assert event_count == 0
    finally:
        engine.dispose()


def test_migration_refuses_existing_non_pending_task(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'phase29b-invalid.db'}")
    try:
        Base.metadata.create_all(engine)
        with engine.begin() as connection:
            connection.execute(text("DROP TABLE execution_task_transitions"))
            connection.execute(
                text("ALTER TABLE execution_tasks DROP COLUMN state_version")
            )
            connection.execute(
                text(
                    "ALTER TABLE execution_tasks DROP COLUMN "
                    "validation_contract_status"
                )
            )
            connection.execute(
                text(
                    "DROP INDEX IF EXISTS " "ix_execution_tasks_validation_contract_id"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE execution_tasks DROP COLUMN " "validation_contract_id"
                )
            )
            connection.execute(
                text(
                    "DROP INDEX IF EXISTS "
                    "ix_execution_plans_validation_contract_set_hash"
                )
            )
            connection.execute(
                text(
                    "ALTER TABLE execution_plans DROP COLUMN validation_contract_set_hash"
                )
            )
            connection.execute(
                text("DROP TABLE execution_task_validation_specifications")
            )
            run_schema_migrations(engine, MIGRATIONS[:-6])
        with engine.begin() as connection:
            connection.execute(
                text(
                    "INSERT INTO execution_tasks "
                    "(execution_plan_id, plan_task_id, title, blocking_state, "
                    "task_spec, done_when, status) VALUES "
                    "(1, 'task-1', 'Task', 'blocking', '{}', '[]', 'running')"
                )
            )
        with pytest.raises(RuntimeError, match="non-pending"):
            run_schema_migrations(engine)
        with engine.connect() as connection:
            applied = connection.execute(
                text(
                    "SELECT COUNT(*) FROM schema_migrations "
                    "WHERE version = '033_execution_task_lifecycle'"
                )
            ).scalar_one()
        assert applied == 0
    finally:
        engine.dispose()


def test_fresh_create_all_schema_matches_lifecycle_migration_shape():
    engine = create_engine("sqlite://")
    try:
        Base.metadata.create_all(engine)
        run_schema_migrations(engine)
        columns = {
            column["name"] for column in inspect(engine).get_columns("execution_tasks")
        }
        transition_columns = {
            column["name"]
            for column in inspect(engine).get_columns("execution_task_transitions")
        }
        assert "state_version" in columns
        assert transition_columns == {
            "id",
            "execution_plan_id",
            "execution_task_id",
            "plan_task_id",
            "sequence",
            "from_state",
            "to_state",
            "reason_code",
            "reason_detail",
            "actor_type",
            "actor_id",
            "command_id",
            "expected_version",
            "resulting_version",
            "canonical_command_hash",
            "canonical_payload_hash",
            "previous_event_hash",
            "event_hash",
            "runtime_attempt_id",
            "runtime_lease_id",
            "runtime_ownership_fence",
            "created_at",
        }
    finally:
        Base.metadata.drop_all(engine)
        engine.dispose()
