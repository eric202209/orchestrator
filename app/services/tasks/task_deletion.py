"""Deletion of the task-owned runtime evidence graph."""

from collections.abc import Iterable

from sqlalchemy import or_
from sqlalchemy.orm import Session

from app.models import (
    HumanGuidance,
    HumanGuidanceRevision,
    HumanGuidanceUsage,
    InterventionRequest,
    KnowledgeUsageLog,
    LogEntry,
    PermissionRequest,
    SessionTask,
    Task,
    TaskCheckpoint,
    TaskExecution,
    TaskExecutionChangeSet,
)


def delete_task_owned_graph(db: Session, task_ids: Iterable[int]) -> None:
    """Delete task-owned runtime evidence in foreign-key dependency order."""
    task_ids = list(set(task_ids))
    if not task_ids:
        return

    execution_ids = [
        execution_id
        for (execution_id,) in db.query(TaskExecution.id)
        .filter(TaskExecution.task_id.in_(task_ids))
        .all()
    ]
    guidance_ids = [
        guidance_id
        for (guidance_id,) in db.query(HumanGuidance.id)
        .filter(HumanGuidance.task_id.in_(task_ids))
        .all()
    ]

    log_filter = LogEntry.task_id.in_(task_ids)
    if execution_ids:
        log_filter = or_(log_filter, LogEntry.task_execution_id.in_(execution_ids))
    db.query(LogEntry).filter(log_filter).delete(synchronize_session=False)

    db.query(TaskExecutionChangeSet).filter(
        TaskExecutionChangeSet.task_id.in_(task_ids)
    ).delete(synchronize_session=False)
    if execution_ids:
        db.query(TaskExecution).filter(TaskExecution.id.in_(execution_ids)).delete(
            synchronize_session=False
        )

    db.query(SessionTask).filter(SessionTask.task_id.in_(task_ids)).delete(
        synchronize_session=False
    )
    db.query(TaskCheckpoint).filter(TaskCheckpoint.task_id.in_(task_ids)).delete(
        synchronize_session=False
    )
    db.query(PermissionRequest).filter(PermissionRequest.task_id.in_(task_ids)).delete(
        synchronize_session=False
    )
    db.query(InterventionRequest).filter(
        InterventionRequest.task_id.in_(task_ids)
    ).delete(synchronize_session=False)
    db.query(HumanGuidanceUsage).filter(
        HumanGuidanceUsage.task_id.in_(task_ids)
    ).delete(synchronize_session=False)
    if guidance_ids:
        db.query(HumanGuidanceRevision).filter(
            HumanGuidanceRevision.guidance_id.in_(guidance_ids)
        ).delete(synchronize_session=False)
    db.query(HumanGuidance).filter(HumanGuidance.task_id.in_(task_ids)).delete(
        synchronize_session=False
    )
    db.query(KnowledgeUsageLog).filter(KnowledgeUsageLog.task_id.in_(task_ids)).delete(
        synchronize_session=False
    )
    db.query(Task).filter(Task.id.in_(task_ids)).delete(synchronize_session=False)
