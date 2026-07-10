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

from celery import Celery
from celery.signals import worker_process_init
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
    task_default_queue="celery",
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
    },
)

# Ensure tasks are registered when workers start with `-A app.celery_app worker`.
celery_app.autodiscover_tasks(["app.tasks"])


@worker_process_init.connect
def _install_subprocess_lifecycle_sigterm_handler(**_kwargs) -> None:
    """Phase 23D-3: ensure a forced SIGTERM to this worker process kills any
    OpenClaw CLI child process group it spawned, instead of orphaning it."""
    from app.services.agents.subprocess_lifecycle import install_sigterm_handler

    install_sigterm_handler()
