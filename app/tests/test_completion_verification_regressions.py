from __future__ import annotations

from types import SimpleNamespace

from app.services.orchestration.phases.completion_flow import (
    _augment_completion_verification_command,
    _classify_completion_verification_failure,
    _execute_completion_verification,
)
from app.services.orchestration.validation.validator import ValidatorService


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
    assert "src/utils/format.test.ts" in verdict.details["expected_core_files"]


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

    assert command == "pnpm test -- --exclude=.openclaw/**"


def test_jest_completion_verification_excludes_openclaw_snapshots():
    command = _augment_completion_verification_command(
        "pnpm test",
        "node --runInBand jest",
    )

    assert command == "pnpm test -- --testPathIgnorePatterns=.openclaw/"


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
        command="pnpm test -- --exclude=.openclaw/**",
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
