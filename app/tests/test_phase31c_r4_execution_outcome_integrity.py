"""Phase 31C-R4 evidence and outcome-integrity regressions."""

from __future__ import annotations

import logging
from types import SimpleNamespace
from unittest.mock import MagicMock

import pytest

from app.models import (
    Project,
    Session as SessionModel,
    SessionTask,
    Task,
    TaskExecution,
)
from app.models import TaskStatus
from app.services.orchestration.coordinators.failure_coordinator import (
    FailureCoordinator,
)
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.types import OrchestrationRunContext


def _noop(*_args, **_kwargs):
    return None


class _TerminalSelfTask:
    max_retries = 0

    class request:
        retries = 0

    def retry(self, exc, **kwargs):
        raise AssertionError("unexpected Celery retry")


def _seed_context(db_session, *, execution_mode="automatic"):
    project = Project(name="R4 Project", workspace_path="/tmp/r4-project")
    db_session.add(project)
    db_session.flush()
    session = SessionModel(
        project_id=project.id,
        name="R4 Session",
        status="running",
        execution_mode=execution_mode,
        is_active=True,
    )
    task = Task(
        project_id=project.id,
        title="R4 Task",
        status=TaskStatus.RUNNING,
        task_subfolder="task-r4",
        plan_position=1,
    )
    db_session.add_all([session, task])
    db_session.flush()
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
        prompt="test task",
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="implementation",
        runs_in_canonical_baseline=True,
        orchestration_state=SimpleNamespace(
            project_dir="/tmp/r4-project",
            status=SimpleNamespace(value="executing"),
            current_phase=SimpleNamespace(value="task_summary"),
            phase_history=[],
        ),
        runtime_service=None,
        task_service=None,
        logger=logging.getLogger("r4-test"),
        emit_live=_noop,
        error_handler=SimpleNamespace(should_retry=lambda *_args: False),
        task_execution_id=execution.id,
    )
    return ctx, session, task, execution


def test_failure_evidence_is_captured_before_automatic_recovery_return(db_session):
    ctx, _session, _task, execution = _seed_context(db_session)
    evidence_logs = []
    queue_fn = MagicMock()

    def capture_log(*args, **kwargs):
        evidence_logs.append((args, kwargs))

    result = FailureCoordinator().handle_failure(
        self_task=_TerminalSelfTask(),
        ctx=ctx,
        exc=RuntimeError("post-QA capacity-shaped application failure"),
        get_latest_session_task_link_fn=lambda *_args, **_kwargs: ctx.session_task_link,
        write_project_state_snapshot_fn=_noop,
        save_orchestration_checkpoint_fn=_noop,
        record_live_log_fn=capture_log,
        queue_task_for_session_fn=queue_fn,
    )

    assert result is None
    queue_fn.assert_called_once()
    evidence = next(
        kwargs["metadata"]
        for _args, kwargs in evidence_logs
        if kwargs.get("metadata", {}).get("traceback")
    )
    assert evidence["exception_type"] == "RuntimeError"
    assert evidence["exception_module"] == "builtins"
    assert (
        evidence["exception_message"] == "post-QA capacity-shaped application failure"
    )
    assert str(execution.id) == str(evidence["task_execution_id"])
    assert evidence["task_status_before_failure"] == TaskStatus.RUNNING.value
    assert evidence["automatic_recovery_eligible"] is True
    assert evidence["authoritative_success_recorded"] is False


def test_failure_evidence_persistence_failure_does_not_hide_original_exception(
    db_session, monkeypatch
):
    ctx, _session, _task, execution = _seed_context(db_session, execution_mode="manual")
    logger = MagicMock()
    ctx.logger = logger

    def event_persistence_failure(**_kwargs):
        raise OSError("database is locked while writing evidence")

    monkeypatch.setattr(
        "app.services.orchestration.phases.failure_flow.append_orchestration_event",
        event_persistence_failure,
    )

    def log_persistence_failure(*_args, **_kwargs):
        raise OSError("database is locked while writing LogEntry")

    original = RuntimeError("original post-QA failure")
    with pytest.raises(RuntimeError, match="original post-QA failure"):
        FailureCoordinator().handle_failure(
            self_task=_TerminalSelfTask(),
            ctx=ctx,
            exc=original,
            get_latest_session_task_link_fn=lambda *_args, **_kwargs: ctx.session_task_link,
            write_project_state_snapshot_fn=_noop,
            save_orchestration_checkpoint_fn=_noop,
            record_live_log_fn=log_persistence_failure,
        )

    assert any(
        "event persistence failed" in call.args[0] and call.args[2] == execution.id
        for call in logger.error.call_args_list
    )
    assert any(
        "durable LogEntry persistence failed" in call.args[0]
        and call.args[2] == execution.id
        for call in logger.error.call_args_list
    )


def test_outcome_checkpoint_has_required_identity_and_operation_fields():
    from app.services.orchestration.diagnostics.outcome_observability import (
        record_outcome_checkpoint,
    )

    ctx = SimpleNamespace(
        task_execution_id=42,
        task_id=7,
        session_id=8,
        task=SimpleNamespace(status=TaskStatus.DONE),
        session_task_link=SimpleNamespace(status=TaskStatus.DONE),
        db=None,
        orchestration_state=SimpleNamespace(
            project_dir="/tmp/r4-checkpoint",
            current_phase=None,
            phase_history=[],
        ),
    )
    append_event = MagicMock()

    logger = MagicMock()
    record_outcome_checkpoint(
        ctx=ctx,
        operation="authoritative_success_committed",
        status="success",
        append_orchestration_event_fn=append_event,
        logger=logger,
    )

    details = append_event.call_args.kwargs["details"]
    assert append_event.call_args.kwargs["event_type"] == EventType.OUTCOME_CHECKPOINT
    assert details["operation"] == "authoritative_success_committed"
    assert details["task_execution_id"] == 42
    assert details["task_status"] == TaskStatus.DONE.value
    assert details["session_task_status"] == TaskStatus.DONE.value
    assert details["orchestration_phase"] == "task_summary"
    assert logger.info.call_args.args[0] == "[R4_OUTCOME_CHECKPOINT] %s"
