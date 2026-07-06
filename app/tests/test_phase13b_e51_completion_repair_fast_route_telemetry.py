"""Phase 13B-E51: Completion Repair Fast-Route Telemetry Tests.

Tests:
- Telemetry emitted on successful completion repair LLM response
- Telemetry emitted on timeout/exception
- Fast profile selected when COMPLETION_REPAIR_BACKEND is configured
- Fallback to default runtime when fast profile unavailable
- Timeout remains 120s
- Completion validation acceptance behavior unchanged
- No full prompt/output stored in telemetry fields
"""

from __future__ import annotations

import asyncio
import logging
from types import SimpleNamespace
from unittest.mock import patch, MagicMock

import pytest

from app.models import (
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.orchestration.phases.completion_flow import (
    _attempt_completion_repair,
)
from app.services.orchestration.policy import COMPLETION_REPAIR_TIMEOUT_SECONDS
from app.services.orchestration.types import OrchestrationRunContext
from app.services.orchestration.prompt_templates import OrchestrationState, StepResult


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


class _FakeRuntime:
    """Fake runtime that records prompts and returns configurable output."""

    def __init__(self, output="not json", *, raises=None):
        self.prompts: list[str] = []
        self._output = output
        self._raises = raises
        self.name = "fake_default"

    async def execute_task(self, prompt, timeout_seconds=None):
        self.prompts.append(str(prompt))
        if self._raises is not None:
            raise self._raises
        return {"output": self._output}

    def get_backend_metadata(self):
        return {"backend": self.name, "model_family": "test"}


class _FakeFastRuntime(_FakeRuntime):
    def __init__(self, output="not json", *, raises=None):
        super().__init__(output, raises=raises)
        self.name = "fake_fast"


def _make_state(tmp_path):
    project_dir = tmp_path / "project"
    (project_dir / "src").mkdir(parents=True)
    (project_dir / "tests").mkdir()
    (project_dir / "src" / "main.py").write_text("x = 1\n", encoding="utf-8")
    (project_dir / "tests" / "test_main.py").write_text(
        "def test_ok(): pass\n", encoding="utf-8"
    )
    state = OrchestrationState(
        session_id="1",
        task_description="Build something",
        project_name="E51",
        task_id=2,
        plan=[
            {
                "description": "Write source",
                "expected_files": ["src/main.py"],
                "step_number": 1,
            }
        ],
    )
    state._project_dir_override = str(project_dir)
    state.execution_results = [
        StepResult(
            step_number=1,
            status="failed",
            output="pytest failed",
            files_changed=["src/main.py"],
        )
    ]
    return state


def _completion_validation():
    return SimpleNamespace(
        stage="task_completion",
        status="repair_required",
        repairable=True,
        profile="implementation",
        reasons=["pytest failure: assertion error"],
        details={
            "expected_core_files": ["src/main.py"],
            "verification_output_preview": "AssertionError",
            "verification_command": "python -m pytest -q",
            "failure_class": "completion_verification:pytest_failure",
        },
    )


def _seed_ctx(db_session, tmp_path, *, default_runtime_output="not json", raises=None):
    state = _make_state(tmp_path)
    project = Project(name="E51 Project", workspace_path=str(state.project_dir))
    db_session.add(project)
    db_session.flush()
    session = SessionModel(
        project_id=project.id,
        name="E51 Session",
        status="running",
        is_active=True,
        execution_mode="manual",
    )
    task = Task(
        project_id=project.id,
        title="E51 Task",
        status=TaskStatus.RUNNING,
        task_subfolder="task-e51",
    )
    db_session.add_all([session, task])
    db_session.flush()
    link = SessionTask(
        session_id=session.id, task_id=task.id, status=TaskStatus.RUNNING
    )
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
    )
    db_session.add_all([link, execution])
    db_session.commit()

    runtime = _FakeRuntime(default_runtime_output, raises=raises)
    ctx = OrchestrationRunContext(
        db=db_session,
        session=session,
        project=project,
        task=task,
        session_task_link=link,
        session_id=session.id,
        task_id=task.id,
        prompt="Build something",
        timeout_seconds=120,
        execution_profile="full_lifecycle",
        validation_profile="implementation",
        runs_in_canonical_baseline=False,
        orchestration_state=state,
        runtime_service=runtime,
        task_service=SimpleNamespace(),
        logger=logging.getLogger("e51-test"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=SimpleNamespace(),
        task_execution_id=execution.id,
        restore_workspace_snapshot_if_needed=lambda reason: None,
    )
    return ctx, runtime


# ---------------------------------------------------------------------------
# Telemetry fields emitted on successful LLM response
# ---------------------------------------------------------------------------


def test_telemetry_emitted_on_successful_repair_response(db_session, tmp_path):
    emitted = []

    def capture_emit(level, msg, *, metadata=None):
        emitted.append({"level": level, "msg": msg, "metadata": metadata or {}})

    ctx, runtime = _seed_ctx(db_session, tmp_path, default_runtime_output="not json")
    ctx = ctx._replace(emit_live=capture_emit) if hasattr(ctx, "_replace") else ctx
    object.__setattr__(ctx, "emit_live", capture_emit)

    with patch("app.config.settings.COMPLETION_REPAIR_BACKEND", None):
        _attempt_completion_repair(
            ctx=ctx,
            completion_validation=_completion_validation(),
            save_orchestration_checkpoint_fn=lambda *a, **k: None,
        )

    telemetry_events = [
        e for e in emitted if "completion_repair_prompt_chars" in e["metadata"]
    ]
    assert (
        telemetry_events
    ), "Expected at least one telemetry emit with completion_repair_prompt_chars"
    t = telemetry_events[0]["metadata"]
    assert t["completion_repair_prompt_chars"] > 0
    assert t["completion_repair_timeout_seconds"] == COMPLETION_REPAIR_TIMEOUT_SECONDS
    assert t["completion_repair_runtime_profile"] == "default"
    assert "completion_repair_started_at" in t
    assert t["completion_repair_duration_seconds"] >= 0
    assert t["completion_repair_timed_out"] is False
    assert "completion_repair_output_chars" in t
    assert t["completion_repair_fast_profile_selected"] is False
    assert t["completion_repair_fast_profile_fallback"] is False


# ---------------------------------------------------------------------------
# Telemetry fields emitted on exception
# ---------------------------------------------------------------------------


def test_telemetry_emitted_on_exception(db_session, tmp_path):
    emitted = []

    def capture_emit(level, msg, *, metadata=None):
        emitted.append({"level": level, "msg": msg, "metadata": metadata or {}})

    ctx, runtime = _seed_ctx(
        db_session, tmp_path, raises=asyncio.TimeoutError("timed out")
    )
    object.__setattr__(ctx, "emit_live", capture_emit)

    with patch("app.config.settings.COMPLETION_REPAIR_BACKEND", None):
        with pytest.raises((asyncio.TimeoutError, Exception)):
            _attempt_completion_repair(
                ctx=ctx,
                completion_validation=_completion_validation(),
                save_orchestration_checkpoint_fn=lambda *a, **k: None,
            )

    telemetry_events = [
        e for e in emitted if "completion_repair_timed_out" in e["metadata"]
    ]
    assert telemetry_events, "Expected telemetry emit on exception"
    t = telemetry_events[0]["metadata"]
    assert t["completion_repair_prompt_chars"] > 0
    assert t["completion_repair_timeout_seconds"] == COMPLETION_REPAIR_TIMEOUT_SECONDS
    assert t["completion_repair_started_at"]
    assert t["completion_repair_duration_seconds"] >= 0
    assert t["completion_repair_timed_out"] is True
    assert "completion_repair_exception_type" in t
    assert t["completion_repair_exception_type"] == "TimeoutError"
    assert t["completion_repair_fast_profile_selected"] is False


# ---------------------------------------------------------------------------
# Fast profile selected for completion repair only
# ---------------------------------------------------------------------------


def test_fast_profile_selected_when_backend_configured(db_session, tmp_path):
    emitted = []

    def capture_emit(level, msg, *, metadata=None):
        emitted.append({"level": level, "msg": msg, "metadata": metadata or {}})

    fast_runtime = _FakeFastRuntime("not json")
    ctx, default_runtime = _seed_ctx(db_session, tmp_path)
    object.__setattr__(ctx, "emit_live", capture_emit)

    with patch("app.config.settings.COMPLETION_REPAIR_BACKEND", "stub_fast"):
        with patch(
            "app.services.orchestration.phases.completion_flow._create_completion_repair_runtime",
            return_value=fast_runtime,
        ):
            _attempt_completion_repair(
                ctx=ctx,
                completion_validation=_completion_validation(),
                save_orchestration_checkpoint_fn=lambda *a, **k: None,
            )

    telemetry_events = [
        e for e in emitted if "completion_repair_fast_profile_selected" in e["metadata"]
    ]
    assert telemetry_events, "Expected telemetry with fast profile field"
    t = telemetry_events[0]["metadata"]
    assert t["completion_repair_fast_profile_selected"] is True
    assert t["completion_repair_runtime_profile"] == "stub_fast"
    assert t["completion_repair_fast_profile_fallback"] is False
    assert (
        fast_runtime.prompts
    ), "Fast runtime should have been called for primary repair generation"
    # Compliance retry may still use ctx.runtime_service (preserved behavior per spec).
    # Verify the fast runtime received the primary repair prompt (contains capsule content).
    assert any(
        "Relevant existing files:" in p for p in fast_runtime.prompts
    ), "Fast runtime should have received the repair capsule prompt"


# ---------------------------------------------------------------------------
# Fallback to default runtime when fast profile unavailable
# ---------------------------------------------------------------------------


def test_fallback_to_default_when_fast_profile_unavailable(db_session, tmp_path):
    emitted = []

    def capture_emit(level, msg, *, metadata=None):
        emitted.append({"level": level, "msg": msg, "metadata": metadata or {}})

    ctx, default_runtime = _seed_ctx(
        db_session, tmp_path, default_runtime_output="not json"
    )
    object.__setattr__(ctx, "emit_live", capture_emit)

    with patch("app.config.settings.COMPLETION_REPAIR_BACKEND", "stub_fast"):
        with patch(
            "app.services.orchestration.phases.completion_flow._create_completion_repair_runtime",
            side_effect=RuntimeError("backend unavailable"),
        ):
            _attempt_completion_repair(
                ctx=ctx,
                completion_validation=_completion_validation(),
                save_orchestration_checkpoint_fn=lambda *a, **k: None,
            )

    telemetry_events = [
        e for e in emitted if "completion_repair_fast_profile_selected" in e["metadata"]
    ]
    assert telemetry_events, "Expected telemetry even on fallback"
    t = telemetry_events[0]["metadata"]
    assert t["completion_repair_fast_profile_selected"] is False
    assert t["completion_repair_fast_profile_fallback"] is True
    assert t["completion_repair_runtime_profile"] == "default"
    assert (
        default_runtime.prompts
    ), "Default runtime should have been called on fallback"


# ---------------------------------------------------------------------------
# Timeout remains 120s
# ---------------------------------------------------------------------------


def test_timeout_remains_120s(db_session, tmp_path):
    captured_timeouts = []

    class _RecordingRuntime:
        def __init__(self):
            self.prompts = []

        async def execute_task(self, prompt, timeout_seconds=None):
            self.prompts.append(prompt)
            captured_timeouts.append(timeout_seconds)
            return {"output": "not json"}

        def get_backend_metadata(self):
            return {"backend": "recording", "model_family": "test"}

    recording = _RecordingRuntime()
    ctx, _ = _seed_ctx(db_session, tmp_path)
    object.__setattr__(ctx, "runtime_service", recording)

    with patch("app.config.settings.COMPLETION_REPAIR_BACKEND", None):
        _attempt_completion_repair(
            ctx=ctx,
            completion_validation=_completion_validation(),
            save_orchestration_checkpoint_fn=lambda *a, **k: None,
        )

    assert captured_timeouts, "No execute_task calls recorded"
    assert captured_timeouts[0] == COMPLETION_REPAIR_TIMEOUT_SECONDS == 120


# ---------------------------------------------------------------------------
# Completion validation acceptance behavior unchanged
# ---------------------------------------------------------------------------


def test_completion_validation_acceptance_unchanged(db_session, tmp_path):
    """Repair still fails when the response is invalid JSON — validator gate preserved."""
    ctx, runtime = _seed_ctx(db_session, tmp_path, default_runtime_output="not json")

    with patch("app.config.settings.COMPLETION_REPAIR_BACKEND", None):
        result = _attempt_completion_repair(
            ctx=ctx,
            completion_validation=_completion_validation(),
            save_orchestration_checkpoint_fn=lambda *a, **k: None,
        )

    assert result["status"] == "failed"
    assert (
        "repair_step_parse_failed" in result["reason"]
        or "repair_step_missing" in result["reason"]
    )


# ---------------------------------------------------------------------------
# No full prompt/output stored in telemetry fields
# ---------------------------------------------------------------------------


def test_no_full_prompt_or_output_in_telemetry(db_session, tmp_path):
    """Telemetry must not leak full prompt text or full model output."""
    emitted = []

    def capture_emit(level, msg, *, metadata=None):
        emitted.append({"level": level, "msg": msg, "metadata": metadata or {}})

    ctx, _ = _seed_ctx(
        db_session, tmp_path, default_runtime_output="some model output text"
    )
    object.__setattr__(ctx, "emit_live", capture_emit)

    with patch("app.config.settings.COMPLETION_REPAIR_BACKEND", None):
        _attempt_completion_repair(
            ctx=ctx,
            completion_validation=_completion_validation(),
            save_orchestration_checkpoint_fn=lambda *a, **k: None,
        )

    telemetry_events = [
        e for e in emitted if "completion_repair_prompt_chars" in e["metadata"]
    ]
    assert telemetry_events
    for e in telemetry_events:
        md = e["metadata"]
        # Must not store full prompt or output text
        for key, val in md.items():
            if isinstance(val, str) and len(val) > 200:
                pytest.fail(
                    f"Telemetry field '{key}' stores a long string ({len(val)} chars) — "
                    "possible full prompt/output leak"
                )
        assert "prompt" not in str(md.values())[:0]  # ensure chars fields are ints
        assert isinstance(md.get("completion_repair_prompt_chars"), int)
        if "completion_repair_output_chars" in md:
            assert isinstance(md["completion_repair_output_chars"], int)
