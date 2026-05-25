from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.phases.execution_loop import (
    _execute_local_shell_commands_step,
)
from app.services.orchestration.phases.planning_flow import (
    _prune_unmaterialized_expected_files,
)
from app.services.orchestration.validation.validator import ValidatorService


def test_w25_flags_fake_test_output_artifact_verification():
    plan = [
        {
            "step_number": 1,
            "description": "Run unit tests",
            "commands": ["python -m unittest discover -s tests"],
            "verification": (
                "python -c 'import pathlib,sys; "
                'content=pathlib.Path("tests/test_module.py.out").read_text(); '
                'sys.exit(0 if "OK" in content else 1)\''
            ),
            "rollback": None,
            "expected_files": ["tests/test_module.py.out"],
        }
    ]

    issues = PlannerService.find_immediate_repair_step_issues(plan)

    assert issues["fake_verification_artifact_steps"] == [1]


def test_w25_validator_rejects_fake_test_output_artifact_verification(tmp_path):
    plan = [
        {
            "step_number": 1,
            "description": "Run unit tests",
            "commands": ["python -m unittest discover -s tests"],
            "verification": (
                "python -c 'import pathlib,sys; "
                'content=pathlib.Path("tests/test_module.py.out").read_text(); '
                'sys.exit(0 if "OK" in content else 1)\''
            ),
            "rollback": None,
            "expected_files": ["tests/test_module.py.out"],
        }
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Create backend tests and run unittest",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert not verdict.accepted
    assert verdict.details["fake_verification_artifact_steps"] == [1]
    assert "invented test output artifacts" in " ".join(verdict.reasons)


def test_w25_validator_accepts_plain_unittest_exit_code(tmp_path):
    plan = [
        {
            "step_number": 1,
            "description": "Run unit tests",
            "commands": ["python -m unittest discover -s tests"],
            "verification": "python -m unittest discover -s tests",
            "rollback": None,
            "expected_files": [],
        }
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Run backend tests",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        title="Validation pass",
        description="Validate current backend tests",
    )

    assert verdict.accepted


def test_w25_rejects_inline_unittest_main_without_discovery(tmp_path):
    plan = [
        {
            "step_number": 1,
            "description": "Create backend test",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "tests/test_module.py",
                    "content": "def test_smoke():\n    assert True\n",
                }
            ],
            "verification": "python -c \"import unittest; unittest.main(argv=[''], exit=False)\"",
            "rollback": None,
            "expected_files": ["tests/test_module.py"],
        }
    ]

    issues = PlannerService.find_immediate_repair_step_issues(plan)
    verdict = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Create backend tests and run unittest",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        workflow_stage="backend",
    )

    assert issues.get("weak_verification_steps") == [1]
    assert not verdict.accepted
    assert "weak verification" in " ".join(verdict.reasons).lower()


def test_w25_shell_command_created_files_are_shared_writable(tmp_path):
    result = _execute_local_shell_commands_step(
        project_dir=tmp_path,
        commands=["mkdir -p build && printf '{}' > package.json"],
        verification_command="test -f package.json && test -d build",
    )

    assert result is not None
    assert result["status"] == "completed"
    assert "package.json" in result["files_changed"]
    assert (tmp_path / "package.json").stat().st_mode & 0o006 == 0o006
    assert (tmp_path / "build").stat().st_mode & 0o007 == 0o007


def test_w25_rejects_echo_that_does_not_create_expected_report(tmp_path):
    plan = [
        {
            "step_number": 1,
            "description": "Create the review report",
            "commands": ["echo 'Review report created'"],
            "verification": "cat review_report.txt",
            "rollback": None,
            "expected_files": ["review_report.txt"],
        }
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Create a review report",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        title="Review pass",
        description="Write docs/review.md with findings",
    )

    assert not verdict.accepted
    assert verdict.details["unmaterialized_expected_files"] == ["review_report.txt"]


def test_w25_accepts_write_file_review_report(tmp_path):
    plan = [
        {
            "step_number": 1,
            "description": "Create the review report",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "docs/review.md",
                    "content": "# Review Report\n\n## Findings\n\n- Checked.\n",
                }
            ],
            "verification": "python -c \"import pathlib,sys; sys.exit(0 if pathlib.Path('docs/review.md').exists() else 1)\"",
            "rollback": "rm -f docs/review.md",
            "expected_files": ["docs/review.md"],
        }
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Create a review report",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        title="Review pass",
        description="Write docs/review.md with findings",
    )

    assert verdict.accepted


def test_w25_prune_preserves_expected_files_used_by_pytest_verification():
    plan = [
        {
            "step_number": 1,
            "description": "Implement module",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "backend/module.py",
                    "content": "def create_item(): return True\n",
                }
            ],
            "verification": "python -m pytest tests/test_module.py -q",
            "rollback": None,
            "expected_files": ["backend/module.py", "tests/test_module.py"],
        }
    ]

    pruned, details = _prune_unmaterialized_expected_files(
        plan, ["tests/test_module.py"]
    )

    assert not details["changed"]
    assert pruned[0]["expected_files"] == ["backend/module.py", "tests/test_module.py"]
    assert details["preserved_referenced_expected_files"] == ["tests/test_module.py"]


def test_w25_prune_removes_unreferenced_speculative_expected_file():
    plan = [
        {
            "step_number": 1,
            "description": "Implement module",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "backend/module.py",
                    "content": "def create_item(): return True\n",
                }
            ],
            "verification": "python -m py_compile backend/module.py",
            "rollback": None,
            "expected_files": ["backend/module.py", "docs/notes.md"],
        }
    ]

    pruned, details = _prune_unmaterialized_expected_files(plan, ["docs/notes.md"])

    assert details["changed"]
    assert pruned[0]["expected_files"] == ["backend/module.py"]
    assert details["removed_expected_files"] == ["docs/notes.md"]


def test_w25_review_stage_allows_own_report_artifact(tmp_path):
    plan = [
        {
            "step_number": 1,
            "description": "Write review report",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "docs/review.md",
                    "content": "# Review Report\n\n## Findings\n\n- Checked.\n",
                }
            ],
            "verification": "python -c \"import pathlib,sys; sys.exit(0 if pathlib.Path('docs/review.md').is_file() else 1)\"",
            "rollback": "rm -f docs/review.md",
            "expected_files": ["docs/review.md"],
        }
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Review the current project and write docs/review.md",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        title="Review pass",
        description="Write docs/review.md",
        workflow_stage="review",
    )

    assert verdict.accepted


def test_w25_review_stage_still_rejects_source_mutation(tmp_path):
    plan = [
        {
            "step_number": 1,
            "description": "Change source during review",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "backend/module.py",
                    "content": "def create_item(): return True\n",
                }
            ],
            "verification": "python -m py_compile backend/module.py",
            "rollback": "rm -f backend/module.py",
            "expected_files": ["backend/module.py"],
        }
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Review the current project",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        title="Review pass",
        description="Do not modify source files",
        workflow_stage="review",
    )

    assert not verdict.accepted
    assert verdict.details["read_only_stage_mutation_steps"] == [1]
