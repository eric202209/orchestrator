from datetime import datetime, timezone

from app.services.orchestration.execution.execution_flow import assess_step_execution
from app.services.orchestration.execution.executor import ExecutorService
from app.services.orchestration.planning.normalization import (
    complete_repaired_plan_contract,
    normalize_existing_file_target_plan,
    normalize_stale_replace_ops_to_small_file_writes,
)
from app.services.orchestration.task_rules import get_workflow_profile
from app.services.orchestration.validation.validator import ValidatorService


def _step(
    description: str,
    commands: list[str],
    *,
    verification: str,
    expected_files: list[str] | None = None,
) -> dict:
    return {
        "step_number": 1,
        "description": description,
        "commands": commands,
        "verification": verification,
        "rollback": None,
        "expected_files": expected_files or [],
    }


def test_backend_only_small_fix_does_not_require_scaffold_phases(tmp_path):
    plan = [
        _step(
            "Fix notes API status code regression",
            ["python -m pytest app/tests/test_notes_api.py -q"],
            verification="python -m pytest app/tests/test_notes_api.py -q",
            expected_files=[],
        )
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Fix a small FastAPI bug. Do not scaffold or verify startup.",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        title="Fix FastAPI notes status code",
        description="Patch one backend bug and run the focused regression test.",
        workflow_profile="backend_only",
    )

    assert "missing_workflow_phases" not in verdict.details
    assert not any("required workflow phase" in reason for reason in verdict.reasons)


def test_frontend_only_small_module_does_not_require_scaffold_phases(tmp_path):
    plan = [
        _step(
            "Update existing frontend utility",
            ["pnpm test -- filterDate"],
            verification="pnpm test -- filterDate",
            expected_files=[],
        )
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt=(
            "Update one existing React/Vite frontend utility. "
            "Do not scaffold, start a dev server, or run a build."
        ),
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        title="Update frontend date filter",
        description="Small module change only, with targeted test verification.",
        workflow_profile="frontend_only",
    )

    assert "missing_workflow_phases" not in verdict.details
    assert not any("required workflow phase" in reason for reason in verdict.reasons)


def test_negated_dev_server_does_not_infer_fullstack_scaffold_profile():
    profile = get_workflow_profile(
        "full_lifecycle",
        "Phase10R frontend small module corrected rerun",
        (
            "Update the existing ES-module frontend utility src/formatStatus.js. "
            "Do not scaffold React, Vite, or package files, do not create "
            "index.html, do not run a dev server, and do not create a static site."
        ),
    )

    assert profile == "frontend_only"


def test_fullstack_scaffold_still_warns_when_required_phase_missing(tmp_path):
    plan = [
        _step(
            "Create backend API route",
            ["mkdir -p app && touch app/main.py"],
            verification="test -f app/main.py",
            expected_files=["app/main.py"],
        )
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Set up frontend React and backend FastAPI with clean architecture.",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        title="Set up fullstack app",
        description="Create frontend and backend scaffold.",
        workflow_profile="fullstack_scaffold",
    )

    assert "missing_workflow_phases" in verdict.details
    assert "create_frontend_skeleton" in verdict.details["missing_workflow_phases"]


def test_review_only_expected_files_are_not_source_requirements(tmp_path):
    plan = [
        _step(
            "Inspect current project structure",
            ["ls -la"],
            verification="",
            expected_files=["package.json", "README.md"],
        )
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Review the current project. Do not create or modify files.",
        execution_profile="review_only",
        project_dir=tmp_path,
        title="Review project structure",
        description="Read-only inspection.",
        workflow_stage="review",
    )

    assert verdict.accepted is True
    assert "missing_workspace_expected_files" not in verdict.details
    assert "unmaterialized_expected_files" not in verdict.details


def test_validation_only_expected_files_are_not_source_requirements(tmp_path):
    plan = [
        _step(
            "Validate requested project facts",
            ["python -c \"print('checked')\""],
            verification="python -c \"print('checked')\"",
            expected_files=["package.json", "README.md"],
        )
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Validate current project state only. Do not modify files.",
        execution_profile="test_only",
        project_dir=tmp_path,
        title="Validate project state",
        description="Validation-only inspection.",
        workflow_stage="validate",
    )

    assert verdict.accepted is True
    assert "missing_workspace_expected_files" not in verdict.details
    assert "unmaterialized_expected_files" not in verdict.details


def test_validation_stage_alias_is_read_only(tmp_path):
    plan = [
        _step(
            "Validate requested fixture",
            ["python -m unittest discover -s tests"],
            verification="python -m unittest discover -s tests",
            expected_files=["src/validator_fixture.py"],
        )
    ]
    (tmp_path / "src").mkdir()
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "validator_fixture.py").write_text(
        "def is_even(value): return value % 2 == 0\n",
        encoding="utf-8",
    )

    verdict = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Validate the existing fixture only. Do not edit files.",
        execution_profile="test_only",
        project_dir=tmp_path,
        title="Validate fixture",
        description="Validation-only inspection.",
        workflow_stage="validation",
    )

    assert verdict.accepted is True
    assert "unmaterialized_expected_files" not in verdict.details
    assert "missing_verification_steps" not in verdict.details


def test_read_only_review_rejects_missing_workspace_probe(tmp_path):
    plan = [
        _step(
            "Check for unit tests",
            ["ls tests/"],
            verification="ls tests/",
            expected_files=[],
        )
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Review the codebase only. Do not edit files.",
        execution_profile="review_only",
        project_dir=tmp_path,
        title="Review fixture",
        description="Review-only inspection.",
        workflow_stage="review",
    )

    assert verdict.accepted is False
    assert "tests" in verdict.details["missing_workspace_expected_files"]


def test_review_only_rejects_failable_grep_probe(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "handler.py").write_text(
        "def handle(value):\n    return value.strip().lower()\n",
        encoding="utf-8",
    )
    plan = [
        _step(
            "Check whether handler has exception handling",
            ["grep -E 'try|except|finally|raise' src/handler.py"],
            verification="grep -E 'try|except|finally|raise' src/handler.py",
            expected_files=[],
        )
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Review the codebase only. Do not edit files.",
        execution_profile="review_only",
        project_dir=tmp_path,
        title="Review fixture",
        description="Review-only inspection.",
        workflow_stage="review",
    )

    assert verdict.accepted is False
    assert verdict.details["read_only_stage_failable_probe_steps"] == [1]


def test_existing_file_target_normalization_maps_missing_basename_to_unique_path(
    tmp_path,
):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "calculator.py").write_text(
        "def total(cents):\n    return cents\n",
        encoding="utf-8",
    )
    plan = [
        {
            "step_number": 1,
            "description": "Patch calculator.py",
            "commands": [],
            "verification": "python -m pytest tests/test_calculator.py -q",
            "rollback": None,
            "expected_files": ["calculator.py"],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "calculator.py",
                    "old": "return cents",
                    "new": "return cents / 100",
                }
            ],
        }
    ]

    normalized, details = normalize_existing_file_target_plan(
        plan,
        project_dir=tmp_path,
    )

    assert details["changed"] is True
    assert details["rewritten_paths"] == {"calculator.py": "app/calculator.py"}
    assert normalized[0]["ops"][0]["path"] == "app/calculator.py"
    assert normalized[0]["expected_files"] == ["app/calculator.py"]


def test_existing_file_target_normalization_maps_nested_root_drift_to_src_path(
    tmp_path,
):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "formatStatus.js").write_text(
        "export function formatStatus(value) { return value; }\n",
        encoding="utf-8",
    )
    plan = [
        {
            "step_number": 1,
            "description": "Update frontend/src/formatStatus.js",
            "commands": ["node -e \"import('./frontend/src/formatStatus.js')\""],
            "verification": "node -e \"import('./frontend/src/formatStatus.js')\"",
            "rollback": None,
            "expected_files": ["frontend/src/formatStatus.js"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "frontend/src/formatStatus.js",
                    "content": "export function formatStatus(value) { return String(value); }\n",
                }
            ],
        }
    ]

    normalized, details = normalize_existing_file_target_plan(
        plan,
        project_dir=tmp_path,
    )

    assert details["changed"] is True
    assert details["rewritten_paths"] == {
        "frontend/src/formatStatus.js": "src/formatStatus.js"
    }
    assert normalized[0]["ops"][0]["path"] == "src/formatStatus.js"
    assert normalized[0]["expected_files"] == ["src/formatStatus.js"]
    assert "frontend/src/formatStatus.js" not in normalized[0]["verification"]
    assert "src/formatStatus.js" in normalized[0]["verification"]


def test_existing_file_target_normalization_ignores_ambiguous_matches(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / "lib").mkdir()
    (tmp_path / "app" / "config.py").write_text("DEBUG = False\n", encoding="utf-8")
    (tmp_path / "lib" / "config.py").write_text("DEBUG = False\n", encoding="utf-8")
    plan = [
        {
            "step_number": 1,
            "description": "Patch config.py",
            "commands": [],
            "verification": "",
            "rollback": None,
            "expected_files": ["config.py"],
            "ops": [{"op": "write_file", "path": "config.py", "content": ""}],
        }
    ]

    normalized, details = normalize_existing_file_target_plan(
        plan,
        project_dir=tmp_path,
    )

    assert details["changed"] is False
    assert normalized[0]["ops"][0]["path"] == "config.py"


def test_frontend_python_content_probe_does_not_create_stack_conflict(tmp_path):
    plan = [
        _step(
            "Inspect current workspace",
            ["ls"],
            verification="test -f package.json && test -f src/formatStatus.js",
            expected_files=["package.json"],
        ),
        {
            "step_number": 2,
            "description": "Update formatStatus to return uppercase status text",
            "commands": [
                'python -c \'import sys; sys.exit(0 if "toUpperCase()" in open("src/formatStatus.js").read() else 1)\''
            ],
            "verification": 'python -c \'import sys; sys.exit(0 if "toUpperCase()" in open("src/formatStatus.js").read() else 1)\'',
            "rollback": None,
            "expected_files": ["src/formatStatus.js"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/formatStatus.js",
                    "content": "export function formatStatus(status) { return status.toUpperCase(); }\n",
                }
            ],
        },
        _step(
            "Run tests",
            ["npm test"],
            verification="npm test",
            expected_files=[],
        ),
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt=(
            "Update the existing formatStatus frontend utility. "
            "Do not scaffold React, Vite, or a static site."
        ),
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        title="Frontend utility",
        description="Small JavaScript utility change.",
        workflow_profile="frontend_only",
    )

    assert "stack_conflict" not in verdict.details
    assert not any("inconsistent implementation stacks" in r for r in verdict.reasons)


def test_static_site_contract_completion_does_not_treat_js_as_static_site():
    plan = [
        {
            "step_number": 1,
            "description": "Update JavaScript utility",
            "commands": [],
            "verification": "",
            "rollback": None,
            "expected_files": ["src/formatStatus.js"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/formatStatus.js",
                    "content": "export function formatStatus(status) { return status.toUpperCase(); }\n",
                }
            ],
        }
    ]

    normalized, details = complete_repaired_plan_contract(
        plan,
        task_prompt="Update one existing frontend utility.",
        repaired=True,
    )

    assert details["changed"] is False
    assert normalized == plan


def test_stale_replace_fallback_converts_small_python_function_to_write_file(
    tmp_path,
):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "calculator.py").write_text(
        "def cents_to_dollars(cents):\n    return cents\n",
        encoding="utf-8",
    )
    plan = [
        {
            "step_number": 1,
            "description": "Fix calculator conversion",
            "commands": [],
            "verification": "python -m pytest tests/test_calculator.py -q",
            "rollback": None,
            "expected_files": ["app/calculator.py"],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "app/calculator.py",
                    "old": "def cents_to_dollars(cents):\n    return cents / 100\n",
                    "new": "def cents_to_dollars(cents):\n    return cents / 100\n",
                }
            ],
        }
    ]

    normalized, details = normalize_stale_replace_ops_to_small_file_writes(
        plan,
        project_dir=tmp_path,
    )

    assert details["changed"] is True
    assert details["converted_paths"] == ["app/calculator.py"]
    assert normalized[0]["ops"][0]["op"] == "write_file"
    assert normalized[0]["ops"][0]["path"] == "app/calculator.py"


def test_stale_replace_fallback_converts_small_js_function_to_write_file(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "formatStatus.js").write_text(
        "export function formatStatus(value) {\n  return String(value);\n}\n",
        encoding="utf-8",
    )
    plan = [
        {
            "step_number": 1,
            "description": "Fix status formatting",
            "commands": [],
            "verification": "npm test",
            "rollback": None,
            "expected_files": ["src/formatStatus.js"],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "src/formatStatus.js",
                    "old": "export function formatStatus(status) { return status; }",
                    "new": "export function formatStatus(value) {\n  return String(value).toUpperCase();\n}\n",
                }
            ],
        }
    ]

    normalized, details = normalize_stale_replace_ops_to_small_file_writes(
        plan,
        project_dir=tmp_path,
    )

    assert details["changed"] is True
    assert details["converted_paths"] == ["src/formatStatus.js"]
    assert normalized[0]["ops"][0]["op"] == "write_file"


def test_stale_replace_fallback_does_not_convert_large_or_unrelated_file(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "formatStatus.js").write_text(
        "export function formatStatus(value) {\n  return String(value);\n}\n",
        encoding="utf-8",
    )
    plan = [
        {
            "step_number": 1,
            "description": "Patch unrelated helper",
            "commands": [],
            "verification": "",
            "rollback": None,
            "expected_files": ["src/formatStatus.js"],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "src/formatStatus.js",
                    "old": "missing",
                    "new": "export function otherHelper(value) {\n  return value;\n}\n",
                }
            ],
        }
    ]

    normalized, details = normalize_stale_replace_ops_to_small_file_writes(
        plan,
        project_dir=tmp_path,
    )

    assert details["changed"] is False
    assert normalized[0]["ops"][0]["op"] == "replace_in_file"


def test_stale_replace_fallback_can_synthesize_single_return_change(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "formatStatus.js").write_text(
        "export function formatStatus(value) {\n  return String(value);\n}\n",
        encoding="utf-8",
    )
    plan = [
        {
            "step_number": 1,
            "description": "Fix return expression",
            "commands": [],
            "verification": "npm test",
            "rollback": None,
            "expected_files": ["src/formatStatus.js"],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "src/formatStatus.js",
                    "old": "return status;",
                    "new": "return String(value).toUpperCase();",
                }
            ],
        }
    ]

    normalized, details = normalize_stale_replace_ops_to_small_file_writes(
        plan,
        project_dir=tmp_path,
    )

    assert details["changed"] is True
    assert normalized[0]["ops"][0]["op"] == "write_file"
    assert "return String(value).toUpperCase();" in normalized[0]["ops"][0]["content"]


def test_ops_only_step_ignores_unrelated_tool_failure_logs(tmp_path, monkeypatch):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "calculator.py").write_text(
        "def cents_to_dollars(cents):\n    return float(cents / 100)\n",
        encoding="utf-8",
    )

    monkeypatch.setattr(
        ExecutorService,
        "recent_step_tool_failures",
        staticmethod(lambda *args, **kwargs: ["replace_in_file old text not found"]),
    )

    assessment = assess_step_execution(
        db=None,
        session_id=1,
        task_id=1,
        project_dir=tmp_path,
        step={
            "step_number": 1,
            "description": "Update calculator",
            "commands": [],
            "verification": "cat app/calculator.py",
            "expected_files": ["app/calculator.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "app/calculator.py",
                    "content": "def cents_to_dollars(cents):\n    return float(cents / 100)\n",
                }
            ],
        },
        step_result={
            "status": "completed",
            "output": "",
            "files_changed": ["app/calculator.py"],
        },
        step_started_at=datetime.now(timezone.utc),
        validation_profile="implementation",
    )

    assert assessment.step_status == "success"
    assert assessment.tool_failures == []


def test_npm_install_source_file_is_non_runnable_for_frontend_module(tmp_path):
    plan = [
        _step(
            "Install source file",
            ["npm install ./src/formatStatus.js"],
            verification="npm test",
            expected_files=[],
        )
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Update one existing frontend utility. Do not scaffold.",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        title="Frontend utility",
        description="Small JavaScript utility change.",
        workflow_profile="frontend_only",
    )

    assert verdict.accepted is False
    assert verdict.details["non_runnable_steps"] == [1]


def test_source_file_mv_is_non_runnable_for_backend_bugfix(tmp_path):
    plan = [
        _step(
            "Move generated calculator module",
            ["mv ./app/calculator.py ./calculator/calculator.py"],
            verification="python -m pytest tests/test_calculator.py -q",
            expected_files=["calculator/calculator.py"],
        )
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Fix one existing Python backend module. Do not reorganize files.",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        title="Backend bug fix",
        description="Patch the calculator conversion in place.",
        workflow_profile="backend_only",
    )

    assert verdict.accepted is False
    assert verdict.details["non_runnable_steps"] == [1]


def test_implementation_plan_without_materialization_is_repair_required(tmp_path):
    plan = [
        _step(
            "Modify cents_to_dollars to return a numeric value",
            ["python -c 'import app.calculator'"],
            verification=(
                "python -c 'from app.calculator import cents_to_dollars; "
                "assert cents_to_dollars(100) == 1.0'"
            ),
            expected_files=[],
        )
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt=(
            "Fix the existing calculator module so cents_to_dollars returns "
            "dollars as a numeric value."
        ),
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        title="Backend bug fix",
        description="Patch one backend module.",
        workflow_profile="backend_only",
    )

    assert verdict.accepted is False
    assert verdict.details["missing_materialization_for_implementation"] is True


def test_frontend_only_rejects_extensionless_python_materialization(tmp_path):
    plan = [
        {
            "step_number": 1,
            "description": "Update formatStatus",
            "commands": [],
            "verification": "npm test",
            "rollback": None,
            "expected_files": ["formatStatus"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "formatStatus",
                    "content": "def format_status(status):\n    return status.upper()\n",
                }
            ],
        }
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Update the existing formatStatus frontend utility.",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        title="Frontend utility",
        description="Small JavaScript utility change.",
        workflow_profile="frontend_only",
    )

    assert verdict.accepted is False
    assert verdict.details["frontend_wrong_stack_materializations"] == ["formatStatus"]


def test_frontend_write_rejects_obvious_undefined_return_identifier(tmp_path):
    plan = [
        {
            "step_number": 1,
            "description": "Update formatStatus",
            "commands": [],
            "verification": "npm test",
            "rollback": None,
            "expected_files": ["src/formatStatus.js"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/formatStatus.js",
                    "content": (
                        "export function formatStatus(value) {\n"
                        "  return status.toUpperCase();\n"
                        "}\n"
                    ),
                }
            ],
        }
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Update the existing formatStatus frontend utility.",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
        title="Frontend utility",
        description="Small JavaScript utility change.",
        workflow_profile="frontend_only",
    )

    assert verdict.accepted is False
    assert verdict.details["undefined_js_identifier_materializations"] == [
        "src/formatStatus.js"
    ]
