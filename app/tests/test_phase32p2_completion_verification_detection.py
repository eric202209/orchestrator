"""Phase 32P-2 — completion verification command reachability (defect D1).

Baseline behaviour: `_detect_completion_verification_command` accepted a
`pytest.ini` in its outer Python-test check but then re-required
`pyproject.toml` or a top-level `tests/` in the inner branch, so a project
declaring its suite through `pytest.ini` alone (e.g. `testpaths = app/tests`)
got `(None, None)` and completion verification was skipped entirely.
"""

from __future__ import annotations

import os
import stat
from pathlib import Path

from app.services.orchestration.phases.completion_repair import (
    _detect_completion_verification_command,
    _has_pytest_ini_config,
)

PYTEST_INI_APP_TESTS = "[pytest]\ntestpaths = app/tests\npythonpath = .\n"


def _write(path: Path, content: str) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")
    return path


def test_pytest_ini_with_non_root_testpaths_selects_verification_command(
    tmp_path: Path,
) -> None:
    """The Attempt-10-family project shape: pytest.ini + app/tests, nothing else."""

    _write(tmp_path / "pytest.ini", PYTEST_INI_APP_TESTS)
    (tmp_path / "app" / "tests").mkdir(parents=True)

    assert not (tmp_path / "tests").exists()
    assert not (tmp_path / "pyproject.toml").exists()
    assert not (tmp_path / "package.json").exists()

    command, source = _detect_completion_verification_command(tmp_path)

    assert command is not None
    assert command.endswith(" -m pytest")
    assert source == "python test suite detected"


def test_pytest_ini_alone_is_sufficient_without_any_test_directory(
    tmp_path: Path,
) -> None:
    _write(tmp_path / "pytest.ini", "[pytest]\n")

    command, source = _detect_completion_verification_command(tmp_path)

    assert command is not None
    assert source == "python test suite detected"


def test_pytest_ini_without_pytest_section_is_not_sufficient(tmp_path: Path) -> None:
    """An ini file that does not actually configure pytest must not qualify."""

    _write(tmp_path / "pytest.ini", "[tool.other]\nkey = value\n")

    assert _has_pytest_ini_config(tmp_path) is False
    assert _detect_completion_verification_command(tmp_path) == (None, None)


def test_empty_pytest_ini_is_not_sufficient(tmp_path: Path) -> None:
    _write(tmp_path / "pytest.ini", "")

    assert _has_pytest_ini_config(tmp_path) is False
    assert _detect_completion_verification_command(tmp_path) == (None, None)


def test_unreadable_pytest_ini_does_not_raise(tmp_path: Path) -> None:
    pytest_ini = _write(tmp_path / "pytest.ini", PYTEST_INI_APP_TESTS)
    pytest_ini.chmod(0)
    try:
        if os.geteuid() == 0:
            # root bypasses the permission bit; assert the directory-shaped
            # OSError path instead, which is the same except branch.
            pytest_ini.unlink()
            pytest_ini.mkdir()
        assert _has_pytest_ini_config(tmp_path) is False
    finally:
        if pytest_ini.is_file():
            pytest_ini.chmod(stat.S_IRUSR | stat.S_IWUSR)


def test_no_python_test_evidence_still_returns_no_command(tmp_path: Path) -> None:
    (tmp_path / "app").mkdir()

    assert _detect_completion_verification_command(tmp_path) == (None, None)


def test_top_level_tests_directory_still_selects_command(tmp_path: Path) -> None:
    """Pre-existing detection path must be unchanged."""

    (tmp_path / "tests").mkdir()

    command, source = _detect_completion_verification_command(tmp_path)

    assert command is not None
    assert source == "python test suite detected"


def test_pyproject_with_pytest_ini_still_selects_command(tmp_path: Path) -> None:
    _write(tmp_path / "pytest.ini", PYTEST_INI_APP_TESTS)
    _write(tmp_path / "pyproject.toml", "[project]\nname = 'x'\n")

    command, _source = _detect_completion_verification_command(tmp_path)

    assert command is not None


def test_package_json_detection_is_unchanged(tmp_path: Path) -> None:
    """A JS project must still win the package.json branch, not the pytest one."""

    _write(tmp_path / "package.json", '{"scripts": {"test": "vitest run"}}')
    (tmp_path / "tests").mkdir()

    command, source = _detect_completion_verification_command(tmp_path)

    assert command is not None
    assert command.startswith("npm test")
    assert source == "package.json test script via npm"


def test_selected_command_prefers_project_local_interpreter(tmp_path: Path) -> None:
    _write(tmp_path / "pytest.ini", PYTEST_INI_APP_TESTS)
    project_python = tmp_path / "venv" / "bin" / "python"
    project_python.parent.mkdir(parents=True)
    project_python.write_text("#!/bin/sh\n", encoding="utf-8")
    project_python.chmod(0o755)

    command, _source = _detect_completion_verification_command(tmp_path)

    assert command is not None
    assert str(project_python) in command
