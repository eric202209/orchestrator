import json

import pytest
from app.services.orchestration.planning.planner import PlannerService

from app.services.orchestration.validation.validator import ValidatorService

from app.services.orchestration.planning.source_materialization import (
    plan_has_concrete_source_materialization as _plan_has_concrete_source_materialization,
    repair_context_requires_source_materialization as _repair_context_requires_source_materialization,
)


def test_no_materialization_repair_prompt_requires_grounded_source_edits(tmp_path):
    (tmp_path / "src" / "medium_cli").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "medium_cli" / "__init__.py").write_text(
        "",
        encoding="utf-8",
    )
    (tmp_path / "src" / "medium_cli" / "cli.py").write_text(
        "def main(argv=None):\n    return 0\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_summary.py").write_text(
        "from medium_cli.cli import main\n"
        "\n"
        "def test_summary_command(capsys):\n"
        "    assert main(['summary']) == 0\n",
        encoding="utf-8",
    )

    prompt = PlannerService.build_planning_repair_prompt(
        "Add a summary command to the Python CLI.",
        malformed_output=json.dumps(
            [
                {
                    "step_number": 1,
                    "commands": ["cat tests/test_summary.py src/medium_cli/cli.py"],
                    "expected_files": [
                        "tests/test_summary.py",
                        "src/medium_cli/cli.py",
                    ],
                },
                {
                    "step_number": 2,
                    "description": "Implement summary command",
                    "commands": [],
                    "expected_files": ["src/medium_cli/cli.py"],
                },
            ]
        ),
        project_dir=tmp_path,
        rejection_reasons=[
            "Implementation task plan does not materialize any source changes",
        ],
    )

    assert "Grounded source-edit repair required:" in prompt
    assert "## PYTHON TEST SOURCE CONTEXT" in prompt
    assert "Preserve existing tests as the behavior contract" in prompt
    assert "Edit real source behavior using the provided test/source context" in prompt
    assert "Preserve existing Python package roots imported by tests" in prompt
    assert "Do not create a replacement src/<new_package> root" in prompt
    assert (
        "Prefer concrete ops for src/ files named by the test/source context" in prompt
    )
    assert "src/medium_cli/cli.py" in prompt
    assert "must include at least one concrete source edit operation" in prompt
    assert "Do not return inspect-only, verification-only, or test-only plans" in prompt
    assert "Do not fix implementation tasks by editing only tests" in prompt


def test_missing_materialization_context_requires_source_repair_only_for_implementation():
    assert _repair_context_requires_source_materialization(
        execution_profile="implementation",
        reason="plan_validation_failed",
        rejection_reasons=[
            "Implementation task plan does not materialize any source changes"
        ],
    )
    assert not _repair_context_requires_source_materialization(
        execution_profile="read_only",
        reason="plan_validation_failed",
        rejection_reasons=[
            "Implementation task plan does not materialize any source changes"
        ],
    )


def test_concrete_source_materialization_guard_rejects_test_only_ops(tmp_path):
    plan = [
        {
            "step_number": 1,
            "description": "Rewrite tests only",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "tests/test_summary.py",
                    "content": "def test_summary():\n    assert True\n",
                }
            ],
            "verification": "python3 -m pytest -q",
            "rollback": None,
            "expected_files": ["tests/test_summary.py"],
        }
    ]

    assert not _plan_has_concrete_source_materialization(plan, tmp_path)


def test_concrete_source_materialization_guard_accepts_source_write_file(tmp_path):
    plan = [
        {
            "step_number": 1,
            "description": "Edit source",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/medium_cli/cli.py",
                    "content": "def main(argv=None):\n    return 0\n",
                }
            ],
            "verification": "python3 -m pytest -q",
            "rollback": None,
            "expected_files": ["src/medium_cli/cli.py"],
        }
    ]

    assert _plan_has_concrete_source_materialization(plan, tmp_path)


def test_concrete_source_materialization_guard_accepts_normalized_nested_write_file(
    tmp_path,
):
    plan = [
        {
            "step_number": 1,
            "description": "Edit source",
            "commands": [],
            "ops": [
                {
                    "write_file": {
                        "path": "src/medium_cli/cli.py",
                        "content": "def main(argv=None):\n    return 0\n",
                    }
                }
            ],
            "verification": "python3 -m pytest -q",
            "rollback": None,
            "expected_files": ["src/medium_cli/cli.py"],
        }
    ]

    sanitized = PlannerService.sanitize_common_plan_issues(plan)

    assert sanitized[0]["ops"] == [
        {
            "op": "write_file",
            "path": "src/medium_cli/cli.py",
            "content": "def main(argv=None):\n    return 0\n",
        }
    ]
    assert _plan_has_concrete_source_materialization(sanitized, tmp_path)


def test_concrete_source_materialization_guard_accepts_normalized_o_alias_write_file(
    tmp_path,
):
    plan = [
        {
            "step_number": 1,
            "description": "Edit source",
            "commands": [],
            "ops": [
                {
                    "o": "write_file",
                    "path": "src/medium_cli/cli.py",
                    "content": "def main(argv=None):\n    return 0\n",
                }
            ],
            "verification": "python3 -m pytest -q",
            "rollback": None,
            "expected_files": ["src/medium_cli/cli.py"],
        }
    ]

    sanitized = PlannerService.sanitize_common_plan_issues(plan)

    assert sanitized[0]["ops"] == [
        {
            "op": "write_file",
            "path": "src/medium_cli/cli.py",
            "content": "def main(argv=None):\n    return 0\n",
        }
    ]
    assert _plan_has_concrete_source_materialization(sanitized, tmp_path)


def test_planning_repair_normalizes_top_level_write_file_into_ops(tmp_path):
    plan = [
        {
            "step_number": 1,
            "description": "Edit source",
            "commands": [],
            "verification": None,
            "rollback": None,
            "expected_files": ["src/small_cli/cli.py"],
            "write_file": {
                "path": "src/small_cli/cli.py",
                "content": "def main(argv=None):\n    return 0\n",
            },
        }
    ]

    sanitized = PlannerService.sanitize_common_plan_issues(plan)

    assert sanitized[0]["ops"] == [
        {
            "op": "write_file",
            "path": "src/small_cli/cli.py",
            "content": "def main(argv=None):\n    return 0\n",
        }
    ]
    assert _plan_has_concrete_source_materialization(sanitized, tmp_path)


def test_planning_repair_normalizes_top_level_append_and_replace_into_ops():
    plan = [
        {
            "step_number": 1,
            "description": "Edit files",
            "commands": [],
            "verification": None,
            "rollback": None,
            "expected_files": ["tests/test_cli.py", "src/small_cli/cli.py"],
            "append_file": {
                "path": "tests/test_cli.py",
                "content": "\ndef test_uppercase():\n    assert True\n",
            },
            "replace_in_file": {
                "path": "src/small_cli/cli.py",
                "old": "return message",
                "new": "return message.upper()",
            },
        }
    ]

    sanitized = PlannerService.sanitize_common_plan_issues(plan)

    assert sanitized[0]["ops"] == [
        {
            "op": "append_file",
            "path": "tests/test_cli.py",
            "content": "\ndef test_uppercase():\n    assert True\n",
        },
        {
            "op": "replace_in_file",
            "path": "src/small_cli/cli.py",
            "old": "return message",
            "new": "return message.upper()",
        },
    ]


def test_planning_repair_preserves_existing_valid_ops_when_normalizing_top_level_ops():
    plan = [
        {
            "step_number": 1,
            "description": "Edit source",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/existing.py",
                    "content": "VALUE = 1\n",
                }
            ],
            "write_file": {
                "path": "src/new_file.py",
                "content": "VALUE = 2\n",
            },
            "verification": None,
            "rollback": None,
            "expected_files": ["src/existing.py", "src/new_file.py"],
        }
    ]

    sanitized = PlannerService.sanitize_common_plan_issues(plan)

    assert sanitized[0]["ops"] == [
        {"op": "write_file", "path": "src/existing.py", "content": "VALUE = 1\n"},
        {"op": "write_file", "path": "src/new_file.py", "content": "VALUE = 2\n"},
    ]


def test_planning_repair_unknown_top_level_keys_are_not_converted():
    plan = [
        {
            "step_number": 1,
            "description": "Unknown operation",
            "commands": [],
            "verification": None,
            "rollback": None,
            "expected_files": [],
            "copy_file": {
                "path": "src/small_cli/cli.py",
                "content": "ignored",
            },
        }
    ]

    sanitized = PlannerService.sanitize_common_plan_issues(plan)

    assert "ops" not in sanitized[0]


def test_planning_repair_malformed_top_level_file_ops_are_ignored_safely():
    plan = [
        {
            "step_number": 1,
            "description": "Malformed operation",
            "commands": [],
            "verification": None,
            "rollback": None,
            "expected_files": ["src/small_cli/cli.py"],
            "write_file": {
                "path": "src/small_cli/cli.py",
            },
            "append_file": {
                "content": "missing path",
            },
            "replace_in_file": {
                "path": "src/small_cli/cli.py",
                "old": "return message",
            },
        }
    ]

    sanitized = PlannerService.sanitize_common_plan_issues(plan)

    assert "ops" not in sanitized[0]


def test_concrete_source_materialization_guard_accepts_source_replace_in_file(
    tmp_path,
):
    plan = [
        {
            "step_number": 1,
            "description": "Patch source",
            "commands": [],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "src/medium_cli/cli.py",
                    "old": "return 0",
                    "new": "return 1",
                }
            ],
            "verification": "python3 -m pytest -q",
            "rollback": None,
            "expected_files": ["src/medium_cli/cli.py"],
        }
    ]

    assert _plan_has_concrete_source_materialization(plan, tmp_path)


def test_concrete_source_materialization_guard_accepts_project_package_source(
    tmp_path,
):
    (tmp_path / "medium_cli").mkdir()
    (tmp_path / "medium_cli" / "__init__.py").write_text("", encoding="utf-8")
    plan = [
        {
            "step_number": 1,
            "description": "Patch package source",
            "commands": [],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "medium_cli/cli.py",
                    "old": "return 0",
                    "new": "return 1",
                }
            ],
            "verification": "python3 -m pytest -q",
            "rollback": None,
            "expected_files": ["medium_cli/cli.py"],
        }
    ]

    assert _plan_has_concrete_source_materialization(plan, tmp_path)


def test_no_materialization_repair_rejects_new_package_root_when_tests_import_existing(
    tmp_path,
):
    (tmp_path / "src" / "medium_cli").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "medium_cli" / "__init__.py").write_text(
        "",
        encoding="utf-8",
    )
    (tmp_path / "src" / "medium_cli" / "cli.py").write_text(
        "def main(argv=None):\n    return 0\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_summary.py").write_text(
        "from medium_cli.cli import main\n"
        "\n"
        "def test_summary_command():\n"
        "    assert main(['summary']) == 0\n",
        encoding="utf-8",
    )
    plan = [
        {
            "step_number": 1,
            "description": "Create implementation under a new package",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/task_cli/cli.py",
                    "content": "def main(argv=None):\n    return 0\n",
                },
                {
                    "op": "write_file",
                    "path": "tests/test_summary.py",
                    "content": (
                        "from task_cli.cli import main\n"
                        "\n"
                        "def test_summary_command():\n"
                        "    assert main(['summary']) == 0\n"
                    ),
                },
            ],
            "verification": "python3 -m pytest -q",
            "rollback": None,
            "expected_files": ["src/task_cli/cli.py", "tests/test_summary.py"],
        }
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Add a summary command to the Python CLI.",
        execution_profile="implementation",
        project_dir=tmp_path,
    )

    assert verdict.repairable
    assert any("changes package roots" in reason for reason in verdict.reasons)
    details = verdict.details["python_package_root_contract"]
    assert details["existing_package_roots"] == ["medium_cli"]
    assert details["introduced_package_roots"] == ["task_cli"]
    assert details["rewritten_test_import_roots"] == ["task_cli"]


def test_no_materialization_repair_accepts_existing_package_root_source_edit(tmp_path):
    (tmp_path / "src" / "medium_cli").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "medium_cli" / "__init__.py").write_text(
        "",
        encoding="utf-8",
    )
    (tmp_path / "src" / "medium_cli" / "cli.py").write_text(
        "def main(argv=None):\n    return 0\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_summary.py").write_text(
        "from medium_cli.cli import main\n"
        "\n"
        "def test_summary_command():\n"
        "    assert main(['summary']) == 0\n",
        encoding="utf-8",
    )
    plan = [
        {
            "step_number": 1,
            "description": "Edit existing package implementation",
            "commands": [],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "src/medium_cli/cli.py",
                    "old": "def main(argv=None):\n    return 0\n",
                    "new": "def main(argv=None):\n    return 0\n",
                }
            ],
            "verification": "python3 -m pytest -q",
            "rollback": None,
            "expected_files": ["src/medium_cli/cli.py"],
        }
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Add a summary command to the Python CLI.",
        execution_profile="implementation",
        project_dir=tmp_path,
    )

    assert "python_package_root_contract" not in verdict.details
    assert not any("changes package roots" in reason for reason in verdict.reasons)


def test_explicit_package_rename_bypasses_package_root_guard(tmp_path):
    (tmp_path / "src" / "medium_cli").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "medium_cli" / "__init__.py").write_text(
        "",
        encoding="utf-8",
    )
    (tmp_path / "src" / "medium_cli" / "cli.py").write_text(
        "def main(argv=None):\n    return 0\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_summary.py").write_text(
        "from medium_cli.cli import main\n"
        "\n"
        "def test_summary_command():\n"
        "    assert main(['summary']) == 0\n",
        encoding="utf-8",
    )
    plan = [
        {
            "step_number": 1,
            "description": "Rename the package root",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/task_cli/cli.py",
                    "content": "def main(argv=None):\n    return 0\n",
                },
                {
                    "op": "write_file",
                    "path": "tests/test_summary.py",
                    "content": "from task_cli.cli import main\n",
                },
            ],
            "verification": "python3 -m pytest -q",
            "rollback": None,
            "expected_files": ["src/task_cli/cli.py", "tests/test_summary.py"],
        }
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Rename the Python package from medium_cli to task_cli.",
        execution_profile="implementation",
        project_dir=tmp_path,
    )

    assert "python_package_root_contract" not in verdict.details
    assert not any("changes package roots" in reason for reason in verdict.reasons)


def test_package_root_guard_ignores_non_python_or_no_import_contract(tmp_path):
    (tmp_path / "src").mkdir()
    plan = [
        {
            "step_number": 1,
            "description": "Create a package in a project without Python tests",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/task_cli/cli.py",
                    "content": "def main(argv=None):\n    return 0\n",
                }
            ],
            "verification": "python3 -m py_compile src/task_cli/cli.py",
            "rollback": None,
            "expected_files": ["src/task_cli/cli.py"],
        }
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Add a small Python CLI.",
        execution_profile="implementation",
        project_dir=tmp_path,
    )

    assert "python_package_root_contract" not in verdict.details
    assert not any("changes package roots" in reason for reason in verdict.reasons)
