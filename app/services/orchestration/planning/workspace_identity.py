"""Explicit planner-facing identity for a physically isolated workspace."""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Any, Iterable

from app.services.workspace.path_display import render_workspace_path_for_prompt
from app.services.workspace.project_isolation_service import (
    resolve_project_workspace_path,
)


def _unique_aliases(values: Iterable[str | None]) -> tuple[str, ...]:
    aliases: list[str] = []
    for value in values:
        alias = str(value or "").strip().replace("\\", "/").strip("/")
        if not alias or "/" in alias or alias in {".", ".."}:
            continue
        if alias not in aliases:
            aliases.append(alias)
    return tuple(aliases)


@dataclass(frozen=True)
class PlannerWorkspaceIdentity:
    """The distinct workspace facts planning and validation may consume.

    ``physical_runtime_root`` is the only execution/containment root. The
    other fields are planner-facing metadata and never change where an
    operation is executed.
    """

    physical_runtime_root: Path
    project_workspace_root: Path
    logical_project_name: str
    planner_display_root: str
    display_project_path: str
    display_project_workspace_path: str
    physical_runtime_basename: str
    forbidden_root_aliases: tuple[str, ...]

    @classmethod
    def from_paths(
        cls,
        *,
        project_workspace: Path,
        physical_runtime_root: Path,
        logical_project_name: str | None = None,
        display_project_path: str | None = None,
        display_project_workspace_path: str | None = None,
    ) -> "PlannerWorkspaceIdentity":
        project_root = Path(project_workspace).resolve()
        runtime_root = Path(physical_runtime_root).resolve()
        logical_name = (
            str(logical_project_name or "").strip()
            or project_root.name
            or "current project"
        )
        runtime_basename = runtime_root.name
        return cls(
            physical_runtime_root=runtime_root,
            project_workspace_root=project_root,
            logical_project_name=logical_name,
            planner_display_root="current isolated task workspace",
            display_project_path=(
                display_project_path or render_workspace_path_for_prompt(runtime_root)
            ),
            display_project_workspace_path=(
                display_project_workspace_path
                or render_workspace_path_for_prompt(project_root)
            ),
            physical_runtime_basename=runtime_basename,
            forbidden_root_aliases=_unique_aliases(
                (logical_name, project_root.name, runtime_basename)
            ),
        )


def planner_workspace_identity_for_context(ctx: Any) -> PlannerWorkspaceIdentity:
    """Build the identity at the planning boundary from existing authorities."""

    state = ctx.orchestration_state
    project = getattr(ctx, "project", None)
    project_name = str(
        getattr(project, "name", None) or getattr(state, "project_name", None) or ""
    ).strip()
    project_workspace_value = getattr(project, "workspace_path", None)
    if project is not None:
        project_workspace = resolve_project_workspace_path(
            project_workspace_value,
            project_name,
            db=getattr(ctx, "db", None),
        )
    else:
        project_workspace = Path(getattr(state, "project_workspace_path"))

    runtime_workspace = Path(state.project_dir)
    return PlannerWorkspaceIdentity.from_paths(
        project_workspace=project_workspace,
        physical_runtime_root=runtime_workspace,
        logical_project_name=project_name,
        display_project_path=render_workspace_path_for_prompt(
            runtime_workspace, db=getattr(ctx, "db", None)
        ),
        display_project_workspace_path=render_workspace_path_for_prompt(
            project_workspace, db=getattr(ctx, "db", None)
        ),
    )


def render_planner_workspace_identity(identity: PlannerWorkspaceIdentity | None) -> str:
    """Render concise, explicit planner-only identity guidance."""

    if identity is None:
        return ""
    return "\n".join(
        (
            "Workspace identity contract:",
            f'- Logical project name: "{identity.logical_project_name}" (metadata only; not a path prefix).',
            f"- Project Workspace (baseline authority): {identity.display_project_workspace_path}",
            f"- Current Runtime Workspace (physical execution root): {identity.display_project_path}",
            "- Physical execution occurs inside an isolated runtime workspace. Treat the current directory as the project root for this task.",
            "- all generated file paths must be relative to the current root.",
            "- Do not recreate the project root, runtime root, or any alias of them.",
            "- Do not prepend the project name, runtime folder name, session ID, or task-execution ID to project-relative paths.",
            "- Remove redundant root `mkdir` or `cd` operations; legitimate existing child directories remain usable when the task actually targets them.",
        )
    )


__all__ = [
    "PlannerWorkspaceIdentity",
    "planner_workspace_identity_for_context",
    "render_planner_workspace_identity",
]
