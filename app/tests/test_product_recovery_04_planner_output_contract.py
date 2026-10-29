"""PR04 contract regressions for actual rendered Planning prompts.

These tests inspect prompt output and Plan admission only. They never invoke a
provider or execute commands from a Plan.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
import json

from app.services.agents.agent_backends import (
    BackendCapabilities,
    ExecutionTopology,
)
from app.services.orchestration.phases.execution_local_steps import (
    _is_simple_verification_command,
)
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.source_materialization import (
    materialize_planner_source_context,
)
from app.services.orchestration.prompt_templates import PromptTemplates
from app.services.orchestration.validation.validator import ValidatorService


TASK = "Make a localized behavior fix in src/sample.py and preserve existing tests."
SOURCE = "def run(value):\n    return value + 1\n\n\ndef keep_api():\n    return True\n"


def _source_materialization(root: Path):
    source_path = root / "src/sample.py"
    source_path.parent.mkdir(parents=True, exist_ok=True)
    source_path.write_text(SOURCE, encoding="utf-8")
    return materialize_planner_source_context(
        root,
        task_description=TASK,
        expected_paths=["src/sample.py"],
    )


def _structured_prompt(prompt: str, root: Path) -> str:
    return PlannerService.include_execution_capability_contract(
        prompt,
        runtime_service=SimpleNamespace(),
        project_dir=root,
    )


def _assert_structured_output_contract(prompt: str) -> None:
    lowered = prompt.lower()
    assert "exactly one" in lowered and "verification command" in lowered
    assert "multiple pytest node ids" in lowered
    assert "`verification` value alone does not make a step runnable" in lowered
    assert "commands" in lowered and "ops" in lowered and "verification" in lowered
    assert "project-importing inline python" in lowered
    assert "inline python `-c` is limited" in lowered
    assert "python -m pytest -q tests" in prompt
    assert "Verification must use `python -c`" not in prompt
    assert "import check" not in lowered


def test_full_and_compact_planning_prompts_render_the_structured_step_contract(
    tmp_path: Path,
):
    materialization = _source_materialization(tmp_path)
    full = PromptTemplates.build_planning_prompt(
        task_description=TASK,
        project_dir=str(tmp_path),
        project_structure_capsule="src/sample.py",
        source_materialization=materialization,
    )
    prompts = [
        _structured_prompt(full, tmp_path),
        _structured_prompt(
            PlannerService.build_minimal_planning_prompt(
                TASK, tmp_path, source_materialization=materialization
            ),
            tmp_path,
        ),
        _structured_prompt(
            PlannerService.build_ultra_minimal_planning_prompt(
                TASK, tmp_path, source_materialization=materialization
            ),
            tmp_path,
        ),
    ]

    for prompt in prompts:
        _assert_structured_output_contract(prompt)
        assert "For a localized existing-file change" in prompt
        assert "complete GR2-grounded source" in prompt
    assert SOURCE.strip() in prompts[1]
    assert SOURCE.strip() in prompts[2]


def test_full_planning_prompt_does_not_present_write_file_as_default_edit(
    tmp_path: Path,
):
    materialization = _source_materialization(tmp_path)
    prompt = _structured_prompt(
        PromptTemplates.build_planning_prompt(
            task_description=TASK,
            project_dir=str(tmp_path),
            project_structure_capsule="src/sample.py",
            source_materialization=materialization,
        ),
        tmp_path,
    )

    lowered = prompt.lower()
    assert "`write_file` replaces the whole file" in lowered
    assert "prefer an available precise `replace_in_file`" in lowered
    assert "write_file` is only for task-required full rewrites" in lowered
    assert "for routine file changes, prefer `ops`: `[" not in lowered
    assert "new files" in lowered and "write_file" in lowered


def test_structured_prompt_keeps_a_single_pytest_invocation_with_multiple_targets(
    tmp_path: Path,
):
    command = (
        "python -m pytest -q tests/test_aggregate.py::test_case_a "
        "tests/test_aggregate.py::test_case_b"
    )

    assert _is_simple_verification_command(command, project_dir=tmp_path)


def test_small_local_model_profile_does_not_override_execution_capability_contract():
    prompt = PlannerService.apply_prompt_profile(
        "BASE", prompt_profile="local_qwen_small_json_array"
    )

    lowered = prompt.lower()
    assert (
        "verification forms admitted by the resolved execution capability contract"
        in lowered
    )
    assert "do not require a python command for every implementation step" in lowered
    assert (
        "one bounded python verification command per implementation step" not in lowered
    )


def test_agent_runtime_capability_contract_does_not_inherit_structured_limits(
    tmp_path: Path,
):
    capabilities = BackendCapabilities(
        supports_planning=True,
        supports_step_execution=True,
        supports_debug_repair=True,
        supports_streaming=True,
        supports_checkpoint_resume=True,
        supports_tool_execution=True,
        supports_json_mode=True,
        supports_step_reasoning=True,
        supports_agent_workspace_binding=True,
    )
    runtime = SimpleNamespace(
        backend_descriptor=SimpleNamespace(capabilities=capabilities)
    )
    prompt = PlannerService.include_execution_capability_contract(
        "BODY",
        runtime_service=runtime,
        project_dir=tmp_path,
    )
    contract = prompt.split("\n\nBODY", maxsplit=1)[0]

    assert "Resolved topology: agent runtime" in contract
    assert "configured runtime's declared capabilities" in contract
    assert "one admitted verification command in that step" not in contract
    assert "project-importing inline Python are not available" not in contract


def test_repair_feedback_renders_execution_contract_for_all_step_shape_rejections(
    tmp_path: Path,
):
    materialization = _source_materialization(tmp_path)
    malformed = json.dumps(
        [
            {
                "step_number": 1,
                "description": "Verify the localized fix",
                "commands": [],
                "verification": "python -m pytest -q tests",
                "rollback": None,
                "expected_files": [],
                "ops": [],
            }
        ]
    )
    rejection_cases = (
        "unsupported_step_command: rejected project-importing inline Python",
        "unsupported_verification_command: rejected project-importing inline Python",
        "Plan contains steps without runnable commands (steps: [1])",
    )

    for reason in rejection_cases:
        assert PlannerService._repair_requires_execution_capability_contract([reason])
        compact_prompt = PlannerService.build_compact_planning_repair_prompt(
            malformed_output=malformed,
            rejection_reasons=[reason],
            task_description=TASK,
            project_dir=tmp_path,
            source_materialization=materialization,
            guidance_block=materialization.to_prompt_block(provider_safe=True),
        )
        full_prompt = PlannerService.build_planning_repair_prompt_with_metadata(
            task_description=TASK,
            malformed_output=malformed,
            project_dir=tmp_path,
            rejection_reasons=[reason],
            guidance_block=materialization.to_prompt_block(provider_safe=True),
        ).prompt

        compact_prompt = PlannerService.include_execution_capability_contract(
            compact_prompt,
            runtime_service=SimpleNamespace(),
            project_dir=tmp_path,
        )
        full_prompt = PlannerService.include_execution_capability_contract(
            full_prompt,
            runtime_service=SimpleNamespace(),
            project_dir=tmp_path,
        )

        _assert_structured_output_contract(compact_prompt)
        _assert_structured_output_contract(full_prompt)
        for prompt in (compact_prompt, full_prompt):
            lowered = prompt.lower()
            assert "repair" in lowered
            assert "do not remove, comment out, or weaken" in lowered


def test_repair_feedback_keeps_targeted_existing_module_preservation_guidance(
    tmp_path: Path,
):
    materialization = _source_materialization(tmp_path)
    malformed = json.dumps(
        [
            {
                "step_number": 1,
                "description": "Fix the existing function",
                "commands": [],
                "verification": "python -m pytest -q tests",
                "rollback": None,
                "expected_files": ["src/sample.py"],
                "ops": [
                    {
                        "op": "write_file",
                        "path": "src/sample.py",
                        "content": "def run(value):\n    return missing_helper(value)\n",
                    }
                ],
            }
        ]
    )
    prompt = PlannerService.build_compact_planning_repair_prompt(
        malformed_output=malformed,
        rejection_reasons=["python_write_file_drops_referenced_definitions: keep_api"],
        task_description=TASK,
        project_dir=tmp_path,
        source_materialization=materialization,
        guidance_block=materialization.to_prompt_block(provider_safe=True),
    )

    assert "Existing Python module preservation repair required" in prompt
    assert "replace that rejected operation with one `replace_in_file`" in prompt
    assert "dropped definitions" in prompt
    assert (
        "preserve required imports/helpers/definitions and existing behavior" in prompt
    )
    assert "do not remove, comment out, or weaken existing test assertions" in prompt


def test_verification_only_step_remains_rejected_by_production_validator(
    tmp_path: Path,
):
    plan = [
        {
            "step_number": 1,
            "description": "Verify the requested behavior",
            "commands": [],
            "verification": "python -m pytest -q tests",
            "rollback": None,
            "expected_files": [],
            "ops": [],
        }
    ]

    schema = ValidatorService.validate_plan_schema(plan)
    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=TASK,
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        execution_topology=ExecutionTopology.STRUCTURED_ORCHESTRATOR,
    )

    assert schema["valid"]
    assert not verdict.accepted
    assert any(
        "steps without runnable commands" in reason for reason in verdict.reasons
    )
