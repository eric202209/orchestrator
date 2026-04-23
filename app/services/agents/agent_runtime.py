"""Factory helpers for orchestration runtime backends."""

from __future__ import annotations

import asyncio
from typing import Any, Optional

from sqlalchemy.orm import Session

from app.config import settings
from app.services.agents.agent_backends import (
    UnsupportedAgentBackendError,
    require_backend_descriptor,
)
from app.services.agents.interfaces import AgentRuntime
from app.services.agents.providers import get_runtime_factory
from app.services.workspace.system_settings import get_effective_agent_backend


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
    descriptor = require_backend_descriptor(backend_name)
    runtime_factory = get_runtime_factory(descriptor.name)
    if runtime_factory is not None:
        return runtime_factory(
            db,
            session_id,
            task_id,
            use_demo_mode=use_demo_mode,
        )

    raise UnsupportedAgentBackendError(
        f"Backend '{descriptor.name}' does not have a registered runtime adapter."
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


def invoke_runtime_prompt(
    db: Session,
    prompt: str,
    *,
    session_id: Optional[int] = None,
    task_id: Optional[int] = None,
    source_brain: str = "local",
    timeout_seconds: int = 180,
    session_prefix: str = "planning",
) -> dict[str, Any]:
    """Execute a one-shot runtime prompt across local or remote backends."""

    runtime = create_agent_runtime(db, session_id, task_id)
    return asyncio.run(
        runtime.invoke_prompt(
            prompt,
            timeout_seconds=timeout_seconds,
            source_brain=source_brain,
            session_prefix=session_prefix,
        )
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


def runtime_reports_context_overflow(
    db: Session,
    result: Optional[dict[str, Any]],
    *,
    session_id: Optional[int] = None,
    task_id: Optional[int] = None,
) -> bool:
    """Backend-neutral context overflow check for planning retries."""

    runtime = create_agent_runtime(db, session_id=session_id, task_id=task_id)
    return runtime.reports_context_overflow(result)
