"""Test-only slot extraction and merge helpers for planning repair."""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import PurePosixPath
from typing import Any


SOURCE_OPS = {"write_file", "append_file", "replace_in_file"}


class SlotRepairError(ValueError):
    """Raised when slots cannot be safely compiled into a typed plan."""


@dataclass(frozen=True)
class SlotRepairTaskContext:
    allowed_target_files: tuple[str, ...]
    allowed_verification_commands: tuple[str, ...]
    allow_test_changes: bool = False
    current_file_contents: dict[str, str] = field(default_factory=dict)
    bootstrap_required_source_files: tuple[str, ...] = ()
    bootstrap_required_test_files: tuple[str, ...] = ()
    bootstrap_required_verification: tuple[str, ...] = ()


@dataclass(frozen=True)
class PlanSlots:
    target_file: str | None = None
    source_op: dict[str, Any] | None = None
    verification_command: str | None = None
    commands: tuple[str, ...] = ()
    expected_files: tuple[str, ...] = ()
    bootstrap_required_source_files: tuple[str, ...] = ()
    bootstrap_required_test_files: tuple[str, ...] = ()
    bootstrap_required_verification: tuple[str, ...] = ()
    rejected: bool = False
    rejection_reasons: tuple[str, ...] = field(default_factory=tuple)


def extract_plan_slots(
    plan: Any,
    task_context: SlotRepairTaskContext,
) -> PlanSlots:
    """Extract reusable planning slots from a possibly incomplete plan."""

    reasons: list[str] = []
    if not isinstance(plan, list):
        return _slots(task_context, rejected=True, reasons=["plan is not a list"])

    allowed_targets = {
        _normalize_relative_file(path) for path in task_context.allowed_target_files
    }
    allowed_verification = {
        command.strip() for command in task_context.allowed_verification_commands
    }
    target_file: str | None = None
    source_op: dict[str, Any] | None = None
    verification_command: str | None = None
    commands: list[str] = []
    expected_files: list[str] = []
    seen_source_op_paths: set[str] = set()

    for step in plan:
        if not isinstance(step, dict):
            continue
        for raw_expected_file in step.get("expected_files") or []:
            normalized = _normalize_relative_file_or_none(raw_expected_file)
            if normalized and normalized not in expected_files:
                expected_files.append(normalized)

        for command in step.get("commands") or []:
            if isinstance(command, str) and command.strip():
                commands.append(command.strip())
                if command.strip() in allowed_verification and not verification_command:
                    verification_command = command.strip()
            _record_source_op_shape_error(command, reasons)
            op_from_command = _normalize_source_op(command)
            if op_from_command:
                candidate = _validated_source_op(
                    op_from_command,
                    allowed_targets=allowed_targets,
                    allow_test_changes=task_context.allow_test_changes,
                    current_file_contents=task_context.current_file_contents,
                    seen_source_op_paths=seen_source_op_paths,
                    reasons=reasons,
                )
                if candidate and source_op is None:
                    source_op = candidate
                    target_file = str(candidate["path"])

        verification = step.get("verification")
        if isinstance(verification, str) and verification.strip():
            if (
                verification.strip() in allowed_verification
                and not verification_command
            ):
                verification_command = verification.strip()

        for raw_op in step.get("ops") or []:
            _record_source_op_shape_error(raw_op, reasons)
            normalized_op = _normalize_source_op(raw_op)
            if not normalized_op:
                continue
            candidate = _validated_source_op(
                normalized_op,
                allowed_targets=allowed_targets,
                allow_test_changes=task_context.allow_test_changes,
                current_file_contents=task_context.current_file_contents,
                seen_source_op_paths=seen_source_op_paths,
                reasons=reasons,
            )
            if candidate and source_op is None:
                source_op = candidate
                target_file = str(candidate["path"])

    if source_op and source_op["path"] not in expected_files:
        expected_files.append(str(source_op["path"]))

    return _slots(
        task_context,
        target_file=target_file,
        source_op=source_op,
        verification_command=verification_command,
        commands=tuple(dict.fromkeys(commands)),
        expected_files=tuple(dict.fromkeys(expected_files)),
        rejected=bool(reasons),
        reasons=reasons,
    )


def merge_repair_slots(
    previous_slots: PlanSlots,
    candidate_slots: PlanSlots,
    repair_reason: str,
) -> PlanSlots:
    """Merge candidate repair slots with previous known-good slots."""

    if candidate_slots.rejected:
        return previous_slots

    reason = str(repair_reason or "").lower()
    verification_only = "verification" in reason and "source" not in reason

    source_op = candidate_slots.source_op or previous_slots.source_op
    if verification_only and previous_slots.source_op is not None:
        source_op = previous_slots.source_op
    elif candidate_slots.source_op and previous_slots.source_op:
        candidate_path = str(candidate_slots.source_op.get("path") or "")
        previous_path = str(previous_slots.source_op.get("path") or "")
        if candidate_path != previous_path:
            source_op = previous_slots.source_op

    target_file = (
        str(source_op.get("path"))
        if source_op
        else candidate_slots.target_file or previous_slots.target_file
    )
    verification_command = (
        candidate_slots.verification_command
        or previous_slots.verification_command
        or _first(candidate_slots.bootstrap_required_verification)
        or _first(previous_slots.bootstrap_required_verification)
    )
    commands = candidate_slots.commands or previous_slots.commands
    if verification_command and verification_command not in commands:
        commands = (*commands, verification_command)

    expected_files = tuple(
        dict.fromkeys(
            [
                *(previous_slots.expected_files if verification_only else ()),
                *candidate_slots.expected_files,
                *(
                    previous_slots.expected_files
                    if not candidate_slots.expected_files
                    else ()
                ),
                *([str(source_op["path"])] if source_op else []),
            ]
        )
    )

    return PlanSlots(
        target_file=target_file,
        source_op=source_op,
        verification_command=verification_command,
        commands=tuple(dict.fromkeys(commands)),
        expected_files=expected_files,
        bootstrap_required_source_files=tuple(
            dict.fromkeys(
                previous_slots.bootstrap_required_source_files
                + candidate_slots.bootstrap_required_source_files
            )
        ),
        bootstrap_required_test_files=tuple(
            dict.fromkeys(
                previous_slots.bootstrap_required_test_files
                + candidate_slots.bootstrap_required_test_files
            )
        ),
        bootstrap_required_verification=tuple(
            dict.fromkeys(
                previous_slots.bootstrap_required_verification
                + candidate_slots.bootstrap_required_verification
            )
        ),
    )


def compile_slots_to_typed_plan(slots: PlanSlots) -> list[dict[str, Any]]:
    """Compile complete slots into the normal strict typed plan shape."""

    if slots.rejected:
        raise SlotRepairError("rejected slots cannot be compiled")
    if not slots.source_op:
        raise SlotRepairError("source materialization slot is required")
    if not slots.verification_command:
        raise SlotRepairError("verification command slot is required")
    target_file = _normalize_relative_file(str(slots.source_op.get("path") or ""))
    expected_files = (target_file,)
    verification_expected_files = tuple(
        dict.fromkeys(
            _normalize_relative_file(path)
            for path in slots.bootstrap_required_test_files
        )
    )
    verification_commands = tuple(
        dict.fromkeys([*slots.commands, slots.verification_command])
    )

    return [
        {
            "step_number": 1,
            "description": f"Apply source materialization for {target_file}",
            "commands": [],
            "verification": slots.verification_command,
            "rollback": None,
            "expected_files": list(expected_files),
            "ops": [dict(slots.source_op)],
        },
        {
            "step_number": 2,
            "description": f"Verify repaired plan for {target_file}",
            "commands": list(verification_commands),
            "verification": slots.verification_command,
            "rollback": None,
            "expected_files": list(verification_expected_files),
        },
    ]


def _slots(
    task_context: SlotRepairTaskContext,
    *,
    target_file: str | None = None,
    source_op: dict[str, Any] | None = None,
    verification_command: str | None = None,
    commands: tuple[str, ...] = (),
    expected_files: tuple[str, ...] = (),
    rejected: bool = False,
    reasons: list[str] | None = None,
) -> PlanSlots:
    return PlanSlots(
        target_file=target_file,
        source_op=source_op,
        verification_command=verification_command,
        commands=commands,
        expected_files=expected_files,
        bootstrap_required_source_files=task_context.bootstrap_required_source_files,
        bootstrap_required_test_files=task_context.bootstrap_required_test_files,
        bootstrap_required_verification=task_context.bootstrap_required_verification,
        rejected=rejected,
        rejection_reasons=tuple(reasons or ()),
    )


def _normalize_source_op(value: Any) -> dict[str, Any] | None:
    if not isinstance(value, dict):
        return None
    op_name = str(value.get("op") or value.get("type") or "").strip()
    if op_name not in SOURCE_OPS:
        return None
    path = _normalize_relative_file_or_none(value.get("path"))
    if not path:
        return None
    if op_name in {"write_file", "append_file"}:
        content = value.get("content")
        if not isinstance(content, str):
            return None
        return {"op": op_name, "path": path, "content": content}
    old = value.get("old")
    new = value.get("new")
    if not isinstance(old, str) or not isinstance(new, str):
        return None
    return {"op": op_name, "path": path, "old": old, "new": new}


def _validated_source_op(
    op: dict[str, Any],
    *,
    allowed_targets: set[str],
    allow_test_changes: bool,
    current_file_contents: dict[str, str],
    seen_source_op_paths: set[str],
    reasons: list[str],
) -> dict[str, Any] | None:
    path = str(op.get("path") or "")
    if path not in allowed_targets:
        reasons.append(f"target file is not allowed: {path}")
        return None
    if not allow_test_changes and _is_test_path(path):
        reasons.append(f"test file changes are not allowed: {path}")
        return None
    if path in seen_source_op_paths:
        reasons.append(f"duplicate source materialization op for target: {path}")
        return None
    if str(op.get("op") or "") == "replace_in_file":
        current_content = current_file_contents.get(path)
        old_text = str(op.get("old") or "")
        if current_content is not None and old_text not in current_content:
            reasons.append(f"stale replace_in_file old text for target: {path}")
            return None
    seen_source_op_paths.add(path)
    return op


def _record_source_op_shape_error(value: Any, reasons: list[str]) -> None:
    if not isinstance(value, dict):
        return
    op_name = str(value.get("op") or value.get("type") or "").strip()
    if op_name not in SOURCE_OPS:
        return
    if not _normalize_relative_file_or_none(value.get("path")):
        reasons.append("source op path must be a safe relative file path")


def _normalize_relative_file_or_none(value: Any) -> str | None:
    try:
        return _normalize_relative_file(str(value or ""))
    except SlotRepairError:
        return None


def _normalize_relative_file(path_text: str) -> str:
    normalized = str(path_text or "").strip().replace("\\", "/")
    while normalized.startswith("./"):
        normalized = normalized[2:]
    path = PurePosixPath(normalized)
    if not normalized or path.is_absolute() or ".." in path.parts:
        raise SlotRepairError("path must be a safe relative file path")
    if normalized.endswith("/") or not path.suffix:
        raise SlotRepairError("path must be a file path")
    return path.as_posix()


def _is_test_path(path: str) -> bool:
    normalized = str(path or "").replace("\\", "/").lstrip("./")
    name = PurePosixPath(normalized).name
    return normalized.startswith(("tests/", "test/")) or name.startswith("test_")


def _first(values: tuple[str, ...]) -> str | None:
    return values[0] if values else None
