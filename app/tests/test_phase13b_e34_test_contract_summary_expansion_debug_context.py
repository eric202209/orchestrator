"""Phase 13B-E34: TEST CONTRACT SUMMARY Expansion and Debug Repair Context Injection.

Tests for:
- E33-I1: _expected_behavior_lines returns up to 5 (was 3)
- E33-I2: assemble_debugging_prompt includes TEST CONTRACT SUMMARY when tests exist
"""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace

from app.services.orchestration.context.assembly import (
    DebugPromptInputs,
    assemble_debugging_prompt,
)
from app.services.project.source_imports import (
    _expected_behavior_lines,
    extract_python_test_contract,
    render_python_test_contract_summary,
)
from app.services.prompt_templates import OrchestrationState


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _make_medium_cli_fixture(tmp_path: Path) -> Path:
    """Create a medium_cli-like project with 8 assertions across 3 test files."""
    project = tmp_path / "medium_cli"
    _write(project / "src" / "medium_cli" / "__init__.py", "")
    _write(
        project / "src" / "medium_cli" / "cli.py",
        "def main(argv=None):\n    return 0\n",
    )
    _write(project / "src" / "medium_cli" / "store.py", "class TaskStore:\n    pass\n")
    _write(
        project / "src" / "medium_cli" / "formatting.py",
        "def format_summary(total, completed):\n    raise NotImplementedError\n",
    )
    _write(
        project / "tests" / "test_cli.py",
        "from medium_cli.cli import main\n"
        "import argparse\n"
        "\n"
        "def test_args():\n"
        "    args = argparse.Namespace(command='list')\n"
        "    assert args.command == 'list'\n"
        "    assert main(['list']) == 0\n"
        "    output = ['write docs', 'ship feature', 'close ticket']\n"
        "    assert output == ['write docs', 'ship feature', 'close ticket']\n",
    )
    _write(
        project / "tests" / "test_store.py",
        "from medium_cli.store import TaskStore\n"
        "\n"
        "def test_completed():\n"
        "    store = TaskStore()\n"
        "    assert [task.title for task in store.completed()] == ['write docs']\n",
    )
    _write(
        project / "tests" / "test_summary.py",
        "from medium_cli.store import TaskStore\n"
        "from medium_cli.formatting import format_summary\n"
        "from medium_cli.cli import main\n"
        "\n"
        "def test_summary(capsys):\n"
        "    store = TaskStore()\n"
        "    assert store.summary() == (3, 2)\n"
        "    assert format_summary(total=3, completed=2) == '3 tasks, 2 complete'\n"
        "    assert main(['summary']) == 0\n"
        "    assert capsys.readouterr().out.strip() == '3 tasks, 2 complete'\n",
    )
    return project


def _make_debug_ctx(project_dir: Path) -> SimpleNamespace:
    state = OrchestrationState(
        session_id="99",
        task_description="Implement summary command",
        project_name="medium_cli",
        project_context="",
        task_id=42,
    )
    state._project_dir_override = str(project_dir)
    return SimpleNamespace(
        db=None,
        prompt="Implement summary command",
        execution_profile="full_lifecycle",
        workflow_profile="default",
        orchestration_state=state,
    )


# --- E33-I1 tests ---


def test_expected_behavior_lines_returns_up_to_5(tmp_path: Path):
    project = _make_medium_cli_fixture(tmp_path)
    contract = extract_python_test_contract(project)
    assert contract is not None
    result = _expected_behavior_lines(contract)
    assert len(result) == 5


def test_render_python_test_contract_summary_shows_5_behavior_lines(tmp_path: Path):
    project = _make_medium_cli_fixture(tmp_path)
    contract = extract_python_test_contract(project)
    assert contract is not None
    summary = render_python_test_contract_summary(contract)
    behavior_section = (
        summary.split("Expected behavior:")[1]
        if "Expected behavior:" in summary
        else ""
    )
    # Exclude the "Summary truncated" notice which also uses "- " prefix
    behavior_items = [
        line.strip()
        for line in behavior_section.splitlines()
        if line.strip().startswith("-") and "truncated" not in line.lower()
    ]
    assert len(behavior_items) == 5


def test_medium_cli_assertion_rank_5_appears_in_rendered_summary(tmp_path: Path):
    project = _make_medium_cli_fixture(tmp_path)
    contract = extract_python_test_contract(project)
    assert contract is not None
    summary = render_python_test_contract_summary(contract)
    assert "store.summary() should equal (3, 2)" in summary


def test_expected_behavior_lines_bounded_when_fewer_than_5_assertions(tmp_path: Path):
    _write(tmp_path / "src" / "pkg" / "__init__.py", "")
    _write(tmp_path / "src" / "pkg" / "mod.py", "def run(x):\n    return x\n")
    _write(
        tmp_path / "tests" / "test_mod.py",
        "from pkg.mod import run\n"
        "\n"
        "def test_run():\n"
        "    assert run('a') == 'A'\n"
        "    assert run('b') == 'B'\n",
    )
    contract = extract_python_test_contract(tmp_path)
    assert contract is not None
    result = _expected_behavior_lines(contract)
    assert len(result) == 2
    assert len(result) <= 5


# --- E33-I2 tests ---


def test_assemble_debugging_prompt_includes_test_contract_when_tests_exist(
    tmp_path: Path,
):
    project = _make_medium_cli_fixture(tmp_path)
    ctx = _make_debug_ctx(project)
    prompt = assemble_debugging_prompt(
        ctx,
        DebugPromptInputs(
            step_description="Run pytest",
            error_message="test_store_summary_counts_total_and_completed FAILED",
            command_output="NotImplementedError",
            verification_output="",
            attempt_number=1,
            max_attempts=2,
        ),
    )
    assert "## TEST CONTRACT SUMMARY" in prompt
    assert "store.summary() should equal (3, 2)" in prompt


def test_assemble_debugging_prompt_omits_test_contract_when_no_tests(tmp_path: Path):
    project = tmp_path / "ts_project"
    project.mkdir()
    (project / "src").mkdir()
    (project / "src" / "main.ts").write_text(
        "export const ok = true;\n", encoding="utf-8"
    )
    ctx = _make_debug_ctx(project)
    prompt = assemble_debugging_prompt(
        ctx,
        DebugPromptInputs(
            step_description="Run tests",
            error_message="failed",
            command_output="",
            verification_output="",
            attempt_number=1,
            max_attempts=2,
        ),
    )
    assert "## TEST CONTRACT SUMMARY" not in prompt


def test_debugging_prompt_knowledge_guidance_unchanged(tmp_path: Path):
    from app.schemas.knowledge import (
        KnowledgeContext,
        KnowledgeItemRef,
        RecommendedAction,
    )

    project = _make_medium_cli_fixture(tmp_path)
    ctx = _make_debug_ctx(project)
    knowledge = KnowledgeContext(
        retrieved_items=[
            KnowledgeItemRef(
                id="e14-heuristic",
                title="Repair heuristic",
                knowledge_type="debug_case",
                content="Inspect the failing assertion before patching.",
                priority=10,
                confidence=0.92,
            )
        ],
        query="assertion failed",
        trigger_phase="failure",
        retrieval_reason="semantic_retrieval",
        confidence=0.92,
        matched_failure_memory=False,
        recommended_action=RecommendedAction.review_failure,
    )
    prompt = assemble_debugging_prompt(
        ctx,
        DebugPromptInputs(
            step_description="Run pytest",
            error_message="FAILED",
            command_output="",
            verification_output="",
            attempt_number=1,
            max_attempts=2,
            knowledge_context=knowledge,
        ),
    )
    assert "## KNOWLEDGE REFERENCES" in prompt
    assert "Repair heuristic" in prompt
    assert "Inspect the failing assertion before patching." in prompt
    assert "## TEST CONTRACT SUMMARY" in prompt
