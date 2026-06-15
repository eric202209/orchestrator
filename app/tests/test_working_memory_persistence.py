"""Tests for WorkingMemory persistence (Slice H), rendering (Slice I), injection (Slice J).

Constraints enforced:
- No live model calls.
- No changes to validator, planning schema, repair logic, or execution.
- All three feature flags default to False.
- Persistence tests use tmp_path; no production filesystem access.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from unittest.mock import MagicMock

import pytest

from app.services.orchestration.working_memory import (
    SCHEMA_VERSION,
    _FILENAME,
    _HUMAN_GUIDANCE_LIMIT,
    _INJECTION_BUDGET,
    _empty_schema,
    _extract_active_constraints,
    _extract_api_contract,
    _extract_known_good_commands,
    _extract_operator_guidance,
    _render_content,
    _render_working_memory_content,
    inject_working_memory_into_context,
    render_working_memory,
    write_working_memory,
)


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_orchestration_state(
    project_dir: str,
    plan: list | None = None,
    changed_files: list | None = None,
    validation_history: list | None = None,
) -> MagicMock:
    state = MagicMock()
    state.project_dir = project_dir
    state.plan = plan or []
    state.changed_files = changed_files or []
    state.validation_history = validation_history or []
    state.project_context = ""
    return state


def _make_task(
    task_id: int = 1, title: str = "test task", plan_position: int = 1
) -> MagicMock:
    task = MagicMock()
    task.id = task_id
    task.title = title
    task.plan_position = plan_position
    return task


def _make_logger() -> MagicMock:
    return MagicMock()


# ---------------------------------------------------------------------------
# Feature flag defaults
# ---------------------------------------------------------------------------


def test_persistence_flag_defaults_false():
    from app.config import settings

    assert settings.WORKING_MEMORY_PERSISTENCE_ENABLED is False, (
        "WORKING_MEMORY_PERSISTENCE_ENABLED must default to False — "
        "persistence must be opt-in"
    )


def test_render_flag_defaults_false():
    from app.config import settings

    assert (
        settings.WORKING_MEMORY_RENDER_ENABLED is False
    ), "WORKING_MEMORY_RENDER_ENABLED must default to False"


def test_injection_flag_defaults_false():
    from app.config import settings

    assert settings.WORKING_MEMORY_INJECTION_ENABLED is False, (
        "WORKING_MEMORY_INJECTION_ENABLED must default to False — "
        "injection must be opt-in"
    )


# ---------------------------------------------------------------------------
# Slice H: write_working_memory
# ---------------------------------------------------------------------------


def test_write_working_memory_no_op_when_flag_off(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", False)
    state = _make_orchestration_state(str(tmp_path))
    task = _make_task()
    write_working_memory(
        orchestration_state=state, task=task, summary="done", logger=_make_logger()
    )
    assert not (tmp_path / ".agent" / _FILENAME).exists()


def test_write_working_memory_creates_file(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True)
    state = _make_orchestration_state(
        str(tmp_path),
        plan=[{"description": "Create file", "commands": ["touch foo.py"]}],
        changed_files=["foo.py"],
    )
    task = _make_task(task_id=42, title="Create foo")
    write_working_memory(
        orchestration_state=state,
        task=task,
        summary="Created foo.py",
        logger=_make_logger(),
    )
    path = tmp_path / ".agent" / _FILENAME
    assert path.exists()
    data = json.loads(path.read_text())
    assert data["schema_version"] == SCHEMA_VERSION
    assert "42" in data["files_by_task"]
    assert data["files_by_task"]["42"]["task_title"] == "Create foo"
    assert "foo.py" in data["files_by_task"]["42"]["added"]


def test_write_working_memory_appends_known_good_commands(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True)
    state = _make_orchestration_state(
        str(tmp_path),
        plan=[
            {"description": "Step 1", "commands": ["npm install", "npm test"]},
            {"description": "Step 2", "commands": ["node -e \"require('./app')\""]},
        ],
    )
    task = _make_task(task_id=10, title="Run tests")
    write_working_memory(
        orchestration_state=state,
        task=task,
        summary="Tests passed",
        logger=_make_logger(),
    )
    data = json.loads((tmp_path / ".agent" / _FILENAME).read_text())
    assert len(data["known_good_commands"]) == 1
    entry = data["known_good_commands"][0]
    assert entry["task_id"] == 10
    assert len(entry["steps"]) == 2
    assert "npm install" in entry["steps"][0]["commands"]
    assert "node -e \"require('./app')\"" in entry["steps"][1]["commands"]


def test_write_working_memory_records_implementation_strategy(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True)
    state = _make_orchestration_state(str(tmp_path))
    task = _make_task(task_id=5, title="Add feature")
    summary = "Used factory pattern to isolate DB calls"
    write_working_memory(
        orchestration_state=state, task=task, summary=summary, logger=_make_logger()
    )
    data = json.loads((tmp_path / ".agent" / _FILENAME).read_text())
    assert len(data["implementation_strategy"]) == 1
    assert data["implementation_strategy"][0]["summary"] == summary


def test_write_working_memory_accumulates_across_tasks(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True)
    for task_id, title in [(1, "Task one"), (2, "Task two")]:
        state = _make_orchestration_state(
            str(tmp_path),
            plan=[{"description": "Do work", "commands": [f"cmd_{task_id}"]}],
            changed_files=[f"file_{task_id}.py"],
        )
        task = _make_task(task_id=task_id, title=title)
        write_working_memory(
            orchestration_state=state,
            task=task,
            summary=f"summary {task_id}",
            logger=_make_logger(),
        )
    data = json.loads((tmp_path / ".agent" / _FILENAME).read_text())
    assert "1" in data["files_by_task"]
    assert "2" in data["files_by_task"]
    assert len(data["known_good_commands"]) == 2
    assert len(data["implementation_strategy"]) == 2


def test_write_working_memory_records_active_constraints(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True)
    state = _make_orchestration_state(
        str(tmp_path),
        validation_history=[
            {"reasons": ["heredoc syntax not allowed", "use node -e for verification"]},
            {
                "reasons": ["heredoc syntax not allowed"]
            },  # duplicate — should not double-add
        ],
    )
    task = _make_task()
    write_working_memory(
        orchestration_state=state, task=task, summary="", logger=_make_logger()
    )
    data = json.loads((tmp_path / ".agent" / _FILENAME).read_text())
    constraints = [c["constraint"] for c in data["active_constraints"]]
    assert "heredoc syntax not allowed" in constraints
    assert "use node -e for verification" in constraints
    assert constraints.count("heredoc syntax not allowed") == 1  # no duplicate


def test_write_working_memory_no_project_dir_is_safe(monkeypatch):
    monkeypatch.setattr("app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True)
    state = MagicMock()
    state.project_dir = None
    logger = _make_logger()
    write_working_memory(
        orchestration_state=state, task=_make_task(), summary="", logger=logger
    )
    logger.warning.assert_not_called()


def test_write_working_memory_corrupted_file_is_replaced(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True)
    openclaw_dir = tmp_path / ".agent"
    openclaw_dir.mkdir()
    (openclaw_dir / _FILENAME).write_text("not valid json")
    state = _make_orchestration_state(
        str(tmp_path),
        plan=[{"description": "Step", "commands": ["ls"]}],
    )
    task = _make_task(task_id=99)
    write_working_memory(
        orchestration_state=state, task=task, summary="ok", logger=_make_logger()
    )
    data = json.loads((openclaw_dir / _FILENAME).read_text())
    assert data["schema_version"] == SCHEMA_VERSION
    assert "99" in data["files_by_task"]


# ---------------------------------------------------------------------------
# Internal extraction helpers
# ---------------------------------------------------------------------------


def test_extract_known_good_commands_empty_plan():
    state = _make_orchestration_state("/tmp/x", plan=[])
    assert _extract_known_good_commands(state) == []


def test_extract_known_good_commands_skips_empty_commands():
    state = _make_orchestration_state(
        "/tmp/x",
        plan=[
            {"description": "Step", "commands": []},
            {"description": "Step 2", "commands": ["echo hi"]},
        ],
    )
    result = _extract_known_good_commands(state)
    assert len(result) == 1
    assert result[0]["commands"] == ["echo hi"]


def test_extract_active_constraints_deduplicates():
    state = _make_orchestration_state(
        "/tmp/x",
        validation_history=[
            {"reasons": ["rule A", "rule B"]},
            {"reasons": ["rule A"]},
        ],
    )
    result = _extract_active_constraints(state)
    assert result.count("rule A") == 1
    assert "rule B" in result


def test_extract_active_constraints_caps_at_20():
    reasons = [f"reason {i}" for i in range(30)]
    state = _make_orchestration_state(
        "/tmp/x",
        validation_history=[{"reasons": reasons}],
    )
    result = _extract_active_constraints(state)
    assert len(result) == 20


# ---------------------------------------------------------------------------
# Slice I: render_working_memory
# ---------------------------------------------------------------------------


def test_render_returns_empty_when_flag_off(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.WORKING_MEMORY_RENDER_ENABLED", False)
    result = render_working_memory(tmp_path, _make_logger())
    assert result == ""


def test_render_returns_empty_when_no_file(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.WORKING_MEMORY_RENDER_ENABLED", True)
    result = render_working_memory(tmp_path, _make_logger())
    assert result == ""


def test_render_produces_block_when_flag_on(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True)
    monkeypatch.setattr("app.config.settings.WORKING_MEMORY_RENDER_ENABLED", True)
    state = _make_orchestration_state(
        str(tmp_path),
        plan=[{"description": "Install deps", "commands": ["npm install"]}],
        changed_files=["package.json"],
    )
    task = _make_task(task_id=3, title="Install")
    write_working_memory(
        orchestration_state=state, task=task, summary="Installed", logger=_make_logger()
    )
    result = render_working_memory(tmp_path, _make_logger())
    assert "=== WORKING MEMORY ===" in result
    assert "=== END WORKING MEMORY ===" in result
    # Implementation Strategy is rendered; commands/files are omitted (redundant with workspace truth)
    assert "Implementation Strategy" in result
    assert "Installed" in result


def test_render_includes_constraints(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True)
    monkeypatch.setattr("app.config.settings.WORKING_MEMORY_RENDER_ENABLED", True)
    state = _make_orchestration_state(
        str(tmp_path),
        validation_history=[{"reasons": ["use node -e for all verification"]}],
    )
    task = _make_task()
    write_working_memory(
        orchestration_state=state, task=task, summary="", logger=_make_logger()
    )
    result = render_working_memory(tmp_path, _make_logger())
    assert "use node -e for all verification" in result


def test_render_includes_implementation_strategy(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True)
    monkeypatch.setattr("app.config.settings.WORKING_MEMORY_RENDER_ENABLED", True)
    state = _make_orchestration_state(str(tmp_path))
    task = _make_task(title="Phase 2")
    write_working_memory(
        orchestration_state=state,
        task=task,
        summary="Used incremental approach for speed",
        logger=_make_logger(),
    )
    result = render_working_memory(tmp_path, _make_logger())
    assert "Used incremental approach for speed" in result


def test_render_only_flag_reads_existing_fixture(tmp_path, monkeypatch):
    """Render flag True + persistence flag False: render reads pre-existing fixture."""
    monkeypatch.setattr("app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", False)
    monkeypatch.setattr("app.config.settings.WORKING_MEMORY_RENDER_ENABLED", True)
    # Write fixture file manually (simulating a previously persisted state)
    openclaw_dir = tmp_path / ".agent"
    openclaw_dir.mkdir()
    fixture: dict = {
        "schema_version": SCHEMA_VERSION,
        "project_dir": str(tmp_path),
        "last_updated": "2026-06-07T00:00:00+00:00",
        "files_by_task": {
            "1": {
                "task_id": 1,
                "task_title": "Fixture task",
                "added": ["fixture.py"],
                "modified": [],
                "deleted": [],
            }
        },
        "known_good_commands": [
            {
                "task_id": 1,
                "task_title": "Fixture task",
                "steps": [{"step": "Install", "commands": ["npm ci"]}],
            }
        ],
        "active_constraints": [],
        "implementation_strategy": [
            {
                "task_id": 1,
                "task_title": "Fixture task",
                "summary": "Fixture bootstrap complete. Created fixture.py with helpers.",
            }
        ],
        "unresolved_failures": [],
    }
    (openclaw_dir / _FILENAME).write_text(json.dumps(fixture))
    result = render_working_memory(tmp_path, _make_logger())
    assert "=== WORKING MEMORY ===" in result
    # Implementation Strategy is rendered first; commands/files omitted from render
    assert "Implementation Strategy" in result
    assert "Fixture bootstrap complete." in result


# ---------------------------------------------------------------------------
# Slice J: inject_working_memory_into_context
# ---------------------------------------------------------------------------


def test_inject_no_op_when_flag_off(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.WORKING_MEMORY_INJECTION_ENABLED", False)
    state = _make_orchestration_state(str(tmp_path))
    state.project_context = "original"
    task = _make_task(plan_position=2)
    inject_working_memory_into_context(
        orchestration_state=state, task=task, logger=_make_logger()
    )
    assert state.project_context == "original"


def test_inject_no_op_for_task_1(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.WORKING_MEMORY_INJECTION_ENABLED", True)
    state = _make_orchestration_state(str(tmp_path))
    state.project_context = "original"
    task = _make_task(plan_position=1)
    inject_working_memory_into_context(
        orchestration_state=state, task=task, logger=_make_logger()
    )
    assert state.project_context == "original"


def test_inject_no_op_when_no_memory_file(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.WORKING_MEMORY_INJECTION_ENABLED", True)
    state = _make_orchestration_state(str(tmp_path))
    state.project_context = "original"
    task = _make_task(plan_position=2)
    inject_working_memory_into_context(
        orchestration_state=state, task=task, logger=_make_logger()
    )
    assert state.project_context == "original"


def test_inject_prepends_block_for_task_2_plus(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True)
    monkeypatch.setattr("app.config.settings.WORKING_MEMORY_INJECTION_ENABLED", True)
    # Write memory for task 1
    state1 = _make_orchestration_state(
        str(tmp_path),
        plan=[{"description": "Step", "commands": ["node -e \"require('./app')\""]}],
        changed_files=["app.js"],
    )
    write_working_memory(
        orchestration_state=state1,
        task=_make_task(task_id=1, title="Task one"),
        summary="Created app.js using module pattern",
        logger=_make_logger(),
    )
    # Inject for task 2
    state2 = _make_orchestration_state(str(tmp_path))
    state2.project_context = "existing context"
    task2 = _make_task(task_id=2, plan_position=2)
    inject_working_memory_into_context(
        orchestration_state=state2, task=task2, logger=_make_logger()
    )
    ctx = state2.project_context
    assert "=== WORKING MEMORY ===" in ctx
    # Implementation Strategy (summary) is rendered; commands/files omitted from render
    assert "Created app.js using module pattern" in ctx
    assert "existing context" in ctx


def test_inject_respects_budget_cap(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True)
    monkeypatch.setattr("app.config.settings.WORKING_MEMORY_INJECTION_ENABLED", True)
    # Write many commands to generate a large render
    state1 = _make_orchestration_state(
        str(tmp_path),
        plan=[
            {"description": f"Step {i}", "commands": [f"echo {'x' * 100} {i}"]}
            for i in range(50)
        ],
        changed_files=[f"file_{i}.py" for i in range(100)],
    )
    write_working_memory(
        orchestration_state=state1,
        task=_make_task(task_id=1),
        summary="big task",
        logger=_make_logger(),
    )
    state2 = _make_orchestration_state(str(tmp_path))
    state2.project_context = ""
    task2 = _make_task(plan_position=2)
    inject_working_memory_into_context(
        orchestration_state=state2, task=task2, logger=_make_logger()
    )
    assert len(state2.project_context) <= _INJECTION_BUDGET


def test_inject_for_task_position_none_is_safe(tmp_path, monkeypatch):
    monkeypatch.setattr("app.config.settings.WORKING_MEMORY_INJECTION_ENABLED", True)
    state = _make_orchestration_state(str(tmp_path))
    state.project_context = "original"
    task = _make_task()
    task.plan_position = None
    inject_working_memory_into_context(
        orchestration_state=state, task=task, logger=_make_logger()
    )
    assert state.project_context == "original"


def test_inject_only_flag_reads_existing_fixture(tmp_path, monkeypatch):
    """Inject flag True + persistence flag False: injection reads pre-existing fixture."""
    monkeypatch.setattr("app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", False)
    monkeypatch.setattr("app.config.settings.WORKING_MEMORY_INJECTION_ENABLED", True)
    # Write fixture file manually (simulating a previously persisted state)
    openclaw_dir = tmp_path / ".agent"
    openclaw_dir.mkdir()
    fixture: dict = {
        "schema_version": SCHEMA_VERSION,
        "project_dir": str(tmp_path),
        "last_updated": "2026-06-07T00:00:00+00:00",
        "files_by_task": {
            "1": {
                "task_id": 1,
                "task_title": "Prior task",
                "added": ["index.js"],
                "modified": [],
                "deleted": [],
            }
        },
        "known_good_commands": [
            {
                "task_id": 1,
                "task_title": "Prior task",
                "steps": [
                    {"step": "Verify", "commands": ["node -e \"require('./index')\""]}
                ],
            }
        ],
        "active_constraints": [],
        "implementation_strategy": [
            {
                "task_id": 1,
                "task_title": "Prior task",
                "summary": "Bootstrap complete. Created index.js with module exports.",
            }
        ],
        "unresolved_failures": [],
    }
    (openclaw_dir / _FILENAME).write_text(json.dumps(fixture))
    state = _make_orchestration_state(str(tmp_path))
    state.project_context = "existing context"
    task2 = _make_task(task_id=2, plan_position=2)
    inject_working_memory_into_context(
        orchestration_state=state, task=task2, logger=_make_logger()
    )
    ctx = state.project_context
    assert "=== WORKING MEMORY ===" in ctx
    # Implementation Strategy (summary) is rendered; commands/files omitted from render
    assert "Bootstrap complete." in ctx
    assert "existing context" in ctx


# ---------------------------------------------------------------------------
# Stage 6: Visibility tests — render order and planning context trim survival
# ---------------------------------------------------------------------------


class TestStage6Visibility:
    """Stage 6: verify Implementation Strategy is first in render and survives the
    400-char planning context trim applied by assemble_planning_prompt."""

    def _wm_with_strategy_and_commands(
        self,
        strategy_summary: str,
        constraint: str = "",
    ) -> dict:
        wm: dict = {
            "schema_version": SCHEMA_VERSION,
            "implementation_strategy": [
                {
                    "task_id": 1,
                    "task_title": "Bootstrap task",
                    "summary": strategy_summary,
                }
            ],
            "known_good_commands": [
                {
                    "task_id": 1,
                    "task_title": "Bootstrap task",
                    "steps": [
                        {
                            "step": "Run tests",
                            "commands": [
                                "PYTHONPATH=src python3 -m pytest tests/ -v",
                                "python3 -m pytest tests/test_parser.py -v",
                            ],
                        }
                    ],
                }
            ],
            "files_by_task": {
                "1": {
                    "task_id": 1,
                    "task_title": "Bootstrap task",
                    "added": [
                        "src/calclib/__init__.py",
                        "src/calclib/parser.py",
                        "tests/test_parser.py",
                        "pytest.ini",
                    ],
                    "modified": [],
                    "deleted": [],
                }
            },
            "active_constraints": [],
            "unresolved_failures": [],
        }
        if constraint:
            wm["active_constraints"] = [
                {
                    "task_id": 1,
                    "constraint": constraint,
                    "source": "validation_rejection",
                }
            ]
        return wm

    def _trim_text(self, text: str, max_chars: int) -> str:
        """Replicate assembly._trim_text to simulate planning context trim."""
        value = " ".join(str(text or "").split())
        if len(value) <= max_chars:
            return value
        return value[: max_chars - 3].rstrip() + "..."

    def test_render_block_starts_with_implementation_strategy(self):
        from app.services.orchestration.working_memory import _render_content

        wm = self._wm_with_strategy_and_commands("parse_number returns a dict.")
        rendered = _render_content(wm)
        # Find section header positions
        impl_pos = rendered.find("Implementation Strategy")
        end_pos = rendered.find("=== END WORKING MEMORY ===")
        assert impl_pos != -1, "Implementation Strategy section missing"
        assert impl_pos < end_pos
        # Implementation Strategy must come before Constraints (if any)
        constraints_pos = rendered.find("Constraints")
        if constraints_pos != -1:
            assert impl_pos < constraints_pos

    def test_constraints_render_after_implementation_strategy(self):
        from app.services.orchestration.working_memory import _render_content

        wm = self._wm_with_strategy_and_commands(
            "Impl strategy text.", constraint="do not use heredoc syntax"
        )
        rendered = _render_content(wm)
        impl_pos = rendered.find("Implementation Strategy")
        constraints_pos = rendered.find("Constraints")
        assert impl_pos != -1
        assert constraints_pos != -1
        assert (
            impl_pos < constraints_pos
        ), "Implementation Strategy must appear before Constraints in rendered block"

    def test_known_good_commands_stored_but_not_rendered(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True
        )
        monkeypatch.setattr("app.config.settings.WORKING_MEMORY_RENDER_ENABLED", True)
        state = _make_orchestration_state(
            str(tmp_path),
            plan=[{"description": "Run tests", "commands": ["pytest tests/ -v"]}],
            changed_files=["src/parser.py"],
        )
        write_working_memory(
            orchestration_state=state,
            task=_make_task(task_id=1, title="Bootstrap"),
            summary="Implemented parser.",
            logger=_make_logger(),
        )
        # Stored in JSON
        data = json.loads((tmp_path / ".agent" / _FILENAME).read_text())
        assert len(data["known_good_commands"]) == 1
        assert (
            "pytest tests/ -v" in data["known_good_commands"][0]["steps"][0]["commands"]
        )
        # Not in rendered block
        rendered = render_working_memory(tmp_path, _make_logger())
        assert "pytest tests/ -v" not in rendered
        assert "Known Good Commands" not in rendered

    def test_recent_files_stored_but_not_rendered(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True
        )
        monkeypatch.setattr("app.config.settings.WORKING_MEMORY_RENDER_ENABLED", True)
        state = _make_orchestration_state(
            str(tmp_path),
            changed_files=["src/calclib/parser.py", "tests/test_parser.py"],
        )
        write_working_memory(
            orchestration_state=state,
            task=_make_task(task_id=1, title="Bootstrap"),
            summary="Implemented parser module.",
            logger=_make_logger(),
        )
        # Stored in JSON
        data = json.loads((tmp_path / ".agent" / _FILENAME).read_text())
        assert "src/calclib/parser.py" in data["files_by_task"]["1"]["added"]
        # Not in rendered block
        rendered = render_working_memory(tmp_path, _make_logger())
        assert "src/calclib/parser.py" not in rendered
        assert "Recent Files" not in rendered

    def test_implementation_strategy_survives_400_char_trim(self):
        from app.services.orchestration.working_memory import _render_content

        # Use 400 A's as strategy text to fill the render budget
        strategy_text = "A" * 400
        wm = self._wm_with_strategy_and_commands(strategy_text)
        rendered = _render_content(wm)
        trimmed = self._trim_text(rendered, 400)
        surviving_a_count = trimmed.count("A")
        assert surviving_a_count >= 300, (
            f"Only {surviving_a_count} chars of implementation_strategy survived "
            "the 400-char planning context trim (need >= 300)"
        )

    def test_implementation_strategy_visible_in_trimmed_block(self):
        from app.services.orchestration.working_memory import _render_content

        strategy_text = (
            "parse_number(text: str) -> dict returns ok/value/error fields. "
            "For valid integers ok=True and value holds the int. "
            "For invalid input ok=False and error='INVALID_NUMBER'. "
            "Never raises an exception. Import from src/calclib/parser.py."
        )
        wm = self._wm_with_strategy_and_commands(strategy_text)
        rendered = _render_content(wm)
        trimmed = self._trim_text(rendered, 400)
        assert "Implementation Strategy" in trimmed
        assert "parse_number" in trimmed
        # Known Good Commands and Recent Files must not appear in trimmed block
        assert "Known Good Commands" not in trimmed
        assert "Recent Files" not in trimmed


# ---------------------------------------------------------------------------
# API Contract render-first tests (Stage: render-first fix)
# ---------------------------------------------------------------------------

# Realistic parser summary matching actual LLM output from the calclib pilot.
_PARSER_SUMMARY = (
    "Task Summary:\n"
    "Implemented `parse_amount` in `src/calclib/parser.py` with strict integer "
    "validation, error codes, and boundary checks, along with package initialization, "
    "pytest configuration, and comprehensive test suite.\n\n"
    "API Contract:\n"
    "- function: parse_amount(text: str) -> dict\n"
    '- failure return: {"ok": False, "code": str}\n'
    '- success return: {"ok": True, "value": int}\n'
    "- keys/fields: ok, code, value\n"
    '- sentinel/error values: "EMPTY", "FORMAT", "OVERFLOW"\n'
    "- exception behavior: never raises for invalid input\n\n"
    "Changed Files:\n"
    "- src/calclib/__init__.py\n"
    "- src/calclib/parser.py\n"
    "- pytest.ini\n"
    "- tests/test_parser.py\n"
)


def _trim_text_400(text: str) -> str:
    """Replicate _shape_project_context._trim_text at 400-char planning cap."""
    value = " ".join(str(text or "").split())
    if len(value) <= 400:
        return value
    return value[:397].rstrip() + "..."


class TestApiContractRenderFirst:
    """Render-first fix: API Contract section appears before prose in WM block,
    ensuring key fields survive the 400-char planning context trim."""

    # ------------------------------------------------------------------
    # _extract_api_contract unit tests
    # ------------------------------------------------------------------

    def test_extract_api_contract_found(self):
        api_block, prose = _extract_api_contract(_PARSER_SUMMARY)
        assert api_block.startswith("API Contract:")
        assert '- failure return: {"ok": False, "code": str}' in api_block
        assert '- success return: {"ok": True, "value": int}' in api_block
        assert "EMPTY" in api_block
        assert "FORMAT" in api_block
        assert "OVERFLOW" in api_block

    def test_extract_api_contract_ends_at_next_section(self):
        api_block, prose = _extract_api_contract(_PARSER_SUMMARY)
        # 'Changed Files:' is the next section header — must not appear in api_block
        assert "Changed Files:" not in api_block

    def test_extract_api_contract_prose_contains_task_summary(self):
        api_block, prose = _extract_api_contract(_PARSER_SUMMARY)
        assert "Implemented `parse_amount`" in prose

    def test_extract_api_contract_not_present_returns_empty(self):
        summary = "Used incremental approach for speed. No API details captured."
        api_block, prose = _extract_api_contract(summary)
        assert api_block == ""
        assert prose == summary

    def test_extract_api_contract_at_start_of_summary(self):
        summary = (
            "API Contract:\n"
            "- function: f(x: int) -> bool\n"
            "- failure return: False\n"
        )
        api_block, prose = _extract_api_contract(summary)
        assert api_block.startswith("API Contract:")
        assert "f(x: int) -> bool" in api_block

    # ------------------------------------------------------------------
    # _render_content ordering tests
    # ------------------------------------------------------------------

    def test_api_contract_renders_before_prose(self):
        from app.services.orchestration.working_memory import _render_content

        wm = {
            "schema_version": SCHEMA_VERSION,
            "implementation_strategy": [
                {
                    "task_id": 1,
                    "task_title": "Bootstrap parse_amount parser",
                    "summary": _PARSER_SUMMARY,
                }
            ],
            "active_constraints": [],
            "known_good_commands": [],
            "files_by_task": {},
            "unresolved_failures": [],
        }
        rendered = _render_content(wm)
        api_pos = rendered.find("API Contract:")
        prose_pos = rendered.find("Implemented `parse_amount`")
        assert api_pos != -1, "API Contract: section missing from rendered block"
        assert prose_pos != -1, "Prose summary missing from rendered block"
        assert (
            api_pos < prose_pos
        ), f"API Contract ({api_pos}) must appear before prose ({prose_pos})"

    def test_code_visible_within_250_chars(self):
        from app.services.orchestration.working_memory import _render_content

        wm = {
            "schema_version": SCHEMA_VERSION,
            "implementation_strategy": [
                {
                    "task_id": 1,
                    "task_title": "Bootstrap parse_amount parser",
                    "summary": _PARSER_SUMMARY,
                }
            ],
            "active_constraints": [],
            "known_good_commands": [],
            "files_by_task": {},
            "unresolved_failures": [],
        }
        rendered = _render_content(wm)
        collapsed = " ".join(rendered.split())
        code_pos = collapsed.find('"code"')
        assert code_pos != -1, '"code" key not found in rendered block'
        assert code_pos < 250, (
            f'"code" key at char {code_pos} — must be within first 250 chars '
            f"of collapsed rendered block (was {code_pos})"
        )

    def test_sentinels_visible_within_400_chars(self):
        from app.services.orchestration.working_memory import _render_content

        wm = {
            "schema_version": SCHEMA_VERSION,
            "implementation_strategy": [
                {
                    "task_id": 1,
                    "task_title": "Bootstrap parse_amount parser",
                    "summary": _PARSER_SUMMARY,
                }
            ],
            "active_constraints": [],
            "known_good_commands": [],
            "files_by_task": {},
            "unresolved_failures": [],
        }
        rendered = _render_content(wm)
        collapsed = " ".join(rendered.split())
        for sentinel in ("EMPTY", "FORMAT", "OVERFLOW"):
            pos = collapsed.find(sentinel)
            assert pos != -1, f"{sentinel} missing from rendered block"
            assert pos < 400, (
                f"{sentinel} at char {pos} — must be within first 400 chars "
                f"of collapsed rendered block"
            )

    def test_no_api_contract_falls_back_to_existing_render(self):
        from app.services.orchestration.working_memory import _render_content

        summary = "Used incremental approach for speed. Created parser.py."
        wm = {
            "schema_version": SCHEMA_VERSION,
            "implementation_strategy": [
                {"task_id": 1, "task_title": "Bootstrap", "summary": summary}
            ],
            "active_constraints": [],
            "known_good_commands": [],
            "files_by_task": {},
            "unresolved_failures": [],
        }
        rendered = _render_content(wm)
        assert "API Contract:" not in rendered
        assert "Used incremental approach for speed" in rendered
        assert "Implementation Strategy" in rendered

    def test_active_constraints_visible_near_top(self):
        from app.services.orchestration.working_memory import _render_content

        wm = {
            "schema_version": SCHEMA_VERSION,
            "implementation_strategy": [
                {
                    "task_id": 1,
                    "task_title": "Bootstrap parse_amount parser",
                    "summary": _PARSER_SUMMARY,
                }
            ],
            "active_constraints": [
                {
                    "task_id": 1,
                    "constraint": "use PYTHONPATH=src for pytest",
                    "source": "validation_rejection",
                }
            ],
            "known_good_commands": [],
            "files_by_task": {},
            "unresolved_failures": [],
        }
        rendered = _render_content(wm)
        impl_pos = rendered.find("Implementation Strategy")
        constraints_pos = rendered.find("use PYTHONPATH=src for pytest")
        assert constraints_pos != -1, "Constraint missing from rendered block"
        # Constraints render after Implementation Strategy (which contains API Contract)
        assert impl_pos < constraints_pos
        # Constraint is visible within 600 chars of collapsed block
        collapsed = " ".join(rendered.split())
        c_pos = collapsed.find("use PYTHONPATH=src for pytest")
        assert c_pos < 600, f"Constraint at char {c_pos} — should be within 600 chars"

    def test_known_good_commands_stored_not_rendered_with_api_contract(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True
        )
        monkeypatch.setattr("app.config.settings.WORKING_MEMORY_RENDER_ENABLED", True)
        state = _make_orchestration_state(
            str(tmp_path),
            plan=[{"description": "Run tests", "commands": ["pytest tests/ -v"]}],
        )
        write_working_memory(
            orchestration_state=state,
            task=_make_task(task_id=1, title="Bootstrap"),
            summary=_PARSER_SUMMARY,
            logger=_make_logger(),
        )
        data = json.loads((tmp_path / ".agent" / _FILENAME).read_text())
        # known_good_commands are stored
        assert len(data["known_good_commands"]) == 1
        # API contract is in stored summary
        assert "API Contract:" in data["implementation_strategy"][0]["summary"]
        # Rendered block: API Contract first, commands not rendered
        rendered = render_working_memory(tmp_path, _make_logger())
        assert "API Contract:" in rendered
        assert "pytest tests/ -v" not in rendered

    def test_400_char_trim_shows_failure_return_and_sentinels(self):
        """End-to-end: after trim, failure return and sentinels are all visible."""
        from app.services.orchestration.working_memory import _render_content

        wm = {
            "schema_version": SCHEMA_VERSION,
            "implementation_strategy": [
                {
                    "task_id": 1,
                    "task_title": "Bootstrap parse_amount parser",
                    "summary": _PARSER_SUMMARY,
                }
            ],
            "active_constraints": [],
            "known_good_commands": [],
            "files_by_task": {},
            "unresolved_failures": [],
        }
        rendered = _render_content(wm)
        trimmed = _trim_text_400(rendered)

        assert "failure return" in trimmed, "failure return not in 400-char trim"
        assert "EMPTY" in trimmed, "EMPTY sentinel not in 400-char trim"
        assert "FORMAT" in trimmed, "FORMAT sentinel not in 400-char trim"
        assert "OVERFLOW" in trimmed, "OVERFLOW sentinel not in 400-char trim"
        # code key fully visible (not truncated)
        code_idx = trimmed.find('"code"')
        assert (
            code_idx != -1 and code_idx < 250
        ), f'"code" key at char {code_idx} in trimmed block — must be < 250'


# ---------------------------------------------------------------------------
# human_guidance: persistence, deduplication, bounding, render order
# ---------------------------------------------------------------------------


def _make_mock_log_entry(message: str, task_id: int = 1, created_at: Any = None) -> Any:
    entry = MagicMock()
    entry.message = message
    entry.task_id = task_id
    entry.created_at = created_at
    return entry


def _make_mock_db(entries: list) -> Any:
    """Return a mock db whose query chain returns `entries`."""
    mock_query = MagicMock()
    mock_query.filter.return_value = mock_query
    mock_query.order_by.return_value = mock_query
    mock_query.all.return_value = entries
    mock_db = MagicMock()
    mock_db.query.return_value = mock_query
    return mock_db


class TestHumanGuidance:
    # 1. Empty schema includes human_guidance: []
    def test_empty_schema_includes_human_guidance(self):
        schema = _empty_schema("/tmp/proj")
        assert "human_guidance" in schema
        assert schema["human_guidance"] == []

    # 2. When no operator guidance exists (db=None), WM writes with empty field
    def test_no_guidance_when_db_is_none(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True
        )
        state = _make_orchestration_state(str(tmp_path))
        state.session_id = 42
        task = _make_task(task_id=1)
        write_working_memory(
            orchestration_state=state,
            task=task,
            summary="done",
            logger=_make_logger(),
            db=None,
        )
        data = json.loads((tmp_path / ".agent" / _FILENAME).read_text())
        assert "human_guidance" in data
        assert data["human_guidance"] == []

    # 3. One [OPERATOR_GUIDANCE] message is persisted to human_guidance
    def test_one_guidance_message_persisted(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True
        )
        entry = _make_mock_log_entry(
            "[OPERATOR_GUIDANCE] Never use mutable default arguments", task_id=1
        )
        mock_db = _make_mock_db([entry])
        state = _make_orchestration_state(str(tmp_path))
        state.session_id = 7
        task = _make_task(task_id=1)
        write_working_memory(
            orchestration_state=state,
            task=task,
            summary="done",
            logger=_make_logger(),
            db=mock_db,
        )
        data = json.loads((tmp_path / ".agent" / _FILENAME).read_text())
        assert len(data["human_guidance"]) == 1
        assert (
            data["human_guidance"][0]["message"]
            == "Never use mutable default arguments"
        )
        assert data["human_guidance"][0]["source"] == "operator_guidance"

    # 4. Duplicate messages are deduplicated
    def test_duplicate_guidance_messages_deduplicated(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True
        )
        msg = "[OPERATOR_GUIDANCE] Never use mutable default arguments"
        entries = [
            _make_mock_log_entry(msg, task_id=1),
            _make_mock_log_entry(msg, task_id=1),
        ]
        mock_db = _make_mock_db(entries)
        state = _make_orchestration_state(str(tmp_path))
        state.session_id = 7
        task = _make_task(task_id=1)
        write_working_memory(
            orchestration_state=state,
            task=task,
            summary="done",
            logger=_make_logger(),
            db=mock_db,
        )
        data = json.loads((tmp_path / ".agent" / _FILENAME).read_text())
        messages = [g["message"] for g in data["human_guidance"]]
        assert messages.count("Never use mutable default arguments") == 1

    # 5. More than 10 messages are bounded to latest 10
    def test_guidance_bounded_to_limit(self, tmp_path, monkeypatch):
        monkeypatch.setattr(
            "app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True
        )
        entries = [
            _make_mock_log_entry(f"[OPERATOR_GUIDANCE] Rule {i}", task_id=1)
            for i in range(15)
        ]
        mock_db = _make_mock_db(entries)
        state = _make_orchestration_state(str(tmp_path))
        state.session_id = 7
        task = _make_task(task_id=1)
        write_working_memory(
            orchestration_state=state,
            task=task,
            summary="done",
            logger=_make_logger(),
            db=mock_db,
        )
        data = json.loads((tmp_path / ".agent" / _FILENAME).read_text())
        assert len(data["human_guidance"]) == _HUMAN_GUIDANCE_LIMIT

    # 6. human_guidance renders before Implementation Strategy and active_constraints
    def test_human_guidance_renders_before_constraints(self):
        wm = {
            "schema_version": SCHEMA_VERSION,
            "implementation_strategy": [
                {"task_id": 1, "task_title": "T1", "summary": "Used factory pattern."}
            ],
            "human_guidance": [
                {
                    "task_id": 1,
                    "message": "Never use mutable defaults",
                    "created_at": "",
                    "source": "operator_guidance",
                }
            ],
            "active_constraints": [
                {
                    "task_id": 1,
                    "constraint": "no heredoc syntax",
                    "source": "validation_rejection",
                }
            ],
            "known_good_commands": [],
            "files_by_task": {},
            "unresolved_failures": [],
        }
        rendered = _render_content(wm)
        guidance_pos = rendered.find("Never use mutable defaults")
        constraint_pos = rendered.find("no heredoc syntax")
        impl_pos = rendered.find("Implementation Strategy")
        assert guidance_pos != -1, "human_guidance message missing from render"
        assert constraint_pos != -1, "constraint missing from render"
        assert guidance_pos < impl_pos < constraint_pos, (
            f"Expected order: human_guidance ({guidance_pos}) < "
            f"Implementation Strategy ({impl_pos}) < constraints ({constraint_pos})"
        )

    # 7. HG is first section when present — visible within 250 chars of rendered block
    def test_human_guidance_visible_within_250_chars(self):
        """Regression: HG must survive the 400-char planning-context trim.

        With a full 7-field API Contract in IS, the old IS-first order pushed
        HG to char ~451 (collapsed) — past the 400-char budget, invisible.
        The new HG-first order puts it at char ~42, well within 250.
        """
        full_summary = (
            "Task Summary:\n"
            "Created the `utiltools` Python package with a `normalize_name` function.\n\n"
            "API Contract:\n"
            "- function: normalize_name(name: str) -> str\n"
            "- failure return: N/A\n"
            "- success return: str\n"
            "- keys/fields: N/A\n"
            "- sentinel/error values: N/A\n"
            "- exception behavior: never raises for invalid input\n\n"
            "Changed Files:\n"
            "- utiltools/__init__.py\n"
            "- utiltools/core.py\n"
        )
        wm = {
            "schema_version": SCHEMA_VERSION,
            "implementation_strategy": [
                {
                    "task_id": 1,
                    "task_title": "Create utiltools package",
                    "summary": full_summary,
                }
            ],
            "human_guidance": [
                {
                    "task_id": 1,
                    "message": "use dataclasses for all structured records",
                    "created_at": "",
                    "source": "operator_guidance",
                }
            ],
            "active_constraints": [],
            "known_good_commands": [],
            "files_by_task": {},
            "unresolved_failures": [],
        }
        rendered = _render_content(wm)
        collapsed = " ".join(rendered.split())
        hg_pos = collapsed.find("use dataclasses for all structured records")
        assert hg_pos != -1, "HG message missing from render"
        assert hg_pos < 250, (
            f"HG message at collapsed char {hg_pos} — must be < 250 to survive "
            f"the 400-char planning context trim (WM header ~22 chars prefix)"
        )

    # 8. When HG absent, IS is still rendered first (API Contract pilot backwards-compat)
    def test_implementation_strategy_first_when_no_human_guidance(self):
        wm = {
            "schema_version": SCHEMA_VERSION,
            "implementation_strategy": [
                {
                    "task_id": 1,
                    "task_title": "T1",
                    "summary": "API Contract:\n- function: f() -> str\n",
                }
            ],
            "human_guidance": [],
            "active_constraints": [
                {
                    "task_id": 1,
                    "constraint": "no mutable defaults",
                    "source": "validation_rejection",
                }
            ],
            "known_good_commands": [],
            "files_by_task": {},
            "unresolved_failures": [],
        }
        rendered = _render_content(wm)
        is_pos = rendered.find("Implementation Strategy")
        constraint_pos = rendered.find("no mutable defaults")
        assert is_pos != -1, "Implementation Strategy missing"
        assert constraint_pos != -1, "Constraint missing"
        assert (
            is_pos < constraint_pos
        ), "IS must come before Constraints when HG is absent"
        assert (
            rendered.find("Operator Guidance") == -1
        ), "Operator Guidance header must be absent when no HG"

    # 9. human_guidance is not rendered when render flag is off
    def test_human_guidance_not_rendered_when_flag_off(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.config.settings.WORKING_MEMORY_RENDER_ENABLED", False)
        openclaw_dir = tmp_path / ".agent"
        openclaw_dir.mkdir()
        wm_data = {
            "schema_version": SCHEMA_VERSION,
            "project_dir": str(tmp_path),
            "last_updated": "",
            "files_by_task": {},
            "known_good_commands": [],
            "active_constraints": [],
            "human_guidance": [
                {
                    "task_id": 1,
                    "message": "Never use mutable defaults",
                    "created_at": "",
                    "source": "operator_guidance",
                }
            ],
            "implementation_strategy": [],
            "unresolved_failures": [],
        }
        (openclaw_dir / _FILENAME).write_text(json.dumps(wm_data))
        result = render_working_memory(tmp_path, _make_logger())
        assert result == ""

    # 8. human_guidance is not written to progress_notes
    def test_human_guidance_not_in_progress_notes(self, tmp_path, monkeypatch):
        from app.services.orchestration.phases.completion_flow import (
            _write_progress_notes,
        )

        state = _make_orchestration_state(str(tmp_path))
        state.execution_results = []
        task = _make_task()
        _write_progress_notes(
            orchestration_state=state,
            task=task,
            prompt="Create a package",
            summary="Used factory pattern",
            logger=_make_logger(),
        )
        notes_path = tmp_path / ".agent" / "progress_notes.md"
        if notes_path.exists():
            content = notes_path.read_text()
            assert "human_guidance" not in content
            assert "OPERATOR_GUIDANCE" not in content
            assert "Operator Guidance" not in content

    # 9. _extract_operator_guidance returns empty list when db is None
    def test_extract_operator_guidance_no_db(self):
        result = _extract_operator_guidance(None, session_id=1, task_id=1)
        assert result == []

    # 10. _extract_operator_guidance strips the [OPERATOR_GUIDANCE] prefix
    def test_extract_operator_guidance_strips_prefix(self):
        entry = _make_mock_log_entry(
            "[OPERATOR_GUIDANCE] Use None for defaults", task_id=1
        )
        entry.created_at = None
        mock_db = _make_mock_db([entry])
        result = _extract_operator_guidance(mock_db, session_id=5, task_id=1)
        assert len(result) == 1
        assert result[0]["message"] == "Use None for defaults"
        assert result[0]["source"] == "operator_guidance"

    # 11. human_guidance renders with Operator Guidance header
    def test_human_guidance_renders_with_header(self):
        wm = {
            "schema_version": SCHEMA_VERSION,
            "implementation_strategy": [],
            "human_guidance": [
                {
                    "task_id": 1,
                    "message": "Always use type hints",
                    "created_at": "",
                    "source": "operator_guidance",
                }
            ],
            "active_constraints": [],
            "known_good_commands": [],
            "files_by_task": {},
            "unresolved_failures": [],
        }
        rendered = _render_content(wm)
        assert "Operator Guidance" in rendered
        assert "Always use type hints" in rendered
        assert "=== WORKING MEMORY ===" in rendered

    # 12. Existing tests still pass — write then render includes all existing fields
    def test_existing_persistence_unaffected_by_human_guidance(
        self, tmp_path, monkeypatch
    ):
        monkeypatch.setattr(
            "app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True
        )
        monkeypatch.setattr("app.config.settings.WORKING_MEMORY_RENDER_ENABLED", True)
        state = _make_orchestration_state(
            str(tmp_path),
            plan=[{"description": "Step 1", "commands": ["pytest"]}],
            changed_files=["app.py"],
            validation_history=[{"reasons": ["use PYTHONPATH=src"]}],
        )
        state.session_id = 99
        task = _make_task(task_id=3, title="Test task")
        write_working_memory(
            orchestration_state=state,
            task=task,
            summary="Done with pytest",
            logger=_make_logger(),
            db=None,
        )
        data = json.loads((tmp_path / ".agent" / _FILENAME).read_text())
        assert "3" in data["files_by_task"]
        assert len(data["known_good_commands"]) == 1
        assert len(data["implementation_strategy"]) == 1
        assert len(data["active_constraints"]) == 1
        assert data["human_guidance"] == []
        rendered = render_working_memory(tmp_path, _make_logger())
        assert "Implementation Strategy" in rendered
        assert "Constraints" in rendered
        assert "Done with pytest" in rendered
        assert "use PYTHONPATH=src" in rendered
