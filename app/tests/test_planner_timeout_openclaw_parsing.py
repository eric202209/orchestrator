import asyncio
import logging
import json
from unittest.mock import MagicMock

import pytest
from app.services.orchestration.planning.planner import (
    PlannerService,
    PlanningRepairBudgetExceeded,
    PLANNING_REPAIR_PROMPT_MAX_CHARS,
)

from app.services.orchestration.validation.validator import ValidatorService

from app.services.agents.openclaw_service import (
    OpenClawSessionError,
    OpenClawSessionService,
)

from app.tests.planner_timeout_test_helpers import _openclaw_parse_service


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


def test_planner_contract_violations_allow_optional_ops_key():
    violations = PlannerService.describe_planning_contract_violations(
        output_text="[]",
        parse_success=True,
        strategy_info="",
        extracted_plan=[
            {
                "step_number": 1,
                "description": "Create source file",
                "commands": [],
                "verification": "python -m py_compile app.py",
                "rollback": "rm -f app.py",
                "expected_files": ["app.py"],
                "ops": [
                    {
                        "op": "write_file",
                        "path": "app.py",
                        "content": "print('ok')\n",
                    }
                ],
            }
        ],
    )

    assert not any("extra keys: ops" in violation for violation in violations)
