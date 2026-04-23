"""Factory and protocol for orchestration runtime backends."""

from __future__ import annotations

from typing import Any, Optional, Protocol

from sqlalchemy.orm import Session

from app.config import settings
from app.services.agents.openclaw_service import OpenClawSessionService
from app.services.workspace.system_settings import get_effective_agent_backend


class AgentRuntime(Protocol):
    """Minimal runtime contract shared by orchestration entrypoints."""

    backend_descriptor: Any

    async def create_openclaw_session(
        self, task_description: str, context: Optional[dict[str, Any]] = None
    ) -> str: ...

    async def execute_task(
        self, prompt: str, timeout_seconds: int = 300, log_callback: Any = None
    ) -> dict[str, Any]: ...

    async def execute_task_with_orchestration(
        self, prompt: str, timeout_seconds: int = 300, orchestration_state: Any = None
    ) -> dict[str, Any]: ...

    async def pause_session(self) -> None: ...

    async def resume_session(self, checkpoint_name: Optional[str] = None) -> str: ...

    async def stop_session(self) -> None: ...

    async def get_session_context(self) -> dict[str, Any]: ...

    def get_backend_metadata(self) -> dict[str, Any]: ...

    def build_cli_agent_command(
        self,
        prompt: str,
        *,
        source_brain: str = "local",
        timeout_seconds: int = 180,
        session_prefix: str = "planning",
    ) -> list[str]: ...

    def parse_cli_response(self, proc: Any) -> dict[str, Any]: ...


def create_agent_runtime(
    db: Session,
    session_id: Optional[int],
    task_id: Optional[int] = None,
    *,
    use_demo_mode: Optional[bool] = None,
) -> AgentRuntime:
    """Instantiate the configured backend runtime for a session/task pair."""

    backend_name = get_effective_agent_backend(
        settings.ORCHESTRATOR_AGENT_BACKEND, db=db
    ).strip()
    if backend_name == "local_openclaw":
        return OpenClawSessionService(
            db,
            session_id,
            task_id,
            use_demo_mode=use_demo_mode,
        )

    # Unknown backends currently fall back to the local runtime so the platform
    # remains operational while new adapters are being wired in.
    return OpenClawSessionService(
        db,
        session_id,
        task_id,
        use_demo_mode=use_demo_mode,
    )


def build_runtime_cli_agent_command(
    db: Session,
    prompt: str,
    *,
    session_id: Optional[int] = None,
    task_id: Optional[int] = None,
    source_brain: str = "local",
    timeout_seconds: int = 180,
    session_prefix: str = "planning",
) -> list[str]:
    """Build a backend-specific CLI command for synchronous planning flows."""

    runtime = create_agent_runtime(db, session_id, task_id)
    return runtime.build_cli_agent_command(
        prompt,
        source_brain=source_brain,
        timeout_seconds=timeout_seconds,
        session_prefix=session_prefix,
    )


def parse_runtime_cli_response(
    db: Session,
    proc: Any,
    *,
    session_id: Optional[int] = None,
    task_id: Optional[int] = None,
) -> dict[str, Any]:
    """Parse backend CLI output through the active runtime adapter."""

    runtime = create_agent_runtime(db, session_id, task_id)
    return runtime.parse_cli_response(proc)


def runtime_reports_context_overflow(result: Optional[dict[str, Any]]) -> bool:
    """Backend-neutral context overflow check for planning retries."""

    return OpenClawSessionService._is_context_overflow_result(result)
