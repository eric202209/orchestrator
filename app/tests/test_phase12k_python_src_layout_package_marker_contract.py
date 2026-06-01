from app.services.orchestration.phases.planning_task1_bootstrap import (
    normalize_task1_python_src_layout_verification,
)
from app.services.orchestration.validation.validator import ValidatorService


def _step(
    *,
    ops,
    expected_files,
    verification="python -m pytest -q",
):
    return {
        "step_number": 1,
        "description": "Bootstrap Python src-layout package",
        "commands": [],
        "verification": verification,
        "rollback": None,
        "expected_files": expected_files,
        "ops": ops,
    }


def _validate(plan, tmp_path):
    return ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Build the first Python package slice with tests",
        execution_profile="implementation",
        project_dir=tmp_path,
        is_first_ordered_task=True,
    )


def test_task1_requires_src_layout_package_marker_when_tests_import_package(tmp_path):
    plan = [
        _step(
            ops=[
                {
                    "op": "write_file",
                    "path": "src/phase12j_greeting/greeter.py",
                    "content": "def greet(name: str) -> str:\n    return f'Hello, {name}!'\n",
                },
                {
                    "op": "write_file",
                    "path": "tests/test_greeter.py",
                    "content": (
                        "from phase12j_greeting.greeter import greet\n\n"
                        "def test_greet():\n"
                        "    assert greet('Ada') == 'Hello, Ada!'\n"
                    ),
                },
            ],
            expected_files=[
                "src/phase12j_greeting/greeter.py",
                "tests/test_greeter.py",
            ],
        )
    ]

    verdict = _validate(plan, tmp_path)
    contract = verdict.details["task1_bootstrap_contract"]

    assert not verdict.accepted
    assert contract["python_import_targets"] == ["phase12j_greeting.greeter"]
    assert contract["python_package_markers"] == ["src/phase12j_greeting/__init__.py"]
    assert contract["missing_python_package_markers"] == [
        "src/phase12j_greeting/__init__.py"
    ]
    assert (
        "task1_bootstrap_missing_python_package_marker" in contract["violation_codes"]
    )


def test_task1_accepts_empty_required_src_layout_package_marker(tmp_path):
    plan = [
        _step(
            ops=[
                {
                    "op": "write_file",
                    "path": "src/phase12j_greeting/__init__.py",
                    "content": "",
                },
                {
                    "op": "write_file",
                    "path": "src/phase12j_greeting/greeter.py",
                    "content": "def greet(name: str) -> str:\n    return f'Hello, {name}!'\n",
                },
                {
                    "op": "write_file",
                    "path": "tests/test_greeter.py",
                    "content": (
                        "from phase12j_greeting.greeter import greet\n\n"
                        "def test_greet():\n"
                        "    assert greet('Ada') == 'Hello, Ada!'\n"
                    ),
                },
            ],
            expected_files=[
                "src/phase12j_greeting/__init__.py",
                "src/phase12j_greeting/greeter.py",
                "tests/test_greeter.py",
            ],
        )
    ]

    verdict = _validate(plan, tmp_path)
    contract = verdict.details["task1_bootstrap_contract"]

    assert verdict.accepted
    assert contract["python_package_markers"] == ["src/phase12j_greeting/__init__.py"]
    assert contract["forbidden_python_src_imports"] == []
    assert contract["missing_python_package_markers"] == []


def test_task1_rejects_src_prefixed_test_import_for_src_layout_package(tmp_path):
    plan = [
        _step(
            ops=[
                {
                    "op": "write_file",
                    "path": "src/phase12j_greeting/__init__.py",
                    "content": "",
                },
                {
                    "op": "write_file",
                    "path": "src/phase12j_greeting/greeter.py",
                    "content": "def greet(name: str) -> str:\n    return f'Hello, {name}!'\n",
                },
                {
                    "op": "write_file",
                    "path": "tests/test_greeter.py",
                    "content": (
                        "from src.phase12j_greeting.greeter import greet\n\n"
                        "def test_greet():\n"
                        "    assert greet('Ada') == 'Hello, Ada!'\n"
                    ),
                },
            ],
            expected_files=[
                "src/phase12j_greeting/__init__.py",
                "src/phase12j_greeting/greeter.py",
                "tests/test_greeter.py",
            ],
        )
    ]

    verdict = _validate(plan, tmp_path)
    contract = verdict.details["task1_bootstrap_contract"]

    assert not verdict.accepted
    assert contract["forbidden_python_src_imports"] == ["src.phase12j_greeting.greeter"]
    assert "task1_bootstrap_forbidden_python_src_import" in contract["violation_codes"]


def test_task1_src_layout_verification_injects_src_import_path(tmp_path):
    plan = [
        _step(
            ops=[
                {
                    "op": "write_file",
                    "path": "src/phase12j_greeting/__init__.py",
                    "content": "",
                },
                {
                    "op": "write_file",
                    "path": "src/phase12j_greeting/greeter.py",
                    "content": "def greet(name: str) -> str:\n    return f'Hello, {name}!'\n",
                },
                {
                    "op": "write_file",
                    "path": "tests/test_greeter.py",
                    "content": (
                        "from phase12j_greeting.greeter import greet\n\n"
                        "def test_greet():\n"
                        "    assert greet('Ada') == 'Hello, Ada!'\n"
                    ),
                },
            ],
            expected_files=[
                "src/phase12j_greeting/__init__.py",
                "src/phase12j_greeting/greeter.py",
                "tests/test_greeter.py",
            ],
            verification="pytest -q",
        )
    ]
    verdict = _validate(plan, tmp_path)

    normalized = normalize_task1_python_src_layout_verification(plan, verdict)

    assert "sys.path.insert(0, 'src')" in normalized[0]["verification"]
    assert "pytest.main" in normalized[0]["verification"]
    assert "pytest.ini" in normalized[0]["expected_files"]
    assert {
        "op": "write_file",
        "path": "pytest.ini",
        "content": "[pytest]\npythonpath = src\n",
    } in normalized[0]["ops"]


def test_task1_src_layout_keeps_existing_pytest_config(tmp_path):
    plan = [
        _step(
            ops=[
                {
                    "op": "write_file",
                    "path": "pytest.ini",
                    "content": "[pytest]\npythonpath = src\n",
                },
                {
                    "op": "write_file",
                    "path": "src/phase12j_greeting/__init__.py",
                    "content": "",
                },
                {
                    "op": "write_file",
                    "path": "src/phase12j_greeting/greeter.py",
                    "content": "def greet(name: str) -> str:\n    return f'Hello, {name}!'\n",
                },
                {
                    "op": "write_file",
                    "path": "tests/test_greeter.py",
                    "content": (
                        "from phase12j_greeting.greeter import greet\n\n"
                        "def test_greet():\n"
                        "    assert greet('Ada') == 'Hello, Ada!'\n"
                    ),
                },
            ],
            expected_files=[
                "pytest.ini",
                "src/phase12j_greeting/__init__.py",
                "src/phase12j_greeting/greeter.py",
                "tests/test_greeter.py",
            ],
            verification="pytest -q",
        )
    ]
    verdict = _validate(plan, tmp_path)

    normalized = normalize_task1_python_src_layout_verification(plan, verdict)

    pytest_config_ops = [
        operation
        for operation in normalized[0]["ops"]
        if operation.get("path") == "pytest.ini"
    ]
    assert len(pytest_config_ops) == 1


def test_task1_does_not_require_package_marker_for_standalone_script(tmp_path):
    plan = [
        _step(
            ops=[
                {
                    "op": "write_file",
                    "path": "src/greet.py",
                    "content": (
                        "def greet(name: str) -> str:\n"
                        "    return f'Hello, {name}!'\n\n"
                        "if __name__ == '__main__':\n"
                        "    print(greet('Ada'))\n"
                    ),
                },
                {
                    "op": "write_file",
                    "path": "tests/test_greet_cli.py",
                    "content": (
                        "import pathlib\n"
                        "import subprocess\n"
                        "import sys\n\n"
                        "def test_greet_cli():\n"
                        "    script = pathlib.Path('src/greet.py')\n"
                        "    result = subprocess.run(\n"
                        "        [sys.executable, str(script)],\n"
                        "        check=True,\n"
                        "        text=True,\n"
                        "        capture_output=True,\n"
                        "    )\n"
                        "    assert result.stdout.strip() == 'Hello, Ada!'\n"
                    ),
                },
            ],
            expected_files=["src/greet.py", "tests/test_greet_cli.py"],
        )
    ]

    verdict = _validate(plan, tmp_path)
    contract = verdict.details["task1_bootstrap_contract"]

    assert verdict.accepted
    assert contract["python_package_markers"] == []
    assert contract["missing_python_package_markers"] == []
    assert (
        "task1_bootstrap_missing_python_package_marker"
        not in contract["violation_codes"]
    )


def test_task1_does_not_require_package_marker_for_non_python_task(tmp_path):
    plan = [
        _step(
            ops=[
                {
                    "op": "write_file",
                    "path": "src/greeting.js",
                    "content": "export function greet(name) { return `Hello, ${name}!`; }\n",
                },
                {
                    "op": "write_file",
                    "path": "tests/greeting.test.js",
                    "content": (
                        "import { greet } from '../src/greeting.js';\n"
                        "if (greet('Ada') !== 'Hello, Ada!') throw new Error();\n"
                    ),
                },
            ],
            expected_files=["src/greeting.js", "tests/greeting.test.js"],
            verification="node tests/greeting.test.js",
        )
    ]

    verdict = _validate(plan, tmp_path)
    contract = verdict.details["task1_bootstrap_contract"]

    assert contract["python_package_markers"] == []
    assert contract["missing_python_package_markers"] == []
    assert (
        "task1_bootstrap_missing_python_package_marker"
        not in contract["violation_codes"]
    )
