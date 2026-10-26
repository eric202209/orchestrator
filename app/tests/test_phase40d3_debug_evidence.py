"""Phase 40D-3: DEBUG provider-response retention is observability only."""

from __future__ import annotations

import copy
import json
import re
from pathlib import Path
from types import SimpleNamespace

import pytest

from app.config import settings
from app.services.agents import agent_runtime
from app.services.agents.providers import openai_chat_adapter
from app.services.agents.providers.openai_chat_adapter import (
    OpenAIChatCompletionsRuntime,
)
from app.services.agents.runtime_invocation import RuntimeInvocationOptions
from app.services.observability import planning_provider_evidence
from app.services.observability.planning_provider_evidence import (
    MAX_RETAINED_VISIBLE_CHARS,
    debug_repair_response_evidence_target,
)
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.phases import execution_loop as execution_loop_module
from app.services.orchestration.phases.execution_loop import execute_step_loop
from app.tests.test_phase7f_debug_feedback import (
    _FakeRuntime,
    _extract_structured_text,
    _make_run_context,
    _normalize_step,
)
from app.tests.test_rer01o_provider_response_capture import _AsyncClient, _body

ADMITTED_REPAIR = json.dumps(
    [
        {
            "title": "Fix test command",
            "command": "python3 -m py_compile tests/test_demo.py",
            "verification_command": "python3 -m py_compile tests/test_demo.py",
        }
    ]
)
STEP_FAILURE = {
    "status": "failed",
    "output": "FAILED tests/test_demo.py::test_import - AssertionError",
    "error": "AssertionError: missing import",
    "returncode": 1,
}


TIMING_KEY = re.compile(r"duration|elapsed|timestamp|_utc$|_at$")
RUN_IDENTITY_KEYS = {"session_id", "task_id", "task_execution_id", "project_id"}


def _scrub(value):
    """Drop per-run identifiers (random request ids, seeded row ids)."""

    if isinstance(value, dict):
        return {
            key: _scrub(item)
            for key, item in value.items()
            if "request_id" not in key
            and key not in RUN_IDENTITY_KEYS
            and not TIMING_KEY.search(key)
        }
    if isinstance(value, list):
        return [_scrub(item) for item in value]
    if isinstance(value, str):
        return re.sub(r"session_\d+_task_\d+(_execution_\d+)?", "<run>", value)
    return value


VALID_DEMO = "def test_import():\n    assert True\n"
BROKEN_DEMO = "def test_import(:\n"


def _run(
    db_session,
    tmp_path,
    monkeypatch,
    *,
    capture,
    debug_content,
    primary,
    demo_source=VALID_DEMO,
):
    """Run one step through the role-owned DEBUG runtime path."""

    run_dir = tmp_path / ("on" if capture else "off")
    run_dir.mkdir()
    monkeypatch.setattr(
        settings, "DEBUG_REPAIR_PROVIDER_RESPONSE_EVIDENCE_CAPTURE", capture
    )
    control = run_dir / "control"
    monkeypatch.setattr(
        planning_provider_evidence,
        "_resolve_control_state_location",
        lambda **_kwargs: control,
    )
    events = []

    def _append(**kwargs):
        # Ids count only pre-existing event types so the added evidence
        # event cannot shift parent ids in the comparison.
        events.append(copy.deepcopy(kwargs))
        if kwargs["event_type"] == EventType.DEBUG_REPAIR_PROVIDER_RESPONSE:
            return {"event_id": "debug-evidence"}
        return {"event_id": f"event-{len(_behavior_events(events))}"}

    monkeypatch.setattr(execution_loop_module, "append_orchestration_event", _append)
    _AsyncClient.body = _body(debug_content)
    _AsyncClient.failure = None
    _AsyncClient.calls = []
    monkeypatch.setattr(
        openai_chat_adapter.httpx, "AsyncClient", lambda **kwargs: _AsyncClient()
    )
    runtime = _FakeRuntime(primary)
    runtime.runtime_configuration = SimpleNamespace(role="execution")

    def _debug_runtime(db, session_id, task_id, *, role, **_kwargs):
        debug = OpenAIChatCompletionsRuntime(db, session_id, task_id)
        debug.backend_role = role.value
        return debug

    monkeypatch.setattr(agent_runtime, "create_agent_runtime", _debug_runtime)
    ctx, execution = _make_run_context(
        db_session,
        run_dir,
        runtime=runtime,
        step_overrides={"commands": ["custom-test-command"], "verification": ""},
    )
    tests_dir = ctx.orchestration_state.project_dir / "tests"
    tests_dir.mkdir(parents=True)
    (tests_dir / "test_demo.py").write_text(demo_source, encoding="utf-8")
    try:
        result = execute_step_loop(
            ctx=ctx,
            extract_structured_text=_extract_structured_text,
            normalize_step=_normalize_step,
            normalize_plan_with_live_logging=lambda *args, **kwargs: [],
            workspace_violation_error_cls=RuntimeError,
            write_project_state_snapshot_fn=lambda *args, **kwargs: None,
            record_live_log_fn=lambda *args, **kwargs: None,
        )
    except Exception as exc:  # terminal classification is compared, not hidden
        result = {"raised": type(exc).__name__, "message": str(exc)}
    project_dir = ctx.orchestration_state.project_dir
    workspace = {
        str(path.relative_to(project_dir)): path.read_bytes()
        for path in sorted(project_dir.rglob("*"))
        if path.is_file()
        and not {".agent", "__pycache__"} & set(path.relative_to(project_dir).parts)
    }
    return SimpleNamespace(
        result=result,
        execution=execution,
        events=events,
        provider_requests=[call["json"] for call in _AsyncClient.calls],
        primary_prompts=list(runtime.prompts),
        plan=copy.deepcopy(ctx.orchestration_state.plan),
        debug_ids=list(ctx.orchestration_state.debug_repair_task_execution_ids),
        step_index=ctx.orchestration_state.current_step_index,
        workspace=workspace,
        control=control,
    )


def _behavior_events(events):
    return [
        event
        for event in events
        if event["event_type"] != EventType.DEBUG_REPAIR_PROVIDER_RESPONSE
    ]


def _behavior(run):
    """Everything DEBUG behavior is defined by, minus the new evidence event."""

    events = []
    for event in _behavior_events(run.events):
        event = _scrub(dict(event))
        event.pop("project_dir", None)
        events.append(event)
    result = _scrub(run.result)
    return {
        "result": result,
        "provider_request_count": len(run.provider_requests),
        "provider_requests": run.provider_requests,
        "primary_invocation_order": [prompt[:200] for prompt in run.primary_prompts],
        "event_order": [event["event_type"] for event in events],
        "events": events,
        "plan": run.plan,
        "debug_repair_budget_used": len(run.debug_ids),
        "current_step_index": run.step_index,
        "workspace": run.workspace,
    }


def _first_difference(left, right, path="$"):
    if type(left) is not type(right):
        return f"{path}: {left!r:.200} != {right!r:.200}"
    if isinstance(left, dict):
        for key in sorted(set(left) | set(right), key=str):
            if left.get(key) != right.get(key):
                return _first_difference(left.get(key), right.get(key), f"{path}.{key}")
    if isinstance(left, list):
        if len(left) != len(right):
            return f"{path}: length {len(left)} != {len(right)}"
        for index, (a, b) in enumerate(zip(left, right)):
            if a != b:
                return _first_difference(a, b, f"{path}[{index}]")
    return None if left == right else f"{path}: {left!r:.200} != {right!r:.200}"


def _evidence_events(run):
    return [
        event
        for event in run.events
        if event["event_type"] == EventType.DEBUG_REPAIR_PROVIDER_RESPONSE
    ]


@pytest.mark.parametrize(
    "debug_content, primary, demo_source, terminal",
    [
        # Admitted command repair that is retried and succeeds.
        (
            ADMITTED_REPAIR,
            [STEP_FAILURE],
            VALID_DEMO,
            {"status": "completed"},
        ),
        # Admitted repair whose retry fails again: DEBUG budget exhausted.
        (
            ADMITTED_REPAIR,
            [STEP_FAILURE],
            BROKEN_DEMO,
            {"status": "failed", "reason": "debug_repair_budget_exhausted"},
        ),
        # Unparseable DEBUG output is rejected before any mutation.
        (
            "this is not a repair",
            [STEP_FAILURE],
            VALID_DEMO,
            {"status": "failed", "reason": "debug_parse_error"},
        ),
    ],
    ids=["admitted_success", "budget_exhausted", "parse_rejected"],
)
def test_capture_on_off_preserves_debug_behavior(
    db_session, tmp_path, monkeypatch, debug_content, primary, demo_source, terminal
):
    off = _run(
        db_session,
        tmp_path,
        monkeypatch,
        capture=False,
        debug_content=debug_content,
        primary=copy.deepcopy(primary),
        demo_source=demo_source,
    )
    on = _run(
        db_session,
        tmp_path,
        monkeypatch,
        capture=True,
        debug_content=debug_content,
        primary=copy.deepcopy(primary),
        demo_source=demo_source,
    )

    assert _behavior(on) == _behavior(off), _first_difference(
        _behavior(off), _behavior(on)
    )
    assert len(off.provider_requests) == 1
    assert _scrub(off.result) == terminal
    assert not _evidence_events(off)
    assert not (off.control / ".agent").exists() or not list(
        off.control.rglob("debug-repair-evidence")
    )

    [event] = _evidence_events(on)
    details = event["details"]
    assert details["capture_status"] == "complete"
    assert details["invocation_path"] == "role_owned_runtime"
    assert details["response_content_retained"] is True
    assert details["task_execution_id"] == on.execution.id
    assert details["debug_repair_invocation_index"] == 1
    request_id = details["debug_repair_request_id"]
    evidence_path = Path(details["evidence_path"])
    assert evidence_path.parts[-4:] == (
        "debug-repair-evidence",
        f"execution-{on.execution.id}",
        request_id,
        "provider-response.json",
    )
    document = json.loads(evidence_path.read_text())
    assert document["schema_version"] == "debug_repair_provider_response_evidence.v1"
    assert document["capture_scope"] == "debug_repair"
    assert document["provider"]["role"] == "debug_repair"
    assert document["correlation"]["provider_call_id"] == f"debug-repair-{request_id}"
    assert document["representations"]["assistant_content"]["value"] == debug_content
    assert document["debug_repair"]["identity"]["debug_repair_request_id"] == request_id
    assert document["debug_repair"]["identity"]["task_execution_id"] == (
        on.execution.id
    )
    assert document["evidence_persistence"]["status"] == "complete"
    parse = document["debug_repair"]["parse"]
    rejected = [e for e in on.events if e["event_type"] == EventType.REPAIR_REJECTED]
    if debug_content == ADMITTED_REPAIR:
        assert parse["status"] == "accepted"
        generated = [
            e for e in on.events if e["event_type"] == EventType.REPAIR_GENERATED
        ]
        assert parse["repair_output_sha256"] == (
            generated[0]["details"]["repair_output_sha256"]
        )
    else:
        assert parse["status"] == "rejected"
        assert rejected[0]["details"]["debug_repair_request_ids"] == [request_id]
        assert parse["repair_output_sha256"] == (
            rejected[0]["details"]["repair_output_sha256"]
        )


def test_persistence_failure_is_visible_and_never_success(
    db_session, tmp_path, monkeypatch
):
    primary = [STEP_FAILURE, {"status": "success", "output": "ok"}]
    off = _run(
        db_session,
        tmp_path,
        monkeypatch,
        capture=False,
        debug_content=ADMITTED_REPAIR,
        primary=copy.deepcopy(primary),
    )

    def _fail(*_args, **_kwargs):
        raise OSError("disk full")

    monkeypatch.setattr(planning_provider_evidence, "_write_json", _fail)
    on = _run(
        db_session,
        tmp_path,
        monkeypatch,
        capture=True,
        debug_content=ADMITTED_REPAIR,
        primary=copy.deepcopy(primary),
    )

    assert _behavior(on) == _behavior(off)
    [event] = _evidence_events(on)
    assert event["details"]["capture_status"] == "missing"
    assert event["details"]["response_content_retained"] is False
    assert not Path(event["details"]["evidence_path"]).exists()


def test_primary_runtime_path_reports_capture_unavailable(
    db_session, tmp_path, monkeypatch
):
    monkeypatch.setattr(
        settings, "DEBUG_REPAIR_PROVIDER_RESPONSE_EVIDENCE_CAPTURE", True
    )
    events = []
    monkeypatch.setattr(
        execution_loop_module,
        "append_orchestration_event",
        lambda **kwargs: events.append(kwargs) or {"event_id": f"event-{len(events)}"},
    )
    runtime = _FakeRuntime(
        [STEP_FAILURE, {"output": ADMITTED_REPAIR}, {"status": "success"}]
    )
    ctx, execution = _make_run_context(
        db_session,
        tmp_path,
        runtime=runtime,
        step_overrides={"commands": ["custom-test-command"], "verification": ""},
    )
    (ctx.orchestration_state.project_dir / "tests").mkdir(parents=True)
    (ctx.orchestration_state.project_dir / "tests" / "test_demo.py").write_text(
        "def test_import():\n    assert True\n", encoding="utf-8"
    )

    result = execute_step_loop(
        ctx=ctx,
        extract_structured_text=_extract_structured_text,
        normalize_step=_normalize_step,
        normalize_plan_with_live_logging=lambda *args, **kwargs: [],
        workspace_violation_error_cls=RuntimeError,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        record_live_log_fn=lambda *args, **kwargs: None,
    )

    assert result["status"] == "completed", result
    [event] = [
        e for e in events if e["event_type"] == EventType.DEBUG_REPAIR_PROVIDER_RESPONSE
    ]
    assert event["details"]["capture_status"] == "not_captured:primary_runtime_path"
    assert event["details"]["evidence_path"] is None
    assert event["details"]["task_execution_id"] == execution.id


def test_target_binds_task_execution_and_request(tmp_path, monkeypatch):
    monkeypatch.setattr(
        planning_provider_evidence,
        "_resolve_control_state_location",
        lambda **_kwargs: tmp_path,
    )
    runtime = SimpleNamespace(db=None, task_id=3, project_id=8)

    first = debug_repair_response_evidence_target(
        runtime, task_execution_id=31, request_id="req-a"
    )
    second = debug_repair_response_evidence_target(
        runtime, task_execution_id=32, request_id="req-a"
    )

    assert first[0] != second[0]
    assert "/debug-repair-evidence/execution-31/req-a/" in first[0]
    assert "/debug-repair-evidence/execution-32/req-a/" in second[0]
    assert first[1] == "debug-repair-req-a"
    assert (
        debug_repair_response_evidence_target(
            runtime, task_execution_id=None, request_id="req-a"
        )
        is None
    )
    assert (
        debug_repair_response_evidence_target(
            runtime, task_execution_id=31, request_id=""
        )
        is None
    )


def _adapter_capture(tmp_path, monkeypatch, *, role, content):
    _AsyncClient.body = _body(content)
    _AsyncClient.failure = None
    _AsyncClient.calls = []
    monkeypatch.setattr(
        openai_chat_adapter.httpx, "AsyncClient", lambda **kwargs: _AsyncClient()
    )
    runtime = OpenAIChatCompletionsRuntime(None, session_id=None)
    runtime.backend_role = role
    path = tmp_path / role / "provider-response.json"
    import asyncio

    result = asyncio.run(
        runtime.invoke_prompt(
            "repair prompt",
            invocation_options=RuntimeInvocationOptions(
                timeout_seconds=30,
                max_output_tokens=64,
                temperature=0.0,
                reasoning_enabled=False,
                stream=False,
                provider_response_evidence_path=str(path),
                provider_response_evidence_correlation_id="debug-repair-req",
            ),
        )
    )
    return result, json.loads(path.read_text())


def test_debug_capture_is_secret_minimized_and_size_bounded(tmp_path, monkeypatch):
    secret = "sk-" + "a" * 32
    content = f"api_key={secret} " + "x" * (MAX_RETAINED_VISIBLE_CHARS + 500)
    result, document = _adapter_capture(
        tmp_path, monkeypatch, role="debug_repair", content=content
    )

    assert result["output"] == content
    serialized = json.dumps(document)
    assert secret not in serialized
    assistant = document["representations"]["assistant_content"]
    assert assistant["redacted"] is True
    assert assistant["truncated"] is True
    assert len(assistant["value"]) == MAX_RETAINED_VISIBLE_CHARS
    assert assistant["length"] > MAX_RETAINED_VISIBLE_CHARS
    assert "Authorization" not in serialized


def test_planning_repair_capture_scope_is_unchanged(tmp_path, monkeypatch):
    content = "y" * (MAX_RETAINED_VISIBLE_CHARS + 10)
    _result, document = _adapter_capture(
        tmp_path, monkeypatch, role="repair", content=content
    )

    assert document["schema_version"] == (
        "planning_repair_provider_response_evidence.v1"
    )
    assert document["capture_scope"] == "planning_repair"
    assistant = document["representations"]["assistant_content"]
    assert "truncated" not in assistant
    assert assistant["value"] == content
    assert "debug_repair" not in document
