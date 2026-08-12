"""Phase 33D-6 provider-facing target-ID rollout tests."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from app.services.orchestration.execution.executor import ExecutorService
from app.services.orchestration.planning.semantic_target_inventory import (
    SemanticTargetContractError,
    build_semantic_target_inventory,
    normalize_provider_semantic_intents,
)
from app.services.orchestration.planning.source_materialization import (
    materialize_planner_source_context,
    render_repair_source_materialization,
)
from app.services.orchestration.validation.accepted_path_authority import (
    accepted_path_authority_from_verdict,
)
from app.services.orchestration.validation.validator import ValidatorService


def _materialize(
    root: Path,
    *,
    source: str = "needle()\n",
    task: str = "replace target.txt `needle()`",
    expected_paths: list[str] | None = None,
    supporting_paths: list[str] | None = None,
):
    (root / "target.txt").write_text(source, encoding="utf-8")
    return materialize_planner_source_context(
        root,
        task_description=task,
        expected_paths=(
            expected_paths if expected_paths is not None else ["target.txt"]
        ),
        supporting_paths=supporting_paths,
    )


def _step(operation: dict) -> list[dict]:
    return [
        {
            "step_number": 1,
            "description": "Apply the requested bounded source change",
            "commands": [],
            "verification": "test -f target.txt",
            "rollback": None,
            "expected_files": ["target.txt"],
            "ops": [operation],
        }
    ]


def _semantic_provider_response(target_id: str, *, path: str = "target.txt"):
    return _step(
        {
            "op": "replace_in_file",
            "path": path,
            "target_id": target_id,
            "new": "changed()\n",
        }
    )


def _normalize(raw_plan, root: Path, materialization):
    inventory = build_semantic_target_inventory(materialization)
    return normalize_provider_semantic_intents(
        raw_plan,
        inventory=inventory,
        project_dir=root,
        source_materialization=materialization,
    )


def test_inventory_exposes_only_provider_safe_handle_fields(tmp_path):
    materialization = _materialize(tmp_path)
    inventory = build_semantic_target_inventory(materialization)

    assert len(inventory.handles) == 1
    handle = inventory.handles[0]
    assert set(handle.to_provider_dict()) == {"target_id", "path", "label", "context"}
    assert handle.path == "target.txt"
    assert handle.target_id.startswith("tgt_")
    assert len(handle.target_id) == 28


def test_target_ids_are_stable_and_bound_to_path_and_source_version(tmp_path):
    first_materialization = _materialize(tmp_path)
    first = build_semantic_target_inventory(first_materialization).handles[0].target_id
    repeated = (
        build_semantic_target_inventory(first_materialization).handles[0].target_id
    )
    assert first == repeated

    (tmp_path / "target.txt").write_text("needle()\nchanged\n", encoding="utf-8")
    second_materialization = _materialize(tmp_path, source="needle()\nchanged\n")
    second = (
        build_semantic_target_inventory(second_materialization).handles[0].target_id
    )
    assert second != first


def test_ambiguous_readonly_and_unsafe_materialization_expose_no_handle(tmp_path):
    ambiguous = _materialize(tmp_path, source="needle()\nneedle()\n")
    assert build_semantic_target_inventory(ambiguous).handles == ()
    assert "target_id:" not in ambiguous.to_prompt_block(provider_safe=True)

    readonly = _materialize(
        tmp_path,
        source="needle()\n",
        task="inspect target.txt `needle()`",
        expected_paths=[],
        supporting_paths=["target.txt"],
    )
    assert build_semantic_target_inventory(readonly).handles == ()
    assert "target_id:" not in readonly.to_prompt_block(provider_safe=True)

    outside = tmp_path / "outside.txt"
    outside.write_text("needle()\n", encoding="utf-8")
    target = tmp_path / "target.txt"
    target.unlink()
    target.symlink_to(outside)
    unsafe = materialize_planner_source_context(
        tmp_path,
        task_description="replace target.txt `needle()`",
        expected_paths=["target.txt"],
    )
    assert build_semantic_target_inventory(unsafe).handles == ()
    assert "target_id:" not in unsafe.to_prompt_block(provider_safe=True)


def test_prompt_inventory_is_provider_safe_and_reduces_legacy_metadata(tmp_path):
    materialization = _materialize(tmp_path)
    legacy_block = materialization.to_prompt_block()
    provider_block = materialization.to_prompt_block(provider_safe=True)
    target_id = build_semantic_target_inventory(materialization).handles[0].target_id

    assert target_id in provider_block
    assert "target_id:" in provider_block
    for forbidden in (
        "start_byte:",
        "end_byte:",
        "version_identity:",
        "content_hash:",
        "selected_region_sha256:",
        "derivation_kind:",
        "selector:",
        "visible_lines:",
    ):
        assert forbidden not in provider_block
    assert "replace_in_file.old_text must occur" not in provider_block
    assert len(provider_block) < len(legacy_block)
    assert len(provider_block) // 4 <= len(legacy_block) // 4


def test_repair_prompt_projection_keeps_target_handles_without_selector_facts(tmp_path):
    materialization = _materialize(tmp_path)
    prompt = render_repair_source_materialization(
        materialization, provider_safe=True, compaction_level=0
    )
    assert "target_id:" in prompt
    assert "selector:" not in prompt
    assert "start_byte:" not in prompt
    assert "expected_source_version:" not in prompt
    assert "content_hash:" not in prompt


def test_semantic_provider_response_normalizes_to_canonical_selector_before_validation(
    tmp_path,
):
    materialization = _materialize(tmp_path)
    target_id = build_semantic_target_inventory(materialization).handles[0].target_id
    normalized = _normalize(
        _semantic_provider_response(target_id), tmp_path, materialization
    )
    operation = normalized[0]["ops"][0]

    assert set(operation) == {"op", "path", "selector", "new"}
    assert "target_id" not in operation
    assert "old" not in operation
    verdict = ValidatorService.validate_plan(
        normalized,
        output_text=json.dumps(normalized),
        task_prompt="replace target.txt `needle()`",
        execution_profile="implementation",
        project_dir=tmp_path,
        source_materialization=materialization,
    )
    assert verdict.accepted, verdict.reasons
    assert accepted_path_authority_from_verdict(verdict) is not None


def test_full_mocked_semantic_pipeline_executes_without_old_or_provider(tmp_path):
    materialization = _materialize(tmp_path)
    target_id = build_semantic_target_inventory(materialization).handles[0].target_id
    normalized = _normalize(
        _semantic_provider_response(target_id), tmp_path, materialization
    )
    verdict = ValidatorService.validate_plan(
        normalized,
        output_text=json.dumps(normalized),
        task_prompt="replace target.txt `needle()`",
        execution_profile="implementation",
        project_dir=tmp_path,
        source_materialization=materialization,
    )
    authority = accepted_path_authority_from_verdict(verdict)
    assert verdict.accepted and authority is not None
    reloaded_plan = json.loads(json.dumps(normalized))
    result = ExecutorService.execute_file_ops(
        tmp_path, reloaded_plan[0]["ops"], accepted_path_authority=authority
    )
    assert result["success"] is True, result
    assert (tmp_path / "target.txt").read_text(encoding="utf-8") == "changed()\n"
    assert '"old"' not in json.dumps(reloaded_plan)
    assert '"target_id"' not in json.dumps(reloaded_plan)


@pytest.mark.parametrize(
    ("raw_operation", "code"),
    [
        (
            {
                "op": "replace_in_file",
                "path": "target.txt",
                "target_id": "tgt_invented",
                "new": "changed()\n",
            },
            "unknown_target_id",
        ),
        (
            {
                "op": "replace_in_file",
                "path": "target.txt",
                "target_id": "tgt_invented",
                "old": "needle()\n",
                "new": "changed()\n",
            },
            "provider_mixed_old_target_id",
        ),
        (
            {
                "op": "replace_in_file",
                "path": "target.txt",
                "selector": {"start_byte": 0},
                "new": "changed()\n",
            },
            "provider_selector_internals_forbidden",
        ),
        (
            {
                "op": "replace_in_file",
                "path": "target.txt",
                "target_id": "tgt_invented",
                "new": "changed()\n",
                "start_byte": 0,
            },
            "provider_selector_internals_forbidden",
        ),
        (
            {
                "op": "replace_in_file",
                "path": "target.txt",
                "target_id": "tgt_invented",
                "selector": {"start_byte": 0},
                "new": "changed()\n",
            },
            "provider_selector_internals_forbidden",
        ),
        (
            {
                "op": "write_file",
                "path": "target.txt",
                "target_id": "tgt_invented",
                "new": "changed()\n",
            },
            "target_id_operation_forbidden",
        ),
    ],
)
def test_invalid_provider_semantic_shapes_fail_closed(tmp_path, raw_operation, code):
    materialization = _materialize(tmp_path)
    with pytest.raises(SemanticTargetContractError) as exc_info:
        _normalize(_step(raw_operation), tmp_path, materialization)
    assert exc_info.value.code == code


def test_cross_path_target_misuse_is_rejected(tmp_path):
    materialization = _materialize(tmp_path)
    target_id = build_semantic_target_inventory(materialization).handles[0].target_id
    with pytest.raises(SemanticTargetContractError) as exc_info:
        _normalize(
            _semantic_provider_response(target_id, path="other.txt"),
            tmp_path,
            materialization,
        )
    assert exc_info.value.code == "target_id_path_mismatch"


def test_stale_target_id_is_not_remapped_after_materialization_changes(tmp_path):
    first_materialization = _materialize(tmp_path)
    first_id = (
        build_semantic_target_inventory(first_materialization).handles[0].target_id
    )
    (tmp_path / "target.txt").write_text("needle()\nchanged\n", encoding="utf-8")
    second_materialization = _materialize(tmp_path, source="needle()\nchanged\n")
    assert (
        build_semantic_target_inventory(second_materialization).handles[0].target_id
        != first_id
    )
    with pytest.raises(SemanticTargetContractError) as exc_info:
        _normalize(
            _semantic_provider_response(first_id), tmp_path, second_materialization
        )
    assert exc_info.value.code == "unknown_target_id"


def test_semantic_construction_failure_does_not_downgrade_to_legacy(tmp_path):
    materialization = _materialize(tmp_path)
    target_id = build_semantic_target_inventory(materialization).handles[0].target_id
    (tmp_path / "target.txt").write_text("needle()\nchanged\n", encoding="utf-8")
    with pytest.raises(SemanticTargetContractError) as exc_info:
        _normalize(_semantic_provider_response(target_id), tmp_path, materialization)
    assert exc_info.value.code == "semantic_target_construction_version_mismatch"
    assert "old" not in str(exc_info.value).lower()


def test_legacy_provider_output_remains_unchanged_and_accepted(tmp_path):
    materialization = _materialize(tmp_path)
    legacy = _step(
        {
            "op": "replace_in_file",
            "path": "target.txt",
            "old": "needle()\n",
            "new": "changed()\n",
        }
    )
    normalized = _normalize(legacy, tmp_path, materialization)
    assert normalized == legacy
    verdict = ValidatorService.validate_plan(
        normalized,
        output_text=json.dumps(normalized),
        task_prompt="replace target.txt `needle()`",
        execution_profile="implementation",
        project_dir=tmp_path,
        source_materialization=materialization,
    )
    authority = accepted_path_authority_from_verdict(verdict)
    assert verdict.accepted and authority is not None
    result = ExecutorService.execute_file_ops(
        tmp_path, normalized[0]["ops"], accepted_path_authority=authority
    )
    assert result["success"] is True, result


def test_only_constructible_primary_target_is_inventoried(tmp_path):
    (tmp_path / "a.txt").write_text("needle()\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("needle()\nneedle()\n", encoding="utf-8")
    materialization = materialize_planner_source_context(
        tmp_path,
        task_description="replace a.txt `needle()` and b.txt `needle()`",
        expected_paths=["a.txt", "b.txt"],
    )
    inventory = build_semantic_target_inventory(materialization)
    assert [handle.path for handle in inventory.handles] == ["a.txt"]


def test_target_id_collision_fails_closed(tmp_path, monkeypatch):
    (tmp_path / "a.txt").write_text("needle()\n", encoding="utf-8")
    (tmp_path / "b.txt").write_text("needle()\n", encoding="utf-8")
    materialization = materialize_planner_source_context(
        tmp_path,
        task_description="replace a.txt `needle()` and b.txt `needle()`",
        expected_paths=["a.txt", "b.txt"],
    )
    assert sum(bool(item.target_included) for item in materialization.files) == 2

    import app.services.orchestration.planning.semantic_target_inventory as inventory_module

    monkeypatch.setattr(
        inventory_module, "_target_id_for_record", lambda _item, _path: "tgt_collision"
    )
    with pytest.raises(inventory_module.SemanticTargetInventoryError) as exc_info:
        build_semantic_target_inventory(materialization)
    assert exc_info.value.code == "target_id_collision"
