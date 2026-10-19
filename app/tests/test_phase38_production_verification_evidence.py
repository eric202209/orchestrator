from __future__ import annotations

import asyncio
import hashlib
import json
import shutil
import subprocess
from dataclasses import asdict
from types import SimpleNamespace

import pytest

from app.services.agents.openclaw_service import (
    _CapturedStreamDigest,
    _capture_evaluator_stream_chunk,
    _captured_evaluator_stream_metadata,
)
from app.services.orchestration.diagnostics.outcome_observability import (
    redact_observed_text,
)
from app.services.orchestration.execution.execution_flow import (
    execute_verification_command,
)
from app.services.orchestration.phases.completion_flow import (
    _build_evaluator_response_diagnostics,
)
from app.services.orchestration.phases import execution_loop
from app.services.orchestration.prompt_templates import StepResult
from app.services.orchestration.state import persistence
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.state.persistence import append_orchestration_event


def _stub_process(monkeypatch, *, returncode=0, stdout=b"4 passed\n", stderr=b""):
    observed = {}

    def fake_run(actual_command, **kwargs):
        observed["command"] = actual_command
        observed["kwargs"] = kwargs
        return subprocess.CompletedProcess(
            args=actual_command,
            returncode=returncode,
            stdout=stdout,
            stderr=stderr,
        )

    monkeypatch.setattr(
        "app.services.orchestration.execution.execution_flow.resolve_project_python",
        lambda _project_dir: None,
    )
    monkeypatch.setattr(
        "app.services.orchestration.execution.execution_flow.subprocess.run",
        fake_run,
    )
    return observed


def test_successful_verification_retains_exact_observed_process_result(
    monkeypatch, tmp_path
):
    command = "pytest -q tests/test_slugify.py"
    stdout = b"4 passed\n"
    stderr = b""
    observed = _stub_process(monkeypatch, returncode=0, stdout=stdout, stderr=stderr)

    result = execute_verification_command(
        project_dir=tmp_path, command=command, include_evidence=True
    )

    evidence = result["verification_evidence"]
    assert result["success"] is True
    assert evidence["command"] == observed["command"] == command
    assert evidence["status"] == "completed"
    assert evidence["process_exit_code"] == 0
    assert evidence["output_byte_count"] == len(stdout + stderr)
    assert evidence["output_sha256"] == hashlib.sha256(stdout + stderr).hexdigest()
    assert evidence["started_at"]
    assert evidence["ended_at"]
    assert evidence["duration_seconds"] >= 0
    assert observed["kwargs"]["text"] is False


def test_nonzero_verification_exit_remains_failed(monkeypatch, tmp_path):
    _stub_process(monkeypatch, returncode=7, stdout=b"failed\n", stderr=b"trace\n")

    result = execute_verification_command(
        project_dir=tmp_path,
        command="pytest -q tests/test_slugify.py",
        include_evidence=True,
    )

    assert result["success"] is False
    assert result["returncode"] == 7
    assert result["verification_evidence"]["process_exit_code"] == 7
    assert result["verification_evidence"]["verification_status"] == "failed"


def test_timeout_does_not_fabricate_zero_exit_code(monkeypatch, tmp_path):
    def fake_timeout(command, **kwargs):
        raise subprocess.TimeoutExpired(
            command, kwargs["timeout"], output=b"partial", stderr=b""
        )

    monkeypatch.setattr(
        "app.services.orchestration.execution.execution_flow.resolve_project_python",
        lambda _project_dir: None,
    )
    monkeypatch.setattr(
        "app.services.orchestration.execution.execution_flow.subprocess.run",
        fake_timeout,
    )

    result = execute_verification_command(
        project_dir=tmp_path, command="pytest -q", include_evidence=True
    )

    assert result["success"] is False
    assert result["returncode"] is None
    assert result["verification_evidence"]["status"] == "timed_out"
    assert result["verification_evidence"]["process_exit_code"] is None
    assert result["verification_evidence"]["bounded_output"] == "partial"


def test_cancellation_propagates_without_success_evidence(monkeypatch, tmp_path):
    def fake_cancel(command, **kwargs):
        raise asyncio.CancelledError()

    monkeypatch.setattr(
        "app.services.orchestration.execution.execution_flow.resolve_project_python",
        lambda _project_dir: None,
    )
    monkeypatch.setattr(
        "app.services.orchestration.execution.execution_flow.subprocess.run",
        fake_cancel,
    )

    with pytest.raises(asyncio.CancelledError):
        execute_verification_command(
            project_dir=tmp_path, command="pytest -q", include_evidence=True
        )


def test_effective_command_identity_is_retained_after_python_resolution(
    monkeypatch, tmp_path
):
    command = "python3 -m pytest -q"
    observed = {}

    def fake_run(actual_command, **kwargs):
        observed["command"] = actual_command
        return subprocess.CompletedProcess(
            args=actual_command,
            returncode=0,
            stdout=b"ok\n",
            stderr=b"",
        )

    monkeypatch.setattr(
        "app.services.orchestration.execution.execution_flow.resolve_project_python",
        lambda _project_dir: "/opt/project/.venv/bin/python",
    )
    monkeypatch.setattr(
        "app.services.orchestration.execution.execution_flow.subprocess.run",
        fake_run,
    )

    result = execute_verification_command(
        project_dir=tmp_path, command=command, include_evidence=True
    )

    evidence = result["verification_evidence"]
    assert evidence["declared_command"] == command
    assert evidence["command"] == observed["command"]
    assert evidence["command"] != command


def test_in_process_portable_verification_does_not_claim_process_exit_code(
    tmp_path,
):
    (tmp_path / "README.md").write_text("hello\n", encoding="utf-8")

    result = execute_verification_command(
        project_dir=tmp_path, command="cat README.md", include_evidence=True
    )

    assert result["success"] is True
    assert result["verification_evidence"]["execution_kind"] == "in_process_portable"
    assert result["verification_evidence"]["process_exit_code"] is None


def test_bounded_output_truncation_is_explicit(monkeypatch, tmp_path):
    stdout = b"x" * 5000
    _stub_process(monkeypatch, stdout=stdout)

    result = execute_verification_command(
        project_dir=tmp_path, command="pytest -q", include_evidence=True
    )
    evidence = result["verification_evidence"]

    assert len(evidence["bounded_output"].encode("utf-8")) <= 4096
    assert evidence["output_truncated"] is True
    assert evidence["output_byte_count"] == len(stdout)


def test_durable_verification_evidence_survives_runtime_workspace_disposal(
    monkeypatch, tmp_path
):
    control_root = tmp_path / "control"
    runtime_workspace = tmp_path / "runtime" / "tasks" / "173" / "390"
    runtime_workspace.mkdir(parents=True)
    evidence = {
        "project_id": 173,
        "task_execution_id": 390,
        "session_id": 249,
        "task_id": 298,
        "plan_step_id": 2,
        "command": "pytest -q tests/test_slugify.py",
        "command_execution_status": "completed",
        "process_exit_code": 0,
        "started_at": "2026-10-19T15:00:00+00:00",
        "ended_at": "2026-10-19T15:00:01+00:00",
        "duration_seconds": 1.0,
        "output_byte_count": 9,
        "output_sha256": hashlib.sha256(b"4 passed\n").hexdigest(),
        "bounded_output": "4 passed",
        "output_truncated": False,
        "capture_error": None,
    }

    monkeypatch.setattr(execution_loop, "control_state_of", lambda _state: control_root)
    filename, summaries = execution_loop._persist_step_verification_evidence_records(
        raw_records=[evidence],
        project=SimpleNamespace(id=173),
        orchestration_state=object(),
        task_execution_id=390,
        session_id=249,
        task_id=298,
        step={"step_number": 2},
        step_index=1,
        step_attempt=1,
    )
    append_orchestration_event(
        project_dir=control_root,
        session_id=249,
        task_id=298,
        event_type=EventType.STEP_FINISHED,
        details={
            "verification_evidence_ref": filename,
            "verification_evidence_records": summaries,
        },
        project_id=173,
    )

    shutil.rmtree(runtime_workspace)

    evidence_file = next(control_root.rglob(filename))
    event_file = next(control_root.rglob("session_249_task_298.jsonl"))
    stored = json.loads(evidence_file.read_text(encoding="utf-8").splitlines()[0])
    event = json.loads(event_file.read_text(encoding="utf-8").splitlines()[0])
    assert stored["task_execution_id"] == 390
    assert stored["project_id"] == 173
    assert stored["session_id"] == 249
    assert stored["plan_step_id"] == 2
    assert stored["command"] == "pytest -q tests/test_slugify.py"
    assert stored["process_exit_code"] == 0
    assert stored["output_sha256"] == hashlib.sha256(b"4 passed\n").hexdigest()
    result_snapshot = asdict(
        StepResult(
            step_number=2,
            status="success",
            verification_evidence_ref=filename,
        )
    )
    assert result_snapshot["verification_evidence_ref"] == filename
    assert event["details"]["verification_evidence_ref"] == filename
    event_summary = event["details"]["verification_evidence_records"][0]
    assert event_summary["plan_step_id"] == 2
    assert "bounded_output" not in event_summary
    assert "4 passed" not in event_file.read_text(encoding="utf-8")
    assert evidence_file.stat().st_mode & 0o777 == 0o600
    assert not evidence_file.is_relative_to(runtime_workspace)


def test_sensitive_command_and_output_values_are_redacted_before_persistence(
    monkeypatch, tmp_path
):
    secret = "oauth-token-value-12345"
    api_key = "sk-test-secret-value-12345"
    raw_output = json.dumps(
        {
            "api_key": api_key,
            "authorization": f"Bearer {secret}",
            "profiles": [{"host": "internal-auth-host", "user": "operator"}],
            "result": "4 passed",
        }
    ).encode("utf-8")
    _stub_process(monkeypatch, stdout=raw_output)

    raw_command = f"pytest -q --api-key {api_key}"
    result = execute_verification_command(
        project_dir=tmp_path, command=raw_command, include_evidence=True
    )
    evidence = result["verification_evidence"]
    safe_output = evidence["bounded_output"]
    filename = persistence.append_verification_execution_evidence(
        project_dir=tmp_path / "control",
        project_id=173,
        session_id=249,
        task_id=298,
        task_execution_id=390,
        evidence=evidence,
    )
    stored_text = next((tmp_path / "control").rglob(filename)).read_text(
        encoding="utf-8"
    )

    assert api_key not in safe_output
    assert secret not in safe_output
    assert "<redacted>" in safe_output
    assert api_key not in evidence["command"]
    assert api_key not in stored_text
    assert secret not in stored_text
    assert "internal-auth-host" not in stored_text
    assert "operator" not in stored_text
    assert evidence["command_redacted"] is True
    assert redact_observed_text('API_TOKEN="abc123"') == 'API_TOKEN="<redacted>"'
    assert "oauth-token-value-12345" not in redact_observed_text(
        "Authorization: Bearer oauth-token-value-12345"
    )
    assert "username:password" not in redact_observed_text(
        "Authorization: Basic username:password"
    )
    assert "host.internal" not in redact_observed_text(
        "host auth profile: host.internal\nuser: operator"
    )


def test_evidence_persistence_failure_raises_closed(monkeypatch, tmp_path):
    monkeypatch.setattr(execution_loop, "control_state_of", lambda _state: tmp_path)
    monkeypatch.setattr(
        execution_loop,
        "append_verification_execution_evidence",
        lambda **_kwargs: (_ for _ in ()).throw(
            persistence.VerificationEvidencePersistenceError("disk unavailable")
        ),
    )
    with pytest.raises(persistence.VerificationEvidencePersistenceError):
        execution_loop._persist_step_verification_evidence_records(
            raw_records=[{"process_exit_code": 0}],
            project=SimpleNamespace(id=173),
            orchestration_state=object(),
            task_execution_id=390,
            session_id=249,
            task_id=298,
            step={"step_number": 2},
            step_index=1,
            step_attempt=1,
        )

    monkeypatch.setattr(
        persistence.os,
        "open",
        lambda *args, **kwargs: (_ for _ in ()).throw(OSError("disk unavailable")),
    )

    with pytest.raises(persistence.VerificationEvidencePersistenceError):
        persistence.append_verification_execution_evidence(
            project_dir=tmp_path,
            project_id=173,
            session_id=249,
            task_id=298,
            task_execution_id=390,
            evidence={"process_exit_code": 0},
        )


def test_evaluator_stream_metadata_survives_producer_and_diagnostic_projection():
    stdout = b'{"result": "ok"}\n'
    stderr = b"assistant text\n"
    captures = {"stdout": _CapturedStreamDigest(), "stderr": _CapturedStreamDigest()}
    _capture_evaluator_stream_chunk(captures, "stdout", stdout)
    _capture_evaluator_stream_chunk(captures, "stderr", stderr)
    producer_metadata = _captured_evaluator_stream_metadata(captures)

    projected = _build_evaluator_response_diagnostics(
        runtime_result={
            "status": "completed",
            "runtime_diagnostics": {
                "return_code": 0,
                **producer_metadata,
            },
        },
        runtime_exception=None,
        task_execution_id=390,
        evaluator_invocation_id="offline-test",
        runtime_status="completed",
    )

    assert projected["stdout_byte_length"] == len(stdout)
    assert projected["stderr_byte_length"] == len(stderr)
    assert projected["stdout_sha256"] == hashlib.sha256(stdout).hexdigest()
    assert projected["stderr_sha256"] == hashlib.sha256(stderr).hexdigest()
    assert "assistant text" not in json.dumps(projected)
