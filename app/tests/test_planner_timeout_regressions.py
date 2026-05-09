import asyncio
import logging
import json
import time
from unittest.mock import MagicMock

import pytest

from app.models import TaskStatus
from app.services.orchestration.phases.planning_flow import (
    _PlanningRetryState,
    _build_repair_rejection_reasons,
    _classify_planning_timeout_failure,
    _compress_project_context_for_planning,
    _emit_planning_diagnostics_contract_violation,
    _get_targeted_second_repair_reason,
    _plan_contract_diagnostics,
    _should_repair_truncated_single_step_plan,
    _terminal_validation_failure_details,
    TRUNCATED_PLAN_REPAIR_REJECTION_REASON,
    execute_planning_phase,
)
from app.services.orchestration.planning.planner import (
    PlannerService,
    MINIMAL_PLANNING_PROMPT_TOKEN_DIAGNOSTIC_THRESHOLD,
    PlanningRepairBudgetExceeded,
    PlanningRepairNoOutputTimeout,
    PlanningRepairOutputContractViolation,
    PLANNING_REPAIR_MAX_MALFORMED_OUTPUT_CHARS,
    PLANNING_REPAIR_PROMPT_MAX_CHARS,
)
from app.services.orchestration.types import OrchestrationRunContext
from app.services.orchestration.validation.validator import ValidatorService
from app.services.orchestration.validation.parsing import extract_structured_text
from app.services.orchestration.policy import (
    MINIMAL_PLANNING_TIMEOUT_SECONDS,
    ORCHESTRATION_TASK_SOFT_TIME_LIMIT_SECONDS,
    ORCHESTRATION_TASK_TIME_LIMIT_SECONDS,
    PLANNING_REPAIR_TIMEOUT_SECONDS,
    PLANNING_REPAIR_NO_OUTPUT_TIMEOUT_SECONDS,
    STRICT_JSON_RETRY_TIMEOUT_SECONDS,
    ULTRA_MINIMAL_PLANNING_TIMEOUT_SECONDS,
)
from app.services.agents.openclaw_service import (
    OpenClawSessionError,
    OpenClawSessionService,
)
from app.tasks.worker import execute_orchestration_task


def _valid_three_step_plan():
    return [
        {
            "step_number": 1,
            "description": "Inspect current planning modules",
            "commands": ['rg -n "PlannerService" app/services/orchestration/planning'],
            "verification": "python3 -c \"print('inspect ok')\"",
            "rollback": None,
            "expected_files": [],
        },
        {
            "step_number": 2,
            "description": "Update planner timeout handling",
            "commands": ["printf 'ok\\n' > planner_timeout_marker.txt"],
            "verification": "python3 - <<'PY'\nfrom pathlib import Path\nassert Path('planner_timeout_marker.txt').read_text() == 'ok\\n'\nPY",
            "rollback": "rm -f planner_timeout_marker.txt",
            "expected_files": ["planner_timeout_marker.txt"],
        },
        {
            "step_number": 3,
            "description": "Verify planner tests",
            "commands": [
                "python3 -m pytest app/tests/test_planner_timeout_regressions.py -q"
            ],
            "verification": "python3 -m pytest app/tests/test_planner_timeout_regressions.py -q",
            "rollback": None,
            "expected_files": [],
        },
    ]


def _patch_planning_flow_external_writes(monkeypatch):
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


def test_worker_soft_time_limit_allows_planning_retries_and_execution_headroom():
    assert ORCHESTRATION_TASK_SOFT_TIME_LIMIT_SECONDS == 3300
    assert ORCHESTRATION_TASK_TIME_LIMIT_SECONDS == 3600
    assert execute_orchestration_task.soft_time_limit == 3300
    assert execute_orchestration_task.time_limit == 3600


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


def test_initial_planning_prompt_contains_valid_json_contract_example():
    from app.services.prompt_templates import PromptTemplates

    prompt = PromptTemplates.build_planning_prompt(
        "Build a small React page",
        project_context="empty workspace",
        project_dir="/tmp/project",
    )

    assert prompt.startswith(
        "Return ONLY a valid JSON array. First character must be `[`. Last must be `]`.\n"
        "No prose. No markdown fences. No plan.json. No explanation."
    )
    assert "Valid Minimal JSON Example:" in prompt
    assert '"step_number": 1' in prompt
    assert '"description": "Inspect the current workspace"' in prompt
    assert '"commands": ["rg --files . | sort"]' in prompt
    assert (
        "No background processes, &, nohup, disown, dev servers, or long commands"
        in prompt
    )
    assert (
        "Verification must use `node -e`, `npm run build`, `python -m`, or a project test command"
        in prompt
    )
    assert "no `test -f`, `grep -q`, or `echo`" in prompt
    assert "Prefer scaffold: `npm create vite@latest . -- --template react`" in prompt
    assert "If scaffold is used, do not use heredoc" in prompt
    assert "exactly these six keys and no extra keys" in prompt
    assert "No markdown. No prose." in prompt
    assert 'Objects like {"steps": [...]} instead of a top-level array' in prompt


def test_minimal_and_ultra_minimal_planning_prompts_include_contract_example():
    minimal = PlannerService.build_minimal_planning_prompt(
        "Build a small React page",
        project_dir=__import__("pathlib").Path("/tmp/project"),
    )
    ultra = PlannerService.build_ultra_minimal_planning_prompt(
        "Build a small React page",
        project_dir=__import__("pathlib").Path("/tmp/project"),
    )

    for prompt in (minimal, ultra):
        assert prompt.startswith(
            "Return ONLY a valid JSON array. First character must be `[`. Last must be `]`.\n"
            "No prose. No markdown fences. No plan.json. No explanation."
        )
        assert "Valid minimal JSON example:" in prompt
        assert '"step_number": 1' in prompt
        assert '"commands": ["rg --files . | sort"]' in prompt
        assert "exactly these keys and no extra keys" in prompt
        assert "No markdown. No prose." in prompt


def test_planning_repair_normalizes_fenced_json_array(tmp_path):
    events = []
    plan_json = json.dumps(_valid_three_step_plan())

    class Runtime:
        async def invoke_prompt(self, *args, **kwargs):
            return {"output": f"```json\n{plan_json}\n```"}

    result = PlannerService.repair_output(
        runtime_service=Runtime(),
        task_description="Build a small Python utility",
        malformed_output="not json",
        project_dir=tmp_path,
        timeout_seconds=10,
        logger=logging.getLogger("test.planning_repair_normalizes_fenced_json_array"),
        emit_live=lambda level, message, metadata=None: events.append(
            (level, message, metadata or {})
        ),
        reason="plan_validation_failed",
    )

    assert result["output"] == plan_json
    assert any(
        metadata.get("reason") == "planning_repair_fenced_json_normalized"
        for _, _, metadata in events
    )


def test_planning_repair_still_rejects_prose_output(tmp_path):
    class Runtime:
        async def invoke_prompt(self, *args, **kwargs):
            return {"output": "Here is the repaired plan: []"}

    with pytest.raises(PlanningRepairOutputContractViolation):
        PlannerService.repair_output(
            runtime_service=Runtime(),
            task_description="Build a small Python utility",
            malformed_output="not json",
            project_dir=tmp_path,
            timeout_seconds=10,
            logger=logging.getLogger("test.planning_repair_still_rejects_prose_output"),
            emit_live=lambda *args, **kwargs: None,
            reason="plan_validation_failed",
        )


def test_minimal_prompt_retry_emits_prompt_size_diagnostics(tmp_path):
    events = []
    captured = {}

    class Runtime:
        async def execute_task(self, prompt, **kwargs):
            captured["prompt"] = prompt
            captured["kwargs"] = kwargs
            return {"status": "completed", "output": "[]"}

    PlannerService.retry_with_minimal_prompt(
        runtime_service=Runtime(),
        task_description="Build a small Python health checker",
        project_dir=tmp_path,
        timeout_seconds=300,
        logger=logging.getLogger("test.minimal_prompt_size_diagnostics"),
        emit_live=lambda level, message, metadata=None: events.append(
            (level, message, metadata or {})
        ),
        reason="dense_planning_context",
        workflow_profile="default",
    )

    retry_metadata = next(
        metadata
        for _, _, metadata in events
        if metadata.get("retry") == "minimal_prompt_first"
    )
    attempt_metadata = next(
        metadata
        for _, _, metadata in events
        if metadata.get("strategy") == "minimal_prompt" and metadata.get("attempt") == 2
    )

    assert retry_metadata["minimal_prompt_chars"] == len(captured["prompt"])
    assert (
        retry_metadata["minimal_prompt_estimated_tokens"]
        == (len(captured["prompt"]) + 3) // 4
    )
    assert retry_metadata["minimal_prompt_token_threshold"] == (
        MINIMAL_PLANNING_PROMPT_TOKEN_DIAGNOSTIC_THRESHOLD
    )
    assert retry_metadata["ultra_dense_planning_context"] is False
    assert (
        attempt_metadata["minimal_prompt_estimated_tokens"]
        == retry_metadata["minimal_prompt_estimated_tokens"]
    )
    assert captured["kwargs"]["diagnostic_label"] == "MINIMAL_PLANNING"
    assert captured["kwargs"]["diagnostic_metadata"]["planning_attempt"] == "minimal"
    assert (
        captured["kwargs"]["diagnostic_metadata"]["minimal_prompt_estimated_tokens"]
        == retry_metadata["minimal_prompt_estimated_tokens"]
    )


def test_openclaw_session_lock_is_classified_distinctly():
    exc = TimeoutError(
        "OpenClaw planning failed: session file locked (timeout 10000ms): "
        "pid=123 /root/.openclaw/agents/main/sessions/sessions.json.lock"
    )

    assert (
        _classify_planning_timeout_failure(exc, _PlanningRetryState())
        == "planning_openclaw_lock_contention"
    )
    assert PlannerService.is_openclaw_lock_contention(
        {
            "error": (
                "Error: session file locked (timeout 10000ms): "
                "pid=123 /root/.openclaw/agents/main/sessions/sessions.json.lock"
            )
        }
    )


def test_planning_model_calls_use_interprocess_lock(tmp_path, monkeypatch):
    from app.services.orchestration.planning import planner as planner_module

    captured = {}
    lock_path = tmp_path / "planning.lock"
    monkeypatch.setattr(
        planner_module,
        "OPENCLAW_PLANNING_LOCK_PATH",
        lock_path,
    )

    class Runtime:
        async def execute_task(self, prompt, **kwargs):
            captured["lock_exists_during_call"] = (
                planner_module.OPENCLAW_PLANNING_LOCK_PATH.exists()
            )
            captured["prompt"] = prompt
            captured["kwargs"] = kwargs
            return {"status": "completed", "output": "[]"}

    result = asyncio.run(
        PlannerService._execute_task_with_planning_lock(
            Runtime(),
            "[]",
            timeout_seconds=120,
            reuse_task_session=False,
        )
    )

    assert result["status"] == "completed"
    assert captured["prompt"] == "[]"
    assert captured["kwargs"]["timeout_seconds"] == 120
    assert captured["lock_exists_during_call"] is True
    assert lock_path.exists()


def test_minimal_prompt_retry_flags_ultra_dense_prompt_without_changing_retry(
    tmp_path,
    monkeypatch,
):
    events = []
    captured = {}
    huge_prompt = "x" * (MINIMAL_PLANNING_PROMPT_TOKEN_DIAGNOSTIC_THRESHOLD * 4 + 1)

    class Runtime:
        async def execute_task(self, prompt, **kwargs):
            captured["prompt"] = prompt
            captured["kwargs"] = kwargs
            return {"status": "completed", "output": "[]"}

    monkeypatch.setattr(
        PlannerService,
        "build_minimal_planning_prompt",
        staticmethod(lambda *args, **kwargs: huge_prompt),
    )

    PlannerService.retry_with_minimal_prompt(
        runtime_service=Runtime(),
        task_description="Build a small Python health checker",
        project_dir=tmp_path,
        timeout_seconds=300,
        logger=logging.getLogger("test.ultra_dense_minimal_prompt_diagnostics"),
        emit_live=lambda level, message, metadata=None: events.append(
            (level, message, metadata or {})
        ),
        reason="dense_planning_context",
        workflow_profile="default",
    )

    ultra_dense_events = [
        metadata
        for _, _, metadata in events
        if metadata.get("reason") == "ultra_dense_planning_context"
    ]

    assert ultra_dense_events
    assert ultra_dense_events[0]["ultra_dense_planning_context"] is True
    assert ultra_dense_events[0]["minimal_prompt_estimated_tokens"] > (
        MINIMAL_PLANNING_PROMPT_TOKEN_DIAGNOSTIC_THRESHOLD
    )
    assert captured["prompt"] == huge_prompt
    assert captured["kwargs"]["diagnostic_label"] == "MINIMAL_PLANNING"


def test_minimal_planning_prompt_keeps_workflow_rules_for_existing_fullstack_workspace():
    prompt = PlannerService.build_minimal_planning_prompt(
        "Bring the existing frontend and backend to dev-ready state",
        project_dir=__import__("pathlib").Path("/tmp/project"),
        workflow_profile="fullstack_scaffold",
        workflow_phases=[
            "create_frontend_skeleton",
            "create_backend_skeleton",
            "wire_api_config",
            "verify_dev_startup",
        ],
        workspace_has_existing_files=True,
    )

    assert "Workflow profile: fullstack_scaffold" in prompt
    assert "Extend or verify existing files instead of re-scaffolding" in prompt
    assert "Never use parent-directory traversal like `../backend`" in prompt
    assert "`verification` must be a single shell string or null" in prompt
    assert "Do not use background processes" in prompt


def test_large_planning_context_is_compressed_before_first_attempt():
    state = type("State", (), {})()
    state.project_context = "Very long planning context " * 600
    state.plan = []
    state.current_step_index = 0
    state.completed_steps = []
    state.failed_steps = []
    state.debug_attempts = []
    state.changed_files = []

    compressed = _compress_project_context_for_planning(state)

    assert len(compressed) < len(state.project_context)
    assert "Very long planning context" in compressed


def test_planner_rejects_pseudo_commands_and_flags_background_commands():
    issues = PlannerService.find_immediate_repair_step_issues(
        [
            {
                "step_number": 1,
                "description": "Write file",
                "commands": ["write frontend/src/App.tsx: render root shell"],
                "verification": "python3 -m py_compile frontend/src/App.tsx",
                "expected_files": ["frontend/src/App.tsx"],
            },
            {
                "step_number": 2,
                "description": "Start backend",
                "commands": ["cd backend && npx tsx src/index.ts &"],
            },
        ]
    )

    assert issues == {
        "non_runnable_steps": [1],
        "background_process_steps": [2],
    }


def test_planner_flags_placeholder_only_implementation_and_weak_verification():
    issues = PlannerService.find_immediate_repair_step_issues(
        [
            {
                "step_number": 1,
                "description": "Create the webpage files",
                "commands": [
                    "mkdir -p assets/css assets/js",
                    "touch index.html assets/css/styles.css assets/js/app.js",
                ],
                "verification": "test -f index.html && test -f assets/css/styles.css",
                "rollback": "rm -f index.html assets/css/styles.css assets/js/app.js",
                "expected_files": [
                    "index.html",
                    "assets/css/styles.css",
                    "assets/js/app.js",
                ],
            }
        ]
    )

    assert issues == {
        "placeholder_only_steps": [1],
        "weak_verification_steps": [1],
    }


def test_planner_allows_scaffold_only_structurally_empty_files():
    issues = PlannerService.find_immediate_repair_step_issues(
        [
            {
                "step_number": 1,
                "description": "Create project directory structure",
                "commands": [
                    "mkdir -p orchestrator tests",
                    "touch orchestrator/__init__.py tests/__init__.py",
                ],
                "verification": 'python3 -c "import orchestrator, tests"',
                "rollback": "rm -rf orchestrator tests",
                "expected_files": [
                    "orchestrator/__init__.py",
                    "tests/__init__.py",
                ],
            }
        ]
    )

    assert "placeholder_only_steps" not in issues


def test_planner_still_flags_scaffold_only_normal_files():
    issues = PlannerService.find_immediate_repair_step_issues(
        [
            {
                "step_number": 1,
                "description": "Create service files",
                "commands": [
                    "mkdir -p services tests",
                    "touch services/health.py tests/test_health.py",
                ],
                "verification": "python3 -m py_compile services/health.py",
                "rollback": "rm -rf services tests",
                "expected_files": [
                    "services/health.py",
                    "tests/test_health.py",
                ],
            }
        ]
    )

    assert issues["placeholder_only_steps"] == [1]


def test_minimal_planning_prompt_requires_real_content_and_strong_verification():
    prompt = PlannerService.build_minimal_planning_prompt(
        "Build a one-page site",
        project_dir=__import__("pathlib").Path("/tmp/project"),
        workspace_has_existing_files=True,
    )

    assert "materially write or edit file contents" in prompt
    assert "verification must prove behavior or content" in prompt
    assert "Commands must be runnable shell, not prose" in prompt
    assert "Do not create or cd into a nested project folder" in prompt
    assert "Return 3 or 4 small sequential steps maximum" in prompt
    assert "Keep each command under 900 characters" in prompt
    assert "Include exactly one final meaningful verification/build step" in prompt
    assert "inspect -> edit -> verify" in prompt
    assert "Do not use heredoc-heavy commands" in prompt
    assert "never emit `python -c` commands" in prompt
    assert (
        "Do not put escaped apostrophes like `\\'` inside single-quoted strings"
        in prompt
    )
    assert (
        "No background processes, &, nohup, disown, dev servers, or long commands"
        in prompt
    )
    assert (
        "Verification must use `node -e`, `npm run build`, `python -m`, or a project test command"
        in prompt
    )
    assert "no `test -f`, `grep -q`, or `echo`" in prompt
    assert "Prefer scaffold: `npm create vite@latest . -- --template react`" in prompt
    assert "If scaffold is used, do not use heredoc" in prompt
    assert (
        "Each step must include exactly these keys and no extra keys: step_number, description, commands, verification, rollback, expected_files"
        in prompt
    )
    assert "`step_number` must be a unique integer" in prompt
    assert "Do not omit keys" in prompt


def test_weak_verification_is_treated_as_blocking_immediate_repair_issue():
    plan = [
        {
            "step_number": 1,
            "description": "Build the page shell",
            "commands": [
                "mkdir -p assets/css",
                "printf '<!doctype html>' > index.html",
            ],
            "verification": "test -f index.html",
            "rollback": "rm -f index.html",
            "expected_files": ["index.html"],
        }
    ]

    issues = PlannerService.find_immediate_repair_step_issues(plan)
    assert issues["weak_verification_steps"] == [1]


def test_python3_assertion_import_text_is_not_weak_verification_for_repair_gate():
    plan = [
        {
            "step_number": 1,
            "description": "Create project structure and shared models",
            "commands": [
                "mkdir -p services",
                "printf 'class WorkflowRecord: pass\\n' > models.py",
            ],
            "verification": (
                "python3 -c 'from models import WorkflowRecord; "
                'record = WorkflowRecord(); assert record is not None; print("OK")\''
            ),
            "rollback": "rm -f models.py",
            "expected_files": ["models.py"],
        },
        {
            "step_number": 2,
            "description": "Implement service handlers",
            "commands": [
                "mkdir -p services",
                "printf 'class ServiceHandler: pass\\n' > services/handlers.py",
            ],
            "verification": (
                "python3 -c 'from services.handlers import ServiceHandler; "
                "from models import WorkflowRecord; handler = ServiceHandler(); "
                'record = WorkflowRecord(); assert handler is not None and record is not None; print("OK")\''
            ),
            "rollback": "rm -f services/handlers.py",
            "expected_files": ["services/handlers.py"],
        },
    ]

    issues = PlannerService.find_immediate_repair_step_issues(plan)

    assert "weak_verification_steps" not in issues


def test_validator_rejects_weak_verification_for_implementation_plan(tmp_path):
    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Build the page shell",
                "commands": ["printf '<!doctype html>' > index.html"],
                "verification": "test -f index.html",
                "rollback": "rm -f index.html",
                "expected_files": ["index.html"],
            }
        ],
        output_text="[]",
        task_prompt="Build a one-page site",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert verdict.repairable is True
    assert "weak_verification_steps" in verdict.details
    assert "weak_verification" in verdict.details["semantic_violation_codes"]


def test_validator_accepts_python3_assertion_import_text_as_strong_verification(
    tmp_path,
):
    plan = [
        {
            "step_number": 1,
            "description": "Build the model implementation",
            "commands": ["printf 'class WorkflowRecord: pass\\n' > models.py"],
            "verification": (
                "python3 -c 'from models import WorkflowRecord; "
                'record = WorkflowRecord(); assert record is not None; print("OK")\''
            ),
            "rollback": "rm -f models.py",
            "expected_files": ["models.py"],
        }
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Build a workflow model",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert "weak_verification_steps" not in verdict.details
    assert "weak_verification" not in verdict.details.get(
        "semantic_violation_codes", []
    )


def test_validator_still_rejects_standalone_weak_shell_verification(tmp_path):
    plan = [
        {
            "step_number": 1,
            "description": "Build the model implementation",
            "commands": ["printf 'class WorkflowRecord: pass\\n' > models.py"],
            "verification": "ls models.py",
            "rollback": "rm -f models.py",
            "expected_files": ["models.py"],
        }
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Build a workflow model",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert verdict.details["weak_verification_steps"] == [1]


def test_validator_stack_conflict_ignores_json_method_substring(tmp_path):
    plan = [
        {
            "step_number": 1,
            "description": "Create FastAPI health endpoint",
            "commands": [
                "printf 'from fastapi import FastAPI\\napp = FastAPI()\\n' > app.py"
            ],
            "verification": "python3 -m py_compile app.py",
            "rollback": "rm -f app.py",
            "expected_files": ["app.py"],
        },
        {
            "step_number": 2,
            "description": "Create TestClient health test",
            "commands": [
                'printf \'def test_health():\\n    assert response.json()["status"] == "ok"\\n\' > test_app.py'
            ],
            "verification": "python3 -m pytest test_app.py",
            "rollback": "rm -f test_app.py",
            "expected_files": ["test_app.py"],
        },
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Create a minimal FastAPI app with a health endpoint",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert "stack_conflict" not in verdict.details
    assert (
        "Plan mixes inconsistent implementation stacks for one task"
        not in verdict.reasons
    )


def test_validator_stack_conflict_still_detects_real_js_file(tmp_path):
    plan = [
        {
            "step_number": 1,
            "description": "Create Python and JavaScript files",
            "commands": [
                "printf 'print(\"ok\")\\n' > app.py",
                "printf 'console.log(\"ok\")\\n' > main.js",
            ],
            "verification": "python3 -m py_compile app.py",
            "rollback": "rm -f app.py main.js",
            "expected_files": ["app.py", "main.js"],
        }
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Create a small health endpoint",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert verdict.details["stack_conflict"] is True
    assert (
        "Plan mixes inconsistent implementation stacks for one task" in verdict.reasons
    )


def test_validator_treats_placeholder_stub_plan_as_repairable(tmp_path):
    plan = [
        {
            "step_number": 1,
            "description": "Build the health service",
            "commands": [
                "mkdir -p services",
                "printf 'class ServiceStatus:\\n    pass\\n' > services/health.py",
            ],
            "verification": "python3 -m py_compile services/health.py",
            "rollback": "rm -f services/health.py",
            "expected_files": ["services/health.py"],
        }
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Build a distributed workflow health checker",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert verdict.status == "repair_required"
    assert verdict.repairable is True
    assert verdict.rejected is False
    assert verdict.details["placeholder_only_implementation"] is True
    assert (
        "Plan appears to generate placeholder or stub implementations"
        in verdict.reasons
    )


def test_validator_treats_placeholder_stub_plus_oversized_plan_as_repairable(
    tmp_path,
):
    long_body = "x = 1\n" * 220
    plan = [
        {
            "step_number": 1,
            "description": "Build the health service",
            "commands": [
                "mkdir -p services",
                "printf 'class ServiceStatus:\\n    pass\\n' > services/health.py",
            ],
            "verification": "python3 -m py_compile services/health.py",
            "rollback": "rm -f services/health.py",
            "expected_files": ["services/health.py"],
        },
        {
            "step_number": 2,
            "description": "Write a large test module",
            "commands": [f"printf '{long_body}' > tests/test_health.py"],
            "verification": "python3 -m py_compile tests/test_health.py",
            "rollback": "rm -f tests/test_health.py",
            "expected_files": ["tests/test_health.py"],
        },
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Build a distributed workflow health checker",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert verdict.status == "repair_required"
    assert verdict.rejected is False
    assert verdict.details["placeholder_only_implementation"] is True
    assert "oversized_command_length" in verdict.details["brittle_command_subcodes"]
    assert (
        "Plan appears to generate placeholder or stub implementations"
        in verdict.reasons
    )
    assert (
        "Plan contains brittle heredoc-heavy or malformed commands" in verdict.reasons
    )


def test_validator_does_not_set_placeholder_flag_for_non_stub_plan(tmp_path):
    plan = [
        {
            "step_number": 1,
            "description": "Build the health service",
            "commands": [
                "mkdir -p services",
                "printf 'class ServiceStatus:\\n    status = \"healthy\"\\n' > services/health.py",
            ],
            "verification": "python3 -m py_compile services/health.py",
            "rollback": "rm -f services/health.py",
            "expected_files": ["services/health.py"],
        }
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Build a distributed workflow health checker",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert "placeholder_only_implementation" not in verdict.details
    assert (
        "Plan appears to generate placeholder or stub implementations"
        not in verdict.reasons
    )


def test_validator_rejects_non_runnable_pseudo_command_with_code(tmp_path):
    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Create the landing page",
                "commands": ["create files for the board game cafe landing page"],
                "verification": "python3 - <<'PY'\nprint('ok')\nPY",
                "rollback": None,
                "expected_files": ["src/App.tsx"],
            }
        ],
        output_text="[]",
        task_prompt="Build a board game cafe landing page",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert verdict.repairable is True
    assert verdict.details["non_runnable_steps"] == [1]
    assert "non_runnable_command" in verdict.details["semantic_violation_codes"]


def test_validator_rejects_nested_project_folder_command_with_code(tmp_path):
    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Create a nested Vite app",
                "commands": [
                    "npm create vite@latest board-game-cafe -- --template react"
                ],
                "verification": "npm run build",
                "rollback": "rm -rf board-game-cafe",
                "expected_files": [
                    "board-game-cafe/package.json",
                    "board-game-cafe/src/App.tsx",
                    "board-game-cafe/index.html",
                ],
            }
        ],
        output_text="[]",
        task_prompt="Build a board game cafe landing page",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert verdict.repairable is True
    assert verdict.details["nested_project_root_steps"] == [1]
    assert (
        "nested_project_folder_command" in verdict.details["semantic_violation_codes"]
    )


def test_validator_rejects_missing_verification_with_code(tmp_path):
    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Build the page shell",
                "commands": ["printf '<main>Board Game Cafe</main>' > index.html"],
                "verification": None,
                "rollback": "rm -f index.html",
                "expected_files": ["index.html"],
            }
        ],
        output_text="[]",
        task_prompt="Build a board game cafe landing page",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert verdict.repairable is True
    assert verdict.details["missing_verification_steps"] == [1]
    assert "missing_verification_command" in verdict.details["semantic_violation_codes"]


def test_schema_valid_planner_output_passes_validator_without_repair(tmp_path):
    plan = [
        {
            "step_number": 1,
            "description": "Inspect current Python runtime entry points",
            "commands": ['rg -n "FastAPI|create_app|app =" app || true'],
            "verification": "python3 -c \"print('inspect ok')\"",
            "rollback": None,
            "expected_files": [],
        },
        {
            "step_number": 2,
            "description": "Update runtime configuration defaults",
            "commands": ["mkdir -p app && printf 'VALUE = 1\\n' > app/config.py"],
            "verification": "python3 -m py_compile app/config.py",
            "rollback": "rm -f app/config.py",
            "expected_files": ["app/config.py"],
        },
        {
            "step_number": 3,
            "description": "Verify configuration imports cleanly",
            "commands": ["python3 -m py_compile app/config.py"],
            "verification": "python3 -m py_compile app/config.py",
            "rollback": None,
            "expected_files": [],
        },
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Update runtime configuration",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert verdict.accepted is True
    assert verdict.repairable is False
    assert verdict.rejected is False
    assert verdict.details["step_count"] == 3
    assert verdict.details["max_command_length"] > 0
    assert verdict.details["heredoc_command_count"] == 0
    assert verdict.details["command_total_chars"] > 0


def test_validator_rejects_too_many_initial_plan_steps(tmp_path):
    plan = [
        {
            "step_number": index,
            "description": f"Inspect area {index}",
            "commands": ["rg --files . | sort"],
            "verification": "python3 -c \"print('ok')\"",
            "rollback": None,
            "expected_files": [],
        }
        for index in range(1, 6)
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Inspect the current project",
        execution_profile="review_only",
        project_dir=tmp_path,
    )

    assert verdict.repairable is True
    assert verdict.details["step_count"] == 5
    assert verdict.details["max_steps"] == 4
    assert "too many steps" in " ".join(verdict.reasons).lower()


def test_validator_rejects_huge_heredoc_command_with_budget_diagnostics(tmp_path):
    huge_body = "\n".join(f"line {index}" for index in range(120))
    command = f"cat > src/App.tsx << 'EOF'\n{huge_body}\nEOF"
    plan = [
        {
            "step_number": 1,
            "description": "Write oversized component inline",
            "commands": ["mkdir -p src", command],
            "verification": "python3 -c \"print('ok')\"",
            "rollback": "rm -f src/App.tsx",
            "expected_files": ["src/App.tsx"],
        },
        {
            "step_number": 2,
            "description": "Run build",
            "commands": ["npm run build"],
            "verification": "npm run build",
            "rollback": None,
            "expected_files": [],
        },
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Build a React landing page",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert verdict.repairable is True
    assert "brittle heredoc-heavy" in " ".join(verdict.reasons)
    assert verdict.details["step_count"] == 2
    assert verdict.details["max_command_length"] == len(command)
    assert verdict.details["heredoc_command_count"] == 1
    assert verdict.details["command_total_chars"] >= len(command)
    assert verdict.details["oversized_command_steps"] == [1]


def test_validator_routes_printf_apostrophe_shell_quoting_to_repair(tmp_path):
    command = (
        "printf 'export default function App() {\\n"
        "  return <h2>This Week\\'s Featured Games</h2>;\\n"
        "}\\n' > src/App.jsx"
    )
    plan = [
        {
            "step_number": 1,
            "description": "Write React component with malformed shell quoting",
            "commands": ["mkdir -p src", command],
            "verification": "node -e \"require('fs').readFileSync('src/App.jsx','utf8')\"",
            "rollback": "rm -f src/App.jsx",
            "expected_files": ["src/App.jsx"],
        },
        {
            "step_number": 2,
            "description": "Run build",
            "commands": ["npm run build"],
            "verification": "npm run build",
            "rollback": None,
            "expected_files": [],
        },
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Build a React/Vite landing page",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert verdict.repairable is True
    assert verdict.details["malformed_shell_quoting_steps"] == [1]
    assert "malformed_shell_quoting" in verdict.details["semantic_violation_codes"]


def test_validator_accepts_single_relative_file_write_heredoc(tmp_path):
    command = (
        "mkdir -p src && cat > src/App.jsx <<'EOF'\n"
        "export default function App() { return <main>Board Game Cafe</main>; }\n"
        "EOF"
    )
    plan = [
        {
            "step_number": 1,
            "description": "Create the Vite package file",
            "commands": [
                'printf \'{"scripts":{"build":"vite --host 0.0.0.0"},"dependencies":{"@vitejs/plugin-react":"latest","vite":"latest","react":"latest","react-dom":"latest"},"devDependencies":{}}\\n\' > package.json'
            ],
            "verification": "node -e \"const p=require('./package.json'); if(!p.scripts.build) process.exit(1)\"",
            "rollback": "rm -f package.json",
            "expected_files": ["package.json"],
        },
        {
            "step_number": 2,
            "description": "Write one concise React component",
            "commands": [command],
            "verification": "node -e \"const fs=require('fs'); if(!fs.readFileSync('src/App.jsx','utf8').includes('Board Game Cafe')) process.exit(1)\"",
            "rollback": "rm -f src/App.jsx",
            "expected_files": ["src/App.jsx"],
        },
        {
            "step_number": 3,
            "description": "Run build",
            "commands": ["npm run build"],
            "verification": "npm run build",
            "rollback": None,
            "expected_files": [],
        },
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Build a React/Vite landing page",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert verdict.accepted is True
    assert verdict.details["heredoc_command_count"] == 1
    assert verdict.details["max_command_length"] < 900


def test_validator_rejects_multi_file_heredoc_command(tmp_path):
    command = (
        "cat > src/App.jsx <<'EOF'\n"
        "export default function App() { return <main>Board Game Cafe</main>; }\n"
        "EOF\n"
        "cat > src/App.css <<'EOF'\n"
        "main { color: #123; }\n"
        "EOF"
    )
    plan = [
        {
            "step_number": 1,
            "description": "Write multiple files in one command",
            "commands": [command],
            "verification": "node -e \"console.log('ok')\"",
            "rollback": "rm -f src/App.jsx src/App.css",
            "expected_files": ["src/App.jsx", "src/App.css"],
        },
        {
            "step_number": 2,
            "description": "Run build",
            "commands": ["npm run build"],
            "verification": "npm run build",
            "rollback": None,
            "expected_files": [],
        },
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Build a React/Vite landing page",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert verdict.repairable is True
    assert "brittle heredoc-heavy" in " ".join(verdict.reasons)
    assert verdict.details["heredoc_command_count"] == 2


def test_validator_accepts_concise_three_step_react_vite_landing_page_plan(tmp_path):
    plan = [
        {
            "step_number": 1,
            "description": "Create package files and source directory at the project root",
            "commands": [
                'mkdir -p src && printf \'{"scripts":{"build":"vite --host 0.0.0.0"},"dependencies":{"@vitejs/plugin-react":"latest","vite":"latest","react":"latest","react-dom":"latest","typescript":"latest"},"devDependencies":{}}\\n\' > package.json'
            ],
            "verification": "node -e \"const p=require('./package.json'); if(!p.scripts.build) process.exit(1)\"",
            "rollback": "rm -rf src package.json",
            "expected_files": ["package.json"],
        },
        {
            "step_number": 2,
            "description": "Write the board game cafe React landing page",
            "commands": [
                'printf \'export default function App() { return <main>Board Game Cafe</main>; }\\n\' > src/App.tsx && printf \'<div id="root"></div><script type="module" src="src/App.tsx"></script>\\n\' > index.html'
            ],
            "verification": "node -e \"const fs=require('fs'); if(!fs.readFileSync('src/App.tsx','utf8').includes('Board Game Cafe')) process.exit(1)\"",
            "rollback": "rm -f src/App.tsx index.html",
            "expected_files": ["src/App.tsx", "index.html"],
        },
        {
            "step_number": 3,
            "description": "Run the project build",
            "commands": ["npm run build"],
            "verification": "npm run build",
            "rollback": None,
            "expected_files": [],
        },
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Build a simple landing page for a board game cafe with React/Vite.",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert verdict.accepted is True
    assert verdict.details.get("semantic_violation_codes") is None
    assert verdict.details["step_count"] == 3
    assert verdict.details["max_command_length"] < 900
    assert verdict.details["heredoc_command_count"] == 0


def test_semantic_violation_metadata_is_logged_with_task_execution_id():
    events = []
    ctx = MagicMock(
        session_id=55,
        task_id=10,
        task_execution_id=21,
        emit_live=lambda level, message, metadata=None: events.append(
            (level, message, metadata or {})
        ),
    )

    _emit_planning_diagnostics_contract_violation(
        ctx,
        reason="plan_validation_failed",
        contract_violations=[
            "Plan contains non-runnable pseudo-commands such as `edit` or prose instructions (steps: [1])"
        ],
        semantic_violation_codes=["non_runnable_command"],
        output_text='[{"step_number":1}]',
        strategy_info="plan_validation_failed",
    )

    assert events == [
        (
            "WARN",
            "[OPENCLAW][PLANNING_DIAGNOSTICS] contract violation detected",
            {
                "session_id": 55,
                "task_id": 10,
                "task_execution_id": 21,
                "contract_violation_type": "non_runnable_command",
                "reason": "plan_validation_failed",
                "strategy_info": "plan_validation_failed",
                "output_chars": 19,
                "truncated_output_detected": False,
                "contract_violations": [
                    "Plan contains non-runnable pseudo-commands such as `edit` or prose instructions (steps: [1])"
                ],
                "semantic_violation_codes": ["non_runnable_command"],
                "step_count": None,
                "max_command_length": None,
                "heredoc_command_count": None,
                "command_total_chars": None,
            },
        )
    ]


def test_planner_sanitizes_common_local_model_static_site_plan_issues():
    sanitized = PlannerService.sanitize_common_plan_issues(
        [
            {
                "step_number": 1,
                "description": "Create index",
                "commands": [
                    "write index.html: html shell",
                    "file index.html should be a semantic landing page",
                ],
                "verification": "test -f index.html",
                "rollback": "trash index.html",
                "expected_files": ["index.html"],
            },
            {
                "step_number": 2,
                "description": "Final validation: open the page in a local preview to confirm rendering",
                "commands": [
                    "python3 -m http.server 8080 --bind 127.0.0.1 &",
                    "sleep 1 && curl -s -o /dev/null -w '%{http_code}' http://127.0.0.1:8080/index.html",
                    "pkill -f 'python3 -m http.server 8080' || true",
                ],
                "verification": "echo ok",
                "rollback": "pkill -f 'python3 -m http.server' || true",
                "expected_files": ["index.html"],
            },
        ]
    )

    assert len(sanitized) == 1
    assert sanitized[0]["step_number"] == 1
    assert sanitized[0]["commands"] == ["write index.html: html shell"]
    assert sanitized[0]["rollback"] == "rm -f index.html"
    assert sanitized[0]["verification"] == "test -f index.html"
    assert sanitized[0]["expected_files"] == ["index.html"]


def test_planner_sanitization_aligns_schema_and_step_sequence():
    sanitized = PlannerService.sanitize_common_plan_issues(
        [
            {
                "step_number": 9,
                "description": "",
                "commands": "printf 'ok\\n' > app/config.py",
                "verification": ["python3 -m py_compile app/config.py"],
                "rollback": "",
                "expected_files": "app/config.py",
            },
            {
                "step_number": 9,
                "description": "Verify config import",
                "commands": ["python3 -m py_compile app/config.py", ""],
                "verification": "python3 -m py_compile app/config.py",
                "rollback": None,
                "expected_files": None,
            },
        ]
    )

    assert sanitized == [
        {
            "step_number": 1,
            "description": "Execute step 1",
            "commands": ["printf 'ok\\n' > app/config.py"],
            "verification": None,
            "rollback": None,
            "expected_files": ["app/config.py"],
        },
        {
            "step_number": 2,
            "description": "Verify config import",
            "commands": ["python3 -m py_compile app/config.py"],
            "verification": "python3 -m py_compile app/config.py",
            "rollback": None,
            "expected_files": [],
        },
    ]


def test_planning_repair_prompt_forbids_duplicated_workspace_roots():
    prompt = PlannerService.build_planning_repair_prompt(
        "Build frontend and backend scaffolding",
        malformed_output='[{"step_number":1,"commands":["mkdir -p frontend/src/frontend/src"]}]',
        project_dir=__import__("pathlib").Path("/tmp/project"),
        workflow_profile="fullstack_scaffold",
        workflow_phases=[
            "create_frontend_skeleton",
            "create_backend_skeleton",
            "wire_api_config",
            "verify_dev_startup",
        ],
    )

    assert "frontend/src/frontend/src" in prompt
    assert "backend/src/backend/src" in prompt
    assert "rooted exactly once" in prompt
    assert "Never use parent-directory traversal like `../backend`" not in prompt


def test_planning_repair_prompt_bans_external_helpers_and_heredoc():
    prompt = PlannerService.build_planning_repair_prompt(
        "Build a React/Vite landing page",
        malformed_output='[{"step_number":2,"commands":["python3 /root/write_file.py src/App.jsx ..."]}]',
        project_dir=__import__("pathlib").Path("/tmp/project"),
        rejection_reasons=[
            "Plan commands reference parent-directory paths outside the task workspace (steps: [2])"
        ],
    )

    assert prompt.startswith(
        "Return ONLY a valid JSON array. First character must be `[`. Last must be `]`.\n"
        "No prose. No markdown fences. No plan.json. No explanation."
    )
    assert "Do not create, edit, read, or write files during planning repair" in prompt
    assert "return the JSON array as message text only" in prompt
    assert "Repair the plan, not the task" in prompt
    assert "Preserve valid steps" in prompt
    assert "Use 3 to 4 steps" in prompt
    assert "no separate mkdir/touch-only scaffold step for normal files" in prompt
    assert "/root/write_file.py" in prompt
    assert "absolute helper scripts" in prompt
    assert "no `test -f`, `grep -q`, `echo`, or `cd /... &&`" in prompt
    assert "No `\\'` inside single-quoted strings" in prompt
    assert "{{ return <main>Ready</main>; }}" not in prompt
    assert "npm create vite@latest . -- --template react" in prompt
    assert "it creates src/App.jsx and src/App.css" in prompt
    assert "Never use heredoc" in prompt
    assert "<<'EOF'" in prompt
    assert "<<'PY'" in prompt
    assert "<<'HEREDOC'" in prompt
    assert "Use printf to overwrite only needed JSX body/CSS lines" in prompt
    assert "Always use printf for all file writes" in prompt
    assert "exactly ONE heredoc across ENTIRE plan, all steps combined" not in prompt
    assert "use double quotes instead" in prompt
    assert "use double quotes or heredoc" not in prompt
    assert "multiple heredoc commands" in prompt


def test_planning_repair_prompt_includes_truncated_plan_restart_hint():
    prompt = PlannerService.build_planning_repair_prompt(
        "Build a workflow checker",
        malformed_output='[{"step_number":1,"commands":["printf \\"unterminated',
        project_dir=__import__("pathlib").Path("/tmp/project"),
        rejection_reasons=[TRUNCATED_PLAN_REPAIR_REJECTION_REASON],
    )

    assert "Validation error:" in prompt
    assert "Output was cut off mid-stream" in prompt
    assert "Ignore the broken output above" in prompt
    assert "Produce a complete new JSON array from scratch" in prompt


def test_planning_repair_prompt_uses_reduced_context_only():
    knowledge_context = type(
        "KnowledgeCtx",
        (),
        {
            "retrieved_items": [
                type(
                    "Ref",
                    (),
                    {
                        "knowledge_type": "format_guide",
                        "title": "First",
                        "content": "alpha" * 200,
                    },
                )(),
                type(
                    "Ref",
                    (),
                    {
                        "knowledge_type": "task_example",
                        "title": "Second",
                        "content": "beta" * 200,
                    },
                )(),
                type(
                    "Ref",
                    (),
                    {
                        "knowledge_type": "debug_case",
                        "title": "Third",
                        "content": "gamma" * 200,
                    },
                )(),
            ]
        },
    )()

    prompt = PlannerService.build_planning_repair_prompt(
        "Massive task context that should not survive into repair prompt",
        malformed_output='{"nonProjectContext":"' + ("x" * 7000) + '"}',
        project_dir=__import__("pathlib").Path("/tmp/project"),
        rejection_reasons=[
            "commands must be an array",
            "verification must be a shell string",
        ],
        workflow_profile="fullstack_scaffold",
        workflow_phases=[
            "create_frontend_skeleton",
            "create_backend_skeleton",
        ],
        workspace_has_existing_files=True,
        knowledge_context=knowledge_context,
    )

    assert "Task:" not in prompt
    assert "Working directory:" not in prompt
    assert "Workflow profile:" not in prompt
    assert "projectContext" not in prompt
    assert "nonProjectContext" not in prompt
    assert "[format_guide]" not in prompt
    assert "[task_example]" not in prompt
    assert "Third" not in prompt
    assert "nonProjectContextChars" not in prompt
    assert "Massive task context" not in prompt
    assert "Validation error:" in prompt
    assert "Strict output schema:" in prompt
    assert "logs, session history" in prompt
    assert len(prompt) < PLANNING_REPAIR_PROMPT_MAX_CHARS


def test_planning_repair_prompt_has_deterministic_compact_limit():
    prompt = PlannerService.build_planning_repair_prompt(
        "Large context should be ignored",
        malformed_output=json.dumps(
            {
                "payloads": [{"text": "remove me"}],
                "finalAssistantVisibleText": "x" * 12000,
                "projectContext": "project context must be stripped",
            }
        ),
        project_dir=__import__("pathlib").Path("/tmp/project"),
        rejection_reasons=["validation error " + ("z" * 1000)] * 20,
        knowledge_context=type(
            "KnowledgeCtx",
            (),
            {
                "retrieved_items": [
                    type(
                        "Ref",
                        (),
                        {
                            "knowledge_type": "format_guide",
                            "title": "Huge guide",
                            "content": "guide " * 5000,
                        },
                    )()
                ]
            },
        )(),
    )

    assert len(prompt) < 4000
    assert len(prompt) < PLANNING_REPAIR_PROMPT_MAX_CHARS
    assert "...<truncated malformed planning output>..." in prompt
    assert "project context must be stripped" not in prompt
    assert "Huge guide" not in prompt
    excerpt = prompt.split("Validation error:")[0]
    assert len(excerpt) < PLANNING_REPAIR_MAX_MALFORMED_OUTPUT_CHARS + 400


def test_validator_rejects_brittle_python_c_with_nested_quotes(tmp_path):
    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Check Python version",
                "commands": [
                    "python3 -c \"import sys; print(f'Python {sys.version}')\"",
                ],
                "verification": "test -n ok",
                "rollback": None,
                "expected_files": [],
            }
        ],
        output_text="[]",
        task_prompt="Check runtime",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert verdict.repairable is True
    assert "brittle" in " ".join(verdict.reasons).lower()


def test_shell_safe_command_guide_recommends_python_heredoc():
    guide = (
        __import__("pathlib")
        .Path("knowledge/seed/format_guides/shell-safe-command.md")
        .read_text()
    )

    assert "prefer heredoc syntax for inline python" in guide.lower()
    assert "python3 - <<'PY'" in guide


def test_planning_repair_still_succeeds_for_small_malformed_output():
    captured = {}

    class Runtime:
        async def execute_task(self, prompt, timeout_seconds=300, **kwargs):
            captured["prompt"] = prompt
            captured["timeout_seconds"] = timeout_seconds
            return {"output": '[{"step_number":1}]'}

    result = PlannerService.repair_output(
        runtime_service=Runtime(),
        task_description="Build a page",
        malformed_output='{"steps":"bad"}',
        project_dir=__import__("pathlib").Path("/tmp/project"),
        timeout_seconds=300,
        logger=__import__("logging").getLogger("test"),
        emit_live=lambda *a, **kw: None,
        reason="json_parse_failed",
        rejection_reasons=["commands must be an array"],
        knowledge_context=None,
        session_id=1,
        task_id=2,
    )

    assert result == {"output": '[{"step_number":1}]'}
    assert "nonProjectContext" not in captured["prompt"]
    assert len(captured["prompt"]) < PLANNING_REPAIR_PROMPT_MAX_CHARS


def test_planning_repair_uses_task_workspace_one_shot_prompt_when_available():
    captured = {}

    class Runtime:
        async def invoke_prompt(self, prompt, **kwargs):
            captured["prompt"] = prompt
            captured["kwargs"] = kwargs
            return {"output": '[{"step_number":1}]'}

    result = PlannerService.repair_output(
        runtime_service=Runtime(),
        task_description="Build a page",
        malformed_output='{"projectContext":"bad","nonProjectContext":"bad"}',
        project_dir=__import__("pathlib").Path("/tmp/project"),
        timeout_seconds=300,
        logger=__import__("logging").getLogger("test"),
        emit_live=lambda *a, **kw: None,
        reason="json_parse_failed",
        rejection_reasons=["commands must be an array"],
        knowledge_context=None,
        session_id=1,
        task_id=2,
    )

    assert result == {"output": '[{"step_number":1}]'}
    assert "projectContext" not in captured["prompt"]
    assert "nonProjectContext" not in captured["prompt"]
    assert captured["kwargs"]["isolate_workspace_context"] is False
    assert captured["kwargs"]["session_prefix"] == "planning-repair"


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
    assert metadata["no_output_timeout_seconds"] == 200
    assert "secret prompt" not in json.dumps(metadata)


def test_planning_repair_timeout_is_capped_below_full_local_planning_budget():
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

    assert captured["timeout_seconds"] == PLANNING_REPAIR_TIMEOUT_SECONDS
    assert (
        captured["no_output_timeout_seconds"]
        == PLANNING_REPAIR_NO_OUTPUT_TIMEOUT_SECONDS
    )
    assert captured["timeout_seconds"] < MINIMAL_PLANNING_TIMEOUT_SECONDS


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
    assert duration_events[0]["timeout_seconds"] == PLANNING_REPAIR_TIMEOUT_SECONDS
    assert "repair_prompt_build_seconds" in duration_events[0]
    assert "openclaw_request_seconds" in duration_events[0]
    assert "parser_validation_seconds" in duration_events[0]
    assert duration_events[0]["repair_attempts"] == 1
    assert duration_events[0]["repair_output_chars"] > 0


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


def _openclaw_parse_service():
    service = object.__new__(OpenClawSessionService)
    service.logged_entries = []

    def log_entry(level, message, metadata=None, **kwargs):
        service.logged_entries.append(
            {
                "level": level,
                "message": message,
                "metadata": metadata,
                **kwargs,
            }
        )

    service._log_entry = log_entry
    return service


@pytest.mark.asyncio
async def test_openclaw_refuses_task_run_without_resolved_workspace_cwd():
    service = _openclaw_parse_service()
    service.task_model = object()
    service.session_model = None

    with pytest.raises(OpenClawSessionError, match="resolved project workspace cwd"):
        await service._run_cli_prompt_with_diagnostics(
            ["openclaw"],
            timeout_seconds=1,
            cwd=None,
            prompt="[]",
        )


def test_openclaw_parse_uses_stdout_only_model_output():
    service = _openclaw_parse_service()
    proc = __import__("subprocess").CompletedProcess(
        args=["openclaw"],
        returncode=0,
        stdout=json.dumps({"payloads": [{"text": "stdout plan"}]}),
        stderr="",
    )

    result = service._parse_openclaw_response(proc)

    assert result["status"] == "completed"
    assert result["output"] == "stdout plan"
    assert result["output_channel_used"] == "stdout"
    assert result["stderr_contains_model_content"] is False
    assert result["stderr_contains_only_logs"] is False


def test_openclaw_parse_normalizes_stderr_only_model_output():
    service = _openclaw_parse_service()
    proc = __import__("subprocess").CompletedProcess(
        args=["openclaw"],
        returncode=0,
        stdout="",
        stderr=json.dumps({"payloads": [{"text": "stderr plan"}]}),
    )

    result = service._parse_openclaw_response(proc)

    assert result["status"] == "completed"
    assert result["output"] == "stderr plan"
    assert result["output_channel_used"] == "stderr"
    assert result["stderr_contains_model_content"] is True
    assert result["stderr_contains_only_logs"] is False
    assert any(
        "normalized model response from stderr" in entry["message"]
        for entry in service.logged_entries
    )


def test_openclaw_parse_prefers_stdout_for_mixed_model_output():
    service = _openclaw_parse_service()
    proc = __import__("subprocess").CompletedProcess(
        args=["openclaw"],
        returncode=0,
        stdout=json.dumps({"payloads": [{"text": "stdout plan"}]}),
        stderr=json.dumps({"payloads": [{"text": "stderr plan"}]}),
    )

    result = service._parse_openclaw_response(proc)

    assert result["status"] == "completed"
    assert result["output"] == "stdout plan"
    assert result["output_channel_used"] == "mixed"
    assert result["stderr_contains_model_content"] is True


def test_openclaw_parse_does_not_treat_diagnostic_stderr_as_plan_output():
    service = _openclaw_parse_service()
    diagnostic_stderr = json.dumps(
        {
            "aborted": False,
            "source": "run",
            "systemPrompt": {"chars": 48902},
            "projectContextChars": 15365,
            "nonProjectContextChars": 33537,
        }
    )
    proc = __import__("subprocess").CompletedProcess(
        args=["openclaw"],
        returncode=0,
        stdout="",
        stderr=diagnostic_stderr,
    )

    result = service._parse_openclaw_response(proc)

    assert result["status"] == "failed"
    assert result["output"] == ""
    assert result["output_channel_used"] == "none"
    assert result["stderr_contains_model_content"] is False
    assert result["stderr_contains_only_logs"] is True


def test_openclaw_repair_diagnostics_log_keeps_task_execution_id():
    added = []

    class FakeDb:
        def add(self, entry):
            added.append(entry)

    service = object.__new__(OpenClawSessionService)
    service.db = FakeDb()
    service.session_id = 55
    service.task_id = 10
    service.task_execution_id = 17
    service.session_model = MagicMock(instance_id="phase6f")
    service.task_model = None

    entry = service._log_entry(
        "INFO",
        "[OPENCLAW][REPAIR_DIAGNOSTICS] duration=30.00s",
        metadata=json.dumps({"no_output_timeout": True}),
    )

    assert added == [entry]
    assert entry.session_id == 55
    assert entry.task_id == 10
    assert entry.task_execution_id == 17


def test_openclaw_planning_diagnostics_log_keeps_task_execution_id():
    added = []

    class FakeDb:
        def add(self, entry):
            added.append(entry)

    service = object.__new__(OpenClawSessionService)
    service.db = FakeDb()
    service.session_id = 55
    service.task_id = 10
    service.task_execution_id = 19
    service.session_model = MagicMock(instance_id="phase6h")
    service.task_model = None

    entry = service._log_entry(
        "INFO",
        "[OPENCLAW][PLANNING_DIAGNOSTICS] duration=64.55s",
        metadata=json.dumps(
            {
                "planning_prompt_size": 4096,
                "output_channel_used": "stderr",
                "stderr_contains_model_content": True,
                "contract_violation_type": "truncated_multistep_plan_detected",
            }
        ),
    )

    assert added == [entry]
    assert entry.session_id == 55
    assert entry.task_id == 10
    assert entry.task_execution_id == 19
    metadata = json.loads(entry.log_metadata)
    assert metadata["output_channel_used"] == "stderr"
    assert metadata["stderr_contains_model_content"] is True


def test_minimal_first_logging_is_not_strict_json_retry():
    events = []

    class Runtime:
        async def execute_task(self, prompt, timeout_seconds=300, **kwargs):
            return {"output": '[{"step_number":1}]'}

    PlannerService.retry_with_minimal_prompt(
        runtime_service=Runtime(),
        task_description="Build a page",
        project_dir=__import__("pathlib").Path("/tmp/project"),
        timeout_seconds=300,
        logger=__import__("logging").getLogger("test"),
        emit_live=lambda level, message, metadata=None: events.append(
            (level, message, metadata or {})
        ),
        reason="dense_planning_context",
    )

    warn_messages = [message for level, message, _ in events if level == "WARN"]
    assert any("Planning context is dense" in message for message in warn_messages)
    assert all("strict JSON retry" not in message for message in warn_messages)


def test_planning_repair_budget_fails_fast_without_retry():
    runtime = type(
        "Runtime",
        (),
        {
            "execute_task": lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("repair should be skipped before runtime call")
            )
        },
    )()
    oversized_output = '[{"step_number":1,"description":"' + ("x" * 12000) + '"}]'

    from app.services.orchestration import planning as planning_pkg

    original_budget = planning_pkg.planner.REPAIR_PROMPT_MAX_CHARS
    original_alias_budget = planning_pkg.planner.PLANNING_REPAIR_PROMPT_MAX_CHARS
    planning_pkg.planner.REPAIR_PROMPT_MAX_CHARS = 200
    planning_pkg.planner.PLANNING_REPAIR_PROMPT_MAX_CHARS = 200
    try:
        try:
            PlannerService.repair_output(
                runtime_service=runtime,
                task_description="Build a page",
                malformed_output=oversized_output,
                project_dir=__import__("pathlib").Path("/tmp/project"),
                timeout_seconds=300,
                logger=__import__("logging").getLogger("test"),
                emit_live=lambda *a, **kw: None,
                reason="json_parse_failed",
                rejection_reasons=["commands must be array " + ("z" * 400)] * 4,
                knowledge_context=type(
                    "KnowledgeCtx",
                    (),
                    {
                        "retrieved_items": [
                            type(
                                "Ref",
                                (),
                                {
                                    "knowledge_type": "format_guide",
                                    "title": "Hint",
                                    "content": "y" * 2000,
                                },
                            )(),
                            type(
                                "Ref",
                                (),
                                {
                                    "knowledge_type": "task_example",
                                    "title": "Hint 2",
                                    "content": "q" * 2000,
                                },
                            )(),
                        ]
                    },
                )(),
            )
        except PlanningRepairBudgetExceeded as exc:
            assert "malformed_output=" in str(exc)
            assert "validation_error=" in str(exc)
            assert "knowledge_context=" in str(exc)
        else:
            raise AssertionError("Expected PlanningRepairBudgetExceeded")
    finally:
        planning_pkg.planner.REPAIR_PROMPT_MAX_CHARS = original_budget
        planning_pkg.planner.PLANNING_REPAIR_PROMPT_MAX_CHARS = original_alias_budget


def test_validator_schema_requires_full_planner_step_shape():
    schema = ValidatorService.validate_plan_schema(
        [
            {
                "step_number": 1,
                "description": "Inspect files",
                "commands": ["rg -n foo app"],
                "expected_files": [],
            }
        ]
    )

    assert schema["valid"] is False
    assert "missing_required_fields" in schema["details"]
    assert schema["details"]["missing_required_fields"][1] == [
        "rollback",
        "verification",
    ]


def test_validator_schema_rejects_extra_planner_step_keys():
    schema = ValidatorService.validate_plan_schema(
        [
            {
                "step_number": 1,
                "description": "Inspect files",
                "commands": ["rg --files ."],
                "verification": "test -d .",
                "rollback": None,
                "expected_files": [],
                "rationale": "extra prose field",
            }
        ]
    )

    assert schema["valid"] is False
    assert "Plan steps must not include extra keys" in schema["errors"]
    assert schema["details"]["extra_fields"] == {1: ["rationale"]}


def test_planner_describes_contract_violations_before_repair():
    violations = PlannerService.describe_planning_contract_violations(
        output_text='```json\n{"steps": []}\n```',
        parse_success=False,
        strategy_info="json parse failed",
        plan_data={"steps": []},
        extracted_plan=[
            {
                "step_number": 1,
                "description": "Run dev server",
                "commands": ["npm run dev &"],
                "verification": "echo ok",
                "rollback": None,
                "expected_files": [],
                "notes": "extra",
            }
        ],
        immediate_repair_issues={"background_process_steps": [1]},
    )

    assert "markdown-wrapped JSON" in violations
    assert "object wrapper instead of top-level JSON array" in violations
    assert "step 1 has extra keys: notes" in violations
    assert "background process command in steps [1]" in violations


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


def test_targeted_second_repair_reason_centralizes_blocking_eligibility():
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True

    reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        blocking_repair_issues={"weak_verification_steps": [3, 2]},
    )

    assert reason is not None
    assert reason.issue_key == "weak_verification_steps"
    assert reason.event_reason == "post_repair_weak_verification_second_pass"
    assert reason.semantic_violation_code == "weak_verification"
    assert reason.step_numbers == [3, 2]
    assert reason.cap_used is False
    assert reason.cap_attribute == "post_repair_blocking_second_repair_used"
    assert "steps [3, 2]" in reason.rejection_text


def test_targeted_second_repair_reason_requires_prior_repair():
    retry_state = _PlanningRetryState()

    reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        blocking_repair_issues={"weak_verification_steps": [1]},
    )

    assert reason is None


def test_targeted_second_repair_reason_rejects_mixed_blocking_classes():
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True

    reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        blocking_repair_issues={
            "weak_verification_steps": [1],
            "background_process_steps": [2],
        },
    )

    assert reason is None


def test_targeted_second_repair_reason_respects_blocking_cap():
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True
    retry_state.post_repair_blocking_second_repair_used = True

    reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        blocking_repair_issues={"background_process_steps": [1]},
    )

    assert reason is not None
    assert reason.issue_key == "background_process_steps"
    assert reason.cap_used is True


def test_targeted_second_repair_reason_centralizes_validator_eligibility():
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True
    verdict = type(
        "Verdict",
        (),
        {
            "reasons": [
                "Plan is missing verification commands for implementation-heavy work (steps: [1])"
            ],
            "details": {
                "missing_verification_steps": [1],
                "semantic_violation_codes": ["missing_verification_command"],
            },
        },
    )()

    reason = _get_targeted_second_repair_reason(
        retry_state=retry_state,
        plan_verdict=verdict,
    )

    assert reason is not None
    assert reason.issue_key == "missing_verification_steps"
    assert reason.event_reason == "post_repair_missing_verification_second_pass"
    assert reason.semantic_violation_code == "missing_verification_command"
    assert reason.cap_attribute == "post_repair_validation_second_repair_used"
    assert "implementation-heavy step" in reason.rejection_text


def test_targeted_second_repair_reason_does_not_add_brittle_eligibility():
    retry_state = _PlanningRetryState()
    retry_state.repair_prompt_used = True
    verdict = type(
        "Verdict",
        (),
        {
            "reasons": ["Plan contains brittle heredoc-heavy or malformed commands"],
            "details": {
                "brittle_command_subcodes": ["oversized_command_length"],
                "semantic_violation_codes": ["brittle_command"],
            },
        },
    )()

    assert (
        _get_targeted_second_repair_reason(
            retry_state=retry_state,
            plan_verdict=verdict,
        )
        is None
    )


def test_post_repair_weak_verification_gets_one_targeted_second_repair(
    tmp_path, monkeypatch
):
    initial_plan = [
        {
            "step_number": 1,
            "description": "Create text stats implementation",
            "commands": [
                "printf 'def analyze_text(text): return {}\\n' > text_stats.py"
            ],
            "verification": "echo ok",
            "rollback": "rm -f text_stats.py",
            "expected_files": ["text_stats.py"],
        }
    ]
    first_repair_plan = [
        {
            "step_number": 1,
            "description": "Inspect files",
            "commands": ["ls"],
            "verification": "python -m pytest --version",
            "rollback": None,
            "expected_files": [],
        },
        {
            "step_number": 2,
            "description": "Create text stats implementation",
            "commands": [
                "printf 'def analyze_text(text): return {}\\n' > text_stats.py"
            ],
            "verification": "echo ok",
            "rollback": "rm -f text_stats.py",
            "expected_files": ["text_stats.py"],
        },
        {
            "step_number": 3,
            "description": "Create text stats tests",
            "commands": [
                "printf 'from text_stats import analyze_text\\n' > test_text_stats.py"
            ],
            "verification": "test -f test_text_stats.py",
            "rollback": "rm -f test_text_stats.py",
            "expected_files": ["test_text_stats.py"],
        },
    ]
    second_repair_plan = [
        {
            "step_number": 1,
            "description": "Inspect files",
            "commands": ["ls"],
            "verification": "python -m pytest --version",
            "rollback": None,
            "expected_files": [],
        },
        {
            "step_number": 2,
            "description": "Create text stats implementation",
            "commands": [
                "printf 'def analyze_text(text): return {}\\n' > text_stats.py"
            ],
            "verification": "python -m pytest test_text_stats.py -q",
            "rollback": "rm -f text_stats.py",
            "expected_files": ["text_stats.py"],
        },
        {
            "step_number": 3,
            "description": "Create text stats tests",
            "commands": [
                "printf 'from text_stats import analyze_text\\n' > test_text_stats.py"
            ],
            "verification": "python -m pytest test_text_stats.py -q",
            "rollback": "rm -f test_text_stats.py",
            "expected_files": ["test_text_stats.py"],
        },
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
            return {"status": "completed", "output": json.dumps(initial_plan)}

    task = MagicMock()
    task.title = "Repair weak verification"
    task.description = "Repair weak verification"
    session = MagicMock()
    session.status = "running"
    session.is_active = True
    session_task_link = MagicMock()
    events = []

    ctx = OrchestrationRunContext(
        db=MagicMock(),
        session=session,
        project=MagicMock(),
        task=task,
        session_task_link=session_task_link,
        session_id=64,
        task_id=15,
        prompt="Repair weak verification",
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=orchestration_state,
        runtime_service=Runtime(),
        task_service=MagicMock(),
        logger=logging.getLogger("test.post_repair_weak_verification_second_pass"),
        emit_live=lambda *args, **kwargs: events.append((args, kwargs)),
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
            "intent": "Create text utility",
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
    assert len(repair_calls) == 2
    assert repair_calls[1]["reason"].startswith("post_repair_weak_verification_steps")
    assert "steps [2, 3]" in repair_calls[1]["rejection_reasons"][0]
    assert ctx.orchestration_state.plan == second_repair_plan


def test_post_repair_weak_verification_second_repair_is_capped(tmp_path, monkeypatch):
    weak_plan = [
        {
            "step_number": 1,
            "description": "Create text stats implementation",
            "commands": [
                "printf 'def analyze_text(text): return {}\\n' > text_stats.py"
            ],
            "verification": "echo ok",
            "rollback": "rm -f text_stats.py",
            "expected_files": ["text_stats.py"],
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
            return {"status": "completed", "output": json.dumps(weak_plan)}

    task = MagicMock()
    task.title = "Repair weak verification cap"
    task.description = "Repair weak verification cap"
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
        session_id=65,
        task_id=16,
        prompt="Repair weak verification cap",
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=orchestration_state,
        runtime_service=Runtime(),
        task_service=MagicMock(),
        logger=logging.getLogger("test.post_repair_weak_verification_cap"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=MagicMock(),
    )
    ctx.error_handler.attempt_json_parsing = lambda output, **kwargs: (
        True,
        json.loads(output),
        "json",
    )

    _patch_planning_flow_external_writes(monkeypatch)
    monkeypatch.setattr(
        PlannerService,
        "should_start_with_minimal_prompt",
        staticmethod(lambda *args, **kwargs: False),
    )

    repair_calls = []

    def repair_output(cls, *args, **kwargs):
        repair_calls.append(kwargs)
        return {"output": json.dumps(weak_plan)}

    monkeypatch.setattr(PlannerService, "repair_output", classmethod(repair_output))

    result = execute_planning_phase(
        ctx=ctx,
        workspace_review={"has_existing_files": False},
        extract_structured_text=extract_structured_text,
        extract_plan_steps=lambda value: value if isinstance(value, list) else None,
        looks_like_truncated_multistep_plan=lambda text, plan: False,
        normalize_plan_with_live_logging=lambda *args, **kwargs: args[3],
        workspace_violation_error_cls=RuntimeError,
    )

    assert len(repair_calls) == 2
    assert result == {
        "status": "failed",
        "reason": "planning_invalid_commands_after_repair",
    }
    assert task.status == TaskStatus.FAILED
    assert session_task_link.status == TaskStatus.FAILED
    assert session.status == "paused"
    assert session.is_active is False


def test_post_repair_background_process_gets_one_targeted_second_repair(
    tmp_path, monkeypatch
):
    initial_plan = [
        {
            "step_number": 1,
            "description": "Create text stats implementation",
            "commands": [
                "printf 'def analyze_text(text): return {}\\n' > text_stats.py"
            ],
            "verification": "echo ok",
            "rollback": "rm -f text_stats.py",
            "expected_files": ["text_stats.py"],
        }
    ]
    first_repair_plan = [
        {
            "step_number": 1,
            "description": "Start a background server",
            "commands": ["python -m http.server 8000 &"],
            "verification": "python -m pytest --version",
            "rollback": None,
            "expected_files": [],
        }
    ]
    second_repair_plan = [
        {
            "step_number": 1,
            "description": "Run a bounded foreground check",
            "commands": ["python -m pytest --version"],
            "verification": "python -m pytest --version",
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
            return {"status": "completed", "output": json.dumps(initial_plan)}

    task = MagicMock()
    task.title = "Repair background process"
    task.description = "Repair background process"
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
        session_id=66,
        task_id=15,
        prompt="Repair background process",
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=orchestration_state,
        runtime_service=Runtime(),
        task_service=MagicMock(),
        logger=logging.getLogger("test.post_repair_background_process_second_pass"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=MagicMock(),
    )
    ctx.error_handler.attempt_json_parsing = lambda output, **kwargs: (
        True,
        json.loads(output),
        "json",
    )

    _patch_planning_flow_external_writes(monkeypatch)
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
    assert len(repair_calls) == 2
    assert repair_calls[1]["reason"].startswith("post_repair_background_process_steps")
    assert "steps [1]" in repair_calls[1]["rejection_reasons"][0]
    assert "bounded foreground commands" in repair_calls[1]["rejection_reasons"][0]
    assert ctx.orchestration_state.plan == second_repair_plan


def test_post_repair_missing_verification_gets_one_targeted_second_repair(
    tmp_path, monkeypatch
):
    initial_plan = [
        {
            "step_number": 1,
            "description": "Create CSV reporter",
            "commands": ["printf 'def build_report(rows): return []\\n' > reporter.py"],
            "verification": None,
            "rollback": "rm -f reporter.py",
            "expected_files": ["reporter.py"],
        }
    ]
    first_repair_plan = [
        {
            "step_number": 1,
            "description": "Create CSV reporter",
            "commands": ["printf 'def build_report(rows): return []\\n' > reporter.py"],
            "verification": None,
            "rollback": "rm -f reporter.py",
            "expected_files": ["reporter.py"],
        }
    ]
    second_repair_plan = [
        {
            "step_number": 1,
            "description": "Create CSV reporter",
            "commands": ["printf 'def build_report(rows): return []\\n' > reporter.py"],
            "verification": "python -m pytest test_reporter.py -q",
            "rollback": "rm -f reporter.py",
            "expected_files": ["reporter.py"],
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
            return {"status": "completed", "output": json.dumps(initial_plan)}

    task = MagicMock()
    task.title = "Repair missing verification"
    task.description = "Repair missing verification"
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
        session_id=67,
        task_id=17,
        prompt="Repair missing verification",
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=orchestration_state,
        runtime_service=Runtime(),
        task_service=MagicMock(),
        logger=logging.getLogger("test.post_repair_missing_verification_second_pass"),
        emit_live=lambda *args, **kwargs: None,
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
            "intent": "Create CSV reporter",
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
        PlannerService,
        "should_start_with_minimal_prompt",
        staticmethod(lambda *args, **kwargs: False),
    )
    monkeypatch.setattr(
        PlannerService,
        "find_immediate_repair_step_issues",
        staticmethod(lambda *args, **kwargs: {}),
    )

    repair_calls = []

    def repair_output(cls, *args, **kwargs):
        repair_calls.append(kwargs)
        if len(repair_calls) == 1:
            return {"output": json.dumps(first_repair_plan)}
        return {"output": json.dumps(second_repair_plan)}

    validate_calls = []

    def validate_plan(*args, **kwargs):
        validate_calls.append(args)
        if len(validate_calls) < 3:
            return type(
                "Verdict",
                (),
                {
                    "accepted": False,
                    "warning": False,
                    "status": "rejected",
                    "reasons": [
                        "Plan is missing verification commands for implementation-heavy work (steps: [1])"
                    ],
                    "details": {
                        "missing_verification_steps": [1],
                        "semantic_violation_codes": ["missing_verification_command"],
                    },
                    "verdict": {"status": "rejected"},
                },
            )()
        return type(
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

    monkeypatch.setattr(PlannerService, "repair_output", classmethod(repair_output))
    monkeypatch.setattr(ValidatorService, "validate_plan", staticmethod(validate_plan))

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
    assert len(repair_calls) == 2
    assert repair_calls[1]["reason"].startswith(
        "post_repair_missing_verification_steps"
    )
    assert "steps [1]" in repair_calls[1]["rejection_reasons"][0]
    assert "implementation-heavy step" in repair_calls[1]["rejection_reasons"][0]
    assert ctx.orchestration_state.plan == second_repair_plan


def test_post_repair_missing_verification_second_repair_is_capped(
    tmp_path, monkeypatch
):
    missing_plan = [
        {
            "step_number": 1,
            "description": "Create CSV reporter",
            "commands": ["printf 'def build_report(rows): return []\\n' > reporter.py"],
            "verification": None,
            "rollback": "rm -f reporter.py",
            "expected_files": ["reporter.py"],
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
            return {"status": "completed", "output": json.dumps(missing_plan)}

    task = MagicMock()
    task.title = "Repair missing verification cap"
    task.description = "Repair missing verification cap"
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
        session_id=68,
        task_id=17,
        prompt="Repair missing verification cap",
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="standard",
        runs_in_canonical_baseline=False,
        orchestration_state=orchestration_state,
        runtime_service=Runtime(),
        task_service=MagicMock(),
        logger=logging.getLogger("test.post_repair_missing_verification_cap"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=MagicMock(),
    )
    ctx.error_handler.attempt_json_parsing = lambda output, **kwargs: (
        True,
        json.loads(output),
        "json",
    )

    _patch_planning_flow_external_writes(monkeypatch)
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

    repair_calls = []

    def repair_output(cls, *args, **kwargs):
        repair_calls.append(kwargs)
        return {"output": json.dumps(missing_plan)}

    def rejected_missing_verification(*args, **kwargs):
        return type(
            "Verdict",
            (),
            {
                "accepted": False,
                "warning": False,
                "status": "rejected",
                "reasons": [
                    "Plan is missing verification commands for implementation-heavy work (steps: [1])"
                ],
                "details": {
                    "missing_verification_steps": [1],
                    "semantic_violation_codes": ["missing_verification_command"],
                },
                "verdict": {"status": "rejected"},
            },
        )()

    monkeypatch.setattr(PlannerService, "repair_output", classmethod(repair_output))
    monkeypatch.setattr(
        ValidatorService, "validate_plan", staticmethod(rejected_missing_verification)
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

    assert len(repair_calls) == 2
    assert result == {
        "status": "failed",
        "reason": "planning_validation_failed_after_repair",
    }
    assert task.status == TaskStatus.FAILED
    assert session_task_link.status == TaskStatus.FAILED
    assert session.status == "paused"
    assert session.is_active is False


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


def test_validator_brittle_subcodes_oversized_printf_command(tmp_path):
    # Mirrors Board Game Cafe TaskExecution 37: step 2 command 1684 chars,
    # step 3 command 1668 chars — both above MAX_PLANNING_COMMAND_CHARS (900).
    long_body = "A" * 1200
    plan = [
        {
            "step_number": 1,
            "description": "Scaffold project",
            "commands": ["npm create vite@latest . -- --template react", "npm install"],
            "verification": "node -e \"require('fs').existsSync('src/App.jsx')\"",
            "rollback": None,
            "expected_files": ["src/App.jsx"],
        },
        {
            "step_number": 2,
            "description": "Write App component",
            "commands": [f"printf '{long_body}' > src/App.jsx"],
            "verification": "npm run build",
            "rollback": "rm -f src/App.jsx",
            "expected_files": ["src/App.jsx"],
        },
        {
            "step_number": 3,
            "description": "Write CSS",
            "commands": [f"printf '{long_body}' > src/App.css"],
            "verification": "npm run build",
            "rollback": None,
            "expected_files": ["src/App.css"],
        },
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Build a Board Game Cafe landing page",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert verdict.repairable is True
    assert "brittle heredoc-heavy" in " ".join(verdict.reasons)
    assert "oversized_command_length" in verdict.details["brittle_command_subcodes"]
    assert 2 in verdict.details["brittle_command_step_details"]
    assert 3 in verdict.details["brittle_command_step_details"]
    assert (
        "oversized_command_length" in verdict.details["brittle_command_step_details"][2]
    )
    assert (
        "oversized_command_length" in verdict.details["brittle_command_step_details"][3]
    )
    assert verdict.details["brittle_command_step_command_lengths"][2]
    assert verdict.details["brittle_command_step_command_lengths"][3]


def test_validator_brittle_subcodes_too_many_lines(tmp_path):
    long_command = "echo start\n" + "\n".join(f"echo {i}" for i in range(30))
    plan = [
        {
            "step_number": 1,
            "description": "Run many echo lines",
            "commands": [long_command],
            "verification": "echo ok",
            "rollback": None,
            "expected_files": [],
        }
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Do something",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert verdict.repairable is True
    assert "too_many_lines" in verdict.details["brittle_command_subcodes"]
    assert 1 in verdict.details["brittle_command_step_details"]
    assert "too_many_lines" in verdict.details["brittle_command_step_details"][1]


def test_validator_brittle_subcodes_multiple_heredoc_across_plan(tmp_path):
    heredoc1 = "mkdir -p src && cat > src/App.jsx <<'EOF'\nexport default function App() {}\nEOF"
    heredoc2 = "cat > src/App.css <<'EOF'\nbody { margin: 0; }\nEOF"
    plan = [
        {
            "step_number": 1,
            "description": "Write component",
            "commands": [heredoc1],
            "verification": "echo ok",
            "rollback": None,
            "expected_files": ["src/App.jsx"],
        },
        {
            "step_number": 2,
            "description": "Write CSS",
            "commands": [heredoc2],
            "verification": "echo ok",
            "rollback": None,
            "expected_files": ["src/App.css"],
        },
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Build a landing page",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert verdict.repairable is True
    assert "multiple_heredoc_across_plan" in verdict.details["brittle_command_subcodes"]


def test_validator_brittle_aggregate_reason_preserved_alongside_subcodes(tmp_path):
    long_body = "B" * 1000
    plan = [
        {
            "step_number": 1,
            "description": "Write oversized file",
            "commands": [f"printf '{long_body}' > out.txt"],
            "verification": "echo ok",
            "rollback": None,
            "expected_files": ["out.txt"],
        }
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Do something",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert verdict.repairable is True
    reasons_text = " ".join(verdict.reasons)
    assert "Plan contains brittle heredoc-heavy or malformed commands" in reasons_text
    assert "brittle_command_subcodes" in verdict.details
    assert verdict.details["brittle_command_subcodes"]


# Phase 6P: pass oversized command details into repair


def test_repair_rejection_reasons_prepend_oversized_command_details():
    reasons = ["Plan contains brittle heredoc-heavy or malformed commands"]
    details = {
        "brittle_command_subcodes": ["oversized_command_length"],
        "oversized_command_steps": [2, 3],
        "brittle_command_step_command_lengths": {2: [1684], 3: [1668]},
    }

    enriched = _build_repair_rejection_reasons(reasons, details)

    assert enriched[0].startswith("oversized_command_length:")
    assert "steps [2, 3]" in enriched[0]
    assert "step 2: 1684 chars" in enriched[0]
    assert "step 3: 1668 chars" in enriched[0]
    assert "max 900" in enriched[0]
    assert enriched[1:] == reasons


def test_repair_rejection_reasons_prepend_multiple_heredoc_details():
    reasons = ["Plan contains brittle heredoc-heavy or malformed commands"]
    details = {
        "brittle_command_subcodes": ["multiple_heredoc_across_plan"],
        "heredoc_command_count": 3,
    }

    enriched = _build_repair_rejection_reasons(reasons, details)

    assert enriched[0].startswith("multiple_heredoc_across_plan:")
    assert "3 heredoc blocks found" in enriched[0]
    assert "max 1 across entire plan" in enriched[0]
    assert "Replace all but one with printf" in enriched[0]
    assert enriched[1:] == reasons


def test_repair_rejection_reasons_prepend_too_many_lines_step_details():
    reasons = ["Plan contains brittle heredoc-heavy or malformed commands"]
    details = {
        "brittle_command_subcodes": ["too_many_lines"],
        "brittle_command_step_details": {
            1: ["too_many_lines"],
            2: ["oversized_command_length"],
            "3": ["too_many_lines"],
        },
    }

    enriched = _build_repair_rejection_reasons(reasons, details)

    assert enriched[0].startswith("too_many_lines:")
    assert "step [1, 3]" in enriched[0]
    assert "commands exceed line limit" in enriched[0]
    assert "Use printf or split content across steps" in enriched[0]
    assert enriched[1:] == reasons


def test_repair_rejection_reasons_prepend_weak_verification_step_details():
    reasons = [
        "Plan uses weak verification for implementation-heavy work (steps: [1, 2])"
    ]
    details = {"weak_verification_steps": [2, "1", "bad"]}

    enriched = _build_repair_rejection_reasons(reasons, details)

    assert enriched[0].startswith("weak_verification_steps:")
    assert "steps [1, 2]" in enriched[0]
    assert "replace with pytest, python -m, or npm run build" in enriched[0]
    assert enriched[1:] == reasons


def test_repair_rejection_reasons_prepend_missing_verification_step_details():
    reasons = [
        "Plan is missing verification commands for implementation-heavy work (steps: [1])"
    ]
    details = {"missing_verification_steps": ["1", "bad"]}

    enriched = _build_repair_rejection_reasons(reasons, details)

    assert enriched[0].startswith("missing_verification_steps:")
    assert "steps [1]" in enriched[0]
    assert "add pytest, python -m, npm run build" in enriched[0]
    assert enriched[1:] == reasons


def test_repair_rejection_reasons_preserve_base_reasons_without_targeted_subcodes():
    reasons = ["Plan contains brittle heredoc-heavy or malformed commands"]
    details = {
        "brittle_command_subcodes": ["disallowed_heredoc_shape"],
        "brittle_command_step_details": {1: ["disallowed_heredoc_shape"]},
    }

    assert _build_repair_rejection_reasons(reasons, details) == reasons


def test_repair_prompt_includes_injected_oversized_rejection_line():
    reasons = _build_repair_rejection_reasons(
        ["Plan contains brittle heredoc-heavy or malformed commands"],
        {
            "brittle_command_subcodes": ["oversized_command_length"],
            "oversized_command_steps": [2],
            "brittle_command_step_command_lengths": {2: [1684]},
        },
    )

    prompt = PlannerService.build_planning_repair_prompt(
        "Build a landing page",
        malformed_output='[{"step_number":2,"commands":["printf ..."]}]',
        project_dir=__import__("pathlib").Path("/tmp/project"),
        rejection_reasons=reasons,
    )

    assert "Validation error:" in prompt
    assert "oversized_command_length: steps [2]" in prompt
    assert "step 2: 1684 chars" in prompt


def test_repair_prompt_includes_injected_brittle_shape_rejection_lines():
    reasons = _build_repair_rejection_reasons(
        ["Plan contains brittle heredoc-heavy or malformed commands"],
        {
            "brittle_command_subcodes": [
                "multiple_heredoc_across_plan",
                "too_many_lines",
            ],
            "heredoc_command_count": 2,
            "brittle_command_step_details": {1: ["too_many_lines"]},
        },
    )

    prompt = PlannerService.build_planning_repair_prompt(
        "Build a static page",
        malformed_output='[{"step_number":1,"commands":["cat > index.html <<EOF"]}]',
        project_dir=__import__("pathlib").Path("/tmp/project"),
        rejection_reasons=reasons,
    )

    assert "Validation error:" in prompt
    assert "multiple_heredoc_across_plan: 2 heredoc blocks found" in prompt
    assert "too_many_lines: step [1]" in prompt


def test_repair_prompt_includes_injected_weak_verification_rejection_line():
    reasons = _build_repair_rejection_reasons(
        ["Plan uses weak verification for implementation-heavy work (steps: [1, 2])"],
        {"weak_verification_steps": [1, 2]},
    )

    prompt = PlannerService.build_planning_repair_prompt(
        "Build a FastAPI health endpoint",
        malformed_output='[{"step_number":1,"verification":null}]',
        project_dir=__import__("pathlib").Path("/tmp/project"),
        rejection_reasons=reasons,
    )

    assert "Validation error:" in prompt
    assert "weak_verification_steps: steps [1, 2]" in prompt
    assert "pytest, python -m, or npm run build" in prompt


# Phase 6Q: expose brittle-command subcodes in planning events


def test_plan_contract_diagnostics_include_brittle_subcodes_when_present():
    diagnostics = _plan_contract_diagnostics(
        {
            "step_count": 3,
            "max_command_length": 1203,
            "heredoc_command_count": 0,
            "command_total_chars": 2445,
            "brittle_command_subcodes": ["oversized_command_length"],
            "brittle_command_step_details": {2: ["oversized_command_length"]},
        }
    )

    assert diagnostics["step_count"] == 3
    assert diagnostics["max_command_length"] == 1203
    assert diagnostics["brittle_command_subcodes"] == ["oversized_command_length"]
    assert diagnostics["brittle_command_step_details"] == {
        2: ["oversized_command_length"]
    }


def test_plan_contract_diagnostics_omit_brittle_keys_when_absent():
    diagnostics = _plan_contract_diagnostics(
        {
            "step_count": 3,
            "max_command_length": 1203,
            "heredoc_command_count": 0,
            "command_total_chars": 2445,
        }
    )

    assert "brittle_command_subcodes" not in diagnostics
    assert "brittle_command_step_details" not in diagnostics


def test_planning_contract_violation_event_includes_brittle_subcodes():
    events = []
    ctx = MagicMock(
        session_id=55,
        task_id=10,
        task_execution_id=38,
        emit_live=lambda level, message, metadata=None: events.append(
            (level, message, metadata or {})
        ),
    )

    _emit_planning_diagnostics_contract_violation(
        ctx,
        reason="plan_validation_failed",
        contract_violations=[
            "Plan contains brittle heredoc-heavy or malformed commands"
        ],
        contract_diagnostics={
            "step_count": 3,
            "max_command_length": 1203,
            "heredoc_command_count": 0,
            "command_total_chars": 2445,
            "brittle_command_subcodes": ["oversized_command_length"],
            "brittle_command_step_details": {2: ["oversized_command_length"]},
        },
        output_text='[{"step_number":2}]',
        strategy_info="plan_validation_failed",
    )

    metadata = events[0][2]
    assert metadata["brittle_command_subcodes"] == ["oversized_command_length"]
    assert metadata["brittle_command_step_details"] == {2: ["oversized_command_length"]}


def test_terminal_validation_failure_details_include_brittle_subcodes_when_present():
    verdict = type(
        "Verdict",
        (),
        {
            "reasons": ["Plan contains brittle heredoc-heavy or malformed commands"],
            "details": {
                "brittle_command_subcodes": ["oversized_command_length"],
                "brittle_command_step_details": {2: ["oversized_command_length"]},
            },
        },
    )()

    details = _terminal_validation_failure_details(verdict)

    assert details["reason"] == "planning_validation_failed_after_repair"
    assert details["validation_reasons"] == [
        "Plan contains brittle heredoc-heavy or malformed commands"
    ]
    assert details["brittle_command_subcodes"] == ["oversized_command_length"]
    assert details["brittle_command_step_details"] == {2: ["oversized_command_length"]}


def test_terminal_validation_failure_details_omit_brittle_keys_when_absent():
    verdict = type(
        "Verdict",
        (),
        {
            "reasons": ["Plan contains brittle heredoc-heavy or malformed commands"],
            "details": {},
        },
    )()

    details = _terminal_validation_failure_details(verdict)

    assert details == {
        "reason": "planning_validation_failed_after_repair",
        "validation_reasons": [
            "Plan contains brittle heredoc-heavy or malformed commands"
        ],
    }
