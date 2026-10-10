"""Canonical workspace ownership and dogfood-admission checks.

This module is the single authority for the distinction between a stored
Project workspace string and the canonical realpath which owns runtime work.
It deliberately keeps historical soft-deleted Project rows visible to audit
while excluding them from launch ownership.
"""

from __future__ import annotations

import json
import os
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Any

from app.models import Project
from app.services.orchestration.execution.executor_workspace_binding import (
    ExecutorWorkspaceBindingError,
    resolve_openclaw_runner_agent_id,
    validate_runtime_owned_openclaw_agent,
)
from app.services.project.lifecycle import assert_project_launch_eligible
from app.services.workspace.project_isolation_service import (
    resolve_project_workspace_path,
)
from app.services.workspace.system_settings import get_effective_runtime_root

if TYPE_CHECKING:
    from sqlalchemy.orm import Session


class WorkspaceAdmissionError(ValueError):
    """Fail-closed, operator-actionable workspace admission failure."""

    def __init__(
        self,
        category: str,
        detail: str,
        *,
        paths: list[str] | None = None,
        metadata: dict[str, Any] | None = None,
    ):
        self.category = category
        self.detail = detail
        self.paths = paths or []
        self.metadata = dict(metadata or {})
        super().__init__(f"{category}: {detail}")

    def payload(self) -> dict:
        return {
            "category": self.category,
            "detail": self.detail,
            "paths": self.paths,
            **self.metadata,
        }


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


def matching_openclaw_agent_ids(config: dict[str, Any], workspace: Path) -> list[str]:
    canonical = canonical_workspace_realpath(workspace)
    matches: list[str] = []
    for agent in (config.get("agents") or {}).get("list") or []:
        if not isinstance(agent, dict):
            continue
        agent_id = str(agent.get("id") or "").strip()
        agent_workspace = str(agent.get("workspace") or "").strip()
        if (
            agent_id
            and agent_workspace
            and canonical_workspace_realpath(agent_workspace) == canonical
        ):
            matches.append(agent_id)
    return matches


def _default_openclaw_config_path() -> Path:
    configured = os.environ.get("OPENCLAW_CONFIG_PATH", "").strip()
    if configured:
        return Path(configured).expanduser()
    state_dir = os.environ.get("OPENCLAW_STATE_DIR", "").strip()
    if state_dir:
        return Path(state_dir).expanduser() / "openclaw.json"
    return Path.home() / ".openclaw" / "openclaw.json"


def _load_dogfood_openclaw_config(config_path: Path) -> dict[str, Any]:
    """Read the operator config read-only and require an unambiguous ``main``.

    ``main`` is a configuration-integrity check only. It is never an execution
    identity: dispatch binds the ephemeral runtime agent instead.
    """

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
    except Exception as exc:
        raise WorkspaceAdmissionError(
            "openclaw_config_invalid", f"Could not read OpenClaw config: {exc}"
        ) from exc
    agents = config.get("agents") if isinstance(config, dict) else None
    agent_list = agents.get("list") if isinstance(agents, dict) else None
    if not isinstance(agent_list, list):
        raise WorkspaceAdmissionError(
            "openclaw_config_invalid",
            f"OpenClaw config has no agents.list array: {config_path}",
        )
    main_count = sum(
        1
        for agent in agent_list
        if isinstance(agent, dict) and str(agent.get("id") or "").strip() == "main"
    )
    if main_count != 1:
        raise WorkspaceAdmissionError(
            "openclaw_config_invalid",
            f"Expected exactly one OpenClaw 'main' agent; found {main_count}.",
        )
    return config


@dataclass(frozen=True)
class DogfoodWorkspaceAdmission:
    project_id: int
    workspace: str
    # Legacy persistent agent registered for this ProductRoot, if any. It is
    # diagnostic only; dispatch never selects it. None for fresh ProductRoots.
    openclaw_agent_id: str | None


@dataclass(frozen=True)
class OpenClawWorkspaceBindingAdmission:
    project_id: int
    workspace: str
    openclaw_agent_id: str
    matching_agent_count: int
    configured_provider: str
    admission_stage: str


def admit_openclaw_workspace_binding(
    db: "Session",
    project: Project,
    *,
    configured_provider: str,
    admission_stage: str = "dispatch",
    openclaw_config_path: Path | None = None,
    configured_providers: dict[str, str] | None = None,
) -> OpenClawWorkspaceBindingAdmission:
    """Require one explicit runtime-owned OpenClaw runner for dispatch.

    This provider-specific dispatch gate validates the runner identity and
    persistent runtime-owned template. Git cleanliness and per-invocation
    workspace allocation remain owned by their existing layers.
    """

    workspace = project_workspace_realpath(project, db)
    config_path = openclaw_config_path or _default_openclaw_config_path()
    metadata = {
        "project_id": project.id,
        "normalized_project_workspace": str(workspace),
        "workspace_exists": workspace.exists() and workspace.is_dir(),
        "matching_agent_count": 0,
        "configured_provider": configured_provider,
        "admission_stage": admission_stage,
    }
    if configured_providers:
        metadata["configured_providers"] = dict(configured_providers)

    if not metadata["workspace_exists"]:
        raise WorkspaceAdmissionError(
            "openclaw_workspace_binding_unavailable",
            f"Project {project.id} workspace does not exist: {workspace}",
            metadata=metadata,
        )

    try:
        config = json.loads(config_path.read_text(encoding="utf-8"))
        selection = validate_runtime_owned_openclaw_agent(
            config,
            agent_id=resolve_openclaw_runner_agent_id(),
            project_workspace=workspace,
            runtime_root=get_effective_runtime_root(db),
        )
    except (OSError, TypeError, ValueError) as exc:
        raise WorkspaceAdmissionError(
            "openclaw_workspace_binding_unavailable",
            f"Could not read OpenClaw config: {exc}",
            metadata=metadata,
        ) from exc
    except ExecutorWorkspaceBindingError as exc:
        raise WorkspaceAdmissionError(
            "openclaw_workspace_binding_unavailable",
            str(exc),
            metadata=metadata,
        ) from exc

    metadata["matching_agent_count"] = 1
    if not selection.agent_id:
        raise WorkspaceAdmissionError(
            "openclaw_workspace_binding_unavailable",
            f"Project {project.id} has no valid runtime-owned OpenClaw runner.",
            metadata=metadata,
        )

    return OpenClawWorkspaceBindingAdmission(
        project_id=project.id,
        workspace=str(workspace),
        openclaw_agent_id=selection.agent_id,
        matching_agent_count=1,
        configured_provider=configured_provider,
        admission_stage=admission_stage,
    )


def admit_project_openclaw_binding_for_dispatch(
    db: "Session",
    project: Project,
    *,
    admission_stage: str = "dispatch",
    planning_backend_override: str | None = None,
) -> OpenClawWorkspaceBindingAdmission | None:
    """Admit the project when a resolved planning/execution role uses OpenClaw."""

    from app.services.agents.agent_runtime import (
        resolve_backend_name_for_role,
    )
    from app.services.agents.runtime_configuration import BackendRole

    configured_providers = {
        role.value: resolve_backend_name_for_role(db, role)
        for role in (BackendRole.PLANNING, BackendRole.EXECUTION)
    }
    if planning_backend_override:
        configured_providers[BackendRole.PLANNING.value] = planning_backend_override
    if "local_openclaw" not in configured_providers.values():
        return None

    return admit_openclaw_workspace_binding(
        db,
        project,
        configured_provider="local_openclaw",
        admission_stage=admission_stage,
        configured_providers=configured_providers,
    )


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
    config = _load_dogfood_openclaw_config(config_path)
    # A persistent per-ProductRoot agent is not required: dispatch uses an
    # ephemeral binding. More than one legacy registration stays ambiguous.
    matches = matching_openclaw_agent_ids(config, workspace)
    if len(matches) > 1:
        raise WorkspaceAdmissionError(
            "workspace_openclaw_mismatch",
            f"Expected at most one OpenClaw agent for {workspace}; found {matches}.",
        )
    return DogfoodWorkspaceAdmission(
        project_id=project.id,
        workspace=str(workspace),
        openclaw_agent_id=matches[0] if matches else None,
    )
