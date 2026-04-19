"""Workspace isolation and path normalization helpers."""

from __future__ import annotations

import logging
import os
import re
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.orchestration.persistence import record_live_log


class TaskWorkspaceViolationError(ValueError):
    """Raised when a planned command escapes the task workspace."""


def strip_heredoc_bodies(command_text: str) -> str:
    """Replace heredoc bodies so shell validation only sees the outer command."""

    return re.sub(
        r"<<\s*['\"]?([A-Za-z0-9_-]+)['\"]?.*?\n.*?\n\1",
        "<<HEREDOC",
        command_text or "",
        flags=re.DOTALL,
    )


def is_quoted_route_literal(
    token: str, original_command: str, segment_command: Optional[str]
) -> bool:
    """Treat quoted grep route patterns like '/refresh' as literals, not paths."""

    if segment_command not in {"grep", "egrep", "fgrep", "rg", "ripgrep"}:
        return False

    if not re.fullmatch(r"/[A-Za-z0-9._:/-]+", token):
        return False

    return f"'{token}'" in original_command or f'"{token}"' in original_command


def normalize_path_reference(path_text: str, project_dir: Path) -> str:
    raw = (path_text or "").strip().strip("\"'")
    if not raw:
        raise TaskWorkspaceViolationError("Empty path reference is not allowed")
    if "~" in raw:
        raise TaskWorkspaceViolationError(
            f"Home-directory path is not allowed in task workspace: {raw}"
        )

    candidate = Path(raw)
    resolved = (
        candidate.resolve()
        if candidate.is_absolute()
        else (project_dir / candidate).resolve()
    )

    if not resolved.is_relative_to(project_dir):
        raise TaskWorkspaceViolationError(
            f"Path escapes task workspace: {raw} -> {resolved}"
        )

    relative = os.path.relpath(resolved, project_dir)
    return "." if relative == "." else relative


def looks_like_plain_english_instruction(command: str) -> bool:
    text = (command or "").strip()
    if not text:
        return False

    if any(symbol in text for symbol in ("&&", "||", "|", ";", "$(", "`", ">", "<")):
        return False

    tokens = text.split()
    if len(tokens) < 3:
        return False

    first = tokens[0]
    if first != first.capitalize():
        return False

    known_shell_starts = {
        "python",
        "python3",
        "node",
        "npm",
        "pnpm",
        "yarn",
        "bash",
        "sh",
        "cd",
        "mkdir",
        "rm",
        "mv",
        "cp",
        "cat",
        "echo",
        "grep",
        "rg",
        "test",
        "curl",
        "wget",
        "git",
        "pytest",
        "uv",
        "make",
        "cargo",
        "go",
        "java",
        "javac",
    }
    if first.lower() in known_shell_starts:
        return False

    return any(
        word.lower() in {"verify", "check", "ensure", "confirm", "validate", "exposes"}
        for word in tokens[:3]
    )


def normalize_command(command: str, project_dir: Path) -> str:
    normalized = (command or "").strip()
    if not normalized:
        raise TaskWorkspaceViolationError("Empty command is not allowed")

    if looks_like_plain_english_instruction(normalized):
        return normalized

    traversal_check_target = strip_heredoc_bodies(normalized)

    if "~" in traversal_check_target:
        raise TaskWorkspaceViolationError(
            f"Home-directory paths are not allowed: {normalized}"
        )

    if re.search(r"(^|[\s'\"=/])\.\.(?:/|$)", traversal_check_target):
        raise TaskWorkspaceViolationError(
            f"Parent-directory traversal is not allowed: {normalized}"
        )

    current = normalized
    cd_pattern = re.compile(r"^\s*cd\s+([^;&|]+?)\s*&&\s*(.+)$")
    while True:
        match = cd_pattern.match(current)
        if not match:
            break
        target = normalize_path_reference(match.group(1), project_dir)
        remainder = match.group(2).strip()
        if target in (".", "./"):
            current = remainder
        else:
            current = f"cd {shlex.quote(target)} && {remainder}"

    abs_path_matches = []
    path_scan_target = strip_heredoc_bodies(current)
    segment_command: Optional[str] = None
    for token in shlex.split(path_scan_target, posix=True):
        if token in {"&&", "||", "|", ";"}:
            segment_command = None
            continue

        if segment_command is None:
            segment_command = token

        if not token.startswith("/"):
            continue
        if any(char in token for char in "<>"):
            continue
        if is_quoted_route_literal(token, current, segment_command):
            continue
        if not re.fullmatch(r"/[A-Za-z0-9._/@:+-]+(?:/[A-Za-z0-9._@:+-]+)*/*", token):
            continue
        abs_path_matches.append(token)

    abs_paths = sorted(set(abs_path_matches), key=len, reverse=True)
    for abs_path in abs_paths:
        replacement = normalize_path_reference(abs_path, project_dir)
        replacement = "." if replacement == "." else f"./{replacement}"
        current = current.replace(abs_path, replacement)

    current_traversal_target = strip_heredoc_bodies(current)
    if "~" in current_traversal_target or re.search(
        r"(^|[\s'\"=/])\.\.(?:/|$)", current_traversal_target
    ):
        raise TaskWorkspaceViolationError(
            f"Command still contains unsafe path traversal: {current}"
        )

    return current


def normalize_expected_files(
    expected_files: Optional[List[str]],
    project_dir: Path,
    logger_obj: logging.Logger,
    step_index: Optional[int] = None,
) -> List[str]:
    normalized_files: List[str] = []
    for file_path in expected_files or []:
        raw_file_path = str(file_path).strip()
        if not raw_file_path:
            continue
        if any(char in raw_file_path for char in "<>"):
            logger_obj.warning(
                "[ISOLATION] Skipping suspicious expected_files entry that looks like markup: %s",
                raw_file_path,
            )
            continue
        try:
            normalized = normalize_path_reference(raw_file_path, project_dir)
            normalized_files.append("." if normalized == "." else normalized)
        except TaskWorkspaceViolationError as exc:
            step_label = f"step {step_index} " if step_index is not None else ""
            logger_obj.warning(
                "[ISOLATION] Skipping %sexpected_files entry outside workspace: %s (%s)",
                step_label,
                raw_file_path,
                exc,
            )
    return normalized_files


def normalize_step(
    step: Dict[str, Any],
    project_dir: Path,
    logger_obj: logging.Logger,
    step_index: Optional[int] = None,
) -> Dict[str, Any]:
    step_label = f"step {step_index}" if step_index is not None else "step"

    normalized_step = dict(step)
    normalized_commands = []
    for command_index, command in enumerate(step.get("commands", []) or [], start=1):
        raw_command = str(command)
        if not raw_command.strip():
            logger_obj.warning(
                "[ISOLATION] Skipping blank command in %s command %s",
                step_label,
                command_index,
            )
            continue
        try:
            normalized_commands.append(normalize_command(raw_command, project_dir))
        except TaskWorkspaceViolationError as exc:
            raise TaskWorkspaceViolationError(
                f"{step_label} command {command_index} blocked: {exc}. "
                f"Offending command: {raw_command}"
            ) from exc
    normalized_step["commands"] = normalized_commands

    raw_verification = str(step.get("verification") or "").strip()
    if raw_verification:
        try:
            normalized_step["verification"] = normalize_command(
                raw_verification, project_dir
            )
        except TaskWorkspaceViolationError as exc:
            raise TaskWorkspaceViolationError(
                f"{step_label} verification blocked: {exc}. "
                f"Offending command: {raw_verification}"
            ) from exc
    else:
        normalized_step["verification"] = None

    raw_rollback = str(step.get("rollback") or "").strip()
    if raw_rollback:
        try:
            normalized_step["rollback"] = normalize_command(raw_rollback, project_dir)
        except TaskWorkspaceViolationError as exc:
            raise TaskWorkspaceViolationError(
                f"{step_label} rollback blocked: {exc}. "
                f"Offending command: {raw_rollback}"
            ) from exc
    else:
        normalized_step["rollback"] = None

    normalized_step["expected_files"] = normalize_expected_files(
        step.get("expected_files", []), project_dir, logger_obj, step_index
    )
    return normalized_step


def normalize_plan(
    plan: List[Dict[str, Any]], project_dir: Path, logger_obj: logging.Logger
) -> List[Dict[str, Any]]:
    normalized_plan: List[Dict[str, Any]] = []
    for index, step in enumerate(plan or [], start=1):
        normalized_step = normalize_step(step, project_dir, logger_obj, index)
        if normalized_step != step:
            logger_obj.info(
                "[ISOLATION] Normalized step %s to stay within task workspace", index
            )
        normalized_plan.append(normalized_step)
    return normalized_plan


def normalize_plan_with_live_logging(
    db: Any,
    session_id: int,
    task_id: int,
    plan: List[Dict[str, Any]],
    project_dir: Path,
    logger_obj: logging.Logger,
    session_instance_id: Optional[str],
    stage: str,
) -> List[Dict[str, Any]]:
    try:
        return normalize_plan(plan, project_dir, logger_obj)
    except TaskWorkspaceViolationError as exc:
        detail = str(exc)
        logger_obj.error("[ISOLATION] %s blocked: %s", stage, detail)
        record_live_log(
            db,
            session_id,
            task_id,
            "ERROR",
            f"[ISOLATION] {stage} blocked: {detail}",
            session_instance_id=session_instance_id,
            metadata={"stage": stage, "project_dir": str(project_dir)},
        )
        record_live_log(
            db,
            session_id,
            task_id,
            "ERROR",
            f"[ORCHESTRATION] Task stopped because a command escaped the task workspace `{project_dir}`",
            session_instance_id=session_instance_id,
            metadata={"stage": stage},
        )
        raise
