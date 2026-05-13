"""Structured operation repair helpers for execution debugging."""

from __future__ import annotations

from typing import Any

from app.services.orchestration.file_ops_contract import (
    normalize_file_op_shape,
    validate_file_op_shape,
)

WRAPPED_ASSISTANT_TEXT_KEYS = (
    "finalAssistantVisibleText",
    "final_assistant_visible_text",
    "assistant_visible_text",
)


def extract_wrapped_assistant_text(parsed_data: dict[str, Any]) -> str | None:
    """Return assistant-visible repair text from runtime envelope objects."""

    for key in WRAPPED_ASSISTANT_TEXT_KEYS:
        value = parsed_data.get(key)
        if isinstance(value, str) and value.strip():
            return value
    return None


def normalize_replacement_ops(parsed_data: dict[str, Any]) -> list[dict[str, Any]]:
    """Normalize typed replacement operations from a debug repair payload."""

    raw_ops: Any = None
    if isinstance(parsed_data.get("ops"), list):
        raw_ops = parsed_data.get("ops")
    elif isinstance(parsed_data.get("replacement_ops"), list):
        raw_ops = parsed_data.get("replacement_ops")
    elif isinstance(parsed_data.get("replacement_op"), dict):
        raw_ops = [parsed_data.get("replacement_op")]
    elif isinstance(parsed_data.get("op"), dict):
        raw_ops = [parsed_data.get("op")]

    if not raw_ops:
        return []

    normalized_ops: list[dict[str, Any]] = []
    for operation in raw_ops:
        if not isinstance(operation, dict):
            return []
        normalized = normalize_file_op_shape(operation)
        if not validate_file_op_shape(normalized):
            return []
        normalized_ops.append(normalized)
    return normalized_ops
