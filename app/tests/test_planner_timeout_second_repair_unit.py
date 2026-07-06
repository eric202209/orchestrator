import pytest
from app.services.orchestration.phases.planning_flow import (
    _PlanningRetryState,
    _get_targeted_second_repair_reason,
)


def test_targeted_second_repair_reason_centralizes_blocking_eligibility():
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True

    reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        blocking_repair_issues={"weak_verification_steps": [3, 2]},
    )

    assert reason is not None
    assert reason.issue_key == "weak_verification_steps"
    assert reason.event_reason == "post_repair_weak_verification_second_pass"
    assert reason.semantic_violation_code == "weak_verification"
    assert reason.step_numbers == [3, 2]
    assert reason.cap_used is False
    assert reason.cap_attribute == "post_repair_blocking_second_repair_used"
    assert "steps [3, 2]" in reason.rejection_text


def test_targeted_second_repair_reason_requires_prior_repair():
    retry_state = _PlanningRetryState()

    reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        blocking_repair_issues={"weak_verification_steps": [1]},
    )

    assert reason is None


def test_targeted_second_repair_reason_rejects_mixed_blocking_classes():
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True

    reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        blocking_repair_issues={
            "weak_verification_steps": [1],
            "background_process_steps": [2],
        },
    )

    assert reason is None


def test_targeted_second_repair_reason_respects_blocking_cap():
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True
    retry_state.post_repair_blocking_second_repair_used = True

    reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        blocking_repair_issues={"background_process_steps": [1]},
    )

    assert reason is not None
    assert reason.issue_key == "background_process_steps"
    assert reason.cap_used is True


def test_targeted_second_repair_reason_centralizes_validator_eligibility():
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True
    verdict = type(
        "Verdict",
        (),
        {
            "reasons": [
                "Plan is missing verification commands for implementation-heavy work (steps: [1])"
            ],
            "details": {
                "missing_verification_steps": [1],
                "semantic_violation_codes": ["missing_verification_command"],
            },
        },
    )()

    reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        plan_verdict=verdict,
    )

    assert reason is not None
    assert reason.issue_key == "missing_verification_steps"
    assert reason.event_reason == "post_repair_missing_verification_second_pass"
    assert reason.semantic_violation_code == "missing_verification_command"
    assert reason.cap_attribute == "post_repair_validation_second_repair_used"
    assert "implementation-heavy step" in reason.rejection_text


def test_targeted_second_repair_reason_handles_missing_runnable_commands():
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True
    verdict = type(
        "Verdict",
        (),
        {
            "reasons": ["Plan contains steps without runnable commands (steps: [3])"],
            "details": {
                "missing_commands_steps": [3],
            },
        },
    )()

    reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        plan_verdict=verdict,
    )

    assert reason is not None
    assert reason.issue_key == "missing_commands_steps"
    assert reason.event_reason == "post_repair_missing_commands_second_pass"
    assert reason.semantic_violation_code == "missing_runnable_command"
    assert reason.cap_attribute == "post_repair_validation_second_repair_used"
    assert "runnable command" in reason.rejection_text


def test_targeted_second_repair_reason_handles_python_source_syntax_invalid():
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True
    verdict = type(
        "Verdict",
        (),
        {
            "reasons": [
                "Plan writes Python source with invalid syntax "
                "(python_source_syntax_invalid; src/app.py line 1, offset 5: invalid syntax)"
            ],
            "details": {
                "python_source_syntax_invalid": [
                    {
                        "path": "src/app.py",
                        "line": 1,
                        "offset": 5,
                        "message": "invalid syntax",
                        "candidate_content_excerpt": "def broken(: pass",
                    }
                ],
                "semantic_violation_codes": ["python_source_syntax_invalid"],
            },
        },
    )()

    reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        plan_verdict=verdict,
    )

    assert reason is not None
    assert reason.issue_key == "python_source_syntax_invalid"
    assert reason.event_reason == "post_repair_python_source_syntax_second_pass"
    assert reason.semantic_violation_code == "python_source_syntax_invalid"
    assert reason.cap_attribute == "post_repair_python_source_syntax_second_repair_used"
    assert "src/app.py line 1, offset 5" in reason.rejection_text
    assert "invalid syntax" in reason.rejection_text
    assert "def broken(: pass" in reason.rejection_text
    assert "valid JSON array only" in reason.rejection_text
    assert "ops.write_file" in reason.rejection_text
    assert "compile(content, path, 'exec')" in reason.rejection_text


def test_targeted_second_repair_reason_preserves_line_numbered_syntax_excerpt():
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True
    candidate_content = (
        '"""Broken module docstring.\n'
        "from __future__ import annotations\n"
        "\n"
        "def main() -> int:\n"
        "    return 0\n"
    )
    verdict = type(
        "Verdict",
        (),
        {
            "reasons": [
                "Plan writes Python source with invalid syntax "
                "(python_source_syntax_invalid; src/app.py line 1, offset 1: "
                "unterminated triple-quoted string literal)"
            ],
            "details": {
                "python_source_syntax_invalid": [
                    {
                        "path": "src/app.py",
                        "line": 1,
                        "offset": 1,
                        "message": "unterminated triple-quoted string literal",
                        "candidate_content": candidate_content,
                        "candidate_content_excerpt": "flattened fallback",
                    }
                ],
                "semantic_violation_codes": ["python_source_syntax_invalid"],
            },
        },
    )()

    reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        plan_verdict=verdict,
    )

    assert reason is not None
    assert "Candidate source excerpt with real newlines preserved:" in (
        reason.rejection_text
    )
    assert '   1: """Broken module docstring.' in reason.rejection_text
    assert "   2: from __future__ import annotations" in reason.rejection_text
    assert "   4: def main() -> int:" in reason.rejection_text
    assert '"""Broken module docstring.\n   2:' in reason.rejection_text
    assert "flattened fallback" not in reason.rejection_text


def test_targeted_second_repair_reason_windows_large_syntax_excerpt():
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True
    lines = [f"value_{index} = {index}" for index in range(1, 260)]
    lines[150] = "def broken(:"
    candidate_content = "\n".join(lines) + "\n"
    verdict = type(
        "Verdict",
        (),
        {
            "reasons": [
                "Plan writes Python source with invalid syntax "
                "(python_source_syntax_invalid; src/app.py line 151, offset 11: "
                "invalid syntax)"
            ],
            "details": {
                "python_source_syntax_invalid": [
                    {
                        "path": "src/app.py",
                        "line": 151,
                        "offset": 11,
                        "message": "invalid syntax",
                        "candidate_content": candidate_content,
                    }
                ],
                "semantic_violation_codes": ["python_source_syntax_invalid"],
            },
        },
    )()

    reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        plan_verdict=verdict,
    )

    assert reason is not None
    assert "... preceding lines omitted ..." in reason.rejection_text
    assert "... following lines omitted ..." in reason.rejection_text
    assert " 151: def broken(:" in reason.rejection_text
    assert "   1: value_1 = 1" not in reason.rejection_text
    assert " 259: value_259 = 259" not in reason.rejection_text


def test_targeted_second_repair_reason_handles_argparse_framework_mismatch(tmp_path):
    source_dir = tmp_path / "src" / "medium_cli"
    source_dir.mkdir(parents=True)
    (source_dir / "cli.py").write_text(
        "\n".join(
            [
                "import argparse",
                "from medium_cli.formatting import format_task_line",
                "from medium_cli.store import TaskStore",
                "",
                "def build_parser() -> argparse.ArgumentParser:",
                "    parser = argparse.ArgumentParser()",
                "    subparsers = parser.add_subparsers(dest='command', required=True)",
                "    subparsers.add_parser('list')",
                "    return parser",
                "",
                "def build_store() -> TaskStore:",
                "    return TaskStore()",
                "",
                "def main(argv=None) -> int:",
                "    parser = build_parser()",
                "    args = parser.parse_args(argv)",
                "    if args.command == 'list':",
                "        return 0",
                "    return 2",
                "",
            ]
        ),
        encoding="utf-8",
    )
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True
    verdict = type(
        "Verdict",
        (),
        {
            "reasons": [
                "Plan writes Python decorators whose root name is undefined "
                "(files: ['src/medium_cli/cli.py'])"
            ],
            "details": {
                "undefined_python_decorator_materializations": [
                    "src/medium_cli/cli.py"
                ],
            },
        },
    )()

    reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        plan_verdict=verdict,
        project_dir=tmp_path,
    )

    assert reason is not None
    assert reason.issue_key == "framework_mismatch"
    assert reason.event_reason == "post_repair_framework_mismatch_second_pass"
    assert reason.semantic_violation_code == "framework_mismatch"
    assert reason.cap_attribute == "post_repair_framework_second_repair_used"
    assert "detected framework: argparse" in reason.rejection_text
    assert "src/medium_cli/cli.py" in reason.rejection_text
    assert "def build_parser()" in reason.rejection_text
    assert "def main(argv=None)" in reason.rejection_text
    assert "build_store" in reason.rejection_text
    assert "TaskStore" in reason.rejection_text
    assert "format_task_line" in reason.rejection_text
    assert "@click.command" in reason.rejection_text
    assert "@cli.command" in reason.rejection_text
    assert "click.echo" in reason.rejection_text
    assert "add a summary subparser" in reason.rejection_text
    assert "valid JSON array only" in reason.rejection_text
    assert "ops.write_file" in reason.rejection_text
    assert "compile(content, path, 'exec')" in reason.rejection_text


def test_targeted_second_repair_reason_skips_non_argparse_decorator_mismatch(tmp_path):
    source_dir = tmp_path / "src" / "api"
    source_dir.mkdir(parents=True)
    (source_dir / "routes.py").write_text(
        "from fastapi import APIRouter\n\nrouter = APIRouter()\n",
        encoding="utf-8",
    )
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True
    verdict = type(
        "Verdict",
        (),
        {
            "reasons": [
                "Plan writes Python decorators whose root name is undefined "
                "(files: ['src/api/routes.py'])"
            ],
            "details": {
                "undefined_python_decorator_materializations": ["src/api/routes.py"],
            },
        },
    )()

    reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        plan_verdict=verdict,
        project_dir=tmp_path,
    )

    assert reason is None


def test_targeted_second_repair_reason_adds_brittle_eligibility_when_only_issue():
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True
    verdict = type(
        "Verdict",
        (),
        {
            "reasons": ["Plan contains brittle heredoc-heavy or malformed commands"],
            "details": {
                "brittle_command_subcodes": ["oversized_command_length"],
                "brittle_command_step_details": {1: ["oversized_command_length"]},
                "semantic_violation_codes": ["brittle_command"],
            },
        },
    )()

    reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        plan_verdict=verdict,
    )
    assert reason is not None
    assert reason.issue_key == "brittle_commands"
    assert reason.event_reason == "post_repair_brittle_commands_second_pass"
    assert "write_file" in reason.rejection_text


def test_targeted_second_repair_reason_brittle_blocked_when_other_issues_exist():
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True
    verdict = type(
        "Verdict",
        (),
        {
            "reasons": [
                "Plan contains brittle heredoc-heavy or malformed commands",
                "Plan contains steps without runnable commands (steps: [2])",
            ],
            "details": {
                "brittle_command_subcodes": ["oversized_command_length"],
                "missing_commands_steps": [2],
                "semantic_violation_codes": ["brittle_command"],
            },
        },
    )()

    # When other blocking issues exist alongside brittle commands, brittle
    # second repair should not fire (missing_commands_steps takes priority).
    reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        plan_verdict=verdict,
    )
    # missing_commands_steps is a blocking key for brittle, but brittle_command_subcodes
    # is also a blocking key for missing_commands_steps — neither fires; returns None.
    assert reason is None


def test_targeted_second_repair_reason_centralizes_malformed_shell_eligibility():
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True

    reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        malformed_shell_quoting_violation=True,
    )

    assert reason is not None
    assert reason.issue_key == "malformed_shell_quoting"
    assert reason.event_reason == "post_repair_malformed_shell_quoting_second_pass"
    assert reason.semantic_violation_code == "malformed_shell_quoting"
    assert reason.cap_attribute == "post_repair_malformed_shell_second_repair_used"
    assert "python -c snippets" in reason.rejection_text
