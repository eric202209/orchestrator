"""Placeholder adapter for a remote OpenClaw gateway runtime."""

from __future__ import annotations

from typing import Optional

from sqlalchemy.orm import Session

from app.services.agents.agent_backends import UnsupportedAgentBackendError


def create_runtime(
    db: Session,
    session_id: Optional[int],
    task_id: Optional[int] = None,
    *,
    use_demo_mode: Optional[bool] = None,
):
    """Reject remote gateway runtime creation until a concrete adapter exists."""

    raise UnsupportedAgentBackendError(
        "Backend 'remote_openclaw_gateway' is registered but its runtime adapter "
        "has not been implemented yet."
    )
