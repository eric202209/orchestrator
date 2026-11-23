"""Celery task registration for orchestration runtimes."""

# The system /usr/share/zoneinfo/UTC on this host contains America/Toronto data
# (corrupted tzdata). ZoneInfo('UTC') therefore reports EDT/EST offsets instead
# of +00:00, causing Celery's countdown→ETA conversion to schedule retries hours
# late (countdown=15 → ETA 4h in the future). Fix: prepend the Python tzdata
# package path to ZoneInfo's search path so 'UTC' resolves from the correct file.
# This must run before any Celery import touches ZoneInfo('UTC').
import importlib.resources as _ir
import os
import zoneinfo as _zoneinfo
from datetime import timedelta

try:
    _tzdata_zoneinfo_path = str(_ir.files("tzdata").joinpath("zoneinfo"))
    _zoneinfo.reset_tzpath([_tzdata_zoneinfo_path] + list(_zoneinfo.TZPATH))
    _zoneinfo.ZoneInfo.clear_cache()
except Exception:
    pass  # tzdata package unavailable; system files used as-is

from datetime import UTC, datetime

from celery import Celery, Task
from celery.signals import (
    after_task_publish,
    before_task_publish,
    task_prerun,
    worker_process_init,
)
from .config import settings
from app.services.orchestration.worker_boundary import (
    WorkerBoundaryViolation,
    validate_disposable_task_admission,
    validate_structured_http_provider_contract,
    validate_worker_authority_paths,
)


def _disposable_worker_enabled() -> bool:
    return os.environ.get("WORKER_ISOLATION_MODE", "").strip() == "disposable"


def _disposable_worker_queue() -> str:
    return (
        os.environ.get("CELERY_QUEUE_NAME", "").strip()
        or str(settings.CELERY_QUEUE_NAME or "").strip()
    )


def _disposable_worker_task_allowlist() -> str:
    return os.environ.get("WORKER_TASK_ALLOWLIST", "")


def _validate_disposable_publication(
    *, task_name: str | None, queue_name: str | None, use_configured_queue: bool = True
) -> None:
    validate_disposable_task_admission(
        task_name=task_name,
        queue_name=(
            queue_name
            if queue_name is not None
            else (_disposable_worker_queue() if use_configured_queue else "")
        ),
        isolation_mode=os.environ.get("WORKER_ISOLATION_MODE"),
        task_allowlist=_disposable_worker_task_allowlist(),
    )


def _validate_disposable_runtime_contract() -> None:
    """Recheck mount/provider inputs after the Worker has booted."""

    if not _disposable_worker_enabled():
        return
    validate_structured_http_provider_contract(
        provider_name=os.environ.get("PLANNING_PROVIDER"),
        backend_values={
            name: os.environ.get(name)
            for name in (
                "AGENT_BACKEND",
                "AGENT_SECONDARY_BACKEND",
                "PLANNING_BACKEND",
                "EXECUTION_BACKEND",
                "DEBUG_REPAIR_BACKEND",
                "REPAIR_BACKEND",
                "COMPLETION_REPAIR_BACKEND",
            )
        },
    )
    validate_worker_authority_paths(
        application_root=os.environ.get("WORKER_APP_ROOT")
        or settings.WORKER_SOURCE_ROOT,
        authority_paths={
            name: os.environ.get(name, "")
            for name in (
                "WORKER_DATABASE_DIR",
                "WORKER_RUNTIME_ROOT",
                "WORKER_CHECKPOINT_DIR",
                "WORKER_IDENTITY_ROOT",
                "WORKSPACE_ROOT",
            )
        },
        model_workspace_root=os.environ.get("MODEL_WORKSPACE_ROOT"),
    )


def _require_fresh_disposable_worker_identity() -> None:
    """Reject a task before its body when the boot identity is stale/missing."""

    if not _disposable_worker_enabled():
        return
    if not settings.CONTROLLED_WORKER_IDENTITY_REQUIRED:
        raise WorkerBoundaryViolation(
            "disposable Worker requires controlled Worker identity admission"
        )
    from app.services.orchestration.controlled_worker_identity import (
        verify_controlled_worker_identity,
    )

    evidence = verify_controlled_worker_identity(
        source_root=settings.WORKER_SOURCE_ROOT,
        runtime_root=settings.RUNTIME_ROOT,
        pin_path=settings.WORKER_CANDIDATE_PIN_PATH,
        pin_protected_root=(
            settings.WORKER_CANDIDATE_PIN_PROTECTED_ROOT or settings.RUNTIME_ROOT
        ),
        expected_node_identity=settings.CONTROLLED_WORKER_NODE_IDENTITY,
        expected_pid=os.getpid(),
    )
    if not evidence.identity_verified:
        reason = (
            evidence.reasons[0] if evidence.reasons else "worker_identity_unverified"
        )
        raise WorkerBoundaryViolation(f"fresh Worker identity required: {reason}")


def _require_execution_enabled() -> None:
    if settings.DEV_API_ONLY:
        raise RuntimeError("DEVELOPMENT_EXECUTION_DISABLED")
    if not settings.EXECUTION_ENABLED:
        raise RuntimeError("EXECUTION_DISABLED")


class DevelopmentGatedTask(Task):
    def apply_async(self, *args, **kwargs):
        _require_execution_enabled()
        _validate_disposable_publication(
            task_name=self.name,
            queue_name=kwargs.get("queue")
            or kwargs.get("routing_key")
            or getattr(self, "queue", None),
        )
        return super().apply_async(*args, **kwargs)

    def apply(self, *args, **kwargs):
        _require_execution_enabled()
        return super().apply(*args, **kwargs)


class DevelopmentGatedCelery(Celery):
    def send_task(self, *args, **kwargs):
        # A before_task_publish signal cannot enforce this: Celery signals
        # can swallow receiver exceptions. Refuse before reaching transport.
        _require_execution_enabled()
        _validate_disposable_publication(
            task_name=args[0] if args else None,
            queue_name=kwargs.get("queue") or kwargs.get("routing_key"),
        )
        return super().send_task(*args, **kwargs)


celery_app = DevelopmentGatedCelery(
    "orchestrator",
    task_cls=DevelopmentGatedTask,
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


@before_task_publish.connect
def _verify_disposable_task_before_publish(
    sender=None, routing_key=None, headers=None, **_kwargs
) -> None:
    """Keep direct Celery publications inside the disposable task contract."""

    _validate_disposable_publication(
        task_name=sender,
        queue_name=routing_key
        or (headers or {}).get("queue")
        or _disposable_worker_queue(),
    )


@task_prerun.connect
def _admit_disposable_task_before_run(task=None, **_kwargs) -> None:
    """Reject a misrouted or stale task before entering TaskExecution code."""

    if not _disposable_worker_enabled():
        return
    request = getattr(task, "request", None)
    delivery_info = getattr(request, "delivery_info", None) or {}
    _validate_disposable_publication(
        task_name=getattr(task, "name", None),
        queue_name=delivery_info.get("routing_key") or delivery_info.get("queue"),
        use_configured_queue=False,
    )
    _validate_disposable_runtime_contract()
    _require_fresh_disposable_worker_identity()


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
