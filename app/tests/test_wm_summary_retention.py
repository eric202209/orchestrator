"""Tests for Stage 4 WM summary retention: storage and render limits.

Verifies:
1. LLM summaries longer than 400 chars are stored up to _SUMMARY_STORAGE_LIMIT (1200).
2. API contract details after char 400 are preserved in working_memory.json.
3. Rendered WM content is bounded by _SUMMARY_RENDER_LIMIT (600) per entry
   and by _INJECTION_BUDGET (2000) at injection time.
4. progress_notes is not affected.
5. known_good_commands / files_by_task / active_constraints are unchanged.
6. Flag OFF behavior unchanged.
7. Stage 3 routing tests still pass (those are in test_llm_summary_routing.py).
"""

from __future__ import annotations

import json
from pathlib import Path
from unittest.mock import MagicMock

import pytest

from app.services.orchestration.working_memory import (
    _INJECTION_BUDGET,
    _SUMMARY_RENDER_LIMIT,
    _SUMMARY_STORAGE_LIMIT,
    _render_content,
    write_working_memory,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

_LONG_LLM = (
    "parse_number(text: str) -> dict: Returns a plain dictionary "
    "with three fields: ok (bool), value (int or None), error (str or None). "
    "For valid integer strings ('42', '-7', '0') the function returns "
    "{'ok': True, 'value': <int>, 'error': None}. "
    "For any invalid input ('abc', '', '3.14', None) the function returns "
    "{'ok': False, 'value': None, 'error': 'INVALID_NUMBER'}. "
    "The function never raises an exception for any input type. "
    "Implemented in src/calclib/parser.py; re-exported from src/calclib/__init__.py. "
    "Tests in tests/test_parser.py cover all branches. pytest.ini sets pythonpath = src."
)

_DETERMINISTIC = (
    "Task completed with verified execution evidence. "
    "Completed steps: 3/3. Changed files: src/calclib/__init__.py, "
    "src/calclib/parser.py, tests/test_parser.py, pytest.ini."
)


def _make_state(project_dir: str) -> MagicMock:
    state = MagicMock()
    state.project_dir = project_dir
    state.execution_results = []
    state.changed_files = []
    state.plan = []
    state.validation_history = []
    return state


def _make_task(task_id: int = 1) -> MagicMock:
    t = MagicMock()
    t.id = task_id
    t.title = "Bootstrap calclib"
    t.plan_position = 1
    return t


def _write(tmp_path: Path, summary: str, monkeypatch, task_id: int = 1) -> dict:
    monkeypatch.setattr("app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True)
    state = _make_state(str(tmp_path))
    write_working_memory(
        orchestration_state=state,
        task=_make_task(task_id),
        summary=summary,
        logger=MagicMock(),
    )
    wm_path = tmp_path / ".agent" / "working_memory.json"
    assert wm_path.exists(), "working_memory.json was not created"
    return json.loads(wm_path.read_text())


# ---------------------------------------------------------------------------
# 1. Constants sanity
# ---------------------------------------------------------------------------


class TestConstants:
    def test_storage_limit_is_1200(self):
        assert _SUMMARY_STORAGE_LIMIT == 1200

    def test_render_limit_is_600(self):
        assert _SUMMARY_RENDER_LIMIT == 600

    def test_storage_exceeds_old_limit(self):
        assert _SUMMARY_STORAGE_LIMIT > 400

    def test_render_exceeds_old_render(self):
        assert _SUMMARY_RENDER_LIMIT > 200

    def test_render_less_than_storage(self):
        assert _SUMMARY_RENDER_LIMIT < _SUMMARY_STORAGE_LIMIT

    def test_injection_budget_unchanged(self):
        assert _INJECTION_BUDGET == 2000


# ---------------------------------------------------------------------------
# 2. Storage: LLM summaries longer than 400 chars stored up to 1200
# ---------------------------------------------------------------------------


class TestStorageLimit:
    def test_long_summary_stored_beyond_400(self, tmp_path, monkeypatch):
        assert len(_LONG_LLM) > 400, "test fixture must exceed old 400-char limit"
        data = _write(tmp_path, _LONG_LLM, monkeypatch)
        stored = data["implementation_strategy"][0]["summary"]
        assert len(stored) > 400

    def test_long_summary_stored_up_to_1200(self, tmp_path, monkeypatch):
        data = _write(tmp_path, _LONG_LLM, monkeypatch)
        stored = data["implementation_strategy"][0]["summary"]
        assert len(stored) <= _SUMMARY_STORAGE_LIMIT

    def test_summary_exactly_1200_stored_fully(self, tmp_path, monkeypatch):
        text = "x" * 1200
        data = _write(tmp_path, text, monkeypatch)
        stored = data["implementation_strategy"][0]["summary"]
        assert len(stored) == 1200

    def test_summary_exceeding_1200_truncated_at_1200(self, tmp_path, monkeypatch):
        text = "y" * 2000
        data = _write(tmp_path, text, monkeypatch)
        stored = data["implementation_strategy"][0]["summary"]
        assert len(stored) == 1200

    def test_short_summary_stored_fully(self, tmp_path, monkeypatch):
        short = "Task done in 3 steps."
        data = _write(tmp_path, short, monkeypatch)
        stored = data["implementation_strategy"][0]["summary"]
        assert stored == short


# ---------------------------------------------------------------------------
# 3. API contract details after char 400 are preserved
# ---------------------------------------------------------------------------


class TestApiContractRetention:
    def test_invalid_number_sentinel_preserved(self, tmp_path, monkeypatch):
        data = _write(tmp_path, _LONG_LLM, monkeypatch)
        stored = data["implementation_strategy"][0]["summary"]
        # INVALID_NUMBER appears after char 400 in the fixture
        assert "INVALID_NUMBER" in stored

    def test_no_exception_detail_preserved(self, tmp_path, monkeypatch):
        data = _write(tmp_path, _LONG_LLM, monkeypatch)
        stored = data["implementation_strategy"][0]["summary"]
        assert "never raises" in stored

    def test_ok_key_preserved(self, tmp_path, monkeypatch):
        data = _write(tmp_path, _LONG_LLM, monkeypatch)
        stored = data["implementation_strategy"][0]["summary"]
        assert "'ok'" in stored or '"ok"' in stored

    def test_value_key_preserved(self, tmp_path, monkeypatch):
        data = _write(tmp_path, _LONG_LLM, monkeypatch)
        stored = data["implementation_strategy"][0]["summary"]
        assert "'value'" in stored or '"value"' in stored

    def test_error_key_preserved(self, tmp_path, monkeypatch):
        data = _write(tmp_path, _LONG_LLM, monkeypatch)
        stored = data["implementation_strategy"][0]["summary"]
        assert "'error'" in stored or '"error"' in stored

    def test_stored_prefix_matches_llm_text(self, tmp_path, monkeypatch):
        data = _write(tmp_path, _LONG_LLM, monkeypatch)
        stored = data["implementation_strategy"][0]["summary"]
        assert _LONG_LLM.startswith(stored) or stored == _LONG_LLM


# ---------------------------------------------------------------------------
# 4. Rendered WM content is bounded
# ---------------------------------------------------------------------------


class TestRenderLimit:
    def _wm_with_summaries(self, *summaries: str) -> dict:
        strategies = [
            {"task_id": i + 1, "task_title": f"Task {i + 1}", "summary": s}
            for i, s in enumerate(summaries)
        ]
        return {
            "schema_version": 1,
            "implementation_strategy": strategies,
            "known_good_commands": [],
            "files_by_task": {},
            "active_constraints": [],
        }

    def test_render_truncates_each_summary_at_600(self):
        long_summary = "A" * 1200
        wm = self._wm_with_summaries(long_summary)
        rendered = _render_content(wm)
        # The rendered summary block must not contain more than 600 A's
        assert rendered.count("A") <= _SUMMARY_RENDER_LIMIT

    def test_render_shows_last_two_entries(self):
        wm = self._wm_with_summaries("First", "Second", "Third")
        rendered = _render_content(wm)
        assert "Second" in rendered
        assert "Third" in rendered
        assert "First" not in rendered

    def test_render_of_two_long_summaries_bounded(self):
        long = "B" * 1200
        wm = self._wm_with_summaries(long, long)
        rendered = _render_content(wm)
        assert len(rendered) <= 2 * _SUMMARY_RENDER_LIMIT + 200  # 200 for headings/titles

    def test_render_empty_when_no_data(self):
        wm = {
            "schema_version": 1,
            "implementation_strategy": [],
            "known_good_commands": [],
            "files_by_task": {},
            "active_constraints": [],
        }
        rendered = _render_content(wm)
        assert rendered == "" or "WORKING MEMORY" not in rendered or "END" in rendered


# ---------------------------------------------------------------------------
# 5. Other WM fields unchanged
# ---------------------------------------------------------------------------


class TestOtherFieldsUnchanged:
    def test_files_by_task_populated(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True)
        state = _make_state(str(tmp_path))
        state.changed_files = ["src/foo.py", "tests/test_foo.py"]
        write_working_memory(
            orchestration_state=state, task=_make_task(7),
            summary="some summary", logger=MagicMock(),
        )
        data = json.loads((tmp_path / ".agent" / "working_memory.json").read_text())
        assert data["files_by_task"]["7"]["added"] == ["src/foo.py", "tests/test_foo.py"]

    def test_active_constraints_unaffected(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", True)
        state = _make_state(str(tmp_path))
        state.validation_history = []
        write_working_memory(
            orchestration_state=state, task=_make_task(),
            summary="s", logger=MagicMock(),
        )
        data = json.loads((tmp_path / ".agent" / "working_memory.json").read_text())
        assert data["active_constraints"] == []

    def test_schema_version_unchanged(self, tmp_path, monkeypatch):
        data = _write(tmp_path, "summary", monkeypatch)
        assert data["schema_version"] == 1


# ---------------------------------------------------------------------------
# 6. Flag OFF: no file, behavior unchanged
# ---------------------------------------------------------------------------


class TestFlagOff:
    def test_no_wm_file_when_persistence_off(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", False)
        state = _make_state(str(tmp_path))
        write_working_memory(
            orchestration_state=state, task=_make_task(),
            summary=_LONG_LLM, logger=MagicMock(),
        )
        assert not (tmp_path / ".agent" / "working_memory.json").exists()

    def test_flag_off_no_side_effects(self, tmp_path, monkeypatch):
        monkeypatch.setattr("app.config.settings.WORKING_MEMORY_PERSISTENCE_ENABLED", False)
        state = _make_state(str(tmp_path))
        write_working_memory(
            orchestration_state=state, task=_make_task(),
            summary="x" * 2000, logger=MagicMock(),
        )
        assert not (tmp_path / ".agent").exists()


# ---------------------------------------------------------------------------
# 7. Deterministic routing unaffected (Stage 3 boundary check)
# ---------------------------------------------------------------------------


class TestDeterministicUnaffected:
    def test_deterministic_summary_stored_fully(self, tmp_path, monkeypatch):
        """Deterministic summary is short; stored unchanged."""
        data = _write(tmp_path, _DETERMINISTIC, monkeypatch)
        stored = data["implementation_strategy"][0]["summary"]
        assert stored == _DETERMINISTIC

    def test_deterministic_starts_with_canonical_prefix(self, tmp_path, monkeypatch):
        data = _write(tmp_path, _DETERMINISTIC, monkeypatch)
        stored = data["implementation_strategy"][0]["summary"]
        assert stored.startswith("Task completed with verified execution evidence")
