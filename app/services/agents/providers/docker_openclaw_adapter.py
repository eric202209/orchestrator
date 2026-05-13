"""Docker-backed OpenClaw runtime adapter."""

from __future__ import annotations

import shlex
from pathlib import Path
from typing import List, Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.services.agents.agent_backends import get_backend_descriptor
from app.services.agents.openclaw_service import (
    OpenClawSessionError,
    OpenClawSessionService,
)
from app.services.workspace.sandbox_contract import build_docker_workspace_sandbox_plan


class DockerOpenClawSessionService(OpenClawSessionService):
    """Run OpenClaw inside a Docker container mounted to the active workspace."""

    def __init__(
        self,
        db: Session,
        session_id: Optional[int],
        task_id: Optional[int] = None,
        *,
        use_demo_mode: Optional[bool] = None,
    ):
        super().__init__(db, session_id, task_id, use_demo_mode=use_demo_mode)
        self.backend_descriptor = get_backend_descriptor("docker_openclaw")

    def _resolve_openclaw_command(self) -> List[str]:
        command = (settings.OPENCLAW_DOCKER_COMMAND or "openclaw").strip()
        try:
            parsed = shlex.split(command)
        except ValueError as exc:
            raise OpenClawSessionError(
                f"OPENCLAW_DOCKER_COMMAND could not be parsed: {exc}"
            ) from exc
        if not parsed:
            raise OpenClawSessionError("OPENCLAW_DOCKER_COMMAND is empty")
        return parsed

    def _wrap_command_for_docker(
        self, command: List[str], cwd: Optional[str]
    ) -> List[str]:
        if not cwd:
            raise OpenClawSessionError(
                "Docker OpenClaw requires a resolved task workspace cwd"
            )
        workspace = Path(cwd).resolve()
        plan = build_docker_workspace_sandbox_plan(
            project_root=workspace.parent,
            task_workspace=workspace,
            image=settings.OPENCLAW_DOCKER_IMAGE,
            network=settings.OPENCLAW_DOCKER_NETWORK,
            environment={"OPENCLAW_DOCKER_BACKEND": "1"},
        )
        return plan.docker_run_args(command)

    async def _run_cli_prompt_with_diagnostics(self, full_cmd, **kwargs):
        docker_cmd = self._wrap_command_for_docker(full_cmd, kwargs.get("cwd"))
        return await super()._run_cli_prompt_with_diagnostics(docker_cmd, **kwargs)


def create_runtime(
    db: Session,
    session_id: Optional[int],
    task_id: Optional[int] = None,
    *,
    use_demo_mode: Optional[bool] = None,
) -> DockerOpenClawSessionService:
    """Instantiate the Docker-backed OpenClaw runtime."""

    return DockerOpenClawSessionService(
        db,
        session_id,
        task_id,
        use_demo_mode=use_demo_mode,
    )
