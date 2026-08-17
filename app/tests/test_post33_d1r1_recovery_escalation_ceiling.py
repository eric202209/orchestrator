"""POST33-D1R1 — bounded recovery escalation ceiling.

Reproduces the POST33-D1 live defect provider-free:

    planning_semantic_target_contract_violation
      → treated as a transient/retryable failure
      → up to four Planning passes per TaskExecution (Celery retry)
      → retry exhaustion queues one automatic recovery rerun
      → the rerun's fresh TaskExecution gets a fresh retry budget
      → the Celery retry path resets task.workspace_status, re-arming the
        one-shot recovery guard
      → executions 281 → 282 → 283 → 284, 13 Planning invocations, 0 plans

The repair has two halves, both asserted here:

1. Deterministic planning/plan contract rejections are retry-exempt, so the
   provider is never re-invoked with identical inputs.
2. The automatic recovery rerun ceiling is derived from the persisted
   TaskExecution rows (the durable episode generation counter) instead of the
   mutable ``task.workspace_status``, so it survives TaskExecution replacement.

No provider calls.
"""

from __future__ import annotations

import logging

import pytest

from app.models import (
    Project,
    Session as SessionModel,
    Task,
    TaskExecution,
    TaskStatus,
)
from app.services.orchestration.error_handler import EnhancedErrorHandler
from app.services.orchestration.phases.failure_flow import handle_task_failure
from app.services.orchestration.types import OrchestrationRunContext
from app.services.session.execution_policy import (
    automatic_recovery_rerun_allowed,
    classify_failure,
    is_deterministic_planning_contract_failure,
    is_retry_exempt_category,
)

# The exact reason string the worker raises for the D1 failure:
# planning_flow returns {"status": "failed", "reason": failure_type} and the
# worker re-raises it as RuntimeError(reason).
D1_FAILURE_REASON = "planning_semantic_target_contract_violation"
WINDOW4_POST_REPAIR_FAILURE_REASON = "planning_validation_failed_after_repair"
WINDOW4_MISSING_SOURCE_MATERIALIZATION_REASON = (
    "planning_repair_missing_source_materialization"
)

DETERMINISTIC_REASONS = [
    D1_FAILURE_REASON,
    WINDOW4_POST_REPAIR_FAILURE_REASON,
    WINDOW4_MISSING_SOURCE_MATERIALIZATION_REASON,
    "unknown_target_id: target_id is not present in the current inventory",
    "provider_plan_shape_invalid: provider plan must be a list",
    "provider_selector_internals_forbidden: provider replace operation ...",
    "target_id_path_mismatch: target_id is bound to a different canonical path",
    "planning_semantic_target_inventory_invalid",
    "op_contract_violation",
    "repair_output_contract_violation",
]

TRANSIENT_REASONS = [
    "OpenClaw provider transport interrupted while streaming",
    "temporary backend availability failure; provider unreachable",
    "planning backend timed out after 300s",
]


class _ExhaustedSelfTask:
    """Celery task whose retry budget is already spent."""

    max_retries = 3

    class request:
        retries = 3

    def retry(self, exc, **kwargs):  # pragma: no cover - must never fire
        raise AssertionError("retry must not be scheduled once the budget is spent")


class _FirstAttemptSelfTask:
    """Celery task on its first attempt (retries=0, max_retries=3)."""

    max_retries = 3

    class request:
        retries = 0

    class RetrySignal(Exception):
        pass

    def retry(self, exc, **kwargs):
        self.retry_kwargs = kwargs
        raise self.RetrySignal(exc)


def _make_episode(
    db,
    *,
    execution_mode: str = "automatic",
    session_status: str = "running",
    task_status: TaskStatus = TaskStatus.RUNNING,
    workspace_status: str = "isolated",
    execution_count: int = 1,
):
    project = Project(name="D1R1 Project")
    db.add(project)
    db.commit()
    db.refresh(project)

    session = SessionModel(
        project_id=project.id,
        name="D1R1 Session",
        status=session_status,
        execution_mode=execution_mode,
        is_active=session_status == "running",
    )
    db.add(session)
    db.commit()
    db.refresh(session)

    task = Task(
        project_id=project.id,
        title="Add the bounded repair",
        description="Add the bounded repair to the existing module.",
        status=task_status,
        execution_profile="full_lifecycle",
        plan_position=1,
        workspace_status=workspace_status,
        task_subfolder="task-add-the-bounded-repair",
    )
    db.add(task)
    db.commit()
    db.refresh(task)

    for attempt in range(1, execution_count + 1):
        db.add(
            TaskExecution(
                session_id=session.id,
                task_id=task.id,
                attempt_number=attempt,
                status=TaskStatus.RUNNING,
            )
        )
    db.commit()

    return project, session, task


def _make_ctx(db, project, session, task):
    """Context wired to the real EnhancedErrorHandler — no stubbed retry policy."""

    current_execution = (
        db.query(TaskExecution)
        .filter(
            TaskExecution.session_id == session.id,
            TaskExecution.task_id == task.id,
        )
        .order_by(TaskExecution.attempt_number.desc())
        .first()
    )
    return OrchestrationRunContext(
        db=db,
        session=session,
        project=project,
        task=task,
        task_execution_id=current_execution.id if current_execution else None,
        session_task_link=None,
        session_id=session.id,
        task_id=task.id,
        prompt=task.description,
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="implementation",
        runs_in_canonical_baseline=False,
        orchestration_state=None,
        runtime_service=None,
        task_service=None,
        logger=logging.getLogger(__name__),
        emit_live=lambda *_args, **_kwargs: None,
        error_handler=EnhancedErrorHandler(),
        restore_workspace_snapshot_if_needed=None,
    )


def _run_failure(db, ctx, *, self_task, reason, queued):
    def fake_queue_task_for_session(*, db, session, task_id, **_kwargs):
        # Production queue_task_for_session creates a fresh TaskExecution;
        # mirror that so the durable episode counter advances realistically.
        db.add(
            TaskExecution(
                session_id=session.id,
                task_id=task_id,
                attempt_number=(
                    db.query(TaskExecution)
                    .filter(
                        TaskExecution.session_id == session.id,
                        TaskExecution.task_id == task_id,
                    )
                    .count()
                    + 1
                ),
                status=TaskStatus.PENDING,
            )
        )
        db.commit()
        queued.append(task_id)
        return {"task_id": task_id}

    raised = None
    try:
        handle_task_failure(
            self_task=self_task,
            ctx=ctx,
            exc=RuntimeError(reason),
            get_latest_session_task_link_fn=lambda *_a, **_k: None,
            queue_task_for_session_fn=fake_queue_task_for_session,
            write_project_state_snapshot_fn=lambda *_a, **_k: None,
            save_orchestration_checkpoint_fn=lambda *_a, **_k: None,
            record_live_log_fn=lambda *_a, **_k: None,
        )
    except Exception as exc:  # terminal failures re-raise the original error
        raised = exc
    return raised


# ── A/B/C: deterministic classification ──────────────────────────────────────


@pytest.mark.parametrize("reason", DETERMINISTIC_REASONS)
def test_deterministic_contract_failures_are_retry_exempt(reason):
    """A, B, C — semantic contract violation, unknown target ID, malformed contract."""

    assert is_deterministic_planning_contract_failure(reason) is True
    assert classify_failure(reason, "", {"failure_phase": "planning"}) == (
        "planning_contract_violation"
    )
    assert is_retry_exempt_category("planning_contract_violation") is True
    assert EnhancedErrorHandler().should_retry(RuntimeError(reason)) is False


# ── D/E: transient retry behaviour preserved ─────────────────────────────────


@pytest.mark.parametrize("reason", TRANSIENT_REASONS)
def test_transient_provider_failures_keep_existing_retry_behaviour(reason):
    """D, E — transport interruption, transient unavailability, timeout."""

    assert is_deterministic_planning_contract_failure(reason) is False
    assert classify_failure(reason, "", {}) != "planning_contract_violation"
    assert EnhancedErrorHandler().should_retry(RuntimeError(reason)) is True


def test_transient_failure_still_schedules_one_celery_retry(db_session):
    """A transient failure with retry capacity still enters the Celery retry path."""

    project, session, task = _make_episode(db_session)
    ctx = _make_ctx(db_session, project, session, task)
    self_task = _FirstAttemptSelfTask()
    queued: list[int] = []

    raised = _run_failure(
        db_session,
        ctx,
        self_task=self_task,
        reason="OpenClaw provider transport interrupted while streaming",
        queued=queued,
    )

    assert isinstance(raised, _FirstAttemptSelfTask.RetrySignal)
    assert queued == []


# ── D1 reproduction: no provider retry, no recovery rerun ────────────────────


def test_d1_failure_does_not_consume_a_provider_retry(db_session):
    """Pre-fix this raised RetrySignal (Planning pass 2 of 4 for one execution)."""

    project, session, task = _make_episode(db_session)
    ctx = _make_ctx(db_session, project, session, task)
    self_task = _FirstAttemptSelfTask()
    queued: list[int] = []

    raised = _run_failure(
        db_session, ctx, self_task=self_task, reason=D1_FAILURE_REASON, queued=queued
    )

    assert not isinstance(raised, _FirstAttemptSelfTask.RetrySignal)
    assert isinstance(raised, RuntimeError)
    assert str(raised) == D1_FAILURE_REASON
    assert queued == []
    assert (
        db_session.query(TaskExecution).filter(TaskExecution.task_id == task.id).count()
        == 1
    )


def test_window4_post_repair_validation_failure_does_not_schedule_celery_retry(
    db_session,
):
    """Task 213 shape: exhausted Plan Repair validation is terminal, not transient."""

    project, session, task = _make_episode(db_session)
    ctx = _make_ctx(db_session, project, session, task)
    queued: list[int] = []

    raised = _run_failure(
        db_session,
        ctx,
        self_task=_FirstAttemptSelfTask(),
        reason=WINDOW4_POST_REPAIR_FAILURE_REASON,
        queued=queued,
    )

    db_session.refresh(task)
    db_session.refresh(session)
    execution = (
        db_session.query(TaskExecution).filter(TaskExecution.task_id == task.id).one()
    )
    assert not isinstance(raised, _FirstAttemptSelfTask.RetrySignal)
    assert isinstance(raised, RuntimeError)
    assert str(raised) == WINDOW4_POST_REPAIR_FAILURE_REASON
    assert queued == []
    assert execution.failure_category == "planning_contract_violation"
    assert execution.status == TaskStatus.FAILED
    assert task.status == TaskStatus.FAILED
    assert session.status == "paused"
    assert (
        db_session.query(TaskExecution).filter(TaskExecution.task_id == task.id).count()
        == 1
    )


def test_window4_missing_source_materialization_does_not_schedule_celery_retry(
    db_session,
):
    """Attempt 4 shape: completed Plan Repair with no concrete source edit is terminal."""

    project, session, task = _make_episode(db_session)
    ctx = _make_ctx(db_session, project, session, task)
    queued: list[int] = []

    raised = _run_failure(
        db_session,
        ctx,
        self_task=_FirstAttemptSelfTask(),
        reason=WINDOW4_MISSING_SOURCE_MATERIALIZATION_REASON,
        queued=queued,
    )

    execution = (
        db_session.query(TaskExecution).filter(TaskExecution.task_id == task.id).one()
    )
    assert not isinstance(raised, _FirstAttemptSelfTask.RetrySignal)
    assert isinstance(raised, RuntimeError)
    assert str(raised) == WINDOW4_MISSING_SOURCE_MATERIALIZATION_REASON
    assert queued == []
    assert execution.failure_category == "planning_contract_violation"
    assert execution.status == TaskStatus.FAILED


def test_f_deterministic_exhaustion_queues_no_automatic_recovery(db_session):
    """F — retry exhaustion on a deterministic failure must not queue a rerun."""

    project, session, task = _make_episode(db_session)
    ctx = _make_ctx(db_session, project, session, task)
    queued: list[int] = []

    raised = _run_failure(
        db_session,
        ctx,
        self_task=_ExhaustedSelfTask(),
        reason=D1_FAILURE_REASON,
        queued=queued,
    )

    db_session.refresh(task)
    assert queued == []
    assert isinstance(raised, RuntimeError)
    assert task.workspace_status != "changes_requested"


# ── N/K: truthful terminal state ─────────────────────────────────────────────


def test_n_terminal_state_is_truthful_after_deterministic_failure(db_session):
    """N — no PENDING task while a hidden recovery execution runs; K — retryable."""

    project, session, task = _make_episode(db_session)
    ctx = _make_ctx(db_session, project, session, task)
    queued: list[int] = []

    _run_failure(
        db_session,
        ctx,
        self_task=_ExhaustedSelfTask(),
        reason=D1_FAILURE_REASON,
        queued=queued,
    )

    db_session.refresh(task)
    db_session.refresh(session)
    execution = (
        db_session.query(TaskExecution).filter(TaskExecution.task_id == task.id).one()
    )

    assert task.status == TaskStatus.FAILED
    assert task.status != TaskStatus.PENDING
    assert D1_FAILURE_REASON in (task.error_message or "")
    assert execution.status == TaskStatus.FAILED
    assert execution.failure_category == "planning_contract_violation"
    assert session.status == "paused"
    assert session.last_alert_level == "error"
    # K: FAILED is the state the operator manual-retry endpoint accepts.
    assert task.status in {TaskStatus.FAILED, TaskStatus.CANCELLED, TaskStatus.DONE}


# ── G/H/I/J: the recovery ceiling itself ─────────────────────────────────────


def test_g_recovery_eligible_failure_queues_one_rerun(db_session):
    """G — a legitimately recovery-eligible failure still gets its one rerun."""

    project, session, task = _make_episode(db_session)
    ctx = _make_ctx(db_session, project, session, task)
    queued: list[int] = []

    _run_failure(
        db_session,
        ctx,
        self_task=_ExhaustedSelfTask(),
        reason="Execution guessed the wrong workspace structure",
        queued=queued,
    )

    db_session.refresh(task)
    assert queued == [task.id]
    assert task.status == TaskStatus.PENDING
    assert task.workspace_status == "changes_requested"
    assert (
        db_session.query(TaskExecution).filter(TaskExecution.task_id == task.id).count()
        == 2
    )


def test_h_second_exhaustion_cannot_queue_a_second_rerun(db_session):
    """H — the rerun's own failure must not queue another rerun."""

    project, session, task = _make_episode(db_session, execution_count=2)
    ctx = _make_ctx(db_session, project, session, task)
    queued: list[int] = []

    raised = _run_failure(
        db_session,
        ctx,
        self_task=_ExhaustedSelfTask(),
        reason="Execution guessed the wrong workspace structure",
        queued=queued,
    )

    db_session.refresh(task)
    assert queued == []
    assert isinstance(raised, RuntimeError)
    assert task.status == TaskStatus.FAILED


@pytest.mark.parametrize("workspace_status", ["not_created", "in_progress", "isolated"])
def test_i_workspace_status_reset_cannot_re_arm_the_ceiling(
    db_session, workspace_status
):
    """I — the Celery retry path resets workspace_status; the ceiling must hold."""

    project, session, task = _make_episode(
        db_session, workspace_status=workspace_status, execution_count=2
    )
    ctx = _make_ctx(db_session, project, session, task)
    queued: list[int] = []

    _run_failure(
        db_session,
        ctx,
        self_task=_ExhaustedSelfTask(),
        reason="Execution guessed the wrong workspace structure",
        queued=queued,
    )

    assert queued == []


def test_j_ceiling_survives_task_execution_replacement(db_session):
    """J — the guard is durable across the fresh TaskExecution of the rerun."""

    project, session, task = _make_episode(db_session)
    assert automatic_recovery_rerun_allowed(
        db_session, session_id=session.id, task_id=task.id
    )

    db_session.add(
        TaskExecution(
            session_id=session.id,
            task_id=task.id,
            attempt_number=2,
            status=TaskStatus.RUNNING,
        )
    )
    db_session.commit()

    # A brand-new TaskExecution with a pristine workspace_status still cannot
    # re-arm the episode ceiling.
    task.workspace_status = "in_progress"
    db_session.commit()
    assert not automatic_recovery_rerun_allowed(
        db_session, session_id=session.id, task_id=task.id
    )


# ── L/M: cancellation and session stop ───────────────────────────────────────


def test_l_cancelled_task_does_not_escalate_to_recovery(db_session):
    """L — cancellation must not queue an automatic recovery rerun."""

    project, session, task = _make_episode(db_session, task_status=TaskStatus.CANCELLED)
    ctx = _make_ctx(db_session, project, session, task)
    queued: list[int] = []

    _run_failure(
        db_session,
        ctx,
        self_task=_ExhaustedSelfTask(),
        reason="Execution cancelled by operator",
        queued=queued,
    )

    assert queued == []


def test_m_stopped_session_enqueues_no_new_execution(db_session):
    """M — a stopped session must not be re-armed by a late failure."""

    project, session, task = _make_episode(db_session, session_status="stopped")
    ctx = _make_ctx(db_session, project, session, task)
    queued: list[int] = []

    _run_failure(
        db_session,
        ctx,
        self_task=_ExhaustedSelfTask(),
        reason="Execution guessed the wrong workspace structure",
        queued=queued,
    )

    assert queued == []
    assert (
        db_session.query(TaskExecution).filter(TaskExecution.task_id == task.id).count()
        == 1
    )


# ── The loop invariant ───────────────────────────────────────────────────────


def test_deterministic_failure_episode_cannot_grow_an_execution_chain(db_session):
    """Drive the full policy repeatedly; the episode must stay bounded.

    Pre-fix this produced an unbounded 281 → 282 → 283 → 284 … chain. The
    ceiling for a deterministic planning contract violation is one
    TaskExecution, one Planning provider intent, zero recovery reruns.
    """

    project, session, task = _make_episode(db_session)
    queued: list[int] = []

    for _ in range(6):
        # Re-arm every mutable field the retry lifecycle legitimately resets.
        task.status = TaskStatus.RUNNING
        task.workspace_status = "in_progress"
        session.status = "running"
        session.is_active = True
        db_session.commit()

        ctx = _make_ctx(db_session, project, session, task)
        _run_failure(
            db_session,
            ctx,
            self_task=_FirstAttemptSelfTask(),
            reason=D1_FAILURE_REASON,
            queued=queued,
        )

    assert queued == []
    assert (
        db_session.query(TaskExecution).filter(TaskExecution.task_id == task.id).count()
        == 1
    )
