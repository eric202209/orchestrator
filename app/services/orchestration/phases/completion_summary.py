"""Task-summary helpers for completion finalization."""

from __future__ import annotations

import ast
import asyncio
import os
from pathlib import Path
from typing import Any

from app.services.agents.runtime_invocation import RuntimeInvocationOptions
from app.services.orchestration.policy import SUMMARY_TIMEOUT_SECONDS
from app.services.orchestration.types import OrchestrationRunContext

_EVIDENCE_MAX_CHARS = 1500
_EVIDENCE_MAX_FUNCS = 20
_EVIDENCE_MAX_CLASSES = 10


def _extract_python_symbols(file_path: Path) -> tuple[list[str], list[str]]:
    """Return (functions, classes) defined at the top level of a Python source file."""
    try:
        source = file_path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(source, filename=str(file_path))
    except Exception:
        return [], []
    functions = [
        node.name
        for node in tree.body
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))
    ]
    classes = [node.name for node in tree.body if isinstance(node, ast.ClassDef)]
    return functions, classes


def build_workspace_evidence_block(
    changed_files: list[str],
    project_dir: str | Path,
    *,
    max_chars: int = _EVIDENCE_MAX_CHARS,
) -> str:
    """Build a compact workspace evidence block listing confirmed symbols per file.

    For Python files, lists top-level functions and classes actually present in
    the file on disk.  Non-Python files are listed by path only.  Missing files
    are noted so the LLM knows the evidence is incomplete.
    """
    root = Path(project_dir)
    lines: list[str] = []
    total_chars = 0

    for rel_path in changed_files:
        if total_chars >= max_chars:
            break
        p = Path(rel_path)
        abs_path = p if p.is_absolute() else root / p

        if not abs_path.exists():
            entry = f"- {rel_path} (not found in workspace)"
            lines.append(entry)
            total_chars += len(entry)
            continue

        if abs_path.suffix == ".py":
            functions, classes = _extract_python_symbols(abs_path)
            func_str = (
                ", ".join(functions[:_EVIDENCE_MAX_FUNCS]) if functions else "N/A"
            )
            cls_str = ", ".join(classes[:_EVIDENCE_MAX_CLASSES]) if classes else "N/A"
            entry = f"- {rel_path}\n  functions: {func_str}\n  classes: {cls_str}"
        else:
            entry = f"- {rel_path}"

        lines.append(entry)
        total_chars += len(entry)

    if not lines:
        return "(no changed files recorded)"
    return "\n".join(lines)


async def _call_planning_lane(
    prompt: str, *, db: Any = None, owner_runtime: Any = None
) -> str:
    """Invoke the completion-summary repair lane through the role registry.

    The summary is Task-scoped: its canonical owner is the attempt whose
    runtime (``owner_runtime``) produced the completion, never the most
    recent TaskExecution and never a separate budget.
    """

    if db is None:
        raise RuntimeError(
            "Completion summary requires a database-backed repair runtime"
        )
    from app.services.agents.agent_runtime import BackendRole, create_agent_runtime
    from app.services.execution.workflow_lifecycle import propagate_workflow_identity

    runtime = create_agent_runtime(
        db, session_id=None, task_id=None, role=BackendRole.REPAIR
    )
    propagate_workflow_identity(owner_runtime, runtime, BackendRole.REPAIR)
    result = await runtime.invoke_prompt(
        prompt,
        timeout_seconds=SUMMARY_TIMEOUT_SECONDS,
        source_brain="local",
        session_prefix="completion-summary",
        invocation_options=RuntimeInvocationOptions(
            timeout_seconds=float(SUMMARY_TIMEOUT_SECONDS),
            max_output_tokens=512,
            temperature=0.0,
            reasoning_enabled=False,
            stream=False,
        ),
    )
    return str(result.get("output") or "").strip()


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
        det = _deterministic_task_summary(ctx.orchestration_state)
        return {
            "status": "completed",
            "output": det,
            "pn_summary": det,
            "fallback": True,
            "source": "deterministic",
        }

    try:
        output = asyncio.run(
            asyncio.wait_for(
                _call_planning_lane(
                    summary_prompt,
                    db=ctx.db,
                    owner_runtime=getattr(ctx, "runtime_service", None),
                ),
                timeout=float(SUMMARY_TIMEOUT_SECONDS),
            )
        )
        summary_result = {"status": "completed", "output": output}
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
            "pn_summary": fallback_summary,
            "fallback": True,
            "error": str(exc)[:500],
        }

    if not isinstance(summary_result, dict):
        det = _deterministic_task_summary(ctx.orchestration_state)
        return {
            "status": "completed",
            "output": det,
            "pn_summary": det,
            "fallback": True,
            "error": "summary_result_not_dict",
        }
    if not str(summary_result.get("output") or "").strip():
        summary_result = dict(summary_result)
        det = _deterministic_task_summary(ctx.orchestration_state)
        summary_result["output"] = det
        summary_result["pn_summary"] = det
        summary_result["fallback"] = True
        summary_result.setdefault("status", "completed")
    else:
        # LLM produced content: WM gets LLM output; progress_notes gets deterministic.
        summary_result["pn_summary"] = _deterministic_task_summary(
            ctx.orchestration_state
        )
    return summary_result
