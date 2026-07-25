"""Phase 30H planner-facing workspace identity and path semantics."""

from pathlib import Path

from app.services.orchestration.execution.runtime import build_runtime_executor_context
from app.services.workspace.task_sandbox_allocator import TaskSandbox
from app.services.workspace.workspace_paths import RUNTIME_METADATA_FILENAME
from app.services.orchestration.planning.planning_prompts import (
    build_minimal_planning_prompt,
)
from app.services.orchestration.planning.planner import PlannerService
from app.services.orchestration.planning.repair_prompts import (
    _build_nested_workspace_repair_guidance,
)
from app.services.orchestration.planning.workspace_identity import (
    PlannerWorkspaceIdentity,
)
from app.services.orchestration.prompt_templates import PromptTemplates
from app.services.orchestration.phases.planning_support import (
    _build_repair_rejection_reasons,
)
from app.services.orchestration.validation.rules.core_paths import (
    _plan_nests_task_workspace,
)
from app.services.orchestration.validation.validator import ValidatorService


def _identity(tmp_path: Path, *, logical_name: str = "inventory-api"):
    project_workspace = tmp_path / "inventory-api"
    runtime_workspace = tmp_path / "runtime" / "tasks" / "17" / "42"
    project_workspace.mkdir(parents=True)
    runtime_workspace.mkdir(parents=True)
    return PlannerWorkspaceIdentity.from_paths(
        project_workspace=project_workspace,
        physical_runtime_root=runtime_workspace,
        logical_project_name=logical_name,
    )


def _plan(*, commands=None, expected_files=None):
    return [
        {
            "step_number": 1,
            "description": "Implement the requested change",
            "commands": commands or [],
            "verification": "python -m pytest",
            "rollback": None,
            "expected_files": expected_files or [],
        }
    ]


def test_identity_separates_project_workspace_from_numeric_runtime_root(tmp_path):
    identity = _identity(tmp_path)

    assert identity.project_workspace_root.name == "inventory-api"
    assert identity.physical_runtime_root.name == "42"
    assert identity.physical_runtime_basename == "42"
    assert identity.logical_project_name == "inventory-api"
    assert "42" in identity.forbidden_root_aliases
    assert "inventory-api" in identity.forbidden_root_aliases


def test_minimal_prompt_uses_logical_identity_not_numeric_runtime_basename(tmp_path):
    identity = _identity(tmp_path)

    prompt = build_minimal_planning_prompt(
        "Build the inventory API",
        identity.physical_runtime_root,
        workspace_identity=identity,
    )

    assert 'Logical project name: "inventory-api"' in prompt
    assert 'workspace "42"' not in prompt
    assert "Treat the current directory as the project root" in prompt
    assert "task-execution ID" in prompt
    assert "all generated file paths must be relative" in prompt


def test_full_prompt_receives_both_identity_roles(tmp_path):
    identity = _identity(tmp_path)

    prompt = PromptTemplates.build_planning_prompt(
        "Build the inventory API",
        project_context="Existing inventory-api backend",
        project_dir=str(identity.physical_runtime_root),
        workspace_identity=identity,
    )

    assert "Project Workspace (baseline authority)" in prompt
    assert "Current Runtime Workspace (physical execution root)" in prompt
    assert 'Logical project name: "inventory-api"' in prompt
    assert 'workspace "42"' not in prompt


def test_numeric_runtime_alias_is_rejected_when_it_recreates_the_root(tmp_path):
    identity = _identity(tmp_path)
    plan = _plan(commands=["mkdir -p 42", "cd 42"], expected_files=["42/app.py"])

    assert _plan_nests_task_workspace(
        plan, identity.physical_runtime_root, workspace_identity=identity
    ) == [1]
    verdict = ValidatorService.validate_plan(
        plan,
        output_text="[]",
        task_prompt="Build the inventory API",
        execution_profile="full_lifecycle",
        project_dir=identity.physical_runtime_root,
        workspace_identity=identity,
    )

    assert (
        "nested_project_folder_command" in verdict.details["semantic_violation_codes"]
    )
    assert verdict.details["physical_runtime_basename"] == "42"
    assert verdict.details["logical_project_name"] == "inventory-api"
    assert verdict.details["offending_root_alias"] == "42"
    assert "42/app.py" in verdict.details["offending_fragments"][1]
    assert "app.py" in verdict.details["corrected_fragments"][1]


def test_redundant_new_alias_mkdir_and_cd_are_rejected(tmp_path):
    identity = _identity(tmp_path)
    plan = _plan(commands=["mkdir inventory-api", "cd inventory-api"])

    assert _plan_nests_task_workspace(
        plan, identity.physical_runtime_root, workspace_identity=identity
    ) == [1]


def test_existing_semantic_package_directory_is_allowed(tmp_path):
    identity = _identity(tmp_path)
    (identity.physical_runtime_root / "inventory-api").mkdir()
    plan = _plan(
        commands=["python -m pytest inventory-api/tests"],
        expected_files=["inventory-api/routes.py"],
    )

    assert (
        _plan_nests_task_workspace(
            plan, identity.physical_runtime_root, workspace_identity=identity
        )
        == []
    )


def test_existing_numeric_child_directory_is_allowed(tmp_path):
    identity = _identity(tmp_path)
    (identity.physical_runtime_root / "42").mkdir()
    plan = _plan(expected_files=["42/fixtures.py"])

    assert (
        _plan_nests_task_workspace(
            plan, identity.physical_runtime_root, workspace_identity=identity
        )
        == []
    )


def test_numeric_child_path_without_root_recreation_intent_is_allowed(tmp_path):
    identity = _identity(tmp_path)
    plan = _plan(expected_files=["42/fixture.py"])

    assert (
        _plan_nests_task_workspace(
            plan, identity.physical_runtime_root, workspace_identity=identity
        )
        == []
    )


def test_similar_alias_is_allowed(tmp_path):
    identity = _identity(tmp_path)
    plan = _plan(expected_files=["inventory-api-cli/routes.py"])

    assert (
        _plan_nests_task_workspace(
            plan, identity.physical_runtime_root, workspace_identity=identity
        )
        == []
    )


def test_absolute_and_traversal_guards_remain_rejected(tmp_path):
    identity = _identity(tmp_path)
    absolute_plan = _plan(expected_files=["/etc/passwd"])
    traversal_plan = _plan(commands=["cat ../secrets.txt"])

    assert ValidatorService._plan_contains_unsafe_paths(absolute_plan)
    assert ValidatorService._plan_contains_unsafe_command_paths(traversal_plan)


def test_repair_reason_and_guidance_name_alias_fragment_and_correction(tmp_path):
    identity = _identity(tmp_path)
    details = {
        "nested_workspace_steps": [1],
        "nested_workspace_name": "42",
        "nested_workspace_prefix": "42/",
        "nested_workspace_offending_fragments": {1: ["mkdir 42", "42/app.py"]},
        "nested_workspace_corrected_fragments": {1: ["remove mkdir 42", "app.py"]},
        "physical_runtime_basename": identity.physical_runtime_basename,
        "logical_project_name": identity.logical_project_name,
        "display_project_path": identity.display_project_path,
        "offending_root_alias": "42",
        "violation_kind": "duplicate_root_alias",
    }

    reasons = _build_repair_rejection_reasons([], details)
    combined = "\n".join(reasons)
    guidance = _build_nested_workspace_repair_guidance(reasons)

    assert 'offending alias "42"' in combined
    assert "42/app.py" in combined
    assert "app.py" in combined
    assert 'Logical project name: "inventory-api"' in guidance
    assert "remove `mkdir 42`" in guidance
    assert "physical runtime directory" in guidance


def test_repair_prompt_carries_identity_and_exact_correction(tmp_path):
    identity = _identity(tmp_path)
    reasons = _build_repair_rejection_reasons(
        [],
        {
            "nested_workspace_steps": [1],
            "nested_workspace_name": "42",
            "nested_workspace_prefix": "42/",
            "nested_workspace_offending_fragments": {1: ["42/app.py"]},
            "nested_workspace_corrected_fragments": {1: ["app.py"]},
            "physical_runtime_basename": "42",
            "logical_project_name": "inventory-api",
            "display_project_path": identity.display_project_path,
            "offending_root_alias": "42",
            "violation_kind": "duplicate_root_alias",
        },
    )

    prompt = PlannerService.build_planning_repair_prompt(
        "Build the inventory API",
        malformed_output='[{"step_number":1,"expected_files":["42/app.py"]}]',
        project_dir=identity.physical_runtime_root,
        rejection_reasons=reasons,
        workspace_identity=identity,
    )

    assert 'Logical project name: "inventory-api"' in prompt
    assert 'physical runtime directory "42"' in prompt
    assert "42/app.py" in prompt
    assert "app.py" in prompt
    assert "remove `mkdir 42`" in prompt


def test_execution_context_still_uses_physical_runtime_root(tmp_path):
    project_workspace = tmp_path / "inventory-api"
    runtime_workspace = tmp_path / "runtime" / "tasks" / "17" / "42"
    project_workspace.mkdir(parents=True)
    runtime_workspace.mkdir(parents=True)
    (runtime_workspace / RUNTIME_METADATA_FILENAME).write_text(
        '{"base_commit": null}\n', encoding="utf-8"
    )
    sandbox = TaskSandbox(
        path=runtime_workspace,
        project_id=9,
        task_execution_id=42,
        executor="local_openclaw",
        is_git=False,
    )
    context = build_runtime_executor_context(
        sandbox=sandbox,
        project_workspace=project_workspace,
        executor="local_openclaw",
        project_id=9,
        task_execution_id=42,
    )

    assert context.project_workspace == project_workspace
    assert context.runtime_workspace == runtime_workspace


def test_repair_outcome_keeps_identity_and_fragment_evidence(tmp_path):
    from app.services.orchestration.phases.planning_support import (
        _PlanningRetryState,
        _emit_repair_outcome_if_pending,
        _record_pending_repair_outcome,
    )

    identity = _identity(tmp_path)
    retry_state = _PlanningRetryState()
    _record_pending_repair_outcome(
        retry_state,
        attempt_number=1,
        original_violation_codes=["nested_project_folder_command"],
        targeted_violation_code="nested_project_folder_command",
        validator_details={
            "physical_runtime_basename": identity.physical_runtime_basename,
            "logical_project_name": identity.logical_project_name,
            "display_project_path": identity.display_project_path,
            "offending_root_alias": "42",
            "offending_fragments": {1: ["42/app.py"]},
            "corrected_fragments": {1: ["app.py"]},
            "violation_kind": "duplicate_root_alias",
        },
    )
    events = []
    ctx = type(
        "Ctx",
        (),
        {
            "session_id": 1,
            "task_id": 2,
            "emit_live": staticmethod(
                lambda level, message, metadata=None: events.append(metadata)
            ),
        },
    )()

    _emit_repair_outcome_if_pending(ctx, retry_state, type("V", (), {"details": {}})())

    metadata = events[0]
    assert metadata["physical_runtime_basename"] == "42"
    assert metadata["logical_project_name"] == "inventory-api"
    assert metadata["offending_root_alias"] == "42"
    assert metadata["offending_fragments"] == {1: ["42/app.py"]}
    assert metadata["repair_guidance_identity"]["corrected_fragments"] == {
        1: ["app.py"]
    }
