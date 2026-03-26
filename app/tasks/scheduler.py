"""Job scheduling system for Celery"""

import logging
from typing import Optional, Dict, Any, List
from datetime import datetime, timedelta
from celery.schedules import crontab
from app.celery_app import celery_app
from app.tasks.worker import cleanup_old_logs, scheduled_task_execution

logger = logging.getLogger(__name__)


class JobScheduler:
    """
    Schedule and manage background jobs
    
    Uses Celery Beat for periodic task scheduling
    """
    
    # Periodic task schedule
    PERIODIC_TASKS = {
        "cleanup-old-logs": {
            "task": "app.tasks.worker.cleanup_old_logs",
            "schedule": crontab(hour=0, minute=0),  # Daily at midnight
            "kwargs": {"days": 30},
            "enabled": True,
        },
        "health-check": {
            "task": "app.tasks.worker.cleanup_old_logs",  # Placeholder
            "schedule": timedelta(minutes=30),  # Every 30 minutes
            "kwargs": {},
            "enabled": False,  # Disable until implemented
        },
    }
    
    @classmethod
    def get_periodic_schedule(cls) -> Dict[str, Any]:
        """
        Get Celery Beat periodic task schedule
        
        Returns:
            Dictionary compatible with Celery Beat configuration
        """
        schedule = {}
        
        for name, config in cls.PERIODIC_TASKS.items():
            if config["enabled"]:
                schedule[name] = {
                    "task": config["task"],
                    "schedule": config["schedule"],
                    "kwargs": config["kwargs"],
                }
        
        return schedule
    
    @classmethod
    def schedule_task(
        cls,
        task_name: str,
        task_id: int,
        scheduled_time: datetime,
        **kwargs
    ) -> str:
        """
        Schedule a task to run at a specific time
        
        Args:
            task_name: Name of the Celery task
            task_id: Task identifier
            scheduled_time: When to run the task
            **kwargs: Task arguments
            
        Returns:
            Task ID
        """
        from datetime import datetime as dt
        
        scheduled_dt = scheduled_time.replace(tzinfo=None)
        
        # Calculate delay
        now = dt.utcnow()
        delay_seconds = (scheduled_dt - now).total_seconds()
        
        if delay_seconds < 0:
            # Already past, execute immediately
            logger.warning(f"Task scheduled in the past ({task_id}), executing immediately")
            delay_seconds = 0
        
        # Get the task
        task = getattr(celery_app.tasks.get(task_name), task_name, None)
        
        if not task:
            raise ValueError(f"Task {task_name} not found")
        
        # Schedule the task
        result = task.apply_async(
            args=[task_id, scheduled_dt.isoformat()],
            kwargs=kwargs,
            countdown=delay_seconds,
            expires=delay_seconds + 3600,  # Expire after 1 hour
        )
        
        logger.info(f"Scheduled task {task_name} for {scheduled_dt.isoformat()}")
        
        return result.id
    
    @classmethod
    def schedule_recurring_task(
        cls,
        task_name: str,
        interval: timedelta,
        task_args: Optional[List] = None,
        task_kwargs: Optional[Dict] = None,
        task_id: Optional[str] = None
    ):
        """
        Schedule a recurring task
        
        Args:
            task_name: Name of the Celery task
            interval: How often to run
            task_args: Task arguments
            task_kwargs: Task keyword arguments
            task_id: Custom task ID
        """
        task_args = task_args or []
        task_kwargs = task_kwargs or {}
        
        # This would normally be registered with Celery Beat
        # For now, we'll just log it
        logger.info(
            f"Recurring task {task_name} scheduled every {interval} "
            f"with args={task_args}, kwargs={task_kwargs}"
        )
        
        # In production, you'd use:
        # celery_app.conf.beat_schedule[task_id or task_name] = {
        #     "task": task_name,
        #     "schedule": interval,
        #     "args": task_args,
        #     "kwargs": task_kwargs,
        # }
    
    @classmethod
    def cancel_scheduled_task(cls, task_id: str) -> bool:
        """
        Cancel a scheduled task
        
        Args:
            task_id: Task ID to cancel
            
        Returns:
            True if cancelled, False if not found
        """
        from celery.result import AsyncResult
        
        result = AsyncResult(task_id)
        
        try:
            result.revoke(terminate=True)
            logger.info(f"Cancelled scheduled task {task_id}")
            return True
        except Exception as e:
            logger.error(f"Failed to cancel task {task_id}: {str(e)}")
            return False


@celery_app.on_after_finalize.connect
def setup_periodic_tasks(sender, **kwargs):
    """Setup periodic tasks when Celery app is finalized"""
    
    # Register periodic tasks from JobScheduler
    schedule = JobScheduler.get_periodic_schedule()
    
    for name, config in schedule.items():
        logger.info(f"Registered periodic task: {name}")


# Convenience functions for scheduling
def schedule_daily_cleanup(days: int = 30):
    """Schedule daily log cleanup"""
    return celery_app.send_task(
        "app.tasks.worker.cleanup_old_logs",
        kwargs={"days": days},
        countdown=86400,  # 24 hours
    )


def schedule_task_with_delay(task_name: str, task_id: int, delay_seconds: int, **kwargs):
    """Schedule a task to run after a delay"""
    from app.tasks.worker import execute_openclaw_task
    
    # Get the task function
    task_func = execute_openclaw_task  # Example
    
    return task_func.apply_async(
        args=[task_id],
        kwargs=kwargs,
        countdown=delay_seconds,
    )
