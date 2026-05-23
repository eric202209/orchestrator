from pathlib import Path

from app.services.orchestration.planning.planner import (
    PLANNING_REPAIR_PROMPT_MAX_CHARS,
    PlannerService,
)
from app.services.orchestration.phases.execution_loop import (
    _debug_ops_have_placeholder_content,
    _is_read_only_inspection_command,
)
from app.services.session.session_inspection_service import (
    _classify_test_scaffold_failure,
)


def test_phase10n_test_scaffold_guidance_is_in_planning_and_repair_prompts(
    tmp_path: Path,
):
    minimal = PlannerService.build_minimal_planning_prompt(
        "Add ReportService.generate_summary with tests",
        tmp_path,
        workspace_has_existing_files=True,
    )
    ultra = PlannerService.build_ultra_minimal_planning_prompt(
        "Add ReportService.generate_summary with tests",
        tmp_path,
        workspace_has_existing_files=True,
    )
    repair = PlannerService.build_planning_repair_prompt(
        "Add ReportService.generate_summary with tests",
        '[{"bad": true}]',
        tmp_path,
        rejection_reasons=["missing verification"],
    )
    compact = PlannerService.build_compact_planning_repair_prompt(
        '[{"bad": true}]',
        rejection_reasons=["missing verification"],
    )

    for prompt in (minimal, ultra, repair, compact):
        assert "inspect nearby tests first" in prompt
        assert "fixtures, factories, and domain constructors" in prompt
        assert "raw dicts" in prompt
        assert "Compile changed Python tests" in prompt
        assert "python -m pytest tests/ -q" in prompt

    assert len(compact) < PLANNING_REPAIR_PROMPT_MAX_CHARS


def test_phase10n_unittest_inference_prefers_pytest_when_project_signal_exists():
    plan = [
        {
            "step_number": 1,
            "description": "Create tests/test_smoke_status.py unittest coverage",
            "commands": [],
            "verification": None,
            "rollback": "rm -f tests/test_smoke_status.py",
            "expected_files": ["tests/test_smoke_status.py"],
        }
    ]
    task_prompt = (
        "pytest.ini exists. Add unittest coverage in tests/test_smoke_status.py. "
        "Run scripts/smoke_status.py and stdout equals 'ready'."
    )

    sanitized = PlannerService.sanitize_common_plan_issues(
        plan, task_prompt=task_prompt
    )

    assert sanitized[0]["verification"] == "python -m pytest tests/ -q"
    assert sanitized[0]["commands"] == []
    assert sanitized[0]["ops"][0]["op"] == "write_file"
    assert sanitized[0]["ops"][0]["path"] == "tests/test_smoke_status.py"
    assert "def test_smoke_status_output" in sanitized[0]["ops"][0]["content"]
    assert "import unittest" not in sanitized[0]["ops"][0]["content"]


def test_phase10n_unittest_inference_keeps_unittest_without_pytest_signal():
    plan = [
        {
            "step_number": 1,
            "description": "Create tests/test_smoke_status.py unittest coverage",
            "commands": [],
            "verification": None,
            "rollback": "rm -f tests/test_smoke_status.py",
            "expected_files": ["tests/test_smoke_status.py"],
        }
    ]
    task_prompt = (
        "Add unittest coverage in tests/test_smoke_status.py. "
        "Run scripts/smoke_status.py and stdout equals 'ready'."
    )

    sanitized = PlannerService.sanitize_common_plan_issues(
        plan, task_prompt=task_prompt
    )

    assert sanitized[0]["verification"] == "python -m unittest discover -s tests"
    assert "import unittest" in sanitized[0]["ops"][0]["content"]


def test_phase10n_test_scaffold_failure_classifier_uses_specific_buckets():
    assert (
        _classify_test_scaffold_failure(
            "pytest failed: AttributeError: 'dict' object has no attribute 'title'"
        )
        == "test_scaffold_type_mismatch"
    )
    assert (
        _classify_test_scaffold_failure(
            "NameError: name 'Task' is not defined while running test_report.py"
        )
        == "test_scaffold_import_error"
    )
    assert _classify_test_scaffold_failure("assert 2 == 3") is None


def test_phase10n_read_only_inspection_allows_workspace_grep():
    assert _is_read_only_inspection_command(
        "grep -r 'Task' src/models/ src/services/ tests/unit/"
    )
    assert _is_read_only_inspection_command("rg 'Task\\(' tests/unit src/models")
    assert not _is_read_only_inspection_command("grep -r 'Task' src/ > report.txt")
    assert not _is_read_only_inspection_command("grep -r 'Task' /etc")


def test_phase10n_debug_ops_reject_placeholder_test_content():
    assert _debug_ops_have_placeholder_content(
        [
            {
                "op": "write_file",
                "path": "tests/unit/test_report_service.py",
                "content": "def test_report_service():\n    pass\n",
            }
        ]
    )
    assert not _debug_ops_have_placeholder_content(
        [
            {
                "op": "write_file",
                "path": "tests/unit/test_report_service.py",
                "content": "def test_report_service():\n    assert report.total == 3\n",
            }
        ]
    )


def test_phase10n_plan_flags_replace_ops_when_old_text_is_absent(tmp_path: Path):
    test_file = tmp_path / "tests" / "unit" / "test_report_service.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text("def test_existing():\n    assert True\n", encoding="utf-8")

    issues = PlannerService.find_immediate_repair_step_issues(
        [
            {
                "step_number": 2,
                "description": "Patch report service tests",
                "commands": [],
                "verification": "python -m py_compile tests/unit/test_report_service.py",
                "rollback": None,
                "expected_files": ["tests/unit/test_report_service.py"],
                "ops": [
                    {
                        "op": "replace_in_file",
                        "path": "tests/unit/test_report_service.py",
                        "old": "def test_missing():",
                        "new": "def test_missing():\n    assert True\n",
                    }
                ],
            }
        ],
        project_dir=tmp_path,
    )

    assert issues["stale_replace_ops_steps"] == [2]

    hints = PlannerService.stale_replace_repair_hints(
        [
            {
                "step_number": 2,
                "ops": [
                    {
                        "op": "replace_in_file",
                        "path": "tests/unit/test_report_service.py",
                        "old": "def test_missing():",
                        "new": "def test_missing():\n    assert True\n",
                    }
                ],
            }
        ],
        tmp_path,
    )

    assert "old text not found" in hints[0]
    assert "def test_existing" in hints[0]
