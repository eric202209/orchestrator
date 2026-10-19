from __future__ import annotations

import json
import os
import sys
import asyncio
from datetime import UTC, datetime, timedelta
from types import SimpleNamespace

import pytest

import app.services.agents.openclaw_service as openclaw_service_module
from app.services.agents.openclaw_service import OpenClawSessionService
from app.api.v1.endpoints import project_logs
from app.models import LogEntry, Project, Session as SessionModel
from app.services.observability.log_stream import LogStreamService
from app.services.observability.openclaw_diagnostics import (
    OpenClawDiagnosticEvent,
    build_openclaw_diagnostic_metadata,
)
from app.tasks import maintenance


class _CapturingDb:
    def __init__(self, *, fail_commit: bool = False):
        self.entries = []
        self.fail_commit = fail_commit
        self.rollback_count = 0

    def add(self, entry):
        self.entries.append(entry)

    def commit(self):
        if self.fail_commit:
            raise RuntimeError("synthetic persistence failure")

    def rollback(self):
        self.rollback_count += 1


def _execution_service(tmp_path, body: str, *, fail_commit: bool = False):
    service = object.__new__(OpenClawSessionService)
    service.db = _CapturingDb(fail_commit=fail_commit)
    service.session_id = 41
    service.task_id = 42
    service.task_execution_id = 43
    service._task_session_id = None
    service.session_model = None
    service.task_model = None
    service.execution_cwd_override = None
    service._strict_provider_controls = None
    service._last_selected_openclaw_agent_id = None
    service._resolve_openclaw_command = lambda: [sys.executable]
    service._resolve_execution_cwd = lambda: str(tmp_path)
    service._build_openclaw_agent_command = lambda *_args, **_kwargs: [
        sys.executable,
        "-c",
        body,
    ]
    service._apply_discovery_tool_suppression = lambda _label: None
    service._validate_runtime_invocation_boundary = lambda _cwd: None
    service._resolve_project_root_for_workspace_guard = lambda **_kwargs: None
    service._apply_workspace_binding_env = lambda env: env
    service._resolve_openclaw_cli_version = lambda: None
    service._workspace_binding_spawn_kwargs = lambda: {}
    service._openclaw_invocation_metadata = lambda **_kwargs: {
        "invocation_kind": "planning"
    }
    service._runtime_result_contract = lambda: {}
    service._record_runtime_pollution = lambda *_args, **_kwargs: None
    service._apply_reported_workspace_guard = lambda result, **_kwargs: result
    return service


def _log_service(db=None):
    service = object.__new__(OpenClawSessionService)
    service.db = db or _CapturingDb()
    service.session_id = 41
    service.task_id = 42
    service.task_execution_id = 43
    service.session_model = None
    service.task_model = None
    return service


@pytest.mark.asyncio
async def test_streamed_assistant_output_is_not_persisted(monkeypatch, tmp_path):
    response = (
        "SYNTHETIC_ASSISTANT_RESPONSE api_key=synthetic-known-token-canary "
        "synthetic-secret-shaped-value-not-a-credential-001"
    )
    wire_payload = json.dumps({"payloads": [{"text": response}]})
    body = f"import sys; print({wire_payload!r}, file=sys.stderr, flush=True)"
    service = _execution_service(tmp_path, body)
    monkeypatch.setattr(
        openclaw_service_module,
        "build_git_containment_env",
        lambda: (dict(os.environ), None),
    )

    result = await service.execute_task_with_streaming(
        "synthetic prompt",
        timeout_seconds=2,
        diagnostic_label="EVALUATOR_RESPONSE_EVIDENCE",
    )

    assert result["output"] == response
    assert service.db.entries
    serialized_logs = "\n".join(
        f"{entry.message}\n{entry.log_metadata or ''}" for entry in service.db.entries
    )
    assert response not in serialized_logs
    assert "synthetic-known-token-canary" not in serialized_logs
    assert "synthetic-secret-shaped-value-not-a-credential-001" not in serialized_logs
    assert "stdout_tail" not in result["runtime_diagnostics"]
    assert "stderr_tail" not in result["runtime_diagnostics"]

    diagnostics = [
        json.loads(entry.log_metadata)
        for entry in service.db.entries
        if entry.log_metadata
    ]
    evaluator_record = next(
        item
        for item in diagnostics
        if item.get("diagnostic_event") == "openclaw_evaluator_diagnostics"
    )
    assert evaluator_record["diagnostic_schema_version"] == 2
    assert evaluator_record["content_omitted"] is True
    assert evaluator_record["response_present"] is True
    assert evaluator_record["selected_response_channel"] == "stderr"
    assert evaluator_record["stderr_byte_count"] > 0
    assert len(evaluator_record["stderr_sha256"]) == 64
    assert "stdout_tail" not in evaluator_record
    assert "stderr_tail" not in evaluator_record
    assert all(entry.session_id == 41 for entry in service.db.entries)
    assert all(entry.task_id == 42 for entry in service.db.entries)
    assert all(entry.task_execution_id == 43 for entry in service.db.entries)


@pytest.mark.parametrize(
    ("diagnostics", "expected"),
    [
        (
            {
                "diagnostic_category": "provider_failure",
                "stderr_chars": 38,
                "stderr_diagnostic_only": True,
                "stderr_tail": "synthetic diagnostic content",
            },
            {"stderr_character_count": 38, "stderr_diagnostic_only": True},
        ),
        (
            {"stdout_chars": 0, "stderr_chars": 0, "return_code": 0},
            {
                "stdout_character_count": 0,
                "stderr_character_count": 0,
                "runtime_status": "completed",
            },
        ),
        (
            {"json_parsing_outcome": "FAILED", "stdout_tail": "{synthetic invalid"},
            {"parser_outcome": "invalid_json"},
        ),
        (
            {"timed_out": True, "timeout_seconds": 3},
            {"runtime_status": "timed_out", "timed_out": True},
        ),
        (
            {"cancelled": True},
            {"runtime_status": "interrupted", "interrupted": True},
        ),
        (
            {"return_code": 9},
            {"runtime_status": "failed", "cli_return_code": 9},
        ),
    ],
)
def test_diagnostics_contract_preserves_safe_typed_statuses(diagnostics, expected):
    canary = "synthetic-unrecognized-secret-shaped-canary-002"
    metadata = build_openclaw_diagnostic_metadata(
        OpenClawDiagnosticEvent.GENERIC_DIAGNOSTICS,
        {
            **diagnostics,
            "stdout_tail": canary,
            "stderr_tail": canary,
            "stdout": canary,
            "stderr": canary,
            "prompt": canary,
            "provider_payload": {"body": canary},
            "environment": {"TOKEN": canary},
            "authorization_header": f"Bearer {canary}",
            "command": f"openclaw --token {canary}",
            "diagnostic_category": canary,
            "recognized_response_field": canary,
        },
    )

    assert metadata["diagnostic_schema_version"] == 2
    assert metadata["content_omitted"] is True
    for key, value in expected.items():
        assert metadata[key] == value
    assert canary not in json.dumps(metadata)
    assert not {"stdout_tail", "stderr_tail", "stdout", "stderr", "prompt"} & set(
        metadata
    )


def test_optional_diagnostic_persistence_failure_is_nonfatal():
    service = _log_service(_CapturingDb(fail_commit=True))

    persisted = service._log_openclaw_diagnostic(
        OpenClawDiagnosticEvent.PROCESS_RESULT,
        {"return_code": 0, "diagnostic_category": "provider_success"},
    )

    assert persisted is False
    assert service.db.rollback_count == 1


def test_provider_evidence_snapshot_stays_separate_from_operational_metadata():
    canary = "synthetic-provider-evidence-canary"
    snapshot = OpenClawSessionService._provider_evidence_partial_snapshot(
        f"prefix {canary}", "synthetic stderr"
    )

    class _EvidenceRecorder:
        def fail(self, _exception, **kwargs):
            self.failure_kwargs = kwargs
            return {}

    service = _log_service()
    recorder = _EvidenceRecorder()
    service._active_planning_provider_evidence = recorder
    service._finish_active_planning_provider_evidence(
        exception=RuntimeError("synthetic execution failure"),
        diagnostics={"stdout_byte_count": 42},
        partial_content_snapshot=snapshot,
    )

    assert recorder.failure_kwargs["partial_content_snapshot"] == f"prefix {canary}"
    operational_metadata = build_openclaw_diagnostic_metadata(
        OpenClawDiagnosticEvent.GENERIC_DIAGNOSTICS,
        {"stdout_tail": snapshot, "stderr_tail": snapshot},
    )
    assert canary not in json.dumps(operational_metadata)


@pytest.mark.asyncio
async def test_diagnostic_persistence_failure_does_not_change_execution_result(
    monkeypatch, tmp_path
):
    response = "SYNTHETIC_EXECUTION_RESULT"
    wire_payload = json.dumps({"payloads": [{"text": response}]})
    body = f"import sys; print({wire_payload!r}, file=sys.stderr, flush=True)"
    service = _execution_service(tmp_path, body, fail_commit=True)
    monkeypatch.setattr(
        openclaw_service_module,
        "build_git_containment_env",
        lambda: (dict(os.environ), None),
    )

    result = await service.execute_task_with_streaming(
        "synthetic prompt", timeout_seconds=2
    )

    assert result["status"] == "completed"
    assert result["output"] == response
    assert service.db.rollback_count > 0


class _Query:
    def __init__(self, rows=(), first=None):
        self.rows = rows
        self.first_row = first

    def filter(self, *_args, **_kwargs):
        return self

    def join(self, *_args, **_kwargs):
        return self

    def order_by(self, *_args, **_kwargs):
        return self

    def limit(self, *_args, **_kwargs):
        return self

    def all(self):
        return list(self.rows)

    def first(self):
        return self.first_row


def _project_log_fixture(metadata: str, message: str):
    row = SimpleNamespace(
        id=7,
        session_id=41,
        task_id=42,
        task_execution_id=43,
        level="INFO",
        message=message,
        log_metadata=metadata,
        created_at=datetime.now(UTC),
        session_instance_id="f2be8165-00a1-44ee-a956-da5fec04743a",
    )

    class Db:
        def query(self, *entities):
            entity = entities[0]
            if entity is Project:
                project = SimpleNamespace(id=99, name="Synthetic project")
                return _Query([project], first=project)
            if entity is SessionModel.id:
                return _Query([(41,)])
            if entity is LogEntry.level:
                return _Query([("INFO", 1)])
            if entity is LogEntry:
                return _Query([row])
            raise AssertionError(f"Unexpected query entity: {entity!r}")

    return row, Db()


def test_log_stream_serializer_sanitizes_v2_and_preserves_historical_rows():
    canary = "synthetic-response-secret-shaped-003"
    v2_metadata = json.dumps(
        {
            "diagnostic_schema_version": 2,
            "content_omitted": False,
            "diagnostic_event": "openclaw_evaluator_diagnostics",
            "diagnostic_category": canary,
            "stderr_byte_count": 24,
            "stderr_sha256": "a" * 64,
            "stderr_tail": canary,
            "authorization_header": f"Bearer {canary}",
        }
    )
    row, db = _project_log_fixture(v2_metadata, f"assistant response: {canary}")
    streamed = next(LogStreamService(db).stream_logs(session_id=41))

    assert canary not in json.dumps(streamed)
    assert streamed["message"] == (
        "[OPENCLAW][EVALUATOR_RESPONSE_EVIDENCE_DIAGNOSTICS] content omitted"
    )
    assert streamed["metadata"]["content_omitted"] is True
    assert streamed["metadata"]["stderr_byte_count"] == 24
    assert streamed["metadata"]["stderr_sha256"] == "a" * 64
    assert "diagnostic_category" not in streamed["metadata"]

    historical_metadata = json.dumps({"stderr_tail": "synthetic historical value"})
    historical_row, historical_db = _project_log_fixture(
        historical_metadata, "Historical log message"
    )
    historical = next(LogStreamService(historical_db).stream_logs(session_id=41))
    assert historical["message"] == historical_row.message
    assert historical["metadata"] == {"stderr_tail": "synthetic historical value"}


def test_project_log_api_serializes_metadata_only_records(monkeypatch):
    canary = "synthetic-api-response-canary-004"
    metadata = json.dumps(
        {
            "diagnostic_schema_version": 2,
            "content_omitted": True,
            "diagnostic_event": "openclaw_planning_diagnostics",
            "stderr_character_count": 22,
        }
    )
    _, db = _project_log_fixture(metadata, f"assistant: {canary}")
    monkeypatch.setattr(
        LogStreamService,
        "get_project_logs_summary",
        lambda _self, _project_id: {
            "total_logs": 1,
            "by_level": {"INFO": 1},
            "recent_logs": [],
        },
    )

    response = project_logs.get_project_logs(project_id=99, db=db, limit=10)

    assert canary not in json.dumps(response)
    assert response["logs"][0]["message"] == (
        "[OPENCLAW][PLANNING_DIAGNOSTICS] content omitted"
    )
    assert response["logs"][0]["metadata"]["stderr_character_count"] == 22


def test_project_log_summary_uses_safe_v2_message():
    canary = "synthetic-summary-response-canary-005"
    metadata = json.dumps(
        {
            "diagnostic_schema_version": 2,
            "content_omitted": True,
            "diagnostic_event": "openclaw_planning_diagnostics",
        }
    )
    _, db = _project_log_fixture(metadata, canary)
    summary = LogStreamService(db).get_project_logs_summary(project_id=99)

    assert canary not in json.dumps(summary)
    assert summary["recent_logs"][0]["message"] == (
        "[OPENCLAW][PLANNING_DIAGNOSTICS] content omitted"
    )


def test_log_cleanup_keeps_the_existing_30_day_cutoff(monkeypatch):
    class CleanupQuery(_Query):
        def __init__(self):
            super().__init__()
            self.filters = []

        def filter(self, expression, **_kwargs):
            self.filters.append(expression)
            return self

        def delete(self, **_kwargs):
            return 0

    class CleanupDb:
        def __init__(self):
            self.query_result = CleanupQuery()

        def query(self, *_args):
            return self.query_result

        def commit(self):
            pass

        def close(self):
            pass

    db = CleanupDb()
    monkeypatch.setattr(maintenance, "get_db_session", lambda: db)

    result = maintenance.cleanup_old_logs()

    cutoff = db.query_result.filters[0].right.value
    elapsed = datetime.now(UTC) - cutoff
    assert result["days"] == 30
    assert (
        timedelta(days=30) - timedelta(seconds=3)
        <= elapsed
        <= timedelta(days=30, seconds=2)
    )


@pytest.mark.asyncio
async def test_interrupted_stream_logs_only_typed_stderr_metadata(
    monkeypatch, tmp_path
):
    canary = "synthetic-interrupted-stderr-secret-shaped-006"
    body = (
        "import sys, time; "
        f"print({canary!r}, file=sys.stderr, flush=True); "
        "time.sleep(60)"
    )
    service = _execution_service(tmp_path, body)
    monkeypatch.setattr(
        openclaw_service_module,
        "build_git_containment_env",
        lambda: (dict(os.environ), None),
    )
    task = asyncio.create_task(
        service.execute_task_with_streaming(
            "synthetic prompt",
            timeout_seconds=2,
            diagnostic_label="EVALUATOR_RESPONSE_EVIDENCE",
        )
    )
    await asyncio.sleep(0.1)
    task.cancel()

    with pytest.raises(asyncio.CancelledError):
        await task

    serialized_logs = "\n".join(
        f"{entry.message}\n{entry.log_metadata or ''}" for entry in service.db.entries
    )
    assert canary not in serialized_logs
    records = [
        json.loads(entry.log_metadata)
        for entry in service.db.entries
        if entry.log_metadata
    ]
    evaluator_record = next(
        item
        for item in records
        if item.get("diagnostic_event") == "openclaw_evaluator_diagnostics"
    )
    assert evaluator_record["runtime_status"] == "interrupted"
    assert evaluator_record["interrupted"] is True
    chunk_record = next(
        item
        for item in records
        if item.get("diagnostic_event") == "openclaw_output_chunk"
    )
    assert chunk_record["stream_channel"] == "stderr"
    assert chunk_record["stream_byte_count"] > 0
    assert len(chunk_record["stream_sha256"]) == 64


@pytest.mark.asyncio
@pytest.mark.parametrize(
    (
        "body",
        "canary",
        "expected_count_key",
        "expected_count",
        "diagnostic_only_stderr",
    ),
    [
        (
            "pass",
            None,
            "stdout_character_count",
            0,
            False,
        ),
        (
            'import sys; print(\'{"livenessState":"running","durationMs":2}\', file=sys.stderr, flush=True)',
            "livenessState",
            "stderr_character_count",
            None,
            True,
        ),
        (
            "print('{\"payloads\":[', flush=True)",
            "payloads",
            "stdout_character_count",
            None,
            False,
        ),
        (
            "import sys; print('synthetic-nonzero-cli-canary-007', file=sys.stderr, flush=True); sys.exit(9)",
            "synthetic-nonzero-cli-canary-007",
            "cli_return_code",
            9,
            False,
        ),
    ],
)
async def test_cli_edge_output_is_not_persisted(
    monkeypatch,
    tmp_path,
    body,
    canary,
    expected_count_key,
    expected_count,
    diagnostic_only_stderr,
):
    service = _execution_service(tmp_path, body)
    monkeypatch.setattr(
        openclaw_service_module,
        "build_git_containment_env",
        lambda: (dict(os.environ), None),
    )

    await service.execute_task_with_streaming("synthetic prompt", timeout_seconds=2)

    serialized_logs = "\n".join(
        f"{entry.message}\n{entry.log_metadata or ''}" for entry in service.db.entries
    )
    if canary:
        assert canary not in serialized_logs
    process_record = next(
        json.loads(entry.log_metadata)
        for entry in service.db.entries
        if entry.log_metadata
        and json.loads(entry.log_metadata).get("diagnostic_event")
        == "openclaw_process_result"
    )
    if expected_count is None:
        assert process_record[expected_count_key] > 0
    else:
        assert process_record[expected_count_key] == expected_count
    assert process_record["diagnostic_schema_version"] == 2
    assert process_record["content_omitted"] is True
    if diagnostic_only_stderr:
        assert process_record["stderr_diagnostic_only"] is True
