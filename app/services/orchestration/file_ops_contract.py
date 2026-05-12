"""Structured file operation contract shared across orchestration modules."""

from __future__ import annotations

from typing import Any, Mapping, Set

FILE_OP_FIELD_SETS: Mapping[str, Set[str]] = {
    "mkdir": {"op", "path"},
    "delete_file": {"op", "path"},
    "write_file": {"op", "path", "content"},
    "append_file": {"op", "path", "content"},
    "replace_in_file": {"op", "path", "old", "new"},
}
SUPPORTED_FILE_OPS = frozenset(FILE_OP_FIELD_SETS)
CONTENT_FILE_OPS = frozenset({"write_file", "append_file"})


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


def render_supported_file_ops() -> str:
    return ", ".join(sorted(SUPPORTED_FILE_OPS))
