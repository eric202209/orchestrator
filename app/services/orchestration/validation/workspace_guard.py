"""Workspace isolation, path normalization, and write-scope audit helpers."""

from __future__ import annotations

import hashlib
import logging
import os
import re
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.orchestration.persistence import record_live_log


class TaskWorkspaceViolationError(ValueError):
    """Raised when a planned command escapes the task workspace."""


_ALLOWED_ABSOLUTE_SINK_PATHS = frozenset(
    {
        "/dev/null",
        "/dev/stdout",
        "/dev/stderr",
        "/dev/stdin",
    }
)
_TRANSIENT_EXPECTED_FILE_PARTS = frozenset(
    {
        ".venv",
        "venv",
        "node_modules",
        "dist",
        "build",
        "__pycache__",
        ".pytest_cache",
    }
)


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
    if raw in _ALLOWED_ABSOLUTE_SINK_PATHS:
        return raw

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


def _looks_like_path_traversal_token(token: str) -> bool:
    """Return True only for shell-token path traversal, not embedded source code."""

    stripped = (token or "").strip().strip("\"'")
    if not stripped:
        return False
    if any(char.isspace() for char in stripped):
        return False
    if any(char in stripped for char in "(){};,`"):
        return False
    if stripped in {"..", "../", "./.."}:
        return True
    return bool(re.fullmatch(r"\.\.(?:/[A-Za-z0-9._@:+-]+)+/?", stripped))


def _repair_workspace_relative_cd_target(path_text: str, project_dir: Path) -> str:
    """Rewrite mistaken `../name` cd targets back into the current workspace.

    This is intentionally narrow:
    - only one-segment paths like `../backend`
    - only when `<project_dir>/<name>` exists
    - only when a real sibling `<project_dir.parent>/<name>` does not exist
    """

    raw = (path_text or "").strip().strip("\"'")
    if not raw.startswith("../"):
        return path_text

    candidate_name = raw[3:].strip("/")
    if not candidate_name or "/" in candidate_name:
        return path_text

    in_workspace_candidate = project_dir / candidate_name
    sibling_candidate = project_dir.parent / candidate_name

    if in_workspace_candidate.exists() and not sibling_candidate.exists():
        return candidate_name

    return path_text


def _normalize_cd_target_for_cwd(
    path_text: str, current_dir: Path, project_dir: Path
) -> str:
    raw = (path_text or "").strip().strip("\"'")
    if not raw:
        raise TaskWorkspaceViolationError("Empty cd target is not allowed")
    if "~" in raw:
        raise TaskWorkspaceViolationError(
            f"Home-directory path is not allowed in task workspace: {raw}"
        )

    repaired = _repair_workspace_relative_cd_target(raw, current_dir)
    candidate = Path(repaired)
    resolved = (
        candidate.resolve()
        if candidate.is_absolute()
        else (current_dir / candidate).resolve()
    )

    if not resolved.is_relative_to(project_dir):
        raise TaskWorkspaceViolationError(
            f"Path escapes task workspace: {raw} -> {resolved}"
        )

    relative = os.path.relpath(resolved, project_dir)
    return "." if relative == "." else relative


def _rewrite_safe_cd_chain(command: str, project_dir: Path) -> str:
    """Rewrite chained `cd` segments into root-relative workspace-safe hops."""

    if "&&" not in command:
        return command

    segments = [segment.strip() for segment in command.split("&&")]
    if len(segments) <= 1:
        return command

    current_dir = project_dir
    rewritten_segments: List[str] = []
    changed = False
    cd_only_pattern = re.compile(r"^cd\s+(.+?)\s*$")

    for raw_segment in segments:
        if not raw_segment:
            continue
        match = cd_only_pattern.match(raw_segment)
        if not match:
            rewritten_segments.append(raw_segment)
            continue
        normalized_target = _normalize_cd_target_for_cwd(
            match.group(1), current_dir, project_dir
        )
        current_dir = (project_dir / normalized_target).resolve()
        if normalized_target == ".":
            changed = True
            continue
        rewritten_segment = f"cd {shlex.quote(normalized_target)}"
        rewritten_segments.append(rewritten_segment)
        if rewritten_segment != raw_segment:
            changed = True

    if not changed:
        return command
    return " && ".join(rewritten_segments)


def _normalize_write_pseudo_command(command: str, project_dir: Path) -> Optional[str]:
    """Normalize `write path: description` pseudo-commands without parsing prose as shell."""

    match = re.match(
        r"^\s*write\s+([^:]+?)\s*:\s*(.+)$", command or "", flags=re.DOTALL
    )
    if not match:
        return None

    target_path = normalize_path_reference(match.group(1), project_dir)
    description = match.group(2).strip()
    if not description:
        raise TaskWorkspaceViolationError(
            f"Write pseudo-command is missing a file description: {command}"
        )

    normalized_target = "." if target_path == "." else target_path
    return f"write {normalized_target}: {description}"


def normalize_command(command: str, project_dir: Path) -> str:
    normalized = (command or "").strip()
    if not normalized:
        raise TaskWorkspaceViolationError("Empty command is not allowed")

    write_pseudo_command = _normalize_write_pseudo_command(normalized, project_dir)
    if write_pseudo_command is not None:
        return write_pseudo_command

    if looks_like_plain_english_instruction(normalized):
        return normalized

    traversal_check_target = strip_heredoc_bodies(normalized)

    if "~" in traversal_check_target:
        raise TaskWorkspaceViolationError(
            f"Home-directory paths are not allowed: {normalized}"
        )

    current = _rewrite_safe_cd_chain(normalized, project_dir)
    cd_pattern = re.compile(r"^\s*cd\s+([^;&|]+?)\s*&&\s*(.+)$")
    while True:
        match = cd_pattern.match(current)
        if not match:
            break
        raw_target = _repair_workspace_relative_cd_target(match.group(1), project_dir)
        target = normalize_path_reference(raw_target, project_dir)
        remainder = match.group(2).strip()
        if target in (".", "./"):
            current = remainder
        else:
            rewritten = f"cd {shlex.quote(target)} && {remainder}"
            if rewritten == current:
                break
            current = rewritten

    abs_path_matches = []
    path_scan_target = strip_heredoc_bodies(current)
    segment_command: Optional[str] = None
    try:
        split_tokens = shlex.split(path_scan_target, posix=True)
    except ValueError as exc:
        raise TaskWorkspaceViolationError(
            f"Command contains malformed shell quoting: {normalized}"
        ) from exc
    for token in split_tokens:
        if token in {"&&", "||", "|", ";"}:
            segment_command = None
            continue

        if segment_command is None:
            segment_command = token

        if _looks_like_path_traversal_token(token):
            raise TaskWorkspaceViolationError(
                f"Parent-directory traversal is not allowed: {normalized}"
            )

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
    try:
        current_tokens = shlex.split(current_traversal_target, posix=True)
    except ValueError as exc:
        raise TaskWorkspaceViolationError(
            f"Command contains malformed shell quoting after normalization: {current}"
        ) from exc
    if "~" in current_traversal_target or any(
        _looks_like_path_traversal_token(token) for token in current_tokens
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
            normalized = "." if normalized == "." else normalized
            if any(
                part in _TRANSIENT_EXPECTED_FILE_PARTS
                for part in Path(normalized).parts
            ):
                step_label = f"step {step_index} " if step_index is not None else ""
                logger_obj.warning(
                    "[ISOLATION] Skipping %sexpected_files transient artifact: %s",
                    step_label,
                    raw_file_path,
                )
                continue
            normalized_files.append(normalized)
        except TaskWorkspaceViolationError as exc:
            step_label = f"step {step_index} " if step_index is not None else ""
            logger_obj.warning(
                "[ISOLATION] Skipping %sexpected_files entry outside workspace: %s (%s)",
                step_label,
                raw_file_path,
                exc,
            )
    return normalized_files


def _normalize_optional_command_field(
    value: Any,
    project_dir: Path,
    step_label: str,
    field_name: str,
) -> Optional[str]:
    if value is None:
        return None

    if isinstance(value, list):
        normalized_parts: List[str] = []
        preserve_root_per_item = field_name == "rollback"
        for item_index, item in enumerate(value, start=1):
            raw_item = str(item).strip()
            if not raw_item:
                continue
            try:
                normalized_item = normalize_command(raw_item, project_dir)
            except TaskWorkspaceViolationError as exc:
                raise TaskWorkspaceViolationError(
                    f"{step_label} {field_name} item {item_index} blocked: {exc}. "
                    f"Offending command: {raw_item}"
                ) from exc
            if raw_item.startswith("cd ") or normalized_item.startswith("cd "):
                preserve_root_per_item = True
            normalized_parts.append(normalized_item)

        if not normalized_parts:
            return None
        if preserve_root_per_item:
            return " && ".join(f"( {item} )" for item in normalized_parts)
        return " && ".join(normalized_parts)

    rendered = str(value).strip()
    return rendered or None


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

    raw_verification = _normalize_optional_command_field(
        step.get("verification"),
        project_dir,
        step_label,
        "verification",
    )
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

    raw_rollback = _normalize_optional_command_field(
        step.get("rollback"),
        project_dir,
        step_label,
        "rollback",
    )
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


_CHECKSUM_IGNORED = frozenset(
    {"node_modules", ".git", "__pycache__", "dist", "build", ".openclaw"}
)


def compute_workspace_checksum(project_dir: Path) -> Dict[str, str]:
    """SHA-256 checksum of every tracked file; used for pre-task audit."""
    checksums: Dict[str, str] = {}
    if not project_dir.exists():
        return checksums
    for path in sorted(project_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(project_dir)
        if any(part in _CHECKSUM_IGNORED for part in relative.parts):
            continue
        try:
            checksums[str(relative)] = hashlib.sha256(path.read_bytes()).hexdigest()
        except OSError:
            pass
    return checksums


def detect_scope_violations(
    project_dir: Path,
    expected_files: List[str],
    pre_checksum: Dict[str, str],
) -> List[str]:
    """Return paths written or modified outside expected_files since pre_checksum.

    Only flags files not declared in the step's expected_files list and either
    newly created or byte-level changed.  Config/lock files are excluded from
    the violation list to avoid noise from package-manager side-effects.
    """
    _NOISE_SUFFIXES = {".lock", ".log"}
    _NOISE_NAMES = {"package-lock.json", "yarn.lock", "pnpm-lock.yaml"}
    allowed = {str(f).lstrip("./") for f in (expected_files or [])}
    post_checksum = compute_workspace_checksum(project_dir)
    violations: List[str] = []
    for rel_path, checksum in post_checksum.items():
        normalized = rel_path.lstrip("./")
        if normalized in allowed:
            continue
        if Path(rel_path).name in _NOISE_NAMES:
            continue
        if Path(rel_path).suffix in _NOISE_SUFFIXES:
            continue
        if rel_path not in pre_checksum or pre_checksum[rel_path] != checksum:
            violations.append(rel_path)
    return sorted(violations)


def summarize_step_changes(
    pre_checksum: Dict[str, str],
    project_dir: Path,
) -> List[str]:
    """CoVe helper: list files created, modified, or deleted since pre_checksum."""
    post_checksum = compute_workspace_checksum(project_dir)
    changed: List[str] = []
    for rel_path, checksum in post_checksum.items():
        if rel_path not in pre_checksum or pre_checksum[rel_path] != checksum:
            changed.append(rel_path)
    for rel_path in pre_checksum:
        if rel_path not in post_checksum:
            changed.append(f"{rel_path} (deleted)")
    return sorted(changed)


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


def verify_workspace_contract(
    *,
    expected_root: Path,
    task_dir: Path,
    expected_task_subfolder: Optional[str] = None,
    allow_project_root_task_dir: bool = False,
    runtime_session_context: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Validate that orchestration and runtime agree on workspace locations."""

    expected_root = Path(expected_root).resolve()
    task_dir = Path(task_dir).resolve()
    resolved_root = expected_root
    reason: Optional[str] = None

    if expected_task_subfolder and not allow_project_root_task_dir:
        expected_task_dir = (expected_root / expected_task_subfolder).resolve()
        if task_dir != expected_task_dir:
            reason = (
                "task workspace does not match locked task subfolder"
                f" ({task_dir} != {expected_task_dir})"
            )
    elif allow_project_root_task_dir:
        if task_dir != expected_root:
            reason = (
                "task workspace must execute in canonical project root"
                f" ({task_dir} != {expected_root})"
            )
    elif not task_dir.is_relative_to(expected_root):
        reason = (
            "task workspace escapes configured project workspace"
            f" ({task_dir} not under {expected_root})"
        )
    elif task_dir != expected_root:
        resolved_root = task_dir.parent

    runtime_session_context = runtime_session_context or {}
    runtime_project_root = runtime_session_context.get("project_workspace_path")
    if (
        runtime_project_root
        and Path(str(runtime_project_root)).resolve() != expected_root
    ):
        reason = (
            "runtime project workspace path disagrees with configured project workspace"
        )

    runtime_task_dir = runtime_session_context.get(
        "task_workspace_path"
    ) or runtime_session_context.get("execution_cwd")
    if runtime_task_dir and Path(str(runtime_task_dir)).resolve() != task_dir:
        reason = (
            "runtime task workspace path disagrees with orchestration task workspace"
        )

    return {
        "ok": reason is None,
        "expected_root": str(expected_root),
        "resolved_root": str(resolved_root),
        "task_dir": str(task_dir),
        "reason": reason,
    }
