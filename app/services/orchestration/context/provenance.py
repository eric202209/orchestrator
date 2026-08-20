"""Read-only planning context provenance collector.

Captures what context entered the planning prompt without changing anything
about how that context is assembled, ranked, or applied.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Optional

from app.schemas.knowledge import KnowledgeContext
from app.services.project.source_imports import python_test_source_context_from_tests
from app.services.workspace.control_state_paths import control_state_of

_TASK_PREVIEW_CHARS = 200


def _maybe_emit_provenance(
    ctx: Any,
    workspace_review: Dict[str, Any],
    knowledge_context: Any,
    planning_prompt: Optional[str],
) -> None:
    """Emit a planning_context_provenance event — silently ignores all errors."""
    try:
        from app.services.orchestration.state.persistence import (
            append_orchestration_event,
        )
        from app.services.orchestration.events.event_types import EventType

        state = getattr(ctx, "orchestration_state", None)
        provenance = collect_planning_context_provenance(
            task_description=getattr(ctx, "prompt", None),
            project_context=getattr(state, "project_context", None),
            project_dir=Path(getattr(state, "project_dir", None) or "."),
            workspace_review=workspace_review or {},
            knowledge_context=knowledge_context,
            planning_prompt=planning_prompt,
            planner_source_materialization=getattr(
                ctx, "planner_source_materialization", None
            ),
        )
        append_orchestration_event(
            project_dir=control_state_of(state),
            session_id=getattr(ctx, "session_id", None),
            task_id=getattr(ctx, "task_id", None),
            event_type=EventType.PLANNING_CONTEXT_PROVENANCE,
            details=provenance,
        )
    except Exception:
        pass


def collect_planning_context_provenance(
    *,
    task_description: Optional[str],
    project_context: Optional[str],
    project_dir: Path,
    workspace_review: Dict[str, Any],
    knowledge_context: Optional[KnowledgeContext],
    planning_prompt: Optional[str],
    planner_source_materialization: Any = None,
) -> Dict[str, Any]:
    """Return a read-only snapshot of what context entered the planning prompt."""
    task_desc = task_description or ""
    proj_ctx = project_context or ""

    knowledge_items: List[Dict[str, Any]] = []
    failure_memories: List[Dict[str, Any]] = []
    if knowledge_context and knowledge_context.retrieved_items:
        for item in knowledge_context.retrieved_items:
            entry: Dict[str, Any] = {
                "id": item.id,
                "title": item.title,
                "knowledge_type": item.knowledge_type,
                "confidence": item.confidence,
            }
            knowledge_items.append(entry)
            if item.knowledge_type == "failure_memory":
                failure_memories.append(entry)

    try:
        python_ctx: Optional[str] = python_test_source_context_from_tests(project_dir)
    except Exception:
        python_ctx = None

    omitted: Dict[str, str] = {}
    if knowledge_context is None:
        omitted["knowledge"] = "retrieval_failed_or_skipped"
    elif not knowledge_context.retrieved_items:
        omitted["knowledge"] = "no_items_retrieved"

    if not python_ctx:
        omitted["python_source_context"] = "no_test_files_found"

    if not planning_prompt:
        omitted["planning_prompt"] = "runtime_service_unavailable"

    return {
        "task_description": {
            "chars": len(task_desc),
            "preview": task_desc[:_TASK_PREVIEW_CHARS],
        },
        "project_context": {
            "chars": len(proj_ctx),
        },
        "workspace_files": {
            "file_count": int(workspace_review.get("file_count") or 0),
            "source_file_count": int(workspace_review.get("source_file_count") or 0),
            "has_existing_files": bool(workspace_review.get("has_existing_files")),
        },
        "knowledge_items_injected": knowledge_items,
        "failure_memories_injected": failure_memories,
        "matched_failure_memory": bool(
            knowledge_context and knowledge_context.matched_failure_memory
        ),
        "omitted_sources": omitted,
        "planner_source_materialization": (
            planner_source_materialization.to_metadata()
            if planner_source_materialization is not None
            else None
        ),
    }
