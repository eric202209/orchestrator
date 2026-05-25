"""Workspace-scoping helpers for task completion."""

from __future__ import annotations

from pathlib import Path
from typing import Any


_PYTHON_SUFFIXES = {".py"}
_NODE_SUFFIXES = {".js", ".ts", ".tsx", ".jsx", ".mjs", ".cjs"}


def _stack_set_for_paths(paths: list[str]) -> set[str]:
    stacks: set[str] = set()
    for raw_path in paths or []:
        suffix = Path(str(raw_path or "").strip()).suffix.lower()
        if suffix in _PYTHON_SUFFIXES:
            stacks.add("python")
        elif suffix in _NODE_SUFFIXES:
            stacks.add("node")
    return stacks


def _completion_expected_paths(plan: list[dict[str, Any]]) -> list[str]:
    paths: list[str] = []
    for step in plan or []:
        paths.extend(str(path or "") for path in step.get("expected_files", []) or [])
        for op in step.get("ops", []) or []:
            if isinstance(op, dict):
                paths.append(str(op.get("path") or ""))
    return [path for path in paths if path.strip()]


def _scope_workspace_consistency_to_task_changes(
    workspace_consistency: dict[str, Any],
    *,
    plan: list[dict[str, Any]],
    reported_changed_files: list[str],
) -> dict[str, Any]:
    """Do not fail single-stack work because another stack already exists."""

    if not workspace_consistency.get("mixed_stack"):
        return workspace_consistency

    task_paths = list(reported_changed_files or []) + _completion_expected_paths(plan)
    task_stacks = _stack_set_for_paths(task_paths)
    if len(task_stacks) != 1:
        return workspace_consistency

    scoped = dict(workspace_consistency)
    scoped["workspace_mixed_stack"] = True
    scoped["mixed_stack"] = False
    scoped["task_scoped_stack"] = next(iter(task_stacks))
    scoped["mixed_stack_scope"] = "preexisting_workspace_ignored"
    return scoped
