"""Tests for Phase 7L WorkspaceEvidenceCapsule."""

from __future__ import annotations

import subprocess
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

from app.services.orchestration.diagnostics.evidence_capsule import (
    WorkspaceEvidenceCapsule,
    _commands_for_failure_class,
    _extract_module_name,
    _sanitize_output,
    _truncate,
    collect_workspace_evidence,
    render_evidence_section,
)

# ── _truncate ─────────────────────────────────────────────────────────────────


def test_truncate_short_text_unchanged():
    assert _truncate("hello", 100) == "hello"


def test_truncate_long_text_adds_ellipsis():
    result = _truncate("x" * 400, 350)
    assert len(result) == 350
    assert result.endswith("...")


def test_truncate_strips_whitespace():
    assert _truncate("  hello  ", 100) == "hello"


# ── _extract_module_name ───────────────────────────────────────────────────────


def test_extract_module_name_no_module():
    assert _extract_module_name("No module named 'app.config'") == "app"


def test_extract_module_name_import_line():
    assert _extract_module_name("from fastapi import FastAPI") == "fastapi"


def test_extract_module_name_none_on_empty():
    assert _extract_module_name("") is None


def test_extract_module_name_none_on_unrelated():
    assert _extract_module_name("some random error text") is None


# ── _commands_for_failure_class ────────────────────────────────────────────────


def test_commands_module_not_found_contains_find():
    cmds = _commands_for_failure_class(
        "module_not_found", Path("."), "No module named 'requests'"
    )
    flat = [" ".join(c) for c in cmds]
    assert any("find" in c for c in flat)


def test_commands_module_not_found_with_module_contains_grep():
    cmds = _commands_for_failure_class(
        "module_not_found", Path("."), "No module named 'requests'"
    )
    flat = [" ".join(c) for c in cmds]
    assert any("grep" in c and "requests" in c for c in flat)


def test_commands_import_error_contains_grep():
    cmds = _commands_for_failure_class(
        "import_error", Path("."), "from app import config"
    )
    flat = [" ".join(c) for c in cmds]
    assert any("grep" in c for c in flat)


def test_commands_pytest_failure_contains_pytest():
    cmds = _commands_for_failure_class("pytest_failure", Path("."), "")
    flat = [" ".join(c) for c in cmds]
    assert all("pytest" not in c for c in flat)
    assert any(c.startswith("find") for c in flat)
    assert any(c.startswith("grep") for c in flat)


def test_commands_missing_dependency_contains_find_requirements():
    cmds = _commands_for_failure_class("missing_dependency", Path("."), "")
    flat = [" ".join(c) for c in cmds]
    assert any("requirements" in c for c in flat)


def test_commands_unknown_returns_find():
    cmds = _commands_for_failure_class("unknown", Path("."), "")
    assert len(cmds) >= 1
    assert any("find" in " ".join(c) for c in cmds)


# ── WorkspaceEvidenceCapsule ───────────────────────────────────────────────────


def test_capsule_is_empty_when_no_results():
    capsule = WorkspaceEvidenceCapsule(failure_class="unknown")
    assert capsule.is_empty()


def test_capsule_not_empty_when_has_results():
    capsule = WorkspaceEvidenceCapsule(
        failure_class="pytest_failure",
        results={"find .": "app/main.py"},
        total_chars=12,
    )
    assert not capsule.is_empty()


# ── collect_workspace_evidence ─────────────────────────────────────────────────


def test_collect_graceful_on_subprocess_failure(tmp_path):
    with patch(
        "app.services.orchestration.diagnostics.evidence_capsule._run_cmd",
        return_value="",
    ):
        capsule = collect_workspace_evidence("module_not_found", tmp_path)
    assert isinstance(capsule, WorkspaceEvidenceCapsule)
    assert capsule.failure_class == "module_not_found"
    assert capsule.total_chars == 0


def test_collect_records_commands_run(tmp_path):
    with patch(
        "app.services.orchestration.diagnostics.evidence_capsule._run_cmd",
        return_value="",
    ):
        capsule = collect_workspace_evidence("pytest_failure", tmp_path)
    assert len(capsule.commands_run) >= 1


def test_collect_budget_enforcement(tmp_path):
    long_output = "x" * 400

    with patch(
        "app.services.orchestration.diagnostics.evidence_capsule._run_cmd",
        return_value=long_output,
    ):
        capsule = collect_workspace_evidence("module_not_found", tmp_path)

    assert capsule.total_chars <= 1500


def test_collect_per_command_truncation(tmp_path):
    long_output = "y" * 400

    with patch(
        "app.services.orchestration.diagnostics.evidence_capsule._run_cmd",
        return_value=long_output,
    ):
        capsule = collect_workspace_evidence("pytest_failure", tmp_path)

    for output in capsule.results.values():
        if output:
            assert len(output) <= 350


def test_collect_empty_capsule_on_all_failures(tmp_path):
    with patch(
        "app.services.orchestration.diagnostics.evidence_capsule.subprocess.run",
        side_effect=subprocess.TimeoutExpired(cmd="find", timeout=5),
    ):
        capsule = collect_workspace_evidence("syntax_error", tmp_path)
    assert capsule.total_chars == 0


def test_collect_pytest_failure_does_not_mutate_workspace(tmp_path):
    before = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}

    collect_workspace_evidence("pytest_failure", tmp_path)

    after = {path.relative_to(tmp_path) for path in tmp_path.rglob("*")}
    assert after == before


def test_collect_pytest_argparse_failure_includes_imported_source(tmp_path):
    src_dir = tmp_path / "src" / "small_cli"
    src_dir.mkdir(parents=True)
    (src_dir / "__init__.py").write_text("", encoding="utf-8")
    (src_dir / "cli.py").write_text(
        "import argparse\n"
        "\n"
        "def build_parser():\n"
        "    parser = argparse.ArgumentParser()\n"
        "    parser.add_argument('message')\n"
        "    return parser\n",
        encoding="utf-8",
    )
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    (tests_dir / "test_cli.py").write_text(
        "from small_cli.cli import build_parser\n"
        "\n"
        "def test_uppercase():\n"
        "    assert build_parser().parse_args(['--uppercase', 'hello'])\n",
        encoding="utf-8",
    )

    capsule = collect_workspace_evidence(
        "pytest_failure",
        tmp_path,
        failure_context=(
            "pytest: error: unrecognized arguments: --uppercase\n"
            "usage: pytest [-h] message"
        ),
    )

    rendered = render_evidence_section(capsule)
    assert "source excerpt imported by failing tests: src/small_cli/cli.py" in rendered
    assert "def build_parser" in rendered
    assert "./src/small_cli/cli.py" in capsule.files_inspected


def test_commands_use_portable_find_and_grep_only():
    failure_classes = [
        "module_not_found",
        "import_error",
        "pytest_failure",
        "syntax_error",
        "missing_dependency",
        "completion_validation_failed",
    ]
    for failure_class in failure_classes:
        cmds = _commands_for_failure_class(failure_class, Path("."), "")
        assert cmds
        assert {cmd[0] for cmd in cmds} <= {"find", "grep"}


def test_sanitize_output_drops_secret_lines():
    output = _sanitize_output(
        "app/main.py\n.env:SECRET_KEY=abc\nconfig.py:API_TOKEN=def\nsafe.txt"
    )
    assert "app/main.py" in output
    assert "safe.txt" in output
    assert ".env" not in output
    assert "SECRET_KEY" not in output
    assert "API_TOKEN" not in output


# ── render_evidence_section ───────────────────────────────────────────────────


def test_render_empty_capsule_returns_empty_string():
    capsule = WorkspaceEvidenceCapsule(failure_class="unknown")
    assert render_evidence_section(capsule) == ""


def test_render_capsule_with_results_includes_command():
    capsule = WorkspaceEvidenceCapsule(
        failure_class="pytest_failure",
        results={"python3 -m pytest -q": "1 failed"},
        total_chars=8,
    )
    section = render_evidence_section(capsule)
    assert "Workspace evidence:" in section
    assert "python3 -m pytest -q" in section
    assert "1 failed" in section


def test_render_skips_empty_command_results():
    capsule = WorkspaceEvidenceCapsule(
        failure_class="module_not_found",
        commands_run=["find . -name '*.py'", "grep -rn import app ."],
        results={"find . -name '*.py'": "", "grep -rn import app .": "app/main.py"},
        total_chars=11,
    )
    section = render_evidence_section(capsule)
    assert "find . -name '*.py'" not in section
    assert "app/main.py" in section


# ── Integration: prompt injection check ───────────────────────────────────────


def test_build_bounded_debug_repair_prompt_includes_evidence():
    from app.services.orchestration.diagnostics.debug_feedback import (
        DebugFeedbackEnvelope,
        build_bounded_debug_repair_prompt,
    )

    envelope = DebugFeedbackEnvelope(
        task_execution_id=1,
        task_id=1,
        step_index=0,
        failure_phase="execution",
        failure_class="pytest_failure",
        workspace_path=".",
    )
    capsule = WorkspaceEvidenceCapsule(
        failure_class="pytest_failure",
        results={"python3 -m pytest -q": "1 failed in 0.1s"},
        total_chars=18,
    )
    prompt = build_bounded_debug_repair_prompt(envelope, capsule)
    assert "Workspace evidence:" in prompt
    assert "1 failed" in prompt


def test_build_bounded_debug_repair_prompt_no_evidence_section_when_none():
    from app.services.orchestration.diagnostics.debug_feedback import (
        DebugFeedbackEnvelope,
        build_bounded_debug_repair_prompt,
    )

    envelope = DebugFeedbackEnvelope(
        task_execution_id=1,
        task_id=1,
        step_index=0,
        failure_phase="execution",
        failure_class="pytest_failure",
        workspace_path=".",
    )
    prompt = build_bounded_debug_repair_prompt(envelope, None)
    assert "Workspace evidence:" not in prompt


def test_build_bounded_completion_repair_prompt_includes_evidence():
    from app.services.orchestration.phases.completion_repair_capsule import (
        CompletionRepairCapsule,
        build_bounded_completion_repair_prompt,
    )

    repair_capsule = CompletionRepairCapsule(
        validation_reasons=["missing file app/main.py"],
        relevant_files=["app/main.py"],
        last_step_summary="Step 1: done",
        workspace_path=".",
        task_prompt_excerpt="Build a FastAPI app",
    )
    evidence = WorkspaceEvidenceCapsule(
        failure_class="completion_validation_failed",
        results={"find . -maxdepth 2": "app/main.py"},
        total_chars=11,
    )
    prompt = build_bounded_completion_repair_prompt(repair_capsule, 2, evidence)
    assert "Workspace evidence:" in prompt
    assert "app/main.py" in prompt
