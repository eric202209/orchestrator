"""Focused tests for the immutable Protocol v2 Structured Task Plan domain."""

from __future__ import annotations

from dataclasses import replace
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
    Constraint,
    Goal,
    ImplementationStrategy,
    PlanningBrief,
    Requirement,
    ScopeItem,
    SourceReference,
    ValidationStrategy,
    validate_planning_brief,
)
from app.services.planning.protocol_persistence import (
    PlanningProtocolPersistenceService,
)
from app.services.planning.structured_task_plan import (
    BriefReference,
    Dependency,
    EffortEstimate,
    ExecutionGroup,
    ExecutionProfile,
    InputManifestReference,
    StructuredTaskPlan,
    StructuredTaskPlanSchemaError,
    StructuredTaskPlanValidationError,
    Task,
    Traceability,
    WorkItem,
    build_dependency_graph,
    canonical_json_bytes,
    diff_structured_task_plans,
    project_structured_task_plan,
    render_structured_task_plan,
    validate_structured_task_plan,
)


def _manifest(session_id: int = 1, generation: str = "generation-1"):
    return build_input_manifest(
        session_id=session_id,
        session_generation_id=generation,
        planning_request={"message_id": 42, "content": "Implement the Task Plan."},
        clarification_messages=[],
        project_metadata={
            "project_id": 9,
            "name": "Task Plan project",
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
        },
        stage_configuration={
            "stages": [{"identifier": "planning_brief", "version": 1}]
        },
        selection_timestamps={"engineering_context": "2026-07-20T00:00:00+00:00"},
        manifest_built_at="2026-07-20T00:00:00+00:00",
    )


def _brief(manifest):
    source = manifest.sources[0]
    source_ref = SourceReference(
        source.source_id, source.source_type, source.content_hash
    )
    return PlanningBrief.create(
        input_manifest=manifest,
        objective=Goal(
            "provider-goal", "Implement the canonical Task Plan.", (source.source_id,)
        ),
        scope=(
            ScopeItem(
                "provider-scope",
                "in_scope",
                "Task Plan domain only.",
                (source.source_id,),
            ),
        ),
        requirements=(
            Requirement(
                "provider-req",
                "functional",
                "Implement the immutable Task Plan.",
                "required",
                (source.source_id,),
            ),
        ),
        constraints=(
            Constraint(
                "provider-con",
                "phase_do_not_change",
                "Do not call providers.",
                "must",
                "deterministic",
                (source.source_id,),
            ),
        ),
        acceptance_criteria=(
            AcceptanceCriterion(
                "provider-ac",
                "The Task Plan round-trips canonically.",
                "Run the domain tests.",
                ("REQ-001",),
                "required",
            ),
        ),
        implementation_strategy=(
            ImplementationStrategy(
                "provider-strategy",
                "Use immutable domain records.",
                (source.source_id,),
                ("REQ-001",),
                ("CON-001",),
            ),
        ),
        validation_strategy=(
            ValidationStrategy(
                "provider-validation",
                "Run deterministic validation.",
                (source.source_id,),
                ("AC-001",),
                ("REQ-001",),
            ),
        ),
        source_references=(source_ref,),
    )


def _task(
    candidate_id: str,
    title: str,
    category: str,
    references: tuple[Traceability, ...],
    *,
    expected: int = 20,
):
    return Task(
        id=candidate_id,
        title=title,
        objective=title + " outcome",
        implementation_description="Perform the bounded canonical work.",
        rationale="The accepted Brief authorizes this atomic outcome.",
        priority="required",
        complexity="small",
        estimated_effort=EffortEstimate(
            unit="person_minutes",
            lower=expected // 2,
            expected=expected,
            upper=expected * 2,
            confidence="medium",
        ),
        category=category,
        execution_profile=ExecutionProfile(
            "agent", "isolated_workspace", "project", "none", "safe", "after_task"
        ),
        blocking_state="blocking",
        work_items=(
            WorkItem(
                "implement",
                "app/task_plan.py",
                "canonical result",
                "focused tests pass",
            ),
        ),
        traceability=references,
    )


def _plan(*, brief_checkpoint_id: str = "7", manifest_id: str = "manifest"):
    implementation = _task(
        "implementation-candidate",
        "Implement Task Plan",
        "implementation",
        (
            Traceability("goal", "GOAL-001", "implements"),
            Traceability("requirement", "REQ-001", "implements"),
            Traceability("constraint", "CON-001", "constrained_by"),
        ),
    )
    verification = _task(
        "verification-candidate",
        "Verify Task Plan",
        "verification",
        (Traceability("acceptance_criterion", "AC-001", "verifies"),),
        expected=10,
    )
    return StructuredTaskPlan.create(
        brief_ref=BriefReference(brief_checkpoint_id, "c" * 64),
        input_manifest_ref=InputManifestReference(manifest_id, "d" * 64),
        tasks=(implementation, verification),
        dependencies=(
            Dependency(
                "candidate-dependency",
                "implementation-candidate",
                "verification-candidate",
                "hard_completion",
                "implementation artifact is required",
            ),
        ),
    )


def _seed_session(db_session):
    project = Project(
        name=f"Task Plan {uuid.uuid4().hex[:8]}",
        workspace_path=f"task-plan-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(project)
    db_session.flush()
    session = PlanningSession(
        project_id=project.id,
        title="Structured Task Plan",
        prompt="Implement the canonical Task Plan.",
        status="active",
        protocol_version="v2",
        generation_id=str(uuid.uuid4()),
        processing_token="task-plan-fence",
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)
    return session


def test_records_are_immutable_and_canonical_ids_are_application_owned():
    plan = _plan()
    assert [task.id for task in plan.tasks] == ["TASK-001", "TASK-002"]
    assert [dependency.id for dependency in plan.dependencies] == ["DEP-001"]
    assert plan.tasks[0].id not in {
        "implementation-candidate",
        "verification-candidate",
    }
    with pytest.raises(AttributeError):
        plan.tasks = ()
    assert plan.canonical_bytes() == canonical_json_bytes(plan.to_dict())
    assert plan.content_hash == plan.task_plan_hash


def test_graph_orders_deterministically_and_calculates_critical_path():
    plan = _plan()
    graph = build_dependency_graph(plan)
    assert graph.topological_order == plan.topology.topological_order
    assert graph.critical_path == plan.topology.critical_path
    assert graph.critical_path_effort == 30
    assert graph.predecessors("TASK-001") == ()
    assert graph.successors("TASK-001") == ("TASK-002",)


def test_execution_groups_are_explicit_graph_constraints():
    first = _task(
        "first",
        "First",
        "implementation",
        (Traceability("goal", "GOAL-001", "implements"),),
    )
    second = _task(
        "second",
        "Second",
        "implementation",
        (Traceability("goal", "GOAL-001", "implements"),),
    )
    plan = StructuredTaskPlan.create(
        brief_ref=BriefReference("7", "a" * 64),
        input_manifest_ref=InputManifestReference("manifest", "b" * 64),
        tasks=(second, first),
        execution_groups=(
            ExecutionGroup(
                "candidate-group",
                "sequential",
                1,
                ("first", "second"),
                1,
                "not_skippable",
            ),
        ),
    )
    assert plan.execution_groups[0].id == "GROUP-001"
    assert plan.dependencies[0].type == "ordering"
    assert validate_structured_task_plan(plan).protocol_acceptable


def test_cycle_detection_is_a_validation_error():
    plan = _plan()
    cyclic = replace(
        plan,
        dependencies=(
            Dependency("DEP-001", "TASK-001", "TASK-002", "hard_completion", "forward"),
            Dependency(
                "DEP-002", "TASK-002", "TASK-001", "hard_completion", "backward"
            ),
        ),
        topology=type(plan.topology)(),
    )
    result = validate_structured_task_plan(cyclic)
    assert any(issue.code == "dependency_cycle" for issue in result.errors)
    with pytest.raises(StructuredTaskPlanValidationError):
        from app.services.planning.structured_task_plan import (
            require_valid_structured_task_plan,
        )

        require_valid_structured_task_plan(cyclic)


def test_validation_covers_brief_obligations_and_rejects_orphans():
    manifest = _manifest()
    brief = _brief(manifest)
    plan = _plan(manifest_id=manifest.manifest_id)
    plan = replace(
        plan,
        brief_ref=BriefReference("7", brief.content_hash),
        input_manifest_ref=InputManifestReference(
            manifest.manifest_id, manifest.manifest_hash
        ),
    )
    valid = validate_structured_task_plan(plan, brief=brief, input_manifest=manifest)
    assert valid.protocol_acceptable, valid.errors
    orphan = replace(
        plan,
        tasks=plan.tasks
        + (
            _task(
                "orphan",
                "Orphan",
                "implementation",
                (Traceability("scope", "SCOPE-999", "implements"),),
            ),
        ),
    )
    result = validate_structured_task_plan(orphan, brief=brief, input_manifest=manifest)
    assert any(issue.code == "orphan_task" for issue in result.errors)


def test_renderer_projection_and_structural_diff_ignore_markdown_formatting():
    plan = _plan()
    rendered = render_structured_task_plan(plan)
    assert rendered == render_structured_task_plan(plan)
    projection = project_structured_task_plan(plan)
    assert projection.source_plan_hash == plan.content_hash
    changed = replace(
        plan, tasks=(replace(plan.tasks[0], title="Changed title"), plan.tasks[1])
    )
    diff = diff_structured_task_plans(plan, changed)
    assert diff.changed
    assert diff.changed_tasks[0].task_id == plan.tasks[0].id
    assert not diff.topology_changed
    assert project_structured_task_plan(plan).markdown == rendered


def test_unknown_canonical_fields_are_rejected():
    raw = _plan().to_dict()
    raw["unknown"] = True
    with pytest.raises(StructuredTaskPlanSchemaError):
        StructuredTaskPlan.from_dict(raw)


def test_checkpoint_persistence_and_stage_context_reload_without_provider(db_session):
    session = _seed_session(db_session)
    persistence = PlanningProtocolPersistenceService(db_session)
    manifest = _manifest(session.id, session.generation_id)
    persistence.record_input_manifest(session.id, manifest=manifest)
    brief = _brief(manifest)
    brief_acceptance = validate_planning_brief(brief, input_manifest=manifest)
    brief_checkpoint = persistence.record_planning_brief(
        session.id,
        brief=brief,
        acceptance=brief_acceptance,
        stage_generation_id="brief-stage",
        attempt_id="brief-attempt",
        fencing_token=session.processing_token,
        session_generation_id=session.generation_id,
    )
    plan = _plan(manifest_id=manifest.manifest_id)
    plan = replace(
        plan,
        brief_ref=BriefReference(str(brief_checkpoint.id), brief.content_hash),
        input_manifest_ref=InputManifestReference(
            manifest.manifest_id, manifest.manifest_hash
        ),
    )
    validation = validate_structured_task_plan(
        plan, brief=brief, input_manifest=manifest
    )
    assert validation.protocol_acceptable, validation.errors
    checkpoint = persistence.record_structured_task_plan(
        session.id,
        task_plan=plan,
        validation=validation,
        stage_generation_id="task-plan-stage",
        attempt_id="task-plan-attempt",
        fencing_token=session.processing_token,
        session_generation_id=session.generation_id,
        parent_checkpoint_ids=(brief_checkpoint.id,),
    )
    db_session.commit()
    assert checkpoint.content == plan.canonical_json()
    assert checkpoint.content_hash == plan.content_hash
    assert checkpoint.validation_json["task_plan_hash"] == plan.content_hash
    loaded = persistence.load_accepted_structured_task_plan(session.id)
    assert loaded is not None and loaded.content_hash == plan.content_hash

    observed = {}
    engine = StageExecutor(
        db_session,
        [
            StageDefinition(
                "task-plan-consumer",
                execute=lambda context: observed.setdefault(
                    "hash",
                    (
                        context.accepted_structured_task_plan.content_hash
                        if context.accepted_structured_task_plan
                        else None
                    ),
                ),
            )
        ],
    )
    result = engine.advance(
        session.id,
        session_generation_id=session.generation_id,
        fencing_token=session.processing_token,
    )
    assert result.status == StageStatus.COMPLETED
    assert observed["hash"] == plan.content_hash
