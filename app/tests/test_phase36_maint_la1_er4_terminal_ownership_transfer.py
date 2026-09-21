"""Provider-free ER4 terminal Planning ownership-transfer regressions.

Phase 36 Maintenance LA1-ER4, cases E3-R1 .. E3-R15 plus the CA2 replay.

Every case is deterministic and provider-free: no planning, repair,
reflection, digest, or completion provider is reachable from these tests.
``_provider_free_failure_setup`` installs a sentinel that fails the test if a
provider lane is entered.
"""

from __future__ import annotations

import ast
import inspect
import logging
import socket
from datetime import UTC, datetime, timedelta
from pathlib import Path
from types import SimpleNamespace

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
from app.services.orchestration.lifecycle.authority import derive_lifecycle_authority
from app.services.orchestration.lifecycle.terminal_handoff import (
    TerminalAttemptHandoffError,
    terminal_attempt_handoff_from_planning_result,
)
from app.services.orchestration.lifecycle.transitions import (
    LifecycleTransitionError,
    claim_continuation,
    enter_recovering,
    finalize_logical_failure,
    resolve_continuation_identity,
    schedule_continuation,
)
from app.services.orchestration.phases.planning_support import (
    _finalize_planning_terminal_failure,
)
from app.services.orchestration.state.session_state import (
    mark_session_paused,
    mark_session_stopped,
)
from app.services.orchestration.types import OrchestrationRunContext
from app.services.session.orphan_ownership import evaluate_execution_ownership


T0 = datetime(2026, 9, 21, 12, 0, 0, tzinfo=UTC)

DISCOVERY_TERMINAL_RESULT = {
    "status": "failed",
    "reason": "discovery_output_not_json",
    "failure_category": "discovery_terminal_failure",
    "terminal_failure": True,
    "discovery_turns_used": 1,
}


class _RetrySignal(Exception):
    pass


class _RetryTask:
    """Celery task double that records whether ``retry`` was ever entered."""

    max_retries = 3
    default_retry_delay = 0

    def __init__(self, retries: int = 0):
        self.request = SimpleNamespace(retries=retries, kwargs={})
        self.retry_calls: list[Exception] = []

    def retry(self, exc, **_kwargs):
        self.retry_calls.append(exc)
        raise _RetrySignal(str(exc))


class _NoRetryTask(_RetryTask):
    max_retries = 0


def _seed(
    db,
    tmp_path: Path,
    *,
    instance_id: str = "er4-generation-1",
    status: str = "running",
):
    project = Project(
        name="ER4 Project",
        workspace_path=str(tmp_path / "project-workspace"),
    )
    session = SessionModel(
        project=project,
        name="ER4 Execution Session",
        status=status,
        execution_mode="manual",
        is_active=True,
        instance_id=instance_id,
    )
    task = Task(
        project=project,
        title="ER4 Task",
        description="Terminal Planning ownership transfer",
        status=TaskStatus.RUNNING,
        task_subfolder="task-er4",
        workspace_status="in_progress",
        plan_position=1,
    )
    link = SessionTask(session=session, task=task, status=TaskStatus.RUNNING)
    execution = TaskExecution(
        session=session,
        task=task,
        attempt_number=1,
        status=TaskStatus.RUNNING,
        worker_pid=424242,
        worker_hostname=socket.gethostname(),
        worker_process_start_identity="er4-process-start",
        heartbeat_at=T0,
    )
    db.add_all([project, session, task, link, execution])
    db.commit()
    return project, session, task, link, execution


def _ctx(db, project, session, task, link, execution, *, should_retry=True):
    return OrchestrationRunContext(
        db=db,
        session=session,
        project=project,
        task=task,
        session_task_link=link,
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
        error_handler=SimpleNamespace(should_retry=lambda _exc, _scope: should_retry),
        restore_workspace_snapshot_if_needed=None,
        task_execution_id=execution.id,
    )


def _failure_kwargs(link):
    return {
        "get_latest_session_task_link_fn": lambda *_args, **_kwargs: link,
        "write_project_state_snapshot_fn": lambda *_args, **_kwargs: None,
        "save_orchestration_checkpoint_fn": lambda *_args, **_kwargs: None,
        "record_live_log_fn": lambda *_args, **_kwargs: None,
    }


class _ProviderLedger:
    """Fails the test if any provider-capable lane is entered."""

    def __init__(self):
        self.calls: list[str] = []

    def forbid(self, lane: str):
        def _blocked(*_args, **_kwargs):
            self.calls.append(lane)
            raise AssertionError(f"provider lane entered: {lane}")

        return _blocked


def _provider_free_failure_setup(monkeypatch) -> _ProviderLedger:
    ledger = _ProviderLedger()
    monkeypatch.setattr(
        "app.services.orchestration.phases.failure_flow._prepare_retry_workspace",
        lambda **_kwargs: (True, {}, False),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.failure_flow._apply_knowledge_halt",
        lambda **_kwargs: False,
    )
    monkeypatch.setattr(
        "app.services.orchestration.recovery.recovery_strategy_registry.RecoveryStrategyRegistry.route",
        lambda *_args, **_kwargs: SimpleNamespace(strategy="continue"),
    )
    monkeypatch.setattr(
        "app.services.orchestration.phases.failure_flow.record_failure_knowledge_for_stopped_session",
        lambda **_kwargs: True,
    )
    monkeypatch.setattr(
        "app.services.orchestration.coordinators.failure_coordinator._invoke_reflection_prompt",
        ledger.forbid("failure_reflection"),
    )
    return ledger


def _handoff(session, execution, result=None):
    return terminal_attempt_handoff_from_planning_result(
        result or DISCOVERY_TERMINAL_RESULT,
        session_id=session.id,
        task_execution_id=execution.id,
        expected_session_instance_id=session.instance_id,
    )


def _commit_attempt_evidence(ctx, *, failure_type, failure_reason, root_cause=None):
    """Planning finalizer: durable attempt evidence, no Session outcome."""

    _finalize_planning_terminal_failure(
        ctx=ctx,
        failure_type=failure_type,
        failure_reason=failure_reason,
        planning_root_cause=root_cause,
    )


def _authority(db, session, task, execution=None):
    return derive_lifecycle_authority(
        db, session, task_id=task.id, latest_task_execution=execution
    )


def _assert_physical_release(execution):
    assert execution.worker_pid is None
    assert execution.worker_hostname is None
    assert execution.worker_process_start_identity is None
    assert execution.heartbeat_at is None


# ---------------------------------------------------------------------------
# E3-R1  discovery terminal / recovery disabled
# ---------------------------------------------------------------------------


def test_e3_r1_discovery_terminal_recovery_disabled_is_logically_terminal(
    db_session, tmp_path, monkeypatch
):
    project, session, task, link, execution = _seed(db_session, tmp_path)
    ctx = _ctx(db_session, project, session, task, link, execution)
    ledger = _provider_free_failure_setup(monkeypatch)
    original_instance = session.instance_id

    _commit_attempt_evidence(
        ctx,
        failure_type="discovery_output_not_json",
        failure_reason="read_only_discovery_failed_closed: discovery_output_not_json",
    )
    db_session.refresh(session)
    db_session.refresh(execution)
    # Planning owns attempt evidence only.
    assert task.status == TaskStatus.FAILED
    assert link.status == TaskStatus.FAILED
    assert execution.status == TaskStatus.FAILED
    assert session.status == "running"
    assert session.continuation_task_id is None

    handoff = _handoff(session, execution)
    fc_calls = []
    with pytest.raises(TerminalAttemptHandoffError) as raised:
        fc_calls.append(1)
        FailureCoordinator().handle_failure(
            self_task=_RetryTask(),
            ctx=ctx,
            exc=handoff,
            **_failure_kwargs(link),
        )

    assert raised.value.failure_category == "discovery_terminal_failure"
    assert len(fc_calls) == 1
    db_session.expire_all()
    session = db_session.query(SessionModel).filter_by(id=session.id).one()
    execution = db_session.query(TaskExecution).filter_by(id=execution.id).one()
    authority = _authority(db_session, session, task, execution)
    assert session.status == "failed"
    assert session.is_active is False
    assert session.continuation_task_id is None
    assert session.instance_id != original_instance  # generation fenced
    assert authority.logical_terminal is True
    assert authority.quiescent is True
    assert authority.continuation_pending is False
    _assert_physical_release(execution)
    assert ledger.calls == []


# ---------------------------------------------------------------------------
# E3-R2  recoverable Planning failure
# ---------------------------------------------------------------------------


def test_e3_r2_recoverable_planning_failure_still_recovers(
    db_session, tmp_path, monkeypatch
):
    project, session, task, link, execution = _seed(db_session, tmp_path)
    ctx = _ctx(db_session, project, session, task, link, execution)
    ledger = _provider_free_failure_setup(monkeypatch)

    _commit_attempt_evidence(
        ctx,
        failure_type="planning_json_error",
        failure_reason="malformed planning JSON after repair",
    )
    with pytest.raises(_RetrySignal):
        FailureCoordinator().handle_failure(
            self_task=_RetryTask(),
            ctx=ctx,
            exc=RuntimeError("planning_json_error: retryable planning failure"),
            **_failure_kwargs(link),
        )

    db_session.expire_all()
    session = db_session.query(SessionModel).filter_by(id=session.id).one()
    execution = db_session.query(TaskExecution).filter_by(id=execution.id).one()
    authority = _authority(db_session, session, task, execution)
    assert session.status == "retry_pending"
    assert execution.status == TaskStatus.PENDING
    assert authority.continuation_pending is True
    assert authority.logical_terminal is False
    assert session.continuation_task_id == task.id
    assert ledger.calls == []


# ---------------------------------------------------------------------------
# E3-R3  Planning repair exhaustion
# ---------------------------------------------------------------------------


def test_e3_r3_planning_repair_exhaustion_does_not_preempt_recovery(
    db_session, tmp_path, monkeypatch
):
    project, session, task, link, execution = _seed(db_session, tmp_path)
    ctx = _ctx(db_session, project, session, task, link, execution)
    _provider_free_failure_setup(monkeypatch)

    _commit_attempt_evidence(
        ctx,
        failure_type="root_cause_oscillation_no_progress",
        failure_reason="invalid_python: bounded Planning repair exhausted",
        root_cause="root_cause_oscillation_no_progress",
    )
    db_session.refresh(session)
    assert session.status == "running"

    with pytest.raises(_RetrySignal):
        FailureCoordinator().handle_failure(
            self_task=_RetryTask(),
            ctx=ctx,
            exc=RuntimeError(
                "Planning invalid_python root_cause_oscillation_no_progress"
            ),
            **_failure_kwargs(link),
        )

    db_session.expire_all()
    session = db_session.query(SessionModel).filter_by(id=session.id).one()
    authority = _authority(db_session, session, task)
    assert session.status == "retry_pending"
    assert authority.continuation_pending is True
    assert authority.logical_terminal is False


# ---------------------------------------------------------------------------
# E3-R4  provider protocol / output-contract terminal failure
# ---------------------------------------------------------------------------


def test_e3_r4_output_contract_terminal_failure_transfers_original_category(
    db_session, tmp_path, monkeypatch
):
    project, session, task, link, execution = _seed(db_session, tmp_path)
    ctx = _ctx(db_session, project, session, task, link, execution)
    _provider_free_failure_setup(monkeypatch)

    contract_result = {
        "status": "failed",
        "reason": "discovery_output_contract_violation",
        "failure_category": "discovery_terminal_failure",
        "terminal_failure": True,
    }
    _commit_attempt_evidence(
        ctx,
        failure_type="discovery_output_contract_violation",
        failure_reason="provider output contract violated during read-only discovery",
    )
    handoff = _handoff(session, execution, contract_result)
    observed: list[str] = []
    original_handle = FailureCoordinator.handle_failure

    def _observing(self, *, exc, **kwargs):
        observed.append(getattr(exc, "failure_category", None))
        return original_handle(self, exc=exc, **kwargs)

    monkeypatch.setattr(FailureCoordinator, "handle_failure", _observing)

    with pytest.raises(TerminalAttemptHandoffError):
        FailureCoordinator().handle_failure(
            self_task=_RetryTask(),
            ctx=ctx,
            exc=handoff,
            **_failure_kwargs(link),
        )

    # The original failure facts survive the handoff, not a generic error.
    assert observed == ["discovery_terminal_failure"]
    assert handoff.failure_reason == "discovery_output_contract_violation"
    db_session.expire_all()
    session = db_session.query(SessionModel).filter_by(id=session.id).one()
    assert session.status == "failed"
    assert _authority(db_session, session, task).logical_terminal is True


# ---------------------------------------------------------------------------
# E3-R5  FailureCoordinator admission failure (pre-marker)
# ---------------------------------------------------------------------------


def test_e3_r5_admission_failure_terminalizes_and_leaves_no_orphan(
    db_session, tmp_path, monkeypatch
):
    project, session, task, link, execution = _seed(db_session, tmp_path)
    ctx = _ctx(db_session, project, session, task, link, execution)
    _provider_free_failure_setup(monkeypatch)
    scheduled = []

    def reject_recovery(*_args, **_kwargs):
        raise LifecycleTransitionError("injected_recovery_admission_failure")

    monkeypatch.setattr(
        "app.services.orchestration.coordinators.failure_coordinator.enter_recovering",
        reject_recovery,
    )
    monkeypatch.setattr(
        "app.services.orchestration.coordinators.failure_coordinator.schedule_continuation",
        lambda *a, **k: scheduled.append(1),
    )

    with pytest.raises(RuntimeError, match="retryable failure"):
        FailureCoordinator().handle_failure(
            self_task=_RetryTask(),
            ctx=ctx,
            exc=RuntimeError("retryable failure"),
            **_failure_kwargs(link),
        )

    db_session.expire_all()
    session = db_session.query(SessionModel).filter_by(id=session.id).one()
    execution = db_session.query(TaskExecution).filter_by(id=execution.id).one()
    authority = _authority(db_session, session, task, execution)
    assert scheduled == []
    assert session.status == "failed"
    assert session.is_active is False
    assert session.continuation_task_id is None
    assert execution.status == TaskStatus.FAILED
    assert authority.logical_terminal is True
    assert authority.quiescent is True
    assert authority.continuation_pending is False
    # No pending orphan attempt and no capable-owner gap.
    pending = (
        db_session.query(TaskExecution)
        .filter(
            TaskExecution.session_id == session.id,
            TaskExecution.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]),
        )
        .count()
    )
    assert pending == 0
    _assert_physical_release(execution)


# ---------------------------------------------------------------------------
# E3-R6  concurrent operator pause
# ---------------------------------------------------------------------------


def test_e3_r6_operator_pause_remains_authoritative(db_session, tmp_path, monkeypatch):
    project, session, task, link, execution = _seed(db_session, tmp_path)
    ctx = _ctx(db_session, project, session, task, link, execution)
    _provider_free_failure_setup(monkeypatch)

    _commit_attempt_evidence(
        ctx,
        failure_type="discovery_output_not_json",
        failure_reason="read_only_discovery_failed_closed",
    )
    handoff = _handoff(session, execution)

    # The operator pause wins between attempt finalization and reconciliation.
    mark_session_paused(session, alert_level="info", alert_message="operator pause")
    db_session.commit()
    paused_instance = session.instance_id

    with pytest.raises(TerminalAttemptHandoffError):
        FailureCoordinator().handle_failure(
            self_task=_RetryTask(),
            ctx=ctx,
            exc=handoff,
            **_failure_kwargs(link),
        )

    db_session.expire_all()
    session = db_session.query(SessionModel).filter_by(id=session.id).one()
    execution = db_session.query(TaskExecution).filter_by(id=execution.id).one()
    assert session.status == "paused"
    assert session.instance_id == paused_instance
    assert session.continuation_task_id is None
    assert (
        _authority(db_session, session, task, execution).continuation_pending is False
    )
    _assert_physical_release(execution)


# ---------------------------------------------------------------------------
# E3-R7  concurrent operator stop / cancel
# ---------------------------------------------------------------------------


@pytest.mark.parametrize("terminal_status", ["stopped", "cancelled"])
def test_e3_r7_operator_stop_cancel_is_never_reopened(
    db_session, tmp_path, monkeypatch, terminal_status
):
    project, session, task, link, execution = _seed(db_session, tmp_path)
    ctx = _ctx(db_session, project, session, task, link, execution)
    _provider_free_failure_setup(monkeypatch)

    _commit_attempt_evidence(
        ctx,
        failure_type="discovery_output_not_json",
        failure_reason="read_only_discovery_failed_closed",
    )
    handoff = _handoff(session, execution)

    if terminal_status == "stopped":
        mark_session_stopped(session)
    else:
        session.status = "cancelled"
        session.is_active = False
    db_session.commit()
    stop_instance = session.instance_id

    with pytest.raises(TerminalAttemptHandoffError):
        FailureCoordinator().handle_failure(
            self_task=_RetryTask(),
            ctx=ctx,
            exc=handoff,
            **_failure_kwargs(link),
        )

    db_session.expire_all()
    session = db_session.query(SessionModel).filter_by(id=session.id).one()
    assert session.status == terminal_status
    assert session.is_active is False
    assert session.instance_id == stop_instance
    assert session.continuation_task_id is None


# ---------------------------------------------------------------------------
# E3-R8  stale worker generation (successor generation race)
# ---------------------------------------------------------------------------


def test_e3_r8_stale_generation_cannot_touch_successor(
    db_session, tmp_path, monkeypatch
):
    project, session, task, link, execution = _seed(db_session, tmp_path)
    ctx = _ctx(db_session, project, session, task, link, execution)
    _provider_free_failure_setup(monkeypatch)

    _commit_attempt_evidence(
        ctx,
        failure_type="discovery_output_not_json",
        failure_reason="read_only_discovery_failed_closed",
    )
    handoff = _handoff(session, execution)
    assert handoff.expected_session_instance_id == "er4-generation-1"

    # A successor generation is admitted before the old handoff reconciles.
    session.instance_id = "er4-generation-2"
    session.status = "running"
    session.is_active = True
    successor = TaskExecution(
        session=session,
        task=task,
        attempt_number=2,
        status=TaskStatus.RUNNING,
        worker_pid=515151,
        worker_hostname="successor-host",
        worker_process_start_identity="successor-process-start",
        heartbeat_at=T0 + timedelta(seconds=30),
    )
    db_session.add(successor)
    db_session.commit()
    successor_id = successor.id

    with pytest.raises(TerminalAttemptHandoffError):
        FailureCoordinator().handle_failure(
            self_task=_RetryTask(),
            ctx=ctx,
            exc=handoff,
            **_failure_kwargs(link),
        )

    db_session.expire_all()
    session = db_session.query(SessionModel).filter_by(id=session.id).one()
    successor = db_session.query(TaskExecution).filter_by(id=successor_id).one()
    old_execution = db_session.query(TaskExecution).filter_by(id=execution.id).one()
    assert session.status == "running"
    assert session.is_active is True
    assert session.instance_id == "er4-generation-2"
    assert session.continuation_task_id is None
    # Successor attempt and its physical ownership are untouched.
    assert successor.status == TaskStatus.RUNNING
    assert successor.worker_pid == 515151
    assert successor.worker_hostname == "successor-host"
    assert successor.worker_process_start_identity == "successor-process-start"
    # Only the old owner's own physical identity was released.
    assert old_execution.status == TaskStatus.FAILED
    _assert_physical_release(old_execution)


# ---------------------------------------------------------------------------
# E3-R9  successful retry continuation
# ---------------------------------------------------------------------------


def test_e3_r9_retry_continuation_is_durable_and_claimable(
    db_session, tmp_path, monkeypatch
):
    project, session, task, link, execution = _seed(db_session, tmp_path)
    ctx = _ctx(db_session, project, session, task, link, execution)
    _provider_free_failure_setup(monkeypatch)

    with pytest.raises(_RetrySignal):
        FailureCoordinator().handle_failure(
            self_task=_RetryTask(),
            ctx=ctx,
            exc=RuntimeError("planning_json_error: retryable planning failure"),
            **_failure_kwargs(link),
        )

    db_session.expire_all()
    session = db_session.query(SessionModel).filter_by(id=session.id).one()
    identity = resolve_continuation_identity(db_session, session)
    assert identity is not None
    assert identity.instance_id == session.instance_id
    assert identity.continuation_task_id == task.id

    claimed = claim_continuation(db_session, identity, commit=True)
    assert bool(claimed) is True
    db_session.refresh(session)
    assert session.status == "running"
    assert session.continuation_task_id is None


# ---------------------------------------------------------------------------
# E3-R10  BR2 lost publication
# ---------------------------------------------------------------------------


def test_e3_r10_lost_publication_remains_br2_owned(db_session, tmp_path):
    _project, session, task, _link, execution = _seed(db_session, tmp_path)

    enter_recovering(
        db_session,
        session,
        task_execution=execution,
        continuation_kind="celery_retry",
        retry_count=0,
        failure_reason="retryable failure",
        commit=True,
    )
    schedule_continuation(
        db_session,
        session,
        continuation_kind="celery_retry",
        retry_count=1,
        retry_eta=T0 + timedelta(seconds=15),
        commit=True,
    )
    # The publication that should follow this commit is lost here.
    db_session.refresh(session)

    identity = resolve_continuation_identity(db_session, session)
    authority = _authority(db_session, session, task)
    assert session.status == "retry_pending"
    assert identity is not None
    assert authority.continuation_pending is True
    assert authority.logical_terminal is False
    assert authority.quiescent is False


# ---------------------------------------------------------------------------
# E3-R11  physical cleanup
# ---------------------------------------------------------------------------


def test_e3_r11_physical_cleanup_is_identity_conditional(
    db_session, tmp_path, monkeypatch
):
    project, session, task, link, execution = _seed(db_session, tmp_path)
    ctx = _ctx(db_session, project, session, task, link, execution)
    _provider_free_failure_setup(monkeypatch)

    other_task = Task(
        project=project,
        title="ER4 Unrelated Task",
        description="unrelated",
        status=TaskStatus.RUNNING,
        task_subfolder="task-er4-other",
    )
    db_session.add(other_task)
    db_session.commit()

    _commit_attempt_evidence(
        ctx,
        failure_type="discovery_output_not_json",
        failure_reason="read_only_discovery_failed_closed",
    )
    with pytest.raises(TerminalAttemptHandoffError):
        FailureCoordinator().handle_failure(
            self_task=_RetryTask(),
            ctx=ctx,
            exc=_handoff(session, execution),
            **_failure_kwargs(link),
        )

    db_session.expire_all()
    execution = db_session.query(TaskExecution).filter_by(id=execution.id).one()
    _assert_physical_release(execution)
    assert execution.status == TaskStatus.FAILED


# ---------------------------------------------------------------------------
# E3-R12  Celery wrapper / result semantics
# ---------------------------------------------------------------------------


def test_e3_r12_terminal_handoff_is_celery_failure_not_retry(
    db_session, tmp_path, monkeypatch
):
    project, session, task, link, execution = _seed(db_session, tmp_path)
    ctx = _ctx(db_session, project, session, task, link, execution)
    _provider_free_failure_setup(monkeypatch)
    celery_task = _RetryTask()

    _commit_attempt_evidence(
        ctx,
        failure_type="discovery_output_not_json",
        failure_reason="read_only_discovery_failed_closed",
    )
    with pytest.raises(TerminalAttemptHandoffError) as raised:
        FailureCoordinator().handle_failure(
            self_task=celery_task,
            ctx=ctx,
            exc=_handoff(session, execution),
            **_failure_kwargs(link),
        )

    # Celery sees FAILURE (the typed exception propagates), never a retry.
    assert celery_task.retry_calls == []
    assert isinstance(raised.value, TerminalAttemptHandoffError)
    assert raised.value.terminal_failure is True
    db_session.expire_all()
    session = db_session.query(SessionModel).filter_by(id=session.id).one()
    assert session.status == "failed"


# ---------------------------------------------------------------------------
# E3-R13  orphan sweep fallback (defence in depth, unchanged)
# ---------------------------------------------------------------------------


def test_e3_r13_orphan_sweep_threshold_and_fallback_are_unchanged(db_session, tmp_path):
    from app.tasks.maintenance import sweep_orphaned_running_sessions

    signature = inspect.signature(sweep_orphaned_running_sessions.__wrapped__)
    assert signature.parameters["stale_after_seconds"].default == 2100

    # A genuine pre-existing orphan is still classified as recoverable.
    _project, _session, _task, _link, execution = _seed(db_session, tmp_path)
    execution.worker_pid = 2**21
    execution.worker_hostname = socket.gethostname()
    execution.worker_process_start_identity = "long-gone-process"
    execution.heartbeat_at = T0 - timedelta(seconds=9000)
    db_session.commit()

    evaluation = evaluate_execution_ownership(
        execution,
        now=T0,
        stale_after_seconds=2100,
        current_hostname=socket.gethostname(),
    )
    assert evaluation.stale_threshold_seconds == 2100
    assert evaluation.ownership_classification != "TERMINAL_EXECUTION"


# ---------------------------------------------------------------------------
# E3-R14  every worker-visible terminal Planning branch transfers ownership
# ---------------------------------------------------------------------------


def test_e3_r14_no_planning_result_branch_returns_without_reconciliation():
    import app.tasks.worker as worker_module

    source = Path(inspect.getsourcefile(worker_module)).read_text()
    tree = ast.parse(source)

    returns_planning_result = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Return) and isinstance(node.value, ast.Name):
            if node.value.id == "planning_phase_result":
                returns_planning_result.append(node.lineno)
    assert returns_planning_result == [], (
        "a terminal Planning result is returned without lifecycle reconciliation "
        f"at worker.py lines {returns_planning_result}"
    )

    # The discovery-terminal branch must raise the typed handoff.
    assert "terminal_attempt_handoff_from_planning_result(" in source
    assert "raise terminal_attempt_handoff_from_planning_result(" in source


# ---------------------------------------------------------------------------
# E3-R15  exact execution mismatch
# ---------------------------------------------------------------------------


def test_e3_r15_stale_execution_cannot_reconcile_current_attempt(
    db_session, tmp_path, monkeypatch
):
    project, session, task, link, execution = _seed(db_session, tmp_path)
    ctx = _ctx(db_session, project, session, task, link, execution)
    _provider_free_failure_setup(monkeypatch)

    _commit_attempt_evidence(
        ctx,
        failure_type="discovery_output_not_json",
        failure_reason="read_only_discovery_failed_closed",
    )
    handoff = _handoff(session, execution)

    # Same Session identity and generation, but a newer attempt is current.
    current = TaskExecution(
        session=session,
        task=task,
        attempt_number=2,
        status=TaskStatus.RUNNING,
        worker_pid=606060,
        worker_hostname="current-host",
        worker_process_start_identity="current-process-start",
    )
    db_session.add(current)
    session.status = "running"
    session.is_active = True
    db_session.commit()
    current_id = current.id
    generation = session.instance_id

    with pytest.raises(TerminalAttemptHandoffError):
        FailureCoordinator().handle_failure(
            self_task=_RetryTask(),
            ctx=ctx,
            exc=handoff,
            **_failure_kwargs(link),
        )

    db_session.expire_all()
    session = db_session.query(SessionModel).filter_by(id=session.id).one()
    current = db_session.query(TaskExecution).filter_by(id=current_id).one()
    assert session.status == "running"
    assert session.is_active is True
    assert session.instance_id == generation
    assert current.status == TaskStatus.RUNNING
    assert current.worker_pid == 606060
    assert current.worker_process_start_identity == "current-process-start"


def test_e3_r15_lifecycle_fence_rejects_stale_execution_directly(db_session, tmp_path):
    _project, session, task, _link, execution = _seed(db_session, tmp_path)
    newer = TaskExecution(
        session=session, task=task, attempt_number=2, status=TaskStatus.RUNNING
    )
    db_session.add(newer)
    db_session.commit()

    with pytest.raises(LifecycleTransitionError) as raised:
        finalize_logical_failure(
            db_session,
            session,
            task_execution=execution,
            failure_reason="stale",
            expected_instance_id=session.instance_id,
            expected_task_execution_id=execution.id,
            commit=True,
        )
    assert raised.value.reason == "superseded_task_execution"
    db_session.rollback()
    db_session.refresh(session)
    assert session.status == "running"


def test_e3_r15_lifecycle_fence_rejects_stale_generation_directly(db_session, tmp_path):
    _project, session, task, _link, execution = _seed(db_session, tmp_path)

    with pytest.raises(LifecycleTransitionError) as raised:
        finalize_logical_failure(
            db_session,
            session,
            task_execution=execution,
            failure_reason="stale",
            expected_instance_id="er4-generation-0",
            expected_task_execution_id=execution.id,
            commit=True,
        )
    assert raised.value.reason == "stale_session_generation"
    db_session.rollback()
    db_session.refresh(session)
    assert session.status == "running"


# ---------------------------------------------------------------------------
# CA2 deterministic replay
# ---------------------------------------------------------------------------


def test_ca2_deterministic_replay_reaches_logical_closure(
    db_session, tmp_path, monkeypatch
):
    """CA2: discovery_output_not_json must not strand a running Session."""

    project, session, task, link, execution = _seed(db_session, tmp_path)
    ctx = _ctx(db_session, project, session, task, link, execution)
    ledger = _provider_free_failure_setup(monkeypatch)

    # Synthetic start state.
    assert session.status == "running" and session.is_active is True
    assert task.status == TaskStatus.RUNNING
    assert link.status == TaskStatus.RUNNING
    assert execution.status == TaskStatus.RUNNING
    assert session.continuation_task_id is None

    # Deterministic discovery seam: the malformed discovery result.
    from app.services.orchestration.planning.read_only_discovery import (
        fail_closed_discovery,
    )

    captured: dict = {}

    planning_result = fail_closed_discovery(
        ctx=SimpleNamespace(
            orchestration_state=SimpleNamespace(status=None, abort_reason=None),
            emit_live=lambda *_a, **_k: None,
            restore_workspace_snapshot_if_needed=None,
        ),
        reason="discovery_output_not_json",
        detail="planner discovery output was not valid JSON",
        aborted_status="aborted",
        emit_phase_event=lambda *_a, **_k: None,
        finalize_failure=lambda **kwargs: captured.update(kwargs),
    )
    assert planning_result["failure_category"] == "discovery_terminal_failure"
    assert planning_result["terminal_failure"] is True

    # Planning finalizer against the real rows.
    _commit_attempt_evidence(
        ctx,
        failure_type=planning_result["reason"],
        failure_reason="planner discovery output was not valid JSON",
    )
    db_session.refresh(session)
    db_session.refresh(task)
    db_session.refresh(link)
    db_session.refresh(execution)
    # Intermediate state required by ER4 section 25.
    assert task.status == TaskStatus.FAILED
    assert link.status == TaskStatus.FAILED
    assert execution.status == TaskStatus.FAILED
    assert session.status == "running"
    assert session.is_active is True
    assert session.continuation_task_id is None

    # Repaired worker handoff.
    fc_invocations = []
    original = FailureCoordinator.handle_failure

    def _counting(self, **kwargs):
        fc_invocations.append(1)
        return original(self, **kwargs)

    monkeypatch.setattr(FailureCoordinator, "handle_failure", _counting)

    with pytest.raises(TerminalAttemptHandoffError):
        FailureCoordinator().handle_failure(
            self_task=_RetryTask(),
            ctx=ctx,
            exc=terminal_attempt_handoff_from_planning_result(
                planning_result,
                session_id=session.id,
                task_execution_id=execution.id,
                expected_session_instance_id=session.instance_id,
            ),
            **_failure_kwargs(link),
        )

    db_session.expire_all()
    session = db_session.query(SessionModel).filter_by(id=session.id).one()
    task = db_session.query(Task).filter_by(id=task.id).one()
    link = db_session.query(SessionTask).filter_by(id=link.id).one()
    execution = db_session.query(TaskExecution).filter_by(id=execution.id).one()
    authority = _authority(db_session, session, task, execution)

    assert fc_invocations == [1]
    assert task.status == TaskStatus.FAILED
    assert link.status == TaskStatus.FAILED
    assert execution.status == TaskStatus.FAILED
    assert session.status == "failed"
    assert session.is_active is False
    assert session.continuation_task_id is None
    assert authority.logical_terminal is True
    assert authority.quiescent is True
    _assert_physical_release(execution)
    assert ledger.calls == []

    # The forbidden CA2 graph is no longer reachable.
    forbidden = (
        execution.status == TaskStatus.FAILED
        and session.status == "running"
        and session.continuation_task_id is None
    )
    assert forbidden is False
