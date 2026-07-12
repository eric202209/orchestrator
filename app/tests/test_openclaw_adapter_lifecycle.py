import json
import os
import sys
from pathlib import Path

import pytest

from app.services.agents.openclaw_service import OpenClawSessionService


def _service():
    service = object.__new__(OpenClawSessionService)
    service.db = type(
        "FakeDb",
        (),
        {"add": lambda self, entry: None, "commit": lambda self: None},
    )()
    service.session_id = None
    service.task_id = None
    service.task_execution_id = None
    service.session_model = None
    service.task_model = None
    service.execution_cwd_override = None
    service.logged_entries = []
    return service


def _command(body: str):
    return [sys.executable, "-c", body]


def _response(text="workflow continues"):
    return json.dumps({"payloads": [{"text": text}]})


@pytest.mark.asyncio
async def test_adapter_returns_immediately_after_normal_gateway_response_and_cli_exit():
    service = _service()
    proc, diagnostics = await service._run_cli_prompt_with_diagnostics(
        _command(f"print({(_response())!r}, flush=True)"),
        timeout_seconds=2,
        cwd=None,
    )

    assert service.parse_cli_response(proc)["output"] == "workflow continues"
    assert proc.returncode == 0
    assert diagnostics["response_boundary_reached"] is True


@pytest.mark.asyncio
async def test_adapter_cleans_up_hanging_cli_process_group_after_response(tmp_path):
    service = _service()
    child_pid_file = str(tmp_path / "child.pid")
    body = (
        "import subprocess, sys, time; "
        f"child=subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)']); "
        f"open({child_pid_file!r}, 'w').write(str(child.pid)); "
        f"print({_response()!r}, flush=True); time.sleep(60)"
    )
    try:
        proc, diagnostics = await service._run_cli_prompt_with_diagnostics(
            _command(body), timeout_seconds=2, cwd=None
        )
        assert service.parse_cli_response(proc)["status"] == "completed"
        assert diagnostics["response_boundary_reached"] is True
        assert diagnostics["response_cleanup_return_code"] is not None
        child_pid = int(Path(child_pid_file).read_text())
        with pytest.raises(ProcessLookupError):
            os.kill(child_pid, 0)
    finally:
        if os.path.exists(child_pid_file):
            os.unlink(child_pid_file)


@pytest.mark.asyncio
async def test_adapter_preserves_no_output_timeout_when_gateway_never_returns():
    service = _service()
    with pytest.raises(Exception) as exc_info:
        await service._run_cli_prompt_with_diagnostics(
            _command("import time; time.sleep(60)"),
            timeout_seconds=2,
            no_output_timeout_seconds=0.05,
            cwd=None,
        )

    assert exc_info.value.__class__.__name__ == "OpenClawNoOutputTimeoutError"


@pytest.mark.asyncio
async def test_adapter_preserves_failed_cli_exit():
    service = _service()
    proc, diagnostics = await service._run_cli_prompt_with_diagnostics(
        _command("import sys; print('gateway failed', file=sys.stderr); sys.exit(7)"),
        timeout_seconds=2,
        cwd=None,
    )

    assert proc.returncode == 7
    assert diagnostics["response_boundary_reached"] is False
    result = service.parse_cli_response(proc)
    assert result["status"] == "failed"
    assert "gateway failed" in result["error"]
