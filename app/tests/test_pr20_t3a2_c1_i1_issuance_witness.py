"""PR20 T3-A2a C1-I1/I2: SQLite request-changes witness issuance and enforcement.

Every test runs on a file-backed SQLite database migrated by the real runner
(foreign keys on, WAL, migration 061 triggers installed), so constraints and
triggers are exercised rather than assumed. Publication is recording-only; no
provider, Celery broker, generated command, or production database is used.
Worker and replay enforcement tests use the same migrated file-backed database.
"""

import threading
import time
from copy import deepcopy
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine, event, inspect, text
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.api.v1.endpoints.tasks import TaskRetryRequest, _queue_task_retry
from app.db_migrations import (
    MIGRATIONS,
    _061_trigger_statements,
    _migration_061_request_changes_issuance_witness,
    _verify_061_request_changes_issuance_witness,
    run_schema_migrations,
)
from app.models import Base, Project, RequestChangesIssuanceWitness
from app.models import Session as SessionModel
from app.models import Task, TaskExecution, TaskExecutionChangeSet, TaskStatus
from app.services.execution.workflow_lifecycle import (
    _dispatch_replay_arguments,
    configure_task_execution_dispatch,
    dispatch_value_sha256,
    publish_task_execution_dispatch,
)
from app.services.tasks.execution import create_task_execution
from app.services.tasks.service import TaskService
from app.tests.test_pr20_t3a2_request_changes_boundary import (
    OPERATOR_DESCRIPTION,
    _add_prior_change_set,
    _create_task,
    _queue_active_request_changes_retry,
    _run_worker_completion_seam,
    _stub_retry_publication,
    _worker_seam_failure_diagnostics,
)

NOTE = "Review note."
SAME_SESSION = {"retry_intent": "request_changes"}
NEW_SESSION = {
    "retry_intent": "request_changes",
    "create_new_session": True,
    "execution_scope": "new_session",
}
WITNESS_TABLE = "request_changes_issuance_witnesses"
LOCK_TIMEOUT_SECONDS = 2.0


def _engine(path: Path, *, timeout: float = LOCK_TIMEOUT_SECONDS):
    """Mirror the production SQLite connection policy on a temporary file."""

    engine = create_engine(
        f"sqlite:///{path}",
        connect_args={"check_same_thread": False, "timeout": timeout},
    )

    @event.listens_for(engine, "connect")
    def _pragmas(dbapi_connection, _):
        cursor = dbapi_connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute(f"PRAGMA busy_timeout={int(timeout * 1000)}")
        cursor.close()

    return engine


@pytest.fixture
def db_session_factory(tmp_path: Path):
    """Override conftest: a migrated file-backed database for this module."""

    engine = _engine(tmp_path / "c1-i1.db")
    Base.metadata.create_all(bind=engine)
    run_schema_migrations(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    try:
        yield factory
    finally:
        engine.dispose()


def _bind(factory):
    return factory.kw["bind"]


def _review_task(authenticated_client, db_session, tmp_path: Path):
    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)
    prior = _add_prior_change_set(db_session, task)
    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/request-changes", json={"note": NOTE}
    )
    assert response.status_code == 200, response.text
    return task, prior


def _witness_rows(factory) -> list[dict]:
    with _bind(factory).connect() as connection:
        rows = connection.execute(
            text(f"SELECT * FROM {WITNESS_TABLE} ORDER BY current_task_execution_id")
        )
        return [dict(row._mapping) for row in rows]


def _execution_ids(factory, task_id: int) -> list[int]:
    with _bind(factory).connect() as connection:
        rows = connection.execute(
            text("SELECT id FROM task_executions WHERE task_id = :t ORDER BY id"),
            {"t": task_id},
        )
        return [row[0] for row in rows]


def _fresh(factory, model, ident):
    observer = factory()
    try:
        row = observer.get(model, ident)
        if row is not None:
            observer.expunge(row)
        return row
    finally:
        observer.close()


def _assert_review_intact(factory, task_id: int):
    task = _fresh(factory, Task, task_id)
    assert task.status == TaskStatus.DONE
    assert task.workspace_status == "changes_requested"
    assert task.promotion_note == NOTE
    assert task.description == OPERATOR_DESCRIPTION


def _assert_authority_inputs_intact(factory, task_id: int):
    """After a successful issuance the Task is PENDING; inputs are unchanged."""

    task = _fresh(factory, Task, task_id)
    assert task.status == TaskStatus.PENDING
    assert task.workspace_status == "changes_requested"
    assert task.promotion_note == NOTE
    assert task.description == OPERATOR_DESCRIPTION


def _assert_no_issuance(factory, task, prior, published):
    """No retry row, no witness, no publication; review state unchanged."""

    assert _execution_ids(factory, task.id) == [prior.task_execution_id]
    assert _witness_rows(factory) == []
    assert published == {}
    _assert_review_intact(factory, task.id)


def _expected_witness(execution: TaskExecution, prior: TaskExecutionChangeSet):
    authority = execution.dispatch_invocation["acceptance_authority"]
    assert authority["prior_change_set"]["change_set_id"] == prior.id
    return {
        "current_task_execution_id": execution.id,
        "source_task_execution_id": prior.task_execution_id,
        "source_session_id": prior.session_id,
        "change_set_id": prior.id,
        "bounded_fields_sha256": authority["prior_change_set"]["bounded_fields_sha256"],
        "invocation_sha256": execution.dispatch_invocation_sha256,
        "witness_schema_version": 1,
    }


def _issue(authenticated_client, db_session_factory, task, prior, monkeypatch, body):
    published = _stub_retry_publication(monkeypatch)
    response = authenticated_client.post(f"/api/v1/tasks/{task.id}/retry", json=body)
    assert response.status_code == 200, response.text
    execution = _fresh(
        db_session_factory, TaskExecution, response.json()["task_execution_id"]
    )
    return response, execution, published


def _clone_retry_execution(
    factory,
    original_execution_id,
    *,
    invocation_prior=None,
    invocation_updates=None,
    persisted_invocation_sha256=None,
    witness_overrides=None,
    include_witness=True,
    same_attempt_change_set=False,
):
    """Create an INSERT-only malformed retry fixture under migration 061.

    The dispatch row and optional witness are inserted while all migration
    constraints/triggers remain active. This bypasses issuance only to model
    malformed persisted data at the verifier boundary.
    """
    db = factory()
    try:
        original = db.get(TaskExecution, original_execution_id)
        original_witness = db.get(RequestChangesIssuanceWitness, original_execution_id)
        assert original is not None and original_witness is not None
        clone = create_task_execution(
            db,
            session_id=original.session_id,
            task_id=original.task_id,
        )
        db.flush()
        invocation = deepcopy(original.dispatch_invocation)
        selected_prior = invocation_prior
        if same_attempt_change_set:
            pinned_record = db.get(
                TaskExecutionChangeSet, original_witness.change_set_id
            )
            assert pinned_record is not None
            candidate = TaskExecutionChangeSet(
                project_id=pinned_record.project_id,
                task_id=original.task_id,
                session_id=original.session_id,
                task_execution_id=clone.id,
                base_snapshot_key=f"same-attempt:{clone.id}",
                added_files=["same-attempt.py"],
                modified_files=[],
                deleted_files=[],
                warning_flags=[],
                status="completed",
                captured_at=datetime.now(timezone.utc),
                created_at=datetime.now(timezone.utc),
            )
            db.add(candidate)
            db.flush()
            candidate_record = TaskService(db).get_task_execution_change_set(
                task_execution_id=clone.id
            )
            selected_prior = {
                "change_set_id": candidate.id,
                "task_execution_id": clone.id,
                "bounded_fields_sha256": dispatch_value_sha256(
                    TaskService.request_changes_change_set_fields(candidate_record)
                ),
            }
        if selected_prior is not None:
            invocation["acceptance_authority"]["prior_change_set"] = dict(
                selected_prior
            )
        invocation.update(invocation_updates or {})
        clone.dispatch_state = "READY"
        clone.dispatch_kind = "ROOT"
        clone.dispatch_session_instance_id = original.dispatch_session_instance_id
        clone.dispatch_celery_task_id = f"i2-fixture-{uuid4().hex}"
        clone.dispatch_invocation = invocation
        clone.dispatch_invocation_sha256 = (
            persisted_invocation_sha256 or dispatch_value_sha256(invocation)
        )
        db.flush()
        if include_witness:
            overrides = dict(witness_overrides or {})
            witness = RequestChangesIssuanceWitness(
                current_task_execution_id=clone.id,
                source_task_execution_id=overrides.get(
                    "source_task_execution_id",
                    original_witness.source_task_execution_id,
                ),
                source_session_id=overrides.get(
                    "source_session_id", original_witness.source_session_id
                ),
                change_set_id=overrides.get(
                    "change_set_id", original_witness.change_set_id
                ),
                bounded_fields_sha256=overrides.get(
                    "bounded_fields_sha256", original_witness.bounded_fields_sha256
                ),
                invocation_sha256=overrides.get(
                    "invocation_sha256", clone.dispatch_invocation_sha256
                ),
                witness_schema_version=overrides.get(
                    "witness_schema_version", original_witness.witness_schema_version
                ),
            )
            db.add(witness)
            db.flush()
        db.commit()
        db.refresh(clone)
        db.expunge(clone)
        return clone
    finally:
        db.close()


def _replay_reason(factory, task_execution_id):
    replay_db = factory()
    try:
        execution = replay_db.get(TaskExecution, task_execution_id)
        arguments, reason = _dispatch_replay_arguments(replay_db, execution)
        assert arguments is None if reason else arguments is not None
        return reason
    finally:
        replay_db.close()


def _publish_replay(factory, task_execution_id):
    replay_db = factory()
    published = []
    try:
        execution = replay_db.get(TaskExecution, task_execution_id)
        execution.dispatch_next_attempt_at = datetime.now(timezone.utc).replace(
            tzinfo=None
        ) - timedelta(seconds=1)
        replay_db.commit()
        identity = (
            execution.id,
            execution.dispatch_celery_task_id,
            execution.dispatch_invocation_sha256,
        )
        result = publish_task_execution_dispatch(
            replay_db,
            task_execution_id,
            publisher=lambda *, kwargs, task_id, retries=0: published.append(
                (kwargs, task_id, retries)
            ),
        )
        final_state = replay_db.get(TaskExecution, task_execution_id).dispatch_state
        return result, published, identity, final_state
    finally:
        replay_db.close()


def _assert_real_worker_refuses(
    monkeypatch, tmp_path, db_session_factory, task, execution, published, reason
):
    result = _run_worker_completion_seam(
        monkeypatch,
        tmp_path,
        db_session_factory=db_session_factory,
        task=task,
        prompt=published["prompt"],
        context=None,
        persisted_task_execution_id=execution.id,
        expected_attempt_number=execution.attempt_number,
        celery_task_id=execution.dispatch_celery_task_id,
    )
    diagnostics = _worker_seam_failure_diagnostics(result)
    assert result.worker_contexts == [], diagnostics
    assert len(result.primary_exceptions) == 1, diagnostics
    assert reason in str(result.primary_exceptions[0])
    assert result.validator_calls == [], diagnostics
    assert result.provider_calls == [], diagnostics
    assert result.executed_commands == [], diagnostics
    assert result.step_loop_calls == [], diagnostics
    return result


# ------------------------------------------------------------ I1-A schema


def test_migration_061_is_registered_recorded_and_exactly_verified(
    db_session_factory,
):
    engine = _bind(db_session_factory)
    assert MIGRATIONS[-1].version == "061_request_changes_issuance_witness"
    assert MIGRATIONS[-1].verify is _verify_061_request_changes_issuance_witness
    with engine.connect() as connection:
        recorded = connection.execute(
            text("SELECT version FROM schema_migrations WHERE version = :v"),
            {"v": "061_request_changes_issuance_witness"},
        ).fetchall()
        triggers = dict(
            connection.exec_driver_sql(
                "SELECT name, sql FROM sqlite_master WHERE type='trigger' "
                f"AND tbl_name='{WITNESS_TABLE}'"
            ).fetchall()
        )
        keys = connection.exec_driver_sql(
            f"PRAGMA foreign_key_list({WITNESS_TABLE})"
        ).fetchall()
    assert len(recorded) == 1
    assert set(triggers) == set(_061_trigger_statements())
    assert {(row[3], row[2], str(row[6]).upper()) for row in keys} == {
        ("current_task_execution_id", "task_executions", "RESTRICT"),
        ("source_task_execution_id", "task_executions", "RESTRICT"),
        ("source_session_id", "sessions", "RESTRICT"),
        ("change_set_id", "task_execution_change_sets", "RESTRICT"),
    }
    pk = inspect(engine).get_pk_constraint(WITNESS_TABLE)
    assert pk["constrained_columns"] == ["current_task_execution_id"]
    _verify_061_request_changes_issuance_witness(engine)


@pytest.mark.parametrize(
    "drift",
    (
        "DROP TRIGGER trg_rc_issuance_witness_no_update",
        "DROP TRIGGER trg_rc_issuance_witness_no_delete",
        "DROP INDEX ix_rc_issuance_witness_change_set",
    ),
)
def test_verify_061_detects_schema_drift(db_session_factory, drift):
    engine = _bind(db_session_factory)
    with engine.begin() as connection:
        connection.exec_driver_sql(drift)
    with pytest.raises(RuntimeError, match="061"):
        _verify_061_request_changes_issuance_witness(engine)


def test_migration_061_creates_table_without_backfill_and_is_idempotent(
    tmp_path: Path,
):
    engine = _engine(tmp_path / "pre-061.db")
    try:
        Base.metadata.create_all(
            bind=engine,
            tables=[
                table
                for table in Base.metadata.sorted_tables
                if table.name != WITNESS_TABLE
            ],
        )
        factory = sessionmaker(autoflush=False, bind=engine)
        db = factory()
        project = Project(name="pre-061", workspace_path=str(tmp_path))
        db.add(project)
        db.flush()
        task = Task(
            project_id=project.id,
            title="legacy",
            description=OPERATOR_DESCRIPTION,
            status=TaskStatus.DONE,
            workspace_status="changes_requested",
            promotion_note=NOTE,
        )
        db.add(task)
        db.commit()
        _add_prior_change_set(db, task)
        db.close()
        assert WITNESS_TABLE not in inspect(engine).get_table_names()

        run_schema_migrations(engine)
        run_schema_migrations(engine)

        assert WITNESS_TABLE in inspect(engine).get_table_names()
        with engine.connect() as connection:
            assert (
                connection.exec_driver_sql(
                    f"SELECT COUNT(*) FROM {WITNESS_TABLE}"
                ).scalar()
                == 0
            )
        _verify_061_request_changes_issuance_witness(engine)
    finally:
        engine.dispose()


def test_migration_061_refuses_non_sqlite(monkeypatch):
    import app.db_migrations as db_migrations

    monkeypatch.setattr(
        db_migrations,
        "_table_names",
        lambda _engine: set(db_migrations._061_OWNER_TABLES),
    )
    fake = SimpleNamespace(dialect=SimpleNamespace(name="postgresql"))
    with pytest.raises(RuntimeError, match="SQLite only"):
        _migration_061_request_changes_issuance_witness(fake)
    with pytest.raises(RuntimeError, match="SQLite only"):
        _verify_061_request_changes_issuance_witness(fake)


# ------------------------------------------------- I1-B/C positive issuance


def test_same_session_issuance_commits_exact_witness_before_publication(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    observed = []

    def _observe(kwargs, _options):
        execution = _fresh(
            db_session_factory, TaskExecution, kwargs["task_execution_id"]
        )
        observed.append((execution.dispatch_state, _witness_rows(db_session_factory)))

    published = _stub_retry_publication(monkeypatch, on_publish=_observe)
    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry", json=SAME_SESSION
    )

    assert response.status_code == 200, response.text
    execution = _fresh(
        db_session_factory, TaskExecution, response.json()["task_execution_id"]
    )
    expected = _expected_witness(execution, prior)
    assert observed == [("READY", [expected])]
    assert _witness_rows(db_session_factory) == [expected]
    assert execution.dispatch_kind == "ROOT"
    assert execution.dispatch_parent_task_execution_id is None
    assert execution.dispatch_invocation_sha256 == dispatch_value_sha256(
        execution.dispatch_invocation
    )
    assert published["prompt"] == OPERATOR_DESCRIPTION
    assert published["published_task_id"] == execution.dispatch_celery_task_id
    _assert_authority_inputs_intact(db_session_factory, task.id)


def test_new_session_issuance_is_cross_session_and_keeps_p1_archive(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)

    response, execution, published = _issue(
        authenticated_client, db_session_factory, task, prior, monkeypatch, NEW_SESSION
    )

    assert response.json()["repair_archive_result"]["archived"] is True
    assert execution.session_id != prior.session_id
    assert _witness_rows(db_session_factory) == [_expected_witness(execution, prior)]
    assert _witness_rows(db_session_factory)[0]["source_session_id"] == (
        prior.session_id
    )
    assert execution.dispatch_kind == "ROOT"
    assert execution.dispatch_parent_task_execution_id is None
    assert published["prompt"] == OPERATOR_DESCRIPTION
    _assert_authority_inputs_intact(db_session_factory, task.id)


def test_ordinary_retry_creates_no_witness(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)
    _add_prior_change_set(db_session, task)
    published = _stub_retry_publication(monkeypatch)

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry", json={"retry_intent": "ordinary"}
    )

    assert response.status_code == 200, response.text
    execution = _fresh(
        db_session_factory, TaskExecution, response.json()["task_execution_id"]
    )
    assert execution.dispatch_invocation["acceptance_authority"]["acceptance_mode"] == (
        "ordinary"
    )
    assert _witness_rows(db_session_factory) == []
    assert published["published_task_id"]


def test_later_change_set_does_not_rewrite_pinned_witness(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    _, execution, published = _issue(
        authenticated_client, db_session_factory, task, prior, monkeypatch, SAME_SESSION
    )
    pinned = _witness_rows(db_session_factory)
    assert execution.session_id == pinned[0]["source_session_id"]

    newer = _add_prior_change_set(db_session, task)

    assert newer.id != prior.id
    assert _witness_rows(db_session_factory) == pinned
    assert pinned[0]["change_set_id"] == prior.id

    replayed = []
    replay_db = db_session_factory()
    try:
        row = replay_db.get(TaskExecution, execution.id)
        row.dispatch_next_attempt_at = datetime.now(timezone.utc).replace(
            tzinfo=None
        ) - timedelta(seconds=1)
        replay_db.commit()
        result = publish_task_execution_dispatch(
            replay_db,
            execution.id,
            publisher=lambda *, kwargs, task_id, retries=0: replayed.append(
                (kwargs, task_id, retries)
            ),
        )
    finally:
        replay_db.close()
    assert result["state"] == "published_unclaimed"
    assert replayed[0][0]["task_execution_id"] == execution.id
    assert replayed[0][1] == execution.dispatch_celery_task_id
    assert replayed[0][0]["prompt"] == OPERATOR_DESCRIPTION
    assert _witness_rows(db_session_factory) == pinned

    worker_result = _run_worker_completion_seam(
        monkeypatch,
        tmp_path / "later-change-set-p1-worker",
        db_session_factory=db_session_factory,
        task=task,
        prompt=published["prompt"],
        context=None,
        persisted_task_execution_id=execution.id,
        expected_attempt_number=2,
        celery_task_id=execution.dispatch_celery_task_id,
    )
    diagnostics = _worker_seam_failure_diagnostics(worker_result)
    assert len(worker_result.worker_contexts) == 1, diagnostics
    assert (
        worker_result.worker_contexts[0].acceptance_authority_status
        == "verified_request_changes"
    )
    assert worker_result.provider_calls == [], diagnostics
    assert worker_result.executed_commands == [
        "/offline/python -m pytest -q tests/test_operator.py"
    ], diagnostics


def test_new_session_request_changes_retry_verifies_source_witness(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    _, execution, published = _issue(
        authenticated_client, db_session_factory, task, prior, monkeypatch, NEW_SESSION
    )
    assert execution.session_id != prior.session_id
    assert execution.dispatch_kind == "ROOT"
    assert execution.dispatch_parent_task_execution_id is None
    assert _replay_reason(db_session_factory, execution.id) is None

    result = _run_worker_completion_seam(
        monkeypatch,
        tmp_path / "new-session-witness-worker",
        db_session_factory=db_session_factory,
        task=task,
        prompt=published["prompt"],
        context=None,
        persisted_task_execution_id=execution.id,
        expected_attempt_number=execution.attempt_number,
        celery_task_id=execution.dispatch_celery_task_id,
    )
    diagnostics = _worker_seam_failure_diagnostics(result)
    assert len(result.worker_contexts) == 1, diagnostics
    assert (
        result.worker_contexts[0].acceptance_authority_status
        == "verified_request_changes"
    )
    assert result.provider_calls == [], diagnostics


@pytest.mark.parametrize("substitution", ("same_attempt", "cross_attempt"))
def test_rehashed_change_set_substitution_refuses_real_worker_and_replay(
    authenticated_client,
    db_session,
    db_session_factory,
    tmp_path,
    monkeypatch,
    substitution,
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    _, execution, published = _issue(
        authenticated_client, db_session_factory, task, prior, monkeypatch, SAME_SESSION
    )
    if substitution == "same_attempt":
        clone = _clone_retry_execution(
            db_session_factory,
            execution.id,
            same_attempt_change_set=True,
        )
    else:
        substituted = _add_prior_change_set(db_session, task)
        record = TaskService(db_session).get_task_execution_change_set(
            task_execution_id=substituted.task_execution_id
        )
        fields = TaskService.request_changes_change_set_fields(record)
        clone = _clone_retry_execution(
            db_session_factory,
            execution.id,
            invocation_prior={
                "change_set_id": substituted.id,
                "task_execution_id": substituted.task_execution_id,
                "bounded_fields_sha256": dispatch_value_sha256(fields),
            },
        )

    # 061's dispatch guard prevents rewriting a READY row. This inserts a
    # malformed attempt and inconsistent witness once, with constraints live,
    # to exercise the verifier even if corrupted state predates enforcement.
    assert _replay_reason(db_session_factory, clone.id) == (
        "request_changes_witness_invocation_change_set_mismatch"
    )
    clone_published = {
        "prompt": published["prompt"],
        "published_task_id": clone.dispatch_celery_task_id,
    }
    _assert_real_worker_refuses(
        monkeypatch,
        tmp_path / f"{substitution}-substitution-worker",
        db_session_factory,
        task,
        clone,
        clone_published,
        "request_changes_witness_invocation_change_set_mismatch",
    )


@pytest.mark.parametrize(
    ("mutation", "replay_reason", "worker_reason"),
    (
        (
            "bounded_fields",
            "request_changes_witness_change_set_fields_changed",
            "request_changes_witness_change_set_fields_changed",
        ),
        (
            "description",
            "task_prompt_reference_changed",
            "broker_prompt_differs_from_persisted_reference",
        ),
        (
            "promotion_note",
            "acceptance_request_changes_source_changed",
            "request_changes_source_changed",
        ),
    ),
)
def test_persisted_source_mutations_refuse_worker_and_replay(
    authenticated_client,
    db_session,
    db_session_factory,
    tmp_path,
    monkeypatch,
    mutation,
    replay_reason,
    worker_reason,
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    _, execution, published = _issue(
        authenticated_client, db_session_factory, task, prior, monkeypatch, SAME_SESSION
    )
    if mutation == "bounded_fields":
        prior.added_files = ["rewritten-after-issuance.py"]
    elif mutation == "description":
        task.description = OPERATOR_DESCRIPTION + " edited after issuance"
    elif mutation == "promotion_note":
        task.promotion_note = NOTE + " edited after issuance"
    db_session.commit()

    actual_replay_reason = _replay_reason(db_session_factory, execution.id)
    assert replay_reason in actual_replay_reason
    _assert_real_worker_refuses(
        monkeypatch,
        tmp_path / f"{mutation}-mutation-worker",
        db_session_factory,
        task,
        execution,
        published,
        worker_reason,
    )


def test_bounded_change_set_mutation_with_rehashed_invocation_still_refuses(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    _, issued, published = _issue(
        authenticated_client, db_session_factory, task, prior, monkeypatch, SAME_SESSION
    )

    prior.added_files = ["rewritten-after-issuance.py"]
    db_session.commit()
    mutated_record = TaskService(db_session).get_task_execution_change_set(
        task_execution_id=prior.task_execution_id
    )
    mutated_fields_digest = dispatch_value_sha256(
        TaskService.request_changes_change_set_fields(mutated_record)
    )
    # Model an attacker who updates the invocation pin and recomputes its
    # digest. The independent insert-only witness remains bound to issuance.
    execution = _clone_retry_execution(
        db_session_factory,
        issued.id,
        invocation_prior={
            "change_set_id": prior.id,
            "task_execution_id": prior.task_execution_id,
            "bounded_fields_sha256": mutated_fields_digest,
        },
    )

    expected_reason = "request_changes_witness_invocation_change_set_mismatch"
    assert _replay_reason(db_session_factory, execution.id) == expected_reason
    _assert_real_worker_refuses(
        monkeypatch,
        tmp_path / "bounded-fields-rehashed-worker",
        db_session_factory,
        task,
        execution,
        {
            "prompt": published["prompt"],
            "published_task_id": execution.dispatch_celery_task_id,
        },
        expected_reason,
    )


def test_invocation_digest_mismatch_refuses_worker_and_replay(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    _, issued, published = _issue(
        authenticated_client, db_session_factory, task, prior, monkeypatch, SAME_SESSION
    )
    execution = _clone_retry_execution(
        db_session_factory,
        issued.id,
        invocation_updates={"timeout_seconds": 2},
        persisted_invocation_sha256=issued.dispatch_invocation_sha256,
        witness_overrides={"invocation_sha256": issued.dispatch_invocation_sha256},
    )
    assert _replay_reason(db_session_factory, execution.id) == (
        "invocation_digest_mismatch"
    )
    _assert_real_worker_refuses(
        monkeypatch,
        tmp_path / "invocation-digest-mismatch-worker",
        db_session_factory,
        task,
        execution,
        {
            "prompt": published["prompt"],
            "published_task_id": execution.dispatch_celery_task_id,
        },
        "invocation_digest_mismatch",
    )


def test_migration_dispatch_trigger_rejects_invocation_digest_rewrite(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    _, execution, _ = _issue(
        authenticated_client, db_session_factory, task, prior, monkeypatch, SAME_SESSION
    )
    current = db_session.get(TaskExecution, execution.id)
    original_invocation = deepcopy(current.dispatch_invocation)
    original_digest = current.dispatch_invocation_sha256
    invocation = deepcopy(original_invocation)
    invocation["timeout_seconds"] = 2
    current.dispatch_invocation = invocation
    current.dispatch_invocation_sha256 = dispatch_value_sha256(invocation)

    with pytest.raises(IntegrityError, match="dispatch identity is immutable"):
        db_session.commit()
    db_session.rollback()
    persisted = _fresh(db_session_factory, TaskExecution, execution.id)
    assert persisted.dispatch_invocation == original_invocation
    assert persisted.dispatch_invocation_sha256 == original_digest
    assert _replay_reason(db_session_factory, execution.id) is None


def test_unsupported_witness_version_refuses_worker_and_replay(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    # Version 2 satisfies migration 061's positive-version CHECK but is not a
    # version supported by this verifier. Insert-only triggers remain active.
    _, issued, published = _issue(
        authenticated_client, db_session_factory, task, prior, monkeypatch, SAME_SESSION
    )
    execution = _clone_retry_execution(
        db_session_factory,
        issued.id,
        witness_overrides={"witness_schema_version": 2},
    )
    assert _replay_reason(db_session_factory, execution.id) == (
        "request_changes_witness_schema_unsupported"
    )
    _assert_real_worker_refuses(
        monkeypatch,
        tmp_path / "unsupported-witness-worker",
        db_session_factory,
        task,
        execution,
        {
            "prompt": published["prompt"],
            "published_task_id": execution.dispatch_celery_task_id,
        },
        "request_changes_witness_schema_unsupported",
    )


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    (
        ("wrong_source_session", "request_changes_witness_source_session_invalid"),
        (
            "wrong_change_set_id",
            "request_changes_witness_invocation_change_set_mismatch",
        ),
    ),
)
def test_wrong_source_session_and_change_set_id_refuse_replay_and_worker(
    authenticated_client,
    db_session,
    db_session_factory,
    tmp_path,
    monkeypatch,
    case,
    expected_reason,
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    _, issued, published = _issue(
        authenticated_client, db_session_factory, task, prior, monkeypatch, SAME_SESSION
    )
    if case == "wrong_source_session":
        wrong_session = SessionModel(
            project_id=task.project_id,
            name=f"wrong-source-session-{uuid4().hex}",
            status="completed",
            instance_id=str(uuid4()),
        )
        db_session.add(wrong_session)
        db_session.commit()
        witness_overrides = {"source_session_id": wrong_session.id}
    else:
        wrong_change_set = _add_prior_change_set(db_session, task)
        witness_overrides = {"change_set_id": wrong_change_set.id}
    execution = _clone_retry_execution(
        db_session_factory,
        issued.id,
        witness_overrides=witness_overrides,
    )
    assert _replay_reason(db_session_factory, execution.id) == expected_reason
    _assert_real_worker_refuses(
        monkeypatch,
        tmp_path / f"{case}-worker",
        db_session_factory,
        task,
        execution,
        {
            "prompt": published["prompt"],
            "published_task_id": execution.dispatch_celery_task_id,
        },
        expected_reason,
    )


@pytest.mark.parametrize(
    ("case", "expected_reason"),
    (
        ("wrong_source_execution", "request_changes_witness_source_execution_invalid"),
        (
            "wrong_change_set_id",
            "request_changes_witness_invocation_change_set_mismatch",
        ),
        ("wrong_task", "request_changes_witness_change_set_provenance_invalid"),
        ("wrong_project", "request_changes_witness_change_set_provenance_invalid"),
    ),
)
def test_witness_source_identity_relationships_refuse_worker_and_replay(
    authenticated_client,
    db_session,
    db_session_factory,
    tmp_path,
    monkeypatch,
    case,
    expected_reason,
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    _, issued, published = _issue(
        authenticated_client, db_session_factory, task, prior, monkeypatch, SAME_SESSION
    )
    invocation_prior = None
    witness_overrides = None
    if case == "wrong_source_execution":
        other_root = tmp_path / "wrong-source-task"
        other_root.mkdir()
        other_task = _create_task(db_session, other_root, OPERATOR_DESCRIPTION)
        foreign_source = _add_prior_change_set(db_session, other_task)
        pinned = _witness_rows(db_session_factory)[0]
        invocation_prior = {
            "change_set_id": pinned["change_set_id"],
            "task_execution_id": foreign_source.task_execution_id,
            "bounded_fields_sha256": pinned["bounded_fields_sha256"],
        }
        witness_overrides = {
            "source_task_execution_id": foreign_source.task_execution_id,
            "source_session_id": foreign_source.session_id,
        }
    elif case == "wrong_change_set_id":
        alternate = _add_prior_change_set(db_session, task)
        witness_overrides = {"change_set_id": alternate.id}
    elif case == "wrong_task":
        other_root = tmp_path / "other-task"
        other_root.mkdir()
        other_task = _create_task(db_session, other_root, OPERATOR_DESCRIPTION)
        prior.task_id = other_task.id
        db_session.commit()
    else:
        other_project = Project(
            name=f"wrong-project-{uuid4().hex}",
            workspace_path=str(tmp_path / "wrong-project-root"),
        )
        db_session.add(other_project)
        db_session.flush()
        prior.project_id = other_project.id
        db_session.commit()

    execution = _clone_retry_execution(
        db_session_factory,
        issued.id,
        invocation_prior=invocation_prior,
        witness_overrides=witness_overrides,
    )
    assert _replay_reason(db_session_factory, execution.id) == expected_reason
    _assert_real_worker_refuses(
        monkeypatch,
        tmp_path / f"{case}-witness-worker",
        db_session_factory,
        task,
        execution,
        {
            "prompt": published["prompt"],
            "published_task_id": execution.dispatch_celery_task_id,
        },
        expected_reason,
    )


def test_missing_witness_wrong_current_execution_refuses_worker_and_replay(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    _, execution, published = _issue(
        authenticated_client, db_session_factory, task, prior, monkeypatch, SAME_SESSION
    )
    worker_clone = _clone_retry_execution(
        db_session_factory,
        execution.id,
        include_witness=False,
    )
    replay_clone = _clone_retry_execution(
        db_session_factory,
        execution.id,
        include_witness=False,
    )

    assert _replay_reason(db_session_factory, worker_clone.id) == (
        "request_changes_witness_missing"
    )
    result, published_replays, identity, final_state = _publish_replay(
        db_session_factory, replay_clone.id
    )
    assert result["state"] == "refused"
    assert result["reason"] == "request_changes_witness_missing"
    assert published_replays == []
    assert final_state == "REFUSED"
    assert identity == (
        replay_clone.id,
        replay_clone.dispatch_celery_task_id,
        replay_clone.dispatch_invocation_sha256,
    )
    assert len(_witness_rows(db_session_factory)) == 1

    _assert_real_worker_refuses(
        monkeypatch,
        tmp_path / "wrong-current-execution-worker",
        db_session_factory,
        task,
        worker_clone,
        {
            "prompt": published["prompt"],
            "published_task_id": worker_clone.dispatch_celery_task_id,
        },
        "request_changes_witness_missing",
    )


def test_worker_rejects_mismatched_task_execution_before_acceptance(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    _, execution, published = _issue(
        authenticated_client, db_session_factory, task, prior, monkeypatch, SAME_SESSION
    )
    result = _run_worker_completion_seam(
        monkeypatch,
        tmp_path / "mismatched-worker-execution",
        db_session_factory=db_session_factory,
        task=task,
        prompt=published["prompt"],
        context=None,
        persisted_task_execution_id=execution.id,
        expected_attempt_number=execution.attempt_number,
        celery_task_id=execution.dispatch_celery_task_id,
        worker_task_execution_id=execution.id + 1000,
    )
    diagnostics = _worker_seam_failure_diagnostics(result)
    assert result.worker_contexts == [], diagnostics
    assert result.validator_calls == [], diagnostics
    assert result.provider_calls == [], diagnostics
    assert result.executed_commands == [], diagnostics


@pytest.mark.parametrize("row_kind", ("change_set", "source_execution"))
def test_migration_061_foreign_keys_reject_missing_pinned_source_rows(
    authenticated_client,
    db_session,
    db_session_factory,
    tmp_path,
    monkeypatch,
    row_kind,
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    _, execution, _ = _issue(
        authenticated_client, db_session_factory, task, prior, monkeypatch, SAME_SESSION
    )
    if row_kind == "change_set":
        db_session.delete(prior)
    else:
        source = db_session.get(TaskExecution, prior.task_execution_id)
        db_session.delete(source)
    with pytest.raises(IntegrityError):
        db_session.commit()
    db_session.rollback()
    assert _witness_rows(db_session_factory)
    observer = db_session_factory()
    try:
        assert observer.get(TaskExecutionChangeSet, prior.id) is not None
        assert observer.get(TaskExecution, prior.task_execution_id) is not None
    finally:
        observer.close()


# ------------------------------------------------- I1-E issuance negatives


def test_missing_source_change_set_refuses_issuance(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)
    changed = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/request-changes", json={"note": NOTE}
    )
    assert changed.status_code == 200, changed.text
    published = _stub_retry_publication(monkeypatch)

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry", json=SAME_SESSION
    )

    assert response.status_code == 409
    assert "requires a prior ChangeSet" in response.json()["detail"]
    assert _execution_ids(db_session_factory, task.id) == []
    assert _witness_rows(db_session_factory) == []
    assert published == {}
    _assert_review_intact(db_session_factory, task.id)


def test_new_session_missing_change_set_refuses_before_archive(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)
    changed = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/request-changes", json={"note": NOTE}
    )
    assert changed.status_code == 200, changed.text
    published = _stub_retry_publication(monkeypatch)

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry", json=NEW_SESSION
    )

    assert response.status_code == 409
    assert "requires a prior ChangeSet" in response.json()["detail"]
    assert (tmp_path / "project" / "task-review").exists()
    assert _fresh(db_session_factory, Task, task.id).task_subfolder == "task-review"
    assert _execution_ids(db_session_factory, task.id) == []
    assert _witness_rows(db_session_factory) == []
    assert published == {}


def _corrupt_source(db_session, task, prior, corruption):
    record = db_session.get(TaskExecutionChangeSet, prior.id)
    if corruption == "source-execution-other-task":
        other = Task(
            project_id=task.project_id,
            title="other",
            description="other",
            status=TaskStatus.DONE,
        )
        db_session.add(other)
        db_session.flush()
        other_execution = TaskExecution(
            session_id=record.session_id,
            task_id=other.id,
            attempt_number=1,
            status=TaskStatus.DONE,
        )
        db_session.add(other_execution)
        db_session.flush()
        record.task_execution_id = other_execution.id
    elif corruption == "contradictory-source-session":
        other_session = SessionModel(
            project_id=task.project_id,
            name=f"contradictory-{uuid4().hex}",
            status="completed",
            instance_id=str(uuid4()),
        )
        db_session.add(other_session)
        db_session.flush()
        record.session_id = other_session.id
    elif corruption == "null-source-session":
        record.session_id = None
    db_session.commit()


@pytest.mark.parametrize(
    "corruption",
    (
        "source-execution-other-task",
        "contradictory-source-session",
        "null-source-session",
    ),
)
def test_invalid_source_provenance_refuses_issuance(
    authenticated_client,
    db_session,
    db_session_factory,
    tmp_path,
    monkeypatch,
    corruption,
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    expected_ids = _execution_ids(db_session_factory, task.id)
    _corrupt_source(db_session, task, prior, corruption)
    published = _stub_retry_publication(monkeypatch)

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry", json=SAME_SESSION
    )

    assert response.status_code == 409
    assert "witness cannot be persisted" in response.json()["detail"]
    assert _execution_ids(db_session_factory, task.id) == expected_ids
    assert _witness_rows(db_session_factory) == []
    assert published == {}
    _assert_review_intact(db_session_factory, task.id)


def test_unsupported_database_dialect_refuses_before_any_mutation(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    published = _stub_retry_publication(monkeypatch)
    monkeypatch.setattr(_bind(db_session_factory).dialect, "name", "postgresql")

    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry", json=NEW_SESSION
    )

    monkeypatch.undo()
    assert response.status_code == 409
    assert response.json()["detail"].startswith("UNSUPPORTED_DATABASE")
    assert (tmp_path / "project" / "task-review").exists()
    _assert_no_issuance(db_session_factory, task, prior, published)


def test_sqlite_lock_contention_refuses_without_partial_issuance(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    published = _stub_retry_publication(monkeypatch)
    blocker = _bind(db_session_factory).raw_connection()
    try:
        blocker.cursor().execute("BEGIN IMMEDIATE")
        started = time.monotonic()
        response = authenticated_client.post(
            f"/api/v1/tasks/{task.id}/retry", json=SAME_SESSION
        )
        waited = time.monotonic() - started
    finally:
        blocker.rollback()
        blocker.close()

    assert response.status_code == 409
    assert "write lock" in response.json()["detail"]
    assert waited >= LOCK_TIMEOUT_SECONDS * 0.9
    _assert_no_issuance(db_session_factory, task, prior, published)

    # Once the competing writer is gone the same review issues normally.
    _, execution, _ = _issue(
        authenticated_client, db_session_factory, task, prior, monkeypatch, SAME_SESSION
    )
    assert _witness_rows(db_session_factory) == [_expected_witness(execution, prior)]


def test_invocation_commit_failure_leaves_no_execution_or_witness(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    published = _stub_retry_publication(monkeypatch)
    real_writer = TaskService.record_request_changes_issuance_witness

    def _write_then_break_commit(self, **kwargs):
        witness = real_writer(self, **kwargs)

        def _fail_commit():
            raise RuntimeError("offline commit failure")

        monkeypatch.setattr(self.db, "commit", _fail_commit)
        return witness

    monkeypatch.setattr(
        TaskService, "record_request_changes_issuance_witness", _write_then_break_commit
    )
    with pytest.raises(RuntimeError, match="offline commit failure"):
        authenticated_client.post(f"/api/v1/tasks/{task.id}/retry", json=SAME_SESSION)

    _assert_no_issuance(db_session_factory, task, prior, published)


@pytest.mark.parametrize("failure", ("value-error", "foreign-key"))
def test_witness_insertion_failure_rolls_back_invocation(
    authenticated_client,
    db_session,
    db_session_factory,
    tmp_path,
    monkeypatch,
    failure,
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    published = _stub_retry_publication(monkeypatch)
    real_writer = TaskService.record_request_changes_issuance_witness

    def _failing_writer(self, **kwargs):
        if failure == "value-error":
            raise ValueError("injected witness refusal")
        # A real database-level failure: the invocation has been flushed and
        # the witness insert violates the RESTRICT foreign key.
        self.db.add(
            RequestChangesIssuanceWitness(
                current_task_execution_id=kwargs["task_execution"].id,
                source_task_execution_id=prior.task_execution_id,
                source_session_id=prior.session_id,
                change_set_id=10**9,
                bounded_fields_sha256="0" * 64,
                invocation_sha256="0" * 64,
                witness_schema_version=1,
            )
        )
        self.db.flush()
        return real_writer(self, **kwargs)

    monkeypatch.setattr(
        TaskService, "record_request_changes_issuance_witness", _failing_writer
    )
    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry", json=SAME_SESSION
    )

    assert response.status_code == 409
    assert "witness cannot be persisted" in response.json()["detail"]
    _assert_no_issuance(db_session_factory, task, prior, published)


def test_publication_failure_keeps_execution_and_witness_for_replay(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)

    def _fail(_kwargs, _options):
        raise RuntimeError("offline publisher failure")

    published = _stub_retry_publication(monkeypatch, on_publish=_fail)
    response = authenticated_client.post(
        f"/api/v1/tasks/{task.id}/retry", json=SAME_SESSION
    )

    assert response.status_code == 200, response.text
    execution_id = response.json()["task_execution_id"]
    execution = _fresh(db_session_factory, TaskExecution, execution_id)
    assert execution.dispatch_state == "READY"
    assert execution.dispatch_last_error == "publisher_error:RuntimeError"
    witness = _witness_rows(db_session_factory)
    assert witness == [_expected_witness(execution, prior)]
    assert published["published_task_id"] == execution.dispatch_celery_task_id

    replayed = []
    db = db_session_factory()
    try:
        row = db.get(TaskExecution, execution_id)
        row.dispatch_next_attempt_at = datetime.now(timezone.utc).replace(
            tzinfo=None
        ) - timedelta(seconds=1)
        db.commit()
        result = publish_task_execution_dispatch(
            db,
            execution_id,
            publisher=lambda *, kwargs, task_id, retries=0: replayed.append(task_id),
        )
    finally:
        db.close()
    assert result["state"] == "published_unclaimed"
    assert replayed == [execution.dispatch_celery_task_id]
    assert _witness_rows(db_session_factory) == witness


def test_duplicate_publication_reuses_execution_and_creates_no_second_witness(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    _, execution, published = _issue(
        authenticated_client, db_session_factory, task, prior, monkeypatch, SAME_SESSION
    )
    witness = _witness_rows(db_session_factory)

    duplicates = []
    db = db_session_factory()
    try:
        row = db.get(TaskExecution, execution.id)
        row.dispatch_next_attempt_at = datetime.now(timezone.utc).replace(
            tzinfo=None
        ) - timedelta(seconds=1)
        db.commit()
        result = publish_task_execution_dispatch(
            db,
            execution.id,
            publisher=lambda *, kwargs, task_id, retries=0: duplicates.append(task_id),
        )
    finally:
        db.close()

    assert result["state"] == "published_unclaimed"
    assert duplicates == [published["published_task_id"]]
    assert _execution_ids(db_session_factory, task.id) == [
        prior.task_execution_id,
        execution.id,
    ]
    assert _witness_rows(db_session_factory) == witness
    assert (
        witness[0]["invocation_sha256"]
        == _fresh(
            db_session_factory, TaskExecution, execution.id
        ).dispatch_invocation_sha256
    )


# --------------------------------------------- I1-E writer-level negatives


def _current_execution(db, task, authority):
    session = SessionModel(
        project_id=task.project_id,
        name=f"current-{uuid4().hex}",
        status="pending",
        instance_id=str(uuid4()),
    )
    db.add(session)
    db.flush()
    execution = create_task_execution(db, session_id=session.id, task_id=task.id)
    assert configure_task_execution_dispatch(
        db,
        task_execution=execution,
        session=session,
        task=task,
        dispatch_kind="ROOT",
        prompt=task.description.strip(),
        timeout_seconds=300,
        acceptance_authority=authority,
    )
    return execution


def _writer_case(db, task, prior):
    service = TaskService(db)
    change_set = service.get_latest_task_change_set_for_task(task.id)
    assert change_set["change_set_id"] == prior.id
    authority = TaskService.build_request_changes_acceptance_authority(
        task=task, acceptance_mode="request_changes", change_set=change_set
    )
    return service, change_set, authority


def test_writer_rejects_wrong_task_change_set(
    authenticated_client, db_session, tmp_path
):
    task, _ = _review_task(authenticated_client, db_session, tmp_path)
    db_session.refresh(task)
    other = Task(
        project_id=task.project_id,
        title="other",
        description="other task",
        status=TaskStatus.DONE,
    )
    db_session.add(other)
    db_session.commit()
    _add_prior_change_set(db_session, other)
    service = TaskService(db_session)
    other_change_set = service.get_latest_task_change_set_for_task(other.id)
    authority = TaskService.build_request_changes_acceptance_authority(
        task=task, acceptance_mode="request_changes", change_set=None
    )
    authority["prior_change_set"] = {
        "change_set_id": other_change_set["change_set_id"],
        "task_execution_id": other_change_set["task_execution_id"],
        "bounded_fields_sha256": dispatch_value_sha256(
            TaskService.request_changes_change_set_fields(other_change_set)
        ),
    }
    execution = _current_execution(db_session, task, authority)

    with pytest.raises(ValueError, match="provenance is invalid"):
        service.record_request_changes_issuance_witness(
            task=task,
            task_execution=execution,
            change_set=other_change_set,
            acceptance_authority=authority,
        )
    db_session.rollback()


def test_writer_rejects_current_execution_self_reference(
    authenticated_client, db_session, tmp_path
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    db_session.refresh(task)
    service, change_set, authority = _writer_case(db_session, task, prior)
    execution = _current_execution(db_session, task, authority)
    same_attempt = TaskExecutionChangeSet(
        project_id=task.project_id,
        task_id=task.id,
        session_id=execution.session_id,
        task_execution_id=execution.id,
        base_snapshot_key=f"same-attempt:{execution.id}",
        added_files=[],
        modified_files=[],
        deleted_files=[],
        warning_flags=[],
        status="completed",
        created_at=datetime.now(timezone.utc) + timedelta(seconds=5),
    )
    db_session.add(same_attempt)
    db_session.flush()
    self_change_set = service.get_task_execution_change_set(
        task_execution_id=execution.id
    )
    self_authority = dict(authority)
    self_authority["prior_change_set"] = {
        "change_set_id": same_attempt.id,
        "task_execution_id": execution.id,
        "bounded_fields_sha256": dispatch_value_sha256(
            TaskService.request_changes_change_set_fields(self_change_set)
        ),
    }

    with pytest.raises(ValueError, match="current TaskExecution is invalid"):
        service.record_request_changes_issuance_witness(
            task=task,
            task_execution=execution,
            change_set=self_change_set,
            acceptance_authority=self_authority,
        )
    db_session.rollback()


@pytest.mark.parametrize(
    "mutation",
    ("missing-change-set", "uppercase-digest", "short-digest", "invocation-mismatch"),
)
def test_writer_rejects_malformed_inputs(
    authenticated_client, db_session, tmp_path, mutation
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    db_session.refresh(task)
    service, change_set, authority = _writer_case(db_session, task, prior)
    execution = _current_execution(db_session, task, authority)
    kwargs = {
        "task": task,
        "task_execution": execution,
        "change_set": change_set,
        "acceptance_authority": authority,
    }
    if mutation == "missing-change-set":
        kwargs["change_set"] = None
        match = "requires a source ChangeSet"
    elif mutation == "invocation-mismatch":
        kwargs["acceptance_authority"] = {
            **authority,
            "promotion_note_sha256": dispatch_value_sha256("forged"),
        }
        match = "invocation authority mismatch"
    else:
        digest = authority["prior_change_set"]["bounded_fields_sha256"]
        digest = digest.upper() if mutation == "uppercase-digest" else digest[:63]
        kwargs["acceptance_authority"] = {
            **authority,
            "prior_change_set": {
                **authority["prior_change_set"],
                "bounded_fields_sha256": digest,
            },
        }
        match = "digest is malformed"

    with pytest.raises(ValueError, match=match):
        service.record_request_changes_issuance_witness(**kwargs)
    db_session.rollback()


def test_writer_duplicate_witness_for_same_execution_is_rejected(
    authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    _, execution, _ = _issue(
        authenticated_client, db_session_factory, task, prior, monkeypatch, SAME_SESSION
    )
    db = db_session_factory()
    try:
        current = db.get(TaskExecution, execution.id)
        task_row = db.get(Task, task.id)
        authority = current.dispatch_invocation["acceptance_authority"]
        change_set = TaskService(db).get_latest_task_change_set_for_task(task.id)
        with pytest.raises(IntegrityError):
            TaskService(db).record_request_changes_issuance_witness(
                task=task_row,
                task_execution=current,
                change_set=change_set,
                acceptance_authority=authority,
            )
        db.rollback()
    finally:
        db.close()
    assert len(_witness_rows(db_session_factory)) == 1


# --------------------------------------------- I1-A/E database constraints


@pytest.fixture
def issued(authenticated_client, db_session, db_session_factory, tmp_path, monkeypatch):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    _, execution, _ = _issue(
        authenticated_client, db_session_factory, task, prior, monkeypatch, SAME_SESSION
    )
    spare = TaskExecution(
        session_id=prior.session_id,
        task_id=task.id,
        attempt_number=9,
        status=TaskStatus.CANCELLED,
    )
    db_session.add(spare)
    db_session.commit()
    return SimpleNamespace(
        task=task,
        prior=prior,
        execution=execution,
        spare_id=spare.id,
        witness=_witness_rows(db_session_factory)[0],
    )


def _raw(factory, sql, params=None):
    with _bind(factory).begin() as connection:
        return connection.execute(text(sql), params or {})


@pytest.mark.parametrize(
    ("column", "value"),
    (
        ("change_set_id", None),
        ("bounded_fields_sha256", "f" * 64),
        ("invocation_sha256", "e" * 64),
        ("source_session_id", None),
        ("witness_schema_version", 2),
    ),
)
def test_database_rejects_witness_update(db_session_factory, issued, column, value):
    if value is None:
        value = issued.witness[column] + 1
    with pytest.raises(IntegrityError, match="immutable"):
        _raw(
            db_session_factory,
            f"UPDATE {WITNESS_TABLE} SET {column} = :v "
            "WHERE current_task_execution_id = :c",
            {"v": value, "c": issued.execution.id},
        )
    assert _witness_rows(db_session_factory) == [issued.witness]


def test_database_rejects_witness_delete(db_session_factory, issued):
    with pytest.raises(IntegrityError, match="retained"):
        _raw(db_session_factory, f"DELETE FROM {WITNESS_TABLE}")
    assert _witness_rows(db_session_factory) == [issued.witness]


def _insert(factory, **overrides):
    row = {
        "current_task_execution_id": overrides.pop("current"),
        "source_task_execution_id": overrides.pop("source"),
        "source_session_id": overrides.pop("session"),
        "change_set_id": overrides.pop("change_set"),
        "bounded_fields_sha256": overrides.pop("bounded", "a" * 64),
        "invocation_sha256": overrides.pop("invocation", "b" * 64),
        "witness_schema_version": overrides.pop("version", 1),
    }
    assert not overrides
    columns = ", ".join(row)
    values = ", ".join(f":{name}" for name in row)
    _raw(factory, f"INSERT INTO {WITNESS_TABLE} ({columns}) VALUES ({values})", row)


@pytest.mark.parametrize(
    ("case", "match"),
    (
        ("duplicate", "UNIQUE|PRIMARY"),
        ("self-reference", "ck_rc_issuance_witness_distinct_execution|CHECK"),
        ("missing-change-set-fk", "FOREIGN KEY"),
        ("missing-session-fk", "FOREIGN KEY"),
        ("uppercase-digest", "CHECK"),
        ("short-digest", "CHECK"),
        ("version-zero", "CHECK"),
    ),
)
def test_database_rejects_invalid_witness_insert(
    db_session_factory, issued, case, match
):
    base = {
        "current": issued.spare_id,
        "source": issued.prior.task_execution_id,
        "session": issued.prior.session_id,
        "change_set": issued.prior.id,
    }
    if case == "duplicate":
        base["current"] = issued.execution.id
    elif case == "self-reference":
        base["source"] = issued.spare_id
    elif case == "missing-change-set-fk":
        base["change_set"] = 10**9
    elif case == "missing-session-fk":
        base["session"] = 10**9
    elif case == "uppercase-digest":
        base["bounded"] = "A" * 64
    elif case == "short-digest":
        base["invocation"] = "b" * 63
    elif case == "version-zero":
        base["version"] = 0

    with pytest.raises(IntegrityError, match=match):
        _insert(db_session_factory, **base)
    assert _witness_rows(db_session_factory) == [issued.witness]


def test_restrictive_foreign_keys_block_referenced_row_deletion(
    db_session_factory, issued
):
    for sql in (
        "DELETE FROM task_execution_change_sets WHERE id = :i",
        "DELETE FROM task_executions WHERE id = :i",
    ):
        ident = (
            issued.prior.id if "change_sets" in sql else issued.prior.task_execution_id
        )
        with pytest.raises(IntegrityError, match="FOREIGN KEY"):
            _raw(db_session_factory, sql, {"i": ident})
    assert _witness_rows(db_session_factory) == [issued.witness]


# ---------------------------------------------------- I1-D retained evidence


def test_task_delete_with_witness_returns_retained_evidence_conflict(
    authenticated_client, db_session_factory, issued
):
    response = authenticated_client.delete(f"/api/v1/tasks/{issued.task.id}")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "RETAINED_EXECUTION_EVIDENCE_CONFLICT"
    assert _fresh(db_session_factory, Task, issued.task.id) is not None
    assert _witness_rows(db_session_factory) == [issued.witness]


def test_task_delete_foreign_key_race_is_409_not_500(
    authenticated_client, db_session_factory, issued, monkeypatch
):
    import app.services.tasks.task_deletion as task_deletion

    # Bypass the preflight so the delete reaches the RESTRICT foreign key.
    monkeypatch.setattr(
        task_deletion, "require_no_retained_execution_evidence", lambda *a, **k: None
    )
    response = authenticated_client.delete(f"/api/v1/tasks/{issued.task.id}")

    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "RETAINED_EXECUTION_EVIDENCE_CONFLICT"
    assert _fresh(db_session_factory, Task, issued.task.id) is not None
    assert _witness_rows(db_session_factory) == [issued.witness]


def test_project_delete_and_purge_with_witness_return_conflict(
    authenticated_client, db_session, db_session_factory, issued, monkeypatch
):
    from app.api.v1.endpoints import projects

    project_id = issued.task.project_id
    response = authenticated_client.delete(f"/api/v1/projects/{project_id}")
    assert response.status_code == 409
    assert response.json()["detail"]["code"] == "RETAINED_EXECUTION_EVIDENCE_CONFLICT"

    project = db_session.get(Project, project_id)
    project.deleted_at = datetime.now(timezone.utc) - timedelta(days=365)
    db_session.commit()
    purge = authenticated_client.delete("/api/v1/projects/purge-soft-deleted")
    assert purge.status_code == 409
    assert purge.json()["detail"]["code"] == "RETAINED_EXECUTION_EVIDENCE_CONFLICT"

    # Purge race: preflight passes once, the FK then fails, and the handler
    # still reports the retained-evidence conflict rather than a 500.
    real_exists = projects.retained_execution_evidence_exists
    calls = []

    def _allow_preflight_once(db, **scope):
        calls.append(scope)
        return False if len(calls) == 1 else real_exists(db, **scope)

    monkeypatch.setattr(
        projects, "retained_execution_evidence_exists", _allow_preflight_once
    )
    raced = authenticated_client.delete("/api/v1/projects/purge-soft-deleted")
    assert raced.status_code == 409
    assert raced.json()["detail"]["code"] == "RETAINED_EXECUTION_EVIDENCE_CONFLICT"
    assert len(calls) >= 2
    assert _fresh(db_session_factory, Project, project_id) is not None
    assert _witness_rows(db_session_factory) == [issued.witness]


def test_task_without_witness_remains_deletable(
    authenticated_client, db_session, db_session_factory, tmp_path
):
    task = _create_task(db_session, tmp_path, OPERATOR_DESCRIPTION)
    _add_prior_change_set(db_session, task)

    response = authenticated_client.delete(f"/api/v1/tasks/{task.id}")

    assert response.status_code == 204, response.text
    assert _fresh(db_session_factory, Task, task.id) is None
    assert _execution_ids(db_session_factory, task.id) == []


# ------------------------------------------------------- I1-F concurrency


@pytest.mark.parametrize("round_number", (1, 2, 3))
def test_concurrent_issuance_admits_exactly_one_witnessed_retry(
    authenticated_client,
    db_session,
    db_session_factory,
    tmp_path,
    monkeypatch,
    round_number,
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    publish_observations = []

    def _observe(kwargs, _options):
        publish_observations.append(
            (kwargs["task_execution_id"], _witness_rows(db_session_factory))
        )

    published = _stub_retry_publication(monkeypatch, on_publish=_observe)
    real_build = TaskService.build_request_changes_acceptance_authority
    holders = []

    def _slow_build(**kwargs):
        # Called only under BEGIN IMMEDIATE: hold the writer lock long enough
        # that the competing request must wait on it.
        holders.append(threading.get_ident())
        time.sleep(0.4)
        return real_build(**kwargs)

    monkeypatch.setattr(
        TaskService,
        "build_request_changes_acceptance_authority",
        staticmethod(_slow_build),
    )
    barrier = threading.Barrier(2)
    outcomes = []

    def _attempt():
        db = db_session_factory()
        try:
            row = db.get(Task, task.id)
            barrier.wait()
            result = _queue_task_retry(
                db, row, retry_request=TaskRetryRequest(**SAME_SESSION)
            )
            outcomes.append(("issued", result["task_execution_id"]))
        except HTTPException as exc:
            outcomes.append(("refused", exc.status_code, exc.detail))
        finally:
            db.close()

    threads = [threading.Thread(target=_attempt) for _ in range(2)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=30)
    assert not any(thread.is_alive() for thread in threads)

    issued = [outcome for outcome in outcomes if outcome[0] == "issued"]
    refused = [outcome for outcome in outcomes if outcome[0] == "refused"]
    assert len(issued) == 1, outcomes
    assert refused == [
        ("refused", 409, "Request-changes review state is incomplete or stale")
    ], outcomes
    # Only the winner reached the locked authority construction.
    assert len(holders) == 1
    execution_id = issued[0][1]
    execution = _fresh(db_session_factory, TaskExecution, execution_id)
    witness = [_expected_witness(execution, prior)]
    assert _witness_rows(db_session_factory) == witness
    assert _execution_ids(db_session_factory, task.id) == [
        prior.task_execution_id,
        execution_id,
    ]
    # Publication happened once, after the invocation and witness committed.
    assert publish_observations == [(execution_id, witness)]
    assert published["published_task_id"] == execution.dispatch_celery_task_id
