import shutil
from pathlib import Path

import pytest

from app.services.orchestration.planning.repair_arbitration import (
    classify_planning_repair_candidate,
)
from app.services.orchestration.planning.slot_repair import (
    SlotRepairError,
    SlotRepairTaskContext,
    compile_slots_to_typed_plan,
    extract_plan_slots,
    merge_repair_slots,
)
from app.services.orchestration.validation.validator import ValidatorService


AMD_FIXTURE = (
    Path(__file__).resolve().parents[2]
    / "scripts"
    / "evals"
    / "fixtures"
    / "amd_tiny_source_rewrite"
)
AMD_TARGET = "src/amd_tiny/formatting.py"
OTHER_TARGET = "src/amd_tiny/other.py"
AMD_TEST = "tests/test_formatting.py"
AMD_VERIFY = "python3 -m pytest -q"
AMD_TASK_PROMPT = (
    "Fix the existing string formatter in src/amd_tiny/formatting.py so the "
    "existing tests pass. Edit only that source file. Do not create new files. "
    "Do not edit tests. Verify with python3 -m pytest -q."
)
VALID_REPLACEMENT = '''"""Formatting helpers for the AMD tiny fixture."""


def format_label(value: str) -> str:
    """Return a display label for user-provided text."""
    return " ".join(str(value).split()).title()
'''
ALTERNATE_VALID_REPLACEMENT = '''"""Formatting helpers for the AMD tiny fixture."""


def format_label(value: str) -> str:
    """Return a display label for user-provided text."""
    words = str(value).split()
    return " ".join(words).title()
'''
INVALID_REPLACEMENT = """def format_label(value: str) -> str:\n    return (\n"""


@pytest.fixture()
def amd_workspace(tmp_path):
    workspace = tmp_path / "amd_tiny_source_rewrite"
    shutil.copytree(AMD_FIXTURE, workspace)
    return workspace


@pytest.fixture()
def slot_context():
    return SlotRepairTaskContext(
        allowed_target_files=(AMD_TARGET,),
        allowed_verification_commands=(AMD_VERIFY,),
        allow_test_changes=False,
    )


@pytest.fixture()
def bootstrap_context():
    return SlotRepairTaskContext(
        allowed_target_files=(AMD_TARGET,),
        allowed_verification_commands=(AMD_VERIFY,),
        allow_test_changes=False,
        bootstrap_required_source_files=(AMD_TARGET,),
        bootstrap_required_test_files=(AMD_TEST,),
        bootstrap_required_verification=(AMD_VERIFY,),
    )


def _source_plan(content=VALID_REPLACEMENT, *, verification=AMD_VERIFY):
    return [
        {
            "step_number": 1,
            "description": "Rewrite formatter",
            "commands": [],
            "verification": verification,
            "rollback": None,
            "expected_files": [AMD_TARGET],
            "ops": [
                {
                    "op": "write_file",
                    "path": AMD_TARGET,
                    "content": content,
                }
            ],
        },
        {
            "step_number": 2,
            "description": "Verify formatter",
            "commands": [verification] if verification else [],
            "verification": verification,
            "rollback": None,
            "expected_files": [],
        },
    ]


def _source_plan_without_verification(content=VALID_REPLACEMENT):
    plan = _source_plan(content)
    plan[0] = {**plan[0], "verification": None}
    plan[1] = {**plan[1], "commands": [], "verification": None}
    return plan


def _verification_only_plan():
    return [
        {
            "step_number": 1,
            "description": "Verify formatter",
            "commands": [AMD_VERIFY],
            "verification": AMD_VERIFY,
            "rollback": None,
            "expected_files": [],
        }
    ]


def _replace_plan(
    old_text="return value.lower()", new_text=None, *, verification=AMD_VERIFY
):
    return [
        {
            "step_number": 1,
            "description": "Patch formatter",
            "commands": [],
            "verification": verification,
            "rollback": None,
            "expected_files": [],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": AMD_TARGET,
                    "old": old_text,
                    "new": new_text or 'return " ".join(str(value).split()).title()',
                }
            ],
        },
        {
            "step_number": 2,
            "description": "Verify formatter",
            "commands": [verification] if verification else [],
            "verification": verification,
            "rollback": None,
            "expected_files": [],
        },
    ]


def _validate(plan, workspace):
    return ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt=AMD_TASK_PROMPT,
        execution_profile="implementation",
        project_dir=workspace,
        is_first_ordered_task=False,
    )


def _validate_bootstrap(plan, workspace):
    return ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt=AMD_TASK_PROMPT,
        execution_profile="implementation",
        project_dir=workspace,
        is_first_ordered_task=True,
    )


def test_candidate_missing_verification_preserves_previous_source_materialization(
    slot_context,
):
    previous = extract_plan_slots(_source_plan(), slot_context)
    candidate = extract_plan_slots(_source_plan_without_verification(), slot_context)

    merged = merge_repair_slots(previous, candidate, "python_source_syntax_repair")
    plan = compile_slots_to_typed_plan(merged)

    assert plan[0]["ops"][0]["path"] == AMD_TARGET
    assert plan[0]["ops"][0]["content"] == VALID_REPLACEMENT
    assert plan[1]["verification"] == AMD_VERIFY


def test_candidate_missing_materialization_preserves_previous_source_op_for_verification_repair(
    slot_context,
):
    previous = extract_plan_slots(_source_plan(), slot_context)
    candidate = extract_plan_slots(_verification_only_plan(), slot_context)

    merged = merge_repair_slots(previous, candidate, "missing verification")
    plan = compile_slots_to_typed_plan(merged)

    assert plan[0]["ops"][0]["path"] == AMD_TARGET
    assert plan[0]["ops"][0]["content"] == VALID_REPLACEMENT
    assert plan[1]["commands"] == [AMD_VERIFY]


def test_op_shaped_dict_in_commands_is_promoted_into_ops(slot_context):
    candidate_plan = [
        {
            "step_number": 1,
            "description": "Rewrite formatter",
            "commands": [
                {
                    "op": "write_file",
                    "path": AMD_TARGET,
                    "content": VALID_REPLACEMENT,
                },
                AMD_VERIFY,
            ],
            "verification": AMD_VERIFY,
            "rollback": None,
            "expected_files": [],
        }
    ]

    slots = extract_plan_slots(candidate_plan, slot_context)
    plan = compile_slots_to_typed_plan(slots)

    assert plan[0]["ops"] == [
        {
            "op": "write_file",
            "path": AMD_TARGET,
            "content": VALID_REPLACEMENT,
        }
    ]


def test_expected_files_are_derived_from_ops_path(slot_context):
    candidate_plan = _source_plan()
    candidate_plan[0] = {**candidate_plan[0], "expected_files": []}

    slots = extract_plan_slots(candidate_plan, slot_context)
    plan = compile_slots_to_typed_plan(slots)

    assert plan[0]["expected_files"] == [AMD_TARGET]


def test_safe_verification_is_copied_into_commands(slot_context):
    candidate_plan = _source_plan()
    candidate_plan[1] = {
        **candidate_plan[1],
        "commands": [],
        "verification": AMD_VERIFY,
    }

    slots = extract_plan_slots(candidate_plan, slot_context)
    plan = compile_slots_to_typed_plan(slots)

    assert plan[1]["commands"] == [AMD_VERIFY]


def test_candidate_rewriting_tests_is_rejected():
    context = SlotRepairTaskContext(
        allowed_target_files=(AMD_TARGET, AMD_TEST),
        allowed_verification_commands=(AMD_VERIFY,),
        allow_test_changes=False,
    )
    candidate_plan = [
        {
            "step_number": 1,
            "description": "Rewrite tests",
            "commands": [],
            "verification": AMD_VERIFY,
            "rollback": None,
            "expected_files": [AMD_TEST],
            "ops": [
                {
                    "op": "write_file",
                    "path": AMD_TEST,
                    "content": "def test_weakened():\n    assert True\n",
                }
            ],
        }
    ]

    slots = extract_plan_slots(candidate_plan, context)

    assert slots.rejected
    assert any(
        "test file changes are not allowed" in item for item in slots.rejection_reasons
    )


def test_candidate_fixed_source_is_used_only_when_materialization_path_remains_valid(
    slot_context,
):
    previous = extract_plan_slots(_source_plan(INVALID_REPLACEMENT), slot_context)
    candidate = extract_plan_slots(
        _source_plan(ALTERNATE_VALID_REPLACEMENT),
        slot_context,
    )

    merged = merge_repair_slots(previous, candidate, "python source syntax")
    plan = compile_slots_to_typed_plan(merged)

    assert plan[0]["ops"][0]["path"] == AMD_TARGET
    assert plan[0]["ops"][0]["content"] == ALTERNATE_VALID_REPLACEMENT

    bad_candidate = extract_plan_slots(
        [
            {
                "step_number": 1,
                "description": "Move fix into tests",
                "commands": [],
                "verification": AMD_VERIFY,
                "rollback": None,
                "expected_files": [AMD_TEST],
                "ops": [
                    {
                        "op": "write_file",
                        "path": AMD_TEST,
                        "content": "def test_weakened():\n    assert True\n",
                    }
                ],
            }
        ],
        SlotRepairTaskContext(
            allowed_target_files=(AMD_TARGET, AMD_TEST),
            allowed_verification_commands=(AMD_VERIFY,),
            allow_test_changes=False,
        ),
    )
    ignored = merge_repair_slots(previous, bad_candidate, "python source syntax")

    assert ignored.source_op == previous.source_op


def test_candidate_syntax_invalid_still_fails_existing_validator(
    amd_workspace,
    slot_context,
):
    slots = extract_plan_slots(_source_plan(INVALID_REPLACEMENT), slot_context)
    plan = compile_slots_to_typed_plan(slots)

    verdict = _validate(plan, amd_workspace)

    assert not verdict.accepted
    assert "python_source_syntax_invalid" in verdict.details
    assert "python_source_syntax_invalid" in verdict.details["semantic_violation_codes"]


def test_compiled_slot_plan_passes_existing_validator_when_valid(
    amd_workspace,
    slot_context,
):
    slots = extract_plan_slots(_source_plan(), slot_context)
    plan = compile_slots_to_typed_plan(slots)

    verdict = _validate(plan, amd_workspace)

    assert verdict.accepted
    assert "semantic_violation_codes" not in verdict.details


def test_arbitration_detects_removed_materialization_without_slot_merge(
    amd_workspace,
    slot_context,
):
    previous_plan = compile_slots_to_typed_plan(
        extract_plan_slots(_source_plan(), slot_context)
    )

    arbitration = classify_planning_repair_candidate(
        previous_plan=previous_plan,
        repaired_plan=_verification_only_plan(),
        project_dir=amd_workspace,
    )

    assert arbitration["source_materialization"]["previous_paths"] == [AMD_TARGET]
    assert arbitration["source_materialization"]["repaired_paths"] == []
    assert arbitration["source_materialization"]["status"] == "removed"
    assert "removed_materialization" in arbitration["regression_labels"]


def test_slot_merged_plan_does_not_bypass_arbitration_or_validator(
    amd_workspace,
    slot_context,
):
    previous = extract_plan_slots(_source_plan(), slot_context)
    candidate = extract_plan_slots(_verification_only_plan(), slot_context)
    merged_plan = compile_slots_to_typed_plan(
        merge_repair_slots(previous, candidate, "missing verification")
    )

    verdict = _validate(merged_plan, amd_workspace)
    arbitration = classify_planning_repair_candidate(
        previous_plan=_source_plan(),
        repaired_plan=merged_plan,
        project_dir=amd_workspace,
    )

    assert verdict.accepted
    assert arbitration["source_materialization"]["status"] == "preserved"

    invalid_plan = compile_slots_to_typed_plan(
        extract_plan_slots(_source_plan(INVALID_REPLACEMENT), slot_context)
    )
    invalid_verdict = _validate(invalid_plan, amd_workspace)

    assert not invalid_verdict.accepted
    assert "python_source_syntax_invalid" in invalid_verdict.details


def test_bootstrap_required_source_test_and_verifier_slots_are_preserved(
    bootstrap_context,
):
    previous = extract_plan_slots(_source_plan(), bootstrap_context)
    candidate = extract_plan_slots(_verification_only_plan(), bootstrap_context)

    merged = merge_repair_slots(previous, candidate, "missing verification")

    assert merged.bootstrap_required_source_files == (AMD_TARGET,)
    assert merged.bootstrap_required_test_files == (AMD_TEST,)
    assert merged.bootstrap_required_verification == (AMD_VERIFY,)
    assert merged.verification_command == AMD_VERIFY


def test_compiled_bootstrap_slots_remain_visible_to_validator(
    amd_workspace,
    bootstrap_context,
):
    previous = extract_plan_slots(_source_plan(), bootstrap_context)
    candidate = extract_plan_slots(_verification_only_plan(), bootstrap_context)
    merged = merge_repair_slots(previous, candidate, "missing verification")

    plan = compile_slots_to_typed_plan(merged)
    verdict = _validate_bootstrap(plan, amd_workspace)

    assert plan[0]["expected_files"] == [AMD_TARGET]
    assert plan[1]["expected_files"] == [AMD_TEST]
    assert verdict.accepted


def test_partial_slots_are_not_accepted_directly(slot_context):
    slots = extract_plan_slots(_verification_only_plan(), slot_context)

    with pytest.raises(
        SlotRepairError, match="source materialization slot is required"
    ):
        compile_slots_to_typed_plan(slots)


def test_rejected_candidate_slots_never_override_previous_valid_slots():
    context = SlotRepairTaskContext(
        allowed_target_files=(AMD_TARGET, AMD_TEST),
        allowed_verification_commands=(AMD_VERIFY,),
        allow_test_changes=False,
    )
    previous = extract_plan_slots(_source_plan(), context)
    candidate = extract_plan_slots(
        [
            {
                "step_number": 1,
                "description": "Rewrite forbidden tests",
                "commands": [AMD_VERIFY],
                "verification": AMD_VERIFY,
                "rollback": None,
                "expected_files": [AMD_TEST],
                "ops": [
                    {
                        "op": "write_file",
                        "path": AMD_TEST,
                        "content": "def test_weakened():\n    assert True\n",
                    }
                ],
            }
        ],
        context,
    )

    merged = merge_repair_slots(previous, candidate, "python source syntax")

    assert candidate.rejected
    assert merged.source_op == previous.source_op


def test_source_only_repair_cannot_remove_existing_verifier_slot(slot_context):
    previous = extract_plan_slots(_source_plan(), slot_context)
    candidate = extract_plan_slots(
        _source_plan_without_verification(ALTERNATE_VALID_REPLACEMENT),
        slot_context,
    )

    merged = merge_repair_slots(previous, candidate, "python source syntax")
    plan = compile_slots_to_typed_plan(merged)

    assert plan[0]["ops"][0]["content"] == ALTERNATE_VALID_REPLACEMENT
    assert plan[1]["verification"] == AMD_VERIFY
    assert plan[1]["commands"] == [AMD_VERIFY]


def test_valid_source_content_replaces_invalid_only_for_same_allowed_target():
    context = SlotRepairTaskContext(
        allowed_target_files=(AMD_TARGET, OTHER_TARGET),
        allowed_verification_commands=(AMD_VERIFY,),
        allow_test_changes=False,
    )
    previous = extract_plan_slots(_source_plan(INVALID_REPLACEMENT), context)
    different_target_candidate = extract_plan_slots(
        [
            {
                "step_number": 1,
                "description": "Write different source target",
                "commands": [AMD_VERIFY],
                "verification": AMD_VERIFY,
                "rollback": None,
                "expected_files": [OTHER_TARGET],
                "ops": [
                    {
                        "op": "write_file",
                        "path": OTHER_TARGET,
                        "content": ALTERNATE_VALID_REPLACEMENT,
                    }
                ],
            }
        ],
        context,
    )

    merged = merge_repair_slots(
        previous,
        different_target_candidate,
        "python source syntax",
    )

    assert merged.source_op == previous.source_op


def test_expected_files_are_not_trusted_without_materializing_ops(slot_context):
    previous = extract_plan_slots(_source_plan(), slot_context)
    candidate = extract_plan_slots(
        [
            {
                "step_number": 1,
                "description": "Declare files only",
                "commands": [AMD_VERIFY],
                "verification": AMD_VERIFY,
                "rollback": None,
                "expected_files": ["README.md"],
            }
        ],
        slot_context,
    )

    merged = merge_repair_slots(previous, candidate, "missing verification")
    plan = compile_slots_to_typed_plan(merged)

    assert plan[0]["expected_files"] == [AMD_TARGET]


def test_stale_replace_in_file_is_rejected_against_current_source():
    context = SlotRepairTaskContext(
        allowed_target_files=(AMD_TARGET,),
        allowed_verification_commands=(AMD_VERIFY,),
        current_file_contents={
            AMD_TARGET: "def format_label(value: str) -> str:\n    return value.lower()\n"
        },
    )

    stale_slots = extract_plan_slots(
        _replace_plan(old_text="return missing_old_text"),
        context,
    )
    valid_slots = extract_plan_slots(_replace_plan(), context)

    assert stale_slots.rejected
    assert any(
        "stale replace_in_file" in item for item in stale_slots.rejection_reasons
    )
    assert not valid_slots.rejected
    assert valid_slots.source_op["op"] == "replace_in_file"


def test_candidate_with_valid_source_op_and_forbidden_test_rewrite_is_rejected():
    context = SlotRepairTaskContext(
        allowed_target_files=(AMD_TARGET, AMD_TEST),
        allowed_verification_commands=(AMD_VERIFY,),
        allow_test_changes=False,
    )
    candidate = extract_plan_slots(
        [
            {
                "step_number": 1,
                "description": "Source and test rewrite",
                "commands": [AMD_VERIFY],
                "verification": AMD_VERIFY,
                "rollback": None,
                "expected_files": [AMD_TARGET, AMD_TEST],
                "ops": [
                    {
                        "op": "write_file",
                        "path": AMD_TARGET,
                        "content": VALID_REPLACEMENT,
                    },
                    {
                        "op": "write_file",
                        "path": AMD_TEST,
                        "content": "def test_weakened():\n    assert True\n",
                    },
                ],
            }
        ],
        context,
    )

    assert candidate.rejected
    assert any(
        "test file changes are not allowed" in item
        for item in candidate.rejection_reasons
    )


def test_duplicate_materialization_ops_for_same_target_are_rejected(slot_context):
    candidate = extract_plan_slots(
        [
            {
                "step_number": 1,
                "description": "Duplicate source writes",
                "commands": [AMD_VERIFY],
                "verification": AMD_VERIFY,
                "rollback": None,
                "expected_files": [AMD_TARGET],
                "ops": [
                    {
                        "op": "write_file",
                        "path": AMD_TARGET,
                        "content": VALID_REPLACEMENT,
                    },
                    {
                        "op": "write_file",
                        "path": AMD_TARGET,
                        "content": ALTERNATE_VALID_REPLACEMENT,
                    },
                ],
            }
        ],
        slot_context,
    )

    assert candidate.rejected
    assert any(
        "duplicate source materialization" in item
        for item in candidate.rejection_reasons
    )


def test_unsafe_source_op_paths_are_rejected(slot_context):
    slots = extract_plan_slots(
        [
            {
                "step_number": 1,
                "description": "Unsafe source path",
                "commands": [AMD_VERIFY],
                "verification": AMD_VERIFY,
                "rollback": None,
                "expected_files": ["../outside.py"],
                "ops": [
                    {
                        "op": "write_file",
                        "path": "../outside.py",
                        "content": VALID_REPLACEMENT,
                    }
                ],
            }
        ],
        slot_context,
    )

    assert slots.rejected
    assert any("safe relative file path" in item for item in slots.rejection_reasons)


def test_validator_rejects_unsafe_path_missing_materialization_and_missing_verification(
    amd_workspace,
):
    unsafe_plan = _source_plan()
    unsafe_plan[0] = {
        **unsafe_plan[0],
        "expected_files": ["../outside.py"],
        "ops": [
            {
                "op": "write_file",
                "path": "../outside.py",
                "content": VALID_REPLACEMENT,
            }
        ],
    }
    missing_materialization_plan = _verification_only_plan()
    missing_verification_plan = _source_plan_without_verification()

    unsafe_verdict = _validate(unsafe_plan, amd_workspace)
    missing_materialization_verdict = _validate(
        missing_materialization_plan,
        amd_workspace,
    )
    missing_verification_verdict = _validate(
        missing_verification_plan,
        amd_workspace,
    )

    assert not unsafe_verdict.accepted
    assert any("outside the workspace" in reason for reason in unsafe_verdict.reasons)
    assert not missing_materialization_verdict.accepted
    assert not missing_verification_verdict.accepted
    assert missing_verification_verdict.details["missing_verification_steps"]


def test_arbitration_detects_test_rewrite_regression_and_slot_merge_rejects_it(
    amd_workspace,
):
    context = SlotRepairTaskContext(
        allowed_target_files=(AMD_TARGET, AMD_TEST),
        allowed_verification_commands=(AMD_VERIFY,),
        allow_test_changes=False,
    )
    previous_plan = _source_plan()
    candidate_plan = [
        {
            "step_number": 1,
            "description": "Rewrite source and test",
            "commands": [AMD_VERIFY],
            "verification": AMD_VERIFY,
            "rollback": None,
            "expected_files": [AMD_TARGET, AMD_TEST],
            "ops": [
                {
                    "op": "write_file",
                    "path": AMD_TARGET,
                    "content": VALID_REPLACEMENT,
                },
                {
                    "op": "write_file",
                    "path": AMD_TEST,
                    "content": "def test_weakened():\n    assert True\n",
                },
            ],
        }
    ]

    arbitration = classify_planning_repair_candidate(
        previous_plan=previous_plan,
        repaired_plan=candidate_plan,
        project_dir=amd_workspace,
    )
    previous = extract_plan_slots(previous_plan, context)
    candidate = extract_plan_slots(candidate_plan, context)
    merged = merge_repair_slots(previous, candidate, "python source syntax")

    assert "test_rewrite" in arbitration["regression_labels"]
    assert candidate.rejected
    assert merged.source_op == previous.source_op
