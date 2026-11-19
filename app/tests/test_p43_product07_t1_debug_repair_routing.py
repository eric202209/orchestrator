"""P43-PRODUCT-07-T1: diff-scoped Debug Repair routing (provider-free).

R1 (TaskExecution 405) reached diff-scoped Debug Repair with a role-owned
DEBUG_REPAIR runtime configured. The repair prompt was still sent first to the
tool-capable Execution runtime (local OpenClaw, 180 s, no output), then to the
role-owned runtime through ``execute_task`` without bounded invocation options
(another 180 s). Diff-scoped output is parsed by the same strict bounded
parser, so it must use the role-owned lane and its bounded options directly.
"""

from __future__ import annotations

import json

from app.services.agents.agent_runtime import BackendRole
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.phases.execution_loop import execute_step_loop
from app.services.orchestration.state.persistence import read_orchestration_events
from app.tests.test_phase7f_debug_feedback import (
    _extract_structured_text,
    _FakeDebugRepairRuntime,
    _FakeRuntime,
    _install_role_owned_debug_runtime,
    _make_run_context,
    _normalize_step,
)

DIFF_FIX = {
    "output": json.dumps(
        [
            {
                "repair_type": "ops_fix",
                "ops": [
                    {
                        "op": "replace_in_file",
                        "path": "src/demo.py",
                        "old": "VALUE = 2\n",
                        "new": "VALUE = 1\n",
                    }
                ],
                "verification_command": "python3 -m py_compile src/demo.py",
            }
        ]
    )
}


class _RoleOwnedDebugRuntime(_FakeDebugRepairRuntime):
    """Role-owned runtime that also records any legacy ``execute_task`` use."""

    def __init__(self, responses):
        super().__init__(responses)
        self.execute_task_calls = []

    async def execute_task(self, prompt, timeout_seconds=None, **kwargs):
        self.execute_task_calls.append(kwargs.get("invocation_options"))
        return dict(DIFF_FIX)


def test_diff_scoped_debug_repair_uses_role_owned_runtime_first(
    db_session, tmp_path, monkeypatch
):
    holder = {}

    def fail_after_file_change():
        source = holder["project_dir"] / "src" / "demo.py"
        source.write_text("VALUE = 2\n", encoding="utf-8")
        return {
            "status": "failed",
            "output": "FAILED tests/test_demo.py::test_value",
            "error": "AssertionError: expected 1",
            "files_changed": ["src/demo.py"],
            "returncode": 1,
        }

    # The Execution runtime models R1's OpenClaw agent: if asked to repair, it
    # returns no usable output.
    runtime = _FakeRuntime(
        [
            fail_after_file_change,
            {"output": "", "status": "failed", "error": "agent run timed out"},
        ]
    )
    debug_runtime = _RoleOwnedDebugRuntime([dict(DIFF_FIX)])
    roles = _install_role_owned_debug_runtime(monkeypatch, runtime, debug_runtime)
    ctx, _execution = _make_run_context(
        db_session,
        tmp_path,
        runtime=runtime,
        expected_files=["src/demo.py"],
        step_overrides={"commands": ["custom-test-command"], "verification": ""},
    )
    holder["project_dir"] = ctx.orchestration_state.project_dir
    (holder["project_dir"] / "src").mkdir(parents=True)
    (holder["project_dir"] / "src" / "demo.py").write_text(
        "VALUE = 1\n", encoding="utf-8"
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
    events = read_orchestration_events(
        ctx.orchestration_state.project_dir, ctx.session_id, ctx.task_id
    )
    attempted = [
        event
        for event in events
        if event["event_type"] == EventType.DEBUG_REPAIR_ATTEMPTED
    ]
    assert (
        attempted[-1]["details"]["debug_prompt_mode_architecture"]
        == "diff_scoped_debug_repair"
    )
    assert set(roles) == {BackendRole.DEBUG_REPAIR}
    # The Execution runtime saw only the step, never the repair prompt.
    assert len(runtime.prompts) == 1
    assert not any("Unified diff capsule" in prompt for prompt in runtime.prompts)
    # One role-owned invocation with bounded options; no legacy fallback.
    assert len(debug_runtime.prompts) == 1
    assert "Unified diff capsule" in debug_runtime.prompts[0]
    assert debug_runtime.execute_task_calls == []
    options = debug_runtime.options[0]
    assert options is not None
    assert options.max_output_tokens == 2048
    assert options.temperature == 0.0
    assert options.stream is False
    source = holder["project_dir"] / "src" / "demo.py"
    assert source.read_text(encoding="utf-8") == "VALUE = 1\n"
