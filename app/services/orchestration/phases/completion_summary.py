"""Task-summary helpers for completion finalization."""

from __future__ import annotations

import ast
import asyncio
import os
from pathlib import Path
from typing import Any

import httpx

from app.config import settings
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


async def _call_planning_lane(prompt: str) -> str:
    """Direct HTTP chat completion to the planning lane.

    Uses settings.PLANNING_REPAIR_BASE_URL / MODEL — the same endpoint the
    planning lane uses — so this works regardless of deployment configuration.
    """
    base_url = settings.PLANNING_REPAIR_BASE_URL.rstrip("/")
    model = (settings.PLANNING_REPAIR_MODEL or "").strip() or "qwen-local"
    api_key = (settings.PLANNING_REPAIR_API_KEY or "").strip()
    headers: dict[str, str] = {"Content-Type": "application/json"}
    if api_key:
        headers["Authorization"] = f"Bearer {api_key}"
    payload = {
        "model": model,
        "messages": [{"role": "user", "content": prompt}],
        "temperature": 0.0,
        "max_tokens": 512,
        "stream": False,
        "think": False,
        "enable_thinking": False,
        "chat_template_kwargs": {"enable_thinking": False},
    }
    async with httpx.AsyncClient(timeout=float(SUMMARY_TIMEOUT_SECONDS)) as client:
        resp = await client.post(
            f"{base_url}/chat/completions", json=payload, headers=headers
        )
    resp.raise_for_status()
    body = resp.json()
    choices = body.get("choices") or []
    if not choices:
        return ""
    message = choices[0].get("message") or {}
    content = message.get("content") or ""
    if isinstance(content, list):
        content = "".join(
            item.get("text", "") for item in content if isinstance(item, dict)
        )
    return str(content).strip()


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
                _call_planning_lane(summary_prompt),
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
