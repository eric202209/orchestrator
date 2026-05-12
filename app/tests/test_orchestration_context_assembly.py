from __future__ import annotations

from types import SimpleNamespace

from app.services.orchestration.context_assembly import (
    assemble_completion_repair_inputs,
    assemble_execution_prompt,
    assemble_planning_prompt,
    build_workspace_inventory_summary,
    collect_workspace_inventory_paths,
    sanitize_progress_notes_for_workspace,
)
from app.services.prompt_templates import OrchestrationState, StepResult
from app.services.workspace.path_display import render_workspace_path_for_prompt


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
        db=None,
        prompt="Build a TypeScript module with tests in the current workspace",
        execution_profile="full_lifecycle",
        workflow_profile="default",
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
    ctx.workflow_profile = "fullstack_scaffold"

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
    assert "Workflow profile: fullstack_scaffold" in planning_prompt
    assert "1. create_frontend_skeleton" in planning_prompt
    assert "2. create_backend_skeleton" in planning_prompt
    assert "3. wire_api_config" in planning_prompt
    assert "4. verify_dev_startup" in planning_prompt
    assert len(planning_prompt) < 6600
    assert "Current workspace inventory:" in execution_prompt
    assert "tests/main.test.ts" in execution_prompt
    assert len(execution_prompt) < 7600


def test_compact_execution_prompt_is_smaller_but_keeps_workspace_truth(tmp_path):
    ctx = _make_ctx(tmp_path)

    regular_prompt = assemble_execution_prompt(
        ctx,
        {
            "description": "Run tests",
            "commands": ["npm test"],
            "verification": "npm test",
            "rollback": None,
            "expected_files": ["tests/main.test.ts"],
        },
    )
    compact_prompt = assemble_execution_prompt(
        ctx,
        {
            "description": "Run tests",
            "commands": ["npm test"],
            "verification": "npm test",
            "rollback": None,
            "expected_files": ["tests/main.test.ts"],
        },
        compact=True,
    )

    assert "Current workspace inventory:" in compact_prompt
    assert "tests/main.test.ts" in compact_prompt
    assert "<<<HITL_REQUEST:" in compact_prompt
    assert "authorization, destructive/risky actions" in compact_prompt
    assert len(compact_prompt) < len(regular_prompt)


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


def test_progress_notes_filter_stale_file_references_against_live_workspace(tmp_path):
    ctx = _make_ctx(tmp_path)
    project_dir = ctx.orchestration_state.project_dir
    (project_dir / "src" / "utils").mkdir()
    (project_dir / "src" / "utils" / "format.spec.ts").write_text(
        "test('format', () => {});\n",
        encoding="utf-8",
    )

    notes = """
## Prior task
- Renamed src/utils/format.spec.ts to src/utils/format.test.ts
- Verified src/utils/format.test.ts exists
- package.json restored to vitest run
"""

    sanitized = sanitize_progress_notes_for_workspace(notes, project_dir)

    assert "Verified src/utils/format.test.ts exists" not in sanitized
    assert "Ignore prior-note file references" in sanitized
    assert "src/utils/format.test.ts" in sanitized
    assert "package.json restored to vitest run" in sanitized


def test_workspace_inventory_skips_ignored_directories_without_descending(tmp_path):
    project_dir = tmp_path / "project"
    (project_dir / "src").mkdir(parents=True)
    (project_dir / "src" / "main.ts").write_text("export const ok = true;\n")
    (project_dir / "node_modules" / "pkg").mkdir(parents=True)
    (project_dir / "node_modules" / "pkg" / "index.js").write_text(
        "module.exports = {}\n"
    )

    inventory = collect_workspace_inventory_paths(project_dir, max_files=10)

    assert "src/main.ts" in inventory
    assert all("node_modules" not in path for path in inventory)


def test_render_workspace_path_for_prompt_uses_configured_workspace_root(monkeypatch):
    monkeypatch.setattr(
        "app.services.workspace.path_display.get_effective_workspace_root",
        lambda db=None: __import__("pathlib").Path(
            "/root/.openclaw/workspace/vault/projects"
        ),
    )

    rendered = render_workspace_path_for_prompt(
        "/root/.openclaw/workspace/vault/projects/skillsync"
    )

    assert rendered == "/root/.openclaw/workspace/vault/projects/skillsync"


def test_assembled_prompts_do_not_leak_host_workspace_paths(monkeypatch, tmp_path):
    monkeypatch.setattr(
        "app.services.workspace.path_display.get_effective_workspace_root",
        lambda db=None: __import__("pathlib").Path(
            "/root/.openclaw/workspace/vault/projects"
        ),
    )

    host_prefix = "/home/ci-runner/host-workspace/vault/projects"
    ctx = _make_ctx(tmp_path)
    ctx.orchestration_state._project_dir_override = f"{host_prefix}/skillsync"

    planning_prompt = assemble_planning_prompt(
        ctx,
        {"file_count": 2, "source_file_count": 2, "placeholder_issue_count": 0},
    )

    assert host_prefix not in planning_prompt
