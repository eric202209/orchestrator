"""Successful task completion finalization for orchestration."""

from __future__ import annotations

import json
from datetime import UTC, datetime
from typing import Any, Callable, Dict, Optional

from app.models import LogEntry, SessionTask, Task, TaskExecution, TaskStatus
from app.services.orchestration.events.event_types import EventType
from app.services.orchestration.run_state import (
    mark_task_attempt_done,
    mark_task_attempt_pending,
)
from app.services.orchestration.state.persistence import append_orchestration_event
from app.services.orchestration.state.session_state import (
    clear_session_alert,
    mark_session_completed,
    mark_session_paused,
    mark_session_running,
)
from app.services.orchestration.types import OrchestrationRunContext


class TaskCompletionFinalizer:
    """Wrap successful task/session/event finalization after validation passes."""

    def __init__(self, *, db: Any, task_service: Any) -> None:
        self.db = db
        self.task_service = task_service

    def finalize_success(
        self,
        *,
        ctx: OrchestrationRunContext,
        summary: str,
        baseline_publish_result: Optional[Dict[str, Any]],
        completion_validation: Any,
        write_project_state_snapshot_fn: Callable[..., None],
        write_progress_notes_fn: Callable[..., None],
        get_next_pending_project_task_fn: Optional[Callable[..., Any]] = None,
        get_latest_session_task_link_fn: Optional[Callable[..., Any]] = None,
        execute_orchestration_task_delay_fn: Optional[Callable[..., Any]] = None,
    ) -> Dict[str, Any]:
        db = self.db
        task_service = self.task_service
        session = ctx.session
        project = ctx.project
        task = ctx.task
        session_id = ctx.session_id
        task_id = ctx.task_id
        orchestration_state = ctx.orchestration_state

        task_execution = (
            db.query(TaskExecution)
            .filter(TaskExecution.id == ctx.task_execution_id)
            .first()
            if ctx.task_execution_id
            else None
        )
        completed_at = mark_task_attempt_done(
            task=task,
            session_task_link=ctx.session_task_link,
            task_execution=task_execution,
            completed_at=datetime.now(UTC),
        )
        task.summary = summary[:2000]
        task.current_step = len(orchestration_state.plan)
        promoted_workspace_archive_result = None
        if (
            baseline_publish_result
            and not baseline_publish_result.get("auto_publish_skipped")
            and project
            and task.task_subfolder
        ):
            promoted_workspace_archive_result = (
                task_service.archive_promoted_task_workspace(project, task)
            )
            baseline_publish_result["promoted_workspace_archive_result"] = (
                promoted_workspace_archive_result
            )
        elif project and task and ctx.runs_in_canonical_baseline:
            task.workspace_status = "promoted"
            task.promoted_at = getattr(task, "promoted_at", None) or completed_at
            existing_note = (getattr(task, "promotion_note", None) or "").strip()
            canonical_note = (
                "Task completed directly in the canonical project root; no separate "
                "task workspace is retained for review."
            )
            task.promotion_note = (
                f"{existing_note}\n{canonical_note}"
                if existing_note
                else canonical_note
            )
            archive_unlocked = getattr(
                getattr(task_service, "baselines", None),
                "archive_promoted_task_workspace_unlocked",
                None,
            )
            if task.task_subfolder and callable(archive_unlocked):
                promoted_workspace_archive_result = archive_unlocked(
                    project,
                    task,
                    reason="canonical_root_task_completed",
                    project_root=task_service.get_project_root(project),
                )
        else:
            task.workspace_status = "ready" if task.task_subfolder else "not_created"
        task.completed_at = completed_at
        append_orchestration_event(
            project_dir=orchestration_state.project_dir,
            session_id=session_id,
            task_id=task_id,
            event_type=EventType.TASK_COMPLETED,
            details={
                "steps_completed": len(orchestration_state.plan),
                "execution_profile": ctx.execution_profile,
            },
        )

        write_progress_notes_fn(
            orchestration_state=orchestration_state,
            task=task,
            prompt=ctx.prompt,
            summary=summary,
            logger=ctx.logger,
        )

        clear_session_alert(session)
        db.flush()

        next_task = None
        blocked_pending_task = None
        if (
            session
            and session.execution_mode == "automatic"
            and get_next_pending_project_task_fn
        ):
            next_task = get_next_pending_project_task_fn(db, session.project_id)
            if not next_task and session.project_id:
                blocked_pending_task = (
                    db.query(Task)
                    .filter(
                        Task.project_id == session.project_id,
                        Task.status == TaskStatus.PENDING,
                    )
                    .order_by(
                        Task.plan_position.asc().nullslast(),
                        Task.priority.desc(),
                        Task.created_at.asc().nullslast(),
                        Task.id.asc(),
                    )
                    .first()
                )

        if session:
            failed_task_links = self._session_failed_task_links(session_id)
            if next_task:
                mark_session_running(session)
            elif failed_task_links:
                failed_task_ids = [link.task_id for link in failed_task_links[:5]]
                failed_tasks = (
                    db.query(Task)
                    .filter(Task.id.in_(failed_task_ids))
                    .order_by(Task.id.asc())
                    .all()
                    if failed_task_ids
                    else []
                )
                failed_summary = ", ".join(
                    f"#{item.id} {item.title}" for item in failed_tasks[:3]
                )
                mark_session_paused(
                    session,
                    alert_level="error",
                    alert_message=(
                        "Session has failed task(s) that must be repaired or retried "
                        f"before completion: {failed_summary or len(failed_task_links)} failed task(s)"
                    )[:2000],
                )
            elif blocked_pending_task:
                mark_session_paused(session)
                blockers = type(task_service)(db).get_blocking_prior_tasks(
                    blocked_pending_task
                )
                if blockers:
                    blocking_summary = ", ".join(
                        f"#{item.plan_position} {item.title} ({item.status.value})"
                        for item in blockers[:3]
                    )
                    mark_session_paused(
                        session,
                        alert_level="warning",
                        alert_message=(
                            "Automatic execution is paused because an earlier ordered task "
                            f"is incomplete: {blocking_summary}"
                        )[:2000],
                    )
            else:
                mark_session_completed(session, completed_at=datetime.now(UTC))

        db.commit()
        write_project_state_snapshot_fn(db, project, task, session_id)

        if baseline_publish_result:
            publish_skipped = bool(baseline_publish_result.get("auto_publish_skipped"))
            db.add(
                LogEntry(
                    session_id=session_id,
                    session_instance_id=session.instance_id,
                    task_id=task_id,
                    level="INFO",
                    message=(
                        "[ORCHESTRATION] Held task workspace for manual review"
                        if publish_skipped
                        else (
                            "[ORCHESTRATION] Published task workspace into canonical project baseline "
                            f"({baseline_publish_result.get('files_copied', 0)} files)"
                        )
                    ),
                    log_metadata=json.dumps(baseline_publish_result),
                )
            )
            db.commit()

        if (
            session
            and next_task
            and get_latest_session_task_link_fn
            and execute_orchestration_task_delay_fn
        ):
            next_session_task_link = get_latest_session_task_link_fn(
                db, session_id, next_task.id
            )
            if not next_session_task_link:
                next_session_task_link = SessionTask(
                    session_id=session_id,
                    task_id=next_task.id,
                    status=TaskStatus.PENDING,
                    started_at=None,
                )
                db.add(next_session_task_link)
            else:
                mark_task_attempt_pending(
                    task=None,
                    session_task_link=next_session_task_link,
                    reset_started_at=True,
                )

            mark_task_attempt_pending(
                task=next_task,
                reset_started_at=True,
                error_message=None,
            )
            from app.services.task_execution_service import create_task_execution

            next_task_execution = create_task_execution(
                db,
                session_id=session_id,
                task_id=next_task.id,
                status=TaskStatus.PENDING,
                started_at=None,
            )

            db.add(
                LogEntry(
                    session_id=session_id,
                    session_instance_id=session.instance_id,
                    task_id=next_task.id,
                    task_execution_id=next_task_execution.id,
                    level="INFO",
                    message=(
                        f"[ORCHESTRATION] Auto-advancing to next task {next_task.id}: {next_task.title}"
                    ),
                    log_metadata=json.dumps(
                        {
                            "auto_advance": True,
                            "task_execution_id": next_task_execution.id,
                            "plan_position": getattr(next_task, "plan_position", None),
                        }
                    ),
                )
            )
            db.commit()
            execute_orchestration_task_delay_fn(
                session_id=session_id,
                task_id=next_task.id,
                prompt=next_task.description or next_task.title,
                timeout_seconds=ctx.timeout_seconds,
                task_execution_id=next_task_execution.id,
            )

        append_orchestration_event(
            project_dir=orchestration_state.project_dir,
            session_id=session_id,
            task_id=task_id,
            event_type=EventType.PHASE_FINISHED,
            details={
                "phase": "task_summary",
                "status": completion_validation.status,
                "task_status": str(task.status.value if task else "done"),
            },
        )

        return {
            "completed_at": completed_at,
            "promoted_workspace_archive_result": promoted_workspace_archive_result,
        }

    def _session_failed_task_links(self, session_id: int) -> list[SessionTask]:
        if not session_id:
            return []
        return (
            self.db.query(SessionTask)
            .filter(
                SessionTask.session_id == session_id,
                SessionTask.status == TaskStatus.FAILED,
            )
            .order_by(SessionTask.id.asc())
            .all()
        )
