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
    _INJECTION_BUDGET,
    _extract_active_constraints,
    _extract_known_good_commands,
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
    assert not (tmp_path / ".openclaw" / _FILENAME).exists()


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
    path = tmp_path / ".openclaw" / _FILENAME
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
    data = json.loads((tmp_path / ".openclaw" / _FILENAME).read_text())
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
    data = json.loads((tmp_path / ".openclaw" / _FILENAME).read_text())
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
    data = json.loads((tmp_path / ".openclaw" / _FILENAME).read_text())
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
    data = json.loads((tmp_path / ".openclaw" / _FILENAME).read_text())
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
    openclaw_dir = tmp_path / ".openclaw"
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
    assert "npm install" in result
    assert "package.json" in result


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
    openclaw_dir = tmp_path / ".openclaw"
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
        "implementation_strategy": [],
        "unresolved_failures": [],
    }
    (openclaw_dir / _FILENAME).write_text(json.dumps(fixture))
    result = render_working_memory(tmp_path, _make_logger())
    assert "=== WORKING MEMORY ===" in result
    assert "npm ci" in result
    assert "fixture.py" in result


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
        summary="Created app.js",
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
    assert "node -e" in ctx
    assert "app.js" in ctx
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
    openclaw_dir = tmp_path / ".openclaw"
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
        "implementation_strategy": [],
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
    assert "node -e" in ctx
    assert "index.js" in ctx
    assert "existing context" in ctx
