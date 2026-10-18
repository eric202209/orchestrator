"""LEGACY_EXECUTION_REPAIR_02 -- stop_session()/cleanup() must not claim a
termination they did not observe.

Provider-free and signal-free: the "owned process" cases use an in-memory fake
handle, so no real process or process group is ever signalled.
"""

from __future__ import annotations

import asyncio
import subprocess

import pytest

from app.services.session.session_lifecycle_service import stop_session_lifecycle
from app.tests.test_post33r2a_stream_activity import _service
from app.tests.test_session_lifecycle_regressions import _make_project, _make_session

_TERMINATION_CLAIMS = {"Session cleanup complete", "OpenClaw session stopped"}


def _messages(service) -> list[str]:
    return [message for _level, message, _metadata in service.logged_entries]


class _FakeHandle:
    """Popen-shaped stand-in; records calls instead of sending signals."""

    def __init__(self, *, exits_on_wait: bool) -> None:
        self.exits_on_wait = exits_on_wait
        self.returncode = None
        self.calls: list[str] = []

    def terminate(self) -> None:
        self.calls.append("terminate")

    def wait(self, timeout=None):
        self.calls.append("wait")
        if not self.exits_on_wait:
            raise subprocess.TimeoutExpired("fake", timeout)
        self.returncode = -15
        return self.returncode

    def kill(self) -> None:
        self.calls.append("kill")


@pytest.mark.asyncio
async def test_stop_session_without_owned_process_claims_no_termination():
    service = _service()
    service.process = None

    await service.stop_session()

    messages = _messages(service)
    assert not _TERMINATION_CLAIMS & set(messages)
    assert any("no subprocess owned" in message for message in messages)


@pytest.mark.asyncio
async def test_stop_session_with_confirmed_exit_reports_stopped():
    service = _service()
    handle = _FakeHandle(exits_on_wait=True)
    service.process = handle

    await service.stop_session()

    assert handle.calls == ["terminate", "wait"]
    assert _TERMINATION_CLAIMS <= set(_messages(service))


@pytest.mark.asyncio
async def test_stop_session_with_unconfirmed_kill_does_not_claim_stopped():
    service = _service()
    handle = _FakeHandle(exits_on_wait=False)
    service.process = handle

    await service.stop_session()

    assert handle.calls == ["terminate", "wait", "kill"]
    assert not _TERMINATION_CLAIMS & set(_messages(service))
    assert ("WARN", "Session cleanup: SIGKILL sent, exit not confirmed", None) in (
        service.logged_entries
    )


def test_stop_lifecycle_runtime_stop_claims_no_api_side_termination(
    db_session, monkeypatch
):
    """The production stop path builds a fresh runtime in the API process.

    It owns no subprocess, so its logs must not claim one was terminated; the
    session is still fenced to ``stopped`` exactly as before.
    """
    project = _make_project(db_session)
    session = _make_session(db_session, project, status="running", is_active=True)
    runtime = _service()
    runtime.process = None
    revoked: list[tuple[int, bool]] = []

    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.create_agent_runtime",
        lambda *a, **kw: runtime,
    )
    monkeypatch.setattr(
        "app.services.session.session_lifecycle_service.revoke_session_celery_tasks",
        lambda db, session_id, terminate=False: revoked.append((session_id, terminate))
        or ["celery-1"],
    )

    result = asyncio.run(stop_session_lifecycle(db_session, session.id))

    assert revoked == [(session.id, True)]
    assert result["status"] == "stopped"
    db_session.refresh(session)
    assert session.status == "stopped"
    messages = _messages(runtime)
    assert "Stopping OpenClaw session" in messages
    assert not _TERMINATION_CLAIMS & set(messages)
