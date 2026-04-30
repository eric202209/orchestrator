from app.services.orchestration.phases.planning_flow import (
    _compress_project_context_for_planning,
    _should_repair_truncated_single_step_plan,
)
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.validation.validator import ValidatorService
from app.services.orchestration.policy import (
    MINIMAL_PLANNING_TIMEOUT_SECONDS,
    ORCHESTRATION_TASK_SOFT_TIME_LIMIT_SECONDS,
    ORCHESTRATION_TASK_TIME_LIMIT_SECONDS,
    PLANNING_REPAIR_TIMEOUT_SECONDS,
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
    assert PLANNING_REPAIR_TIMEOUT_SECONDS == 300
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


def test_planner_flags_immediate_repair_issues_for_write_and_background_commands():
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


def test_minimal_planning_prompt_requires_real_content_and_strong_verification():
    prompt = PlannerService.build_minimal_planning_prompt(
        "Build a one-page site",
        project_dir=__import__("pathlib").Path("/tmp/project"),
        workspace_has_existing_files=True,
    )

    assert "materially write or edit file contents" in prompt
    assert "verification must prove behavior or content" in prompt
    assert "inspect -> edit -> verify" in prompt


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
    assert "Never use parent-directory traversal like `../backend`" in prompt


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
