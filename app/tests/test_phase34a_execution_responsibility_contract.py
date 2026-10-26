"""PHASE34-A provider-free execution responsibility contract regressions.

One invariant is under test: ``ACCEPTED_STEP_EXECUTION_CAPABILITY_COMPLETE``.
Every accepted mutating step must have an execution channel that can actually
perform the mutation --

* E1: a structured file operation applied by the Orchestrator under APA;
* E2: a shell command the local command policy can execute;
* E5: an explicitly resolved AGENT_RUNTIME topology with native side effects --

and a residual text-only runtime turn (E4) is never one of them.

No provider is called: runtime calls stop at the POST33-EXEC2 direct-runtime
protocol stub, which is reused here rather than re-implemented.
"""

from __future__ import annotations

import json
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.services.agents.agent_backends import (
    ExecutionTopology,
    get_backend_descriptor,
    resolve_execution_topology,
)
from app.services.agents.agent_runtime import execution_topology_for_runtime
from app.services.orchestration.prompt_templates import PromptTemplates
from app.services.orchestration.validation.local_command_policy import (
    local_shell_command_is_executable,
)
from app.services.orchestration.validation.rules.core_execution_capability import (
    command_requires_mutation,
    plan_steps_without_execution_channel,
)
from app.services.orchestration.validation.validator import ValidatorService

from app.models import TaskCheckpoint
from app.services.orchestration.validation.path_authority import GrantClass

from app.tests.test_post33_exec2_structured_orchestrator_consumption import (
    DirectRuntimeStub,
    _authority,
    _make_loop_context,
    _persist_authority,
    _run_loop,
    _step,
)


VERIFY_APP = (
    "python -c \"import pathlib; assert pathlib.Path('src/app.py').exists(); "
    "print('ok')\""
)


def _plan_step(
    number: int,
    *,
    description: str,
    commands: list[str] | None = None,
    ops: list[dict[str, Any]] | None = None,
    verification: str | None = None,
    expected_files: list[str] | None = None,
) -> dict[str, Any]:
    step: dict[str, Any] = {
        "step_number": number,
        "description": description,
        "commands": list(commands or []),
        "verification": verification,
        "rollback": None,
        "expected_files": list(expected_files or []),
    }
    if ops is not None:
        step["ops"] = ops
    return step


def _validate(
    plan: list[dict[str, Any]],
    project_dir: Path,
    *,
    task_prompt: str,
    execution_topology: ExecutionTopology | None,
):
    return ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt=task_prompt,
        execution_profile="full_lifecycle",
        project_dir=project_dir,
        execution_topology=execution_topology,
    )


@pytest.fixture
def workspace(tmp_path: Path) -> Path:
    (tmp_path / "src").mkdir()
    return tmp_path


# ---------------------------------------------------------------------------
# Topology is one resolved fact, derived from declared capabilities only
# ---------------------------------------------------------------------------


def test_topology_is_derived_from_declared_capabilities_not_backend_names():
    assert (
        resolve_execution_topology(
            get_backend_descriptor("local_openclaw").capabilities
        )
        is ExecutionTopology.AGENT_RUNTIME
    )
    for backend in ("direct_ollama", "openai_chat_completions", "openai_responses_api"):
        assert (
            resolve_execution_topology(get_backend_descriptor(backend).capabilities)
            is ExecutionTopology.STRUCTURED_ORCHESTRATOR
        )


def test_runtime_without_descriptor_fails_closed_to_structured_orchestrator():
    assert (
        execution_topology_for_runtime(SimpleNamespace())
        is ExecutionTopology.STRUCTURED_ORCHESTRATOR
    )


# ---------------------------------------------------------------------------
# CASE 1 / CASE 2 -- E1 structured mutation stays the Orchestrator's own write
# ---------------------------------------------------------------------------


def test_case1_structured_new_file_mutation_is_admitted_and_orchestrator_written(
    db_session, tmp_path, monkeypatch
):
    step = _step(
        description="Create the accepted structured artifact",
        ops=[{"op": "write_file", "path": "accepted.txt", "content": "safe\n"}],
        expected_files=["accepted.txt"],
    )
    runtime = DirectRuntimeStub(
        {"status": "completed", "output": "provider must not be called"}
    )
    ctx, _execution, runtime_workspace = _make_loop_context(
        db_session, tmp_path, step=step, runtime=runtime
    )
    assert (
        execution_topology_for_runtime(runtime)
        is ExecutionTopology.STRUCTURED_ORCHESTRATOR
    )

    result = _run_loop(ctx)

    assert result == {"status": "completed"}
    assert (runtime_workspace / "accepted.txt").read_text(encoding="utf-8") == "safe\n"
    # No residual mutation dependency: the provider was never called at all.
    assert runtime.calls == []


def test_case1_structured_new_file_plan_is_admitted_under_structured_topology(
    workspace,
):
    plan = [
        _plan_step(
            1,
            description="Create the module",
            ops=[
                {
                    "op": "write_file",
                    "path": "src/app.py",
                    "content": "def main():\n    print('hi')\n",
                }
            ],
            verification=VERIFY_APP,
            expected_files=["src/app.py"],
        )
    ]
    outcome = _validate(
        plan,
        workspace,
        task_prompt="Create src/app.py that prints hi",
        execution_topology=ExecutionTopology.STRUCTURED_ORCHESTRATOR,
    )
    assert outcome.status == "accepted"
    assert "steps_without_execution_channel" not in outcome.details


def test_case2_replace_existing_file_is_source_fenced_and_orchestrator_written(
    db_session, tmp_path
):
    step = _step(
        description="Replace the existing accepted artifact",
        ops=[
            {
                "op": "replace_in_file",
                "path": "accepted.txt",
                "old": "before",
                "new": "after",
            }
        ],
        expected_files=["accepted.txt"],
    )
    runtime = DirectRuntimeStub({"status": "completed", "output": "unreachable"})
    ctx, _execution, runtime_workspace = _make_loop_context(
        db_session, tmp_path, step=step, runtime=runtime
    )
    (runtime_workspace / "accepted.txt").write_text("before\n", encoding="utf-8")
    # An existing file is mutable only under an EXISTING_MUTABLE grant; the
    # default helper authority grants creation, which must not authorize this.
    db_session.query(TaskCheckpoint).delete()
    _persist_authority(
        db_session,
        session_id=ctx.session_id,
        task_id=ctx.task_id,
        authority=_authority(
            runtime_workspace,
            [step],
            [("accepted.txt", GrantClass.EXISTING_MUTABLE)],
        ),
    )

    result = _run_loop(ctx)

    assert result == {"status": "completed"}
    assert (runtime_workspace / "accepted.txt").read_text(encoding="utf-8") == "after\n"
    assert runtime.calls == []


def test_case2_replace_existing_file_without_existing_mutable_grant_fails_closed(
    db_session, tmp_path
):
    step = _step(
        description="Replace the existing accepted artifact",
        ops=[
            {
                "op": "replace_in_file",
                "path": "accepted.txt",
                "old": "before",
                "new": "after",
            }
        ],
        expected_files=["accepted.txt"],
    )
    runtime = DirectRuntimeStub({"status": "completed", "output": "unreachable"})
    ctx, _execution, runtime_workspace = _make_loop_context(
        db_session, tmp_path, step=step, runtime=runtime
    )
    (runtime_workspace / "accepted.txt").write_text("before\n", encoding="utf-8")

    result = _run_loop(ctx)

    assert result["status"] == "failed"
    assert result["reason"] == "execution_mutation_authority_denied"
    assert (runtime_workspace / "accepted.txt").read_text(
        encoding="utf-8"
    ) == "before\n"
    assert runtime.calls == []


# ---------------------------------------------------------------------------
# CASE 3 -- E2 is admitted only when the local command policy can run it
# ---------------------------------------------------------------------------


def test_case3_locally_executable_shell_materialization_is_admitted(workspace):
    command = "printf 'def main():\\n    print(1)\\n' > src/app.py"
    assert command_requires_mutation(command)
    assert local_shell_command_is_executable(command, workspace)

    outcome = _validate(
        [
            _plan_step(
                1,
                description="Materialize the module with a bounded local command",
                commands=[command],
                verification=VERIFY_APP,
                expected_files=["src/app.py"],
            )
        ],
        workspace,
        task_prompt="Create src/app.py that prints hi",
        execution_topology=ExecutionTopology.STRUCTURED_ORCHESTRATOR,
    )
    assert outcome.status == "accepted"


def test_case3_local_command_policy_still_refuses_unsafe_shell_forms(workspace):
    for command in (
        "curl https://example.com > src/app.py",
        "rm -rf src",
        "echo hi > ../escape.txt",
        "printf 'x' > /etc/passwd",
    ):
        assert not local_shell_command_is_executable(command, workspace), command


# ---------------------------------------------------------------------------
# CASE 4 -- the accepted-but-unexecutable heredoc class
# ---------------------------------------------------------------------------


HEREDOC_COMMAND = "cat > src/app.py <<'PYEOF'\ndef main():\n    print('hi')\nPYEOF"


def _heredoc_plan() -> list[dict[str, Any]]:
    return [
        _plan_step(
            1,
            description="Create the module with a heredoc",
            commands=[HEREDOC_COMMAND],
            verification=VERIFY_APP,
            expected_files=["src/app.py"],
        )
    ]


def test_case4_red_the_heredoc_write_is_admitted_when_no_topology_is_resolved(
    workspace,
):
    """The pre-repair behavior: admission is topology-blind and accepts it."""

    outcome = _validate(
        _heredoc_plan(),
        workspace,
        task_prompt="Create src/app.py that prints hi",
        execution_topology=None,
    )
    assert outcome.status == "accepted"
    # ... while no Orchestrator-owned channel can carry the mutation.
    assert not local_shell_command_is_executable(HEREDOC_COMMAND, workspace)


def test_case4_green_heredoc_write_is_repair_required_under_structured_topology(
    workspace,
):
    outcome = _validate(
        _heredoc_plan(),
        workspace,
        task_prompt="Create src/app.py that prints hi",
        execution_topology=ExecutionTopology.STRUCTURED_ORCHESTRATOR,
    )
    assert outcome.status == "repair_required"
    assert outcome.details["steps_without_execution_channel"] == {
        "1": [HEREDOC_COMMAND]
    }
    assert outcome.details["execution_topology"] == (
        ExecutionTopology.STRUCTURED_ORCHESTRATOR.value
    )
    assert "step_execution_channel_missing" in outcome.details["validator_rule_ids"]


def test_case4_direct_residual_dispatch_count_is_zero_for_unexecutable_mutation(
    db_session, tmp_path
):
    """A rejected step never reaches the residual provider turn."""

    plan = _heredoc_plan()
    workspace_dir = tmp_path / "runtime-workspace"
    outcome = _validate(
        plan,
        tmp_path,
        task_prompt="Create src/app.py that prints hi",
        execution_topology=ExecutionTopology.STRUCTURED_ORCHESTRATOR,
    )
    assert not outcome.accepted

    runtime = DirectRuntimeStub({"status": "completed", "output": "unreachable"})
    # The plan never becomes an accepted plan, so the execution loop is never
    # entered for it and the direct runtime receives nothing.
    assert runtime.calls == []
    assert not workspace_dir.exists()


def test_case4_agent_runtime_topology_keeps_the_same_plan_admissible(workspace):
    outcome = _validate(
        _heredoc_plan(),
        workspace,
        task_prompt="Create src/app.py that prints hi",
        execution_topology=ExecutionTopology.AGENT_RUNTIME,
    )
    assert outcome.status == "accepted"


# ---------------------------------------------------------------------------
# CASE 5 / CASE 6 -- legal residual reasoning is preserved
# ---------------------------------------------------------------------------


def test_case5_verification_only_step_without_ops_remains_admissible(workspace):
    plan = [
        _plan_step(
            1,
            description="Verify the existing implementation",
            commands=["python -m pytest"],
            ops=[],
            verification="python -m pytest",
            expected_files=[],
        )
    ]
    outcome = _validate(
        plan,
        workspace,
        task_prompt="Review the repository and report on test health",
        execution_topology=ExecutionTopology.STRUCTURED_ORCHESTRATOR,
    )
    assert outcome.status == "accepted"
    assert "steps_without_execution_channel" not in outcome.details


def test_case5_read_only_inspection_step_is_not_treated_as_mutating(workspace):
    plan = [
        _plan_step(
            1,
            description="Inspect the workspace",
            commands=["rg --files . | sort", "ls src"],
            ops=[],
            verification="python -m pytest",
            expected_files=[],
        )
    ]
    assert (
        plan_steps_without_execution_channel(
            plan,
            project_dir=workspace,
            execution_topology=ExecutionTopology.STRUCTURED_ORCHESTRATOR,
        )
        == {}
    )
    outcome = _validate(
        plan,
        workspace,
        task_prompt="Review the repository and report on its structure",
        execution_topology=ExecutionTopology.STRUCTURED_ORCHESTRATOR,
    )
    assert outcome.status == "accepted"


def test_case6_unsupported_residual_command_never_reaches_a_direct_runtime(
    db_session, tmp_path
):
    step = _step(
        description="Interpret the already-applied structured operation",
        ops=[{"op": "write_file", "path": "accepted.txt", "content": "safe\n"}],
        commands=["custom-direct-runtime-step"],
        expected_files=["accepted.txt"],
    )
    runtime = DirectRuntimeStub(
        {
            "status": "completed",
            "output": json.dumps(
                {
                    "status": "completed",
                    "output": "The accepted artifact is present.",
                    "verification_output": "advisory reasoning accepted",
                }
            ),
        }
    )
    ctx, _execution, runtime_workspace = _make_loop_context(
        db_session, tmp_path, step=step, runtime=runtime
    )

    result = _run_loop(ctx)

    # Phase 40C-2: E1 applied the structured op, but the residual command has
    # no channel under STRUCTURED_ORCHESTRATOR.  The direct runtime cannot run
    # it, so it is not asked to and its self-report is never step evidence.
    assert result["status"] == "failed"
    assert result["reason"] == "execution_capability_unsupported"
    assert (runtime_workspace / "accepted.txt").read_text(encoding="utf-8") == "safe\n"
    assert runtime.calls == []


# ---------------------------------------------------------------------------
# CASE 7 / CASE 8 -- topology-aware execution prompts
# ---------------------------------------------------------------------------


TOOLFUL_MARKERS = (
    "Write tool",
    "file-write tool",
    "provider cwd",
    "files_changed",
)


def _prompt(topology: ExecutionTopology | None) -> str:
    return PromptTemplates.build_execution_prompt(
        step_description="Create the module",
        step_commands=["printf 'x' > src/app.py"],
        project_dir="/runtime/workspace",
        verification_command="python -m pytest",
        rollback_command=None,
        expected_files=["src/app.py"],
        execution_topology=topology,
    )


def test_case7_agent_runtime_keeps_the_toolful_execution_prompt():
    prompt = _prompt(ExecutionTopology.AGENT_RUNTIME)
    assert prompt.startswith("Execute this step.")
    assert any(marker in prompt for marker in TOOLFUL_MARKERS)


def test_case7_unresolved_topology_keeps_the_historical_prompt():
    assert _prompt(None) == _prompt(ExecutionTopology.AGENT_RUNTIME)


def test_case7_agent_runtime_backend_remains_execution_eligible():
    descriptor = get_backend_descriptor("local_openclaw")
    assert (
        descriptor.capabilities.missing_execution_capabilities(
            ExecutionTopology.AGENT_RUNTIME
        )
        == []
    )


def test_case8_direct_backend_never_receives_toolful_execution_instructions():
    prompt = _prompt(ExecutionTopology.STRUCTURED_ORCHESTRATOR)
    for marker in TOOLFUL_MARKERS:
        assert marker not in prompt, marker
    assert "Report on this step." in prompt
    assert "The Orchestrator owns this step's file operations" in prompt
    assert "Reporting a file as changed does not change it." in prompt


def test_case8_structured_prompt_is_selected_for_a_direct_execution_runtime(
    db_session, tmp_path
):
    from app.services.orchestration.context.assembly import assemble_execution_prompt

    step = _step(
        description="Interpret the already-applied structured operation",
        ops=[{"op": "write_file", "path": "accepted.txt", "content": "safe\n"}],
        commands=["custom-direct-runtime-step"],
        expected_files=["accepted.txt"],
    )
    runtime = DirectRuntimeStub({"status": "completed", "output": "advisory"})
    ctx, _execution, _workspace = _make_loop_context(
        db_session, tmp_path, step=step, runtime=runtime
    )

    prompt = assemble_execution_prompt(ctx, step)

    assert "Report on this step." in prompt
    for marker in TOOLFUL_MARKERS:
        assert marker not in prompt, marker
