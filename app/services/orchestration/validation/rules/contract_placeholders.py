"""Workload-contract placeholder / fake-artifact detection rules.

Moved verbatim from validator.py in Phase 20K (validator rule split,
slice 1). Functions here inspect plan write operations and commands for
placeholder/stub implementations and invented (unmaterialized)
verification artifacts.
"""

from __future__ import annotations

import ast
import re
import shlex
from pathlib import Path
from typing import Any, Dict, List

from ..placeholder_policy import path_allows_placeholder_fixture_content

PLAN_STRUCTURAL_PLACEHOLDER_MARKER_PATTERN = re.compile(
    r"\b(?:placeholder|stub|notimplemented|notimplementederror)\b|"
    r"\bnot[-_\s]*implemented\b",
    re.IGNORECASE,
)
PLAN_PASS_MARKER_PATTERN = re.compile(r"\bpass\b", re.IGNORECASE)
PLAN_TODO_FIXME_MARKER_PATTERN = re.compile(r"\b(?:todo|fixme)\b", re.IGNORECASE)


def _task_allows_todo_fixme_literals(task_prompt: str) -> bool:
    lowered = str(task_prompt or "").lower()
    if not any(marker in lowered for marker in ("todo", "fixme")):
        return False
    return any(
        intent in lowered
        for intent in (
            "report",
            "scan",
            "scanner",
            "generator",
            "detect",
            "extract",
            "list",
            "summar",
        )
    )


def _write_file_content_has_placeholder_implementation(
    path_text: str, content: str, *, allow_todo_fixme_literals: bool = False
) -> bool:
    raw = str(content or "")
    if path_allows_placeholder_fixture_content(path_text):
        return False

    if PLAN_STRUCTURAL_PLACEHOLDER_MARKER_PATTERN.search(raw):
        return True
    if not allow_todo_fixme_literals and PLAN_TODO_FIXME_MARKER_PATTERN.search(raw):
        return True
    if not PLAN_PASS_MARKER_PATTERN.search(raw):
        return False

    if Path(str(path_text or "")).suffix.lower() != ".py":
        return True

    try:
        tree = ast.parse(raw)
    except SyntaxError:
        return True

    for node in ast.walk(tree):
        body = getattr(node, "body", None)
        if not isinstance(body, list) or len(body) != 1:
            continue
        if isinstance(body[0], ast.Pass) and isinstance(
            node, (ast.Module, ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)
        ):
            return True
    return False


def _command_write_targets(command: str) -> List[str]:
    from ..validator import ValidatorService

    targets = ValidatorService._single_file_write_heredoc_targets(command)
    try:
        tokens = shlex.split(str(command or ""), posix=True)
    except ValueError:
        tokens = str(command or "").split()

    index = 0
    while index < len(tokens):
        token = tokens[index]
        if token in {">", ">>"} and index + 1 < len(tokens):
            targets.append(tokens[index + 1])
            index += 2
            continue
        if token.startswith((">", ">>")) and token not in {">&1", ">&2"}:
            target = token.lstrip(">")
            if target:
                targets.append(target)
        if token == "tee":
            next_index = index + 1
            while next_index < len(tokens) and tokens[next_index].startswith("-"):
                next_index += 1
            if next_index < len(tokens):
                targets.append(tokens[next_index])
        index += 1

    return [target for target in targets if target]


def _command_writes_placeholder_implementation(
    command: str, *, allow_todo_fixme_literals: bool = False
) -> bool:
    raw = str(command or "")
    has_marker = (
        PLAN_STRUCTURAL_PLACEHOLDER_MARKER_PATTERN.search(raw)
        or PLAN_PASS_MARKER_PATTERN.search(raw)
        or (
            not allow_todo_fixme_literals and PLAN_TODO_FIXME_MARKER_PATTERN.search(raw)
        )
    )
    if not has_marker:
        return False

    targets = _command_write_targets(raw)
    if targets:
        return not all(
            path_allows_placeholder_fixture_content(target) for target in targets
        )

    return False


def _plan_placeholder_source_write_ops(
    plan: List[Dict[str, Any]], task_prompt: str = ""
) -> List[Dict[str, Any]]:
    allow_todo_fixme_literals = _task_allows_todo_fixme_literals(task_prompt)
    findings: List[Dict[str, Any]] = []

    for index, step in enumerate(plan, start=1):
        step_number = step.get("step_number", index)
        for operation in step.get("ops", []) or []:
            if not isinstance(operation, dict):
                continue
            if operation.get("op") != "write_file":
                continue
            path_text = str(operation.get("path", "")).strip().lstrip("./")
            content = str(operation.get("content", ""))
            if not _write_file_content_has_placeholder_implementation(
                path_text,
                content,
                allow_todo_fixme_literals=allow_todo_fixme_literals,
            ):
                continue
            findings.append(
                {
                    "step_number": step_number,
                    "op": "write_file",
                    "path": path_text,
                    "content_excerpt": " ".join(content.split())[:260],
                }
            )

    return findings


def _plan_contains_placeholder_commands(
    plan: List[Dict[str, Any]], task_prompt: str = ""
) -> bool:
    allow_todo_fixme_literals = _task_allows_todo_fixme_literals(task_prompt)

    for step in plan:
        for command in step.get("commands", []) or []:
            if _command_writes_placeholder_implementation(
                str(command or ""),
                allow_todo_fixme_literals=allow_todo_fixme_literals,
            ):
                return True
    return False


def _plan_contains_placeholder_intent(
    plan: List[Dict[str, Any]], task_prompt: str = ""
) -> bool:
    return bool(
        _plan_placeholder_source_write_ops(plan, task_prompt)
    ) or _plan_contains_placeholder_commands(plan, task_prompt)


def _plan_materialized_file_targets(plan: List[Dict[str, Any]]) -> set[str]:
    files: set[str] = set()
    for step in plan:
        for operation in step.get("ops", []) or []:
            if not isinstance(operation, dict):
                continue
            if str(operation.get("op") or "") in {
                "write_file",
                "append_file",
                "replace_in_file",
            }:
                path = str(operation.get("path") or "").strip().rstrip("/").lstrip("./")
                if path:
                    files.add(path)
        top_level_op = str(
            step.get("op") or step.get("step") or step.get("type") or ""
        ).strip()
        if top_level_op in {"create_file", "write_file", "write", "append_file"}:
            path = (
                str(step.get("path") or step.get("file") or "")
                .strip()
                .rstrip("/")
                .lstrip("./")
            )
            if path:
                files.add(path)
        for command in step.get("commands", []) or []:
            for target in _command_write_targets(str(command or "")):
                path = str(target or "").strip().rstrip("/").lstrip("./")
                if path:
                    files.add(path)
    return files


def _step_uses_fake_verification_artifact(step: Dict[str, Any]) -> bool:
    """Detect invented test-output artifacts used instead of test exit codes."""

    fake_artifact_pattern = re.compile(
        r"(?<![A-Za-z0-9_.~/-])"
        r"((?:tests?|spec)/[A-Za-z0-9_./-]*\.(?:out|log|txt))"
        r"(?![A-Za-z0-9_.-])",
        re.IGNORECASE,
    )
    step_text_parts = [
        str(step.get("verification") or ""),
        str(step.get("rollback") or ""),
    ]
    step_text_parts.extend(
        str(command or "") for command in step.get("commands", []) or []
    )
    step_text_parts.extend(
        str(path or "") for path in step.get("expected_files", []) or []
    )
    mentioned = {
        match.group(1).strip().lstrip("./")
        for text in step_text_parts
        for match in fake_artifact_pattern.finditer(text)
    }
    if not mentioned:
        return False

    materialized: set[str] = set()
    for operation in step.get("ops", []) or []:
        if not isinstance(operation, dict):
            continue
        if str(operation.get("op") or "") in {"write_file", "append_file"}:
            path = str(operation.get("path") or "").strip().lstrip("./")
            if path:
                materialized.add(path)
    for command in step.get("commands", []) or []:
        for target in _command_write_targets(str(command or "")):
            path = str(target or "").strip().lstrip("./")
            if path:
                materialized.add(path)

    return bool(mentioned.difference(materialized))


def _plan_fake_verification_artifact_steps(plan: List[Dict[str, Any]]) -> List[int]:
    steps: List[int] = []
    for index, step in enumerate(plan, start=1):
        if _step_uses_fake_verification_artifact(step):
            steps.append(int(step.get("step_number", index)))
    return sorted(set(steps))
