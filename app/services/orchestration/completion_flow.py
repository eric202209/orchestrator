"""Task completion and finalization flow."""

import asyncio
import json
import logging
from datetime import datetime
from typing import Any, Callable, Dict, Optional

from app.models import SessionTask, Task, TaskStatus
from app.services.orchestration.persistence import (
    record_validation_verdict,
    save_orchestration_checkpoint,
    set_session_alert,
)
from app.services.orchestration.runtime import write_project_state_snapshot
from app.services.orchestration.validator import ValidatorService
from app.services.prompt_templates import OrchestrationStatus


def finalize_successful_task(
    *,
    db: Any,
    openclaw_service: Any,
    task_service: Any,
    session: Any,
    project: Any,
    task: Any,
    session_task_link: Optional[SessionTask],
    session_id: int,
    task_id: int,
    prompt: str,
    execution_profile: str,
    validation_profile: str,
    orchestration_state: Any,
    emit_live: Callable[..., None],
    logger: logging.Logger,
    write_project_state_snapshot_fn: Callable[..., None] = write_project_state_snapshot,
    save_orchestration_checkpoint_fn: Callable[
        ..., None
    ] = save_orchestration_checkpoint,
    get_next_pending_project_task_fn: Optional[Callable[..., Any]] = None,
    get_latest_session_task_link_fn: Optional[Callable[..., Any]] = None,
    execute_openclaw_task_delay_fn: Optional[Callable[..., Any]] = None,
    build_task_report_payload_fn: Optional[Callable[..., Dict[str, Any]]] = None,
    render_task_report_fn: Optional[Callable[..., Dict[str, Any]]] = None,
) -> Dict[str, Any]:
    logger.info("[ORCHESTRATION] Phase 5: TASK_SUMMARY - summarizing completion")
    emit_live(
        "INFO",
        "[ORCHESTRATION] Phase 5: TASK_SUMMARY - summarizing completion",
        metadata={"phase": "task_summary"},
    )

    from app.services import PromptTemplates

    summary_prompt = PromptTemplates.build_task_summary(
        task_description=prompt,
        plan_summary=json.dumps(orchestration_state.plan, indent=2),
        execution_results_summary=orchestration_state.prior_results_summary(),
        changed_files=orchestration_state.changed_files,
        num_debug_attempts=len(orchestration_state.debug_attempts),
        final_status="success",
        execution_profile=execution_profile,
    )
    summary_result = asyncio.run(
        openclaw_service.execute_task(summary_prompt, timeout_seconds=60)
    )

    completion_validation = ValidatorService.validate_task_completion(
        project_dir=orchestration_state.project_dir,
        plan=orchestration_state.plan,
        task_prompt=prompt,
        execution_profile=execution_profile,
        workspace_consistency=task_service.analyze_workspace_consistency(
            orchestration_state.project_dir
        ),
        title=task.title if task else None,
        description=task.description if task else None,
    )
    record_validation_verdict(
        db,
        session_id,
        task_id,
        orchestration_state,
        completion_validation,
    )
    db.commit()

    if not completion_validation.accepted:
        completion_error = "Completion validation failed: " + "; ".join(
            completion_validation.reasons[:5]
        )
        orchestration_state.status = OrchestrationStatus.ABORTED
        orchestration_state.abort_reason = completion_error
        task.status = TaskStatus.FAILED
        task.completed_at = datetime.utcnow()
        task.error_message = completion_error
        task.current_step = len(orchestration_state.plan)
        task.workspace_status = "blocked"
        if session_task_link:
            session_task_link.status = TaskStatus.FAILED
            session_task_link.completed_at = task.completed_at
        if session:
            session.status = "paused"
            session.is_active = False
            set_session_alert(session, "error", completion_error[:2000])
        db.commit()
        emit_live(
            "ERROR",
            "[ORCHESTRATION] Task completion failed validation",
            metadata={
                "phase": "task_validation",
                "validation_status": completion_validation.status,
                "profile": completion_validation.profile,
                "reasons": completion_validation.reasons[:10],
            },
        )
        save_orchestration_checkpoint_fn(
            db, session_id, task_id, prompt, orchestration_state
        )
        write_project_state_snapshot_fn(db, project, task, session_id)
        return {"status": "failed", "reason": "completion_validation_failed"}

    baseline_publish_result = None
    baseline_publish_validation = None
    if project and task.task_subfolder and not runs_in_canonical_baseline:
        baseline_publish_result = task_service.auto_publish_task_into_baseline(
            project, task
        )
        baseline_materialization = task_service.validate_task_baseline_materialization(
            project, task
        )
        baseline_overview = task_service.validate_project_baseline(
            project, current_task=task
        )
        baseline_publish_validation = ValidatorService.validate_baseline_publish(
            validation_profile=validation_profile,
            baseline_path=baseline_materialization.get("baseline_path") or "",
            baseline_file_count=baseline_materialization.get("baseline_file_count", 0),
            missing_task_expected_files=baseline_materialization.get(
                "missing_expected_files", []
            ),
            missing_prior_expected_files=baseline_overview.get(
                "missing_expected_files", []
            ),
            consistency_issues=baseline_materialization.get("consistency_issues", []),
            consistency_details=baseline_materialization.get("consistency"),
        )
        record_validation_verdict(
            db,
            session_id,
            task_id,
            orchestration_state,
            baseline_publish_validation,
        )
        db.commit()
        if not baseline_publish_validation.accepted:
            baseline_error = "Baseline publish validation failed: " + "; ".join(
                baseline_publish_validation.reasons[:5]
            )
            orchestration_state.status = OrchestrationStatus.ABORTED
            orchestration_state.abort_reason = baseline_error
            task.status = TaskStatus.FAILED
            task.completed_at = datetime.utcnow()
            task.error_message = baseline_error
            task.current_step = len(orchestration_state.plan)
            task.workspace_status = "blocked"
            if session_task_link:
                session_task_link.status = TaskStatus.FAILED
                session_task_link.completed_at = task.completed_at
            if session:
                session.status = "paused"
                session.is_active = False
                set_session_alert(session, "error", baseline_error[:2000])
            db.commit()
            emit_live(
                "ERROR",
                "[ORCHESTRATION] Baseline publish failed validation",
                metadata={
                    "phase": "baseline_publish",
                    "validation_status": baseline_publish_validation.status,
                    "reasons": baseline_publish_validation.reasons[:10],
                },
            )
            save_orchestration_checkpoint_fn(
                db, session_id, task_id, prompt, orchestration_state
            )
            write_project_state_snapshot_fn(db, project, task, session_id)
            return {
                "status": "failed",
                "reason": "baseline_publish_validation_failed",
            }

    task.status = TaskStatus.DONE
    task.completed_at = datetime.utcnow()
    task.error_message = None
    task.summary = summary_result.get("output", "")[:2000]
    task.current_step = len(orchestration_state.plan)
    task.workspace_status = "ready" if task.task_subfolder else "not_created"
    if session_task_link:
        session_task_link.status = TaskStatus.DONE
        session_task_link.completed_at = task.completed_at

    set_session_alert(session, None, None)

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
        if next_task:
            session.status = "running"
            session.is_active = True
        elif blocked_pending_task:
            session.status = "paused"
            session.is_active = False
            blockers = type(task_service)(db).get_blocking_prior_tasks(
                blocked_pending_task
            )
            if blockers:
                blocking_summary = ", ".join(
                    f"#{item.plan_position} {item.title} ({item.status.value})"
                    for item in blockers[:3]
                )
                set_session_alert(
                    session,
                    "warning",
                    (
                        "Automatic execution is paused because an earlier ordered task "
                        f"is incomplete: {blocking_summary}"
                    )[:2000],
                )
        else:
            session.status = "stopped"
            session.is_active = False

    db.commit()
    write_project_state_snapshot_fn(db, project, task, session_id)

    logger.info(
        "[ORCHESTRATION] Task %s completed successfully with %s steps",
        task_id,
        len(orchestration_state.plan),
    )
    emit_live(
        "INFO",
        f"[ORCHESTRATION] Task {task_id} completed successfully with {len(orchestration_state.plan)} steps",
        metadata={
            "phase": "completed",
            "steps": len(orchestration_state.plan),
            "baseline_publish_result": baseline_publish_result,
        },
    )

    if baseline_publish_result:
        db.add(
            LogEntry(
                session_id=session_id,
                session_instance_id=session.instance_id,
                task_id=task_id,
                level="INFO",
                message=(
                    "[ORCHESTRATION] Published task workspace into canonical project baseline "
                    f"({baseline_publish_result.get('files_copied', 0)} files)"
                ),
                log_metadata=json.dumps(baseline_publish_result),
            )
        )
        db.commit()

    if (
        session
        and next_task
        and get_latest_session_task_link_fn
        and execute_openclaw_task_delay_fn
    ):
        next_session_task_link = get_latest_session_task_link_fn(
            db, session_id, next_task.id
        )
        if not next_session_task_link:
            next_session_task_link = SessionTask(
                session_id=session_id,
                task_id=next_task.id,
                status=TaskStatus.RUNNING,
                started_at=datetime.utcnow(),
            )
            db.add(next_session_task_link)
        else:
            next_session_task_link.status = TaskStatus.RUNNING
            next_session_task_link.started_at = datetime.utcnow()
            next_session_task_link.completed_at = None

        next_task.status = TaskStatus.RUNNING
        next_task.started_at = datetime.utcnow()
        next_task.completed_at = None
        next_task.error_message = None
        next_task.current_step = 0

        db.add(
            LogEntry(
                session_id=session_id,
                session_instance_id=session.instance_id,
                task_id=next_task.id,
                level="INFO",
                message=(
                    f"[ORCHESTRATION] Auto-advancing to next task {next_task.id}: {next_task.title}"
                ),
                log_metadata=json.dumps(
                    {
                        "auto_advance": True,
                        "plan_position": getattr(next_task, "plan_position", None),
                    }
                ),
            )
        )
        db.commit()
        execute_openclaw_task_delay_fn(
            session_id=session_id,
            task_id=next_task.id,
            prompt=next_task.description or next_task.title,
            timeout_seconds=900,
        )

    if build_task_report_payload_fn and render_task_report_fn:
        try:
            report_payload = build_task_report_payload_fn(db, task_id)
            report_result = render_task_report_fn(
                report_payload, output_format="markdown"
            )
            if report_result and "report" in report_result:
                report_content = report_result["report"]
                report_filename = f"task_report_{task_id}.md"
                report_path = orchestration_state.project_dir / report_filename
                os.makedirs(orchestration_state.project_dir, exist_ok=True)
                with open(report_path, "w", encoding="utf-8") as handle:
                    handle.write(report_content)
                logger.info("[REPORT] Task report saved to: %s", report_path)
        except Exception as report_error:
            logger.error(
                "[REPORT] Failed to generate task report: %s", str(report_error)
            )

    return {
        "status": "completed",
        "task_id": task_id,
        "session_id": session_id,
        "steps_completed": len(orchestration_state.plan),
        "debug_attempts": len(orchestration_state.debug_attempts),
        "summary": summary_result.get("output", "")[:500],
    }
