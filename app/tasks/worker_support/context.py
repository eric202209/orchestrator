"""Context assembly helpers for orchestration worker tasks."""

import json
from pathlib import Path
from typing import Any, Dict, Optional

from sqlalchemy.orm import Session

from app.models import Task
from app.services.orchestration.context.assembly import (
    collect_workspace_inventory_paths,
    sanitize_progress_notes_for_workspace,
)
from app.services.task_service import TaskService

_PROGRESS_NOTES_MAX_BYTES = 8000


def _decode_context_snapshot_object(raw_snapshot: Any) -> Dict[str, Any]:
    if not raw_snapshot:
        return {}
    try:
        decoded = json.loads(raw_snapshot)
    except (TypeError, ValueError):
        return {}
    if isinstance(decoded, dict):
        return decoded
    return {"previous_context_snapshot": decoded}


def _inject_progress_notes_into_context(
    *,
    orchestration_state: Any,
    logger: Any,
) -> None:
    """Read .agent/progress_notes.md and prepend it to project_context."""

    project_dir = getattr(orchestration_state, "project_dir", None)
    if not project_dir:
        return
    notes_path = Path(project_dir) / ".agent" / "progress_notes.md"
    if not notes_path.exists():
        return
    try:
        notes_text = notes_path.read_text(encoding="utf-8", errors="replace")
        sanitized_notes = sanitize_progress_notes_for_workspace(
            notes_text,
            Path(project_dir),
        )
        workspace_inventory = collect_workspace_inventory_paths(
            Path(project_dir),
            max_files=40,
        )
        if len(sanitized_notes) > _PROGRESS_NOTES_MAX_BYTES:
            sanitized_notes = (
                "...(truncated)\n" + sanitized_notes[-_PROGRESS_NOTES_MAX_BYTES:]
            )
        workspace_truth = ["=== CURRENT WORKSPACE TRUTH ==="]
        if workspace_inventory:
            workspace_truth.extend(f"- {path}" for path in workspace_inventory[:40])
        else:
            workspace_truth.append("- No tracked files detected yet.")
        prefix = (
            "=== PRIOR SESSION PROGRESS NOTES ===\n"
            + sanitized_notes.strip()
            + "\n=== END PRIOR SESSION PROGRESS NOTES ===\n\n"
            + "\n".join(workspace_truth)
            + "\n=== END CURRENT WORKSPACE TRUTH ===\n\n"
        )
        current = orchestration_state.project_context or ""
        orchestration_state.project_context = (prefix + current)[:8000]
        logger.info("[ORIENT] Injected progress notes from %s", notes_path)
    except Exception as e:
        logger.warning("[ORIENT] Failed to read progress notes: %s", e)


def _get_next_pending_project_task(
    db: Session, project_id: Optional[int]
) -> Optional[Task]:
    if not project_id:
        return None
    return TaskService(db).get_next_pending_task(project_id)


def _build_base_project_context(
    task_service: Any,
    project: Any,
    task: Any,
    hydration_result: Dict[str, Any],
    max_chars: int = 5000,
) -> str:
    context = task_service.build_project_execution_context(
        project=project,
        current_task=task,
    )
    if hydration_result.get("hydrated"):
        hydrated_sources = ", ".join(
            f"#{item.get('task_id')} {item.get('title')}"
            for item in hydration_result.get("source_tasks", [])[:6]
        )
        context = (
            f"{context}\n"
            f"Hydrated baseline sources available directly in this workspace: {hydrated_sources}"
        )[:max_chars]
    return context
