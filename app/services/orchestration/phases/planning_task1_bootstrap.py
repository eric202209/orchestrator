"""Task-1 bootstrap helpers for planning flow."""

from __future__ import annotations

import json
import shlex
from typing import Any

from app.services.orchestration.types import OrchestrationRunContext


def is_first_ordered_task(task: Any) -> bool:
    return getattr(task, "plan_position", None) == 1


def emit_task1_bootstrap_contract_event(
    ctx: OrchestrationRunContext,
    plan_verdict: Any,
) -> None:
    contract = (getattr(plan_verdict, "details", None) or {}).get(
        "task1_bootstrap_contract"
    )
    if not contract or not is_first_ordered_task(ctx.task):
        return
    passed = bool(contract.get("passed"))
    event_type = (
        "task1_bootstrap_contract_passed"
        if passed
        else "task1_bootstrap_contract_failed"
    )
    ctx.emit_live(
        "INFO" if passed else "WARN",
        (
            "[ORCHESTRATION] Task 1 bootstrap contract passed"
            if passed
            else "[ORCHESTRATION] Task 1 bootstrap contract failed"
        ),
        metadata={
            "event_type": event_type,
            "phase": "planning",
            "task1_bootstrap_contract": contract,
        },
    )


def task1_bootstrap_contract_passed(plan_verdict: Any) -> bool:
    contract = (getattr(plan_verdict, "details", None) or {}).get(
        "task1_bootstrap_contract"
    )
    return bool(contract and contract.get("passed"))


def task1_plan_failed_only_brittle_command_shape(plan_verdict: Any) -> bool:
    details = getattr(plan_verdict, "details", None) or {}
    reasons = list(getattr(plan_verdict, "reasons", None) or [])
    if reasons != ["Plan contains brittle heredoc-heavy or malformed commands"]:
        return False
    semantic_codes = set(details.get("semantic_violation_codes") or [])
    return not any(str(code).startswith("task1_bootstrap_") for code in semantic_codes)


def normalize_task1_bootstrap_plan_for_json_stability(
    plan: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    normalized: list[dict[str, Any]] = []
    for step in plan:
        updated = dict(step)
        ops = [
            operation
            for operation in (updated.get("ops") or [])
            if isinstance(operation, dict)
            and str(operation.get("op") or "") in {"write_file", "append_file"}
        ]
        if ops:
            updated["commands"] = []
        normalized.append(updated)
    return normalized


def normalize_task1_python_src_layout_verification(
    plan: list[dict[str, Any]],
    plan_verdict: Any,
) -> list[dict[str, Any]]:
    contract = (getattr(plan_verdict, "details", None) or {}).get(
        "task1_bootstrap_contract"
    )
    if not contract or not contract.get("python_package_markers"):
        return plan

    has_pytest_config = _plan_has_path(
        plan, {"pytest.ini", "pyproject.toml", "setup.cfg"}
    )
    normalized: list[dict[str, Any]] = []
    for index, step in enumerate(plan):
        updated = dict(step)
        if index == 0 and not has_pytest_config:
            updated = _with_src_layout_pytest_config(updated)
        verification = _src_layout_verification_command(
            updated.get("verification"),
            contract.get("python_import_targets") or [],
        )
        if verification != updated.get("verification"):
            updated["verification"] = verification
        normalized.append(updated)
    return normalized


def _plan_has_path(plan: list[dict[str, Any]], paths: set[str]) -> bool:
    for step in plan:
        if any(path in paths for path in step.get("expected_files") or []):
            return True
        for operation in step.get("ops") or []:
            if isinstance(operation, dict) and operation.get("path") in paths:
                return True
    return False


def _with_src_layout_pytest_config(step: dict[str, Any]) -> dict[str, Any]:
    updated = dict(step)
    updated["expected_files"] = list(updated.get("expected_files") or []) + [
        "pytest.ini"
    ]
    updated["ops"] = list(updated.get("ops") or []) + [
        {
            "op": "write_file",
            "path": "pytest.ini",
            "content": "[pytest]\npythonpath = src\n",
        }
    ]
    return updated


def _src_layout_verification_command(command: Any, import_targets: list[str]) -> Any:
    text = str(command or "").strip()
    if not text:
        return command
    if "sys.path.insert(0, 'src')" in text or 'sys.path.insert(0, "src")' in text:
        return command

    if text.startswith(("pytest", "python -m pytest", "python3 -m pytest")):
        script = (
            "import sys,pytest; "
            "sys.path.insert(0, 'src'); "
            "raise SystemExit(pytest.main(['-q']))"
        )
        return "python -c " + json.dumps(script)

    if not text.startswith(("python -c ", "python3 -c ")):
        return command

    try:
        parts = shlex.split(text)
    except ValueError:
        return command
    if len(parts) < 3 or parts[1] != "-c":
        return command
    script = parts[2]
    if import_targets and not any(target in script for target in import_targets):
        return command
    script = "import sys; sys.path.insert(0, 'src'); " + script
    return f"{parts[0]} -c " + json.dumps(script)
