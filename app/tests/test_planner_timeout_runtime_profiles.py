import logging
import json
from unittest.mock import AsyncMock, MagicMock

import pytest
from app.models import TaskStatus

from app.services.orchestration.phases.planning_flow import execute_planning_phase

from app.services.orchestration.planning.planner import PlannerService

from app.services.orchestration.types import OrchestrationRunContext

from app.services.orchestration.validation.validator import ValidatorService

from app.services.orchestration.validation.parsing import extract_structured_text

from app.services.orchestration.validation.workspace_guard import (
    TaskOperationContractViolation,
)

from app.services.orchestration.policy import (
    MINIMAL_PLANNING_TIMEOUT_SECONDS,
    ORCHESTRATION_TASK_SOFT_TIME_LIMIT_SECONDS,
    ORCHESTRATION_TASK_TIME_LIMIT_SECONDS,
    PLANNING_REPAIR_TIMEOUT_SECONDS,
    PLANNING_REPAIR_NO_OUTPUT_TIMEOUT_SECONDS,
    STRICT_JSON_RETRY_TIMEOUT_SECONDS,
    ULTRA_MINIMAL_PLANNING_TIMEOUT_SECONDS,
    clamp_planning_timeout,
)

from app.tasks.worker import execute_orchestration_task

from app.tests.planner_timeout_test_helpers import (
    _valid_three_step_plan,
    _patch_planning_flow_external_writes,
)


def test_operation_contract_violation_terminal_reason_is_not_workspace_isolation(
    tmp_path, monkeypatch
):
    _patch_planning_flow_external_writes(monkeypatch)
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow._build_reasoning_artifact",
        lambda *args, **kwargs: {
            "intent": "Patch a file with ops",
            "workspace_facts": ["README.md exists"],
            "planned_actions": ["Use replace_in_file"],
            "verification_plan": ["Check README.md"],
        },
    )
    monkeypatch.setattr(
        ValidatorService,
        "validate_reasoning_artifact",
        classmethod(
            lambda cls, *args, **kwargs: type(
                "Verdict",
                (),
                {"accepted": True, "status": "accepted", "reasons": []},
            )()
        ),
    )
    monkeypatch.setattr(
        PlannerService,
        "should_start_with_minimal_prompt",
        staticmethod(lambda *args, **kwargs: False),
    )
    monkeypatch.setattr(
        PlannerService,
        "find_immediate_repair_step_issues",
        staticmethod(lambda *args, **kwargs: {}),
    )

    task = MagicMock()
    task.title = "Patch README"
    task.description = "Use replace_in_file ops"
    session = MagicMock(instance_id=None)
    ctx = OrchestrationRunContext(
        db=MagicMock(),
        session=session,
        project=MagicMock(),
        task=task,
        session_task_link=MagicMock(),
        session_id=45,
        task_id=6,
        prompt="Patch README",
        timeout_seconds=300,
        execution_profile="implementation",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=MagicMock(project_dir=tmp_path, plan=None),
        runtime_service=MagicMock(),
        task_service=MagicMock(),
        logger=logging.getLogger("test.op_contract_violation"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=MagicMock(),
        task_execution_id=None,
    )
    ctx.runtime_service.execute_task = AsyncMock(
        return_value={"output": json.dumps(_valid_three_step_plan())}
    )
    ctx.error_handler.attempt_json_parsing = lambda *args, **kwargs: (
        True,
        _valid_three_step_plan(),
        "ok",
    )

    def _raise_operation_contract(*args, **kwargs):
        raise TaskOperationContractViolation("step 1 op 1 must contain keys")

    result = execute_planning_phase(
        ctx=ctx,
        workspace_review={"has_existing_files": False},
        extract_structured_text=extract_structured_text,
        extract_plan_steps=lambda value: value if isinstance(value, list) else None,
        looks_like_truncated_multistep_plan=lambda text, plan: False,
        normalize_plan_with_live_logging=_raise_operation_contract,
        workspace_violation_error_cls=RuntimeError,
    )

    assert result == {"status": "failed", "reason": "op_contract_violation"}
    assert task.status == TaskStatus.FAILED
    assert "step 1 op 1" in task.error_message


def test_minimal_first_unexpected_plan_shape_routes_to_repair_not_second_minimal(
    tmp_path, monkeypatch
):
    plan = _valid_three_step_plan()
    _patch_planning_flow_external_writes(monkeypatch)
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow._build_reasoning_artifact",
        lambda *args, **kwargs: {
            "intent": "Create smoke script",
            "workspace_facts": ["README.md already exists"],
            "planned_actions": ["Create scripts/smoke_status.py"],
            "verification_plan": ["Run the smoke script"],
        },
    )
    monkeypatch.setattr(
        ValidatorService,
        "validate_reasoning_artifact",
        classmethod(
            lambda cls, *args, **kwargs: type(
                "Verdict",
                (),
                {"accepted": True, "status": "accepted", "reasons": []},
            )()
        ),
    )
    monkeypatch.setattr(
        ValidatorService,
        "validate_plan",
        staticmethod(
            lambda *args, **kwargs: type(
                "Verdict",
                (),
                {
                    "accepted": True,
                    "warning": False,
                    "status": "accepted",
                    "reasons": [],
                    "details": {},
                    "verdict": {"status": "accepted"},
                },
            )()
        ),
    )
    monkeypatch.setattr(
        PlannerService,
        "should_start_with_minimal_prompt",
        staticmethod(lambda *args, **kwargs: True),
    )
    monkeypatch.setattr(
        PlannerService,
        "find_immediate_repair_step_issues",
        staticmethod(lambda *args, **kwargs: {}),
    )

    minimal_calls = {"count": 0}
    repair_calls = {"count": 0, "reason": None}

    def _minimal_retry(*args, **kwargs):
        minimal_calls["count"] += 1
        return {"status": "completed", "output": json.dumps({"steps": plan})}

    def _repair_output(*args, **kwargs):
        repair_calls["count"] += 1
        repair_calls["reason"] = kwargs.get("reason")
        return {"status": "completed", "output": json.dumps(plan)}

    monkeypatch.setattr(
        PlannerService,
        "retry_with_minimal_prompt",
        classmethod(lambda cls, *args, **kwargs: _minimal_retry(*args, **kwargs)),
    )
    monkeypatch.setattr(
        PlannerService,
        "repair_output",
        classmethod(lambda cls, *args, **kwargs: _repair_output(*args, **kwargs)),
    )

    orchestration_state = MagicMock()
    orchestration_state.project_dir = tmp_path
    orchestration_state.project_context = ""
    orchestration_state.plan = []
    orchestration_state.current_step_index = 0
    orchestration_state.reasoning_artifact = None

    task = MagicMock()
    task.title = "Create Smoke Status Script"
    task.description = "Create scripts/smoke_status.py"

    ctx = OrchestrationRunContext(
        db=MagicMock(),
        session=MagicMock(instance_id=None),
        project=MagicMock(),
        task=task,
        session_task_link=MagicMock(),
        session_id=9,
        task_id=8,
        prompt="Create scripts/smoke_status.py",
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=orchestration_state,
        runtime_service=MagicMock(),
        task_service=MagicMock(),
        logger=logging.getLogger("test.minimal_first_unexpected_shape"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=MagicMock(),
        task_execution_id=15,
    )
    ctx.runtime_service.get_backend_metadata.return_value = {}
    ctx.error_handler.attempt_json_parsing = lambda output, **kwargs: (
        True,
        json.loads(output),
        "ok",
    )

    result = execute_planning_phase(
        ctx=ctx,
        workspace_review={"has_existing_files": True},
        extract_structured_text=extract_structured_text,
        extract_plan_steps=lambda value: value if isinstance(value, list) else None,
        looks_like_truncated_multistep_plan=lambda text, plan: False,
        normalize_plan_with_live_logging=lambda *args, **kwargs: args[3],
        workspace_violation_error_cls=RuntimeError,
    )

    assert result == {"status": "completed"}
    assert minimal_calls["count"] == 1
    assert repair_calls == {
        "count": 1,
        "reason": "unexpected_plan_shape_after_minimal",
    }
    assert orchestration_state.plan == plan


def test_build_task_with_clean_architecture_does_not_start_minimal_first():
    assert (
        PlannerService.should_start_with_minimal_prompt(
            "Set up frontend (React or Vite) and backend (Node.js or FastAPI) with clean architecture.",
            "",
        )
        is False
    )


def test_true_inspection_task_still_starts_minimal_first():
    assert (
        PlannerService.should_start_with_minimal_prompt(
            "Inspect current project structure and review architecture before changes.",
            "",
        )
        is True
    )


def test_planning_fallback_timeouts_are_relaxed_for_local_models():
    assert MINIMAL_PLANNING_TIMEOUT_SECONDS == 300
    assert STRICT_JSON_RETRY_TIMEOUT_SECONDS == 120
    assert PLANNING_REPAIR_TIMEOUT_SECONDS == 240
    assert PLANNING_REPAIR_NO_OUTPUT_TIMEOUT_SECONDS == 200
    assert ULTRA_MINIMAL_PLANNING_TIMEOUT_SECONDS == 240


def test_low_resource_runtime_profile_caps_planning_timeout(monkeypatch):
    from app.services.orchestration import policy as policy_module

    monkeypatch.setattr(policy_module.settings, "RUNTIME_PROFILE", "low_resource")
    monkeypatch.setattr(
        policy_module.settings,
        "PLANNING_SYNTHESIS_TIMEOUT_SECONDS",
        90,
    )

    assert clamp_planning_timeout(300) == 90
    assert clamp_planning_timeout(1800) == 90
    assert clamp_planning_timeout(10) == 90


def test_compact_local_runtime_profile_caps_planning_timeout(monkeypatch):
    from app.services.orchestration import policy as policy_module

    monkeypatch.setattr(policy_module.settings, "RUNTIME_PROFILE", "compact_local")
    monkeypatch.setattr(
        policy_module.settings,
        "PLANNING_SYNTHESIS_TIMEOUT_SECONDS",
        90,
    )

    assert clamp_planning_timeout(300) == 90
    assert clamp_planning_timeout(1800) == 90
    assert clamp_planning_timeout(10) == 90


def test_medium_runtime_profile_caps_planning_timeout(monkeypatch):
    from app.services.orchestration import policy as policy_module

    monkeypatch.setattr(policy_module.settings, "RUNTIME_PROFILE", "medium")
    monkeypatch.setattr(
        policy_module.settings,
        "PLANNING_SYNTHESIS_TIMEOUT_SECONDS",
        120,
    )

    assert clamp_planning_timeout(300) == 120
    assert clamp_planning_timeout(1800) == 120


def test_standard_runtime_profile_keeps_existing_planning_timeout_bounds(monkeypatch):
    from app.services.orchestration import policy as policy_module

    monkeypatch.setattr(policy_module.settings, "RUNTIME_PROFILE", "standard")
    monkeypatch.setattr(
        policy_module.settings,
        "PLANNING_SYNTHESIS_TIMEOUT_SECONDS",
        90,
    )

    assert clamp_planning_timeout(10) == 180
    assert clamp_planning_timeout(240) == 240
    assert clamp_planning_timeout(1800) == 300


def test_worker_soft_time_limit_allows_planning_retries_and_execution_headroom():
    assert ORCHESTRATION_TASK_SOFT_TIME_LIMIT_SECONDS == 3300
    assert ORCHESTRATION_TASK_TIME_LIMIT_SECONDS == 3600
    assert execute_orchestration_task.soft_time_limit == 3300
    assert execute_orchestration_task.time_limit == 3600


def test_worker_task_requeues_orchestration_on_worker_loss():
    assert execute_orchestration_task.acks_late is True
    assert execute_orchestration_task.reject_on_worker_lost is True
    assert execute_orchestration_task.acks_on_failure_or_timeout is True


def test_qwen_local_prompt_profile_enforces_array_only_output():
    profile = PlannerService.select_prompt_profile("local_openclaw", "qwen-local")
    prompt = PlannerService.build_minimal_planning_prompt(
        "Build a hiring platform",
        project_dir=__import__("pathlib").Path("/tmp/project"),
        prompt_profile=profile,
    )

    assert profile == "local_qwen_json_array"
    assert "first non-whitespace character must be `[`" in prompt
    assert "Do not wrap it in an object" in prompt


def test_amd_14b_lane_uses_smaller_stricter_plan_shape_label():
    profile = PlannerService.select_prompt_profile(
        "local_openclaw", "Qwen2.5-Coder-14B-Instruct-Q5_K_M"
    )
    prompt = PlannerService.apply_prompt_profile(
        "Return a plan.", prompt_profile=profile
    )

    assert (
        PlannerService.model_capability_label(
            "local_openclaw", "Qwen2.5-Coder-14B-Instruct-Q5_K_M"
        )
        == "local_qwen_small_strict"
    )
    assert profile == "local_qwen_small_json_array"
    assert "smallest valid plan shape" in prompt
    assert "Prefer typed `ops` for file writes" in prompt


def test_larger_qwen_lane_keeps_general_qwen_profile():
    profile = PlannerService.select_prompt_profile("local_openclaw", "qwen3.6:27b")

    assert (
        PlannerService.model_capability_label("local_openclaw", "qwen3.6:27b")
        == "local_qwen_capable"
    )
    assert profile == "local_qwen_json_array"
