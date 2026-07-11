"""Phase 13B-E61: Structured completion repair ops + command-only post-diff guard tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.orchestration.diagnostics.signature_guard import (
    COMPLETION_REPAIR_POST_DIFF_VIOLATION_REASON,
    check_post_execution_signature_drift,
    snapshot_public_python_signatures,
)
from app.services.orchestration.phases.completion_repair import (
    _apply_completion_repair_ops_direct,
    _canonicalize_completion_repair_envelope,
    _extract_completion_repair_step,
    _normalize_completion_repair_step,
)


def _contract_step(**overrides):
    step = {
        "description": "Repair the failing service assertion",
        "ops": [{"op": "replace_in_file", "path": "src/f.py", "old": "a", "new": "b"}],
        "verification": "python -m pytest -q",
        "expected_files": ["src/f.py"],
    }
    step.update(overrides)
    return step


def test_canonical_completion_repair_envelope_normalizes_one_step():
    result = _canonicalize_completion_repair_envelope(
        {"repair_step": _contract_step()}, 4
    )
    assert result is not None
    assert list(result) == ["repair_step"]
    assert result["repair_step"]["step_number"] == 4


@pytest.mark.parametrize(
    "wrapper", ["step", "completion_repair_step", "payload", "result"]
)
def test_supported_completion_repair_wrappers_normalize_to_same_step(wrapper):
    canonical = _canonicalize_completion_repair_envelope(
        {"repair_step": _contract_step()}, 4
    )
    wrapped = _canonicalize_completion_repair_envelope({wrapper: _contract_step()}, 4)
    assert wrapped == canonical


def test_legacy_direct_completion_repair_step_normalizes_to_canonical():
    result = _canonicalize_completion_repair_envelope(
        {"step_number": 9, **_contract_step()}, 4
    )
    assert result is not None
    assert result["repair_step"]["step_number"] == 9


@pytest.mark.parametrize(
    "value",
    [
        {"status": "ready", "message": "done"},
        {"repair_step": {}},
        {"repair_step": {**_contract_step(), "commands": [], "ops": []}},
        {"repair_step": {**_contract_step(), "verification": []}},
        {"repair_step": {**_contract_step(), "expected_files": "src/f.py"}},
        {"outer": {"repair_step": _contract_step()}},
        [{"repair_step": _contract_step()}],
    ],
)
def test_completion_repair_contract_rejects_noncanonical_or_malformed_shapes(value):
    assert _canonicalize_completion_repair_envelope(value, 4) is None


from app.services.orchestration.phases.completion_repair_capsule import (
    CompletionRepairCapsule,
    build_bounded_completion_repair_prompt,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


# ---------------------------------------------------------------------------
# Prompt format
# ---------------------------------------------------------------------------


def _make_capsule(tmp_path: Path) -> CompletionRepairCapsule:
    return CompletionRepairCapsule(
        validation_reasons=["pytest failed: format_summary signature mismatch"],
        relevant_files=["src/formatting.py"],
        last_step_summary="Step 2: wrote formatting.py - success. Files: src/formatting.py.",
        workspace_path=str(tmp_path),
        task_prompt_excerpt="Implement format_summary(total, completed) -> str",
    )


def test_prompt_contains_ops_fix_repair_type():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        capsule = _make_capsule(Path(tmp))
        prompt = build_bounded_completion_repair_prompt(capsule, 3)
        assert '"ops_fix"' in prompt or "ops_fix" in prompt


def test_prompt_contains_replace_in_file_example():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        capsule = _make_capsule(Path(tmp))
        prompt = build_bounded_completion_repair_prompt(capsule, 3)
        assert "replace_in_file" in prompt


def test_prompt_does_not_use_commands_in_example():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        capsule = _make_capsule(Path(tmp))
        prompt = build_bounded_completion_repair_prompt(capsule, 3)
        # The JSON output example must not show a commands array.
        assert '"commands": [' not in prompt


def test_prompt_uses_verification_field():
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        capsule = _make_capsule(Path(tmp))
        prompt = build_bounded_completion_repair_prompt(capsule, 3)
        assert '"verification"' in prompt


# ---------------------------------------------------------------------------
# _normalize_completion_repair_step — ops-fix input
# ---------------------------------------------------------------------------


def test_normalize_ops_fix_step_preserves_ops():
    raw = {
        "step_number": 3,
        "repair_type": "ops_fix",
        "description": "Fix signature",
        "ops": [{"op": "replace_in_file", "path": "src/f.py", "old": "a", "new": "b"}],
        "verification": "python -m pytest -q",
        "expected_files": ["src/f.py"],
    }
    result = _normalize_completion_repair_step(raw, 3)
    assert result["ops"] == raw["ops"]
    assert result["verification"] == "python -m pytest -q"
    assert result["commands"] == []


def test_normalize_ops_fix_step_verification_command_alias():
    raw = {
        "repair_type": "ops_fix",
        "ops": [{"op": "write_file", "path": "src/f.py", "content": "x = 1\n"}],
        "verification_command": "python -m pytest -q",
    }
    result = _normalize_completion_repair_step(raw, 4)
    assert result["verification"] == "python -m pytest -q"


def test_normalize_ops_fix_derives_expected_files_from_ops():
    raw = {
        "repair_type": "ops_fix",
        "ops": [
            {"op": "replace_in_file", "path": "src/a.py", "old": "x", "new": "y"},
            {"op": "replace_in_file", "path": "src/b.py", "old": "x", "new": "y"},
        ],
    }
    result = _normalize_completion_repair_step(raw, 5)
    assert "src/a.py" in result["expected_files"]
    assert "src/b.py" in result["expected_files"]


def test_normalize_ops_fix_explicit_expected_files_not_overwritten():
    raw = {
        "repair_type": "ops_fix",
        "ops": [{"op": "write_file", "path": "src/a.py", "content": ""}],
        "expected_files": ["src/a.py", "src/b.py"],
    }
    result = _normalize_completion_repair_step(raw, 1)
    assert result["expected_files"] == ["src/a.py", "src/b.py"]


# ---------------------------------------------------------------------------
# _extract_completion_repair_step — ops-fix format
# ---------------------------------------------------------------------------


def test_extract_ops_fix_dict():
    data = {
        "step_number": 2,
        "repair_type": "ops_fix",
        "description": "fix",
        "ops": [{"op": "replace_in_file", "path": "src/f.py", "old": "a", "new": "b"}],
        "verification": "pytest",
        "expected_files": ["src/f.py"],
    }
    step = _extract_completion_repair_step(data, 2)
    assert step is not None
    assert step["ops"] == data["ops"]


def test_extract_ops_fix_wrapped_in_list():
    data = [
        {
            "repair_type": "ops_fix",
            "ops": [{"op": "write_file", "path": "src/f.py", "content": "x = 1\n"}],
            "verification_command": "python -m pytest",
        }
    ]
    step = _extract_completion_repair_step(data, 3)
    assert step is not None
    assert step["ops"][0]["op"] == "write_file"


def test_extract_ops_fix_inside_wrapper_key():
    data = {
        "repair_step": {
            "repair_type": "ops_fix",
            "ops": [{"op": "replace_in_file", "path": "x.py", "old": "a", "new": "b"}],
        }
    }
    step = _extract_completion_repair_step(data, 1)
    assert step is not None


# ---------------------------------------------------------------------------
# _apply_completion_repair_ops_direct
# ---------------------------------------------------------------------------


def test_apply_write_file(tmp_path):
    ops = [{"op": "write_file", "path": "src/mod.py", "content": "x = 1\n"}]
    result = _apply_completion_repair_ops_direct(ops, tmp_path)
    assert result["success"]
    assert (tmp_path / "src/mod.py").read_text() == "x = 1\n"


def test_apply_replace_in_file(tmp_path):
    _write(tmp_path / "src/f.py", "def foo(a, b): pass\n")
    ops = [
        {
            "op": "replace_in_file",
            "path": "src/f.py",
            "old": "def foo(a, b): pass",
            "new": "def foo(a, b, c): pass",
        }
    ]
    result = _apply_completion_repair_ops_direct(ops, tmp_path)
    assert result["success"]
    assert "def foo(a, b, c): pass" in (tmp_path / "src/f.py").read_text()


def test_apply_append_file(tmp_path):
    _write(tmp_path / "src/f.py", "x = 1\n")
    ops = [{"op": "append_file", "path": "src/f.py", "content": "y = 2\n"}]
    result = _apply_completion_repair_ops_direct(ops, tmp_path)
    assert result["success"]
    assert (tmp_path / "src/f.py").read_text() == "x = 1\ny = 2\n"


def test_apply_replace_in_file_missing_file(tmp_path):
    ops = [{"op": "replace_in_file", "path": "src/missing.py", "old": "a", "new": "b"}]
    result = _apply_completion_repair_ops_direct(ops, tmp_path)
    assert not result["success"]
    assert any("not found" in e for e in result["errors"])


def test_apply_replace_in_file_old_not_found(tmp_path):
    _write(tmp_path / "src/f.py", "x = 1\n")
    ops = [{"op": "replace_in_file", "path": "src/f.py", "old": "NOTHERE", "new": "y"}]
    result = _apply_completion_repair_ops_direct(ops, tmp_path)
    assert not result["success"]
    assert any("not found" in e for e in result["errors"])


def test_apply_path_escape_rejected(tmp_path):
    ops = [{"op": "write_file", "path": "../escape.py", "content": "x = 1\n"}]
    result = _apply_completion_repair_ops_direct(ops, tmp_path)
    assert not result["success"]
    assert result["errors"]


def test_apply_multiple_ops_partial_failure(tmp_path):
    _write(tmp_path / "src/a.py", "a = 1\n")
    ops = [
        {"op": "write_file", "path": "src/b.py", "content": "b = 2\n"},
        {"op": "replace_in_file", "path": "src/missing.py", "old": "x", "new": "y"},
    ]
    result = _apply_completion_repair_ops_direct(ops, tmp_path)
    assert not result["success"]
    assert "src/b.py" in result["applied"]


# ---------------------------------------------------------------------------
# snapshot_public_python_signatures
# ---------------------------------------------------------------------------


def test_snapshot_fingerprints_existing_file(tmp_path):
    _write(
        tmp_path / "src/formatting.py",
        "def format_task_line(task: Task, *, include_status: bool = False) -> str:\n    return ''\n",
    )
    snap = snapshot_public_python_signatures(tmp_path, ["src/formatting.py"])
    assert "src/formatting.py" in snap
    assert "format_task_line" in snap["src/formatting.py"]


def test_snapshot_skips_missing_file(tmp_path):
    snap = snapshot_public_python_signatures(tmp_path, ["src/missing.py"])
    assert snap == {}


def test_snapshot_skips_non_python(tmp_path):
    _write(tmp_path / "src/f.ts", "export const x = 1;")
    snap = snapshot_public_python_signatures(tmp_path, ["src/f.ts"])
    assert snap == {}


def test_snapshot_skips_unparseable_file(tmp_path):
    _write(tmp_path / "src/bad.py", "def broken(\n")
    snap = snapshot_public_python_signatures(tmp_path, ["src/bad.py"])
    assert snap == {}


# ---------------------------------------------------------------------------
# check_post_execution_signature_drift
# ---------------------------------------------------------------------------


def _snap(tmp_path: Path, rel_path: str, src: str) -> dict:
    _write(tmp_path / rel_path, src)
    return snapshot_public_python_signatures(tmp_path, [rel_path])


def test_post_diff_clean_no_violations(tmp_path):
    original = "def format_summary(total: int, completed: int) -> str:\n    return ''\n"
    snap = _snap(tmp_path, "src/formatting.py", original)
    # File unchanged — re-write with same content.
    _write(tmp_path / "src/formatting.py", original)
    result = check_post_execution_signature_drift(snap, tmp_path)
    assert not result.violations
    assert result.checked


def test_post_diff_detects_signature_changed(tmp_path):
    original = "def format_summary(total: int, completed: int) -> str:\n    return ''\n"
    snap = _snap(tmp_path, "src/formatting.py", original)
    # Drift: keyword-only parameters forced.
    drifted = "def format_summary(*, total: int = 0, completed: int = 0) -> str:\n    return ''\n"
    _write(tmp_path / "src/formatting.py", drifted)
    result = check_post_execution_signature_drift(snap, tmp_path)
    assert len(result.violations) == 1
    assert result.violations[0].violation_type == "signature_changed"
    assert result.violations[0].qualified_name == "format_summary"


def test_post_diff_detects_missing_definition(tmp_path):
    original = "def format_task_line(task, *, include_status=False):\n    return ''\n"
    snap = _snap(tmp_path, "src/formatting.py", original)
    # Repair removed the function entirely.
    _write(tmp_path / "src/formatting.py", "# nothing\n")
    result = check_post_execution_signature_drift(snap, tmp_path)
    assert any(
        v.violation_type == "missing_existing_definition" for v in result.violations
    )


def test_post_diff_empty_snapshot_returns_unchecked(tmp_path):
    result = check_post_execution_signature_drift({}, tmp_path)
    assert not result.checked
    assert not result.violations


def test_post_diff_violation_reason_constant():
    assert COMPLETION_REPAIR_POST_DIFF_VIOLATION_REASON == (
        "completion_repair_post_execution_signature_drift_violation"
    )
