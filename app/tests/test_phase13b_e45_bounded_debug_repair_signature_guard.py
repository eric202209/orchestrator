"""Phase 13B-E45: Post-repair signature guard unit tests."""

from __future__ import annotations

from pathlib import Path

import pytest

from app.services.orchestration.diagnostics.signature_guard import (
    BOUNDED_DEBUG_REPAIR_SIGNATURE_VIOLATION_REASON,
    SignatureViolation,
    check_bounded_debug_repair_signature_contract,
    signature_violation_event_details,
)


# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------


def _write_py(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


def _write_op(rel_path: str, content: str) -> dict:
    return {"op": "write_file", "path": rel_path, "content": content}


def _replace_op(rel_path: str, old: str, new: str) -> dict:
    return {"op": "replace_in_file", "path": rel_path, "old": old, "new": new}


def _append_op(rel_path: str, content: str) -> dict:
    return {"op": "append_file", "path": rel_path, "content": content}


# ---------------------------------------------------------------------------
# test 1 — reject full replacement (M4 pattern, signature_changed)
# ---------------------------------------------------------------------------


def test_guard_rejects_signature_changed_full_replacement(tmp_path):
    _write_py(
        tmp_path / "src/medium_cli/formatting.py",
        "def format_summary(total: int, completed: int) -> str:\n    pass\n",
    )
    ops = [
        _write_op(
            "src/medium_cli/formatting.py",
            "def format_summary(store) -> str:\n    return str(store)\n",
        )
    ]
    violations = check_bounded_debug_repair_signature_contract(
        project_dir=tmp_path, ops=ops
    )
    assert len(violations) == 1
    v = violations[0]
    assert v.violation_type == "signature_changed"
    assert v.qualified_name == "format_summary"
    assert v.pre_signature == "(total, completed)"
    assert v.post_signature == "(store)"


# ---------------------------------------------------------------------------
# test 2 — reject dual-definition (M1 pattern, duplicate_definition)
# ---------------------------------------------------------------------------


def test_guard_rejects_duplicate_definition(tmp_path):
    _write_py(
        tmp_path / "src/medium_cli/formatting.py",
        "def format_summary(total: int, completed: int) -> str:\n    pass\n",
    )
    post_content = (
        "def format_summary(total: int, completed: int) -> str:\n"
        "    pass\n\n"
        "def format_summary(store) -> str:\n"
        "    return str(store)\n"
    )
    ops = [_write_op("src/medium_cli/formatting.py", post_content)]
    violations = check_bounded_debug_repair_signature_contract(
        project_dir=tmp_path, ops=ops
    )
    assert len(violations) == 1
    v = violations[0]
    assert v.violation_type == "duplicate_definition"
    assert v.qualified_name == "format_summary"
    assert "(total, completed)" in v.pre_signature
    assert "store" in v.post_signature


# ---------------------------------------------------------------------------
# test 3 — allow stub body implementation (same signature, body changes)
# ---------------------------------------------------------------------------


def test_guard_allows_stub_body_implementation(tmp_path):
    _write_py(
        tmp_path / "src/medium_cli/formatting.py",
        "def format_summary(total: int, completed: int) -> str:\n"
        "    raise NotImplementedError\n",
    )
    post_content = (
        "def format_summary(total: int, completed: int) -> str:\n"
        "    return f'{total} tasks, {completed} complete'\n"
    )
    ops = [_write_op("src/medium_cli/formatting.py", post_content)]
    violations = check_bounded_debug_repair_signature_contract(
        project_dir=tmp_path, ops=ops
    )
    assert violations == []


# ---------------------------------------------------------------------------
# test 4 — allow adding new helper function
# ---------------------------------------------------------------------------


def test_guard_allows_new_helper_function_added(tmp_path):
    _write_py(
        tmp_path / "src/medium_cli/formatting.py",
        "def format_summary(total: int, completed: int) -> str:\n    pass\n",
    )
    post_content = (
        "def format_summary(total: int, completed: int) -> str:\n"
        "    return _fmt(total, completed)\n\n"
        "def _fmt(total, completed):\n"
        "    return f'{total} tasks, {completed} complete'\n"
    )
    ops = [_write_op("src/medium_cli/formatting.py", post_content)]
    violations = check_bounded_debug_repair_signature_contract(
        project_dir=tmp_path, ops=ops
    )
    assert violations == []


# ---------------------------------------------------------------------------
# test 5 — reject method signature change
# ---------------------------------------------------------------------------


def test_guard_rejects_method_signature_changed(tmp_path):
    _write_py(
        tmp_path / "src/medium_cli/store.py",
        "class TaskStore:\n    def summary(self) -> str:\n        pass\n",
    )
    post_content = (
        "class TaskStore:\n"
        "    def summary(self, verbose: bool = False) -> str:\n"
        "        return ''\n"
    )
    ops = [_write_op("src/medium_cli/store.py", post_content)]
    violations = check_bounded_debug_repair_signature_contract(
        project_dir=tmp_path, ops=ops
    )
    assert len(violations) == 1
    v = violations[0]
    assert v.violation_type == "signature_changed"
    assert v.qualified_name == "TaskStore.summary"
    assert v.pre_signature == "(self)"
    assert "verbose" in v.post_signature


# ---------------------------------------------------------------------------
# test 6 — reject duplicate method definition
# ---------------------------------------------------------------------------


def test_guard_rejects_duplicate_method_definition(tmp_path):
    _write_py(
        tmp_path / "src/medium_cli/store.py",
        "class TaskStore:\n    def summary(self) -> str:\n        pass\n",
    )
    post_content = (
        "class TaskStore:\n"
        "    def summary(self) -> str:\n"
        "        pass\n\n"
        "    def summary(self, extra) -> str:\n"
        "        return extra\n"
    )
    ops = [_write_op("src/medium_cli/store.py", post_content)]
    violations = check_bounded_debug_repair_signature_contract(
        project_dir=tmp_path, ops=ops
    )
    assert len(violations) == 1
    v = violations[0]
    assert v.violation_type == "duplicate_definition"
    assert v.qualified_name == "TaskStore.summary"


# ---------------------------------------------------------------------------
# test 7 — reject unparsable post-repair Python
# ---------------------------------------------------------------------------


def test_guard_rejects_post_parse_error(tmp_path):
    _write_py(
        tmp_path / "src/medium_cli/formatting.py",
        "def format_summary(total: int, completed: int) -> str:\n    pass\n",
    )
    ops = [
        _write_op(
            "src/medium_cli/formatting.py", "def format_summary(\n  BAD SYNTAX HERE\n"
        )
    ]
    violations = check_bounded_debug_repair_signature_contract(
        project_dir=tmp_path, ops=ops
    )
    assert len(violations) == 1
    v = violations[0]
    assert v.violation_type == "post_parse_error"
    assert v.qualified_name == "<module>"
    assert v.pre_signature is None
    assert v.post_signature is not None


# ---------------------------------------------------------------------------
# test 8 — no violation when file does not exist pre-repair (new file)
# ---------------------------------------------------------------------------


def test_guard_allows_new_python_file(tmp_path):
    ops = [
        _write_op(
            "src/medium_cli/helpers.py",
            "def helper(x: int) -> str:\n    return str(x)\n",
        )
    ]
    violations = check_bounded_debug_repair_signature_contract(
        project_dir=tmp_path, ops=ops
    )
    assert violations == []


# ---------------------------------------------------------------------------
# test 9 — no violation for non-Python file ops
# ---------------------------------------------------------------------------


def test_guard_no_violation_non_python_file(tmp_path):
    ops = [
        {"op": "write_file", "path": "config.json", "content": '{"key": "val"}'},
        {"op": "write_file", "path": "README.md", "content": "# docs"},
    ]
    violations = check_bounded_debug_repair_signature_contract(
        project_dir=tmp_path, ops=ops
    )
    assert violations == []


# ---------------------------------------------------------------------------
# test 10 — signature_violation_event_details structure
# ---------------------------------------------------------------------------


def test_guard_violation_event_details_structure():
    violations = [
        SignatureViolation(
            path="src/medium_cli/formatting.py",
            qualified_name="format_summary",
            violation_type="signature_changed",
            pre_signature="(total, completed)",
            post_signature="(store)",
        )
    ]
    details = signature_violation_event_details(violations)

    assert "bounded_execution_debug_repair_signature_violations" in details
    assert "bounded_execution_debug_repair_signature_violation_paths" in details
    assert "bounded_execution_debug_repair_signature_violation_types" in details

    rows = details["bounded_execution_debug_repair_signature_violations"]
    assert len(rows) == 1
    assert rows[0]["qualified_name"] == "format_summary"
    assert rows[0]["violation_type"] == "signature_changed"
    assert rows[0]["pre_signature"] == "(total, completed)"
    assert rows[0]["post_signature"] == "(store)"

    assert details["bounded_execution_debug_repair_signature_violation_paths"] == [
        "src/medium_cli/formatting.py"
    ]
    assert details["bounded_execution_debug_repair_signature_violation_types"] == [
        "signature_changed"
    ]

    assert (
        BOUNDED_DEBUG_REPAIR_SIGNATURE_VIOLATION_REASON
        == "bounded_execution_debug_repair_signature_contract_violation"
    )


# ---------------------------------------------------------------------------
# test 11 — reject missing existing definition
# ---------------------------------------------------------------------------


def test_guard_rejects_missing_existing_definition(tmp_path):
    _write_py(
        tmp_path / "src/medium_cli/formatting.py",
        "def format_summary(total: int, completed: int) -> str:\n    pass\n\n"
        "def format_task_line(task: object) -> str:\n    pass\n",
    )
    post_content = "def format_task_line(task: object) -> str:\n    return str(task)\n"
    ops = [_write_op("src/medium_cli/formatting.py", post_content)]
    violations = check_bounded_debug_repair_signature_contract(
        project_dir=tmp_path, ops=ops
    )
    assert len(violations) == 1
    v = violations[0]
    assert v.violation_type == "missing_existing_definition"
    assert v.qualified_name == "format_summary"
    assert v.pre_signature == "(total, completed)"
    assert v.post_signature is None


# ---------------------------------------------------------------------------
# test 12 — replace_in_file op type is handled
# ---------------------------------------------------------------------------


def test_guard_handles_replace_in_file_op(tmp_path):
    pre_content = (
        "def format_summary(total: int, completed: int) -> str:\n"
        "    raise NotImplementedError\n"
    )
    _write_py(tmp_path / "src/medium_cli/formatting.py", pre_content)

    old = "    raise NotImplementedError\n"
    new = "    return f'{total} tasks, {completed} complete'\n"
    ops = [_replace_op("src/medium_cli/formatting.py", old, new)]

    violations = check_bounded_debug_repair_signature_contract(
        project_dir=tmp_path, ops=ops
    )
    assert violations == []
