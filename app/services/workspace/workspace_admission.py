"""Canonical workspace ownership and dogfood-admission checks.

This module is the single authority for the distinction between a stored
Project workspace string and the canonical realpath which owns runtime work.
It deliberately keeps historical soft-deleted Project rows visible to audit
while excluding them from launch ownership.
"""

from __future__ import annotations

import json
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING

from app.models import Project
from app.services.project.lifecycle import assert_project_launch_eligible
from app.services.workspace.project_isolation_service import (
    resolve_project_workspace_path,
)

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class WorkspaceAdmissionError(ValueError):
    """Fail-closed, operator-actionable workspace admission failure."""

    def __init__(self, category: str, detail: str, *, paths: list[str] | None = None):
        self.category = category
        self.detail = detail
        self.paths = paths or []
        super().__init__(f"{category}: {detail}")

    def payload(self) -> dict:
        return {"category": self.category, "detail": self.detail, "paths": self.paths}


def canonical_workspace_realpath(value: str | Path) -> Path:
    """Return the diagnostic canonical realpath without admitting nonexistence."""

    return Path(value).expanduser().resolve(strict=False)


def project_workspace_realpath(project: Project, db: "Session") -> Path:
    return canonical_workspace_realpath(
        resolve_project_workspace_path(project.workspace_path, project.name, db=db)
    )


def active_workspace_owners(db: "Session", workspace: Path) -> list[Project]:
    """Return every active Project whose resolved workspace is exactly workspace."""

    canonical = canonical_workspace_realpath(workspace)
    owners: list[Project] = []
    for candidate in (
        db.query(Project)
        .filter(Project.deleted_at.is_(None), Project.retired_at.is_(None))
        .all()
    ):
        if project_workspace_realpath(candidate, db) == canonical:
            owners.append(candidate)
    return owners


def assert_unique_active_workspace_owner(db: "Session", project: Project) -> Path:
    workspace = project_workspace_realpath(project, db)
    owners = active_workspace_owners(db, workspace)
    if len(owners) != 1 or owners[0].id != project.id:
        owner_ids = ", ".join(str(owner.id) for owner in owners) or "none"
        raise WorkspaceAdmissionError(
            "workspace_mapping_ambiguous",
            f"Canonical workspace {workspace} has active Project owners [{owner_ids}]; "
            "exactly one active owner is required.",
        )
    return workspace


def _git(workspace: Path, *args: str) -> tuple[int, str]:
    completed = subprocess.run(
        ["git", "-C", str(workspace), *args],
        text=True,
        stdout=subprocess.PIPE,
        stderr=subprocess.STDOUT,
        timeout=10,
        check=False,
    )
    return completed.returncode, completed.stdout.strip()


def _matching_openclaw_agent_ids(config_path: Path, workspace: Path) -> list[str]:
    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise WorkspaceAdmissionError(
            "workspace_openclaw_mismatch", f"Could not read OpenClaw config: {exc}"
        ) from exc
    matches: list[str] = []
    for agent in (config.get("agents") or {}).get("list") or []:
        if not isinstance(agent, dict):
            continue
        agent_id = str(agent.get("id") or "").strip()
        agent_workspace = str(agent.get("workspace") or "").strip()
        if (
            agent_id
            and agent_workspace
            and canonical_workspace_realpath(agent_workspace) == workspace
        ):
            matches.append(agent_id)
    return matches


@dataclass(frozen=True)
class DogfoodWorkspaceAdmission:
    project_id: int
    workspace: str
    openclaw_agent_id: str


def admit_dogfood_workspace(
    db: "Session", project: Project, *, openclaw_config_path: Path | None = None
) -> DogfoodWorkspaceAdmission:
    """Validate the dogfood-only launch profile without mutating any Project data."""

    assert_project_launch_eligible(project)
    workspace = assert_unique_active_workspace_owner(db, project)
    if not workspace.exists():
        raise WorkspaceAdmissionError(
            "workspace_missing", f"Workspace does not exist: {workspace}"
        )
    code, _ = _git(workspace, "rev-parse", "--is-inside-work-tree")
    if code != 0:
        raise WorkspaceAdmissionError(
            "workspace_not_git", f"Workspace is not a Git repository: {workspace}"
        )
    code, dirty = _git(workspace, "status", "--porcelain", "--untracked-files=all")
    if code != 0:
        raise WorkspaceAdmissionError(
            "workspace_not_git", f"Git status failed for: {workspace}"
        )
    if dirty:
        raise WorkspaceAdmissionError(
            "workspace_dirty",
            "Workspace has uncommitted or untracked paths.",
            paths=dirty.splitlines(),
        )
    code, remote = _git(workspace, "remote")
    if code != 0 or not remote:
        raise WorkspaceAdmissionError(
            "workspace_remote_missing",
            f"Workspace has no configured Git remote: {workspace}",
        )
    config_path = openclaw_config_path or Path.home() / ".openclaw" / "openclaw.json"
    matches = _matching_openclaw_agent_ids(config_path, workspace)
    if len(matches) != 1:
        raise WorkspaceAdmissionError(
            "workspace_openclaw_mismatch",
            f"Expected exactly one OpenClaw agent for {workspace}; found {matches or 'none'}.",
        )
    return DogfoodWorkspaceAdmission(
        project_id=project.id, workspace=str(workspace), openclaw_agent_id=matches[0]
    )
