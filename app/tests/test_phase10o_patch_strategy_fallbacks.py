from pathlib import Path

from app.services.orchestration.phases.planning_support import (
    _PlanningRetryState,
    _get_targeted_second_repair_reason,
    _model_lane_limitation_for_invalid_planning_commands,
)
from app.services.orchestration.planning.planner import PlannerService
from app.services.session.session_inspection_service import (
    _classify_test_scaffold_failure,
)


def test_phase10o_stale_replace_fallback_hints_preserve_test_assertions(
    tmp_path: Path,
):
    test_file = tmp_path / "tests" / "unit" / "test_report_service.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "def test_existing_report_summary():\n"
        "    summary = service.project_summary(tasks)\n"
        "    assert summary['total'] == 3\n",
        encoding="utf-8",
    )

    hints = PlannerService.stale_replace_fallback_hints(
        [
            {
                "step_number": 2,
                "ops": [
                    {
                        "op": "replace_in_file",
                        "path": "tests/unit/test_report_service.py",
                        "old": "def test_missing_report_summary():",
                        "new": "def test_missing_report_summary():\n    assert True\n",
                    }
                ],
            }
        ],
        tmp_path,
    )

    assert len(hints) == 1
    assert "patch_strategy_fallback_required" in hints[0]
    assert "do not emit another replace_in_file" in hints[0]
    assert "ops.write_file with complete preserved file content" in hints[0]
    assert "preserve existing tests and assertion intent" in hints[0]
    assert "assert summary['total'] == 3" in hints[0]


def test_phase10o_stale_replace_fallback_repair_prompt_keeps_file_excerpt(
    tmp_path: Path,
):
    cli_file = tmp_path / "src" / "small_cli" / "cli.py"
    cli_file.parent.mkdir(parents=True)
    cli_file.write_text(
        '"""Tiny message-printing CLI used by the orchestrator eval fixture."""\n'
        "\n"
        "from __future__ import annotations\n"
        "\n"
        "import argparse\n"
        "\n"
        "\n"
        "def format_message(message: str) -> str:\n"
        "    return message\n"
        "\n"
        "\n"
        "def build_parser() -> argparse.ArgumentParser:\n"
        '    parser = argparse.ArgumentParser(description="Print a message.")\n'
        '    parser.add_argument("message", help="Message to print")\n'
        "    return parser\n"
        "\n"
        "\n"
        "def main(argv: list[str] | None = None) -> int:\n"
        "    parser = build_parser()\n"
        "    args = parser.parse_args(argv)\n"
        "    print(format_message(args.message))\n"
        "    return 0\n",
        encoding="utf-8",
    )
    plan = [
        {
            "step_number": 2,
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "src/small_cli/cli.py",
                    "old": "def main()",
                    "new": "def main(argv: list[str] | None = None) -> int:",
                }
            ],
        }
    ]
    reasons = [
        "stale_replace_ops_steps: steps [2] still use replace_in_file with old "
        "text that is absent from the current workspace. Exact-text patching is "
        "exhausted for these targets; do not emit another replace_in_file for "
        "the same missing old text or same target. Use ops.write_file with "
        "complete preserved file content grounded in the current file excerpt",
        *PlannerService.stale_replace_fallback_hints(plan, tmp_path),
    ]

    prompt = PlannerService.build_planning_repair_prompt(
        "Implement --uppercase for the small_cli CLI",
        malformed_output='[{"step_number":2,"ops":[{"op":"replace_in_file"}]}]',
        project_dir=tmp_path,
        rejection_reasons=reasons,
    )

    assert "Current file excerpt:" in prompt
    assert "def main(argv: list[str] | None = None) -> int:" in prompt
    assert 'parser.add_argument("message", help="Message to print")' in prompt
    assert "do not emit another replace_in_file" in prompt


def test_phase10o_python_test_repair_prompt_includes_imported_source_excerpt(
    tmp_path: Path,
):
    cli_file = tmp_path / "src" / "small_cli" / "cli.py"
    cli_file.parent.mkdir(parents=True)
    cli_file.write_text(
        "from __future__ import annotations\n"
        "\n"
        "import argparse\n"
        "\n"
        "\n"
        "def format_message(message: str) -> str:\n"
        "    return message\n"
        "\n"
        "\n"
        "def build_parser() -> argparse.ArgumentParser:\n"
        "    parser = argparse.ArgumentParser(description='Print a message.')\n"
        "    parser.add_argument('message', help='Message to print')\n"
        "    return parser\n"
        "\n"
        "\n"
        "def main(argv: list[str] | None = None) -> int:\n"
        "    parser = build_parser()\n"
        "    args = parser.parse_args(argv)\n"
        "    print(format_message(args.message))\n"
        "    return 0\n",
        encoding="utf-8",
    )
    test_file = tmp_path / "tests" / "test_cli.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "from small_cli.cli import build_parser, main\n"
        "\n"
        "\n"
        "def test_uppercase_option_prints_uppercase_message(capsys):\n"
        "    assert main(['--uppercase', 'hello']) == 0\n"
        "    assert capsys.readouterr().out.strip() == 'HELLO'\n",
        encoding="utf-8",
    )

    prompt = PlannerService.build_planning_repair_prompt(
        "Implement --uppercase for the small_cli CLI",
        malformed_output=(
            '[{"step_number":3,"ops":[{"op":"write_file",'
            '"path":"tests/test_cli.py"}]}]'
        ),
        project_dir=tmp_path,
        rejection_reasons=[
            "test_assertion_loss_ops_steps: steps [3] rewrite an existing "
            "Python test file with fewer assertions: tests/test_cli.py"
        ],
    )

    assert "## PYTHON TEST SOURCE CONTEXT" in prompt
    assert "source excerpt imported by tests: src/small_cli/cli.py" in prompt
    assert "import argparse" in prompt
    assert "def build_parser() -> argparse.ArgumentParser:" in prompt
    assert "def main(argv: list[str] | None = None) -> int:" in prompt


def test_phase10o_python_test_repair_prompt_has_api_preservation_guidance(
    tmp_path: Path,
):
    (tmp_path / "src" / "small_cli").mkdir(parents=True)
    (tmp_path / "src" / "small_cli" / "cli.py").write_text(
        "import argparse\n"
        "\n"
        "def build_parser():\n"
        "    return argparse.ArgumentParser()\n"
        "\n"
        "def main(argv=None):\n"
        "    return 0\n",
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_cli.py").write_text(
        "from small_cli.cli import build_parser, main\n",
        encoding="utf-8",
    )

    prompt = PlannerService.build_planning_repair_prompt(
        "Implement --uppercase for the small_cli CLI",
        malformed_output='[{"ops":[{"path":"tests/test_cli.py"}]}]',
        project_dir=tmp_path,
        rejection_reasons=[
            "stale_replace_ops_steps: step 3 replace_in_file old text not found "
            "in tests/test_cli.py"
        ],
    )

    assert "Preserve public functions called by tests" in prompt
    assert "main(argv)" in prompt
    assert "build_parser()" in prompt
    assert "Do not switch argparse to Click or Typer" in prompt
    assert "Implement behavior in source code" in prompt


def test_phase10o_python_test_source_context_stays_bounded(tmp_path: Path):
    (tmp_path / "src" / "small_cli").mkdir(parents=True)
    (tmp_path / "src" / "small_cli" / "cli.py").write_text(
        "import argparse\n" + ("# filler\n" * 1000),
        encoding="utf-8",
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_cli.py").write_text(
        "from small_cli.cli import main\n",
        encoding="utf-8",
    )

    prompt = PlannerService.build_planning_repair_prompt(
        "Implement --uppercase for the small_cli CLI",
        malformed_output='[{"ops":[{"path":"tests/test_cli.py"}]}]',
        project_dir=tmp_path,
        rejection_reasons=["test_assertion_loss_ops_steps: rewrite tests/test_cli.py"],
    )

    source_context = prompt.split("## PYTHON TEST SOURCE CONTEXT", 1)[1].split(
        "PROJECT STRUCTURE CAPSULE", 1
    )[0]
    assert len(source_context) <= 1400
    assert len(prompt) <= 6000


def test_phase10o_stale_replace_after_repair_gets_fallback_second_pass():
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True

    reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        blocking_repair_issues={"stale_replace_ops_steps": [2]},
    )

    assert reason is not None
    assert reason.issue_key == "stale_replace_ops_steps"
    assert reason.retry_reason == "post_repair_stale_replace_fallback"
    assert reason.event_reason == "post_repair_stale_replace_fallback_pass"
    assert reason.semantic_violation_code == "patch_strategy_fallback_required"
    assert "Exact-text patching is exhausted" in reason.rejection_text
    assert not reason.cap_used
    assert reason.cap_attribute == "post_repair_stale_replace_second_repair_used"

    setattr(retry_state, reason.cap_attribute, True)

    exhausted = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        blocking_repair_issues={"stale_replace_ops_steps": [2]},
    )

    assert exhausted is not None
    assert exhausted.cap_used


def test_phase10o_stale_replace_gets_own_cap_after_test_preservation_repair():
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True

    test_preservation = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        blocking_repair_issues={"test_assertion_loss_ops_steps": [3]},
    )
    assert test_preservation is not None
    assert test_preservation.cap_attribute == "post_repair_blocking_second_repair_used"
    setattr(retry_state, test_preservation.cap_attribute, True)

    stale_fallback = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        blocking_repair_issues={"stale_replace_ops_steps": [2]},
    )

    assert stale_fallback is not None
    assert stale_fallback.issue_key == "stale_replace_ops_steps"
    assert stale_fallback.retry_reason == "post_repair_stale_replace_fallback"
    assert (
        stale_fallback.cap_attribute == "post_repair_stale_replace_second_repair_used"
    )
    assert stale_fallback.cap_used is False


def test_phase10o_repeated_stale_replace_fallback_is_capped():
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True
    retry_state.post_repair_stale_replace_second_repair_used = True

    reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        blocking_repair_issues={"stale_replace_ops_steps": [2]},
    )

    assert reason is not None
    assert reason.issue_key == "stale_replace_ops_steps"
    assert reason.cap_used is True


def test_phase10o_flags_write_file_fallback_that_drops_test_assertions(
    tmp_path: Path,
):
    test_file = tmp_path / "tests" / "test_report_service.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "def test_report_summary():\n"
        "    assert service.summary()['total'] == 3\n"
        "    assert service.summary()['done'] == 1\n",
        encoding="utf-8",
    )

    issues = PlannerService.find_immediate_repair_step_issues(
        [
            {
                "step_number": 3,
                "description": "Fallback rewrite report tests",
                "commands": [],
                "verification": "python -m pytest tests/ -q",
                "rollback": None,
                "expected_files": ["tests/test_report_service.py"],
                "ops": [
                    {
                        "op": "write_file",
                        "path": "tests/test_report_service.py",
                        "content": (
                            "def test_report_summary():\n"
                            "    assert service.summary()['total'] == 3\n"
                        ),
                    }
                ],
            }
        ],
        project_dir=tmp_path,
    )

    assert issues["test_assertion_loss_ops_steps"] == [3]


def test_phase10o_assertion_loss_after_repair_gets_preservation_second_pass():
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True

    reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        blocking_repair_issues={"test_assertion_loss_ops_steps": [3]},
    )

    assert reason is not None
    assert reason.retry_reason == "post_repair_test_assertion_preservation"
    assert reason.semantic_violation_code == "test_assertion_preservation_failed"
    assert "fewer assertions" in reason.rejection_text
    assert "Preserve existing tests and assertion intent" in reason.rejection_text


def test_phase10o_recovery_bucket_classifies_patch_strategy_failures():
    assert (
        _classify_test_scaffold_failure(
            "Planning repair still produced invalid commands: "
            "stale_replace_ops_steps=[2]; "
            "model_lane_limitation=repeated_stale_exact_patch_after_capsule"
        )
        == "model_lane_repeated_stale_exact_patch"
    )
    assert (
        _classify_test_scaffold_failure(
            "planning failed: post_repair_stale_replace_fallback; "
            "patch_strategy_fallback_required"
        )
        == "patch_strategy_fallback_required"
    )
    assert (
        _classify_test_scaffold_failure(
            "Planning repair still produced invalid commands: "
            "stale_replace_ops_steps=[2]"
        )
        == "stale_replace_in_file_old_text"
    )
    assert (
        _classify_test_scaffold_failure(
            "test_assertion_loss_ops_steps: rewrite has fewer assertions"
        )
        == "test_assertion_preservation_failed"
    )
    assert (
        _classify_test_scaffold_failure("test_deletion_ops_steps=[4]")
        == "test_preservation_violation"
    )


def test_phase10u_repeated_stale_patch_records_model_lane_limitation():
    marker = _model_lane_limitation_for_invalid_planning_commands(
        {"stale_replace_ops_steps": [2]}
    )

    assert marker == {
        "model_lane_limitation": "repeated_stale_exact_patch_after_capsule",
        "failure_cause_bucket": "model_lane_repeated_stale_exact_patch",
        "runtime_rewrite_added": False,
        "recommended_action": (
            "Treat as planner/model-lane limitation. Use better planning context "
            "or scoped prompt guidance; do not add another runtime normalizer."
        ),
    }
    assert (
        _model_lane_limitation_for_invalid_planning_commands(
            {"weak_verification_steps": [1]}
        )
        is None
    )


def test_phase10o_flags_delete_file_fallback_for_existing_python_tests(
    tmp_path: Path,
):
    test_file = tmp_path / "tests" / "test_report_service.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "def test_report_summary():\n" "    assert service.summary()['total'] == 3\n",
        encoding="utf-8",
    )

    issues = PlannerService.find_immediate_repair_step_issues(
        [
            {
                "step_number": 4,
                "description": "Remove stale report tests",
                "commands": [],
                "verification": "python -m pytest tests/ -q",
                "rollback": None,
                "expected_files": ["tests/test_report_service.py"],
                "ops": [
                    {
                        "op": "delete_file",
                        "path": "tests/test_report_service.py",
                    }
                ],
            }
        ],
        project_dir=tmp_path,
    )

    assert issues["test_deletion_ops_steps"] == [4]


def test_phase10o_test_delete_after_repair_gets_preservation_second_pass():
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True

    reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        blocking_repair_issues={"test_deletion_ops_steps": [4]},
    )

    assert reason is not None
    assert reason.retry_reason == "post_repair_test_deletion_preservation"
    assert reason.semantic_violation_code == "test_preservation_violation"
    assert "delete existing Python test files" in reason.rejection_text
    assert "Do not delete tests during fallback repair" in reason.rejection_text
