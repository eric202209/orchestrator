"""Deterministic context shaping helpers for orchestration phases."""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from app.services.prompt_templates import PromptTemplates, StepResult


_IGNORED_PARTS = {"node_modules", ".openclaw", "__pycache__", ".git", "dist", "build"}


def _trim_text(text: Any, max_chars: int) -> str:
    value = " ".join(str(text or "").split())
    if len(value) <= max_chars:
        return value
    return value[: max_chars - 3].rstrip() + "..."


def _trim_list(items: Iterable[str], max_items: int, max_chars: int) -> List[str]:
    lines: List[str] = []
    total_chars = 0
    for item in items:
        if len(lines) >= max_items:
            break
        rendered = str(item or "").strip()
        if not rendered:
            continue
        rendered = _trim_text(
            rendered, max_chars=max(32, max_chars // max(1, max_items))
        )
        if total_chars + len(rendered) > max_chars and lines:
            break
        lines.append(rendered)
        total_chars += len(rendered)
    return lines


def collect_workspace_inventory_paths(
    project_dir: Path,
    *,
    max_files: int = 80,
) -> List[str]:
    existing_files: List[str] = []
    if not project_dir.exists():
        return existing_files

    for path in sorted(project_dir.rglob("*")):
        if not path.is_file():
            continue
        relative = path.relative_to(project_dir)
        if any(part in _IGNORED_PARTS for part in relative.parts):
            continue
        existing_files.append(str(relative))
        if len(existing_files) >= max_files:
            break
    return existing_files


def build_workspace_inventory_summary(
    project_dir: Path,
    *,
    workspace_review: Optional[Dict[str, Any]] = None,
    expected_files: Optional[Iterable[str]] = None,
    max_files: int = 60,
) -> str:
    inventory = collect_workspace_inventory_paths(project_dir, max_files=max_files)
    lines: List[str] = []

    if workspace_review:
        file_count = int(workspace_review.get("file_count") or 0)
        source_file_count = int(workspace_review.get("source_file_count") or 0)
        placeholder_issue_count = int(
            workspace_review.get("placeholder_issue_count") or 0
        )
        if file_count or source_file_count or placeholder_issue_count:
            lines.append(
                "Workspace review:"
                f" files={file_count}, source_files={source_file_count},"
                f" placeholder_issues={placeholder_issue_count}"
            )
        review_summary = _trim_text(workspace_review.get("summary") or "", 600)
        if review_summary:
            lines.append(f"Workspace review notes: {review_summary}")

    if inventory:
        lines.append("Current workspace inventory:")
        lines.extend(f"- {path}" for path in inventory)
    else:
        lines.append("Current workspace inventory: no tracked files detected yet.")

    trimmed_expected = _trim_list(
        [
            str(path or "").strip()
            for path in (expected_files or [])
            if str(path or "").strip()
        ],
        max_items=20,
        max_chars=900,
    )
    if trimmed_expected:
        lines.append("Expected file delta:")
        lines.extend(f"- {path}" for path in trimmed_expected)

    return "\n".join(lines)


def _condense_step_results(
    execution_results: Iterable[StepResult],
    *,
    max_entries: int = 4,
    max_chars: int = 900,
) -> str:
    lines: List[str] = []
    for result in list(execution_results)[-max_entries:]:
        files = ", ".join((result.files_changed or [])[:4])
        line = (
            f"step={result.step_number} verdict={result.status}"
            f" files=[{files}]"
            f" note={_trim_text(result.output or result.error_message, 160)}"
        )
        lines.append(line)
    rendered = "\n".join(lines) or "No steps completed yet."
    return _trim_text(rendered, max_chars)


def _condense_dict_events(
    events: Iterable[Dict[str, Any]],
    *,
    max_entries: int = 4,
    max_chars: int = 800,
) -> str:
    lines: List[str] = []
    for item in list(events)[-max_entries:]:
        phase = item.get("phase") or item.get("stage") or item.get("attempt") or "event"
        status = item.get("status") or item.get("verdict") or ""
        reason = item.get("message") or item.get("reason") or item.get("error") or ""
        files_touched = item.get("files_touched") or item.get("files_changed") or []
        files_summary = ", ".join(str(path) for path in list(files_touched)[:4])
        line = (
            f"{phase}:{status} "
            f"reason={_trim_text(reason, 140)}"
            + (f" files=[{files_summary}]" if files_summary else "")
        ).strip()
        lines.append(line)
    rendered = "\n".join(lines) or "No recent phase/debug history."
    return _trim_text(rendered, max_chars)


def _shape_project_context(
    base_context: str,
    *,
    workspace_summary: str,
    recent_history: str,
    validation_history: str,
    max_chars: int,
) -> str:
    sections = [
        ("Project context", _trim_text(base_context, max_chars // 2)),
        ("Workspace truth", _trim_text(workspace_summary, max_chars // 2)),
        ("Recent orchestration history", _trim_text(recent_history, max_chars // 4)),
        ("Recent validation history", _trim_text(validation_history, max_chars // 4)),
    ]
    parts = [f"{label}:\n{content}" for label, content in sections if content]
    return _trim_text("\n\n".join(parts), max_chars)


def assemble_planning_prompt(ctx: Any, workspace_review: Dict[str, Any]) -> str:
    workspace_summary = build_workspace_inventory_summary(
        Path(ctx.orchestration_state.project_dir),
        workspace_review=workspace_review,
        max_files=50,
    )
    project_context = _shape_project_context(
        ctx.orchestration_state.project_context,
        workspace_summary=workspace_summary,
        recent_history=_condense_dict_events(
            ctx.orchestration_state.phase_history, max_entries=4
        ),
        validation_history=_condense_dict_events(
            ctx.orchestration_state.validation_history, max_entries=3
        ),
        max_chars=2200,
    )
    return PromptTemplates.build_planning_prompt(
        task_description=ctx.prompt,
        project_context=project_context,
        project_dir=str(ctx.orchestration_state.project_dir),
        execution_profile=ctx.execution_profile,
    )


def assemble_execution_prompt(ctx: Any, step: Dict[str, Any]) -> str:
    expected_files = step.get("expected_files", []) or []
    workspace_summary = build_workspace_inventory_summary(
        Path(ctx.orchestration_state.project_dir),
        expected_files=expected_files,
        max_files=40,
    )
    project_context = _shape_project_context(
        ctx.orchestration_state.project_context,
        workspace_summary=workspace_summary,
        recent_history=_condense_dict_events(
            ctx.orchestration_state.phase_history, max_entries=3, max_chars=500
        ),
        validation_history=_condense_dict_events(
            ctx.orchestration_state.validation_history, max_entries=2, max_chars=400
        ),
        max_chars=1500,
    )
    return PromptTemplates.build_execution_prompt(
        step_description=step.get("description", ""),
        step_commands=step.get("commands", []) or [],
        project_dir=str(ctx.orchestration_state.project_dir),
        verification_command=step.get("verification"),
        rollback_command=step.get("rollback"),
        expected_files=expected_files,
        completed_steps_summary=_condense_step_results(
            ctx.orchestration_state.execution_results
        ),
        project_context=project_context,
        execution_profile=ctx.execution_profile,
    )


def assemble_completion_repair_inputs(
    ctx: Any,
    completion_validation: Any,
    *,
    max_inventory_files: int = 80,
) -> Dict[str, str]:
    expected_files = (
        list((completion_validation.details or {}).get("expected_core_files", []) or [])
        if completion_validation
        else []
    )
    workspace_summary = build_workspace_inventory_summary(
        Path(ctx.orchestration_state.project_dir),
        expected_files=expected_files,
        max_files=max_inventory_files,
    )
    project_context = _shape_project_context(
        ctx.orchestration_state.project_context,
        workspace_summary=workspace_summary,
        recent_history=_condense_dict_events(
            ctx.orchestration_state.phase_history, max_entries=4, max_chars=700
        ),
        validation_history=_condense_dict_events(
            ctx.orchestration_state.validation_history, max_entries=3, max_chars=600
        ),
        max_chars=1900,
    )
    return {
        "prior_results_summary": _condense_step_results(
            ctx.orchestration_state.execution_results, max_entries=5, max_chars=1100
        ),
        "project_context": project_context,
        "workspace_inventory": workspace_summary,
    }
