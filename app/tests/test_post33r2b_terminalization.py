"""POST33-R2B provider-free legacy process terminalization fixtures."""

from __future__ import annotations

import asyncio
import time
from unittest.mock import AsyncMock, patch

import pytest

import app.services.agents.openclaw_service as openclaw_service_module
from app.services.agents.openclaw_service import OpenClawSessionError
from app.tests.test_post33r2a_stream_activity import _legacy_service


class _BlockingStream:
    async def readline(self):
        await asyncio.sleep(60)


class _NeverReapingProcess:
    pid = 987654
    returncode = None

    def __init__(self):
        self.stdout = _BlockingStream()
        self.stderr = _BlockingStream()
        self.wait_calls = 0
        self.kill_calls = 0

    async def wait(self):
        self.wait_calls += 1
        await asyncio.sleep(60)

    def kill(self):
        self.kill_calls += 1


@pytest.mark.asyncio
async def test_legacy_timeout_bounds_a_never_returning_post_kill_wait(tmp_path):
    service = _legacy_service(tmp_path, "pass")
    service._build_openclaw_agent_command = lambda command, cwd: ["fake-openclaw"]
    process = _NeverReapingProcess()
    create_process = AsyncMock(return_value=process)
    real_wait_for = openclaw_service_module.asyncio.wait_for

    async def capped_wait_for(awaitable, timeout):
        return await real_wait_for(awaitable, min(timeout, 0.02))

    async def invoke():
        await service.execute_task_with_streaming(
            "provider-free never-reaping timeout fixture",
            timeout_seconds=0.01,
            diagnostic_label="PLANNING",
        )

    started_at = time.monotonic()
    with patch.object(
        openclaw_service_module.asyncio,
        "create_subprocess_exec",
        create_process,
    ), patch.object(
        openclaw_service_module.asyncio,
        "wait_for",
        capped_wait_for,
    ), patch.object(
        openclaw_service_module, "kill_process_group"
    ) as kill_group:
        with pytest.raises(OpenClawSessionError) as exc_info:
            await real_wait_for(invoke(), timeout=0.5)

    elapsed = time.monotonic() - started_at
    diagnostics = exc_info.value.runtime_diagnostics
    assert elapsed < 0.5
    assert kill_group.call_count == 1
    assert process.wait_calls == 2
    assert process.kill_calls == 1
    assert diagnostics["activity_classification"] == "no_output_timeout"
    assert diagnostics["terminal_reason"] == "no_output_timeout"
    assert diagnostics["cleanup_status"] == "timed_out"
    assert diagnostics["cleanup_timed_out"] is True


@pytest.mark.asyncio
async def test_legacy_cancellation_bounds_reap_and_preserves_identity(tmp_path):
    service = _legacy_service(tmp_path, "pass")
    service._build_openclaw_agent_command = lambda command, cwd: ["fake-openclaw"]
    process = _NeverReapingProcess()
    create_process = AsyncMock(return_value=process)
    real_wait_for = openclaw_service_module.asyncio.wait_for

    async def capped_wait_for(awaitable, timeout):
        if timeout == 5:
            return await real_wait_for(awaitable, 0.02)
        return await real_wait_for(awaitable, timeout)

    async def invoke():
        await service.execute_task_with_streaming(
            "provider-free never-reaping cancellation fixture",
            timeout_seconds=2,
            diagnostic_label="PLANNING",
        )

    task = asyncio.create_task(invoke())
    with patch.object(
        openclaw_service_module.asyncio,
        "create_subprocess_exec",
        create_process,
    ), patch.object(
        openclaw_service_module.asyncio,
        "wait_for",
        capped_wait_for,
    ), patch.object(
        openclaw_service_module, "kill_process_group"
    ) as kill_group:
        await real_wait_for(asyncio.sleep(0.03), timeout=0.2)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as exc_info:
            await real_wait_for(task, timeout=0.5)

    diagnostics = getattr(exc_info.value, "runtime_diagnostics", None)
    assert kill_group.call_count == 1
    assert process.wait_calls == 2
    assert process.kill_calls == 1
    assert diagnostics is not None
    assert diagnostics["activity_classification"] == "caller_cancelled"
    assert diagnostics["terminal_reason"] == "caller_cancelled"
    assert diagnostics["cleanup_status"] == "timed_out"
    assert diagnostics["cleanup_timed_out"] is True
