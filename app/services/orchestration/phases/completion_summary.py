"""Task-summary helpers for completion finalization."""

from __future__ import annotations

import asyncio
import os
from typing import Any

from app.services.orchestration.policy import SUMMARY_TIMEOUT_SECONDS
from app.services.orchestration.types import OrchestrationRunContext


def _deterministic_task_summary(orchestration_state: Any) -> str:
    changed_files = list(
        dict.fromkeys(
            path
            for result in (getattr(orchestration_state, "execution_results", []) or [])
            for path in (getattr(result, "files_changed", []) or [])
            if str(path).strip()
        )
    )
    completed_steps = sum(
        1
        for result in (getattr(orchestration_state, "execution_results", []) or [])
        if getattr(result, "status", "") == "completed"
    )
    total_steps = len(getattr(orchestration_state, "plan", []) or [])
    file_summary = ", ".join(changed_files[:10]) if changed_files else "none recorded"
    return (
        "Task completed with verified execution evidence. "
        f"Completed steps: {completed_steps}/{total_steps}. "
        f"Changed files: {file_summary}."
    )


def _generate_task_summary_with_fallback(
    *,
    ctx: OrchestrationRunContext,
    summary_prompt: str,
) -> dict[str, Any]:
    if os.getenv("ORCHESTRATOR_GENERATE_LLM_TASK_SUMMARY", "").lower() not in {
        "1",
        "true",
        "yes",
    }:
        return {
            "status": "completed",
            "output": _deterministic_task_summary(ctx.orchestration_state),
            "fallback": True,
            "source": "deterministic",
        }

    try:
        summary_result = asyncio.run(
            ctx.runtime_service.execute_task(
                summary_prompt, timeout_seconds=SUMMARY_TIMEOUT_SECONDS
            )
        )
    except Exception as exc:
        fallback_summary = _deterministic_task_summary(ctx.orchestration_state)
        ctx.emit_live(
            "WARN",
            "[ORCHESTRATION] Task summary generation failed; using deterministic completion summary",
            metadata={
                "phase": "task_summary",
                "reason": "summary_generation_failed",
                "error": str(exc)[:500],
                "timeout_seconds": SUMMARY_TIMEOUT_SECONDS,
            },
        )
        return {
            "status": "completed",
            "output": fallback_summary,
            "fallback": True,
            "error": str(exc)[:500],
        }

    if not isinstance(summary_result, dict):
        return {
            "status": "completed",
            "output": _deterministic_task_summary(ctx.orchestration_state),
            "fallback": True,
            "error": "summary_result_not_dict",
        }
    if not str(summary_result.get("output") or "").strip():
        summary_result = dict(summary_result)
        summary_result["output"] = _deterministic_task_summary(ctx.orchestration_state)
        summary_result["fallback"] = True
        summary_result.setdefault("status", "completed")
    return summary_result
