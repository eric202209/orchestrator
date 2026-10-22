"""Phase 39 B1-R1: test contracts that import a not-yet-created source module.

B1 (TE 393) crashed planning-prompt assembly with FileNotFoundError because a
call to a symbol from a missing module was recorded as an existing source
target and then parsed.
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

import pytest

from app.services.orchestration.context.assembly import assemble_planning_prompt
from app.services.orchestration.prompt_templates import OrchestrationState
from app.services.project import source_imports
from app.services.project.source_imports import (
    extract_python_test_contract,
    python_test_source_context_from_tests,
)

TARGET = "textkit_b1/wordcount.py"

# Verbatim B1 seed test file (seed commit 51ef4a8, sha256 16ec7bad...).
B1_PER_TEST_HELPER = (
    "def _count_words(text):\n"
    "    from textkit_b1.wordcount import count_words\n"
    "\n"
    "    return count_words(text)\n"
    "\n"
    "\n"
    "def test_empty_string_returns_zero():\n"
    '    assert _count_words("") == 0\n'
    "\n"
    "\n"
    "def test_whitespace_only_returns_zero():\n"
    '    assert _count_words("   \\t \\n  ") == 0\n'
    "\n"
    "\n"
    "def test_punctuation_and_multiple_spaces():\n"
    '    assert _count_words("Hello,   world!  It\'s  a test.") == 5\n'
    "\n"
    "\n"
    "def test_newlines_and_tabs():\n"
    '    assert _count_words("one\\ntwo\\tthree\\n\\nfour\\t\\tfive") == 5\n'
)

MODULE_LEVEL = (
    "from textkit_b1.wordcount import count_words\n"
    "\n"
    "\n"
    "def test_empty_string_returns_zero():\n"
    '    assert count_words("") == 0\n'
    "\n"
    "\n"
    "def test_newlines_and_tabs():\n"
    '    assert count_words("one\\ntwo") == 2\n'
)

PER_TEST_IMPORT = (
    "def test_empty_string_returns_zero():\n"
    "    from textkit_b1.wordcount import count_words\n"
    "\n"
    '    assert count_words("") == 0\n'
    "\n"
    "\n"
    "def test_newlines_and_tabs():\n"
    "    from textkit_b1.wordcount import count_words\n"
    "\n"
    '    assert count_words("one\\ntwo") == 2\n'
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _b1_project(root: Path, test_text: str = B1_PER_TEST_HELPER) -> Path:
    _write(root / "textkit_b1" / "__init__.py", "")
    _write(root / "tests" / "test_wordcount.py", test_text)
    return root


def _paths(targets) -> list[str]:
    return [path for path, _reason in targets]


def _assert_missing_target_only(contract) -> None:
    assert contract is not None
    assert TARGET in _paths(contract.missing_source_targets)
    assert TARGET not in _paths(contract.source_targets)
    reason = dict(contract.missing_source_targets)[TARGET]
    assert "imports missing textkit_b1.wordcount.count_words" in reason


@pytest.mark.parametrize(
    "test_text",
    [B1_PER_TEST_HELPER, MODULE_LEVEL, PER_TEST_IMPORT],
    ids=["b1_fixture", "module_level_import", "per_test_import"],
)
def test_missing_imported_module_is_a_creation_target_not_a_crash(
    tmp_path: Path, test_text: str
):
    project = _b1_project(tmp_path, test_text)
    assert not (project / TARGET).exists()

    contract = extract_python_test_contract(project)

    _assert_missing_target_only(contract)
    assert not (project / TARGET).exists()


def test_b1_summary_marks_missing_module_for_creation_once(tmp_path: Path):
    summary = python_test_source_context_from_tests(_b1_project(tmp_path))

    assert f"- {TARGET} (create missing source module)" in summary
    assert f"- {TARGET}\n" not in summary
    assert summary.count(TARGET) == 1


def test_existing_valid_source_is_parsed_as_existing_target(tmp_path: Path):
    project = _b1_project(tmp_path, MODULE_LEVEL)
    _write(
        project / TARGET,
        "from textkit_b1.tokens import split_tokens\n"
        "\n"
        "def count_words(text):\n"
        "    return len(split_tokens(text))\n",
    )

    contract = extract_python_test_contract(project)

    assert contract is not None
    assert TARGET in _paths(contract.source_targets)
    assert TARGET not in _paths(contract.missing_source_targets)
    reasons = dict(contract.source_targets)[TARGET]
    assert "imports textkit_b1.wordcount.count_words" in reasons
    assert 'calls count_words("")' in reasons
    # The existing source is still parsed for its own missing project imports.
    assert "textkit_b1/tokens.py" in _paths(contract.missing_source_targets)


def test_existing_source_with_syntax_error_keeps_existing_contract(tmp_path: Path):
    project = _b1_project(tmp_path, MODULE_LEVEL)
    _write(project / TARGET, "def count_words(text:\n")

    contract = extract_python_test_contract(project)

    assert contract is not None
    assert TARGET in _paths(contract.source_targets)
    assert _paths(contract.missing_source_targets) == []


def test_unexpected_read_failure_on_existing_source_still_propagates(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    project = _b1_project(tmp_path, MODULE_LEVEL)
    _write(project / TARGET, "def count_words(text):\n    return 0\n")
    original = source_imports._safe_read_text

    def _read(path: Path) -> str:
        if Path(path).name == "wordcount.py":
            raise PermissionError(13, "Permission denied", str(path))
        return original(path)

    monkeypatch.setattr(source_imports, "_safe_read_text", _read)

    with pytest.raises(PermissionError):
        extract_python_test_contract(project)


def test_import_target_helper_error_contract_is_unchanged(tmp_path: Path):
    with pytest.raises(FileNotFoundError):
        source_imports._source_import_missing_targets(
            tmp_path, tmp_path / "absent.py", {"absent"}
        )


def test_missing_targets_never_read_outside_project_root(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
):
    project = tmp_path / "project"
    outside = tmp_path / "outside"
    _write(outside / "wordcount.py", "def count_words(text):\n    return 0\n")
    _b1_project(project, MODULE_LEVEL)
    (project / "textkit_b1" / "__init__.py").unlink()
    (project / "textkit_b1").rmdir()
    (project / "textkit_b1").symlink_to(outside, target_is_directory=True)
    reads: list[Path] = []
    original = source_imports._safe_read_text

    def _read(path: Path) -> str:
        reads.append(Path(path).resolve())
        return original(path)

    monkeypatch.setattr(source_imports, "_safe_read_text", _read)

    contract = extract_python_test_contract(project)

    root = project.resolve()
    assert reads and all(path.is_relative_to(root) for path in reads)
    if contract is not None:
        for path in _paths(contract.source_targets) + _paths(
            contract.missing_source_targets
        ):
            assert (root / path).resolve().is_relative_to(root)


def _planning_context(project_dir: Path) -> SimpleNamespace:
    prompt = "Create textkit_b1/wordcount.py defining count_words(text: str) -> int."
    state = OrchestrationState(
        session_id="b1-r1",
        task_description=prompt,
        project_name="phase39-b1-r1",
        project_context="",
        task_id=301,
    )
    state._project_dir_override = str(project_dir)
    return SimpleNamespace(
        db=None,
        prompt=prompt,
        execution_profile="full_lifecycle",
        workflow_profile="default",
        orchestration_state=state,
    )


def test_b1_fixture_passes_planning_prompt_assembly(tmp_path: Path):
    project = _b1_project(tmp_path)

    prompt = assemble_planning_prompt(_planning_context(project), {})

    assert "## TEST CONTRACT SUMMARY" in prompt
    assert f"- {TARGET} (create missing source module)" in prompt
    assert not (project / TARGET).exists()
