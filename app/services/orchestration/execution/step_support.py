"""Step execution and repair support helpers for orchestration."""

from __future__ import annotations

import asyncio
import json
import logging
import re
import shlex
from pathlib import Path
from typing import Any, Callable, Dict, Optional

from sqlalchemy.orm import Session

from app.services.error_handler import error_handler
from app.services.orchestration.context_assembly import render_adapted_runtime_prompt
from app.services.orchestration.types import FailureEnvelope
from app.services.orchestration.file_ops_contract import (
    operation_has_file_op_path,
    render_supported_file_ops,
)
from app.services.orchestration.execution.structured_op_repair import (
    extract_wrapped_assistant_text,
    normalize_replacement_ops,
)
from app.services.workspace.path_display import render_workspace_path_for_prompt

_SHELL_BUILTIN_COMMAND_TOKENS = frozenset(
    {
        "alias",
        "bg",
        "break",
        "cd",
        "command",
        "continue",
        "declare",
        "dirs",
        "echo",
        "eval",
        "exec",
        "exit",
        "export",
        "false",
        "fg",
        "hash",
        "jobs",
        "let",
        "local",
        "popd",
        "printf",
        "pushd",
        "pwd",
        "read",
        "readonly",
        "return",
        "set",
        "shift",
        "source",
        "test",
        "times",
        "trap",
        "true",
        "type",
        "ulimit",
        "umask",
        "unalias",
        "unset",
    }
)
_COMMON_PROJECT_COMMAND_TOKENS = frozenset(
    {
        "bash",
        "bun",
        "cargo",
        "cat",
        "chmod",
        "cp",
        "deno",
        "git",
        "go",
        "grep",
        "make",
        "mkdir",
        "mv",
        "node",
        "npm",
        "npx",
        "pip",
        "pip3",
        "pnpm",
        "poetry",
        "pytest",
        "python",
        "python3",
        "rm",
        "rg",
        "ruff",
        "sed",
        "sh",
        "touch",
        "uv",
        "yarn",
    }
)


def _shell_executable_token(command_text: str) -> Optional[str]:
    try:
        tokens = shlex.split(
            normalize_runnable_shell_command_fix(str(command_text or "")),
            posix=True,
        )
    except ValueError:
        return None

    for token in tokens:
        if not token or "=" in token and token.split("=", 1)[0].isidentifier():
            continue
        return token
    return None


def normalize_runnable_shell_command_fix(command_text: str) -> str:
    command = str(command_text or "").strip()
    command = re.sub(r"^```(?:bash|sh|shell)?\s*", "", command, flags=re.IGNORECASE)
    command = re.sub(r"\s*```$", "", command).strip()
    command = re.sub(
        r"^(?:run|command|fix|execute)\s*:\s*",
        "",
        command,
        count=1,
        flags=re.IGNORECASE,
    ).strip()
    if (
        (command.startswith("`") and command.endswith("`"))
        or (command.startswith('"') and command.endswith('"'))
    ) and len(command) >= 2:
        command = command[1:-1].strip()
    return command


def is_runnable_shell_command_fix(command_text: str) -> bool:
    token = _shell_executable_token(command_text)
    if not token:
        return False
    if "/" in token:
        return token.startswith("./") or token.startswith("scripts/")
    return (
        token in _SHELL_BUILTIN_COMMAND_TOKENS
        or token in _COMMON_PROJECT_COMMAND_TOKENS
    )


def step_needs_command_repair(step: Dict[str, Any]) -> bool:
    commands = step.get("commands", [])
    ops = step.get("ops", [])
    if isinstance(ops, list) and any(
        operation_has_file_op_path(operation) for operation in ops
    ):
        return False
    if not isinstance(commands, list):
        return True
    return not any(str(command or "").strip() for command in commands)


def build_step_repair_prompt(
    task_prompt: str,
    step: Dict[str, Any],
    step_index: int,
    project_dir: Path,
    prior_results_summary: str,
    project_context: str,
    failure_envelope: Optional[FailureEnvelope] = None,
) -> str:
    prompt_project_dir = render_workspace_path_for_prompt(project_dir)
    failure_block = (
        "\n\nNormalized execution error:\n"
        + failure_envelope.to_prompt_block(max_chars=1800)
        if failure_envelope is not None
        else ""
    )
    return f"""Repair this execution step so it becomes machine-runnable JSON. Return JSON object only.

Task:
{task_prompt[:2000]}

Current step index:
{step_index + 1}

Current step JSON:
{json.dumps(step, indent=2)[:4000]}

Project context:
{project_context[:3000]}

Prior completed results:
{prior_results_summary[:2000]}

{failure_block}

Rules:
1. Working directory is {prompt_project_dir}
2. Use relative paths only
3. Do not use .., ~, or absolute paths
4. commands must be a JSON array of runnable shell strings, not prose instructions
5. commands may be empty when ops contains deterministic file operations
6. Optional ops may contain these operation objects: {render_supported_file_ops()}
7. Prefer ops write_file entries for file rewrites, and use other ops for routine deterministic file changes; do not use heredoc rewrites
8. verification and rollback may be null
9. expected_files must be a JSON array
10. Keep the step intent the same
11. Output JSON object only, no prose
12. If expected_files already exist but are empty or stubbed, use ops write_file entries to write real content into those files
13. Use verification stronger than file-existence checks for implementation-heavy steps

Example:
{{
  "step_number": 1,
  "description": "Create a small configuration file",
  "commands": [],
  "ops": [
    {{"op": "write_file", "path": "src/config.js", "content": "export const ready = true;\\n"}}
  ],
  "verification": "node -e \"import('./src/config.js').then(m => {{ if (!m.ready) process.exit(1) }})\"",
  "rollback": null,
  "expected_files": ["src/config.js"]
}}
"""


def repair_step_commands_with_self_correction(
    *,
    runtime_service: Any,
    db: Session,
    session_id: int,
    task_id: int,
    session_instance_id: Optional[str],
    task_prompt: str,
    step: Dict[str, Any],
    step_index: int,
    project_dir: Path,
    prior_results_summary: str,
    project_context: str,
    logger_obj: logging.Logger,
    extract_structured_text: Callable[[Any], str],
    normalize_step: Callable[
        [Dict[str, Any], Path, logging.Logger, int], Dict[str, Any]
    ],
    record_live_log: Callable[..., None],
    failure_envelope: Optional[FailureEnvelope] = None,
) -> Optional[Dict[str, Any]]:
    repair_prompt = build_step_repair_prompt(
        task_prompt=task_prompt,
        step=step,
        step_index=step_index,
        project_dir=project_dir,
        prior_results_summary=prior_results_summary,
        project_context=project_context,
        failure_envelope=failure_envelope,
    )
    repair_prompt = render_adapted_runtime_prompt(
        db,
        objective="Repair a malformed execution step so it becomes machine-runnable.",
        execution_mode="step_repair",
        prompt_body=repair_prompt,
        instructions=[
            "Keep the step intent the same.",
            "Return JSON only.",
        ],
        context={
            "Project Directory": render_workspace_path_for_prompt(project_dir),
            "Step Index": step_index + 1,
        },
        expected_output="JSON object containing the repaired step fields.",
    )
    repair_result = asyncio.run(
        runtime_service.execute_task(repair_prompt, timeout_seconds=120)
    )
    repair_output = extract_structured_text(repair_result.get("output", "{}"))
    success, repair_data, strategy_info = error_handler.attempt_json_parsing(
        repair_output, context="step_repair"
    )
    if not success or not isinstance(repair_data, dict):
        logger_obj.warning(
            "[ORCHESTRATION] Step %s self-correction failed to parse: %s",
            step_index + 1,
            strategy_info,
        )
        record_live_log(
            db,
            session_id,
            task_id,
            "WARN",
            f"[ORCHESTRATION] Step {step_index + 1} self-correction failed: {strategy_info}",
            session_instance_id=session_instance_id,
            metadata={"phase": "step_validation", "strategy": strategy_info},
        )
        return None

    repaired_step = normalize_step(repair_data, project_dir, logger_obj, step_index + 1)
    if step_needs_command_repair(repaired_step):
        record_live_log(
            db,
            session_id,
            task_id,
            "WARN",
            f"[ORCHESTRATION] Step {step_index + 1} self-correction returned no runnable commands",
            session_instance_id=session_instance_id,
            metadata={"phase": "step_validation"},
        )
        return None

    record_live_log(
        db,
        session_id,
        task_id,
        "INFO",
        f"[ORCHESTRATION] Step {step_index + 1} repaired by self-correction",
        session_instance_id=session_instance_id,
        metadata={"phase": "step_validation", "strategy": strategy_info},
    )
    return repaired_step


def coerce_execution_step_result(
    raw_result: Dict[str, Any],
    *,
    expected_files: Optional[list[str]] = None,
    extract_structured_text: Callable[[Any], str],
) -> Dict[str, Any]:
    """Recover a structured step result when the model returned prose instead of JSON."""
    result = dict(raw_result or {})

    if (
        result.get("status") in {"completed", "success"}
        and result.get("files_changed")
        and str(result.get("output") or "").strip().startswith("write_file ")
    ):
        return result

    output_text = extract_structured_text(result.get("output", ""))

    if isinstance(result.get("output"), dict):
        return result

    success, parsed_data, _strategy_info = error_handler.attempt_json_parsing(
        output_text, context="execution"
    )
    if success and isinstance(parsed_data, dict):
        merged = dict(result)
        merged.update(parsed_data)
        return merged

    normalized = (output_text or "").strip()
    lowered = normalized.lower()
    if not normalized:
        return result

    success_markers = (
        "status:** success",
        "status: success",
        "step complete",
        "verification results:",
        "files changed:",
        "dependencies installed:",
    )
    failure_markers = (
        "status:** failed",
        "status: failed",
        "error:",
        "failed:",
    )

    coerced = dict(result)
    if any(marker in lowered for marker in success_markers):
        coerced["status"] = "success"
        coerced["output"] = normalized
        coerced.setdefault("verification_output", normalized[:1000])
        coerced.setdefault("files_changed", list(expected_files or []))
        coerced.setdefault("error", "")
        return coerced

    if any(marker in lowered for marker in failure_markers):
        coerced["status"] = "failed"
        coerced["output"] = normalized
        coerced.setdefault("verification_output", normalized[:1000])
        coerced.setdefault("error", normalized[:1000])
        return coerced

    return result


def coerce_debug_step_result(
    raw_result: Dict[str, Any],
    *,
    error_message: str,
    step: Optional[Dict[str, Any]],
    extract_structured_text: Callable[[Any], str],
) -> tuple[bool, Optional[Dict[str, Any]], str]:
    """Recover a structured debug result when the model returned prose."""
    raw_output = (raw_result or {}).get("output", "")
    raw_output_text = str(raw_output or "")
    raw_stripped_output = raw_output_text.strip()
    if raw_stripped_output.startswith(("{", "[")):
        try:
            raw_parsed_data = json.loads(raw_stripped_output)
            if isinstance(raw_parsed_data, dict):
                wrapped_payload = _extract_wrapped_debug_payload(raw_parsed_data)
                if wrapped_payload is not None:
                    return (
                        True,
                        _finalize_debug_payload(wrapped_payload),
                        "Parsed wrapped assistant debug JSON",
                    )
                return (
                    True,
                    _finalize_debug_payload(raw_parsed_data),
                    "Parsed full debug JSON",
                )
        except ValueError:
            pass

    fenced_payload = _extract_fenced_debug_json(raw_output_text)
    if isinstance(fenced_payload, dict):
        return True, _finalize_debug_payload(fenced_payload), "Parsed fenced debug JSON"

    output_text = extract_structured_text(raw_output)
    fenced_payload = _extract_fenced_debug_json(output_text)
    if isinstance(fenced_payload, dict):
        return True, _finalize_debug_payload(fenced_payload), "Parsed fenced debug JSON"

    stripped_output = output_text.strip()
    success = False
    parsed_data: Any = None
    strategy_info = ""
    if stripped_output.startswith(("{", "[")):
        try:
            parsed_data = json.loads(stripped_output)
            success = True
            strategy_info = "Parsed full debug JSON"
        except ValueError:
            success = False
    if not success:
        success, parsed_data, strategy_info = error_handler.attempt_json_parsing(
            output_text, context="debug"
        )
    if success and isinstance(parsed_data, dict):
        wrapped_payload = _extract_wrapped_debug_payload(parsed_data)
        if wrapped_payload is not None:
            return (
                True,
                _finalize_debug_payload(wrapped_payload),
                "Parsed wrapped assistant debug JSON",
            )
        return True, _finalize_debug_payload(parsed_data), strategy_info

    inferred = _infer_debug_payload_from_text(
        output_text,
        error_message=error_message,
        step=step,
    )
    if inferred:
        return True, inferred, "Inferred structured debug payload from prose"

    return False, None, strategy_info


def _finalize_debug_payload(parsed_data: Dict[str, Any]) -> Dict[str, Any]:
    payload = dict(parsed_data)
    replacement_ops = normalize_replacement_ops(payload)
    if replacement_ops:
        payload["fix_type"] = "ops_fix"
        payload["ops"] = replacement_ops
        return payload
    if payload.get("fix_type") == "command_fix":
        fix_command = normalize_runnable_shell_command_fix(
            str(payload.get("fix") or "")
        )
        if is_runnable_shell_command_fix(fix_command):
            payload["fix"] = fix_command
        else:
            payload["fix_type"] = "code_fix"
    elif payload.get("fix_type") == "code_fix":
        fix_command = normalize_runnable_shell_command_fix(
            str(payload.get("fix") or "")
        )
        if is_runnable_shell_command_fix(fix_command):
            payload["fix_type"] = "command_fix"
            payload["fix"] = fix_command
    return payload


def _extract_wrapped_debug_payload(
    parsed_data: Dict[str, Any],
) -> Optional[Dict[str, Any]]:
    assistant_text = extract_wrapped_assistant_text(parsed_data)
    if not assistant_text:
        return None

    fenced_payload = _extract_fenced_debug_json(assistant_text)
    if isinstance(fenced_payload, dict):
        return fenced_payload

    success, inner_payload, _strategy_info = error_handler.attempt_json_parsing(
        assistant_text, context="debug"
    )
    if success and isinstance(inner_payload, dict):
        return inner_payload
    return None


def _extract_fenced_debug_json(text: str) -> Optional[Dict[str, Any]]:
    for match in re.finditer(
        r"```(?:json)?\s*(\{.*?\})\s*```",
        str(text or ""),
        flags=re.IGNORECASE | re.DOTALL,
    ):
        try:
            parsed = json.loads(match.group(1))
        except ValueError:
            continue
        if isinstance(parsed, dict):
            return parsed
    return None


def _infer_debug_payload_from_text(
    text: str,
    *,
    error_message: str,
    step: Optional[Dict[str, Any]],
) -> Optional[Dict[str, Any]]:
    normalized = (text or "").strip()
    if not normalized:
        return None

    lowered = normalized.lower()
    analysis = _extract_labeled_debug_field(
        normalized,
        ("analysis", "root cause", "cause"),
    )
    fix = _extract_labeled_debug_field(
        normalized,
        ("fix", "recommended fix", "proposed fix", "solution", "next step"),
    )
    confidence = _extract_labeled_debug_field(normalized, ("confidence",))
    explicit_fix_type = _extract_labeled_debug_field(
        normalized,
        ("fix_type", "fix type"),
    )

    if not analysis:
        analysis = normalized.split("\n\n", 1)[0].strip()[:800]

    fix_type_match = re.search(
        r"\b(code_fix|command_fix|revise_plan)\b",
        explicit_fix_type or normalized,
        flags=re.IGNORECASE,
    )
    if fix_type_match:
        fix_type = fix_type_match.group(1).lower()
    elif any(
        marker in lowered
        for marker in (
            "revise_plan",
            "revise the plan",
            "split the step",
            "split this step",
            "rewrite the remaining plan",
            "too large",
            "too brittle",
        )
    ):
        fix_type = "revise_plan"
    elif any(
        marker in lowered
        for marker in (
            "replace the command",
            "update the command",
            "run `",
            "use `",
            "use rg --files",
            "list the files first",
            "wrong expected file",
            "wrong expected files",
        )
    ):
        fix_type = "command_fix"
    else:
        fix_type = "code_fix"

    fix_str = (fix or "").strip()[:1200]
    if fix_type == "command_fix":
        normalized_fix_str = normalize_runnable_shell_command_fix(fix_str)
        if is_runnable_shell_command_fix(normalized_fix_str):
            fix_str = normalized_fix_str[:1200]
        else:
            fix_type = "code_fix"

    payload: Dict[str, Any] = {
        "fix_type": fix_type,
        "analysis": analysis[:1200],
        "fix": fix_str,
        "confidence": _normalize_debug_confidence(confidence or normalized),
    }

    missing_expected_files = _extract_missing_expected_files(error_message)
    should_trim_expected_files = (
        bool(missing_expected_files)
        and isinstance(step, dict)
        and isinstance(step.get("expected_files"), list)
        and any(
            marker in lowered
            for marker in (
                "doesn't exist",
                "does not exist",
                "not required",
                "should not",
                "shouldn't",
                "wrong assumption",
                "incorrectly expected",
                "remove",
                "no readme",
                "without expecting",
            )
        )
    )
    if should_trim_expected_files:
        updated_expected_files = [
            item
            for item in step.get("expected_files", [])
            if item not in missing_expected_files
        ]
        if updated_expected_files != step.get("expected_files", []):
            payload["expected_files"] = updated_expected_files
            if not payload["fix"]:
                payload["fix"] = (
                    "Retry the step without expecting these files: "
                    + ", ".join(missing_expected_files)
                )[:1200]
            if payload["fix_type"] == "code_fix" and is_runnable_shell_command_fix(
                payload.get("fix", "")
            ):
                payload["fix_type"] = "command_fix"

    if (
        not payload.get("analysis")
        and not payload.get("fix")
        and "expected_files" not in payload
    ):
        return None

    return payload


def _extract_labeled_debug_field(text: str, labels: tuple[str, ...]) -> str:
    for label in labels:
        pattern = re.compile(
            rf"(?:^|\n)\s*(?:\*\*)?{re.escape(label)}(?:\*\*)?\s*:\s*(.+?)(?=\n\s*(?:\*\*)?[A-Za-z _-]+(?:\*\*)?\s*:|\Z)",
            flags=re.IGNORECASE | re.DOTALL,
        )
        match = pattern.search(text)
        if match:
            cleaned = match.group(1).strip()
            cleaned = re.sub(r"^\*+\s*", "", cleaned)
            cleaned = re.sub(r"\s*\*+$", "", cleaned)
            return cleaned.strip()
    return ""


def _normalize_debug_confidence(value: str) -> str:
    lowered = (value or "").lower()
    if "high" in lowered:
        return "HIGH"
    if "low" in lowered:
        return "LOW"
    return "MEDIUM"


def _extract_missing_expected_files(error_message: str) -> list[str]:
    prefix = "expected files are missing:"
    lowered = (error_message or "").lower()
    if prefix not in lowered:
        return []

    start = lowered.index(prefix) + len(prefix)
    raw_suffix = (error_message or "")[start:]
    candidates = []
    for item in raw_suffix.split(","):
        cleaned = item.strip().strip(".")
        if cleaned:
            candidates.append(cleaned)
    return candidates
