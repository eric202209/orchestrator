"""Factory helpers for orchestration runtime backends."""

from __future__ import annotations

import asyncio
import enum
import logging
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

logger = logging.getLogger(__name__)


class BackendRole(str, enum.Enum):
    PLANNING = "planning"
    EXECUTION = "execution"
    REPAIR = "repair"


_TEST_RUNTIME_BACKENDS = {"stub_success", "stub_capacity"}


def _test_runtime_backends_enabled() -> bool:
    return bool(getattr(settings, "ENABLE_TEST_RUNTIME_BACKENDS", False))


def _is_test_runtime_backend(backend_name: str) -> bool:
    return backend_name in _TEST_RUNTIME_BACKENDS


def resolve_backend_name_for_role(db: Session, role: BackendRole) -> str:
    """Return the configured backend name for role, falling back to AGENT_BACKEND."""
    role_setting = {
        BackendRole.PLANNING: settings.PLANNING_BACKEND,
        BackendRole.EXECUTION: settings.EXECUTION_BACKEND,
        BackendRole.REPAIR: settings.REPAIR_BACKEND,
    }.get(role)
    if role_setting:
        return role_setting.strip()
    return get_effective_agent_backend(settings.AGENT_BACKEND, db=db).strip()


def create_agent_runtime(
    db: Session,
    session_id: Optional[int],
    task_id: Optional[int] = None,
    *,
    use_demo_mode: Optional[bool] = None,
    role: Optional[BackendRole] = None,
) -> AgentRuntime:
    """Instantiate the configured backend runtime for a session/task pair."""

    if role is not None:
        backend_name = resolve_backend_name_for_role(db, role)
    else:
        backend_name = get_effective_agent_backend(
            settings.AGENT_BACKEND, db=db
        ).strip()
    if _is_test_runtime_backend(backend_name):
        if not _test_runtime_backends_enabled():
            raise UnsupportedAgentBackendError(
                f"Backend '{backend_name}' is test-only and ENABLE_TEST_RUNTIME_BACKENDS is false."
            )
        from app.services.agents.stub_runtime import create_stub_runtime

        return create_stub_runtime(
            db,
            session_id,
            task_id,
            use_demo_mode=use_demo_mode,
            backend_id=backend_name,
        )
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


def invoke_runtime_prompt(
    db: Session,
    prompt: str,
    *,
    session_id: Optional[int] = None,
    task_id: Optional[int] = None,
    task_execution_id: Optional[int] = None,
    source_brain: str = "local",
    timeout_seconds: int = 180,
    session_prefix: str = "planning",
) -> dict[str, Any]:
    """Execute a one-shot runtime prompt across local or remote backends."""

    runtime = create_agent_runtime(db, session_id, task_id)
    if task_execution_id is not None and hasattr(runtime, "task_execution_id"):
        runtime.task_execution_id = task_execution_id
    try:
        return asyncio.run(
            runtime.invoke_prompt(
                prompt,
                timeout_seconds=timeout_seconds,
                source_brain=source_brain,
                session_prefix=session_prefix,
            )
        )
    except Exception:
        logger.exception(
            "Runtime prompt invocation failed for session_id=%s task_id=%s "
            "task_execution_id=%s",
            session_id,
            task_id,
            task_execution_id,
        )
        raise


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
