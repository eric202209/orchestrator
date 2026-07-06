import json
from unittest.mock import MagicMock

import pytest
from app.services.orchestration.phases.planning_flow import (
    _emit_planning_diagnostics_contract_violation,
)

from app.services.orchestration.planning.planner import PlannerService

from app.services.orchestration.validation.validator import ValidatorService


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


def test_validator_rejects_stringified_dict_commands_from_checkpoint_plan():
    plan = [
        {
            "step_number": 1,
            "description": "Create project directory",
            "commands": ["{'ops': 'mkdir project_root'}"],
            "verification": "python -c \"import os; print(os.path.exists('project_root'))\"",
            "rollback": "rm -rf project_root",
            "expected_files": ["project_root"],
            "ops": [],
        }
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Update documentation in the current project root",
        execution_profile="full_lifecycle",
        project_dir=None,
        title="Task 1: docs update",
        description="Update docs in the current project root",
    )

    assert not verdict.accepted
    assert "non-runnable pseudo-commands" in " ".join(verdict.reasons)
    assert verdict.details["non_runnable_steps"] == [1]


def test_validator_rejects_json_escaped_stringified_dict_commands():
    plan = [
        {
            "step_number": 1,
            "description": "Create project directory",
            "commands": ['{\\"ops\\": \\"mkdir project_root\\"}'],
            "verification": "python -m pytest app/tests -q",
            "rollback": None,
            "expected_files": ["project_root"],
            "ops": [],
        }
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Update documentation in the current project root",
        execution_profile="full_lifecycle",
        project_dir=None,
        title="Task 1: docs update",
        description="Update docs in the current project root",
    )

    assert not verdict.accepted
    assert verdict.details["non_runnable_steps"] == [1]


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
    assert "keep under 900 chars" in prompt
    assert "Include exactly one final meaningful verification/build step" in prompt
    assert "inspect -> edit -> verify" in prompt
    assert "Use `ops` for file writes" in prompt
    assert '"op": "write_file"' in prompt
    assert "fallback limits" in prompt
    assert "If content needs quoting, move that content into `ops`" in prompt
    assert (
        "Verification must be a real project check with a nonzero failure mode"
        in prompt
    )
    assert "No heredocs, background processes, absolute helpers" in prompt
    assert (
        "Verification must use `python -c`, `python -m`, `npm run build`, `node -e`, or a project test command"
        in prompt
    )
    assert "must prove behavior or content using current workspace evidence" in prompt
    assert "If a scaffold command is genuinely required" in prompt
    assert "use `ops` for any follow-up source edits" in prompt
    assert (
        "Each step must include these required keys, optional ops, and no other keys: step_number, description, commands, verification, rollback, expected_files"
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


def test_python_sys_exit_zero_without_real_check_is_weak_verification(tmp_path):
    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Create shell page",
                "commands": ["printf '<!doctype html>' > index.html"],
                "verification": "python3 -c 'import sys; sys.exit(0)'",
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


def test_validator_stack_conflict_ignores_readonly_inspection_globs(tmp_path):
    plan = [
        {
            "step_number": 1,
            "description": "Inspect current workspace",
            "commands": [
                "find . -type f -name '*.json' -o -name '*.js' -o -name '*.py' | head -20"
            ],
            "verification": None,
            "rollback": None,
            "expected_files": [],
        },
        {
            "step_number": 2,
            "description": "Create manifest.json",
            "ops": [
                {
                    "op": "write_file",
                    "path": "manifest.json",
                    "content": '{"name":"phase10a-alpha","version":"1.0.0"}',
                }
            ],
            "commands": [],
            "verification": "node -e \"const fs=require('fs'); JSON.parse(fs.readFileSync('manifest.json','utf8'))\"",
            "rollback": "rm -f manifest.json",
            "expected_files": ["manifest.json"],
        },
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Create manifest.json with name phase10a-alpha and version 1.0.0",
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


def test_validator_records_placeholder_source_write_context(tmp_path):
    plan = [
        {
            "step_number": 2,
            "description": "Create the missing import path",
            "commands": [],
            "verification": "python3 -m pytest -q",
            "rollback": None,
            "expected_files": ["src/math_tools/operations.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "src/math_tools/operations.py",
                    "content": "# Placeholder for operations\n\ndef add(x, y):\n    return x + y\n",
                }
            ],
        }
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Fix missing math_tools.operations import",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert verdict.details["placeholder_only_implementation"] is True
    assert verdict.details["placeholder_source_write_ops"] == [
        {
            "step_number": 2,
            "op": "write_file",
            "path": "src/math_tools/operations.py",
            "content_excerpt": "# Placeholder for operations def add(x, y): return x + y",
        }
    ]


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


def test_validator_allows_todo_fixture_content_for_report_generator(tmp_path):
    plan = [
        {
            "step_number": 1,
            "description": "Create sample files for the TODO report generator",
            "commands": [
                "mkdir -p fixtures",
                "printf '# Sample\\nTODO: Add intro\\nFIXME: Broken link\\n' > fixtures/sample.md",
            ],
            "ops": [
                {
                    "op": "write_file",
                    "path": "fixtures/sample.txt",
                    "content": "TODO: Refactor logic\nFIXME: Memory leak\n",
                }
            ],
            "verification": "test -f fixtures/sample.md && test -f fixtures/sample.txt",
            "rollback": "rm -rf fixtures",
            "expected_files": ["fixtures/sample.md", "fixtures/sample.txt"],
        },
        {
            "step_number": 2,
            "description": "Implement the TODO report generator",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "todo_report.py",
                    "content": "from pathlib import Path\nMARKERS = ('TODO', 'FIXME')\ntry:\n    print(Path('fixtures/sample.md').read_text())\nexcept OSError:\n    pass\n",
                }
            ],
            "verification": "python3 -m py_compile todo_report.py",
            "rollback": "rm -f todo_report.py",
            "expected_files": ["todo_report.py"],
        },
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Build a TODO and FIXME report generator with sample fixture files",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert "placeholder_only_implementation" not in verdict.details
    assert (
        "Plan appears to generate placeholder or stub implementations"
        not in verdict.reasons
    )


def test_validator_flags_write_file_stub_python_body(tmp_path):
    plan = [
        {
            "step_number": 1,
            "description": "Build the health service",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "services/health.py",
                    "content": "class ServiceStatus:\n    pass\n",
                }
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

    assert verdict.details["placeholder_only_implementation"] is True
    assert (
        "Plan appears to generate placeholder or stub implementations"
        in verdict.reasons
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

    assert sanitized[0]["verification"].startswith("python -c ")
    assert sanitized[0]["expected_files"] == ["app/config.py"]
    assert sanitized == [
        {
            "step_number": 1,
            "description": "Execute step 1",
            "commands": ["printf 'ok\\n' > app/config.py"],
            "verification": sanitized[0]["verification"],
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
