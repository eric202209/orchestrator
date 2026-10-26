"""Phase 40C-2 Correction A: command capability admission.

Under ``STRUCTURED_ORCHESTRATOR`` the execution runtime has no command tools.
A step command no Orchestrator executor admits must be rejected at plan
admission (repairable) and, if it slips through, refused at execution without
asking the text-only runtime to impersonate the command.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest

from app.services.agents.agent_backends import ExecutionTopology
from app.services.orchestration.phases.execution_local_steps import (
    _execute_read_only_inspection_step,
    local_step_commands_are_executable,
)
from app.services.orchestration.phases.planning_support import (
    _build_repair_rejection_reasons,
)
from app.services.orchestration.validation.rules.core_execution_capability import (
    plan_steps_with_unsupported_commands,
)
from app.services.orchestration.validation.validator import ValidatorService
from app.tests.test_post33_exec2_structured_orchestrator_consumption import (
    DirectRuntimeStub,
    _make_loop_context,
    _run_loop,
    _step,
)

INSPECTION = (
    'python -c "from order_service.checkout import checkout_total; '
    'print(checkout_total(None, 800))"'
)


def _plan_step(number: int, **fields: Any) -> dict[str, Any]:
    step = {
        "step_number": number,
        "description": fields.pop("description", f"Step {number}"),
        "commands": fields.pop("commands", []),
        "verification": fields.pop("verification", None),
        "rollback": None,
        "expected_files": fields.pop("expected_files", []),
        "ops": fields.pop("ops", []),
    }
    step.update(fields)
    return step


def _validate(plan, project_dir, topology):
    return ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Fix checkout_total in order_service/checkout.py",
        execution_profile="full_lifecycle",
        project_dir=project_dir,
        execution_topology=topology,
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    package = tmp_path / "order_service"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "checkout.py").write_text(
        "def checkout_total(order, tax):\n    return 0\n", encoding="utf-8"
    )
    (tmp_path / "tests").mkdir()
    (tmp_path / "tests" / "test_checkout.py").write_text(
        "def test_x():\n    assert True\n", encoding="utf-8"
    )
    return tmp_path


def _workflow_a_inspection_plan() -> list[dict[str, Any]]:
    return [
        _plan_step(
            1,
            description="Analyze checkout_total",
            commands=[INSPECTION],
            verification=INSPECTION,
        )
    ]


def test_project_importing_inline_python_is_not_locally_executable(workspace):
    assert not local_step_commands_are_executable(
        project_dir=workspace, commands=[INSPECTION], verification_command=INSPECTION
    )


@pytest.mark.parametrize(
    ("commands", "verification"),
    [
        (["python -m pytest -q"], "python -m pytest -q"),
        (["pytest tests/test_checkout.py"], None),
        (["cat order_service/checkout.py"], None),
        (["grep -n checkout_total order_service/checkout.py"], None),
        (["ls"], None),
        (["python -m py_compile order_service/checkout.py"], None),
        (
            ["python -c \"print(open('order_service/checkout.py').read())\""],
            None,
        ),
    ],
)
def test_existing_safe_commands_remain_locally_executable(
    workspace, commands, verification
):
    assert local_step_commands_are_executable(
        project_dir=workspace, commands=commands, verification_command=verification
    )
    plan = [_plan_step(1, commands=commands, verification=verification)]
    assert (
        plan_steps_with_unsupported_commands(
            plan,
            project_dir=workspace,
            execution_topology=ExecutionTopology.STRUCTURED_ORCHESTRATOR,
        )
        == {}
    )


def test_unsupported_command_is_repair_required_under_structured_topology(
    workspace,
):
    outcome = _validate(
        _workflow_a_inspection_plan(),
        workspace,
        ExecutionTopology.STRUCTURED_ORCHESTRATOR,
    )

    assert outcome.status == "repair_required"
    assert outcome.details["steps_with_unsupported_commands"] == {"1": [INSPECTION]}
    assert (
        "step_command_capability_unsupported" in outcome.details["validator_rule_ids"]
    )
    reason = next(r for r in outcome.reasons if "unsupported_step_command" in r)
    # The short validation reason survives the existing 180-character repair
    # bound; supported alternatives are supplied by the resolved capability
    # contract added to the repair prompt.
    visible = reason[:180]
    assert "unsupported_step_command" in visible
    assert "structured runtime has no command tools" in visible
    assert "Plan ops for edits" in visible
    assert "keep goal, scope, checks" in visible
    assert "from order_service" in reason


def test_capability_feedback_reaches_planning_repair(workspace):
    outcome = _validate(
        _workflow_a_inspection_plan(),
        workspace,
        ExecutionTopology.STRUCTURED_ORCHESTRATOR,
    )
    repair_reasons = _build_repair_rejection_reasons(outcome.reasons, outcome.details)
    assert any("unsupported_step_command" in reason for reason in repair_reasons)


@pytest.mark.parametrize("topology", [ExecutionTopology.AGENT_RUNTIME, None])
def test_tool_capable_or_unresolved_topology_is_unchanged(workspace, topology):
    outcome = _validate(_workflow_a_inspection_plan(), workspace, topology)
    assert "steps_with_unsupported_commands" not in outcome.details
    assert not any("unsupported_step_command" in reason for reason in outcome.reasons)


def test_mixed_step_with_executable_mkdir_still_reports_unsupported_command(
    workspace,
):
    plan = [_plan_step(1, commands=["mkdir build", INSPECTION])]
    assert plan_steps_with_unsupported_commands(
        plan,
        project_dir=workspace,
        execution_topology=ExecutionTopology.STRUCTURED_ORCHESTRATOR,
    ) == {1: ["mkdir build", INSPECTION]}


def test_unexecutable_mutation_is_reported_once_by_the_mutation_rule(workspace):
    heredoc = "cat > order_service/new.py <<'EOF'\nx = 1\nEOF"
    plan = [_plan_step(1, commands=[heredoc])]
    outcome = _validate(plan, workspace, ExecutionTopology.STRUCTURED_ORCHESTRATOR)
    assert "steps_without_execution_channel" in outcome.details
    assert "steps_with_unsupported_commands" not in outcome.details


def test_repaired_plan_is_admitted_and_runs_through_the_loop(
    db_session, tmp_path, workspace
):
    repaired = [
        _plan_step(
            1,
            description="Read the checkout module",
            commands=["cat order_service/checkout.py"],
        )
    ]
    outcome = _validate(repaired, workspace, ExecutionTopology.STRUCTURED_ORCHESTRATOR)
    assert "steps_with_unsupported_commands" not in outcome.details
    assert not any("unsupported_step_command" in reason for reason in outcome.reasons)

    runtime = DirectRuntimeStub({"status": "failed", "output": "unused"})
    ctx, _, _ = _make_loop_context(
        db_session,
        tmp_path / "loop",
        step=_step(description="Read the workspace", commands=["ls"], verification=""),
        runtime=runtime,
    )
    assert _run_loop(ctx) == {"status": "completed"}
    assert runtime.calls == []


def test_capability_rejection_is_terminal_without_retry_or_runtime_call(
    db_session, tmp_path
):
    runtime = DirectRuntimeStub({"status": "completed", "output": "{}"})
    ctx, execution, _ = _make_loop_context(
        db_session,
        tmp_path,
        step=_step(
            description="Analyze", commands=[INSPECTION], verification=INSPECTION
        ),
        runtime=runtime,
    )

    result = _run_loop(ctx)

    assert result["reason"] == "execution_capability_unsupported"
    # No runtime call at all: neither a step dispatch nor a debug repair turn.
    assert runtime.calls == []
    # Returned, not raised: the Celery task completes with a failed result, so
    # no Celery retry is consumed.  One failure record, one attempt.
    assert len(ctx.orchestration_state.debug_attempts) == 1
    assert ctx.orchestration_state.status.value == "aborted"
    db_session.refresh(execution)
    assert execution.failure_category == "validation_failure"


def test_read_only_search_no_match_is_a_completed_inspection(workspace):
    (workspace / "README.md").write_text("Project overview\n", encoding="utf-8")

    result = _execute_read_only_inspection_step(
        project_dir=workspace,
        commands=["rg TODO ./README.md"],
    )

    assert result is not None
    assert result["status"] == "completed"
    assert "no matches" in result["output"].lower()


def test_read_only_search_syntax_error_remains_a_failure(workspace):
    (workspace / "README.md").write_text("Project overview\n", encoding="utf-8")

    result = _execute_read_only_inspection_step(
        project_dir=workspace,
        commands=["rg '[' ./README.md"],
    )

    assert result is not None
    assert result["status"] == "failed"


def test_debug_repair_cannot_introduce_a_command_outside_the_accepted_step():
    from app.services.orchestration.phases.execution_debug_repair import (
        bounded_debug_repair_command_authority_rejection,
    )

    reason = bounded_debug_repair_command_authority_rejection(
        {
            "fix_type": "code_fix",
            "verification": "test -f ./README.md && rg TODO ./README.md",
        },
        {
            "commands": ["rg TODO ./README.md"],
            "verification": "python -m pytest -q",
        },
    )

    assert reason is not None
    assert "debug_repair_command_not_authorized" in reason
    assert "test -f ./README.md && rg TODO ./README.md" in reason


def test_debug_repair_may_reuse_an_accepted_step_command():
    from app.services.orchestration.phases.execution_debug_repair import (
        bounded_debug_repair_command_authority_rejection,
    )

    assert (
        bounded_debug_repair_command_authority_rejection(
            {"fix_type": "command_fix", "fix": "python -m pytest -q"},
            {
                "commands": ["rg TODO ./README.md"],
                "verification": "python -m pytest -q",
            },
        )
        is None
    )


def test_debug_repair_may_use_a_new_command_admitted_by_local_executor(workspace):
    from app.services.orchestration.phases.execution_debug_repair import (
        bounded_debug_repair_command_authority_rejection,
    )
    from app.services.orchestration.phases.execution_local_steps import (
        local_step_commands_are_executable,
    )

    (workspace / "src").mkdir()
    (workspace / "src" / "module.py").write_text("value = 1\n", encoding="utf-8")
    command = "python3 -m py_compile src/module.py"

    assert (
        bounded_debug_repair_command_authority_rejection(
            {"fix_type": "code_fix", "verification": command},
            {"commands": ["ls"], "verification": "ls"},
            command_is_admitted=lambda proposed: local_step_commands_are_executable(
                project_dir=workspace,
                commands=[proposed],
                verification_command=proposed,
            ),
        )
        is None
    )


def test_execution_loop_rejects_debug_replacement_outside_accepted_step(
    db_session, tmp_path
):
    accepted_command = "python -m pytest -q tests/test_failure.py"
    runtime = DirectRuntimeStub(
        {
            "status": "completed",
            "output": (
                '[{"title":"Rerun the suite",'
                '"command":"test -f ./README.md && rg TODO ./README.md",'
                '"verification_command":"python -m pytest -q tests/test_failure.py"}]'
            ),
        }
    )
    ctx, execution, workspace = _make_loop_context(
        db_session,
        tmp_path,
        step=_step(
            description="Run the accepted failing verification",
            commands=[accepted_command],
            verification=accepted_command,
        ),
        runtime=runtime,
    )
    tests = workspace / "tests"
    tests.mkdir()
    (tests / "test_failure.py").write_text(
        "def test_fails():\n    assert False\n", encoding="utf-8"
    )

    _run_loop(ctx)

    assert "debug_repair_command_not_authorized" in ctx.orchestration_state.abort_reason
    assert ctx.orchestration_state.plan[0]["commands"] == [accepted_command]
    assert len(runtime.calls) == 1  # DEBUG request only; its replacement never ran.
    db_session.refresh(execution)
    assert "fail" in str(execution.status).lower()
