import logging
import json

import pytest
from app.services.orchestration.phases.planning_flow import (
    _build_repair_rejection_reasons,
    TRUNCATED_PLAN_REPAIR_REJECTION_REASON,
)

from app.services.orchestration.phases.planning_support import (
    _repeated_physical_src_import_repair_details,
)

from app.services.orchestration.planning.planner import (
    PlannerService,
    PlanningRepairBudgetExceeded,
    PLANNING_REPAIR_MAX_MALFORMED_OUTPUT_CHARS,
    PLANNING_REPAIR_PROMPT_MAX_CHARS,
)

from app.services.orchestration.validation.validator import ValidatorService


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
    assert "no touch-only scaffold step" in prompt
    assert "/root/write_file.py" in prompt
    assert "absolute helper scripts" in prompt
    assert "no `echo` or `cd /... &&`" in prompt
    assert "{{ return <main>Ready</main>; }}" not in prompt
    assert "If scaffolding is required" in prompt
    assert "use ops for follow-up edits" in prompt
    assert "Use `ops` for file writes" in prompt
    assert "fallback limits" in prompt
    assert "write_file" in prompt
    assert "write_file.content and append_file.content must be JSON strings" in prompt
    assert "newline characters must be escaped as \\n" in prompt
    assert "do not use raw triple-quoted Python blocks" in prompt
    assert "do not place bare multiline code outside JSON quotes" in prompt
    assert "output must remain a valid JSON array" in prompt
    assert "exactly ONE heredoc across ENTIRE plan, all steps combined" not in prompt
    assert "use double quotes or heredoc" not in prompt
    assert "Each step is a separate JSON object" in prompt
    assert "Never merge steps" in prompt


def test_planning_repair_prompt_guides_unsafe_python_append_fragments():
    prompt = PlannerService.build_planning_repair_prompt(
        "Add a summary command to this Python CLI.",
        malformed_output=json.dumps(
            [
                {
                    "step_number": 2,
                    "description": "Append summary branch",
                    "commands": ["python -m py_compile src/medium_cli/cli.py"],
                    "verification": "python -m py_compile src/medium_cli/cli.py",
                    "rollback": None,
                    "expected_files": ["src/medium_cli/cli.py"],
                    "ops": [
                        {
                            "op": "append_file",
                            "path": "src/medium_cli/cli.py",
                            "content": (
                                "\n    elif command == 'summary':\n"
                                "        return 0\n"
                            ),
                        }
                    ],
                }
            ]
        ),
        project_dir=__import__("pathlib").Path("/tmp/project"),
        rejection_reasons=[
            "Plan uses append_file to add contextual Python control-flow "
            "fragments that only make sense inside an existing block; use "
            "context-aware replace_in_file or write_file with complete valid "
            "file content instead (files: ['src/medium_cli/cli.py'])",
            "unsafe_python_append_fragments: src/medium_cli/cli.py",
        ],
    )

    assert "Unsafe Python append_file repair" in prompt
    assert "Do not append indented `elif`" in prompt
    assert "context-aware `replace_in_file`" in prompt
    assert "`write_file` with complete valid file content" in prompt
    assert "complete def/class/import/comment" in prompt


def test_planning_repair_reasons_include_heredoc_and_inline_python_subcodes():
    reasons = _build_repair_rejection_reasons(
        ["Plan contains brittle heredoc-heavy or malformed commands"],
        {
            "brittle_command_subcodes": [
                "brittle_inline_python",
                "disallowed_heredoc_shape",
            ],
            "brittle_command_step_details": {
                1: ["disallowed_heredoc_shape"],
                2: ["brittle_inline_python"],
            },
            "placeholder_only_implementation": True,
        },
    )

    rendered = "\n".join(reasons)
    assert "Step [1]: invalid heredoc shape" in rendered
    assert "disallowed_heredoc_shape" in rendered
    assert "No heredoc" in rendered
    assert "Step [2]: brittle inline Python" in rendered
    assert "python -m py_compile" in rendered
    assert "tiny test file with ops" in rendered
    assert "placeholder_only_implementation:" in rendered
    assert reasons[-1] == "Plan contains brittle heredoc-heavy or malformed commands"


def test_placeholder_repair_reasons_include_offending_source_write_context():
    reasons = _build_repair_rejection_reasons(
        ["Plan appears to generate placeholder or stub implementations"],
        {
            "placeholder_only_implementation": True,
            "placeholder_source_write_ops": [
                {
                    "step_number": 2,
                    "op": "write_file",
                    "path": "src/math_tools/operations.py",
                    "content_excerpt": "# Placeholder for operations def add(x, y): return x + y",
                }
            ],
        },
    )

    rendered = "\n".join(reasons)
    assert "preserve source write path `src/math_tools/operations.py`" in rendered
    assert "replace placeholder/stub content with real implementation" in rendered
    assert "do not convert package imports to `src.*` imports" in rendered
    assert "do not remove materializing source operations" in rendered
    assert "# Placeholder for operations" in rendered


def test_physical_src_import_repair_reasons_include_invalid_line_and_guidance():
    reasons = _build_repair_rejection_reasons(
        [
            "Plan writes Python imports using the physical `src.` prefix in a "
            "src-layout project"
        ],
        {
            "physical_src_import_materializations": ["tests/test_operations_import.py"],
            "physical_src_import_details": [
                {
                    "path": "tests/test_operations_import.py",
                    "invalid_imports": ["from src.math_tools import operations"],
                }
            ],
        },
    )

    rendered = "\n".join(reasons)
    assert "Invalid import line(s): from src.math_tools import operations" in rendered
    assert "Do not use `src.` as a Python import prefix" in rendered
    assert "from math_tools.operations import add" in rendered
    assert "src/math_tools/operations.py" in rendered


def test_undefined_python_test_repair_reasons_preserve_existing_tests():
    reasons = _build_repair_rejection_reasons(
        ["Plan writes Python tests with obvious undefined names"],
        {
            "undefined_python_test_name_materializations": [
                "tests/test_cli_uppercase.py"
            ],
        },
    )

    rendered = "\n".join(reasons)
    assert "undefined_python_test_names" in rendered
    assert "Repair the source behavior instead of adding broken tests" in rendered
    assert "Preserve existing tests as the contract" in rendered
    assert "undefined helper names" in rendered
    assert "`src.`-prefixed imports" in rendered
    assert "tests/test_cli_uppercase.py" in rendered


def test_undefined_python_decorator_repair_reasons_preserve_framework():
    reasons = _build_repair_rejection_reasons(
        ["Plan writes Python decorators whose root name is undefined"],
        {
            "undefined_python_decorator_materializations": ["src/medium_cli/cli.py"],
        },
    )

    rendered = "\n".join(reasons)
    assert "framework_mismatch" in rendered
    assert "Preserve the framework already present" in rendered
    assert "argparse CLIs" in rendered
    assert "parser/build_parser/main flow" in rendered
    assert "@app.command" in rendered
    assert "src/medium_cli/cli.py" in rendered


def test_planning_repair_prompt_existing_tests_contract_removes_test_ops(tmp_path):
    (tmp_path / "src" / "small_cli").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "small_cli" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "small_cli" / "cli.py").write_text(
        "def build_parser():\n"
        "    return None\n"
        "\n"
        "def main(argv=None):\n"
        "    return 0\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_cli.py").write_text(
        "from small_cli.cli import build_parser, main\n"
        "\n"
        "def test_uppercase_option_prints_uppercase_message(capsys):\n"
        "    assert main(['--uppercase', 'hello']) == 0\n"
        "    assert capsys.readouterr().out.strip() == 'HELLO'\n",
        encoding="utf-8",
    )

    prompt = PlannerService.build_planning_repair_prompt(
        "Add the --uppercase option to this small Python CLI.",
        malformed_output=json.dumps(
            [
                {
                    "step_number": 2,
                    "description": "Append tests",
                    "commands": [],
                    "verification": "python -m pytest",
                    "rollback": None,
                    "expected_files": ["tests/test_cli.py"],
                    "ops": [
                        {
                            "append_file": {
                                "path": "tests/test_cli.py",
                                "content": "def test_uppercase_option():\n"
                                "    assert cli.main(['--uppercase', 'hello']) == 0\n",
                            }
                        }
                    ],
                }
            ]
        ),
        project_dir=tmp_path,
        rejection_reasons=[
            "Plan writes Python tests with obvious undefined names",
            "undefined_python_test_name_materializations: tests/test_cli.py",
        ],
    )

    assert "Existing-test contract repair:" in prompt
    assert "Remove tests/ ops from the repaired plan" in prompt
    assert "Repair source files under src/ only" in prompt
    assert "src/small_cli/cli.py" in prompt
    assert "undefined helper names" in prompt
    assert "main(['--uppercase', 'hello']) == 0" in prompt
    assert "capsys.readouterr().out.strip() == 'HELLO'" in prompt


def test_planning_repair_prompt_preserves_argparse_for_undefined_decorator(
    tmp_path,
):
    (tmp_path / "src" / "medium_cli").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "medium_cli" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "medium_cli" / "cli.py").write_text(
        "import argparse\n"
        "\n"
        "def build_parser():\n"
        "    parser = argparse.ArgumentParser()\n"
        "    return parser\n"
        "\n"
        "def main(argv=None):\n"
        "    parser = build_parser()\n"
        "    parser.parse_args(argv)\n"
        "    return 0\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_cli.py").write_text(
        "from medium_cli.cli import build_parser, main\n"
        "\n"
        "def test_summary_command_runs():\n"
        "    parser = build_parser()\n"
        "    assert parser is not None\n"
        "    assert main(['summary']) == 0\n",
        encoding="utf-8",
    )

    prompt = PlannerService.build_planning_repair_prompt(
        "Add a summary command to this Python CLI.",
        malformed_output=json.dumps(
            [
                {
                    "step_number": 2,
                    "description": "Implement summary command",
                    "commands": [],
                    "verification": "python -m pytest",
                    "rollback": None,
                    "expected_files": ["src/medium_cli/cli.py"],
                    "ops": [
                        {
                            "write_file": {
                                "path": "src/medium_cli/cli.py",
                                "content": "import typer\n\napp = typer.Typer()\n\n@app.command()\ndef summary():\n    pass\n",
                            }
                        }
                    ],
                }
            ]
        ),
        project_dir=tmp_path,
        rejection_reasons=[
            "Plan writes Python decorators whose root name is undefined",
            "framework_mismatch: offending source file(s): ['src/medium_cli/cli.py']",
        ],
    )

    assert "Python framework-aware repair:" in prompt
    assert "preserve the framework already in use" in prompt
    assert "For argparse CLIs" in prompt
    assert "do not introduce Typer/Click/FastAPI/Django decorator patterns" in prompt
    assert "`@app.command`" in prompt
    assert "parser/build_parser/main flow" in prompt
    assert "src/medium_cli/cli.py" in prompt


def test_planning_repair_prompt_allows_explicit_test_change_request(tmp_path):
    (tmp_path / "src" / "small_cli").mkdir(parents=True)
    (tmp_path / "tests").mkdir()
    (tmp_path / "src" / "small_cli" / "__init__.py").write_text("", encoding="utf-8")
    (tmp_path / "src" / "small_cli" / "cli.py").write_text(
        "def main(argv=None):\n    return 0\n",
        encoding="utf-8",
    )
    (tmp_path / "tests" / "test_cli.py").write_text(
        "from small_cli.cli import main\n"
        "\n"
        "def test_uppercase_option_prints_uppercase_message(capsys):\n"
        "    assert main(['--uppercase', 'hello']) == 0\n"
        "    assert capsys.readouterr().out.strip() == 'HELLO'\n",
        encoding="utf-8",
    )

    prompt = PlannerService.build_planning_repair_prompt(
        "Add the --uppercase option and add tests for edge cases.",
        malformed_output='[{"step_number":2,"expected_files":["tests/test_cli.py"]}]',
        project_dir=tmp_path,
        rejection_reasons=[
            "Plan writes Python tests with obvious undefined names",
            "undefined_python_test_name_materializations: tests/test_cli.py",
        ],
    )

    assert "Existing-test contract repair:" not in prompt
    assert "Remove tests/ ops from the repaired plan" not in prompt
    assert "## PYTHON TEST SOURCE CONTEXT" in prompt


def test_repeated_physical_src_import_repair_details_reports_clear_reason():
    plan_verdict = type(
        "PlanVerdict",
        (),
        {
            "details": {
                "physical_src_import_materializations": [
                    "tests/test_operations_import.py"
                ],
                "physical_src_import_details": [
                    {
                        "path": "tests/test_operations_import.py",
                        "invalid_imports": ["from src.math_tools import operations"],
                    }
                ],
            }
        },
    )()

    details = _repeated_physical_src_import_repair_details(plan_verdict)

    assert details == {
        "reason": "repeated_physical_src_import",
        "physical_src_import_materializations": ["tests/test_operations_import.py"],
        "invalid_imports": ["from src.math_tools import operations"],
    }


def test_compact_planning_repair_prompt_preserves_phase7k_contract_rules():
    prompt = PlannerService.build_compact_planning_repair_prompt(
        malformed_output='[{"step_number":1,"commands":["cat > app.py <<EOF"]}]',
        rejection_reasons=[
            "heredoc_command_shape: disallowed_heredoc_shape in steps [1]",
            "placeholder_only_implementation: implementation steps look like stubs",
        ],
    )

    assert "no nested project folder" in prompt
    assert "no duplicated path roots" in prompt
    assert "Use `ops` for file writes" in prompt
    assert "fallback limits" in prompt
    assert "write_file.content and append_file.content must be JSON strings" in prompt
    assert "newline characters must be escaped as \\n" in prompt
    assert "do not use raw triple-quoted Python blocks" in prompt
    assert "do not place bare multiline code outside JSON quotes" in prompt
    assert "output must remain a valid JSON array" in prompt
    assert "each step is a separate complete JSON object in the array" in prompt
    assert "never merge content from multiple steps into one step" in prompt
    assert "placeholder-only implementation" in prompt


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

    assert len(prompt) < 5200
    assert len(prompt) < PLANNING_REPAIR_PROMPT_MAX_CHARS
    assert "...<truncated malformed planning output>..." in prompt
    assert "project context must be stripped" not in prompt
    assert "Huge guide" not in prompt
    excerpt = prompt.split("Validation error:")[0]
    assert len(excerpt) < PLANNING_REPAIR_MAX_MALFORMED_OUTPUT_CHARS + 400


def test_profiled_planning_repair_prompt_over_budget_falls_back_to_compact(
    tmp_path, monkeypatch
):
    from app.services.orchestration.planning import repair_prompts

    malformed_output = '[{"bad": true}]'
    rejection_reasons = ["schema rejected"]
    full_prompt = PlannerService.build_planning_repair_prompt(
        "Build a page",
        malformed_output=malformed_output,
        project_dir=tmp_path,
        rejection_reasons=rejection_reasons,
    )
    profiled_full_prompt = PlannerService.apply_prompt_profile(
        full_prompt, "local_qwen_small_json_array"
    )
    compact_prompt = PlannerService.build_compact_planning_repair_prompt(
        malformed_output,
        rejection_reasons=rejection_reasons,
        prompt_profile="local_qwen_small_json_array",
    )
    prompt_cap = len(full_prompt) + 20

    assert len(full_prompt) <= prompt_cap
    assert len(profiled_full_prompt) > prompt_cap
    assert len(compact_prompt) <= prompt_cap

    monkeypatch.setattr(
        repair_prompts,
        "PLANNING_REPAIR_PROMPT_MAX_CHARS",
        prompt_cap,
    )

    prompt = PlannerService.build_planning_repair_prompt(
        "Build a page",
        malformed_output=malformed_output,
        project_dir=tmp_path,
        rejection_reasons=rejection_reasons,
        prompt_profile="local_qwen_small_json_array",
    )

    assert "Repair this invalid plan into 3 to 4 executable steps." in prompt
    assert "Output discipline for this model:" in prompt
    assert "15. Use the smallest valid plan shape" in prompt
    assert len(prompt) <= prompt_cap


def test_compact_profiled_repair_prompt_over_budget_still_fails_fast(
    tmp_path, monkeypatch
):
    from app.services.orchestration.planning import planner as planner_module
    from app.services.orchestration.planning import repair_prompts

    runtime = type(
        "Runtime",
        (),
        {
            "execute_task": lambda *args, **kwargs: (_ for _ in ()).throw(
                AssertionError("repair should be skipped before runtime call")
            )
        },
    )()
    prompt_cap = 200
    monkeypatch.setattr(
        repair_prompts,
        "PLANNING_REPAIR_PROMPT_MAX_CHARS",
        prompt_cap,
    )
    monkeypatch.setattr(
        planner_module,
        "PLANNING_REPAIR_PROMPT_MAX_CHARS",
        prompt_cap,
    )

    with pytest.raises(PlanningRepairBudgetExceeded):
        PlannerService.repair_output(
            runtime_service=runtime,
            task_description="Build a page",
            malformed_output='[{"bad": true}]',
            project_dir=tmp_path,
            timeout_seconds=300,
            logger=logging.getLogger("test"),
            emit_live=lambda *args, **kwargs: None,
            reason="json_parse_failed",
            rejection_reasons=["schema rejected"],
            prompt_profile="local_qwen_small_json_array",
        )


def test_non_profile_planning_repair_over_budget_compaction_is_unchanged(
    tmp_path, monkeypatch
):
    from app.services.orchestration.planning import repair_prompts

    malformed_output = '[{"bad": true}]'
    rejection_reasons = ["schema rejected"]
    compact_prompt = PlannerService.build_compact_planning_repair_prompt(
        malformed_output,
        rejection_reasons=rejection_reasons,
    )
    prompt_cap = len(compact_prompt) + 20

    monkeypatch.setattr(
        repair_prompts,
        "PLANNING_REPAIR_PROMPT_MAX_CHARS",
        prompt_cap,
    )

    prompt = PlannerService.build_planning_repair_prompt(
        "Build a page",
        malformed_output=malformed_output,
        project_dir=tmp_path,
        rejection_reasons=rejection_reasons,
    )

    assert "Repair this invalid plan into 3 to 4 executable steps." in prompt
    assert "Output discipline for this model:" not in prompt
    assert len(prompt) <= prompt_cap


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


def test_validator_rejects_python_c_stdin_read_without_input_pipe(tmp_path):
    command = (
        'python -c "import sys; sys.exit(0 if sys.stdin.read().strip() '
        "== 'Phase 10G Windows Smoke: Ready' else 1)\""
    )
    verdict = ValidatorService.validate_plan(
        [
            {
                "step_number": 1,
                "description": "Create smoke status script",
                "commands": [command],
                "verification": command,
                "rollback": None,
                "expected_files": ["scripts/smoke_status.py"],
                "ops": [
                    {
                        "op": "write_file",
                        "path": "scripts/smoke_status.py",
                        "content": 'print("Phase 10G Windows Smoke: Ready")\n',
                    }
                ],
            }
        ],
        output_text="[]",
        task_prompt="Create scripts/smoke_status.py",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert verdict.repairable is True
    assert "brittle_inline_python" in verdict.details["brittle_command_subcodes"]
    assert 1 in verdict.details["brittle_command_step_details"]


def test_validator_rejects_negative_existing_file_precondition_on_retry(tmp_path):
    script = tmp_path / "scripts" / "smoke_status.py"
    script.parent.mkdir()
    script.write_text('print("Phase 10G Windows Smoke: Ready")\n', encoding="utf-8")
    plan = [
        {
            "step_number": 1,
            "description": "Reproduce the bug by verifying script absence",
            "commands": ["test ! -f scripts/smoke_status.py"],
            "verification": "test ! -f scripts/smoke_status.py",
            "rollback": None,
            "expected_files": [],
        },
        {
            "step_number": 2,
            "description": "Create script",
            "commands": ["python scripts/smoke_status.py"],
            "verification": "python scripts/smoke_status.py",
            "rollback": None,
            "expected_files": ["scripts/smoke_status.py"],
            "ops": [
                {
                    "op": "write_file",
                    "path": "scripts/smoke_status.py",
                    "content": 'print("Phase 10G Windows Smoke: Ready")\n',
                }
            ],
        },
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Create scripts/smoke_status.py",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert verdict.repairable is True
    assert verdict.details["negative_existing_file_checks"] == {
        1: ["scripts/smoke_status.py"]
    }


def test_validator_allows_python_c_pathlib_content_assertions_from_ops_plan(tmp_path):
    plan = [
        {
            "step_number": 1,
            "description": "Create README",
            "commands": [
                "python -c \"import pathlib,sys; sys.exit(0 if 'Reliability Smoke 2' in pathlib.Path('README.md').read_text() and 'Ready' in pathlib.Path('README.md').read_text() else 1)\""
            ],
            "ops": [
                {
                    "op": "write_file",
                    "path": "README.md",
                    "content": "# Reliability Smoke 2\n\n## Status\n\nReady\n",
                }
            ],
            "verification": "python -c \"import pathlib,sys; sys.exit(0 if 'Reliability Smoke 2' in pathlib.Path('README.md').read_text() and 'Ready' in pathlib.Path('README.md').read_text() else 1)\"",
            "rollback": "rm -f README.md",
            "expected_files": ["README.md"],
        },
        {
            "step_number": 2,
            "description": "Verify README exists",
            "commands": ["ls -l README.md"],
            "verification": "python -c \"import pathlib,sys; sys.exit(0 if pathlib.Path('README.md').exists() else 1)\"",
            "rollback": None,
            "expected_files": ["README.md"],
        },
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Create README",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert "brittle_inline_python" not in verdict.details.get(
        "brittle_command_subcodes", []
    )
    assert "Plan contains brittle heredoc-heavy or malformed commands" not in (
        verdict.reasons
    )


def test_validator_allows_python_c_print_content_assertions_from_ops_plan(tmp_path):
    plan = [
        {
            "step_number": 1,
            "description": "Create README",
            "commands": [],
            "ops": [
                {
                    "op": "write_file",
                    "path": "README.md",
                    "content": "# Reliability Smoke 2\n\n## Status\nReady\n",
                }
            ],
            "verification": "python -c \"import pathlib; print('OK' if 'Reliability Smoke 2' in pathlib.Path('README.md').read_text() and 'Ready' in pathlib.Path('README.md').read_text() else 'FAIL')\"",
            "rollback": "rm -f README.md",
            "expected_files": ["README.md"],
        },
        {
            "step_number": 2,
            "description": "Verify README",
            "commands": [],
            "verification": "python -c \"import pathlib; print('OK' if 'Reliability Smoke 2' in pathlib.Path('README.md').read_text() and 'Ready' in pathlib.Path('README.md').read_text() else 'FAIL')\"",
            "rollback": None,
            "expected_files": ["README.md"],
        },
    ]

    verdict = ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt="Create README",
        execution_profile="full_lifecycle",
        project_dir=tmp_path,
    )

    assert "brittle_inline_python" not in verdict.details.get(
        "brittle_command_subcodes", []
    )
    assert "Plan contains brittle heredoc-heavy or malformed commands" not in (
        verdict.reasons
    )


def test_shell_safe_command_guide_rejects_python_heredoc():
    guide = (
        __import__("pathlib")
        .Path("knowledge/seed/format_guides/shell-safe-command.md")
        .read_text()
    )

    assert "do not use heredoc syntax" in guide.lower()
    assert "cat > file <<EOF" in guide
    assert "python3 - <<'PY'" not in guide


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
