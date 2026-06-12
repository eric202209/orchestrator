"""Characterization tests for `_plan_creates_nested_project_root`.

Derived from the 2026-06-10 false-positive incidents (tasks 711, 717, 748, 760):
`docs/roadmap/reports/maintenance/dense-context-planning-exhaustion-analysis-20260610.md`.

The rule must keep flagging genuinely new nested scaffold roots (my-app/...)
while ignoring in-place work on directories that already exist in the workspace
and read-only inspection steps whose only signal is `expected_files`.
"""

from app.services.orchestration.validation.validator import ValidatorService


def _nested_flagged(verdict) -> bool:
    if verdict.details.get("nested_project_root_steps"):
        return True
    return "nested_project_folder_command" in verdict.details.get(
        "semantic_violation_codes", []
    )


def _make_python_package(tmp_path, package_name, module_names):
    package_dir = tmp_path / package_name
    package_dir.mkdir()
    (package_dir / "__init__.py").write_text("")
    for module_name in module_names:
        (package_dir / f"{module_name}.py").write_text("")
    return package_dir


def test_task_711_calclib_inspection_step_not_flagged(tmp_path):
    """Task 711: read-only cat step over the existing calclib/ package."""
    _make_python_package(tmp_path, "calclib", ["arithmetic", "stats"])

    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Inspect existing calclib module structure and current __init__.py content",
                "commands": [
                    "cat calclib/__init__.py",
                    "cat calclib/arithmetic.py",
                    "cat calclib/stats.py",
                ],
                "verification": "python -c \"import pathlib; assert pathlib.Path('calclib/__init__.py').exists()\"",
                "rollback": None,
                "expected_files": [
                    "calclib/__init__.py",
                    "calclib/arithmetic.py",
                    "calclib/stats.py",
                ],
            }
        ],
        output_text="[]",
        task_prompt="Public API exports",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert not _nested_flagged(verdict)


def test_task_717_pathtools_inspection_step_not_flagged(tmp_path):
    """Task 717: read-only cat step over 4 files in the existing pathtools/ package."""
    _make_python_package(tmp_path, "pathtools", ["filters", "matchers", "walker"])

    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Inspect existing source files to identify public functions for re-export",
                "commands": [
                    "cat pathtools/filters.py",
                    "cat pathtools/matchers.py",
                    "cat pathtools/walker.py",
                    "cat pathtools/__init__.py",
                ],
                "verification": "test -f pathtools/__init__.py",
                "rollback": None,
                "expected_files": [
                    "pathtools/filters.py",
                    "pathtools/matchers.py",
                    "pathtools/walker.py",
                    "pathtools/__init__.py",
                ],
            }
        ],
        output_text="[]",
        task_prompt="Public API exports",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert not _nested_flagged(verdict)


def test_cd_and_cat_inspection_of_new_dir_reference_not_flagged_as_nested(tmp_path):
    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Inspect generated package paths",
                "commands": ["cd mylib && cat mylib/core.py", "echo mylib"],
                "verification": "test -f mylib/core.py",
                "rollback": None,
                "expected_files": [
                    "mylib/__init__.py",
                    "mylib/core.py",
                    "mylib/utils.py",
                ],
            }
        ],
        output_text="[]",
        task_prompt="Inspect generated package files",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert not _nested_flagged(verdict)


def test_task_760_strtools_inspection_steps_not_flagged(tmp_path):
    """Task 760: read-only cat steps over existing tests/ and strtools/ dirs."""
    _make_python_package(tmp_path, "strtools", ["format", "transform", "validate"])
    tests_dir = tmp_path / "tests"
    tests_dir.mkdir()
    for name in [
        "test_strtools",
        "test_format",
        "test_transform",
        "test_validate",
        "test_edge_cases",
    ]:
        (tests_dir / f"{name}.py").write_text("")

    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Inspect existing test files to understand the contract",
                "commands": [
                    "cat tests/test_strtools.py",
                    "cat tests/test_format.py",
                    "cat tests/test_transform.py",
                    "cat tests/test_validate.py",
                    "cat tests/test_edge_cases.py",
                ],
                "verification": "ls tests/*.py",
                "rollback": None,
                "expected_files": [
                    "tests/test_strtools.py",
                    "tests/test_format.py",
                    "tests/test_transform.py",
                    "tests/test_validate.py",
                    "tests/test_edge_cases.py",
                ],
            },
            {
                "step_number": 2,
                "description": "Inspect existing source files to understand current implementation",
                "commands": [
                    "cat strtools/__init__.py",
                    "cat strtools/format.py",
                    "cat strtools/transform.py",
                    "cat strtools/validate.py",
                ],
                "verification": "ls strtools/*.py",
                "rollback": None,
                "expected_files": [
                    "strtools/__init__.py",
                    "strtools/format.py",
                    "strtools/transform.py",
                    "strtools/validate.py",
                ],
            },
            {
                "step_number": 3,
                "description": "Run the full test suite",
                "commands": ["python -m pytest tests/ -v"],
                "verification": "python -m pytest tests/ -v",
                "rollback": None,
                "expected_files": [],
            },
        ],
        output_text="[]",
        task_prompt="Final verification",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert not _nested_flagged(verdict)


def test_task_748_in_place_writes_to_existing_package_not_flagged_as_nested(tmp_path):
    """Task 748: write_file ops into the existing calclib/ package are in-place
    work, not a new nested project root (other rules may still object to the
    writes themselves)."""
    _make_python_package(tmp_path, "calclib", ["arithmetic", "stats"])

    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Implement or fix source files to match test expectations",
                "ops": [
                    {
                        "op": "write_file",
                        "path": "calclib/__init__.py",
                        "content": "from .arithmetic import add\n",
                    },
                    {
                        "op": "write_file",
                        "path": "calclib/arithmetic.py",
                        "content": "def add(a, b):\n    return a + b\n",
                    },
                    {
                        "op": "write_file",
                        "path": "calclib/stats.py",
                        "content": "def mean(xs):\n    return sum(xs) / len(xs)\n",
                    },
                ],
                "commands": [],
                "verification": "python -m pytest tests/ -v",
                "rollback": None,
                "expected_files": [
                    "calclib/__init__.py",
                    "calclib/arithmetic.py",
                    "calclib/stats.py",
                ],
            }
        ],
        output_text="[]",
        task_prompt="Final verification",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert not _nested_flagged(verdict)


def test_expected_files_alone_under_new_dir_not_flagged(tmp_path):
    """A read-only step listing >=3 expected_files under a not-yet-existing dir
    must not flag: expected_files alone is not scaffold creation."""
    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Inspect package files",
                "commands": [
                    "cat calclib/__init__.py",
                    "cat calclib/arithmetic.py",
                    "cat calclib/stats.py",
                ],
                "verification": "ls calclib/",
                "rollback": None,
                "expected_files": [
                    "calclib/__init__.py",
                    "calclib/arithmetic.py",
                    "calclib/stats.py",
                ],
            }
        ],
        output_text="[]",
        task_prompt="Public API exports",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert not _nested_flagged(verdict)


def test_true_positive_write_ops_into_new_scaffold_dir_still_flagged(tmp_path):
    """A genuinely new my_app/ scaffold materialized via write_file ops must
    still flag."""
    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Scaffold the app",
                "ops": [
                    {
                        "op": "write_file",
                        "path": "my_app/package.json",
                        "content": "{}",
                    },
                    {
                        "op": "write_file",
                        "path": "my_app/src/App.tsx",
                        "content": "export default function App() { return null; }",
                    },
                    {
                        "op": "write_file",
                        "path": "my_app/README.md",
                        "content": "# my_app",
                    },
                ],
                "commands": [],
                "verification": "test -f my_app/package.json",
                "rollback": "rm -rf my_app",
                "expected_files": [
                    "my_app/package.json",
                    "my_app/src/App.tsx",
                    "my_app/README.md",
                ],
            }
        ],
        output_text="[]",
        task_prompt="Build an app landing page",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert verdict.details["nested_project_root_steps"] == [1]
    assert (
        "nested_project_folder_command" in verdict.details["semantic_violation_codes"]
    )


def test_true_positive_mkdir_touch_into_new_scaffold_dir_still_flagged(tmp_path):
    """A genuinely new scaffold dir materialized via mkdir/touch commands must
    still flag, even with project_dir provided."""
    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Create nested app inside the current workspace",
                "commands": [
                    "mkdir -p flower-site/src flower-site/public",
                    "touch flower-site/package.json flower-site/src/main.js flower-site/public/index.html",
                ],
                "verification": "test -f flower-site/package.json",
                "rollback": "rm -rf flower-site",
                "expected_files": [
                    "flower-site/package.json",
                    "flower-site/src/main.js",
                    "flower-site/public/index.html",
                ],
            }
        ],
        output_text="[]",
        task_prompt="Build a flower website in the current project workspace",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert verdict.details["nested_project_root_steps"] == [1]
    assert (
        "nested_project_folder_command" in verdict.details["semantic_violation_codes"]
    )


def test_mixed_root_and_package_files_not_flagged(tmp_path):
    """Root-level deliverables alongside package files must not flag."""
    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Create package layout",
                "ops": [
                    {"op": "write_file", "path": "setup.py", "content": ""},
                    {
                        "op": "write_file",
                        "path": "calclib/__init__.py",
                        "content": "",
                    },
                    {
                        "op": "write_file",
                        "path": "calclib/core.py",
                        "content": "",
                    },
                ],
                "commands": [],
                "verification": "test -f setup.py",
                "rollback": None,
                "expected_files": [
                    "setup.py",
                    "calclib/__init__.py",
                    "calclib/core.py",
                ],
            }
        ],
        output_text="[]",
        task_prompt="Bootstrap calclib package",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert not _nested_flagged(verdict)
