"""Core-invariant plan schema and step-sequence rules.

Moved from validator.py in Phase 20M (validator rule split). Functions here
cover structural plan schema validation, required step fields, consecutive step
numbering, read-only review probe checks, and workflow phase sequencing.
"""

from __future__ import annotations

import re
import shlex
from pathlib import Path
from typing import Any, Dict, List, Optional

from app.services.orchestration.operations.file_ops_contract import (
    operation_has_file_op_path,
    validate_file_op_shape,
)
from app.services.orchestration.workflow_profiles import (
    get_workflow_markers,
    get_workflow_phases,
)

from .core_file_ops import (
    _file_op_alias_issue,
    _nested_file_op_issue,
    _replace_in_file_has_repairable_old_text_issue,
)


def validate_plan_schema(plan: Any) -> Dict[str, Any]:
    """Validate the structural schema of a plan independently of heuristics."""

    errors: List[str] = []
    details: Dict[str, Any] = {}
    if not isinstance(plan, list):
        return {
            "valid": False,
            "errors": ["Plan payload must be a list of step objects"],
            "details": {"received_type": type(plan).__name__},
        }

    non_dict_steps: List[int] = []
    invalid_step_numbers: List[int] = []
    invalid_descriptions: List[int] = []
    invalid_commands: List[int] = []
    invalid_verification: List[int] = []
    invalid_rollback: List[int] = []
    invalid_expected_files: List[int] = []
    invalid_ops: List[int] = []
    invalid_file_op_aliases: Dict[int, List[str]] = {}
    invalid_nested_file_ops: Dict[int, List[str]] = {}
    missing_required_fields: Dict[int, List[str]] = {}
    extra_fields: Dict[int, List[str]] = {}
    required_fields = {
        "step_number",
        "description",
        "commands",
        "verification",
        "rollback",
        "expected_files",
    }
    allowed_fields = set(required_fields)
    allowed_fields.add("ops")

    for index, step in enumerate(plan, start=1):
        if not isinstance(step, dict):
            non_dict_steps.append(index)
            continue
        missing_fields = sorted(required_fields.difference(step.keys()))
        if missing_fields:
            missing_required_fields[index] = missing_fields
        extras = sorted(set(step.keys()).difference(allowed_fields))
        if extras:
            extra_fields[index] = extras
        if not isinstance(step.get("step_number"), int):
            invalid_step_numbers.append(index)
        if not isinstance(step.get("description", ""), str):
            invalid_descriptions.append(index)
        commands = step.get("commands", [])
        if not isinstance(commands, list) or any(
            not isinstance(command, str) for command in commands
        ):
            invalid_commands.append(index)
        verification = step.get("verification")
        if verification is not None and not isinstance(verification, str):
            invalid_verification.append(index)
        rollback = step.get("rollback")
        if rollback is not None and not isinstance(rollback, str):
            invalid_rollback.append(index)
        expected_files = step.get("expected_files", [])
        if expected_files is not None and (
            not isinstance(expected_files, list)
            or any(not isinstance(path, str) for path in expected_files)
        ):
            invalid_expected_files.append(index)
        ops = step.get("ops", [])
        if ops is not None:
            if not isinstance(ops, list):
                invalid_ops.append(index)
            else:
                for op_index, operation in enumerate(ops, start=1):
                    alias_issue = _file_op_alias_issue(operation)
                    if alias_issue:
                        invalid_file_op_aliases.setdefault(index, []).append(
                            f"op {op_index}: {alias_issue}"
                        )
                    nested_issue = _nested_file_op_issue(operation)
                    if nested_issue:
                        invalid_nested_file_ops.setdefault(index, []).append(
                            f"op {op_index}: {nested_issue}"
                        )
                    if not validate_file_op_shape(
                        operation
                    ) and not _replace_in_file_has_repairable_old_text_issue(operation):
                        invalid_ops.append(index)
                        break

    if non_dict_steps:
        errors.append("Plan contains non-object steps")
        details["non_dict_steps"] = non_dict_steps
    if invalid_step_numbers:
        errors.append("Plan steps must define integer step_number values")
        details["invalid_step_number_steps"] = invalid_step_numbers
    if invalid_descriptions:
        errors.append("Plan step descriptions must be strings")
        details["invalid_description_steps"] = invalid_descriptions
    if invalid_commands:
        errors.append("Plan step commands must be arrays of strings")
        details["invalid_commands_steps"] = invalid_commands
    if missing_required_fields:
        errors.append(
            "Plan steps must include step_number, description, commands, verification, rollback, and expected_files"
        )
        details["missing_required_fields"] = missing_required_fields
    if extra_fields:
        errors.append("Plan steps must not include extra keys")
        details["extra_fields"] = extra_fields
    if invalid_verification:
        errors.append("Plan step verification values must be strings or null")
        details["invalid_verification_steps"] = invalid_verification
    if invalid_rollback:
        errors.append("Plan step rollback values must be strings or null")
        details["invalid_rollback_steps"] = invalid_rollback
    if invalid_expected_files:
        errors.append("Plan expected_files must be arrays of strings")
        details["invalid_expected_files_steps"] = invalid_expected_files
    if invalid_ops:
        errors.append(
            "Plan ops must be arrays of supported operation objects with valid string fields"
        )
        details["invalid_ops_steps"] = sorted(set(invalid_ops))
    if invalid_file_op_aliases:
        errors.append("Plan contains invalid_file_op_alias entries")
        details["invalid_file_op_alias"] = invalid_file_op_aliases
    if invalid_nested_file_ops:
        errors.append("Plan contains invalid_nested_file_op entries")
        details["invalid_nested_file_ops"] = invalid_nested_file_ops

    return {"valid": not errors, "errors": errors, "details": details}


def _plan_missing_required_fields(
    plan: List[Dict[str, Any]],
) -> Dict[str, List[int]]:
    missing_description: List[int] = []
    missing_commands: List[int] = []

    for index, step in enumerate(plan, start=1):
        step_number = step.get("step_number", index)
        if not str(step.get("description") or "").strip():
            missing_description.append(step_number)

        commands = step.get("commands", [])
        ops = step.get("ops", [])
        has_file_ops = isinstance(ops, list) and any(
            operation_has_file_op_path(operation) for operation in ops
        )
        if not isinstance(commands, list) or (
            not any(str(command or "").strip() for command in commands)
            and not has_file_ops
        ):
            missing_commands.append(step_number)

    return {
        "missing_description_steps": missing_description,
        "missing_commands_steps": missing_commands,
    }


def _plan_has_invalid_step_sequence(plan: List[Dict[str, Any]]) -> bool:
    step_numbers = [step.get("step_number") for step in plan]
    if not all(isinstance(step_number, int) for step_number in step_numbers):
        return True
    return step_numbers != list(range(1, len(plan) + 1))


def _plan_failable_review_probe_steps(
    plan: List[Dict[str, Any]], workflow_stage: Optional[str]
) -> List[int]:
    if workflow_stage != "review":
        return []

    findings: List[int] = []
    for index, step in enumerate(plan, start=1):
        step_number = int(step.get("step_number", index))
        commands = [str(command or "") for command in step.get("commands", []) or []]
        verification = str(step.get("verification") or "")
        for command in commands + ([verification] if verification else []):
            command_text = command.strip()
            if not command_text:
                continue
            try:
                tokens = shlex.split(command_text, posix=True)
            except ValueError:
                tokens = command_text.split()
            command_name = Path(tokens[0]).name if tokens else ""
            if command_name != "grep":
                continue
            if re.search(r"(\|\|\s*true|\|\|\s*echo|\bif\s+grep\b)", command_text):
                continue
            findings.append(step_number)
            break
    return findings


def _infer_workflow_phase_for_step(
    step: Dict[str, Any], workflow_profile: Optional[str]
) -> Optional[str]:
    if workflow_profile != "fullstack_scaffold":
        return None

    text = " ".join(
        [
            str(step.get("description") or ""),
            str(step.get("verification") or ""),
            str(step.get("rollback") or ""),
        ]
        + [str(command or "") for command in step.get("commands", []) or []]
        + [str(path or "") for path in step.get("expected_files", []) or []]
    ).lower()
    marker_groups = get_workflow_markers(workflow_profile)
    frontend_markers = marker_groups.get("frontend") or []
    backend_markers = marker_groups.get("backend") or []
    wire_api_config_markers = marker_groups.get("wire_api_config") or []
    verify_dev_startup_markers = marker_groups.get("verify_dev_startup") or []
    frontend_exclusions = marker_groups.get("frontend_skeleton_exclusions") or []
    backend_exclusions = marker_groups.get("backend_skeleton_exclusions") or []

    has_frontend_markers = any(marker in text for marker in frontend_markers)
    has_backend_markers = any(marker in text for marker in backend_markers)

    if any(marker in text for marker in wire_api_config_markers):
        return "wire_api_config"

    if has_frontend_markers and not any(
        marker in text for marker in frontend_exclusions
    ):
        return "create_frontend_skeleton"

    if has_backend_markers and not any(marker in text for marker in backend_exclusions):
        return "create_backend_skeleton"

    if any(marker in text for marker in verify_dev_startup_markers):
        return "verify_dev_startup"

    if has_frontend_markers:
        return "create_frontend_skeleton"
    if has_backend_markers:
        return "create_backend_skeleton"

    return None


def _workflow_phase_order_violations(
    plan: List[Dict[str, Any]],
    workflow_profile: Optional[str],
) -> Dict[str, Any]:
    if workflow_profile != "fullstack_scaffold":
        return {}

    phase_order = get_workflow_phases(workflow_profile or "")
    if not phase_order:
        return {}

    phase_positions = {phase: idx for idx, phase in enumerate(phase_order)}
    seen_sequence: List[Dict[str, Any]] = []
    last_position = -1
    violating_steps: List[int] = []

    for index, step in enumerate(plan, start=1):
        phase = _infer_workflow_phase_for_step(step, workflow_profile)
        if not phase:
            continue
        step_number = int(step.get("step_number", index))
        position = phase_positions[phase]
        seen_sequence.append({"step_number": step_number, "phase": phase})
        if position < last_position:
            violating_steps.append(step_number)
        else:
            last_position = position

    missing_phases = [
        phase
        for phase in phase_order
        if phase not in {entry["phase"] for entry in seen_sequence}
    ]
    return {
        "phase_sequence": seen_sequence,
        "violating_steps": violating_steps,
        "missing_phases": missing_phases,
    }
