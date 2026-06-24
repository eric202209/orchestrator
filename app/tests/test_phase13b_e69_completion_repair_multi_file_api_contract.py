"""Phase 13B-E69: Completion repair multi-file coherence and API contract prompt tests.

Verifies:
  1. _extract_source_api_contract extracts top-level function signatures.
  2. _extract_source_api_contract extracts class method signatures.
  3. TaskStore.all, TaskStore.summary, and format_summary(total, completed) appear for
     medium_cli-style source_file_contents.
  4. Prompt contains SOURCE API CONTRACT section.
  5. Prompt contains rule prohibiting invented attributes/methods such as .tasks.
  6. Prompt contains rule prohibiting wrong function argument shapes.
  7. Prompt contains rule requiring implementation of visible NotImplementedError bodies.
  8. Prompt contains multi-file repair/coherence rule.
  9. Prompt still contains CURRENT FILE CONTENT.
  10. Prompt still contains Rules 12-13 (exact old text / write_file escape hatch).
  11. Prompt still contains ops_fix schema (E61).
  12. Prompt still excludes "commands": [ (E61).
  Plus additional edge-case and regression tests.
"""

from __future__ import annotations

from pathlib import Path

from app.services.orchestration.phases.completion_repair_capsule import (
    _SOURCE_TRUNCATED_MARKER,
    CompletionRepairCapsule,
    _extract_source_api_contract,
    _format_func_sig_from_ast,
    build_bounded_completion_repair_prompt,
)

# ---------------------------------------------------------------------------
# Fixtures: synthetic medium_cli source files
# ---------------------------------------------------------------------------

STORE_PY = """\
from typing import NamedTuple


class Task(NamedTuple):
    title: str
    completed: bool = False


class TaskStore:
    def __init__(self) -> None:
        self._tasks: list[Task] = []

    def add(self, title: str) -> Task:
        t = Task(title)
        self._tasks.append(t)
        return t

    def all(self) -> list[Task]:
        return list(self._tasks)

    def completed(self) -> list[Task]:
        return [t for t in self._tasks if t.completed]

    def summary(self) -> tuple[int, int]:
        raise NotImplementedError
"""

FORMATTING_PY = """\
def format_summary(total: int, completed: int) -> str:
    return f"{completed}/{total} tasks completed"


def format_task_line(task, *, include_status: bool = False) -> str:
    return task.title
"""

CLI_PY = """\
import argparse


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser()
    return 0
"""


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_capsule(tmp_path: Path, **kwargs) -> CompletionRepairCapsule:
    defaults = dict(
        validation_reasons=["pytest failed"],
        relevant_files=["src/medium_cli/cli.py"],
        last_step_summary="Step 3: wrote cli.py - success. Files: src/medium_cli/cli.py.",
        workspace_path=str(tmp_path),
        task_prompt_excerpt="Add summary command to CLI",
    )
    defaults.update(kwargs)
    return CompletionRepairCapsule(**defaults)


def _make_medium_cli_capsule(tmp_path: Path) -> CompletionRepairCapsule:
    return _make_capsule(
        tmp_path,
        relevant_files=[
            "src/medium_cli/formatting.py",
            "src/medium_cli/cli.py",
            "src/medium_cli/store.py",
        ],
        source_file_contents={
            "src/medium_cli/formatting.py": FORMATTING_PY,
            "src/medium_cli/cli.py": CLI_PY,
            "src/medium_cli/store.py": STORE_PY,
        },
    )


# ---------------------------------------------------------------------------
# 1. _extract_source_api_contract — top-level function signatures
# ---------------------------------------------------------------------------


def test_contract_includes_top_level_function_signatures():
    """Required test 1: top-level functions appear in the contract."""
    contents = {"src/medium_cli/formatting.py": FORMATTING_PY}
    contract = _extract_source_api_contract(contents)
    assert "format_summary" in contract
    assert "total: int" in contract
    assert "completed: int" in contract


# ---------------------------------------------------------------------------
# 2. _extract_source_api_contract — class method signatures
# ---------------------------------------------------------------------------


def test_contract_includes_class_method_signatures():
    """Required test 2: class methods appear as ClassName.method_name(...)."""
    contents = {"src/medium_cli/store.py": STORE_PY}
    contract = _extract_source_api_contract(contents)
    assert "TaskStore.add" in contract
    assert "TaskStore.all" in contract
    assert "TaskStore.summary" in contract


# ---------------------------------------------------------------------------
# 3. TaskStore.all, TaskStore.summary, format_summary(total, completed) present
# ---------------------------------------------------------------------------


def test_contract_includes_medium_cli_full_api():
    """Required test 3: all E67 relevant APIs appear for medium_cli-style contents."""
    contents = {
        "src/medium_cli/store.py": STORE_PY,
        "src/medium_cli/formatting.py": FORMATTING_PY,
    }
    contract = _extract_source_api_contract(contents)
    assert "TaskStore.all" in contract
    assert "TaskStore.summary" in contract
    assert "format_summary" in contract
    assert "total: int" in contract
    assert "completed: int" in contract


# ---------------------------------------------------------------------------
# 4. Prompt contains SOURCE API CONTRACT section
# ---------------------------------------------------------------------------


def test_prompt_contains_source_api_contract_section(tmp_path):
    """Required test 4: prompt contains SOURCE API CONTRACT header."""
    capsule = _make_medium_cli_capsule(tmp_path)
    prompt = build_bounded_completion_repair_prompt(capsule, 4)
    assert "SOURCE API CONTRACT" in prompt


# ---------------------------------------------------------------------------
# 5. Prompt contains rule prohibiting invented attributes such as .tasks
# ---------------------------------------------------------------------------


def test_prompt_contains_rule_prohibiting_invented_attributes(tmp_path):
    """Required test 5: prompt warns against inventing attributes like .tasks."""
    capsule = _make_medium_cli_capsule(tmp_path)
    prompt = build_bounded_completion_repair_prompt(capsule, 4)
    assert ".tasks" in prompt


# ---------------------------------------------------------------------------
# 6. Prompt contains rule prohibiting wrong function argument shapes
# ---------------------------------------------------------------------------


def test_prompt_contains_rule_prohibiting_wrong_argument_shapes(tmp_path):
    """Required test 6: prompt warns against wrong argument shapes / call signatures."""
    capsule = _make_medium_cli_capsule(tmp_path)
    prompt = build_bounded_completion_repair_prompt(capsule, 4)
    # Rule 16 mentions both "argument" and "signatures"
    assert "argument" in prompt.lower()
    assert "signature" in prompt.lower()


# ---------------------------------------------------------------------------
# 7. Prompt contains rule requiring implementation of NotImplementedError bodies
# ---------------------------------------------------------------------------


def test_prompt_contains_rule_requiring_stub_implementation(tmp_path):
    """Required test 7: prompt requires implementing bodies that raise NotImplementedError."""
    capsule = _make_medium_cli_capsule(tmp_path)
    prompt = build_bounded_completion_repair_prompt(capsule, 4)
    assert "NotImplementedError" in prompt


# ---------------------------------------------------------------------------
# 8. Prompt contains multi-file repair/coherence rule
# ---------------------------------------------------------------------------


def test_prompt_contains_multi_file_repair_rule(tmp_path):
    """Required test 8: prompt requires ops across multiple files when needed."""
    capsule = _make_medium_cli_capsule(tmp_path)
    prompt = build_bounded_completion_repair_prompt(capsule, 4)
    # Rule 14 says "every file" and "multiple ops across multiple files"
    assert "every file" in prompt or "multiple" in prompt.lower()


# ---------------------------------------------------------------------------
# 9. Prompt still contains CURRENT FILE CONTENT
# ---------------------------------------------------------------------------


def test_prompt_still_contains_current_file_content(tmp_path):
    """Required test 9: CURRENT FILE CONTENT section still present."""
    capsule = _make_medium_cli_capsule(tmp_path)
    prompt = build_bounded_completion_repair_prompt(capsule, 4)
    assert "CURRENT FILE CONTENT" in prompt


# ---------------------------------------------------------------------------
# 10. Rules 12–13 still present (exact old text / write_file escape hatch)
# ---------------------------------------------------------------------------


def test_prompt_still_contains_rule_12_character_for_character(tmp_path):
    """Required test 10a: Rule 12 exact-copy instruction still present."""
    capsule = _make_medium_cli_capsule(tmp_path)
    prompt = build_bounded_completion_repair_prompt(capsule, 4)
    assert "character-for-character" in prompt


def test_prompt_still_contains_rule_13_write_file_escape_hatch(tmp_path):
    """Required test 10b: Rule 13 write_file escape hatch still present."""
    capsule = _make_medium_cli_capsule(tmp_path)
    prompt = build_bounded_completion_repair_prompt(capsule, 4)
    assert "Do not invent" in prompt or "Do not guess" in prompt


# ---------------------------------------------------------------------------
# 11. Prompt still contains ops_fix schema (E61)
# ---------------------------------------------------------------------------


def test_prompt_still_contains_ops_fix_schema(tmp_path):
    """Required test 11: ops_fix repair_type still required."""
    capsule = _make_medium_cli_capsule(tmp_path)
    prompt = build_bounded_completion_repair_prompt(capsule, 4)
    assert "ops_fix" in prompt


# ---------------------------------------------------------------------------
# 12. Prompt still excludes "commands": [ (E61)
# ---------------------------------------------------------------------------


def test_prompt_still_excludes_commands_key(tmp_path):
    """Required test 12: prompt still excludes the commands array key."""
    capsule = _make_medium_cli_capsule(tmp_path)
    prompt = build_bounded_completion_repair_prompt(capsule, 4)
    assert '"commands": [' not in prompt


# ---------------------------------------------------------------------------
# Additional: API contract content in prompt
# ---------------------------------------------------------------------------


def test_prompt_api_contract_contains_task_store_all(tmp_path):
    """TaskStore.all appears in the SOURCE API CONTRACT block in the prompt."""
    capsule = _make_medium_cli_capsule(tmp_path)
    prompt = build_bounded_completion_repair_prompt(capsule, 4)
    assert "TaskStore.all" in prompt


def test_prompt_api_contract_contains_format_summary_sig(tmp_path):
    """format_summary(total: int, completed: int) appears in the prompt."""
    capsule = _make_medium_cli_capsule(tmp_path)
    prompt = build_bounded_completion_repair_prompt(capsule, 4)
    assert "format_summary" in prompt
    assert "total: int" in prompt
    assert "completed: int" in prompt


# ---------------------------------------------------------------------------
# Additional: contract absent when no Python source_file_contents
# ---------------------------------------------------------------------------


_CONTRACT_HEADER = "SOURCE API CONTRACT (derived from files above"


def test_api_contract_section_absent_when_source_contents_empty(tmp_path):
    """SOURCE API CONTRACT section header absent when source_file_contents is empty.

    Rules 15-16 still mention SOURCE API CONTRACT in rule text, but the dedicated
    section header (which includes the parenthetical) must not appear.
    """
    capsule = _make_capsule(tmp_path, source_file_contents={})
    prompt = build_bounded_completion_repair_prompt(capsule, 4)
    assert _CONTRACT_HEADER not in prompt


def test_api_contract_section_absent_for_non_python_only_contents(tmp_path):
    """SOURCE API CONTRACT section header absent when only non-Python files present."""
    capsule = _make_capsule(
        tmp_path,
        source_file_contents={"README.md": "# docs\n", "config.json": "{}"},
    )
    prompt = build_bounded_completion_repair_prompt(capsule, 4)
    assert _CONTRACT_HEADER not in prompt


# ---------------------------------------------------------------------------
# Additional: _extract_source_api_contract edge cases
# ---------------------------------------------------------------------------


def test_contract_skips_non_python_files():
    """Non-Python files (.md, .json) are silently skipped."""
    contents = {"src/README.md": "# Docs\n", "src/config.json": "{}"}
    contract = _extract_source_api_contract(contents)
    assert contract == ""


def test_contract_skips_files_with_syntax_errors():
    """Files that fail to parse are silently skipped."""
    contents = {"src/broken.py": "def this is not valid python:::\n"}
    contract = _extract_source_api_contract(contents)
    assert contract == ""


def test_contract_strips_truncation_marker_before_parsing():
    """Truncation marker at end of content is stripped before AST parse."""
    partial = "def my_func(x: int) -> str:\n    pass\n" + _SOURCE_TRUNCATED_MARKER
    contents = {"src/partial.py": partial}
    contract = _extract_source_api_contract(contents)
    assert "my_func" in contract


def test_contract_includes_return_type_annotation():
    """Return type annotations appear in the extracted signatures."""
    contents = {"src/medium_cli/store.py": STORE_PY}
    contract = _extract_source_api_contract(contents)
    # summary() -> tuple[int, int]
    assert "tuple" in contract


def test_contract_includes_keyword_only_args():
    """Keyword-only args (after *) appear in signatures with * separator."""
    contents = {"src/medium_cli/formatting.py": FORMATTING_PY}
    contract = _extract_source_api_contract(contents)
    # format_task_line has *, include_status: bool = False
    assert "format_task_line" in contract
    assert "include_status" in contract


def test_contract_empty_for_empty_source_file_contents():
    """Empty dict returns empty string."""
    assert _extract_source_api_contract({}) == ""


def test_contract_lists_per_file_header():
    """Each Python file gets its own header line in the contract."""
    contents = {
        "src/medium_cli/store.py": STORE_PY,
        "src/medium_cli/formatting.py": FORMATTING_PY,
    }
    contract = _extract_source_api_contract(contents)
    assert "src/medium_cli/store.py" in contract
    assert "src/medium_cli/formatting.py" in contract


def test_prompt_source_api_contract_precedes_rules(tmp_path):
    """SOURCE API CONTRACT appears before the Rules block in the prompt."""
    capsule = _make_medium_cli_capsule(tmp_path)
    prompt = build_bounded_completion_repair_prompt(capsule, 4)
    contract_pos = prompt.index("SOURCE API CONTRACT")
    rules_pos = prompt.index("\nRules:\n")
    assert contract_pos < rules_pos
