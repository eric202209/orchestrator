"""Phase 33C-4 regression probes and execution mutation-gate tests.

The first three tests are deliberately written at the production seams that
exposed the 33C-3 limitations: accepted-plan authority construction and the
post-verdict Task-1 normalization.  They were run against the pre-33C-4 code
before the implementation was changed.
"""

from __future__ import annotations

import json
import hashlib
import logging
import os
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from app.models import (
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskCheckpoint,
    TaskExecution,
    TaskStatus,
)
from app.services.orchestration.planning.source_materialization import (
    PlannerSourceMaterialization,
    MaterializedSourceFile,
    SOURCE_STATUS_EXISTING,
    SOURCE_STATUS_NEW,
    current_source_version_identity,
)
from app.services.orchestration.phases.planning_task1_bootstrap import (
    normalize_task1_python_src_layout_verification,
    reconcile_task1_bootstrap_plan,
)
from app.services.orchestration.validation.accepted_path_authority import (
    AcceptedPathAuthority,
    accepted_plan_identity,
)
from app.services.orchestration.state.persistence import (
    load_accepted_path_authority_for_execution,
)
from app.services.orchestration.execution.executor import ExecutorService
from app.services.orchestration.error_handler import error_handler
from app.services.orchestration.phases.execution_loop import execute_step_loop
from app.services.orchestration.prompt_templates import OrchestrationState
from app.services.orchestration.types import OrchestrationRunContext
from app.services.orchestration.validation.workspace_guard import (
    compute_workspace_checksum,
    detect_scope_violations,
)
from app.services.orchestration.validation.path_authority import (
    GrantClass,
    GrantProvenance,
    PathAuthorityError,
    PathGrant,
    declare,
)
from app.services.orchestration.validation.validator import ValidatorService


def _validate(
    plan: list[dict[str, Any]],
    *,
    project_dir: Path,
    task_prompt: str,
    source_materialization: PlannerSourceMaterialization,
):
    return ValidatorService.validate_plan(
        plan,
        output_text=json.dumps(plan),
        task_prompt=task_prompt,
        execution_profile="implementation",
        project_dir=project_dir,
        title="Phase 33C-4",
        source_materialization=source_materialization,
    )


def _write_plan(path: str, content: str) -> list[dict[str, Any]]:
    return [
        {
            "step_number": 1,
            "description": f"Write {path}",
            "commands": ["python3 -m pytest -q"],
            "verification": "python3 -m pytest -q",
            "rollback": None,
            "expected_files": [path],
            "ops": [{"op": "write_file", "path": path, "content": content}],
        }
    ]


def _source_record(
    path: str,
    *,
    workspace: Path,
    status: str,
    content: str | None,
    creation_authorized: bool = False,
) -> MaterializedSourceFile:
    digest = hashlib.sha256((content or "").encode("utf-8")).hexdigest()
    return MaterializedSourceFile(
        relative_path=path,
        workspace_identity=str(workspace),
        content=content,
        content_hash=None if status == SOURCE_STATUS_NEW else digest,
        version_identity=(
            current_source_version_identity(workspace / path) or "test:source"
        ),
        status=status,
        truncated=False,
        source_length=len(content or ""),
        source_length_chars=len(content or ""),
        included_prompt_length=len(content or ""),
        expected=True,
        creation_authorized=creation_authorized,
    )


def _authority(
    workspace: Path,
    plan: Any,
    *,
    path: str | None = None,
    grant_class: GrantClass | None = None,
) -> AcceptedPathAuthority:
    grants = ()
    if path is not None and grant_class is not None:
        baseline = None
        if grant_class is not GrantClass.CREATION_AUTHORIZED:
            baseline = "1" * 64
        grants = (
            PathGrant(
                path=declare(path),
                grant_class=grant_class,
                provenance=GrantProvenance.ACCEPTED_PLAN,
                baseline_content_hash=baseline,
            ),
        )
    return AcceptedPathAuthority.create(
        accepted_plan_identity=accepted_plan_identity(plan),
        workspace_identity=str(workspace.resolve()),
        maximum_scope_digest="2" * 64,
        grants=grants,
    )


def _seed_execution(db, workspace: Path) -> tuple[int, int, int]:
    project = Project(name="Phase 33C-4", workspace_path=str(workspace))
    db.add(project)
    db.flush()
    session = SessionModel(
        project_id=project.id,
        name="Phase 33C-4 session",
        status="running",
        is_active=True,
        execution_mode="manual",
    )
    db.add(session)
    db.flush()
    task = Task(
        project_id=project.id,
        title="Phase 33C-4 task",
        description="Execution mutation gate test",
        status=TaskStatus.RUNNING.value,
    )
    db.add(task)
    db.flush()
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.PENDING,
    )
    db.add(execution)
    db.flush()
    return int(session.id), int(task.id), int(execution.id)


def _persist_authority(
    db,
    *,
    session_id: int,
    task_id: int,
    authority: AcceptedPathAuthority,
    status: str = "accepted",
) -> None:
    db.add(
        TaskCheckpoint(
            session_id=session_id,
            task_id=task_id,
            checkpoint_type="validation_plan",
            description=f"plan:{status}",
            state_snapshot=json.dumps(
                {
                    "stage": "plan",
                    "status": status,
                    "details": {"accepted_path_authority": authority.to_dict()},
                }
            ),
        )
    )
    db.commit()


class _ObservedScopeRuntime:
    def __init__(self, project_dir: Path):
        self.project_dir = project_dir
        self.prompts: list[str] = []

    async def execute_task(self, prompt, timeout_seconds=None, **kwargs):
        self.prompts.append(str(prompt))
        (self.project_dir / "app").mkdir(parents=True, exist_ok=True)
        (self.project_dir / "app" / "unauthorized.py").write_text(
            "outside = True\n", encoding="utf-8"
        )
        return {
            "status": "success",
            "output": "runtime wrote an unauthorized path",
            "files_changed": ["app/unauthorized.py"],
        }

    def reports_context_overflow(self, result):
        return False

    def get_backend_metadata(self):
        return {"backend": "provider-free-test", "model_family": "test"}


def _make_scope_loop_context(db, tmp_path):
    project_dir = tmp_path / "loop-workspace"
    project_dir.mkdir()
    project = Project(name="Phase 33C-4 loop", workspace_path=str(project_dir))
    db.add(project)
    db.flush()
    session = SessionModel(
        project_id=project.id,
        name="Phase 33C-4 loop session",
        status="running",
        is_active=True,
        execution_mode="manual",
    )
    task = Task(
        project_id=project.id,
        title="scope gate",
        description="scope gate",
        status=TaskStatus.RUNNING.value,
        task_subfolder="scope-gate",
    )
    db.add_all([session, task])
    db.flush()
    link = SessionTask(
        session_id=session.id,
        task_id=task.id,
        status=TaskStatus.RUNNING,
    )
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
    )
    db.add_all([link, execution])
    db.flush()
    step = {
        "step_number": 1,
        "description": "Run the execution runtime",
        "commands": ["custom-test-command"],
        "verification": "",
        "rollback": None,
        "expected_files": [],
        "ops": [],
    }
    state = OrchestrationState(
        session_id=str(session.id),
        task_description="Run the execution runtime",
        project_name=project.name,
        project_context="",
        task_id=task.id,
        plan=[step],
        reasoning_artifact={
            "intent": "run execution",
            "workspace_facts": [f"project_dir={project_dir}"],
            "planned_actions": ["run runtime"],
            "verification_plan": ["observe runtime output"],
        },
    )
    state._project_dir_override = str(project_dir)
    authority = _authority(project_dir, state.plan)
    _persist_authority(
        db,
        session_id=session.id,
        task_id=task.id,
        authority=authority,
    )
    return OrchestrationRunContext(
        db=db,
        session=session,
        project=project,
        task=task,
        session_task_link=link,
        session_id=session.id,
        task_id=task.id,
        prompt="Run the execution runtime",
        timeout_seconds=30,
        execution_profile="test_only",
        validation_profile="verification",
        runs_in_canonical_baseline=False,
        orchestration_state=state,
        runtime_service=_ObservedScopeRuntime(project_dir),
        task_service=SimpleNamespace(),
        logger=logging.getLogger("phase33c4-scope-test"),
        emit_live=lambda *args, **kwargs: None,
        error_handler=error_handler,
        task_execution_id=execution.id,
        restore_workspace_snapshot_if_needed=lambda reason: None,
    )


def test_reproduce_under_grant_a_expected_new_write_needs_creation_grant(tmp_path):
    plan = _write_plan("app/generated.py", "VALUE = 1\n")
    materialization = PlannerSourceMaterialization(
        workspace_identity=str(tmp_path),
        files=(),
    )

    outcome = _validate(
        plan,
        project_dir=tmp_path,
        task_prompt="Create app/generated.py and verify it.",
        source_materialization=materialization,
    )

    assert outcome.accepted, outcome.reasons
    grants = outcome.details["accepted_path_authority"]["grants"]
    assert {grant["path"] for grant in grants} == {"app/generated.py"}
    assert grants[0]["grant_class"] == GrantClass.CREATION_AUTHORIZED.value


def test_reproduce_under_grant_b_existing_authorized_write_needs_mutable_grant(
    tmp_path,
):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "existing.py").write_text("VALUE = 1\n", encoding="utf-8")
    plan = _write_plan("app/existing.py", "VALUE = 2\n")
    plan[0]["description"] = "Rewrite the entire existing app/existing.py file"

    outcome = _validate(
        plan,
        project_dir=tmp_path,
        task_prompt="Rewrite the existing app/existing.py and verify it.",
        source_materialization=PlannerSourceMaterialization(
            workspace_identity=str(tmp_path),
            files=(),
        ),
    )

    assert not outcome.accepted
    assert "accepted_path_authority" not in outcome.details
    assert (
        outcome.details["accepted_path_authority_error"]["code"]
        == "existing_mutation_source_evidence_missing"
    )


def test_incomplete_existing_source_evidence_cannot_leave_write_under_granted(
    tmp_path,
):
    (tmp_path / "app").mkdir()
    (tmp_path / "app" / "existing.py").write_text("VALUE = 1\n", encoding="utf-8")
    plan = _write_plan("app/existing.py", "VALUE = 2\n")
    plan[0]["description"] = "Rewrite the entire existing app/existing.py file"
    incomplete_record = replace(
        _source_record(
            "app/existing.py",
            workspace=tmp_path,
            status=SOURCE_STATUS_EXISTING,
            content="VALUE = 1\n",
        ),
        content_hash=None,
    )

    outcome = _validate(
        plan,
        project_dir=tmp_path,
        task_prompt="Rewrite the existing app/existing.py and verify it.",
        source_materialization=PlannerSourceMaterialization(
            workspace_identity=str(tmp_path), files=(incomplete_record,)
        ),
    )

    assert not outcome.accepted
    assert outcome.details["accepted_path_authority_error"]["code"] == (
        "existing_mutation_source_evidence_missing"
    )


def test_delete_file_is_rejected_before_execution_without_deterministic_authority(
    tmp_path,
):
    target = tmp_path / "app" / "delete-me.py"
    target.parent.mkdir()
    target.write_text("x\n", encoding="utf-8")
    plan = [
        {
            "step_number": 1,
            "description": "Delete app/delete-me.py",
            "commands": ["python3 -m pytest -q"],
            "verification": "python3 -m pytest -q",
            "rollback": None,
            "expected_files": ["app/delete-me.py"],
            "ops": [{"op": "delete_file", "path": "app/delete-me.py"}],
        }
    ]
    outcome = _validate(
        plan,
        project_dir=tmp_path,
        task_prompt="Delete app/delete-me.py and verify.",
        source_materialization=PlannerSourceMaterialization(
            workspace_identity=str(tmp_path),
            files=(
                _source_record(
                    "app/delete-me.py",
                    workspace=tmp_path,
                    status=SOURCE_STATUS_EXISTING,
                    content="x\n",
                ),
            ),
        ),
    )
    assert not outcome.accepted
    assert outcome.details["accepted_path_authority_error"]["code"] == (
        "deletion_authorization_unavailable"
    )


def test_reproduce_post_verdict_task1_normalization_changes_accepted_identity():
    plan = [
        {
            "step_number": 1,
            "description": "Run the package tests",
            "commands": ["pytest -q"],
            "verification": "pytest -q",
            "rollback": None,
            "expected_files": ["src/app.py"],
            "ops": [
                {"op": "write_file", "path": "src/app.py", "content": "VALUE = 1\n"}
            ],
        }
    ]
    accepted_verdict = SimpleNamespace(
        details={
            "task1_bootstrap_contract": {
                "python_package_markers": ["pyproject.toml"],
                "python_import_targets": ["app"],
            }
        }
    )

    normalized = normalize_task1_python_src_layout_verification(plan, accepted_verdict)

    assert normalized != plan
    assert accepted_plan_identity(normalized) != accepted_plan_identity(plan)


def test_task1_normalization_revalidates_the_plan_identity_before_checkpoint(
    tmp_path,
):
    plan = [
        {
            "step_number": 1,
            "description": "Run the package tests",
            "commands": ["pytest -q"],
            "verification": "pytest -q",
            "rollback": None,
            "expected_files": ["src/app.py"],
            "ops": [
                {"op": "write_file", "path": "src/app.py", "content": "VALUE = 1\n"}
            ],
        }
    ]
    state = OrchestrationState(
        session_id="1",
        task_description="Bootstrap the package",
        project_name="Phase 33C-4",
        task_id=1,
        plan=plan,
    )
    state._project_dir_override = str(tmp_path)
    ctx = SimpleNamespace(
        orchestration_state=state,
        emit_live=lambda *args, **kwargs: None,
        prompt="Bootstrap the package",
        execution_profile="implementation",
        validation_severity="standard",
        workflow_profile="default",
        workflow_stage="planning",
        planner_source_materialization=PlannerSourceMaterialization(
            workspace_identity=str(tmp_path),
            files=(
                _source_record(
                    "src/app.py",
                    workspace=tmp_path,
                    status=SOURCE_STATUS_NEW,
                    content=None,
                    creation_authorized=True,
                ),
            ),
        ),
        task=SimpleNamespace(title="Bootstrap", description="Bootstrap the package"),
    )
    accepted_verdict = SimpleNamespace(
        details={
            "task1_bootstrap_contract": {
                "python_package_markers": ["pyproject.toml"],
                "python_import_targets": ["app"],
            }
        }
    )

    revalidated = reconcile_task1_bootstrap_plan(
        ctx,
        normalize=lambda current_plan: normalize_task1_python_src_layout_verification(
            current_plan, accepted_verdict
        ),
        reason="test_task1_normalization",
        message="test task1 normalization",
    )

    assert revalidated is not None
    assert accepted_plan_identity(ctx.orchestration_state.plan) == (
        revalidated.verdict.details["accepted_path_authority"]["accepted_plan_identity"]
    )


def test_structured_existing_mutation_matrix_uses_exact_grant_classes(tmp_path):
    target = tmp_path / "app" / "config.py"
    target.parent.mkdir()
    target.write_text("VALUE = 1\n", encoding="utf-8")
    for operation in (
        {"op": "write_file", "path": "app/config.py", "content": "VALUE = 2\n"},
        {"op": "append_file", "path": "app/config.py", "content": "# done\n"},
        {
            "op": "replace_in_file",
            "path": "app/config.py",
            "old": "# done",
            "new": "# complete",
        },
    ):
        authority = _authority(
            tmp_path,
            [{"step_number": 1, "ops": [operation]}],
            path="app/config.py",
            grant_class=GrantClass.EXISTING_MUTABLE,
        )
        result = ExecutorService.execute_file_ops(
            tmp_path, [operation], accepted_path_authority=authority
        )
        assert result["success"], result


def test_existing_readonly_is_denied_before_write(tmp_path):
    target = tmp_path / "app" / "readonly.py"
    target.parent.mkdir()
    target.write_text("VALUE = 1\n", encoding="utf-8")
    operation = {"op": "write_file", "path": "app/readonly.py", "content": "x\n"}
    authority = _authority(
        tmp_path,
        [{"step_number": 1, "ops": [operation]}],
        path="app/readonly.py",
        grant_class=GrantClass.EXISTING_READONLY,
    )

    result = ExecutorService.execute_file_ops(
        tmp_path, [operation], accepted_path_authority=authority
    )
    assert not result["success"]
    assert result["authority_error"]["code"] == "grant_class_mismatch"
    assert target.read_text(encoding="utf-8") == "VALUE = 1\n"


def test_creation_grant_materializes_only_its_parent_chain(tmp_path):
    plan = _write_plan("app/generated/report.py", "x = 1\n")
    operation = plan[0]["ops"][0]
    authority = _authority(
        tmp_path,
        plan,
        path="app/generated/report.py",
        grant_class=GrantClass.CREATION_AUTHORIZED,
    )
    result = ExecutorService.execute_file_ops(
        tmp_path, [operation], accepted_path_authority=authority
    )
    assert result["success"]
    assert (tmp_path / "app" / "generated" / "report.py").exists()
    sibling = {"op": "write_file", "path": "app/generated/sibling.py", "content": "x\n"}
    denied = ExecutorService.execute_file_ops(
        tmp_path, [sibling], accepted_path_authority=authority
    )
    assert not denied["success"]
    assert denied["authority_error"]["code"] == "authority_missing"
    assert not (tmp_path / "app" / "generated" / "sibling.py").exists()


def test_creation_grant_cannot_overwrite_unexpected_collision(tmp_path):
    target = tmp_path / "app" / "new.py"
    target.parent.mkdir()
    target.write_text("original\n", encoding="utf-8")
    operation = {"op": "write_file", "path": "app/new.py", "content": "changed\n"}
    authority = _authority(
        tmp_path,
        [{"step_number": 1, "ops": [operation]}],
        path="app/new.py",
        grant_class=GrantClass.CREATION_AUTHORIZED,
    )
    result = ExecutorService.execute_file_ops(
        tmp_path, [operation], accepted_path_authority=authority
    )
    assert not result["success"]
    assert result["authority_error"]["code"] == "grant_class_mismatch"
    assert target.read_text(encoding="utf-8") == "original\n"


def test_delete_without_deletion_grant_is_denied_before_unlink(tmp_path):
    target = tmp_path / "app" / "delete-me.py"
    target.parent.mkdir()
    target.write_text("keep\n", encoding="utf-8")
    operation = {"op": "delete_file", "path": "app/delete-me.py"}
    authority = _authority(
        tmp_path,
        [{"step_number": 1, "ops": [operation]}],
    )

    result = ExecutorService.execute_file_ops(
        tmp_path, [operation], accepted_path_authority=authority
    )

    assert not result["success"]
    assert result["authority_error"]["code"] == "authority_missing"
    assert target.read_text(encoding="utf-8") == "keep\n"


@pytest.mark.parametrize(
    "operation",
    [
        {"op": "write_file", "path": "app/missing/file.py", "content": "x\n"},
        {"op": "append_file", "path": "app/missing/file.py", "content": "x\n"},
        {"op": "delete_file", "path": "app/missing/file.py"},
    ],
)
def test_missing_or_incompatible_grant_denies_before_any_parent_or_file_mutation(
    tmp_path, operation
):
    before = compute_workspace_checksum(tmp_path)
    authority = _authority(
        tmp_path,
        [{"step_number": 1, "ops": [operation]}],
    )
    result = ExecutorService.execute_file_ops(
        tmp_path, [operation], accepted_path_authority=authority
    )
    assert not result["success"]
    assert result["failure_category"] == "validation_failure"
    assert compute_workspace_checksum(tmp_path) == before


@pytest.mark.parametrize(
    "path",
    [
        "app/b.py",
        "app/real.py",
        "../outside.py",
        "/absolute.py",
        "C:/drive.py",
        ".git/config",
    ],
)
def test_path_alias_traversal_and_protected_roots_fail_closed_before_mutation(
    tmp_path, path
):
    operation = {"op": "write_file", "path": path, "content": "x\n"}
    result = ExecutorService.execute_file_ops(
        tmp_path,
        [operation],
        accepted_path_authority=_authority(
            tmp_path,
            [{"step_number": 1, "ops": [operation]}],
            path="App/Real.py" if path == "app/real.py" else None,
            grant_class=(
                GrantClass.CREATION_AUTHORIZED if path == "app/real.py" else None
            ),
        ),
    )
    assert not result["success"]
    assert not (tmp_path / "app" / "b.py").exists()


def test_symlink_and_special_file_targets_are_denied(tmp_path):
    outside = tmp_path.parent / "phase33c4-outside.txt"
    outside.write_text("outside\n", encoding="utf-8")
    (tmp_path / "linkdir").symlink_to(outside.parent, target_is_directory=True)
    intermediate = {"op": "write_file", "path": "linkdir/escaped.py", "content": "x\n"}
    intermediate_result = ExecutorService.execute_file_ops(
        tmp_path,
        [intermediate],
        accepted_path_authority=_authority(
            tmp_path,
            [{"step_number": 1, "ops": [intermediate]}],
            path="linkdir/escaped.py",
            grant_class=GrantClass.CREATION_AUTHORIZED,
        ),
    )
    assert not intermediate_result["success"]
    assert intermediate_result["authority_error"]["code"] == "path_symlink_rejected"
    assert not (tmp_path.parent / "escaped.py").exists()

    (tmp_path / "final-link").symlink_to(outside)
    final = {"op": "write_file", "path": "final-link", "content": "changed\n"}
    final_result = ExecutorService.execute_file_ops(
        tmp_path,
        [final],
        accepted_path_authority=_authority(
            tmp_path,
            [{"step_number": 1, "ops": [final]}],
            path="final-link",
            grant_class=GrantClass.CREATION_AUTHORIZED,
        ),
    )
    assert not final_result["success"]
    assert outside.read_text(encoding="utf-8") == "outside\n"

    fifo = tmp_path / "special"
    os.mkfifo(fifo)
    special = {"op": "write_file", "path": "special", "content": "x\n"}
    special_result = ExecutorService.execute_file_ops(
        tmp_path,
        [special],
        accepted_path_authority=_authority(
            tmp_path,
            [{"step_number": 1, "ops": [special]}],
            path="special",
            grant_class=GrantClass.CREATION_AUTHORIZED,
        ),
    )
    assert not special_result["success"]


def test_observed_scope_gate_uses_apa_not_expected_or_changeset_paths(tmp_path):
    authorized = tmp_path / "app" / "authorized.py"
    unauthorized = tmp_path / "app" / "unauthorized.py"
    authorized.parent.mkdir()
    authorized.write_text("a\n", encoding="utf-8")
    unauthorized.write_text("u\n", encoding="utf-8")
    pre_checksum = compute_workspace_checksum(tmp_path)
    authorized.write_text("changed\n", encoding="utf-8")
    unauthorized.write_text("changed\n", encoding="utf-8")
    plan = _write_plan("app/authorized.py", "changed\n")
    authority = _authority(
        tmp_path,
        plan,
        path="app/authorized.py",
        grant_class=GrantClass.EXISTING_MUTABLE,
    )

    assert detect_scope_violations(
        tmp_path,
        ["app/unauthorized.py"],
        pre_checksum,
        accepted_path_authority=authority,
    ) == ["app/unauthorized.py"]
    subset_checksum = compute_workspace_checksum(tmp_path)
    authorized.write_text("changed again\n", encoding="utf-8")
    assert (
        detect_scope_violations(
            tmp_path,
            [],
            subset_checksum,
            accepted_path_authority=authority,
        )
        == []
    )


def test_observed_scope_outside_apa_is_blocking_evidence_not_new_authority(tmp_path):
    outside = tmp_path / "app" / "outside.py"
    outside.parent.mkdir()
    outside.write_text("before\n", encoding="utf-8")
    pre_checksum = compute_workspace_checksum(tmp_path)
    outside.write_text("after\n", encoding="utf-8")
    authority = _authority(
        tmp_path,
        [{"step_number": 1, "ops": []}],
    )

    violations = detect_scope_violations(
        tmp_path,
        ["app/outside.py"],
        pre_checksum,
        accepted_path_authority=authority,
    )
    assert violations == ["app/outside.py"]
    assert authority.grant_for(declare("app/outside.py")) is None


def test_execution_loop_blocks_observed_outside_scope_and_preserves_evidence(
    db_session, tmp_path
):
    ctx = _make_scope_loop_context(db_session, tmp_path)
    result = execute_step_loop(
        ctx=ctx,
        extract_structured_text=lambda value: (
            str(value.get("output", value)) if isinstance(value, dict) else str(value)
        ),
        normalize_step=lambda raw_step, project_dir, logger_obj, step_number: dict(
            raw_step
        ),
        normalize_plan_with_live_logging=lambda *args, **kwargs: [],
        workspace_violation_error_cls=RuntimeError,
        write_project_state_snapshot_fn=lambda *args, **kwargs: None,
        record_live_log_fn=lambda *args, **kwargs: None,
    )

    assert result["status"] == "failed"
    assert result["reason"] == "execution_observed_scope_violation"
    assert result["failure_category"] == "validation_failure"
    assert result["observed_scope_violations"] == ["app/unauthorized.py"]
    assert (ctx.orchestration_state.project_dir / "app" / "unauthorized.py").exists()
    assert "app/unauthorized.py" in ctx.orchestration_state.abort_reason


def test_persisted_authority_loads_for_exact_task_session_plan_and_workspace(
    db_session, tmp_path
):
    plan = _write_plan("app/new.py", "x = 1\n")
    session_id, task_id, execution_id = _seed_execution(db_session, tmp_path)
    authority = _authority(
        tmp_path,
        plan,
        path="app/new.py",
        grant_class=GrantClass.CREATION_AUTHORIZED,
    )
    _persist_authority(
        db_session,
        session_id=session_id,
        task_id=task_id,
        authority=authority,
    )

    restored = load_accepted_path_authority_for_execution(
        db_session,
        task_id=task_id,
        session_id=session_id,
        task_execution_id=execution_id,
        plan=plan,
        workspace_identity=str(tmp_path.resolve()),
    )
    assert restored.authority_identity == authority.authority_identity


@pytest.mark.parametrize(
    ("checkpoint_state", "expected_code"),
    [
        (None, "authority_record_missing"),
        ("no_authority", "authority_record_invalid"),
        ("malformed_json", "authority_record_invalid"),
    ],
)
def test_authority_loader_fails_closed_for_missing_or_malformed_checkpoint(
    db_session, tmp_path, checkpoint_state, expected_code
):
    plan = _write_plan("app/new.py", "x = 1\n")
    session_id, task_id, execution_id = _seed_execution(db_session, tmp_path)
    if checkpoint_state is not None:
        snapshot = (
            "not-json"
            if checkpoint_state == "malformed_json"
            else json.dumps({"stage": "plan", "status": "accepted", "details": {}})
        )
        db_session.add(
            TaskCheckpoint(
                session_id=session_id,
                task_id=task_id,
                checkpoint_type="validation_plan",
                state_snapshot=snapshot,
            )
        )
        db_session.commit()

    with pytest.raises(PathAuthorityError) as exc_info:
        load_accepted_path_authority_for_execution(
            db_session,
            task_id=task_id,
            session_id=session_id,
            task_execution_id=execution_id,
            plan=plan,
            workspace_identity=str(tmp_path.resolve()),
        )
    assert exc_info.value.code == expected_code


def test_authority_loader_rejects_tampered_identity_and_wrong_plan_or_workspace(
    db_session, tmp_path
):
    plan = _write_plan("app/new.py", "x = 1\n")
    session_id, task_id, execution_id = _seed_execution(db_session, tmp_path)
    authority = _authority(
        tmp_path,
        plan,
        path="app/new.py",
        grant_class=GrantClass.CREATION_AUTHORIZED,
    )
    tampered = authority.to_dict()
    tampered["authority_identity"] = "0" * 64
    db_session.add(
        TaskCheckpoint(
            session_id=session_id,
            task_id=task_id,
            checkpoint_type="validation_plan",
            state_snapshot=json.dumps(
                {
                    "stage": "plan",
                    "status": "accepted",
                    "details": {"accepted_path_authority": tampered},
                }
            ),
        )
    )
    db_session.commit()

    with pytest.raises(PathAuthorityError, match="authority_identity_mismatch"):
        load_accepted_path_authority_for_execution(
            db_session,
            task_id=task_id,
            session_id=session_id,
            task_execution_id=execution_id,
            plan=plan,
            workspace_identity=str(tmp_path.resolve()),
        )

    db_session.query(TaskCheckpoint).delete()
    _persist_authority(
        db_session,
        session_id=session_id,
        task_id=task_id,
        authority=authority,
    )
    with pytest.raises(PathAuthorityError, match="authority_plan_identity_mismatch"):
        load_accepted_path_authority_for_execution(
            db_session,
            task_id=task_id,
            session_id=session_id,
            task_execution_id=execution_id,
            plan=_write_plan("app/other.py", "x = 2\n"),
            workspace_identity=str(tmp_path.resolve()),
        )
    with pytest.raises(PathAuthorityError, match="authority_workspace_mismatch"):
        load_accepted_path_authority_for_execution(
            db_session,
            task_id=task_id,
            session_id=session_id,
            task_execution_id=execution_id,
            plan=plan,
            workspace_identity=str(tmp_path / "other"),
        )


def test_authority_loader_rejects_wrong_execution_context_and_conflicting_matches(
    db_session, tmp_path
):
    plan = _write_plan("app/new.py", "x = 1\n")
    session_id, task_id, execution_id = _seed_execution(db_session, tmp_path)
    authority = _authority(
        tmp_path,
        plan,
        path="app/new.py",
        grant_class=GrantClass.CREATION_AUTHORIZED,
    )
    _persist_authority(
        db_session,
        session_id=session_id,
        task_id=task_id,
        authority=authority,
    )
    with pytest.raises(
        PathAuthorityError, match="authority_execution_context_mismatch"
    ):
        load_accepted_path_authority_for_execution(
            db_session,
            task_id=task_id + 999,
            session_id=session_id,
            task_execution_id=execution_id,
            plan=plan,
            workspace_identity=str(tmp_path.resolve()),
        )

    second = _authority(
        tmp_path,
        plan,
        path="app/other.py",
        grant_class=GrantClass.CREATION_AUTHORIZED,
    )
    _persist_authority(
        db_session,
        session_id=session_id,
        task_id=task_id,
        authority=second,
    )
    with pytest.raises(PathAuthorityError, match="authority_ambiguous"):
        load_accepted_path_authority_for_execution(
            db_session,
            task_id=task_id,
            session_id=session_id,
            task_execution_id=execution_id,
            plan=plan,
            workspace_identity=str(tmp_path.resolve()),
        )


def test_create_file_is_not_an_accepted_structured_mutation_shape(tmp_path):
    plan = [
        {
            "step_number": 1,
            "description": "Create app/new.py",
            "commands": ["python3 -m pytest -q"],
            "verification": "python3 -m pytest -q",
            "rollback": None,
            "expected_files": ["app/new.py"],
            "ops": [{"op": "create_file", "path": "app/new.py"}],
        }
    ]

    outcome = _validate(
        plan,
        project_dir=tmp_path,
        task_prompt="Create app/new.py and verify it.",
        source_materialization=PlannerSourceMaterialization(
            workspace_identity=str(tmp_path), files=()
        ),
    )

    assert not outcome.accepted
    assert any("supported operation" in reason.lower() for reason in outcome.reasons)
