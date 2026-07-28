"""Maintenance Celery tasks: webhook processing, recovery sweeps, cleanup, report generation."""

import logging
import time
import uuid
from datetime import UTC, datetime, timezone, timedelta
from typing import Dict, Any, Optional

from app.celery_app import celery_app
from app.database import get_db_session
from app.models import Task, TaskStatus, LogEntry, Project
from app.services.orchestration import (
    build_task_report_payload as _build_task_report_payload,
    render_task_report as _render_task_report,
)
from app.services.orchestration.run_state import (
    mark_task_attempt_done,
    mark_task_attempt_running,
)
from app.services.observability.maintenance_observability import (
    MAINTENANCE_COMPLETED,
    MAINTENANCE_FAILED,
    MAINTENANCE_RECEIVED,
    MAINTENANCE_STARTED,
    build_sweep_result_counts,
    record_maintenance_event,
)

logger = logging.getLogger(__name__)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def process_github_webhook(
    self, webhook_data: Dict[str, Any], repo_owner: str, repo_name: str
):
    db = get_db_session()
    try:
        project = (
            db.query(Project)
            .filter(Project.github_url.ilike(f"%{repo_owner}/{repo_name}%"))
            .first()
        )
        if not project:
            project = Project(
                name=f"{repo_owner}/{repo_name}",
                github_url=f"https://github.com/{repo_owner}/{repo_name}",
                description="Auto-created from GitHub webhook",
            )
            db.add(project)
            db.commit()
            db.refresh(project)

        webhook_type = webhook_data.get("type", "Unknown")
        if webhook_type == "PushEvent":
            logger.info(f"Processing push event for {repo_owner}/{repo_name}")
        elif webhook_type == "PullRequestEvent":
            logger.info(f"Processing PR event for {repo_owner}/{repo_name}")
        elif webhook_type == "IssueEvent":
            logger.info(f"Processing issue event for {repo_owner}/{repo_name}")

        return {
            "status": "processed",
            "webhook_type": webhook_type,
            "project_id": project.id if project else None,
        }
    except Exception as exc:
        logger.error(f"Webhook processing failed: {str(exc)}")
        raise self.retry(exc=exc)
    finally:
        db.close()


@celery_app.task(bind=True, max_retries=5, default_retry_delay=30)
def scheduled_task_execution(self, task_id: int, scheduled_time: str, prompt: str):
    from datetime import datetime as dt

    db = None
    try:
        now = dt.utcnow()
        schedule_dt = dt.fromisoformat(scheduled_time.replace("Z", "+00:00"))
        if now < schedule_dt:
            delay_seconds = (schedule_dt - now).total_seconds()
            logger.info(
                f"Task {task_id} scheduled for later, retrying in {delay_seconds}s"
            )
            raise self.retry(countdown=delay_seconds)

        db = get_db_session()
        task = db.query(Task).filter(Task.id == task_id).first()
        if task:
            mark_task_attempt_running(task=task, started_at=dt.now(timezone.utc))
            db.commit()

        # TODO: Implement actual scheduled execution
        if task:
            mark_task_attempt_done(task=task, completed_at=dt.now(timezone.utc))
            db.commit()

        return {
            "status": "completed",
            "task_id": task_id,
            "executed_at": dt.utcnow().isoformat(),
        }
    except Exception as exc:
        logger.error(f"Scheduled task {task_id} failed: {str(exc)}")
        raise self.retry(exc=exc, max_retries=3)
    finally:
        if db is not None:
            db.close()


@celery_app.task(bind=True)
def sweep_orphaned_running_sessions(
    self, stale_after_seconds: int = 2100
) -> Dict[str, Any]:
    db = get_db_session()
    request = getattr(self, "request", None)
    invocation_id = str(getattr(request, "id", None) or uuid.uuid4())
    worker_identity = getattr(request, "hostname", None)
    started_monotonic = time.monotonic()

    def _record(event_type: str, **kwargs: Any) -> None:
        try:
            record_maintenance_event(
                db,
                event_type=event_type,
                invocation_id=invocation_id,
                worker_identity=worker_identity,
                **kwargs,
            )
            db.commit()
        except Exception as record_exc:
            db.rollback()
            logger.warning(
                "Maintenance observability record failed event=%s: %s",
                event_type,
                record_exc,
            )

    try:
        from app.services.session.session_lifecycle_service import (
            recover_stale_running_sessions,
        )

        _record(MAINTENANCE_RECEIVED, dispatch_source="celery_worker")
        _record(MAINTENANCE_STARTED)
        decision_records: list[dict[str, Any]] = []
        recovered = recover_stale_running_sessions(
            db,
            stale_after_seconds=stale_after_seconds,
            decision_records=decision_records,
        )
        counts = build_sweep_result_counts(
            decision_records, recovered_count=len(recovered)
        )
        status = "completed_with_errors" if counts["error_count"] else "completed"
        _record(
            MAINTENANCE_COMPLETED,
            counts=counts,
            duration_seconds=time.monotonic() - started_monotonic,
            decision_evidence=decision_records,
        )
        return {
            "status": status,
            **counts,
            "recovered_sessions": recovered,
        }
    except Exception as exc:
        _record(
            MAINTENANCE_FAILED,
            error_category=exc.__class__.__name__,
            error_timestamp=datetime.now(UTC),
            duration_seconds=time.monotonic() - started_monotonic,
        )
        logger.error("Orphaned running session sweep failed: %s", exc)
        raise self.retry(exc=exc, max_retries=3)
    finally:
        db.close()


@celery_app.task(bind=True)
def cleanup_old_logs(self, days: int = 30, session_id: Optional[int] = None):
    from datetime import datetime

    db = get_db_session()
    try:
        cutoff_date = datetime.now(timezone.utc) - timedelta(days=days)
        query = db.query(LogEntry).filter(LogEntry.created_at < cutoff_date)
        if session_id:
            query = query.filter(LogEntry.session_id == session_id)
        deleted_count = query.delete(synchronize_session=False)
        db.commit()
        logger.info(f"Deleted {deleted_count} old log entries")
        return {
            "status": "completed",
            "deleted_count": deleted_count,
            "days": days,
            "session_id": session_id,
        }
    except Exception as exc:
        logger.error(f"Log cleanup failed: {str(exc)}")
        raise self.retry(exc=exc, max_retries=3)
    finally:
        db.close()


@celery_app.task(bind=True)
def generate_task_report(self, task_id: int, output_format: str = "json"):
    try:
        db = get_db_session()
        report = _build_task_report_payload(db, task_id)
        return _render_task_report(report, output_format=output_format)
    except Exception as exc:
        logger.error(f"Report generation failed: {str(exc)}")
        raise self.retry(exc=exc, max_retries=3)
    finally:
        db.close()
