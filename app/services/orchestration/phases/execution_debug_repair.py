"""Bounded debug-repair helpers for the execution/debug loop."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path
from typing import Any, Dict, Optional

from app.runtime_naming import (
    BOUNDED_DEBUG_REPAIR_PROMPT_MODE,
    DIFF_SCOPED_DEBUG_REPAIR_PROMPT_MODE,
    bounded_debug_repair_timeout_alias_details,
    debug_prompt_mode_alias_details,
    is_bounded_debug_repair_mode,
    is_diff_scoped_debug_repair_mode,
)
from app.services.orchestration.state.execution_states import OrchestrationPhase


def _is_source_or_test_path(path: Any) -> bool:
    normalized = str(path or "").strip().replace("\\", "/").lstrip("./")
    return normalized.startswith(("src/", "tests/", "test/")) or "/tests/" in normalized


def _is_weak_completion_verifier_failure(envelope: Any) -> bool:
    if envelope is None or getattr(envelope, "failure_class", None) != (
        "completion_validation_failed"
    ):
        return False
    command = str(getattr(envelope, "failed_command", "") or "").strip().lower()
    if not command:
        return False
    if not re.search(r"\b(?:python3?|node)\s+-(?:c|e)\b", command):
        return False
    return bool(
        ("sys.argv" in command or "process.argv" in command)
        and re.search(r"['\"]--[a-z0-9][a-z0-9-]*['\"]", command)
    )


def _command_fix_materially_targets_source_or_tests(command: str) -> bool:
    lowered = str(command or "").strip().lower().replace("\\", "/")
    if not any(marker in lowered for marker in ("src/", "tests/", "test/")):
        return False
    return bool(
        re.search(
            r"\b(?:sed|perl)\b|>>?|write_text|replace\(|open\(|path\(",
            lowered,
        )
    )


def _debug_repair_materially_changes_source_or_tests(
    debug_data: dict[str, Any]
) -> bool:
    fix_type = str((debug_data or {}).get("fix_type") or "").strip()
    if fix_type == "ops_fix":
        return any(
            isinstance(op, dict) and _is_source_or_test_path(op.get("path"))
            for op in (debug_data.get("ops") or [])
        )
    if fix_type == "code_fix":
        return any(
            _is_source_or_test_path(path)
            for path in (debug_data.get("expected_files") or [])
        )
    if fix_type == "command_fix":
        return _command_fix_materially_targets_source_or_tests(
            str(debug_data.get("fix") or "")
        )
    return fix_type == "revise_plan"


def _bounded_debug_repair_source_edit_context(
    step: dict[str, Any], envelope: Any
) -> bool:
    ops = step.get("ops") if isinstance(step, dict) else []
    if isinstance(ops, list) and any(
        isinstance(op, dict)
        and _is_source_or_test_path(op.get("path"))
        and str(op.get("path") or "").replace("\\", "/").lstrip("./").startswith("src/")
        for op in ops
    ):
        return True
    expected_files = step.get("expected_files") if isinstance(step, dict) else []
    if isinstance(expected_files, list) and any(
        str(path or "").replace("\\", "/").lstrip("./").startswith("src/")
        for path in expected_files
    ):
        return True
    changed_files = (
        getattr(envelope, "changed_files", []) if envelope is not None else []
    )
    return any(
        str(path or "").replace("\\", "/").lstrip("./").startswith("src/")
        for path in changed_files
    )


def _bounded_debug_repair_prior_source_paths(
    orchestration_state: Any,
    failed_step_index: int,
) -> list[str]:
    """Return ordered source paths written before the failed execution step."""

    paths: list[str] = []
    seen: set[str] = set()

    def add_path(path: Any) -> None:
        normalized = str(path or "").replace("\\", "/").lstrip("./")
        if (
            normalized.startswith("src/")
            and normalized.endswith(".py")
            and normalized not in seen
        ):
            seen.add(normalized)
            paths.append(normalized)

    for result in getattr(orchestration_state, "execution_results", []) or []:
        step_number = getattr(result, "step_number", 0) or 0
        if step_number <= failed_step_index:
            for path in getattr(result, "files_changed", []) or []:
                add_path(path)

    for prior_step in (getattr(orchestration_state, "plan", []) or [])[
        :failed_step_index
    ]:
        if not isinstance(prior_step, dict):
            continue
        for operation in prior_step.get("ops") or []:
            if isinstance(operation, dict) and operation.get("op") in {
                "write_file",
                "append_file",
                "replace_in_file",
            }:
                add_path(operation.get("path"))
    return paths


def _bounded_debug_repair_prompt_manifest(metadata: dict[str, Any]) -> dict[str, Any]:
    """Select non-sensitive changed-file context fields for prompt observability."""

    return {
        "bounded_execution_debug_repair_changed_file_context_present": bool(
            metadata.get("bounded_execution_debug_repair_changed_file_context_present")
        ),
        "bounded_execution_debug_repair_changed_file_context_paths": list(
            metadata.get("bounded_execution_debug_repair_changed_file_context_paths")
            or []
        ),
        "bounded_execution_debug_repair_changed_file_context_chars": int(
            metadata.get("bounded_execution_debug_repair_changed_file_context_chars")
            or 0
        ),
    }


def _bounded_debug_repair_output_observability(
    repair_output: str,
    debug_data: dict[str, Any],
) -> dict[str, Any]:
    paths: list[str] = []
    for operation in debug_data.get("ops") or []:
        if not isinstance(operation, dict):
            continue
        path = _safe_relative_op_path(operation.get("path"))
        if path and path not in paths:
            paths.append(path)
    return {
        "repair_output_sha256": hashlib.sha256(
            str(repair_output or "").encode("utf-8")
        ).hexdigest(),
        "repair_output_changed_paths": paths,
    }


def _is_low_value_weak_verifier_command_fix(
    envelope: Any, debug_data: dict[str, Any]
) -> bool:
    if not _is_weak_completion_verifier_failure(envelope):
        return False
    if str((debug_data or {}).get("fix_type") or "") != "command_fix":
        return False
    if _debug_repair_materially_changes_source_or_tests(debug_data):
        return False
    command = str((debug_data or {}).get("fix") or "").strip().lower()
    if re.match(r"^echo\s+['\"]?--[a-z0-9][a-z0-9-]*['\"]?", command):
        return True
    verification = str((debug_data or {}).get("verification") or "").strip().lower()
    failed_command = str(getattr(envelope, "failed_command", "") or "").strip().lower()
    return bool(
        ("sys.argv" in failed_command or "process.argv" in failed_command)
        and re.search(r"['\"]--[a-z0-9][a-z0-9-]*['\"]", verification)
    )


def _debug_repair_output_excerpt(value: Any, max_chars: int = 500) -> str:
    text = str(value or "").strip()
    text = re.sub(r"```(?:json|javascript|js|python|bash|sh|shell)?", "", text)
    text = text.replace("```", "").strip()
    text = re.sub(
        r"(?i)(api[_-]?key|access[_-]?token|secret|password|bearer)\s*[:=]\s*"
        r"['\"]?[^'\"\\s,}]+",
        r"\1=<redacted>",
        text,
    )
    if len(text) <= max_chars:
        return text
    return text[: max_chars - 3].rstrip() + "..."


def _safe_relative_op_path(path_value: Any) -> Optional[str]:
    raw_path = str(path_value or "").strip().replace("\\", "/")
    if not raw_path:
        return None
    relative = raw_path.lstrip("./")
    path = Path(relative)
    if path.is_absolute() or ".." in path.parts:
        return None
    return relative


def _bounded_debug_repair_stale_replace_issues(
    ops: Any,
    project_dir: Path,
) -> list[dict[str, Any]]:
    if not isinstance(ops, list):
        return []
    issues: list[dict[str, Any]] = []
    for index, op in enumerate(ops):
        if not isinstance(op, dict):
            continue
        if str(op.get("op") or "").strip() != "replace_in_file":
            continue
        relative = _safe_relative_op_path(op.get("path"))
        old_text = str(op.get("old") or "")
        if not relative:
            issues.append(
                {
                    "index": index,
                    "path": str(op.get("path") or ""),
                    "old": old_text,
                    "reason": "invalid_path",
                    "current_excerpt": "",
                }
            )
            continue
        target = project_dir / relative
        if not target.exists() or not target.is_file():
            issues.append(
                {
                    "index": index,
                    "path": relative,
                    "old": old_text,
                    "reason": "target_missing",
                    "current_excerpt": "",
                }
            )
            continue
        try:
            current_text = target.read_text(encoding="utf-8")
        except UnicodeDecodeError:
            current_text = target.read_text(encoding="utf-8", errors="replace")
        if old_text not in current_text:
            issues.append(
                {
                    "index": index,
                    "path": relative,
                    "old": old_text,
                    "reason": "old_text_not_found",
                    "current_excerpt": current_text[:6000],
                }
            )
    return issues


def _build_bounded_debug_repair_stale_replace_correction_prompt(
    *,
    debug_data: dict[str, Any],
    stale_issues: list[dict[str, Any]],
) -> str:
    return (
        "Return only a bare JSON array containing one source repair object. "
        "No markdown. No prose.\n"
        "The prior Phase 7F ops_fix used stale replace_in_file.old text. "
        "Correct only the stale operation(s); preserve valid source intent.\n"
        "For each failed target, use either:\n"
        "1. replace_in_file with old copied exactly from the current file excerpt; or\n"
        "2. write_file with complete grounded file content preserving imports and public signatures.\n"
        "Do not infer old signatures from tests. Do not use shell commands, cat, sed, heredocs, or python -c to mutate files.\n"
        "Schema example:\n"
        '[{"repair_type":"ops_fix","ops":[{"op":"replace_in_file","path":"src/...","old":"exact current text","new":"replacement"}],"verification_command":"python3 -m pytest -q"}]\n\n'
        "Prior normalized repair object:\n"
        f"{json.dumps(debug_data, indent=2, sort_keys=True)}\n\n"
        "Failed replace_in_file targets with exact current file excerpts:\n"
        f"{json.dumps(stale_issues, indent=2, sort_keys=True)}\n"
    )


def _mark_bounded_debug_repair_timeout_if_applicable(
    debug_error: Exception,
    *,
    debug_prompt_mode: str,
    debug_failure_class: Optional[str],
) -> None:
    diagnostics = dict(getattr(debug_error, "runtime_diagnostics", None) or {})
    is_timeout = bool(diagnostics.get("timed_out")) or (
        "timed out" in str(debug_error).lower() or "timeout" in str(debug_error).lower()
    )
    if (
        is_timeout
        and is_bounded_debug_repair_mode(debug_prompt_mode)
        and debug_failure_class == "source_step_validation"
    ):
        diagnostics.update(
            {
                "failure_phase": OrchestrationPhase.DEBUG_REPAIR,
                **debug_prompt_mode_alias_details(debug_prompt_mode),
                "debug_failure_class": debug_failure_class,
                **bounded_debug_repair_timeout_alias_details(True),
                "timed_out": True,
            }
        )
        setattr(debug_error, "runtime_diagnostics", diagnostics)


def _debug_prompt_mode_architecture(debug_prompt_mode: str) -> Optional[str]:
    if is_bounded_debug_repair_mode(debug_prompt_mode):
        return BOUNDED_DEBUG_REPAIR_PROMPT_MODE
    if is_diff_scoped_debug_repair_mode(debug_prompt_mode):
        return DIFF_SCOPED_DEBUG_REPAIR_PROMPT_MODE
    return None


def _bounded_debug_repair_rejection_alias_details(
    *,
    rejection_reason: Optional[str],
    parsed_shape: Any,
    raw_output_excerpt: str,
) -> Dict[str, Any]:
    return {
        "bounded_execution_debug_repair_rejection_reason": rejection_reason,
        "bounded_execution_debug_repair_parsed_shape": parsed_shape,
        "bounded_execution_debug_repair_raw_output_excerpt": raw_output_excerpt,
    }
