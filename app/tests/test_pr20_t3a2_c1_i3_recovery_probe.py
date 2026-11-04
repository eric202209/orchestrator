"""Independent I3 recovery-path probe for C1 request-changes authority."""

from copy import deepcopy
from pathlib import Path
from uuid import uuid4

import pytest
from sqlalchemy import create_engine, event
from sqlalchemy.exc import IntegrityError
from sqlalchemy.orm import sessionmaker

from app.db_migrations import run_schema_migrations
from app.models import (
    Base,
    RequestChangesIssuanceWitness,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.execution.workflow_lifecycle import (
    _dispatch_replay_arguments,
    configure_task_execution_dispatch,
    dispatch_value_sha256,
    publish_task_execution_dispatch,
)
from app.services.tasks.execution import create_task_execution
from app.tests.test_pr20_t3a2_c1_i1_issuance_witness import (
    NEW_SESSION,
    NOTE,
    _issue,
    _review_task,
)
from app.tests.test_pr20_t3a2_request_changes_boundary import (
    OPERATOR_DESCRIPTION,
    _run_worker_completion_seam,
    _worker_seam_failure_diagnostics,
    _worker_task,
)


@pytest.fixture
def db_session_factory(tmp_path: Path):
    """Use migrated, file-backed SQLite with FK enforcement and WAL."""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'i3-recovery.db'}",
        connect_args={"check_same_thread": False, "timeout": 2.0},
    )

    @event.listens_for(engine, "connect")
    def _sqlite_pragmas(connection, _record):
        cursor = connection.cursor()
        cursor.execute("PRAGMA foreign_keys=ON")
        cursor.execute("PRAGMA journal_mode=WAL")
        cursor.execute("PRAGMA synchronous=NORMAL")
        cursor.execute("PRAGMA busy_timeout=2000")
        cursor.close()

    Base.metadata.create_all(bind=engine)
    run_schema_migrations(engine)
    factory = sessionmaker(autocommit=False, autoflush=False, bind=engine)
    try:
        yield factory
    finally:
        engine.dispose()


def _create_recovery_dispatch(db_session_factory, *, task_id, parent_execution_id):
    """Persist a managed RECOVERY identity with the normal continuation tuple."""

    db = db_session_factory()
    try:
        task = db.get(Task, task_id)
        parent = db.get(TaskExecution, parent_execution_id)
        assert task is not None and parent is not None
        session = db.get(SessionModel, parent.session_id)
        assert session is not None
        link = (
            db.query(SessionTask)
            .filter(
                SessionTask.session_id == session.id,
                SessionTask.task_id == task.id,
            )
            .order_by(SessionTask.id.desc())
            .first()
        )
        assert link is not None

        execution = create_task_execution(
            db,
            session_id=session.id,
            task_id=task.id,
        )
        db.flush()
        session.status = "recovering"
        session.continuation_task_id = task.id
        session.continuation_task_execution_id = execution.id
        session.continuation_kind = "automatic_recovery"
        session.continuation_retry_count = 0
        assert configure_task_execution_dispatch(
            db,
            task_execution=execution,
            session=session,
            task=task,
            dispatch_kind="RECOVERY",
            prompt=(task.description or "").strip(),
            timeout_seconds=120,
            parent_task_execution_id=parent.id,
            continuation_task_id=task.id,
            continuation_kind="automatic_recovery",
            continuation_retry_count=0,
        )
        execution.dispatch_state = "READY"
        db.commit()
        identity = {
            "id": execution.id,
            "task_id": execution.task_id,
            "session_id": execution.session_id,
            "dispatch_kind": execution.dispatch_kind,
            "dispatch_parent_task_execution_id": (
                execution.dispatch_parent_task_execution_id
            ),
            "dispatch_celery_task_id": execution.dispatch_celery_task_id,
            "dispatch_invocation": deepcopy(execution.dispatch_invocation),
            "dispatch_invocation_sha256": execution.dispatch_invocation_sha256,
        }
        return identity
    finally:
        db.close()


def test_recovery_from_witnessed_request_changes_refuses_before_worker_context(
    authenticated_client,
    db_session,
    db_session_factory,
    tmp_path: Path,
    monkeypatch,
):
    """A recovery delivery for a witnessed request-changes Task must stay gated."""

    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    _response, source_execution, _published = _issue(
        authenticated_client,
        db_session_factory,
        task,
        prior,
        monkeypatch,
        NEW_SESSION,
    )
    observer = db_session_factory()
    try:
        assert (
            observer.query(RequestChangesIssuanceWitness)
            .filter(
                RequestChangesIssuanceWitness.current_task_execution_id
                == source_execution.id
            )
            .one_or_none()
            is not None
        )
    finally:
        observer.close()

    recovery_task = _worker_task(
        OPERATOR_DESCRIPTION,
        workspace_status="changes_requested",
        promotion_note=NOTE,
        error_message=(
            "Original witnessed request-changes attempt failed; automatic recovery."
        ),
    )
    recovery_task.id = task.id
    recovery_task.project_id = task.project_id
    result = _run_worker_completion_seam(
        monkeypatch,
        tmp_path / "recovery-worker",
        db_session_factory=db_session_factory,
        task=recovery_task,
        prompt=OPERATOR_DESCRIPTION,
        context=None,
        dispatch_kind="RECOVERY",
        complete=False,
    )

    diagnostics = _worker_seam_failure_diagnostics(result)
    assert result.worker_contexts == [], diagnostics
    assert len(result.primary_exceptions) == 1, diagnostics
    assert "acceptance_provenance_refused:request_changes_review_active" in str(
        result.primary_exceptions[0]
    ), diagnostics
    assert result.validator_calls == [], diagnostics
    assert result.provider_calls == [], diagnostics
    assert result.executed_commands == [], diagnostics
    assert result.step_loop_calls == [], diagnostics
    assert result.task_execution.id == result.initial_identity["task_execution_id"]
    assert result.task_execution.task_id == result.initial_identity["task_id"]
    assert result.task_execution.session_id == result.initial_identity["session_id"]
    assert (
        result.task_execution.attempt_number
        == result.initial_identity["attempt_number"]
    )
    assert (
        result.task_execution.dispatch_state
        == result.initial_identity["dispatch_state"]
    )
    assert result.task_execution.dispatch_kind == "RECOVERY"
    assert result.task_execution.dispatch_parent_task_execution_id is None
    assert result.task_execution.dispatch_celery_task_id is None
    assert result.task_execution.dispatch_invocation is None
    assert result.task_execution.dispatch_invocation_sha256 is None
    observer = db_session_factory()
    try:
        assert observer.get(RequestChangesIssuanceWitness, source_execution.id)
        assert observer.query(RequestChangesIssuanceWitness).count() == 1
        assert (
            observer.query(RequestChangesIssuanceWitness)
            .filter(
                RequestChangesIssuanceWitness.current_task_execution_id
                == result.task_execution.id
            )
            .one_or_none()
            is None
        )
    finally:
        observer.close()


def test_recovery_replay_refuses_active_review_before_publication(
    authenticated_client,
    db_session,
    db_session_factory,
    tmp_path: Path,
    monkeypatch,
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    _response, source_execution, _published = _issue(
        authenticated_client,
        db_session_factory,
        task,
        prior,
        monkeypatch,
        {"retry_intent": "request_changes"},
    )
    recovery_identity = _create_recovery_dispatch(
        db_session_factory,
        task_id=task.id,
        parent_execution_id=source_execution.id,
    )
    published = []

    db = db_session_factory()
    try:
        outcome = publish_task_execution_dispatch(
            db,
            recovery_identity["id"],
            publisher=lambda **kwargs: published.append(kwargs),
        )
        assert outcome["state"] == "refused"
        assert outcome["reason"] == "request_changes_review_active"
        assert published == []
        db.rollback()
    finally:
        db.close()

    observer = db_session_factory()
    try:
        unchanged = observer.get(TaskExecution, recovery_identity["id"])
        assert unchanged is not None
        assert unchanged.id == recovery_identity["id"]
        assert unchanged.task_id == recovery_identity["task_id"]
        assert unchanged.session_id == recovery_identity["session_id"]
        assert unchanged.dispatch_kind == recovery_identity["dispatch_kind"]
        assert (
            unchanged.dispatch_parent_task_execution_id
            == recovery_identity["dispatch_parent_task_execution_id"]
        )
        assert (
            unchanged.dispatch_celery_task_id
            == recovery_identity["dispatch_celery_task_id"]
        )
        assert unchanged.dispatch_invocation == recovery_identity["dispatch_invocation"]
        assert (
            unchanged.dispatch_invocation_sha256
            == recovery_identity["dispatch_invocation_sha256"]
        )
        assert unchanged.dispatch_state == "REFUSED"
        assert unchanged.dispatch_refusal_reason == "request_changes_review_active"
        assert (
            observer.query(RequestChangesIssuanceWitness)
            .filter(
                RequestChangesIssuanceWitness.current_task_execution_id
                == recovery_identity["id"]
            )
            .one_or_none()
            is None
        )
        assert observer.query(RequestChangesIssuanceWitness).count() == 1
    finally:
        observer.close()


@pytest.mark.parametrize(
    ("workspace_status", "promotion_note"),
    (("in_progress", None), ("changes_requested", None)),
)
def test_automatic_recovery_worker_without_operator_review_remains_supported(
    db_session_factory,
    tmp_path: Path,
    monkeypatch,
    workspace_status,
    promotion_note,
):
    recovery_task = _worker_task(
        OPERATOR_DESCRIPTION,
        workspace_status=workspace_status,
        promotion_note=promotion_note,
    )
    result = _run_worker_completion_seam(
        monkeypatch,
        tmp_path / "ordinary-recovery-worker",
        db_session_factory=db_session_factory,
        task=recovery_task,
        prompt=OPERATOR_DESCRIPTION,
        context=None,
        dispatch_kind="RECOVERY",
        complete=False,
    )

    diagnostics = _worker_seam_failure_diagnostics(result)
    assert len(result.worker_contexts) == 1, diagnostics
    assert result.step_loop_calls == ["reached"], diagnostics
    assert result.worker_contexts[0].acceptance_authority_status == (
        "verified_recovery_legacy"
    )
    assert result.validator_calls == [], diagnostics
    assert result.provider_calls == [], diagnostics
    assert result.executed_commands == [], diagnostics


def test_recovery_worker_ignores_historical_witness_after_review_closes(
    authenticated_client,
    db_session,
    db_session_factory,
    tmp_path: Path,
    monkeypatch,
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    _response, source_execution, _published = _issue(
        authenticated_client,
        db_session_factory,
        task,
        prior,
        monkeypatch,
        NEW_SESSION,
    )
    recovery_task = _worker_task(
        OPERATOR_DESCRIPTION,
        workspace_status="ready",
        promotion_note=NOTE,
    )
    recovery_task.id = task.id
    recovery_task.project_id = task.project_id
    result = _run_worker_completion_seam(
        monkeypatch,
        tmp_path / "closed-review-recovery-worker",
        db_session_factory=db_session_factory,
        task=recovery_task,
        prompt=OPERATOR_DESCRIPTION,
        context=None,
        dispatch_kind="RECOVERY",
        complete=False,
    )

    diagnostics = _worker_seam_failure_diagnostics(result)
    assert result.worker_contexts, diagnostics
    assert result.step_loop_calls == ["reached"], diagnostics
    assert result.worker_contexts[0].acceptance_authority_status == (
        "verified_recovery_legacy"
    )
    assert result.validator_calls == [], diagnostics
    assert result.provider_calls == [], diagnostics
    assert result.executed_commands == [], diagnostics
    observer = db_session_factory()
    try:
        assert observer.get(RequestChangesIssuanceWitness, source_execution.id)
        assert observer.query(RequestChangesIssuanceWitness).count() == 1
    finally:
        observer.close()


def test_recovery_replay_with_historical_witness_and_closed_review_publishes(
    authenticated_client,
    db_session,
    db_session_factory,
    tmp_path: Path,
    monkeypatch,
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    _response, source_execution, _published = _issue(
        authenticated_client,
        db_session_factory,
        task,
        prior,
        monkeypatch,
        {"retry_intent": "request_changes"},
    )
    observer = db_session_factory()
    try:
        persisted_task = observer.get(Task, task.id)
        assert persisted_task is not None
        persisted_task.workspace_status = "ready"
        observer.commit()
    finally:
        observer.close()

    recovery_identity = _create_recovery_dispatch(
        db_session_factory,
        task_id=task.id,
        parent_execution_id=source_execution.id,
    )
    published = []
    db = db_session_factory()
    try:
        outcome = publish_task_execution_dispatch(
            db,
            recovery_identity["id"],
            publisher=lambda **kwargs: published.append(kwargs),
        )
        assert outcome["state"] == "published_unclaimed"
        assert len(published) == 1
        assert published[0]["task_id"] == recovery_identity["dispatch_celery_task_id"]
        db.rollback()
    finally:
        db.close()

    observer = db_session_factory()
    try:
        assert observer.get(RequestChangesIssuanceWitness, source_execution.id)
        assert observer.query(RequestChangesIssuanceWitness).count() == 1
        assert (
            observer.query(RequestChangesIssuanceWitness)
            .filter(
                RequestChangesIssuanceWitness.current_task_execution_id
                == recovery_identity["id"]
            )
            .one_or_none()
            is None
        )
    finally:
        observer.close()


@pytest.mark.parametrize(
    "column",
    (
        "dispatch_kind",
        "dispatch_parent_task_execution_id",
        "dispatch_celery_task_id",
    ),
)
def test_migration_060_freezes_request_changes_dispatch_identity(
    authenticated_client,
    db_session,
    db_session_factory,
    tmp_path: Path,
    monkeypatch,
    column,
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    _response, execution, _published = _issue(
        authenticated_client,
        db_session_factory,
        task,
        prior,
        monkeypatch,
        NEW_SESSION,
    )
    persisted = db_session.get(TaskExecution, execution.id)
    original_identity = {
        "dispatch_kind": persisted.dispatch_kind,
        "dispatch_parent_task_execution_id": (
            persisted.dispatch_parent_task_execution_id
        ),
        "dispatch_celery_task_id": persisted.dispatch_celery_task_id,
        "dispatch_invocation": deepcopy(persisted.dispatch_invocation),
        "dispatch_invocation_sha256": persisted.dispatch_invocation_sha256,
    }
    assert (
        db_session.query(RequestChangesIssuanceWitness)
        .filter(RequestChangesIssuanceWitness.current_task_execution_id == execution.id)
        .count()
        == 1
    )
    replacement = {
        "dispatch_kind": "RECOVERY",
        "dispatch_parent_task_execution_id": prior.task_execution_id,
        "dispatch_celery_task_id": f"replacement-{uuid4().hex}",
    }[column]
    setattr(persisted, column, replacement)

    with pytest.raises(IntegrityError, match="dispatch identity is immutable"):
        db_session.commit()
    db_session.rollback()

    observer = db_session_factory()
    try:
        unchanged = observer.get(TaskExecution, execution.id)
        assert getattr(unchanged, column) != replacement
        assert {
            "dispatch_kind": unchanged.dispatch_kind,
            "dispatch_parent_task_execution_id": (
                unchanged.dispatch_parent_task_execution_id
            ),
            "dispatch_celery_task_id": unchanged.dispatch_celery_task_id,
            "dispatch_invocation": unchanged.dispatch_invocation,
            "dispatch_invocation_sha256": unchanged.dispatch_invocation_sha256,
        } == original_identity
        assert (
            observer.query(RequestChangesIssuanceWitness)
            .filter(
                RequestChangesIssuanceWitness.current_task_execution_id == execution.id
            )
            .count()
            == 1
        )
    finally:
        observer.close()


def test_legacy_request_changes_replay_without_authority_refuses(
    authenticated_client,
    db_session,
    db_session_factory,
    tmp_path: Path,
    monkeypatch,
):
    task, prior = _review_task(authenticated_client, db_session, tmp_path)
    _response, issued, _published = _issue(
        authenticated_client,
        db_session_factory,
        task,
        prior,
        monkeypatch,
        NEW_SESSION,
    )
    legacy = create_task_execution(
        db_session,
        session_id=issued.session_id,
        task_id=issued.task_id,
    )
    db_session.flush()
    invocation = deepcopy(issued.dispatch_invocation)
    invocation.pop("acceptance_authority")
    legacy.dispatch_state = "READY"
    legacy.dispatch_kind = "ROOT"
    legacy.dispatch_session_instance_id = issued.dispatch_session_instance_id
    legacy.dispatch_parent_task_execution_id = None
    legacy.dispatch_celery_task_id = f"legacy-{uuid4().hex}"
    legacy.dispatch_invocation = invocation
    legacy.dispatch_invocation_sha256 = dispatch_value_sha256(invocation)
    db_session.commit()

    arguments, reason = _dispatch_replay_arguments(db_session, legacy)
    assert arguments is None
    assert reason == "acceptance_authority_missing_for_request_changes"
    assert (
        db_session.query(RequestChangesIssuanceWitness)
        .filter(RequestChangesIssuanceWitness.current_task_execution_id == legacy.id)
        .count()
        == 0
    )
