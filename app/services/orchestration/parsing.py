"""Parsing helpers shared across orchestration flows."""

from __future__ import annotations

import json
import re
from typing import Any, Dict, List, Optional


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

    for key in ("text", "output_text", "content_text"):
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
