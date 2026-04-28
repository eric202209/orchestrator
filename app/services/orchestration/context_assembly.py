"""Deterministic context shaping helpers for orchestration phases."""

from __future__ import annotations

import json
import re
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from app.services.model_adaptation import render_prompt_for_profile
from app.services.model_adaptation.schemas import PromptEnvelope
from app.services.prompt_templates import PromptTemplates, StepResult
from app.services.workspace.system_settings import get_effective_adaptation_profile


_IGNORED_PARTS = {"node_modules", ".openclaw", "__pycache__", ".git", "dist", "build"}
_PATH_TOKEN_RE = re.compile(
    r"(?<![\w./-])([A-Za-z0-9_.-]+(?:/[A-Za-z0-9_.-]+)+\.[A-Za-z0-9_.-]+)(?![\w./-])"
)


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


def identify_stale_path_references(
    text: str,
    project_dir: Path,
    *,
    max_files: int = 300,
    max_items: int = 20,
) -> List[str]:
    inventory = {
        path
        for path in collect_workspace_inventory_paths(project_dir, max_files=max_files)
        if path
    }
    stale_paths: List[str] = []
    seen: set[str] = set()

    for match in _PATH_TOKEN_RE.finditer(str(text or "")):
        raw_token = match.group(1).strip().strip("`'\".,:;()[]{}")
        if not raw_token:
            continue
        normalized = raw_token.replace("\\", "/").lstrip("./")
        if not normalized or normalized in seen:
            continue
        if normalized.startswith("/") or normalized.startswith(".."):
            continue
        if any(part in _IGNORED_PARTS for part in Path(normalized).parts):
            continue
        if normalized in inventory:
            continue
        seen.add(normalized)
        stale_paths.append(normalized)
        if len(stale_paths) >= max_items:
            break

    return stale_paths


def sanitize_progress_notes_for_workspace(
    notes_text: str,
    project_dir: Path,
    *,
    max_files: int = 300,
    max_chars: int = 8000,
) -> str:
    stale_paths = identify_stale_path_references(
        notes_text,
        project_dir,
        max_files=max_files,
    )
    stale_set = set(stale_paths)
    kept_lines: List[str] = []
    removed_lines = 0

    for line in str(notes_text or "").splitlines():
        if stale_set and any(path in line for path in stale_set):
            removed_lines += 1
            continue
        kept_lines.append(line)

    sections: List[str] = []
    sanitized_notes = "\n".join(kept_lines).strip()
    if sanitized_notes:
        sections.append(sanitized_notes)
    if stale_paths:
        sections.append(
            "Ignore prior-note file references that are not present in the current workspace:\n"
            + "\n".join(f"- {path}" for path in stale_paths[:12])
        )
    if removed_lines:
        sections.append(
            f"Filtered {removed_lines} stale note line(s) that referenced missing workspace paths."
        )

    rendered = "\n\n".join(section for section in sections if section).strip()
    return _trim_text(rendered, max_chars=max_chars)


def compress_orchestration_context(
    orchestration_state: Any,
    *,
    max_chars: int = 2000,
) -> str:
    """Condense execution state into a compact snapshot for dense-context replanning.

    Produces a short text block summarising step progress, recent failures, and
    debug history so the planner can resume without the full log context.
    """
    plan = getattr(orchestration_state, "plan", []) or []
    step_idx = getattr(orchestration_state, "current_step_index", 0) or 0
    completed = getattr(orchestration_state, "completed_steps", []) or []
    failed = getattr(orchestration_state, "failed_steps", []) or []
    debug_attempts = getattr(orchestration_state, "debug_attempts", []) or []
    changed_files = getattr(orchestration_state, "changed_files", []) or []

    lines: List[str] = [f"Step progress: {step_idx}/{len(plan)}"]
    if completed:
        nums = ", ".join(str(getattr(r, "step_number", "?")) for r in completed[:6])
        lines.append(f"Completed steps: {nums}")
    if failed:
        nums = ", ".join(str(getattr(r, "step_number", "?")) for r in failed[:4])
        lines.append(f"Failed steps: {nums}")
    if debug_attempts:
        last = debug_attempts[-1]
        step_label = last.get("step_index", "?")
        if isinstance(step_label, int):
            step_label = step_label + 1
        lines.append(
            f"Debug attempts: {len(debug_attempts)} total; last was "
            f"{last.get('fix_type', '?')} on step {step_label} — "
            f"{str(last.get('error', ''))[:120]}"
        )
    if changed_files:
        lines.append(f"Changed files: {', '.join(str(f) for f in changed_files[:10])}")

    summary = "\n".join(lines)
    if len(summary) > max_chars:
        summary = summary[: max_chars - 3].rstrip() + "..."
    return summary


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


def render_adapted_runtime_prompt(
    db: Any,
    *,
    objective: str,
    execution_mode: str,
    prompt_body: str,
    instructions: Optional[Iterable[str]] = None,
    context: Optional[Dict[str, Any]] = None,
    expected_output: Optional[str] = None,
) -> str:
    profile_name = get_effective_adaptation_profile(db=db)
    envelope = PromptEnvelope(
        objective=objective,
        execution_mode=execution_mode,
        instructions=[item for item in (instructions or []) if str(item or "").strip()],
        context=context or {},
        expected_output=expected_output,
        prompt_body=prompt_body,
    )
    return render_prompt_for_profile(profile_name, envelope)


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
    raw_prompt = PromptTemplates.build_planning_prompt(
        task_description=ctx.prompt,
        project_context=project_context,
        project_dir=str(ctx.orchestration_state.project_dir),
        execution_profile=ctx.execution_profile,
    )
    return render_adapted_runtime_prompt(
        ctx.db,
        objective="Generate a machine-runnable JSON execution plan for the requested task.",
        execution_mode="planning",
        prompt_body=raw_prompt,
        instructions=[
            "Do not implement anything yet.",
            "Return a sequential JSON plan only.",
        ],
        context={
            "Project Directory": str(ctx.orchestration_state.project_dir),
            "Execution Profile": ctx.execution_profile,
        },
        expected_output="JSON array of orchestration step objects.",
    )


def assemble_execution_prompt(
    ctx: Any, step: Dict[str, Any], *, compact: bool = False
) -> str:
    expected_files = step.get("expected_files", []) or []
    workspace_max_files = 18 if compact else 40
    project_context_max_chars = 700 if compact else 1500
    recent_history_entries = 2 if compact else 3
    recent_history_chars = 260 if compact else 500
    validation_history_entries = 1 if compact else 2
    validation_history_chars = 180 if compact else 400
    instructions = [
        "Treat the provided step commands as the primary implementation plan for this step.",
        "Ground your work in the current workspace state.",
        (
            "If you need human confirmation before continuing, output exactly one "
            "sentinel in this format and stop: "
            '<<<HITL_REQUEST:{"intervention_type":"approval","prompt":"...",'
            '"context":{...}}>>>. Use this for authorization, destructive/risky '
            "actions, missing credentials, or when operator intent is ambiguous."
        ),
    ]
    if compact:
        instructions.append(
            "Keep your reasoning concise and avoid repeating workspace context."
        )

    workspace_summary = build_workspace_inventory_summary(
        Path(ctx.orchestration_state.project_dir),
        expected_files=expected_files,
        max_files=workspace_max_files,
    )
    project_context = _shape_project_context(
        ctx.orchestration_state.project_context,
        workspace_summary=workspace_summary,
        recent_history=_condense_dict_events(
            ctx.orchestration_state.phase_history,
            max_entries=recent_history_entries,
            max_chars=recent_history_chars,
        ),
        validation_history=_condense_dict_events(
            ctx.orchestration_state.validation_history,
            max_entries=validation_history_entries,
            max_chars=validation_history_chars,
        ),
        max_chars=project_context_max_chars,
    )
    raw_prompt = PromptTemplates.build_execution_prompt(
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
    return render_adapted_runtime_prompt(
        ctx.db,
        objective=(
            f"Execute orchestration step {step.get('step_number') or ctx.orchestration_state.current_step_index + 1} "
            "inside the active task workspace."
        ),
        execution_mode="step_execution",
        prompt_body=raw_prompt,
        instructions=instructions,
        context={
            "Project Directory": str(ctx.orchestration_state.project_dir),
            "Verification Command": step.get("verification"),
            "Rollback Command": step.get("rollback"),
            "Expected Files": expected_files,
            "Execution Profile": ctx.execution_profile,
            "Compact Retry": compact,
        },
        expected_output=(
            "Structured step result describing status, output, verification_output, "
            "files_changed, and any error details."
        ),
    )


def assemble_debugging_prompt(
    ctx: Any,
    *,
    step_description: str,
    error_message: str,
    command_output: str,
    verification_output: str,
    attempt_number: int,
    max_attempts: int,
    compact: bool = False,
) -> str:
    raw_prompt = PromptTemplates.build_debugging_prompt(
        step_description=step_description,
        error_message=error_message,
        command_output=command_output,
        verification_output=verification_output,
        attempt_number=attempt_number,
        max_attempts=max_attempts,
        prior_debug_attempts=ctx.orchestration_state.debug_attempts,
        project_name=ctx.orchestration_state.project_name,
        workspace_root=str(ctx.orchestration_state.workspace_root),
        project_dir=str(ctx.orchestration_state.project_dir),
        compact=compact,
    )
    return render_adapted_runtime_prompt(
        ctx.db,
        objective="Diagnose the failed orchestration step and return the next repair action.",
        execution_mode="debugging",
        prompt_body=raw_prompt,
        instructions=[
            "Base the diagnosis on the actual failure output and workspace state.",
            "Return machine-parseable structured debugging guidance.",
        ],
        context={
            "Project Directory": str(ctx.orchestration_state.project_dir),
            "Attempt Number": attempt_number,
            "Max Attempts": max_attempts,
            "Compact Retry": compact,
        },
        expected_output=(
            "JSON object with analysis, fix, confidence, and fix_type fields."
        ),
    )


def assemble_plan_revision_prompt(
    ctx: Any,
    *,
    failed_steps: List[StepResult],
    debug_analysis: str,
) -> str:
    raw_prompt = PromptTemplates.build_plan_revision_prompt(
        original_plan=ctx.orchestration_state.plan,
        failed_steps=failed_steps,
        debug_analysis=debug_analysis,
        completed_steps=ctx.orchestration_state.completed_steps,
        workspace_root=str(ctx.orchestration_state.workspace_root),
        project_dir=str(ctx.orchestration_state.project_dir),
    )
    return render_adapted_runtime_prompt(
        ctx.db,
        objective="Revise the remaining orchestration plan without discarding completed work.",
        execution_mode="plan_revision",
        prompt_body=raw_prompt,
        instructions=[
            "Preserve completed steps.",
            "Return only a revised machine-runnable plan payload.",
        ],
        context={
            "Project Directory": str(ctx.orchestration_state.project_dir),
            "Completed Step Count": len(ctx.orchestration_state.completed_steps),
            "Original Plan Length": len(ctx.orchestration_state.plan),
        },
        expected_output="JSON object containing a revised_plan array.",
    )


def assemble_task_summary_prompt(ctx: Any) -> str:
    raw_prompt = PromptTemplates.build_task_summary(
        task_description=ctx.prompt,
        plan_summary=_trim_text(
            json.dumps(ctx.orchestration_state.plan, indent=2), 3000
        ),
        execution_results_summary=ctx.orchestration_state.prior_results_summary(),
        changed_files=ctx.orchestration_state.changed_files,
        num_debug_attempts=len(ctx.orchestration_state.debug_attempts),
        final_status="success",
        execution_profile=ctx.execution_profile,
    )
    return render_adapted_runtime_prompt(
        ctx.db,
        objective="Summarize the completed orchestration task accurately and concisely.",
        execution_mode="task_summary",
        prompt_body=raw_prompt,
        instructions=[
            "Ground the summary in the actual completed steps and changed files.",
            "Do not claim work that was not completed.",
        ],
        context={
            "Execution Profile": ctx.execution_profile,
            "Changed Files Count": len(ctx.orchestration_state.changed_files),
            "Debug Attempt Count": len(ctx.orchestration_state.debug_attempts),
        },
        expected_output="A concise completion summary for operators.",
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
