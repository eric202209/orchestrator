from __future__ import annotations

import pytest

from app.services.session import session_execution_service


@pytest.mark.asyncio
async def test_start_agent_session_payload_delegates_to_primary_helper(monkeypatch):
    calls: list[tuple[int, str]] = []

    async def _fake_start_session_payload(
        db, session_id: int, *, task_description: str
    ):
        calls.append((session_id, task_description))
        return {"status": "started", "session_id": session_id}

    monkeypatch.setattr(
        session_execution_service,
        "start_session_payload",
        _fake_start_session_payload,
    )

    result = await session_execution_service.start_agent_session_payload(
        object(),
        17,
        task_description="ship it",
    )

    assert result == {"status": "started", "session_id": 17}
    assert calls == [(17, "ship it")]


@pytest.mark.asyncio
async def test_start_openclaw_session_payload_remains_backward_compatible(monkeypatch):
    calls: list[tuple[int, str]] = []

    async def _fake_start_session_payload(
        db, session_id: int, *, task_description: str
    ):
        calls.append((session_id, task_description))
        return {"status": "started", "session_id": session_id}

    monkeypatch.setattr(
        session_execution_service,
        "start_session_payload",
        _fake_start_session_payload,
    )

    result = await session_execution_service.start_openclaw_session_payload(
        object(),
        21,
        task_description="legacy caller",
    )

    assert result == {"status": "started", "session_id": 21}
    assert calls == [(21, "legacy caller")]
