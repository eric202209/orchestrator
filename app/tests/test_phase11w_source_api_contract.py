from app.services.orchestration.planning.source_api_contract import (
    build_source_api_contract_capsule,
)
from app.services.orchestration.diagnostics.debug_feedback import (
    DebugFeedbackEnvelope,
    build_bounded_debug_repair_prompt_with_metadata,
)
from app.services.orchestration.planning import repair_prompts
from app.services.orchestration.planning.repair_prompts import (
    build_planning_repair_prompt_with_metadata,
)
from app.services.orchestration.planning.planner import PlannerService


def test_source_api_contract_capsule_collects_python_source_api(tmp_path):
    source_dir = tmp_path / "src" / "medium_cli"
    tests_dir = tmp_path / "tests"
    source_dir.mkdir(parents=True)
    tests_dir.mkdir()
    (source_dir / "__init__.py").write_text("", encoding="utf-8")
    (source_dir / "cli.py").write_text(
        "\n".join(
            [
                '"""CLI module."""',
                "",
                "import argparse",
                "",
                "class CliRunner:",
                "    pass",
                "",
                "def build_parser():",
                "    return argparse.ArgumentParser()",
                "",
                "def main(argv=None):",
                "    return 0",
                "",
                "def _private_helper():",
                "    return None",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (source_dir / "store.py").write_text(
        "class TaskStore:\n    pass\n\n" "def load_tasks():\n    return []\n",
        encoding="utf-8",
    )
    (tests_dir / "test_cli.py").write_text(
        "from medium_cli.cli import build_parser, main\n"
        "from medium_cli.store import TaskStore\n"
        "\n"
        "def test_cli():\n"
        "    assert main([]) == 0\n",
        encoding="utf-8",
    )

    capsule = build_source_api_contract_capsule(tmp_path)
    payload = capsule.to_dict()

    assert payload["framework_family"] == "argparse"
    assert payload["source_modules"] == [
        "src/medium_cli/__init__.py",
        "src/medium_cli/cli.py",
        "src/medium_cli/store.py",
    ]
    assert payload["public_symbols"]["medium_cli.cli"] == [
        "CliRunner",
        "build_parser",
        "main",
    ]
    assert payload["public_symbols"]["medium_cli.store"] == [
        "TaskStore",
        "load_tasks",
    ]
    assert payload["test_imported_symbols"] == {
        "medium_cli.cli": ["build_parser", "main"],
        "medium_cli.store": ["TaskStore"],
    }
    assert "def build_parser()" in payload["source_excerpt"]["medium_cli.cli"]
    assert "_private_helper" in payload["source_excerpt"]["medium_cli.cli"]


def test_source_api_contract_capsule_only_uses_direct_test_imports(tmp_path):
    source_dir = tmp_path / "src" / "small_cli"
    tests_dir = tmp_path / "tests"
    source_dir.mkdir(parents=True)
    tests_dir.mkdir()
    (source_dir / "cli.py").write_text(
        "def build_parser():\n    return None\n\n"
        "def main(argv=None):\n    return 0\n",
        encoding="utf-8",
    )
    (tests_dir / "test_cli.py").write_text(
        "import small_cli.cli as cli\n"
        "from small_cli.cli import main as entrypoint\n"
        "\n"
        "def test_cli():\n"
        "    assert cli.build_parser() is None\n"
        "    assert entrypoint([]) == 0\n",
        encoding="utf-8",
    )

    capsule = build_source_api_contract_capsule(tmp_path)

    assert capsule.public_symbols["small_cli.cli"] == ["build_parser", "main"]
    assert capsule.test_imported_symbols == {"small_cli.cli": ["entrypoint"]}


def test_source_api_contract_capsule_bounds_source_excerpts(tmp_path):
    source_dir = tmp_path / "src" / "pkg"
    source_dir.mkdir(parents=True)
    (source_dir / "large.py").write_text(
        "def first():\n    return 1\n\n" + ("# filler\n" * 100),
        encoding="utf-8",
    )

    capsule = build_source_api_contract_capsule(tmp_path, max_excerpt_chars=80)
    excerpt = capsule.source_excerpt["pkg.large"]

    assert len(excerpt) <= 80
    assert excerpt.endswith("...")
    assert "def first" in excerpt


def test_source_api_contract_capsule_handles_non_python_or_missing_src(tmp_path):
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_readme.py").write_text(
        "from external.module import Thing\n",
        encoding="utf-8",
    )

    capsule = build_source_api_contract_capsule(tmp_path)

    assert capsule.framework_family is None
    assert capsule.source_modules == []
    assert capsule.public_symbols == {}
    assert capsule.test_imported_symbols == {"external.module": ["Thing"]}
    assert capsule.source_excerpt == {}


def test_planning_repair_prompt_includes_source_api_contract_for_python_repair(
    tmp_path,
):
    source_dir = tmp_path / "src" / "medium_cli"
    tests_dir = tmp_path / "tests"
    source_dir.mkdir(parents=True)
    tests_dir.mkdir()
    (source_dir / "cli.py").write_text(
        "\n".join(
            [
                "import argparse",
                "",
                "def build_parser():",
                "    return argparse.ArgumentParser()",
                "",
                "def main(argv=None):",
                "    return 0",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (tests_dir / "test_cli.py").write_text(
        "from medium_cli.cli import build_parser, main\n",
        encoding="utf-8",
    )

    prompt = PlannerService.build_planning_repair_prompt(
        "Add a summary command.",
        malformed_output=(
            '[{"ops":[{"op":"append_file","path":"src/medium_cli/cli.py",'
            '"content":"\\n    elif args.command == \\"summary\\": pass"}]}]'
        ),
        project_dir=tmp_path,
        rejection_reasons=[
            "Plan uses append_file to add contextual Python control-flow fragments "
            "that only make sense inside an existing block; "
            "unsafe_python_append_fragments: src/medium_cli/cli.py"
        ],
    )

    assert "## SOURCE/API CONTRACT CAPSULE" in prompt
    assert "framework_family: argparse" in prompt
    assert "- Preserve the detected argparse framework family." in prompt
    assert "public_symbols:" in prompt
    assert "- medium_cli.cli: build_parser, main" in prompt
    assert "test_imported_symbols:" in prompt
    assert "- medium_cli.cli: build_parser, main" in prompt
    assert "source_excerpt:" in prompt
    assert "def build_parser()" in prompt
    assert "Prefer canonical source ops under existing source modules" in prompt
    assert (
        "Do not rewrite tests unless the user explicitly requested test changes"
        in prompt
    )
    assert (
        "Inside package code, never import using physical `src.<package>` paths"
        in prompt
    )
    assert "Preserve test-imported public function/class signatures" in prompt
    assert "main(argv=None)" in prompt
    assert (
        "Do not repair missing symbols with self-imports or re-export hacks" in prompt
    )


def test_planning_repair_prompt_omits_source_api_contract_for_docs_only_repair(
    tmp_path,
):
    source_dir = tmp_path / "src" / "medium_cli"
    source_dir.mkdir(parents=True)
    (source_dir / "cli.py").write_text(
        "def main(argv=None):\n    return 0\n",
        encoding="utf-8",
    )

    prompt = PlannerService.build_planning_repair_prompt(
        "Update README documentation.",
        malformed_output='[{"step_number":1,"commands":["echo docs"]}]',
        project_dir=tmp_path,
        rejection_reasons=["Documentation step needs clearer verification"],
    )

    assert "## SOURCE/API CONTRACT CAPSULE" not in prompt
    assert "framework_family:" not in prompt
    assert "test_imported_symbols:" not in prompt


def _write_medium_cli_fixture(tmp_path):
    source_dir = tmp_path / "src" / "medium_cli"
    tests_dir = tmp_path / "tests"
    source_dir.mkdir(parents=True)
    tests_dir.mkdir()
    (source_dir / "__init__.py").write_text(
        '"""Medium CLI fixture package."""\n',
        encoding="utf-8",
    )
    (source_dir / "cli.py").write_text(
        "\n".join(
            [
                '"""Task-list CLI."""',
                "import argparse",
                "from medium_cli.formatting import format_task_line",
                "from medium_cli.store import TaskStore",
                "",
                "def build_store():",
                "    return TaskStore()",
                "",
                "def build_parser():",
                "    parser = argparse.ArgumentParser()",
                "    subparsers = parser.add_subparsers(dest='command', required=True)",
                "    subparsers.add_parser('list')",
                "    return parser",
                "",
                "def main(argv=None):",
                "    parser = build_parser()",
                "    args = parser.parse_args(argv)",
                "    return 0",
                "",
            ]
        ),
        encoding="utf-8",
    )
    (source_dir / "formatting.py").write_text(
        "from medium_cli.store import Task\n\n"
        "def format_task_line(task: Task):\n"
        "    return task.title\n\n"
        "def format_summary(total: int, completed: int):\n"
        "    raise NotImplementedError\n",
        encoding="utf-8",
    )
    (source_dir / "store.py").write_text(
        "class Task:\n" "    pass\n\n" "class TaskStore:\n" "    pass\n",
        encoding="utf-8",
    )
    (tests_dir / "test_cli.py").write_text(
        "from medium_cli.cli import build_parser, main\n"
        "from medium_cli.formatting import format_summary\n"
        "from medium_cli.store import TaskStore\n",
        encoding="utf-8",
    )


def _python_repair_output() -> str:
    return (
        '[{"step_number":2,"description":"Add summary command",'
        '"ops":[{"op":"append_file","path":"src/medium_cli/cli.py",'
        '"content":"\\n\\n@click.command()\\ndef summary(): pass\\n"}],'
        '"commands":[],"verification":"python3 -m pytest -q",'
        '"expected_files":["src/medium_cli/cli.py"]}]'
    )


def test_source_api_contract_budget_protected_before_structure_and_knowledge(
    tmp_path,
):
    _write_medium_cli_fixture(tmp_path)
    item = type(
        "Item",
        (),
        {
            "knowledge_type": "failure_memory",
            "title": "Known medium repair failure",
            "content": "avoid stale repair " * 80,
        },
    )()
    knowledge_context = type(
        "Knowledge",
        (),
        {"matched_failure_memory": True, "retrieved_items": [item]},
    )()

    result = build_planning_repair_prompt_with_metadata(
        "Add summary command.",
        malformed_output=_python_repair_output(),
        project_dir=tmp_path,
        rejection_reasons=[
            "Plan writes Python decorators whose root name is undefined "
            "(files: ['src/medium_cli/cli.py'])"
        ],
        knowledge_context=knowledge_context,
        project_structure_capsule="PROJECT STRUCTURE\n" + ("x" * 9000),
    )

    assert "## SOURCE/API CONTRACT CAPSULE" in result.prompt
    assert "framework_family: argparse" in result.prompt
    assert "- medium_cli.cli: build_parser, build_store, main" in result.prompt
    assert len(result.prompt) <= repair_prompts.PLANNING_REPAIR_PROMPT_MAX_CHARS
    assert result.metadata["source_api_contract_available"] is True
    assert result.metadata["source_api_contract_included"] is True
    assert result.metadata["source_api_contract_chars"] > 0
    assert result.metadata["source_api_contract_omitted_reason"] is None


def test_bounded_debug_repair_prompt_includes_source_api_contract_for_import_error(
    tmp_path,
):
    _write_medium_cli_fixture(tmp_path)
    envelope = DebugFeedbackEnvelope(
        task_execution_id=1,
        task_id=1,
        step_index=1,
        failure_phase="execution",
        failed_command="python3 -m pytest -q",
        return_code=1,
        stderr_excerpt=(
            "ImportError: cannot import name 'build_parser' " "from 'medium_cli.cli'"
        ),
        pytest_excerpt="tests/test_cli.py: from medium_cli.cli import build_parser",
        changed_files=[],
        workspace_path=str(tmp_path),
        failure_class="pytest_failure",
    )

    result = build_bounded_debug_repair_prompt_with_metadata(envelope)

    assert "## SOURCE/API CONTRACT CAPSULE" in result.prompt
    assert "- medium_cli.cli: build_parser, main" in result.prompt
    assert result.metadata["source_api_contract_included"] is True
    assert (
        result.metadata["source_api_contract_included_reason"]
        == "import_error_public_api_risk"
    )


def test_bounded_debug_repair_prompt_includes_source_api_contract_for_expected_py_file(
    tmp_path,
):
    _write_medium_cli_fixture(tmp_path)
    envelope = DebugFeedbackEnvelope(
        task_execution_id=1,
        task_id=1,
        step_index=1,
        failure_phase="execution",
        failed_command="python3 -m pytest -q",
        return_code=1,
        stderr_excerpt="pytest failure",
        changed_files=[],
        expected_files=["src/medium_cli/cli.py"],
        workspace_path=str(tmp_path),
        failure_class="pytest_failure",
    )

    result = build_bounded_debug_repair_prompt_with_metadata(envelope)

    assert "## SOURCE/API CONTRACT CAPSULE" in result.prompt
    assert result.metadata["source_api_contract_included"] is True
    assert (
        result.metadata["source_api_contract_included_reason"]
        == "expected_python_source_files"
    )


def test_bounded_debug_repair_prompt_includes_source_api_contract_for_candidate_py_ops(
    tmp_path,
):
    _write_medium_cli_fixture(tmp_path)
    envelope = DebugFeedbackEnvelope(
        task_execution_id=1,
        task_id=1,
        step_index=1,
        failure_phase="execution",
        failed_command="python3 -m pytest -q",
        return_code=1,
        stderr_excerpt="pytest failure",
        changed_files=[],
        workspace_path=str(tmp_path),
        failure_class="pytest_failure",
    )

    result = build_bounded_debug_repair_prompt_with_metadata(
        envelope,
        candidate_repair={
            "repair_type": "ops_fix",
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/medium_cli/cli.py",
                    "content": "def main(argv=None):\n    return 0\n",
                }
            ],
        },
    )

    assert "## SOURCE/API CONTRACT CAPSULE" in result.prompt
    assert result.metadata["source_api_contract_included"] is True
    assert result.metadata["source_api_contract_included_reason"] == "python_source_ops"


def test_source_api_contract_uses_compact_capsule_when_full_does_not_fit(
    tmp_path,
    monkeypatch,
):
    _write_medium_cli_fixture(tmp_path)
    monkeypatch.setattr(
        repair_prompts,
        "PLANNING_REPAIR_PROMPT_MAX_CHARS",
        5200,
    )

    result = build_planning_repair_prompt_with_metadata(
        "Add summary command.",
        malformed_output=_python_repair_output(),
        project_dir=tmp_path,
        rejection_reasons=[
            "Plan writes Python decorators whose root name is undefined "
            "(files: ['src/medium_cli/cli.py'])"
        ],
    )

    assert "## SOURCE/API CONTRACT CAPSULE" in result.prompt
    assert "test_imported_symbols:" in result.prompt
    assert "source_excerpt:" not in result.prompt
    assert result.metadata["source_api_contract_included"] is True
    assert result.metadata["source_api_contract_compacted"] is True


def test_source_api_contract_metadata_records_omitted_reason_when_unfit(
    tmp_path,
    monkeypatch,
):
    _write_medium_cli_fixture(tmp_path)
    monkeypatch.setattr(
        repair_prompts,
        "PLANNING_REPAIR_PROMPT_MAX_CHARS",
        100,
    )

    result = build_planning_repair_prompt_with_metadata(
        "Add summary command.",
        malformed_output=_python_repair_output(),
        project_dir=tmp_path,
        rejection_reasons=[
            "Plan writes Python decorators whose root name is undefined "
            "(files: ['src/medium_cli/cli.py'])"
        ],
    )

    assert result.metadata["source_api_contract_available"] is True
    assert result.metadata["source_api_contract_included"] is False
    assert result.metadata["source_api_contract_omitted_reason"]


def test_syntax_retry_over_budget_fallback_retains_compact_source_api_contract(
    tmp_path,
    monkeypatch,
):
    _write_medium_cli_fixture(tmp_path)
    monkeypatch.setattr(
        repair_prompts,
        "PLANNING_REPAIR_PROMPT_MAX_CHARS",
        5200,
    )

    result = build_planning_repair_prompt_with_metadata(
        "Add summary command.",
        malformed_output=_python_repair_output() + (" " * 9000),
        project_dir=tmp_path,
        rejection_reasons=[
            "python_source_syntax_invalid: repaired Python source is still invalid. "
            "Affected file: src/medium_cli/cli.py line 1, offset 1: "
            "unterminated triple-quoted string literal"
        ],
        project_structure_capsule="PROJECT STRUCTURE\n" + ("x" * 9000),
    )

    assert len(result.prompt) <= repair_prompts.PLANNING_REPAIR_PROMPT_MAX_CHARS
    assert "## SOURCE/API CONTRACT CAPSULE" in result.prompt
    assert "source_excerpt:" not in result.prompt
    assert "Minimal hard-budget repair contract." in result.prompt
    assert "No physical src.<package> imports inside package code" in result.prompt
    assert "main(argv=None)" in result.prompt
    assert (
        result.metadata["source_api_contract_included_reason"]
        == "hard_budget_minimal_capsule"
    )
    assert result.metadata["source_api_contract_available"] is True
    assert result.metadata["source_api_contract_included"] is True
    assert result.metadata["source_api_contract_compacted"] is True
    assert result.metadata["source_api_contract_omitted_reason"] is None


def test_hard_budget_compact_fallback_uses_minimal_source_api_contract(
    tmp_path,
    monkeypatch,
):
    _write_medium_cli_fixture(tmp_path)
    monkeypatch.setattr(
        repair_prompts,
        "PLANNING_REPAIR_PROMPT_MAX_CHARS",
        5600,
    )

    result = build_planning_repair_prompt_with_metadata(
        "Add summary command.",
        malformed_output=_python_repair_output() + (" x" * 12000),
        project_dir=tmp_path,
        rejection_reasons=[
            "plan_validation_failed: Plan writes Python source with invalid syntax "
            "(python_source_syntax_invalid; src/medium_cli/cli.py line 53, "
            "offset 5: invalid syntax; files: ['src/medium_cli/cli.py']); "
            "Plan contains brittle heredoc-heavy or malformed commands; "
            "Plan uses append_file to add contextual Python control-flow fragments "
            "that only make sense inside an existing block"
        ],
        project_structure_capsule="PROJECT STRUCTURE\n" + ("x" * 9000),
    )

    assert len(result.prompt) <= repair_prompts.PLANNING_REPAIR_PROMPT_MAX_CHARS
    assert "Minimal hard-budget repair contract." in result.prompt
    assert "framework_family: argparse" in result.prompt
    assert "package_roots: medium_cli" in result.prompt
    assert "- medium_cli.cli: build_parser, main" in result.prompt
    assert "Preserve main(argv=None)" in result.prompt
    assert "forbid click.*, typer.*, @cli.command, @app.command" in result.prompt
    assert "source_excerpt:" not in result.prompt
    assert result.metadata["source_api_contract_available"] is True
    assert result.metadata["source_api_contract_included"] is True
    assert result.metadata["source_api_contract_compacted"] is True
    assert (
        result.metadata["source_api_contract_included_reason"]
        == "hard_budget_minimal_capsule"
    )
    assert result.metadata["source_api_contract_omitted_reason"] is None


def test_hard_budget_compact_fallback_drops_knowledge_before_minimal_capsule(
    tmp_path,
    monkeypatch,
):
    _write_medium_cli_fixture(tmp_path)
    monkeypatch.setattr(
        repair_prompts,
        "PLANNING_REPAIR_PROMPT_MAX_CHARS",
        5200,
    )
    item = type(
        "Item",
        (),
        {
            "knowledge_type": "failure_memory",
            "title": "Verbose memory",
            "content": "repair knowledge " * 200,
        },
    )()
    knowledge_context = type(
        "Knowledge",
        (),
        {"matched_failure_memory": True, "retrieved_items": [item]},
    )()

    result = build_planning_repair_prompt_with_metadata(
        "Add summary command.",
        malformed_output=_python_repair_output() + (" x" * 12000),
        project_dir=tmp_path,
        rejection_reasons=[
            "plan_validation_failed: Plan writes Python source with invalid syntax "
            "(python_source_syntax_invalid; src/medium_cli/cli.py line 53, "
            "offset 5: invalid syntax; files: ['src/medium_cli/cli.py'])"
        ],
        knowledge_context=knowledge_context,
        project_structure_capsule="PROJECT STRUCTURE\n" + ("x" * 9000),
    )

    assert "Minimal hard-budget repair contract." in result.prompt
    assert "REPAIR KNOWLEDGE REFERENCES" not in result.prompt
    assert "PROJECT STRUCTURE" not in result.prompt
    assert result.metadata["source_api_contract_included"] is True
    assert (
        result.metadata["source_api_contract_included_reason"]
        == "hard_budget_minimal_capsule"
    )


def test_stale_replace_over_budget_fallback_retains_compact_source_api_contract(
    tmp_path,
    monkeypatch,
):
    _write_medium_cli_fixture(tmp_path)
    monkeypatch.setattr(
        repair_prompts,
        "PLANNING_REPAIR_PROMPT_MAX_CHARS",
        4200,
    )

    result = build_planning_repair_prompt_with_metadata(
        "Add summary command.",
        malformed_output=(
            '[{"step_number":2,"description":"Patch CLI",'
            '"ops":[{"op":"replace_in_file","path":"src/medium_cli/cli.py",'
            '"old":"missing stale text","new":"updated"}],'
            '"verification":"python3 -m pytest -q",'
            '"expected_files":["src/medium_cli/cli.py"]}]' + (" " * 9000)
        ),
        project_dir=tmp_path,
        rejection_reasons=[
            "post_repair_stale_replace_fallback: stale_replace_ops_steps: "
            "steps [2] still use replace_in_file with old text that is not in "
            "the current file. Current file excerpt: def build_parser(): ..."
        ],
        project_structure_capsule="PROJECT STRUCTURE\n" + ("x" * 9000),
    )

    assert len(result.prompt) <= repair_prompts.PLANNING_REPAIR_PROMPT_MAX_CHARS
    assert "Stale replace repair mode" in result.prompt
    assert "## SOURCE/API CONTRACT CAPSULE" in result.prompt
    assert "Minimal hard-budget repair contract." in result.prompt
    assert "No physical src.<package> imports inside package code" in result.prompt
    assert "forbid click.*, typer.*, @cli.command, @app.command" in result.prompt
    assert (
        result.metadata["source_api_contract_included_reason"]
        == "hard_budget_minimal_capsule"
    )
    assert result.metadata["source_api_contract_available"] is True
    assert result.metadata["source_api_contract_included"] is True
    assert result.metadata["source_api_contract_compacted"] is True
    assert result.metadata["source_api_contract_omitted_reason"] is None


def test_bounded_debug_repair_prompt_includes_source_api_contract_for_python_failure(
    tmp_path,
):
    _write_medium_cli_fixture(tmp_path)
    envelope = DebugFeedbackEnvelope(
        task_execution_id=1,
        task_id=1,
        step_index=1,
        failure_phase="execution",
        failed_command="python3 -m pytest -q",
        return_code=1,
        stderr_excerpt=(
            "ImportError: cannot import name 'build_parser' "
            "from 'medium_cli.cli' (/workspace/src/medium_cli/cli.py)"
        ),
        pytest_excerpt="from medium_cli.cli import build_parser, main",
        changed_files=["src/medium_cli/cli.py"],
        workspace_path=str(tmp_path),
        failure_class="import_error",
    )

    result = build_bounded_debug_repair_prompt_with_metadata(
        envelope,
        source_edit_context=True,
    )

    assert "## SOURCE/API CONTRACT CAPSULE" in result.prompt
    assert "framework_family: argparse" in result.prompt
    assert "public_symbols:" in result.prompt
    assert "- medium_cli.cli: build_parser, build_store, main" in result.prompt
    assert "test_imported_symbols:" in result.prompt
    assert "- medium_cli.cli: build_parser, main" in result.prompt
    assert "required_public_symbols:" in result.prompt
    assert "source_excerpt:" in result.prompt
    assert "def build_parser()" in result.prompt
    assert "Do not add self-imports to restore symbols" in result.prompt
    assert "Do not add physical src. imports inside package code" in result.prompt
    assert (
        "Prefer repairing existing source definitions over re-export hacks"
        in result.prompt
    )
    assert result.metadata["source_api_contract_available"] is True
    assert result.metadata["source_api_contract_included"] is True
    assert result.metadata["source_api_contract_chars"] > 0
    assert result.metadata["source_api_contract_omitted_reason"] is None
    assert (
        result.metadata["source_api_contract_included_reason"] == "source_edit_context"
    )


def test_bounded_debug_repair_prompt_omits_source_api_contract_for_non_source_failure(
    tmp_path,
):
    _write_medium_cli_fixture(tmp_path)
    envelope = DebugFeedbackEnvelope(
        task_execution_id=1,
        task_id=1,
        step_index=1,
        failure_phase="execution",
        failed_command="python3 -m pytest -q",
        return_code=1,
        stderr_excerpt="pytest: error: unrecognized arguments: --bad-flag",
        pytest_excerpt="",
        changed_files=[],
        workspace_path=str(tmp_path),
        failure_class="pytest_failure",
    )

    result = build_bounded_debug_repair_prompt_with_metadata(envelope)

    assert "## SOURCE/API CONTRACT CAPSULE" not in result.prompt
    assert result.metadata["source_api_contract_available"] is True
    assert result.metadata["source_api_contract_included"] is False
    assert (
        result.metadata["source_api_contract_omitted_reason"]
        == "non_python_debug_context"
    )
    assert result.metadata["source_api_contract_included_reason"] is None
