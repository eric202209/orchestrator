import asyncio
import hashlib
import logging
import json

import pytest
from app.services.orchestration.planning.planner import (
    PlannerService,
    PlanningRepairNoOutputTimeout,
    PlanningRepairOutputContractViolation,
)

from app.services.orchestration.policy import (
    MINIMAL_PLANNING_TIMEOUT_SECONDS,
    PLANNING_REPAIR_TIMEOUT_SECONDS,
)

from app.services.agents.openclaw_service import OpenClawSessionService


def test_openclaw_invocation_metadata_redacts_prompt_and_captures_flags():
    metadata = OpenClawSessionService._openclaw_invocation_metadata(
        full_cmd=[
            "/usr/bin/openclaw",
            "agent",
            "--local",
            "--session-id",
            "planning-repair-123",
            "--message",
            "secret prompt",
            "--json",
            "--timeout",
            "240",
        ],
        prompt="secret prompt",
        timeout_seconds=240,
        cwd="/tmp/isolated",
        invocation_kind="planning-repair",
        isolate_workspace_context=True,
        no_output_timeout_seconds=200,
    )

    assert metadata["executable_path"] == "/usr/bin/openclaw"
    assert metadata["subcommand"] == "agent"
    assert metadata["has_local_flag"] is True
    assert metadata["has_json_flag"] is True
    assert metadata["timeout_arg"] == "240"
    assert metadata["session_id_prefix"] == "planning-repair"
    assert metadata["session_id_shape"] == "planning-repair-000"
    assert metadata["cwd"] == "/tmp/isolated"
    assert metadata["isolate_workspace_context"] is True
    assert metadata["prompt_size"] == len("secret prompt")
    assert (
        metadata["prompt_sha256_12"]
        == hashlib.sha256(b"secret prompt").hexdigest()[:12]
    )
    assert metadata["no_output_timeout_seconds"] == 200
    assert "secret prompt" not in json.dumps(metadata)


def test_phase7f_openclaw_diagnostics_classify_boundary_and_redact_stream_tail():
    assert (
        OpenClawSessionService._diagnostic_invocation_kind("PHASE7F_DEBUG_REPAIR")
        == "debug_repair"
    )
    assert (
        OpenClawSessionService._diagnostic_invocation_kind(
            "BOUNDED_EXECUTION_DEBUG_REPAIR"
        )
        == "debug_repair"
    )
    assert (
        OpenClawSessionService._diagnostic_timeout_boundary("PHASE7F_DEBUG_REPAIR")
        == "debug_repair_wait_for"
    )
    assert (
        OpenClawSessionService._diagnostic_timeout_boundary(
            "BOUNDED_EXECUTION_DEBUG_REPAIR"
        )
        == "debug_repair_wait_for"
    )
    assert OpenClawSessionService._diagnostic_invocation_kind("PLANNING") == "planning"
    assert (
        OpenClawSessionService._diagnostic_timeout_boundary("PLANNING")
        == "planning_wait_for"
    )

    tail = OpenClawSessionService._diagnostic_text_tail(
        "[tools] read failed\nAuthorization: Bearer abc.def.ghi\npassword=secret"
    )

    assert "[tools] read failed" in tail
    assert "abc.def.ghi" not in tail
    assert "password=secret" not in tail
    assert "password=<redacted>" in tail


def test_planning_repair_timeout_uses_effective_runtime_profile_timeout(monkeypatch):
    from app.services.orchestration.planning import planner as planner_module

    monkeypatch.setattr(
        planner_module.settings,
        "PLANNING_REPAIR_TIMEOUT_SECONDS",
        45,
    )
    captured = {}

    class Runtime:
        async def invoke_prompt(self, prompt, **kwargs):
            captured["timeout_seconds"] = kwargs["timeout_seconds"]
            captured["no_output_timeout_seconds"] = kwargs["no_output_timeout_seconds"]
            return {"output": '[{"step_number":1}]'}

    PlannerService.repair_output(
        runtime_service=Runtime(),
        task_description="Build a page",
        malformed_output='[{"step_number":1,"commands":["touch index.html"]}]',
        project_dir=__import__("pathlib").Path("/tmp/project"),
        timeout_seconds=300,
        logger=__import__("logging").getLogger("test"),
        emit_live=lambda *a, **kw: None,
        reason="plan_validation_failed",
        rejection_reasons=["Plan contains brittle heredoc-heavy commands"],
        knowledge_context=None,
        session_id=1,
        task_id=2,
    )

    assert captured["timeout_seconds"] == 45
    assert captured["no_output_timeout_seconds"] == 45
    assert captured["timeout_seconds"] < MINIMAL_PLANNING_TIMEOUT_SECONDS


def test_stale_replace_planning_repair_uses_extended_timeout_margin(
    monkeypatch, tmp_path
):
    from app.services.orchestration.planning import planner as planner_module

    monkeypatch.setattr(planner_module.settings, "PLANNING_REPAIR_ENABLED", False)
    monkeypatch.setattr(
        planner_module,
        "STALE_REPLACE_REPAIR_DIAGNOSTIC_DIR",
        tmp_path / "planning-stale-replace-repair",
    )
    captured = {}
    events = []

    class Runtime:
        async def invoke_prompt(self, prompt, **kwargs):
            captured["timeout_seconds"] = kwargs["timeout_seconds"]
            captured["no_output_timeout_seconds"] = kwargs["no_output_timeout_seconds"]
            return {"output": '[{"step_number":1}]'}

    PlannerService.repair_output(
        runtime_service=Runtime(),
        task_description="Build a page",
        malformed_output='[{"step_number":1,"commands":["touch index.html"]}]',
        project_dir=tmp_path,
        timeout_seconds=300,
        logger=logging.getLogger("test.stale_replace_timeout_margin"),
        emit_live=lambda level, message, metadata=None: events.append(
            (level, message, metadata or {})
        ),
        reason="post_repair_stale_replace_fallback: stale_replace_ops_steps",
        rejection_reasons=["replace_in_file old text not found in workspace"],
        knowledge_context=None,
        session_id=1,
        task_id=2,
    )

    assert captured["timeout_seconds"] == 120.0
    assert captured["no_output_timeout_seconds"] == 120
    running_events = [
        metadata
        for _level, message, metadata in events
        if "Planning repair attempt is now running" in message
    ]
    assert running_events[-1]["timeout_seconds"] == 120.0
    assert running_events[-1]["stale_replace_timeout_margin"] is True
    assert (
        running_events[-1]["repair_timeout_margin_reason"]
        == "post_repair_stale_replace_fallback"
    )


def test_first_pass_stale_replace_repair_uses_extended_timeout_margin(
    monkeypatch, tmp_path
):
    from app.services.orchestration.planning import planner as planner_module

    monkeypatch.setattr(planner_module.settings, "PLANNING_REPAIR_ENABLED", False)
    captured = {}
    events = []

    class Runtime:
        async def invoke_prompt(self, prompt, **kwargs):
            captured["timeout_seconds"] = kwargs["timeout_seconds"]
            captured["no_output_timeout_seconds"] = kwargs["no_output_timeout_seconds"]
            return {"output": '[{"step_number":1}]'}

    PlannerService.repair_output(
        runtime_service=Runtime(),
        task_description="Build a page",
        malformed_output='[{"step_number":1,"commands":["touch index.html"]}]',
        project_dir=tmp_path,
        timeout_seconds=300,
        logger=logging.getLogger("test.first_pass_stale_replace_timeout_margin"),
        emit_live=lambda level, message, metadata=None: events.append(
            (level, message, metadata or {})
        ),
        reason=(
            "plan_contains_immediate_repair_issues: replace_in_file old text "
            "not found in workspace in steps [3]"
        ),
        rejection_reasons=["replace_in_file old text not found in workspace"],
        knowledge_context=None,
        session_id=1,
        task_id=2,
    )

    assert captured["timeout_seconds"] == 120.0
    assert captured["no_output_timeout_seconds"] == 120
    running_events = [
        metadata
        for _level, message, metadata in events
        if "Planning repair attempt is now running" in message
    ]
    assert running_events[-1]["timeout_seconds"] == 120.0
    assert running_events[-1]["stale_replace_timeout_margin"] is True
    assert (
        running_events[-1]["repair_timeout_margin_reason"]
        == "first_pass_stale_replace_old_text"
    )


def test_normal_planning_repair_timeout_remains_default(monkeypatch):
    from app.services.orchestration.planning import planner as planner_module

    monkeypatch.setattr(planner_module.settings, "PLANNING_REPAIR_ENABLED", False)
    monkeypatch.setattr(planner_module.settings, "PLANNING_REPAIR_TIMEOUT_SECONDS", 90)
    captured = {}

    class Runtime:
        async def invoke_prompt(self, prompt, **kwargs):
            captured["timeout_seconds"] = kwargs["timeout_seconds"]
            return {"output": '[{"step_number":1}]'}

    PlannerService.repair_output(
        runtime_service=Runtime(),
        task_description="Build a page",
        malformed_output='[{"step_number":1,"commands":["touch index.html"]}]',
        project_dir=__import__("pathlib").Path("/tmp/project"),
        timeout_seconds=300,
        logger=logging.getLogger("test.normal_repair_timeout"),
        emit_live=lambda *a, **kw: None,
        reason="plan_validation_failed",
        rejection_reasons=["Plan contains brittle heredoc-heavy commands"],
        knowledge_context=None,
        session_id=1,
        task_id=2,
    )

    assert captured["timeout_seconds"] == 90


def test_first_pass_non_stale_repair_timeout_remains_default(monkeypatch):
    from app.services.orchestration.planning import planner as planner_module

    monkeypatch.setattr(planner_module.settings, "PLANNING_REPAIR_ENABLED", False)
    monkeypatch.setattr(planner_module.settings, "PLANNING_REPAIR_TIMEOUT_SECONDS", 90)
    captured = {}
    events = []

    class Runtime:
        async def invoke_prompt(self, prompt, **kwargs):
            captured["timeout_seconds"] = kwargs["timeout_seconds"]
            return {"output": '[{"step_number":1}]'}

    PlannerService.repair_output(
        runtime_service=Runtime(),
        task_description="Build a page",
        malformed_output='[{"step_number":1,"commands":["touch index.html"]}]',
        project_dir=__import__("pathlib").Path("/tmp/project"),
        timeout_seconds=300,
        logger=logging.getLogger("test.first_pass_non_stale_repair_timeout"),
        emit_live=lambda level, message, metadata=None: events.append(
            (level, message, metadata or {})
        ),
        reason="plan_contains_immediate_repair_issues: brittle_inline_python",
        rejection_reasons=["Plan contains brittle heredoc-heavy commands"],
        knowledge_context=None,
        session_id=1,
        task_id=2,
    )

    assert captured["timeout_seconds"] == 90
    running_events = [
        metadata
        for _level, message, metadata in events
        if "Planning repair attempt is now running" in message
    ]
    assert running_events[-1]["stale_replace_timeout_margin"] is False
    assert running_events[-1]["repair_timeout_margin_reason"] is None


def test_planning_repair_logs_duration(caplog):
    events = []

    class Runtime:
        async def invoke_prompt(self, prompt, **kwargs):
            return {"output": '[{"step_number":1}]'}

    caplog.set_level(logging.INFO, logger="test.planning_repair_duration")

    PlannerService.repair_output(
        runtime_service=Runtime(),
        task_description="Build a page",
        malformed_output='[{"step_number":1,"commands":["touch index.html"]}]',
        project_dir=__import__("pathlib").Path("/tmp/project"),
        timeout_seconds=300,
        logger=logging.getLogger("test.planning_repair_duration"),
        emit_live=lambda *args, **kwargs: events.append((args, kwargs)),
        reason="plan_validation_failed",
        rejection_reasons=["Plan contains brittle heredoc-heavy commands"],
        knowledge_context=None,
        session_id=1,
        task_id=2,
    )

    assert "Planning repair completed in" in caplog.text
    duration_events = [
        kwargs["metadata"]
        for args, kwargs in events
        if args
        and args[0] == "INFO"
        and str(args[1]).startswith("[ORCHESTRATION] Planning repair completed in ")
    ]
    assert duration_events
    assert duration_events[0]["duration_seconds"] >= 0
    assert duration_events[0]["timeout_seconds"] == (
        PlannerService._effective_planning_repair_timeout(300)
    )
    assert "repair_prompt_build_seconds" in duration_events[0]
    assert "openclaw_request_seconds" in duration_events[0]
    assert "parser_validation_seconds" in duration_events[0]
    assert duration_events[0]["repair_attempts"] == 1
    assert duration_events[0]["repair_output_chars"] > 0
    assert duration_events[0]["planning_lock_wait_seconds"] >= 0


def test_planning_repair_timeout_emits_runtime_diagnostics(monkeypatch):
    from app.services.orchestration import planning as planning_pkg

    original_timeout = planning_pkg.planner.PLANNING_REPAIR_TIMEOUT_SECONDS
    planning_pkg.planner.PLANNING_REPAIR_TIMEOUT_SECONDS = 0.01
    events = []

    class Runtime:
        async def invoke_prompt(self, prompt, **kwargs):
            await asyncio.sleep(1)
            return {"output": '[{"step_number":1}]'}

    try:
        with pytest.raises(TimeoutError):
            PlannerService.repair_output(
                runtime_service=Runtime(),
                task_description="Build a page",
                malformed_output='{"steps":"bad"}',
                project_dir=__import__("pathlib").Path("/tmp/project"),
                timeout_seconds=300,
                logger=logging.getLogger("test.planning_repair_diagnostics"),
                emit_live=lambda level, message, metadata=None: events.append(
                    (level, message, metadata or {})
                ),
                reason="json_parse_failed",
                rejection_reasons=["commands must be an array"],
                knowledge_context=None,
                session_id=1,
                task_id=2,
            )
    finally:
        planning_pkg.planner.PLANNING_REPAIR_TIMEOUT_SECONDS = original_timeout

    diagnostics_events = [
        metadata
        for level, message, metadata in events
        if level == "ERROR"
        and message
        == "[ORCHESTRATION] Planning repair diagnostics captured timeout boundary"
    ]
    assert diagnostics_events
    metadata = diagnostics_events[0]
    assert metadata["reason"] == "malformed_planning_output_repair_timeout"
    assert metadata["timeout_boundary"] == "planner_wait_for"
    assert metadata["repair_attempts"] == 1
    assert metadata["repair_prompt_chars"] > 0
    assert metadata["malformed_output_chars"] > 0
    assert metadata["repair_prompt_build_seconds"] >= 0
    assert metadata["openclaw_request_seconds"] >= 0
    assert metadata["planning_lock_wait_seconds"] >= 0


def test_planning_repair_lock_wait_timeout_emits_attribution(tmp_path, monkeypatch):
    from app.services.orchestration.planning import planner as planner_module

    events = []
    called_runtime = False
    lock_path = tmp_path / "planning.lock"

    monkeypatch.setattr(planner_module, "OPENCLAW_PLANNING_LOCK_PATH", lock_path)
    monkeypatch.setattr(
        planner_module, "OPENCLAW_PLANNING_LOCK_ACQUIRE_TIMEOUT_SECONDS", 0.01
    )
    monkeypatch.setattr(planner_module, "OPENCLAW_PLANNING_LOCK_POLL_SECONDS", 0.001)

    def busy_flock(_fd, flags):
        if flags & planner_module.fcntl.LOCK_NB:
            raise BlockingIOError(planner_module.errno.EAGAIN, "busy")
        return None

    monkeypatch.setattr(planner_module.fcntl, "flock", busy_flock)

    class Runtime:
        async def invoke_prompt(self, prompt, **kwargs):
            nonlocal called_runtime
            called_runtime = True
            return {"output": '[{"step_number":1}]'}

    with pytest.raises(TimeoutError):
        PlannerService.repair_output(
            runtime_service=Runtime(),
            task_description="Build a page",
            malformed_output='{"steps":"bad"}',
            project_dir=tmp_path,
            timeout_seconds=300,
            logger=logging.getLogger("test.planning_repair_lock_wait_timeout"),
            emit_live=lambda level, message, metadata=None: events.append(
                (level, message, metadata or {})
            ),
            reason="json_parse_failed",
            rejection_reasons=["commands must be an array"],
            knowledge_context=None,
            session_id=1,
            task_id=2,
        )

    assert called_runtime is False
    diagnostics_events = [
        metadata
        for level, message, metadata in events
        if level == "ERROR"
        and message
        == "[ORCHESTRATION] Planning repair diagnostics captured timeout boundary"
    ]
    assert diagnostics_events
    metadata = diagnostics_events[0]
    assert metadata["reason"] == "malformed_planning_output_repair_timeout"
    assert metadata["timeout_boundary"] == "planner_wait_for"
    assert metadata["planning_lock_wait_seconds"] >= 0.01


def test_planning_repair_no_output_timeout_classification():
    events = []
    attempts = {"count": 0}

    class Runtime:
        async def invoke_prompt(self, prompt, **kwargs):
            attempts["count"] += 1
            exc = RuntimeError("OpenClaw prompt produced no output before 30s")
            exc.runtime_diagnostics = {
                "no_output_timeout": True,
                "timeout_boundary": "repair_no_output",
                "first_output_after_seconds": None,
                "stdout_chars": 0,
                "stderr_chars": 0,
                "return_code": -9,
                "cancelled": True,
            }
            raise exc

    with pytest.raises(PlanningRepairNoOutputTimeout) as exc_info:
        PlannerService.repair_output(
            runtime_service=Runtime(),
            task_description="Build a page",
            malformed_output='{"steps":"bad"}',
            project_dir=__import__("pathlib").Path("/tmp/project"),
            timeout_seconds=300,
            logger=logging.getLogger("test.planning_repair_no_output_timeout"),
            emit_live=lambda level, message, metadata=None: events.append(
                (level, message, metadata or {})
            ),
            reason="json_parse_failed",
            rejection_reasons=["commands must be an array"],
            knowledge_context=None,
            session_id=1,
            task_id=2,
        )

    assert "no output" in str(exc_info.value).lower()
    assert attempts["count"] == 2
    assert exc_info.value.runtime_diagnostics["return_code"] == -9
    retry_events = [
        metadata
        for level, message, metadata in events
        if level == "WARN"
        and metadata.get("reason") == "planning_repair_no_output_retry"
    ]
    assert retry_events
    assert retry_events[0]["next_repair_attempt"] == 2
    assert retry_events[0]["next_strategy"] == "compact_repair_prompt"
    no_output_events = [
        metadata
        for level, message, metadata in events
        if level == "ERROR"
        and message
        == (
            "[ORCHESTRATION] Repair prompt was built, but OpenClaw "
            "produced no output before timeout."
        )
    ]
    assert no_output_events
    metadata = no_output_events[0]
    assert metadata["reason"] == "planning_repair_no_output_timeout"
    assert metadata["repair_attempts"] == 2
    assert metadata["first_output_delay"] is None
    assert metadata["stdout_chars"] == 0
    assert metadata["stderr_chars"] == 0
    assert metadata["return_code"] == -9
    assert metadata["cancelled"] is True
    assert metadata["timeout_boundary"] == "repair_no_output"
    assert metadata["planning_lock_wait_seconds"] >= 0


def test_planning_repair_no_output_retry_can_succeed():
    attempts = {"count": 0}
    prompts = []

    class Runtime:
        async def invoke_prompt(self, prompt, **kwargs):
            attempts["count"] += 1
            prompts.append(prompt)
            if attempts["count"] == 1:
                exc = RuntimeError("OpenClaw prompt produced no output before 30s")
                exc.runtime_diagnostics = {
                    "no_output_timeout": True,
                    "timeout_boundary": "repair_no_output",
                    "stdout_chars": 0,
                    "stderr_chars": 0,
                }
                raise exc
            return {"output": '[{"step_number":1}]'}

    result = PlannerService.repair_output(
        runtime_service=Runtime(),
        task_description="Build a page",
        malformed_output='{"steps":"bad"}',
        project_dir=__import__("pathlib").Path("/tmp/project"),
        timeout_seconds=300,
        logger=logging.getLogger("test.planning_repair_no_output_retry_success"),
        emit_live=lambda *args, **kwargs: None,
        reason="json_parse_failed",
        rejection_reasons=["commands must be an array"],
        knowledge_context=None,
        session_id=1,
        task_id=2,
    )

    assert attempts["count"] == 2
    assert len(prompts[1]) < len(prompts[0])
    assert "Repair this invalid plan into 3 to 4 executable steps." in prompts[1]
    assert result == {"output": '[{"step_number":1}]'}


def test_planning_repair_returned_prose_raises_output_contract_violation():
    events = []
    attempts = {"count": 0}

    class Runtime:
        async def invoke_prompt(self, prompt, **kwargs):
            attempts["count"] += 1
            return {"output": "I repaired the plan. Here are the steps..."}

    with pytest.raises(PlanningRepairOutputContractViolation) as exc_info:
        PlannerService.repair_output(
            runtime_service=Runtime(),
            task_description="Build a page",
            malformed_output='{"steps":"bad"}',
            project_dir=__import__("pathlib").Path("/tmp/project"),
            timeout_seconds=300,
            logger=logging.getLogger("test.planning_repair_returned_prose"),
            emit_live=lambda level, message, metadata=None: events.append(
                (level, message, metadata or {})
            ),
            reason="json_parse_failed",
            rejection_reasons=["commands must be an array"],
            knowledge_context=None,
            session_id=1,
            task_id=2,
        )

    assert attempts["count"] == 1
    assert exc_info.value.runtime_diagnostics["output_contract_violated"] is True
    assert exc_info.value.runtime_diagnostics["repair_output_fenced"] is False
    assert "prose" in str(exc_info.value)
    contract_events = [
        metadata
        for _, _, metadata in events
        if metadata.get("reason") == "repair_output_contract_violation"
    ]
    assert contract_events
    assert contract_events[0]["repair_attempts"] == 1


def test_planning_repair_returned_fenced_json_is_normalized_before_parsing():
    events = []
    fenced_payload = '[{"step": 1, "commands": ["echo hi"], "verification": "echo hi"}]'

    class Runtime:
        async def invoke_prompt(self, prompt, **kwargs):
            return {"output": f"```json\n{fenced_payload}\n```"}

    result = PlannerService.repair_output(
        runtime_service=Runtime(),
        task_description="Build a page",
        malformed_output='{"steps":"bad"}',
        project_dir=__import__("pathlib").Path("/tmp/project"),
        timeout_seconds=300,
        logger=logging.getLogger("test.planning_repair_fenced_json"),
        emit_live=lambda level, message, metadata=None: events.append(
            (level, message, metadata or {})
        ),
        reason="json_parse_failed",
        rejection_reasons=["commands must be an array"],
        knowledge_context=None,
        session_id=1,
        task_id=2,
    )

    assert result["output"] == fenced_payload
    assert any(
        metadata.get("reason") == "planning_repair_fenced_json_normalized"
        for _, _, metadata in events
    )


def test_planning_repair_bare_json_array_does_not_raise_output_contract_violation():
    events = []

    class Runtime:
        async def invoke_prompt(self, prompt, **kwargs):
            return {
                "output": '[{"step": 1, "commands": ["echo hi"], "verification": "echo hi"}]'
            }

    result = PlannerService.repair_output(
        runtime_service=Runtime(),
        task_description="Build a page",
        malformed_output='{"steps":"bad"}',
        project_dir=__import__("pathlib").Path("/tmp/project"),
        timeout_seconds=300,
        logger=logging.getLogger("test.planning_repair_bare_json"),
        emit_live=lambda level, message, metadata=None: events.append(
            (level, message, metadata or {})
        ),
        reason="json_parse_failed",
        rejection_reasons=["commands must be an array"],
        knowledge_context=None,
        session_id=1,
        task_id=2,
    )

    assert result is not None
    contract_events = [
        metadata
        for _, _, metadata in events
        if metadata.get("reason") == "repair_output_contract_violation"
    ]
    assert not contract_events


def test_planning_repair_no_output_skips_parser_validation_metadata():
    events = []

    class Runtime:
        async def invoke_prompt(self, prompt, **kwargs):
            exc = RuntimeError("OpenClaw prompt produced no output before 30s")
            exc.runtime_diagnostics = {
                "no_output_timeout": True,
                "timeout_boundary": "repair_no_output",
                "stdout_chars": 0,
                "stderr_chars": 0,
            }
            raise exc

    with pytest.raises(PlanningRepairNoOutputTimeout):
        PlannerService.repair_output(
            runtime_service=Runtime(),
            task_description="Build a page",
            malformed_output='{"steps":"bad"}',
            project_dir=__import__("pathlib").Path("/tmp/project"),
            timeout_seconds=300,
            logger=logging.getLogger("test.planning_repair_no_parser"),
            emit_live=lambda level, message, metadata=None: events.append(
                (level, message, metadata or {})
            ),
            reason="json_parse_failed",
            rejection_reasons=["commands must be an array"],
            knowledge_context=None,
            session_id=1,
            task_id=2,
        )

    completed_events = [
        metadata
        for level, message, metadata in events
        if level == "INFO"
        and str(message).startswith("[ORCHESTRATION] Planning repair completed")
    ]
    assert completed_events == []
    no_output_metadata = [
        metadata
        for _, _, metadata in events
        if metadata.get("reason") == "planning_repair_no_output_timeout"
    ][0]
    assert no_output_metadata["parser_validation_seconds"] is None


def test_openclaw_repair_diagnostics_summary_includes_stream_timing_fields():
    summary = OpenClawSessionService._stream_diagnostics_summary(
        {
            "duration_seconds": 12.345,
            "timeout_seconds": 90,
            "timed_out": False,
            "cancelled": False,
            "return_code": 0,
            "first_output_after_seconds": 1.2,
            "last_output_after_seconds": 10.5,
            "max_silent_gap_seconds": 4.2,
            "stdout_chars": 120,
            "stderr_chars": 80,
            "output_token_estimate": 50,
            "stdout_lines": 3,
            "stderr_lines": 2,
            "output_channel_used": "stdout",
            "stderr_contains_model_content": False,
            "stderr_contains_only_logs": False,
            "stream_stalled": False,
            "truncated": False,
        }
    )

    assert "duration=12.35s" in summary
    assert "first_output_after=1.20s" in summary
    assert "last_output_after=10.50s" in summary
    assert "max_silent_gap=4.20s" in summary
    assert "stdout_chars=120" in summary
    assert "output_token_estimate=50" in summary
    assert "output_channel_used=stdout" in summary
    assert "stderr_contains_model_content=False" in summary


def test_openclaw_planning_diagnostics_summary_includes_initial_planning_fields():
    summary = OpenClawSessionService._stream_diagnostics_summary(
        {
            "planning_prompt_size": 4096,
            "duration_seconds": 64.55,
            "timeout_seconds": 300,
            "timed_out": False,
            "cancelled": False,
            "return_code": 0,
            "first_output_after_seconds": 2.5,
            "last_output_after_seconds": 64.0,
            "max_silent_gap_seconds": 18.0,
            "stdout_chars": 9000,
            "stderr_chars": 120,
            "output_token_estimate": 2280,
            "stdout_lines": 30,
            "stderr_lines": 2,
            "output_channel_used": "stderr",
            "stderr_contains_model_content": True,
            "stderr_contains_only_logs": False,
            "stream_stalled": True,
            "truncated": True,
            "contract_violation_type": "truncated_multistep_plan_detected",
        }
    )

    assert "planning_prompt_size=4096" in summary
    assert "duration=64.55s" in summary
    assert "first_output_after=2.50s" in summary
    assert "max_silent_gap=18.00s" in summary
    assert "stdout_chars=9000" in summary
    assert "stream_stalled=True" in summary
    assert "output_channel_used=stderr" in summary
    assert "stderr_contains_model_content=True" in summary
    assert "contract_violation_type=truncated_multistep_plan_detected" in summary
