"""Phase 26D-9 red/green ownership-fence regression tests."""

from __future__ import annotations

import json
import threading
from datetime import datetime, timedelta, timezone

import pytest
from fastapi import HTTPException
from sqlalchemy import create_engine
from sqlalchemy.orm import sessionmaker

from app.config import settings
from app.db_migrations import _migration_026_planning_generation_fence
from app.models import Base, Plan, PlanningArtifact, PlanningSession, Project
from app.services.planning.planning_session_service import PlanningSessionService


def _synthesis_payload(project_name: str) -> dict[str, str]:
    return {
        "requirements": "# A requirements",
        "design": "# A design",
        "implementation_plan": "# A implementation plan",
        "planner_markdown": "\n".join(
            [
                f"# Project: {project_name}",
                "",
                "## Task List",
                "- [ ] TASK_START: A-only task | Must never attach to B | order=1 | P1 | effort=small | profile=test_only",
            ]
        ),
    }


def _new_database(tmp_path, name: str):
    engine = create_engine(
        f"sqlite:///{tmp_path / name}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    return engine, sessionmaker(autoflush=False, bind=engine)


def _seed_session(session_factory, *, status: str = "active"):
    db = session_factory()
    project = Project(
        name="Phase 26D-9 test project",
        description="No-provider ownership fence test",
        workspace_path="phase26d9-test",
    )
    db.add(project)
    db.flush()
    session = PlanningSession(
        project_id=project.id,
        title="Planning test",
        prompt="Build a bounded planning test with API, database, and tests.",
        status=status,
        source_brain="stub_no_provider",
    )
    db.add(session)
    db.flush()
    PlanningSessionService(db)._add_message(
        session,
        "user",
        session.prompt,
        metadata={"kind": "prompt", "skip_clarification": True},
    )
    db.commit()
    return db, project, session


def test_exact_sqlite_id_reuse_returns_stale_owner_without_cross_generation_writes(
    tmp_path, monkeypatch
):
    """A provider barrier must not let logical A write into reused-ID B."""

    engine = create_engine(
        f"sqlite:///{tmp_path / 'phase26d9-id-reuse.db'}",
        connect_args={"check_same_thread": False},
    )
    Base.metadata.create_all(bind=engine)
    session_factory = sessionmaker(autoflush=False, bind=engine)

    setup_db = session_factory()
    project = Project(
        name="Phase 26D-9 barrier project",
        description="No-provider ownership fence test",
        workspace_path="phase26d9-barrier",
    )
    setup_db.add(project)
    setup_db.flush()
    logical_a = PlanningSession(
        project_id=project.id,
        title="Logical A",
        prompt="Build the A-only planning output.",
        status="active",
        source_brain="stub_no_provider",
    )
    setup_db.add(logical_a)
    setup_db.flush()
    PlanningSessionService(setup_db)._add_message(
        logical_a,
        "user",
        logical_a.prompt,
        metadata={"kind": "prompt", "skip_clarification": True},
    )
    setup_db.commit()
    logical_a_id = logical_a.id
    logical_a_generation = logical_a.generation_id
    logical_a_owner = "owner-logical-a"
    logical_a.processing_token = logical_a_owner
    logical_a.processing_started_at = datetime.now(timezone.utc)
    setup_db.commit()
    project_name = project.name
    setup_db.close()

    provider_entered = threading.Event()
    release_provider = threading.Event()
    worker_observation: dict[str, object] = {}

    def deterministic_provider(
        self,
        prompt: str,
        *,
        source_brain: str = "local",
        timeout_seconds: int | None = None,
        project_id: int | None = None,
    ) -> dict[str, str]:
        provider_entered.set()
        assert release_provider.wait(timeout=10), "provider barrier timed out"
        return {
            "status": "completed",
            "output": json.dumps(_synthesis_payload(project_name)),
            "backend": "stub_no_provider",
            "model_family": "none",
        }

    monkeypatch.setattr(PlanningSessionService, "_run_openclaw", deterministic_provider)

    def run_logical_a() -> None:
        worker_db = session_factory()
        try:
            worker_observation["result"] = PlanningSessionService(
                worker_db
            ).process_session(logical_a_id, logical_a_generation, logical_a_owner)
        finally:
            worker_db.close()

    worker = threading.Thread(target=run_logical_a, daemon=True)
    worker.start()
    assert provider_entered.wait(timeout=10), "logical A never reached provider barrier"

    control_db = session_factory()
    control_service = PlanningSessionService(control_db)
    control_service.cancel(logical_a_id)
    control_service.delete_terminal_session(logical_a_id)

    logical_b = PlanningSession(
        project_id=project.id,
        title="Logical B",
        prompt="Build the B-only planning output.",
        status="active",
        source_brain="stub_no_provider",
    )
    control_db.add(logical_b)
    control_db.flush()
    logical_b_id = logical_b.id
    PlanningSessionService(control_db)._add_message(
        logical_b,
        "user",
        logical_b.prompt,
        metadata={"kind": "prompt"},
    )
    control_db.commit()
    control_db.close()

    assert logical_a_id == logical_b_id
    release_provider.set()
    worker.join(timeout=10)
    assert not worker.is_alive(), "logical A worker did not exit after barrier release"

    verify_db = session_factory()
    try:
        replacement = verify_db.get(PlanningSession, logical_b_id)
        assert replacement is not None
        assert replacement.status == "active"
        assert replacement.last_error is None
        assert (
            verify_db.query(PlanningArtifact)
            .filter_by(planning_session_id=logical_b_id)
            .count()
            == 0
        )
        assert [message.content for message in replacement.messages] == [
            "Build the B-only planning output."
        ]
        assert isinstance(worker_observation["result"], dict)
        assert worker_observation["result"]["status"] == "stale_owner"
        with pytest.raises(HTTPException) as commit_error:
            PlanningSessionService(verify_db).commit(logical_b_id)
        assert commit_error.value.status_code == 409
        assert verify_db.query(Plan).count() == 0
    finally:
        verify_db.close()
        Base.metadata.drop_all(bind=engine)
        engine.dispose()


def test_generation_and_owner_mismatches_return_structured_stale_owner(tmp_path):
    engine, session_factory = _new_database(tmp_path, "phase26d9-claim.db")
    db, _, session = _seed_session(session_factory)
    session.processing_token = "owner-1"
    session.processing_started_at = datetime.now(timezone.utc)
    db.commit()

    service = PlanningSessionService(db)
    generation_mismatch = service.process_session(
        session.id, "wrong-generation", "owner-1"
    )
    owner_mismatch = service.process_session(
        session.id, session.generation_id, "wrong-owner"
    )

    assert generation_mismatch == {
        "status": "stale_owner",
        "session_id": session.id,
        "generation_id": "wrong-generation",
        "reason": "generation_mismatch",
    }
    assert owner_mismatch["status"] == "stale_owner"
    assert owner_mismatch["reason"] == "owner_mismatch"
    assert db.get(PlanningSession, session.id).processing_token == "owner-1"
    db.close()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def test_scheduler_serializes_generation_owner_and_retry_preserves_generation(
    tmp_path, monkeypatch
):
    engine, session_factory = _new_database(tmp_path, "phase26d9-schedule.db")
    db, project, session = _seed_session(session_factory)
    monkeypatch.setattr(settings, "INLINE_PLANNING", False)
    published: list[dict[str, object]] = []
    monkeypatch.setattr(
        "app.tasks.planning_tasks.advance_planning_session.apply_async",
        lambda **kwargs: published.append(kwargs),
    )

    service = PlanningSessionService(db)
    service.schedule_processing(session.id)
    db.refresh(session)
    assert len(published) == 1
    assert published[0]["args"] == (
        session.id,
        session.generation_id,
        session.processing_token,
    )
    assert published[0]["task_id"] == session.processing_task_id
    original_generation = session.generation_id
    original_owner = session.processing_token

    session.status = "failed"
    session.last_error = "retry test"
    session.processing_token = None
    session.processing_task_id = None
    db.commit()
    service.retry(session.id)
    db.refresh(session)
    assert len(published) == 2
    assert session.generation_id == original_generation
    assert session.processing_token != original_owner
    assert published[1]["args"] == (
        session.id,
        original_generation,
        session.processing_token,
    )
    assert published[1]["task_id"] == session.processing_task_id

    db.close()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def test_celery_retry_keeps_all_three_fence_arguments(monkeypatch):
    from app.tasks.planning_tasks import advance_planning_session

    class RetrySignal(Exception):
        pass

    captured: dict[str, object] = {}

    class FakeDatabase:
        def close(self):
            return None

    monkeypatch.setattr(
        "app.tasks.planning_tasks.get_db_session", lambda: FakeDatabase()
    )
    monkeypatch.setattr(
        PlanningSessionService,
        "process_session",
        lambda *args, **kwargs: (_ for _ in ()).throw(RuntimeError("retry")),
    )
    monkeypatch.setattr(
        advance_planning_session,
        "retry",
        lambda **kwargs: (
            captured.update(kwargs),
            (_ for _ in ()).throw(RetrySignal()),
        )[1],
    )

    with pytest.raises(RetrySignal):
        advance_planning_session.run(41, "generation-41", "owner-41")

    assert captured["args"] == (41, "generation-41", "owner-41")


def test_concurrent_owner_takeover_cannot_be_cleared_by_old_provider_result(
    tmp_path, monkeypatch
):
    engine, session_factory = _new_database(tmp_path, "phase26d9-concurrent.db")
    db, _, session = _seed_session(session_factory)
    session.processing_token = "owner-1"
    session.processing_started_at = datetime.now(timezone.utc)
    db.commit()
    generation_id = session.generation_id
    session_id = session.id

    entered = threading.Event()
    release = threading.Event()

    def provider(self, prompt, **kwargs):
        entered.set()
        assert release.wait(timeout=10)
        return {
            "status": "completed",
            "output": json.dumps(_synthesis_payload("Phase 26D-9 test project")),
        }

    monkeypatch.setattr(PlanningSessionService, "_run_openclaw", provider)
    observed: dict[str, object] = {}

    def old_owner():
        worker_db = session_factory()
        try:
            observed["result"] = PlanningSessionService(worker_db).process_session(
                session_id, generation_id, "owner-1"
            )
        finally:
            worker_db.close()

    worker = threading.Thread(target=old_owner, daemon=True)
    worker.start()
    assert entered.wait(timeout=10)

    takeover_db = session_factory()
    replacement = takeover_db.get(PlanningSession, session_id)
    replacement.processing_token = "owner-2"
    replacement.processing_started_at = datetime.now(timezone.utc)
    takeover_db.commit()
    takeover_db.close()
    release.set()
    worker.join(timeout=10)
    assert not worker.is_alive()

    verify = session_factory()
    current = verify.get(PlanningSession, session_id)
    assert observed["result"]["status"] == "stale_owner"
    assert current.processing_token == "owner-2"
    assert (
        verify.query(PlanningArtifact).filter_by(planning_session_id=session_id).count()
        == 0
    )
    verify.close()
    db.close()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def test_stale_provider_return_cannot_write_malformed_diagnostic(tmp_path, monkeypatch):
    engine, session_factory = _new_database(tmp_path, "phase26d9-diagnostic.db")
    db, _, session = _seed_session(session_factory)
    session.processing_token = "owner-diagnostic"
    session.processing_started_at = datetime.now(timezone.utc)
    db.commit()
    session_id = session.id
    generation_id = session.generation_id
    entered = threading.Event()
    release = threading.Event()

    def malformed_provider(self, prompt, **kwargs):
        entered.set()
        assert release.wait(timeout=10)
        return {"status": "completed", "output": "not valid json"}

    monkeypatch.setattr(PlanningSessionService, "_run_openclaw", malformed_provider)
    observed: dict[str, object] = {}

    def old_owner():
        worker_db = session_factory()
        try:
            observed["result"] = PlanningSessionService(worker_db).process_session(
                session_id, generation_id, "owner-diagnostic"
            )
        finally:
            worker_db.close()

    worker = threading.Thread(target=old_owner, daemon=True)
    worker.start()
    assert entered.wait(timeout=10)
    control = session_factory()
    PlanningSessionService(control).cancel(session_id)
    control.close()
    release.set()
    worker.join(timeout=10)
    assert not worker.is_alive()

    verify = session_factory()
    assert observed["result"]["status"] == "stale_owner"
    assert (
        verify.query(PlanningArtifact)
        .filter_by(
            planning_session_id=session_id,
            artifact_type="planning_synthesis_parse_failure_diagnostic",
        )
        .count()
        == 0
    )
    assert verify.get(PlanningSession, session_id).status == "cancelled"
    verify.close()
    db.close()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def test_cancel_invalidates_owner_before_revoke_and_delete_is_safe(
    tmp_path, monkeypatch
):
    engine, session_factory = _new_database(tmp_path, "phase26d9-cancel.db")
    db, _, session = _seed_session(session_factory)
    session.processing_token = "owner-cancel"
    session.processing_task_id = "celery-cancel"
    session.processing_started_at = datetime.now(timezone.utc)
    db.commit()
    revoked: list[str] = []
    monkeypatch.setattr(
        PlanningSessionService,
        "_revoke_processing_task",
        staticmethod(lambda task_id: revoked.append(task_id)),
    )

    cancelled = PlanningSessionService(db).cancel(session.id)
    assert cancelled.processing_token is None
    assert cancelled.processing_task_id == "celery-cancel"
    assert revoked == ["celery-cancel"]

    PlanningSessionService(db).delete_terminal_session(session.id)
    assert db.get(PlanningSession, session.id) is None
    db.close()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def test_successful_planning_cleans_owner_and_observational_task_residue(
    tmp_path, monkeypatch
):
    engine, session_factory = _new_database(tmp_path, "phase26d9-success.db")
    db, project, _ = _seed_session(session_factory)
    monkeypatch.setattr(settings, "INLINE_PLANNING", True)
    monkeypatch.setattr(
        PlanningSessionService,
        "_run_openclaw",
        lambda self, prompt, **kwargs: {
            "status": "completed",
            "output": json.dumps(_synthesis_payload(project.name)),
        },
    )
    session = db.query(PlanningSession).one()
    generation_id = session.generation_id
    updated = PlanningSessionService(db).process_session(
        session.id, generation_id, "owner-success"
    )
    # The direct call uses a caller-owned token, so it must be explicitly
    # prepared by the scheduler in production.  Exercise that real path too.
    if isinstance(updated, dict):
        session.processing_token = None
        session.processing_started_at = None
        db.commit()
        PlanningSessionService(db).schedule_processing(session.id)
    db.refresh(session)
    assert session.status == "completed"
    assert session.processing_token is None
    assert session.processing_task_id is None
    assert session.generation_id == generation_id
    db.close()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def test_expired_owner_is_stale_and_cannot_reclaim_lease(tmp_path):
    engine, session_factory = _new_database(tmp_path, "phase26d9-lease.db")
    db, _, session = _seed_session(session_factory)
    session.processing_token = "expired-owner"
    session.processing_started_at = datetime.now(timezone.utc) - timedelta(minutes=11)
    db.commit()

    result = PlanningSessionService(db).process_session(
        session.id, session.generation_id, "expired-owner"
    )
    assert result["status"] == "stale_owner"
    assert result["reason"] == "lease_expired"
    db.refresh(session)
    assert session.processing_token == "expired-owner"
    db.close()
    Base.metadata.drop_all(bind=engine)
    engine.dispose()


def test_project_delete_invalidates_planning_owner_before_soft_delete(
    authenticated_client, db_session
):
    project_response = authenticated_client.post(
        "/api/v1/projects",
        json={"name": "Phase 26D-9 lifecycle project", "description": "Fence"},
    )
    assert project_response.status_code == 201
    project_id = project_response.json()["id"]
    session = PlanningSession(
        project_id=project_id,
        title="Project-owned planning",
        prompt="Planning owned by a project",
        status="active",
        processing_token="project-owner",
        processing_task_id="project-task",
        processing_started_at=datetime.now(timezone.utc),
    )
    db_session.add(session)
    db_session.commit()

    deleted = authenticated_client.delete(f"/api/v1/projects/{project_id}")
    assert deleted.status_code == 200
    db_session.expire_all()
    persisted = db_session.get(PlanningSession, session.id)
    assert persisted.status == "cancelled"
    assert persisted.processing_token is None
    assert persisted.processing_task_id == "project-task"


def test_legacy_generation_migration_backfills_unique_rows_and_null_rows_do_not_schedule(
    tmp_path,
):
    engine = create_engine(f"sqlite:///{tmp_path / 'phase26d9-legacy.db'}")
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "CREATE TABLE planning_sessions (id INTEGER PRIMARY KEY, title VARCHAR(255))"
        )
        connection.exec_driver_sql(
            "INSERT INTO planning_sessions (id, title) VALUES (1, 'legacy A'), (2, 'legacy B')"
        )
    _migration_026_planning_generation_fence(engine)
    with engine.connect() as connection:
        rows = connection.exec_driver_sql(
            "SELECT id, generation_id FROM planning_sessions ORDER BY id"
        ).fetchall()
    assert len({row[1] for row in rows}) == 2
    assert all(row[1] for row in rows)

    engine.dispose()

    null_engine, db_factory = _new_database(
        tmp_path, "phase26d9-legacy-null-readable.db"
    )
    db = db_factory()
    project = Project(name="Legacy readable", workspace_path="legacy-readable")
    db.add(project)
    db.flush()
    current = PlanningSession(
        project_id=project.id,
        title="Legacy nullable",
        prompt="Readable but not schedulable",
        status="active",
    )
    db.add(current)
    db.flush()
    current.generation_id = None
    db.commit()
    result = PlanningSessionService(db).schedule_processing(current.id)
    assert result["status"] == "stale_owner"
    assert result["reason"] == "missing_generation"
    db.close()
    Base.metadata.drop_all(bind=null_engine)
    null_engine.dispose()
