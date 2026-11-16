"""Phase 13B-E40 bounded Phase 7F changed-source context coverage."""

from __future__ import annotations

from types import SimpleNamespace

from app.services.orchestration.diagnostics.debug_feedback import (
    DebugFeedbackEnvelope,
    build_bounded_debug_repair_prompt_with_metadata,
)
from app.services.orchestration.phases.execution_loop import (
    _bounded_debug_repair_output_observability,
    _bounded_debug_repair_prior_source_paths,
    _bounded_debug_repair_prompt_manifest,
)


def _write(project_dir, relative_path: str, content: str) -> None:
    path = project_dir / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _envelope(project_dir, **overrides) -> DebugFeedbackEnvelope:
    values = {
        "task_execution_id": 40,
        "task_id": 40,
        "step_index": 3,
        "failure_phase": "execution",
        "failed_command": "python3 -m pytest -q",
        "workspace_path": str(project_dir),
        "failure_class": "pytest_failure",
    }
    values.update(overrides)
    return DebugFeedbackEnvelope(**values)


def test_phase7f_uses_prior_source_steps_when_verification_changed_files_empty(
    tmp_path,
):
    _write(tmp_path, "src/pkg/store.py", "def summary():\n    return (3, 2)\n")
    state = SimpleNamespace(
        execution_results=[
            SimpleNamespace(step_number=1, files_changed=["src/pkg/store.py"])
        ],
        plan=[
            {"ops": [{"op": "write_file", "path": "src/pkg/store.py", "content": "x"}]},
            {"ops": []},
            {"ops": []},
        ],
    )
    envelope = _envelope(tmp_path, changed_files=[])

    result = build_bounded_debug_repair_prompt_with_metadata(
        envelope,
        prior_source_paths=_bounded_debug_repair_prior_source_paths(state, 2),
    )

    assert "## CURRENT CONTENT OF IMPLICATED SOURCE FILES" in result.prompt
    assert "--- src/pkg/store.py" in result.prompt
    assert "return (3, 2)" in result.prompt
    assert result.metadata[
        "bounded_execution_debug_repair_changed_file_context_paths"
    ] == ["src/pkg/store.py"]


def test_phase7f_includes_source_paths_from_traceback(tmp_path):
    _write(
        tmp_path, "src/pkg/formatting.py", "def format_summary():\n    return 'bad'\n"
    )
    envelope = _envelope(
        tmp_path,
        stderr_excerpt="E AssertionError\nsrc/pkg/formatting.py:12: AssertionError",
    )

    result = build_bounded_debug_repair_prompt_with_metadata(envelope)

    assert "--- src/pkg/formatting.py" in result.prompt
    assert result.metadata[
        "bounded_execution_debug_repair_changed_file_context_paths"
    ] == ["src/pkg/formatting.py"]


def test_phase7f_changed_source_context_enforces_file_and_character_budgets(tmp_path):
    prior_paths = []
    for index in range(4):
        relative_path = f"src/pkg/module_{index}.py"
        _write(tmp_path, relative_path, "VALUE = '" + ("x" * 2400) + "'\n")
        prior_paths.append(relative_path)

    result = build_bounded_debug_repair_prompt_with_metadata(
        _envelope(tmp_path),
        prior_source_paths=prior_paths,
    )
    metadata = result.metadata

    # P42-V17-N: [TRUNCATED] labels and boundary lines are charged to the
    # unchanged 3000-char total, so the third file's sub-100-char sliver that
    # used to follow two capped files no longer fits.  Order is unchanged.
    assert metadata["bounded_execution_debug_repair_changed_file_context_paths"] == [
        "src/pkg/module_0.py",
        "src/pkg/module_1.py",
    ]
    assert metadata["bounded_execution_debug_repair_changed_file_context_chars"] <= 3000
    assert (
        metadata["bounded_execution_debug_repair_changed_file_context_complete_paths"]
        == []
    )
    assert result.prompt.count("[TRUNCATED: ") == 2
    assert result.prompt.count("[TRUNCATION BOUNDARY: ") == 2


def test_phase7f_excludes_test_files_even_when_traceback_mentions_them(tmp_path):
    _write(
        tmp_path, "tests/test_formatting.py", "def test_summary():\n    assert False\n"
    )
    _write(
        tmp_path, "src/pkg/formatting.py", "def format_summary():\n    return 'bad'\n"
    )
    envelope = _envelope(
        tmp_path,
        stderr_excerpt=(
            "tests/test_formatting.py:4: AssertionError\n"
            "src/pkg/formatting.py:8: AssertionError"
        ),
    )

    result = build_bounded_debug_repair_prompt_with_metadata(
        envelope,
        prior_source_paths=["tests/test_formatting.py"],
    )

    context_block = result.prompt.split(
        "## CURRENT CONTENT OF IMPLICATED SOURCE FILES", 1
    )[1]
    assert "tests/test_formatting.py" not in context_block
    assert "src/pkg/formatting.py" in context_block


def test_phase7f_prompt_manifest_records_changed_source_context(tmp_path):
    _write(tmp_path, "src/pkg/store.py", "def summary():\n    return (0, 0)\n")
    result = build_bounded_debug_repair_prompt_with_metadata(
        _envelope(tmp_path),
        prior_source_paths=["src/pkg/store.py"],
    )

    manifest = _bounded_debug_repair_prompt_manifest(result.metadata)

    assert manifest == {
        "bounded_execution_debug_repair_changed_file_context_present": True,
        "bounded_execution_debug_repair_changed_file_context_paths": [
            "src/pkg/store.py"
        ],
        "bounded_execution_debug_repair_changed_file_context_chars": result.metadata[
            "bounded_execution_debug_repair_changed_file_context_chars"
        ],
    }


def test_phase7f_omits_changed_source_context_when_no_source_is_implicated(tmp_path):
    _write(tmp_path, "README.md", "No source files\n")

    result = build_bounded_debug_repair_prompt_with_metadata(_envelope(tmp_path))

    assert "## CURRENT CONTENT OF IMPLICATED SOURCE FILES" not in result.prompt
    assert (
        result.metadata["bounded_execution_debug_repair_changed_file_context_present"]
        is False
    )
    assert (
        result.metadata["bounded_execution_debug_repair_changed_file_context_paths"]
        == []
    )
    assert (
        result.metadata["bounded_execution_debug_repair_changed_file_context_chars"]
        == 0
    )


def test_phase7f_repair_output_observability_records_hash_and_changed_paths():
    metadata = _bounded_debug_repair_output_observability(
        '[{"repair_type":"ops_fix"}]',
        {
            "ops": [
                {"op": "write_file", "path": "src/pkg/store.py"},
                {"op": "replace_in_file", "path": "src/pkg/store.py"},
                {"op": "write_file", "path": "src/pkg/formatting.py"},
            ]
        },
    )

    assert len(metadata["repair_output_sha256"]) == 64
    assert metadata["repair_output_changed_paths"] == [
        "src/pkg/store.py",
        "src/pkg/formatting.py",
    ]
