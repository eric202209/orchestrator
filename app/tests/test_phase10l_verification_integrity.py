from pathlib import Path

from app.services.orchestration.validation.integrity import (
    capture_baseline_result,
    check_test_preservation,
    classify_verification_command,
    compare_baseline,
    pre_existing_python_test_files,
    scan_python_test_text,
    scan_test_file_changes,
)
from app.services.orchestration.validation.validator import ValidatorService


def test_scan_test_file_changes_flags_tautological_assertion(tmp_path: Path):
    project_dir = tmp_path / "project"
    tests_dir = project_dir / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_app.py").write_text(
        "def test_repair():\n" "    assert True\n",
        encoding="utf-8",
    )

    findings = scan_test_file_changes(["tests/test_app.py"], project_dir)

    assert any(finding.code == "tautological_assertion" for finding in findings)


def test_check_test_preservation_flags_deleted_test_file(tmp_path: Path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()

    findings = check_test_preservation(
        {"deleted_files": ["tests/test_app.py"]},
        project_dir,
    )

    assert findings[0].code == "test_weakened_or_removed"
    assert findings[0].severity == "error"


def test_check_test_preservation_flags_partial_assertion_removal(tmp_path: Path):
    snapshot_dir = tmp_path / "snapshot"
    target_dir = tmp_path / "project"
    before_test = snapshot_dir / "tests" / "test_calc.py"
    after_test = target_dir / "tests" / "test_calc.py"
    before_test.parent.mkdir(parents=True)
    after_test.parent.mkdir(parents=True)
    before_test.write_text(
        "def test_calc():\n" "    assert 1 + 1 == 2\n" "    assert 2 + 2 == 4\n",
        encoding="utf-8",
    )
    after_test.write_text(
        "def test_calc():\n" "    assert 1 + 1 == 2\n",
        encoding="utf-8",
    )

    findings = check_test_preservation(
        {
            "snapshot_path": str(snapshot_dir),
            "target_path": str(target_dir),
            "modified_files": ["tests/test_calc.py"],
        },
        target_dir,
    )

    assert any(
        finding.code == "test_weakened_or_removed" and "2 -> 1" in finding.message
        for finding in findings
    )


def test_scan_test_file_changes_flags_self_derived_expected_value(tmp_path: Path):
    project_dir = tmp_path / "project"
    test_file = project_dir / "tests" / "test_calc.py"
    test_file.parent.mkdir(parents=True)
    test_file.write_text(
        "from calc import total\n\n"
        "def test_total():\n"
        "    result = total([1, 2])\n"
        "    expected = total([1, 2])\n"
        "    assert result == expected\n",
        encoding="utf-8",
    )

    findings = scan_test_file_changes(["tests/test_calc.py"], project_dir)

    assert any(finding.code == "self_derived_expected_value" for finding in findings)


def test_python_test_function_local_from_import_is_defined():
    findings = scan_python_test_text(
        "def test_version_defined():\n"
        "    from strtools import __version__\n"
        '    assert __version__ == "0.1.0"\n',
        "tests/test_strtools.py",
    )

    assert not any(finding.code == "undefined_test_name" for finding in findings)


def test_python_test_function_local_import_is_defined():
    findings = scan_python_test_text(
        "def test_version_defined():\n"
        "    import strtools\n"
        '    assert strtools.__version__ == "0.1.0"\n',
        "tests/test_strtools.py",
    )

    assert not any(finding.code == "undefined_test_name" for finding in findings)


def test_python_test_function_local_import_aliases_are_defined():
    findings = scan_python_test_text(
        "def test_version_defined():\n"
        "    import strtools as st\n"
        "    from package import name as alias\n"
        "    assert st.__version__ == alias\n",
        "tests/test_strtools.py",
    )

    assert not any(finding.code == "undefined_test_name" for finding in findings)


def test_python_test_module_import_behavior_is_unchanged():
    findings = scan_python_test_text(
        "import strtools\n\n"
        "def test_version_defined():\n"
        '    assert strtools.__version__ == "0.1.0"\n',
        "tests/test_strtools.py",
    )

    assert not any(finding.code == "undefined_test_name" for finding in findings)


def test_python_test_real_undefined_name_is_still_rejected():
    findings = scan_python_test_text(
        "def test_bad():\n" "    assert missing_name == 1\n",
        "tests/test_bad.py",
    )

    assert any(
        finding.code == "undefined_test_name" and "missing_name" in finding.message
        for finding in findings
    )


def test_python_test_function_local_assignment_is_defined():
    findings = scan_python_test_text(
        "def test_value():\n" "    expected = 1\n" "    assert expected == 1\n",
        "tests/test_value.py",
    )

    assert not any(finding.code == "undefined_test_name" for finding in findings)


def test_classify_verification_command_distinguishes_quality():
    assert classify_verification_command(None) == "missing"
    assert classify_verification_command("grep -q Ready app.py") == "insufficient"
    assert classify_verification_command("test -f app.py") == "smoke_only"
    assert (
        classify_verification_command("python -m unittest discover -s tests")
        == "regression_test"
    )
    assert classify_verification_command("python app.py --json") == "behavioral"


def test_repair_completion_rejects_tautological_test_replacement(tmp_path: Path):
    project_dir = tmp_path / "project"
    tests_dir = project_dir / "tests"
    tests_dir.mkdir(parents=True)
    (project_dir / "app.py").write_text(
        "def status():\n" "    return 'ready'\n",
        encoding="utf-8",
    )
    (tests_dir / "test_app.py").write_text(
        "def test_repair():\n" "    assert True\n",
        encoding="utf-8",
    )

    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=[
            {
                "step_number": 1,
                "description": "Fix broken status behavior",
                "verification": "python -m unittest discover -s tests",
                "expected_files": ["app.py"],
            }
        ],
        task_prompt="Fix the failing status behavior and preserve tests.",
        execution_profile="full_lifecycle",
        workspace_consistency={},
        title="Fix status regression",
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": ["app.py", "tests/test_app.py"],
        },
    )

    assert verdict.accepted is False
    evidence = verdict.details["validation_evidence"]
    assert evidence["verification_insufficient"] is True
    assert "tautological_assertion" in evidence["semantic_violation_codes"]
    assert any("Verification integrity blocker" in reason for reason in verdict.reasons)


def test_repair_completion_rejects_deleted_existing_test(tmp_path: Path):
    project_dir = tmp_path / "project"
    project_dir.mkdir()
    (project_dir / "app.py").write_text(
        "def status():\n" "    return 'ready'\n",
        encoding="utf-8",
    )

    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=[
            {
                "step_number": 1,
                "description": "Repair status behavior",
                "verification": "python -m unittest discover -s tests",
                "expected_files": ["app.py"],
            }
        ],
        task_prompt="Repair the failing status behavior.",
        execution_profile="full_lifecycle",
        workspace_consistency={},
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": ["app.py"],
            "change_set": {"deleted_files": ["tests/test_app.py"]},
        },
    )

    assert verdict.accepted is False
    evidence = verdict.details["validation_evidence"]
    assert "test_preservation_violation" in evidence["semantic_violation_codes"]


def test_repair_completion_rejects_only_newly_generated_regression_tests(
    tmp_path: Path,
):
    project_dir = tmp_path / "project"
    tests_dir = project_dir / "tests"
    tests_dir.mkdir(parents=True)
    (project_dir / "app.py").write_text(
        "def status():\n" "    return 'ready'\n",
        encoding="utf-8",
    )
    (tests_dir / "test_app.py").write_text(
        "from app import status\n\n"
        "def test_status():\n"
        "    assert status() == 'ready'\n",
        encoding="utf-8",
    )

    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=[
            {
                "step_number": 1,
                "description": "Fix status behavior",
                "verification": "python -m unittest discover -s tests",
                "expected_files": ["app.py"],
            }
        ],
        task_prompt="Fix the status regression.",
        execution_profile="full_lifecycle",
        workspace_consistency={},
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": ["app.py", "tests/test_app.py"],
            "change_set": {
                "added_files": ["tests/test_app.py"],
                "modified_files": ["app.py"],
            },
        },
    )

    assert verdict.accepted is False
    evidence = verdict.details["validation_evidence"]
    assert evidence["verification_insufficient"] is True
    assert evidence["pre_existing_test_files"] == []
    assert any("newly generated" in reason for reason in verdict.reasons)


def test_repair_completion_accepts_pre_existing_regression_test_evidence(
    tmp_path: Path,
):
    project_dir = tmp_path / "project"
    tests_dir = project_dir / "tests"
    tests_dir.mkdir(parents=True)
    (project_dir / "app.py").write_text(
        "def status():\n" "    return 'ready'\n",
        encoding="utf-8",
    )
    (tests_dir / "test_app.py").write_text(
        "from app import status\n\n"
        "def test_status():\n"
        "    assert status() == 'ready'\n",
        encoding="utf-8",
    )

    assert pre_existing_python_test_files(
        project_dir, {"modified_files": ["app.py"]}
    ) == ["tests/test_app.py"]

    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=[
            {
                "step_number": 1,
                "description": "Fix status behavior",
                "verification": "python -m unittest discover -s tests",
                "expected_files": ["app.py"],
            }
        ],
        task_prompt="Fix the status regression.",
        execution_profile="full_lifecycle",
        workspace_consistency={},
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": ["app.py"],
            "change_set": {"modified_files": ["app.py"]},
        },
    )

    assert verdict.accepted is True
    evidence = verdict.details["validation_evidence"]
    assert evidence["has_independent_regression_test"] is True
    assert evidence["verification_insufficient"] is False


def test_baseline_compare_detects_fail_to_pass_transition():
    before = capture_baseline_result(
        command="python -m unittest discover -s tests",
        returncode=1,
        stderr="FAIL: test_status",
    )
    after = capture_baseline_result(
        command="python -m unittest discover -s tests",
        returncode=0,
        stderr="OK",
    )

    result = compare_baseline(before, after)

    assert result["passed"] is True
    assert result["status"] == "passed"


def test_behavior_baseline_can_satisfy_repair_independent_evidence(
    tmp_path: Path,
):
    project_dir = tmp_path / "project"
    tests_dir = project_dir / "tests"
    tests_dir.mkdir(parents=True)
    (project_dir / "app.py").write_text(
        "def status():\n" "    return 'ready'\n",
        encoding="utf-8",
    )
    (tests_dir / "test_app.py").write_text(
        "from app import status\n\n"
        "def test_status():\n"
        "    assert status() == 'ready'\n",
        encoding="utf-8",
    )
    baseline = compare_baseline(
        capture_baseline_result(
            command="python -m unittest discover -s tests",
            returncode=1,
            stderr="FAIL",
        ),
        capture_baseline_result(
            command="python -m unittest discover -s tests",
            returncode=0,
            stderr="OK",
        ),
    )

    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=[
            {
                "step_number": 1,
                "description": "Fix status behavior",
                "verification": "python -m unittest discover -s tests",
                "expected_files": ["app.py"],
            }
        ],
        task_prompt="Fix the status regression.",
        execution_profile="full_lifecycle",
        workspace_consistency={},
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": ["app.py", "tests/test_app.py"],
            "change_set": {
                "added_files": ["tests/test_app.py"],
                "modified_files": ["app.py"],
            },
            "behavior_baseline": baseline,
        },
    )

    assert verdict.accepted is True
    evidence = verdict.details["validation_evidence"]
    assert evidence["behavior_baseline_passed"] is True
    assert evidence["verification_insufficient"] is False


def test_fresh_bootstrap_accepts_new_source_and_generated_tests(tmp_path: Path):
    project_dir = tmp_path / "project"
    source_file = project_dir / "src" / "calclib" / "parser.py"
    init_file = source_file.parent / "__init__.py"
    test_file = project_dir / "tests" / "test_parser.py"
    source_file.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    init_file.write_text(
        "from .parser import parse_amount\n",
        encoding="utf-8",
    )
    source_file.write_text(
        "def parse_amount(text: str) -> int:\n" "    return int(text.strip())\n",
        encoding="utf-8",
    )
    test_file.write_text(
        "from calclib.parser import parse_amount\n\n"
        "def test_parse_amount():\n"
        "    assert parse_amount(' 42 ') == 42\n",
        encoding="utf-8",
    )
    plan = [
        {
            "step_number": 1,
            "description": "Create parser source and tests for failure cases",
            "verification": "PYTHONPATH=src python3 -m pytest tests/test_parser.py -q",
            "expected_files": [
                "src/calclib/__init__.py",
                "src/calclib/parser.py",
                "tests/test_parser.py",
            ],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/calclib/__init__.py",
                    "content": init_file.read_text(encoding="utf-8"),
                },
                {
                    "op": "write_file",
                    "path": "src/calclib/parser.py",
                    "content": source_file.read_text(encoding="utf-8"),
                },
                {
                    "op": "write_file",
                    "path": "tests/test_parser.py",
                    "content": test_file.read_text(encoding="utf-8"),
                },
            ],
        }
    ]

    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=plan,
        task_prompt="Create a parser and cover success and failure cases.",
        execution_profile="full_lifecycle",
        workspace_consistency={},
        title="Bootstrap parser",
        is_first_ordered_task=True,
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": [
                "src/calclib/__init__.py",
                "src/calclib/parser.py",
                "tests/test_parser.py",
            ],
            "completion_verification_command": (
                "PYTHONPATH=src python3 -m pytest tests/test_parser.py -q"
            ),
            "change_set": {
                "added_files": [
                    "src/calclib/__init__.py",
                    "src/calclib/parser.py",
                    "tests/test_parser.py",
                ],
            },
        },
    )

    assert verdict.accepted is True
    evidence = verdict.details["validation_evidence"]
    assert evidence["fresh_bootstrap_generated_test_evidence"] is True
    assert evidence["requires_independent_evidence"] is False
    assert evidence["verification_insufficient"] is False


def test_wm_parser_t1_bootstrap_accepts_generated_contract_tests(tmp_path: Path):
    project_dir = tmp_path / "project"
    package_dir = project_dir / "src" / "calclib"
    test_file = project_dir / "tests" / "test_parser.py"
    package_dir.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    init_file = package_dir / "__init__.py"
    parser_file = package_dir / "parser.py"
    init_file.write_text(
        "from .parser import parse_amount\n",
        encoding="utf-8",
    )
    parser_file.write_text(
        "def parse_amount(text: str) -> dict:\n"
        "    stripped = text.strip()\n"
        "    if not stripped:\n"
        "        return {'ok': False, 'code': 'EMPTY'}\n"
        "    try:\n"
        "        value = int(stripped)\n"
        "    except ValueError:\n"
        "        return {'ok': False, 'code': 'FORMAT'}\n"
        "    if value < -999999 or value > 999999:\n"
        "        return {'ok': False, 'code': 'OVERFLOW'}\n"
        "    return {'ok': True, 'value': value}\n",
        encoding="utf-8",
    )
    test_file.write_text(
        "from calclib.parser import parse_amount\n\n"
        "def test_empty():\n"
        "    assert parse_amount('')['code'] == 'EMPTY'\n\n"
        "def test_valid():\n"
        "    assert parse_amount('42')['value'] == 42\n",
        encoding="utf-8",
    )
    added_files = [
        "src/calclib/__init__.py",
        "src/calclib/parser.py",
        "tests/test_parser.py",
    ]
    plan = [
        {
            "step_number": 1,
            "description": "Create parse_amount parser and tests",
            "verification": "PYTHONPATH=src python3 -m pytest tests/test_parser.py -q",
            "expected_files": added_files,
            "ops": [
                {
                    "op": "write_file",
                    "path": path,
                    "content": (project_dir / path).read_text(encoding="utf-8"),
                }
                for path in added_files
            ],
        }
    ]

    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=plan,
        task_prompt=(
            "Create parse_amount. Return a code for failure cases: EMPTY, "
            "FORMAT, and OVERFLOW. Create tests and run pytest."
        ),
        execution_profile="full_lifecycle",
        workspace_consistency={},
        title="Bootstrap parse_amount parser",
        is_first_ordered_task=True,
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": added_files,
            "completion_verification_command": (
                "PYTHONPATH=src python3 -m pytest tests/test_parser.py -q"
            ),
            "change_set": {"added_files": added_files},
        },
    )

    assert verdict.accepted is True
    assert (
        verdict.details["validation_evidence"][
            "fresh_bootstrap_generated_test_evidence"
        ]
        is True
    )


def test_fresh_repair_task_still_requires_independent_evidence(tmp_path: Path):
    project_dir = tmp_path / "project"
    test_file = project_dir / "tests" / "test_app.py"
    test_file.parent.mkdir(parents=True)
    app_file = project_dir / "app.py"
    app_file.write_text("def status():\n    return 'ready'\n", encoding="utf-8")
    test_file.write_text(
        "from app import status\n\n"
        "def test_status():\n"
        "    assert status() == 'ready'\n",
        encoding="utf-8",
    )

    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=[
            {
                "step_number": 1,
                "description": "Fix broken status behavior",
                "verification": "python3 -m pytest tests/test_app.py -q",
                "expected_files": ["app.py", "tests/test_app.py"],
                "ops": [
                    {
                        "op": "write_file",
                        "path": "app.py",
                        "content": app_file.read_text(encoding="utf-8"),
                    },
                    {
                        "op": "write_file",
                        "path": "tests/test_app.py",
                        "content": test_file.read_text(encoding="utf-8"),
                    },
                ],
            }
        ],
        task_prompt="Fix the broken status regression.",
        execution_profile="full_lifecycle",
        workspace_consistency={},
        title="Fix status regression",
        is_first_ordered_task=True,
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": ["app.py", "tests/test_app.py"],
            "completion_verification_command": "python3 -m pytest tests/test_app.py -q",
            "change_set": {
                "added_files": ["app.py", "tests/test_app.py"],
            },
        },
    )

    assert verdict.accepted is False
    evidence = verdict.details["validation_evidence"]
    assert evidence["fresh_bootstrap_generated_test_evidence"] is False
    assert evidence["requires_independent_evidence"] is True
    assert evidence["verification_insufficient"] is True


def test_mature_project_test_rewrite_remains_rejected(tmp_path: Path):
    snapshot_dir = tmp_path / "snapshot"
    project_dir = tmp_path / "project"
    before_test = snapshot_dir / "tests" / "test_app.py"
    after_test = project_dir / "tests" / "test_app.py"
    before_test.parent.mkdir(parents=True)
    after_test.parent.mkdir(parents=True)
    before_test.write_text(
        "def test_status():\n"
        "    assert status() == 'ready'\n"
        "    assert status() != 'broken'\n",
        encoding="utf-8",
    )
    after_test.write_text(
        "def test_status():\n" "    assert status() == 'ready'\n",
        encoding="utf-8",
    )

    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=[
            {
                "step_number": 1,
                "description": "Fix status regression",
                "verification": "python3 -m pytest tests/test_app.py -q",
                "expected_files": ["tests/test_app.py"],
            }
        ],
        task_prompt="Fix the status regression without weakening tests.",
        execution_profile="full_lifecycle",
        workspace_consistency={},
        title="Fix mature project regression",
        is_first_ordered_task=False,
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": ["tests/test_app.py"],
            "completion_verification_command": "python3 -m pytest tests/test_app.py -q",
            "change_set": {
                "snapshot_path": str(snapshot_dir),
                "target_path": str(project_dir),
                "modified_files": ["tests/test_app.py"],
            },
        },
    )

    assert verdict.accepted is False
    evidence = verdict.details["validation_evidence"]
    assert evidence["requires_independent_evidence"] is True
    assert "test_preservation_violation" in evidence["semantic_violation_codes"]
