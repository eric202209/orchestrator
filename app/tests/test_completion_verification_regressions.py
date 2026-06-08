from __future__ import annotations

import json
import logging
import os
import shlex
from types import SimpleNamespace

import pytest

from app.models import (
    LogEntry,
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskExecutionChangeSet,
    TaskStatus,
)
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.state.persistence import read_orchestration_events
from app.services.orchestration.phases.completion_flow import (
    _augment_completion_verification_command,
    _classify_completion_verification_failure,
    _detect_completion_verification_command,
    _execute_completion_verification,
    _scope_workspace_consistency_to_task_changes,
    finalize_successful_task,
)
from app.services.orchestration.execution.runtime import workspace_snapshot_key
from app.services.orchestration.execution.execution_flow import (
    execute_verification_command,
    patch_python_verification_imports,
)
from app.services.orchestration.types import OrchestrationRunContext, ValidationVerdict
from app.services.orchestration.validation.validator import ValidatorService
from app.services.prompt_templates import OrchestrationState, StepResult
from app.services.task_service import TaskService


class _FakeRuntime:
    async def execute_task(self, prompt, timeout_seconds=None):
        return {"output": "Task summary"}

    def get_backend_metadata(self):
        return {"backend": "fake", "model_family": "test"}


class _FailingSummaryRuntime:
    async def execute_task(self, prompt, timeout_seconds=None):
        raise TimeoutError("summary timed out")

    def get_backend_metadata(self):
        return {"backend": "fake", "model_family": "test"}


class _NeedsReviewEvaluatorRuntime:
    async def execute_task(self, prompt, timeout_seconds=None):
        if "independent QA evaluator" in prompt:
            return {
                "output": (
                    "SCORES: goal=1/3 regressions=1/2 quality=1/2 files=1/3\n"
                    "TOTAL: 4/10\n"
                    "VERDICT: NEEDS_REVIEW\n"
                    "NOTES: incomplete task coverage"
                )
            }
        return {"output": "Task summary"}

    def get_backend_metadata(self):
        return {"backend": "fake", "model_family": "test"}


class _FakeTaskService:
    def analyze_workspace_consistency(self, project_dir):
        return {}


class _CountingTaskService(_FakeTaskService):
    def __init__(self):
        self.analyze_calls = 0

    def analyze_workspace_consistency(self, project_dir):
        self.analyze_calls += 1
        return {"calls": self.analyze_calls}


def test_completion_mixed_stack_check_ignores_preexisting_unrelated_stack():
    scoped = _scope_workspace_consistency_to_task_changes(
        {
            "mixed_stack": True,
            "python_files": ["backend/module.py", "tests/test_module.py"],
            "node_files": ["frontend/src/App.tsx"],
        },
        plan=[
            {
                "expected_files": ["backend/module.py", "tests/test_module.py"],
                "ops": [{"op": "write_file", "path": "backend/module.py"}],
            }
        ],
        reported_changed_files=["backend/module.py", "tests/test_module.py"],
    )

    assert scoped["mixed_stack"] is False
    assert scoped["workspace_mixed_stack"] is True
    assert scoped["task_scoped_stack"] == "python"


def test_completion_mixed_stack_check_keeps_task_level_mixed_stack():
    scoped = _scope_workspace_consistency_to_task_changes(
        {"mixed_stack": True},
        plan=[
            {
                "expected_files": ["backend/module.py", "frontend/src/App.tsx"],
                "ops": [],
            }
        ],
        reported_changed_files=["backend/module.py", "frontend/src/App.tsx"],
    )

    assert scoped["mixed_stack"] is True


def test_review_report_artifact_materialization_is_accepted(tmp_path):
    backend_dir = tmp_path / "backend"
    tests_dir = tmp_path / "tests"
    docs_dir = tmp_path / "docs"
    backend_dir.mkdir()
    tests_dir.mkdir()
    docs_dir.mkdir()
    (backend_dir / "module.py").write_text("def get_items():\n    return []\n")
    (tests_dir / "test_module.py").write_text("def test_smoke():\n    assert True\n")
    (docs_dir / "review.md").write_text("# Review\n\nNo blockers found.\n")

    verdict = ValidatorService.validate_task_completion(
        project_dir=tmp_path,
        plan=[
            {
                "step_number": 1,
                "ops": [{"op": "write_file", "path": "docs/review.md"}],
                "expected_files": ["docs/review.md"],
                "verification": "python -c \"from pathlib import Path; assert Path('docs/review.md').exists()\"",
            }
        ],
        task_prompt="Review backend/module.py and tests/test_module.py and write docs/review.md.",
        execution_profile="validation",
        title="Workspace Review pass",
        description="Review the project artifacts and record findings.",
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": ["docs/review.md"],
        },
        workflow_stage="review",
    )

    assert (
        "Completion evidence reported changed files, but none materialized in the canonical workspace"
        not in verdict.reasons
    )
    assert verdict.details["materialized_reported_files"] == ["docs/review.md"]
    assert verdict.accepted


def test_python_verification_imports_adds_sys_without_backend_path():
    command = (
        'python -c "import module, sys; '
        'sys.exit(0 if callable(module.get_items) else 1)"'
    )

    patched = patch_python_verification_imports(command)

    assert "import sys" in patched
    assert "sys.path.append" not in patched
    assert "backend" not in patched
    assert "import module" in patched


def test_python_inline_verification_env_resolves_src_layout_package(tmp_path):
    (tmp_path / "pyproject.toml").write_text(
        '[tool.pytest.ini_options]\npythonpath = ["src"]\n',
        encoding="utf-8",
    )
    package_dir = tmp_path / "src" / "pkg"
    package_dir.mkdir(parents=True)
    (package_dir / "__init__.py").write_text("VALUE = 42\n", encoding="utf-8")

    result = execute_verification_command(
        project_dir=tmp_path,
        command='python -c "from pkg import VALUE; raise SystemExit(0 if VALUE == 42 else 1)"',
    )

    assert result["success"] is True
    assert result["returncode"] == 0


def test_python_verification_imports_leave_stdlib_checks_alone():
    command = (
        'python -c "import pathlib, sys; '
        "sys.exit(0 if pathlib.Path('tests/test_module.py').exists() else 1)\""
    )

    assert patch_python_verification_imports(command) == command


def test_python_verification_imports_leave_unittest_checks_alone():
    command = "python -c \"import unittest; unittest.main(argv=[''], exit=False)\""

    assert patch_python_verification_imports(command) == command


def test_missing_jest_binary_is_treated_as_repairable_completion_verification():
    completion_validation = SimpleNamespace(
        profile="implementation",
        details={"expected_core_files": ["src/index.ts", "src/utils/format.test.ts"]},
    )

    verdict = _classify_completion_verification_failure(
        command="pnpm test",
        source="package.json test script via pnpm",
        verification_output=(
            "> demo@1.0.0 test /workspace/demo\n" "> jest\n" "sh: 1: jest: not found\n"
        ),
        completion_validation=completion_validation,
    )

    assert verdict is not None
    assert verdict.repairable is True
    assert verdict.stage == "completion_verification"
    assert "dependencies are missing or not installed" in verdict.reasons[0]
    assert verdict.details["verification_command"] == "pnpm test"
    assert (
        verdict.details["completion_repair_source"] == "final_completion_verification"
    )
    assert verdict.details["failure_class"] == "missing_dependency"
    assert "src/utils/format.test.ts" in verdict.details["expected_core_files"]


def test_python_no_module_named_is_repairable_completion_verification():
    completion_validation = SimpleNamespace(
        profile="implementation",
        details={"expected_core_files": ["calc_smoke.py", "tests/test_calc.py"]},
    )

    verdict = _classify_completion_verification_failure(
        command="pytest",
        source="python test suite detected",
        verification_output=(
            "ModuleNotFoundError: No module named 'calc_smoke'\n"
            "ERROR tests/test_calc.py"
        ),
        completion_validation=completion_validation,
    )

    assert verdict is not None
    assert verdict.repairable is True
    assert verdict.stage == "completion_verification"
    assert "repairable test/module issue" in verdict.reasons[0]
    assert verdict.details["verification_command"] == "pytest"
    assert (
        verdict.details["completion_repair_source"] == "final_completion_verification"
    )
    assert verdict.details["failure_class"] == "module_not_found"


def test_python_modulenotfounderror_prefix_is_repairable_completion_verification():
    completion_validation = SimpleNamespace(
        profile="implementation",
        details={"expected_core_files": ["calc_smoke.py"]},
    )

    verdict = _classify_completion_verification_failure(
        command="pytest",
        source="python test suite detected",
        verification_output="ModuleNotFoundError while importing test module",
        completion_validation=completion_validation,
    )

    assert verdict is not None
    assert verdict.repairable is True


def test_real_test_failure_is_not_reclassified_as_missing_dependency():
    completion_validation = SimpleNamespace(
        profile="implementation",
        details={"expected_core_files": ["src/index.ts"]},
    )

    verdict = _classify_completion_verification_failure(
        command="pnpm test",
        source="package.json test script via pnpm",
        verification_output=(
            "FAIL src/index.test.ts\n" "Expected: 2\n" "Received: 1\n"
        ),
        completion_validation=completion_validation,
    )

    assert verdict is None


def test_vitest_completion_verification_excludes_openclaw_snapshots():
    command = _augment_completion_verification_command(
        "pnpm test",
        "vitest run",
    )

    assert command == "pnpm test -- --exclude=.agent/**"


def test_jest_completion_verification_excludes_openclaw_snapshots():
    command = _augment_completion_verification_command(
        "pnpm test",
        "node --runInBand jest",
    )

    assert command == "pnpm test -- --testPathIgnorePatterns=.agent/"


def test_python_completion_verification_detects_python_module_pytest(tmp_path):
    project_dir = tmp_path / "project"
    (project_dir / "tests").mkdir(parents=True)
    (project_dir / "tests" / "test_config.py").write_text(
        "def test_ok():\n    assert True\n",
        encoding="utf-8",
    )

    command, source = _detect_completion_verification_command(project_dir)

    assert command.endswith(" -m pytest")
    assert source == "python test suite detected"


def test_python_completion_verification_prefers_project_venv(tmp_path):
    project_dir = tmp_path / "project"
    (project_dir / "tests").mkdir(parents=True)
    (project_dir / "venv" / "bin").mkdir(parents=True)
    python_bin = project_dir / "venv" / "bin" / "python"
    python_bin.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    python_bin.chmod(python_bin.stat().st_mode | 0o111)

    command, source = _detect_completion_verification_command(project_dir)

    assert command == f"{shlex.quote(str(python_bin))} -m pytest"
    assert source == "python test suite detected"


def test_python_module_pytest_completion_verification_imports_workspace_root(
    tmp_path,
):
    project_dir = tmp_path / "project"
    (project_dir / "tests").mkdir(parents=True)
    (project_dir / "app_config.py").write_text(
        "FEATURE_FLAG = True\n",
        encoding="utf-8",
    )
    (project_dir / "tests" / "test_config.py").write_text(
        "import app_config\n\n"
        "def test_feature_flag_is_true():\n"
        "    assert app_config.FEATURE_FLAG is True\n",
        encoding="utf-8",
    )
    command, _ = _detect_completion_verification_command(project_dir)

    result = _execute_completion_verification(
        project_dir=project_dir,
        command=command,
        timeout_seconds=10,
    )

    assert result["success"] is True


def test_completion_verification_honors_pytest_ini_src_pythonpath(tmp_path):
    project_dir = tmp_path / "project"
    package_dir = project_dir / "src" / "notes_app"
    tests_dir = project_dir / "tests"
    package_dir.mkdir(parents=True)
    tests_dir.mkdir()
    (project_dir / "pytest.ini").write_text(
        "[pytest]\npythonpath = src\n",
        encoding="utf-8",
    )
    (package_dir / "__init__.py").write_text("", encoding="utf-8")
    (package_dir / "greetings.py").write_text(
        "def greeting(name):\n    return f'Hello, {name}!'\n",
        encoding="utf-8",
    )
    (tests_dir / "test_greetings.py").write_text(
        "from notes_app.greetings import greeting\n\n"
        "def test_greeting():\n"
        "    assert greeting('Ada') == 'Hello, Ada!'\n",
        encoding="utf-8",
    )
    command, source = _detect_completion_verification_command(project_dir)

    result = _execute_completion_verification(
        project_dir=project_dir,
        command=command or "",
        timeout_seconds=10,
    )

    assert command is not None
    assert source == "python test suite detected"
    assert result["success"] is True


def test_completion_validation_requires_source_path_named_by_task(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "smoke_status.py").write_text(
        'print("Reliability Smoke 2: Ready Verify")\n',
        encoding="utf-8",
    )

    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=[
            {
                "step_number": 1,
                "description": "Create smoke status script",
                "ops": [
                    {
                        "op": "write_file",
                        "path": "smoke_status.py",
                        "content": 'print("Reliability Smoke 2: Ready Verify")\n',
                    }
                ],
                "commands": [],
                "verification": "python -m py_compile smoke_status.py",
                "expected_files": ["smoke_status.py"],
            }
        ],
        task_prompt=(
            "Create deterministic Python script scripts/smoke_status.py that "
            "prints exactly Reliability Smoke 2: Ready."
        ),
        execution_profile="full_lifecycle",
        workspace_consistency={},
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": ["smoke_status.py"],
        },
    )

    assert verdict.accepted is False
    assert "scripts/smoke_status.py" in verdict.details["missing_core_files"]


@pytest.mark.skipif(os.name == "nt", reason="uses a POSIX shebang executable")
def test_completion_verification_executes_project_venv_python(tmp_path):
    project_dir = tmp_path / "project"
    (project_dir / "tests").mkdir(parents=True)
    (project_dir / "venv" / "bin").mkdir(parents=True)
    marker = project_dir / "used-venv-python"
    python_bin = project_dir / "venv" / "bin" / "python"
    python_bin.write_text(
        "#!/bin/sh\n" f"touch {marker}\n" "exit 0\n",
        encoding="utf-8",
    )
    python_bin.chmod(python_bin.stat().st_mode | 0o111)

    command, _ = _detect_completion_verification_command(project_dir)
    result = _execute_completion_verification(
        project_dir=project_dir,
        command=command,
        timeout_seconds=10,
    )

    assert result["success"] is True
    assert marker.exists()


def test_completion_verification_rejects_shell_metacharacters(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    result = _execute_completion_verification(
        project_dir=project_dir,
        command="pytest; echo pwned",
        timeout_seconds=1,
    )

    assert result["success"] is False
    assert "unsafe shell metacharacters" in result["output"]


def test_module_resolution_failure_is_treated_as_repairable_verification_issue():
    completion_validation = SimpleNamespace(
        profile="implementation",
        details={
            "expected_core_files": ["src/utils/format.ts", "src/utils/format.spec.ts"]
        },
    )

    verdict = _classify_completion_verification_failure(
        command="pnpm test -- --exclude=.agent/**",
        source="package.json test script via pnpm",
        verification_output=(
            "FAIL src/utils/format.spec.ts\n"
            "Error: Failed to load url ./format.js in src/utils/format.spec.ts. "
            "Does the file exist?\n"
        ),
        completion_validation=completion_validation,
    )

    assert verdict is not None
    assert verdict.repairable is True
    assert "repairable test/module issue" in verdict.reasons[0]


def test_verification_completion_does_not_require_execution_results(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "test").mkdir()
    (project_dir / "test" / "replay.spec.ts").write_text(
        "export const ok = true;\n",
        encoding="utf-8",
    )

    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=[
            {
                "step_number": 1,
                "description": "Inspect replay coverage",
                "commands": ["ls test"],
                "verification": "test -f test/replay.spec.ts",
                "expected_files": ["test/replay.spec.ts"],
            }
        ],
        task_prompt="Review the project and verify replay stability.",
        execution_profile="review_only",
        workspace_consistency={},
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 0,
        },
    )

    assert verdict.accepted is True
    assert (
        "Completion contract requires at least one recorded execution result"
        not in verdict.reasons
    )


def test_completion_validation_accepts_readme_package_mutation_without_source(
    tmp_path,
):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "package.json").write_text(
        '{\n  "name": "demo",\n  "version": "0.2.0"\n}\n',
        encoding="utf-8",
    )
    (project_dir / "README.md").write_text(
        "# Demo\n\nStatus: ready\n\n## Changelog\n",
        encoding="utf-8",
    )

    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=[
            {
                "step_number": 1,
                "description": "Update package metadata and README status",
                "ops": [
                    {
                        "op": "replace_in_file",
                        "path": "package.json",
                        "old": '"version": "0.1.0"',
                        "new": '"version": "0.2.0"',
                    },
                    {
                        "op": "append_file",
                        "path": "README.md",
                        "content": "\n## Changelog\n",
                    },
                ],
                "commands": [],
                "verification": "test -f README.md",
                "expected_files": ["package.json", "README.md"],
            }
        ],
        task_prompt="Update package.json version and append README changelog.",
        execution_profile="full_lifecycle",
        workspace_consistency={},
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": ["package.json", "README.md"],
        },
    )

    assert verdict.accepted is True
    assert "No core implementation source files were produced" not in verdict.reasons
    assert verdict.details["completion_contract"]["validation_profile"] == "mutation"
    assert verdict.details["completion_contract"]["requires_source_outputs"] is False
    assert verdict.details["mutation_completion"]["materialized_files"] == [
        "package.json",
        "README.md",
    ]


def test_completion_validation_accepts_docs_mutation_without_source(tmp_path):
    project_dir = tmp_path / "project"
    (project_dir / "docs" / "archive").mkdir(parents=True)
    (project_dir / "docs" / "index.md").write_text(
        "# Docs\n\nLifecycle: stable\n\n## Links\n",
        encoding="utf-8",
    )
    (project_dir / "docs" / "archive" / "README.md").write_text(
        "# Archive\n",
        encoding="utf-8",
    )

    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=[
            {
                "step_number": 1,
                "description": "Update docs lifecycle and archive docs",
                "ops": [
                    {
                        "op": "replace_in_file",
                        "path": "docs/index.md",
                        "old": "alpha",
                        "new": "stable",
                    },
                    {
                        "op": "write_file",
                        "path": "docs/archive/README.md",
                        "content": "# Archive\n",
                    },
                    {"op": "delete_file", "path": "docs/draft.md"},
                ],
                "commands": [],
                "verification": "test -f docs/archive/README.md",
                "expected_files": ["docs/index.md", "docs/archive/README.md"],
            }
        ],
        task_prompt="Replace docs lifecycle marker and create docs archive README.",
        execution_profile="full_lifecycle",
        workspace_consistency={},
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": [
                "docs/index.md",
                "docs/archive/README.md",
                "docs/draft.md (deleted)",
            ],
        },
    )

    assert verdict.accepted is True
    assert "No core implementation source files were produced" not in verdict.reasons
    assert verdict.details["completion_contract"]["validation_profile"] == "mutation"
    assert verdict.details["mutation_completion"]["matched_reported_files"] == [
        "docs/index.md",
        "docs/archive/README.md",
    ]


def test_artifact_only_bootstrap_completion_skips_source_file_obligation(tmp_path):
    project_dir = tmp_path / "project"
    (project_dir / "reports").mkdir(parents=True)
    (project_dir / "reports" / "status.md").write_text(
        "# Phase 12T Status\n\n"
        "## Findings\n"
        "- Artifact-only evidence is present.\n\n"
        "## Recommendations\n"
        "- Ready for continuation.\n",
        encoding="utf-8",
    )

    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=[
            {
                "step_number": 1,
                "description": "Create report artifact",
                "ops": [
                    {
                        "op": "write_file",
                        "path": "reports/status.md",
                        "content": (
                            "# Phase 12T Status\n\n"
                            "## Findings\n"
                            "- Artifact-only evidence is present.\n\n"
                            "## Recommendations\n"
                            "- Ready for continuation.\n"
                        ),
                    }
                ],
                "commands": [],
                "verification": (
                    'python -c "from pathlib import Path; '
                    "text=Path('reports/status.md').read_text(); "
                    "assert 'Ready for continuation' in text\""
                ),
                "expected_files": ["reports/status.md"],
            }
        ],
        task_prompt=(
            "Create a status report artifact. This is an artifact-only "
            "deliverable; do not create source code."
        ),
        execution_profile="full_lifecycle",
        workspace_consistency={},
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": ["reports/status.md"],
        },
    )

    assert verdict.accepted is True
    assert "No core implementation source files were produced" not in verdict.reasons
    assert (
        "Workspace contains only framework/config scaffolding without any implementation source files"
        not in verdict.reasons
    )
    assert (
        verdict.details["completion_contract"]["bootstrap_task_type"] == "ARTIFACT_ONLY"
    )
    assert verdict.details["completion_contract"]["artifact_only_completion"] is True


def test_artifact_only_bootstrap_completion_still_rejects_placeholder_artifact(
    tmp_path,
):
    project_dir = tmp_path / "project"
    (project_dir / "reports").mkdir(parents=True)
    (project_dir / "reports" / "status.md").write_text(
        "TODO placeholder\n",
        encoding="utf-8",
    )

    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=[
            {
                "step_number": 1,
                "description": "Create report artifact",
                "ops": [
                    {
                        "op": "write_file",
                        "path": "reports/status.md",
                        "content": "TODO placeholder\n",
                    }
                ],
                "commands": [],
                "verification": "test -s reports/status.md",
                "expected_files": ["reports/status.md"],
            }
        ],
        task_prompt=(
            "Create a status report artifact. This is an artifact-only "
            "deliverable; do not create source code."
        ),
        execution_profile="full_lifecycle",
        workspace_consistency={},
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": ["reports/status.md"],
        },
    )

    assert verdict.accepted is False
    assert verdict.details["completion_contract"]["artifact_only_completion"] is True
    assert "Artifact completion lacks substantive artifact evidence" in verdict.reasons


def test_mixed_bootstrap_completion_keeps_source_file_obligation(tmp_path):
    project_dir = tmp_path / "project"
    (project_dir / "reports").mkdir(parents=True)
    (project_dir / "reports" / "summary.txt").write_text(
        "phase12t summary\n",
        encoding="utf-8",
    )

    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=[
            {
                "step_number": 1,
                "description": "Create mixed deliverable",
                "ops": [
                    {
                        "op": "write_file",
                        "path": "reports/summary.txt",
                        "content": "phase12t summary\n",
                    }
                ],
                "commands": [],
                "verification": "test -s reports/summary.txt",
                "expected_files": ["reports/summary.txt"],
            }
        ],
        task_prompt="Implement a script and create a report artifact.",
        execution_profile="full_lifecycle",
        workspace_consistency={},
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": ["reports/summary.txt"],
        },
    )

    assert verdict.accepted is False
    assert verdict.details["completion_contract"]["bootstrap_task_type"] == "MIXED"
    assert verdict.details["completion_contract"]["artifact_only_completion"] is False
    assert "No core implementation source files were produced" in verdict.reasons


def test_completion_validation_still_rejects_code_task_with_only_package_json(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "package.json").write_text(
        '{"scripts": {"test": "echo missing"}}\n',
        encoding="utf-8",
    )

    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=[
            {
                "step_number": 1,
                "description": "Create app scaffold",
                "ops": [
                    {
                        "op": "write_file",
                        "path": "package.json",
                        "content": '{"scripts": {"test": "echo missing"}}\n',
                    }
                ],
                "commands": [],
                "verification": "test -f package.json",
                "expected_files": ["package.json"],
            }
        ],
        task_prompt="Build a React app with source implementation.",
        execution_profile="full_lifecycle",
        workspace_consistency={},
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": ["package.json"],
        },
    )

    assert verdict.accepted is False
    assert "No core implementation source files were produced" in verdict.reasons


def test_completion_validation_does_not_treat_generic_update_as_mutation_task(
    tmp_path,
):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "README.md").write_text(
        "# Notes\n\nUpdated docs only.\n",
        encoding="utf-8",
    )

    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=[
            {
                "step_number": 1,
                "description": "Update README only",
                "ops": [
                    {
                        "op": "append_file",
                        "path": "README.md",
                        "content": "\nUpdated docs only.\n",
                    }
                ],
                "commands": [],
                "verification": "test -f README.md",
                "expected_files": ["README.md"],
            }
        ],
        task_prompt="Update the React app to add feature X.",
        execution_profile="full_lifecycle",
        workspace_consistency={},
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": ["README.md"],
        },
    )

    assert verdict.accepted is False
    assert "No core implementation source files were produced" in verdict.reasons
    assert verdict.details["mutation_completion"]["mutation_task"] is False


def test_validation_profile_infers_mutation_before_node_implementation_marker():
    profile = ValidatorService.infer_validation_profile(
        task_prompt=(
            "Update package.json and README.md only. In package.json keep version "
            "1.1.0 and add scripts.test. Verify with node -e. Do not create app "
            "source files."
        ),
        execution_profile="full_lifecycle",
        title="Phase 9D package docs mutation",
        description="Metadata/docs-only package update",
    )

    assert profile == "mutation"


def test_validation_profile_keeps_source_implementation_for_app_builds():
    profile = ValidatorService.infer_validation_profile(
        task_prompt="Build a React app and update package.json scripts.",
        execution_profile="full_lifecycle",
        title="React app implementation",
        description="Create application source implementation",
    )

    assert profile == "implementation"


def test_workspace_consistency_ignores_virtualenv_vendor_javascript(tmp_path):
    project_dir = tmp_path / "project"
    (project_dir / "tests").mkdir(parents=True)
    (project_dir / ".venv" / "lib" / "python3.12" / "site-packages" / "urllib3").mkdir(
        parents=True
    )
    (project_dir / "app.py").write_text("print('ok')\n", encoding="utf-8")
    (project_dir / "tests" / "test_app.py").write_text(
        "def test_ok():\n    assert True\n",
        encoding="utf-8",
    )
    (
        project_dir
        / ".venv"
        / "lib"
        / "python3.12"
        / "site-packages"
        / "urllib3"
        / "emscripten_fetch_worker.js"
    ).write_text("self.onmessage = () => {};\n", encoding="utf-8")

    consistency = TaskService(None).analyze_workspace_consistency(project_dir)

    assert consistency["dominant_stack"] == "python"
    assert consistency["mixed_stack"] is False
    assert consistency["node_source_count"] == 0
    assert consistency["node_files"] == []


def test_completion_validation_rejects_reported_files_that_never_materialized(tmp_path):
    project_dir = tmp_path / "project"
    (project_dir / "src").mkdir(parents=True)
    (project_dir / "src" / "index.ts").write_text(
        "export const ready = true;\n",
        encoding="utf-8",
    )

    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=[
            {
                "step_number": 1,
                "description": "Create source implementation",
                "commands": ["echo ready"],
                "verification": "test -f src/index.ts",
                "expected_files": ["src/index.ts"],
            }
        ],
        task_prompt="Implement the source file.",
        execution_profile="full_lifecycle",
        workspace_consistency={},
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": ["README.md"],
        },
    )

    assert verdict.accepted is False
    assert verdict.repairable is True
    assert "none materialized in the canonical workspace" in verdict.reasons[0]
    assert verdict.details["reported_changed_files"] == ["README.md"]


def test_completion_validation_placeholder_pass_remains_rejected(tmp_path):
    project_dir = tmp_path / "project"
    (project_dir / "services").mkdir(parents=True)
    (project_dir / "services" / "health.py").write_text(
        "class ServiceStatus:\n    pass\n",
        encoding="utf-8",
    )

    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=[
            {
                "step_number": 1,
                "description": "Create health service",
                "commands": [
                    "printf 'class ServiceStatus:\\n    pass\\n' > services/health.py"
                ],
                "verification": "python3 -m py_compile services/health.py",
                "expected_files": ["services/health.py"],
            }
        ],
        task_prompt="Build a distributed workflow health checker.",
        execution_profile="full_lifecycle",
        workspace_consistency={},
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": ["services/health.py"],
        },
    )

    assert verdict.status == "rejected"
    assert verdict.rejected is True
    assert "placeholder_only_implementation" not in verdict.details
    assert verdict.details["placeholder_reasons"] == [
        "health.py still contains `pass` placeholders"
    ]


def test_detect_placeholder_content_flags_broken_python_main_guard(tmp_path):
    entrypoint = tmp_path / "app.py"
    entrypoint.write_text(
        'if __name__ == __main__:\n    print("broken")\n',
        encoding="utf-8",
    )

    reasons = ValidatorService._detect_placeholder_content(entrypoint)

    assert any(
        "broken Python __main__ entrypoint check" in reason for reason in reasons
    )


def test_detect_placeholder_content_accepts_single_quoted_python_main_guard(tmp_path):
    entrypoint = tmp_path / "app.py"
    entrypoint.write_text(
        "if __name__ == '__main__':\n    print('ok')\n",
        encoding="utf-8",
    )

    reasons = ValidatorService._detect_placeholder_content(entrypoint)

    assert not any(
        "broken Python __main__ entrypoint check" in reason for reason in reasons
    )


def test_detect_placeholder_content_allows_fixture_todo_markers(tmp_path):
    fixture = tmp_path / "fixtures" / "sample.md"
    fixture.parent.mkdir()
    fixture.write_text(
        "# Sample\nTODO: Add intro\nFIXME: Broken link\n",
        encoding="utf-8",
    )

    reasons = ValidatorService._detect_placeholder_content(fixture)

    assert reasons == []


def test_detect_placeholder_content_allows_todo_report_literals_and_except_pass(
    tmp_path,
):
    report = tmp_path / "todo_report.py"
    report.write_text(
        "MARKERS = ['TODO', 'FIXME']\n"
        "try:\n"
        "    value = 1\n"
        "except OSError:\n"
        "    pass\n",
        encoding="utf-8",
    )

    reasons = ValidatorService._detect_placeholder_content(report)

    assert reasons == []


def test_detect_placeholder_content_still_flags_stub_python_pass(tmp_path):
    service = tmp_path / "health.py"
    service.write_text("class ServiceStatus:\n    pass\n", encoding="utf-8")

    reasons = ValidatorService._detect_placeholder_content(service)

    assert reasons == ["health.py still contains `pass` placeholders"]


def _seed_finalize_ctx(db_session, tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    project = Project(name="Phase 7J", workspace_path=str(project_dir))
    db_session.add(project)
    db_session.flush()
    session = SessionModel(
        project_id=project.id,
        name="Phase 7J Session",
        status="running",
        is_active=True,
        execution_mode="manual",
    )
    task = Task(
        project_id=project.id,
        title="Phase 7J Task",
        status=TaskStatus.RUNNING,
        task_subfolder=None,
    )
    db_session.add_all([session, task])
    db_session.flush()
    link = SessionTask(
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.RUNNING,
    )
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
    )
    db_session.add_all([link, execution])
    db_session.commit()

    state = OrchestrationState(
        session_id=str(session.id),
        task_description="Fix import verification",
        project_name="Phase 7J",
        task_id=task.id,
        plan=[
            {
                "step_number": 1,
                "description": "Create source",
                "commands": ["true"],
                "verification": "python -m py_compile calc_smoke.py",
                "rollback": None,
                "expected_files": ["calc_smoke.py"],
            }
        ],
    )
    state._project_dir_override = str(project_dir)
    state.execution_results = [
        StepResult(
            step_number=1,
            status="success",
            output="created",
            files_changed=["calc_smoke.py"],
        )
    ]
    ctx = OrchestrationRunContext(
        db=db_session,
        session=session,
        project=project,
        task=task,
        session_task_link=link,
        session_id=session.id,
        task_id=task.id,
        prompt="Fix import verification",
        timeout_seconds=120,
        execution_profile="full_lifecycle",
        validation_profile="implementation",
        runs_in_canonical_baseline=True,
        orchestration_state=state,
        runtime_service=_FakeRuntime(),
        task_service=_FakeTaskService(),
        logger=logging.getLogger("phase7j-test"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=SimpleNamespace(),
        task_execution_id=execution.id,
        restore_workspace_snapshot_if_needed=lambda reason: None,
    )
    return ctx, execution


def _seed_legacy_finalize_ctx(db_session, tmp_path, *, task_subfolder="task-work"):
    project_root = tmp_path / "legacy-project"
    workspace_dir = project_root / task_subfolder
    workspace_dir.mkdir(parents=True)
    project = Project(name="Legacy Finalize", workspace_path=str(project_root))
    db_session.add(project)
    db_session.flush()
    session = SessionModel(
        project_id=project.id,
        name="Legacy Finalize Session",
        status="running",
        is_active=True,
        execution_mode="manual",
        instance_id="legacy-finalize-session",
    )
    task = Task(
        project_id=project.id,
        title="Legacy Finalize Task",
        status=TaskStatus.RUNNING,
        task_subfolder=task_subfolder,
    )
    db_session.add_all([session, task])
    db_session.flush()
    link = SessionTask(
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.RUNNING,
    )
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
    )
    db_session.add_all([link, execution])
    db_session.commit()

    state = OrchestrationState(
        session_id=str(session.id),
        task_description="Create project files",
        project_name="Legacy Finalize",
        task_id=task.id,
        plan=[
            {
                "step_number": 1,
                "description": "Create files",
                "commands": ["true"],
                "verification": "test -d .",
                "rollback": None,
                "expected_files": [],
            }
        ],
    )
    state._project_dir_override = str(workspace_dir)
    state.execution_results = [
        StepResult(
            step_number=1,
            status="success",
            output="created",
            files_changed=[],
        )
    ]
    task_service = TaskService(db_session)
    ctx = OrchestrationRunContext(
        db=db_session,
        session=session,
        project=project,
        task=task,
        session_task_link=link,
        session_id=session.id,
        task_id=task.id,
        prompt="Create project files",
        timeout_seconds=120,
        execution_profile="full_lifecycle",
        validation_profile="implementation",
        runs_in_canonical_baseline=False,
        orchestration_state=state,
        runtime_service=_FakeRuntime(),
        task_service=task_service,
        logger=logging.getLogger("legacy-finalize-test"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=SimpleNamespace(),
        task_execution_id=execution.id,
        restore_workspace_snapshot_if_needed=lambda reason: None,
    )
    return ctx, execution, project_root, workspace_dir


def test_final_verification_7f_gate_repairs_when_classifier_misses(
    db_session, tmp_path, monkeypatch
):
    ctx, execution = _seed_finalize_ctx(db_session, tmp_path)
    repair_calls = []
    verification_outputs = [
        {
            "success": False,
            "returncode": 1,
            "output": "ImportError: cannot import name 'add' from 'calc_smoke'",
        },
        {"success": True, "returncode": 0, "output": "1 passed"},
    ]

    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.ValidatorService.validate_task_completion",
        lambda **kwargs: ValidationVerdict(
            stage="task_completion",
            status="accepted",
            profile="implementation",
            reasons=[],
            details={"expected_core_files": ["calc_smoke.py"]},
        ),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._detect_completion_verification_command",
        lambda project_dir: ("pytest", "python test suite detected"),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._classify_completion_verification_failure",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._execute_completion_verification",
        lambda **kwargs: verification_outputs.pop(0),
    )

    def _fake_repair(ctx, completion_validation, save_orchestration_checkpoint_fn):
        repair_calls.append(completion_validation)
        return {"status": "success", "step": {"description": "repair"}}

    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._attempt_completion_repair",
        _fake_repair,
    )

    result = finalize_successful_task(
        ctx=ctx,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        save_orchestration_checkpoint_fn=lambda *args, **kwargs: None,
    )

    assert result["status"] == "completed", result
    assert repair_calls
    assert repair_calls[0].details["completion_repair_source"] == (
        "final_completion_verification"
    )
    assert repair_calls[0].details["failure_class"] == "import_error"
    assert ctx.orchestration_state.debug_repair_task_execution_ids == []
    assert ctx.task.status == TaskStatus.DONE


def test_finalize_reuses_workspace_consistency_across_completion_validations(
    db_session, tmp_path, monkeypatch
):
    ctx, execution = _seed_finalize_ctx(db_session, tmp_path)
    counting_service = _CountingTaskService()
    ctx.task_service = counting_service
    validations = []

    def fake_validate_task_completion(**kwargs):
        validations.append(kwargs["workspace_consistency"])
        return ValidationVerdict(
            stage="task_completion",
            status=("repair_required" if len(validations) == 1 else "accepted"),
            profile="implementation",
            reasons=(["needs completion repair"] if len(validations) == 1 else []),
            details={"expected_core_files": ["calc_smoke.py"]},
        )

    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.ValidatorService.validate_task_completion",
        fake_validate_task_completion,
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._attempt_completion_repair",
        lambda **kwargs: {"status": "success", "step": {"description": "repair"}},
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._detect_completion_verification_command",
        lambda project_dir: (None, None),
    )

    result = finalize_successful_task(
        ctx=ctx,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        save_orchestration_checkpoint_fn=lambda *args, **kwargs: None,
    )

    assert result["status"] == "completed", result
    assert counting_service.analyze_calls == 1
    assert len(validations) >= 2
    assert all(consistency is validations[0] for consistency in validations)
    assert ctx.task.status == TaskStatus.DONE


def test_finalize_uses_deterministic_summary_when_runtime_summary_times_out(
    db_session, tmp_path, monkeypatch
):
    ctx, execution = _seed_finalize_ctx(db_session, tmp_path)
    ctx.runtime_service = _FailingSummaryRuntime()

    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.ValidatorService.validate_task_completion",
        lambda **kwargs: ValidationVerdict(
            stage="task_completion",
            status="accepted",
            profile="implementation",
            reasons=[],
            details={"expected_core_files": ["calc_smoke.py"]},
        ),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._detect_completion_verification_command",
        lambda project_dir: (None, None),
    )

    result = finalize_successful_task(
        ctx=ctx,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        save_orchestration_checkpoint_fn=lambda *args, **kwargs: None,
    )

    assert result["status"] == "completed", result
    assert ctx.task.status == TaskStatus.DONE
    assert "Task completed with verified execution evidence" in ctx.task.summary
    db_session.refresh(execution)
    assert execution.status == TaskStatus.DONE


def test_auto_advance_preserves_current_timeout_budget(
    db_session, tmp_path, monkeypatch
):
    ctx, _execution = _seed_finalize_ctx(db_session, tmp_path)
    ctx.session.execution_mode = "automatic"
    ctx.timeout_seconds = 90

    next_task = Task(
        project_id=ctx.project.id,
        title="Next low-resource task",
        description="Continue with the same low-resource budget",
        status=TaskStatus.PENDING,
        plan_position=2,
    )
    db_session.add(next_task)
    db_session.commit()

    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.ValidatorService.validate_task_completion",
        lambda **kwargs: ValidationVerdict(
            stage="task_completion",
            status="accepted",
            profile="implementation",
            reasons=[],
            details={"expected_core_files": ["calc_smoke.py"]},
        ),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._detect_completion_verification_command",
        lambda project_dir: (None, None),
    )

    captured_delay = {}

    result = finalize_successful_task(
        ctx=ctx,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        save_orchestration_checkpoint_fn=lambda *args, **kwargs: None,
        get_next_pending_project_task_fn=lambda db, project_id: next_task,
        get_latest_session_task_link_fn=lambda db, session_id, task_id: None,
        execute_orchestration_task_delay_fn=lambda **kwargs: captured_delay.update(
            kwargs
        ),
    )

    assert result["status"] == "completed"
    assert captured_delay["task_id"] == next_task.id
    assert captured_delay["timeout_seconds"] == 90


def test_auto_completion_stamps_change_set_metadata_on_trivial_publish(
    db_session, tmp_path, monkeypatch
):
    ctx, execution, project_root, workspace_dir = _seed_legacy_finalize_ctx(
        db_session, tmp_path
    )
    task_service = ctx.task_service
    snapshot_key = workspace_snapshot_key(ctx.task_id, execution.id)
    task_service.create_workspace_snapshot(
        ctx.project,
        workspace_dir,
        snapshot_key=snapshot_key,
        preserve_project_root_rules=False,
    )
    (workspace_dir / "src").mkdir()
    (workspace_dir / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")

    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.ValidatorService.validate_task_completion",
        lambda **kwargs: ValidationVerdict(
            stage="task_completion",
            status="accepted",
            profile="implementation",
            reasons=[],
            details={"expected_core_files": ["src/app.py"]},
        ),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.ValidatorService.validate_baseline_publish",
        lambda **kwargs: ValidationVerdict(
            stage="baseline_publish",
            status="accepted",
            profile="implementation",
            reasons=[],
            details={},
        ),
    )

    result = finalize_successful_task(
        ctx=ctx,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        save_orchestration_checkpoint_fn=lambda *args, **kwargs: None,
    )

    assert result["status"] == "completed"
    assert (project_root / "src" / "app.py").exists()
    publish_log = (
        db_session.query(LogEntry)
        .filter(LogEntry.task_id == ctx.task_id)
        .filter(LogEntry.message.like("[ORCHESTRATION] Published task workspace%"))
        .one()
    )
    payload = json.loads(publish_log.log_metadata)
    assert payload["workspace_review_policy"] == "hold_nontrivial"
    assert payload["accepted_change_set"]["task_execution_id"] == execution.id
    assert payload["accepted_change_set"]["change_set"]["added_files"] == ["src/app.py"]
    durable_change_set = (
        db_session.query(TaskExecutionChangeSet)
        .filter(TaskExecutionChangeSet.task_execution_id == execution.id)
        .one()
    )
    assert durable_change_set.review_decision["outcome"] == "auto_promote"
    assert durable_change_set.disposition == "promoted"
    assert durable_change_set.disposition_metadata["action"] == "auto_promote"


def test_auto_completion_flushes_done_state_before_next_task_lookup(
    db_session, tmp_path, monkeypatch
):
    ctx, execution, project_root, workspace_dir = _seed_legacy_finalize_ctx(
        db_session, tmp_path
    )
    del execution, project_root, workspace_dir
    ctx.session.execution_mode = "automatic"
    ctx.task.plan_position = 1
    next_task = Task(
        project_id=ctx.project.id,
        title="Next automatic task",
        status=TaskStatus.PENDING,
        plan_position=2,
    )
    db_session.add(next_task)
    db_session.commit()

    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.ValidatorService.validate_task_completion",
        lambda **kwargs: ValidationVerdict(
            stage="task_completion",
            status="accepted",
            profile="implementation",
            reasons=[],
            details={},
        ),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._detect_completion_verification_command",
        lambda project_dir: (None, None),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.ValidatorService.validate_baseline_publish",
        lambda **kwargs: ValidationVerdict(
            stage="baseline_publish",
            status="accepted",
            profile="implementation",
            reasons=[],
            details={},
        ),
    )

    result = finalize_successful_task(
        ctx=ctx,
        get_next_pending_project_task_fn=lambda db, project_id: TaskService(
            db
        ).get_next_pending_task(project_id),
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        save_orchestration_checkpoint_fn=lambda *args, **kwargs: None,
    )

    db_session.refresh(ctx.session)
    db_session.refresh(ctx.task)
    assert result["status"] == "completed", result
    assert ctx.task.status == TaskStatus.DONE
    assert ctx.session.status == "running"


def test_auto_completion_marks_session_completed_when_no_work_remains(
    db_session, tmp_path, monkeypatch
):
    ctx, execution, project_root, workspace_dir = _seed_legacy_finalize_ctx(
        db_session, tmp_path
    )
    del execution, project_root, workspace_dir
    ctx.session.execution_mode = "automatic"
    ctx.task.plan_position = 1
    db_session.commit()

    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.ValidatorService.validate_task_completion",
        lambda **kwargs: ValidationVerdict(
            stage="task_completion",
            status="accepted",
            profile="implementation",
            reasons=[],
            details={},
        ),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._detect_completion_verification_command",
        lambda project_dir: (None, None),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.ValidatorService.validate_baseline_publish",
        lambda **kwargs: ValidationVerdict(
            stage="baseline_publish",
            status="accepted",
            profile="implementation",
            reasons=[],
            details={},
        ),
    )

    result = finalize_successful_task(
        ctx=ctx,
        get_next_pending_project_task_fn=lambda db, project_id: TaskService(
            db
        ).get_next_pending_task(project_id),
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        save_orchestration_checkpoint_fn=lambda *args, **kwargs: None,
    )

    db_session.refresh(ctx.session)
    db_session.refresh(ctx.task)
    assert result["status"] == "completed", result
    assert ctx.task.status == TaskStatus.DONE
    assert ctx.session.status == "completed"
    assert ctx.session.is_active is False
    events = read_orchestration_events(
        ctx.orchestration_state.project_dir, ctx.session_id, ctx.task_id
    )
    event_types = [event["event_type"] for event in events]
    assert EventType.TASK_COMPLETED in event_types
    assert event_types.index(EventType.TASK_COMPLETED) < event_types.index(
        EventType.PHASE_FINISHED
    )


def test_auto_completion_holds_nontrivial_change_set_for_manual_review(
    db_session, tmp_path, monkeypatch
):
    ctx, execution, project_root, workspace_dir = _seed_legacy_finalize_ctx(
        db_session, tmp_path
    )
    task_service = ctx.task_service
    (workspace_dir / "README.md").write_text("before\n", encoding="utf-8")
    (workspace_dir / "old.md").write_text("old\n", encoding="utf-8")
    snapshot_key = workspace_snapshot_key(ctx.task_id, execution.id)
    task_service.create_workspace_snapshot(
        ctx.project,
        workspace_dir,
        snapshot_key=snapshot_key,
        preserve_project_root_rules=False,
    )
    (workspace_dir / "README.md").write_text("after\n", encoding="utf-8")
    (workspace_dir / "old.md").unlink()

    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.ValidatorService.validate_task_completion",
        lambda **kwargs: ValidationVerdict(
            stage="task_completion",
            status="accepted",
            profile="mutation",
            reasons=[],
            details={"expected_core_files": ["README.md"]},
        ),
    )

    result = finalize_successful_task(
        ctx=ctx,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        save_orchestration_checkpoint_fn=lambda *args, **kwargs: None,
    )

    assert result["status"] == "completed"
    assert not (project_root / "README.md").exists()
    assert workspace_dir.exists()
    assert ctx.task.workspace_status == "ready"
    review_log = (
        db_session.query(LogEntry)
        .filter(LogEntry.task_id == ctx.task_id)
        .filter(
            LogEntry.message == "[ORCHESTRATION] Held task workspace for manual review"
        )
        .one()
    )
    payload = json.loads(review_log.log_metadata)
    assert payload["auto_publish_skipped"] is True
    assert payload["reason"] == "nontrivial_change_set_review_required"
    assert payload["workspace_review_policy"] == "hold_nontrivial"
    assert "deleted_files" in payload["warning_flags"]


def test_canonical_root_completion_archives_task_path_metadata(
    db_session, tmp_path, monkeypatch
):
    ctx, execution, project_root, workspace_dir = _seed_legacy_finalize_ctx(
        db_session, tmp_path
    )
    del execution
    ctx.runs_in_canonical_baseline = True
    ctx.orchestration_state._project_dir_override = str(project_root)
    (workspace_dir / ".agent" / "events").mkdir(parents=True)
    (workspace_dir / ".agent" / "events" / "session.jsonl").write_text(
        "{}\n", encoding="utf-8"
    )

    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.ValidatorService.validate_task_completion",
        lambda **kwargs: ValidationVerdict(
            stage="task_completion",
            status="accepted",
            profile="implementation",
            reasons=[],
            details={},
        ),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._detect_completion_verification_command",
        lambda project_dir: (None, None),
    )

    result = finalize_successful_task(
        ctx=ctx,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        save_orchestration_checkpoint_fn=lambda *args, **kwargs: None,
    )

    assert result["status"] == "completed"
    assert ctx.task.workspace_status == "promoted"
    assert ctx.task.task_subfolder.startswith(".agent/promoted-workspace-archive/")
    assert not workspace_dir.exists()
    assert (project_root / ctx.task.task_subfolder).exists()


def test_auto_publish_all_policy_publishes_nontrivial_change_set(
    db_session, tmp_path, monkeypatch
):
    ctx, execution, project_root, workspace_dir = _seed_legacy_finalize_ctx(
        db_session, tmp_path
    )
    task_service = ctx.task_service
    (workspace_dir / "README.md").write_text("before\n", encoding="utf-8")
    (workspace_dir / "old.md").write_text("old\n", encoding="utf-8")
    snapshot_key = workspace_snapshot_key(ctx.task_id, execution.id)
    task_service.create_workspace_snapshot(
        ctx.project,
        workspace_dir,
        snapshot_key=snapshot_key,
        preserve_project_root_rules=False,
    )
    (workspace_dir / "README.md").write_text("after\n", encoding="utf-8")
    (workspace_dir / "old.md").unlink()

    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.get_effective_workspace_review_policy",
        lambda default_policy, db=None: "auto_publish_all",
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.ValidatorService.validate_task_completion",
        lambda **kwargs: ValidationVerdict(
            stage="task_completion",
            status="accepted",
            profile="mutation",
            reasons=[],
            details={"expected_core_files": ["README.md"]},
        ),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.ValidatorService.validate_baseline_publish",
        lambda **kwargs: ValidationVerdict(
            stage="baseline_publish",
            status="accepted",
            profile="mutation",
            reasons=[],
            details={},
        ),
    )

    result = finalize_successful_task(
        ctx=ctx,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        save_orchestration_checkpoint_fn=lambda *args, **kwargs: None,
    )

    assert result["status"] == "completed"
    assert (project_root / "README.md").read_text(encoding="utf-8") == "after\n"
    assert not (project_root / "old.md").exists()
    assert ctx.task.workspace_status == "promoted"
    publish_log = (
        db_session.query(LogEntry)
        .filter(LogEntry.task_id == ctx.task_id)
        .filter(LogEntry.message.like("[ORCHESTRATION] Published task workspace%"))
        .one()
    )
    payload = json.loads(publish_log.log_metadata)
    assert payload["workspace_review_policy"] == "auto_publish_all"
    assert (
        "deleted_files" in payload["accepted_change_set"]["change_set"]["warning_flags"]
    )


def test_evaluator_needs_review_holds_before_auto_publish(
    db_session, tmp_path, monkeypatch
):
    ctx, execution, project_root, workspace_dir = _seed_legacy_finalize_ctx(
        db_session, tmp_path
    )
    ctx.runtime_service = _NeedsReviewEvaluatorRuntime()
    task_service = ctx.task_service
    (workspace_dir / "README.md").write_text("before\n", encoding="utf-8")
    snapshot_key = workspace_snapshot_key(ctx.task_id, execution.id)
    task_service.create_workspace_snapshot(
        ctx.project,
        workspace_dir,
        snapshot_key=snapshot_key,
        preserve_project_root_rules=False,
    )
    (workspace_dir / "README.md").write_text("after\n", encoding="utf-8")

    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.get_effective_workspace_review_policy",
        lambda default_policy, db=None: "auto_publish_all",
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.ValidatorService.validate_task_completion",
        lambda **kwargs: ValidationVerdict(
            stage="task_completion",
            status="accepted",
            profile="mutation",
            reasons=[],
            details={"expected_core_files": ["README.md"]},
        ),
    )

    result = finalize_successful_task(
        ctx=ctx,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        save_orchestration_checkpoint_fn=lambda *args, **kwargs: None,
    )

    assert result["status"] == "completed"
    assert not (project_root / "README.md").exists()
    assert workspace_dir.exists()
    assert ctx.task.workspace_status == "ready"
    review_log = (
        db_session.query(LogEntry)
        .filter(LogEntry.task_id == ctx.task_id)
        .filter(
            LogEntry.message == "[ORCHESTRATION] Held task workspace for manual review"
        )
        .one()
    )
    payload = json.loads(review_log.log_metadata)
    assert payload["auto_publish_skipped"] is True
    assert payload["reason"] == "evaluator_needs_review"


def test_successful_later_task_does_not_complete_session_with_failed_link(
    db_session, tmp_path, monkeypatch
):
    ctx, execution = _seed_finalize_ctx(db_session, tmp_path)
    failed_task = Task(
        project_id=ctx.project.id,
        title="Earlier failed task",
        status=TaskStatus.FAILED,
        error_message="prior failure",
    )
    db_session.add(failed_task)
    db_session.flush()
    db_session.add(
        SessionTask(
            session_id=ctx.session_id,
            task_id=failed_task.id,
            status=TaskStatus.FAILED,
        )
    )
    db_session.commit()

    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.ValidatorService.validate_task_completion",
        lambda **kwargs: ValidationVerdict(
            stage="task_completion",
            status="accepted",
            profile="implementation",
            reasons=[],
            details={"expected_core_files": ["calc_smoke.py"]},
        ),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._detect_completion_verification_command",
        lambda project_dir: (None, None),
    )

    result = finalize_successful_task(
        ctx=ctx,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        save_orchestration_checkpoint_fn=lambda *args, **kwargs: None,
    )

    assert result["status"] == "completed", result
    assert ctx.task.status == TaskStatus.DONE
    assert ctx.session.status == "paused"
    assert ctx.session.last_alert_level == "error"
    assert "failed task" in ctx.session.last_alert_message.lower()


def test_hold_all_policy_holds_trivial_change_set_for_manual_review(
    db_session, tmp_path, monkeypatch
):
    ctx, execution, project_root, workspace_dir = _seed_legacy_finalize_ctx(
        db_session, tmp_path
    )
    task_service = ctx.task_service
    snapshot_key = workspace_snapshot_key(ctx.task_id, execution.id)
    task_service.create_workspace_snapshot(
        ctx.project,
        workspace_dir,
        snapshot_key=snapshot_key,
        preserve_project_root_rules=False,
    )
    (workspace_dir / "src").mkdir()
    (workspace_dir / "src" / "app.py").write_text("print('ok')\n", encoding="utf-8")

    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.get_effective_workspace_review_policy",
        lambda default_policy, db=None: "hold_all",
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.ValidatorService.validate_task_completion",
        lambda **kwargs: ValidationVerdict(
            stage="task_completion",
            status="accepted",
            profile="implementation",
            reasons=[],
            details={"expected_core_files": ["src/app.py"]},
        ),
    )

    result = finalize_successful_task(
        ctx=ctx,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        save_orchestration_checkpoint_fn=lambda *args, **kwargs: None,
    )

    assert result["status"] == "completed"
    assert not (project_root / "src" / "app.py").exists()
    assert workspace_dir.exists()
    review_log = (
        db_session.query(LogEntry)
        .filter(LogEntry.task_id == ctx.task_id)
        .filter(
            LogEntry.message == "[ORCHESTRATION] Held task workspace for manual review"
        )
        .one()
    )
    payload = json.loads(review_log.log_metadata)
    assert payload["auto_publish_skipped"] is True
    assert payload["workspace_review_policy"] == "hold_all"
    assert payload["warning_flags"] == []


def test_final_verification_repair_runs_with_prior_execution_debug_attempt(
    db_session, tmp_path, monkeypatch
):
    ctx, execution = _seed_finalize_ctx(db_session, tmp_path)
    ctx.orchestration_state.debug_repair_task_execution_ids = [execution.id]
    repair_calls = []

    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.ValidatorService.validate_task_completion",
        lambda **kwargs: ValidationVerdict(
            stage="task_completion",
            status="accepted",
            profile="implementation",
            reasons=[],
            details={"expected_core_files": ["calc_smoke.py"]},
        ),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._detect_completion_verification_command",
        lambda project_dir: ("pytest", "python test suite detected"),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._classify_completion_verification_failure",
        lambda **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._execute_completion_verification",
        lambda **kwargs: {
            "success": False,
            "returncode": 1,
            "output": "ImportError: cannot import name 'add' from 'calc_smoke'",
        },
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._attempt_completion_repair",
        lambda *args, **kwargs: repair_calls.append(args) or {"status": "success"},
    )

    result = finalize_successful_task(
        ctx=ctx,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        save_orchestration_checkpoint_fn=lambda *args, **kwargs: None,
    )

    assert result == {"status": "failed", "reason": "completion_verification_failed"}
    assert repair_calls
    assert ctx.task.status == TaskStatus.FAILED
    events = read_orchestration_events(
        ctx.orchestration_state.project_dir, ctx.session_id, ctx.task_id
    )
    assert events[-1]["event_type"] == EventType.PHASE_FINISHED
    assert events[-1]["details"]["status"] == "verification_failed"


def test_completion_verification_repair_has_separate_budget_from_execution_debug(
    db_session, tmp_path, monkeypatch
):
    ctx, execution = _seed_finalize_ctx(db_session, tmp_path)
    ctx.orchestration_state.debug_repair_task_execution_ids = [execution.id]
    ctx.orchestration_state.completion_repair_attempts = 0
    verification_outputs = [
        {
            "success": False,
            "returncode": 2,
            "output": "ImportError while importing test module tests/test_config.py",
        },
        {"success": True, "returncode": 0, "output": "2 passed"},
    ]
    runtime_outputs = [
        {"output": "Task summary"},
        {
            "output": (
                '{"description":"repair import","commands":["python -c \\"print(1)\\""],'
                '"verification":"python -c \\"print(1)\\"","expected_files":[]}'
            )
        },
        {"output": "repair applied"},
    ]

    class _Runtime:
        async def execute_task(self, prompt, timeout_seconds=None):
            return runtime_outputs.pop(0)

        def get_backend_metadata(self):
            return {"backend": "fake", "model_family": "test"}

    ctx.runtime_service = _Runtime()

    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.ValidatorService.validate_task_completion",
        lambda **kwargs: ValidationVerdict(
            stage="task_completion",
            status="accepted",
            profile="implementation",
            reasons=[],
            details={"expected_core_files": ["tests/test_config.py"]},
        ),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._detect_completion_verification_command",
        lambda project_dir: ("pytest", "python test suite detected"),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._classify_completion_verification_failure",
        lambda **kwargs: ValidationVerdict(
            stage="completion_verification",
            status="repair_required",
            profile="implementation",
            reasons=["Completion verification found a repairable import issue"],
            details={
                "verification_command": "pytest",
                "completion_repair_source": "final_completion_verification",
                "failure_class": "import_error",
            },
        ),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow._execute_completion_verification",
        lambda **kwargs: verification_outputs.pop(0),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.completion_flow.assess_step_execution",
        lambda **kwargs: SimpleNamespace(
            step_status="success",
            step_output="repair applied",
            error_message="",
            missing_files=[],
            stub_files=[],
            tool_failures=[],
            correction_hints=[],
            verification_output="",
            validation_verdict=None,
        ),
    )

    result = finalize_successful_task(
        ctx=ctx,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        save_orchestration_checkpoint_fn=lambda *args, **kwargs: None,
    )

    assert result["status"] == "completed"
    assert ctx.orchestration_state.completion_repair_attempts == 1
    assert ctx.task.status == TaskStatus.DONE
