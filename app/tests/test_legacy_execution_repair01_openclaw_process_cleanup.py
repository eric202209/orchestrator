"""LEGACY_EXECUTION_REPAIR_01 -- OpenClaw subprocess lifecycle and cleanup.

Provider-free: every subprocess is a synthetic ``python -c`` child started by
the production runners with ``start_new_session=True``. The only direct
signals sent by this module target a group whose leader wrote its own pid and
is verified (via /proc) to still lead that group.
"""

from __future__ import annotations

import asyncio
import os
import signal
import sys
from unittest.mock import patch

import pytest

import app.services.agents.subprocess_lifecycle as sl
from app.tests.test_post33r2a_stream_activity import (
    _command,
    _last_diagnostics,
    _legacy_service,
    _service,
)

# A single stdout line larger than STREAM_READ_LIMIT makes StreamReader.readline
# raise ValueError: an unexpected (non-timeout, non-cancel) runner failure.
_OVERSIZED_LINE = "sys.stdout.write('x' * 400000); sys.stdout.flush(); "


def _body(pid_file, extra: str = "", sleep: float = 30) -> str:
    return (
        "import os, sys, time; "
        f"open({str(pid_file)!r}, 'w').write(str(os.getpid())); "
        f"{extra}time.sleep({sleep})"
    )


def _running(pid: int) -> bool:
    try:
        with open(f"/proc/{pid}/stat") as stat_file:
            raw = stat_file.read()
    except OSError:
        return False
    return raw[raw.rfind(")") + 2] != "Z"


def _reap_leftover(pid_file) -> None:
    """Kill a leaked synthetic group only if its leader still leads it."""
    if not pid_file.exists() or not pid_file.read_text():
        return
    pid = int(pid_file.read_text())
    stat = sl._read_proc_stat(pid)
    if pid > 1 and stat is not None and stat[1] == pid:
        os.killpg(pid, signal.SIGKILL)


async def _wait_for_pid(pid_file) -> int:
    for _ in range(200):
        if pid_file.exists() and pid_file.read_text():
            return int(pid_file.read_text())
        await asyncio.sleep(0.02)
    raise AssertionError("synthetic child never started")


@pytest.fixture(autouse=True)
def _clean_registry():
    sl._reset_for_tests()
    yield
    sl._reset_for_tests()


# Case A -- normal completion: exit status collected, no termination sent.
@pytest.mark.asyncio
async def test_a_normal_completion_collects_exit_without_termination(tmp_path):
    service = _service()
    with patch.object(sl.os, "killpg") as killpg:
        process, diagnostics = await service._run_cli_prompt_with_diagnostics(
            _command("print('plain output')"), timeout_seconds=5, cwd=str(tmp_path)
        )

    assert process.returncode == 0
    assert diagnostics["cleanup_status"] == "completed"
    assert "cleanup_reap_attempts" not in diagnostics
    killpg.assert_not_called()
    assert diagnostics["process_pid"] not in sl._active_process_groups


# Case B -- explicit cancellation reaches and reaps the owned child.
@pytest.mark.asyncio
async def test_b_cancellation_terminates_and_reaps_owned_child(tmp_path):
    service = _service()
    pid_file = tmp_path / "b.pid"
    task = asyncio.create_task(
        service._run_cli_prompt_with_diagnostics(
            _command(_body(pid_file)), timeout_seconds=20, cwd=str(tmp_path)
        )
    )
    try:
        pid = await _wait_for_pid(pid_file)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as exc_info:
            await task
        diagnostics = exc_info.value.runtime_diagnostics
        assert diagnostics["process_pid"] == pid
        assert diagnostics["cleanup_status"] == "completed"
        assert diagnostics["return_code"] is not None
        assert not _running(pid)
        assert pid not in sl._active_process_groups
    finally:
        _reap_leftover(pid_file)


# Case C -- timeout reaches the correct process and cleans up.
@pytest.mark.asyncio
async def test_c_timeout_terminates_and_reaps_owned_child(tmp_path):
    service = _service()
    pid_file = tmp_path / "c.pid"
    try:
        with pytest.raises(asyncio.TimeoutError) as exc_info:
            await service._run_cli_prompt_with_diagnostics(
                _command(_body(pid_file)),
                timeout_seconds=0.3,
                cwd=str(tmp_path),
                strict_provider_result=True,
            )
        pid = int(pid_file.read_text())
        diagnostics = exc_info.value.runtime_diagnostics
        assert diagnostics["process_pid"] == pid
        assert diagnostics["timed_out"] is True
        assert diagnostics["cleanup_status"] == "completed"
        assert not _running(pid)
        assert pid not in sl._active_process_groups
    finally:
        _reap_leftover(pid_file)


# Case D -- repeated cleanup is harmless and never signals again.
@pytest.mark.asyncio
async def test_d_repeated_cleanup_is_idempotent_and_signals_nothing(tmp_path):
    service = _service()
    pid_file = tmp_path / "d.pid"
    task = asyncio.create_task(
        service._run_cli_prompt_with_diagnostics(
            _command(_body(pid_file)), timeout_seconds=20, cwd=str(tmp_path)
        )
    )
    try:
        await _wait_for_pid(pid_file)
        task.cancel()
        with pytest.raises(asyncio.CancelledError) as exc_info:
            await task
        process_pid = exc_info.value.runtime_diagnostics["process_pid"]

        with patch.object(sl.os, "killpg") as killpg:
            sl.kill_process_group(process_pid)
            sl.kill_process_group(process_pid)
        killpg.assert_not_called()
    finally:
        _reap_leftover(pid_file)


# Case E -- cancelling one invocation leaves a concurrent one untouched.
@pytest.mark.asyncio
async def test_e_cancelling_one_invocation_does_not_affect_another(tmp_path):
    victim, survivor = _service(), _service()
    victim_pid_file = tmp_path / "victim.pid"
    survivor_pid_file = tmp_path / "survivor.pid"
    victim_task = asyncio.create_task(
        victim._run_cli_prompt_with_diagnostics(
            _command(_body(victim_pid_file)), timeout_seconds=20, cwd=str(tmp_path)
        )
    )
    survivor_task = asyncio.create_task(
        survivor._run_cli_prompt_with_diagnostics(
            _command(_body(survivor_pid_file, "print('done', flush=True); ", 1.0)),
            timeout_seconds=20,
            cwd=str(tmp_path),
        )
    )
    try:
        victim_pid = await _wait_for_pid(victim_pid_file)
        survivor_pid = await _wait_for_pid(survivor_pid_file)
        assert victim_pid != survivor_pid

        victim_task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await victim_task
        assert not _running(victim_pid)
        assert _running(survivor_pid)

        process, diagnostics = await survivor_task
        assert process.returncode == 0
        assert "cleanup_reap_attempts" not in diagnostics
    finally:
        _reap_leftover(victim_pid_file)
        _reap_leftover(survivor_pid_file)


# Case F1 -- launch failure is surfaced; nothing is registered or signaled.
@pytest.mark.asyncio
async def test_f1_launch_failure_is_surfaced_without_registration(tmp_path):
    service = _service()
    with patch.object(sl.os, "killpg") as killpg:
        with pytest.raises(FileNotFoundError) as exc_info:
            await service._run_cli_prompt_with_diagnostics(
                [str(tmp_path / "missing-openclaw")],
                timeout_seconds=5,
                cwd=str(tmp_path),
            )
    assert exc_info.value.runtime_diagnostics["provider_invocation_started"] is False
    assert sl._active_process_groups == {}
    killpg.assert_not_called()


# Case F2 -- an unexpected runner failure must still terminate the child.
@pytest.mark.asyncio
async def test_f2_prompt_runner_failure_terminates_owned_child(tmp_path):
    service = _service()
    pid_file = tmp_path / "f2.pid"
    try:
        with pytest.raises(ValueError):
            await service._run_cli_prompt_with_diagnostics(
                _command(_body(pid_file, _OVERSIZED_LINE)),
                timeout_seconds=20,
                cwd=str(tmp_path),
            )
        pid = int(pid_file.read_text())
        assert not _running(pid)
        assert pid not in sl._active_process_groups
        assert _last_diagnostics(service)["cleanup_status"] == "completed"
    finally:
        _reap_leftover(pid_file)


@pytest.mark.asyncio
async def test_f3_streaming_runner_failure_terminates_owned_child(tmp_path):
    pid_file = tmp_path / "f3.pid"
    service = _legacy_service(tmp_path, _body(pid_file, _OVERSIZED_LINE))
    try:
        with pytest.raises(ValueError):
            await service.execute_task_with_streaming("probe", timeout_seconds=20)
        pid = int(pid_file.read_text())
        assert not _running(pid)
        assert pid not in sl._active_process_groups
    finally:
        _reap_leftover(pid_file)


# F-E2P-01 -- documented, not repaired: stop_session()/cleanup() act on
# self.process, which no launch path assigns. The production caller
# (stop_session_lifecycle) builds a fresh runtime in the API process, so
# retaining the handle on the launching instance would not reach the worker's
# child; the effective stop path is revoke(terminate=True) -> worker SIGTERM
# handler -> kill_process_group.
@pytest.mark.xfail(strict=True, reason="F-E2P-01 open: stop_session owns no process")
@pytest.mark.asyncio
async def test_stop_session_on_launching_instance_terminates_child(tmp_path):
    service = _service()
    service.process = None
    pid_file = tmp_path / "stop.pid"
    task = asyncio.create_task(
        service._run_cli_prompt_with_diagnostics(
            _command(_body(pid_file)), timeout_seconds=20, cwd=str(tmp_path)
        )
    )
    try:
        pid = await _wait_for_pid(pid_file)
        await service.stop_session()
        await asyncio.sleep(0.3)
        assert not _running(pid)
    finally:
        task.cancel()
        with pytest.raises(BaseException):
            await task
        _reap_leftover(pid_file)
