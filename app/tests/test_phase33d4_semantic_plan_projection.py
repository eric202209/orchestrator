"""Phase 33D-4 semantic Plan projection and repair-contract tests."""

from __future__ import annotations

import hashlib
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.orchestration.operations.file_ops_contract import (
    ReplaceOperationMode,
    SemanticReplaceIntent,
    classify_replace_operation,
    normalize_file_op_shape,
    replace_mode_transitions,
    validate_file_op_shape,
)
from app.services.orchestration.operations.source_region_identity import (
    SourceRegionIdentity,
)
from app.services.orchestration.execution.executor import ExecutorService
from app.services.orchestration.planning.normalization import (
    normalize_stale_replace_ops_to_small_file_writes,
)
from app.services.orchestration.planning.operation_repair import (
    OperationRepairError,
    build_operation_anchor_registry,
    merge_operation_repairs,
    parse_operation_repair_response,
    select_operation_repair_route,
)
from app.services.orchestration.planning.planner import (
    PlannerService,
    PlanningRepairOutputContractViolation,
)
from app.services.orchestration.phases.planning_repair_arbitration_control import (
    arbitrate_planning_repair_candidate,
)
from app.services.orchestration.phases.planning_support import _PlanningRetryState
from app.services.orchestration.planning.source_materialization import (
    current_source_version_identity,
    materialize_planner_source_context,
)
from app.services.orchestration.validation.accepted_path_authority import (
    accepted_plan_identity,
    plan_identity_text,
)
from app.services.orchestration.validation.validator import ValidatorService
from app.tests.phase33c4_test_helpers import executor_test_authority


def _selector(root: Path, *, path: str = "target.txt", version: str | None = None):
    source = (root / path).read_bytes()
    return SourceRegionIdentity.from_region(
        canonical_path=path,
        expected_source_version=version or current_source_version_identity(root / path),
        start_byte=0,
        end_byte=len(source),
        selected_region_sha256=hashlib.sha256(source).hexdigest(),
    )


def _step(operation: dict, *, path: str = "target.txt") -> list[dict]:
    return [
        {
            "step_number": 1,
            "description": "Apply the requested target change",
            "commands": [],
            "verification": f"test -f {path}",
            "rollback": None,
            "expected_files": [path],
            "ops": [operation],
        }
    ]


def _materialization(root: Path, *, expected_paths: list[str] | None = None):
    return materialize_planner_source_context(
        root,
        task_description="replace target.txt",
        expected_paths=expected_paths or ["target.txt"],
    )


def _semantic_operation(root: Path, *, path: str = "target.txt", new: str = "after\n"):
    return {
        "op": "replace_in_file",
        "path": path,
        "selector": _selector(root, path=path).to_dict(),
        "new": new,
    }


def _validate(root: Path, plan: list[dict], materialization=None):
    return ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="replace target.txt",
        execution_profile="implementation",
        project_dir=root,
        source_materialization=materialization or _materialization(root),
    )


def test_replace_mode_classifier_has_one_owner():
    selector = {
        "schema_version": "source-region/1",
        "canonical_path": "target.txt",
        "expected_source_version": "v1",
        "start_byte": 0,
        "end_byte": 1,
        "selected_region_sha256": hashlib.sha256(b"a").hexdigest(),
        "derivation_kind": "exact_region",
    }
    assert (
        classify_replace_operation(
            {"op": "replace_in_file", "path": "target.txt", "old": "a", "new": "b"}
        )
        is ReplaceOperationMode.LEGACY_REPLACE
    )
    assert (
        classify_replace_operation(
            {
                "op": "replace_in_file",
                "path": "target.txt",
                "selector": selector,
                "new": "b",
            }
        )
        is ReplaceOperationMode.SEMANTIC_REPLACE
    )
    assert (
        classify_replace_operation(
            {
                "op": "replace_in_file",
                "path": "target.txt",
                "old": "a",
                "selector": selector,
                "new": "b",
            }
        )
        is ReplaceOperationMode.INVALID_MIXED_REPLACE
    )
    assert (
        classify_replace_operation({"op": "write_file", "path": "x", "content": "y"})
        is ReplaceOperationMode.OTHER
    )


@pytest.mark.parametrize("alias", ["old_text", "search", "match"])
def test_legacy_aliases_canonicalize_only_to_legacy(alias):
    normalized = normalize_file_op_shape(
        {"op": "replace_in_file", "path": "target.txt", alias: "a", "replacement": "b"}
    )
    assert normalized == {
        "op": "replace_in_file",
        "path": "target.txt",
        "old": "a",
        "new": "b",
    }
    assert classify_replace_operation(normalized) is ReplaceOperationMode.LEGACY_REPLACE


def test_semantic_projection_is_canonical_and_old_free(tmp_path):
    (tmp_path / "target.txt").write_bytes(b"before\n")
    operation = _semantic_operation(tmp_path)

    normalized = normalize_file_op_shape(operation)
    intent = SemanticReplaceIntent.from_operation(normalized)

    assert normalized == intent.to_dict()
    assert normalized["path"] == "target.txt"
    assert set(normalized) == {"op", "path", "selector", "new"}
    assert "old" not in normalized
    assert "old_text" not in normalized
    assert "search" not in normalized
    assert validate_file_op_shape(normalized)


def test_mixed_old_and_selector_is_invalid_and_never_silently_drops_old(tmp_path):
    (tmp_path / "target.txt").write_bytes(b"before\n")
    materialization = _materialization(tmp_path)
    operation = {**_semantic_operation(tmp_path), "old": "before\n"}

    assert not validate_file_op_shape(operation)
    verdict = _validate(tmp_path, _step(operation), materialization)

    assert verdict.rejected
    assert "semantic_replace_mixed_operations" in verdict.details
    assert any(
        "semantic_replace_mixed_old_selector" in reason for reason in verdict.reasons
    )
    assert not any(
        "stale_replace" in reason or "empty_old" in reason for reason in verdict.reasons
    )


def test_semantic_valid_without_old_is_accepted_and_has_no_old_failure_details(
    tmp_path,
):
    (tmp_path / "target.txt").write_bytes(b"before\n")
    verdict = _validate(tmp_path, _step(_semantic_operation(tmp_path)))

    assert verdict.accepted, verdict.reasons
    assert "stale_replace_materialization" not in verdict.details
    assert "empty_replace_old_text_steps" not in verdict.details
    assert not any("old" in str(reason).lower() for reason in verdict.reasons)


def test_semantic_malformed_selector_is_a_contract_failure_not_old_repair(tmp_path):
    (tmp_path / "target.txt").write_bytes(b"before\n")
    operation = {
        "op": "replace_in_file",
        "path": "target.txt",
        "selector": {"schema_version": "wrong"},
        "new": "after\n",
    }

    verdict = _validate(tmp_path, _step(operation))

    assert verdict.rejected
    assert "invalid_ops_steps" in verdict.details["plan_schema"]["details"]
    assert not any(
        "stale_replace" in reason or "empty_old" in reason for reason in verdict.reasons
    )


def test_semantic_wrong_path_is_rejected_before_execution(tmp_path):
    (tmp_path / "target.txt").write_bytes(b"before\n")
    (tmp_path / "other.txt").write_bytes(b"other\n")
    materialization = _materialization(
        tmp_path, expected_paths=["target.txt", "other.txt"]
    )
    operation = _semantic_operation(tmp_path, path="other.txt")
    operation["selector"] = _selector(tmp_path, path="target.txt").to_dict()

    verdict = _validate(tmp_path, _step(operation, path="other.txt"), materialization)

    assert verdict.rejected
    assert "semantic_replace_contract_issues" in verdict.details
    assert not any("stale_replace" in reason for reason in verdict.reasons)


def test_semantic_wrong_version_is_rejected_without_stale_old_reason(tmp_path):
    (tmp_path / "target.txt").write_bytes(b"before\n")
    materialization = _materialization(tmp_path)
    operation = _semantic_operation(tmp_path)
    operation["selector"] = _selector(
        tmp_path, version="not-the-authoritative-version"
    ).to_dict()

    verdict = _validate(tmp_path, _step(operation), materialization)

    assert verdict.rejected
    assert not verdict.repairable
    assert "semantic_replace_version_mismatches" in verdict.details
    assert not any(
        "stale_replace" in reason or "empty_old" in reason for reason in verdict.reasons
    )


def test_semantic_validation_never_calls_legacy_old_verifier(tmp_path, monkeypatch):
    (tmp_path / "target.txt").write_bytes(b"before\n")
    import app.services.orchestration.validation.validator as validator_module

    def fail_if_called(*_args, **_kwargs):
        raise AssertionError("semantic mode entered legacy old-text verification")

    monkeypatch.setattr(validator_module, "verify_replace_operation", fail_if_called)
    verdict = _validate(tmp_path, _step(_semantic_operation(tmp_path)))

    assert verdict.accepted, verdict.reasons
    assert "source_operation_findings" not in verdict.details


def test_semantic_immediate_repair_issues_have_no_old_anchor_reasons(tmp_path):
    (tmp_path / "target.txt").write_bytes(b"before\n")
    issues = PlannerService.find_immediate_repair_step_issues(
        _step(_semantic_operation(tmp_path)),
        project_dir=tmp_path,
        source_materialization=_materialization(tmp_path),
    )

    assert "stale_replace_ops_steps" not in issues
    assert "empty_replace_old_text_steps" not in issues


def test_semantic_operation_never_enters_old_anchor_registry(tmp_path):
    (tmp_path / "target.txt").write_bytes(b"before\n")
    materialization = _materialization(tmp_path)
    finding = {
        "step_number": 1,
        "operation_index": 1,
        "relative_path": "target.txt",
        "failure_code": "stale_old_text_absent_from_current_source",
        "visibility": "full_file_verified",
        "source_version_identity": materialization.file_map()[
            "target.txt"
        ].version_identity,
    }

    with pytest.raises(OperationRepairError, match="semantic or mixed"):
        build_operation_anchor_registry(
            original_plan=_step(_semantic_operation(tmp_path)),
            rejected_findings=[finding],
            source_materialization=materialization,
            project_dir=tmp_path,
        )


def test_operation_repair_cannot_downgrade_semantic_replace_to_old_new(tmp_path):
    (tmp_path / "target.txt").write_bytes(b"before\n")
    materialization = _materialization(tmp_path)
    finding = {
        "step_number": 1,
        "operation_index": 1,
        "relative_path": "target.txt",
        "failure_code": "stale_old_text_absent_from_current_source",
        "visibility": "full_file_verified",
        "source_version_identity": materialization.file_map()[
            "target.txt"
        ].version_identity,
    }

    with pytest.raises(OperationRepairError, match="semantic or mixed"):
        merge_operation_repairs(
            original_plan=_step(_semantic_operation(tmp_path)),
            rejected_operations=[finding],
            repairs=parse_operation_repair_response(
                json.dumps(
                    {
                        "repairs": [
                            {
                                "step_number": 1,
                                "operation_index": 1,
                                "anchor_id": "anchor-1-1-1",
                                "new": "after\n",
                            }
                        ]
                    }
                )
            ),
            source_materialization=materialization,
            project_dir=tmp_path,
        )


def test_legacy_stale_finding_keeps_operation_repair_route(tmp_path):
    (tmp_path / "target.txt").write_bytes(b"before\n")
    materialization = _materialization(tmp_path)
    route = select_operation_repair_route(
        findings=[
            {
                "step_number": 1,
                "operation_index": 1,
                "relative_path": "target.txt",
                "failure_code": "stale_old_text_absent_from_current_source",
                "visibility": "full_file_verified",
                "source_version_identity": materialization.file_map()[
                    "target.txt"
                ].version_identity,
            }
        ],
        source_materialization=materialization,
        project_dir=tmp_path,
    )
    assert route.lane == "operation_level"


def test_stale_normalizer_does_not_downgrade_semantic_to_write_or_legacy(tmp_path):
    (tmp_path / "target.txt").write_bytes(b"before\n")
    operation = _semantic_operation(tmp_path, new="def value():\n    return 2\n")
    plan = _step(operation)

    normalized, metadata = normalize_stale_replace_ops_to_small_file_writes(
        plan, project_dir=tmp_path
    )

    assert metadata["changed"] is False
    assert normalized == plan
    assert (
        classify_replace_operation(normalized[0]["ops"][0])
        is ReplaceOperationMode.SEMANTIC_REPLACE
    )


def test_repair_mode_transitions_are_explicitly_detected(tmp_path):
    (tmp_path / "target.txt").write_bytes(b"before\n")
    legacy = _step(
        {
            "op": "replace_in_file",
            "path": "target.txt",
            "old": "before\n",
            "new": "after\n",
        }
    )
    semantic = _step(_semantic_operation(tmp_path))

    downgrade = replace_mode_transitions(semantic, legacy)
    upgrade = replace_mode_transitions(legacy, semantic)

    assert downgrade and downgrade[0]["from"] == "SEMANTIC_REPLACE"
    assert downgrade[0]["to"] == "LEGACY_REPLACE"
    assert upgrade and upgrade[0]["from"] == "LEGACY_REPLACE"
    assert upgrade[0]["to"] == "SEMANTIC_REPLACE"


def test_full_plan_repair_fence_rejects_semantic_to_legacy_downgrade(tmp_path):
    (tmp_path / "target.txt").write_bytes(b"before\n")
    semantic = _step(_semantic_operation(tmp_path))
    legacy = _step(
        {
            "op": "replace_in_file",
            "path": "target.txt",
            "old": "before\n",
            "new": "after\n",
        }
    )
    ctx = SimpleNamespace(
        orchestration_state=SimpleNamespace(project_dir=tmp_path, plan=legacy),
        prompt="replace target.txt",
        task=None,
    )

    with pytest.raises(
        PlanningRepairOutputContractViolation, match="changed replace operation mode"
    ):
        arbitrate_planning_repair_candidate(
            ctx=ctx,
            retry_state=_PlanningRetryState(),
            previous_plan=semantic,
            immediate_repair_issues={},
            planning_phase_event=None,
            output_text="[]",
            planning_timeout_seconds=1,
            prompt_profile=None,
            repair_planning_output=lambda **_kwargs: None,
        )


def test_semantic_json_reload_preserves_projection_identity_and_apa(tmp_path):
    (tmp_path / "target.txt").write_bytes(b"before\n")
    materialization = _materialization(tmp_path)
    plan = _step(_semantic_operation(tmp_path))
    first = _validate(tmp_path, plan, materialization)
    reloaded_plan = json.loads(json.dumps(plan, sort_keys=True))
    second = _validate(tmp_path, reloaded_plan, materialization)

    assert first.accepted and second.accepted
    assert plan_identity_text(plan) == plan_identity_text(reloaded_plan)
    assert accepted_plan_identity(plan) == accepted_plan_identity(reloaded_plan)
    assert (
        first.details["accepted_path_authority"]
        == second.details["accepted_path_authority"]
    )
    assert reloaded_plan[0]["ops"][0]["selector"] == plan[0]["ops"][0]["selector"]


def test_semantic_reloaded_operation_still_uses_d3_execution_branch(tmp_path):
    (tmp_path / "target.txt").write_bytes(b"before\n")
    plan = _step(_semantic_operation(tmp_path))
    reloaded_operation = json.loads(json.dumps(plan))[0]["ops"][0]

    result = ExecutorService.execute_file_ops(
        tmp_path,
        [reloaded_operation],
        accepted_path_authority=executor_test_authority(tmp_path, [reloaded_operation]),
    )

    assert result["success"] is True
    assert (tmp_path / "target.txt").read_bytes() == b"after\n"


def test_legacy_json_reload_preserves_identity_apa_and_legacy_mode(tmp_path):
    (tmp_path / "target.txt").write_bytes(b"before\n")
    materialization = _materialization(tmp_path)
    plan = _step(
        {
            "op": "replace_in_file",
            "path": "target.txt",
            "old": "before\n",
            "new": "after\n",
        }
    )
    first = _validate(tmp_path, plan, materialization)
    reloaded_plan = json.loads(json.dumps(plan, sort_keys=True))
    second = _validate(tmp_path, reloaded_plan, materialization)

    assert first.accepted and second.accepted
    assert (
        classify_replace_operation(reloaded_plan[0]["ops"][0])
        is ReplaceOperationMode.LEGACY_REPLACE
    )
    assert accepted_plan_identity(plan) == accepted_plan_identity(reloaded_plan)
    assert (
        first.details["accepted_path_authority"]
        == second.details["accepted_path_authority"]
    )
