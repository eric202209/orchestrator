"""Claim-only Celery worker app for the BR2 provider-free certification.

This module deliberately defines its OWN Celery application instead of reusing
``app.celery_app``. The production app declares ``include=["app.tasks.worker",
...]``, so a worker started from it registers the real orchestration task and
would run planning — and therefore a provider — as soon as a delivery arrived.
A standalone app registers exactly one task, under the production task name, so
the broker message, worker loop, database and the real strict E2 continuation
claim are all exercised while no orchestration or provider work can start.
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[2]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

from celery import Celery  # noqa: E402

ORCHESTRATION_TASK_NAME = "app.tasks.worker.execute_orchestration_task"
CLAIM_EVIDENCE_MESSAGE = "BR2 certification strict claim"

_broker = os.environ.get("CELERY_BROKER_URL", "redis://localhost:6379/9")

# No `include`: nothing from app.tasks is registered in this worker.
app = Celery("br2_certification", broker=_broker, backend=_broker)
app.conf.update(
    task_default_queue="br2_certification",
    task_serializer="json",
    accept_content=["json"],
    result_serializer="json",
    timezone="UTC",
    enable_utc=True,
    task_acks_late=True,
    worker_prefetch_multiplier=1,
)


@app.task(bind=True, name=ORCHESTRATION_TASK_NAME, max_retries=0)
def execute_orchestration_task(self, **kwargs):
    """Run the real strict claim for a delivered continuation and record it."""

    from app.database import get_db_session
    from app.models import LogEntry
    from app.tasks.worker_support.dispatch import _claim_continuation_for_worker

    db = get_db_session()
    try:
        result = _claim_continuation_for_worker(
            db=db,
            session_id=kwargs.get("session_id"),
            task_id=kwargs.get("task_id"),
            instance_id=kwargs.get("expected_session_instance_id"),
            continuation_task_id=kwargs.get("continuation_task_id"),
            continuation_kind=kwargs.get("continuation_kind"),
            task_execution_id=kwargs.get("task_execution_id"),
            retry_count=kwargs.get("continuation_retry_count"),
        )
        payload = {
            "accepted": bool(result.accepted),
            "reason": result.reason,
            "celery_retries": int(getattr(self.request, "retries", 0) or 0),
            "task_execution_id": result.task_execution_id,
        }
        db.add(
            LogEntry(
                session_id=kwargs.get("session_id"),
                task_id=kwargs.get("task_id"),
                task_execution_id=kwargs.get("task_execution_id"),
                level="INFO",
                message=CLAIM_EVIDENCE_MESSAGE,
                log_metadata=json.dumps(payload, sort_keys=True),
            )
        )
        db.commit()
        return payload
    finally:
        db.close()
