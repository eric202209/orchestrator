"""PR10 step-command contract and rejection-feedback precision.

These tests render prompts and run Plan admission only. They never invoke a
provider or execute commands from a Plan.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.agents.agent_backends import ExecutionTopology
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.source_materialization import (
    materialize_planner_source_context,
)
from app.services.orchestration.prompt_templates import PromptTemplates
from app.services.orchestration.validation.rules.core_execution_capability import (
    render_planning_execution_capability_contract,
)
from app.services.orchestration.validation.validator import ValidatorService

TASK = "Fix the duplicate-customer regression in dataflow/aggregate.py."
CAT_A = "cat dataflow/aggregate.py"
CAT_N = "cat dataflow/normalize.py"
PYTEST_A = "python -m pytest tests/test_aggregate.py::test_case_a -v"
PYTEST_B = "python -m pytest tests/test_aggregate.py::test_case_b -v"
PYTEST_AB = (
    "python -m pytest tests/test_aggregate.py::test_case_a "
    "tests/test_aggregate.py::test_case_b -v"
)
MIX_MARKER = "mixes inspection and verification commands"


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "dataflow").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "dataflow/aggregate.py").write_text(
        "def customer_count(rows):\n    return len(rows)\n", encoding="utf-8"
    )
    (tmp_path / "dataflow/normalize.py").write_text(
        "def normalize_records(rows):\n    return rows\n", encoding="utf-8"
    )
    (tmp_path / "tests/test_aggregate.py").write_text(
        "def test_case_a():\n    pass\n\n\ndef test_case_b():\n    pass\n",
        encoding="utf-8",
    )
    return tmp_path


def _step(number: int, commands: list[str], **fields: Any) -> dict[str, Any]:
    return {
        "step_number": number,
        "description": fields.pop("description", f"Step {number}"),
        "commands": commands,
        "verification": fields.pop("verification", None),
        "rollback": None,
        "expected_files": [],
        "ops": [],
    }


def _validate(plan: list[dict[str, Any]], project_dir: Path):
    return ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=TASK,
        execution_profile="full_lifecycle",
        project_dir=project_dir,
        execution_topology=ExecutionTopology.STRUCTURED_ORCHESTRATOR,
    )


def _command_reason(outcome) -> str:
    return next(r for r in outcome.reasons if "unsupported_step_command" in r)


def _assert_step_channel_contract(text: str) -> None:
    assert "Do not mix inspection and verification commands in one step" in text
    assert "put them in separate steps" in text
    assert "A step's full `commands` list must fit one admitted channel" in text
    assert "admitted inspection commands may share a step" in text
    assert "a verification command must be the only command in its step" in text
    assert "safe local shell commands keep their own admission rules" in text


# Test A


def test_shared_contract_states_the_per_step_channel_rule(workspace: Path):
    contract = render_planning_execution_capability_contract(
        runtime_service=SimpleNamespace(), project_dir=workspace
    )

    _assert_step_channel_contract(contract)
    assert "use exactly one admitted verification command in that step" in contract
    assert "Local inspection forms admitted by this executor: rg --files .; ls" in (
        contract
    )
    assert "a Structured step needs an admitted command or at least one operation" in (
        contract
    )
    assert "Arbitrary shell" in contract


# Tests B and C


def test_full_minimal_and_ultra_minimal_prompts_carry_the_shared_rule(
    workspace: Path,
):
    materialization = materialize_planner_source_context(
        workspace, task_description=TASK, expected_paths=["dataflow/aggregate.py"]
    )
    prompts = (
        PromptTemplates.build_planning_prompt(
            task_description=TASK,
            project_dir=str(workspace),
            project_structure_capsule="dataflow/aggregate.py",
            source_materialization=materialization,
        ),
        PlannerService.build_minimal_planning_prompt(
            TASK, workspace, source_materialization=materialization
        ),
        PlannerService.build_ultra_minimal_planning_prompt(
            TASK, workspace, source_materialization=materialization
        ),
    )
    contract = render_planning_execution_capability_contract(
        runtime_service=SimpleNamespace(), project_dir=workspace
    )

    for prompt in prompts:
        rendered = PlannerService.include_execution_capability_contract(
            prompt, runtime_service=SimpleNamespace(), project_dir=workspace
        )
        assert rendered.startswith(contract)
        _assert_step_channel_contract(rendered)


# Test D


def test_repair_prompt_for_mixed_step_reason_carries_the_shared_rule(
    workspace: Path,
):
    plan = [_step(1, [CAT_A, CAT_N, PYTEST_A, PYTEST_B])]
    reason = _command_reason(_validate(plan, workspace))
    assert PlannerService._repair_requires_execution_capability_contract([reason])

    materialization = materialize_planner_source_context(
        workspace, task_description=TASK, expected_paths=["dataflow/aggregate.py"]
    )
    compact = PlannerService.build_compact_planning_repair_prompt(
        malformed_output=json.dumps(plan),
        rejection_reasons=[reason],
        task_description=TASK,
        project_dir=workspace,
        source_materialization=materialization,
    )
    full = PlannerService.build_planning_repair_prompt_with_metadata(
        task_description=TASK,
        malformed_output=json.dumps(plan),
        project_dir=workspace,
        rejection_reasons=[reason],
    ).prompt
    for prompt in (compact, full):
        rendered = PlannerService.include_execution_capability_contract(
            prompt, runtime_service=SimpleNamespace(), project_dir=workspace
        )
        _assert_step_channel_contract(rendered)
        assert MIX_MARKER in rendered


# Tests E, F and J


@pytest.mark.parametrize(
    "commands",
    [[CAT_A, CAT_N, PYTEST_A, PYTEST_B], [CAT_A, PYTEST_AB]],
    ids=["pr08_cat_cat_pytest_pytest", "cat_plus_one_pytest"],
)
def test_inspection_plus_verification_reason_names_the_mix(
    workspace: Path, commands: list[str]
):
    outcome = _validate([_step(1, commands)], workspace)

    assert outcome.status == "repair_required"
    assert (
        "step_command_capability_unsupported" in outcome.details["validator_rule_ids"]
    )
    reason = _command_reason(outcome)
    assert reason.startswith("unsupported_step_command: step 1 ")
    assert MIX_MARKER in reason
    assert "separate step" in reason
    assert CAT_A not in reason
    assert "no command tools" not in reason
    assert len(reason) <= 180


# Test G


def test_multiple_verification_commands_are_not_called_a_mix(workspace: Path):
    outcome = _validate([_step(1, [PYTEST_A, PYTEST_B])], workspace)

    assert outcome.status == "repair_required"
    assert (
        "step_command_capability_unsupported" in outcome.details["validator_rule_ids"]
    )
    reason = _command_reason(outcome)
    assert MIX_MARKER not in reason
    assert "inspection" not in reason
    assert "2 verification commands" in reason
    assert "exactly one per step" in reason
    assert len(reason) <= 180


def test_individually_admitted_commands_in_no_shared_channel_get_generic_reason(
    workspace: Path,
):
    outcome = _validate([_step(1, ["mkdir build", CAT_A])], workspace)

    reason = _command_reason(outcome)
    assert outcome.status == "repair_required"
    assert MIX_MARKER not in reason
    assert "fit no single admitted channel" in reason
    assert len(reason) <= 180


# Test H


@pytest.mark.parametrize(
    "plan",
    [
        [_step(1, [CAT_A, CAT_N])],
        [_step(1, [PYTEST_AB])],
        [_step(1, [CAT_A, CAT_N]), _step(2, [PYTEST_AB], verification=PYTEST_AB)],
        [_step(1, ["mkdir build", "touch build/.keep"])],
    ],
    ids=["inspection_only", "one_verification", "split_steps", "safe_shell"],
)
def test_admitted_command_shapes_remain_accepted(workspace: Path, plan):
    outcome = _validate(plan, workspace)

    assert "steps_with_unsupported_commands" not in outcome.details
    assert not any("unsupported_step_command" in r for r in outcome.reasons)


# Tests I and J


@pytest.mark.parametrize(
    "unsupported",
    [
        'python -c "from dataflow.aggregate import customer_count; print(1)"',
        "curl https://example.invalid/" + "x" * 200,
    ],
    ids=["project_importing_inline_python", "long_network_command"],
)
def test_unsupported_command_stays_rejected_and_is_named(
    workspace: Path, unsupported: str
):
    outcome = _validate([_step(1, [CAT_A, unsupported])], workspace)

    assert outcome.status == "repair_required"
    assert (
        "step_command_capability_unsupported" in outcome.details["validator_rule_ids"]
    )
    reason = _command_reason(outcome)
    assert reason.startswith("unsupported_step_command: rejected ")
    assert CAT_A not in reason
    assert unsupported.split()[0] in reason
    assert "Plan ops for edits; keep goal, scope, checks." in reason
    assert len(reason) <= 180
