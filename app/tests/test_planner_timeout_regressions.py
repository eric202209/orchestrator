import logging
import json
from unittest.mock import MagicMock

from app.models import TaskStatus
from app.services.orchestration.phases.planning_flow import (
    _compress_project_context_for_planning,
    _should_repair_truncated_single_step_plan,
    execute_planning_phase,
)
from app.services.orchestration.planning.planner import (
    PlannerService,
    PlanningRepairBudgetExceeded,
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
    STRICT_JSON_RETRY_TIMEOUT_SECONDS,
    ULTRA_MINIMAL_PLANNING_TIMEOUT_SECONDS,
)
from app.tasks.worker import execute_orchestration_task


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
    assert STRICT_JSON_RETRY_TIMEOUT_SECONDS == 180
    assert PLANNING_REPAIR_TIMEOUT_SECONDS == 180
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


def test_planner_allows_write_pseudo_commands_but_flags_background_commands():
    issues = PlannerService.find_immediate_repair_step_issues(
        [
            {
                "step_number": 1,
                "description": "Write file",
                "commands": ["write frontend/src/App.tsx: render root shell"],
            },
            {
                "step_number": 2,
                "description": "Start backend",
                "commands": ["cd backend && npx tsx src/index.ts &"],
            },
        ]
    )

    assert issues == {
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


def test_minimal_planning_prompt_requires_real_content_and_strong_verification():
    prompt = PlannerService.build_minimal_planning_prompt(
        "Build a one-page site",
        project_dir=__import__("pathlib").Path("/tmp/project"),
        workspace_has_existing_files=True,
    )

    assert "materially write or edit file contents" in prompt
    assert "verification must prove behavior or content" in prompt
    assert "inspect -> edit -> verify" in prompt
    assert "prefer `python3 - <<'PY'` heredoc" in prompt
    assert "never emit `python -c` commands" in prompt
    assert (
        "Each step must include: step_number, description, commands, verification, rollback, expected_files"
        in prompt
    )
    assert "`step_number` must be a unique integer" in prompt
    assert "Do not omit keys" in prompt


def test_weak_verification_is_not_treated_as_blocking_immediate_repair_issue():
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
    blocking_issue_keys = {
        "non_runnable_steps",
        "background_process_steps",
        "placeholder_only_steps",
    }
    blocking = {
        key: value for key, value in issues.items() if key in blocking_issue_keys
    }

    assert issues["weak_verification_steps"] == [1]
    assert blocking == {}


def test_validator_still_warns_on_weak_verification_for_implementation_plan(tmp_path):
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

    assert verdict.warning is True
    assert "weak_verification_steps" in verdict.details


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
    assert prompt.count("[format_guide]") == 1
    assert prompt.count("[task_example]") == 1
    assert "Third" not in prompt
    assert "nonProjectContextChars" not in prompt
    assert "Validation error:" in prompt
    assert "Required JSON schema:" in prompt
    assert len(prompt) < PLANNING_REPAIR_PROMPT_MAX_CHARS
    assert "python3 - <<'PY'" in prompt


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


def test_planning_repair_uses_isolated_one_shot_prompt_when_available():
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
    assert captured["kwargs"]["isolate_workspace_context"] is True
    assert captured["kwargs"]["session_prefix"] == "planning-repair"


def test_planning_repair_timeout_is_capped_below_full_local_planning_budget():
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
        logger=__import__("logging").getLogger("test"),
        emit_live=lambda *a, **kw: None,
        reason="plan_validation_failed",
        rejection_reasons=["Plan contains brittle heredoc-heavy commands"],
        knowledge_context=None,
        session_id=1,
        task_id=2,
    )

    assert captured["timeout_seconds"] == PLANNING_REPAIR_TIMEOUT_SECONDS
    assert captured["timeout_seconds"] < MINIMAL_PLANNING_TIMEOUT_SECONDS


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
                rejection_reasons=[("commands must be array " + ("z" * 400))] * 4,
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
