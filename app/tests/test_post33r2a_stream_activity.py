"""POST33-R2A provider-free OpenClaw stream-activity fixtures."""

from __future__ import annotations

import asyncio
import json
import os
import sys

import pytest

import app.services.agents.openclaw_service as openclaw_service_module
from app.services.agents.openclaw_service import OpenClawSessionError
from app.services.agents.openclaw_service import OpenClawSessionService


def _service() -> OpenClawSessionService:
    service = object.__new__(OpenClawSessionService)
    service.db = type(
        "FakeDb",
        (),
        {"add": lambda self, entry: None, "commit": lambda self: None},
    )()
    service.session_id = None
    service.task_id = None
    service.task_execution_id = None
    service._task_session_id = None
    service.session_model = None
    service.task_model = None
    service.execution_cwd_override = None
    service.logged_entries = []
    service._strict_provider_controls = None
    service._log_entry = lambda level, message, metadata=None, commit=False: service.logged_entries.append(
        (level, message, metadata)
    )
    return service


def _command(body: str) -> list[str]:
    return [sys.executable, "-c", body]


def _response(text: str = "valid response") -> str:
    return json.dumps(
        {
            "payloads": [{"text": text}],
            "meta": {"agentMeta": {"sessionId": "probe-session"}},
        }
    )


def _last_diagnostics(service: OpenClawSessionService) -> dict:
    metadata = service.logged_entries[-1][2]
    assert metadata
    return json.loads(metadata)


def _legacy_service(tmp_path, body: str) -> OpenClawSessionService:
    service = _service()
    service._resolve_openclaw_command = lambda: [sys.executable]
    service._resolve_execution_cwd = lambda: str(tmp_path)
    service._build_openclaw_agent_command = lambda command, cwd: _command(body)
    service._validate_runtime_invocation_boundary = lambda cwd: None
    service._resolve_project_root_for_workspace_guard = lambda *args, **kwargs: None
    service._apply_workspace_binding_env = lambda env: env
    service._resolve_openclaw_cli_version = lambda: None
    service._last_selected_openclaw_agent_id = None
    service._runtime_result_contract = lambda: {}
    service._record_runtime_pollution = lambda *args, **kwargs: None
    service._apply_reported_workspace_guard = lambda result, **kwargs: result
    return service


async def _invoke(
    service: OpenClawSessionService,
    body: str,
    cwd: str,
    *,
    timeout_seconds: float = 0.5,
) -> tuple[object, dict]:
    try:
        process, diagnostics = await service._run_cli_prompt_with_diagnostics(
            _command(body),
            timeout_seconds=timeout_seconds,
            cwd=cwd,
            strict_provider_result=True,
        )
    except BaseException:
        return None, _last_diagnostics(service)
    return process, diagnostics


@pytest.mark.asyncio
async def test_partial_stdout_stall_is_distinguished_from_total_timeout(tmp_path):
    service = _service()
    partial = '{"payloads":[{"text":"partial response"}'

    _, diagnostics = await _invoke(
        service,
        f"import time; print({partial!r}, flush=True); time.sleep(60)",
        str(tmp_path),
        timeout_seconds=0.2,
    )

    assert diagnostics["activity_classification"] == "partial_stream_stall"
    assert diagnostics["terminal_reason"] == "partial_stream_stall"
    assert diagnostics["activity_state"] == "stream_stalled"
    assert diagnostics["partial_response_seen"] is True
    assert diagnostics["stream_stalled"] is True
    assert diagnostics["timed_out"] is True


@pytest.mark.asyncio
async def test_silent_hang_is_no_output_timeout(tmp_path):
    service = _service()

    _, diagnostics = await _invoke(
        service,
        "import time; time.sleep(60)",
        str(tmp_path),
        timeout_seconds=0.08,
    )

    assert diagnostics["activity_classification"] == "no_output_timeout"
    assert diagnostics["terminal_reason"] == "no_output_timeout"
    assert diagnostics["activity_state"] == "startup_wait"
    assert diagnostics["partial_response_seen"] is False
    assert diagnostics["stream_stalled"] is False


@pytest.mark.asyncio
async def test_silent_child_exit_is_missing_result(tmp_path):
    service = _service()

    process, diagnostics = await _invoke(service, "pass", str(tmp_path))

    assert process.returncode == 0
    assert diagnostics["activity_classification"] == "missing_result"
    assert diagnostics["terminal_reason"] == "missing_result"
    assert diagnostics["partial_response_seen"] is False


@pytest.mark.asyncio
async def test_stdout_response_is_completed(tmp_path):
    service = _service()

    process, diagnostics = await _invoke(
        service,
        f"print({_response()!r}, flush=True)",
        str(tmp_path),
    )

    assert process.returncode == 0
    assert diagnostics["activity_classification"] == "completed"
    assert diagnostics["terminal_reason"] == "completed"
    assert diagnostics["activity_state"] == "terminal_response"
    assert diagnostics["partial_response_seen"] is True


@pytest.mark.asyncio
async def test_stderr_only_response_and_diagnostics_remain_completed(tmp_path):
    service = _service()
    diagnostic = json.dumps({"livenessState": "running", "durationMs": 2})

    process, diagnostics = await _invoke(
        service,
        f"import sys; print({diagnostic!r}, file=sys.stderr); print({_response()!r}, file=sys.stderr, flush=True)",
        str(tmp_path),
    )

    assert process.returncode == 0
    assert diagnostics["activity_classification"] == "completed"
    assert diagnostics["response_channel"] == "stderr"
    assert diagnostics["stderr_contains_model_content"] is True


@pytest.mark.asyncio
async def test_diagnostic_only_stderr_timeout_has_no_partial_model_response(tmp_path):
    service = _service()
    diagnostic = json.dumps({"livenessState": "running", "durationMs": 2})

    _, diagnostics = await _invoke(
        service,
        f"import sys, time; print({diagnostic!r}, file=sys.stderr, flush=True); time.sleep(60)",
        str(tmp_path),
        timeout_seconds=0.2,
    )

    assert diagnostics["activity_classification"] == "provider_process_timeout"
    assert diagnostics["partial_response_seen"] is False
    assert diagnostics["stderr_contains_model_content"] is False


@pytest.mark.asyncio
async def test_partial_stderr_model_response_is_distinguished(tmp_path):
    service = _service()
    partial = '{"payloads":[{"text":"partial stderr response"}'

    _, diagnostics = await _invoke(
        service,
        f"import sys, time; print({partial!r}, file=sys.stderr, flush=True); time.sleep(60)",
        str(tmp_path),
        timeout_seconds=0.2,
    )

    assert diagnostics["activity_classification"] == "partial_stream_stall"
    assert diagnostics["partial_response_seen"] is True
    assert diagnostics["response_channel"] == "stderr"


@pytest.mark.asyncio
async def test_long_startup_is_active_then_completed_not_stalled(tmp_path):
    service = _service()

    process, diagnostics = await _invoke(
        service,
        f"import time; time.sleep(0.12); print({_response()!r}, flush=True)",
        str(tmp_path),
    )

    assert process.returncode == 0
    assert diagnostics["activity_classification"] == "completed"
    assert diagnostics["activity_state"] == "terminal_response"
    assert diagnostics["stream_stalled"] is False
    assert diagnostics["first_output_delay_seconds"] >= 0.1


@pytest.mark.asyncio
async def test_valid_response_then_refuses_exit_has_bounded_cleanup(tmp_path):
    service = _service()
    child_pid_file = tmp_path / "child.pid"
    body = (
        "import subprocess, sys, time; "
        f"child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"open({str(child_pid_file)!r}, 'w').write(str(child.pid)); "
        f"print({_response()!r}, flush=True); time.sleep(60)"
    )

    process, diagnostics = await _invoke(service, body, str(tmp_path))

    assert process.returncode == 0
    assert diagnostics["activity_classification"] == "completed"
    assert diagnostics["cleanup_status"] == "completed"
    assert diagnostics["cleanup_timed_out"] is False
    child_pid = int(child_pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


@pytest.mark.asyncio
async def test_sigterm_resistant_child_is_sigkill_cleaned(tmp_path):
    service = _service()
    body = (
        "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); "
        "time.sleep(60)"
    )

    _, diagnostics = await _invoke(service, body, str(tmp_path), timeout_seconds=0.08)

    assert diagnostics["activity_classification"] == "no_output_timeout"
    assert diagnostics["cleanup_status"] == "completed"
    with pytest.raises(ProcessLookupError):
        os.kill(diagnostics["process_pid"], 0)


@pytest.mark.asyncio
async def test_grandchild_process_group_is_cleaned(tmp_path):
    service = _service()
    child_pid_file = tmp_path / "grandchild.pid"
    body = (
        "import subprocess, sys, time; "
        f"child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"open({str(child_pid_file)!r}, 'w').write(str(child.pid)); time.sleep(60)"
    )

    _, diagnostics = await _invoke(service, body, str(tmp_path), timeout_seconds=0.08)

    assert diagnostics["cleanup_status"] == "completed"
    child_pid = int(child_pid_file.read_text())
    with pytest.raises(ProcessLookupError):
        os.kill(child_pid, 0)


@pytest.mark.asyncio
async def test_caller_cancellation_is_distinguished_and_cleaned(tmp_path):
    service = _service()
    task = asyncio.create_task(
        service._run_cli_prompt_with_diagnostics(
            _command("import time; time.sleep(60)"),
            timeout_seconds=2,
            cwd=str(tmp_path),
            strict_provider_result=True,
        )
    )
    await asyncio.sleep(0.05)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    diagnostics = _last_diagnostics(service)
    assert diagnostics["activity_classification"] == "caller_cancelled"
    assert diagnostics["terminal_reason"] == "caller_cancelled"
    assert diagnostics["cleanup_status"] == "completed"


def test_activity_summary_includes_canonical_fields():
    summary = OpenClawSessionService._stream_diagnostics_summary(
        {
            "duration_seconds": 1.25,
            "timeout_seconds": 5,
            "timed_out": True,
            "cancelled": False,
            "diagnostic_category": "timeout",
            "activity_state": "stream_stalled",
            "activity_classification": "partial_stream_stall",
            "terminal_reason": "partial_stream_stall",
            "partial_response_seen": True,
            "cleanup_status": "completed",
            "stream_stalled": True,
        }
    )

    assert "activity_state=stream_stalled" in summary
    assert "activity_classification=partial_stream_stall" in summary
    assert "terminal_reason=partial_stream_stall" in summary
    assert "partial_response_seen=True" in summary
    assert "cleanup_status=completed" in summary


@pytest.mark.asyncio
async def test_legacy_execution_path_reports_completed_activity(tmp_path):
    service = _legacy_service(tmp_path, f"print({_response()!r}, flush=True)")

    result = await service.execute_task_with_streaming(
        "provider-free fixture",
        timeout_seconds=2,
        diagnostic_label="PLANNING",
    )

    diagnostics = result["runtime_diagnostics"]
    assert result["status"] == "completed"
    assert diagnostics["prompt_stage"] == "P6_PROVIDER_BOUND_PROMPT"
    assert diagnostics["provider_bound_prompt_chars"] == len("provider-free fixture")
    assert diagnostics["provider_invocation_started"] is True
    assert diagnostics["provider_response_received"] is True
    assert diagnostics["activity_classification"] == "completed"
    assert diagnostics["terminal_reason"] == "completed"
    assert diagnostics["activity_state"] == "terminal_response"
    assert diagnostics["cleanup_status"] == "completed"


@pytest.mark.asyncio
async def test_legacy_execution_path_reports_partial_stall(monkeypatch, tmp_path):
    partial = '{"payloads":[{"text":"partial legacy response"}'
    service = _legacy_service(
        tmp_path,
        f"import time; print({partial!r}, flush=True); time.sleep(60)",
    )
    real_wait_for = openclaw_service_module.asyncio.wait_for

    async def capped_wait_for(awaitable, timeout):
        return await real_wait_for(awaitable, min(timeout, 0.15))

    monkeypatch.setattr(openclaw_service_module.asyncio, "wait_for", capped_wait_for)

    with pytest.raises(OpenClawSessionError) as exc_info:
        await service.execute_task_with_streaming(
            "provider-free fixture",
            timeout_seconds=0.1,
            diagnostic_label="PLANNING",
        )

    diagnostics = exc_info.value.runtime_diagnostics
    assert diagnostics["prompt_stage"] == "P6_PROVIDER_BOUND_PROMPT"
    assert diagnostics["provider_invocation_started"] is True
    assert diagnostics["provider_response_received"] is False
    assert diagnostics["activity_classification"] == "partial_stream_stall"
    assert diagnostics["terminal_reason"] == "partial_stream_stall"
    assert diagnostics["partial_response_seen"] is True
    assert diagnostics["stream_stalled"] is True


@pytest.mark.asyncio
async def test_provider_initialization_failure_does_not_claim_invocation_or_response(
    monkeypatch, tmp_path
):
    service = _service()
    monkeypatch.setattr(
        openclaw_service_module.asyncio,
        "create_subprocess_exec",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("provider init failed")),
    )

    with pytest.raises(OSError) as exc_info:
        await service._run_cli_prompt_with_diagnostics(
            _command("pass"),
            timeout_seconds=1,
            cwd=str(tmp_path),
            prompt="provider-free fixture",
            invocation_kind="planning",
        )

    diagnostics = exc_info.value.runtime_diagnostics
    assert diagnostics["prompt_stage"] == "P6_PROVIDER_BOUND_PROMPT"
    assert diagnostics["provider_invocation_started"] is False
    assert diagnostics["provider_response_received"] is False
    assert "provider-free fixture" not in json.dumps(diagnostics)
