"""Phase 13B-E64: Completion repair source context injection tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.orchestration.phases.completion_repair import (
    _apply_completion_repair_ops_direct,
)
from app.services.orchestration.phases.completion_repair_capsule import (
    MAX_SOURCE_CONTENT_PER_FILE_CHARS,
    MAX_SOURCE_CONTENT_TOTAL_CHARS,
    _SOURCE_TRUNCATED_MARKER,
    CompletionRepairCapsule,
    _read_bounded_source_contents,
    build_bounded_completion_repair_prompt,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_capsule(tmp_path: Path, **kwargs) -> CompletionRepairCapsule:
    defaults = dict(
        validation_reasons=["pytest failed"],
        relevant_files=["src/cli.py"],
        last_step_summary="Step 2: wrote cli.py - success. Files: src/cli.py.",
        workspace_path=str(tmp_path),
        task_prompt_excerpt="Add summary command",
    )
    defaults.update(kwargs)
    return CompletionRepairCapsule(**defaults)


# ---------------------------------------------------------------------------
# _read_bounded_source_contents
# ---------------------------------------------------------------------------


def test_read_bounded_includes_content_for_existing_file(tmp_path):
    src = "def main():\n    return 0\n"
    _write(tmp_path / "src/cli.py", src)
    contents = _read_bounded_source_contents(tmp_path, ["src/cli.py"])
    assert "src/cli.py" in contents
    assert contents["src/cli.py"] == src


def test_read_bounded_skips_missing_file(tmp_path):
    contents = _read_bounded_source_contents(tmp_path, ["src/missing.py"])
    assert contents == {}


def test_read_bounded_respects_per_file_cap(tmp_path):
    long_content = "x" * (MAX_SOURCE_CONTENT_PER_FILE_CHARS + 200)
    _write(tmp_path / "src/big.py", long_content)
    contents = _read_bounded_source_contents(tmp_path, ["src/big.py"])
    assert "src/big.py" in contents
    stored = contents["src/big.py"]
    # Stored content should be capped + marker
    assert stored.endswith(_SOURCE_TRUNCATED_MARKER)
    assert len(stored) == MAX_SOURCE_CONTENT_PER_FILE_CHARS + len(
        _SOURCE_TRUNCATED_MARKER
    )


def test_read_bounded_no_truncation_marker_when_within_cap(tmp_path):
    short_content = "x" * 100
    _write(tmp_path / "src/short.py", short_content)
    contents = _read_bounded_source_contents(tmp_path, ["src/short.py"])
    assert _SOURCE_TRUNCATED_MARKER not in contents["src/short.py"]


def test_read_bounded_respects_total_cap(tmp_path):
    # Write three files that each fill up the per-file cap
    per_file = "x" * MAX_SOURCE_CONTENT_PER_FILE_CHARS
    _write(tmp_path / "src/a.py", per_file)
    _write(tmp_path / "src/b.py", per_file)
    _write(tmp_path / "src/c.py", per_file)
    contents = _read_bounded_source_contents(
        tmp_path, ["src/a.py", "src/b.py", "src/c.py"]
    )
    total_stored = sum(len(v) for v in contents.values())
    assert total_stored <= MAX_SOURCE_CONTENT_TOTAL_CHARS + len(
        _SOURCE_TRUNCATED_MARKER
    ) * len(contents)
    # Must not exceed total cap by more than the marker overhead per file
    # In practice, the stop condition fires when total_chars >= MAX at start of loop.
    assert total_stored <= MAX_SOURCE_CONTENT_TOTAL_CHARS + len(
        _SOURCE_TRUNCATED_MARKER
    )


def test_read_bounded_skips_files_beyond_total_cap(tmp_path):
    # Four files of 500 chars each fill the 2000-char total cap exactly.
    # A fifth file written after should be skipped.
    chunk = "y" * 500
    for i in range(4):
        _write(tmp_path / f"src/f{i}.py", chunk)
    _write(tmp_path / "src/extra.py", "z = 1\n")
    paths = [f"src/f{i}.py" for i in range(4)] + ["src/extra.py"]
    contents = _read_bounded_source_contents(tmp_path, paths)
    for i in range(4):
        assert f"src/f{i}.py" in contents
    assert "src/extra.py" not in contents


def test_read_bounded_preserves_path_ordering(tmp_path):
    _write(tmp_path / "src/a.py", "a = 1\n")
    _write(tmp_path / "src/b.py", "b = 2\n")
    contents = _read_bounded_source_contents(tmp_path, ["src/b.py", "src/a.py"])
    keys = list(contents.keys())
    assert keys == ["src/b.py", "src/a.py"]


def test_read_bounded_rejects_path_traversal(tmp_path):
    # A path that would escape the project root should not appear in results.
    contents = _read_bounded_source_contents(tmp_path, ["../escape.py"])
    assert contents == {}


# ---------------------------------------------------------------------------
# CompletionRepairCapsule — source_file_contents default
# ---------------------------------------------------------------------------


def test_capsule_source_file_contents_default_empty():
    capsule = CompletionRepairCapsule(
        validation_reasons=[],
        relevant_files=[],
        last_step_summary="",
        workspace_path="/tmp",
        task_prompt_excerpt="",
    )
    assert capsule.source_file_contents == {}


def test_capsule_accepts_source_file_contents(tmp_path):
    capsule = _make_capsule(
        tmp_path,
        source_file_contents={"src/cli.py": "def main(): pass\n"},
    )
    assert capsule.source_file_contents == {"src/cli.py": "def main(): pass\n"}


# ---------------------------------------------------------------------------
# build_bounded_completion_repair_prompt — CURRENT FILE CONTENT section
# ---------------------------------------------------------------------------


def test_prompt_contains_current_file_content_header(tmp_path):
    _write(tmp_path / "src/cli.py", "def main(): pass\n")
    capsule = _make_capsule(
        tmp_path,
        source_file_contents={"src/cli.py": "def main(): pass\n"},
    )
    prompt = build_bounded_completion_repair_prompt(capsule, 4)
    assert "CURRENT FILE CONTENT" in prompt


def test_prompt_contains_file_content_body(tmp_path):
    file_content = "def main():\n    return 0\n"
    capsule = _make_capsule(
        tmp_path,
        source_file_contents={"src/cli.py": file_content},
    )
    prompt = build_bounded_completion_repair_prompt(capsule, 4)
    assert "def main():" in prompt
    assert "--- src/cli.py ---" in prompt


def test_prompt_contains_exact_old_text_rule(tmp_path):
    capsule = _make_capsule(tmp_path)
    prompt = build_bounded_completion_repair_prompt(capsule, 4)
    # Rule 12 must reference exact copying of old text.
    assert "CURRENT FILE CONTENT" in prompt
    assert "character-for-character" in prompt or "exactly" in prompt.lower()


def test_prompt_contains_write_file_escape_hatch_rule(tmp_path):
    capsule = _make_capsule(tmp_path)
    prompt = build_bounded_completion_repair_prompt(capsule, 4)
    # Rule 13 must instruct use of write_file when exact text not visible.
    assert "Do not invent" in prompt or "Do not guess" in prompt


def test_prompt_no_file_block_when_source_contents_empty(tmp_path):
    # When source_file_contents is empty, no --- file --- blocks should appear.
    capsule = _make_capsule(tmp_path, source_file_contents={})
    prompt = build_bounded_completion_repair_prompt(capsule, 4)
    assert "--- src/" not in prompt


def test_prompt_includes_truncation_marker_when_content_truncated(tmp_path):
    long_content = "x = 1\n" * 200  # way over 800 chars
    _write(tmp_path / "src/cli.py", long_content)
    truncated = (
        long_content[:MAX_SOURCE_CONTENT_PER_FILE_CHARS] + _SOURCE_TRUNCATED_MARKER
    )
    capsule = _make_capsule(
        tmp_path,
        source_file_contents={"src/cli.py": truncated},
    )
    prompt = build_bounded_completion_repair_prompt(capsule, 4)
    assert _SOURCE_TRUNCATED_MARKER in prompt


# ---------------------------------------------------------------------------
# E61 schema requirements still hold
# ---------------------------------------------------------------------------


def test_prompt_still_contains_ops_fix(tmp_path):
    capsule = _make_capsule(tmp_path)
    prompt = build_bounded_completion_repair_prompt(capsule, 4)
    assert "ops_fix" in prompt


def test_prompt_still_excludes_commands_array(tmp_path):
    capsule = _make_capsule(tmp_path)
    prompt = build_bounded_completion_repair_prompt(capsule, 4)
    assert '"commands": [' not in prompt


def test_prompt_still_contains_replace_in_file_example(tmp_path):
    capsule = _make_capsule(tmp_path)
    prompt = build_bounded_completion_repair_prompt(capsule, 4)
    assert "replace_in_file" in prompt


def test_prompt_still_contains_verification_field(tmp_path):
    capsule = _make_capsule(tmp_path)
    prompt = build_bounded_completion_repair_prompt(capsule, 4)
    assert '"verification"' in prompt


# ---------------------------------------------------------------------------
# Diagnostics — old_preview in error message
# ---------------------------------------------------------------------------


def test_apply_replace_old_not_found_includes_preview(tmp_path):
    _write(tmp_path / "src/f.py", "x = 1\n")
    ops = [
        {
            "op": "replace_in_file",
            "path": "src/f.py",
            "old": "NOTHERE_DISTINCT_OLD_TEXT",
            "new": "y",
        }
    ]
    result = _apply_completion_repair_ops_direct(ops, tmp_path)
    assert not result["success"]
    error_msg = result["errors"][0]
    assert "NOTHERE_DISTINCT_OLD_TEXT" in error_msg


def test_apply_replace_old_not_found_preview_truncated(tmp_path):
    _write(tmp_path / "src/f.py", "x = 1\n")
    long_old = "Z" * 500
    ops = [{"op": "replace_in_file", "path": "src/f.py", "old": long_old, "new": "y"}]
    result = _apply_completion_repair_ops_direct(ops, tmp_path)
    assert not result["success"]
    error_msg = result["errors"][0]
    # Preview is at most 200 chars of the old text
    assert "ZZZZZ" in error_msg
    # The error must not include all 500 Z chars
    assert error_msg.count("Z") <= 200
