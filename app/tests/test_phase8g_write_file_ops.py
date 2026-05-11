from pathlib import Path

import pytest

from app.services.orchestration.execution.executor import ExecutorService
from app.services.orchestration.execution.step_support import step_needs_command_repair
from app.services.orchestration.validation.validator import ValidatorService
from app.services.orchestration.validation.workspace_guard import (
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


def test_ops_only_step_does_not_need_command_repair():
    assert step_needs_command_repair(_ops_only_step()) is False
