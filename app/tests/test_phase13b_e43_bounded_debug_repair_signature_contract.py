"""Phase 13B-E43: Bounded debug repair signature contract hardening.

Verifies that:
- The changed-file context header contains the authoritative-signature rule.
- The old escape-hatch phrase is gone.
- The new signature-preservation rule appears in both rules_section branches.
- Existing E40 context tests are unaffected (covered separately in test_phase13b_e40).
"""

from __future__ import annotations

from app.services.orchestration.diagnostics.debug_feedback import (
    DebugFeedbackEnvelope,
    build_bounded_debug_repair_changed_file_context,
    build_bounded_debug_repair_prompt_with_metadata,
)


def _write(project_dir, relative_path: str, content: str) -> None:
    path = project_dir / relative_path
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _envelope(project_dir, **overrides) -> DebugFeedbackEnvelope:
    values = dict(
        task_execution_id=43,
        task_id=43,
        step_index=2,
        failure_phase="execution",
        failed_command="python3 -m pytest -q",
        workspace_path=str(project_dir),
        failure_class="pytest_failure",
    )
    values.update(overrides)
    return DebugFeedbackEnvelope(**values)


# ---------------------------------------------------------------------------
# Test 1: section header contains authoritative-signature language
# ---------------------------------------------------------------------------


def test_changed_file_context_header_contains_authoritative_signature_rule(tmp_path):
    _write(
        tmp_path,
        "src/pkg/formatting.py",
        "def format_summary(total: int, completed: int) -> str:\n    raise NotImplementedError\n",
    )
    envelope = _envelope(tmp_path)

    rendered, metadata = build_bounded_debug_repair_changed_file_context(
        envelope,
        prior_source_paths=["src/pkg/formatting.py"],
    )

    assert "Existing function signatures shown here are authoritative" in rendered
    assert (
        metadata["bounded_execution_debug_repair_changed_file_context_present"] is True
    )


# ---------------------------------------------------------------------------
# Test 2: old escape-hatch phrase is absent
# ---------------------------------------------------------------------------


def test_changed_file_context_header_does_not_contain_escape_hatch(tmp_path):
    _write(
        tmp_path,
        "src/pkg/formatting.py",
        "def format_summary(total: int, completed: int) -> str:\n    raise NotImplementedError\n",
    )
    envelope = _envelope(tmp_path)

    rendered, _ = build_bounded_debug_repair_changed_file_context(
        envelope,
        prior_source_paths=["src/pkg/formatting.py"],
    )

    assert "unless the failure requires a change" not in rendered


# ---------------------------------------------------------------------------
# Test 3: signature rule present in prompt when source_contract branch fires
# ---------------------------------------------------------------------------


def test_signature_rule_present_in_source_contract_branch(tmp_path):
    """source_contract branch fires for pytest_failure with extractable contract."""
    _write(
        tmp_path,
        "src/pkg/formatting.py",
        "def format_summary(total: int, completed: int) -> str:\n    raise NotImplementedError\n",
    )
    _write(
        tmp_path,
        "tests/test_formatting.py",
        "from src.pkg.formatting import format_summary\n\ndef test_fmt():\n    assert format_summary(total=3, completed=2) == '3 tasks, 2 done'\n",
    )

    envelope = _envelope(
        tmp_path,
        changed_files=["src/pkg/formatting.py"],
    )

    result = build_bounded_debug_repair_prompt_with_metadata(
        envelope,
        prior_source_paths=["src/pkg/formatting.py"],
        source_edit_context=True,
    )

    assert (
        "If a function whose definition appears in the source excerpts above raises NotImplementedError"
        in result.prompt
    )
    assert "implement its body only" in result.prompt
    assert "Do not change its parameter list" in result.prompt


# ---------------------------------------------------------------------------
# Test 4: signature rule present when source_contract branch does NOT fire
# ---------------------------------------------------------------------------


def test_signature_rule_present_in_no_source_contract_branch(tmp_path):
    """No-source-contract branch fires for non-pytest failure classes."""
    _write(
        tmp_path, "src/pkg/store.py", "def summary():\n    raise NotImplementedError\n"
    )
    envelope = _envelope(
        tmp_path,
        failure_class="missing_dependency",
        failed_command="npm install",
    )

    result = build_bounded_debug_repair_prompt_with_metadata(
        envelope,
        prior_source_paths=["src/pkg/store.py"],
    )

    assert (
        "If a function whose definition appears in the source excerpts above raises NotImplementedError"
        in result.prompt
    )
    assert "implement its body only" in result.prompt
    assert "Do not change its parameter list" in result.prompt


# ---------------------------------------------------------------------------
# Test 5: medium_cli-style prompt with format_summary stub includes the rule
# ---------------------------------------------------------------------------


def test_medium_cli_style_prompt_with_format_summary_stub_includes_rule(tmp_path):
    """Replicates the M10 scenario: all 3 source files visible, stub sig present."""
    _write(
        tmp_path,
        "src/medium_cli/formatting.py",
        (
            "from src.medium_cli.store import TaskStore\n\n"
            "def format_summary(total: int, completed: int) -> str:\n"
            "    raise NotImplementedError\n\n"
            "def format_task_line(task, include_status: bool = True) -> str:\n"
            "    return f'{task.id}: {task.title}'\n"
        ),
    )
    _write(
        tmp_path,
        "src/medium_cli/store.py",
        (
            "class TaskStore:\n"
            "    def __init__(self): self._tasks = []\n"
            "    def all(self): return list(self._tasks)\n"
            "    def completed(self): return [t for t in self._tasks if t.done]\n"
            "    def summary(self):\n"
            "        raise NotImplementedError\n"
        ),
    )
    _write(
        tmp_path,
        "src/medium_cli/cli.py",
        (
            "from src.medium_cli.formatting import format_summary\n"
            "from src.medium_cli.store import TaskStore\n\n"
            "def main(argv=None): pass\n"
        ),
    )

    envelope = _envelope(
        tmp_path,
        failure_class="pytest_failure",
        pytest_excerpt=(
            "FAILED tests/test_cli.py::test_summary - TypeError: format_summary() got "
            "unexpected keyword argument 'total'\n"
            "src/medium_cli/formatting.py:4: NotImplementedError"
        ),
        changed_files=["src/medium_cli/formatting.py", "src/medium_cli/cli.py"],
    )

    result = build_bounded_debug_repair_prompt_with_metadata(
        envelope,
        prior_source_paths=[
            "src/medium_cli/formatting.py",
            "src/medium_cli/cli.py",
            "src/medium_cli/store.py",
        ],
    )

    # source files visible
    assert "format_summary(total: int, completed: int)" in result.prompt
    # authoritative header present
    assert "Existing function signatures shown here are authoritative" in result.prompt
    # signature preservation rule present
    assert "implement its body only" in result.prompt
    assert "Do not change its parameter list" in result.prompt
    # old escape hatch absent from entire prompt
    assert "unless the failure requires a change" not in result.prompt


# ---------------------------------------------------------------------------
# Test 6: no-source-contract branch — zero_test_collect_only rule numbering
# ---------------------------------------------------------------------------


def test_no_src_contract_zero_test_path_has_both_rules_numbered_correctly(tmp_path):
    """When zero-test-collect fires, sig rule is 12 (not colliding with zero_test rule 11)."""
    _write(tmp_path, "src/pkg/cli.py", "def main(): pass\n")
    envelope = _envelope(
        tmp_path,
        failure_class="pytest_failure",
        failed_command="python3 -m pytest --collect-only -q",
        return_code=5,
        stdout_excerpt="collected 0 items",
        stderr_excerpt="",
    )

    result = build_bounded_debug_repair_prompt_with_metadata(
        envelope,
        prior_source_paths=["src/pkg/cli.py"],
    )

    prompt = result.prompt
    # zero_test rule (11) present
    assert "11. Zero tests were collected." in prompt
    # sig rule (12) also present
    assert (
        "12. If a function whose definition appears in the source excerpts above"
        in prompt
    )
