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


# ---------------------------------------------------------------------------
# Recovery-cascade regression tests (bootstrap completion validator fix)
# ---------------------------------------------------------------------------


def test_recovery_boilerplate_in_prompt_does_not_trigger_repair_policy(tmp_path: Path):
    """Recovery boilerplate injected by build_task_execution_prompt must not
    cause repair_keyword_match / explicit_repair_intent to fire when the
    original task title and description have no repair intent.

    This is the root cause of the S3 ON-arm cascade: planning-repair recovery
    rerun appended 'fix', 'bug', 'failure' via boilerplate and blocked the
    fresh_bootstrap_generated_test_evidence exemption.
    """
    project_dir = tmp_path / "project"
    source_file = project_dir / "neutraltools" / "core.py"
    test_file = project_dir / "tests" / "test_core.py"
    source_file.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    source_file.write_text(
        "def normalize_name(name: str) -> str:\n" "    return name.strip().lower()\n",
        encoding="utf-8",
    )
    test_file.write_text(
        "from neutraltools.core import normalize_name\n\n"
        "def test_normalize_name():\n"
        "    assert normalize_name('  Hello ') == 'hello'\n",
        encoding="utf-8",
    )
    added_files = ["neutraltools/core.py", "tests/test_core.py"]
    plan = [
        {
            "step_number": 1,
            "description": "Create normalize_name and tests",
            "verification": "python3 -m pytest tests/test_core.py -q",
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
    recovery_boilerplate = (
        "Create a normalize_name function and add tests.\n\n"
        "Recovery instructions:\n"
        "- The previous execution did not complete successfully.\n"
        "- First inspect the real current workspace, tests, fixtures, and configs"
        " before proposing new structure.\n"
        "- Diagnose and fix the underlying mistake or bug instead of repeating"
        " the same plan.\n"
        "- Reuse existing files when present and treat them as the source of truth.\n"
        "- Previous failure details: plan_validation_failed_after_repair"
    )

    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=plan,
        task_prompt=recovery_boilerplate,
        execution_profile="full_lifecycle",
        workspace_consistency={},
        title="Create normalize_name utility",
        description="Build a normalize_name function in neutraltools.core and add pytest coverage.",
        is_first_ordered_task=True,
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": added_files,
            "completion_verification_command": "python3 -m pytest tests/test_core.py -q",
            "change_set": {"added_files": added_files},
        },
    )

    evidence = verdict.details["validation_evidence"]
    assert (
        evidence["fresh_bootstrap_generated_test_evidence"] is True
    ), "Recovery boilerplate must not block fresh_bootstrap_generated_test_evidence"
    assert evidence["requires_independent_evidence"] is False
    assert verdict.accepted is True


def test_recovery_rerun_fresh_bootstrap_passes_end_to_end(tmp_path: Path):
    """Full end-to-end: fresh bootstrap task on recovery rerun must accept
    newly-generated source + tests when title/description have no repair intent."""
    project_dir = tmp_path / "project"
    source_file = project_dir / "neutraltools" / "core.py"
    test_file = project_dir / "tests" / "test_core.py"
    source_file.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    source_file.write_text(
        "def normalize_name(name: str) -> str:\n" "    return name.strip().lower()\n",
        encoding="utf-8",
    )
    test_file.write_text(
        "from neutraltools.core import normalize_name\n\n"
        "def test_normalize_name():\n"
        "    assert normalize_name('  Hello ') == 'hello'\n",
        encoding="utf-8",
    )
    added_files = ["neutraltools/core.py", "tests/test_core.py"]
    plan = [
        {
            "step_number": 1,
            "description": "Create normalize_name and tests",
            "verification": "python3 -m pytest tests/test_core.py -q",
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
    prior_error = (
        "plan_validation_failed_after_repair workspace lock prevented repair worker"
    )
    task_prompt = (
        "Create a normalize_name function and add tests.\n\n"
        "Recovery instructions:\n"
        "- The previous execution did not complete successfully.\n"
        "- Diagnose and fix the underlying mistake or bug instead of repeating the same plan.\n"
        f"- Previous failure details: {prior_error}"
    )

    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=plan,
        task_prompt=task_prompt,
        execution_profile="full_lifecycle",
        workspace_consistency={},
        title="Bootstrap neutraltools package",
        description="Create neutraltools/core.py with normalize_name and tests.",
        is_first_ordered_task=True,
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": added_files,
            "completion_verification_command": "python3 -m pytest tests/test_core.py -q",
            "change_set": {"added_files": added_files},
        },
    )

    assert verdict.accepted is True
    evidence = verdict.details["validation_evidence"]
    assert evidence["fresh_bootstrap_generated_test_evidence"] is True
    assert evidence["requires_independent_evidence"] is False
    assert evidence["verification_insufficient"] is False


def test_genuine_repair_task_still_requires_independent_evidence(tmp_path: Path):
    """When the original task title/description explicitly say 'fix failing tests',
    requires_independent_evidence must remain True even without recovery boilerplate.
    The fix must preserve repair protection for genuine repair tasks."""
    project_dir = tmp_path / "project"
    test_file = project_dir / "tests" / "test_core.py"
    test_file.parent.mkdir(parents=True)
    app_file = project_dir / "core.py"
    app_file.write_text("def normalize(s):\n    return s.strip()\n", encoding="utf-8")
    test_file.write_text(
        "from core import normalize\n\n"
        "def test_normalize():\n"
        "    assert normalize('  hi ') == 'hi'\n",
        encoding="utf-8",
    )

    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=[
            {
                "step_number": 1,
                "description": "Fix failing normalize tests",
                "verification": "python3 -m pytest tests/test_core.py -q",
                "expected_files": ["core.py", "tests/test_core.py"],
                "ops": [
                    {
                        "op": "write_file",
                        "path": "core.py",
                        "content": app_file.read_text(encoding="utf-8"),
                    },
                    {
                        "op": "write_file",
                        "path": "tests/test_core.py",
                        "content": test_file.read_text(encoding="utf-8"),
                    },
                ],
            }
        ],
        task_prompt="Fix failing normalize tests.",
        execution_profile="full_lifecycle",
        workspace_consistency={},
        title="Fix failing normalize tests",
        description="Debug and fix the normalize function so tests pass.",
        is_first_ordered_task=True,
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": ["core.py", "tests/test_core.py"],
            "completion_verification_command": "python3 -m pytest tests/test_core.py -q",
            "change_set": {
                "added_files": ["core.py", "tests/test_core.py"],
            },
        },
    )

    evidence = verdict.details["validation_evidence"]
    assert evidence["fresh_bootstrap_generated_test_evidence"] is False
    assert evidence["requires_independent_evidence"] is True


def test_recovery_prompt_only_no_repair_in_title_or_description(tmp_path: Path):
    """workspace_status=changes_requested-style recovery text in task_prompt alone
    must not trigger repair policy when title and description are clean bootstrap."""
    project_dir = tmp_path / "project"
    source_file = project_dir / "mylib" / "utils.py"
    test_file = project_dir / "tests" / "test_utils.py"
    source_file.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    source_file.write_text(
        "def slugify(text: str) -> str:\n"
        "    return text.strip().lower().replace(' ', '-')\n",
        encoding="utf-8",
    )
    test_file.write_text(
        "from mylib.utils import slugify\n\n"
        "def test_slugify():\n"
        "    assert slugify('Hello World') == 'hello-world'\n",
        encoding="utf-8",
    )
    added_files = ["mylib/utils.py", "tests/test_utils.py"]
    plan = [
        {
            "step_number": 1,
            "description": "Create slugify utility and tests",
            "verification": "python3 -m pytest tests/test_utils.py -q",
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
    # Simulate workspace_status="changes_requested" boilerplate injection
    prompt_with_changes_requested = (
        "Create a slugify utility and add tests.\n\n"
        "Recovery instructions:\n"
        "- The previous execution did not complete successfully.\n"
        "- Reuse existing files when present and treat them as the source of truth.\n"
        "- Previous failure details: plan_validation_failed_after_repair"
    )

    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=plan,
        task_prompt=prompt_with_changes_requested,
        execution_profile="full_lifecycle",
        workspace_consistency={},
        title="Create slugify utility",
        description="Build mylib/utils.py with slugify and pytest coverage.",
        is_first_ordered_task=True,
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": added_files,
            "completion_verification_command": "python3 -m pytest tests/test_utils.py -q",
            "change_set": {"added_files": added_files},
        },
    )

    evidence = verdict.details["validation_evidence"]
    assert (
        evidence["requires_independent_evidence"] is False
    ), "Recovery boilerplate alone must not trigger repair-only policy"
    assert evidence["fresh_bootstrap_generated_test_evidence"] is True


def test_non_bootstrap_task_repair_detection_unaffected(tmp_path: Path):
    """Repair detection for non-bootstrap tasks (is_first_ordered_task=False)
    must still fire when recovery prompt contains repair keywords from prior error."""
    project_dir = tmp_path / "project"
    test_file = project_dir / "tests" / "test_api.py"
    test_file.parent.mkdir(parents=True)
    pre_existing = project_dir / "tests" / "test_existing.py"
    pre_existing.write_text("def test_existing():\n    assert True\n", encoding="utf-8")
    test_file.write_text("def test_api():\n    assert True\n", encoding="utf-8")
    api_file = project_dir / "api.py"
    api_file.write_text("def get():\n    return {}\n", encoding="utf-8")

    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=[
            {
                "step_number": 1,
                "description": "Fix broken API endpoint",
                "verification": "python3 -m pytest tests/ -q",
                "expected_files": ["api.py", "tests/test_api.py"],
                "ops": [
                    {
                        "op": "write_file",
                        "path": "api.py",
                        "content": api_file.read_text(encoding="utf-8"),
                    },
                    {
                        "op": "write_file",
                        "path": "tests/test_api.py",
                        "content": test_file.read_text(encoding="utf-8"),
                    },
                ],
            }
        ],
        task_prompt="Fix the broken API endpoint.",
        execution_profile="full_lifecycle",
        workspace_consistency={},
        title="Fix broken API endpoint",
        description="The GET endpoint returns 500 — find and fix the root cause.",
        is_first_ordered_task=False,
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": ["api.py", "tests/test_api.py"],
            "completion_verification_command": "python3 -m pytest tests/ -q",
            "change_set": {
                "added_files": ["tests/test_api.py"],
                "modified_files": ["api.py"],
            },
        },
    )

    evidence = verdict.details["validation_evidence"]
    # repair_keyword_match fires from title/description ("fix", "broken", "bug")
    assert evidence["requires_independent_evidence"] is True


def test_neutraltools_flat_layout_recovery_rerun_scenario3_t1_shape(tmp_path: Path):
    """Regression for exact Scenario 3 T1 shape: neutraltools/core.py + tests/test_core.py,
    flat layout, recovery rerun after planning-repair cascade.

    Before the fix: recovery boilerplate 'fix'/'bug'/'failure' → explicit_repair_intent=True
    → fresh_bootstrap_generated_test_evidence=False → requires_independent_evidence=True.
    After the fix: only title/description are checked → exemption fires correctly.
    """
    project_dir = tmp_path / "project"
    core_file = project_dir / "neutraltools" / "core.py"
    test_file = project_dir / "tests" / "test_core.py"
    core_file.parent.mkdir(parents=True)
    test_file.parent.mkdir(parents=True)
    core_file.write_text(
        "def normalize_name(name: str) -> str:\n" "    return name.strip().lower()\n",
        encoding="utf-8",
    )
    test_file.write_text(
        "from neutraltools.core import normalize_name\n\n"
        "def test_basic():\n"
        "    assert normalize_name('  Hello ') == 'hello'\n\n"
        "def test_empty():\n"
        "    assert normalize_name('') == ''\n",
        encoding="utf-8",
    )
    added_files = [
        "neutraltools/__init__.py",
        "neutraltools/core.py",
        "tests/test_core.py",
    ]
    init_file = project_dir / "neutraltools" / "__init__.py"
    init_file.write_text("", encoding="utf-8")

    plan = [
        {
            "step_number": 1,
            "description": "Create neutraltools package with normalize_name and tests",
            "verification": "python3 -m pytest tests/test_core.py -q",
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
    # Exact recovery boilerplate from build_task_execution_prompt when
    # workspace_status="changes_requested" after planning repair cascade
    prior_error = (
        "plan_validation_failed_after_repair: workspace lock prevented repair "
        "worker from completing. failure details: planning contract violation."
    )
    task_prompt = (
        "Create a neutraltools package with normalize_name utility.\n\n"
        "Recovery instructions:\n"
        "- The previous execution did not complete successfully.\n"
        "- First inspect the real current workspace, tests, fixtures, and configs"
        " before proposing new structure.\n"
        "- Diagnose and fix the underlying mistake or bug instead of repeating"
        " the same plan.\n"
        "- Reuse existing files when present and treat them as the source of truth.\n"
        f"- Previous failure details: {prior_error}"
    )

    verdict = ValidatorService.validate_task_completion(
        project_dir=project_dir,
        plan=plan,
        task_prompt=task_prompt,
        execution_profile="full_lifecycle",
        workspace_consistency={},
        title="Create neutraltools package",
        description=(
            "Build neutraltools/core.py with normalize_name function and"
            " add pytest coverage in tests/test_core.py."
        ),
        is_first_ordered_task=True,
        completion_evidence={
            "summary_generated": True,
            "execution_results_count": 1,
            "reported_changed_files": added_files,
            "completion_verification_command": "python3 -m pytest tests/test_core.py -q",
            "change_set": {"added_files": added_files},
        },
    )

    evidence = verdict.details["validation_evidence"]
    assert (
        evidence["fresh_bootstrap_generated_test_evidence"] is True
    ), "Scenario 3 T1 recovery rerun must get fresh_bootstrap exemption"
    assert evidence["requires_independent_evidence"] is False
    assert evidence["verification_insufficient"] is False
    assert verdict.accepted is True
