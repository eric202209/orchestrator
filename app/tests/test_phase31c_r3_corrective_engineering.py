"""Phase 31C-R3: failure-family convergence regressions.

Family A — execution coordination: canonical-root promotion under the
already-held dispatch lock, and single-authority retry/recovery ownership.
Family B — planner contract integrity: artifact-task bootstrap
classification and intact rejected-plan repair excerpts.
Family C — completion validation: covered in
test_phase10l_verification_integrity.py (pre-existing-source waiver).
"""

from __future__ import annotations

import json
import logging
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.models import (
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.orchestration.coordinators.failure_coordinator import (
    FailureCoordinator,
)
from app.services.orchestration.planning.repair_prompts import (
    PLANNING_REPAIR_INTACT_PLAN_MAX_CHARS,
    PLANNING_REPAIR_MAX_MALFORMED_OUTPUT_CHARS,
    compact_invalid_output_excerpt,
)
from app.services.orchestration.planning.task_bootstrap_contract import (
    BootstrapTaskType,
    validate_task1_bootstrap_contract,
)
from app.services.orchestration.types import OrchestrationRunContext
from app.services.session.execution_policy import (
    classify_failure,
    is_retry_exempt_category,
)
from app.services.workspace.baseline_promotion_service import (
    BaselinePromotionService,
)
from app.services.workspace.project_mutation_lock import ProjectMutationLockError

_LOG = logging.getLogger(__name__)


def _NOOP(*a, **k):
    return None


class _TerminalSelfTask:
    max_retries = 0

    class request:
        retries = 0

    def retry(self, exc, **kwargs):
        raise AssertionError("celery retry should not be called")


# ---------------------------------------------------------------------------
# Family A — canonical-root promotion under an already-held dispatch lock
# ---------------------------------------------------------------------------


def _promotion_service_double():
    calls = {"run_locked": 0, "unlocked": 0, "context": 0}

    class _Mutations:
        def run_locked(self, project, *, project_root, operation, owner, fn):
            calls["run_locked"] += 1
            return fn()

    double = SimpleNamespace(
        get_project_root=lambda project: Path("/tmp/r3-project"),
        canonical_mutations=_Mutations(),
    )

    def _unlocked(project, task, change_set):
        calls["unlocked"] += 1
        return {"files_copied": 1}

    double.promote_change_set_into_baseline_unlocked = _unlocked
    double._trigger_engineering_context_generation = lambda project: calls.__setitem__(
        "context", calls["context"] + 1
    )
    return double, calls


def test_promote_change_set_lock_already_held_skips_reacquisition():
    double, calls = _promotion_service_double()
    result = BaselinePromotionService.promote_change_set_into_baseline(
        double,
        SimpleNamespace(id=1),
        SimpleNamespace(id=2),
        {"task_execution_id": 3},
        lock_already_held=True,
    )
    assert result == {"files_copied": 1}
    assert calls["run_locked"] == 0
    assert calls["unlocked"] == 1
    assert calls["context"] == 1


def test_promote_change_set_default_path_still_acquires_lock():
    double, calls = _promotion_service_double()
    result = BaselinePromotionService.promote_change_set_into_baseline(
        double,
        SimpleNamespace(id=1),
        SimpleNamespace(id=2),
        {"task_execution_id": 3},
    )
    assert result == {"files_copied": 1}
    assert calls["run_locked"] == 1
    assert calls["unlocked"] == 1


# ---------------------------------------------------------------------------
# Family A — retry ownership: classification of deterministic capability
# rejections and auto-recovery gating
# ---------------------------------------------------------------------------


def test_context_window_rejection_is_retry_exempt():
    category = classify_failure(
        "OpenClaw CLI error: FailoverError: Model context window too small "
        "(8192 tokens). Minimum is 16000.",
        "local_openclaw",
        {},
    )
    assert category == "backend_transport_error"
    assert is_retry_exempt_category(category) is True


def test_blocked_model_rejection_is_retry_exempt():
    category = classify_failure(
        "[agent] blocked model (context window too small): ollama/x ctx=8192",
        "local_openclaw",
        {},
    )
    assert category == "backend_transport_error"
    assert is_retry_exempt_category(category) is True


def test_generic_execution_failure_stays_retry_eligible():
    category = classify_failure("step 2 raised AssertionError", "b", {})
    assert category == "execution_failure"
    assert is_retry_exempt_category(category) is False


def _seed_auto_ctx(db_session):
    project = Project(name="R3 Project", workspace_path="/tmp/r3-auto")
    db_session.add(project)
    db_session.flush()
    session = SessionModel(
        project_id=project.id,
        name="R3 Session",
        status="running",
        execution_mode="automatic",
        is_active=True,
    )
    task = Task(
        project_id=project.id,
        title="R3 Task",
        status=TaskStatus.RUNNING,
        task_subfolder="task-r3",
        plan_position=1,
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
    ctx = OrchestrationRunContext(
        db=db_session,
        session=session,
        project=project,
        task=task,
        session_task_link=link,
        session_id=session.id,
        task_id=task.id,
        prompt="test prompt",
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="implementation",
        runs_in_canonical_baseline=False,
        orchestration_state=None,
        runtime_service=None,
        task_service=None,
        logger=_LOG,
        emit_live=_NOOP,
        error_handler=type(
            "EH", (), {"should_retry": staticmethod(lambda exc, ctx: False)}
        )(),
        restore_workspace_snapshot_if_needed=None,
        task_execution_id=execution.id,
    )
    return ctx, session, task


def _run_handle_failure(ctx, exc, queue_fn):
    return FailureCoordinator().handle_failure(
        self_task=_TerminalSelfTask(),
        ctx=ctx,
        exc=exc,
        get_latest_session_task_link_fn=lambda *a, **k: None,
        write_project_state_snapshot_fn=_NOOP,
        save_orchestration_checkpoint_fn=_NOOP,
        record_live_log_fn=_NOOP,
        queue_task_for_session_fn=queue_fn,
    )


def test_mutation_lock_conflict_does_not_queue_automatic_recovery(db_session):
    ctx, session, task = _seed_auto_ctx(db_session)
    queue_fn = MagicMock()
    exc = ProjectMutationLockError(
        project_id=ctx.project.id,
        operation="execute_canonical_root_task",
        lock_path=Path("/tmp/r3-auto/.agent/locks/x.mutation.lock"),
    )

    with pytest.raises(ProjectMutationLockError):
        _run_handle_failure(ctx, exc, queue_fn)

    queue_fn.assert_not_called()
    db_session.refresh(task)
    assert task.status == TaskStatus.FAILED


def test_retry_exempt_capability_failure_does_not_queue_automatic_recovery(
    db_session,
):
    ctx, session, task = _seed_auto_ctx(db_session)
    queue_fn = MagicMock()
    exc = RuntimeError(
        "OpenClaw request failed: FailoverError: Model context window too "
        "small (8192 tokens). Minimum is 16000."
    )

    with pytest.raises(RuntimeError):
        _run_handle_failure(ctx, exc, queue_fn)

    queue_fn.assert_not_called()
    db_session.refresh(task)
    assert task.status == TaskStatus.FAILED


def test_generic_execution_failure_still_queues_one_automatic_recovery(db_session):
    ctx, session, task = _seed_auto_ctx(db_session)
    queue_fn = MagicMock()
    exc = RuntimeError("step 2 raised AssertionError in generated code")

    result = _run_handle_failure(ctx, exc, queue_fn)

    assert result is None
    queue_fn.assert_called_once()


# ---------------------------------------------------------------------------
# Family B — bootstrap classification of artifact-only tasks with bare
# action-verb prompts
# ---------------------------------------------------------------------------

_S16_PROMPT = (
    "Implement the requested review-held change: add a short 'Review "
    "checklist' section to README-PILOT.md describing that a human must "
    "verify the Stage 1 evidence before publication. Prepare the change set "
    "and leave it for human review; do not publish or modify unrelated files."
)

_README_PLAN = [
    {
        "step_number": 1,
        "description": "Add the Review checklist section",
        "commands": [],
        "verification": (
            "python -c \"import sys; c=open('README-PILOT.md').read(); "
            "sys.exit(0 if 'Review checklist' in c else 1)\""
        ),
        "rollback": None,
        "expected_files": ["README-PILOT.md"],
        "ops": [
            {
                "op": "write_file",
                "path": "README-PILOT.md",
                "content": (
                    "# Overview\n\n## Review checklist\n\n"
                    "A human must verify the Stage 1 evidence before "
                    "publication.\n"
                ),
            }
        ],
    }
]


def test_action_verb_artifact_task_classifies_artifact_only():
    verdict = validate_task1_bootstrap_contract(
        plan=_README_PLAN,
        task_prompt=_S16_PROMPT,
    )
    assert verdict.contract.bootstrap_task_type == BootstrapTaskType.ARTIFACT_ONLY
    assert verdict.passed, verdict.violations


def test_source_noun_prompt_still_forces_mixed_on_artifact_plan():
    verdict = validate_task1_bootstrap_contract(
        plan=_README_PLAN,
        task_prompt=(
            "Implement the parser module in app/parser.py and summarize the "
            "behavior in README-PILOT.md."
        ),
    )
    assert verdict.contract.bootstrap_task_type == BootstrapTaskType.MIXED
    assert not verdict.passed
    assert "task1_bootstrap_missing_expected_source_files" in verdict.violation_codes


def test_source_intent_without_artifact_vocabulary_still_requires_source():
    verdict = validate_task1_bootstrap_contract(
        plan=[
            {
                "step_number": 1,
                "description": "Write notes",
                "commands": [],
                "verification": "python -c \"print('ok')\"",
                "rollback": None,
                "expected_files": ["notes.txt"],
                "ops": [{"op": "write_file", "path": "notes.txt", "content": "x" * 40}],
            }
        ],
        task_prompt="Implement the new validation behavior for uploads.",
    )
    # No artifact vocabulary and no source surface: stays UNKNOWN, which keeps
    # source materialization required — the lazy-plan guard is preserved.
    assert verdict.contract.bootstrap_task_type == BootstrapTaskType.UNKNOWN
    assert not verdict.passed
    assert "task1_bootstrap_missing_expected_source_files" in verdict.violation_codes


# ---------------------------------------------------------------------------
# Family B — intact rejected plans are passed whole to the repair pass
# ---------------------------------------------------------------------------


def _plan_json_of_length(target_chars: int) -> str:
    steps = []
    index = 1
    while True:
        steps.append(
            {
                "step_number": index,
                "description": "step " + "d" * 120,
                "commands": ["python -c \"print('x')\""],
                "verification": "python3 -m pytest -q",
                "rollback": None,
                "expected_files": ["app/main.py"],
            }
        )
        text = json.dumps(steps)
        if len(text) >= target_chars:
            return text
        index += 1


def test_intact_json_plan_is_not_truncated_in_repair_excerpt():
    plan_text = _plan_json_of_length(2200)
    assert len(plan_text) > PLANNING_REPAIR_MAX_MALFORMED_OUTPUT_CHARS
    assert len(plan_text) <= PLANNING_REPAIR_INTACT_PLAN_MAX_CHARS
    excerpt = compact_invalid_output_excerpt(plan_text)
    assert "...<truncated malformed planning output>..." not in excerpt
    assert json.loads(excerpt) == json.loads(plan_text)


def test_oversized_json_plan_still_truncates():
    plan_text = _plan_json_of_length(PLANNING_REPAIR_INTACT_PLAN_MAX_CHARS + 500)
    excerpt = compact_invalid_output_excerpt(plan_text)
    assert "...<truncated malformed planning output>..." in excerpt
    assert len(excerpt) <= PLANNING_REPAIR_MAX_MALFORMED_OUTPUT_CHARS + 80


def test_non_plan_json_over_limit_still_truncates():
    text = "prose " * 300
    excerpt = compact_invalid_output_excerpt(text)
    assert "...<truncated malformed planning output>..." in excerpt
