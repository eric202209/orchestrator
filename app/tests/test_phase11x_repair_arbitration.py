from pathlib import Path

from app.services.orchestration.planning.repair_arbitration import (
    classify_planning_repair_candidate,
)
from app.services.orchestration.planning.source_api_contract import (
    build_source_api_contract_capsule,
)
from app.services.orchestration.phases.planning_repair_arbitration_control import (
    _materialization_regression_paths,
)
from app.services.orchestration.phases.planning_support import (
    _repair_root_cause_from_arbitration,
)


def test_arbitration_labels_removed_materialization_and_verification(tmp_path: Path):
    previous_plan = [
        {
            "step_number": 1,
            "description": "Implement add",
            "commands": [],
            "verification": "python -m pytest tests/test_ops.py -q",
            "expected_files": ["src/math_tools/operations.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/math_tools/operations.py",
                    "content": "def add(a, b):\n    return a + b\n",
                }
            ],
        }
    ]
    repaired_plan = [
        {
            "step_number": 1,
            "description": "Inspect workspace",
            "commands": ["rg --files . | sort"],
            "verification": None,
            "expected_files": [],
        }
    ]

    result = classify_planning_repair_candidate(
        previous_plan=previous_plan,
        repaired_plan=repaired_plan,
        project_dir=tmp_path,
    )

    assert result["outcome"] == "regressed"
    assert "removed_materialization" in result["regression_labels"]
    assert "removed_verification" in result["regression_labels"]
    assert result["source_materialization"]["status"] == "removed"
    assert result["verification_contract"]["status"] == "removed"
    assert _materialization_regression_paths(result) == ["src/math_tools/operations.py"]
    assert _repair_root_cause_from_arbitration(result) == (
        "missing_source_materialization"
    )


def test_materialization_regression_detects_moved_expected_source_path(
    tmp_path: Path,
):
    previous_plan = [
        {
            "step_number": 1,
            "description": "Create imported formatter module",
            "commands": [],
            "verification": "python -m pytest -q",
            "expected_files": ["src/import_repair/formatters.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/import_repair/formatters.py",
                    "content": "def normalize_greeting(name):\n    return name\n",
                }
            ],
        }
    ]
    repaired_plan = [
        {
            "step_number": 1,
            "description": "Create singular formatter module",
            "commands": [],
            "verification": "python -m pytest -q",
            "expected_files": ["src/import_repair/formatter.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/import_repair/formatter.py",
                    "content": "def normalize_greeting(name):\n    return name\n",
                }
            ],
        }
    ]

    result = classify_planning_repair_candidate(
        previous_plan=previous_plan,
        repaired_plan=repaired_plan,
        project_dir=tmp_path,
    )

    assert result["outcome"] == "regressed"
    assert result["source_materialization"] == {
        "status": "moved",
        "previous_paths": ["src/import_repair/formatters.py"],
        "repaired_paths": ["src/import_repair/formatter.py"],
    }
    assert "removed_materialization" in result["regression_labels"]
    assert _materialization_regression_paths(result) == [
        "src/import_repair/formatters.py"
    ]
    assert _repair_root_cause_from_arbitration(result) == (
        "missing_source_materialization"
    )


def test_arbitration_labels_stale_replace_and_test_rewrite(tmp_path: Path):
    repaired_plan = [
        {
            "step_number": 2,
            "description": "Patch stale code and rewrite test",
            "commands": [],
            "verification": "python -m pytest tests/test_ops.py -q",
            "expected_files": ["src/math_tools/operations.py", "tests/test_ops.py"],
            "ops": [
                {
                    "op": "replace_in_file",
                    "path": "src/math_tools/operations.py",
                    "old": "missing old text",
                    "new": "def add(a, b):\n    return a + b\n",
                },
                {
                    "op": "write_file",
                    "path": "tests/test_ops.py",
                    "content": "def test_placeholder():\n    assert True\n",
                },
            ],
        }
    ]

    result = classify_planning_repair_candidate(
        previous_plan=[],
        repaired_plan=repaired_plan,
        project_dir=tmp_path,
        immediate_repair_issues={
            "stale_replace_ops_steps": [2],
            "test_assertion_loss_ops_steps": [2],
        },
    )

    assert result["outcome"] == "regressed"
    assert "stale_replace" in result["regression_labels"]
    assert "test_rewrite" in result["regression_labels"]
    assert result["write_risk"]["test_write_risk"] is True


def test_arbitration_labels_missing_verification_without_full_validator(
    tmp_path: Path,
):
    repaired_plan = [
        {
            "step_number": 4,
            "description": "Write implementation without verifier",
            "commands": [],
            "verification": "",
            "expected_files": ["src/pkg/mod.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/pkg/mod.py",
                    "content": "def ok():\n    return True\n",
                }
            ],
        }
    ]

    result = classify_planning_repair_candidate(
        previous_plan=[],
        repaired_plan=repaired_plan,
        project_dir=tmp_path,
    )

    assert "removed_verification" in result["regression_labels"]
    assert result["verification_contract"]["status"] == "invalid"
    assert result["verification_contract"]["missing_verification_steps"] == [4]


def test_arbitration_labels_framework_and_source_api_regression(tmp_path: Path):
    source_dir = tmp_path / "src" / "medium_cli"
    tests_dir = tmp_path / "tests"
    source_dir.mkdir(parents=True)
    tests_dir.mkdir()
    (source_dir / "__init__.py").write_text("", encoding="utf-8")
    (source_dir / "cli.py").write_text(
        "import argparse\n\n\ndef build_parser():\n    return argparse.ArgumentParser()\n",
        encoding="utf-8",
    )
    (tests_dir / "test_cli.py").write_text(
        "from medium_cli.cli import build_parser\n",
        encoding="utf-8",
    )
    capsule = build_source_api_contract_capsule(tmp_path)
    repaired_plan = [
        {
            "step_number": 1,
            "description": "Rewrite CLI with a different framework",
            "commands": [],
            "verification": "python -m pytest tests/test_cli.py -q",
            "expected_files": ["src/medium_cli/cli.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/medium_cli/cli.py",
                    "content": (
                        "import typer\n\n"
                        "app = typer.Typer()\n\n"
                        "@app.command()\n"
                        "def main():\n"
                        "    print('ok')\n"
                    ),
                }
            ],
        }
    ]

    result = classify_planning_repair_candidate(
        previous_plan=[],
        repaired_plan=repaired_plan,
        project_dir=tmp_path,
        source_api_capsule=capsule,
    )

    assert "framework_drift" in result["regression_labels"]
    assert "source_api_regression" in result["regression_labels"]
    assert result["framework_contract"]["status"] == "regressed"
    assert result["source_api_contract"]["missing_required_symbols"] == [
        "medium_cli.cli.build_parser"
    ]


def test_arbitration_invalid_python_suppresses_syntax_derived_source_api_regression(
    tmp_path: Path,
):
    source_dir = tmp_path / "src" / "medium_cli"
    tests_dir = tmp_path / "tests"
    source_dir.mkdir(parents=True)
    tests_dir.mkdir()
    (source_dir / "__init__.py").write_text("", encoding="utf-8")
    (source_dir / "cli.py").write_text(
        "import argparse\n\n\ndef build_parser():\n    return argparse.ArgumentParser()\n",
        encoding="utf-8",
    )
    (tests_dir / "test_cli.py").write_text(
        "from medium_cli.cli import build_parser\n",
        encoding="utf-8",
    )
    capsule = build_source_api_contract_capsule(tmp_path)
    repaired_plan = [
        {
            "step_number": 1,
            "description": "Rewrite CLI with invalid Python",
            "commands": [],
            "verification": "python -m pytest tests/test_cli.py -q",
            "expected_files": ["src/medium_cli/cli.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/medium_cli/cli.py",
                    "content": '"""unterminated\n\ndef main():\n    pass\n',
                }
            ],
        }
    ]

    result = classify_planning_repair_candidate(
        previous_plan=[],
        repaired_plan=repaired_plan,
        project_dir=tmp_path,
        source_api_capsule=capsule,
    )

    assert result["outcome"] == "regressed"
    assert "invalid_output" in result["regression_labels"]
    assert "source_api_regression" not in result["regression_labels"]
    assert result["python_syntax"]["status"] == "regressed"
    assert result["source_api_contract"]["status"] == "unknown"
    assert result["source_api_contract"]["missing_required_symbols"] == []
    assert (
        result["source_api_contract"]["source_api_regression_suppressed_due_to_syntax"]
        is True
    )


def test_arbitration_valid_python_still_reports_true_source_api_regression(
    tmp_path: Path,
):
    source_dir = tmp_path / "src" / "medium_cli"
    tests_dir = tmp_path / "tests"
    source_dir.mkdir(parents=True)
    tests_dir.mkdir()
    (source_dir / "__init__.py").write_text("", encoding="utf-8")
    (source_dir / "store.py").write_text(
        "class TaskStore:\n    pass\n",
        encoding="utf-8",
    )
    (tests_dir / "test_store.py").write_text(
        "from medium_cli.store import TaskStore\n",
        encoding="utf-8",
    )
    capsule = build_source_api_contract_capsule(tmp_path)
    repaired_plan = [
        {
            "step_number": 1,
            "description": "Rewrite store without the public API",
            "commands": [],
            "verification": "python -m pytest tests/test_store.py -q",
            "expected_files": ["src/medium_cli/store.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/medium_cli/store.py",
                    "content": "class Task:\n    pass\n",
                }
            ],
        }
    ]

    result = classify_planning_repair_candidate(
        previous_plan=[],
        repaired_plan=repaired_plan,
        project_dir=tmp_path,
        source_api_capsule=capsule,
    )

    assert result["outcome"] == "regressed"
    assert "source_api_regression" in result["regression_labels"]
    assert "invalid_output" not in result["regression_labels"]
    assert result["source_api_contract"]["status"] == "regressed"
    assert result["source_api_contract"]["missing_required_symbols"] == [
        "medium_cli.store.TaskStore"
    ]
    assert (
        result["source_api_contract"]["source_api_regression_suppressed_due_to_syntax"]
        is False
    )


def test_arbitration_valid_python_preserving_symbols_does_not_regress(
    tmp_path: Path,
):
    source_dir = tmp_path / "src" / "medium_cli"
    tests_dir = tmp_path / "tests"
    source_dir.mkdir(parents=True)
    tests_dir.mkdir()
    (source_dir / "__init__.py").write_text("", encoding="utf-8")
    (source_dir / "store.py").write_text(
        "class TaskStore:\n    pass\n",
        encoding="utf-8",
    )
    (tests_dir / "test_store.py").write_text(
        "from medium_cli.store import TaskStore\n",
        encoding="utf-8",
    )
    capsule = build_source_api_contract_capsule(tmp_path)
    repaired_plan = [
        {
            "step_number": 1,
            "description": "Rewrite store while preserving the public API",
            "commands": [],
            "verification": "python -m pytest tests/test_store.py -q",
            "expected_files": ["src/medium_cli/store.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/medium_cli/store.py",
                    "content": "class TaskStore:\n    def list(self):\n        return []\n",
                }
            ],
        }
    ]

    result = classify_planning_repair_candidate(
        previous_plan=[],
        repaired_plan=repaired_plan,
        project_dir=tmp_path,
        source_api_capsule=capsule,
    )

    assert result["outcome"] == "improved_or_preserved"
    assert "source_api_regression" not in result["regression_labels"]
    assert "invalid_output" not in result["regression_labels"]
    assert result["source_api_contract"]["status"] == "preserved"
    assert result["source_api_contract"]["missing_required_symbols"] == []
