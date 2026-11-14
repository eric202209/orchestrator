"""Deletion of the task-owned runtime evidence graph."""

from collections.abc import Iterable

from fastapi import HTTPException
from sqlalchemy import or_, update
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
    TaskStatus,
    Session as SessionModel,
)
from app.services.execution.workflow_lifecycle import (
    require_no_retained_execution_evidence,
    DISPATCH_PENDING_BINDING,
    DISPATCH_READY,
    DISPATCH_CLAIMED,
)
from app.services.orchestration.state.session_state import (
    SessionStatus,
    normalize_session_status,
)


def require_safe_task_deletion(db: Session, task_ids: Iterable[int]) -> None:
    """Hold the DB writer reservation until the caller commits or rolls back.

    A no-op UPDATE works within an existing transaction: SQLite serializes all
    writers, including dispatch/claim writers. Do not rely on cached ORM state.
    Other dialects lock the selected Task rows; admission is currently SQLite.
    """
    task_ids = list(set(task_ids))
    if not task_ids:
        return
    db.execute(
        update(Task)
        .where(Task.id.in_(task_ids))
        .values(id=Task.id, updated_at=Task.updated_at)
        .execution_options(synchronize_session=False)
    )
    # Keep retained-evidence refusal semantics even when execution is active.
    require_no_retained_execution_evidence(db, task_ids=task_ids)
    running = (
        db.query(Task.id)
        .filter(Task.id.in_(task_ids), Task.status == TaskStatus.RUNNING)
        .first()
    )
    active_execution = (
        db.query(TaskExecution.id)
        .filter(
            TaskExecution.task_id.in_(task_ids),
            or_(
                TaskExecution.status.in_([TaskStatus.PENDING, TaskStatus.RUNNING]),
                TaskExecution.dispatch_state.in_(
                    [
                        DISPATCH_PENDING_BINDING,
                        DISPATCH_READY,
                        DISPATCH_CLAIMED,
                    ]
                ),
            ),
        )
        .first()
    )
    sessions = (
        db.query(SessionModel.status, SessionModel.is_active)
        .filter(
            or_(
                SessionModel.id.in_(
                    db.query(SessionTask.session_id).filter(
                        SessionTask.task_id.in_(task_ids)
                    )
                ),
                SessionModel.id.in_(
                    db.query(TaskExecution.session_id).filter(
                        TaskExecution.task_id.in_(task_ids)
                    )
                ),
                SessionModel.continuation_task_id.in_(task_ids),
            )
        )
        .all()
    )
    active_session = any(
        active
        or normalize_session_status(state)
        in {
            SessionStatus.RUNNING.value,
            SessionStatus.RECOVERING.value,
            SessionStatus.RETRY_PENDING.value,
            SessionStatus.AWAITING_INPUT.value,
        }
        for state, active in sessions
    )
    if running or active_execution or active_session:
        db.rollback()
        raise HTTPException(
            status_code=409,
            detail={
                "code": "TASK_EXECUTION_ACTIVE",
                "message": "Stop active task executions before deleting this task or project.",
            },
        )


def delete_task_owned_graph(db: Session, task_ids: Iterable[int]) -> None:
    """Delete task-owned runtime evidence in foreign-key dependency order."""
    task_ids = list(set(task_ids))
    if not task_ids:
        return
    require_safe_task_deletion(db, task_ids)

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
