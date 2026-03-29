"""Celery Worker Tasks

Background task processing for the orchestrator.
Implements multi-step orchestration workflow:
PLANNING → EXECUTING (step-by-step) → DEBUGGING (on failure) → PLAN_REVISION → DONE
"""

import os
import logging
import json
import re
from typing import Optional, Dict, Any, List
from datetime import datetime
from pathlib import Path
from sqlalchemy.orm import Session
from sqlalchemy import create_engine
from app.celery_app import celery_app
from app.models import Session as SessionModel, Task, TaskStatus, LogEntry, Project
from app.database import get_db, get_db_session
from app.services import OpenClawSessionService, PromptTemplates
from app.services.error_handler import error_handler
from app.services.prompt_templates import (
    OrchestrationStatus,
    OrchestrationState,
    StepResult,
    OPENCLAW_WORKSPACE_ROOT,
)

logger = logging.getLogger(__name__)


def slugify_project_name(name: str) -> str:
    """
    Convert a project name to a clean, URL-safe slug.

    Examples:
        "Demo API Server" -> "demo-api-server"
        "Flask API Session" -> "flask-api-session"
        "My Project!" -> "my-project"

    Args:
        name: Original project name

    Returns:
        Slugified name suitable for directory names
    """
    if not name:
        return "session"

    # Convert to lowercase
    slug = name.lower()

    # Replace spaces and underscores with hyphens
    slug = re.sub(r"[\s_]+", "-", slug)

    # Remove special characters (keep only alphanumeric, hyphens, and underscores)
    slug = re.sub(r"[^a-z0-9-_]", "", slug)

    # Replace multiple hyphens with single hyphen
    slug = re.sub(r"-+", "-", slug)

    # Remove leading/trailing hyphens
    slug = slug.strip("-")

    # Ensure we have at least "session" as fallback
    if not slug:
        slug = "session"

    return slug


@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    time_limit=360,
    soft_time_limit=300,
    queue="celery",
)
def execute_openclaw_task(
    self,
    session_id: int,
    task_id: int,
    prompt: str,
    timeout_seconds: int = 300,
    context: Optional[Dict[str, Any]] = None,
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

        # Get the project associated with this session
        project = (
            db.query(Project).filter(Project.id == session.project_id).first()
            if session.project_id
            else None
        )

        # Initialize orchestration state with new workspace architecture
        # Structure: workspace_root / project_workspace / task_subfolder

        orchestration_state = OrchestrationState(
            session_id=str(session_id),
            task_description=prompt,
            project_name=project.name if project else "",
            project_context=context.get("project_context", "") if context else "",
            task_id=task_id,  # Pass task ID for subfolder generation
        )

        # If project has workspace_path configured, use it
        if project and project.workspace_path:
            # workspace_path should be relative (e.g., "TalentBridge"), not absolute
            workspace_path = project.workspace_path
            if not workspace_path.startswith("/"):
                # Make it absolute
                workspace_path = str(OPENCLAW_WORKSPACE_ROOT / workspace_path)

            orchestration_state._workspace_path_override = workspace_path

            # Also set task subfolder if not already in database
            if task.task_subfolder:
                orchestration_state._task_subfolder_override = task.task_subfolder
            else:
                # Generate task subfolder from task title (slugified)
                # Use task title if available, otherwise fall back to task_id
                task_title_slug = (
                    slugify_project_name(task.title)
                    if task.title
                    else f"task_{task_id}"
                )

                # Ensure unique subfolder name (append counter if needed)
                counter = 1
                subfolder_name = task_title_slug
                while True:
                    subfolder_path = f"{workspace_path}/{subfolder_name}"
                    # Check if this subfolder already exists
                    existing_tasks = (
                        db.query(Task)
                        .filter(
                            Task.project_id == session.project_id,
                            Task.task_subfolder == subfolder_name,
                        )
                        .all()
                    )

                    # If this is the first task with this name, or it's our current task
                    if len(existing_tasks) == 1 and existing_tasks[0].id == task_id:
                        break
                    elif len(existing_tasks) == 0:
                        break
                    else:
                        # Another task already uses this name, append counter
                        subfolder_name = f"{task_title_slug}-{counter}"
                        counter += 1

                task.task_subfolder = subfolder_name
                db.commit()
                orchestration_state._task_subfolder_override = subfolder_name
        else:
            # Fallback: use slugified project name
            pass

        # Create the task workspace directory if it doesn't exist
        task_workspace = orchestration_state.project_dir
        if not os.path.exists(task_workspace):
            os.makedirs(task_workspace, exist_ok=True)
            logger.info(f"Created task workspace: {task_workspace}")

        # Check if task has been running too long (safety check)
        if task.started_at:
            time_since_start = datetime.utcnow() - task.started_at
            if time_since_start.total_seconds() > 300:  # 5 minutes
                logger.warning(
                    f"[ORCHESTRATION] Task {task_id} already running for {time_since_start}, marking as failed"
                )
                task.status = TaskStatus.FAILED
                task.error = f"Task already running for {time_since_start}, possible duplicate execution"
                db.commit()
                raise Exception("Task timeout - already running too long")

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

        # Use project_name (already slugified) for the project context
        project_name_slug = (
            orchestration_state.project_name.strip() or f"session-{session_id}"
        )
        project_context = f"Build project: {project_name_slug}"

        planning_prompt = PromptTemplates.build_planning_prompt(
            task_description=prompt,
            project_context=project_context,
            workspace_root=str(orchestration_state.workspace_root),
            project_dir=str(orchestration_state.project_dir),
        )

        planning_result = asyncio.run(
            openclaw_service.execute_task(planning_prompt, timeout_seconds=120)
        )

        # Parse planning result to get steps
        try:
            output_result = planning_result.get("output", {})

            # Debug: Log raw result to diagnose JSON parsing issues
            logger.info(
                f"[ORCHESTRATION] Planning result keys: {list(planning_result.keys()) if isinstance(planning_result, dict) else 'Not a dict'}"
            )
            logger.info(
                f"[ORCHESTRATION] Planning output type: {type(output_result)}, preview: {str(output_result)[:300]}"
            )

            # Debug: Log raw output
            logger.info(
                f"[ORCHESTRATION] Raw planning output type: {type(output_result)}, content preview: {str(output_result)[:200]}"
            )

            # Handle different output formats from OpenClaw CLI
            if isinstance(output_result, dict):
                # Format 1: OpenClaw CLI returns full JSON with "payloads" key
                if "payloads" in output_result:
                    payloads = output_result.get("payloads", [])
                    if isinstance(payloads, list) and len(payloads) > 0:
                        first_payload = payloads[0]
                        if isinstance(first_payload, dict):
                            output_text = first_payload.get("text", "")
                            # Debug: Log the extraction
                            logger.info(
                                f"[ORCHESTRATION] Extracted 'text' from payload, length: {len(output_text)}, preview: {output_text[:100]}"
                            )
                        elif isinstance(first_payload, str):
                            # Payload is already a string
                            output_text = first_payload
                            logger.info(
                                f"[ORCHESTRATION] Payload is string, length: {len(output_text)}"
                            )
                        else:
                            # Unknown type, convert to string
                            output_text = str(first_payload)
                            logger.info(
                                f"[ORCHESTRATION] Payload is {type(first_payload)}, converted to string"
                            )
                    else:
                        output_text = json.dumps(output_result)
                        logger.info(f"[ORCHESTRATION] Empty payloads, using full JSON")
                # Format 2: Direct dict response
                else:
                    output_text = json.dumps(output_result)
                    logger.info(f"[ORCHESTRATION] Direct dict response")
            elif isinstance(output_result, str):
                # Format 3: Raw string response
                output_text = output_result
                logger.info(f"[ORCHESTRATION] Raw string response")
            else:
                output_text = str(output_result)
                logger.info(f"[ORCHESTRATION] Unknown type, converted to string")

            logger.info(
                f"[ORCHESTRATION] Final extracted text length: {len(output_text)}"
            )

            # Strip Markdown code fences if present
            import re

            if isinstance(output_text, str):
                # Remove ```json or ``` wrappers
                markdown_pattern = r"^\s*```(?:json)?\s*|\s*```$"
                output_text = re.sub(markdown_pattern, "", output_text.strip())
                logger.info(
                    f"[ORCHESTRATION] After stripping markdown, length: {len(output_text)}"
                )

            # Use enhanced JSON parsing with multiple recovery strategies
            success, plan_data, strategy_info = error_handler.attempt_json_parsing(
                output_text, context="planning"
            )

            if success:
                if isinstance(plan_data, list):
                    orchestration_state.plan = plan_data
                    logger.info(
                        f"[ORCHESTRATION] Generated {len(plan_data)} steps in plan (using {strategy_info})"
                    )
                else:
                    raise ValueError("Planning result is not a list of steps")
            else:
                # Failed to parse after all strategies
                orchestration_state.status = OrchestrationStatus.ABORTED
                orchestration_state.abort_reason = (
                    f"Planning JSON parse failed: {strategy_info}"
                )
                task.status = TaskStatus.FAILED
                task.error_message = (
                    f"Planning JSON parse failed: {strategy_info}. "
                    f"Raw output: {output_text[:500]}"
                )
                db.commit()
                return {"status": "failed", "reason": "planning_json_error"}
        except Exception as e:
            logger.error(f"[ORCHESTRATION] Failed to parse planning result: {e}")
            orchestration_state.status = OrchestrationStatus.ABORTED
            orchestration_state.abort_reason = f"Planning parse failed: {e}"
            task.status = TaskStatus.FAILED
            task.error_message = str(e)
            db.commit()
            return {"status": "failed", "reason": "planning_parse_error"}

        # PHASE 2: EXECUTING - Execute each step
        logger.info(
            f"[ORCHESTRATION] Phase 2: EXECUTING - executing {len(orchestration_state.plan)} steps"
        )

        for step_index, step in enumerate(orchestration_state.plan):
            orchestration_state.current_step_index = step_index

            step_description = step.get("description", f"Step {step_index + 1}")
            step_commands = step.get("commands", [])
            verification_command = step.get("verification")
            rollback_command = step.get("rollback")
            expected_files = step.get("expected_files", [])

            logger.info(
                f"[ORCHESTRATION] Executing step {step_index + 1}/{len(orchestration_state.plan)}: {step_description[:80]}..."
            )

            # Debug: Log the step data
            logger.info(
                f"[ORCHESTRATION] Step data: commands={step_commands}, verification={verification_command}"
            )

            # Build execution prompt
            # Use project_name (already slugified) for consistency
            project_name_slug = (
                orchestration_state.project_name.strip() or f"session-{session_id}"
            )
            execution_prompt = PromptTemplates.build_execution_prompt(
                step_description=step_description,
                step_commands=step_commands,
                verification_command=verification_command,
                rollback_command=rollback_command,
                expected_files=expected_files,
                completed_steps_summary=orchestration_state.prior_results_summary(),
                project_context=f"Build project: {project_name_slug}",
            )

            # Execute step
            step_result = asyncio.run(
                openclaw_service.execute_task(
                    execution_prompt,
                    timeout_seconds=timeout_seconds // len(orchestration_state.plan),
                )
            )

            # Record result
            step_output = step_result.get("output", "")
            step_status = (
                "success" if step_result.get("status") != "failed" else "failed"
            )

            step_record = StepResult(
                step_number=step_index + 1,
                status=step_status,
                output=step_output[:1000],
                verification_output=step_result.get("verification_output", ""),
                files_changed=expected_files,  # Simplified
                error_message=step_result.get("error", ""),
                attempt=1,
            )

            if step_status == "success":
                orchestration_state.record_success(step_record)
                logger.info(
                    f"[ORCHESTRATION] Step {step_index + 1} completed successfully"
                )
            else:
                orchestration_state.record_failure(step_record)

                # PHASE 3: DEBUGGING - Fix failed step
                logger.info(
                    f"[ORCHESTRATION] Step {step_index + 1} failed, entering DEBUGGING phase"
                )

                debug_prompt = PromptTemplates.build_debugging_prompt(
                    step_description=step_description,
                    error_message=step_record.error_message,
                    command_output=step_output,
                    verification_output=step_record.verification_output,
                    attempt_number=1,
                    max_attempts=3,
                    prior_debug_attempts=orchestration_state.debug_attempts,
                    project_name=orchestration_state.project_name,
                    workspace_root=str(orchestration_state.workspace_root),
                )

                debug_result = asyncio.run(
                    openclaw_service.execute_task(debug_prompt, timeout_seconds=120)
                )

                # Parse debug result with enhanced error handling
                try:
                    debug_output = debug_result.get("output", "{}")
                    success, debug_data, strategy_info = (
                        error_handler.attempt_json_parsing(
                            debug_output, context="debug"
                        )
                    )

                    if not success:
                        raise ValueError(
                            f"Failed to parse debug result: {strategy_info}"
                        )

                    fix_type = debug_data.get("fix_type", "code_fix")
                    logger.info(f"[DEBUG-PARSE] Using strategy: {strategy_info}")

                    if fix_type == "revise_plan":
                        # PHASE 4: PLAN_REVISION
                        logger.info(
                            f"[ORCHESTRATION] Plan revision needed, entering PLAN_REVISION phase"
                        )
                        revise_prompt = PromptTemplates.build_plan_revision_prompt(
                            original_plan=orchestration_state.plan,
                            failed_steps=[step_record],
                            debug_analysis=debug_result.get("output", ""),
                            completed_steps=orchestration_state.completed_steps,
                        )

                        revise_result = asyncio.run(
                            openclaw_service.execute_task(
                                revise_prompt, timeout_seconds=120
                            )
                        )

                        # Update plan with revised version
                        revise_output = revise_result.get("output", "{}")
                        success, revise_data, strategy_info = (
                            error_handler.attempt_json_parsing(
                                revise_output, context="revision"
                            )
                        )

                        if not success:
                            raise ValueError(
                                f"Failed to parse revision: {strategy_info}"
                            )

                        orchestration_state.plan = revise_data.get(
                            "revised_plan", orchestration_state.plan
                        )
                        logger.info(f"[REVISION-PARSE] Using strategy: {strategy_info}")
                        logger.info(
                            f"[ORCHESTRATION] Plan revised, {len(orchestration_state.plan)} steps"
                        )

                        # Retry the step with revised plan
                        continue  # Retry this step

                    elif fix_type == "code_fix" or fix_type == "command_fix":
                        # Retry the step with fix
                        logger.info(
                            f"[ORCHESTRATION] Fix applied, retrying step {step_index + 1}"
                        )
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
            final_status="success",
        )

        summary_result = asyncio.run(
            openclaw_service.execute_task(summary_prompt, timeout_seconds=60)
        )

        # Mark task as done
        task.status = TaskStatus.DONE
        task.completed_at = datetime.utcnow()
        task.summary = summary_result.get("output", "")[:2000]

        # Update session status to stopped when task completes
        if session:
            session.status = "stopped"
            session.is_active = False
            session.completed_at = datetime.utcnow()

        db.commit()

        logger.info(
            f"[ORCHESTRATION] Task {task_id} completed successfully with {len(orchestration_state.plan)} steps"
        )

        # Generate and save task report
        try:
            report_result = self.generate_task_report(task_id, format="markdown")
            if report_result and "report" in report_result:
                report_content = report_result["report"]
                report_filename = f"task_report_{task_id}.md"

                # Save report to task subfolder
                if task.task_subfolder:
                    subfolder_path = os.path.join(
                        os.path.dirname(project.workspace_path), task.task_subfolder
                    )
                    report_path = os.path.join(subfolder_path, report_filename)
                    os.makedirs(subfolder_path, exist_ok=True)
                    with open(report_path, "w", encoding="utf-8") as f:
                        f.write(report_content)
                    logger.info(f"[REPORT] Task report saved to: {report_path}")
        except Exception as report_error:
            logger.error(
                f"[REPORT] Failed to generate task report: {str(report_error)}"
            )

        return {
            "status": "completed",
            "task_id": task_id,
            "session_id": session_id,
            "steps_completed": len(orchestration_state.plan),
            "debug_attempts": len(orchestration_state.debug_attempts),
            "summary": summary_result.get("output", "")[:500],
        }

    except Exception as exc:
        # Use enhanced error handler to determine retry behavior
        should_retry = error_handler.should_retry(exc, "task_execution")

        # Check if this is a timeout error
        is_timeout = "time limit" in str(exc).lower() or "timeout" in str(exc).lower()

        # Update task failure with enhanced error information
        task.status = TaskStatus.FAILED
        task.error_message = str(exc)

        # Add diagnostic information
        error_str = str(exc).lower()
        if "json" in error_str or "parse" in error_str:
            task.error_message += "\nDiagnosis: JSON parsing error detected"
            task.error_message += "\nSuggested fix: Check AI agent response format"
        elif "empty" in error_str:
            task.error_message += "\nDiagnosis: Empty response from AI agent"
            task.error_message += "\nSuggested fix: Retry with more specific prompt"

        # Update session status to stopped when task fails
        if session:
            session.status = "stopped"
            session.is_active = False
            session.completed_at = datetime.utcnow()

        if is_timeout:
            task.error_message += " (Task timed out after 5 minutes)"
            task.error_message += "\nSuggested fix: Break task into smaller steps"

        db.commit()

        logger.error(f"[ORCHESTRATION] Task {task_id} failed: {str(exc)}")
        if is_timeout:
            logger.warning(
                "[ORCHESTRATION] Task exceeded time limit - this prevents hanging tasks"
            )

        # Don't retry timeout errors
        if is_timeout:
            raise  # Re-raise without retry

        # Retry if possible
        raise self.retry(exc=exc)

    finally:
        db.close()


@celery_app.task(bind=True, max_retries=3, default_retry_delay=60)
def process_github_webhook(
    self, webhook_data: Dict[str, Any], repo_owner: str, repo_name: str
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

        project = (
            db.query(Project)
            .filter(Project.github_url.ilike(f"%{repo_owner}/{repo_name}%"))
            .first()
        )

        if not project:
            # Create new project from webhook
            project = Project(
                name=f"{repo_owner}/{repo_name}",
                github_url=f"https://github.com/{repo_owner}/{repo_name}",
                description="Auto-created from GitHub webhook",
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
            "project_id": project.id if project else None,
        }

    except Exception as exc:
        logger.error(f"Webhook processing failed: {str(exc)}")
        raise self.retry(exc=exc)


@celery_app.task(bind=True, max_retries=5, default_retry_delay=30)
def scheduled_task_execution(self, task_id: int, scheduled_time: str, prompt: str):
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
            logger.info(
                f"Task {task_id} scheduled for later, retrying in {delay_seconds}s"
            )
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
            "executed_at": dt.utcnow().isoformat(),
        }

    except Exception as exc:
        logger.error(f"Scheduled task {task_id} failed: {str(exc)}")
        raise self.retry(exc=exc, max_retries=3)


@celery_app.task(bind=True)
def cleanup_old_logs(self, days: int = 30, session_id: Optional[int] = None):
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
            "session_id": session_id,
        }

    except Exception as exc:
        logger.error(f"Log cleanup failed: {str(exc)}")
        raise self.retry(exc=exc, max_retries=3)


@celery_app.task(bind=True)
def generate_task_report(self, task_id: int, output_format: str = "json"):
    """
    Generate a report for a completed task

    Args:
        task_id: Task ID
        output_format: Output format (json, markdown, html)
    """
    try:
        db = get_db_session()

        task = db.query(Task).filter(Task.id == task_id).first()
        if not task:
            raise ValueError(f"Task {task_id} not found")

        # Get session logs
        logs = (
            db.query(LogEntry)
            .filter(LogEntry.task_id == task_id)
            .order_by(LogEntry.created_at)
            .all()
        )

        # Build report
        report = {
            "task_id": task.id,
            "title": task.title,
            "status": task.status.value,
            "created_at": task.created_at.isoformat(),
            "completed_at": (
                task.completed_at.isoformat() if task.completed_at else None
            ),
            "duration_seconds": (
                (task.completed_at - task.started_at).total_seconds()
                if task.started_at and task.completed_at
                else None
            ),
            "logs": [
                {
                    "level": log.level,
                    "message": log.message,
                    "timestamp": log.created_at.isoformat(),
                }
                for log in logs
            ],
        }

        db.close()

        if output_format == "markdown":
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
