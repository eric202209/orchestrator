import logging
import json
from unittest.mock import MagicMock

import pytest
from app.models import TaskStatus

from app.services.orchestration.phases.planning_flow import (
    _PlanningRetryState,
    _should_repair_truncated_single_step_plan,
    execute_planning_phase,
)

from app.services.orchestration.phases.planning_support import (
    _abort_missing_source_materialization_repair,
)

from app.services.orchestration.planning.planner import (
    PlannerService,
    PlanningRepairNoOutputTimeout,
)

from app.services.orchestration.types import OrchestrationRunContext

from app.services.orchestration.validation.parsing import extract_structured_text

from app.tests.planner_timeout_test_helpers import _patch_planning_flow_external_writes


def test_test_focused_missing_materialization_repair_is_not_aborted(tmp_path):
    ctx = MagicMock()
    ctx.prompt = "Add a targeted test for queue latency null handling"
    ctx.task = MagicMock(
        title="Queue latency null handling test",
        description=(
            "Add a targeted test for GET /ops/queue-latency that verifies NULL "
            "queue latency values are excluded from average and max calculations."
        ),
    )
    ctx.orchestration_state = MagicMock()
    ctx.orchestration_state.plan = [{"step_number": 1}]
    ctx.orchestration_state.project_dir = tmp_path

    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True
    retry_state.source_materialization_required_after_repair = True
    retry_state.last_repair_reason = "missing_source_materialization"

    assert (
        _abort_missing_source_materialization_repair(
            ctx=ctx,
            retry_state=retry_state,
            output_text="[]",
        )
        is None
    )


def test_minimal_first_timeout_is_finalized_without_outer_retry(tmp_path, monkeypatch):
    orchestration_state = MagicMock()
    orchestration_state.project_dir = tmp_path
    orchestration_state.project_context = "dense context"
    orchestration_state.plan = []
    orchestration_state.current_step_index = 0

    class Runtime:
        def get_backend_metadata(self):
            return {}

        async def execute_task(self, *args, **kwargs):
            return {
                "status": "failed",
                "output": "Request timed out before a response was generated.",
                "error": "Task timed out after 5 minutes",
            }

    task = MagicMock()
    task.title = "Timeout planning"
    task.description = "Timeout planning"
    session = MagicMock()
    session.status = "running"
    session.is_active = True
    session_task_link = MagicMock()
    restored = []

    ctx = OrchestrationRunContext(
        db=MagicMock(),
        session=session,
        project=MagicMock(),
        task=task,
        session_task_link=session_task_link,
        session_id=48,
        task_id=5,
        prompt="Timeout planning",
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=orchestration_state,
        runtime_service=Runtime(),
        task_service=MagicMock(),
        logger=logging.getLogger("test.planner_minimal_timeout"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=MagicMock(),
        restore_workspace_snapshot_if_needed=lambda reason: restored.append(reason),
    )

    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow.append_orchestration_event",
        lambda *args, **kwargs: {},
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow.write_orchestration_state_snapshot",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow.emit_phase_event",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow.assemble_planning_prompt",
        lambda *args, **kwargs: "mock planning prompt",
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow._retrieve_knowledge",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow._log_knowledge_usage",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow._finalize_planning_timeout_failure",
        lambda **kwargs: True,
    )
    monkeypatch.setattr(
        PlannerService,
        "should_start_with_minimal_prompt",
        staticmethod(lambda *args, **kwargs: True),
    )

    result = execute_planning_phase(
        ctx=ctx,
        workspace_review={"has_existing_files": False},
        extract_structured_text=extract_structured_text,
        extract_plan_steps=lambda value: value if isinstance(value, list) else None,
        looks_like_truncated_multistep_plan=lambda text, plan: False,
        normalize_plan_with_live_logging=lambda *args, **kwargs: args[3],
        workspace_violation_error_cls=RuntimeError,
    )

    assert result == {"status": "failed", "reason": "planning_timeout"}
    assert orchestration_state.status.value == "aborted"
    assert "Planning timed out" in orchestration_state.abort_reason
    assert restored == ["planning timeout or context overflow"]


def test_repair_timeout_is_not_reported_as_generic_planning_timeout(
    tmp_path, monkeypatch
):
    orchestration_state = MagicMock()
    orchestration_state.project_dir = tmp_path
    orchestration_state.project_context = ""
    orchestration_state.plan = []
    orchestration_state.current_step_index = 0
    orchestration_state.reasoning_artifact = None

    class Runtime:
        def get_backend_metadata(self):
            return {}

        async def execute_task(self, *args, **kwargs):
            return {"status": "completed", "output": "not json"}

    task = MagicMock()
    task.title = "Repair timeout"
    task.description = "Repair timeout"
    session = MagicMock()
    session.status = "running"
    session.is_active = True
    session_task_link = MagicMock()
    restored = []

    ctx = OrchestrationRunContext(
        db=MagicMock(),
        session=session,
        project=MagicMock(),
        task=task,
        session_task_link=session_task_link,
        session_id=50,
        task_id=5,
        prompt="Repair timeout",
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=orchestration_state,
        runtime_service=Runtime(),
        task_service=MagicMock(),
        logger=logging.getLogger("test.planner_repair_timeout_classification"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=MagicMock(),
        restore_workspace_snapshot_if_needed=lambda reason: restored.append(reason),
    )
    ctx.error_handler.attempt_json_parsing = lambda *args, **kwargs: (
        False,
        None,
        "json parse failed",
    )

    _patch_planning_flow_external_writes(monkeypatch)
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow._finalize_planning_timeout_failure",
        lambda **kwargs: True,
    )
    monkeypatch.setattr(
        PlannerService,
        "should_start_with_minimal_prompt",
        staticmethod(lambda *args, **kwargs: False),
    )
    minimal_calls = {"count": 0}

    def _minimal_retry(*args, **kwargs):
        minimal_calls["count"] += 1
        return {"status": "completed", "output": "still not json"}

    monkeypatch.setattr(
        PlannerService,
        "retry_with_minimal_prompt",
        classmethod(lambda cls, *args, **kwargs: _minimal_retry(*args, **kwargs)),
    )
    monkeypatch.setattr(
        PlannerService,
        "repair_output",
        classmethod(
            lambda cls, *args, **kwargs: (_ for _ in ()).throw(
                TimeoutError("Planning repair timed out after 90s")
            )
        ),
    )

    result = execute_planning_phase(
        ctx=ctx,
        workspace_review={"has_existing_files": False},
        extract_structured_text=extract_structured_text,
        extract_plan_steps=lambda value: value if isinstance(value, list) else None,
        looks_like_truncated_multistep_plan=lambda text, plan: False,
        normalize_plan_with_live_logging=lambda *args, **kwargs: args[3],
        workspace_violation_error_cls=RuntimeError,
    )

    assert minimal_calls["count"] == 1
    assert result == {
        "status": "failed",
        "reason": "malformed_planning_output_repair_timeout",
    }
    assert "Planning repair timed out after 90s" in orchestration_state.abort_reason
    assert "300s" not in orchestration_state.abort_reason
    assert restored == ["planning repair timeout"]


def test_repair_no_output_timeout_is_terminal_planning_failure(tmp_path, monkeypatch):
    orchestration_state = MagicMock()
    orchestration_state.project_dir = tmp_path
    orchestration_state.project_context = ""
    orchestration_state.plan = []
    orchestration_state.current_step_index = 0
    orchestration_state.reasoning_artifact = None

    class Runtime:
        def get_backend_metadata(self):
            return {}

        async def execute_task(self, *args, **kwargs):
            return {"status": "completed", "output": "not json"}

    task = MagicMock()
    task.title = "Repair no output"
    task.description = "Repair no output"
    session = MagicMock()
    session.status = "running"
    session.is_active = True
    session_task_link = MagicMock()
    restored = []

    ctx = OrchestrationRunContext(
        db=MagicMock(),
        session=session,
        project=MagicMock(),
        task=task,
        session_task_link=session_task_link,
        session_id=51,
        task_id=5,
        prompt="Repair no output",
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=orchestration_state,
        runtime_service=Runtime(),
        task_service=MagicMock(),
        logger=logging.getLogger("test.planner_repair_no_output_classification"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=MagicMock(),
        restore_workspace_snapshot_if_needed=lambda reason: restored.append(reason),
    )
    ctx.error_handler.attempt_json_parsing = lambda *args, **kwargs: (
        False,
        None,
        "json parse failed",
    )

    _patch_planning_flow_external_writes(monkeypatch)

    def finalize_timeout_failure(**kwargs):
        assert kwargs["failure_type"] == "planning_repair_no_output_timeout"
        kwargs["ctx"].task.status = TaskStatus.FAILED
        kwargs["ctx"].session_task_link.status = TaskStatus.FAILED
        kwargs["ctx"].session.status = "paused"
        kwargs["ctx"].session.is_active = False
        return True

    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow._finalize_planning_timeout_failure",
        finalize_timeout_failure,
    )
    monkeypatch.setattr(
        PlannerService,
        "should_start_with_minimal_prompt",
        staticmethod(lambda *args, **kwargs: False),
    )
    monkeypatch.setattr(
        PlannerService,
        "retry_with_minimal_prompt",
        classmethod(
            lambda cls, *args, **kwargs: {
                "status": "completed",
                "output": "still not json",
            }
        ),
    )
    monkeypatch.setattr(
        PlannerService,
        "repair_output",
        classmethod(
            lambda cls, *args, **kwargs: (_ for _ in ()).throw(
                PlanningRepairNoOutputTimeout(
                    "Planning repair produced no output before 30s",
                    {"no_output_timeout": True},
                )
            )
        ),
    )

    result = execute_planning_phase(
        ctx=ctx,
        workspace_review={"has_existing_files": False},
        extract_structured_text=extract_structured_text,
        extract_plan_steps=lambda value: value if isinstance(value, list) else None,
        looks_like_truncated_multistep_plan=lambda text, plan: False,
        normalize_plan_with_live_logging=lambda *args, **kwargs: args[3],
        workspace_violation_error_cls=RuntimeError,
    )

    assert result == {
        "status": "failed",
        "reason": "planning_repair_no_output_timeout",
    }
    assert task.status == TaskStatus.FAILED
    assert session_task_link.status == TaskStatus.FAILED
    assert session.status == "paused"
    assert session.is_active is False
    assert restored == ["planning repair timeout"]


def test_local_qwen_single_step_plan_is_routed_to_repair():
    assert (
        _should_repair_truncated_single_step_plan(
            prompt_profile="local_qwen_json_array",
            execution_profile="full_lifecycle",
            extracted_plan=[
                {
                    "step_number": 1,
                    "description": "Set up frontend and backend foundations",
                    "commands": ["mkdir -p frontend backend"],
                    "verification": "test -d frontend && test -d backend",
                    "rollback": "rm -rf frontend backend",
                    "expected_files": ["frontend/src/main.tsx", "backend/src/index.ts"],
                }
            ],
        )
        is True
    )


def test_non_qwen_or_non_full_lifecycle_single_step_plan_still_uses_retry_guard():
    single_step_plan = [
        {
            "step_number": 1,
            "description": "Do work",
            "commands": ["echo hi"],
            "verification": "test -n hi",
            "rollback": "true",
            "expected_files": [],
        }
    ]

    assert (
        _should_repair_truncated_single_step_plan(
            prompt_profile="default",
            execution_profile="full_lifecycle",
            extracted_plan=single_step_plan,
        )
        is False
    )
    assert (
        _should_repair_truncated_single_step_plan(
            prompt_profile="local_qwen_json_array",
            execution_profile="review_only",
            extracted_plan=single_step_plan,
        )
        is False
    )


def test_aborted_timeout_metadata_is_not_treated_as_salvageable_plan_output():
    output_text = (
        '{"total":0,"aborted":true,"source":"run","generatedAt":1777555426260}'
    )

    assert PlannerService.looks_salvageable_planning_output(output_text) is False


def test_minimal_prompt_retry_uses_fresh_session_instead_of_task_session():
    captured = {}

    class RuntimeService:
        async def execute_task(self, prompt, timeout_seconds=300, **kwargs):
            captured["reuse_task_session"] = kwargs.get("reuse_task_session")
            return {"status": "failed", "output": "", "error": "Task timed out"}

    PlannerService.retry_with_minimal_prompt(
        runtime_service=RuntimeService(),
        task_description="Build a one-page site",
        project_dir=__import__("pathlib").Path("/tmp/project"),
        timeout_seconds=60,
        logger=__import__("logging").getLogger("test"),
        emit_live=lambda *args, **kwargs: None,
        reason="timeout",
    )

    assert captured["reuse_task_session"] is False


# Phase 6O: post-repair brittle command subcodes
