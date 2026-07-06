from __future__ import annotations

from types import SimpleNamespace

from app.services.orchestration.context.assembly import (
    DebugPromptInputs,
    assemble_completion_repair_inputs,
    assemble_debugging_prompt,
    assemble_execution_prompt,
    assemble_plan_revision_prompt,
    assemble_planning_prompt,
    build_workspace_inventory_summary,
    collect_workspace_inventory_paths,
    sanitize_progress_notes_for_workspace,
)
import app.services.orchestration.phases.execution_loop as execution_loop
from app.models import LogEntry
from app.schemas.knowledge import (
    KnowledgeContext,
    KnowledgeItemRef,
    RecommendedAction,
)
from app.services.orchestration.prompt_templates import OrchestrationState, StepResult
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


def _knowledge_ctx_for_debug_prompt() -> KnowledgeContext:
    return KnowledgeContext(
        retrieved_items=[
            KnowledgeItemRef(
                id="debug-case-1",
                title="Pytest assertion repair",
                knowledge_type="debug_case",
                content="Inspect the failing assertion before changing implementation.",
                priority=10,
                confidence=0.92,
            )
        ],
        query="assertion failed",
        trigger_phase="failure",
        retrieval_reason="semantic_retrieval",
        confidence=0.92,
        matched_failure_memory=False,
        recommended_action=RecommendedAction.review_failure,
    )


def _debug_knowledge_ctx(
    *,
    knowledge_type: str = "debug_case",
    confidence: float = 0.92,
    retrieval_reason: str = "semantic_retrieval",
) -> KnowledgeContext:
    return KnowledgeContext(
        retrieved_items=[
            KnowledgeItemRef(
                id="debug-knowledge-1",
                title="Debug repair memory",
                knowledge_type=knowledge_type,
                content="Use the failure output to target the repair.",
                priority=10,
                confidence=confidence,
            )
        ],
        query="debug failure",
        trigger_phase="failure",
        retrieval_reason=retrieval_reason,
        confidence=confidence,
        matched_failure_memory=knowledge_type == "failure_memory",
        recommended_action=RecommendedAction.review_failure,
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
    # Slice H raised the project_context budget from 280 → 800 chars.
    # The prompt is now larger but must stay well under DIRECT_PLANNING_PROMPT_CHAR_CAP=12000.
    assert len(planning_prompt) < 9000
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


def test_operator_guidance_reaches_next_runtime_boundaries(tmp_path, db_session):
    ctx = _make_ctx(tmp_path)
    ctx.db = db_session
    db_session.add(
        LogEntry(
            session_id=11,
            task_id=22,
            level="INFO",
            message="[OPERATOR_GUIDANCE] Prefer the smaller fix.",
            log_metadata="{}",
        )
    )
    db_session.commit()

    step = {
        "description": "Run tests",
        "commands": ["npm test"],
        "verification": "npm test",
        "rollback": None,
        "expected_files": ["tests/main.test.ts"],
    }
    execution_prompt = assemble_execution_prompt(ctx, step)
    debugging_prompt = assemble_debugging_prompt(
        ctx,
        DebugPromptInputs(
            step_description="Run tests",
            error_message="failed",
            command_output="",
            verification_output="",
            attempt_number=1,
            max_attempts=2,
        ),
    )
    revision_prompt = assemble_plan_revision_prompt(
        ctx,
        failed_steps=[
            StepResult(step_number=1, status="failed", error_message="failed")
        ],
        debug_analysis="Need a smaller fix.",
    )
    completion_inputs = assemble_completion_repair_inputs(
        ctx,
        SimpleNamespace(details={"expected_core_files": ["tests/main.test.ts"]}),
    )

    assert "Prefer the smaller fix." in execution_prompt
    assert "Prefer the smaller fix." in debugging_prompt
    assert "Prefer the smaller fix." in revision_prompt
    assert "Prefer the smaller fix." in completion_inputs["project_context"]


def test_debugging_prompt_includes_injected_knowledge_context(tmp_path):
    ctx = _make_ctx(tmp_path)

    debugging_prompt = assemble_debugging_prompt(
        ctx,
        DebugPromptInputs(
            step_description="Run tests",
            error_message="failed",
            command_output="FAILED tests/test_main.py::test_value",
            verification_output="",
            attempt_number=1,
            max_attempts=2,
            knowledge_context=_knowledge_ctx_for_debug_prompt(),
        ),
    )

    assert "## KNOWLEDGE REFERENCES" in debugging_prompt
    assert "Pytest assertion repair" in debugging_prompt
    assert (
        "Inspect the failing assertion before changing implementation."
        in debugging_prompt
    )


def test_low_confidence_generic_failure_memory_is_not_debug_injected():
    filtered = execution_loop._filter_debug_knowledge_context_for_prompt(
        _debug_knowledge_ctx(
            knowledge_type="failure_memory",
            confidence=0.3,
            retrieval_reason="sqlite_fallback_qdrant_or_embedding_unavailable",
        )
    )

    assert filtered is None


def test_debug_knowledge_usage_logged_only_for_filtered_injected_context(monkeypatch):
    retrieved_ctx = _debug_knowledge_ctx(
        knowledge_type="debug_case",
        confidence=0.91,
    )
    usage_calls = []

    class _FakeKnowledgeService:
        def __init__(self, *args, **kwargs):
            pass

        def retrieve(self, **kwargs):
            return retrieved_ctx

    monkeypatch.setattr(
        "app.services.knowledge.knowledge_service.KnowledgeService",
        _FakeKnowledgeService,
    )
    monkeypatch.setattr(
        "app.services.knowledge.usage_log_service.log_usage",
        lambda **kwargs: usage_calls.append(kwargs),
    )

    ctx = SimpleNamespace(db=object(), session_id=11, task_id=22)
    debug_inputs = DebugPromptInputs(
        step_description="Run tests",
        error_message="AssertionError: expected true",
        command_output="FAILED tests/test_main.py::test_value",
        verification_output="",
        attempt_number=1,
        max_attempts=2,
    )

    filtered = execution_loop._retrieve_debug_repair_knowledge(
        ctx, debug_inputs, logger=SimpleNamespace(debug=lambda *_args, **_kwargs: None)
    )
    execution_loop._log_debug_repair_knowledge_usage(
        ctx, filtered, logger=SimpleNamespace(debug=lambda *_args, **_kwargs: None)
    )

    assert filtered is not None
    assert [item.id for item in filtered.retrieved_items] == ["debug-knowledge-1"]
    assert usage_calls
    assert usage_calls[0]["used_in_prompt"] is True
    assert usage_calls[0]["context"].retrieved_items == filtered.retrieved_items


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
    assert "package.json restored to vitest run" in sanitized


def test_progress_notes_filter_absolute_stale_file_references_against_live_workspace(
    tmp_path,
):
    ctx = _make_ctx(tmp_path)
    project_dir = ctx.orchestration_state.project_dir
    (project_dir / "docs").mkdir()
    (project_dir / "docs" / "README.md").write_text("ok\n", encoding="utf-8")

    notes = """
## Prior task
- Read /root/.openclaw/workspace/docs/adr/0002-advisory-human-guidance-conflict-detection.md
- Verified docs/README.md exists
"""

    sanitized = sanitize_progress_notes_for_workspace(notes, project_dir)

    assert "0002-advisory-human-guidance-conflict-detection.md" not in sanitized
    assert "docs/README.md exists" in sanitized


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
