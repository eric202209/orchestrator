"""Tests for RepoMemory population and injection (flag-gated, default off).

Constraints:
- No live model calls.
- No DB access.
- Uses tmp_path; no production filesystem access.
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.services.orchestration.repo_memory import (
    SCHEMA_VERSION,
    _FILENAME,
    _RENDER_CAP,
    RepoMemory,
    build_repo_memory,
    inject_repo_memory_into_context,
    load_repo_memory,
    render_repo_memory,
    write_repo_memory,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _make_logger() -> MagicMock:
    return MagicMock()


def _read_json(path: Path) -> dict:
    return json.loads(path.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# Schema version
# ---------------------------------------------------------------------------


def test_schema_version_is_1():
    assert SCHEMA_VERSION == 1


# ---------------------------------------------------------------------------
# Initial population
# ---------------------------------------------------------------------------


def test_initial_population_creates_file(tmp_path):
    result = write_repo_memory(tmp_path, _logger=_make_logger())
    assert result is not None
    assert (tmp_path / ".agent" / _FILENAME).exists()


def test_initial_population_file_contains_valid_json(tmp_path):
    write_repo_memory(tmp_path, _logger=_make_logger())
    data = _read_json(tmp_path / ".agent" / _FILENAME)
    assert data["schema_version"] == SCHEMA_VERSION
    assert data["project_dir"] == str(tmp_path)
    assert "last_updated" in data
    assert "invalidation_hashes" in data


def test_write_missing_project_dir_returns_none():
    result = write_repo_memory("/nonexistent/path/xyz123", _logger=_make_logger())
    assert result is None


def test_write_none_project_dir_returns_none():
    result = write_repo_memory(None, _logger=_make_logger())
    assert result is None


# ---------------------------------------------------------------------------
# Project type detection
# ---------------------------------------------------------------------------


def test_python_project_from_requirements_txt(tmp_path):
    (tmp_path / "requirements.txt").write_text("pytest\n")
    rm = build_repo_memory(tmp_path)
    assert rm.project_type == "python"


def test_python_project_from_pyproject_toml(tmp_path):
    (tmp_path / "pyproject.toml").write_text("[build-system]\n")
    rm = build_repo_memory(tmp_path)
    assert rm.project_type == "python"


def test_python_project_from_setup_py(tmp_path):
    (tmp_path / "setup.py").write_text("from setuptools import setup\n")
    rm = build_repo_memory(tmp_path)
    assert rm.project_type == "python"


def test_node_project_from_package_json(tmp_path):
    (tmp_path / "package.json").write_text('{"name": "app"}\n')
    rm = build_repo_memory(tmp_path)
    assert rm.project_type == "node"


def test_mixed_project_when_both_present(tmp_path):
    (tmp_path / "requirements.txt").write_text("pytest\n")
    (tmp_path / "package.json").write_text('{"name": "app"}\n')
    rm = build_repo_memory(tmp_path)
    assert rm.project_type == "mixed"


def test_null_project_type_when_no_markers(tmp_path):
    rm = build_repo_memory(tmp_path)
    assert rm.project_type is None


# ---------------------------------------------------------------------------
# Package manager detection
# ---------------------------------------------------------------------------


def test_poetry_from_poetry_lock(tmp_path):
    (tmp_path / "poetry.lock").write_text("")
    (tmp_path / "pyproject.toml").write_text("[tool.poetry]\n")
    rm = build_repo_memory(tmp_path)
    assert rm.package_manager == "poetry"


def test_pipenv_from_pipfile(tmp_path):
    (tmp_path / "Pipfile").write_text("[packages]\n")
    rm = build_repo_memory(tmp_path)
    assert rm.package_manager == "pipenv"


def test_pip_from_requirements_txt(tmp_path):
    (tmp_path / "requirements.txt").write_text("pytest\n")
    rm = build_repo_memory(tmp_path)
    assert rm.package_manager == "pip"


def test_yarn_from_yarn_lock(tmp_path):
    (tmp_path / "yarn.lock").write_text("")
    (tmp_path / "package.json").write_text('{"name": "app"}\n')
    rm = build_repo_memory(tmp_path)
    assert rm.package_manager == "yarn"


def test_npm_from_package_lock_json(tmp_path):
    (tmp_path / "package-lock.json").write_text("{}")
    (tmp_path / "package.json").write_text('{"name": "app"}\n')
    rm = build_repo_memory(tmp_path)
    assert rm.package_manager == "npm"


def test_npm_from_package_json_only(tmp_path):
    (tmp_path / "package.json").write_text('{"name": "app"}\n')
    rm = build_repo_memory(tmp_path)
    assert rm.package_manager == "npm"


def test_null_package_manager_when_no_markers(tmp_path):
    rm = build_repo_memory(tmp_path)
    assert rm.package_manager is None


# ---------------------------------------------------------------------------
# Test command detection
# ---------------------------------------------------------------------------


def test_pytest_from_pytest_ini(tmp_path):
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    rm = build_repo_memory(tmp_path)
    assert rm.test_command == "pytest"


def test_pytest_from_conftest_py(tmp_path):
    (tmp_path / "conftest.py").write_text("")
    rm = build_repo_memory(tmp_path)
    assert rm.test_command == "pytest"


def test_pytest_for_python_project_with_requirements(tmp_path):
    (tmp_path / "requirements.txt").write_text("pytest\n")
    rm = build_repo_memory(tmp_path)
    assert rm.test_command == "pytest"


def test_npm_test_from_package_json(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    rm = build_repo_memory(tmp_path)
    assert rm.test_command == "npm test"


def test_yarn_test_from_package_json_with_yarn_lock(tmp_path):
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {"test": "jest"}}))
    (tmp_path / "yarn.lock").write_text("")
    rm = build_repo_memory(tmp_path)
    assert rm.test_command == "yarn test"


def test_null_test_command_when_no_markers(tmp_path):
    rm = build_repo_memory(tmp_path)
    assert rm.test_command is None


def test_null_test_command_when_package_json_has_no_test_script(tmp_path):
    # D1 fix: package.json present but no "test" key in scripts → None, not "npm test".
    (tmp_path / "package.json").write_text(
        json.dumps({"scripts": {"build": "vite build", "lint": "eslint ."}})
    )
    rm = build_repo_memory(tmp_path)
    assert rm.test_command is None


def test_null_test_command_when_package_json_has_empty_scripts(tmp_path):
    # D1 fix: scripts section is present but empty.
    (tmp_path / "package.json").write_text(json.dumps({"scripts": {}}))
    rm = build_repo_memory(tmp_path)
    assert rm.test_command is None


def test_null_test_command_when_package_json_has_no_scripts_key(tmp_path):
    # D1 fix: no scripts key at all.
    (tmp_path / "package.json").write_text(
        json.dumps({"name": "app", "version": "1.0.0"})
    )
    rm = build_repo_memory(tmp_path)
    assert rm.test_command is None


# ---------------------------------------------------------------------------
# Entry point cap
# ---------------------------------------------------------------------------


def test_entry_point_cap_at_5(tmp_path):
    # Create 8 entry-point files at the root.
    names = [
        "main.py",
        "manage.py",
        "app.py",
        "setup.py",
        "index.js",
        "index.ts",
        "package.json",
        "pyproject.toml",
    ]
    for name in names:
        (tmp_path / name).write_text("")
    rm = build_repo_memory(tmp_path)
    assert len(rm.entry_points) == 5


def test_entry_points_excludes_excluded_dirs(tmp_path):
    excluded = tmp_path / "venv"
    excluded.mkdir()
    (excluded / "main.py").write_text("")
    rm = build_repo_memory(tmp_path)
    assert all("venv" not in ep for ep in rm.entry_points)


def test_entry_points_excludes_dash_venv_dirs(tmp_path):
    # D3 fix: *-venv directories must be excluded.
    bad = tmp_path / ".graphify-venv"
    bad.mkdir()
    (bad / "main.py").write_text("")
    good = tmp_path / "app"
    good.mkdir()
    (good / "main.py").write_text("real entry point")
    rm = build_repo_memory(tmp_path)
    assert not any(".graphify-venv" in ep for ep in rm.entry_points)
    assert any("app/main.py" in ep for ep in rm.entry_points)


def test_entry_points_excludes_underscore_venv_dirs(tmp_path):
    # D3 fix: *_venv directories must be excluded.
    bad = tmp_path / "my_venv"
    bad.mkdir()
    (bad / "main.py").write_text("")
    good = tmp_path / "app"
    good.mkdir()
    (good / "main.py").write_text("")
    rm = build_repo_memory(tmp_path)
    assert not any("my_venv" in ep for ep in rm.entry_points)
    assert any("app/main.py" in ep for ep in rm.entry_points)


def test_entry_points_excludes_hidden_dirs(tmp_path):
    # D3 fix: directories starting with "." must be excluded.
    bad = tmp_path / ".hidden"
    bad.mkdir()
    (bad / "main.py").write_text("")
    good = tmp_path / "app"
    good.mkdir()
    (good / "main.py").write_text("")
    rm = build_repo_memory(tmp_path)
    assert not any(".hidden" in ep for ep in rm.entry_points)
    assert any("app/main.py" in ep for ep in rm.entry_points)


# ---------------------------------------------------------------------------
# Invalidation hashes
# ---------------------------------------------------------------------------


def test_hash_valid_cache_loads(tmp_path):
    (tmp_path / "requirements.txt").write_text("pytest\n")
    write_repo_memory(tmp_path, _logger=_make_logger())
    loaded = load_repo_memory(tmp_path)
    assert loaded is not None
    assert loaded.project_type == "python"
    assert loaded.package_manager == "pip"


def test_hash_invalid_returns_none_after_file_change(tmp_path):
    (tmp_path / "requirements.txt").write_text("pytest\n")
    write_repo_memory(tmp_path, _logger=_make_logger())
    # Mutate a tracked config file after population.
    (tmp_path / "requirements.txt").write_text("pytest\nrequests\n")
    loaded = load_repo_memory(tmp_path)
    assert loaded is None


def test_new_config_file_invalidates_cache(tmp_path):
    (tmp_path / "requirements.txt").write_text("pytest\n")
    write_repo_memory(tmp_path, _logger=_make_logger())
    # A new tracked config file appears.
    (tmp_path / "pyproject.toml").write_text("[build-system]\n")
    loaded = load_repo_memory(tmp_path)
    assert loaded is None


def test_config_file_deletion_invalidates_cache(tmp_path):
    (tmp_path / "requirements.txt").write_text("pytest\n")
    (tmp_path / "pytest.ini").write_text("[pytest]\n")
    write_repo_memory(tmp_path, _logger=_make_logger())
    # Delete a tracked config file.
    (tmp_path / "pytest.ini").unlink()
    loaded = load_repo_memory(tmp_path)
    assert loaded is None


def test_load_missing_file_returns_none(tmp_path):
    assert load_repo_memory(tmp_path) is None


def test_load_wrong_schema_version_returns_none(tmp_path):
    openclaw = tmp_path / ".agent"
    openclaw.mkdir()
    (openclaw / _FILENAME).write_text(json.dumps({"schema_version": 99}))
    assert load_repo_memory(tmp_path) is None


def test_load_corrupt_json_returns_none(tmp_path):
    openclaw = tmp_path / ".agent"
    openclaw.mkdir()
    (openclaw / _FILENAME).write_text("not valid json {{{")
    assert load_repo_memory(tmp_path) is None


def test_corrupt_file_replaced_safely(tmp_path):
    # Write a corrupt repo_memory.json, then call write_repo_memory.
    openclaw = tmp_path / ".agent"
    openclaw.mkdir()
    (openclaw / _FILENAME).write_text("not valid json {{{")
    result = write_repo_memory(tmp_path, _logger=_make_logger())
    assert result is not None
    data = _read_json(openclaw / _FILENAME)
    assert data["schema_version"] == SCHEMA_VERSION


# ---------------------------------------------------------------------------
# Completion flow integration
# ---------------------------------------------------------------------------


def test_write_repo_memory_called_from_write_progress_notes(tmp_path, monkeypatch):
    """_write_progress_notes must call write_repo_memory after task completion."""
    calls = []

    def _fake_write_repo_memory(project_dir, _logger=None):
        calls.append(project_dir)
        return None

    monkeypatch.setattr(
        "app.services.orchestration.repo_memory.write_repo_memory",
        _fake_write_repo_memory,
    )

    from app.services.orchestration.phases.completion_flow import _write_progress_notes

    state = MagicMock()
    state.project_dir = str(tmp_path)
    state.execution_results = []
    state.changed_files = []
    state.plan = []
    state.validation_history = []

    task = MagicMock()
    task.title = "test task"
    task.id = 1

    _write_progress_notes(
        orchestration_state=state,
        task=task,
        prompt="do something",
        summary="done",
        logger=_make_logger(),
    )

    assert len(calls) == 1
    assert calls[0] == str(tmp_path)


def test_write_progress_notes_completion_unaffected_when_repo_memory_fails(
    tmp_path, monkeypatch
):
    """A write_repo_memory failure must not raise into _write_progress_notes."""

    def _failing_write(project_dir, _logger=None):
        raise RuntimeError("disk full")

    monkeypatch.setattr(
        "app.services.orchestration.repo_memory.write_repo_memory",
        _failing_write,
    )

    from app.services.orchestration.phases.completion_flow import _write_progress_notes

    state = MagicMock()
    state.project_dir = str(tmp_path)
    state.execution_results = []
    state.changed_files = []
    state.plan = []
    state.validation_history = []

    task = MagicMock()
    task.title = "test task"
    task.id = 1

    # Must not raise.
    _write_progress_notes(
        orchestration_state=state,
        task=task,
        prompt="do something",
        summary="done",
        logger=_make_logger(),
    )

    notes = (tmp_path / ".agent" / "progress_notes.md").read_text()
    assert "test task" in notes


# ---------------------------------------------------------------------------
# render_repo_memory
# ---------------------------------------------------------------------------


def _full_memory(tmp_path: Path) -> RepoMemory:
    """Return a fully-populated RepoMemory with all injectable fields set."""
    return RepoMemory(
        schema_version=SCHEMA_VERSION,
        project_dir=str(tmp_path),
        last_updated="2026-06-08T00:00:00+00:00",
        invalidation_hashes={},
        project_type="python",
        package_manager="pip",
        source_root="app/",
        test_root="app/tests/",
        test_command="pytest",
        build_command=None,
        entry_points=["app/main.py"],
        known_config_files=["requirements.txt"],
    )


def test_render_all_fields(tmp_path):
    mem = _full_memory(tmp_path)
    result = render_repo_memory(mem)
    assert result == "[Repo] python · pip · src=app/ · tests=app/tests/ · test=pytest"


def test_render_includes_build_command_when_present(tmp_path):
    mem = _full_memory(tmp_path)
    mem.build_command = "make build"
    result = render_repo_memory(mem)
    assert "build=make build" in result


def test_render_omits_null_fields(tmp_path):
    mem = _full_memory(tmp_path)
    mem.package_manager = None
    mem.test_root = None
    result = render_repo_memory(mem)
    assert "pip" not in result
    assert "tests=" not in result
    assert "python" in result
    assert "test=pytest" in result


def test_render_suppresses_source_root_when_project_type_none(tmp_path):
    # D2 guard: source_root must not appear when project_type is None.
    mem = _full_memory(tmp_path)
    mem.project_type = None
    mem.source_root = "app/"
    result = render_repo_memory(mem)
    assert "src=" not in result


def test_render_suppresses_source_root_for_none_only(tmp_path):
    # D2 guard: source_root must appear for python/node/mixed.
    for pt in ("python", "node", "mixed"):
        mem = _full_memory(tmp_path)
        mem.project_type = pt
        mem.source_root = "src/"
        result = render_repo_memory(mem)
        assert "src=src/" in result, f"Expected src= for project_type={pt}"


def test_render_returns_empty_when_no_stable_facts(tmp_path):
    mem = RepoMemory(
        schema_version=SCHEMA_VERSION,
        project_dir=str(tmp_path),
        last_updated="",
        invalidation_hashes={},
        project_type=None,
        package_manager=None,
        source_root=None,
        test_root=None,
        test_command=None,
        build_command=None,
        entry_points=[],
        known_config_files=[],
    )
    assert render_repo_memory(mem) == ""


def test_render_caps_at_160_chars(tmp_path):
    mem = _full_memory(tmp_path)
    # Make build_command very long to force cap.
    mem.build_command = "make " + "x" * 200
    result = render_repo_memory(mem)
    assert len(result) <= _RENDER_CAP


def test_render_does_not_include_entry_points(tmp_path):
    mem = _full_memory(tmp_path)
    mem.entry_points = ["app/main.py", "manage.py"]
    result = render_repo_memory(mem)
    assert "app/main.py" not in result
    assert "manage.py" not in result


def test_render_node_project(tmp_path):
    mem = RepoMemory(
        schema_version=SCHEMA_VERSION,
        project_dir=str(tmp_path),
        last_updated="",
        invalidation_hashes={},
        project_type="node",
        package_manager="npm",
        source_root="src/",
        test_root=None,
        test_command="npm test",
        build_command=None,
        entry_points=[],
        known_config_files=[],
    )
    result = render_repo_memory(mem)
    assert result == "[Repo] node · npm · src=src/ · test=npm test"


# ---------------------------------------------------------------------------
# inject_repo_memory_into_context
# ---------------------------------------------------------------------------


def _make_state(tmp_path: Path, context: str = "") -> MagicMock:
    state = MagicMock()
    state.project_dir = str(tmp_path)
    state.project_context = context
    return state


def test_inject_prepends_to_project_context(tmp_path):
    (tmp_path / "requirements.txt").write_text("pytest\n")
    write_repo_memory(tmp_path, _logger=_make_logger())
    state = _make_state(tmp_path, "existing context")
    inject_repo_memory_into_context(state, logger=_make_logger())
    assert state.project_context.startswith("[Repo]")
    assert "existing context" in state.project_context


def test_inject_repo_line_appears_before_existing_context(tmp_path):
    (tmp_path / "requirements.txt").write_text("pytest\n")
    write_repo_memory(tmp_path, _logger=_make_logger())
    state = _make_state(tmp_path, "prior context")
    inject_repo_memory_into_context(state, logger=_make_logger())
    repo_pos = state.project_context.index("[Repo]")
    prior_pos = state.project_context.index("prior context")
    assert repo_pos < prior_pos


def test_inject_skips_when_cache_missing(tmp_path):
    state = _make_state(tmp_path, "unchanged")
    inject_repo_memory_into_context(state, logger=_make_logger())
    assert state.project_context == "unchanged"


def test_inject_skips_when_cache_stale(tmp_path):
    (tmp_path / "requirements.txt").write_text("pytest\n")
    write_repo_memory(tmp_path, _logger=_make_logger())
    # Invalidate by mutating a tracked file.
    (tmp_path / "requirements.txt").write_text("pytest\nrequests\n")
    state = _make_state(tmp_path, "unchanged")
    inject_repo_memory_into_context(state, logger=_make_logger())
    assert state.project_context == "unchanged"


def test_inject_skips_when_no_stable_facts(tmp_path):
    # Static HTML project — all detection returns None — render returns "".
    write_repo_memory(tmp_path, _logger=_make_logger())
    state = _make_state(tmp_path, "unchanged")
    inject_repo_memory_into_context(state, logger=_make_logger())
    assert state.project_context == "unchanged"


def test_inject_does_not_raise_on_missing_project_dir():
    state = MagicMock()
    state.project_dir = None
    state.project_context = "safe"
    inject_repo_memory_into_context(state, logger=_make_logger())
    assert state.project_context == "safe"


def test_inject_does_not_raise_when_load_raises(tmp_path, monkeypatch):
    def _explode(*a, **kw):
        raise RuntimeError("disk error")

    monkeypatch.setattr(
        "app.services.orchestration.repo_memory.load_repo_memory", _explode
    )
    state = _make_state(tmp_path, "safe")
    inject_repo_memory_into_context(state, logger=_make_logger())
    assert state.project_context == "safe"


# ---------------------------------------------------------------------------
# Config: REPO_MEMORY_INJECTION_ENABLED default
# ---------------------------------------------------------------------------


def test_repo_memory_injection_flag_default_false():
    # Test the code-level default (False), not the live settings singleton which
    # may be overridden by .env during active validation windows.
    from app.config import Settings

    field = Settings.model_fields.get("REPO_MEMORY_INJECTION_ENABLED")
    assert (
        field is not None
    ), "REPO_MEMORY_INJECTION_ENABLED field missing from Settings"
    assert field.default is False, (
        "REPO_MEMORY_INJECTION_ENABLED code default must remain False; "
        "set True in .env to enable the validation window"
    )
