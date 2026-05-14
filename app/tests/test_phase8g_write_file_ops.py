from pathlib import Path

import pytest

from app.services.orchestration.execution.executor import ExecutorService
from app.services.orchestration.execution.step_support import (
    coerce_execution_step_result,
    step_needs_command_repair,
)
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.validation.validator import ValidatorService
from app.services.orchestration.operations.file_ops_contract import (
    normalize_replace_in_file_aliases,
    operation_has_file_op_path,
    validate_file_op_shape,
)
from app.services.orchestration.validation.placeholder_policy import (
    path_allows_placeholder_fixture_content,
)
from app.services.orchestration.validation.workspace_guard import (
    TaskOperationContractViolation,
    TaskWorkspaceViolationError,
    normalize_step,
)


def _ops_only_step(path: str = "src/main.ts") -> dict:
    return {
        "step_number": 1,
        "description": "Create a source file",
        "ops": [
            {
                "op": "write_file",
                "path": path,
                "content": "export const ok = true;\n",
            }
        ],
        "commands": [],
        "verification": (
            "node -e \"const fs=require('fs'); "
            "if(!fs.readFileSync('src/main.ts','utf8').includes('ok')) process.exit(1)\""
        ),
        "rollback": "rm -f src/main.ts",
        "expected_files": ["src/main.ts"],
    }


def test_plan_schema_accepts_ops_only_file_write_step():
    result = ValidatorService.validate_plan_schema([_ops_only_step()])

    assert result == {"valid": True, "errors": [], "details": {}}


def test_validate_plan_allows_empty_commands_when_write_file_ops_present(tmp_path):
    result = ValidatorService.validate_plan(
        [_ops_only_step()],
        output_text="[]",
        task_prompt="Create a source file",
        execution_profile="implementation",
        project_dir=tmp_path,
    )

    assert result.accepted
    assert "missing_commands_steps" not in result.details


def test_verification_plan_allows_expected_file_created_by_write_op(tmp_path):
    (tmp_path / "index.html").write_text(
        '<link rel="stylesheet" href="css/style.css">', encoding="utf-8"
    )
    (tmp_path / "css").mkdir()
    (tmp_path / "css" / "style.css").write_text("body {}", encoding="utf-8")
    plan = [
        {
            "step_number": 1,
            "description": "Create content-aware verification script",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "verify.js",
                    "content": (
                        "const fs=require('fs');"
                        "if(!fs.existsSync('index.html')) process.exit(1);"
                    ),
                }
            ],
            "verification": "node verify.js",
            "rollback": "rm -f verify.js",
            "expected_files": ["verify.js"],
        }
    ]

    result = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Improve static site verification commands",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert result.accepted
    assert "missing_workspace_expected_files" not in result.details


def test_verification_plan_rejects_missing_source_reads_in_commands(tmp_path):
    (tmp_path / "index.html").write_text(
        '<link rel="stylesheet" href="css/style.css">', encoding="utf-8"
    )
    (tmp_path / "css").mkdir()
    (tmp_path / "css" / "style.css").write_text("body {}", encoding="utf-8")
    plan = [
        {
            "step_number": 1,
            "description": "Verify static site assets",
            "commands": [
                "cat styles.css 2>/dev/null || echo missing",
                (
                    "node -e \"const fs=require('fs');"
                    "fs.readFileSync('styles.css','utf8')\""
                ),
            ],
            "verification": (
                "node -e \"const fs=require('fs');"
                "fs.readFileSync('styles.css','utf8')\""
            ),
            "rollback": None,
            "expected_files": [],
        }
    ]

    result = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Improve static site verification commands",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert result.repairable
    assert result.details["missing_workspace_expected_files"] == ["styles.css"]


def test_verification_plan_rejects_new_app_assets_created_to_satisfy_checks(tmp_path):
    (tmp_path / "index.html").write_text(
        '<link rel="stylesheet" href="css/style.css">', encoding="utf-8"
    )
    (tmp_path / "css").mkdir()
    (tmp_path / "css" / "style.css").write_text("body {}", encoding="utf-8")
    plan = [
        {
            "step_number": 1,
            "description": "Create a conventional stylesheet for verification",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "styles.css",
                    "content": "body { background: white; }",
                }
            ],
            "verification": (
                "node -e \"const fs=require('fs');"
                "fs.readFileSync('styles.css','utf8')\""
            ),
            "rollback": "rm -f styles.css",
            "expected_files": ["styles.css"],
        }
    ]

    result = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Improve static site verification commands",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert result.repairable
    assert result.details["verification_profile_created_source_assets"] == [
        "styles.css"
    ]


def test_verification_plan_rejects_mutating_existing_app_assets(tmp_path):
    (tmp_path / "index.html").write_text(
        '<link rel="stylesheet" href="css/style.css">', encoding="utf-8"
    )
    (tmp_path / "css").mkdir()
    (tmp_path / "css" / "style.css").write_text("body {}", encoding="utf-8")
    plan = [
        {
            "step_number": 1,
            "description": "Rewrite app assets before checking them",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "index.html",
                    "content": "<!doctype html><link rel='stylesheet' href='css/style.css'>",
                },
                {
                    "op": "replace_in_file",
                    "path": "css/style.css",
                    "old": "body {}",
                    "new": "body { background: white; }",
                },
            ],
            "verification": (
                "node -e \"const fs=require('fs');"
                "fs.readFileSync('css/style.css','utf8')\""
            ),
            "rollback": None,
            "expected_files": ["index.html", "css/style.css"],
        }
    ]

    result = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Upgrade landing page verification commands",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert result.repairable
    assert result.details["verification_profile_mutated_source_assets"] == [
        "index.html",
        "css/style.css",
    ]


def test_validate_plan_rejects_write_file_ops_outside_workspace(tmp_path):
    result = ValidatorService.validate_plan(
        [_ops_only_step("../outside.ts")],
        output_text="[]",
        task_prompt="Create a source file",
        execution_profile="implementation",
        project_dir=tmp_path,
    )

    assert result.rejected
    assert result.details["invalid_ops_path_steps"] == [1]
    assert any(
        "write_file operations must stay inside" in reason for reason in result.reasons
    )


def test_validate_plan_requires_replace_in_file_target_to_exist(tmp_path):
    (tmp_path / "index.html").write_text("<main>Microsite</main>", encoding="utf-8")
    plan = [
        {
            "step_number": 1,
            "description": "Patch stale React files",
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "src/App.jsx",
                    "old": "Board Game Cafe",
                    "new": "Board Game Cafe - Updated",
                }
            ],
            "commands": [],
            "verification": "node -e \"console.log('checked')\"",
            "rollback": None,
            "expected_files": ["src/App.jsx"],
        }
    ]

    result = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Update the existing static page",
        execution_profile="implementation",
        project_dir=tmp_path,
    )

    assert result.repairable
    assert result.details["missing_replace_in_file_targets"] == {1: ["src/App.jsx"]}


def test_validate_plan_allows_replace_target_created_by_prior_step(tmp_path):
    plan = [
        {
            "step_number": 1,
            "description": "Create file",
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/App.jsx",
                    "content": "export default function App(){return null}\\n",
                }
            ],
            "commands": [],
            "verification": "node -e \"console.log('created')\"",
            "rollback": None,
            "expected_files": ["src/App.jsx"],
        },
        {
            "step_number": 2,
            "description": "Patch file",
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "src/App.jsx",
                    "old": "null",
                    "new": "'ok'",
                }
            ],
            "commands": [],
            "verification": "node -e \"console.log('patched')\"",
            "rollback": None,
            "expected_files": ["src/App.jsx"],
        },
    ]

    result = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Create a React file",
        execution_profile="implementation",
        project_dir=tmp_path,
    )

    assert "missing_replace_in_file_targets" not in result.details


def test_normalize_step_normalizes_write_file_ops_and_rejects_escape(tmp_path):
    normalized = normalize_step(_ops_only_step("./src/main.ts"), tmp_path, None, 1)

    assert normalized["ops"] == [
        {
            "op": "write_file",
            "path": "src/main.ts",
            "content": "export const ok = true;\n",
        }
    ]

    with pytest.raises(TaskWorkspaceViolationError):
        normalize_step(_ops_only_step("../outside.ts"), tmp_path, None, 1)


def test_executor_write_file_ops_create_parent_directory(tmp_path):
    result = ExecutorService.execute_file_ops(
        Path(tmp_path),
        [
            {
                "op": "write_file",
                "path": "src/main.ts",
                "content": "export const ok = true;\n",
            }
        ],
    )

    assert result["success"] is True
    assert result["files_changed"] == ["src/main.ts"]
    assert (tmp_path / "src" / "main.ts").read_text(encoding="utf-8") == (
        "export const ok = true;\n"
    )


def test_phase8k_plan_schema_accepts_expanded_file_ops():
    step = _ops_only_step()
    step["ops"] = [
        {"op": "mkdir", "path": "src"},
        {"op": "append_file", "path": "README.md", "content": "\nUsage\n"},
        {"op": "replace_in_file", "path": "README.md", "old": "Usage", "new": "API"},
        {"op": "delete_file", "path": "tmp/output.txt"},
    ]

    result = ValidatorService.validate_plan_schema([step])

    assert result == {"valid": True, "errors": [], "details": {}}


def test_phase8m_plan_schema_allows_unknown_op_metadata():
    step = _ops_only_step()
    step["ops"] = [
        {"op": "mkdir", "path": "src", "content": "unexpected"},
    ]

    result = ValidatorService.validate_plan_schema([step])

    assert result == {"valid": True, "errors": [], "details": {}}


def test_phase8l_file_op_contract_strips_extra_keys():
    assert validate_file_op_shape({"op": "mkdir", "path": "src"}) is True
    assert (
        validate_file_op_shape({"op": "mkdir", "path": "src", "content": "unexpected"})
        is True
    )
    assert operation_has_file_op_path({"op": "delete_file", "path": "tmp/out.txt"})


def test_phase8m_replace_in_file_aliases_normalize_to_contract():
    assert normalize_replace_in_file_aliases(
        {
            "op": "replace_in_file",
            "path": "README.md",
            "search": "draft",
            "replace": "ready",
            "comment": "model metadata",
        }
    ) == {
        "op": "replace_in_file",
        "path": "README.md",
        "old": "draft",
        "new": "ready",
    }
    assert normalize_replace_in_file_aliases(
        {
            "op": "replace_in_file",
            "path": "README.md",
            "match": "draft",
            "replace": "ready",
        }
    ) == {
        "op": "replace_in_file",
        "path": "README.md",
        "old": "draft",
        "new": "ready",
    }
    assert normalize_replace_in_file_aliases(
        {
            "op": "replace_in_file",
            "path": "README.md",
            "pattern": "draft",
            "replacement": "ready",
        }
    ) == {
        "op": "replace_in_file",
        "path": "README.md",
        "old": "draft",
        "new": "ready",
    }
    assert normalize_replace_in_file_aliases(
        {
            "op": "replace_in_file",
            "path": "README.md",
            "old_string": "draft",
            "new_string": "ready",
        }
    ) == {
        "op": "replace_in_file",
        "path": "README.md",
        "old": "draft",
        "new": "ready",
    }
    assert normalize_replace_in_file_aliases(
        {
            "op": "replace_in_file",
            "path": "README.md",
            "old_str": "draft",
            "new_str": "ready",
        }
    ) == {
        "op": "replace_in_file",
        "path": "README.md",
        "old": "draft",
        "new": "ready",
    }


def test_phase8m_replace_in_file_conflicting_aliases_do_not_normalize():
    assert (
        validate_file_op_shape(
            {
                "op": "replace_in_file",
                "path": "README.md",
                "old": "draft",
                "search": "alpha",
                "new": "ready",
            }
        )
        is False
    )


def test_validate_step_success_counts_absent_delete_file_target_as_materialized(
    tmp_path,
):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_config.py").write_text(
        "def test_ok():\n    assert True\n",
        encoding="utf-8",
    )
    step = {
        "step_number": 1,
        "description": "Delete scratch file",
        "ops": [{"op": "delete_file", "path": "scratch/remove-me.txt"}],
        "commands": ["python -m pytest tests/test_config.py -v"],
        "verification": "python -m pytest tests/test_config.py -v",
        "expected_files": ["tests/test_config.py"],
    }

    verdict = ValidatorService.validate_step_success(
        project_dir=tmp_path,
        step=step,
        step_output="delete_file scratch/remove-me.txt\n2 passed",
        missing_expected_files=[],
        tool_failures=[],
        validation_profile="implementation",
        reported_changed_files=["scratch/remove-me.txt"],
    )

    assert not any("none materialized" in reason for reason in verdict.reasons)


def test_validate_step_success_still_flags_absent_non_delete_reported_file(
    tmp_path,
):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_config.py").write_text(
        "def test_ok():\n    assert True\n",
        encoding="utf-8",
    )
    step = {
        "step_number": 1,
        "description": "Report a missing source file",
        "ops": [],
        "commands": ["python -m pytest tests/test_config.py -v"],
        "verification": "python -m pytest tests/test_config.py -v",
        "expected_files": ["tests/test_config.py"],
    }

    verdict = ValidatorService.validate_step_success(
        project_dir=tmp_path,
        step=step,
        step_output="reported source change",
        missing_expected_files=[],
        tool_failures=[],
        validation_profile="implementation",
        reported_changed_files=["src/app.py"],
    )

    assert any("none materialized" in reason for reason in verdict.reasons)


def test_phase8l_placeholder_fixture_policy_is_shared():
    assert path_allows_placeholder_fixture_content("fixtures/sample.md") is True
    assert path_allows_placeholder_fixture_content("src/app.py") is False


def test_phase8k_validate_plan_rejects_expanded_ops_outside_workspace(tmp_path):
    step = _ops_only_step()
    step["ops"] = [{"op": "mkdir", "path": "../outside"}]

    result = ValidatorService.validate_plan(
        [step],
        output_text="[]",
        task_prompt="Create a source directory",
        execution_profile="implementation",
        project_dir=tmp_path,
    )

    assert result.rejected
    assert result.details["invalid_ops_path_steps"] == [1]
    assert any(
        "file operations must stay inside" in reason for reason in result.reasons
    )


def test_phase8k_normalize_step_preserves_expanded_ops(tmp_path):
    step = _ops_only_step()
    step["ops"] = [
        {"op": "mkdir", "path": "./src"},
        {"op": "append_file", "path": "./README.md", "content": "\nUsage\n"},
        {"op": "replace_in_file", "path": "./README.md", "old": "Usage", "new": "API"},
        {"op": "delete_file", "path": "./tmp/output.txt"},
    ]

    normalized = normalize_step(step, tmp_path, None, 1)

    assert normalized["ops"] == [
        {"op": "mkdir", "path": "src"},
        {"op": "append_file", "path": "README.md", "content": "\nUsage\n"},
        {"op": "replace_in_file", "path": "README.md", "old": "Usage", "new": "API"},
        {"op": "delete_file", "path": "tmp/output.txt"},
    ]


def test_phase8m_normalize_step_accepts_replace_aliases_and_strips_metadata(tmp_path):
    step = _ops_only_step()
    step["ops"] = [
        {
            "op": "replace_in_file",
            "path": "./README.md",
            "search": "draft",
            "replace": "ready",
            "comment": "extra model note",
        },
    ]

    normalized = normalize_step(step, tmp_path, None, 1)

    assert normalized["ops"] == [
        {"op": "replace_in_file", "path": "README.md", "old": "draft", "new": "ready"}
    ]


def test_phase8m_normalize_step_rejects_conflicting_replace_aliases(tmp_path):
    step = _ops_only_step()
    step["ops"] = [
        {
            "op": "replace_in_file",
            "path": "./README.md",
            "old": "draft",
            "search": "alpha",
            "new": "ready",
        },
    ]

    with pytest.raises(TaskOperationContractViolation):
        normalize_step(step, tmp_path, None, 1)


def test_phase8o_contract_violation_reports_raw_op_keys(tmp_path):
    step = _ops_only_step()
    step["ops"] = [
        {
            "op": "replace_in_file",
            "path": "./README.md",
            "from_text": "draft",
            "to_text": "ready",
        },
    ]

    with pytest.raises(TaskOperationContractViolation) as exc_info:
        normalize_step(step, tmp_path, None, 1)

    message = str(exc_info.value)
    assert "must contain keys" in message
    assert "got raw keys" in message
    assert "from_text" in message
    assert "to_text" in message


def test_phase8k_executor_runs_file_ops_in_order_and_reports_changed_files(tmp_path):
    (tmp_path / "README.md").write_text("Title\n", encoding="utf-8")
    tmp_dir = tmp_path / "tmp"
    tmp_dir.mkdir()
    (tmp_dir / "output.txt").write_text("stale\n", encoding="utf-8")

    result = ExecutorService.execute_file_ops(
        Path(tmp_path),
        [
            {"op": "mkdir", "path": "src"},
            {"op": "append_file", "path": "README.md", "content": "\nUsage\n"},
            {
                "op": "replace_in_file",
                "path": "README.md",
                "old": "Usage",
                "new": "API",
            },
            {"op": "write_file", "path": "src/main.py", "content": "print('ok')\n"},
            {"op": "delete_file", "path": "tmp/output.txt"},
        ],
    )

    assert result["success"] is True
    assert result["files_changed"] == [
        "README.md",
        "README.md",
        "src/main.py",
        "tmp/output.txt",
    ]
    assert (tmp_path / "src").is_dir()
    assert (tmp_path / "README.md").read_text(encoding="utf-8") == "Title\n\nAPI\n"
    assert (tmp_path / "src" / "main.py").read_text(encoding="utf-8") == "print('ok')\n"
    assert not (tmp_path / "tmp" / "output.txt").exists()


def test_phase8k_append_file_requires_existing_parent_directory(tmp_path):
    result = ExecutorService.execute_file_ops(
        Path(tmp_path),
        [{"op": "append_file", "path": "missing/README.md", "content": "Usage\n"}],
    )

    assert result["success"] is False
    assert result["files_changed"] == []
    assert "parent directory does not exist" in result["output"]


def test_phase8o_delete_file_accepts_missing_and_rejects_directory_targets(tmp_path):
    missing = ExecutorService.execute_file_ops(
        Path(tmp_path),
        [{"op": "delete_file", "path": "missing.txt"}],
    )
    assert missing["success"] is True
    assert missing["files_changed"] == []
    assert "already absent" in missing["output"]

    (tmp_path / "src").mkdir()
    directory = ExecutorService.execute_file_ops(
        Path(tmp_path),
        [{"op": "delete_file", "path": "src"}],
    )
    assert directory["success"] is False
    assert "target is not a file" in directory["output"]


def test_phase8o_delete_file_is_idempotent_when_already_absent(tmp_path):
    target = tmp_path / "scratch" / "remove-me.txt"
    target.parent.mkdir()
    target.write_text("remove me\n", encoding="utf-8")
    operation = {"op": "delete_file", "path": "scratch/remove-me.txt"}

    first = ExecutorService.execute_file_ops(Path(tmp_path), [operation])
    second = ExecutorService.execute_file_ops(Path(tmp_path), [operation])

    assert first["success"] is True
    assert first["files_changed"] == ["scratch/remove-me.txt"]
    assert second["success"] is True
    assert second["files_changed"] == []
    assert "already absent" in second["output"]
    assert not target.exists()


def test_phase8k_replace_in_file_requires_exactly_one_old_text(tmp_path):
    target = tmp_path / "README.md"
    target.write_text("same\nsame\n", encoding="utf-8")

    ambiguous = ExecutorService.execute_file_ops(
        Path(tmp_path),
        [{"op": "replace_in_file", "path": "README.md", "old": "same", "new": "done"}],
    )
    assert ambiguous["success"] is False
    assert "ambiguous" in ambiguous["output"]
    assert target.read_text(encoding="utf-8") == "same\nsame\n"

    target.write_text("before\n", encoding="utf-8")
    missing = ExecutorService.execute_file_ops(
        Path(tmp_path),
        [
            {
                "op": "replace_in_file",
                "path": "README.md",
                "old": "absent",
                "new": "done",
            }
        ],
    )
    assert missing["success"] is False
    assert "not found" in missing["output"]


def test_phase8o_replace_in_file_is_idempotent_when_already_applied(tmp_path):
    target = tmp_path / "app_config.py"
    target.write_text("DEBUG = False\n", encoding="utf-8")
    operation = {
        "op": "replace_in_file",
        "path": "app_config.py",
        "old": "DEBUG = False",
        "new": "DEBUG = True",
    }

    first = ExecutorService.execute_file_ops(Path(tmp_path), [operation])
    second = ExecutorService.execute_file_ops(Path(tmp_path), [operation])

    assert first["success"] is True
    assert first["files_changed"] == ["app_config.py"]
    assert second["success"] is True
    assert second["files_changed"] == []
    assert "already applied" in second["output"]
    assert target.read_text(encoding="utf-8") == "DEBUG = True\n"


def test_phase8o_replace_in_file_still_fails_when_target_state_is_unproven(tmp_path):
    target = tmp_path / "app_config.py"
    target.write_text("DEBUG = None\n", encoding="utf-8")

    missing_new = ExecutorService.execute_file_ops(
        Path(tmp_path),
        [
            {
                "op": "replace_in_file",
                "path": "app_config.py",
                "old": "DEBUG = False",
                "new": "DEBUG = True",
            }
        ],
    )
    assert missing_new["success"] is False
    assert "old text not found" in missing_new["output"]


def test_phase8u_replace_in_file_uses_regex_fallback_for_pattern_alias(tmp_path):
    target = tmp_path / "app_config.py"
    target.write_text("FEATURE_FLAG = False\n", encoding="utf-8")

    result = ExecutorService.execute_file_ops(
        Path(tmp_path),
        [
            {
                "op": "replace_in_file",
                "path": "app_config.py",
                "old": r"FEATURE_FLAG\s*=\s*False",
                "new": "FEATURE_FLAG = True",
            }
        ],
    )

    assert result["success"] is True
    assert result["files_changed"] == ["app_config.py"]
    assert "regex replacement" in result["output"]
    assert target.read_text(encoding="utf-8") == "FEATURE_FLAG = True\n"


def test_phase8u_replace_in_file_regex_fallback_rejects_ambiguous_matches(tmp_path):
    target = tmp_path / "app_config.py"
    target.write_text("FEATURE_FLAG = False\nFEATURE_FLAG=False\n", encoding="utf-8")

    result = ExecutorService.execute_file_ops(
        Path(tmp_path),
        [
            {
                "op": "replace_in_file",
                "path": "app_config.py",
                "old": r"FEATURE_FLAG\s*=\s*False",
                "new": "FEATURE_FLAG = True",
            }
        ],
    )

    assert result["success"] is False
    assert "regex old text is ambiguous" in result["output"]

    target.write_text("DEBUG = True\nOTHER_DEBUG = True\n", encoding="utf-8")
    ambiguous_new = ExecutorService.execute_file_ops(
        Path(tmp_path),
        [
            {
                "op": "replace_in_file",
                "path": "app_config.py",
                "old": "DEBUG = False",
                "new": "DEBUG = True",
            }
        ],
    )
    assert ambiguous_new["success"] is False
    assert "new text is ambiguous" in ambiguous_new["output"]


def test_phase8k_executor_rejects_unsupported_op(tmp_path):
    result = ExecutorService.execute_file_ops(
        Path(tmp_path),
        [{"op": "run_command", "path": "src", "command": "echo no"}],
    )

    assert result["success"] is False
    assert "unsupported op" in result["output"]


def test_ops_only_step_does_not_need_command_repair():
    assert step_needs_command_repair(_ops_only_step()) is False


def test_plan_sanitizer_preserves_write_file_ops():
    sanitized = PlannerService.sanitize_common_plan_issues([_ops_only_step()])

    assert sanitized[0]["commands"] == []
    assert sanitized[0]["ops"] == _ops_only_step()["ops"]


def test_ops_only_step_with_null_verification_does_not_trigger_weak_repair():
    step = _ops_only_step()
    step["verification"] = None

    issues = PlannerService.find_immediate_repair_step_issues([step])

    assert "weak_verification_steps" not in issues


def test_write_file_step_result_bypasses_model_output_json_recovery():
    result = {
        "status": "completed",
        "output": "write_file csv_summary.py (1420 chars)",
        "verification_output": "",
        "files_changed": ["csv_summary.py"],
    }

    coerced = coerce_execution_step_result(
        result,
        expected_files=["csv_summary.py"],
        extract_structured_text=str,
    )

    assert coerced == result
