"""
Celery tasks for executing OpenClaw sessions
"""

from celery import Celery
from .config import settings

celery_app = Celery(
    "orchestrator",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.tasks.worker",
        "app.tasks.scheduler",
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
)

# Ensure tasks are registered when workers start with `-A app.celery_app worker`.
celery_app.autodiscover_tasks(["app.tasks"], force=True)
