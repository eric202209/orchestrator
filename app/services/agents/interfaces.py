"""Provider-neutral runtime interfaces shared across agent backends."""

from __future__ import annotations

from typing import Any, Optional, Protocol


class AgentRuntimeError(Exception):
    """Backend-neutral runtime failure raised by provider adapters."""


class AgentRuntime(Protocol):
    """Minimal runtime contract shared by orchestration entrypoints."""

    backend_descriptor: Any

    async def create_session(
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

    async def invoke_prompt(
        self,
        prompt: str,
        *,
        timeout_seconds: int = 180,
        source_brain: str = "local",
        session_prefix: str = "planning",
    ) -> dict[str, Any]: ...

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

    def reports_context_overflow(self, result: Optional[dict[str, Any]]) -> bool: ...
