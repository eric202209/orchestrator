"""Task-1 bootstrap helpers for planning flow."""

from __future__ import annotations

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
