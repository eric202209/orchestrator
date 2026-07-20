"""Focused tests for the canonical Protocol v2 Planning Brief domain."""

from __future__ import annotations

from dataclasses import replace
import json
import uuid

import pytest

from app.models import PlanningSession, Project
from app.services.orchestration.stage_engine import (
    StageDefinition,
    StageExecutor,
    StageStatus,
)
from app.services.planning.input_manifest import build_input_manifest
from app.services.planning.planning_brief import (
    AcceptanceCriterion,
    ArchitectureContext,
    Assumption,
    BackgroundFact,
    Constraint,
    Goal,
    ImplementationStrategy,
    InterfaceContract,
    OperatorDecision,
    PlanningBrief,
    PlanningBriefSchemaError,
    Requirement,
    Risk,
    ScopeItem,
    SourceReference,
    UnresolvedQuestion,
    ValidationStrategy,
    diff_planning_briefs,
    project_compatibility,
    render_planning_brief,
    validate_planning_brief,
)
from app.services.planning.protocol_persistence import (
    PlanningProtocolPersistenceService,
)


def _manifest(session_id: int = 1, generation: str = "generation-1"):
    return build_input_manifest(
        session_id=session_id,
        session_generation_id=generation,
        planning_request={"message_id": 42, "content": "Implement the Brief."},
        clarification_messages=[
            {"id": 7, "role": "user", "content": "Protocol v2 only."}
        ],
        project_metadata={
            "project_id": 9,
            "name": "Brief project",
            "description": "Bounded",
        },
        project_rules="Preserve compatibility.",
        repository={
            "available": False,
            "identity": None,
            "workspace": "/workspace",
            "revision": None,
            "dirty": None,
        },
        runtime_configuration={
            "provider": "local",
            "backend": "test",
            "model": "test-model",
            "reasoning_profile": "default",
        },
        stage_configuration={
            "stages": [{"identifier": "planning_brief", "version": 1}]
        },
        selection_timestamps={
            "engineering_context": "2026-07-20T00:00:00+00:00",
            "structural_information": "2026-07-20T00:00:00+00:00",
        },
        manifest_built_at="2026-07-20T00:00:00+00:00",
    )


def _brief(manifest, *, two_requirements: bool = False):
    source = manifest.sources[0]
    source_ref = SourceReference(
        source.source_id, source.source_type, source.content_hash
    )
    requirements = [
        Requirement(
            "provider-id",
            "functional",
            "Implement the canonical Brief.",
            "required",
            (source.source_id,),
        ),
    ]
    if two_requirements:
        requirements.append(
            Requirement(
                "provider-id-2",
                "non_functional",
                "Keep serialization deterministic.",
                "required",
                (source.source_id,),
                quality_attribute="reliability",
            )
        )
    criteria = [
        AcceptanceCriterion(
            "provider-id",
            "The Brief round-trips canonically.",
            "Run the canonical serialization test.",
            ("REQ-001",),
            "required",
        ),
    ]
    validation = [
        ValidationStrategy(
            "provider-id",
            "Run deterministic Brief validation.",
            (source.source_id,),
            ("AC-001",),
            ("REQ-001",),
        ),
    ]
    if two_requirements:
        criteria.append(
            AcceptanceCriterion(
                "provider-id-2",
                "A second requirement remains covered.",
                "Run the coverage test.",
                ("REQ-002",),
                "required",
            )
        )
        validation[0] = replace(
            validation[0],
            acceptance_criterion_ids=("AC-001", "AC-002"),
            requirement_ids=("REQ-001", "REQ-002"),
        )
    return PlanningBrief.create(
        input_manifest=manifest,
        objective=Goal(
            "provider-goal",
            "Implement a canonical planning authority.",
            (source.source_id,),
        ),
        background=(
            BackgroundFact(
                "provider-fact",
                "The manifest owns provenance.",
                "verified",
                (source.source_id,),
            ),
        ),
        scope=(
            ScopeItem(
                "provider-scope",
                "in_scope",
                "Planning Brief domain only.",
                (source.source_id,),
            ),
        ),
        requirements=tuple(requirements),
        constraints=(
            Constraint(
                "provider-constraint",
                "phase_do_not_change",
                "Do not call providers.",
                "must",
                "deterministic",
                (source.source_id,),
            ),
        ),
        acceptance_criteria=tuple(criteria),
        architecture_context=(
            ArchitectureContext(
                "provider-arch",
                "persistence",
                "checkpoint",
                "Persist canonical Brief JSON.",
                (source.source_id,),
            ),
        ),
        interface_contracts=(
            InterfaceContract(
                "provider-interface",
                "api",
                "StageContext exposes an accepted Brief.",
                "additive",
                (source.source_id,),
            ),
        ),
        implementation_strategy=(
            ImplementationStrategy(
                "provider-strategy",
                "Use an immutable domain value.",
                (source.source_id,),
                ("REQ-001",),
                ("CON-001",),
            ),
        ),
        validation_strategy=tuple(validation),
        assumptions=(
            Assumption(
                "provider-assumption",
                "The existing checkpoint model remains additive.",
                (source.source_id,),
                "high",
                "Persistence would need a separate adapter.",
            ),
        ),
        risks=(
            Risk(
                "provider-risk",
                "Checkpoint metadata may be absent in old rows.",
                "low",
                "medium",
                (source.source_id,),
                "Use nullable migration columns.",
            ),
        ),
        unresolved_questions=(),
        operator_decisions=(),
        source_references=(source_ref,),
    )


def _seed_session(db_session):
    project = Project(
        name=f"Brief {uuid.uuid4().hex[:8]}",
        workspace_path=f"brief-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(project)
    db_session.flush()
    session = PlanningSession(
        project_id=project.id,
        title="Planning Brief",
        prompt="Implement the canonical Brief.",
        status="active",
        protocol_version="v2",
        generation_id=str(uuid.uuid4()),
        processing_token="brief-fence",
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)
    return session


def test_every_record_is_immutable_and_serializes_all_defined_fields():
    records = (
        BackgroundFact("FACT-001", "fact", "verified", ("source:one",)),
        ScopeItem("SCOPE-001", "in_scope", "scope", ("source:one",)),
        Requirement(
            "REQ-001",
            "non_functional",
            "requirement",
            "required",
            ("source:one",),
            "why",
            "security",
        ),
        Constraint(
            "CON-001",
            "security",
            "constraint",
            "must",
            "test",
            ("source:one",),
            ("REQ-001",),
        ),
        AcceptanceCriterion(
            "AC-001", "criterion", "test", ("REQ-001",), "required", "group"
        ),
        ArchitectureContext("ARCH-001", "component", "api", "context", ("source:one",)),
        InterfaceContract("IFACE-001", "api", "contract", "preserve", ("source:one",)),
        ImplementationStrategy(
            "STRAT-001", "strategy", ("source:one",), ("REQ-001",), ("CON-001",)
        ),
        ValidationStrategy(
            "VAL-001", "validation", ("source:one",), ("AC-001",), ("REQ-001",)
        ),
        Assumption("ASM-001", "assumption", ("source:one",), "medium", "impact"),
        Risk(
            "RISK-001", "risk", "low", "high", ("source:one",), "mitigation", "trigger"
        ),
        UnresolvedQuestion(
            "Q-001", "question", "informational", ("operator",), ("source:one",)
        ),
        OperatorDecision("DEC-001", "decision", "yes", ("source:one",), "reason"),
    )
    for record in records:
        assert record.to_dict()
        with pytest.raises((AttributeError, TypeError)):
            record.id = "changed"


def test_application_assigns_category_ids_and_canonical_hash_is_stable():
    manifest = _manifest()
    first = _brief(manifest)
    second = _brief(manifest)
    assert first.objective.id == "GOAL-001"
    assert first.requirements[0].id == "REQ-001"
    assert first.content_hash == second.content_hash
    assert first.canonical_bytes() == second.canonical_bytes()
    assert json.loads(first.canonical_json())["schema_version"] == "planning-brief/1.0"
    with pytest.raises(AttributeError):
        first.requirements = ()


def test_unknown_fields_and_reference_integrity_are_rejected():
    manifest = _manifest()
    raw = _brief(manifest).to_dict()
    raw["unknown"] = True
    with pytest.raises(PlanningBriefSchemaError, match="unknown Brief fields"):
        PlanningBrief.from_dict(raw)
    broken = replace(
        _brief(manifest),
        input_manifest_ref=replace(_brief(manifest).input_manifest_ref, hash="f" * 64),
    )
    result = validate_planning_brief(broken, input_manifest=manifest)
    assert not result.protocol_acceptable
    assert any(issue.code == "manifest_mismatch" for issue in result.errors)


def test_validation_covers_enums_scope_precedence_and_required_criteria():
    manifest = _manifest()
    brief = _brief(manifest)
    valid = validate_planning_brief(brief, input_manifest=manifest)
    assert valid.schema_valid and valid.semantically_valid and valid.protocol_acceptable
    conflict = replace(
        brief,
        scope=brief.scope
        + (
            ScopeItem(
                "SCOPE-002",
                "prohibited",
                brief.scope[0].statement,
                brief.scope[0].source_refs,
            ),
        ),
    )
    result = validate_planning_brief(conflict, input_manifest=manifest)
    assert any(issue.code == "scope_precedence_conflict" for issue in result.errors)
    uncovered = replace(
        brief,
        validation_strategy=(
            ValidationStrategy(
                "VAL-001", "No coverage", brief.validation_strategy[0].source_refs
            ),
        ),
    )
    result = validate_planning_brief(uncovered, input_manifest=manifest)
    assert any(issue.code == "acceptance_coverage_gap" for issue in result.errors)


def test_acceptance_blocking_question_is_distinct_from_schema_validity():
    manifest = _manifest()
    brief = replace(
        _brief(manifest),
        unresolved_questions=(
            UnresolvedQuestion(
                "Q-001",
                "Need operator authority.",
                "blocking",
                ("operator",),
                (manifest.sources[0].source_id,),
            ),
        ),
    )
    result = validate_planning_brief(brief, input_manifest=manifest)
    assert result.schema_valid
    assert result.semantically_valid
    assert not result.protocol_acceptable


def test_renderer_and_compatibility_projection_are_deterministic_and_escaped():
    manifest = _manifest()
    brief = replace(
        _brief(manifest),
        objective=Goal(
            "GOAL-001",
            "Render <unsafe> *plain* text.",
            _brief(manifest).objective.source_refs,
        ),
    )
    rendered = render_planning_brief(brief)
    assert rendered == render_planning_brief(brief)
    assert "&lt;unsafe&gt;" in rendered
    assert "\\*plain\\*" in rendered
    projection = project_compatibility(brief)
    assert projection.source_brief_hash == brief.content_hash
    assert projection.projection_hashes["requirements"]
    assert projection.planner_markdown == ""


def test_structural_diff_reports_record_changes_and_reordering_only():
    manifest = _manifest()
    before = _brief(manifest, two_requirements=True)
    after = replace(
        before,
        requirements=(before.requirements[1], before.requirements[0]),
        risks=before.risks
        + (Risk("RISK-002", "new risk", "low", "low", before.risks[0].source_refs),),
    )
    diff = diff_planning_briefs(before, after)
    assert "requirements" in diff.reordered_presentation
    assert any(item.record_id == "RISK-002" for item in diff.added_records)
    assert diff.changed is True


def test_brief_checkpoint_persists_canonical_json_metadata_and_stage_context(
    db_session,
):
    session = _seed_session(db_session)
    persistence = PlanningProtocolPersistenceService(db_session)
    manifest = _manifest(session.id, session.generation_id)
    persistence.record_input_manifest(session.id, manifest=manifest)
    brief = _brief(manifest)
    acceptance = validate_planning_brief(brief, input_manifest=manifest)
    checkpoint = persistence.record_planning_brief(
        session.id,
        brief=brief,
        acceptance=acceptance,
        stage_generation_id="brief-stage",
        attempt_id="brief-attempt",
        fencing_token="brief-fence",
        session_generation_id=session.generation_id,
    )
    db_session.commit()
    assert checkpoint.content == brief.canonical_json()
    assert checkpoint.content_hash == brief.content_hash
    assert checkpoint.brief_hash == brief.content_hash
    assert checkpoint.renderer_version
    assert checkpoint.validator_version
    loaded = persistence.load_accepted_planning_brief(session.id)
    assert loaded is not None and loaded.content_hash == brief.content_hash
    observed = {}
    engine = StageExecutor(
        db_session,
        [
            StageDefinition(
                "brief-consumer",
                execute=lambda context: observed.setdefault(
                    "hash",
                    (
                        context.accepted_brief.content_hash
                        if context.accepted_brief
                        else None
                    ),
                ),
            )
        ],
    )
    result = engine.advance(
        session.id,
        session_generation_id=session.generation_id,
        fencing_token="brief-fence",
    )
    assert result.status == StageStatus.COMPLETED
    assert observed["hash"] == brief.content_hash
    assert (
        persistence.planning_brief_compatibility_projection(
            session.id
        ).source_brief_hash
        == brief.content_hash
    )
