"""Parsing helpers shared across orchestration flows."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional


def _strip_markdown_fences(text: str) -> str:
    stripped = str(text or "").strip()
    if not stripped:
        return ""
    return re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", stripped).strip()


def _find_json_substring(text: str) -> Optional[str]:
    stripped = str(text or "").strip()
    if not stripped:
        return None

    start_positions = [
        idx for idx in (stripped.find("["), stripped.find("{")) if idx >= 0
    ]
    if not start_positions:
        return None
    json_start = min(start_positions)

    brace_count = 0
    bracket_count = 0
    in_string = False
    escape_next = False

    for idx, char in enumerate(stripped[json_start:], json_start):
        if escape_next:
            escape_next = False
            continue
        if char == "\\" and in_string:
            escape_next = True
            continue
        if char == '"':
            in_string = not in_string
            continue
        if in_string:
            continue
        if char == "{":
            brace_count += 1
        elif char == "}":
            brace_count -= 1
        elif char == "[":
            bracket_count += 1
        elif char == "]":
            bracket_count -= 1
        if brace_count == 0 and bracket_count == 0 and idx > json_start:
            return stripped[json_start : idx + 1]

    return None


def _parse_nested_json_text(text: str) -> Any:
    cleaned = _strip_markdown_fences(text)
    if not cleaned:
        return None

    for candidate_text in (cleaned, _find_json_substring(cleaned)):
        if not candidate_text:
            continue
        try:
            return json.loads(candidate_text)
        except json.JSONDecodeError:
            continue
    return None


def _extract_backticked_paths(text: str) -> List[str]:
    return [
        match.strip()
        for match in re.findall(r"`([^`]+)`", text or "")
        if match.strip() and ("/" in match or "." in Path(match.strip()).name)
    ]


def extract_plan_steps_from_summary_text(text: str) -> Optional[List[Dict[str, Any]]]:
    """Recover a rough machine plan from concise prose/table summaries.

    Intended for local-model outputs like:
    - "5-step plan:"
    - markdown tables with columns # / Step / Files
    - short numbered plan summaries
    """

    stripped = str(text or "").strip()
    if not stripped:
        return None
    lowered = stripped.lower()
    if (
        "step plan" not in lowered
        and "5-step plan" not in lowered
        and "| # |" not in lowered
    ):
        return None

    lines = [line.rstrip() for line in stripped.splitlines()]
    plan: List[Dict[str, Any]] = []
    row_pattern = re.compile(r"^\|\s*(\d+)\s*\|\s*(.+?)\s*\|\s*(.*?)\s*\|$")

    for raw_line in lines:
        line = raw_line.strip()
        if not line or line.startswith("|---"):
            continue
        match = row_pattern.match(line)
        if not match:
            continue
        step_number = int(match.group(1))
        step_text = match.group(2).strip()
        files_text = match.group(3).strip()
        expected_files = [
            path
            for path in _extract_backticked_paths(files_text)
            if path not in {"—", "-"}
        ]
        command_candidates = _extract_backticked_paths(step_text)
        description = re.sub(r"`([^`]+)`", r"\1", step_text).strip()

        commands: List[str] = []
        verification: Optional[str] = None
        rollback: Optional[str] = None

        if "create" in step_text.lower() and "dir" in step_text.lower():
            dir_paths = _extract_backticked_paths(step_text)
            if dir_paths:
                commands = [f"mkdir -p {' '.join(dir_paths)}"]
                verification = " && ".join(f"test -d {path}" for path in dir_paths)
                expected_files = dir_paths
        elif step_text.lower().startswith("generate ") or step_text.lower().startswith(
            "write "
        ):
            target_path = (command_candidates or expected_files[:1] or [""])[0]
            if target_path:
                summary = description
                summary = re.sub(
                    r"^(generate|write)\s+", "", summary, flags=re.IGNORECASE
                )
                summary = summary.replace(target_path, "").strip(" :,-")
                summary = summary or f"implement {target_path}"
                commands = [f"write {target_path}: {summary}"]
                verification = f"test -s {target_path}"
                rollback = f"rm -f {target_path}"
                expected_files = [target_path]
        elif step_text.lower().startswith("verify "):
            targets = expected_files[:]
            if targets:
                commands = [" && ".join(f"test -s {path}" for path in targets)]
                verification = commands[0]
            else:
                commands = ["rg --files . | sort"]
                verification = "test -d ."

        if commands:
            plan.append(
                {
                    "step_number": step_number,
                    "description": description,
                    "commands": commands,
                    "verification": verification,
                    "rollback": rollback,
                    "expected_files": expected_files,
                }
            )

    return plan or None


def looks_like_truncated_multistep_plan(
    output_text: str, extracted_plan: Optional[List[Dict[str, Any]]]
) -> bool:
    """Detect mixed-content planning output that collapsed into a single-step plan."""
    if not extracted_plan or len(extracted_plan) != 1:
        return False

    text = output_text or ""
    step_number_mentions = len(
        re.findall(
            r'(?:\\)?["\']step_number(?:\\)?["\']\s*:\s*\d+', text, flags=re.IGNORECASE
        )
    )
    if step_number_mentions > 1:
        return True

    if re.search(
        r'(?:\\)?["\']step_number(?:\\)?["\']\s*:\s*[2-9]\d*',
        text,
        flags=re.IGNORECASE,
    ):
        return True

    description_mentions = len(
        re.findall(
            r'(?:\\)?["\']description(?:\\)?["\']\s*:', text, flags=re.IGNORECASE
        )
    )
    if description_mentions > 1:
        return True

    return False


def extract_plan_steps(parsed_planning_output: Any) -> Optional[List[Dict[str, Any]]]:
    """Accept common planning response wrappers and return the step list."""

    def looks_like_single_step(candidate: Any) -> bool:
        if not isinstance(candidate, dict):
            return False

        step_like_keys = {
            "step_number",
            "description",
            "commands",
            "verification",
            "rollback",
            "expected_files",
        }
        return bool(step_like_keys.intersection(candidate.keys()))

    def looks_like_plan_steps(candidate: Any) -> bool:
        if not isinstance(candidate, list) or not candidate:
            return False

        required_hint_keys = {
            "step_number",
            "description",
            "commands",
            "verification",
            "rollback",
            "expected_files",
        }
        saw_step_like_item = False

        for item in candidate:
            if not isinstance(item, dict):
                return False
            if required_hint_keys.intersection(item.keys()):
                saw_step_like_item = True

        return saw_step_like_item

    if looks_like_single_step(parsed_planning_output):
        return [parsed_planning_output]

    if looks_like_plan_steps(parsed_planning_output):
        return parsed_planning_output

    if isinstance(parsed_planning_output, str):
        reparsed = _parse_nested_json_text(parsed_planning_output)
        if reparsed is None or reparsed == parsed_planning_output:
            return None
        return extract_plan_steps(reparsed)

    if isinstance(parsed_planning_output, list):
        for item in parsed_planning_output:
            nested_plan = extract_plan_steps(item)
            if nested_plan is not None:
                return nested_plan
        return None

    if not isinstance(parsed_planning_output, dict):
        return None

    priority_keys = (
        "steps",
        "plan",
        "task_plan",
        "execution_plan",
        "revised_plan",
        "remaining_steps",
        "workflow",
        "items",
    )
    for key in priority_keys:
        candidate = parsed_planning_output.get(key)
        if looks_like_single_step(candidate):
            return [candidate]
        if looks_like_plan_steps(candidate):
            return candidate

    payloads = parsed_planning_output.get("payloads")
    if isinstance(payloads, list):
        for payload in payloads:
            nested_plan = extract_plan_steps(payload)
            if nested_plan is not None:
                return nested_plan

    for value in parsed_planning_output.values():
        if looks_like_single_step(value):
            return [value]
        if looks_like_plan_steps(value):
            return value
        if isinstance(value, str):
            nested_plan = extract_plan_steps(value)
            if nested_plan is not None:
                return nested_plan

    for value in parsed_planning_output.values():
        if isinstance(value, (dict, list)):
            nested_plan = extract_plan_steps(value)
            if nested_plan is not None:
                return nested_plan

    return None


def extract_structured_text(value: Any) -> str:
    """Recover human/model text from common OpenClaw payload shapes."""

    if value is None:
        return ""

    if isinstance(value, str):
        return value

    if isinstance(value, list):
        parts = [extract_structured_text(item) for item in value]
        return "\n".join(part for part in parts if part)

    if not isinstance(value, dict):
        return str(value)

    for key in (
        "text",
        "output_text",
        "content_text",
        "finalAssistantVisibleText",
        "final_assistant_visible_text",
    ):
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate

    content = value.get("content")
    if isinstance(content, list):
        content_text = extract_structured_text(content)
        if content_text.strip():
            return content_text
    elif isinstance(content, str) and content.strip():
        return content

    message = value.get("message")
    if isinstance(message, (dict, list, str)):
        message_text = extract_structured_text(message)
        if message_text.strip():
            return message_text

    payloads = value.get("payloads")
    if isinstance(payloads, list):
        payload_text = extract_structured_text(payloads)
        if payload_text.strip():
            return payload_text

    for candidate in value.values():
        if isinstance(candidate, (dict, list, str)):
            nested_text = extract_structured_text(candidate)
            if nested_text.strip():
                return nested_text

    return json.dumps(value)
