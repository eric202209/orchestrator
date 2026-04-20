from __future__ import annotations

from types import SimpleNamespace

from app.services.orchestration.context_assembly import (
    assemble_completion_repair_inputs,
    assemble_execution_prompt,
    assemble_planning_prompt,
    build_workspace_inventory_summary,
)
from app.services.prompt_templates import OrchestrationState, StepResult


def _make_ctx(tmp_path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "src").mkdir()
    (project_dir / "src" / "main.ts").write_text(
        "export const ok = true;\n", encoding="utf-8"
    )
    (project_dir / "tests").mkdir()
    (project_dir / "tests" / "main.test.ts").write_text(
        "test('ok', () => {});\n", encoding="utf-8"
    )

    state = OrchestrationState(
        session_id="11",
        task_description="Build a small testable TypeScript module",
        project_name="Assembly Test",
        project_context=("Very long context " * 400),
        task_id=22,
    )
    state._project_dir_override = str(project_dir)
    state.execution_results = [
        StepResult(
            step_number=1,
            status="success",
            output="Created source files and wiring",
            files_changed=["src/main.ts"],
        ),
        StepResult(
            step_number=2,
            status="success",
            output="Added tests and package metadata",
            files_changed=["tests/main.test.ts", "package.json"],
        ),
    ]
    state.phase_history = [
        {
            "phase": "planning",
            "status": "completed",
            "message": "Initial plan generated",
        },
        {
            "phase": "executing",
            "status": "warning",
            "message": "One step needed a retry",
        },
    ]
    state.validation_history = [
        {"stage": "plan", "status": "warning", "reasons": ["naming mismatch"]},
        {"stage": "step", "status": "accepted", "reasons": []},
    ]

    return SimpleNamespace(
        prompt="Build a TypeScript module with tests in the current workspace",
        execution_profile="full_lifecycle",
        orchestration_state=state,
    )


def test_workspace_inventory_summary_prefers_current_workspace_truth(tmp_path):
    ctx = _make_ctx(tmp_path)
    summary = build_workspace_inventory_summary(
        ctx.orchestration_state.project_dir,
        workspace_review={
            "file_count": 2,
            "source_file_count": 2,
            "placeholder_issue_count": 0,
            "summary": "Existing implementation already present in src/ and tests/.",
        },
        expected_files=["src/main.ts", "tests/main.test.ts"],
    )

    assert "Current workspace inventory:" in summary
    assert "- src/main.ts" in summary
    assert "- tests/main.test.ts" in summary
    assert "Expected file delta:" in summary


def test_assembled_prompts_trim_dense_context_but_keep_workspace_inventory(tmp_path):
    ctx = _make_ctx(tmp_path)

    planning_prompt = assemble_planning_prompt(
        ctx,
        {"file_count": 2, "source_file_count": 2, "placeholder_issue_count": 0},
    )
    execution_prompt = assemble_execution_prompt(
        ctx,
        {
            "description": "Run tests",
            "commands": ["npm test"],
            "verification": "npm test",
            "rollback": None,
            "expected_files": ["tests/main.test.ts"],
        },
    )

    assert "Current workspace inventory:" in planning_prompt
    assert "src/main.ts" in planning_prompt
    assert len(planning_prompt) < 5200
    assert "Current workspace inventory:" in execution_prompt
    assert "tests/main.test.ts" in execution_prompt
    assert len(execution_prompt) < 4200


def test_completion_repair_inputs_are_summary_only_and_workspace_driven(tmp_path):
    ctx = _make_ctx(tmp_path)
    completion_validation = SimpleNamespace(
        details={"expected_core_files": ["src/main.ts", "tests/main.test.ts"]},
        reasons=["Missing expected tests"],
    )

    assembled = assemble_completion_repair_inputs(ctx, completion_validation)

    assert "Current workspace inventory:" in assembled["workspace_inventory"]
    assert "src/main.ts" in assembled["workspace_inventory"]
    assert "step=1 verdict=success" in assembled["prior_results_summary"]
    assert len(assembled["project_context"]) < 3000
