"""Phase 13B-E62 synthetic gate tests.

Validates that E61's structured ops path makes E59's AST guard reachable in the
live completion repair flow.  Run this before launching any E62 batch.

Success criteria (per E62 design):
  - completion_repair_signature_guard_checked=True
  - candidate_unavailable=False
  - violation_count > 0
  - repair rejected before workspace mutation
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

from app.models import (
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
    User,
)
from app.services.orchestration.diagnostics.signature_guard import (
    COMPLETION_REPAIR_SIGNATURE_VIOLATION_REASON,
    check_completion_repair_signature_contract,
)
from app.services.orchestration.phases.completion_flow import _attempt_completion_repair
from app.services.orchestration.types import OrchestrationRunContext
from app.services.orchestration.prompt_templates import OrchestrationState, StepResult


# ---------------------------------------------------------------------------
# Shared fixtures
# ---------------------------------------------------------------------------

_ORIGINAL_SIGNATURE = (
    "def format_task_line(task: Task, *, include_status: bool = False) -> str:\n"
    "    return task.title\n"
)

_DRIFTED_SIGNATURE = (
    "def format_task_line(task: object) -> str:\n" "    return task.title\n"
)


def _write(path: Path, content: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(content, encoding="utf-8")


class _MockRuntime:
    """Returns a fixed JSON output for every execute_task call."""

    def __init__(self, output: str):
        self.output = output
        self.prompts: list[str] = []
        self.call_count = 0

    async def execute_task(self, prompt, timeout_seconds=None):
        self.prompts.append(str(prompt))
        self.call_count += 1
        return {"output": self.output}


def _completion_validation(src_file="src/formatting.py"):
    return SimpleNamespace(
        stage="task_completion",
        status="repair_required",
        repairable=True,
        profile="implementation",
        reasons=["pytest failure: format_task_line missing include_status parameter"],
        details={
            "expected_core_files": [src_file],
            "failure_class": "completion_verification:pytest_failure",
            "completion_repair_source": "final_completion_verification",
        },
    )


def _build_ctx(
    db_session,
    project_dir: Path,
    runtime: _MockRuntime,
    emitted: list,
    src_file: str = "src/formatting.py",
):
    """Create a minimal OrchestrationRunContext sufficient for _attempt_completion_repair."""
    eval_user = User(email="eval@local.dev", hashed_password="not-used", is_active=True)
    db_session.add(eval_user)
    db_session.flush()

    project = Project(
        name="E62-gate",
        workspace_path=str(project_dir),
        user_id=eval_user.id,
    )
    db_session.add(project)
    db_session.flush()

    session = SessionModel(
        project_id=project.id,
        name="E62-gate",
        status="running",
        is_active=True,
        execution_mode="manual",
    )
    task = Task(
        project_id=project.id,
        title="E62-gate",
        status=TaskStatus.RUNNING,
        task_subfolder="e62",
    )
    db_session.add_all([session, task])
    db_session.flush()

    link = SessionTask(
        session_id=session.id, task_id=task.id, status=TaskStatus.RUNNING
    )
    execution = TaskExecution(
        session_id=session.id,
        task_id=task.id,
        attempt_number=1,
        status=TaskStatus.RUNNING,
    )
    db_session.add_all([link, execution])
    db_session.commit()

    state = OrchestrationState(
        session_id=str(session.id),
        task_description="implement format_task_line",
        project_name="E62-gate",
        task_id=task.id,
        plan=[
            {
                "step_number": 1,
                "description": "wrote formatting.py",
                "expected_files": [src_file],
            }
        ],
    )
    state._project_dir_override = str(project_dir)
    state.execution_results = [
        StepResult(
            step_number=1,
            status="success",
            output="wrote file",
            files_changed=[src_file],
        )
    ]

    return OrchestrationRunContext(
        db=db_session,
        session=session,
        project=project,
        task=task,
        session_task_link=link,
        session_id=session.id,
        task_id=task.id,
        prompt="implement format_task_line with include_status support",
        timeout_seconds=120,
        execution_profile="full_lifecycle",
        validation_profile="implementation",
        runs_in_canonical_baseline=False,
        orchestration_state=state,
        runtime_service=runtime,
        task_service=SimpleNamespace(),
        logger=logging.getLogger("e62-gate"),
        emit_live=lambda *args, **kwargs: emitted.append(kwargs.get("metadata", {})),
        error_handler=SimpleNamespace(),
        task_execution_id=execution.id,
        restore_workspace_snapshot_if_needed=lambda reason: None,
    )


# ---------------------------------------------------------------------------
# Unit-level: E59 guard receives real ops from an ops-fix step
# ---------------------------------------------------------------------------


def test_e59_guard_fires_checked_with_replace_in_file_op(tmp_path):
    """E59 guard receives a replace_in_file op and runs the full AST check."""
    _write(tmp_path / "src/formatting.py", _ORIGINAL_SIGNATURE)

    ops = [
        {
            "op": "replace_in_file",
            "path": "src/formatting.py",
            "old": "def format_task_line(task: Task, *, include_status: bool = False) -> str:",
            "new": "def format_task_line(task: object) -> str:",
        }
    ]
    result = check_completion_repair_signature_contract(project_dir=tmp_path, ops=ops)
    assert (
        result.checked is True
    ), "E59 guard must run AST check for replace_in_file ops"
    assert result.candidate_unavailable is False
    assert len(result.violations) >= 1
    assert result.violations[0].violation_type == "signature_changed"
    assert result.violations[0].qualified_name == "format_task_line"


def test_e59_guard_reports_correct_pre_post_signatures(tmp_path):
    """Guard telemetry contains the exact pre/post signatures from M6."""
    _write(tmp_path / "src/formatting.py", _ORIGINAL_SIGNATURE)

    ops = [
        {
            "op": "replace_in_file",
            "path": "src/formatting.py",
            "old": "def format_task_line(task: Task, *, include_status: bool = False) -> str:",
            "new": "def format_task_line(task: object) -> str:",
        }
    ]
    result = check_completion_repair_signature_contract(project_dir=tmp_path, ops=ops)
    v = result.violations[0]
    # Pre-signature must encode the keyword-only parameter and annotation.
    assert "include_status" in v.pre_signature
    assert "keyword_only" in v.pre_signature
    # Post-signature must not contain include_status.
    assert "include_status" not in v.post_signature


# ---------------------------------------------------------------------------
# Integration: full _attempt_completion_repair flow with pure ops-fix step
# ---------------------------------------------------------------------------


def test_e61_structured_ops_path_triggers_e59_rejection(db_session, tmp_path):
    """
    Primary E62 gate test.

    A pure ops-fix repair step (no 'commands' key) is returned by the mocked
    runtime.  The step drifts format_task_line from its contract.  E59 must
    fire with checked=True, candidate_unavailable=False, violation_count>=1 and
    reject the repair before any workspace mutation.
    """
    project_dir = tmp_path / "project"
    _write(project_dir / "src/formatting.py", _ORIGINAL_SIGNATURE)

    # Pure ops-fix step: no 'commands' key.
    repair_step = {
        "step_number": 2,
        "repair_type": "ops_fix",
        "description": "Fix format_task_line to match test expectations",
        "ops": [
            {
                "op": "replace_in_file",
                "path": "src/formatting.py",
                "old": "def format_task_line(task: Task, *, include_status: bool = False) -> str:",
                "new": "def format_task_line(task: object) -> str:",
            }
        ],
        "verification": "python -m pytest -q",
        "expected_files": ["src/formatting.py"],
    }
    runtime = _MockRuntime(json.dumps(repair_step))
    emitted: list[dict] = []

    ctx = _build_ctx(db_session, project_dir, runtime, emitted)

    with patch(
        "app.services.orchestration.phases.completion_flow._completion_repair_invalid_paths",
        return_value=[],
    ), patch(
        "app.config.settings.COMPLETION_REPAIR_BACKEND",
        None,
    ):
        result = _attempt_completion_repair(
            ctx=ctx,
            completion_validation=_completion_validation(),
            save_orchestration_checkpoint_fn=lambda *args: None,
        )

    # --- Success criteria ---

    # 1. Repair rejected with E59 reason.
    assert result == {
        "status": "failed",
        "reason": COMPLETION_REPAIR_SIGNATURE_VIOLATION_REASON,
    }, f"Expected E59 rejection, got: {result}"

    # 2. Guard telemetry: checked=True, candidate_unavailable=False.
    guard_events = [
        e for e in emitted if "completion_repair_signature_guard_checked" in e
    ]
    assert guard_events, f"No guard telemetry found in emitted events: {emitted}"
    guard_event = guard_events[0]
    assert guard_event["completion_repair_signature_guard_checked"] is True
    assert (
        guard_event["completion_repair_signature_guard_candidate_unavailable"] is False
    )

    # 3. Violation count > 0.
    assert guard_event["completion_repair_signature_violation_count"] >= 1

    # 4. Plan not extended (repair rejected before append).
    assert (
        len(ctx.orchestration_state.plan) == 1
    ), "Plan must not be extended when E59 rejects the repair"

    # 5. LLM was called exactly once (generation step, no execution).
    assert (
        runtime.call_count == 1
    ), "Runtime must be called once for generation; ops application goes direct"

    # 6. Workspace file unchanged — ops were never applied.
    current = (project_dir / "src/formatting.py").read_text()
    assert (
        "include_status" in current
    ), "Workspace file must be unchanged: ops must not apply when E59 rejects"
    assert (
        "task: object" not in current
    ), "Drifted signature must not appear in the workspace after rejection"


def test_e61_structured_ops_path_checked_flag_is_true_not_unavailable(
    db_session, tmp_path
):
    """
    Regression guard: candidate_unavailable must be False when ops are present.

    This is the key difference from E60 (where candidate_unavailable=True because
    the model returned command-only output).
    """
    project_dir = tmp_path / "project"
    _write(project_dir / "src/formatting.py", _ORIGINAL_SIGNATURE)

    repair_step = {
        "repair_type": "ops_fix",
        "description": "mutate signature",
        "ops": [
            {
                "op": "replace_in_file",
                "path": "src/formatting.py",
                "old": "def format_task_line(task: Task, *, include_status: bool = False) -> str:",
                "new": "def format_task_line(task):\n",
            }
        ],
        "verification": "true",
        "expected_files": ["src/formatting.py"],
    }
    runtime = _MockRuntime(json.dumps(repair_step))
    emitted: list[dict] = []
    ctx = _build_ctx(db_session, project_dir, runtime, emitted)

    with patch(
        "app.services.orchestration.phases.completion_flow._completion_repair_invalid_paths",
        return_value=[],
    ), patch(
        "app.config.settings.COMPLETION_REPAIR_BACKEND",
        None,
    ):
        _attempt_completion_repair(
            ctx=ctx,
            completion_validation=_completion_validation(),
            save_orchestration_checkpoint_fn=lambda *args: None,
        )

    guard_events = [
        e for e in emitted if "completion_repair_signature_guard_checked" in e
    ]
    assert guard_events
    # Must NOT be candidate_unavailable — that was the E60 failure mode.
    assert (
        guard_events[0]["completion_repair_signature_guard_candidate_unavailable"]
        is False
    )


def test_e61_prompt_does_not_produce_candidate_unavailable_schema():
    """
    Prompt-level gate: the capsule prompt must request ops, not shell commands.

    If the model follows the prompt, it cannot produce a command-only step that
    would set candidate_unavailable=True.  This test confirms the prompt schema
    has changed in the expected direction.
    """
    from app.services.orchestration.phases.completion_repair_capsule import (
        CompletionRepairCapsule,
        build_bounded_completion_repair_prompt,
    )
    import tempfile

    with tempfile.TemporaryDirectory() as tmp:
        capsule = CompletionRepairCapsule(
            validation_reasons=["pytest: format_task_line missing include_status"],
            relevant_files=["src/formatting.py"],
            last_step_summary="Step 1: wrote formatting.py - success.",
            workspace_path=tmp,
            task_prompt_excerpt="Implement format_task_line(task, *, include_status=False)",
        )
        prompt = build_bounded_completion_repair_prompt(capsule, 2)

    # The prompt must request structured ops (ops_fix), not shell commands.
    assert "ops_fix" in prompt
    assert "replace_in_file" in prompt
    assert '"commands": [' not in prompt
    # The word "ops" must appear as an instruction key.
    assert '"ops"' in prompt
