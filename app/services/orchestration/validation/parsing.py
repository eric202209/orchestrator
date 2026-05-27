"""Parsing helpers shared across orchestration flows."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, List, Optional

_VISIBLE_TEXT_KEYS = (
    "finalAssistantVisibleText",
    "final_assistant_visible_text",
    "text",
    "output_text",
    "content_text",
)


def build_json_compliance_retry_prompt(
    previous_response: Any,
    *,
    expected_shape: str = "array or object",
) -> str:
    previous_excerpt = str(previous_response or "")[:400]
    return (
        "Your previous response was not valid JSON.\n"
        f"Return only a JSON {expected_shape} with no surrounding text.\n"
        "Previous response:\n"
        f"{previous_excerpt}"
    )


def _strip_markdown_fences(text: str) -> str:
    stripped = str(text or "").strip()
    if not stripped:
        return ""
    return re.sub(r"^\s*```(?:json)?\s*|\s*```\s*$", "", stripped).strip()


def _find_json_substring(text: str) -> Optional[str]:
    stripped = str(text or "").strip()
    if not stripped:
        return None

    candidates = sorted(
        idx for char in ("[", "{") for idx in _all_positions(stripped, char)
    )[:8]
    for json_start in candidates:
        result = _extract_from_position(stripped, json_start)
        if result is None:
            continue
        try:
            parsed = json.loads(result)
        except json.JSONDecodeError:
            continue
        if _should_skip_nested_non_plan_candidate(stripped, result, parsed):
            continue
        return result

    return None


def _has_step_shape(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    step_keys = {"step_number", "commands", "description"}
    return bool(step_keys.intersection(value.keys()))


def _should_skip_nested_non_plan_candidate(
    source_text: str, candidate_text: str, parsed_candidate: Any
) -> bool:
    """Avoid treating JSON-looking content inside a plan string as the plan."""

    source = _strip_markdown_fences(str(source_text or "")).lstrip()
    candidate = str(candidate_text or "").lstrip()
    if not source.startswith("[") or candidate == source:
        return False
    if candidate.startswith("{"):
        return isinstance(parsed_candidate, dict)
    if candidate.startswith("["):
        return not (
            isinstance(parsed_candidate, list)
            and parsed_candidate
            and _has_step_shape(parsed_candidate[0])
        )
    return False


def _looks_like_raw_plan_array_text(text: str) -> bool:
    stripped = _strip_markdown_fences(str(text or "")).lstrip()
    if not stripped.startswith("["):
        return False
    return bool(
        re.search(
            r'"(?:step_number|commands|description|expected_files|ops)"\s*:',
            stripped[:4000],
        )
    )


def _all_positions(text: str, char: str) -> List[int]:
    positions: List[int] = []
    start = 0
    while True:
        idx = text.find(char, start)
        if idx < 0:
            return positions
        positions.append(idx)
        start = idx + 1


def _extract_from_position(text: str, start: int) -> Optional[str]:
    stripped = str(text or "")
    if start < 0 or start >= len(stripped):
        return None

    brace_count = 0
    bracket_count = 0
    in_string = False
    escape_next = False

    for idx, char in enumerate(stripped[start:], start):
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
        if brace_count == 0 and bracket_count == 0 and idx > start:
            return stripped[start : idx + 1]

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


def _extract_quoted_json_string_value(text: str, key: str) -> Optional[str]:
    """Decode a JSON string value from a partial `"key": "..."` fragment."""

    key_pattern = re.compile(rf'"{re.escape(key)}"\s*:', re.DOTALL)
    decoder = json.JSONDecoder()

    for match in key_pattern.finditer(text or ""):
        value_start = match.end()
        while value_start < len(text) and text[value_start].isspace():
            value_start += 1
        if value_start >= len(text) or text[value_start] != '"':
            continue

        fragment = text[value_start:]
        try:
            value, _ = decoder.raw_decode(fragment)
        except json.JSONDecodeError:
            value = None

        if isinstance(value, str) and value.strip():
            return value

        in_escape = False
        for offset, char in enumerate(fragment[1:], 1):
            if in_escape:
                in_escape = False
                continue
            if char == "\\":
                in_escape = True
                continue
            if char != '"':
                continue
            candidate = fragment[: offset + 1]
            try:
                decoded = json.loads(candidate)
            except json.JSONDecodeError:
                break
            if isinstance(decoded, str) and decoded.strip():
                return decoded
            break

    return None


def _extract_visible_text_from_json_like_fragment(text: str) -> Optional[str]:
    """Recover visible model text from invalid outer JSON/OpenClaw fragments."""

    for key in _VISIBLE_TEXT_KEYS:
        value = _extract_quoted_json_string_value(text, key)
        if value and value.strip():
            return value
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
        visible_text = _extract_visible_text_from_json_like_fragment(
            parsed_planning_output
        )
        reparsed = _parse_nested_json_text(visible_text or parsed_planning_output)
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


def _extract_visible_text_payload(value: Any) -> Optional[str]:
    """Return model-visible text from nested wrapper payloads only."""

    if value is None:
        return None

    if isinstance(value, list):
        parts = [
            text for item in value if (text := _extract_visible_text_payload(item))
        ]
        return "\n".join(parts) if parts else None

    if not isinstance(value, dict):
        return None

    for key in _VISIBLE_TEXT_KEYS:
        candidate = value.get(key)
        if isinstance(candidate, str) and candidate.strip():
            return candidate

    for key in ("content", "message", "payloads"):
        candidate = value.get(key)
        if isinstance(candidate, (dict, list)):
            nested_text = _extract_visible_text_payload(candidate)
            if nested_text and nested_text.strip():
                return nested_text
        elif isinstance(candidate, str) and candidate.strip():
            return candidate

    for candidate in value.values():
        if isinstance(candidate, (dict, list)):
            nested_text = _extract_visible_text_payload(candidate)
            if nested_text and nested_text.strip():
                return nested_text
        elif isinstance(candidate, str) and candidate.strip():
            parsed = _parse_nested_json_text(candidate)
            nested_text = _extract_visible_text_payload(parsed)
            if nested_text and nested_text.strip():
                return nested_text

    return None


def extract_structured_text(value: Any) -> str:
    """Recover human/model text from common OpenClaw payload shapes."""

    if value is None:
        return ""

    if isinstance(value, str):
        if _looks_like_raw_plan_array_text(value):
            return _strip_markdown_fences(value)
        parsed = _parse_nested_json_text(value)
        nested_text = _extract_visible_text_payload(parsed)
        if nested_text and nested_text.strip():
            return nested_text
        fragment_text = _extract_visible_text_from_json_like_fragment(value)
        if fragment_text and fragment_text.strip():
            return fragment_text
        return value

    if isinstance(value, list):
        parts = [extract_structured_text(item) for item in value]
        return "\n".join(part for part in parts if part)

    if not isinstance(value, dict):
        return str(value)

    for key in _VISIBLE_TEXT_KEYS:
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

    nested_visible_text = _extract_visible_text_payload(value)
    if nested_visible_text and nested_visible_text.strip():
        return nested_visible_text

    for candidate in value.values():
        if isinstance(candidate, (dict, list, str)):
            nested_text = extract_structured_text(candidate)
            if nested_text.strip():
                return nested_text

    return json.dumps(value)
