"""Core-invariant path-safety and workspace-root rules.

Moved from validator.py in Phase 20M (validator rule split). Functions here
cover unsafe expected-file paths, unsafe command paths, task-workspace nesting,
nested project roots, duplicated path roots, static-site path-root resolution,
and negative existing-file checks.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.orchestration.operations.file_ops_contract import (
    normalize_file_op_shape,
)

from ..workspace_checks import NESTED_PROJECT_STRUCTURAL_DIRS, SOURCE_EXTENSIONS


def _plan_contains_unsafe_paths(plan: List[Dict[str, Any]]) -> List[str]:
    invalid_paths: List[str] = []
    for step in plan:
        for path_value in step.get("expected_files", []) or []:
            raw_path = str(path_value or "").strip()
            if not raw_path:
                continue
            candidate = Path(raw_path)
            if candidate.is_absolute() or ".." in candidate.parts:
                invalid_paths.append(raw_path)
    return invalid_paths[:20]


def _plan_contains_unsafe_command_paths(
    plan: List[Dict[str, Any]],
) -> Dict[int, List[str]]:
    """Detect command paths that violate the task-workspace contract."""

    findings: Dict[int, List[str]] = {}
    absolute_path_pattern = re.compile(
        r"^/[A-Za-z0-9._@:+-]+(?:/[A-Za-z0-9._@:+-]+)*/*$"
    )
    allowed_absolute_tokens = {
        "/dev/null",
        "/dev/stdout",
        "/dev/stderr",
        "/dev/stdin",
    }

    for index, step in enumerate(plan, start=1):
        step_number = step.get("step_number", index)
        fragments: List[str] = []
        step_text_parts = [
            str(step.get("verification") or ""),
            str(step.get("rollback") or ""),
        ]
        step_text_parts.extend(
            str(command or "") for command in step.get("commands", []) or []
        )

        for text in step_text_parts:
            text = _strip_heredoc_bodies_for_command_scanning(text)
            try:
                tokens = shlex.split(text, posix=True)
            except ValueError:
                tokens = []
            for token_index, token in enumerate(tokens):
                previous = tokens[token_index - 1] if token_index >= 1 else ""
                command_name = Path(tokens[0]).name if tokens else ""
                if previous in {"-c", "-e"} and command_name in {
                    "python",
                    "python3",
                    "node",
                }:
                    continue
                if token in allowed_absolute_tokens:
                    continue
                if token.startswith("../") or "/../" in token:
                    if token not in fragments:
                        fragments.append(token)
                    continue
                if absolute_path_pattern.fullmatch(token):
                    if token not in fragments:
                        fragments.append(token)

        if fragments:
            findings[int(step_number)] = fragments[:6]

    return findings


def _strip_heredoc_bodies_for_command_scanning(command: str) -> str:
    """Keep shell syntax visible while hiding heredoc payload text.

    Path-safety checks should inspect the command and heredoc target, not file
    content such as CSS `url('../images/foo.svg')` written by the heredoc.
    """

    lines = str(command or "").splitlines()
    if not lines:
        return ""

    visible: List[str] = []
    delimiter: Optional[str] = None
    heredoc_pattern = re.compile(
        r"<<-?\s*(?:'(?P<single>[A-Za-z_][A-Za-z0-9_]*)'"
        r'|"(?P<double>[A-Za-z_][A-Za-z0-9_]*)"'
        r"|(?P<bare>[A-Za-z_][A-Za-z0-9_]*))"
    )

    for line in lines:
        stripped = line.strip()
        if delimiter is not None:
            if stripped == delimiter:
                delimiter = None
            continue

        visible.append(line)
        match = heredoc_pattern.search(line)
        if match:
            delimiter = (
                match.group("single") or match.group("double") or match.group("bare")
            )

    return "\n".join(visible)


def _plan_nests_task_workspace(
    plan: List[Dict[str, Any]], project_dir: Optional[Path]
) -> List[int]:
    if not project_dir:
        return []
    nested_prefix = f"{project_dir.name}/"
    bad_steps: List[int] = []
    for step in plan:
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
        combined = "\n".join(step_text_parts)
        if nested_prefix in combined:
            bad_steps.append(step.get("step_number"))
    return [step for step in bad_steps if step is not None]


def _plan_creates_nested_project_root(
    plan: List[Dict[str, Any]], project_dir: Optional[Path] = None
) -> List[int]:
    """Detect plans that recreate a whole project under one new top-level folder.

    We only want to flag plans that appear to put the *entire deliverable*
    under a new nested root like ``my-app/...`` inside the current workspace.
    Normal static-site and asset layouts such as ``index.html`` plus
    ``assets/...`` should not be treated as nested-project bugs.
    """

    # Dirs that appear in project_dir path are legitimate prefixes in expected_files
    allowed_from_project = set()
    if project_dir:
        try:
            allowed_from_project = {p for p in project_dir.parts if p and p != "/"}
        except Exception:
            pass

    def looks_like_nested_project_scaffold(root_name: str, paths: List[str]) -> bool:
        root_level_files = [
            path_text for path_text in paths if len(Path(path_text).parts) == 2
        ]
        second_level_dirs = {
            Path(path_text).parts[1]
            for path_text in paths
            if len(Path(path_text).parts) > 2
        }

        if root_level_files:
            return True

        structural_dirs = second_level_dirs.intersection(NESTED_PROJECT_STRUCTURAL_DIRS)
        if len(structural_dirs) >= 2:
            return True

        return False

    read_only_command_heads = {
        "cat",
        "cd",
        "echo",
        "ls",
        "head",
        "tail",
        "grep",
        "find",
        "test",
        "stat",
        "wc",
        "diff",
        "tree",
    }

    def command_is_read_only(command_text: str) -> bool:
        text = command_text.strip()
        if not text:
            return True
        for segment in re.split(r"&&|\|\||;|\|", text):
            stripped_segment = segment.strip()
            if not stripped_segment:
                continue
            try:
                tokens = shlex.split(stripped_segment, posix=True)
            except ValueError:
                return False
            if not tokens:
                continue
            if any(token in {">", ">>", "1>", "2>", "&>"} for token in tokens):
                return False
            if tokens[0] not in read_only_command_heads:
                return False
        return True

    def step_materializes_into(step: Dict[str, Any], root_name: str) -> bool:
        for raw_operation in step.get("ops", []) or []:
            if not isinstance(raw_operation, dict):
                continue
            operation = normalize_file_op_shape(raw_operation)
            raw_path = str(operation.get("path") or "").strip()
            if not raw_path:
                continue
            parts = Path(raw_path).parts
            if parts and parts[0] == root_name:
                return True
        reference_pattern = re.compile(
            rf"(?<![\w@/.-]){re.escape(root_name)}(?![\w@.-])"
        )
        for command in step.get("commands", []) or []:
            text = str(command or "")
            if not reference_pattern.search(text):
                continue
            if not command_is_read_only(text):
                return True
        return False

    bad_steps: List[int] = []
    for step in plan:
        expected_files = [
            str(path or "").strip()
            for path in (step.get("expected_files", []) or [])
            if str(path or "").strip()
        ]
        if len(expected_files) < 3:
            continue

        root_level_files = [
            path_text for path_text in expected_files if len(Path(path_text).parts) == 1
        ]
        top_levels = {
            Path(path_text).parts[0]
            for path_text in expected_files
            if len(Path(path_text).parts) > 1
        }
        suspicious = [
            top
            for top in sorted(top_levels)
            if top not in allowed_from_project and not top.startswith(".")
        ]
        # Only treat this as a nested-project root when the plan appears to
        # put all materialized files under a single new folder and does not
        # also create root-level deliverables like index.html or package.json.
        if len(suspicious) == 1 and not root_level_files:
            nested_root = suspicious[0]
            # An already-existing top-level directory (e.g. the package dir
            # of a Python library workspace) is an in-place target, not a
            # new nested project root.
            if project_dir is not None:
                try:
                    if (Path(project_dir) / nested_root).is_dir():
                        continue
                except Exception:
                    pass
            # expected_files alone is not evidence of scaffold creation;
            # the step must actually materialize into the folder via file
            # ops or non-read-only commands.
            if not step_materializes_into(step, nested_root):
                continue
            nested_root_files = [
                path_text
                for path_text in expected_files
                if Path(path_text).parts[0] == nested_root
            ]
            if not looks_like_nested_project_scaffold(nested_root, nested_root_files):
                continue
            bad_steps.append(step.get("step_number"))
    return [step for step in bad_steps if step is not None]


def _source_path_mentions(*values: Any) -> List[str]:
    """Extract explicit relative source paths from task text."""

    extensions = "|".join(re.escape(ext.lstrip(".")) for ext in SOURCE_EXTENSIONS)
    pattern = re.compile(
        rf"(?<![A-Za-z0-9_.~/-])"
        rf"([A-Za-z0-9_.-]+(?:[/\\][A-Za-z0-9_.-]+)+\.({extensions}))"
        rf"(?![A-Za-z0-9_.-])",
        re.IGNORECASE,
    )
    files: List[str] = []
    seen: set[str] = set()
    for value in values:
        for match in pattern.finditer(str(value or "")):
            path_text = match.group(1).replace("\\", "/").strip().lstrip("./")
            if (
                not path_text
                or path_text.startswith(("/", "../", "~"))
                or "/../" in path_text
            ):
                continue
            if Path(path_text).suffix.lower() not in SOURCE_EXTENSIONS:
                continue
            if path_text not in seen:
                seen.add(path_text)
                files.append(path_text)
    return files


def _resolve_existing_static_site_mentions(
    project_dir: Path,
    file_paths: List[str],
    *context_values: Any,
) -> List[str]:
    context = " ".join(str(value or "") for value in context_values).lower()
    if "public/status-site" not in context:
        return file_paths
    static_root = Path("public/status-site")
    resolved: List[str] = []
    seen: set[str] = set()
    for path_text in file_paths:
        normalized = str(path_text or "").strip().rstrip("/").lstrip("./")
        if not normalized:
            continue
        candidate = Path(normalized)
        if not (project_dir / normalized).exists() and not normalized.startswith(
            f"{static_root.as_posix()}/"
        ):
            scoped = (static_root / candidate).as_posix()
            if (project_dir / scoped).exists():
                normalized = scoped
        if normalized not in seen:
            seen.add(normalized)
            resolved.append(normalized)
    return resolved


def _plan_contains_duplicated_path_roots(
    plan: List[Dict[str, Any]],
) -> Dict[int, List[str]]:
    """Detect repeated root segments like frontend/src/frontend/src in plan text."""

    duplicate_pattern = re.compile(r"\b([A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+)/\1(?:/|$)")
    findings: Dict[int, List[str]] = {}

    for index, step in enumerate(plan, start=1):
        step_number = step.get("step_number", index)
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

        fragments: List[str] = []
        for text in step_text_parts:
            for match in duplicate_pattern.finditer(text):
                fragment = match.group(0).rstrip("/")
                if fragment not in fragments:
                    fragments.append(fragment)
        if fragments:
            findings[int(step_number)] = fragments[:6]

    return findings


def _plan_negative_existing_file_checks(
    plan: List[Dict[str, Any]],
    project_dir: Optional[Path],
) -> Dict[int, List[str]]:
    """Detect negative existence preconditions for files this task creates."""

    if project_dir is None:
        return {}

    expected_targets = {
        str(path or "").strip().lstrip("./")
        for step in plan
        for path in (step.get("expected_files", []) or [])
        if str(path or "").strip()
    }
    for step in plan:
        for operation in step.get("ops", []) or []:
            if not isinstance(operation, dict):
                continue
            if str(operation.get("op") or "").strip() not in {
                "write_file",
                "append_file",
                "replace_in_file",
            }:
                continue
            path_text = str(operation.get("path") or "").strip().lstrip("./")
            if path_text:
                expected_targets.add(path_text)

    findings: Dict[int, List[str]] = {}
    negative_patterns = (
        re.compile(r"\btest\s+!\s+-[efs]\s+(?P<path>[^\s;&|]+)"),
        re.compile(r"\[\s+!\s+-[efs]\s+(?P<path>[^\]\s;&|]+)\s+\]"),
    )
    for index, step in enumerate(plan, start=1):
        step_number = int(step.get("step_number", index))
        commands = [str(command or "") for command in step.get("commands", []) or []]
        if step.get("verification"):
            commands.append(str(step.get("verification") or ""))
        for command in commands:
            for pattern in negative_patterns:
                for match in pattern.finditer(command):
                    path_text = match.group("path").strip().strip("'\"").lstrip("./")
                    if path_text not in expected_targets:
                        continue
                    if (Path(project_dir) / path_text).exists():
                        findings.setdefault(step_number, []).append(path_text)

    return {step: sorted(set(paths)) for step, paths in findings.items()}
