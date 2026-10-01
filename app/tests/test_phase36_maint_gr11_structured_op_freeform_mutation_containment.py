"""PHASE36-MAINT-GR11 structured-op / free-form runtime mutation containment.

REENTRY-5 step 3 applied an admitted ``replace_in_file`` and then, because its
command was not locally executable, fell through to a free-form OpenClaw turn
that undid the insertion and edited a comment.  The step completed and the
comment-only workspace became the Candidate.

These tests drive the real ``execute_step_loop`` provider-free.  Only the
runtime boundary is stubbed; it may mutate the workspace to model an agent.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any, Callable

from app.models import (
    TaskCheckpoint,
    TaskExecution,
    TaskExecutionChangeSet,
    TaskStatus,
)
from app.services.agents.agent_backends import (
    ExecutionTopology,
    get_backend_descriptor,
)
from app.services.agents.agent_runtime import execution_topology_for_runtime
from app.services.orchestration.validation.path_authority import GrantClass
from app.services.orchestration.validation.workspace_guard import (
    compute_workspace_checksum,
    detect_post_structured_op_mutations,
)
from app.tests.test_phase33d3_dual_read_execution_anchor_resolver import (
    _semantic_operation,
)
from app.tests.test_post33_exec2_structured_orchestrator_consumption import (
    DirectRuntimeStub,
    _authority,
    _make_loop_context,
    _persist_authority,
    _run_loop,
    _step,
)

ROUTER = (
    "# Permission Approval\n"
    "ROUTES = ['/permissions/permissions']\n"
    "SETTINGS = 'settings'\n"
)
INSERTED = "ROUTES.append('/permissions')\n"
ROUTER_OP = {
    "op": "replace_in_file",
    "path": "router.py",
    "old": "SETTINGS = 'settings'\n",
    "new": INSERTED + "SETTINGS = 'settings'\n",
}
# REENTRY-5's step-3 command: not read-only, not a simple local verification,
# not a safe local shell form -> free-form runtime dispatch.
IMPORT_CHECK = "python -c \"from router import ROUTES; print('Router imported')\""
BEHAVIORAL_CHECK = (
    "python -c \"from router import ROUTES; assert '/permissions' in ROUTES; "
    "print('ok')\""
)
REASON = "execution_post_structured_op_mutation"


class AgentRuntimeStub(DirectRuntimeStub):
    """Agent-runtime (OpenClaw-capability) boundary that may mutate the workspace."""

    def __init__(self, mutate: Callable[[Path], None] | None = None) -> None:
        super().__init__(
            {"status": "completed", "output": "{}"}, backend="local_openclaw"
        )
        self.backend_descriptor = get_backend_descriptor("local_openclaw")
        self.mutate = mutate
        self.workspace: Path | None = None

    async def execute_task(
        self, prompt: str, timeout_seconds: int = 300, **kwargs: Any
    ) -> dict[str, Any]:
        self.calls.append({"prompt": prompt, "timeout_seconds": timeout_seconds})
        if self.mutate is not None:
            self.mutate(self.workspace)
        return {
            "status": "completed",
            "output": json.dumps(
                {"status": "completed", "output": "done", "files_changed": []}
            ),
        }


def _undo_and_edit_comment(workspace: Path) -> None:
    (workspace / "router.py").write_text(
        ROUTER.replace("# Permission Approval", "# Permissions"), encoding="utf-8"
    )


def _edit_other(workspace: Path) -> None:
    (workspace / "other.py").write_text("X = 2\n", encoding="utf-8")


def _create_extra(workspace: Path) -> None:
    (workspace / "extra.py").write_text("Y = 1\n", encoding="utf-8")


def _run(
    db: Any,
    tmp_path: Path,
    *,
    ops: list[dict[str, Any]] | Callable[[Path], list[dict[str, Any]]] = (),
    commands: list[str] = (),
    verification: str = "",
    mutate: Callable[[Path], None] | None = None,
    extra_grants: list[tuple[str, GrantClass]] = (),
    expected_files: list[str] = ("router.py",),
) -> tuple[dict[str, Any], AgentRuntimeStub, Any, Path]:
    step = _step(
        description="Integrate permissions router",
        commands=list(commands),
        verification=verification,
        expected_files=list(expected_files),
    )
    runtime = AgentRuntimeStub(mutate)
    ctx, _, workspace = _make_loop_context(db, tmp_path, step=step, runtime=runtime)
    runtime.workspace = workspace
    (workspace / "router.py").write_text(ROUTER, encoding="utf-8")
    (workspace / "other.py").write_text("X = 1\n", encoding="utf-8")
    step["ops"] = list(ops(workspace) if callable(ops) else ops)
    db.query(TaskCheckpoint).delete()
    grants = [
        ("router.py", GrantClass.EXISTING_MUTABLE),
        ("other.py", GrantClass.EXISTING_MUTABLE),
        ("extra.py", GrantClass.CREATION_AUTHORIZED),
        *extra_grants,
    ]
    _persist_authority(
        db,
        session_id=ctx.session_id,
        task_id=ctx.task_id,
        authority=_authority(workspace, [step], grants),
    )
    return _run_loop(ctx), runtime, ctx, workspace


def _router(workspace: Path) -> str:
    return (workspace / "router.py").read_text(encoding="utf-8")


def _assert_contained(result, ctx, workspace, paths):
    assert result["status"] == "failed"
    assert result["reason"] == REASON
    assert result["failure_category"] == "validation_failure"
    assert result["post_structured_op_mutations"] == paths
    assert result["observed_scope_violations"] == []
    assert result["authority_error"]["code"] == "runtime_mutation_after_structured_ops"
    assert ctx.orchestration_state.status.value == "aborted"
    # The failed step is recorded as a failure, never as a step result.
    assert ctx.orchestration_state.execution_results == []
    error = ctx.orchestration_state.debug_attempts[-1]["error"]
    assert "after the admitted structured operations" in error
    assert all(path in error for path in paths)


# --- R1-R5: legitimate structured-op steps ---------------------------------


def test_r1_structured_op_only_succeeds(db_session, tmp_path):
    result, runtime, _, workspace = _run(db_session, tmp_path, ops=[ROUTER_OP])
    assert result == {"status": "completed"}
    assert runtime.calls == []
    assert INSERTED in _router(workspace)


def test_r2_structured_op_plus_read_only_inspection_succeeds(db_session, tmp_path):
    result, runtime, _, workspace = _run(
        db_session, tmp_path, ops=[ROUTER_OP], commands=["cat router.py"]
    )
    assert result == {"status": "completed"}
    assert runtime.calls == []
    assert INSERTED in _router(workspace)


def test_r3_structured_op_plus_local_verification_succeeds(db_session, tmp_path):
    command = (
        "python -c \"import pathlib; print(pathlib.Path('router.py').read_text())\""
    )
    result, runtime, _, workspace = _run(
        db_session,
        tmp_path,
        ops=[ROUTER_OP],
        commands=[command],
        verification=command,
    )
    assert result == {"status": "completed"}
    assert runtime.calls == []
    assert INSERTED in _router(workspace)


def test_r4_structured_op_plus_same_step_behavioral_assertion_succeeds(
    db_session, tmp_path
):
    result, runtime, ctx, workspace = _run(
        db_session,
        tmp_path,
        ops=[ROUTER_OP],
        commands=[BEHAVIORAL_CHECK],
        verification=BEHAVIORAL_CHECK,
    )
    assert result == {"status": "completed"}
    assert len(runtime.calls) == 1
    assert INSERTED in _router(workspace)
    assert "ok" in ctx.orchestration_state.execution_results[-1].verification_output


def test_r5_structured_op_plus_runtime_without_mutation_succeeds(db_session, tmp_path):
    result, runtime, ctx, workspace = _run(
        db_session,
        tmp_path,
        ops=[ROUTER_OP],
        commands=[IMPORT_CHECK],
        verification=IMPORT_CHECK,
    )
    assert result == {"status": "completed"}
    assert len(runtime.calls) == 1
    assert INSERTED in _router(workspace)
    assert ctx.orchestration_state.execution_results[-1].status == "success"


def test_runtime_noise_files_are_not_post_op_mutations(db_session, tmp_path):
    def noise(workspace: Path) -> None:
        (workspace / "package-lock.json").write_text("{}", encoding="utf-8")
        (workspace / "run.log").write_text("x", encoding="utf-8")
        (workspace / "__pycache__").mkdir(exist_ok=True)
        (workspace / "__pycache__" / "router.cpython-312.pyc").write_bytes(b"x")

    result, _, _, _ = _run(
        db_session,
        tmp_path,
        ops=[ROUTER_OP],
        commands=[IMPORT_CHECK],
        mutate=noise,
    )
    assert result == {"status": "completed"}


# --- R6-R8: runtime mutation after the admitted structured op ---------------


def test_r6_runtime_undoing_admitted_op_fails_closed(db_session, tmp_path):
    result, runtime, ctx, workspace = _run(
        db_session,
        tmp_path,
        ops=[ROUTER_OP],
        commands=[IMPORT_CHECK],
        verification=IMPORT_CHECK,
        mutate=_undo_and_edit_comment,
    )
    assert len(runtime.calls) == 1
    _assert_contained(result, ctx, workspace, ["router.py"])


def test_r7_runtime_adding_unrelated_source_mutation_fails_closed(db_session, tmp_path):
    result, _, ctx, workspace = _run(
        db_session,
        tmp_path,
        ops=[ROUTER_OP],
        commands=[IMPORT_CHECK],
        mutate=_create_extra,
    )
    _assert_contained(result, ctx, workspace, ["extra.py"])
    # Containment, not rollback: the admitted op stays applied.
    assert INSERTED in _router(workspace)


def test_r8_runtime_changing_another_authorized_path_fails_closed(db_session, tmp_path):
    result, _, ctx, workspace = _run(
        db_session,
        tmp_path,
        ops=[ROUTER_OP],
        commands=[IMPORT_CHECK],
        mutate=_edit_other,
    )
    # other.py holds an EXISTING_MUTABLE APA grant; path authority alone is
    # not authority for the runtime to mutate it after the structured op.
    _assert_contained(result, ctx, workspace, ["other.py"])


def test_r8b_runtime_deleting_a_file_fails_closed(db_session, tmp_path):
    result, _, ctx, workspace = _run(
        db_session,
        tmp_path,
        ops=[ROUTER_OP],
        commands=[IMPORT_CHECK],
        mutate=lambda workspace: (workspace / "other.py").unlink(),
    )
    _assert_contained(result, ctx, workspace, ["other.py"])


def test_r8c_explicit_mutating_command_keeps_independent_runtime_authority(
    db_session, tmp_path
):
    # ``sed -i`` is an admitted mutation form that no local channel runs; the
    # Plan explicitly gave the runtime mutation work, so GR11 does not apply.
    result, runtime, _, workspace = _run(
        db_session,
        tmp_path,
        ops=[ROUTER_OP],
        commands=["sed -i 's/X = 1/X = 2/' other.py"],
        mutate=_edit_other,
    )
    assert result == {"status": "completed"}
    assert len(runtime.calls) == 1
    assert (workspace / "other.py").read_text(encoding="utf-8") == "X = 2\n"


# --- R9-R13: preserved behavior ---------------------------------------------


def test_r9_free_form_only_mutation_workflow_is_preserved(db_session, tmp_path):
    result, runtime, _, workspace = _run(
        db_session,
        tmp_path,
        ops=[],
        commands=["custom-runtime-step"],
        mutate=_undo_and_edit_comment,
    )
    assert result == {"status": "completed"}
    assert len(runtime.calls) == 1
    assert execution_topology_for_runtime(runtime) is ExecutionTopology.AGENT_RUNTIME
    assert _router(workspace).startswith("# Permissions\n")


def test_r10_multiple_structured_ops_are_preserved(db_session, tmp_path):
    other_op = {
        "op": "replace_in_file",
        "path": "other.py",
        "old": "X = 1\n",
        "new": "X = 3\n",
    }
    result, runtime, ctx, workspace = _run(
        db_session,
        tmp_path,
        ops=[ROUTER_OP, other_op],
        commands=[IMPORT_CHECK],
    )
    assert result == {"status": "completed"}
    assert len(runtime.calls) == 1
    assert INSERTED in _router(workspace)
    assert (workspace / "other.py").read_text(encoding="utf-8") == "X = 3\n"
    assert set(ctx.orchestration_state.execution_results[-1].files_changed) >= {
        "router.py",
        "other.py",
    }


def test_r11_selector_replacement_is_preserved(db_session, tmp_path):
    def selector_ops(workspace: Path) -> list[dict[str, Any]]:
        return [_semantic_operation(workspace, path="other.py", new="X = 5\n")]

    result, runtime, _, workspace = _run(
        db_session,
        tmp_path,
        ops=selector_ops,
        commands=[IMPORT_CHECK],
        expected_files=["other.py"],
    )
    assert result == {"status": "completed"}
    assert len(runtime.calls) == 1
    assert (workspace / "other.py").read_text(encoding="utf-8") == "X = 5\n"


def test_r11b_selector_replacement_then_runtime_undo_fails_closed(db_session, tmp_path):
    def selector_ops(workspace: Path) -> list[dict[str, Any]]:
        return [_semantic_operation(workspace, path="other.py", new="X = 5\n")]

    result, _, ctx, workspace = _run(
        db_session,
        tmp_path,
        ops=selector_ops,
        commands=[IMPORT_CHECK],
        mutate=_edit_other,
        expected_files=["other.py"],
    )
    _assert_contained(result, ctx, workspace, ["other.py"])


def test_r12_write_append_delete_structured_ops_are_preserved(db_session, tmp_path):
    ops = [
        {"op": "write_file", "path": "extra.py", "content": "Y = 1\n"},
        {"op": "append_file", "path": "router.py", "content": "Z = 1\n"},
        {"op": "delete_file", "path": "gone.py"},
    ]

    def prepared(workspace: Path) -> list[dict[str, Any]]:
        (workspace / "gone.py").write_text("G = 1\n", encoding="utf-8")
        return ops

    result, runtime, _, workspace = _run(
        db_session,
        tmp_path,
        ops=prepared,
        commands=[IMPORT_CHECK],
        extra_grants=[("gone.py", GrantClass.DELETION_AUTHORIZED)],
    )
    assert result == {"status": "completed"}
    assert len(runtime.calls) == 1
    assert (workspace / "extra.py").read_text(encoding="utf-8") == "Y = 1\n"
    assert _router(workspace).endswith("Z = 1\n")
    assert not (workspace / "gone.py").exists()


def test_r13_declared_verification_still_runs_after_admitted_mutation(
    db_session, tmp_path
):
    failing = (
        'python -c "from router import ROUTES; '
        "assert '/missing' in ROUTES, 'declared-check-ran'\""
    )
    result, _, ctx, _ = _run(
        db_session,
        tmp_path,
        ops=[ROUTER_OP],
        commands=[IMPORT_CHECK],
        verification=failing,
    )
    assert result["status"] == "failed"
    assert result.get("reason") != REASON
    assert "declared-check-ran" in json.dumps(ctx.orchestration_state.debug_attempts)


# --- R14/R15 + REENTRY-5 counterfactual ------------------------------------


def test_r14_r15_reentry5_counterfactual_is_contained_without_candidate(
    db_session, tmp_path
):
    result, runtime, ctx, workspace = _run(
        db_session,
        tmp_path,
        ops=[ROUTER_OP],
        commands=[IMPORT_CHECK],
        verification=IMPORT_CHECK,
        mutate=_undo_and_edit_comment,
    )
    # The free-form turn is handed the step description as implementation
    # work, with no notice that the structured op already applied it.
    assert "fully implement what the step describes" in runtime.calls[0]["prompt"]
    _assert_contained(result, ctx, workspace, ["router.py"])
    execution = db_session.get(TaskExecution, ctx.task_execution_id)
    assert execution.failure_category == "validation_failure"
    assert execution.status == TaskStatus.FAILED
    assert ctx.task.status == TaskStatus.FAILED
    assert ctx.task.workspace_status == "blocked"
    # Execution aborts before task summary, where the change set (Candidate)
    # is captured, so the comment-only workspace never becomes a Candidate.
    assert db_session.query(TaskExecutionChangeSet).count() == 0


# --- detector unit ----------------------------------------------------------


def test_detector_reports_modified_created_and_deleted_paths(tmp_path):
    (tmp_path / "a.py").write_text("a\n", encoding="utf-8")
    (tmp_path / "b.py").write_text("b\n", encoding="utf-8")
    baseline = compute_workspace_checksum(tmp_path)
    (tmp_path / "a.py").write_text("A\n", encoding="utf-8")
    (tmp_path / "b.py").unlink()
    (tmp_path / "c.py").write_text("c\n", encoding="utf-8")
    (tmp_path / "yarn.lock").write_text("x", encoding="utf-8")

    assert detect_post_structured_op_mutations(tmp_path, baseline) == [
        "a.py",
        "b.py",
        "c.py",
    ]
