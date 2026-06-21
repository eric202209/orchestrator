"""Phase 13B-E38 source-stub detection and prompt-injection coverage."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.services.orchestration.context.assembly import (
    DebugPromptInputs,
    assemble_debugging_prompt,
    assemble_planning_prompt,
)
from app.services.project.source_imports import render_source_stub_block
from app.services.prompt_templates import OrchestrationState


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_context(project_dir: Path) -> SimpleNamespace:
    state = OrchestrationState(
        session_id="e38",
        task_description="Implement the incomplete Python functions",
        project_name="stub-project",
        project_context="",
        task_id=38,
    )
    state._project_dir_override = str(project_dir)
    return SimpleNamespace(
        db=None,
        prompt="Implement the incomplete Python functions",
        execution_profile="full_lifecycle",
        workflow_profile="default",
        orchestration_state=state,
    )


def test_detects_notimplementederror_and_extracts_method_signature(tmp_path: Path):
    _write(
        tmp_path / "src/pkg/store.py",
        "class TaskStore:\n"
        "    def summary(self) -> tuple[int, int]:\n"
        "        raise NotImplementedError('not ready')\n",
    )

    block = render_source_stub_block(tmp_path)

    assert "src/pkg/store.py :: TaskStore.summary(self) -> tuple[int, int]" in block


def test_detects_pass_only_stub(tmp_path: Path):
    _write(tmp_path / "src/pkg/api.py", "def pending(value: int) -> str:\n    pass\n")

    block = render_source_stub_block(tmp_path)

    assert "src/pkg/api.py :: pending(value: int) -> str" in block


def test_detects_todo_only_placeholder(tmp_path: Path):
    _write(
        tmp_path / "src/pkg/api.py",
        "def pending() -> None:\n" "    # TODO: implement this API.\n" "    pass\n",
    )

    block = render_source_stub_block(tmp_path)

    assert "src/pkg/api.py :: pending() -> None" in block


def test_extracts_top_level_signature(tmp_path: Path):
    _write(
        tmp_path / "src/pkg/formatting.py",
        "def format_summary(total: int, completed: int) -> str:\n"
        "    raise NotImplementedError\n",
    )

    block = render_source_stub_block(tmp_path)

    assert "format_summary(total: int, completed: int) -> str" in block


def test_planning_prompt_includes_stub_block(tmp_path: Path):
    _write(tmp_path / "src/pkg/api.py", "def pending():\n    pass\n")

    prompt = assemble_planning_prompt(_make_context(tmp_path), {})

    assert "## SOURCE STUBS REQUIRING IMPLEMENTATION" in prompt
    assert "src/pkg/api.py :: pending()" in prompt


def test_debugging_prompt_includes_stub_block(tmp_path: Path):
    _write(tmp_path / "src/pkg/api.py", "def pending():\n    pass\n")

    prompt = assemble_debugging_prompt(
        _make_context(tmp_path),
        DebugPromptInputs(
            step_description="Run tests",
            error_message="pending failed",
            command_output="",
            verification_output="",
            attempt_number=1,
            max_attempts=2,
        ),
    )

    assert "## SOURCE STUBS REQUIRING IMPLEMENTATION" in prompt
    assert "src/pkg/api.py :: pending()" in prompt


def test_returns_empty_for_projects_with_no_stubs(tmp_path: Path):
    _write(tmp_path / "src/pkg/api.py", "def ready() -> bool:\n    return True\n")

    assert render_source_stub_block(tmp_path) == ""


def test_returns_empty_for_non_python_projects(tmp_path: Path):
    _write(tmp_path / "src/main.ts", "export const ready = true;\n")

    assert render_source_stub_block(tmp_path) == ""
    assert "SOURCE STUBS" not in assemble_planning_prompt(_make_context(tmp_path), {})


def test_enforces_max_chars(tmp_path: Path):
    _write(
        tmp_path / "src/pkg/api.py",
        "def first():\n    pass\n\n"
        "def second():\n    pass\n\n"
        "def third():\n    pass\n",
    )

    assert len(render_source_stub_block(tmp_path, max_chars=80)) <= 80
