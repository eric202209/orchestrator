import json

import pytest
from app.services.orchestration.phases.planning_flow import (
    _build_repair_rejection_reasons,
)

from app.services.orchestration.planning.planner import PlannerService

from app.services.orchestration.validation.validator import ValidatorService


def test_validator_brittle_subcodes_oversized_printf_command(tmp_path):
    # Mirrors Board Game Cafe TaskExecution 37: step 2 command 1684 chars,
    # step 3 command 1668 chars — both above MAX_PLANNING_COMMAND_CHARS (900).
    long_body = "A" * 1200
    plan = [
        {
            "step_number": 1,
            "description": "Scaffold project",
            "commands": ["npm create vite@latest . -- --template react", "npm install"],
            "verification": "node -e \"require('fs').existsSync('src/App.jsx')\"",
            "rollback": None,
            "expected_files": ["src/App.jsx"],
        },
        {
            "step_number": 2,
            "description": "Write App component",
            "commands": [f"printf '{long_body}' > src/App.jsx"],
            "verification": "npm run build",
            "rollback": "rm -f src/App.jsx",
            "expected_files": ["src/App.jsx"],
        },
        {
            "step_number": 3,
            "description": "Write CSS",
            "commands": [f"printf '{long_body}' > src/App.css"],
            "verification": "npm run build",
            "rollback": None,
            "expected_files": ["src/App.css"],
        },
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Build a Board Game Cafe landing page",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert verdict.repairable is True
    assert "brittle heredoc-heavy" in " ".join(verdict.reasons)
    assert "oversized_command_length" in verdict.details["brittle_command_subcodes"]
    assert 2 in verdict.details["brittle_command_step_details"]
    assert 3 in verdict.details["brittle_command_step_details"]
    assert (
        "oversized_command_length" in verdict.details["brittle_command_step_details"][2]
    )
    assert (
        "oversized_command_length" in verdict.details["brittle_command_step_details"][3]
    )
    assert verdict.details["brittle_command_step_command_lengths"][2]
    assert verdict.details["brittle_command_step_command_lengths"][3]


def test_validator_brittle_subcodes_too_many_lines(tmp_path):
    long_command = "echo start\n" + "\n".join(f"echo {i}" for i in range(30))
    plan = [
        {
            "step_number": 1,
            "description": "Run many echo lines",
            "commands": [long_command],
            "verification": "echo ok",
            "rollback": None,
            "expected_files": [],
        }
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Do something",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert verdict.repairable is True
    assert "too_many_lines" in verdict.details["brittle_command_subcodes"]
    assert 1 in verdict.details["brittle_command_step_details"]
    assert "too_many_lines" in verdict.details["brittle_command_step_details"][1]


def test_validator_brittle_subcodes_multiple_heredoc_across_plan(tmp_path):
    heredoc1 = "mkdir -p src && cat > src/App.jsx <<'EOF'\nexport default function App() {}\nEOF"
    heredoc2 = "cat > src/App.css <<'EOF'\nbody { margin: 0; }\nEOF"
    plan = [
        {
            "step_number": 1,
            "description": "Write component",
            "commands": [heredoc1],
            "verification": "echo ok",
            "rollback": None,
            "expected_files": ["src/App.jsx"],
        },
        {
            "step_number": 2,
            "description": "Write CSS",
            "commands": [heredoc2],
            "verification": "echo ok",
            "rollback": None,
            "expected_files": ["src/App.css"],
        },
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Build a landing page",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert verdict.repairable is True
    assert "multiple_heredoc_across_plan" in verdict.details["brittle_command_subcodes"]


def test_validator_brittle_aggregate_reason_preserved_alongside_subcodes(tmp_path):
    long_body = "B" * 1000
    plan = [
        {
            "step_number": 1,
            "description": "Write oversized file",
            "commands": [f"printf '{long_body}' > out.txt"],
            "verification": "echo ok",
            "rollback": None,
            "expected_files": ["out.txt"],
        }
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Do something",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert verdict.repairable is True
    reasons_text = " ".join(verdict.reasons)
    assert "Plan contains brittle heredoc-heavy or malformed commands" in reasons_text
    assert "brittle_command_subcodes" in verdict.details
    assert verdict.details["brittle_command_subcodes"]


# Phase 6P: pass oversized command details into repair


def test_repair_rejection_reasons_prepend_oversized_command_details():
    reasons = ["Plan contains brittle heredoc-heavy or malformed commands"]
    details = {
        "brittle_command_subcodes": ["oversized_command_length"],
        "oversized_command_steps": [2, 3],
        "brittle_command_step_command_lengths": {2: [1684], 3: [1668]},
    }

    enriched = _build_repair_rejection_reasons(reasons, details)

    assert enriched[0].startswith("Step [2, 3]:")
    assert "oversized_command_length" in enriched[0]
    assert "step 2: 1684 chars" in enriched[0]
    assert "step 3: 1668 chars" in enriched[0]
    assert "max 900" in enriched[0]
    assert "No heredoc" in enriched[0]
    assert enriched[1:] == reasons


def test_repair_rejection_reasons_prepend_multiple_heredoc_details():
    reasons = ["Plan contains brittle heredoc-heavy or malformed commands"]
    details = {
        "brittle_command_subcodes": ["multiple_heredoc_across_plan"],
        "heredoc_command_count": 3,
    }

    enriched = _build_repair_rejection_reasons(reasons, details)

    assert enriched[0].startswith("Plan:")
    assert "3 heredoc blocks found" in enriched[0]
    assert "multiple_heredoc_across_plan" in enriched[0]
    assert "No heredoc" in enriched[0]
    assert enriched[1:] == reasons


def test_repair_rejection_reasons_prepend_too_many_lines_step_details():
    reasons = ["Plan contains brittle heredoc-heavy or malformed commands"]
    details = {
        "brittle_command_subcodes": ["too_many_lines"],
        "brittle_command_step_details": {
            1: ["too_many_lines"],
            2: ["oversized_command_length"],
            "3": ["too_many_lines"],
        },
    }

    enriched = _build_repair_rejection_reasons(reasons, details)

    assert enriched[0].startswith("Step [1, 3]:")
    assert "too_many_lines" in enriched[0]
    assert "Use ops write_file for file bodies" in enriched[0]
    assert "No heredoc" in enriched[0]
    assert enriched[1:] == reasons


def test_repair_rejection_reasons_prepend_weak_verification_step_details():
    reasons = [
        "Plan uses weak verification for implementation-heavy work (steps: [1, 2])"
    ]
    details = {"weak_verification_steps": [2, "1", "bad"]}

    enriched = _build_repair_rejection_reasons(reasons, details)

    assert enriched[0].startswith("weak_verification_steps:")
    assert "steps [1, 2]" in enriched[0]
    assert "replace with pytest, python -m, or npm run build" in enriched[0]
    assert "python -c file/content assertion is also valid" in enriched[0]
    assert enriched[1:] == reasons


def test_repair_rejection_reasons_prepend_missing_verification_step_details():
    reasons = [
        "Plan is missing verification commands for implementation-heavy work (steps: [1])"
    ]
    details = {"missing_verification_steps": ["1", "bad"]}

    enriched = _build_repair_rejection_reasons(reasons, details)

    assert enriched[0].startswith("missing_verification_steps:")
    assert "steps [1]" in enriched[0]
    assert "add pytest, python -m, npm run build" in enriched[0]
    assert enriched[1:] == reasons


def test_repair_rejection_reasons_prepend_heredoc_shape_subcodes():
    reasons = ["Plan contains brittle heredoc-heavy or malformed commands"]
    details = {
        "brittle_command_subcodes": ["disallowed_heredoc_shape"],
        "brittle_command_step_details": {1: ["disallowed_heredoc_shape"]},
    }

    enriched = _build_repair_rejection_reasons(reasons, details)

    assert enriched[0].startswith("Step [1]:")
    assert "disallowed_heredoc_shape" in enriched[0]
    assert "No heredoc" in enriched[0]
    assert enriched[1:] == reasons


def test_repair_prompt_includes_injected_oversized_rejection_line():
    reasons = _build_repair_rejection_reasons(
        ["Plan contains brittle heredoc-heavy or malformed commands"],
        {
            "brittle_command_subcodes": ["oversized_command_length"],
            "oversized_command_steps": [2],
            "brittle_command_step_command_lengths": {2: [1684]},
        },
    )

    prompt = PlannerService.build_planning_repair_prompt(
        "Build a landing page",
        malformed_output='[{"step_number":2,"commands":["printf ..."]}]',
        project_dir=__import__("pathlib").Path("/tmp/project"),
        rejection_reasons=reasons,
    )

    assert "Validation error:" in prompt
    assert "Step [2]: command body too long (oversized_command_length" in prompt
    assert "step 2: 1684 chars" in prompt


def test_repair_prompt_includes_injected_brittle_shape_rejection_lines():
    reasons = _build_repair_rejection_reasons(
        ["Plan contains brittle heredoc-heavy or malformed commands"],
        {
            "brittle_command_subcodes": [
                "multiple_heredoc_across_plan",
                "too_many_lines",
            ],
            "heredoc_command_count": 2,
            "brittle_command_step_details": {1: ["too_many_lines"]},
        },
    )

    prompt = PlannerService.build_planning_repair_prompt(
        "Build a static page",
        malformed_output='[{"step_number":1,"commands":["cat > index.html <<EOF"]}]',
        project_dir=__import__("pathlib").Path("/tmp/project"),
        rejection_reasons=reasons,
    )

    assert "Validation error:" in prompt
    assert "Plan: 2 heredoc blocks found (multiple_heredoc_across_plan)" in prompt
    assert "Step [1]: command body too long (too_many_lines)" in prompt


def test_repair_prompt_includes_injected_weak_verification_rejection_line():
    reasons = _build_repair_rejection_reasons(
        ["Plan uses weak verification for implementation-heavy work (steps: [1, 2])"],
        {"weak_verification_steps": [1, 2]},
    )

    prompt = PlannerService.build_planning_repair_prompt(
        "Build a FastAPI health endpoint",
        malformed_output='[{"step_number":1,"verification":null}]',
        project_dir=__import__("pathlib").Path("/tmp/project"),
        rejection_reasons=reasons,
    )

    assert "Validation error:" in prompt
    assert "weak_verification_steps: steps [1, 2]" in prompt
    assert "pytest, python -m, or npm run build" in prompt
    assert "python -c file/content assertion is also valid" in prompt


def test_planning_repair_prompt_guides_python_source_syntax_invalid():
    prompt = PlannerService.build_planning_repair_prompt(
        "Add a summary command to this Python CLI.",
        malformed_output=json.dumps(
            [
                {
                    "step_number": 2,
                    "description": "Write CLI source",
                    "commands": ["python3 -m py_compile src/medium_cli/cli.py"],
                    "verification": "python3 -m py_compile src/medium_cli/cli.py",
                    "rollback": None,
                    "expected_files": ["src/medium_cli/cli.py"],
                    "ops": [
                        {
                            "op": "write_file",
                            "path": "src/medium_cli/cli.py",
                            "content": (
                                'def summary():\\n    print("Summary command executed")\n'
                            ),
                        }
                    ],
                }
            ]
        ),
        project_dir=__import__("pathlib").Path("/tmp/project"),
        rejection_reasons=[
            "Plan writes Python source with invalid syntax "
            "(python_source_syntax_invalid; src/medium_cli/cli.py line 1, "
            "offset 15: unexpected character after line continuation character)"
        ],
    )

    assert "Python source syntax repair:" in prompt
    assert "complete valid Python source content" in prompt
    assert "literal backslash-n text" in prompt
    assert "real newline characters" in prompt
    assert "ops.write_file with complete grounded file content" in prompt
    assert "python3 -m py_compile <file>" in prompt
