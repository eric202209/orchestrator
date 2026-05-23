"""Phase 10P — Deterministic Patch Application tests.

Covers add_missing_import, replace_test_function, try_deterministic_patch,
and the executor-level integration for stale replace_in_file fallback.
"""

from pathlib import Path

from app.services.orchestration.execution.executor import ExecutorService
from app.services.orchestration.operations.patch_python import (
    add_missing_import,
    replace_test_function,
    try_deterministic_patch,
)


# --- add_missing_import ---


def test_add_missing_import_inserts_after_last_import(tmp_path: Path):
    f = tmp_path / "module.py"
    f.write_text("import os\nimport sys\n\ndef foo(): pass\n", encoding="utf-8")

    result = add_missing_import(f, "import json")

    assert result.success
    lines = f.read_text().splitlines()
    # import json should appear at index 2 (after os, sys)
    assert "import json" in lines
    assert lines.index("import json") == 2
    assert "import os" in lines
    assert "import sys" in lines
    assert "def foo(): pass" in lines


def test_add_missing_import_already_present_is_idempotent(tmp_path: Path):
    f = tmp_path / "module.py"
    f.write_text("import os\nimport json\n", encoding="utf-8")

    result = add_missing_import(f, "import json")

    assert result.success
    assert "already present" in result.evidence
    assert f.read_text().count("import json") == 1


def test_add_missing_import_no_existing_imports_inserts_at_top(tmp_path: Path):
    f = tmp_path / "module.py"
    f.write_text("def foo(): pass\n", encoding="utf-8")

    result = add_missing_import(f, "import os")

    assert result.success
    lines = f.read_text().splitlines()
    assert lines[0] == "import os"


def test_add_missing_import_ignores_local_imports_when_placing_module_import(
    tmp_path: Path,
):
    f = tmp_path / "module.py"
    f.write_text(
        "def foo():\n" "    import os\n" "    return os.getcwd()\n",
        encoding="utf-8",
    )

    result = add_missing_import(f, "import json")

    assert result.success, result.evidence
    lines = f.read_text().splitlines()
    assert lines[0] == "import json"
    assert "    import os" in lines


def test_add_missing_import_from_form(tmp_path: Path):
    f = tmp_path / "module.py"
    f.write_text("import os\n\ndef foo(): pass\n", encoding="utf-8")

    result = add_missing_import(f, "from pathlib import Path")

    assert result.success
    assert "from pathlib import Path" in f.read_text()


def test_add_missing_import_rejects_on_syntax_error_file(tmp_path: Path):
    f = tmp_path / "bad.py"
    f.write_text("def foo(\n", encoding="utf-8")

    result = add_missing_import(f, "import os")

    assert not result.success
    assert "syntax error" in result.evidence.lower()


# --- replace_test_function ---


def test_replace_test_function_success(tmp_path: Path):
    f = tmp_path / "test_service.py"
    f.write_text(
        "def test_velocity():\n"
        "    assert 1 == 1\n"
        "\n"
        "def test_other():\n"
        "    assert 2 == 2\n",
        encoding="utf-8",
    )

    new_fn = "def test_velocity():\n    result = 1 + 1\n    assert result == 2\n"
    result = replace_test_function(f, "test_velocity", new_fn)

    assert result.success, result.evidence
    content = f.read_text()
    assert "result = 1 + 1" in content
    assert "assert result == 2" in content
    # Other test must be preserved
    assert "def test_other():" in content
    assert "assert 2 == 2" in content


def test_replace_test_function_target_not_found(tmp_path: Path):
    f = tmp_path / "test_service.py"
    f.write_text("def test_existing():\n    assert True\n", encoding="utf-8")

    result = replace_test_function(
        f, "test_missing", "def test_missing():\n    assert 1\n"
    )

    assert not result.success
    assert "not found" in result.evidence


def test_replace_test_function_rejects_placeholder(tmp_path: Path):
    f = tmp_path / "test_service.py"
    f.write_text("def test_foo():\n    assert 1 == 1\n", encoding="utf-8")

    result = replace_test_function(f, "test_foo", "def test_foo():\n    pass\n")

    assert not result.success
    assert "placeholder" in result.evidence


def test_replace_test_function_rejects_assertion_drop(tmp_path: Path):
    f = tmp_path / "test_service.py"
    f.write_text(
        "def test_foo():\n    result = service.get()\n    assert result == 42\n",
        encoding="utf-8",
    )
    stub = "def test_foo():\n    import service\n"

    result = replace_test_function(f, "test_foo", stub)

    assert not result.success
    assert "no assertions" in result.evidence


def test_replace_test_function_rejects_syntax_error_in_replacement(tmp_path: Path):
    f = tmp_path / "test_service.py"
    f.write_text("def test_foo():\n    assert 1\n", encoding="utf-8")

    result = replace_test_function(f, "test_foo", "def test_foo(:\n    assert 1\n")

    assert not result.success
    assert "syntax error" in result.evidence.lower()


def test_replace_test_function_restores_original_when_targeted_pytest_fails(
    tmp_path: Path,
):
    f = tmp_path / "test_service.py"
    original = "def test_foo():\n    assert 1 == 1\n"
    f.write_text(original, encoding="utf-8")

    result = replace_test_function(
        f, "test_foo", "def test_foo():\n    assert 1 == 2\n"
    )

    assert not result.success
    assert "still fails after patch" in result.evidence
    assert "assert 1 == 2" in result.evidence
    assert f.read_text(encoding="utf-8") == original


def test_replace_test_function_preserves_decorators(tmp_path: Path):
    f = tmp_path / "test_service.py"
    f.write_text(
        "import pytest\n"
        "\n"
        "@pytest.mark.unit\n"
        "def test_velocity():\n"
        "    assert 1 == 1\n"
        "\n"
        "def test_other():\n"
        "    assert 2 == 2\n",
        encoding="utf-8",
    )

    new_fn = "@pytest.mark.unit\ndef test_velocity():\n    assert 2 == 2\n"
    result = replace_test_function(f, "test_velocity", new_fn)

    assert result.success, result.evidence
    content = f.read_text()
    assert "@pytest.mark.unit" in content
    assert "def test_other():" in content


def test_replace_test_function_reindents_unittest_method_replacement(tmp_path: Path):
    f = tmp_path / "test_service.py"
    f.write_text(
        "import unittest\n"
        "\n"
        "class TestService(unittest.TestCase):\n"
        "    def test_velocity(self):\n"
        "        self.assertEqual(1, 1)\n"
        "\n"
        "    def test_other(self):\n"
        "        self.assertTrue(True)\n",
        encoding="utf-8",
    )

    new_fn = (
        "def test_velocity(self):\n"
        "    result = 1 + 1\n"
        "    self.assertEqual(result, 2)\n"
    )
    result = replace_test_function(f, "test_velocity", new_fn)

    assert result.success, result.evidence
    content = f.read_text()
    assert "    def test_velocity(self):\n" in content
    assert "        result = 1 + 1\n" in content
    assert "    def test_other(self):" in content


# --- try_deterministic_patch ---


def test_try_deterministic_patch_returns_none_for_non_python(tmp_path: Path):
    f = tmp_path / "config.json"
    f.write_text('{"key": "old"}', encoding="utf-8")

    result = try_deterministic_patch(f, '"old"', '"new"')

    assert result is None


def test_try_deterministic_patch_infers_replace_test_function(tmp_path: Path):
    f = tmp_path / "test_report.py"
    f.write_text(
        "def test_summary():\n    assert 1 + 2 == 3\n",
        encoding="utf-8",
    )
    new_content = (
        "def test_summary():\n"
        "    result = 1 + 2\n"
        "    assert result == 3\n"
        "    assert result > 0\n"
    )

    result = try_deterministic_patch(
        f, "def test_summary():\n    STALE_TEXT\n", new_content
    )

    assert result is not None
    assert result.success, result.evidence


def test_try_deterministic_patch_infers_add_missing_import(tmp_path: Path):
    f = tmp_path / "utils.py"
    f.write_text("import os\n\ndef foo(): pass\n", encoding="utf-8")

    old = "import os"
    new = "import os\nimport json"

    result = try_deterministic_patch(f, old, new)

    assert result is not None
    assert result.success, result.evidence
    assert "import json" in f.read_text()


def test_try_deterministic_patch_returns_none_when_no_helper_matches(tmp_path: Path):
    f = tmp_path / "utils.py"
    f.write_text("import os\n\ndef foo(): pass\n", encoding="utf-8")

    # new has multiple changed lines — not a simple import addition, not a test fn
    result = try_deterministic_patch(
        f, "def foo(): pass", "def foo():\n    return 42\ndef bar(): pass\n"
    )

    assert result is None


# --- Executor integration ---


def test_executor_stale_replace_triggers_replace_test_function(tmp_path: Path):
    test_file = tmp_path / "test_service.py"
    test_file.write_text(
        "def test_velocity():\n"
        "    assert 1 + 1 == 2\n"
        "\n"
        "def test_other():\n"
        "    assert True\n",
        encoding="utf-8",
    )

    new_fn = (
        "def test_velocity():\n"
        "    result = 1 + 1\n"
        "    assert result == 2\n"
        "    assert result > 0\n"
    )
    ops = [
        {
            "op": "replace_in_file",
            "path": "test_service.py",
            "old": "def test_velocity():\n    STALE TEXT THAT DOES NOT EXIST\n",
            "new": new_fn,
        }
    ]

    result = ExecutorService.execute_file_ops(tmp_path, ops)

    assert result["success"], result.get("output")
    assert "test_service.py" in result["files_changed"]
    assert "patch_helper" in result["output"]
    content = test_file.read_text()
    assert "assert result > 0" in content
    assert "def test_other():" in content


def test_executor_stale_replace_triggers_add_missing_import(tmp_path: Path):
    src_file = tmp_path / "utils.py"
    src_file.write_text("import os\n\ndef foo(): pass\n", encoding="utf-8")

    ops = [
        {
            "op": "replace_in_file",
            "path": "utils.py",
            "old": "import os\nimport DOES_NOT_EXIST",
            "new": "import os\nimport json",
        }
    ]

    result = ExecutorService.execute_file_ops(tmp_path, ops)

    assert result["success"], result.get("output")
    assert "utils.py" in result["files_changed"]
    assert "import json" in src_file.read_text()


def test_executor_stale_replace_with_placeholder_replacement_fails(tmp_path: Path):
    test_file = tmp_path / "test_service.py"
    test_file.write_text(
        "def test_velocity():\n    assert compute() == 10\n", encoding="utf-8"
    )

    ops = [
        {
            "op": "replace_in_file",
            "path": "test_service.py",
            "old": "def test_velocity():\n    STALE\n",
            "new": "def test_velocity():\n    pass\n",
        }
    ]

    result = ExecutorService.execute_file_ops(tmp_path, ops)

    assert not result["success"]
    assert "patch_helper" in result["output"]
    assert "placeholder" in result["output"]


def test_executor_stale_replace_non_python_file_returns_standard_error(tmp_path: Path):
    cfg = tmp_path / "config.yaml"
    cfg.write_text("key: old_value\n", encoding="utf-8")

    ops = [
        {
            "op": "replace_in_file",
            "path": "config.yaml",
            "old": "DOES_NOT_EXIST",
            "new": "key: new_value",
        }
    ]

    result = ExecutorService.execute_file_ops(tmp_path, ops)

    assert not result["success"]
    assert "patch_helper" not in result["output"]
    assert "not found" in result["output"]
