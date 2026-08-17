"""Provider-free R2A Planning contract projection tests."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.orchestration.context.assembly import assemble_planning_prompt
from app.services.orchestration.planning.prompt_contracts import (
    render_operation_choice_contract,
)
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.semantic_target_inventory import (
    SemanticTargetContractError,
    build_semantic_target_inventory,
    normalize_provider_semantic_intents,
)
from app.services.orchestration.planning.source_materialization import (
    materialize_planner_source_context,
    provider_planning_contract_capabilities,
)
from app.services.orchestration.prompt_templates import (
    OrchestrationState,
    PromptTemplates,
)
from app.services.orchestration.validation.accepted_path_authority import (
    accepted_path_authority_from_verdict,
)
from app.services.orchestration.validation.path_authority import declare
from app.services.orchestration.validation.validator import ValidatorService


BEFORE_OPERATION_CONTRACT = (
    "Accepted replace shapes are `{op,path,old,new}` or `{op,path,target_id,new}`. "
    "For replace_in_file, use a listed Orchestrator `target_id` for the exact path; "
    "otherwise use exact `old` plus `new` from current evidence. Never mix `old` "
    "with `target_id`, invent IDs, or emit selector internals (offsets, versions, "
    "hashes, or derivation data)."
)
ZERO_HANDLE_UNGROUNDED_CONTRACT = render_operation_choice_contract(
    semantic_mode_available=False,
    legacy_replace_available=False,
)
ZERO_HANDLE_GROUNDED_CONTRACT = render_operation_choice_contract(
    semantic_mode_available=False,
    legacy_replace_available=True,
)


def _materialize(
    root: Path,
    *,
    source: str = "needle()\n",
    task: str = "Replace the exact snippet `needle()` in target.txt.",
    expected_paths: list[str] | None = None,
):
    (root / "target.txt").write_text(source, encoding="utf-8")
    return materialize_planner_source_context(
        root,
        task_description=task,
        expected_paths=(
            expected_paths if expected_paths is not None else ["target.txt"]
        ),
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


def _zero_handle_materialization(root: Path, *, grounded: bool):
    if grounded:
        return _materialize(
            root,
            task="Update target.txt without a source target hint.",
        )
    return materialize_planner_source_context(
        root,
        task_description="Create a new generated.py file.",
        expected_paths=["generated.py"],
    )


def _planning_prompt(root: Path, materialization) -> str:
    prompt = PromptTemplates.build_planning_prompt(
        task_description="Implement the requested bounded change.",
        project_context="Existing workspace.",
        project_dir=str(root),
        project_structure_capsule="- target.txt",
        source_materialization=materialization,
    )
    return prompt + "\n\n" + materialization.to_prompt_block(provider_safe=True)


def _assembled_prompt(root: Path, materialization) -> str:
    state = OrchestrationState(
        session_id="r2a-test",
        task_description="Implement the requested bounded change.",
        project_name="r2a-test",
        project_context="Existing workspace.",
        task_id=1,
    )
    state._project_dir_override = str(root)
    ctx = SimpleNamespace(
        db=None,
        prompt="Implement the requested bounded change.",
        execution_profile="full_lifecycle",
        workflow_profile="default",
        planning_adaptation_profile="local_qwen_json_array",
        orchestration_state=state,
        planner_source_materialization=materialization,
    )
    return assemble_planning_prompt(
        ctx,
        {"file_count": len(materialization.files), "source_file_count": 1},
    )


def _python_comment_materialization(root: Path):
    (root / "target.py").write_text(
        "# needle()\n\n\ndef run():\n    return 1\n",
        encoding="utf-8",
    )
    return materialize_planner_source_context(
        root,
        task_description="Replace `needle()` in target.py.",
        expected_paths=["target.py"],
    )


def test_zero_handles_remove_semantic_mode_and_preserve_grounded_legacy(tmp_path):
    materialization = _zero_handle_materialization(tmp_path, grounded=True)
    assert provider_planning_contract_capabilities(materialization) == (False, True)

    prompt = _planning_prompt(tmp_path, materialization)

    assert BEFORE_OPERATION_CONTRACT not in prompt
    assert ZERO_HANDLE_GROUNDED_CONTRACT in prompt
    assert "{op,path,target_id,new}" not in prompt
    assert "Semantic target mode is unavailable for this task." in prompt
    assert "{op,path,old,new}" in prompt


def test_zero_handles_without_source_keep_nonsemantic_write_contract(tmp_path):
    materialization = _zero_handle_materialization(tmp_path, grounded=False)
    assert provider_planning_contract_capabilities(materialization) == (False, False)

    prompt = _planning_prompt(tmp_path, materialization)

    assert BEFORE_OPERATION_CONTRACT not in prompt
    assert ZERO_HANDLE_UNGROUNDED_CONTRACT in prompt
    assert "{op,path,target_id,new}" not in prompt
    assert "target_id:" not in materialization.to_prompt_block(provider_safe=True)
    assert "{op,path,old,new}" not in prompt
    assert "write_file" in prompt
    assert "Semantic target mode is unavailable for this task." in prompt


def test_positive_handle_preserves_semantic_contract_and_inventory(tmp_path):
    materialization = _materialize(tmp_path)
    inventory = build_semantic_target_inventory(materialization)
    assert len(inventory.handles) == 1
    target_id = inventory.handles[0].target_id
    assert provider_planning_contract_capabilities(materialization) == (True, True)

    prompt = _planning_prompt(tmp_path, materialization)

    assert BEFORE_OPERATION_CONTRACT in prompt
    assert "{op,path,target_id,new}" in prompt
    assert f"target_id: {target_id}" in prompt
    assert "selector: {" not in prompt
    assert "start_byte:" not in prompt
    assert "content_hash:" not in prompt


def test_multiple_handles_remain_bounded_and_provider_visible(tmp_path):
    (tmp_path / "first.py").write_text("needle()\n", encoding="utf-8")
    (tmp_path / "second.py").write_text("mark()\n", encoding="utf-8")
    materialization = materialize_planner_source_context(
        tmp_path,
        task_description="Replace `needle()` in first.py and `mark()` in second.py.",
        expected_paths=["first.py", "second.py"],
    )
    inventory = build_semantic_target_inventory(materialization)
    prompt = _planning_prompt(tmp_path, materialization)

    assert len(inventory.handles) == 2
    assert provider_planning_contract_capabilities(materialization)[0] is True
    assert all(
        f"target_id: {handle.target_id}" in prompt for handle in inventory.handles
    )
    assert "{op,path,target_id,new}" in prompt


@pytest.mark.parametrize(
    "materialization_factory",
    [
        lambda root: _materialize(root, source="needle()\nneedle()\n"),
        lambda root: _python_comment_materialization(root),
        lambda root: _materialize(
            root,
            task="Inspect target.txt `needle()`.",
            expected_paths=[],
        ),
    ],
)
def test_filtered_to_zero_handles_removes_semantic_mode(
    tmp_path, materialization_factory
):
    materialization = materialization_factory(tmp_path)
    assert build_semantic_target_inventory(materialization).handles == ()
    prompt = _planning_prompt(tmp_path, materialization)

    assert "{op,path,target_id,new}" not in prompt
    assert "Semantic target mode is unavailable for this task." in prompt


def test_readonly_zero_handle_source_does_not_expose_legacy_replace(tmp_path):
    materialization = _materialize(
        tmp_path,
        task="Inspect target.txt `needle()`.",
        expected_paths=[],
    )
    prompt = _planning_prompt(tmp_path, materialization)

    assert provider_planning_contract_capabilities(materialization) == (False, False)
    assert "{op,path,target_id,new}" not in prompt
    assert "{op,path,old,new}" not in prompt


def test_local_qwen_final_assembled_zero_handle_prompt_stays_simplified(tmp_path):
    materialization = _zero_handle_materialization(tmp_path, grounded=False)
    prompt = _assembled_prompt(tmp_path, materialization)

    assert "{op,path,target_id,new}" not in prompt
    assert "{op,path,old,new}" not in prompt
    assert "Semantic target mode is unavailable for this task." in prompt
    assert "Do not emit `target_id`." in prompt
    assert "write_file" in prompt


def test_local_qwen_minimal_and_ultra_fallbacks_keep_zero_handle_mode_removed(tmp_path):
    materialization = _zero_handle_materialization(tmp_path, grounded=False)

    minimal = PlannerService.build_minimal_planning_prompt(
        "Create a new generated.py file.",
        tmp_path,
        prompt_profile="local_qwen_json_array",
        source_materialization=materialization,
    )
    ultra = PlannerService.build_ultra_minimal_planning_prompt(
        "Create a new generated.py file.",
        tmp_path,
        prompt_profile="local_qwen_json_array",
        source_materialization=materialization,
    )

    for prompt in (minimal, ultra):
        assert "{op,path,target_id,new}" not in prompt
        assert "{op,path,old,new}" not in prompt
        assert "Semantic target mode is unavailable for this task." in prompt
        assert "write_file" in prompt


@pytest.mark.parametrize(
    "invented_id", ["project_create_validation", "create_task_validation"]
)
def test_zero_handle_fabricated_ids_remain_defensively_rejected(tmp_path, invented_id):
    materialization = _zero_handle_materialization(tmp_path, grounded=False)
    assert build_semantic_target_inventory(materialization).handles == ()
    plan = _step(
        {
            "op": "replace_in_file",
            "path": "generated.py",
            "target_id": invented_id,
            "new": "value = 1\n",
        }
    )

    with pytest.raises(SemanticTargetContractError) as error:
        normalize_provider_semantic_intents(
            plan,
            inventory=build_semantic_target_inventory(materialization),
            project_dir=tmp_path,
            source_materialization=materialization,
        )

    assert error.value.code == "unknown_target_id"


def test_positive_semantic_mocked_pipeline_reaches_d5_d4_and_apa(tmp_path):
    materialization = _materialize(tmp_path)
    inventory = build_semantic_target_inventory(materialization)
    target_id = inventory.handles[0].target_id
    plan = _step(
        {
            "op": "replace_in_file",
            "path": "target.txt",
            "target_id": target_id,
            "new": "changed()\n",
        }
    )

    normalized = normalize_provider_semantic_intents(
        plan,
        inventory=inventory,
        project_dir=tmp_path,
        source_materialization=materialization,
    )
    verdict = ValidatorService.validate_plan(
        normalized,
        output_text="mocked semantic provider response",
        task_prompt="Replace the exact snippet `needle()` in target.txt.",
        execution_profile="implementation",
        project_dir=tmp_path,
        source_materialization=materialization,
    )
    authority = accepted_path_authority_from_verdict(verdict)

    assert verdict.accepted, verdict.reasons
    assert authority is not None
    assert authority.grant_for(declare("target.txt")) is not None
    operation = normalized[0]["ops"][0]
    assert set(operation) == {"op", "path", "selector", "new"}
    assert "target_id" not in operation
