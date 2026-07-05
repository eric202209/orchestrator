"""Task-1 bootstrap helpers for planning flow."""

from __future__ import annotations

import json
import shlex
from typing import Any, Callable

from app.services.orchestration.events.telemetry import emit_phase_event
from app.services.orchestration.types import OrchestrationRunContext
from app.services.orchestration.validation.validator import ValidatorService
from app.services.orchestration.validation.workspace_checks import (
    extract_inline_python_dash_c_script,
    uses_brittle_python_inline_command,
)


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


def plan_has_brittle_inline_python_verification(plan_verdict: Any) -> bool:
    details = getattr(plan_verdict, "details", None) or {}
    subcodes = set(details.get("brittle_command_subcodes") or [])
    return "brittle_inline_python" in subcodes


def normalize_task1_brittle_inline_python_verification(
    plan: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Rewrite brittle `python -c "..."` commands into an ops-written script.

    Resolves the Phase 18N contradiction where `task1_bootstrap_contract`
    carries a first task's literal verification command forward into repair
    prompts while `brittle_commands` simultaneously rejects that same
    nested-quote inline command, giving the repair loop no satisfiable
    target. Writing the script to a file and invoking it plainly preserves
    the exact verification semantics while satisfying both rules.
    """

    normalized: list[dict[str, Any]] = []
    for index, step in enumerate(plan, start=1):
        updated = dict(step)
        ops = list(updated.get("ops") or [])
        rewritten_by_original: dict[str, str] = {}
        changed = False

        def _rewrite(raw_command: str) -> str | None:
            if raw_command in rewritten_by_original:
                return rewritten_by_original[raw_command]
            if not uses_brittle_python_inline_command(raw_command):
                return None
            script = extract_inline_python_dash_c_script(raw_command)
            if script is None:
                return None
            interpreter = (
                "python3"
                if raw_command.strip().lower().startswith("python3")
                else "python"
            )
            script_path = f"verify_task1_step{index}.py"
            ops.append(
                {
                    "op": "write_file",
                    "path": script_path,
                    "content": script + "\n",
                }
            )
            rewritten = f"{interpreter} {script_path}"
            rewritten_by_original[raw_command] = rewritten
            return rewritten

        new_commands = []
        for command in updated.get("commands") or []:
            rewritten = _rewrite(str(command or ""))
            if rewritten is not None:
                new_commands.append(rewritten)
                changed = True
            else:
                new_commands.append(command)

        verification = str(updated.get("verification") or "")
        new_verification: Any = updated.get("verification")
        if verification:
            rewritten = _rewrite(verification)
            if rewritten is not None:
                new_verification = rewritten
                changed = True

        if changed:
            updated["commands"] = new_commands
            updated["verification"] = new_verification
            updated["ops"] = ops
        normalized.append(updated)
    return normalized


def reconcile_task1_bootstrap_plan(
    ctx: OrchestrationRunContext,
    *,
    normalize: Callable[[list[dict[str, Any]]], list[dict[str, Any]]],
    reason: str,
    message: str,
) -> Any | None:
    """Normalize + re-validate a first-task plan; None if normalize was a no-op.

    Shared by every Task-1 bootstrap plan-normalization pass (JSON-stability,
    src-layout verification, brittle-inline-Python rewrite) so planning_flow.py
    only needs a short guarded call instead of inlining the normalize/emit/
    re-validate sequence at each call site.
    """

    normalized_plan = normalize(ctx.orchestration_state.plan)
    if normalized_plan == ctx.orchestration_state.plan:
        return None

    emit_phase_event(
        ctx.orchestration_state,
        ctx.emit_live,
        level="INFO",
        phase="planning",
        message=message,
        details={"reason": reason, "step_count": len(normalized_plan)},
    )
    ctx.orchestration_state.plan = normalized_plan
    return ValidatorService.validate_plan(
        ctx.orchestration_state.plan,
        output_text=json.dumps(normalized_plan),
        task_prompt=ctx.prompt,
        execution_profile=ctx.execution_profile,
        project_dir=ctx.orchestration_state.project_dir,
        title=ctx.task.title if ctx.task else None,
        description=ctx.task.description if ctx.task else None,
        validation_severity=ctx.validation_severity,
        workflow_profile=ctx.workflow_profile,
        workflow_stage=ctx.workflow_stage,
        is_first_ordered_task=is_first_ordered_task(ctx.task),
    )


def apply_task1_brittle_inline_python_normalization(
    ctx: OrchestrationRunContext,
    plan_verdict: Any,
) -> Any | None:
    return reconcile_task1_bootstrap_plan(
        ctx,
        normalize=normalize_task1_brittle_inline_python_verification,
        reason="task1_bootstrap_brittle_inline_python_normalized",
        message=(
            "[ORCHESTRATION] Normalized Task 1 bootstrap plan by rewriting "
            "brittle inline Python verification into an ops-written script"
        ),
    )


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
