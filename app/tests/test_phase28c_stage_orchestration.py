"""Focused tests for the Protocol v2 stage orchestration foundation."""

from __future__ import annotations

import uuid

import pytest

from app.models import PlanningSession, Project
from app.services.orchestration.stage_engine import (
    StageAcceptance,
    StageDefinition,
    StageEngineError,
    StageExecutor,
    StageStatus,
    StageValidation,
)
from app.services.planning.planning_session_service import PlanningSessionService
from app.services.planning.protocol_persistence import (
    PlanningProtocolPersistenceService,
    ProtocolOwnershipError,
)


def _seed_session(db_session, *, protocol_version: str = "v2"):
    project = Project(
        name=f"Stage orchestration {uuid.uuid4().hex[:8]}",
        workspace_path=f"stage-orchestration-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(project)
    db_session.flush()
    session = PlanningSession(
        project_id=project.id,
        title="Stage orchestration test",
        prompt="Exercise a content-agnostic stage graph.",
        status="active",
        protocol_version=protocol_version,
        generation_id=str(uuid.uuid4()),
        processing_token="stage-fence",
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)
    if protocol_version == "v2":
        PlanningProtocolPersistenceService(db_session).record_input_identity(
            session.id,
            planning_input=session.prompt,
            engineering_context_identity="test-context",
            provider_identity="test-provider",
            model_configuration={"planner_model": "test-model"},
            repository_identity="test-repository",
            protocol_version="v2",
            session_generation_id=session.generation_id,
        )
        db_session.commit()
        db_session.refresh(session)
    return project, session


def _stage(identifier, *, prerequisites=(), execute=None, validate=None, accept=None):
    return StageDefinition(
        identifier,
        prerequisites=prerequisites,
        execute=execute or (lambda _context: {"stage": identifier}),
        validate=validate,
        accept=accept,
    )


def test_stage_lifecycle_loads_predecessors_and_completes(db_session):
    _, session = _seed_session(db_session)
    observed = {}

    def execute_child(context):
        observed["predecessors"] = tuple(context.predecessor_checkpoints)
        return {"parent": context.predecessor_checkpoints["first"].content}

    engine = StageExecutor(
        db_session,
        [
            _stage("first", execute=lambda _context: "first-output"),
            _stage("second", prerequisites=("first",), execute=execute_child),
        ],
    )

    result = engine.advance(
        session.id,
        session_generation_id=session.generation_id,
        fencing_token="stage-fence",
    )
    db_session.commit()

    assert result.status == StageStatus.COMPLETED
    assert result.completion is not None and result.completion.manifest is not None
    assert observed["predecessors"] == ("first",)
    effective = engine.persistence.effective_checkpoints(session.id)
    assert [effective[key].status for key in sorted(effective)] == [
        "accepted",
        "accepted",
    ]
    assert [
        item["stage_name"]
        for item in result.completion.manifest.accepted_checkpoint_versions
    ] == [
        "first",
        "second",
    ]


def test_failed_stage_acceptance_is_audited_and_retry_gets_new_attempt(db_session):
    _, session = _seed_session(db_session)
    attempts = []

    def execute(_context):
        attempts.append(len(attempts) + 1)
        return {"attempt": attempts[-1]}

    engine = StageExecutor(
        db_session,
        [
            _stage(
                "only",
                execute=execute,
                validate=lambda _output, _context: (
                    StageValidation(False, "not ready")
                    if len(attempts) == 1
                    else StageValidation(True)
                ),
                accept=lambda _output, _context: StageAcceptance(True),
            )
        ],
    )
    first = engine.advance(
        session.id,
        session_generation_id=session.generation_id,
        fencing_token="stage-fence",
    )
    db_session.commit()
    assert first.status == StageStatus.FAILED
    failed_attempt = first.execution.attempt_id

    second = engine.retry_stage(
        session.id,
        "only",
        session_generation_id=session.generation_id,
        fencing_token="stage-fence",
    )
    db_session.commit()

    assert second.status == StageStatus.ACCEPTED
    assert second.attempt_id != failed_attempt
    records = engine.persistence.list_checkpoints(session.id)
    assert [record.status for record in records] == ["failed", "accepted"]


def test_predecessor_retry_invalidates_all_downstream_checkpoints(db_session):
    _, session = _seed_session(db_session)
    engine = StageExecutor(
        db_session,
        [
            _stage("first", execute=lambda _context: "v1"),
            _stage("second", prerequisites=("first",)),
            _stage("third", prerequisites=("second",)),
        ],
    )
    completed = engine.advance(
        session.id,
        session_generation_id=session.generation_id,
        fencing_token="stage-fence",
    )
    db_session.commit()
    assert completed.status == StageStatus.COMPLETED

    retried = engine.retry_stage(
        session.id,
        "first",
        session_generation_id=session.generation_id,
        fencing_token="stage-fence",
    )
    db_session.commit()

    assert retried.status == StageStatus.ACCEPTED
    effective = engine.persistence.effective_checkpoints(session.id)
    assert effective[("second", 1)].status == "invalidated"
    assert effective[("third", 1)].status == "invalidated"
    completion = engine.evaluate_completion(
        session.id,
        session_generation_id=session.generation_id,
        fencing_token="stage-fence",
    )
    assert not completion.complete


def test_recovery_selects_next_stage_from_persisted_state(db_session):
    _, session = _seed_session(db_session)
    engine = StageExecutor(
        db_session,
        [_stage("first"), _stage("second", prerequisites=("first",))],
    )

    before = engine.recover(session.id)
    assert before.resumable and before.next_stage == "first"

    first = engine.execute_stage(
        session.id,
        "first",
        session_generation_id=session.generation_id,
        fencing_token="stage-fence",
    )
    db_session.commit()
    assert first.status == StageStatus.ACCEPTED

    after = engine.recover(session.id)
    assert after.resumable and after.next_stage == "second"


def test_graph_rejects_cycles_and_fencing_rejects_stale_owner(db_session):
    with pytest.raises(StageEngineError, match="cycle"):
        StageExecutor(
            db_session,
            [_stage("a", prerequisites=("b",)), _stage("b", prerequisites=("a",))],
        )

    _, session = _seed_session(db_session)
    engine = StageExecutor(db_session, [_stage("only")])
    with pytest.raises(ProtocolOwnershipError, match="fencing token"):
        engine.advance(
            session.id,
            session_generation_id=session.generation_id,
            fencing_token="stale-fence",
        )


def test_planning_session_selects_v2_without_entering_legacy_provider_path(
    db_session, monkeypatch
):
    project = Project(
        name="Protocol selection",
        workspace_path=f"protocol-selection-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(project)
    db_session.flush()
    # Phase 28G registers the Brief stage by default; this test preserves the
    # Phase 28C empty-registry compatibility contract explicitly.
    service = PlanningSessionService(db_session, stage_definitions=())
    monkeypatch.setattr(service, "schedule_processing", lambda *_args: None)
    monkeypatch.setattr(
        service,
        "_advance_or_finalize",
        lambda *_args, **_kwargs: pytest.fail("Protocol v2 entered legacy flow"),
    )

    session = service.start_session(
        project,
        "Run the empty Protocol v2 orchestration registry.",
        skip_clarification=True,
        protocol_version="v2",
    )
    processed = service.process_session(session.id)

    assert processed.status == "completed"
    assert processed.protocol_version == "v2"
    assert processed.protocol_input is not None
    assert processed.artifacts == []
    assert processed.completion_manifest is not None
