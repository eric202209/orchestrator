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

    setattr(retry_state, reason.cap_attribute, True)

    exhausted = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        blocking_repair_issues={"stale_replace_ops_steps": [2]},
    )

    assert exhausted is not None
    assert exhausted.cap_used


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
