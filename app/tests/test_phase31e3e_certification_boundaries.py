"""Phase 31E-3E certification-lane separation tests."""

from __future__ import annotations

import json
from dataclasses import replace

import pytest

from app.models import (
    Project,
    Session,
    Task,
    TaskCheckpoint,
    TaskExecution,
    TaskExecutionChangeSet,
    TaskStatus,
)
from app.services.orchestration.coordinators.completion_coordinator import (
    CompletionCoordinator,
)
from app.services.orchestration.validation.accepted_path_authority import (
    accepted_plan_identity,
)
from app.services.orchestration.validation.candidate_checks import (
    candidate_delta_identity,
)
from app.services.orchestration.validation.path_authority import (
    AcceptedPathAuthority,
    GrantClass,
    GrantProvenance,
    PathGrant,
    declare,
)
from scripts.maintenance.phase31_certification_boundaries import (
    AggregateCertificationClassification,
    CertificationBoundaryError,
    CertificationPreconditionError,
    CompletionPublicationClassification,
    ExecutionDebugRepairClassification,
    ExecutionDebugRepairEvidence,
    PlanningExecutionClassification,
    aggregate_capability_results,
    build_capability_evidence,
    build_deterministic_s1_2_fixture,
    classify_legacy_evidence,
    evaluate_completion_publication,
    replay_capability_evidence,
    validate_completion_fixture,
    validate_debug_repair_evidence,
)
from scripts.maintenance.phase31_certification_scenarios import (
    CapabilityBinding,
    ScenarioRegistryError,
    scenario_spec,
    validate_scenario_specification,
)


def _seed_successful_boundary(db_session, tmp_path):
    root = tmp_path / "s1-2-project"
    root.mkdir()
    project = Project(name="phase31e3e", workspace_path=str(root))
    db_session.add(project)
    db_session.flush()
    session = Session(
        project_id=project.id,
        name="phase31e3e-session",
        status="completed",
    )
    task_steps = [
        {
            "step_number": 1,
            "description": "Publish the accepted implementation",
            "commands": ["true"],
            "verification": "true",
            "rollback": None,
            "expected_files": [],
            "ops": [
                {"op": "write_file", "path": "app/main.py"},
                {"op": "write_file", "path": "tests/test_certification.py"},
            ],
        }
    ]
    task = Task(
        project_id=project.id,
        title="S1-2 accepted task",
        description="accepted task",
        status=TaskStatus.DONE,
        steps=json.dumps(task_steps),
        current_step=1,
    )
    db_session.add_all([session, task])
    db_session.flush()
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.DONE,
    )
    db_session.add(execution)
    db_session.flush()
    artifact_root = root / ".agent" / "change-sets" / str(execution.id) / "files"
    (artifact_root / "app").mkdir(parents=True)
    (artifact_root / "tests").mkdir(parents=True)
    (artifact_root / "app" / "main.py").write_text("# accepted\n")
    (artifact_root / "tests" / "test_certification.py").write_text(
        "def test_ok(): pass\n"
    )
    change_set = TaskExecutionChangeSet(
        project_id=project.id,
        task_id=task.id,
        session_id=session.id,
        task_execution_id=execution.id,
        base_snapshot_key=f"task-execution:{execution.id}",
        target_path=str(root),
        snapshot_exists=True,
        added_files=["app/main.py", "tests/test_certification.py"],
        modified_files=[],
        deleted_files=[],
        warning_flags=["scaffold_or_test_surface_changed"],
        status="done",
    )
    db_session.add(change_set)
    db_session.commit()
    authority = AcceptedPathAuthority.create(
        accepted_plan_identity=accepted_plan_identity(task_steps),
        workspace_identity=str(root.resolve()),
        maximum_scope_digest="0" * 64,
        grants=[
            PathGrant(
                path=declare(path),
                grant_class=GrantClass.CREATION_AUTHORIZED,
                provenance=GrantProvenance.ACCEPTED_PLAN,
            )
            for path in change_set.added_files
        ],
    )
    db_session.add(
        TaskCheckpoint(
            task_id=task.id,
            session_id=session.id,
            checkpoint_type="validation_plan",
            state_snapshot=json.dumps(
                {
                    "stage": "plan",
                    "status": "accepted",
                    "details": {"accepted_path_authority": authority.to_dict()},
                }
            ),
        )
    )
    publication_change_set = {
        "added_files": list(change_set.added_files),
        "modified_files": list(change_set.modified_files),
        "deleted_files": list(change_set.deleted_files),
    }
    db_session.add(
        TaskCheckpoint(
            task_id=task.id,
            session_id=session.id,
            checkpoint_type="validation_task_completion",
            state_snapshot=json.dumps(
                {
                    "stage": "task_completion",
                    "status": "accepted",
                    "candidate_identity": candidate_delta_identity(
                        publication_change_set, project_dir=artifact_root
                    ),
                }
            ),
        )
    )
    db_session.commit()
    fixture = build_deterministic_s1_2_fixture(
        project_id=project.id,
        session_id=session.id,
        task_id=task.id,
        task_execution_id=execution.id,
        persisted_change_set_id=change_set.id,
        accepted_task_steps=task_steps,
        source_revision="historical-seed-revision",
        executing_revision="local-harness-revision",
    )
    return project, session, task, execution, change_set, fixture


def _debug_evidence(**overrides):
    values = {
        "capability_id": "execution_debug_repair",
        "request_id": "request-1",
        "provider_backend": "openai_chat_completions",
        "provider_model": "test-model",
        "provider_envelope_type": "dict",
        "provider_envelope_metadata": {"keys": ["choices"]},
        "normalized_content_type": "str",
        "normalized_content_length": 42,
        "normalized_content_hash": "a" * 64,
        "parsed_top_level_type": "dict",
        "parser_branch": "structured_operations",
        "supported_shapes": ("mapping", "operation_list"),
        "rejection_code": None,
        "candidate_operation_count": 1,
        "triggering_failure_class": "verification_failure",
        "failed_current_step": "step-3",
        "bounded_failure_envelope": {"error_class": "pytest_failure"},
        "shape_decision": "accepted",
        "candidate_repair": {"operation_count": 1},
        "mutation_status": "applied",
        "verification_status": "passed",
        "resume_status": "resumed",
        "rollback_status": "not_required",
        "exercised": True,
        "accepted": True,
    }
    values.update(overrides)
    return ExecutionDebugRepairEvidence(**values).with_integrity()


def test_s1_2_declares_required_completion_and_optional_debug_lane():
    bindings = {item.capability_id: item for item in scenario_spec("S1-2").capabilities}
    assert bindings["planning_execution"].required
    assert bindings["completion_publication"].required
    assert bindings["execution_debug_repair"].optional
    assert bindings["execution_debug_repair"].independently_classified


def test_legacy_registry_without_capability_metadata_defaults_safely():
    legacy = replace(scenario_spec("S1-1"), capability_bindings=())
    validate_scenario_specification(legacy)
    assert {item.capability_id for item in legacy.capabilities} == {
        "planning_execution",
        "completion_publication",
        "execution_debug_repair",
    }


def test_contradictory_capability_bindings_reject():
    legacy = replace(
        scenario_spec("S1-1"),
        capability_bindings=(
            CapabilityBinding("planning_execution", "required"),
            CapabilityBinding("planning_execution", "optional"),
        ),
    )
    with pytest.raises(ScenarioRegistryError, match="duplicate capability"):
        validate_scenario_specification(legacy)


def test_fixture_successful_execution_and_verification_permit_lane_a(
    db_session, tmp_path
):
    _, _, _, _, _, fixture = _seed_successful_boundary(db_session, tmp_path)
    validate_completion_fixture(fixture, db=db_session)


@pytest.mark.parametrize(
    ("field", "value", "message"),
    [
        ("execution_status", "failed", "execution is not complete"),
        ("verification_status", "failed", "verification is not successful"),
        ("changed_file_inventory", ("app/main.py",), "inventory mismatch"),
        ("planner_contract_id", "wrong", "planner contract ID mismatch"),
        ("unsafe_failure_count", 1, "unsafe_failure_count"),
        ("duplicate_execution_count", 1, "duplicate_execution_count"),
        ("duplicate_mutation_count", 1, "duplicate_mutation_count"),
    ],
)
def test_lane_a_preconditions_fail_closed(db_session, tmp_path, field, value, message):
    _, _, _, _, _, fixture = _seed_successful_boundary(db_session, tmp_path)
    candidate = replace(fixture, **{field: value}).with_integrity()
    with pytest.raises(CertificationPreconditionError, match=message):
        validate_completion_fixture(candidate, db=db_session)


def test_tampered_fixture_hash_rejects_before_db_or_publication(db_session, tmp_path):
    _, _, _, _, _, fixture = _seed_successful_boundary(db_session, tmp_path)
    tampered = replace(fixture, source_revision="tampered")
    with pytest.raises(CertificationPreconditionError, match="integrity hash"):
        validate_completion_fixture(tampered, db=db_session)


def test_failed_execution_cannot_invoke_completion_boundary(
    db_session, tmp_path, monkeypatch
):
    _, _, _, _, _, fixture = _seed_successful_boundary(db_session, tmp_path)
    failed = replace(fixture, execution_status="failed").with_integrity()
    with pytest.raises(
        CertificationPreconditionError, match="execution is not complete"
    ):
        evaluate_completion_publication(failed, db=db_session)


def test_unsupported_fixture_schema_rejects(db_session, tmp_path):
    _, _, _, _, _, fixture = _seed_successful_boundary(db_session, tmp_path)
    unsupported = replace(fixture, schema_version="phase31-unknown/99").with_integrity()
    with pytest.raises(
        CertificationPreconditionError, match="unsupported fixture schema"
    ):
        validate_completion_fixture(unsupported, db=db_session)


def test_missing_identity_and_contradictory_intent_reject():
    fixture = build_deterministic_s1_2_fixture(
        project_id=1,
        session_id=2,
        task_id=3,
        task_execution_id=4,
        persisted_change_set_id=5,
        accepted_task_steps=["step"],
        source_revision="r1",
        executing_revision="r1",
    )
    missing = replace(fixture, project_id=0).with_integrity()
    with pytest.raises(CertificationPreconditionError, match="project_id"):
        validate_completion_fixture(missing)
    contradictory = replace(fixture, publication_expectation="UNKNOWN").with_integrity()
    with pytest.raises(CertificationPreconditionError, match="publication expectation"):
        validate_completion_fixture(contradictory)


def test_valid_s1_2_lane_a_reaches_real_review_and_publication_services(
    db_session, tmp_path
):
    _, _, _, _, _, fixture = _seed_successful_boundary(db_session, tmp_path)
    evidence = evaluate_completion_publication(
        fixture,
        db=db_session,
        workspace_review_policy="hold_nontrivial",
    )
    assert (
        evidence["classification"] == CompletionPublicationClassification.PASSED.value
    )
    assert evidence["final_review_required_decision"] is False
    assert evidence["publication_allowed"] is True
    assert evidence["publication_eligible"] is True
    assert evidence["publication_attempt"] is True
    assert evidence["publication_result"]["status"] == "published"
    assert (
        evidence["publication_persistence_identity"]["change_set_id"]
        == fixture.persisted_change_set_id
    )


def test_source_risk_and_hold_all_still_hold_lane_a(db_session, tmp_path):
    _, _, _, _, change_set, fixture = _seed_successful_boundary(db_session, tmp_path)
    change_set.warning_flags = ["security_high_risk_command"]
    db_session.commit()
    evidence = evaluate_completion_publication(
        fixture, db=db_session, workspace_review_policy="hold_nontrivial"
    )
    assert (
        evidence["classification"]
        == CompletionPublicationClassification.REVIEW_HELD.value
    )
    assert evidence["publication_attempt"] is False
    assert evidence["publication_result"]["status"] == "held_for_review"

    change_set.warning_flags = []
    db_session.commit()
    evidence = evaluate_completion_publication(
        fixture, db=db_session, workspace_review_policy="hold_all"
    )
    assert (
        evidence["classification"]
        == CompletionPublicationClassification.REVIEW_HELD.value
    )
    assert evidence["publication_attempt"] is False


def test_debug_repair_not_exercised_is_independent():
    evidence = _debug_evidence(
        exercised=False, accepted=False, shape_decision="not_exercised"
    )
    assert (
        validate_debug_repair_evidence(evidence)
        == ExecutionDebugRepairClassification.NOT_EXERCISED
    )


def test_debug_repair_rejection_is_fail_closed_and_visible():
    evidence = _debug_evidence(
        accepted=False,
        shape_decision="rejected",
        rejection_code="unsupported_shape",
        mutation_status="not_applied",
        verification_status="not_run",
        resume_status="not_resumed",
    )
    assert (
        validate_debug_repair_evidence(evidence)
        == ExecutionDebugRepairClassification.REJECTED_FAIL_CLOSED
    )


def test_debug_repair_unrestricted_payload_metadata_rejects():
    evidence = _debug_evidence(
        provider_envelope_metadata={"raw_payload": "not retained"},
    )
    with pytest.raises(CertificationBoundaryError, match="forbidden"):
        validate_debug_repair_evidence(evidence)


def test_lane_a_success_does_not_report_debug_repair_success():
    bindings = scenario_spec("S1-2").capabilities
    results = {
        "planning_execution": {
            "classification": PlanningExecutionClassification.PASSED.value
        },
        "completion_publication": {
            "classification": CompletionPublicationClassification.PASSED.value
        },
        "execution_debug_repair": {
            "classification": ExecutionDebugRepairClassification.NOT_EXERCISED.value
        },
    }
    aggregate = aggregate_capability_results(bindings, results)
    assert (
        aggregate["aggregate_classification"]
        == AggregateCertificationClassification.FULLY_CERTIFIED.value
    )
    assert aggregate["optional_capabilities"][0]["classification"] == "NOT_EXERCISED"


def test_optional_debug_failure_is_surfaced_without_falsifying_lane_a():
    bindings = scenario_spec("S1-2").capabilities
    results = {
        "planning_execution": {"classification": "PASSED"},
        "completion_publication": {"classification": "PASSED"},
        "execution_debug_repair": {"classification": "REJECTED_FAIL_CLOSED"},
    }
    aggregate = aggregate_capability_results(bindings, results)
    assert aggregate["aggregate_classification"] == "PARTIALLY_EVIDENCED"
    assert aggregate["incidental_optional_failures"] == ["execution_debug_repair"]
    assert aggregate["first_failed_required_capability"] is None


def test_required_lane_failure_determines_aggregate():
    aggregate = aggregate_capability_results(
        scenario_spec("S1-2").capabilities,
        {
            "planning_execution": {"classification": "PASSED"},
            "completion_publication": {"classification": "PUBLICATION_FAILED"},
            "execution_debug_repair": {"classification": "RECOVERED"},
        },
    )
    assert aggregate["aggregate_classification"] == "BLOCKED"
    assert aggregate["first_failed_required_capability"] == "completion_publication"


def test_historical_fixture_is_not_fresh_live_execution():
    fixture = build_deterministic_s1_2_fixture(
        project_id=1,
        session_id=2,
        task_id=3,
        task_execution_id=4,
        persisted_change_set_id=5,
        accepted_task_steps=["step"],
        source_revision="historical-revision",
        executing_revision="local-revision",
    )
    assert fixture.evidence_origin != "fresh_live_execution"
    assert "historical" in fixture.source_evidence_reference


def test_capability_evidence_hash_and_replay_are_deterministic():
    bindings = scenario_spec("S1-2").capabilities
    payload = build_capability_evidence(
        scenario_id="S1-2",
        bindings=bindings,
        results={
            "planning_execution": {"classification": "PASSED"},
            "completion_publication": {"classification": "PASSED"},
            "execution_debug_repair": {"classification": "NOT_EXERCISED"},
        },
        source_revision="r1",
        executing_revision="r1",
    )
    replay = replay_capability_evidence(payload)
    assert replay["match"] is True


def test_tampered_capability_evidence_fails_validation():
    bindings = scenario_spec("S1-2").capabilities
    payload = build_capability_evidence(
        scenario_id="S1-2",
        bindings=bindings,
        results={
            "planning_execution": {"classification": "PASSED"},
            "completion_publication": {"classification": "PASSED"},
            "execution_debug_repair": {"classification": "NOT_EXERCISED"},
        },
        source_revision="r1",
        executing_revision="r1",
    )
    payload["aggregate"]["blocked"] = True
    with pytest.raises(CertificationBoundaryError, match="tampered"):
        replay_capability_evidence(payload)


def test_legacy_phase31_evidence_gets_precise_compatibility_classification():
    assert (
        classify_legacy_evidence({"outcome_class": "FAILED_SAFE", "captured_facts": {}})
        == "LEGACY_PHASE31_READABLE_NO_CAPABILITY_SPLIT"
    )
