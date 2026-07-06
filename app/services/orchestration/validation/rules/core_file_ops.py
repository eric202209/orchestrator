"""Core-invariant file-operation contract rules.

Moved from validator.py in Phase 20M (validator rule split). Functions here
cover file-op alias/nesting shape checks, task-workspace file-op path checks,
replace-in-file target/old-text contracts, read-only step inspection, and
read-only-stage mutation detection.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.orchestration.operations.file_ops_contract import (
    normalize_file_op_shape,
    operation_has_file_op_path,
)

from ..workspace_guard import TaskWorkspaceViolationError, normalize_path_reference

READ_ONLY_WORKFLOW_STAGES = {
    "diagnose",
    "plan",
    "review",
    "validate",
    "validation",
    "complete",
}


def _file_op_alias_issue(operation: Any) -> Optional[str]:
    if not isinstance(operation, dict) or "o" not in operation:
        return None

    supported_aliases = {"write_file", "append_file", "replace_in_file"}
    alias_name = str(operation.get("o") or "").strip()
    explicit_name = str(operation.get("op") or "").strip()
    if explicit_name and explicit_name != alias_name:
        return f"conflicting op alias values: op={explicit_name}, o={alias_name}"
    if alias_name not in supported_aliases:
        return f"unsupported file op alias: {alias_name or '<empty>'}"

    required_fields = {
        "write_file": {"path", "content"},
        "append_file": {"path", "content"},
        "replace_in_file": {"path", "old", "new"},
    }[alias_name]
    missing = sorted(
        field
        for field in required_fields
        if not isinstance(operation.get(field), str)
        or (field == "path" and not operation.get(field).strip())
    )
    if missing:
        return f"{alias_name} alias missing required fields: {missing}"
    return None


def _nested_file_op_issue(operation: Any) -> Optional[str]:
    if not isinstance(operation, dict) or "op" in operation:
        return None

    nested_file_op_names = {"write_file", "append_file", "replace_in_file"}
    nested_keys = [key for key in operation if key in nested_file_op_names]
    if not nested_keys:
        return None
    if len(operation) != 1 or len(nested_keys) != 1:
        return "ambiguous nested file op must contain exactly one file-op key"

    op_name = nested_keys[0]
    payload = operation.get(op_name)
    if not isinstance(payload, dict):
        return f"{op_name} payload must be an object"

    required_fields = {
        "write_file": {"path", "content"},
        "append_file": {"path", "content"},
        "replace_in_file": {"path", "old", "new"},
    }[op_name]
    missing = sorted(
        field
        for field in required_fields
        if not isinstance(payload.get(field), str)
        or (field == "path" and not payload.get(field).strip())
    )
    if missing:
        return f"{op_name} missing required fields: {missing}"
    return None


def _plan_invalid_file_ops_paths(
    plan: List[Dict[str, Any]], project_dir: Path
) -> List[int]:
    invalid_steps: List[int] = []
    for index, step in enumerate(plan, start=1):
        step_number = step.get("step_number", index)
        for operation in step.get("ops", []) or []:
            try:
                normalize_path_reference(str(operation.get("path") or ""), project_dir)
            except TaskWorkspaceViolationError:
                invalid_steps.append(int(step_number))
                break
    return sorted(set(invalid_steps))


def _plan_replace_ops_missing_targets(
    plan: List[Dict[str, Any]], project_dir: Path
) -> Dict[int, List[str]]:
    known_paths = {
        str(path.relative_to(project_dir))
        for path in project_dir.rglob("*")
        if path.is_file()
    }
    missing_by_step: Dict[int, List[str]] = {}

    for index, step in enumerate(plan, start=1):
        step_number = int(step.get("step_number", index))
        for raw_operation in step.get("ops", []) or []:
            if not isinstance(raw_operation, dict):
                continue
            operation = normalize_file_op_shape(raw_operation)
            op_name = str(operation.get("op") or "")
            raw_path = str(operation.get("path") or "")
            if not raw_path.strip():
                continue
            try:
                relative_path = normalize_path_reference(raw_path, project_dir)
            except TaskWorkspaceViolationError:
                continue
            if relative_path == ".":
                continue
            if op_name == "replace_in_file" and relative_path not in known_paths:
                missing_by_step.setdefault(step_number, []).append(relative_path)
            elif op_name in {"write_file", "append_file"}:
                known_paths.add(relative_path)
            elif op_name == "delete_file":
                known_paths.discard(relative_path)

    return {
        step: sorted(set(paths)) for step, paths in missing_by_step.items() if paths
    }


def _replace_in_file_has_repairable_old_text_issue(operation: Any) -> bool:
    if not isinstance(operation, dict):
        return False
    if str(operation.get("op") or "").strip() != "replace_in_file":
        return False
    path = operation.get("path")
    if not isinstance(path, str) or not path.strip():
        return False
    normalized = normalize_file_op_shape(operation)
    new_value = normalized.get("new")
    if not isinstance(new_value, str):
        new_value = operation.get("new_text")
    if not isinstance(new_value, str):
        return False
    old_present = "old" in operation or "old_text" in operation
    old_value = (
        operation.get("old") if "old" in operation else operation.get("old_text")
    )
    return not old_present or not isinstance(old_value, str) or not old_value


def _plan_empty_replace_old_text_steps(
    plan: List[Dict[str, Any]]
) -> Dict[int, List[str]]:
    empty_by_step: Dict[int, List[str]] = {}
    for index, step in enumerate(plan, start=1):
        if not isinstance(step, dict):
            continue
        step_number = int(step.get("step_number") or index)
        for raw_operation in step.get("ops", []) or []:
            if not _replace_in_file_has_repairable_old_text_issue(raw_operation):
                continue
            rel_path = str(raw_operation.get("path") or "").strip().lstrip("./")
            empty_by_step.setdefault(step_number, []).append(
                rel_path or "<missing path>"
            )
    return {step: sorted(set(paths)) for step, paths in empty_by_step.items() if paths}


def _step_is_readonly_inspection(step: Dict[str, Any]) -> bool:
    ops = step.get("ops") or []
    if isinstance(ops, list) and any(operation_has_file_op_path(op) for op in ops):
        return False
    commands = [
        str(command or "").strip()
        for command in (step.get("commands", []) or [])
        if str(command or "").strip()
    ]
    if not commands:
        return False
    readonly_prefixes = (
        "ls",
        "cat",
        "pwd",
        "find",
        "rg",
        "grep",
        "wc",
        "head",
        "tail",
        "sed -n",
    )
    if not all(command.startswith(readonly_prefixes) for command in commands):
        return False
    description = str(step.get("description") or "").lower()
    inspection_markers = (
        "inspect",
        "review",
        "analyze",
        "inventory",
        "audit",
        "list",
        "current workspace",
        "current project",
    )
    return any(marker in description for marker in inspection_markers)


def _plan_mutating_steps_for_read_only_stage(
    plan: List[Dict[str, Any]], workflow_stage: Optional[str]
) -> List[int]:
    if workflow_stage not in READ_ONLY_WORKFLOW_STAGES:
        return []

    mutating_ops = {
        "write_file",
        "append_file",
        "replace_in_file",
        "create_file",
        "mkdir",
        "delete_file",
    }
    mutating_command_patterns = (
        re.compile(r"(^|[;&|]\s*)(mkdir|touch|cp|mv|rm)\b"),
        re.compile(r"\bsed\s+-i\b"),
        re.compile(r">\s*[^&\s]"),
        re.compile(r"\btee\s+"),
    )
    findings: List[int] = []
    for index, step in enumerate(plan, start=1):
        step_number = int(step.get("step_number", index))
        for operation in step.get("ops") or []:
            if not isinstance(operation, dict):
                continue
            op_name = str(operation.get("op") or "").strip()
            if op_name not in mutating_ops:
                continue
            path_text = str(operation.get("path") or "").strip().lstrip("./")
            if _read_only_stage_allows_report_write(workflow_stage, op_name, path_text):
                continue
            findings.append(step_number)
            break
        if step_number in findings:
            continue
        commands = [str(command or "") for command in step.get("commands", []) or []]
        for command in commands:
            command_text = command.strip()
            patterns = mutating_command_patterns
            if command_text.startswith(("python -c ", "python3 -c ")):
                patterns = (
                    mutating_command_patterns[0],
                    mutating_command_patterns[1],
                    mutating_command_patterns[3],
                )
            if any(pattern.search(command) for pattern in patterns):
                findings.append(step_number)
                break
    return findings


def _read_only_stage_allows_report_write(
    workflow_stage: Optional[str], op_name: str, path_text: str
) -> bool:
    """Allow read-only stages to materialize their own report artifact only."""

    if op_name not in {"write_file", "append_file"}:
        return False
    normalized_path = str(path_text or "").strip().rstrip("/").lstrip("./")
    allowed_by_stage = {
        "review": {"docs/review.md"},
        "validate": {"docs/validation.md"},
        "validation": {"docs/validation.md"},
        "complete": {"docs/completion.md", "docs/report.md"},
    }
    return normalized_path in allowed_by_stage.get(str(workflow_stage or ""), set())
