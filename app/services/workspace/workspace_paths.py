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

RUNTIME_METADATA_FILENAME = "runtime.json"

HYDRATION_EXCLUDED_NAMES = {
    ".agent",
    ".openclaw",  # legacy dir name; kept as migration guard for existing projects
    # Phase 23D-1: a Runtime Workspace (git worktree) has its own `.git`
    # gitlink *file* at its root; hydration previously walked the source
    # project's `.git` directory tree and tried to mkdir over that file,
    # raising a raw FileExistsError before any step executed. `.git` is
    # never task content, so it is excluded the same way `.agent` already is.
    ".git",
    ".venv",
    "__pycache__",
    "node_modules",
    ".pytest_cache",
    "site-packages",
    "venv",
    # .gitignore is orchestrator scaffolding (written by ensure_project_gitignore_guard)
    # and must not count as user content for the has_existing_files workspace review.
    ".gitignore",
    # Task Execution Sandbox recovery metadata must never enter durable workspace content.
    RUNTIME_METADATA_FILENAME,
    # OpenClaw's own per-workspace agent-identity/onboarding scaffold,
    # written whenever an agent's configured workspace matches the
    # project root (Phase 22B dogfood finding) — not user/task content.
    "BOOTSTRAP.md",
    "HEARTBEAT.md",
    "IDENTITY.md",
    "SOUL.md",
    "TOOLS.md",
    "USER.md",
}
# Kept separate from hydration exclusions so legitimate project-owned
# AGENTS.md files remain visible; the content predicate below identifies only
# the executor-generated OpenClaw onboarding copy.
RUNTIME_SCAFFOLD_FILENAME = "AGENTS.md"
OPENCLAW_RUNTIME_SCAFFOLD_HEADER = "# AGENTS.md - Your Workspace"
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
    # OpenClaw writes its own per-workspace agent-identity/onboarding
    # scaffold into whatever directory an agent's configured workspace
    # points at (see openclaw_service.py agent-workspace-matching). When
    # that directory is a real project's git root, these files must not
    # count as tracked project content (Phase 22B dogfood finding).
    ".openclaw/",
    "BOOTSTRAP.md",
    "HEARTBEAT.md",
    "IDENTITY.md",
    "SOUL.md",
    "TOOLS.md",
    "USER.md",
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


def is_executor_runtime_scaffold(path: Path) -> bool:
    """Identify OpenClaw's generated onboarding AGENTS.md by provenance."""

    path = Path(path)
    if path.name != RUNTIME_SCAFFOLD_FILENAME or not path.is_file():
        return False
    try:
        content = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError):
        return False
    return content.startswith(OPENCLAW_RUNTIME_SCAFFOLD_HEADER) and (
        "This folder is home. Treat it that way." in content
    )
