"""Tests for Phase 1 advisory path guard (detect_advisory_nested_scaffold).

All six scenarios from the design doc:
  1. write_file into existing package dir  -> no advisory
  2. write_file into new scaffold dir      -> advisory emitted, returns advisory (not raises)
  3. top-level file (README.md)            -> no advisory
  4. assets/logo.png                       -> no advisory (non-scaffold single file)
  5. .github/workflows/ci.yml             -> no advisory (hidden dir)
  6. advisory never raises                -> function returns list, never raises

The helper is pure (no I/O, no DB).  Tests run without any fixtures.
"""

from __future__ import annotations

import pytest

from app.services.orchestration.execution.path_guard import (
    PathGuardAdvisory,
    detect_advisory_nested_scaffold,
)


def _checksum_from_paths(paths):
    """Build a fake pre_step_checksum dict from a list of relative path strings."""
    return {p: "deadbeef" for p in paths}


# ── Scenario 1: existing package dir — no advisory ──────────────────────────


def test_no_advisory_for_write_into_existing_package_dir():
    """calclib/ already exists before the step; writing into it is in-place work."""
    pre = _checksum_from_paths(
        [
            "calclib/__init__.py",
            "calclib/arithmetic.py",
            "calclib/stats.py",
        ]
    )
    files_changed = ["calclib/public_api.py"]

    result = detect_advisory_nested_scaffold(pre, files_changed)

    assert result == [], f"Expected no advisory, got {result}"


def test_no_advisory_for_write_multiple_files_into_existing_dir():
    """Multiple new files under an existing top-level dir — not a new scaffold root."""
    pre = _checksum_from_paths(
        [
            "pathtools/__init__.py",
            "pathtools/filters.py",
        ]
    )
    files_changed = [
        "pathtools/walker.py",
        "pathtools/matchers.py",
        "pathtools/utils.py",
    ]

    result = detect_advisory_nested_scaffold(pre, files_changed)

    assert result == []


# ── Scenario 2: new scaffold dir — advisory emitted ─────────────────────────


def test_no_advisory_for_new_python_package_shape():
    """A plain new package directory is not a nested project scaffold by itself."""
    pre = _checksum_from_paths([])
    files_changed = [
        "mylib/__init__.py",
        "mylib/core.py",
        "mylib/utils.py",
    ]

    result = detect_advisory_nested_scaffold(pre, files_changed)

    assert result == []


def test_advisory_for_new_scaffold_dir_project_markers():
    """A new top-level dir with project markers still triggers advisory."""
    pre = _checksum_from_paths([])
    files_changed = [
        "webapp/package.json",
        "webapp/src/main.js",
        "webapp/src/app.js",
    ]

    result = detect_advisory_nested_scaffold(pre, files_changed)

    assert len(result) == 1
    adv = result[0]
    assert isinstance(adv, PathGuardAdvisory)
    assert adv.new_top_dir == "webapp"


def test_advisory_for_new_scaffold_dir_with_structural_subdirs():
    """New top-level dir containing two structural subdirs (src + tests) triggers advisory."""
    pre = _checksum_from_paths([])
    files_changed = [
        "myapp/src/main.py",
        "myapp/tests/test_main.py",
        "myapp/src/utils.py",
    ]

    result = detect_advisory_nested_scaffold(pre, files_changed)

    assert len(result) == 1
    assert result[0].new_top_dir == "myapp"


def test_advisory_does_not_raise_or_block():
    """The function returns a list and never raises, even on edge-case inputs."""
    pre = _checksum_from_paths([])
    files_changed = [
        "newproject/__init__.py",
        "newproject/core.py",
        "newproject/utils.py",
    ]

    # Must not raise
    result = detect_advisory_nested_scaffold(pre, files_changed)

    # Result is a list (may contain an advisory)
    assert isinstance(result, list)


# ── Scenario 3: top-level file (README.md) — no advisory ────────────────────


def test_no_advisory_for_top_level_file():
    """A file at depth 1 (README.md) does not create a directory — no advisory."""
    pre = _checksum_from_paths([])
    files_changed = ["README.md"]

    result = detect_advisory_nested_scaffold(pre, files_changed)

    assert result == []


def test_no_advisory_for_multiple_top_level_files():
    """pyproject.toml, setup.cfg, README.md — all depth-1, no advisory."""
    pre = _checksum_from_paths([])
    files_changed = ["pyproject.toml", "setup.cfg", "README.md"]

    result = detect_advisory_nested_scaffold(pre, files_changed)

    assert result == []


# ── Scenario 4: assets/logo.png — no advisory ───────────────────────────────


def test_no_advisory_for_non_scaffold_single_file_under_new_dir():
    """assets/ is a new top-level dir but a single non-scaffold file is not a scaffold."""
    pre = _checksum_from_paths([])
    files_changed = ["assets/logo.png"]

    result = detect_advisory_nested_scaffold(pre, files_changed)

    assert result == []


def test_no_advisory_for_non_scaffold_few_files():
    """assets/css/ and assets/img/ — two dirs, but neither is in NESTED_PROJECT_STRUCTURAL_DIRS."""
    pre = _checksum_from_paths([])
    # 'css' and 'img' are NOT in NESTED_PROJECT_STRUCTURAL_DIRS
    files_changed = [
        "assets/css/style.css",
        "assets/img/logo.png",
        "assets/img/banner.png",
    ]

    result = detect_advisory_nested_scaffold(pre, files_changed)

    assert result == []


# ── Scenario 5: hidden dir (.github) — no advisory ──────────────────────────


def test_no_advisory_for_hidden_dir():
    """.github/workflows/ci.yml — hidden dir is excluded from advisory checks."""
    pre = _checksum_from_paths([])
    files_changed = [".github/workflows/ci.yml"]

    result = detect_advisory_nested_scaffold(pre, files_changed)

    assert result == []


def test_no_advisory_for_venv_dir():
    """.venv/ is a hidden dir; no advisory even with many files."""
    pre = _checksum_from_paths([])
    files_changed = [
        ".venv/lib/python3.12/site-packages/foo/__init__.py",
        ".venv/lib/python3.12/site-packages/foo/core.py",
    ]

    result = detect_advisory_nested_scaffold(pre, files_changed)

    assert result == []


# ── Advisory metadata contract ───────────────────────────────────────────────


def test_advisory_metadata_fields():
    """Advisory carries exactly the fields expected by the emit_live call."""
    pre = _checksum_from_paths([])
    files_changed = [
        "scaffold/package.json",
        "scaffold/src/main.js",
        "scaffold/src/utils.js",
    ]

    result = detect_advisory_nested_scaffold(pre, files_changed)

    assert len(result) == 1
    adv = result[0]
    assert adv.new_top_dir == "scaffold"
    assert adv.mode == "advisory"
    assert adv.contract_violation_type == "nested_project_folder_created_advisory"
    assert isinstance(adv.files_written, list)
    assert len(adv.files_written) == 3


def test_no_advisory_for_empty_files_changed():
    """Empty files_changed list produces no advisories (fast path)."""
    pre = _checksum_from_paths(["existing/file.py"])

    result = detect_advisory_nested_scaffold(pre, [])

    assert result == []
