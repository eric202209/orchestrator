import asyncio
import logging
import json
import time
from unittest.mock import MagicMock

import pytest
from app.models import TaskStatus

from app.services.orchestration.phases.planning_flow import execute_planning_phase

from app.services.orchestration.planning.planner import PlannerService

from app.services.orchestration.types import OrchestrationRunContext

from app.services.orchestration.validation.validator import ValidatorService

from app.services.orchestration.validation.parsing import extract_structured_text

from app.services.orchestration.policy import PLANNING_REPAIR_TIMEOUT_SECONDS

from app.tests.planner_timeout_test_helpers import (
    _valid_three_step_plan,
    _patch_planning_flow_external_writes,
)


def test_planning_uses_workspace_plan_json_before_strict_retry(tmp_path, monkeypatch):
    plan = [
        {
            "step_number": 1,
            "description": "Inspect current FastAPI routes",
            "commands": ['rg -n "APIRouter|include_router" app/api app/main.py'],
            "verification": "python3 -c \"print('inspect ok')\"",
            "rollback": None,
            "expected_files": [],
        },
        {
            "step_number": 2,
            "description": "Adjust planner recovery path",
            "commands": [
                "printf 'patched\\n' > app/services/orchestration/planning/recovery.txt"
            ],
            "verification": "python3 -c \"print('edit ok')\"",
            "rollback": "rm -f app/services/orchestration/planning/recovery.txt",
            "expected_files": [
                "app/services/orchestration/planning/recovery.txt",
            ],
        },
        {
            "step_number": 3,
            "description": "Verify planner module still imports",
            "commands": [
                "python3 -m py_compile app/services/orchestration/planning/planner.py"
            ],
            "verification": "python3 -m py_compile app/services/orchestration/planning/planner.py",
            "rollback": None,
            "expected_files": [],
        },
    ]
    (tmp_path / "plan.json").write_text(json.dumps(plan), encoding="utf-8")

    orchestration_state = MagicMock()
    orchestration_state.project_dir = tmp_path
    orchestration_state.project_context = ""
    orchestration_state.plan = []
    orchestration_state.current_step_index = 0
    orchestration_state.reasoning_artifact = None

    runtime_service = MagicMock()
    runtime_service.get_backend_metadata.return_value = {}

    async def execute_task(*args, **kwargs):
        return {
            "status": "completed",
            "returncode": 0,
            "output": "",
            "stderr": "Recovered structured response from stderr",
            "finalAssistantVisibleText": "Validated the JSON. Plan written to `plan.json` - 7 steps",
        }

    runtime_service.execute_task = execute_task

    task = MagicMock()
    task.title = "Recover planner output"
    task.description = "Use plan.json when stdout is empty"
    task.status = None
    task.error_message = None
    task.steps = None
    task.current_step = None

    ctx = OrchestrationRunContext(
        db=MagicMock(),
        session=MagicMock(instance_id=None),
        project=MagicMock(),
        task=task,
        session_task_link=MagicMock(),
        session_id=45,
        task_id=6,
        prompt="Fix planner recovery",
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=orchestration_state,
        runtime_service=runtime_service,
        task_service=MagicMock(),
        logger=logging.getLogger("test.planner_workspace_plan"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=MagicMock(),
    )
    ctx.error_handler.attempt_json_parsing = lambda *args, **kwargs: (
        False,
        None,
        "json parse failed",
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
        "app.services.orchestration.phases.planning_flow.record_validation_verdict",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow.maybe_emit_divergence_detected",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow._build_reasoning_artifact",
        lambda *args, **kwargs: {
            "intent": "Recover planner output from workspace file",
            "workspace_facts": ["plan.json exists in the task workspace"],
            "planned_actions": ["Use workspace plan.json instead of retrying"],
            "verification_plan": ["Validate recovered plan with the planner validator"],
        },
    )
    monkeypatch.setattr(
        ValidatorService,
        "validate_reasoning_artifact",
        classmethod(
            lambda cls, *args, **kwargs: type(
                "Verdict",
                (),
                {
                    "accepted": True,
                    "status": "accepted",
                    "reasons": [],
                },
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
    monkeypatch.setattr(
        PlannerService,
        "retry_with_minimal_prompt",
        classmethod(
            lambda cls, *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("strict JSON retry should not be called")
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

    assert result == {"status": "completed"}
    assert ctx.orchestration_state.plan == plan
    assert json.loads(task.steps) == plan


def test_planning_extracts_valid_json_from_recovered_stderr_without_repair(
    tmp_path, monkeypatch
):
    plan = _valid_three_step_plan()
    orchestration_state = MagicMock()
    orchestration_state.project_dir = tmp_path
    orchestration_state.project_context = ""
    orchestration_state.plan = []
    orchestration_state.current_step_index = 0
    orchestration_state.reasoning_artifact = None

    runtime_service = MagicMock()
    runtime_service.get_backend_metadata.return_value = {}

    async def execute_task(*args, **kwargs):
        return {
            "status": "completed",
            "returncode": 0,
            "output": "",
            "stdout": "",
            "stderr": json.dumps(
                {
                    "recovered": True,
                    "payloads": [
                        {
                            "finalAssistantVisibleText": (
                                "Recovered plan:\n" + json.dumps(plan)
                            )
                        }
                    ],
                }
            ),
        }

    runtime_service.execute_task = execute_task

    task = MagicMock()
    task.title = "Recover stderr plan"
    task.description = "Recover stderr plan"
    task.status = None
    task.error_message = None
    task.steps = None
    task.current_step = None

    ctx = OrchestrationRunContext(
        db=MagicMock(),
        session=MagicMock(instance_id=None),
        project=MagicMock(),
        task=task,
        session_task_link=MagicMock(),
        session_id=49,
        task_id=6,
        prompt="Fix planner recovery",
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=orchestration_state,
        runtime_service=runtime_service,
        task_service=MagicMock(),
        logger=logging.getLogger("test.planner_stderr_recovery"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=MagicMock(),
    )
    ctx.error_handler.attempt_json_parsing = lambda output, **kwargs: (
        True,
        json.loads(output[output.index("[") :]),
        "json recovered from finalAssistantVisibleText",
    )

    _patch_planning_flow_external_writes(monkeypatch)
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow._build_reasoning_artifact",
        lambda *args, **kwargs: {
            "intent": "Recover planner output from stderr",
            "workspace_facts": ["stderr contained finalAssistantVisibleText"],
            "planned_actions": ["Use recovered JSON array"],
            "verification_plan": ["Validate recovered plan"],
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
    monkeypatch.setattr(
        PlannerService,
        "repair_output",
        classmethod(
            lambda cls, *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("repair should not run for recovered valid JSON")
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

    assert result == {"status": "completed"}
    assert ctx.orchestration_state.plan == plan
    assert json.loads(task.steps) == plan


def test_multi_step_prose_planning_output_uses_fallback_not_execution(
    tmp_path, monkeypatch
):
    plan = _valid_three_step_plan()
    orchestration_state = MagicMock()
    orchestration_state.project_dir = tmp_path
    orchestration_state.project_context = ""
    orchestration_state.plan = []
    orchestration_state.current_step_index = 0
    orchestration_state.reasoning_artifact = None
    orchestration_state.validation_history = []
    orchestration_state.phase_history = []

    class Runtime:
        def get_backend_metadata(self):
            return {}

        async def execute_task(self, *args, **kwargs):
            assert kwargs["diagnostic_label"] == "PLANNING"
            assert kwargs["diagnostic_metadata"]["session_id"] == 52
            assert kwargs["diagnostic_metadata"]["task_id"] == 6
            return {
                "status": "completed",
                "output": (
                    "5-step plan:\n"
                    "| # | Step | Files |\n"
                    "| 1 | Write `src/App.tsx` | `src/App.tsx` |\n"
                ),
            }

    task = MagicMock()
    task.title = "Reject prose plan"
    task.description = "Reject prose plan"
    task.status = None
    task.error_message = None
    task.steps = None
    task.current_step = None
    events = []

    ctx = OrchestrationRunContext(
        db=MagicMock(),
        session=MagicMock(instance_id=None),
        project=MagicMock(),
        task=task,
        session_task_link=MagicMock(),
        session_id=52,
        task_id=6,
        prompt="Build page",
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=orchestration_state,
        runtime_service=Runtime(),
        task_service=MagicMock(),
        logger=logging.getLogger("test.planner_prose_contract"),
        emit_live=lambda level, message, metadata=None: events.append(
            (level, message, metadata or {})
        ),
        error_handler=MagicMock(),
    )

    def parse_output(output, **kwargs):
        if str(output).lstrip().startswith("["):
            return True, json.loads(output), "json"
        return False, None, "json parse failed"

    ctx.error_handler.attempt_json_parsing = parse_output
    minimal_calls = {"count": 0}

    def retry_with_minimal(*args, **kwargs):
        minimal_calls["count"] += 1
        return {"status": "completed", "output": json.dumps(plan)}

    _patch_planning_flow_external_writes(monkeypatch)
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow._build_reasoning_artifact",
        lambda *args, **kwargs: {
            "intent": "Build page with valid JSON plan",
            "workspace_facts": ["workspace exists"],
            "planned_actions": ["Use valid JSON"],
            "verification_plan": ["Validate plan"],
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
        "retry_with_minimal_prompt",
        classmethod(lambda cls, *args, **kwargs: retry_with_minimal(*args, **kwargs)),
    )
    monkeypatch.setattr(
        PlannerService,
        "repair_output",
        classmethod(
            lambda cls, *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("prose plan should use existing fallback before repair")
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

    assert result == {"status": "completed"}
    assert minimal_calls["count"] == 1
    assert ctx.orchestration_state.plan == plan
    planning_diagnostics = [
        metadata
        for level, message, metadata in events
        if level == "WARN"
        and message == "[OPENCLAW][PLANNING_DIAGNOSTICS] contract violation detected"
    ]
    assert planning_diagnostics
    assert planning_diagnostics[0]["session_id"] == 52
    assert planning_diagnostics[0]["task_id"] == 6
    assert planning_diagnostics[0]["contract_violation_type"] in {
        "multi_step_prose_summary",
        "json_parse_failed_before_minimal",
        "non_json_prose",
    }
    assert planning_diagnostics[0]["output_chars"] > 0


def test_malformed_shell_quoting_workspace_guard_failure_routes_to_repair(
    tmp_path, monkeypatch
):
    bad_plan = [
        {
            "step_number": 1,
            "description": "Write malformed App component",
            "commands": [
                "mkdir -p src",
                "printf 'export default function App() { return <h2>This Week\\'s Featured Games</h2>; }\\n' > src/App.jsx",
            ],
            "verification": "node -e \"require('fs').readFileSync('src/App.jsx','utf8')\"",
            "rollback": "rm -f src/App.jsx",
            "expected_files": ["src/App.jsx"],
        }
    ]
    repaired_plan = _valid_three_step_plan()
    orchestration_state = MagicMock()
    orchestration_state.project_dir = tmp_path
    orchestration_state.project_context = ""
    orchestration_state.plan = []
    orchestration_state.current_step_index = 0
    orchestration_state.reasoning_artifact = None
    orchestration_state.validation_history = []
    orchestration_state.phase_history = []

    class Runtime:
        def get_backend_metadata(self):
            return {}

        async def execute_task(self, *args, **kwargs):
            return {"status": "completed", "output": json.dumps(bad_plan)}

    task = MagicMock()
    task.title = "Repair malformed shell quoting"
    task.description = "Repair malformed shell quoting"
    task.status = None
    task.steps = None
    task.current_step = None
    events = []
    normalize_calls = {"count": 0}
    repair_reasons = []

    ctx = OrchestrationRunContext(
        db=MagicMock(),
        session=MagicMock(instance_id=None),
        project=MagicMock(),
        task=task,
        session_task_link=MagicMock(),
        session_id=53,
        task_id=6,
        prompt="Build page",
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=orchestration_state,
        runtime_service=Runtime(),
        task_service=MagicMock(),
        logger=logging.getLogger("test.malformed_shell_quoting_repair"),
        emit_live=lambda level, message, metadata=None: events.append(
            (level, message, metadata or {})
        ),
        error_handler=MagicMock(),
    )
    ctx.error_handler.attempt_json_parsing = lambda output, **kwargs: (
        True,
        json.loads(output),
        "json",
    )

    _patch_planning_flow_external_writes(monkeypatch)
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow._build_reasoning_artifact",
        lambda *args, **kwargs: {
            "intent": "Repair malformed shell quoting",
            "workspace_facts": ["workspace exists"],
            "planned_actions": ["Use repaired JSON"],
            "verification_plan": ["Validate repaired plan"],
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
        "repair_output",
        classmethod(
            lambda cls, *args, **kwargs: repair_reasons.append(kwargs["reason"])
            or {"output": json.dumps(repaired_plan)}
        ),
    )

    def normalize_once_then_pass(*args, **kwargs):
        normalize_calls["count"] += 1
        if normalize_calls["count"] == 1:
            raise RuntimeError(
                "step 1 command 2 blocked: Command contains malformed shell quoting"
            )
        return args[3]

    result = execute_planning_phase(
        ctx=ctx,
        workspace_review={"has_existing_files": False},
        extract_structured_text=extract_structured_text,
        extract_plan_steps=lambda value: value if isinstance(value, list) else None,
        looks_like_truncated_multistep_plan=lambda text, plan: False,
        normalize_plan_with_live_logging=normalize_once_then_pass,
        workspace_violation_error_cls=RuntimeError,
    )

    assert result == {"status": "completed"}
    assert repair_reasons and repair_reasons[0].startswith("malformed_shell_quoting")
    assert ctx.orchestration_state.plan == repaired_plan
    diagnostics = [
        metadata
        for level, message, metadata in events
        if message == "[OPENCLAW][PLANNING_DIAGNOSTICS] contract violation detected"
    ]
    assert diagnostics
    assert diagnostics[0]["semantic_violation_codes"] == ["malformed_shell_quoting"]


def test_post_repair_malformed_shell_quoting_gets_one_targeted_second_repair(
    tmp_path, monkeypatch
):
    initial_plan = [
        {
            "step_number": 1,
            "description": "Create FastAPI route",
            "commands": ["printf 'from fastapi import FastAPI\\n' > main.py"],
            "verification": "echo ok",
            "rollback": "rm -f main.py",
            "expected_files": ["main.py"],
        }
    ]
    first_repair_plan = [
        {
            "step_number": 1,
            "description": "Create FastAPI route",
            "commands": ["printf 'from fastapi import FastAPI\\n' > main.py"],
            "verification": (
                'PYTHONPATH=src .venv/bin/python -c "from main import app; '
                "assert app.title == 'FastAPI'"
            ),
            "rollback": "rm -f main.py",
            "expected_files": ["main.py"],
        }
    ]
    second_repair_plan = [
        {
            "step_number": 1,
            "description": "Create FastAPI route",
            "commands": ["printf 'from fastapi import FastAPI\\n' > main.py"],
            "verification": "python -m pytest -q",
            "rollback": "rm -f main.py",
            "expected_files": ["main.py"],
        }
    ]

    orchestration_state = MagicMock()
    orchestration_state.project_dir = tmp_path
    orchestration_state.project_context = ""
    orchestration_state.plan = []
    orchestration_state.current_step_index = 0
    orchestration_state.reasoning_artifact = None
    orchestration_state.validation_history = []
    orchestration_state.phase_history = []

    class Runtime:
        def get_backend_metadata(self):
            return {}

        async def execute_task(self, *args, **kwargs):
            return {"status": "completed", "output": json.dumps(initial_plan)}

    task = MagicMock()
    task.title = "Repair weak verification then malformed quoting"
    task.description = "Repair weak verification then malformed quoting"
    events = []

    ctx = OrchestrationRunContext(
        db=MagicMock(),
        session=MagicMock(instance_id=None),
        project=MagicMock(),
        task=task,
        session_task_link=MagicMock(),
        session_id=141,
        task_id=28,
        prompt="Build a FastAPI canary",
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=orchestration_state,
        runtime_service=Runtime(),
        task_service=MagicMock(),
        logger=logging.getLogger("test.post_repair_malformed_shell_second_pass"),
        emit_live=lambda level, message, metadata=None: events.append(
            (level, message, metadata or {})
        ),
        error_handler=MagicMock(),
    )
    ctx.error_handler.attempt_json_parsing = lambda output, **kwargs: (
        True,
        json.loads(output),
        "json",
    )

    _patch_planning_flow_external_writes(monkeypatch)
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow._build_reasoning_artifact",
        lambda *args, **kwargs: {
            "intent": "Create FastAPI canary",
            "workspace_facts": [],
            "planned_actions": [],
            "verification_plan": ["Run pytest"],
        },
    )
    monkeypatch.setattr(
        ValidatorService,
        "validate_reasoning_artifact",
        staticmethod(
            lambda *args, **kwargs: type(
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
        staticmethod(lambda *args, **kwargs: False),
    )
    repair_calls = []

    def repair_output(cls, *args, **kwargs):
        repair_calls.append(kwargs)
        if len(repair_calls) == 1:
            return {"output": json.dumps(first_repair_plan)}
        return {"output": json.dumps(second_repair_plan)}

    monkeypatch.setattr(PlannerService, "repair_output", classmethod(repair_output))

    def normalize_plan(*args, **kwargs):
        plan = args[3]
        if any(
            "PYTHONPATH=src .venv/bin/python -c" in str(step.get("verification") or "")
            for step in plan
        ):
            raise RuntimeError(
                "step 1 verification blocked: Command contains malformed shell quoting"
            )
        return plan

    result = execute_planning_phase(
        ctx=ctx,
        workspace_review={"has_existing_files": False},
        extract_structured_text=extract_structured_text,
        extract_plan_steps=lambda value: value if isinstance(value, list) else None,
        looks_like_truncated_multistep_plan=lambda text, plan: False,
        normalize_plan_with_live_logging=normalize_plan,
        workspace_violation_error_cls=RuntimeError,
    )

    assert result == {"status": "completed"}
    assert len(repair_calls) == 2
    assert repair_calls[0]["reason"].startswith("plan_contains_immediate_repair_issues")
    assert repair_calls[1]["reason"].startswith("post_repair_malformed_shell_quoting")
    assert repair_calls[1]["rejection_reasons"] == [
        "Malformed shell quoting: emit one valid shell command string; avoid "
        "unmatched quotes, mixed quote escaping, and python -c snippets with nested "
        "quotes"
    ]
    assert ctx.orchestration_state.plan == second_repair_plan
    diagnostics = [
        metadata
        for level, message, metadata in events
        if message == "[OPENCLAW][PLANNING_DIAGNOSTICS] contract violation detected"
    ]
    assert diagnostics[-1]["semantic_violation_codes"] == ["malformed_shell_quoting"]


def test_planning_repair_timeout_budget_is_enforced(monkeypatch):
    from app.services.orchestration import planning as planning_pkg

    original_timeout = planning_pkg.planner.PLANNING_REPAIR_TIMEOUT_SECONDS
    planning_pkg.planner.PLANNING_REPAIR_TIMEOUT_SECONDS = 0.01

    class Runtime:
        async def invoke_prompt(self, prompt, **kwargs):
            await asyncio.sleep(1)
            return {"output": '[{"step_number":1}]'}

    started_at = time.monotonic()
    try:
        with pytest.raises(TimeoutError) as exc_info:
            PlannerService.repair_output(
                runtime_service=Runtime(),
                task_description="Build a page",
                malformed_output='{"steps":"bad"}',
                project_dir=__import__("pathlib").Path("/tmp/project"),
                timeout_seconds=300,
                logger=logging.getLogger("test.planning_repair_timeout"),
                emit_live=lambda *a, **kw: None,
                reason="json_parse_failed",
                rejection_reasons=["commands must be an array"],
                knowledge_context=None,
                session_id=1,
                task_id=2,
            )
    finally:
        planning_pkg.planner.PLANNING_REPAIR_TIMEOUT_SECONDS = original_timeout

    assert time.monotonic() - started_at < 0.5
    assert "Planning repair timed out after 0.01s" in str(exc_info.value)


def test_planning_repair_timeout_logs_prompt_size_and_reason(monkeypatch, caplog):
    from app.services.orchestration import planning as planning_pkg

    original_timeout = planning_pkg.planner.PLANNING_REPAIR_TIMEOUT_SECONDS
    planning_pkg.planner.PLANNING_REPAIR_TIMEOUT_SECONDS = 0.01

    class Runtime:
        async def invoke_prompt(self, prompt, **kwargs):
            await asyncio.sleep(1)
            return {"output": '[{"step_number":1}]'}

    caplog.set_level(logging.WARNING, logger="test.planning_repair_timeout_metadata")
    try:
        with pytest.raises(TimeoutError):
            PlannerService.repair_output(
                runtime_service=Runtime(),
                task_description="Build a page",
                malformed_output='{"steps":"bad"}',
                project_dir=__import__("pathlib").Path("/tmp/project"),
                timeout_seconds=300,
                logger=logging.getLogger("test.planning_repair_timeout_metadata"),
                emit_live=lambda *a, **kw: None,
                reason="plan_contains_immediate_repair_issues: background_process_steps",
                rejection_reasons=["commands must be an array"],
                knowledge_context=None,
                session_id=1,
                task_id=2,
            )
    finally:
        planning_pkg.planner.PLANNING_REPAIR_TIMEOUT_SECONDS = original_timeout

    assert "repair_prompt_chars=" in caplog.text
    assert "malformed_output_chars=" in caplog.text
    assert "plan_contains_immediate_repair_issues" in caplog.text


def test_planning_validation_failure_after_repair_marks_session_not_running(
    tmp_path, monkeypatch
):
    plan = [
        {
            "step_number": 1,
            "description": "Inspect files",
            "commands": ["ls"],
            "verification": "echo ok",
            "rollback": None,
            "expected_files": [],
        }
    ]

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
            return {"status": "completed", "output": json.dumps(plan)}

    task = MagicMock()
    task.title = "Reject repaired plan"
    task.description = "Reject repaired plan"
    session = MagicMock()
    session.status = "running"
    session.is_active = True
    session_task_link = MagicMock()

    ctx = OrchestrationRunContext(
        db=MagicMock(),
        session=session,
        project=MagicMock(),
        task=task,
        session_task_link=session_task_link,
        session_id=46,
        task_id=5,
        prompt="Reject repaired plan",
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=orchestration_state,
        runtime_service=Runtime(),
        task_service=MagicMock(),
        logger=logging.getLogger("test.planner_validation_failure"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=MagicMock(),
    )
    ctx.error_handler.attempt_json_parsing = lambda output, **kwargs: (
        True,
        json.loads(output),
        "json",
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
        "app.services.orchestration.phases.planning_flow.record_validation_verdict",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.planning_flow.maybe_emit_divergence_detected",
        lambda *args, **kwargs: None,
    )
    monkeypatch.setattr(
        PlannerService,
        "should_start_with_minimal_prompt",
        staticmethod(lambda *args, **kwargs: False),
    )
    monkeypatch.setattr(
        PlannerService,
        "repair_output",
        classmethod(lambda cls, *args, **kwargs: {"output": json.dumps(plan)}),
    )
    monkeypatch.setattr(
        ValidatorService,
        "validate_plan",
        staticmethod(
            lambda *args, **kwargs: type(
                "Verdict",
                (),
                {
                    "accepted": False,
                    "warning": False,
                    "status": "rejected",
                    "reasons": ["Plan contains brittle commands"],
                    "details": {},
                    "verdict": {"status": "rejected"},
                },
            )()
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
        "reason": "planning_validation_failed_after_repair",
    }
    assert task.status == TaskStatus.FAILED
    assert task.completed_at is not None
    assert session_task_link.status == TaskStatus.FAILED
    assert session_task_link.completed_at is not None
    assert session.status == "paused"
    assert session.is_active is False
    assert session.paused_at is not None
