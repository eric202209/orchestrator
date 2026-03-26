"""Celery Worker Tasks

Background task processing for the orchestrator.
Implements multi-step orchestration workflow:
PLANNING → EXECUTING (step-by-step) → DEBUGGING (on failure) → PLAN_REVISION → DONE
"""

import logging
import json
from typing import Optional, Dict, Any, List
from datetime import datetime
from sqlalchemy.orm import Session
from sqlalchemy import create_engine
from app.celery_app import celery_app
from app.models import Session as SessionModel, Task, TaskStatus, LogEntry, Project
from app.database import get_db
from app.services import OpenClawSessionService, PromptTemplates
from app.services.prompt_templates import OrchestrationStatus, OrchestrationState, StepResult

logger = logging.getLogger(__name__)


def get_db_session():
    """Get database session for Celery tasks"""
    engine = create_engine("sqlite:///./orchestrator.db")
    return Session(bind=engine)


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def execute_openclaw_task(
    self,
    session_id: int,
    task_id: int,
    prompt: str,
    timeout_seconds: int = 300,
    context: Optional[Dict[str, Any]] = None
):
    """
    Execute an OpenClaw task with multi-step orchestration
    
    Workflow:
    1. PLANNING → Generate step plan
    2. EXECUTING → Execute each step
    3. DEBUGGING → Fix failed steps
    4. PLAN_REVISION → Revise plan if needed
    5. DONE → Summarize completion
    
    Args:
        session_id: Session ID
        task_id: Task ID
        prompt: Task prompt to execute
        timeout_seconds: Maximum execution time
        context: Additional context
    """
    db = get_db_session()
    
    try:
        # Get session and task
        session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
        task = db.query(Task).filter(Task.id == task_id).first()
        
        if not session or not task:
            raise ValueError("Session or task not found")
        
        # Initialize orchestration state
        orchestration_state = OrchestrationState(
            session_id=str(session_id),
            task_description=prompt,
            project_name=session.name if session else "",
            project_context=context.get("project_context", "") if context else ""
        )
        
        # Update task status
        task.status = TaskStatus.RUNNING
        task.started_at = datetime.utcnow()
        db.commit()
        
        logger.info(f"[ORCHESTRATION] Starting multi-step execution for task {task_id}")
        
        # Initialize OpenClaw service
        openclaw_service = OpenClawSessionService(db, session_id, task_id)
        
        # Get session context
        import asyncio
        session_context = asyncio.run(openclaw_service.get_session_context())
        
        # PHASE 1: PLANNING - Generate step plan
        logger.info("[ORCHESTRATION] Phase 1: PLANNING - generating step plan")
        planning_prompt = PromptTemplates.build_planning_prompt(
            task_description=prompt,
            project_context=session_context.get("project_context"),
            workspace_root=str(orchestration_state.workspace_root),
            project_dir=str(orchestration_state.project_dir)
        )
        
        planning_result = asyncio.run(openclaw_service.execute_task(planning_prompt, timeout_seconds=120))
        
        # Parse planning result to get steps
        try:
            plan_data = json.loads(planning_result.get("output", "{}"))
            if isinstance(plan_data, list):
                orchestration_state.plan = plan_data
                logger.info(f"[ORCHESTRATION] Generated {len(plan_data)} steps in plan")
            else:
                raise ValueError("Planning result is not a list of steps")
        except Exception as e:
            logger.error(f"[ORCHESTRATION] Failed to parse planning result: {e}")
            orchestration_state.status = OrchestrationStatus.ABORTED
            orchestration_state.abort_reason = f"Planning parse failed: {e}"
            task.status = TaskStatus.FAILED
            task.error_message = str(e)
            db.commit()
            return {"status": "failed", "reason": "planning_parse_error"}
        
        # PHASE 2: EXECUTING - Execute each step
        logger.info(f"[ORCHESTRATION] Phase 2: EXECUTING - executing {len(orchestration_state.plan)} steps")
        
        for step_index, step in enumerate(orchestration_state.plan):
            orchestration_state.current_step_index = step_index
            
            step_description = step.get("description", f"Step {step_index + 1}")
            step_commands = step.get("commands", [])
            verification_command = step.get("verification")
            rollback_command = step.get("rollback")
            expected_files = step.get("expected_files", [])
            
            logger.info(f"[ORCHESTRATION] Executing step {step_index + 1}/{len(orchestration_state.plan)}: {step_description[:80]}...")
            
            # Build execution prompt
            execution_prompt = PromptTemplates.build_execution_prompt(
                step_description=step_description,
                step_commands=step_commands,
                verification_command=verification_command,
                rollback_command=rollback_command,
                expected_files=expected_files,
                completed_steps_summary=orchestration_state.prior_results_summary(),
                project_context=session_context.get("project_context")
            )
            
            # Execute step
            step_result = asyncio.run(openclaw_service.execute_task(execution_prompt, timeout_seconds=timeout_seconds // len(orchestration_state.plan)))
            
            # Record result
            step_output = step_result.get("output", "")
            step_status = "success" if step_result.get("status") != "failed" else "failed"
            
            step_record = StepResult(
                step_number=step_index + 1,
                status=step_status,
                output=step_output[:1000],
                verification_output=step_result.get("verification_output", ""),
                files_changed=expected_files,  # Simplified
                error_message=step_result.get("error", ""),
                attempt=1
            )
            
            if step_status == "success":
                orchestration_state.record_success(step_record)
                logger.info(f"[ORCHESTRATION] Step {step_index + 1} completed successfully")
            else:
                orchestration_state.record_failure(step_record)
                
                # PHASE 3: DEBUGGING - Fix failed step
                logger.info(f"[ORCHESTRATION] Step {step_index + 1} failed, entering DEBUGGING phase")
                
                debug_prompt = PromptTemplates.build_debugging_prompt(
                    step_description=step_description,
                    error_message=step_record.error_message,
                    command_output=step_output,
                    verification_output=step_record.verification_output,
                    attempt_number=1,
                    max_attempts=3,
                    prior_debug_attempts=orchestration_state.debug_attempts,
                    project_name=orchestration_state.project_name,
                    workspace_root=str(orchestration_state.workspace_root)
                )
                
                debug_result = asyncio.run(openclaw_service.execute_task(debug_prompt, timeout_seconds=120))
                
                # Parse debug result
                try:
                    debug_data = json.loads(debug_result.get("output", "{}"))
                    fix_type = debug_data.get("fix_type", "code_fix")
                    
                    if fix_type == "revise_plan":
                        # PHASE 4: PLAN_REVISION
                        logger.info(f"[ORCHESTRATION] Plan revision needed, entering PLAN_REVISION phase")
                        revise_prompt = PromptTemplates.build_plan_revision_prompt(
                            original_plan=orchestration_state.plan,
                            failed_steps=[step_record],
                            debug_analysis=debug_result.get("output", ""),
                            completed_steps=orchestration_state.completed_steps
                        )
                        
                        revise_result = asyncio.run(openclaw_service.execute_task(revise_prompt, timeout_seconds=120))
                        
                        # Update plan with revised version
                        revise_data = json.loads(revise_result.get("output", "{}"))
                        orchestration_state.plan = revise_data.get("revised_plan", orchestration_state.plan)
                        logger.info(f"[ORCHESTRATION] Plan revised, {len(orchestration_state.plan)} steps")
                        
                        # Retry the step with revised plan
                        continue  # Retry this step
                    
                    elif fix_type == "code_fix" or fix_type == "command_fix":
                        # Retry the step with fix
                        logger.info(f"[ORCHESTRATION] Fix applied, retrying step {step_index + 1}")
                        continue  # Retry this step
                        
                except Exception as e:
                    logger.error(f"[ORCHESTRATION] Debug parsing failed: {e}")
                    orchestration_state.status = OrchestrationStatus.ABORTED
                    orchestration_state.abort_reason = f"Debug parse failed: {e}"
                    task.status = TaskStatus.FAILED
                    task.error_message = str(e)
                    db.commit()
                    return {"status": "failed", "reason": "debug_parse_error"}
        
        # PHASE 5: TASK_SUMMARY - Summarize completion
        logger.info("[ORCHESTRATION] Phase 5: TASK_SUMMARY - summarizing completion")
        
        summary_prompt = PromptTemplates.build_task_summary(
            task_description=prompt,
            plan_summary=json.dumps(orchestration_state.plan, indent=2),
            execution_results_summary=orchestration_state.prior_results_summary(),
            changed_files=orchestration_state.changed_files,
            num_debug_attempts=len(orchestration_state.debug_attempts),
            final_status="success"
        )
        
        summary_result = asyncio.run(openclaw_service.execute_task(summary_prompt, timeout_seconds=60))
        
        # Mark task as done
        task.status = TaskStatus.DONE
        task.completed_at = datetime.utcnow()
        task.summary = summary_result.get("output", "")[:2000]
        db.commit()
        
        logger.info(f"[ORCHESTRATION] Task {task_id} completed successfully with {len(orchestration_state.plan)} steps")
        
        return {
            "status": "completed",
            "task_id": task_id,
            "session_id": session_id,
            "steps_completed": len(orchestration_state.plan),
            "debug_attempts": len(orchestration_state.debug_attempts),
            "summary": summary_result.get("output", "")[:500]
        }
        
    except Exception as exc:
        # Update task failure
        task.status = TaskStatus.FAILED
        task.error_message = str(exc)
        db.commit()
        
        logger.error(f"[ORCHESTRATION] Task {task_id} failed: {str(exc)}")
        
        # Retry if possible
        raise self.retry(exc=exc)
        
    finally:
        db.close()


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def process_github_webhook(
    self,
    webhook_data: Dict[str, Any],
    repo_owner: str,
    repo_name: str
):
    """
    Process GitHub webhook in the background
    
    Args:
        webhook_data: GitHub webhook payload
        repo_owner: Repository owner
        repo_name: Repository name
    """
    try:
        # Get or create project
        db = get_db_session()
        
        project = db.query(Project).filter(
            Project.github_url.ilike(f"%{repo_owner}/{repo_name}%")
        ).first()
        
        if not project:
            # Create new project from webhook
            project = Project(
                name=f"{repo_owner}/{repo_name}",
                github_url=f"https://github.com/{repo_owner}/{repo_name}",
                description="Auto-created from GitHub webhook"
            )
            db.add(project)
            db.commit()
            db.refresh(project)
        
        # Process webhook based on type
        webhook_type = webhook_data.get("type", "Unknown")
        
        if webhook_type == "PushEvent":
            # Handle push events
            logger.info(f"Processing push event for {repo_owner}/{repo_name}")
            # TODO: Create task for code analysis
            
        elif webhook_type == "PullRequestEvent":
            # Handle PR events
            logger.info(f"Processing PR event for {repo_owner}/{repo_name}")
            # TODO: Create task for PR review
            
        elif webhook_type == "IssueEvent":
            # Handle issue events
            logger.info(f"Processing issue event for {repo_owner}/{repo_name}")
            # TODO: Create task from issue
            
        db.close()
        
        return {
            "status": "processed",
            "webhook_type": webhook_type,
            "project_id": project.id if project else None
        }
        
    except Exception as exc:
        logger.error(f"Webhook processing failed: {str(exc)}")
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=5, default_retry_delay=30)
def scheduled_task_execution(
    self,
    task_id: int,
    scheduled_time: str,
    prompt: str
):
    """
    Execute a task at a scheduled time
    
    Args:
        task_id: Task ID
        scheduled_time: ISO format scheduled time
        prompt: Task prompt
    """
    from datetime import datetime as dt
    
    try:
        # Check if it's time to execute
        now = dt.utcnow()
        schedule_dt = dt.fromisoformat(scheduled_time.replace("Z", "+00:00"))
        
        if now < schedule_dt:
            # Not time yet, reschedule
            delay_seconds = (schedule_dt - now).total_seconds()
            logger.info(f"Task {task_id} scheduled for later, retrying in {delay_seconds}s")
            raise self.retry(countdown=delay_seconds)
        
        # Execute the task
        db = get_db_session()
        
        task = db.query(Task).filter(Task.id == task_id).first()
        if task:
            task.status = TaskStatus.RUNNING
            task.started_at = dt.utcnow()
            db.commit()
        
        # TODO: Implement actual scheduled execution
        # This would integrate with OpenClaw session
        
        if task:
            task.status = TaskStatus.DONE
            task.completed_at = dt.utcnow()
            db.commit()
        
        db.close()
        
        return {
            "status": "completed",
            "task_id": task_id,
            "executed_at": dt.utcnow().isoformat()
        }
        
    except Exception as exc:
        logger.error(f"Scheduled task {task_id} failed: {str(exc)}")
        raise self.retry(exc=exc, max_retries=3)


@celery_app.task(bind=True)
def cleanup_old_logs(
    self,
    days: int = 30,
    session_id: Optional[int] = None
):
    """
    Clean up old log entries
    
    Args:
        days: Delete logs older than this many days
        session_id: Optional session filter
    """
    try:
        db = get_db_session()
        
        from datetime import datetime, timedelta
        
        cutoff_date = datetime.utcnow() - timedelta(days=days)
        
        query = db.query(LogEntry).filter(LogEntry.created_at < cutoff_date)
        if session_id:
            query = query.filter(LogEntry.session_id == session_id)
        
        deleted_count = query.delete(synchronize_session=False)
        db.commit()
        
        logger.info(f"Deleted {deleted_count} old log entries")
        
        db.close()
        
        return {
            "status": "completed",
            "deleted_count": deleted_count,
            "days": days,
            "session_id": session_id
        }
        
    except Exception as exc:
        logger.error(f"Log cleanup failed: {str(exc)}")
        raise self.retry(exc=exc, max_retries=3)


@celery_app.task(bind=True)
def generate_task_report(
    self,
    task_id: int,
    format: str = "json"
):
    """
    Generate a report for a completed task
    
    Args:
        task_id: Task ID
        format: Output format (json, markdown, html)
    """
    try:
        db = get_db_session()
        
        task = db.query(Task).filter(Task.id == task_id).first()
        if not task:
            raise ValueError(f"Task {task_id} not found")
        
        # Get session logs
        logs = db.query(LogEntry).filter(
            LogEntry.task_id == task_id
        ).order_by(LogEntry.created_at).all()
        
        # Build report
        report = {
            "task_id": task.id,
            "title": task.title,
            "status": task.status.value,
            "created_at": task.created_at.isoformat(),
            "completed_at": task.completed_at.isoformat() if task.completed_at else None,
            "duration_seconds": (task.completed_at - task.started_at).total_seconds() if task.started_at and task.completed_at else None,
            "logs": [
                {
                    "level": log.level,
                    "message": log.message,
                    "timestamp": log.created_at.isoformat()
                }
                for log in logs
            ]
        }
        
        db.close()
        
        if format == "markdown":
            # Convert to markdown
            report_text = f"# Task Report: {task.title}\n\n"
            report_text += f"**Status:** {task.status.value}\n\n"
            report_text += f"**Duration:** {report['duration_seconds']} seconds\n\n"
            report_text += "## Logs\n\n"
            for log in report["logs"]:
                report_text += f"- [{log['level']}] {log['message']}\n"
            
            return {"report": report_text, "format": "markdown"}
        
        return {"report": report, "format": format}
        
    except Exception as exc:
        logger.error(f"Report generation failed: {str(exc)}")
        raise self.retry(exc=exc, max_retries=3)
