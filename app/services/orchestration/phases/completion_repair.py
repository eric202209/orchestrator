"""Completion repair helpers for the task completion phase."""

from __future__ import annotations

import json
import os
import re
import shlex
from pathlib import Path
from typing import Any, Dict, Optional

from app.services.orchestration.context.assembly import (
    collect_workspace_inventory_paths,
)
from app.services.orchestration.types import FailureEnvelope
from app.services.orchestration.phases.completion_repair_capsule import (
    completion_repair_finding_signature,
)
from app.services.orchestration.validation.validator import ValidatorService
from app.services.orchestration.validation.workspace_guard import (
    TaskWorkspaceViolationError,
    normalize_path_reference,
)
from app.services.workspace.path_display import render_workspace_path_for_prompt
from app.services.workspace.permissions import ensure_shared_permissions

RELATIVE_PATH_TOKEN_RE = re.compile(
    r"(?<![\w./-])("
    r"(?:src|tests|fixtures|scripts|config)/[A-Za-z0-9_./-]+"
    r"|(?:package\.json|tsconfig\.json|vitest\.config\.ts|jest\.config\.js|README\.md|\.env\.example)"
    r")(?![\w./-])"
)


def _build_completion_repair_workspace_summary(
    *,
    project_dir: Path,
    completion_validation: Any,
    max_files: int = 80,
) -> str:
    expected_files = (
        list((completion_validation.details or {}).get("expected_core_files", []) or [])
        if completion_validation
        else []
    )
    existing_files = [
        path
        for path in collect_workspace_inventory_paths(project_dir, max_files=max_files)
        if Path(path).suffix.lower() in {".ts", ".tsx", ".js", ".jsx", ".json", ".sh"}
    ][:max_files]

    similar_map: dict[str, list[str]] = {}
    lowered_existing = [(item, item.lower()) for item in existing_files]
    for expected in expected_files[:20]:
        expected_name = Path(expected).stem.lower().replace("-", "_")
        expected_parent = str(Path(expected).parent).lower()
        matches: list[str] = []
        for existing, lowered in lowered_existing:
            existing_name = Path(existing).stem.lower().replace("-", "_")
            if expected_name == existing_name:
                matches.append(existing)
                continue
            if (
                expected_parent
                and expected_parent in lowered
                and any(
                    token and token in existing_name
                    for token in expected_name.split("_")
                )
            ):
                matches.append(existing)
        if matches:
            similar_map[expected] = matches[:5]

    lines = [
        "Current workspace inventory:",
        *[f"- {path}" for path in existing_files[:max_files]],
    ]
    if expected_files:
        lines.append("Expected core files from the current accepted plan:")
        lines.extend(f"- {path}" for path in expected_files[:20])
    if similar_map:
        lines.append(
            "Existing files that look structurally similar to missing expected files:"
        )
        for expected, matches in similar_map.items():
            lines.append(f"- {expected} -> {', '.join(matches)}")
    return "\n".join(lines)


def _extract_relative_paths_from_text(raw_text: str) -> set[str]:
    text = str(raw_text or "")
    return {match.group(1).strip() for match in RELATIVE_PATH_TOKEN_RE.finditer(text)}


def _collect_created_paths_from_commands(
    commands: list[str],
) -> tuple[set[str], set[str]]:
    created_files: set[str] = set()
    created_dirs: set[str] = set()
    for command in commands:
        text = str(command or "").strip()
        if not text:
            continue
        for match in re.finditer(r"(?:cat|tee)\s*>\s*([A-Za-z0-9_./-]+)", text):
            created_files.add(match.group(1).strip())
        if text.startswith("touch "):
            for token in text.split()[1:]:
                cleaned = token.strip()
                if cleaned and ("/" in cleaned or "." in cleaned):
                    created_files.add(cleaned)
        if text.startswith("mkdir -p "):
            for token in text.split()[2:]:
                cleaned = token.strip()
                if cleaned:
                    created_dirs.add(cleaned.rstrip("/"))
        if text.startswith("mv "):
            parts = text.split()
            if len(parts) >= 3:
                created_files.add(parts[-1].strip())
    return created_files, created_dirs


def _completion_repair_invalid_paths(
    *,
    repair_step: Dict[str, Any],
    project_dir: Path,
    completion_validation: Any,
) -> list[str]:
    authorized_paths = set(
        list(
            (completion_validation.details or {}).get("candidate_authorized_paths", [])
            or []
        )
    )
    invalid: list[str] = []
    for operation in repair_step.get("ops") or []:
        raw_path = (
            str(operation.get("path") or "") if isinstance(operation, dict) else ""
        )
        try:
            path = normalize_path_reference(raw_path, project_dir.resolve())
        except TaskWorkspaceViolationError:
            invalid.append(raw_path or "<missing>")
            continue
        if path not in authorized_paths:
            invalid.append(path)
    return invalid


def _extract_reported_changed_files(output_text: str, project_dir: Path) -> list[str]:
    reported: list[str] = []
    text = str(output_text or "")
    patterns = [
        r"`([^`]+)`",
        r"[-*]\s+([A-Za-z0-9_./-]+\.(?:ts|tsx|js|jsx|json|sh))",
        r"(src/[A-Za-z0-9_./-]+\.(?:ts|tsx|js|jsx))",
        r"(tests/[A-Za-z0-9_./-]+\.(?:ts|tsx|js|jsx))",
        r"(vitest\.config\.ts|jest\.config\.js|package\.json|tsconfig\.json|\.env\.example)",
    ]
    seen: set[str] = set()
    for pattern in patterns:
        for match in re.finditer(pattern, text):
            candidate = match.group(1).strip()
            if not candidate or candidate in seen:
                continue
            resolved = (project_dir / candidate).resolve()
            if resolved.exists() and resolved.is_file():
                seen.add(candidate)
                reported.append(candidate)
    return reported


def _apply_completion_repair_ops_direct(
    ops: list[Dict[str, Any]],
    project_dir: Path,
    *,
    authorized_paths: Optional[set[str]] = None,
) -> Dict[str, Any]:
    """Validate and atomically apply one candidate-authorized repair batch."""

    errors: list[str] = []
    project_root = project_dir.resolve()
    normalized_authority = None
    if authorized_paths is not None:
        normalized_authority = set()
        for path in authorized_paths:
            try:
                normalized_authority.add(
                    normalize_path_reference(str(path), project_root)
                )
            except TaskWorkspaceViolationError as exc:
                errors.append(f"invalid candidate-authorized path {path}: {exc}")

    prepared: list[tuple[str, str, Path, Dict[str, Any]]] = []
    seen_paths: set[str] = set()
    for op in ops:
        if not isinstance(op, dict):
            errors.append(f"repair operation must be an object: {op!r}")
            continue
        op_name = str(op.get("op") or "").strip()
        raw_path = str(op.get("path") or "").strip().replace("\\", "/")
        if not raw_path or not op_name:
            errors.append(f"op missing 'op' or 'path': {op}")
            continue
        if Path(raw_path).is_absolute() or raw_path.startswith("~"):
            errors.append(f"absolute/home path not allowed: {raw_path}")
            continue
        try:
            path_str = normalize_path_reference(raw_path, project_root)
        except TaskWorkspaceViolationError as exc:
            errors.append(str(exc))
            continue
        if path_str == ".":
            errors.append(f"path resolves to workspace root: {raw_path}")
            continue
        if normalized_authority is not None and path_str not in normalized_authority:
            errors.append(f"path is not candidate-authorized: {path_str}")
            continue
        if path_str in seen_paths:
            errors.append(f"duplicate/conflicting repair operation: {path_str}")
            continue
        seen_paths.add(path_str)
        abs_path = project_root / path_str
        if op_name not in _COMPLETION_REPAIR_OPS:
            errors.append(f"unsupported op: {op_name}")
            continue
        if op_name in {"write_file", "append_file"} and not isinstance(
            op.get("content"), str
        ):
            errors.append(f"{op_name}: content must be a string for {path_str}")
            continue
        if op_name == "replace_in_file":
            old = op.get("old")
            new = op.get("new")
            if not isinstance(old, str) or not old:
                errors.append(f"replace_in_file: 'old' is empty for {path_str}")
                continue
            if not isinstance(new, str):
                errors.append(f"replace_in_file: 'new' must be a string for {path_str}")
                continue
            if not abs_path.is_file():
                errors.append(f"replace_in_file: file not found: {path_str}")
                continue
            try:
                text = abs_path.read_text(encoding="utf-8")
            except (OSError, UnicodeError) as exc:
                errors.append(f"replace_in_file: cannot read {path_str}: {exc}")
                continue
            if old not in text:
                old_preview = old[:200].replace("\n", "\\n") if old else ""
                errors.append(
                    f"replace_in_file: 'old' text not found in {path_str}"
                    + (f" (attempted: {old_preview!r})" if old_preview else "")
                )
                continue
        prepared.append((op_name, path_str, abs_path, op))

    if errors:
        return {"applied": [], "errors": errors, "success": False, "rolled_back": False}

    snapshots = {
        path_str: (
            abs_path.read_bytes() if abs_path.exists() else None,
            abs_path.stat().st_mode if abs_path.exists() else None,
        )
        for _op_name, path_str, abs_path, _op in prepared
    }
    applied: list[str] = []
    try:
        for op_name, path_str, abs_path, op in prepared:
            abs_path.parent.mkdir(parents=True, exist_ok=True)
            if op_name == "write_file":
                abs_path.write_text(op["content"], encoding="utf-8")
            elif op_name == "append_file":
                existing = (
                    abs_path.read_text(encoding="utf-8") if abs_path.exists() else ""
                )
                abs_path.write_text(existing + op["content"], encoding="utf-8")
            else:
                text = abs_path.read_text(encoding="utf-8")
                abs_path.write_text(
                    text.replace(op["old"], op["new"], 1), encoding="utf-8"
                )
            try:
                ensure_shared_permissions(abs_path)
            except Exception:
                pass
            applied.append(path_str)
    except Exception as exc:
        errors.append(f"repair transaction failed: {type(exc).__name__}: {exc}")
        for _op_name, path_str, abs_path, _op in reversed(prepared):
            prior_bytes, prior_mode = snapshots[path_str]
            if prior_bytes is None:
                if abs_path.exists():
                    abs_path.unlink()
            else:
                abs_path.parent.mkdir(parents=True, exist_ok=True)
                abs_path.write_bytes(prior_bytes)
                if prior_mode is not None:
                    os.chmod(abs_path, prior_mode)
        return {"applied": [], "errors": errors, "success": False, "rolled_back": True}

    return {"applied": applied, "errors": [], "success": True, "rolled_back": False}


def _build_completion_repair_prompt(
    *,
    task_prompt: str,
    completion_validation: Any,
    project_dir: Any,
    prior_results_summary: str,
    project_context: str,
    next_step_number: int,
    workspace_inventory: str,
    failure_envelope: Optional[FailureEnvelope] = None,
) -> str:
    prompt_project_dir = render_workspace_path_for_prompt(project_dir)
    failure_block = (
        "\n\nNormalized execution error:\n"
        + failure_envelope.to_prompt_block(max_chars=1800)
        if failure_envelope is not None
        else ""
    )
    return f"""Return one minimal JSON repair envelope to fix completion validation issues. Output JSON object only.

Task:
{task_prompt[:2000]}

Working directory:
{prompt_project_dir}

Completion validation issues:
{json.dumps(completion_validation.reasons[:10], indent=2)}

Prior completed results:
{prior_results_summary[:2000]}

{failure_block}

Project context:
{project_context[:2500]}

Current workspace inventory:
{workspace_inventory[:5000]}

Rules:
1. Return exactly {{"repair_step": {{...}}}} with one executable repair step.
2. Inside repair_step, set repair_type to "ops_fix" and step_number to {next_step_number}.
3. ops must be a non-empty JSON array of structured file operations.
4. Each op must have "op" (write_file, append_file, or replace_in_file), "path" (relative), and op-specific fields: "content" for write_file/append_file; "old" and "new" for replace_in_file.
5. verification must be one non-empty top-level shell command string. Do not use shell metacharacters.
6. Do not use a "commands" key. Use ops only.
7. Prefer replace_in_file for targeted edits; use write_file only to create or fully overwrite a file.
8. Use relative paths only. No absolute paths, "..", or "~".
9. Do not create documentation files unless explicitly required.
10. Use the workspace inventory as the source of truth; only touch files that appear there or that ops create.
11. expected_files must list the file paths this repair writes or modifies.

Output example:
{{
  "step_number": {next_step_number},
  "repair_type": "ops_fix",
  "description": "Fix format_summary signature to match pre-run contract",
  "ops": [
    {{
      "op": "replace_in_file",
      "path": "src/medium_cli/formatting.py",
      "old": "def format_summary(*, total: int = 0, completed: int = 0) -> str:",
      "new": "def format_summary(total: int, completed: int) -> str:"
    }}
  ],
  "verification": "python -m pytest -q",
  "expected_files": ["src/medium_cli/formatting.py"]
}}
"""


def _normalize_completion_repair_step(
    raw_step: Dict[str, Any], next_step_number: int
) -> Dict[str, Any]:
    commands = raw_step.get("commands", [])
    if isinstance(commands, str):
        commands = [commands]
    if not isinstance(commands, list):
        commands = []

    expected_files = raw_step.get("expected_files", [])
    if isinstance(expected_files, str):
        expected_files = [expected_files]
    if not isinstance(expected_files, list):
        expected_files = []

    # verification_command is the ops-fix schema alias for verification.
    verification = raw_step.get("verification") or raw_step.get("verification_command")

    normalized = {
        "step_number": raw_step.get("step_number") or next_step_number,
        "description": str(
            raw_step.get("description") or "Apply minimal completion repair"
        ),
        "commands": [
            str(command).strip() for command in commands if str(command).strip()
        ],
        "verification": verification,
        "rollback": raw_step.get("rollback"),
        "expected_files": [
            str(path).strip() for path in expected_files if str(path).strip()
        ],
    }
    if isinstance(raw_step.get("ops"), list):
        normalized["ops"] = raw_step["ops"]
        # Derive expected_files from ops paths when not explicitly supplied.
        if not normalized["expected_files"]:
            normalized["expected_files"] = [
                str(op.get("path") or "").strip().lstrip("./")
                for op in raw_step["ops"]
                if isinstance(op, dict) and str(op.get("path") or "").strip()
            ]
    return normalized


_COMPLETION_REPAIR_WRAPPER_KEYS = (
    "repair_step",
    "step",
    "completion_repair_step",
    "payload",
    "result",
)
_COMPLETION_REPAIR_OPS = {"write_file", "append_file", "replace_in_file"}


def _valid_completion_repair_path(value: Any) -> bool:
    path = str(value or "").strip().replace("\\", "/")
    return bool(
        path and not path.startswith(("/", "~")) and ".." not in Path(path).parts
    )


def _valid_completion_repair_ops(value: Any) -> bool:
    if not isinstance(value, list) or not value:
        return False
    for operation in value:
        if not isinstance(operation, dict):
            return False
        operation_name = operation.get("op")
        path = operation.get("path")
        if (
            operation_name not in _COMPLETION_REPAIR_OPS
            or not _valid_completion_repair_path(path)
        ):
            return False
        if operation_name in {"write_file", "append_file"}:
            if not isinstance(operation.get("content"), str):
                return False
        elif not all(isinstance(operation.get(field), str) for field in ("old", "new")):
            return False
    return True


def _valid_completion_repair_step(raw_step: Any) -> bool:
    """Validate the producer contract before permissive legacy normalization."""

    if not isinstance(raw_step, dict):
        return False
    description = raw_step.get("description")
    if not isinstance(description, str) or not description.strip():
        return False

    valid_ops = _valid_completion_repair_ops(raw_step.get("ops"))
    if raw_step.get("commands") or not valid_ops:
        return False

    verification = raw_step.get("verification", raw_step.get("verification_command"))
    if not isinstance(verification, str) or not verification.strip():
        return False

    expected_files = raw_step.get("expected_files")
    if expected_files is not None and (
        not isinstance(expected_files, list)
        or not all(_valid_completion_repair_path(path) for path in expected_files)
    ):
        return False
    rollback = raw_step.get("rollback")
    return rollback is None or isinstance(rollback, str)


def _canonicalize_completion_repair_envelope(
    parsed_data: Any, next_step_number: int
) -> Optional[Dict[str, Any]]:
    """Return one canonical envelope, accepting only bounded legacy shapes."""

    if not isinstance(parsed_data, dict):
        return None

    candidates: list[Dict[str, Any]] = []
    if "repair_step" in parsed_data:
        if set(parsed_data) != {"repair_step"}:
            return None
        candidate = parsed_data.get("repair_step")
        if isinstance(candidate, dict):
            candidates.append(candidate)
    else:
        direct_keys = {
            "step_number",
            "description",
            "commands",
            "verification",
            "verification_command",
            "rollback",
            "expected_files",
            "ops",
            "repair_type",
        }
        if direct_keys.intersection(parsed_data):
            candidates.append(parsed_data)
        else:
            wrapper_keys = [
                key for key in _COMPLETION_REPAIR_WRAPPER_KEYS[1:] if key in parsed_data
            ]
            if len(wrapper_keys) == 1 and isinstance(
                parsed_data.get(wrapper_keys[0]), dict
            ):
                candidates.append(parsed_data[wrapper_keys[0]])

    if len(candidates) != 1 or not _valid_completion_repair_step(candidates[0]):
        return None
    return {
        "repair_step": _normalize_completion_repair_step(
            candidates[0], next_step_number
        )
    }


def _salvage_completion_repair_json_text(text: str) -> str:
    """Close one unterminated commands array before a verification field."""

    raw = str(text or "")
    stripped = raw.strip()
    if not stripped:
        return raw
    try:
        json.loads(stripped)
        return raw
    except json.JSONDecodeError:
        pass

    stack: list[str] = []
    commands_array_open = False
    misplaced_verification_positions: list[tuple[int, int]] = []
    top_level_verification_count = 0
    index = 0

    while index < len(stripped):
        char = stripped[index]
        if char == '"':
            end = index + 1
            escaped = False
            while end < len(stripped):
                current = stripped[end]
                if escaped:
                    escaped = False
                elif current == "\\":
                    escaped = True
                elif current == '"':
                    break
                end += 1
            if end >= len(stripped):
                return raw
            try:
                token = json.loads(stripped[index : end + 1])
            except json.JSONDecodeError:
                return raw

            after = end + 1
            while after < len(stripped) and stripped[after].isspace():
                after += 1
            is_key = after < len(stripped) and stripped[after] == ":"
            if is_key and token == "commands" and stack == ["{"]:
                value_start = after + 1
                while value_start < len(stripped) and stripped[value_start].isspace():
                    value_start += 1
                commands_array_open = (
                    value_start < len(stripped) and stripped[value_start] == "["
                )
            elif is_key and token == "verification":
                if stack == ["{"]:
                    top_level_verification_count += 1
                elif stack == ["{", "["] and commands_array_open:
                    previous = index - 1
                    while previous >= 0 and stripped[previous].isspace():
                        previous -= 1
                    if previous >= 0 and stripped[previous] == ",":
                        misplaced_verification_positions.append((previous, index))
            index = end + 1
            continue

        if char in "{[":
            stack.append(char)
        elif char == "}":
            if not stack:
                return raw
            if stack[-1] == "{":
                stack.pop()
            elif not (
                stack == ["{", "["]
                and commands_array_open
                and misplaced_verification_positions
            ):
                return raw
        elif char == "]":
            if not stack or stack[-1] != "[":
                return raw
            stack.pop()
            if stack == ["{"]:
                commands_array_open = False
        index += 1

    if top_level_verification_count or len(misplaced_verification_positions) != 1:
        return raw

    comma_position, key_position = misplaced_verification_positions[0]
    corrected = stripped[:comma_position] + "]," + stripped[key_position:]

    class _ObjectPairs(list):
        pass

    try:
        parsed = json.loads(
            corrected,
            object_pairs_hook=lambda pairs: _ObjectPairs(pairs),
        )
    except json.JSONDecodeError:
        return raw
    if not isinstance(parsed, _ObjectPairs):
        return raw

    commands_values = [value for key, value in parsed if key == "commands"]
    verification_values = [value for key, value in parsed if key == "verification"]
    expected_files_values = [value for key, value in parsed if key == "expected_files"]
    keys = {key for key, _value in parsed}
    if not {
        "description",
        "commands",
        "verification",
        "expected_files",
    }.issubset(keys):
        return raw
    if (
        len(commands_values) != 1
        or len(verification_values) != 1
        or len(expected_files_values) != 1
    ):
        return raw

    commands = commands_values[0]
    verification = verification_values[0]
    expected_files = expected_files_values[0]
    if (
        not isinstance(commands, list)
        or isinstance(commands, _ObjectPairs)
        or not commands
        or not all(isinstance(command, str) and command.strip() for command in commands)
        or not isinstance(verification, str)
        or not verification.strip()
        or not isinstance(expected_files, list)
        or isinstance(expected_files, _ObjectPairs)
        or not expected_files
        or not all(
            isinstance(path, str)
            and path.strip()
            and not path.strip().startswith(("/", "~"))
            and ".." not in Path(path.strip().replace("\\", "/")).parts
            for path in expected_files
        )
    ):
        return raw
    expected_path_set = {path.strip().replace("\\", "/") for path in expected_files}
    created_files, _created_dirs = _collect_created_paths_from_commands(commands)
    if created_files and not created_files.issubset(expected_path_set):
        return raw
    return corrected


def _extract_completion_repair_step(
    parsed_data: Any, next_step_number: int
) -> Optional[Dict[str, Any]]:
    if isinstance(parsed_data, dict):
        step_like_keys = {
            "step_number",
            "description",
            "commands",
            "verification",
            "verification_command",
            "rollback",
            "expected_files",
            "ops",
            "repair_type",
        }
        if step_like_keys.intersection(parsed_data.keys()):
            return _normalize_completion_repair_step(parsed_data, next_step_number)

        for key in (
            "step",
            "repair_step",
            "completion_repair_step",
            "payload",
            "result",
        ):
            candidate = parsed_data.get(key)
            if isinstance(candidate, dict):
                extracted = _extract_completion_repair_step(candidate, next_step_number)
                if extracted:
                    return extracted

    if isinstance(parsed_data, list):
        for item in parsed_data:
            extracted = _extract_completion_repair_step(item, next_step_number)
            if extracted:
                return extracted

    return None


def _completion_failure_signature(completion_validation: Any) -> str:
    typed_signature = completion_repair_finding_signature(completion_validation)
    if typed_signature:
        return typed_signature
    reasons = list(getattr(completion_validation, "reasons", []) or [])
    details = getattr(completion_validation, "details", {}) or {}
    existing = str(details.get("failure_signature") or "").strip()
    if existing:
        return existing
    return ValidatorService.build_failure_signature(reasons)


def _repeats_prior_completion_failure(
    orchestration_state: Any, completion_validation: Any
) -> bool:
    current_signature = _completion_failure_signature(completion_validation)
    if not current_signature:
        return False
    prior_validation = (
        getattr(orchestration_state, "last_completion_validation", {}) or {}
    )
    prior_signature = completion_repair_finding_signature(prior_validation)
    if not prior_signature:
        prior_signature = str(
            (prior_validation.get("details", {}) or {}).get("failure_signature") or ""
        ).strip()
    if not (prior_signature and prior_signature == current_signature):
        return False
    # A PARTIAL_PROGRESS verdict is the newly validated input to the next
    # bounded iteration, not proof that an iteration repeated itself. The
    # coordinator compares that input with the next same-validator result;
    # unchanged findings then become NO_PROGRESS_OR_REGRESSION and stop.
    progress = str(
        (getattr(completion_validation, "details", {}) or {}).get(
            "completion_repair_progress"
        )
        or ""
    )
    return progress != "PARTIAL_PROGRESS"
