import json
from unittest.mock import MagicMock

import pytest
from app.services.orchestration.phases.planning_flow import (
    _build_repair_rejection_reasons,
    _emit_planning_diagnostics_contract_violation,
    _plan_contract_diagnostics,
    _terminal_validation_failure_details,
    _truncated_multistep_collapse_diagnostics,
    TRUNCATED_PLAN_REPAIR_REJECTION_REASON,
)

from app.services.orchestration.planning.planner import PlannerService

from app.services.orchestration.validation.validator import ValidatorService


def test_placeholder_repair_prompt_rejects_stubs_and_noop_commands(tmp_path):
    prompt = PlannerService.build_planning_repair_prompt(
        "Add a summary command to the Python CLI.",
        malformed_output=json.dumps(
            [
                {
                    "step_number": 2,
                    "commands": ['python -c "import sys; sys.exit(0)"'],
                    "expected_files": [
                        "src/medium_cli/cli.py",
                        "src/medium_cli/formatting.py",
                    ],
                    "ops": [
                        {
                            "op": "write_file",
                            "path": "src/medium_cli/cli.py",
                            "content": "# CLI implementation\npass",
                        }
                    ],
                }
            ]
        ),
        project_dir=tmp_path,
        rejection_reasons=[
            "Plan appears to generate placeholder or stub implementations",
        ],
    )

    assert "Grounded source-edit repair required:" in prompt
    assert "Do not use `pass`, TODOs, placeholder comments" in prompt
    assert "no-op commands such as" in prompt
    assert "Do not generic-rewrite whole files" in prompt


def test_unrelated_repair_prompt_omits_grounded_source_edit_guidance(tmp_path):
    prompt = PlannerService.build_planning_repair_prompt(
        "Build a FastAPI health endpoint",
        malformed_output='[{"step_number":1,"verification":null}]',
        project_dir=tmp_path,
        rejection_reasons=[
            "Plan uses weak verification for implementation-heavy work (steps: [1])",
        ],
    )

    assert "Grounded source-edit repair required:" not in prompt
    assert "Preserve existing tests as the behavior contract" not in prompt
    assert "no-op commands such as" not in prompt


def test_brittle_inline_python_repair_prompt_preserves_source_ops(tmp_path):
    prompt = PlannerService.build_planning_repair_prompt(
        "Add a summary command to the Python CLI.",
        malformed_output=json.dumps(
            [
                {
                    "step_number": 1,
                    "commands": [
                        'python3 -c "from medium_cli.formatting import '
                        "format_summary; assert format_summary(3, 2) == "
                        "'3 tasks, 2 complete'\""
                    ],
                    "verification": (
                        'python3 -c "from medium_cli.formatting import '
                        "format_summary; assert format_summary(3, 2) == "
                        "'3 tasks, 2 complete'\""
                    ),
                    "expected_files": ["src/medium_cli/formatting.py"],
                    "ops": [
                        {
                            "op": "write_file",
                            "path": "src/medium_cli/formatting.py",
                            "content": (
                                "def format_summary(total, completed):\n"
                                '    return f"{total} tasks, {completed} complete"'
                            ),
                        }
                    ],
                }
            ]
        ),
        project_dir=tmp_path,
        rejection_reasons=[
            "Step [1]: command uses brittle inline Python "
            "(brittle_inline_python). Replace nested python -c snippets.",
            "Plan contains brittle heredoc-heavy or malformed commands",
        ],
    )

    assert "Brittle inline Python command repair:" in prompt
    assert "Preserve existing source ops exactly" in prompt
    assert "Do not regenerate unrelated source files" in prompt
    assert "python3 -m pytest -q" in prompt
    assert "python3 -m py_compile <changed source file>" in prompt
    assert "Do not use heredocs, shell assertion one-liners" in prompt


def test_unrelated_repair_prompt_omits_brittle_inline_python_guidance(tmp_path):
    prompt = PlannerService.build_planning_repair_prompt(
        "Build a FastAPI health endpoint",
        malformed_output='[{"step_number":1,"verification":null}]',
        project_dir=tmp_path,
        rejection_reasons=[
            "Plan uses weak verification for implementation-heavy work (steps: [1])",
        ],
    )

    assert "Brittle inline Python command repair:" not in prompt
    assert "Preserve existing source ops exactly" not in prompt
    assert "python3 -m py_compile <changed source file>" not in prompt


def test_repair_prompt_includes_brittle_command_guidance_for_heredoc(tmp_path):
    prompt = PlannerService.build_planning_repair_prompt(
        "Add a summary command to the Python CLI.",
        malformed_output=json.dumps(
            [
                {
                    "step_number": 2,
                    "description": "Rewrite CLI with heredoc",
                    "commands": ["cat > src/medium_cli/cli.py <<'PY'\n...\nPY"],
                    "verification": "python -m pytest -q",
                    "rollback": None,
                    "expected_files": ["src/medium_cli/cli.py"],
                }
            ]
        ),
        project_dir=tmp_path,
        rejection_reasons=[
            "Plan contains brittle heredoc-heavy or malformed commands",
            "Step [2]: invalid heredoc shape (disallowed_heredoc_shape). No heredoc.",
        ],
    )

    assert "Brittle inline Python command repair:" in prompt
    assert "Do not use heredocs or multiline shell-generated file bodies" in prompt
    assert "ops.write_file or ops.replace_in_file" in prompt
    assert "Keep commands short, single-purpose" in prompt


def test_repair_prompt_includes_injected_truncated_multistep_subcodes():
    malformed_output = (
        '[{"step_number":1,"description":"Create files",'
        '"commands":["printf ..."],"verification":"python -m pytest"},'
        '{"step_number":2,"description":"Wire behavior"},'
        '{"step_number":3,"description":"Verify behavior"}]'
    )
    extracted_plan = [
        {
            "step_number": 1,
            "description": "Create files and wire behavior and verify behavior",
            "commands": ["printf ..."],
            "verification": "python -m pytest",
            "rollback": None,
            "expected_files": ["app.py"],
        }
    ]
    details = _truncated_multistep_collapse_diagnostics(
        output_text=malformed_output,
        extracted_plan=extracted_plan,
        repair_stage="after_first_repair",
    )

    reasons = _build_repair_rejection_reasons(
        [TRUNCATED_PLAN_REPAIR_REJECTION_REASON],
        details,
    )
    prompt = PlannerService.build_planning_repair_prompt(
        "Build a small app",
        malformed_output=malformed_output,
        project_dir=__import__("pathlib").Path("/tmp/project"),
        rejection_reasons=reasons,
    )

    assert details["truncated_multistep_subcodes"] == [
        "original_steps_detected_3",
        "absorbed_into_step_1",
        "collapse_after_first_repair",
    ]
    assert "truncated_multistep_subcodes:" in prompt
    assert "Return 3 separate step objects" in prompt
    assert "do not merge into step 1" in prompt


# Phase 6Q: expose brittle-command subcodes in planning events


def test_plan_contract_diagnostics_include_brittle_subcodes_when_present():
    shadow_warnings = [
        {
            "rule_id": "model_behavior.command_length_prompt_patch",
            "category": "model_behavior_patch",
            "shadow_candidate": True,
        }
    ]
    diagnostics = _plan_contract_diagnostics(
        {
            "step_count": 3,
            "max_command_length": 1203,
            "heredoc_command_count": 0,
            "command_total_chars": 2445,
            "brittle_command_subcodes": ["oversized_command_length"],
            "brittle_command_step_details": {2: ["oversized_command_length"]},
            "shadow_warnings": shadow_warnings,
        }
    )

    assert diagnostics["step_count"] == 3
    assert diagnostics["max_command_length"] == 1203
    assert diagnostics["brittle_command_subcodes"] == ["oversized_command_length"]
    assert diagnostics["brittle_command_step_details"] == {
        2: ["oversized_command_length"]
    }
    assert diagnostics["shadow_warnings"] == shadow_warnings


def test_plan_contract_diagnostics_omit_brittle_keys_when_absent():
    diagnostics = _plan_contract_diagnostics(
        {
            "step_count": 3,
            "max_command_length": 1203,
            "heredoc_command_count": 0,
            "command_total_chars": 2445,
        }
    )

    assert "brittle_command_subcodes" not in diagnostics
    assert "brittle_command_step_details" not in diagnostics


def test_plan_contract_diagnostics_include_truncated_multistep_subcodes():
    diagnostics = _plan_contract_diagnostics(
        {
            "truncated_multistep_subcodes": [
                "original_steps_detected_3",
                "absorbed_into_step_1",
                "collapse_before_first_repair",
            ],
            "truncated_multistep_original_step_count": 3,
            "truncated_multistep_absorbing_step": 1,
            "truncated_multistep_repair_stage": "before_first_repair",
        }
    )

    assert diagnostics["truncated_multistep_subcodes"] == [
        "original_steps_detected_3",
        "absorbed_into_step_1",
        "collapse_before_first_repair",
    ]
    assert diagnostics["truncated_multistep_original_step_count"] == 3
    assert diagnostics["truncated_multistep_absorbing_step"] == 1
    assert diagnostics["truncated_multistep_repair_stage"] == "before_first_repair"


def test_planning_contract_violation_event_includes_brittle_subcodes():
    events = []
    shadow_warnings = [
        {
            "rule_id": "model_behavior.command_length_prompt_patch",
            "category": "model_behavior_patch",
            "shadow_candidate": True,
        }
    ]
    ctx = MagicMock(
        session_id=55,
        task_id=10,
        task_execution_id=38,
        emit_live=lambda level, message, metadata=None: events.append(
            (level, message, metadata or {})
        ),
    )

    _emit_planning_diagnostics_contract_violation(
        ctx,
        reason="plan_validation_failed",
        contract_violations=[
            "Plan contains brittle heredoc-heavy or malformed commands"
        ],
        contract_diagnostics={
            "step_count": 3,
            "max_command_length": 1203,
            "heredoc_command_count": 0,
            "command_total_chars": 2445,
            "brittle_command_subcodes": ["oversized_command_length"],
            "brittle_command_step_details": {2: ["oversized_command_length"]},
            "shadow_warnings": shadow_warnings,
        },
        output_text='[{"step_number":2}]',
        strategy_info="plan_validation_failed",
    )

    metadata = events[0][2]
    assert metadata["brittle_command_subcodes"] == ["oversized_command_length"]
    assert metadata["brittle_command_step_details"] == {2: ["oversized_command_length"]}
    assert metadata["shadow_warnings"] == shadow_warnings


def test_planning_contract_violation_event_includes_truncated_subcodes():
    events = []
    ctx = MagicMock(
        session_id=55,
        task_id=10,
        task_execution_id=38,
        emit_live=lambda level, message, metadata=None: events.append(
            (level, message, metadata or {})
        ),
    )

    _emit_planning_diagnostics_contract_violation(
        ctx,
        reason="truncated_multistep_plan_detected",
        contract_violations=["truncated multi-step plan collapsed into a single step"],
        contract_diagnostics={
            "truncated_multistep_subcodes": [
                "original_steps_detected_3",
                "absorbed_into_step_1",
                "collapse_before_first_repair",
            ],
            "truncated_multistep_original_step_count": 3,
            "truncated_multistep_absorbing_step": 1,
            "truncated_multistep_repair_stage": "before_first_repair",
        },
        output_text='[{"step_number":1},{"step_number":2}]',
        strategy_info="truncated_multistep_plan_repair_requested",
    )

    metadata = events[0][2]
    assert metadata["contract_violation_type"] == (
        "truncated_multi_step_plan_collapsed_into_a_single_step"
    )
    assert metadata["truncated_multistep_subcodes"] == [
        "original_steps_detected_3",
        "absorbed_into_step_1",
        "collapse_before_first_repair",
    ]
    assert metadata["truncated_multistep_original_step_count"] == 3
    assert metadata["truncated_multistep_absorbing_step"] == 1
    assert metadata["truncated_multistep_repair_stage"] == "before_first_repair"


def test_terminal_validation_failure_details_include_brittle_subcodes_when_present():
    shadow_warnings = [
        {
            "rule_id": "model_behavior.command_length_prompt_patch",
            "category": "model_behavior_patch",
            "shadow_candidate": True,
        }
    ]
    verdict = type(
        "Verdict",
        (),
        {
            "reasons": ["Plan contains brittle heredoc-heavy or malformed commands"],
            "details": {
                "brittle_command_subcodes": ["oversized_command_length"],
                "brittle_command_step_details": {2: ["oversized_command_length"]},
                "shadow_warnings": shadow_warnings,
            },
        },
    )()

    details = _terminal_validation_failure_details(verdict)

    assert details["reason"] == "planning_validation_failed_after_repair"
    assert details["validation_reasons"] == [
        "Plan contains brittle heredoc-heavy or malformed commands"
    ]
    assert details["brittle_command_subcodes"] == ["oversized_command_length"]
    assert details["brittle_command_step_details"] == {2: ["oversized_command_length"]}
    assert details["shadow_warnings"] == shadow_warnings


def test_terminal_validation_failure_details_omit_brittle_keys_when_absent():
    verdict = type(
        "Verdict",
        (),
        {
            "reasons": ["Plan contains brittle heredoc-heavy or malformed commands"],
            "details": {},
        },
    )()

    details = _terminal_validation_failure_details(verdict)

    assert details == {
        "reason": "planning_validation_failed_after_repair",
        "planning_root_cause": "unknown",
        "validation_reasons": [
            "Plan contains brittle heredoc-heavy or malformed commands"
        ],
    }


def test_shadow_warnings_do_not_change_plan_validation_status():
    plan = [
        {
            "step_number": 1,
            "description": "Write source through a brittle shell fallback",
            "commands": [
                "cat > src/app.py <<'PY'\n"
                + "print('hello')\n" * 80
                + "PY\ncat > src/extra.py <<'PY'\nprint('extra')\nPY"
            ],
            "verification": "python -m py_compile src/app.py",
            "rollback": "rm -f src/app.py",
            "expected_files": ["src/app.py"],
        }
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Create a small Python implementation",
        execution_profile="implementation",
    )

    assert verdict.repairable
    assert (
        "Plan contains brittle heredoc-heavy or malformed commands" in verdict.reasons
    )

    shadow_warnings = verdict.details["shadow_warnings"]
    rule_ids = {warning["rule_id"] for warning in shadow_warnings}

    assert "model_behavior.heredoc_guidance" in rule_ids
    assert "model_behavior.command_length_prompt_patch" in rule_ids
    assert all(warning["shadow_candidate"] is True for warning in shadow_warnings)
