"""Provider-free Celery worker app for the ER4 terminal-handoff certification.

Like the BR2 certification worker, this module defines its OWN Celery
application instead of reusing ``app.celery_app``.  The production app declares
``include=["app.tasks.worker", ...]``, so a worker started from it would
register the real orchestration task and run planning — and therefore a
provider — as soon as a delivery arrived.

This app registers exactly one task, under the production task name.  The task
replays the repaired ER4 handoff against the real database and the real
FailureCoordinator/lifecycle transitions.  The deterministic Planning result is
injected; no planning, repair, reflection, digest or completion provider is
reachable, and the provider-capable seams the coordinator can enter are
explicitly blocked by a sentinel that fails the run if they are touched.
"""

from __future__ import annotations

import json
import logging
import os
import socket
import sys
from pathlib import Path
from types import SimpleNamespace

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from celery import Celery  # noqa: E402

ORCHESTRATION_TASK_NAME = "app.tasks.worker.execute_orchestration_task"
HANDOFF_EVIDENCE_MESSAGE = "ER4 certification terminal handoff"

_broker = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/9")

# No `include`: nothing from app.tasks is registered in this worker.
app = Celery("er4_certification", broker=_broker, backend=_broker)
app.conf.update(
    task_default_queue="er4_certification",
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)


PROVIDER_LANES = (
    "planning_initial",
    "planning_repair",
    "execution",
    "failure_reflection",
    "candidate_completion_repair",
    "completion_summary",
    "digest",
    "debug_repair",
    "grounding_discovery",
)


def _install_provider_sentinel(ledger: list[str]) -> None:
    """Fail closed if any provider-capable lane is entered."""

    import app.services.orchestration.coordinators.failure_coordinator as fc
    import app.services.orchestration.phases.failure_flow as ff
    from app.services.orchestration.recovery.recovery_strategy_registry import (
        RecoveryStrategyRegistry,
    )

    def _blocked(lane: str):
        def _raise(*_args, **_kwargs):
            ledger.append(lane)
            raise AssertionError(f"provider lane entered: {lane}")

        return _raise

    fc._invoke_reflection_prompt = _blocked("failure_reflection")
    ff._prepare_retry_workspace = lambda **_kwargs: (True, {}, False)
    ff._apply_knowledge_halt = lambda **_kwargs: False
    ff.record_failure_knowledge_for_stopped_session = lambda **_kwargs: True
    RecoveryStrategyRegistry.route = staticmethod(
        lambda *_a, **_k: SimpleNamespace(strategy="continue")
    )


def _context(db, session, task, link, execution, *, should_retry: bool):
    from app.services.orchestration.types import OrchestrationRunContext

    return OrchestrationRunContext(
        db=db,
        session=session,
        project=session.project,
        task=task,
        session_task_link=link,
        session_id=session.id,
        task_id=task.id,
        prompt=task.description or "",
        timeout_seconds=300,
        execution_profile="full_lifecycle",
        validation_profile="implementation",
        runs_in_canonical_baseline=False,
        orchestration_state=None,
        runtime_service=None,
        task_service=None,
        logger=logging.getLogger("er4_certification"),
        emit_live=lambda *_a, **_k: None,
        error_handler=SimpleNamespace(should_retry=lambda _e, _s: should_retry),
        restore_workspace_snapshot_if_needed=None,
        task_execution_id=execution.id,
    )


@app.task(bind=True, name=ORCHESTRATION_TASK_NAME, max_retries=3)
def execute_orchestration_task(self, **kwargs):
    """Replay the repaired terminal/recoverable Planning handoff for real.

    ``mode='terminal'`` injects a discovery terminal Planning result.
    ``mode='recoverable'`` injects an ordinary retryable Planning failure and
    deliberately loses the publication that should follow the durable marker,
    so the resulting ``retry_pending`` + valid marker state is the BR2-owned
    graph.  In both cases the real FailureCoordinator owns the lifecycle
    outcome; this task never decides terminality or recovery itself.
    """

    from app.database import get_db_session
    from app.models import (
        LogEntry,
        Session as SessionModel,
        SessionTask,
        Task,
        TaskExecution,
    )
    from app.services.orchestration.coordinators.failure_coordinator import (
        FailureCoordinator,
    )
    from app.services.orchestration.lifecycle.terminal_handoff import (
        TerminalAttemptHandoffError,
        terminal_attempt_handoff_from_planning_result,
    )
    from app.services.orchestration.run_state import mark_task_attempt_failed

    ledger: list[str] = []
    _install_provider_sentinel(ledger)

    mode = kwargs.get("mode") or "terminal"
    session_id = kwargs.get("session_id")
    task_id = kwargs.get("task_id")
    task_execution_id = kwargs.get("task_execution_id")
    expected_instance_id = kwargs.get("expected_session_instance_id")

    db = get_db_session()
    try:
        session = db.query(SessionModel).filter(SessionModel.id == session_id).one()
        task = db.query(Task).filter(Task.id == task_id).one()
        link = (
            db.query(SessionTask)
            .filter(
                SessionTask.session_id == session_id, SessionTask.task_id == task_id
            )
            .order_by(SessionTask.id.desc())
            .first()
        )
        execution = (
            db.query(TaskExecution).filter(TaskExecution.id == task_execution_id).one()
        )

        # --- Planning finalizer equivalent: durable attempt evidence only ---
        mark_task_attempt_failed(
            task=task,
            session_task_link=link,
            task_execution=execution,
            error_message=kwargs.get("failure_reason") or "discovery_output_not_json",
        )
        db.commit()
        intermediate = {
            "task": str(task.status),
            "session_task": str(link.status) if link else None,
            "task_execution": str(execution.status),
            "session": session.status,
            "continuation_task_id": session.continuation_task_id,
        }

        ctx = _context(
            db,
            session,
            task,
            link,
            execution,
            should_retry=(mode == "recoverable"),
        )
        handoff_kwargs = {
            "get_latest_session_task_link_fn": lambda *_a, **_k: link,
            "write_project_state_snapshot_fn": lambda *_a, **_k: None,
            "save_orchestration_checkpoint_fn": lambda *_a, **_k: None,
            "record_live_log_fn": lambda *_a, **_k: None,
        }

        if mode == "terminal":
            exc = terminal_attempt_handoff_from_planning_result(
                {
                    "status": "failed",
                    "reason": kwargs.get("failure_reason")
                    or "discovery_output_not_json",
                    "failure_category": "discovery_terminal_failure",
                    "terminal_failure": True,
                },
                session_id=session_id,
                task_execution_id=task_execution_id,
                expected_session_instance_id=expected_instance_id,
            )
        else:
            exc = RuntimeError(
                kwargs.get("failure_reason")
                or "planning_json_error: retryable planning failure"
            )

        outcome = {"mode": mode, "intermediate": intermediate, "provider_calls": 0}
        # For the recoverable case the publication that should follow the
        # durable continuation marker is deliberately lost, which is the
        # BR2-owned graph.  The marker must already be committed at that point.
        self_task = self if mode == "terminal" else _LostPublicationTask(self)
        try:
            FailureCoordinator().handle_failure(
                self_task=self_task, ctx=ctx, exc=exc, **handoff_kwargs
            )
            outcome["raised"] = None
        except TerminalAttemptHandoffError as handoff_error:
            outcome["raised"] = "TerminalAttemptHandoffError"
            outcome["failure_category"] = handoff_error.failure_category
            outcome["provider_calls"] = len(ledger)
            _record(db, session_id, task_id, task_execution_id, outcome, LogEntry)
            # Celery must observe FAILURE for a terminal Product outcome.
            raise
        except Exception as other:  # noqa: BLE001
            from celery.exceptions import Retry as CeleryRetry

            outcome["raised"] = type(other).__name__
            outcome["provider_calls"] = len(ledger)
            _record(db, session_id, task_id, task_execution_id, outcome, LogEntry)
            if isinstance(other, CeleryRetry):
                raise
            raise

        outcome["provider_calls"] = len(ledger)
        _record(db, session_id, task_id, task_execution_id, outcome, LogEntry)
        return outcome
    finally:
        db.close()


class _LostPublicationFailure(Exception):
    """The retry publication that should have followed the marker is lost."""


class _LostPublicationTask:
    """Celery task facade whose retry publication deliberately fails."""

    max_retries = 3
    default_retry_delay = 0

    def __init__(self, real_task):
        self.request = real_task.request
        self.retry_calls = 0

    def retry(self, exc=None, **_kwargs):
        self.retry_calls += 1
        raise _LostPublicationFailure(str(exc))


def _record(db, session_id, task_id, task_execution_id, payload, LogEntry) -> None:
    try:
        db.rollback()
        db.add(
            LogEntry(
                session_id=session_id,
                task_id=task_id,
                task_execution_id=task_execution_id,
                level="INFO",
                message=HANDOFF_EVIDENCE_MESSAGE,
                log_metadata=json.dumps(payload, default=str, sort_keys=True),
            )
        )
        db.commit()
    except Exception:
        db.rollback()


_ = socket
