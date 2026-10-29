from pathlib import Path

from app.services.orchestration.planning.planner import PlannerService
from app.services.project.source_imports import (
    PythonTestContract,
    extract_python_test_contract,
    python_test_source_context_from_tests,
    render_python_test_contract_summary,
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def test_phase11a_extractor_maps_test_imports_to_existing_source_files(
    tmp_path: Path,
):
    _write(
        tmp_path / "src" / "small_cli" / "__init__.py",
        "",
    )
    _write(
        tmp_path / "src" / "small_cli" / "cli.py",
        "def build_parser():\n"
        "    return None\n"
        "\n"
        "def main(argv=None):\n"
        "    return 0\n",
    )
    _write(
        tmp_path / "tests" / "test_cli.py",
        "from small_cli.cli import build_parser, main\n"
        "\n"
        "def test_uppercase_option_prints_uppercase_message(capsys):\n"
        "    assert main(['--uppercase', 'hello']) == 0\n"
        "    assert capsys.readouterr().out.strip() == 'HELLO'\n",
    )

    contract = extract_python_test_contract(tmp_path)

    assert contract is not None
    assert "tests/test_cli.py" in contract.test_files
    assert "from small_cli.cli import build_parser, main" in contract.imports
    assert ("src/small_cli/cli.py",) == tuple(
        path for path, _reason in contract.source_targets
    )
    assert "main(['--uppercase', 'hello'])" in contract.public_calls
    assert any("HELLO" in assertion for assertion in contract.assertions)


def test_phase11a_extractor_infers_missing_src_module_from_package_reexport(
    tmp_path: Path,
):
    _write(
        tmp_path / "src" / "import_repair" / "__init__.py",
        "from import_repair.formatters import normalize_greeting\n"
        "\n"
        "__all__ = ['normalize_greeting']\n",
    )
    _write(
        tmp_path / "tests" / "test_formatter.py",
        "from import_repair import normalize_greeting\n"
        "\n"
        "def test_normalize_greeting_trims_and_title_cases_name():\n"
        "    assert normalize_greeting('  ada   lovelace ') == 'Hello, Ada Lovelace!'\n",
    )

    contract = extract_python_test_contract(tmp_path)

    assert contract is not None
    assert ("src/import_repair/__init__.py",) == tuple(
        path for path, _reason in contract.source_targets
    )
    assert ("src/import_repair/formatters.py",) == tuple(
        path for path, _reason in contract.missing_source_targets
    )
    missing_reason = contract.missing_source_targets[0][1]
    assert "import_repair.formatters" in missing_reason
    assert "normalize_greeting" in missing_reason


def test_phase11a_summary_guides_preserve_tests_and_prefer_source_edits(
    tmp_path: Path,
):
    _write(tmp_path / "src" / "pkg" / "__init__.py", "from pkg.feature import run\n")
    _write(
        tmp_path / "tests" / "test_feature.py",
        "from pkg import run\n" "\n" "def test_run():\n" "    assert run('x') == 'X'\n",
    )

    summary = python_test_source_context_from_tests(tmp_path, max_chars=1200)

    assert "## TEST CONTRACT SUMMARY" in summary
    assert "Existing tests are the contract. Preserve them." in summary
    assert "Do not rewrite tests or verifier commands" in summary
    assert "Prefer source edits under src/" in summary
    assert "src/pkg/feature.py" in summary
    assert "run('x') should equal 'X'" in summary
    assert len(summary) <= 1200


def test_phase11a_minimal_planning_prompt_includes_test_contract_summary(
    tmp_path: Path,
):
    _write(tmp_path / "src" / "small_cli" / "__init__.py", "")
    _write(
        tmp_path / "src" / "small_cli" / "cli.py",
        "def build_parser():\n"
        "    return None\n"
        "\n"
        "def main(argv=None):\n"
        "    return 0\n",
    )
    _write(
        tmp_path / "tests" / "test_cli.py",
        "from small_cli.cli import build_parser, main\n"
        "\n"
        "def test_uppercase_option_prints_uppercase_message(capsys):\n"
        "    assert main(['--uppercase', 'hello']) == 0\n"
        "    assert capsys.readouterr().out.strip() == 'HELLO'\n",
    )

    prompt = PlannerService.build_minimal_planning_prompt(
        "Add the --uppercase option to this small Python CLI.",
        tmp_path,
        workspace_has_existing_files=True,
    )

    assert "## TEST CONTRACT SUMMARY" in prompt
    assert "src/small_cli/cli.py" in prompt
    assert "main(['--uppercase', 'hello']) should equal 0" in prompt
    assert "Preserve them." in prompt
    assert len(prompt) < 12000


def test_phase11a_summary_is_target_oriented_without_long_excerpts(tmp_path: Path):
    _write(tmp_path / "src" / "small_cli" / "__init__.py", "")
    _write(
        tmp_path / "src" / "small_cli" / "cli.py",
        "def build_parser():\n    return None\n\ndef main(argv=None):\n    return 0\n",
    )
    _write(
        tmp_path / "tests" / "test_cli.py",
        "from small_cli.cli import build_parser, main\n"
        "\n"
        "def test_uppercase_option_prints_uppercase_message(capsys):\n"
        "    assert main(['--uppercase', 'hello']) == 0\n"
        "    assert capsys.readouterr().out.strip() == 'HELLO'\n",
    )

    summary = python_test_source_context_from_tests(tmp_path, max_chars=900)

    assert "Required source targets:" in summary
    assert "src/small_cli/cli.py" in summary
    assert "Expected behavior:" in summary
    assert "main(['--uppercase', 'hello']) should equal 0" in summary
    assert "printed output should equal 'HELLO'" in summary
    assert "Project imports from tests:" not in summary
    assert "Public calls used by tests:" not in summary
    assert "Existing test files:" not in summary
    assert "def build_parser" not in summary
    assert len(summary) < 700


def test_phase11a_summary_budget_remains_under_cap(tmp_path: Path):
    _write(tmp_path / "src" / "pkg" / "__init__.py", "")
    _write(
        tmp_path / "src" / "pkg" / "feature.py",
        "\n".join(f"def helper_{index}(): return {index}" for index in range(50)),
    )
    assertions = "\n".join(
        f"    assert helper_{index}() == {index}" for index in range(30)
    )
    _write(
        tmp_path / "tests" / "test_feature.py",
        "from pkg.feature import "
        + ", ".join(f"helper_{index}" for index in range(30))
        + "\n\n"
        + "def test_many_helpers():\n"
        + assertions
        + "\n",
    )

    contract = extract_python_test_contract(tmp_path)
    assert contract is not None

    summary = render_python_test_contract_summary(contract, max_chars=700)

    assert len(summary) <= 700
    assert "## TEST CONTRACT SUMMARY" in summary
    assert "src/pkg/feature.py" in summary


def test_phase11a_simplified_prompt_is_smaller_than_verbose_contract(tmp_path: Path):
    _write(tmp_path / "src" / "small_cli" / "__init__.py", "")
    _write(
        tmp_path / "src" / "small_cli" / "cli.py",
        "def build_parser():\n    return None\n\ndef main(argv=None):\n    return 0\n",
    )
    _write(
        tmp_path / "tests" / "test_cli.py",
        "from small_cli.cli import build_parser, main\n"
        "\n"
        "def test_uppercase_option_prints_uppercase_message(capsys):\n"
        "    assert main(['--uppercase', 'hello']) == 0\n"
        "    assert capsys.readouterr().out.strip() == 'HELLO'\n",
    )

    contract = extract_python_test_contract(tmp_path)
    assert contract is not None
    prompt = PlannerService.build_minimal_planning_prompt(
        "Add the --uppercase option to this small Python CLI.",
        tmp_path,
        workspace_has_existing_files=True,
    )

    old_verbose_floor = (
        len("Project imports from tests:\n")
        + sum(len(item) + 2 for item in contract.imports)
        + len("Public calls used by tests:\n")
        + sum(len(item) + 2 for item in contract.public_calls)
        + len("Existing test files:\n")
        + sum(len(item) + 2 for item in contract.test_files)
    )

    assert old_verbose_floor > 100
    assert len(python_test_source_context_from_tests(tmp_path)) < 700
    assert len(prompt) < 7000


def _write_dataflow_no_src_project(root: Path) -> None:
    _write(root / "dataflow" / "__init__.py", "")
    _write(
        root / "dataflow" / "normalize.py",
        "def normalize_id(value):\n    return value\n",
    )
    _write(
        root / "dataflow" / "aggregate.py",
        "from dataflow.normalize import normalize_id\n\n"
        "def customer_count(rows):\n    return len(rows)\n",
    )
    _write(
        root / "tests" / "test_aggregate.py",
        "from dataflow.aggregate import customer_count\n\n"
        "def test_count():\n    assert customer_count([]) == 0\n",
    )
    _write(
        root / "tests" / "test_normalize.py",
        "from dataflow.normalize import normalize_id\n\n"
        "def test_normalize_id():\n    assert normalize_id(' AbC ') == 'abc'\n",
    )


def test_pr07_no_src_project_omits_src_preference_and_keeps_targets(
    tmp_path: Path,
):
    _write_dataflow_no_src_project(tmp_path)

    contract = extract_python_test_contract(tmp_path)
    assert contract is not None
    assert contract.src_layout_detected is False

    summary = render_python_test_contract_summary(contract)

    assert "src/" not in summary
    assert "Prefer source edits under" not in summary
    assert "- dataflow/aggregate.py" in summary
    assert "- dataflow/normalize.py" in summary
    assert "Existing tests are the contract. Preserve them." in summary
    assert "Do not rewrite tests or verifier commands" in summary


def test_pr07_src_layout_project_keeps_src_preference(tmp_path: Path):
    _write(tmp_path / "src" / "small_cli" / "__init__.py", "")
    _write(
        tmp_path / "src" / "small_cli" / "cli.py",
        "def main(argv=None):\n    return 0\n",
    )
    _write(
        tmp_path / "tests" / "test_cli.py",
        "from small_cli.cli import main\n\n"
        "def test_main():\n    assert main(['x']) == 0\n",
    )

    contract = extract_python_test_contract(tmp_path)
    assert contract is not None
    assert contract.src_layout_detected is True

    summary = render_python_test_contract_summary(contract)

    assert "- Prefer source edits under src/." in summary
    assert "- src/small_cli/cli.py" in summary
    assert "Existing tests are the contract. Preserve them." in summary
    assert "Do not rewrite tests or verifier commands" in summary


def test_pr07_no_src_without_targets_invents_no_directory():
    contract = PythonTestContract(
        test_files=("tests/test_x.py",),
        imports=("from pkg import f",),
        public_calls=("f(1)",),
        assertions=("f(1) == 2",),
        source_targets=(),
        missing_source_targets=(),
        src_layout_detected=False,
    )

    summary = render_python_test_contract_summary(contract)

    assert summary.splitlines() == [
        "## TEST CONTRACT SUMMARY",
        "- Existing tests are the contract. Preserve them.",
        "- Do not rewrite tests or verifier commands to satisfy imports/behavior.",
        "",
        "Expected behavior:",
        "- f(1) should equal 2",
    ]


def test_pr07_rendering_does_not_alter_extracted_contract(tmp_path: Path):
    _write_dataflow_no_src_project(tmp_path)

    before = extract_python_test_contract(tmp_path)
    assert before is not None
    render_python_test_contract_summary(before)
    after = extract_python_test_contract(tmp_path)

    assert after == before
    assert tuple(path for path, _reason in before.source_targets) == (
        "dataflow/aggregate.py",
        "dataflow/normalize.py",
    )
    assert before.test_files == ("tests/test_aggregate.py", "tests/test_normalize.py")
    assert before.missing_source_targets == ()
