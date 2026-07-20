"""Focused tests for the Protocol v2 Planning Brief generation stage."""

from __future__ import annotations

import copy
import uuid

import pytest

from app.models import PlanningSession, Project
from app.services.orchestration.stage_engine import (
    StageDefinition,
    StageExecutor,
    StageStatus,
)
from app.services.planning.input_manifest import build_input_manifest
from app.services.planning.planning_brief_stage import (
    PlanningBriefProviderInput,
    PlanningBriefApplicationError,
    PlanningBriefStage,
    PlanningBriefTransportError,
    PlanningBriefProviderOutputError,
    build_protocol_v2_stage_definitions,
    canonicalize_planning_brief_candidate,
    parse_planning_brief_candidate,
)
from app.services.planning.protocol_persistence import (
    PlanningProtocolPersistenceService,
)


def _manifest(session_id: int, generation: str):
    return build_input_manifest(
        session_id=session_id,
        session_generation_id=generation,
        planning_request={"message_id": 1, "content": "Implement a Brief stage."},
        clarification_messages=[],
        project_metadata={"project_id": 9, "name": "Stage project"},
        project_rules="Preserve Protocol v1.",
        repository={"available": False, "workspace": "/workspace"},
        runtime_configuration={
            "provider": "test",
            "backend": "test",
            "model": "test-model",
            "reasoning_profile": "default",
        },
        stage_configuration={
            "stages": [{"identifier": "planning_brief", "version": 1}]
        },
        manifest_built_at="2026-07-20T00:00:00+00:00",
    )


def _candidate(manifest, *, reversed_requirements: bool = False):
    first = manifest.sources[0].source_id
    second = manifest.sources[1].source_id
    requirements = [
        {
            "type": "functional",
            "statement": "Preserve the existing Protocol v1 path.",
            "priority": "required",
            "source_refs": [first],
        },
        {
            "type": "non_functional",
            "statement": "Brief generation is deterministic.",
            "priority": "required",
            "source_refs": [second],
            "quality_attribute": "reliability",
        },
    ]
    if reversed_requirements:
        requirements.reverse()
    return {
        "objective": {
            "statement": "Generate one accepted Planning Brief.",
            "source_refs": [first],
        },
        "background": [],
        "scope": [
            {
                "classification": "in_scope",
                "statement": "Planning Brief generation only.",
                "source_refs": [first],
            }
        ],
        "requirements": requirements,
        "constraints": [],
        "acceptance_criteria": [
            {
                "statement": "The Brief checkpoint reloads canonically.",
                "verification_method": "Run the recovery test.",
                "source_requirement_ids": ["requirements[0]"],
                "criticality": "required",
            },
            {
                "statement": "The v1 path remains unchanged.",
                "verification_method": "Run the compatibility test.",
                "source_requirement_ids": ["requirements[1]"],
                "criticality": "required",
            },
        ],
        "architecture_context": [],
        "interface_contracts": [],
        "implementation_strategy": [
            {
                "statement": "Use the existing stage engine and persistence seams.",
                "source_refs": [first],
                "requirement_ids": ["requirements[0]"],
                "constraint_ids": [],
            }
        ],
        "validation_strategy": [
            {
                "statement": "Run deterministic Brief validation.",
                "source_refs": [first],
                "acceptance_criterion_ids": [
                    "acceptance_criteria[0]",
                    "acceptance_criteria[1]",
                ],
                "requirement_ids": ["requirements[0]", "requirements[1]"],
            }
        ],
        "assumptions": [],
        "risks": [],
        "unresolved_questions": [],
        "operator_decisions": [],
    }


def _seed_session(db_session):
    project = Project(
        name=f"Phase 28G {uuid.uuid4().hex[:8]}",
        workspace_path=f"phase28g-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(project)
    db_session.flush()
    session = PlanningSession(
        project_id=project.id,
        title="Planning Brief stage",
        prompt="Implement a Brief stage.",
        status="active",
        protocol_version="v2",
        generation_id=str(uuid.uuid4()),
        processing_token="phase28g-fence",
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)
    manifest = _manifest(session.id, session.generation_id)
    PlanningProtocolPersistenceService(db_session).record_input_manifest(
        session.id, manifest=manifest
    )
    db_session.commit()
    return session, manifest


class _Provider:
    def __init__(self, output):
        self.output = output
        self.requests: list[PlanningBriefProviderInput] = []

    def generate(self, request):
        self.requests.append(request)
        return copy.deepcopy(self.output)


def test_candidate_contract_rejects_unknown_fields_and_provider_ids():
    candidate = _candidate(_manifest(1, "generation"))
    candidate["unknown"] = True
    with pytest.raises(PlanningBriefProviderOutputError, match="unknown fields"):
        parse_planning_brief_candidate(candidate)

    candidate = _candidate(_manifest(1, "generation"))
    candidate["objective"]["id"] = "GOAL-999"
    with pytest.raises(PlanningBriefProviderOutputError, match="application-owned"):
        parse_planning_brief_candidate(candidate)


def test_canonicalization_assigns_application_ids_and_orders_by_manifest_source():
    manifest = _manifest(1, "generation")
    candidate = parse_planning_brief_candidate(
        _candidate(manifest, reversed_requirements=True)
    )
    brief = canonicalize_planning_brief_candidate(candidate, manifest)
    assert brief.objective.id == "GOAL-001"
    assert brief.requirements[0].id == "REQ-001"
    assert brief.requirements[1].id == "REQ-002"
    assert brief.acceptance_criteria[0].source_requirement_ids == ("REQ-002",)
    assert brief.acceptance_criteria[1].source_requirement_ids == ("REQ-001",)
    assert (
        brief.content_hash
        == canonicalize_planning_brief_candidate(
            parse_planning_brief_candidate(
                _candidate(manifest, reversed_requirements=True)
            ),
            manifest,
        ).content_hash
    )


def test_stage_accepts_only_valid_canonical_brief_and_persists_metadata(db_session):
    session, manifest = _seed_session(db_session)
    provider = _Provider(_candidate(manifest))
    engine = StageExecutor(db_session, [PlanningBriefStage(provider)])
    result = engine.advance(
        session.id,
        session_generation_id=session.generation_id,
        fencing_token=session.processing_token,
    )
    assert result.status == StageStatus.COMPLETED
    assert len(provider.requests) == 1
    request = provider.requests[0]
    assert request.manifest_hash == manifest.manifest_hash
    assert request.sources[0]["source_id"] == manifest.sources[0].source_id
    assert "project_id" not in request.to_dict()
    checkpoint = result.completion.manifest
    assert checkpoint is not None
    stored = PlanningProtocolPersistenceService(
        db_session
    ).load_accepted_planning_brief(session.id)
    assert stored is not None
    effective = PlanningProtocolPersistenceService(db_session).effective_checkpoints(
        session.id, stage_versions={"planning_brief": 1}
    )
    brief_checkpoint = effective[("planning_brief", 1)]
    assert brief_checkpoint.status == "accepted"
    assert brief_checkpoint.brief_hash == stored.content_hash
    assert brief_checkpoint.renderer_version
    assert brief_checkpoint.validator_version
    assert brief_checkpoint.validation_json["protocol_acceptable"] is True


def test_malformed_provider_output_is_failed_without_accepted_checkpoint(db_session):
    session, manifest = _seed_session(db_session)
    provider = _Provider({"objective": {"statement": "only"}})
    engine = StageExecutor(db_session, [PlanningBriefStage(provider)])
    result = engine.advance(
        session.id,
        session_generation_id=session.generation_id,
        fencing_token=session.processing_token,
    )
    assert result.status == StageStatus.FAILED
    assert result.reason.startswith("provider_output_failure:")
    persistence = PlanningProtocolPersistenceService(db_session)
    checkpoint = persistence.effective_checkpoints(
        session.id, stage_versions={"planning_brief": 1}
    )[("planning_brief", 1)]
    assert checkpoint.status == "failed"
    assert persistence.load_accepted_planning_brief(session.id) is None


def test_validation_failure_is_classified_and_not_accepted(db_session):
    session, manifest = _seed_session(db_session)
    invalid = _candidate(manifest)
    invalid["requirements"][0]["type"] = "unsupported"
    provider = _Provider(invalid)
    engine = StageExecutor(db_session, [PlanningBriefStage(provider)])
    result = engine.advance(
        session.id,
        session_generation_id=session.generation_id,
        fencing_token=session.processing_token,
    )
    assert result.status == StageStatus.FAILED
    assert result.reason.startswith("validation_failure:")
    checkpoint = PlanningProtocolPersistenceService(db_session).effective_checkpoints(
        session.id, stage_versions={"planning_brief": 1}
    )[("planning_brief", 1)]
    assert checkpoint.status == "failed"
    assert checkpoint.validation_json["semantically_valid"] is False


def test_transport_and_application_failures_are_distinct(db_session):
    session, manifest = _seed_session(db_session)

    class _TransportProvider:
        def generate(self, _request):
            raise ConnectionError("provider unavailable")

    transport_result = StageExecutor(
        db_session, [PlanningBriefStage(_TransportProvider())]
    ).advance(
        session.id,
        session_generation_id=session.generation_id,
        fencing_token=session.processing_token,
    )
    assert transport_result.reason.startswith("transport_failure:")

    session, manifest = _seed_session(db_session)
    oversized = _Provider(_candidate(manifest))
    engine = StageExecutor(
        db_session,
        [PlanningBriefStage(oversized)],
        configuration={"max_source_chars": 1},
    )
    application_result = engine.advance(
        session.id,
        session_generation_id=session.generation_id,
        fencing_token=session.processing_token,
    )
    assert application_result.reason.startswith("application_error:")
    assert isinstance(PlanningBriefApplicationError("bounded"), RuntimeError)
    assert isinstance(PlanningBriefTransportError("transport"), RuntimeError)


def test_recovery_reloads_accepted_brief_without_provider_call(db_session):
    session, manifest = _seed_session(db_session)
    provider = _Provider(_candidate(manifest))
    engine = StageExecutor(db_session, [PlanningBriefStage(provider)])
    first = engine.advance(
        session.id,
        session_generation_id=session.generation_id,
        fencing_token=session.processing_token,
    )
    assert first.status == StageStatus.COMPLETED
    provider.requests.clear()
    recovery = engine.recover(session.id)
    assert recovery.next_stage is None
    second = engine.advance(
        session.id,
        session_generation_id=session.generation_id,
        fencing_token=session.processing_token,
    )
    assert second.status == StageStatus.COMPLETED
    assert provider.requests == []


def test_downstream_stage_context_loads_accepted_brief(db_session):
    session, manifest = _seed_session(db_session)
    provider = _Provider(_candidate(manifest))
    observed = {}
    consumer = StageDefinition(
        "brief-consumer",
        prerequisites=("planning_brief",),
        execute=lambda context: observed.setdefault(
            "hash", context.planning_brief.content_hash
        ),
    )
    result = StageExecutor(
        db_session, [PlanningBriefStage(provider), consumer]
    ).advance(
        session.id,
        session_generation_id=session.generation_id,
        fencing_token=session.processing_token,
    )
    assert result.status == StageStatus.COMPLETED
    assert observed["hash"]


def test_planning_session_registers_default_v2_graph_but_allows_explicit_empty_graph(
    db_session,
):
    from app.services.planning.planning_session_service import PlanningSessionService

    assert PlanningSessionService(db_session).stage_executor.graph.identifiers == (
        "planning_brief",
        "structured_task_plan",
    )
    assert (
        PlanningSessionService(
            db_session, stage_definitions=()
        ).stage_executor.graph.identifiers
        == ()
    )


def test_default_registry_contains_brief_then_task_plan(db_session):
    definitions = build_protocol_v2_stage_definitions(
        db_session, provider=_Provider({})
    )
    assert tuple(definition.identifier for definition in definitions) == (
        "planning_brief",
        "structured_task_plan",
    )
