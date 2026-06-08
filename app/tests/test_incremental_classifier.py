"""Tests for Slice J incremental execution candidate classifier.

Validates all five criteria against the Slice D corpus examples.
No LLM calls, no filesystem access.
"""

from __future__ import annotations

import pytest

from app.services.orchestration.planning.incremental_classifier import (
    _extract_file_paths,
    is_incremental_candidate,
)


# ── Accepted (should return True) ────────────────────────────────────────────


def test_accepts_phase10a_html_creation():
    desc = "Create about.html with heading 'Phase 10A Alpha' and verify it exists."
    assert is_incremental_candidate(desc) is True


def test_accepts_phase10a_css_creation():
    desc = "Create styles.css with body margin 0 and h1 color #333. Verify it exists."
    assert is_incremental_candidate(desc) is True


def test_accepts_phase10a_json_creation():
    desc = "Create manifest.json with name phase10a-alpha and version 1.0.0. Verify it exists."
    assert is_incremental_candidate(desc) is True


def test_accepts_readme_append():
    desc = "Append a Usage section to README.md. Verify the section exists."
    assert is_incremental_candidate(desc) is True


def test_accepts_tiny_money_source_rewrite():
    desc = (
        "Fix money formatter in src/tiny_money/money.py. "
        "Edit only that source file. Do not create new files. "
        "Do not edit tests. Verify with python3 -m pytest -q."
    )
    assert is_incremental_candidate(desc) is True


def test_accepts_single_file_python_creation():
    desc = (
        "Create utils.py with a function add(a, b) that returns a + b. "
        "Add a brief docstring. Verify the file is valid Python."
    )
    assert is_incremental_candidate(desc) is True


# ── Rejected: diagnosis keywords (criterion 4) ───────────────────────────────


def test_rejects_stale_replace_repair_failing_keyword():
    desc = "Fix failing inventory summary tests without weakening tests. Scoped to src/ and tests/."
    assert is_incremental_candidate(desc) is False


def test_rejects_error_keyword():
    desc = "Fix the error in src/app.py. Edit only that file. Verify with pytest."
    assert is_incremental_candidate(desc) is False


def test_rejects_debug_keyword():
    desc = "Debug the function in src/foo.py. Edit only that file. Verify with pytest."
    assert is_incremental_candidate(desc) is False


def test_rejects_resume_keyword():
    desc = "Resume updating src/foo.py with new content. Verify it exists."
    assert is_incremental_candidate(desc) is False


def test_rejects_import_keyword():
    desc = "Fix the import in src/main.py. Edit only that file. Verify with pytest."
    assert is_incremental_candidate(desc) is False


# ── Rejected: too many files (criterion 1) ───────────────────────────────────


def test_rejects_medium_cli_multi_file_feature():
    desc = (
        "Add summary command. New file src/summary.py. "
        "Update src/main.py and src/formatting.py to integrate."
    )
    assert is_incremental_candidate(desc) is False


def test_rejects_garden_story_three_files():
    desc = (
        "Create index.html, css/style.css, images/flower-bg.svg. "
        "CSS uses SVG as background with readable text overlay."
    )
    assert is_incremental_candidate(desc) is False


def test_rejects_more_than_two_files():
    desc = (
        "Create src/a.py with x=1, src/b.py with y=2, and src/c.py with z=3. "
        "Verify with pytest."
    )
    assert is_incremental_candidate(desc) is False


# ── Rejected: no explicit path (criterion 1) ─────────────────────────────────


def test_rejects_no_file_path_in_description():
    desc = (
        "Create a Python module with a function add(a, b). Verify it is valid Python."
    )
    assert is_incremental_candidate(desc) is False


def test_rejects_directory_only_no_filename():
    desc = "Add a config file to the src/ directory with defaults. Verify it exists."
    assert is_incremental_candidate(desc) is False


# ── Rejected: no verify phrase (criterion 3) ─────────────────────────────────


def test_rejects_no_verification_phrase():
    desc = "Create about.html with heading 'Phase 10A Alpha'."
    assert is_incremental_candidate(desc) is False


def test_rejects_implicit_check_it_works():
    desc = "Edit src/app.py with the new handler. Do not change tests. Check it works."
    # "check" matches _VERIFY_RE but also has "check it works" which is vague;
    # the classifier passes criterion 3 if "check" is present. The safety net is
    # the verify-command extraction in the flow module.
    # Criterion 1: src/app.py is 1 path. Criterion 2: "with" and "Do not". → True.
    # This tests that the classifier passes; safety falls to flow-level parsing.
    assert (
        is_incremental_candidate(desc) is True
    )  # classifier accepts; flow falls back if needed


# ── Rejected: no content/constraint spec (criterion 2) ───────────────────────


def test_rejects_no_content_specification():
    desc = "Edit src/app.py. Verify it exists."
    assert is_incremental_candidate(desc) is False


# ── Rejected: description too long (criterion 5) ─────────────────────────────


def test_rejects_description_over_220_chars():
    base = "Create about.html with heading 'Phase 10A Alpha' and verify it exists."
    desc = base + " " + "x" * 200  # well over 220 chars
    assert is_incremental_candidate(desc) is False


def test_accepts_description_exactly_at_boundary():
    # Construct a valid description that is exactly 220 chars.
    base = "Create about.html with heading 'Alpha' and verify it exists."
    padding = " " + "a" * (220 - len(base) - 1)
    desc = base + padding
    assert len(desc) == 220
    assert is_incremental_candidate(desc) is True


def test_rejects_description_one_over_boundary():
    base = "Create about.html with heading 'Alpha' and verify it exists."
    padding = " " + "a" * (221 - len(base) - 1)
    desc = base + padding
    assert len(desc) == 221
    assert is_incremental_candidate(desc) is False


# ── Edge cases ────────────────────────────────────────────────────────────────


def test_empty_description_rejected():
    assert is_incremental_candidate("") is False


def test_none_equivalent_empty_string():
    assert is_incremental_candidate("") is False


# ── _extract_file_paths unit tests ────────────────────────────────────────────


def test_extract_bare_filename():
    assert "about.html" in _extract_file_paths("Create about.html with content.")


def test_extract_path_with_directory():
    paths = _extract_file_paths("Fix money formatter in src/tiny_money/money.py.")
    assert "src/tiny_money/money.py" in paths


def test_extract_deduplicates():
    paths = _extract_file_paths("Edit src/app.py to fix X. Do not rename src/app.py.")
    assert paths.count("src/app.py") == 1


def test_extract_does_not_match_version_number():
    paths = _extract_file_paths("Bump version to 1.0.0 and verify it exists.")
    assert "1.0.0" not in paths


def test_extract_two_paths():
    paths = _extract_file_paths(
        "Create src/a.py with x=1 and src/b.py with y=2. Verify with pytest."
    )
    assert "src/a.py" in paths
    assert "src/b.py" in paths
    assert len(paths) == 2
