"""Celery task registration for orchestration runtimes."""

# The system /usr/share/zoneinfo/UTC on this host contains America/Toronto data
# (corrupted tzdata). ZoneInfo('UTC') therefore reports EDT/EST offsets instead
# of +00:00, causing Celery's countdown→ETA conversion to schedule retries hours
# late (countdown=15 → ETA 4h in the future). Fix: prepend the Python tzdata
# package path to ZoneInfo's search path so 'UTC' resolves from the correct file.
# This must run before any Celery import touches ZoneInfo('UTC').
import importlib.resources as _ir
import zoneinfo as _zoneinfo
from datetime import timedelta

try:
    _tzdata_zoneinfo_path = str(_ir.files("tzdata").joinpath("zoneinfo"))
    _zoneinfo.reset_tzpath([_tzdata_zoneinfo_path] + list(_zoneinfo.TZPATH))
    _zoneinfo.ZoneInfo.clear_cache()
except Exception:
    pass  # tzdata package unavailable; system files used as-is

from datetime import UTC, datetime

from celery import Celery
from celery.signals import after_task_publish, before_task_publish, worker_process_init
from .config import settings

celery_app = Celery(
    "orchestrator",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.tasks.worker",
        "app.tasks.maintenance",
        "app.tasks.github_tasks",
        "app.tasks.planning_tasks",
    ],
)

celery_app.conf.update(
    task_default_queue=settings.CELERY_QUEUE_NAME,
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    task_reject_on_worker_lost=True,
    task_acks_on_failure_or_timeout=True,
    worker_prefetch_multiplier=1,
    beat_schedule={
        "cleanup-old-logs": {
            "task": "app.tasks.maintenance.cleanup_old_logs",
            "schedule": timedelta(days=1),
            "kwargs": {"days": 30},
        },
        "recover-orphaned-running-sessions": {
            "task": "app.tasks.maintenance.sweep_orphaned_running_sessions",
            "schedule": timedelta(minutes=15),
            "kwargs": {},
        },
        # E8: restore transport for durable continuations whose broker
        # delivery was lost after the retry_pending commit.
        "reconcile-stranded-continuation-deliveries": {
            "task": "app.tasks.maintenance.sweep_stranded_continuation_deliveries",
            "schedule": timedelta(minutes=5),
            "kwargs": {},
        },
    },
)

# Ensure tasks are registered when workers start with `-A app.celery_app worker`.
celery_app.autodiscover_tasks(["app.tasks"])


CONTROLLED_EXECUTION_TASK_NAME = "app.tasks.worker.execute_orchestration_task"


def _require_controlled_worker_identity_for_publish(
    *, sender: str | None, body: object
) -> None:
    """Reject controlled task publication without its fresh Worker binding."""

    if (
        not settings.CONTROLLED_WORKER_IDENTITY_REQUIRED
        or sender != CONTROLLED_EXECUTION_TASK_NAME
    ):
        return
    if not isinstance(body, (tuple, list)) or len(body) < 2:
        raise RuntimeError("controlled Worker identity dispatch body is unavailable")
    task_kwargs = body[1]
    if not isinstance(task_kwargs, dict):
        raise RuntimeError("controlled Worker identity dispatch arguments are invalid")
    from app.services.orchestration.controlled_worker_identity import (
        CONTROLLED_WORKER_IDENTITY_CONTEXT_KEY,
        ControlledWorkerIdentityError,
        current_controlled_worker_identity_binding,
    )

    context_value = task_kwargs.get("context")
    if context_value is not None and not isinstance(context_value, dict):
        raise RuntimeError("controlled Worker identity dispatch context is invalid")
    context = dict(context_value or {})
    expected_binding = context.get(CONTROLLED_WORKER_IDENTITY_CONTEXT_KEY)
    if expected_binding is not None and not isinstance(expected_binding, dict):
        raise RuntimeError("controlled Worker identity dispatch binding is invalid")
    try:
        binding = current_controlled_worker_identity_binding(
            source_root=settings.WORKER_SOURCE_ROOT,
            runtime_root=settings.RUNTIME_ROOT,
            pin_path=settings.WORKER_CANDIDATE_PIN_PATH,
            pin_protected_root=(
                settings.WORKER_CANDIDATE_PIN_PROTECTED_ROOT or settings.RUNTIME_ROOT
            ),
            expected_node_identity=settings.CONTROLLED_WORKER_NODE_IDENTITY,
            expected_binding=expected_binding,
        )
    except ControlledWorkerIdentityError as exc:
        raise RuntimeError(
            f"controlled Worker identity dispatch refused: {exc}"
        ) from exc
    context[CONTROLLED_WORKER_IDENTITY_CONTEXT_KEY] = binding
    task_kwargs["context"] = context


@before_task_publish.connect
def _verify_controlled_worker_identity_before_publish(
    sender=None, body=None, **_kwargs
) -> None:
    _require_controlled_worker_identity_for_publish(sender=sender, body=body)


@worker_process_init.connect
def _install_subprocess_lifecycle_sigterm_handler(**_kwargs) -> None:
    """Phase 23D-3: ensure a forced SIGTERM to this worker process kills any
    OpenClaw CLI child process group it spawned, instead of orphaning it."""
    from app.services.agents.subprocess_lifecycle import install_sigterm_handler

    install_sigterm_handler()


@after_task_publish.connect
def _record_orphan_sweep_dispatch(sender=None, headers=None, **_kwargs) -> None:
    """Persist Beat/publisher dispatch evidence for the canonical sweep."""

    from app.services.observability.maintenance_observability import (
        CONTINUATION_SWEEP_SCHEDULE_ID,
        CONTINUATION_SWEEP_TASK_NAME,
        MAINTENANCE_DISPATCHED,
        ORPHAN_SWEEP_SCHEDULE_ID,
        ORPHAN_SWEEP_TASK_NAME,
        record_maintenance_event,
    )

    schedule_ids = {
        ORPHAN_SWEEP_TASK_NAME: ORPHAN_SWEEP_SCHEDULE_ID,
        CONTINUATION_SWEEP_TASK_NAME: CONTINUATION_SWEEP_SCHEDULE_ID,
    }
    if sender not in schedule_ids:
        return
    headers = headers or {}
    invocation_id = str(headers.get("id") or "")
    if not invocation_id:
        return
    observed_at = datetime.now(UTC)
    scheduled_at = None
    raw_eta = headers.get("eta")
    if raw_eta:
        try:
            scheduled_at = datetime.fromisoformat(str(raw_eta).replace("Z", "+00:00"))
        except ValueError:
            scheduled_at = None
    dispatch_source = str(headers.get("periodic_task_name") or "celery_publisher")
    db = None
    try:
        from app.database import get_db_session

        db = get_db_session()
        record_maintenance_event(
            db,
            event_type=MAINTENANCE_DISPATCHED,
            invocation_id=invocation_id,
            observed_at=observed_at,
            task_name=sender,
            schedule_identity=(
                dispatch_source
                if dispatch_source != "celery_publisher"
                else schedule_ids[sender]
            ),
            dispatch_source=dispatch_source,
            scheduled_at=scheduled_at or observed_at,
        )
        db.commit()
    except Exception:
        if db is not None:
            db.rollback()
        # Dispatch evidence must never break task publication.
        return
    finally:
        if db is not None:
            db.close()
