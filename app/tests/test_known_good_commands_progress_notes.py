"""Tests for Slice I: known-good commands appended to progress_notes.md.

Constraints:
- No live model calls.
- No DB access.
- Uses tmp_path; no production filesystem access.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.services.orchestration.phases.completion_flow import (
    _PROGRESS_NOTES_COMMAND_MAX_CHARS,
    _PROGRESS_NOTES_COMMANDS_CAP,
    _extract_progress_notes_commands,
    _write_progress_notes,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_step_result(step_number: int, status: str = "success"):
    r = SimpleNamespace(step_number=step_number, status=status)
    return r


def _make_state(
    project_dir: str,
    plan: list | None = None,
    execution_results: list | None = None,
    changed_files: list | None = None,
) -> MagicMock:
    state = MagicMock()
    state.project_dir = project_dir
    state.plan = plan or []
    state.execution_results = execution_results or []
    state.changed_files = changed_files or []
    return state


def _make_task(title: str = "test task") -> MagicMock:
    task = MagicMock()
    task.title = title
    return task


def _make_logger() -> MagicMock:
    return MagicMock()


# ---------------------------------------------------------------------------
# _extract_progress_notes_commands
# ---------------------------------------------------------------------------


def test_extract_returns_commands_for_successful_steps():
    state = _make_state(
        "/tmp/x",
        plan=[
            {"step_number": 1, "commands": ["npm install", "npm test"]},
            {"step_number": 2, "commands": ["node -e \"require('./app')\""]},
        ],
        execution_results=[_make_step_result(1), _make_step_result(2)],
    )
    result = _extract_progress_notes_commands(state)
    assert "npm install" in result
    assert "npm test" in result
    assert "node -e \"require('./app')\"" in result


def test_extract_skips_steps_not_in_execution_results():
    state = _make_state(
        "/tmp/x",
        plan=[
            {"step_number": 1, "commands": ["npm install"]},
            {"step_number": 2, "commands": ["should-not-appear"]},
        ],
        execution_results=[_make_step_result(1)],
    )
    result = _extract_progress_notes_commands(state)
    assert "npm install" in result
    assert "should-not-appear" not in result


def test_extract_deduplicates_commands():
    state = _make_state(
        "/tmp/x",
        plan=[
            {"step_number": 1, "commands": ["npm test", "npm test"]},
            {"step_number": 2, "commands": ["npm test"]},
        ],
        execution_results=[_make_step_result(1), _make_step_result(2)],
    )
    result = _extract_progress_notes_commands(state)
    assert result.count("npm test") == 1


def test_extract_skips_empty_commands():
    state = _make_state(
        "/tmp/x",
        plan=[
            {"step_number": 1, "commands": ["", "  ", "npm install", None]},
        ],
        execution_results=[_make_step_result(1)],
    )
    result = _extract_progress_notes_commands(state)
    assert result == ["npm install"]


def test_extract_caps_at_limit():
    cmds = [f"echo {i}" for i in range(30)]
    state = _make_state(
        "/tmp/x",
        plan=[{"step_number": 1, "commands": cmds}],
        execution_results=[_make_step_result(1)],
    )
    result = _extract_progress_notes_commands(state)
    assert len(result) == _PROGRESS_NOTES_COMMANDS_CAP


def test_extract_empty_plan_returns_empty():
    state = _make_state("/tmp/x", plan=[], execution_results=[])
    assert _extract_progress_notes_commands(state) == []


def test_extract_empty_execution_results_returns_empty():
    state = _make_state(
        "/tmp/x",
        plan=[{"step_number": 1, "commands": ["npm test"]}],
        execution_results=[],
    )
    assert _extract_progress_notes_commands(state) == []


def test_extract_handles_dict_style_execution_result():
    """execution_results may contain dicts in some code paths."""
    state = _make_state(
        "/tmp/x",
        plan=[{"step_number": 1, "commands": ["pytest"]}],
        execution_results=[{"step_number": 1, "status": "success"}],
    )
    result = _extract_progress_notes_commands(state)
    assert "pytest" in result


# ---------------------------------------------------------------------------
# _write_progress_notes — integration
# ---------------------------------------------------------------------------


def test_write_progress_notes_appends_known_good_commands(tmp_path):
    state = _make_state(
        str(tmp_path),
        plan=[
            {"step_number": 1, "commands": ["npm install", "npm test"]},
        ],
        execution_results=[_make_step_result(1)],
        changed_files=["src/app.js"],
    )
    _write_progress_notes(
        orchestration_state=state,
        task=_make_task("Setup project"),
        prompt="Setup the project",
        summary="Installed and tested",
        logger=_make_logger(),
    )
    notes = (tmp_path / ".agent" / "progress_notes.md").read_text()
    assert "**Known good commands:**" in notes
    assert "- npm install" in notes
    assert "- npm test" in notes


def test_write_progress_notes_no_known_good_section_when_no_commands(tmp_path):
    state = _make_state(
        str(tmp_path),
        plan=[{"step_number": 1, "commands": []}],
        execution_results=[_make_step_result(1)],
    )
    _write_progress_notes(
        orchestration_state=state,
        task=_make_task(),
        prompt="Do work",
        summary="done",
        logger=_make_logger(),
    )
    notes = (tmp_path / ".agent" / "progress_notes.md").read_text()
    assert "**Known good commands:**" not in notes


def test_write_progress_notes_preserves_existing_sections(tmp_path):
    state = _make_state(
        str(tmp_path),
        plan=[{"step_number": 1, "commands": ["python3 -m pytest -q"]}],
        execution_results=[_make_step_result(1)],
        changed_files=["tests/test_app.py"],
    )
    _write_progress_notes(
        orchestration_state=state,
        task=_make_task("Add tests"),
        prompt="Add tests",
        summary="Tests added",
        logger=_make_logger(),
    )
    notes = (tmp_path / ".agent" / "progress_notes.md").read_text()
    # Existing sections must still be present
    assert "**Steps completed" in notes
    assert "**Files changed" in notes
    assert "**Summary:**" in notes
    # Known good commands appended after
    assert "**Known good commands:**" in notes
    assert "- python3 -m pytest -q" in notes


def test_write_progress_notes_deduplicates_across_plan_steps(tmp_path):
    state = _make_state(
        str(tmp_path),
        plan=[
            {"step_number": 1, "commands": ["npm test"]},
            {"step_number": 2, "commands": ["npm test", "npm run build"]},
        ],
        execution_results=[_make_step_result(1), _make_step_result(2)],
    )
    _write_progress_notes(
        orchestration_state=state,
        task=_make_task(),
        prompt="Run build",
        summary="",
        logger=_make_logger(),
    )
    notes = (tmp_path / ".agent" / "progress_notes.md").read_text()
    assert notes.count("npm test") == 1
    assert "npm run build" in notes


def test_write_progress_notes_truncates_long_commands(tmp_path):
    long_cmd = "echo " + "x" * 200
    state = _make_state(
        str(tmp_path),
        plan=[{"step_number": 1, "commands": [long_cmd]}],
        execution_results=[_make_step_result(1)],
    )
    _write_progress_notes(
        orchestration_state=state,
        task=_make_task(),
        prompt="Run long command",
        summary="",
        logger=_make_logger(),
    )
    notes = (tmp_path / ".agent" / "progress_notes.md").read_text()
    for line in notes.splitlines():
        if line.startswith("- echo"):
            assert len(line) <= _PROGRESS_NOTES_COMMAND_MAX_CHARS + 2  # "- " prefix
            break


def test_write_progress_notes_caps_command_count(tmp_path):
    cmds = [f"echo {i}" for i in range(30)]
    state = _make_state(
        str(tmp_path),
        plan=[{"step_number": 1, "commands": cmds}],
        execution_results=[_make_step_result(1)],
    )
    _write_progress_notes(
        orchestration_state=state,
        task=_make_task(),
        prompt="Many commands",
        summary="",
        logger=_make_logger(),
    )
    notes = (tmp_path / ".agent" / "progress_notes.md").read_text()
    cmd_lines = [line for line in notes.splitlines() if line.startswith("- echo")]
    assert len(cmd_lines) == _PROGRESS_NOTES_COMMANDS_CAP


def test_write_progress_notes_accumulates_across_tasks(tmp_path):
    """Second write appends after first; first task's commands remain."""
    for task_num, cmds in [(1, ["npm install"]), (2, ["node -e \"require('./app')\""])]:
        state = _make_state(
            str(tmp_path),
            plan=[{"step_number": 1, "commands": cmds}],
            execution_results=[_make_step_result(1)],
        )
        _write_progress_notes(
            orchestration_state=state,
            task=_make_task(f"Task {task_num}"),
            prompt=f"Task {task_num}",
            summary=f"done {task_num}",
            logger=_make_logger(),
        )
    notes = (tmp_path / ".agent" / "progress_notes.md").read_text()
    assert "npm install" in notes
    assert "node -e" in notes
    assert "Task 1" in notes
    assert "Task 2" in notes


# ---------------------------------------------------------------------------
# Slice H + I integration: commands survive assemble_planning_prompt at 800 chars
# ---------------------------------------------------------------------------


def test_known_good_commands_survive_planning_prompt_after_slice_h(tmp_path):
    """Commands written to progress_notes must reach the assembled planning prompt.

    Reproduces the Task 2+ chain:
    1. Task 1 completes — progress_notes.md written with known_good_commands.
    2. _inject_progress_notes_into_context prepends notes to project_context.
    3. assemble_planning_prompt shapes context at 800-char budget (Slice H).
    4. The command strings must appear in the final planning prompt.
    """
    from types import SimpleNamespace
    from app.services.orchestration.context.assembly import assemble_planning_prompt
    from app.services.orchestration.prompt_templates import OrchestrationState
    from app.tasks.worker_support.context import _inject_progress_notes_into_context

    # Step 1: write progress_notes for a completed task
    notes_dir = tmp_path / ".agent"
    notes_dir.mkdir()
    state_task1 = _make_state(
        str(tmp_path),
        plan=[
            {
                "step_number": 1,
                "commands": ["npm install", "npm test"],
            },
            {
                "step_number": 2,
                "commands": ["node -e \"require('./app')\""],
            },
        ],
        execution_results=[_make_step_result(1), _make_step_result(2)],
    )
    _write_progress_notes(
        orchestration_state=state_task1,
        task=_make_task("Setup project"),
        prompt="Setup the project",
        summary="Installed deps, ran tests",
        logger=_make_logger(),
    )
    assert (tmp_path / ".agent" / "progress_notes.md").exists()

    # Step 2: inject progress_notes into a Task 2 orchestration state
    orch_state = OrchestrationState(
        session_id="test-1",
        task_description="Add feature to the project",
        project_name="test-project",
        project_context="",
        task_id=2,
    )
    orch_state._project_dir_override = str(tmp_path)
    orch_state.phase_history = []
    orch_state.validation_history = []

    inject_logger = _make_logger()
    _inject_progress_notes_into_context(
        orchestration_state=orch_state,
        logger=inject_logger,
    )
    assert (
        "npm install" in orch_state.project_context
    ), "Known good commands not injected into project_context"

    # Step 3: assemble planning prompt
    ctx = SimpleNamespace(
        db=None,
        prompt="Add feature to the project",
        execution_profile="full_lifecycle",
        workflow_profile="default",
        orchestration_state=orch_state,
        session_id=1,
        task_id=2,
    )
    planning_prompt = assemble_planning_prompt(ctx, {})

    # Step 4: assert commands survive the 800-char shaping budget
    assert (
        "npm install" in planning_prompt
    ), "npm install not found in planning prompt — known_good_commands lost at shaping"
    assert (
        "node -e" in planning_prompt
    ), "node -e not found in planning prompt — verification command lost at shaping"
