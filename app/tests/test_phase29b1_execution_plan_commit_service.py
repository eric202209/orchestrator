"""Focused tests for Phase 29B-1 Execution Authority Persistence Foundation."""

from __future__ import annotations

import copy
from dataclasses import replace
import uuid

import pytest

from app.models import (
    ExecutionDependencyEdge,
    ExecutionGroup,
    ExecutionGroupMember,
    ExecutionPlan,
    ExecutionTask,
    PlanningCheckpoint,
    PlanningCommitManifest,
    PlanningCompletionManifest,
    PlanningReviewEvent,
    PlanningSession,
    Project,
)
from app.services.execution.execution_plan_commit_service import (
    DEPENDENCY_RUNTIME_CLASS_MAP,
    ExecutionPlanCommitError,
    ExecutionPlanCommitService,
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
    ExecutionGroup as PlanExecutionGroup,
    ExecutionProfile,
    InputManifestReference,
    StructuredTaskPlan,
    Task,
    Traceability,
    WorkItem,
    validate_structured_task_plan,
)


def _manifest(session_id: int, generation: str):
    return build_input_manifest(
        session_id=session_id,
        session_generation_id=generation,
        planning_request={"message_id": 42, "content": "Implement the Task Plan."},
        clarification_messages=[],
        project_metadata={
            "project_id": 9,
            "name": "Execution Plan project",
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


def _task(candidate_id, title, category, references, *, expected=20):
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


def _plan(*, manifest_id="manifest"):
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
        brief_ref=BriefReference("7", "c" * 64),
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
        execution_groups=(
            PlanExecutionGroup(
                id="",
                kind="sequential",
                order=1,
                task_ids=("implementation-candidate", "verification-candidate"),
                parallel_limit=1,
                skip_policy="not_skippable",
            ),
        ),
    )


def _seed_session(db_session):
    project = Project(
        name=f"Execution Plan {uuid.uuid4().hex[:8]}",
        workspace_path=f"execution-plan-{uuid.uuid4().hex[:8]}",
    )
    db_session.add(project)
    db_session.flush()
    session = PlanningSession(
        project_id=project.id,
        title="Execution Plan commit",
        prompt="Implement the canonical Task Plan.",
        status="active",
        protocol_version="v2",
        generation_id=str(uuid.uuid4()),
        processing_token="execution-plan-fence",
    )
    db_session.add(session)
    db_session.commit()
    db_session.refresh(session)
    return project, session


def _build_accepted_commit_authority(db_session, *, protocol_version="v2"):
    """Build a fully accepted Structured Task Plan and its commit manifest."""

    project, session = _seed_session(db_session)
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

    task_plan_checkpoint = persistence.record_structured_task_plan(
        session.id,
        task_plan=plan,
        validation=validation,
        stage_generation_id="task-plan-stage",
        attempt_id="task-plan-attempt",
        fencing_token=session.processing_token,
        session_generation_id=session.generation_id,
        parent_checkpoint_ids=(brief_checkpoint.id,),
    )
    if protocol_version != "v2":
        session.protocol_version = protocol_version

    completion_manifest = persistence.record_completion_manifest(
        session.id,
        accepted_checkpoint_versions=[
            {"checkpoint_id": brief_checkpoint.id},
            {"checkpoint_id": task_plan_checkpoint.id},
        ],
        dependency_hashes=[brief.content_hash, plan.content_hash],
        fencing_token=session.processing_token,
        session_generation_id=session.generation_id,
    )
    commit_manifest = persistence.record_commit_manifest(
        session.id,
        completion_manifest_id=completion_manifest.id,
        task_provenance={
            "task_plan_hash": plan.content_hash,
            "task_ids": sorted(task.id for task in plan.tasks),
        },
        fencing_token=session.processing_token,
        session_generation_id=session.generation_id,
    )
    db_session.commit()
    db_session.refresh(commit_manifest)
    return (
        project,
        session,
        plan,
        task_plan_checkpoint,
        completion_manifest,
        commit_manifest,
    )


def test_exact_accepted_v2_plan_commits_successfully(db_session):
    _, session, plan, checkpoint, _, commit_manifest = _build_accepted_commit_authority(
        db_session
    )
    service = ExecutionPlanCommitService(db_session)
    execution_plan = service.commit(commit_manifest.id)
    db_session.commit()

    assert execution_plan.planning_session_id == session.id
    assert execution_plan.planning_commit_manifest_id == commit_manifest.id
    assert execution_plan.source_plan_hash == plan.content_hash
    assert execution_plan.source_commit_identity == commit_manifest.commit_identity
    assert execution_plan.source_plan_checkpoint_id == checkpoint.id
    assert execution_plan.generation == 1
    assert execution_plan.protocol_version == "v2"
    assert execution_plan.status == "active"

    tasks = (
        db_session.query(ExecutionTask)
        .filter(ExecutionTask.execution_plan_id == execution_plan.id)
        .all()
    )
    assert {task.plan_task_id for task in tasks} == {task.id for task in plan.tasks}


def test_protocol_v1_is_rejected(db_session):
    (_, session, plan, checkpoint, completion_manifest, commit_manifest) = (
        _build_accepted_commit_authority(db_session)
    )
    session.protocol_version = "v1"
    db_session.commit()

    service = ExecutionPlanCommitService(db_session)
    with pytest.raises(ExecutionPlanCommitError, match="Protocol v2"):
        service.commit(commit_manifest.id)


def test_unaccepted_structured_task_plan_is_rejected(db_session):
    (project, session, plan, checkpoint, completion_manifest, commit_manifest) = (
        _build_accepted_commit_authority(db_session)
    )
    checkpoint.status = "invalidated"
    db_session.commit()

    service = ExecutionPlanCommitService(db_session)
    with pytest.raises(ExecutionPlanCommitError):
        service.commit(commit_manifest.id)


def test_source_hash_mismatch_is_rejected(db_session):
    (project, session, plan, checkpoint, completion_manifest, commit_manifest) = (
        _build_accepted_commit_authority(db_session)
    )
    commit_manifest.task_provenance = {
        **commit_manifest.task_provenance,
        "task_plan_hash": "0" * 64,
    }
    db_session.commit()

    service = ExecutionPlanCommitService(db_session)
    with pytest.raises(ExecutionPlanCommitError, match="provenance hash"):
        service.commit(commit_manifest.id)


def test_planning_session_generation_mismatch_is_rejected(db_session):
    (project, session, plan, checkpoint, completion_manifest, commit_manifest) = (
        _build_accepted_commit_authority(db_session)
    )
    commit_manifest.session_generation_id = str(uuid.uuid4())
    db_session.commit()

    service = ExecutionPlanCommitService(db_session)
    with pytest.raises(ExecutionPlanCommitError, match="generation"):
        service.commit(commit_manifest.id)


def test_completion_manifest_checkpoint_mismatch_is_rejected(db_session):
    (project, session, plan, checkpoint, completion_manifest, commit_manifest) = (
        _build_accepted_commit_authority(db_session)
    )
    versions = copy.deepcopy(completion_manifest.accepted_checkpoint_versions)
    for entry in versions:
        if entry["stage_name"] == "structured_task_plan":
            entry["content_hash"] = "1" * 64
    completion_manifest.accepted_checkpoint_versions = versions
    db_session.commit()

    service = ExecutionPlanCommitService(db_session)
    with pytest.raises(ExecutionPlanCommitError, match="binding mismatch"):
        service.commit(commit_manifest.id)


def test_all_task_ids_and_fields_are_preserved(db_session):
    _, session, plan, checkpoint, _, commit_manifest = _build_accepted_commit_authority(
        db_session
    )
    service = ExecutionPlanCommitService(db_session)
    execution_plan = service.commit(commit_manifest.id)
    db_session.commit()

    tasks = {
        task.plan_task_id: task
        for task in db_session.query(ExecutionTask)
        .filter(ExecutionTask.execution_plan_id == execution_plan.id)
        .all()
    }
    for task in plan.tasks:
        row = tasks[task.id]
        assert row.title == task.title
        assert row.blocking_state == task.blocking_state
        assert row.task_spec == task.to_dict()


def test_done_when_is_preserved_exactly(db_session):
    _, session, plan, checkpoint, _, commit_manifest = _build_accepted_commit_authority(
        db_session
    )
    service = ExecutionPlanCommitService(db_session)
    execution_plan = service.commit(commit_manifest.id)
    db_session.commit()

    tasks = {
        task.plan_task_id: task
        for task in db_session.query(ExecutionTask)
        .filter(ExecutionTask.execution_plan_id == execution_plan.id)
        .all()
    }
    for task in plan.tasks:
        row = tasks[task.id]
        assert row.done_when == [item.done_when for item in task.work_items]


def test_dependency_type_mapping(db_session):
    _, session, plan, checkpoint, _, commit_manifest = _build_accepted_commit_authority(
        db_session
    )
    service = ExecutionPlanCommitService(db_session)
    execution_plan = service.commit(commit_manifest.id)
    db_session.commit()

    edges = (
        db_session.query(ExecutionDependencyEdge)
        .filter(ExecutionDependencyEdge.execution_plan_id == execution_plan.id)
        .all()
    )
    assert edges
    for edge in edges:
        assert (
            edge.runtime_class
            == DEPENDENCY_RUNTIME_CLASS_MAP[edge.source_dependency_type]
        )


def test_unknown_dependency_type_fails_closed(db_session, monkeypatch):
    _, session, plan, checkpoint, _, commit_manifest = _build_accepted_commit_authority(
        db_session
    )
    import app.services.execution.execution_plan_commit_service as module

    monkeypatch.setattr(module, "DEPENDENCY_RUNTIME_CLASS_MAP", {})
    service = ExecutionPlanCommitService(db_session)
    with pytest.raises(ExecutionPlanCommitError, match="unknown Structured Task Plan"):
        service.commit(commit_manifest.id)
    db_session.rollback()
    assert (
        db_session.query(ExecutionPlan)
        .filter(ExecutionPlan.planning_commit_manifest_id == commit_manifest.id)
        .one_or_none()
        is None
    )


def test_groups_and_memberships_are_preserved(db_session):
    _, session, plan, checkpoint, _, commit_manifest = _build_accepted_commit_authority(
        db_session
    )
    service = ExecutionPlanCommitService(db_session)
    execution_plan = service.commit(commit_manifest.id)
    db_session.commit()

    groups = (
        db_session.query(ExecutionGroup)
        .filter(ExecutionGroup.execution_plan_id == execution_plan.id)
        .all()
    )
    assert len(groups) == len(plan.execution_groups)
    for plan_group in plan.execution_groups:
        row = next(g for g in groups if g.plan_group_id == plan_group.id)
        assert row.kind == plan_group.kind
        assert row.order_index == plan_group.order
        assert row.parallel_limit == plan_group.parallel_limit
        assert row.skip_policy == plan_group.skip_policy
        members = (
            db_session.query(ExecutionGroupMember)
            .filter(ExecutionGroupMember.execution_group_id == row.id)
            .order_by(ExecutionGroupMember.member_order.asc())
            .all()
        )
        task_by_id = {
            task.id: task.plan_task_id
            for task in db_session.query(ExecutionTask).filter(
                ExecutionTask.execution_plan_id == execution_plan.id
            )
        }
        assert (
            tuple(task_by_id[m.execution_task_id] for m in members)
            == plan_group.task_ids
        )


def test_same_commit_replay_is_idempotent(db_session):
    _, session, plan, checkpoint, _, commit_manifest = _build_accepted_commit_authority(
        db_session
    )
    service = ExecutionPlanCommitService(db_session)
    first = service.commit(commit_manifest.id)
    db_session.commit()
    second = service.commit(commit_manifest.id)
    db_session.commit()

    assert first.id == second.id
    assert (
        db_session.query(ExecutionPlan)
        .filter(ExecutionPlan.planning_commit_manifest_id == commit_manifest.id)
        .count()
        == 1
    )


def test_replay_after_session_reopen_returns_identical_graph(db_session_factory):
    db = db_session_factory()
    try:
        _, session, plan, checkpoint, _, commit_manifest = (
            _build_accepted_commit_authority(db)
        )
        service = ExecutionPlanCommitService(db)
        first = service.commit(commit_manifest.id)
        db.commit()
        first_id = first.id
        commit_manifest_id = commit_manifest.id
    finally:
        db.close()

    db2 = db_session_factory()
    try:
        service2 = ExecutionPlanCommitService(db2)
        second = service2.commit(commit_manifest_id)
        assert second.id == first_id
        service2.verify_integrity(second.id)
    finally:
        db2.close()


def test_mismatched_pre_existing_graph_fails_closed(db_session):
    _, session, plan, checkpoint, _, commit_manifest = _build_accepted_commit_authority(
        db_session
    )
    service = ExecutionPlanCommitService(db_session)
    execution_plan = service.commit(commit_manifest.id)
    db_session.commit()

    execution_plan.source_plan_hash = "f" * 64
    db_session.commit()

    with pytest.raises(ExecutionPlanCommitError, match="does not match"):
        service.commit(commit_manifest.id)


def test_commit_failure_rolls_back_all_execution_graph_rows(db_session, monkeypatch):
    _, session, plan, checkpoint, _, commit_manifest = _build_accepted_commit_authority(
        db_session
    )
    import app.services.execution.execution_plan_commit_service as module

    monkeypatch.setattr(module, "DEPENDENCY_RUNTIME_CLASS_MAP", {})
    service = ExecutionPlanCommitService(db_session)
    with pytest.raises(ExecutionPlanCommitError):
        service.commit(commit_manifest.id)
    db_session.rollback()

    assert db_session.query(ExecutionPlan).count() == 0
    assert db_session.query(ExecutionTask).count() == 0
    assert db_session.query(ExecutionDependencyEdge).count() == 0


def test_planning_artifacts_remain_unchanged(db_session):
    _, session, plan, checkpoint, completion_manifest, commit_manifest = (
        _build_accepted_commit_authority(db_session)
    )
    checkpoint_count_before = db_session.query(PlanningCheckpoint).count()
    review_event_count_before = db_session.query(PlanningReviewEvent).count()
    completion_hash_before = completion_manifest.manifest_hash
    commit_identity_before = commit_manifest.commit_identity

    service = ExecutionPlanCommitService(db_session)
    service.commit(commit_manifest.id)
    db_session.commit()

    assert db_session.query(PlanningCheckpoint).count() == checkpoint_count_before
    assert db_session.query(PlanningReviewEvent).count() == review_event_count_before
    db_session.refresh(completion_manifest)
    db_session.refresh(commit_manifest)
    assert completion_manifest.manifest_hash == completion_hash_before
    assert commit_manifest.commit_identity == commit_identity_before


def test_no_legacy_execution_activity_occurs(db_session):
    from app.models import Session, Task, TaskExecution, TaskExecutionChangeSet

    _, session, plan, checkpoint, _, commit_manifest = _build_accepted_commit_authority(
        db_session
    )
    service = ExecutionPlanCommitService(db_session)
    service.commit(commit_manifest.id)
    db_session.commit()

    assert db_session.query(Task).count() == 0
    assert db_session.query(Session).count() == 0
    assert db_session.query(TaskExecution).count() == 0
    assert db_session.query(TaskExecutionChangeSet).count() == 0
