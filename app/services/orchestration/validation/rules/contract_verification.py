"""Workload-contract verification-plan / evidence / weak-verification rules.

Moved verbatim from validator.py in Phase 20L (validator rule split,
slice 2). Functions here classify weak verification commands, detect
missing verification steps, and detect verification-only plans that
touch app/source assets they should not (missing workspace files,
newly-created source assets, mutated source assets).
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.orchestration.operations.file_ops_contract import (
    normalize_file_op_shape,
)

from ..workspace_checks import SOURCE_EXTENSIONS
from ..workspace_guard import TaskWorkspaceViolationError, normalize_path_reference


def _verification_is_weak(command: Optional[str]) -> bool:
    text = str(command or "").strip().lower()
    if not text:
        return True
    if (
        re.search(r"\bpython(?:3)?\s+-c\b", text)
        and "unittest.main" in text
        and "discover" not in text
    ):
        return True
    if (
        re.search(r"\bpython(?:3)?\s+-c\b", text)
        and "sys.exit(0)" in text
        and "assert " not in text
        and "pytest" not in text
        and "unittest" not in text
    ):
        return True
    meaningful_markers = (
        "pytest",
        "python3 -m",
        "python3 ",
        "python -m",
        "node -e",
        "node ",
        "npm test",
        "pnpm test",
        "cargo test",
        "go test",
        "python ",
        "uv run",
        "npm run build",
        "pnpm build",
        "yarn build",
        "tsc",
    )
    if any(marker in text for marker in meaningful_markers):
        return False
    weak_command_patterns = (
        r"test\s+-[fds]\b",
        r"grep\s+-q\b",
        r"ls\b",
        r"echo\b",
        r"cat\b",
        r"find\b",
        r"wc\s+-l\b",
    )
    return any(
        re.search(rf"(?:^|[;&|()\n])\s*{pattern}(?:\s|$)", text)
        for pattern in weak_command_patterns
    )


def _command_source_read_targets(command: str) -> List[str]:
    """Extract likely source-file reads from shell or inline Node commands."""

    raw = str(command or "")
    targets: List[str] = []

    for match in re.finditer(
        r"\b(?:readFileSync|existsSync|statSync|lstatSync)\(\s*['\"]([^'\"]+)['\"]",
        raw,
    ):
        targets.append(match.group(1))

    try:
        tokens = shlex.split(raw, posix=True)
    except ValueError:
        tokens = raw.split()

    if tokens:
        command_name = Path(tokens[0]).name
        if command_name in {"cat", "head", "tail", "less"}:
            for token in tokens[1:]:
                if token in {"|", "||", "&&", ";"}:
                    break
                if token.startswith("-") or token.startswith((">", "2>")):
                    continue
                targets.append(token)
        elif command_name in {"ls", "find"}:
            for token in tokens[1:]:
                if token in {"|", "||", "&&", ";"}:
                    break
                if token.startswith("-") or token.startswith((">", "2>")):
                    continue
                if token == ".":
                    continue
                targets.append(token)
        elif command_name in {"test", "["}:
            for index, token in enumerate(tokens[1:], start=1):
                if token in {"|", "||", "&&", ";", "]"}:
                    break
                if token in {"-f", "-d", "-e", "-s"} and index + 1 < len(tokens):
                    targets.append(tokens[index + 1])
        elif command_name in {"node", "python", "python3"} and len(tokens) > 1:
            script = tokens[1]
            if script not in {"-e", "-c"}:
                targets.append(script)

    filtered: List[str] = []
    seen: set[str] = set()
    for target in targets:
        path_text = str(target or "").strip()
        if (
            not path_text
            or path_text in {".", ".."}
            or path_text.startswith(("-", "$", "http://", "https://"))
            or any(char in path_text for char in "*?[]{}")
        ):
            continue
        path = Path(path_text)
        if path.suffix.lower() not in SOURCE_EXTENSIONS and not (
            path_text.endswith("/")
            or "/" in path_text
            or path_text in {"app", "src", "spec", "test", "tests"}
        ):
            continue
        if path_text not in seen:
            seen.add(path_text)
            filtered.append(path_text)
    return filtered


def _verification_plan_missing_workspace_files(
    plan: List[Dict[str, Any]],
    project_dir: Optional[Path],
    *,
    include_expected_files: bool = True,
) -> List[str]:
    """Return expected source files in verification plans that do not exist yet."""

    if not project_dir or not project_dir.exists():
        return []

    project_root = Path(project_dir)
    known_paths = {
        path.relative_to(project_root).as_posix()
        for path in project_root.rglob("*")
        if path.is_file()
    }
    missing: List[str] = []
    seen: set[str] = set()
    for step in plan:
        for raw_operation in step.get("ops", []) or []:
            if not isinstance(raw_operation, dict):
                continue
            operation = normalize_file_op_shape(raw_operation)
            op_name = str(operation.get("op") or "")
            raw_path = str(operation.get("path") or "")
            if not raw_path.strip():
                continue
            try:
                relative_path = normalize_path_reference(raw_path, project_root)
            except TaskWorkspaceViolationError:
                continue
            if relative_path == ".":
                continue
            if op_name in {"write_file", "append_file"}:
                known_paths.add(relative_path)
            elif op_name == "delete_file":
                known_paths.discard(relative_path)

        step_source_paths: List[str] = []
        if include_expected_files:
            step_source_paths.extend(
                str(path or "")
                for path in step.get("expected_files", []) or []
                if str(path or "").strip()
            )
        for command in step.get("commands", []) or []:
            step_source_paths.extend(_command_source_read_targets(str(command or "")))
        verification = str(step.get("verification") or "")
        if verification:
            step_source_paths.extend(_command_source_read_targets(verification))

        for path_text in step_source_paths:
            try:
                relative_path = normalize_path_reference(path_text, project_root)
            except TaskWorkspaceViolationError:
                relative_path = path_text
            if relative_path in known_paths:
                continue
            candidate = (project_root / relative_path).resolve()
            if candidate.exists():
                known_paths.add(relative_path)
                continue
            if relative_path in seen:
                continue
            seen.add(relative_path)
            missing.append(relative_path)
    return [
        path
        for path in missing
        if not any(
            other != path and other.startswith(f"{path.rstrip('/')}/")
            for other in missing
        )
    ]


def _verification_plan_creates_new_source_assets(
    plan: List[Dict[str, Any]], project_dir: Optional[Path]
) -> List[str]:
    """Return app/source assets a verification plan tries to create from scratch."""

    if not project_dir or not project_dir.exists():
        return []

    blocked_extensions = {
        ".css",
        ".html",
        ".jsx",
        ".py",
        ".scss",
        ".svg",
        ".ts",
        ".tsx",
    }
    project_root = Path(project_dir)
    created: List[str] = []
    seen: set[str] = set()
    for step in plan:
        for raw_operation in step.get("ops", []) or []:
            if not isinstance(raw_operation, dict):
                continue
            operation = normalize_file_op_shape(raw_operation)
            op_name = str(operation.get("op") or "")
            if op_name not in {"write_file", "append_file"}:
                continue
            raw_path = str(operation.get("path") or "")
            if not raw_path.strip():
                continue
            try:
                relative_path = normalize_path_reference(raw_path, project_root)
            except TaskWorkspaceViolationError:
                continue
            path = Path(relative_path)
            if path.suffix.lower() not in blocked_extensions:
                continue
            if path.name.lower().startswith(("verify", "check")):
                continue
            if path.parts and path.parts[0] in {"test", "tests", "spec"}:
                continue
            if (project_root / relative_path).exists():
                continue
            if relative_path not in seen:
                seen.add(relative_path)
                created.append(relative_path)
    return created


def _verification_plan_mutates_app_source_assets(
    plan: List[Dict[str, Any]], project_dir: Optional[Path]
) -> List[str]:
    """Return app/source assets mutated by a verification-only plan."""

    if not project_dir or not project_dir.exists():
        return []

    blocked_extensions = {
        ".css",
        ".html",
        ".jsx",
        ".py",
        ".scss",
        ".svg",
        ".ts",
        ".tsx",
    }
    project_root = Path(project_dir)
    mutated: List[str] = []
    seen: set[str] = set()
    for step in plan:
        for raw_operation in step.get("ops", []) or []:
            if not isinstance(raw_operation, dict):
                continue
            operation = normalize_file_op_shape(raw_operation)
            op_name = str(operation.get("op") or "")
            if op_name not in {"write_file", "append_file", "replace_in_file"}:
                continue
            raw_path = str(operation.get("path") or "")
            if not raw_path.strip():
                continue
            try:
                relative_path = normalize_path_reference(raw_path, project_root)
            except TaskWorkspaceViolationError:
                continue
            path = Path(relative_path)
            if path.suffix.lower() not in blocked_extensions:
                continue
            if path.name.lower().startswith(("verify", "check")):
                continue
            if path.parts and path.parts[0] in {"test", "tests", "spec"}:
                continue
            if relative_path not in seen:
                seen.add(relative_path)
                mutated.append(relative_path)
    return mutated


def _plan_missing_verification_steps(plan: List[Dict[str, Any]]) -> List[int]:
    from ..validator import ValidatorService

    missing_steps: List[int] = []
    for index, step in enumerate(plan, start=1):
        step_number = step.get("step_number", index)
        if ValidatorService._step_is_readonly_inspection(step):
            continue
        if not str(step.get("verification") or "").strip():
            missing_steps.append(step_number)
    return [step for step in missing_steps if step is not None]
