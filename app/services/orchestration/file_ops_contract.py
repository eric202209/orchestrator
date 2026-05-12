"""Structured file operation contract shared across orchestration modules."""

from __future__ import annotations

from typing import Any, Dict, Mapping, Set

FILE_OP_FIELD_SETS: Mapping[str, Set[str]] = {
    "mkdir": {"op", "path"},
    "delete_file": {"op", "path"},
    "write_file": {"op", "path", "content"},
    "append_file": {"op", "path", "content"},
    "replace_in_file": {"op", "path", "old", "new"},
}
SUPPORTED_FILE_OPS = frozenset(FILE_OP_FIELD_SETS)
CONTENT_FILE_OPS = frozenset({"write_file", "append_file"})
REPLACE_IN_FILE_OLD_ALIASES = ("search", "match", "pattern", "old_string")
REPLACE_IN_FILE_NEW_ALIASES = ("replace", "replacement", "new_string")


def is_supported_file_op_name(op_name: Any) -> bool:
    return str(op_name or "") in SUPPORTED_FILE_OPS


def operation_has_file_op_path(operation: Any) -> bool:
    return (
        isinstance(operation, dict)
        and is_supported_file_op_name(operation.get("op"))
        and bool(str(operation.get("path") or "").strip())
    )


def validate_file_op_shape(operation: Any) -> bool:
    if not isinstance(operation, dict):
        return False

    operation = normalize_file_op_shape(operation)
    op_name = str(operation.get("op") or "")
    expected_keys = FILE_OP_FIELD_SETS.get(op_name)
    if expected_keys is None or set(operation.keys()) != expected_keys:
        return False

    if not isinstance(operation.get("path"), str):
        return False
    if op_name in CONTENT_FILE_OPS:
        return isinstance(operation.get("content"), str)
    if op_name == "replace_in_file":
        return isinstance(operation.get("old"), str) and isinstance(
            operation.get("new"), str
        )
    return True


def expected_file_op_keys(op_name: str) -> Set[str]:
    return set(FILE_OP_FIELD_SETS[str(op_name)])


def normalize_file_op_shape(operation: Mapping[str, Any]) -> Dict[str, Any]:
    op_name = str(operation.get("op") or "")
    if op_name == "replace_in_file":
        return normalize_replace_in_file_aliases(operation)

    expected_keys = FILE_OP_FIELD_SETS.get(op_name)
    if expected_keys is None:
        return dict(operation)
    return {key: operation[key] for key in expected_keys if key in operation}


def normalize_replace_in_file_aliases(operation: Mapping[str, Any]) -> Dict[str, Any]:
    """Coerce common replace op aliases and drop unrelated metadata keys."""

    normalized: Dict[str, Any] = {
        key: operation[key] for key in ("op", "path", "old", "new") if key in operation
    }
    old_aliases = [key for key in REPLACE_IN_FILE_OLD_ALIASES if key in operation]
    new_aliases = [key for key in REPLACE_IN_FILE_NEW_ALIASES if key in operation]
    if "old" not in normalized:
        if len(old_aliases) == 1:
            normalized["old"] = operation[old_aliases[0]]
        elif len(old_aliases) > 1:
            for key in old_aliases:
                normalized[key] = operation[key]
    else:
        for key in old_aliases:
            normalized[key] = operation[key]

    if "new" not in normalized:
        if len(new_aliases) == 1:
            normalized["new"] = operation[new_aliases[0]]
        elif len(new_aliases) > 1:
            for key in new_aliases:
                normalized[key] = operation[key]
    else:
        for key in new_aliases:
            normalized[key] = operation[key]

    return normalized


def render_supported_file_ops() -> str:
    return ", ".join(sorted(SUPPORTED_FILE_OPS))
