"""Shared workspace path contracts for task workspace services."""

from __future__ import annotations

import re
from pathlib import Path

from sqlalchemy.orm import Session

from app.models import Project
from app.services.workspace.project_isolation_service import (
    _slugify_workspace_name,
    resolve_project_workspace_path,
)

HYDRATION_EXCLUDED_NAMES = {
    ".agent",
    ".venv",
    "__pycache__",
    "node_modules",
    ".pytest_cache",
    "site-packages",
    "venv",
}
LEGACY_BASELINE_DIR_NAME = ".project-baseline"
AUTO_SNAPSHOT_ROOT = ".agent/auto-snapshots"
AUTO_SNAPSHOT_DIR_NAME = "auto-snapshots"
PROMOTED_WORKSPACE_ARCHIVE_ROOT = ".agent/promoted-workspace-archive"
REJECTED_CHANGE_ARCHIVE_ROOT = ".agent/rejected-change-archive"
RETAINED_WORKSPACE_ARCHIVE_ROOT = ".agent/retained-workspace-archive"
REQUESTED_CHANGES_ARCHIVE_ROOT = ".agent/requested-changes-archive"
TASK_REPORT_ROOT = ".agent/task-reports"
TASK_REPORT_RE = re.compile(r"^task_report_\d+\.md$", re.IGNORECASE)

PROJECT_GITIGNORE_GUARD_START = "# BEGIN OpenClaw workspace guard"
PROJECT_GITIGNORE_GUARD_END = "# END OpenClaw workspace guard"
PROJECT_GITIGNORE_GUARD_LINES = [
    ".agent/",
    "__pycache__/",
    "node_modules/",
    ".venv/",
    "venv/",
    ".pytest_cache/",
]


def resolve_project_root(project: Project, db: Session) -> Path:
    """Resolve the canonical workspace root for a project."""
    raw_workspace_path = str(project.workspace_path or "").strip()
    explicit_candidate = Path(raw_workspace_path).expanduser()
    if explicit_candidate.is_absolute():
        explicit_path = explicit_candidate.resolve()
        project_slug = _slugify_workspace_name(project.name or "")
        if explicit_path.name == project_slug:
            return explicit_path
        nested_candidate = explicit_path / project_slug
        if nested_candidate.exists():
            return nested_candidate.resolve()
        return explicit_path
    return resolve_project_workspace_path(
        project.workspace_path,
        project.name,
        db=db,
    )


def is_hydration_excluded_path(relative_path: Path) -> bool:
    return any(part in HYDRATION_EXCLUDED_NAMES for part in relative_path.parts)
