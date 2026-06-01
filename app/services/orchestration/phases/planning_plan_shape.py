"""Planning plan-shape helpers."""

from __future__ import annotations

from typing import Any

from app.services.orchestration.phases.planning_verification import (
    _python_exists_verification_command,
)


def prune_unmaterialized_expected_files(
    plan: list[dict[str, Any]],
    unmaterialized_paths: list[str],
) -> tuple[list[dict[str, Any]], dict[str, Any]]:
    """Drop expected_files entries that validation proved are not outputs."""

    if not unmaterialized_paths:
        return plan, {"changed": False, "reason": "no_unmaterialized_expected_files"}

    stale_paths = {
        str(path or "").strip().rstrip("/").lstrip("./")
        for path in unmaterialized_paths
        if str(path or "").strip()
    }
    if not stale_paths:
        return plan, {"changed": False, "reason": "empty_unmaterialized_expected_files"}

    concrete_op_paths = {
        str(op.get("path") or "").strip().rstrip("/").lstrip("./")
        for step in plan
        if isinstance(step, dict)
        for op in (step.get("ops") or [])
        if isinstance(op, dict)
        and str(op.get("op") or "") in {"write_file", "append_file", "replace_in_file"}
        and str(op.get("path") or "").strip()
    }
    if not concrete_op_paths:
        return plan, {"changed": False, "reason": "no_concrete_file_ops"}

    referenced_paths: set[str] = set()
    for step in plan:
        if not isinstance(step, dict):
            continue
        step_text = "\n".join(
            [str(step.get("verification") or "")]
            + [str(command or "") for command in (step.get("commands") or [])]
        )
        normalized_step_text = step_text.replace("\\", "/")
        for path in stale_paths:
            if not path:
                continue
            if path in normalized_step_text:
                referenced_paths.add(path)
                continue
            if path.startswith("tests/") and "tests" in normalized_step_text:
                referenced_paths.add(path)
                continue
            if path == "tests" and "tests" in normalized_step_text:
                referenced_paths.add(path)

    changed = False
    removed: list[str] = []
    normalized: list[dict[str, Any]] = []
    for step in plan:
        if not isinstance(step, dict):
            normalized.append(step)
            continue
        updated = dict(step)
        expected_files = []
        for raw_path in updated.get("expected_files") or []:
            path = str(raw_path or "").strip().rstrip("/").lstrip("./")
            if not path:
                continue
            if (
                path in stale_paths
                and path not in concrete_op_paths
                and path not in referenced_paths
            ):
                removed.append(path)
                changed = True
                continue
            expected_files.append(path)
        updated["expected_files"] = list(dict.fromkeys(expected_files))
        normalized.append(updated)

    return normalized, {
        "changed": changed,
        "reason": (
            "pruned_unmaterialized_expected_files"
            if changed
            else "no_speculative_expected_files_removed"
        ),
        "removed_expected_files": sorted(set(removed)),
        "concrete_op_paths": sorted(concrete_op_paths),
        "preserved_referenced_expected_files": sorted(referenced_paths),
    }


def split_repaired_single_step_full_lifecycle_plan(
    extracted_plan: Any,
) -> list[dict[str, Any]] | None:
    if not isinstance(extracted_plan, list) or len(extracted_plan) != 1:
        return None
    original = extracted_plan[0]
    if not isinstance(original, dict):
        return None

    ops = original.get("ops") if isinstance(original.get("ops"), list) else []
    commands = (
        original.get("commands") if isinstance(original.get("commands"), list) else []
    )
    commands = [
        str(command or "").strip() for command in commands if str(command or "").strip()
    ]
    expected_files = (
        original.get("expected_files")
        if isinstance(original.get("expected_files"), list)
        else []
    )
    expected_files = [
        str(path or "").strip().lstrip("./")
        for path in expected_files
        if str(path or "").strip()
    ]
    op_paths = [
        str(operation.get("path") or "").strip().lstrip("./")
        for operation in ops
        if isinstance(operation, dict)
        and str(operation.get("op") or "")
        in {"write_file", "append_file", "replace_in_file"}
        and str(operation.get("path") or "").strip()
    ]
    material_paths = list(dict.fromkeys(op_paths or expected_files))
    original_verification = str(original.get("verification") or "").strip()
    verifier = (
        _python_exists_verification_command(material_paths)
        if material_paths
        else original_verification
    )
    if not (ops or commands) or not verifier:
        return None

    implementation_step: dict[str, Any] = {
        "step_number": 2,
        "description": str(original.get("description") or "Apply requested change"),
        "commands": commands,
        "verification": verifier,
        "rollback": original.get("rollback"),
        "expected_files": material_paths,
    }
    if ops:
        implementation_step["ops"] = ops

    return [
        {
            "step_number": 1,
            "description": "Inspect the current workspace",
            "commands": ["rg --files . | sort"],
            "verification": 'python -c "import sys; sys.exit(0)"',
            "rollback": None,
            "expected_files": [],
        },
        implementation_step,
        {
            "step_number": 3,
            "description": "Verify the requested change",
            "commands": [verifier],
            "verification": verifier,
            "rollback": None,
            "expected_files": [],
        },
    ]
