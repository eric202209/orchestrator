"""Focused Phase 29C-7A validation-contract boundary tests."""

from __future__ import annotations

from dataclasses import replace

import pytest
from sqlalchemy import create_engine, inspect

from app.db_migrations import _migration_038_execution_task_validation_contract
from app.models import (
    Base,
    ExecutionPlan,
    ExecutionTask,
    ExecutionTaskAttemptOutcome,
    ExecutionTaskValidationSpecification,
    PlanningCheckpoint,
)
from app.services.execution.validation_contract import (
    ValidationContractService,
    verify_execution_plan_validation_contract_integrity,
)
from app.services.planning.structured_task_plan import WorkItem
from app.services.planning.validation_contract import (
    StructuredValidationContract,
    ValidationContractError,
    ValidationEnvironmentIdentity,
    ValidationPassPolicy,
    ValidationPredicate,
    VALIDATION_CONTRACT_SCHEMA_VERSION,
    VALIDATION_ENVIRONMENT_SCHEMA_VERSION,
    build_task_validation_contract,
    canonical_validation_hash,
)
from app.services.planning.execution_commit import PlanningExecutionCommitService
from app.services.planning.operator_review import ReviewActor
from app.services.planning.operator_review_persistence import OperatorReviewService
from app.services.planning.planning_brief import validate_planning_brief
from app.services.planning.protocol_persistence import (
    PlanningProtocolPersistenceService,
)
from app.services.planning.structured_task_plan import (
    BriefReference,
    InputManifestReference,
    validate_structured_task_plan,
)

from app.tests.test_phase29b2_planning_execution_commit import (
    _build_approved_session,
    _request_for,
)
from app.tests.test_phase29b1_execution_plan_commit_service import (
    _brief,
    _manifest,
    _plan,
    _seed_session,
)


def _environment() -> dict[str, str]:
    return {
        "schema_version": VALIDATION_ENVIRONMENT_SCHEMA_VERSION,
        "validator_set_id": "deterministic_readonly",
        "validator_set_version": "1",
        "configuration_hash": "a" * 64,
        "resolver_version": "candidate-evidence-resolver/1",
        "toolchain_identity": "test-toolchain",
        "timezone": "UTC",
        "locale": "C",
    }


def _pass_policy() -> dict[str, object]:
    return {
        "policy_id": "all_required",
        "policy_version": 1,
        "optional_predicate_behavior": "ignore",
        "missing_evidence": "fail",
        "validator_error": "fail",
        "short_circuit": False,
        "review_separate_requirement": True,
    }


def _contract(
    *, predicate_id: str = "output_reference_exists"
) -> StructuredValidationContract:
    return StructuredValidationContract.from_mapping(
        {
            "schema_version": VALIDATION_CONTRACT_SCHEMA_VERSION,
            "status": "structured_executable",
            "predicates": [
                {
                    "predicate_id": predicate_id,
                    "predicate_version": 1,
                    "evidence_key": "primary_output",
                    "parameters": {},
                    "required": True,
                    "order": 10,
                }
            ],
            "evidence_descriptors": [
                {
                    "evidence_key": "primary_output",
                    "evidence_type": "candidate_output_reference",
                    "source": "candidate_outcome",
                    "required": True,
                    "expected_media_type": "application/json",
                    "expected_hash_algorithm": None,
                    "resolver_version": "candidate-evidence-resolver/1",
                }
            ],
            "pass_policy": _pass_policy(),
            "review_requirement": {
                "requirement": "none",
                "requirement_version": 1,
            },
            "environment": _environment(),
            "specification_source": "authored",
        }
    )


def test_supported_contract_is_bounded_and_canonical():
    contract = _contract()
    assert contract.predicates[0].predicate_id == "output_reference_exists"
    assert contract.environment.validator_set_id == "deterministic_readonly"
    assert canonical_validation_hash(contract.to_dict()) == canonical_validation_hash(
        StructuredValidationContract.from_mapping(contract.to_dict()).to_dict()
    )


@pytest.mark.parametrize(
    "predicate_id",
    ["python", "shell", "raw_llm_judgment", "arbitrary_sql"],
)
def test_unbounded_predicates_are_rejected(predicate_id):
    with pytest.raises(ValidationContractError) as exc_info:
        _contract(predicate_id=predicate_id)
    assert exc_info.value.code == "validation_predicate_unsupported"


def test_predicate_version_and_parameters_are_bounded():
    with pytest.raises(ValidationContractError) as exc_info:
        ValidationPredicate(
            "output_reference_exists",
            predicate_version=2,
            evidence_key="primary_output",
            parameters={},
            required=True,
            order=10,
        )
    assert exc_info.value.code == "validation_predicate_version_unsupported"

    with pytest.raises(ValidationContractError) as exc_info:
        ValidationPredicate(
            "required_fields_present",
            evidence_key="primary_output",
            parameters={"fields": ["ok"], "path": "/tmp/unbounded"},
            required=True,
            order=10,
        )
    assert exc_info.value.code == "validation_contract_parameters_invalid"


def test_pass_policy_review_and_environment_are_load_bearing_hash_inputs():
    contract = _contract()
    baseline = canonical_validation_hash(contract.to_dict())
    for changed in (
        replace(contract, review_requirement="operator_required"),
        replace(
            contract,
            environment=ValidationEnvironmentIdentity(configuration_hash="b" * 64),
        ),
    ):
        assert canonical_validation_hash(changed.to_dict()) != baseline

    altered_policy = dict(_pass_policy())
    altered_policy["policy_id"] = "all_predicates"
    with pytest.raises(ValidationContractError):
        StructuredValidationContract.from_mapping(
            {**contract.to_dict(), "pass_policy": altered_policy}
        )


def test_done_when_alone_is_legacy_and_is_not_parsed():
    item = WorkItem("implement", "app/output.json", "output", "the output is correct")
    assert "validation_contract" not in item.to_dict()
    projection = build_task_validation_contract(
        type("T", (), {"work_items": (item,)})()
    )
    assert projection.contract_status == "legacy_unstructured"
    assert projection.structured_contract is None
    assert projection.original_done_when == ("the output is correct",)


def test_structured_contract_is_explicitly_projected():
    item = WorkItem(
        "implement",
        "app/output.json",
        "output",
        "the output is present",
        validation_contract=_contract(),
    )
    projection = build_task_validation_contract(
        type("T", (), {"work_items": (item,)})()
    )
    assert projection.contract_status == "structured_executable"
    assert projection.canonical_payload["original_done_when"] == [
        "the output is present"
    ]
    assert projection.canonical_payload["structured_contract"] is not None


def test_release_binds_one_legacy_contract_per_task_and_replays(db_session):
    _project, session, _plan, _review_id, _approval, promotion = (
        _build_approved_session(db_session)
    )
    result = PlanningExecutionCommitService(db_session).commit(
        session.id, _request_for(promotion, session)
    )
    tasks = (
        db_session.query(ExecutionTask)
        .filter(ExecutionTask.execution_plan_id == result.execution_plan_id)
        .order_by(ExecutionTask.id)
        .all()
    )
    specifications = (
        db_session.query(ExecutionTaskValidationSpecification)
        .filter(
            ExecutionTaskValidationSpecification.execution_plan_id
            == result.execution_plan_id
        )
        .order_by(ExecutionTaskValidationSpecification.id)
        .all()
    )
    assert len(specifications) == len(tasks)
    assert all(item.contract_status == "legacy_unstructured" for item in specifications)
    assert all(
        item.canonical_payload["structured_contract"] is None for item in specifications
    )
    assert all(
        task.validation_contract_id == spec.id
        for task, spec in zip(tasks, specifications)
    )

    plan = db_session.get(ExecutionPlan, result.execution_plan_id)
    integrity = verify_execution_plan_validation_contract_integrity(db_session, plan.id)
    assert integrity.verified
    hashes = [item.canonical_specification_hash for item in specifications]
    replay = PlanningExecutionCommitService(db_session).commit(
        session.id, _request_for(promotion, session)
    )
    assert replay.execution_plan_id == result.execution_plan_id
    assert [item.canonical_specification_hash for item in specifications] == hashes
    assert db_session.query(ExecutionTaskValidationSpecification).count() == len(tasks)

    inspection = ValidationContractService(db_session).inspect_execution_task(
        tasks[0].id
    )
    assert inspection.contract_status == "legacy_unstructured"
    assert inspection.blocker_code == "validation_contract_unavailable"
    assert db_session.query(ExecutionTaskAttemptOutcome).count() == 0


def test_structured_work_item_contract_is_bound_at_release(db_session):
    _project, session = _seed_session(db_session)
    persistence = PlanningProtocolPersistenceService(db_session)
    manifest = _manifest(session.id, session.generation_id)
    persistence.record_input_manifest(session.id, manifest=manifest)
    brief = _brief(manifest)
    brief_checkpoint = persistence.record_planning_brief(
        session.id,
        brief=brief,
        acceptance=validate_planning_brief(brief, input_manifest=manifest),
        stage_generation_id="brief-stage",
        attempt_id="brief-attempt",
        fencing_token=session.processing_token,
        session_generation_id=session.generation_id,
    )
    candidate = _plan(manifest_id=manifest.manifest_id)
    candidate = replace(
        candidate,
        brief_ref=BriefReference(str(brief_checkpoint.id), brief.content_hash),
        input_manifest_ref=InputManifestReference(
            manifest.manifest_id, manifest.manifest_hash
        ),
        tasks=tuple(
            replace(
                task,
                work_items=tuple(
                    replace(item, validation_contract=_contract())
                    for item in task.work_items
                ),
            )
            for task in candidate.tasks
        ),
    )
    validation = validate_structured_task_plan(
        candidate, brief=brief, input_manifest=manifest
    )
    assert validation.protocol_acceptable, validation.errors
    checkpoint = persistence.record_structured_task_plan(
        session.id,
        task_plan=candidate,
        validation=validation,
        status="failed",
        stage_generation_id="task-plan-stage",
        attempt_id="task-plan-attempt",
        fencing_token=session.processing_token,
        session_generation_id=session.generation_id,
        parent_checkpoint_ids=(brief_checkpoint.id,),
        review_reason_codes=("explicit_operator_review",),
    )
    session.status = "failed"
    db_session.commit()
    review = OperatorReviewService(db_session).open_review_for_candidate(
        session.id, checkpoint.id
    )
    session.processing_token = None
    session.processing_started_at = None
    db_session.commit()
    approval = OperatorReviewService(db_session).approve_review_unchanged(
        review.review_id,
        ReviewActor("operator@example.test", "project_owner", "project_owner"),
        idempotency_key="structured-approval",
        comment="The authored structured validation contract is approved.",
    )
    db_session.commit()
    promotion = db_session.get(PlanningCheckpoint, approval.promotion.checkpoint_id)
    result = PlanningExecutionCommitService(db_session).commit(
        session.id,
        _request_for(promotion, session, idempotency_key="structured-release"),
    )
    specifications = (
        db_session.query(ExecutionTaskValidationSpecification)
        .filter_by(execution_plan_id=result.execution_plan_id)
        .all()
    )
    assert len(specifications) == len(candidate.tasks)
    assert all(
        item.contract_status == "structured_executable" for item in specifications
    )
    assert all(item.structured_contract["predicates"] for item in specifications)
    assert all(
        item.original_done_when
        == [
            task.work_items[0].done_when
            for task in candidate.tasks
            if task.id
            == db_session.get(ExecutionTask, item.execution_task_id).plan_task_id
        ]
        for item in specifications
    )


def test_contract_tampering_is_reported_without_repair(db_session):
    _project, session, _plan, _review_id, _approval, promotion = (
        _build_approved_session(db_session)
    )
    result = PlanningExecutionCommitService(db_session).commit(
        session.id, _request_for(promotion, session)
    )
    specification = (
        db_session.query(ExecutionTaskValidationSpecification)
        .filter_by(execution_plan_id=result.execution_plan_id)
        .first()
    )
    original_hash = specification.canonical_specification_hash
    specification.canonical_payload["contract_status"] = "structured_executable"
    db_session.flush()
    integrity = ValidationContractService(
        db_session
    ).verify_validation_contract_integrity(specification.id)
    assert not integrity.verified
    assert "validation_contract_hash_mismatch" in integrity.issues
    assert specification.canonical_specification_hash == original_hash


def test_migration_classifies_existing_task_without_fabricating_predicates(tmp_path):
    engine = create_engine(f"sqlite:///{tmp_path / 'validation-contract.db'}")
    Base.metadata.create_all(engine)
    with engine.begin() as connection:
        connection.exec_driver_sql(
            "INSERT INTO execution_plans "
            "(project_id, planning_session_id, planning_commit_manifest_id, "
            "generation, protocol_version, source_commit_identity, "
            "source_plan_checkpoint_id, source_plan_hash, status) "
            "VALUES (1, 1, 1, 1, 'v2', 'commit-1', 1, :hash, 'active')",
            {"hash": "a" * 64},
        )
        connection.exec_driver_sql(
            "INSERT INTO execution_tasks "
            "(execution_plan_id, plan_task_id, title, blocking_state, task_spec, "
            "done_when, status, state_version, validation_contract_status) "
            "VALUES (1, 'TASK-001', 'Task', 'blocking', :spec, :done, 'pending', 0, "
            "'legacy_unstructured')",
            {"spec": "{}", "done": '["keep this text"]'},
        )
    _migration_038_execution_task_validation_contract(engine)
    _migration_038_execution_task_validation_contract(engine)
    with engine.connect() as connection:
        row = connection.exec_driver_sql(
            "SELECT contract_status, structured_contract, original_done_when "
            "FROM execution_task_validation_specifications"
        ).one()
        assert row[0] == "legacy_unstructured"
        assert row[1] is None
        assert row[2] == '["keep this text"]'
        assert (
            connection.exec_driver_sql(
                "SELECT COUNT(*) FROM execution_task_validation_specifications"
            ).scalar_one()
            == 1
        )
    columns = {item["name"] for item in inspect(engine).get_columns("execution_plans")}
    assert "validation_contract_set_hash" in columns
    engine.dispose()
