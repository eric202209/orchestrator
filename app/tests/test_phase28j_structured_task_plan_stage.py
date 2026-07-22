"""Focused Protocol v2 Structured Task Plan generation-stage tests."""

from __future__ import annotations

import copy
from dataclasses import replace
import uuid

import pytest

from app.models import PlanningSession, Project
from app.services.orchestration.stage_engine import (
    StageDefinition,
    StageEngineError,
    StageExecutor,
    StageStatus,
)
from app.services.planning.input_manifest import build_input_manifest
from app.services.planning.planning_brief import (
    AcceptanceCriterion,
    Constraint,
    Goal,
    ImplementationStrategy,
)
from app.services.planning.planning_brief_stage import PlanningBriefStage
from app.services.planning.protocol_persistence import (
    PlanningProtocolPersistenceService,
)
from app.services.planning.providers import (
    PlanningArtifactKind,
    PlanningRequest,
    PlanningResponse,
    ProviderDiagnostics,
)
from app.services.planning.structured_task_plan_stage import (
    DEFAULT_TASK_PLAN_CANDIDATE_BYTES,
    StructuredTaskPlanProviderOutputError,
    StructuredTaskPlanStage,
    build_protocol_v2_stage_definitions,
    parse_structured_task_plan_candidate,
)


def _stage_config(**policy):
    limits = {
        "max_tasks": 200,
        "max_groups": 50,
        "max_dependencies_per_task": 8,
        "max_dependency_fan_in": 8,
        "max_dependency_fan_out": 8,
        "max_work_items_per_task": 8,
        "max_expected_effort": 480,
        "max_parallel_width": 4,
        "max_plan_bytes": 256 * 1024,
        "auto_accept": True,
        "max_source_chars": 20_000,
        "max_total_source_chars": 100_000,
        "max_provider_input_bytes": 512 * 1024,
        "max_candidate_bytes": DEFAULT_TASK_PLAN_CANDIDATE_BYTES,
    }
    limits.update(policy)
    return {
        "stages": [
            {"identifier": "planning_brief", "version": 1, "prerequisites": []},
            {
                "identifier": "structured_task_plan",
                "version": 1,
                "prerequisites": ["planning_brief"],
            },
        ],
        "structured_task_plan": limits,
    }


def _manifest(session_id: int, generation: str, stage_config=None):
    return build_input_manifest(
        session_id=session_id,
        session_generation_id=generation,
        planning_request={"message_id": 1, "content": "Generate a Task Plan."},
        clarification_messages=[],
        project_metadata={"project_id": 9, "name": "Task Plan stage"},
        project_rules="Preserve Protocol v1 and cover every required obligation.",
        repository={"available": False, "workspace": "/workspace"},
        runtime_configuration={
            "provider": "test",
            "backend": "test",
            "model": "test-model",
            "reasoning_profile": "default",
        },
        stage_configuration=stage_config or _stage_config(),
        manifest_built_at="2026-07-20T00:00:00+00:00",
    )


def _brief_candidate(manifest):
    source = manifest.sources[0].source_id
    return {
        "objective": {
            "statement": "Implement the accepted Task Plan.",
            "source_refs": [source],
        },
        "background": [],
        "scope": [
            {
                "classification": "in_scope",
                "statement": "Generate and validate the Task Plan.",
                "source_refs": [source],
            }
        ],
        "requirements": [
            {
                "type": "functional",
                "statement": "The accepted Task Plan covers the Brief.",
                "priority": "required",
                "source_refs": [source],
            }
        ],
        "constraints": [
            {
                "type": "phase_do_not_change",
                "statement": "Do not change Protocol v1.",
                "severity": "must",
                "enforcement": "deterministic",
                "source_refs": [source],
            }
        ],
        "acceptance_criteria": [
            {
                "statement": "The Task Plan reloads canonically.",
                "verification_method": "Run recovery tests.",
                "source_requirement_ids": ["requirements[0]"],
                "criticality": "required",
            }
        ],
        "architecture_context": [],
        "interface_contracts": [],
        "implementation_strategy": [
            {
                "statement": "Reuse Stage Engine persistence.",
                "source_refs": [source],
                "requirement_ids": ["requirements[0]"],
                "constraint_ids": ["constraints[0]"],
            }
        ],
        "validation_strategy": [
            {
                "statement": "Run deterministic Task Plan validation.",
                "source_refs": [source],
                "acceptance_criterion_ids": ["acceptance_criteria[0]"],
                "requirement_ids": ["requirements[0]"],
            }
        ],
        "assumptions": [],
        "risks": [],
        "unresolved_questions": [],
        "operator_decisions": [],
    }


def _task(
    title, objective, category, traceability, *, owner="agent", blocking="blocking"
):
    return {
        "title": title,
        "objective": objective,
        "implementation_description": "Perform one bounded authorized outcome.",
        "rationale": "The accepted Brief authorizes this atomic work.",
        "priority": "required",
        "complexity": "small",
        "estimated_effort": {
            "unit": "person_minutes",
            "lower": 5,
            "expected": 20,
            "upper": 40,
            "confidence": "medium",
        },
        "category": category,
        "execution_profile": {
            "owner_role": owner,
            "isolation": "isolated_workspace",
            "write_scope": "operator_only" if owner == "operator" else "project",
            "network": "none",
            "parallelism": "safe",
            "review": "after_task",
        },
        "blocking_state": blocking,
        "work_items": [
            {
                "action": "implement",
                "target": "app/task_plan.py",
                "deliverable": "canonical result",
                "done_when": "focused tests pass",
            }
        ],
        "traceability": traceability,
    }


def _plan_candidate(*, reverse=False, group=None):
    tasks = [
        _task(
            "Implement Task Plan",
            "Implement the canonical Task Plan outcome.",
            "implementation",
            [
                {"target_kind": "goal", "target_id": "GOAL-001", "role": "implements"},
                {
                    "target_kind": "requirement",
                    "target_id": "REQ-001",
                    "role": "implements",
                },
                {
                    "target_kind": "constraint",
                    "target_id": "CON-001",
                    "role": "constrained_by",
                },
            ],
        ),
        _task(
            "Verify Task Plan",
            "Verify the canonical Task Plan outcome.",
            "verification",
            [
                {
                    "target_kind": "acceptance_criterion",
                    "target_id": "AC-001",
                    "role": "verifies",
                }
            ],
        ),
    ]
    if reverse:
        tasks.reverse()
        dependency = {
            "prerequisite_task_id": "tasks[1]",
            "dependent_task_id": "tasks[0]",
            "type": "hard_completion",
            "reason": "implementation must precede verification",
        }
    else:
        dependency = {
            "prerequisite_task_id": "tasks[0]",
            "dependent_task_id": "tasks[1]",
            "type": "hard_completion",
            "reason": "implementation must precede verification",
        }
    candidate = {
        "tasks": tasks,
        "dependencies": [dependency],
        "execution_groups": [],
        "intentional_omissions": [],
    }
    if group:
        candidate["execution_groups"] = [group]
    return candidate


class _BriefProvider:
    def __init__(self, manifest):
        self.manifest = manifest
        self.calls = 0

    def generate(self, _request):
        self.calls += 1
        return PlanningResponse(
            candidate_text=copy.deepcopy(_brief_candidate(self.manifest)),
            provider_name="test",
            provider_version="1",
            diagnostics=ProviderDiagnostics(category="provider_success"),
            latency_seconds=0,
        )


class _TaskPlanProvider:
    def __init__(self, output):
        self.output = output
        self.requests: list[PlanningRequest] = []

    def generate(self, request):
        self.requests.append(request)
        return PlanningResponse(
            candidate_text=copy.deepcopy(self.output),
            provider_name="test",
            provider_version="1",
            diagnostics=ProviderDiagnostics(category="provider_success"),
            latency_seconds=0,
        )


class _CombinedProvider:
    def __init__(self, brief_provider, task_provider):
        self.brief_provider = brief_provider
        self.task_provider = task_provider

    def generate(self, request):
        if request.artifact_kind is PlanningArtifactKind.PLANNING_BRIEF:
            return self.brief_provider.generate(request)
        return self.task_provider.generate(request)


def _seed(db_session, *, stage_config=None):
    project = Project(
        name=f"Phase 28J {uuid.uuid4().hex[:8]}",
        workspace_path=f"phase28j-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(project)
    db_session.flush()
    session = PlanningSession(
        project_id=project.id,
        title="Structured Task Plan stage",
        prompt="Generate a Task Plan.",
        status="active",
        protocol_version="v2",
        generation_id=str(uuid.uuid4()),
        processing_token="phase28j-fence",
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)
    manifest = _manifest(session.id, session.generation_id, stage_config)
    PlanningProtocolPersistenceService(db_session).record_input_manifest(
        session.id, manifest=manifest
    )
    db_session.commit()
    return session, manifest


def _engine(db_session, session, manifest, task_output, *, configuration=None):
    brief_provider = _BriefProvider(manifest)
    task_provider = _TaskPlanProvider(task_output)
    engine = StageExecutor(
        db_session,
        build_protocol_v2_stage_definitions(
            db_session,
            planning_provider=_CombinedProvider(brief_provider, task_provider),
        ),
        configuration=configuration or _stage_config(),
    )
    return engine, brief_provider, task_provider


def test_default_registry_and_prerequisite_keep_custom_empty_registries_supported(
    db_session,
):
    definitions = build_protocol_v2_stage_definitions(db_session)
    assert tuple(item.identifier for item in definitions) == (
        "planning_brief",
        "structured_task_plan",
    )
    assert definitions[1].prerequisites == ("planning_brief",)
    assert StageExecutor(db_session, ()).graph.identifiers == ()


def test_candidate_contract_rejects_unknown_fields_ids_metadata_malformed_and_oversize():
    candidate = _plan_candidate()
    candidate["unknown"] = True
    with pytest.raises(StructuredTaskPlanProviderOutputError, match="unknown fields"):
        parse_structured_task_plan_candidate(candidate)
    candidate = _plan_candidate()
    candidate["tasks"][0]["id"] = "TASK-999"
    with pytest.raises(
        StructuredTaskPlanProviderOutputError, match="application-owned"
    ):
        parse_structured_task_plan_candidate(candidate)
    candidate = _plan_candidate()
    candidate["tasks"][0]["estimated_effort"]["id"] = "EFFORT-1"
    with pytest.raises(
        StructuredTaskPlanProviderOutputError, match="application-owned"
    ):
        parse_structured_task_plan_candidate(candidate)
    with pytest.raises(StructuredTaskPlanProviderOutputError, match="valid JSON"):
        parse_structured_task_plan_candidate("not-json")
    with pytest.raises(StructuredTaskPlanProviderOutputError, match="exceeds bound"):
        parse_structured_task_plan_candidate(_plan_candidate(), max_bytes=10)


def test_candidate_position_references_resolve_and_emission_order_is_not_authority(
    db_session,
):
    session, manifest = _seed(db_session)
    first_engine, _, first_provider = _engine(
        db_session, session, manifest, _plan_candidate()
    )
    first = first_engine.advance(
        session.id,
        session_generation_id=session.generation_id,
        fencing_token=session.processing_token,
    )
    assert first.status == StageStatus.COMPLETED, first.reason
    first_plan = PlanningProtocolPersistenceService(
        db_session
    ).load_accepted_structured_task_plan(session.id)
    assert first_plan is not None
    assert first_plan.tasks[0].id == "TASK-001"
    assert first_plan.dependencies[0].prerequisite_task_id == "TASK-001"
    assert first_provider.requests[0].protocol_input["accepted_planning_brief"][
        "checkpoint_id"
    ]

    session2, manifest2 = _seed(db_session)
    second_engine, _, _ = _engine(
        db_session, session2, manifest2, _plan_candidate(reverse=True)
    )
    second = second_engine.advance(
        session2.id,
        session_generation_id=session2.generation_id,
        fencing_token=session2.processing_token,
    )
    assert second.status == StageStatus.COMPLETED
    second_plan = PlanningProtocolPersistenceService(
        db_session
    ).load_accepted_structured_task_plan(session2.id)
    assert second_plan is not None
    normalized_second = replace(
        second_plan,
        brief_ref=first_plan.brief_ref,
        input_manifest_ref=first_plan.input_manifest_ref,
    )
    assert first_plan.canonical_bytes() == normalized_second.canonical_bytes()
    assert first_plan.content_hash == normalized_second.content_hash


def test_invalid_brief_reference_and_invalid_candidate_task_reference_are_classified(
    db_session,
):
    session, manifest = _seed(db_session)
    invalid = _plan_candidate()
    invalid["tasks"][0]["traceability"][0]["target_id"] = "REQ-999"
    engine, _, _ = _engine(db_session, session, manifest, invalid)
    result = engine.advance(
        session.id,
        session_generation_id=session.generation_id,
        fencing_token=session.processing_token,
    )
    assert result.status == StageStatus.FAILED
    assert result.reason.startswith("reference_resolution_failure:")

    session2, manifest2 = _seed(db_session)
    invalid_position = _plan_candidate()
    invalid_position["dependencies"][0]["dependent_task_id"] = "tasks[99]"
    engine2, _, _ = _engine(db_session, session2, manifest2, invalid_position)
    result2 = engine2.advance(
        session2.id,
        session_generation_id=session2.generation_id,
        fencing_token=session2.processing_token,
    )
    assert result2.status == StageStatus.FAILED
    assert result2.reason.startswith("reference_resolution_failure:")


def test_sequential_groups_materialize_edges_and_graph_failures_never_accept(
    db_session,
):
    session, manifest = _seed(db_session)
    group = {
        "kind": "sequential",
        "order": 1,
        "task_ids": ["tasks[0]", "tasks[1]"],
        "parallel_limit": 1,
        "skip_policy": "not_skippable",
    }
    grouped = _plan_candidate(group=group)
    grouped["dependencies"] = []
    engine, _, _ = _engine(db_session, session, manifest, grouped)
    result = engine.advance(
        session.id,
        session_generation_id=session.generation_id,
        fencing_token=session.processing_token,
    )
    assert result.status == StageStatus.COMPLETED, result.reason
    plan = PlanningProtocolPersistenceService(
        db_session
    ).load_accepted_structured_task_plan(session.id)
    assert plan is not None
    assert plan.execution_groups[0].id == "GROUP-001"
    assert all(edge.type == "ordering" for edge in plan.dependencies)

    session2, manifest2 = _seed(db_session)
    cyclic = _plan_candidate()
    cyclic["dependencies"].append(
        {
            "prerequisite_task_id": "tasks[1]",
            "dependent_task_id": "tasks[0]",
            "type": "hard_completion",
            "reason": "cycle",
        }
    )
    engine2, _, _ = _engine(db_session, session2, manifest2, cyclic)
    failed = engine2.advance(
        session2.id,
        session_generation_id=session2.generation_id,
        fencing_token=session2.processing_token,
    )
    assert failed.status == StageStatus.FAILED
    assert failed.reason.startswith("graph_validation_failure:")
    assert (
        PlanningProtocolPersistenceService(
            db_session
        ).load_accepted_structured_task_plan(session2.id)
        is None
    )


def test_parallel_conflict_and_fan_out_policy_are_rejected(db_session):
    session, manifest = _seed(db_session)
    parallel = _plan_candidate(
        group={
            "kind": "parallel",
            "order": 1,
            "task_ids": ["tasks[0]", "tasks[1]"],
            "parallel_limit": 2,
            "skip_policy": "not_skippable",
        }
    )
    parallel["dependencies"] = []
    engine, _, _ = _engine(db_session, session, manifest, parallel)
    result = engine.advance(
        session.id,
        session_generation_id=session.generation_id,
        fencing_token=session.processing_token,
    )
    assert result.status == StageStatus.FAILED
    assert "parallel_target_conflict" in result.reason

    config = _stage_config(max_dependency_fan_out=0)
    session2, manifest2 = _seed(db_session, stage_config=config)
    engine2, _, _ = _engine(
        db_session,
        session2,
        manifest2,
        _plan_candidate(),
        configuration=config,
    )
    failed = engine2.advance(
        session2.id,
        session_generation_id=session2.generation_id,
        fencing_token=session2.processing_token,
    )
    assert failed.status == StageStatus.FAILED
    assert "dependency_fan_limit" in failed.reason


def test_coverage_failure_and_authorized_omission_boundary(db_session):
    session, manifest = _seed(db_session)
    missing = _plan_candidate()
    missing["tasks"][0]["traceability"] = [
        {"target_kind": "goal", "target_id": "GOAL-001", "role": "implements"},
        {"target_kind": "constraint", "target_id": "CON-001", "role": "constrained_by"},
    ]
    engine, _, _ = _engine(db_session, session, manifest, missing)
    result = engine.advance(
        session.id,
        session_generation_id=session.generation_id,
        fencing_token=session.processing_token,
    )
    assert result.status == StageStatus.FAILED
    assert result.reason.startswith("coverage_validation_failure:")

    session2, manifest2 = _seed(db_session)
    invalid_omission = _plan_candidate()
    invalid_omission["intentional_omissions"] = [
        {
            "target_kind": "requirement",
            "target_id": "REQ-001",
            "reason_code": "optional_scope",
            "brief_scope_or_decision_id": "SCOPE-001",
        }
    ]
    engine2, _, _ = _engine(db_session, session2, manifest2, invalid_omission)
    failed = engine2.advance(
        session2.id,
        session_generation_id=session2.generation_id,
        fencing_token=session2.processing_token,
    )
    assert failed.status == StageStatus.FAILED
    assert failed.reason.startswith("coverage_validation_failure:")


@pytest.mark.parametrize(
    "mutation, expected_prefix",
    [
        (
            lambda candidate: candidate["tasks"][0].update(
                {"category": "operator_action"}
            )
            or candidate,
            "protocol_acceptance_failure:",
        ),
        (
            lambda candidate: candidate["tasks"][1].update(
                {"blocking_state": "review_required"}
            )
            or candidate,
            "protocol_acceptance_failure:",
        ),
        (
            lambda candidate: candidate["tasks"][1].update(
                {"objective": candidate["tasks"][0]["objective"]}
            )
            or candidate,
            "protocol_acceptance_failure:",
        ),
    ],
)
def test_review_policy_candidates_are_validated_but_not_auto_accepted(
    db_session, mutation, expected_prefix
):
    session, manifest = _seed(db_session)
    candidate = mutation(_plan_candidate())
    if candidate["tasks"][0]["category"] == "operator_action":
        candidate["tasks"][0]["execution_profile"]["owner_role"] = "operator"
        candidate["tasks"][0]["execution_profile"]["write_scope"] = "operator_only"
    engine, _, _ = _engine(db_session, session, manifest, candidate)
    result = engine.advance(
        session.id,
        session_generation_id=session.generation_id,
        fencing_token=session.processing_token,
    )
    assert result.status == StageStatus.FAILED
    assert result.reason.startswith(expected_prefix)
    checkpoint = PlanningProtocolPersistenceService(db_session).effective_checkpoints(
        session.id, stage_versions={"structured_task_plan": 1}
    )[("structured_task_plan", 1)]
    assert checkpoint.status == "failed"
    assert checkpoint.validation_json["protocol_acceptable"] is False
    assert (
        PlanningProtocolPersistenceService(
            db_session
        ).load_accepted_structured_task_plan(session.id)
        is None
    )


def test_capacity_exception_is_not_auto_accepted(db_session):
    config = _stage_config(max_tasks=1)
    session, manifest = _seed(db_session, stage_config=config)
    engine, _, _ = _engine(
        db_session, session, manifest, _plan_candidate(), configuration=config
    )
    result = engine.advance(
        session.id,
        session_generation_id=session.generation_id,
        fencing_token=session.processing_token,
    )
    assert result.status == StageStatus.FAILED
    assert "task_count_limit" in result.reason


def test_persistence_lineage_validation_evidence_and_recovery_without_provider_call(
    db_session,
):
    session, manifest = _seed(db_session)
    engine, _, provider = _engine(db_session, session, manifest, _plan_candidate())
    completed = engine.advance(
        session.id,
        session_generation_id=session.generation_id,
        fencing_token=session.processing_token,
    )
    assert completed.status == StageStatus.COMPLETED, completed.reason
    plan = PlanningProtocolPersistenceService(
        db_session
    ).load_accepted_structured_task_plan(session.id)
    assert plan is not None
    checkpoint = PlanningProtocolPersistenceService(db_session).effective_checkpoints(
        session.id, stage_versions={"structured_task_plan": 1}
    )[("structured_task_plan", 1)]
    assert (
        str(checkpoint.validation_json["brief_checkpoint_id"])
        == plan.brief_ref.checkpoint_id
    )
    assert checkpoint.validation_json["brief_hash"] == plan.brief_ref.content_hash
    assert checkpoint.validation_json["input_manifest_hash"] == manifest.manifest_hash
    assert checkpoint.validation_json["validation_hash"]
    assert any(
        parent.parent_checkpoint.stage_name == "planning_brief"
        for parent in checkpoint.dependencies
    )
    provider.requests.clear()
    recovery = engine.recover(session.id)
    assert recovery.next_stage is None
    again = engine.advance(
        session.id,
        session_generation_id=session.generation_id,
        fencing_token=session.processing_token,
    )
    assert again.status == StageStatus.COMPLETED
    assert provider.requests == []


def test_stage_context_exposes_accepted_task_plan_to_downstream_stage(db_session):
    session, manifest = _seed(db_session)
    brief_provider = _BriefProvider(manifest)
    task_provider = _TaskPlanProvider(_plan_candidate())
    observed = {}
    consumer = StageDefinition(
        "task-plan-consumer",
        prerequisites=("structured_task_plan",),
        execute=lambda context: observed.update(
            {
                "plan_hash": context.structured_task_plan.content_hash,
                "brief_hash": context.planning_brief.content_hash,
            }
        ),
    )
    engine = StageExecutor(
        db_session,
        [
            PlanningBriefStage(brief_provider),
            StructuredTaskPlanStage(task_provider),
            consumer,
        ],
        configuration=_stage_config(),
    )
    result = engine.advance(
        session.id,
        session_generation_id=session.generation_id,
        fencing_token=session.processing_token,
    )
    assert result.status == StageStatus.COMPLETED
    assert observed["plan_hash"]
    assert observed["brief_hash"]


def test_invalidated_task_plan_blocks_completion_and_recovery_marks_it_resumable(
    db_session,
):
    session, manifest = _seed(db_session)
    engine, _, _ = _engine(db_session, session, manifest, _plan_candidate())
    result = engine.advance(
        session.id,
        session_generation_id=session.generation_id,
        fencing_token=session.processing_token,
    )
    assert result.status == StageStatus.COMPLETED
    invalidated = engine.invalidate_downstream(
        session.id,
        "planning_brief",
        session_generation_id=session.generation_id,
        fencing_token=session.processing_token,
        reason="Brief changed",
    )
    assert any(item.stage_name == "structured_task_plan" for item in invalidated)
    completion = engine.evaluate_completion(
        session.id,
        session_generation_id=session.generation_id,
        fencing_token=session.processing_token,
    )
    assert completion.complete is False
    assert "invalidated" in completion.reason
    recovery = engine.recover(session.id)
    assert recovery.next_stage == "structured_task_plan"


def test_completion_manifest_binds_brief_task_plan_manifest_and_configuration_hashes(
    db_session,
):
    session, manifest = _seed(db_session)
    engine, _, _ = _engine(db_session, session, manifest, _plan_candidate())
    result = engine.advance(
        session.id,
        session_generation_id=session.generation_id,
        fencing_token=session.processing_token,
    )
    assert result.status == StageStatus.COMPLETED, result.reason
    completion = result.completion.manifest
    assert completion is not None
    stages = {
        item["stage_name"]: item for item in completion.accepted_checkpoint_versions
    }
    assert set(stages) == {"planning_brief", "structured_task_plan"}
    assert manifest.manifest_hash in completion.dependency_hashes
    assert (
        manifest.configuration_identity.stage_configuration_fingerprint
        in completion.dependency_hashes
    )


def test_integrity_failure_does_not_regenerate_or_repair_checkpoint(db_session):
    session, manifest = _seed(db_session)
    engine, _, provider = _engine(db_session, session, manifest, _plan_candidate())
    result = engine.advance(
        session.id,
        session_generation_id=session.generation_id,
        fencing_token=session.processing_token,
    )
    assert result.status == StageStatus.COMPLETED, result.reason
    checkpoint = PlanningProtocolPersistenceService(db_session).effective_checkpoints(
        session.id, stage_versions={"structured_task_plan": 1}
    )[("structured_task_plan", 1)]
    checkpoint.content = checkpoint.content + " "
    db_session.commit()
    provider.requests.clear()
    with pytest.raises(StageEngineError, match="integrity_failure"):
        engine.recover(session.id)
    assert provider.requests == []
