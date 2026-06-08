"""Touch accounting fix: verify-only steps must not pollute changed_files.

Covers:
1. Verify-only step (no ops) → fallback yields [] not expected_files.
2. Write step (has ops) → fallback yields expected_files when result has no files_changed.
3. Scorer _collect_touched_files ignores expected_artifacts.
4. Scorer _collect_touched_files ignores missing_expected_files.
5. Scorer still collects real files_touched / files_changed / changed_files.
6. forbidden_touched_prefixes still blocks real test-file modifications.
"""

from __future__ import annotations

import importlib.util
from pathlib import Path


def _load_scorer():
    path = (
        Path(__file__).resolve().parents[2]
        / "scripts"
        / "maintenance"
        / "score_orchestrator_eval_case.py"
    )
    spec = importlib.util.spec_from_file_location("score_orchestrator_eval_case", path)
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


scorer = _load_scorer()


# ---------------------------------------------------------------------------
# Issue B: execution-loop fallback expression
# The fix changes:
#   step_result.get("files_changed", expected_files)
# to:
#   step_result.get("files_changed", expected_files if step_ops else [])
#
# We test the expression directly since it is a simple inline change.
# ---------------------------------------------------------------------------


def _apply_fallback(step_result, expected_files, step_ops):
    """Mirror of the fixed expression at execution_loop.py:1214."""
    return step_result.get("files_changed", expected_files if step_ops else [])


def test_verify_only_step_no_files_changed_key_yields_empty():
    # No ops → verify-only step. Runtime returns result with no files_changed key.
    step_result = {"status": "completed", "output": "4 passed"}
    expected_files = ["tests/test_money.py"]
    step_ops = []

    result = _apply_fallback(step_result, expected_files, step_ops)

    assert (
        result == []
    ), "verify-only step must not attribute expected_files as changed files"


def test_verify_only_step_explicit_empty_files_changed_yields_empty():
    # Runtime explicitly returns files_changed: [] — key is present, fallback not used.
    step_result = {"status": "completed", "output": "ok", "files_changed": []}
    expected_files = ["tests/test_money.py"]
    step_ops = []

    result = _apply_fallback(step_result, expected_files, step_ops)

    assert result == []


def test_write_step_no_files_changed_key_falls_back_to_expected_files():
    # Has ops (write step). Runtime returns result with no files_changed key.
    # Fallback to expected_files is correct — the write ops targeted those files.
    step_result = {"status": "completed", "output": ""}
    expected_files = ["src/tiny_money/money.py"]
    step_ops = [{"op": "write_file", "path": "src/tiny_money/money.py", "content": ""}]

    result = _apply_fallback(step_result, expected_files, step_ops)

    assert result == expected_files


def test_write_step_explicit_files_changed_key_used_directly():
    # When result has files_changed, it is used regardless of step_ops.
    step_result = {"status": "completed", "output": "", "files_changed": ["src/foo.py"]}
    expected_files = ["src/bar.py"]
    step_ops = [{"op": "write_file", "path": "src/bar.py", "content": ""}]

    result = _apply_fallback(step_result, expected_files, step_ops)

    assert result == ["src/foo.py"]


# ---------------------------------------------------------------------------
# Issue A: scorer _collect_touched_files candidate keys
# ---------------------------------------------------------------------------


def test_scorer_ignores_expected_artifacts_in_event():
    events = [
        {
            "event_type": "intent_outcome_mismatch",
            "details": {
                "expected_artifacts": ["tests/test_money.py"],
                "actual_files": [],
            },
        }
    ]
    touched = scorer._collect_touched_files(events, [])
    assert "tests/test_money.py" not in touched


def test_scorer_ignores_missing_expected_files_in_event():
    events = [
        {
            "event_type": "intent_outcome_mismatch",
            "details": {
                "missing_expected_files": ["tests/test_money.py"],
            },
        }
    ]
    touched = scorer._collect_touched_files(events, [])
    assert "tests/test_money.py" not in touched


def test_scorer_collects_files_touched_from_snapshot():
    snapshots = [{"files_touched": ["src/tiny_money/money.py"]}]
    touched = scorer._collect_touched_files([], snapshots)
    assert "src/tiny_money/money.py" in touched


def test_scorer_collects_files_changed_from_event():
    events = [
        {
            "event_type": "step_finished",
            "details": {"files_changed": ["src/foo.py"]},
        }
    ]
    touched = scorer._collect_touched_files(events, [])
    assert "src/foo.py" in touched


def test_scorer_collects_changed_files_from_event():
    events = [
        {
            "event_type": "step_finished",
            "details": {"changed_files": ["src/bar.py"]},
        }
    ]
    touched = scorer._collect_touched_files(events, [])
    assert "src/bar.py" in touched


def test_scorer_collects_actual_files_from_event():
    events = [
        {
            "event_type": "intent_outcome_mismatch",
            "details": {
                "actual_files": ["src/real.py"],
                "expected_artifacts": ["tests/should_be_ignored.py"],
            },
        }
    ]
    touched = scorer._collect_touched_files(events, [])
    assert "src/real.py" in touched
    assert "tests/should_be_ignored.py" not in touched


# ---------------------------------------------------------------------------
# forbidden_touched_prefixes still gates real modifications
# ---------------------------------------------------------------------------


def test_forbidden_prefix_blocks_real_test_file_modification():
    case = {
        "forbidden_touched_prefixes": ["tests/"],
        "allowed_touched_prefixes": ["src/tiny_money/money.py"],
        "expected_touched_files": [],
    }
    # Simulate: money.py AND test_money.py both appear in files_touched
    # (as if the agent actually wrote tests/test_money.py via a write_file op)
    snapshots = [{"files_touched": ["src/tiny_money/money.py", "tests/test_money.py"]}]
    touched = scorer._collect_touched_files([], snapshots)
    scope = scorer._touch_scope(touched, case)

    assert "tests/test_money.py" in scope["forbidden_touched_files"]
    assert "tests/test_money.py" in scope["unexpected_touched_files"]


def test_forbidden_prefix_passes_when_only_src_touched():
    case = {
        "forbidden_touched_prefixes": ["tests/"],
        "allowed_touched_prefixes": ["src/tiny_money/money.py"],
        "expected_touched_files": [],
    }
    snapshots = [{"files_touched": ["src/tiny_money/money.py"]}]
    touched = scorer._collect_touched_files([], snapshots)
    scope = scorer._touch_scope(touched, case)

    assert scope["forbidden_touched_files"] == []
    assert scope["unexpected_touched_files"] == []


def test_full_clean_success_path_no_forbidden_touch():
    # Simulate a correct run: only money.py touched, verifier passed, task completed.
    case = {
        "forbidden_touched_prefixes": ["tests/"],
        "allowed_touched_prefixes": ["src/tiny_money/money.py"],
        "expected_touched_files": [],
        "success_criteria": [],
    }
    snapshots = [{"files_touched": ["src/tiny_money/money.py"]}]
    touched = scorer._collect_touched_files([], snapshots)
    scope = scorer._touch_scope(touched, case)

    verifier = {"passed": True}
    files = {"missing_required_files": [], "present_forbidden_existing_files": []}
    event_summary = {"task_completed": True}
    snapshot_summary = {"state_snapshot_present": True}

    clean_success, blockers = scorer._derive_clean_success(
        case=case,
        verifier=verifier,
        files=files,
        scope=scope,
        event_summary=event_summary,
        snapshot_summary=snapshot_summary,
    )

    assert clean_success is True
    assert blockers == []
