"""Celery configuration for task queue"""

import os
from celery import Celery
from app.config import settings

# Create Celery app
celery_app = Celery(
    "orchestrator",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.tasks.worker",
        "app.tasks.openclaw_tasks",
        "app.tasks.github_tasks",
    ],
)

# Set configuration from settings
celery_app.conf.update(
    # Broker settings
    broker_url=settings.CELERY_BROKER_URL,
    result_backend=settings.CELERY_RESULT_BACKEND,
    
    # Task settings
    task_serializer="json",
    result_serializer="json",
    accept_content=["json"],
    timezone="UTC",
    enable_utc=True,
    
    # Task routing
    task_routes={
        "app.tasks.worker.*": {"queue": "default"},
        "app.tasks.openclaw_tasks.*": {"queue": "openclaw"},
        "app.tasks.github_tasks.*": {"queue": "github"},
    },
    
    # Concurrency per queue (Celery 5.x+ format)
    worker_concurrency=4,  # Default concurrency for all queues
    
    # Timeouts and retries
    task_time_limit=3600,  # 1 hour max per task
    task_soft_time_limit=3000,  # 50 minute soft timeout
    worker_prefetch_multiplier=1,  # One task at a time
    
    # Retry configuration
    task_reject_on_worker_lost=True,
    task_acks_late=True,
    
    # Monitoring
    worker_send_task_events=True,
    task_send_sent_event=True,
)

# Load config from environment if available
if os.environ.get("CELERY_BROKER_URL"):
    celery_app.conf.broker_url = os.environ["CELERY_BROKER_URL"]
if os.environ.get("CELERY_RESULT_BACKEND"):
    celery_app.conf.result_backend = os.environ["CELERY_RESULT_BACKEND"]
