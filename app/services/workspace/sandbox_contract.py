"""Execution sandbox contract helpers.

These helpers define the file-system boundary for future container-backed task
execution. They intentionally do not run Docker; runtime adapters can consume
the plan and keep Project Baseline promotion semantics unchanged.
"""

from __future__ import annotations

from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Iterable


class SandboxContractError(ValueError):
    """Raised when a sandbox plan would violate workspace boundaries."""


@dataclass(frozen=True)
class SandboxMount:
    source: str
    target: str
    mode: str = "rw"

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


@dataclass(frozen=True)
class SandboxExecutionPlan:
    engine: str
    image: str
    workspace_dir: str
    container_workspace: str = "/workspace"
    mounts: list[SandboxMount] = field(default_factory=list)
    environment: dict[str, str] = field(default_factory=dict)
    network: str = "none"

    def docker_run_args(self, command: Iterable[str]) -> list[str]:
        args = ["docker", "run", "--rm", "--network", self.network]
        for mount in self.mounts:
            args.extend(
                [
                    "--mount",
                    (
                        f"type=bind,source={mount.source},target={mount.target},"
                        f"{'readonly' if mount.mode == 'ro' else 'rw'}"
                    ),
                ]
            )
        for key, value in sorted(self.environment.items()):
            args.extend(["--env", f"{key}={value}"])
        args.extend(["--workdir", self.container_workspace, self.image])
        args.extend(list(command))
        return args

    def to_dict(self) -> dict[str, Any]:
        payload = asdict(self)
        payload["mounts"] = [mount.to_dict() for mount in self.mounts]
        return payload


def build_docker_workspace_sandbox_plan(
    *,
    project_root: Path,
    task_workspace: Path,
    image: str = "python:3.12-slim",
    network: str = "none",
    environment: dict[str, str] | None = None,
) -> SandboxExecutionPlan:
    """Build a Docker sandbox plan for one task workspace.

    The task workspace must be an existing direct child of the project root.
    This keeps retained task folders, baseline files, and future cleanup policy
    aligned around the same boundary.
    """

    resolved_project_root = project_root.resolve()
    resolved_workspace = task_workspace.resolve()
    if not resolved_workspace.exists() or not resolved_workspace.is_dir():
        raise SandboxContractError("task workspace must be an existing directory")
    if resolved_workspace.parent != resolved_project_root:
        raise SandboxContractError(
            "task workspace must be a direct child of the project root"
        )
    if not image.strip():
        raise SandboxContractError("container image is required")

    return SandboxExecutionPlan(
        engine="docker",
        image=image.strip(),
        workspace_dir=str(resolved_workspace),
        mounts=[
            SandboxMount(
                source=str(resolved_workspace),
                target="/workspace",
                mode="rw",
            )
        ],
        environment=dict(environment or {}),
        network=network or "none",
    )
