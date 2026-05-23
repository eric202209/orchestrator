"""Deterministic patch helpers for Python file edits.

Invoked by the executor when replace_in_file old-text is not found and intent
can be inferred from the new content. Mutation authority stays in the
orchestrator; the model only proposes the replacement content.
"""

from __future__ import annotations

import ast
import os
import subprocess
import sys
import textwrap
from dataclasses import dataclass
from pathlib import Path
from typing import Optional


@dataclass
class PatchResult:
    success: bool
    evidence: str


def try_deterministic_patch(
    file_path: Path,
    old: str,
    new: str,
    project_dir: Optional[Path] = None,
) -> Optional[PatchResult]:
    """Try a deterministic patch when replace_in_file old text is not found.

    Returns PatchResult if a helper was applicable (success or rejection).
    Returns None if no helper applies; caller falls through to its normal error.
    """
    if file_path.suffix != ".py":
        return None

    if _is_test_file(file_path):
        fn_name = _infer_test_function_name(new)
        if fn_name:
            return replace_test_function(
                file_path, fn_name, new, project_dir=project_dir
            )

    import_stmt = _infer_added_import(old, new)
    if import_stmt:
        return add_missing_import(file_path, import_stmt)

    return None


def add_missing_import(file_path: Path | str, import_stmt: str) -> PatchResult:
    """Insert import_stmt into a Python file if not already present.

    Locates the last import node via AST and inserts after it.
    Runs py_compile to verify the result is valid.
    """
    path = Path(file_path)
    try:
        content = path.read_text(encoding="utf-8")
    except OSError as exc:
        return PatchResult(False, f"add_missing_import: cannot read {path.name}: {exc}")

    stmt = import_stmt.strip()

    if any(line.strip() == stmt for line in content.splitlines()):
        return PatchResult(
            True, f"add_missing_import: '{stmt}' already present in {path.name}"
        )

    try:
        tree = ast.parse(content)
    except SyntaxError as exc:
        return PatchResult(
            False, f"add_missing_import: {path.name} has syntax error: {exc}"
        )

    import_nodes = [
        node for node in tree.body if isinstance(node, (ast.Import, ast.ImportFrom))
    ]

    lines = content.splitlines(keepends=True)
    if import_nodes:
        insert_after = max(node.end_lineno for node in import_nodes)
        lines.insert(insert_after, stmt + "\n")
    else:
        insert_at = 0
        for node in tree.body:
            if (
                isinstance(node, ast.Expr)
                and isinstance(node.value, ast.Constant)
                and isinstance(node.value.value, str)
            ):
                insert_at = node.end_lineno
            else:
                break
        lines.insert(insert_at, stmt + "\n")

    new_content = "".join(lines)

    try:
        ast.parse(new_content)
    except SyntaxError as exc:
        return PatchResult(False, f"add_missing_import: result has syntax error: {exc}")

    path.write_text(new_content, encoding="utf-8")

    result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(path)],
        capture_output=True,
        text=True,
    )
    if result.returncode != 0:
        return PatchResult(
            False, f"add_missing_import: py_compile failed: {result.stderr.strip()}"
        )

    return PatchResult(True, f"add_missing_import: inserted '{stmt}' into {path.name}")


def replace_test_function(
    file_path: Path | str,
    test_name: str,
    new_fn_content: str,
    *,
    project_dir: Optional[Path | str] = None,
) -> PatchResult:
    """Replace a single test function by name, preserving all other file content.

    Locates the target via AST, validates the replacement has assertions and is
    not a placeholder, splices it in, then runs py_compile and targeted pytest.
    """
    path = Path(file_path)
    try:
        original = path.read_text(encoding="utf-8")
    except OSError as exc:
        return PatchResult(
            False, f"replace_test_function: cannot read {path.name}: {exc}"
        )

    try:
        orig_tree = ast.parse(original)
    except SyntaxError as exc:
        return PatchResult(
            False, f"replace_test_function: {path.name} has syntax error: {exc}"
        )

    targets = _find_test_targets(orig_tree, test_name)
    if not targets:
        return PatchResult(
            False, f"replace_test_function: '{test_name}' not found in {path.name}"
        )
    if len(targets) > 1:
        return PatchResult(
            False,
            f"replace_test_function: ambiguous — {len(targets)} definitions of '{test_name}' in {path.name}",
        )

    target_node, target_class = targets[0]
    orig_assertion_count = _assertion_count(target_node)

    try:
        new_tree = ast.parse(new_fn_content)
    except SyntaxError as exc:
        return PatchResult(
            False, f"replace_test_function: replacement content has syntax error: {exc}"
        )

    replacement_fns = [
        node
        for node in ast.walk(new_tree)
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
        and node.name == test_name
    ]
    if not replacement_fns:
        return PatchResult(
            False,
            f"replace_test_function: replacement does not contain '{test_name}'",
        )

    replacement_fn = replacement_fns[0]

    if _is_placeholder_function(replacement_fn):
        return PatchResult(
            False,
            f"replace_test_function: replacement for '{test_name}' is a placeholder; rejected",
        )

    new_assertion_count = _assertion_count(replacement_fn)
    if orig_assertion_count > 0 and new_assertion_count == 0:
        return PatchResult(
            False,
            f"replace_test_function: replacement for '{test_name}' has no assertions "
            f"(original had {orig_assertion_count}); rejected to preserve test integrity",
        )

    start_line = (
        target_node.decorator_list[0].lineno
        if target_node.decorator_list
        else target_node.lineno
    )
    end_line = target_node.end_lineno  # inclusive, 1-indexed

    lines = original.splitlines(keepends=True)
    before = lines[: start_line - 1]
    after = lines[end_line:]
    original_line = lines[start_line - 1]
    target_indent = original_line[: len(original_line) - len(original_line.lstrip())]
    replacement_text = _indent_replacement(new_fn_content, target_indent)
    result_content = "".join(before) + replacement_text + "".join(after)

    try:
        ast.parse(result_content)
    except SyntaxError as exc:
        return PatchResult(
            False,
            f"replace_test_function: spliced result has syntax error: {exc}",
        )

    path.write_text(result_content, encoding="utf-8")

    compile_result = subprocess.run(
        [sys.executable, "-m", "py_compile", str(path)],
        capture_output=True,
        text=True,
    )
    if compile_result.returncode != 0:
        path.write_text(original, encoding="utf-8")
        return PatchResult(
            False,
            f"replace_test_function: py_compile failed: {compile_result.stderr.strip()}",
        )

    cwd = str(project_dir) if project_dir else None
    pytest_target = (
        f"{path}::{target_class}::{test_name}"
        if target_class
        else f"{path}::{test_name}"
    )
    pytest_result = subprocess.run(
        [sys.executable, "-m", "pytest", "-q", "--tb=short", pytest_target],
        capture_output=True,
        text=True,
        cwd=cwd,
        env=_env_with_absolute_pythonpath(),
    )
    if pytest_result.returncode != 0:
        path.write_text(original, encoding="utf-8")
        evidence = _summarize_pytest_failure(
            pytest_result.stdout + pytest_result.stderr
        )
        return PatchResult(
            False,
            f"replace_test_function: '{test_name}' still fails after patch: {evidence}",
        )

    return PatchResult(
        True,
        f"replace_test_function: '{test_name}' replaced in {path.name} "
        f"({new_assertion_count} assertion(s) verified)",
    )


# --- Intent detection ---


def _infer_test_function_name(content: str) -> Optional[str]:
    """Return the test function name if content is a complete test function."""
    try:
        tree = ast.parse(content.strip())
    except SyntaxError:
        return None
    for node in tree.body:
        if isinstance(
            node, (ast.FunctionDef, ast.AsyncFunctionDef)
        ) and node.name.startswith("test"):
            return node.name
    return None


def _infer_added_import(old: str, new: str) -> Optional[str]:
    """Return an import line if new adds exactly one import statement over old."""
    old_lines = {line.strip() for line in old.splitlines() if line.strip()}
    new_lines = {line.strip() for line in new.splitlines() if line.strip()}
    added = [
        line
        for line in new_lines - old_lines
        if line.startswith("import ") or line.startswith("from ")
    ]
    if len(added) == 1:
        return added[0]
    return None


# --- Internal helpers ---


def _is_test_file(path: Path) -> bool:
    return path.name.startswith("test_") or path.name.endswith("_test.py")


def _find_test_targets(
    tree: ast.Module,
    test_name: str,
) -> list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, Optional[str]]]:
    targets: list[tuple[ast.FunctionDef | ast.AsyncFunctionDef, Optional[str]]] = []

    def visit_body(body: list[ast.stmt], class_name: Optional[str] = None) -> None:
        for node in body:
            if isinstance(node, ast.ClassDef):
                visit_body(node.body, node.name)
            elif (
                isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
                and node.name == test_name
            ):
                targets.append((node, class_name))

    visit_body(tree.body)
    return targets


def _assertion_count(node: ast.FunctionDef | ast.AsyncFunctionDef) -> int:
    return sum(
        1
        for child in ast.walk(node)
        if isinstance(child, ast.Assert) or _is_unittest_assert_call(child)
    )


def _is_unittest_assert_call(node: ast.AST) -> bool:
    return (
        isinstance(node, ast.Call)
        and isinstance(node.func, ast.Attribute)
        and node.func.attr.startswith("assert")
    )


def _is_placeholder_function(node: ast.FunctionDef | ast.AsyncFunctionDef) -> bool:
    non_trivial = [
        stmt
        for stmt in node.body
        if not isinstance(stmt, ast.Pass)
        and not (
            isinstance(stmt, ast.Expr)
            and isinstance(stmt.value, ast.Constant)
            and (stmt.value.value is ... or isinstance(stmt.value.value, str))
        )
    ]
    return len(non_trivial) == 0


def _indent_replacement(content: str, target_indent: str) -> str:
    replacement = textwrap.dedent(content.rstrip("\n"))
    if not target_indent:
        return replacement + "\n"
    return (
        "\n".join(
            f"{target_indent}{line}" if line.strip() else line
            for line in replacement.splitlines()
        )
        + "\n"
    )


def _summarize_pytest_failure(output: str) -> str:
    lines = [line.rstrip() for line in output.splitlines() if line.strip()]
    important = [
        line
        for line in lines
        if line.startswith("E   ")
        or line.startswith("FAILED ")
        or line.startswith("ERROR ")
        or "Error:" in line
    ]
    if important:
        return "\n".join(important)[:800]
    return "\n".join(lines[-12:])[:800]


def _env_with_absolute_pythonpath() -> dict[str, str]:
    """Return os.environ copy with all relative PYTHONPATH entries made absolute.

    Needed when subprocess runs with a different cwd than the parent process:
    relative entries like '.' and 'venv/lib/...' would resolve against the
    subprocess cwd rather than the parent cwd where they were originally set.
    """
    env = os.environ.copy()
    raw = env.get("PYTHONPATH", "")
    if not raw:
        return env
    here = Path.cwd()
    abs_entries = [
        str(here / entry) if entry and not os.path.isabs(entry) else entry
        for entry in raw.split(os.pathsep)
    ]
    env["PYTHONPATH"] = os.pathsep.join(abs_entries)
    return env
