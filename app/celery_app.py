"""
Celery tasks for executing OpenClaw sessions
"""

from celery import Celery
from .services.openclaw_executor import OpenClawExecutor, OpenClawConfig
from .config import settings

celery_app = Celery(
    "orchestrator",
    broker=settings.CELERY_BROKER_URL,
    backend=settings.CELERY_RESULT_BACKEND,
    include=[
        "app.tasks.worker",
        "app.tasks.scheduler",
        "app.tasks.github_tasks",
        "app.tasks.openclaw_tasks",
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


@celery_app.task(bind=True, max_retries=3)
async def execute_task_in_openclaw(
    self, task_id: int, description: str, requirements: str = None
):
    """
    Celery task to execute a task via OpenClaw

    This runs in the background and doesn't block the API
    """
    try:
        executor = OpenClawExecutor(OpenClawConfig())

        # Spawn session
        session_info = await executor.execute_task(
            task_id=str(task_id), description=description, requirements=requirements
        )

        # Monitor session (blocking, but in background worker)
        # NOTE: For long-running tasks, use async + websockets instead
        result = await executor.monitor_session(
            session_key=session_info["sessionKey"], task_id=str(task_id)
        )

        # Get final output
        output = await executor.get_session_output(session_info["sessionKey"])

        # Update orchestrator DB with final result
        # await db.update_task(task_id, {
        #     "status": "Done",
        #     "output": output,
        #     "completed_at": datetime.utcnow()
        # })

        return {
            "success": True,
            "task_id": task_id,
            "session_key": session_info["sessionKey"],
            "output": output,
        }

    except Exception as exc:
        # Retry with exponential backoff
        raise self.retry(exc=exc, countdown=60 * (2**self.request.retries))
    finally:
        await executor.close()


@celery_app.task
async def cancel_openclaw_task(session_key: str) -> bool:
    """
    Cancel a running OpenClaw session

    Args:
        session_key: OpenClaw session key to cancel

    Returns:
        True if cancelled successfully
    """
    try:
        executor = OpenClawExecutor(OpenClawConfig())
        result = await executor.cancel_session(session_key)
        return result
    finally:
        await executor.close()


@celery_app.task
async def get_openclaw_session_history(session_key: str) -> str:
    """
    Get the full history of an OpenClaw session

    Args:
        session_key: OpenClaw session key

    Returns:
        Full session history as string
    """
    try:
        executor = OpenClawExecutor(OpenClawConfig())
        return await executor.get_session_output(session_key)
    finally:
        await executor.close()
