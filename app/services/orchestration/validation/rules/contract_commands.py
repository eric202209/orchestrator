"""Workload-contract brittle / heredoc / background / non-runnable command rules.

Moved verbatim from validator.py in Phase 20L (validator rule split,
slice 2). Functions here inspect plan commands for brittle shapes
(oversized commands, unsafe/looped/multiple heredocs, malformed shell
quoting, brittle inline Python), background-process usage, and
non-runnable (prose-shaped) commands.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional

from ..workspace_checks import (
    uses_brittle_python_inline_command as _uses_brittle_python_inline_command_shared,
)


def _command_has_malformed_shell_quoting(command: str) -> bool:
    raw = str(command or "")
    if "\\'" in raw and re.search(r"\bprintf\s+'", raw):
        return True
    try:
        shlex.split(raw, posix=True)
    except ValueError:
        return True
    return False


def _plan_malformed_shell_quoting_steps(plan: List[Dict[str, Any]]) -> List[int]:
    bad_steps: List[int] = []
    for index, step in enumerate(plan, start=1):
        step_number = step.get("step_number", index)
        step_text_parts = [
            str(step.get("verification") or ""),
            str(step.get("rollback") or ""),
        ]
        step_text_parts.extend(
            str(command or "") for command in step.get("commands", []) or []
        )
        if any(_command_has_malformed_shell_quoting(text) for text in step_text_parts):
            bad_steps.append(int(step_number))
    return sorted(set(bad_steps))


def _single_file_write_heredoc_targets(command: str) -> List[str]:
    """Return targets for bounded `cat > file <<EOF` write heredocs."""

    target_pattern = re.compile(
        r"(?:^|[\n;&|]\s*)"
        r"(?:mkdir\s+-p\s+[^\n;&|]+\s*&&\s*)?"
        r"cat\s+>\s*(?P<target>'[^']+'|\"[^\"]+\"|[^\s<;&|]+)"
        r"\s*<<\s*['\"]?[A-Za-z_][A-Za-z0-9_]*['\"]?",
        re.IGNORECASE,
    )
    targets: List[str] = []
    for match in target_pattern.finditer(str(command or "")):
        target = match.group("target").strip().strip("'\"")
        if target:
            targets.append(target)
    return targets


def _uses_looped_heredoc(command: str) -> bool:
    first_line = str(command or "").split("\n", 1)[0].lower()
    return bool(re.search(r"\b(for|while)\b.*\bdo\b.*cat\s+>", first_line))


def _heredoc_target_is_unsafe(target: str) -> bool:
    path_text = str(target or "").strip()
    if not path_text:
        return True
    candidate = Path(path_text)
    return candidate.is_absolute() or "~" in candidate.parts or ".." in candidate.parts


def _uses_brittle_python_inline_command(command: str) -> bool:
    return _uses_brittle_python_inline_command_shared(command)


def _is_non_runnable_command(command: str) -> bool:
    text = str(command or "").strip()
    lowered = text.lower()
    if not text:
        return True
    if re.match(
        r"^(?:npm|pnpm|yarn)\s+install\s+\.?/[\w./-]+\.(?:js|jsx|ts|tsx)\s*$",
        lowered,
    ):
        return True
    if re.match(
        r"^(?:mv|cp)\s+\.?/[\w./-]+\.(?:py|js|jsx|ts|tsx)\s+\.?/[\w./-]+\.(?:py|js|jsx|ts|tsx)\s*$",
        lowered,
    ):
        return True
    if re.match(
        r"^\{\s*(?:\\?\"\\?|')?(?:ops|op|command|cmd)(?:\\?\"\\?|')?\s*:", text
    ):
        return True
    non_runnable_prefixes = (
        "write ",
        "edit ",
        "create files",
        "create file",
        "set up ",
        "setup ",
        "implement ",
        "add component",
        "update component",
        "verify ",
    )
    if lowered.startswith(non_runnable_prefixes):
        return True
    if lowered.startswith("check ") and "test " not in lowered:
        return True
    if lowered.startswith("ensure "):
        return True
    if lowered.startswith("confirm "):
        return True
    if re.match(
        r"^(create|build|make)\s+(the\s+)?(app|page|site|ui|component)\b", lowered
    ):
        return True
    return False


def _uses_background_process(command: str) -> bool:
    text = str(command or "").strip().lower()
    if not text:
        return False
    # Only check the first line for shell background operator (&).
    # Heredoc bodies start on line 2+, so bare & in HTML content (e.g.
    # "Flowers & Seasons") cannot trigger a false positive.
    first_line = text.split("\n")[0]
    if re.search(r"(?<![&])&(\s|$)", first_line):
        return True
    return any(
        marker in text
        for marker in (
            "nohup ",
            " disown",
            "tail -f",
            "npm run dev",
            "pnpm dev",
            "yarn dev",
            "vite dev",
            "next dev",
            "webpack serve",
        )
    )


def _plan_contains_background_processes(plan: List[Dict[str, Any]]) -> List[int]:
    bad_steps: List[int] = []
    for step in plan:
        for command in step.get("commands", []) or []:
            if _uses_background_process(str(command or "")):
                bad_steps.append(step.get("step_number"))
                break
    return [step for step in bad_steps if step is not None]


def _plan_contains_non_runnable_commands(plan: List[Dict[str, Any]]) -> List[int]:
    bad_steps: List[int] = []
    for step in plan:
        for command in step.get("commands", []) or []:
            if _is_non_runnable_command(str(command or "")):
                bad_steps.append(step.get("step_number"))
                break
    return [step for step in bad_steps if step is not None]


def _plan_command_budget_diagnostics(
    extracted_plan: Optional[List[Dict[str, Any]]], output_text: str = ""
) -> Dict[str, Any]:
    from ..validator import MAX_PLANNING_COMMAND_CHARS

    if not extracted_plan:
        return {
            "step_count": 0,
            "max_command_length": 0,
            "heredoc_command_count": 0,
            "command_total_chars": 0,
            "oversized_command_steps": [],
            "has_brittle_commands": False,
            "brittle_command_subcodes": [],
            "brittle_command_step_details": {},
            "brittle_command_step_command_lengths": {},
        }

    heredoc_count = 0
    max_command_length = 0
    command_total_chars = 0
    oversized_command_steps: List[int] = []
    has_brittle_commands = False
    plan_subcodes: set = set()
    step_subcodes: Dict[int, List[str]] = {}
    step_command_lengths: Dict[int, List[int]] = {}

    def _flag(step_num, code: str) -> None:
        nonlocal has_brittle_commands
        has_brittle_commands = True
        plan_subcodes.add(code)
        if step_num is not None:
            step_subcodes.setdefault(int(step_num), []).append(code)

    for step in extracted_plan:
        commands = step.get("commands", [])
        if not isinstance(commands, list):
            _flag(step.get("step_number"), "non_list_commands")
            continue
        step_number = step.get("step_number")
        for command in commands:
            raw_command = str(command or "")
            lowered = raw_command.lower()
            command_length = len(raw_command)
            write_heredoc_targets = _single_file_write_heredoc_targets(raw_command)
            command_total_chars += command_length
            max_command_length = max(max_command_length, command_length)
            if _uses_brittle_python_inline_command(raw_command):
                _flag(step_number, "brittle_inline_python")
            heredoc_count += len(write_heredoc_targets)
            if "<<" in lowered:
                if not write_heredoc_targets:
                    _flag(step_number, "disallowed_heredoc_shape")
                if len(write_heredoc_targets) > 1:
                    _flag(step_number, "multiple_heredoc_in_command")
                if _uses_looped_heredoc(raw_command):
                    _flag(step_number, "looped_heredoc")
                if any(
                    _heredoc_target_is_unsafe(target)
                    for target in write_heredoc_targets
                ):
                    _flag(step_number, "unsafe_heredoc_target")
            if raw_command.count("\n") > 25:
                _flag(step_number, "too_many_lines")
            if command_length > MAX_PLANNING_COMMAND_CHARS:
                _flag(step_number, "oversized_command_length")
                if step_number is not None:
                    normalized_step_number = int(step_number)
                    oversized_command_steps.append(normalized_step_number)
                    step_command_lengths.setdefault(normalized_step_number, []).append(
                        command_length
                    )

    if heredoc_count >= 2:
        has_brittle_commands = True
        plan_subcodes.add("multiple_heredoc_across_plan")

    lowered_output = (output_text or "").lower()
    if lowered_output.count("cat >") >= 2 and "```json" in lowered_output:
        has_brittle_commands = True
        plan_subcodes.add("markdown_wrapped_heredoc")

    return {
        "step_count": len(extracted_plan),
        "max_command_length": max_command_length,
        "heredoc_command_count": heredoc_count,
        "command_total_chars": command_total_chars,
        "oversized_command_steps": sorted(set(oversized_command_steps)),
        "has_brittle_commands": has_brittle_commands,
        "brittle_command_subcodes": sorted(plan_subcodes),
        "brittle_command_step_details": {
            k: sorted(set(v)) for k, v in step_subcodes.items()
        },
        "brittle_command_step_command_lengths": {
            k: sorted(set(v)) for k, v in step_command_lengths.items()
        },
        "malformed_shell_quoting_steps": _plan_malformed_shell_quoting_steps(
            extracted_plan
        ),
    }


def _plan_contains_brittle_commands(
    extracted_plan: Optional[List[Dict[str, Any]]], output_text: str = ""
) -> bool:
    diagnostics = _plan_command_budget_diagnostics(extracted_plan, output_text)
    return bool(diagnostics.get("has_brittle_commands"))


def _shadow_rule_warnings(
    command_budget: Dict[str, Any],
) -> List[Dict[str, Any]]:
    """Report downgrade candidates without changing validation status."""

    subcodes = set(command_budget.get("brittle_command_subcodes") or [])
    step_details = command_budget.get("brittle_command_step_details") or {}
    warnings: List[Dict[str, Any]] = []

    def _steps_for(codes: set[str]) -> List[int]:
        steps: List[int] = []
        for raw_step, raw_codes in step_details.items():
            if codes.intersection(set(raw_codes or [])):
                try:
                    steps.append(int(raw_step))
                except (TypeError, ValueError):
                    continue
        return sorted(set(steps))

    heredoc_codes = {
        "disallowed_heredoc_shape",
        "multiple_heredoc_in_command",
        "looped_heredoc",
        "unsafe_heredoc_target",
        "multiple_heredoc_across_plan",
        "markdown_wrapped_heredoc",
    }
    if subcodes.intersection(heredoc_codes):
        warnings.append(
            {
                "rule_id": "model_behavior.heredoc_guidance",
                "category": "model_behavior_patch",
                "current_owner": "validator.command_budget_diagnostics",
                "current_behavior": "repair_required",
                "shadow_candidate": True,
                "proposed_shadow_behavior": "warning_after_live_evidence",
                "fallback_detectors": [
                    "structured_ops_contract",
                    "workspace_guard",
                    "completion_verification",
                ],
                "subcodes": sorted(subcodes.intersection(heredoc_codes)),
                "steps": _steps_for(heredoc_codes),
            }
        )

    if "oversized_command_length" in subcodes:
        warnings.append(
            {
                "rule_id": "model_behavior.command_length_prompt_patch",
                "category": "model_behavior_patch",
                "current_owner": "validator.command_budget_diagnostics",
                "current_behavior": "repair_required",
                "shadow_candidate": True,
                "proposed_shadow_behavior": "warning_for_non_file_writing_commands",
                "fallback_detectors": [
                    "structured_ops_contract",
                    "completion_verification",
                ],
                "subcodes": ["oversized_command_length"],
                "steps": command_budget.get("oversized_command_steps") or [],
            }
        )

    malformed_shell_quoting_steps = (
        command_budget.get("malformed_shell_quoting_steps") or []
    )
    printf_or_shell_codes = {"brittle_inline_python", "too_many_lines"}
    if malformed_shell_quoting_steps or subcodes.intersection(printf_or_shell_codes):
        warnings.append(
            {
                "rule_id": "model_behavior.shell_quoting_patch",
                "category": "model_behavior_patch",
                "current_owner": "validator.command_budget_diagnostics",
                "current_behavior": "repair_required",
                "shadow_candidate": True,
                "proposed_shadow_behavior": "shell_fallback_warning",
                "fallback_detectors": [
                    "structured_ops_contract",
                    "executor_command_preflight",
                    "completion_verification",
                ],
                "subcodes": sorted(subcodes.intersection(printf_or_shell_codes)),
                "steps": sorted(set(malformed_shell_quoting_steps)),
            }
        )

    return warnings
