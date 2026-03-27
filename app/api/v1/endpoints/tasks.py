"""Tasks API endpoints"""

from fastapi import APIRouter, Depends, HTTPException, status, Request
from sqlalchemy.orm import Session
from typing import List, Dict, Any, Optional
from datetime import datetime, timedelta
import json
import asyncio
from app.database import get_db
from app.models import Task, TaskStatus, Project, LogEntry
from app.schemas import TaskCreate, TaskUpdate, TaskResponse
from app.services.openclaw_service import OpenClawSessionService
from app.services.log_utils import sort_logs, deduplicate_logs

router = APIRouter()


@router.post("/tasks", response_model=TaskResponse, status_code=status.HTTP_201_CREATED)
def create_task(task: TaskCreate, db: Session = Depends(get_db)):
    """Create a new task"""
    # Verify project exists
    project = db.query(Project).filter(Project.id == task.project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    db_task = Task(**task.model_dump(), status=TaskStatus.PENDING)
    db.add(db_task)
    db.commit()
    db.refresh(db_task)
    return db_task


@router.get("/projects/{project_id}/tasks", response_model=List[TaskResponse])
def get_project_tasks(
    project_id: int, skip: int = 0, limit: int = 100, db: Session = Depends(get_db)
):
    """Get all tasks for a project"""
    # Verify project exists
    project = db.query(Project).filter(Project.id == project_id).first()
    if not project:
        raise HTTPException(status_code=404, detail="Project not found")

    tasks = (
        db.query(Task)
        .filter(Task.project_id == project_id)
        .offset(skip)
        .limit(limit)
        .all()
    )
    return tasks


# Define this BEFORE /tasks/{task_id} to avoid route collision
@router.post("/tasks/{task_id}/execute")
async def execute_task_with_openclaw(
    task_id: int, request: Request, db: Session = Depends(get_db)
):
    """
    Execute a task using OpenClaw AI agent

    Args:
        task_id: Task ID to execute
        request: HTTP request with prompt data
        db: Database session

    Returns:
        Execution result with logs
    """
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    # Get prompt from request body or use task description
    try:
        prompt_data = await request.json()
        prompt = prompt_data.get("prompt") if prompt_data else task.description
        # Get timeout settings from request
        log_timeout_minutes = prompt_data.get(
            "log_timeout_minutes", 5
        )  # Default 5 minutes
        monitor_logs = prompt_data.get("monitor_logs", False)
    except json.JSONDecodeError:
        prompt = task.description
        log_timeout_minutes = 5
        monitor_logs = False

    try:
        # Start OpenClaw session
        session_service = OpenClawSessionService(db, None, task_id)
        openclaw_key = await session_service.start_session(prompt)

        # Build prompt using templates
        from app.services import PromptTemplates

        # Get session context
        session_context = await session_service.get_session_context()

        # Build enhanced prompt with templates
        prompt_text = PromptTemplates.build_task_prompt(
            task_description=prompt,
            project_context=session_context.get(
                "project_context", "No additional context"
            ),
            recent_logs=session_context.get("recent_logs", []),
            available_tools=[
                "File operations",
                "Git operations",
                "Code execution",
                "API calls",
                "Terminal commands",
            ],
        )

        # Log the prompt that will be used
        session_service._log_entry(
            "INFO", f"Using template-built prompt: {prompt_text[:100]}..."
        )
        session_service._log_entry(
            "INFO", f"Log timeout monitoring: {log_timeout_minutes} minutes"
        )

        # Execute the task with timeout and log monitoring
        result = await execute_task_with_timeout_monitoring(
            session_service=session_service,
            prompt=prompt_text,
            timeout_seconds=300,  # 5 minutes max execution
            log_timeout_minutes=log_timeout_minutes,
            monitor_logs=monitor_logs,
            task=task,
            db=db,
            openclaw_key=openclaw_key,
        )

        # Update task with result
        task.status = (
            TaskStatus.DONE
            if result.get("status") == "completed"
            else TaskStatus.FAILED
        )
        task.output = result.get("output", "")[:5000]  # Truncate long outputs
        task.completed_at = datetime.utcnow()

        db.commit()

        return {
            "success": True,
            "task_id": task_id,
            "status": result.get("status"),
            "output": result.get("output", ""),
            "mode": result.get("mode", "unknown"),
            "session_key": openclaw_key,
            "logs": result.get("logs", []),
        }

    except Exception as e:
        db.rollback()
        raise HTTPException(status_code=500, detail=f"Failed to execute task: {str(e)}")


async def execute_task_with_timeout_monitoring(
    session_service: OpenClawSessionService,
    prompt: str,
    timeout_seconds: int,
    log_timeout_minutes: int,
    monitor_logs: bool,
    task: Task,
    db: Session,
    openclaw_key: str,
) -> Dict[str, Any]:
    """
    Execute task with timeout and log monitoring

    Args:
        session_service: OpenClaw session service
        prompt: Task prompt
        timeout_seconds: Maximum execution time
        log_timeout_minutes: Minutes without new logs before timeout
        monitor_logs: Whether to monitor for log activity
        task: Task model
        db: Database session
        openclaw_key: OpenClaw session key

    Returns:
        Execution result
    """
    import subprocess
    import json
    import uuid

    # Check prompt length to avoid context window overflow
    MAX_PROMPT_LENGTH = 50000

    if len(prompt) > MAX_PROMPT_LENGTH:
        session_service._log_entry(
            "WARN",
            f"Prompt too long ({len(prompt)} chars), truncating to {MAX_PROMPT_LENGTH}",
        )
        prompt = prompt[:MAX_PROMPT_LENGTH] + "\n\n[TRUNCATED - prompt was too long]"

    session_service._log_entry(
        "INFO", f"Starting task execution with timeout monitoring"
    )
    session_service._log_entry(
        "INFO",
        f"Max execution time: {timeout_seconds}s, Log timeout: {log_timeout_minutes}min",
    )

    # Track last log time
    last_log_time = datetime.utcnow()

    # Generate unique session ID
    new_session_id = f"orchestrator-task-{task_id}-{uuid.uuid4().hex[:8]}"

    # Escape single quotes in prompt for bash command
    escaped_prompt = prompt.replace("'", "'\\''")

    # Start the OpenClaw process
    process = subprocess.Popen(
        [
            "bash",
            "-c",
            f"openclaw agent --local --session-id {new_session_id} --message '{escaped_prompt}' --json --timeout {timeout_seconds}",
        ],
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
        executable="/usr/bin/bash",
    )

    # Monitor the process and logs
    try:
        # Set up timeout
        start_time = datetime.utcnow()
        timeout_delta = timedelta(seconds=timeout_seconds)

        while True:
            # Check if process is still running
            return_code = process.poll()

            if return_code is not None:
                # Process finished
                stdout, stderr = process.communicate()

                # Check if successful
                if return_code == 0:
                    try:
                        output_data = json.loads(stdout.strip())
                        output_text = (
                            output_data.get("message", "")
                            or output_data.get("text", "")
                            or stdout
                        )

                        session_service._log_entry(
                            "INFO", f"Task execution completed: {output_text[:300]}"
                        )

                        return {
                            "status": "completed",
                            "mode": "real",
                            "output": output_text,
                            "logs": [
                                {
                                    "level": "INFO",
                                    "message": f"Task received: {prompt[:100]}...",
                                    "timestamp": datetime.utcnow().isoformat(),
                                },
                                {
                                    "level": "INFO",
                                    "message": f"Task executed via OpenClaw CLI",
                                    "timestamp": datetime.utcnow().isoformat(),
                                },
                            ],
                            "execution_time": (
                                datetime.utcnow() - start_time
                            ).total_seconds(),
                            "session_key": openclaw_key,
                            "note": "Real execution completed via OpenClaw CLI",
                        }
                    except json.JSONDecodeError:
                        session_service._log_entry(
                            "INFO", f"OpenClaw output: {stdout[:500]}"
                        )
                        return {
                            "status": "completed",
                            "mode": "real",
                            "output": stdout,
                            "logs": [
                                {
                                    "level": "INFO",
                                    "message": f"Task executed via OpenClaw CLI",
                                    "timestamp": datetime.utcnow().isoformat(),
                                }
                            ],
                            "execution_time": (
                                datetime.utcnow() - start_time
                            ).total_seconds(),
                            "session_key": openclaw_key,
                            "note": "Real execution completed via OpenClaw CLI",
                        }
                else:
                    raise Exception(f"OpenClaw CLI failed: {stderr}")

            # Check for log timeout (if monitoring enabled)
            if monitor_logs:
                current_time = datetime.utcnow()
                time_since_last_log = current_time - last_log_time

                # Convert to minutes
                minutes_since_last_log = time_since_last_log.total_seconds() / 60

                session_service._log_entry(
                    "DEBUG",
                    f"Monitoring: {minutes_since_last_log:.1f} minutes since last log",
                )

                # If no new logs for configured timeout, kill the process
                if minutes_since_last_log >= log_timeout_minutes:
                    session_service._log_entry(
                        "ERROR",
                        f"⚠️ TIMEOUT: No new logs for {log_timeout_minutes} minutes. Killing process.",
                    )

                    # Kill the process
                    process.kill()
                    process.wait()

                    return {
                        "status": "failed",
                        "mode": "real",
                        "output": f"Task timed out: No new logs for {log_timeout_minutes} minutes",
                        "logs": [
                            {
                                "level": "ERROR",
                                "message": f"Task timed out: No new logs for {log_timeout_minutes} minutes",
                                "timestamp": datetime.utcnow().isoformat(),
                            }
                        ],
                        "execution_time": (
                            datetime.utcnow() - start_time
                        ).total_seconds(),
                        "error": "Log timeout",
                    }

            # Wait a bit before checking again
            await asyncio.sleep(10)

    except subprocess.TimeoutExpired:
        session_service._log_entry(
            "ERROR", f"Task execution timed out: {timeout_seconds}s"
        )
        process.kill()
        process.wait()

        return {
            "status": "failed",
            "mode": "real",
            "output": f"Task timed out after {timeout_seconds} seconds",
            "logs": [
                {
                    "level": "ERROR",
                    "message": f"Task execution timed out after {timeout_seconds} seconds",
                    "timestamp": datetime.utcnow().isoformat(),
                }
            ],
            "execution_time": timeout_seconds,
            "error": "Timeout",
        }
    except Exception as e:
        error_str = str(e)
        session_service._log_entry("ERROR", f"Error executing task: {error_str}")

        # Kill process if it's still running
        if process.poll() is None:
            process.kill()
            process.wait()

        # Handle specific error types
        if "context" in error_str.lower() and "token" in error_str.lower():
            session_service._log_entry("ERROR", f"Context window exceeded: {error_str}")
            return {
                "status": "failed",
                "mode": "real",
                "output": "Context window exceeded. Prompt is too long for the model.",
                "logs": [
                    {
                        "level": "ERROR",
                        "message": f"Context window exceeded: {error_str}",
                        "timestamp": datetime.utcnow().isoformat(),
                    }
                ],
                "execution_time": 0.0,
                "error": "Context window exceeded",
            }
        elif "signal" in error_str.lower() or "killed" in error_str.lower():
            session_service._log_entry("ERROR", f"Process was killed: {error_str}")
            return {
                "status": "failed",
                "mode": "real",
                "output": f"Process was killed: {error_str}",
                "logs": [
                    {
                        "level": "ERROR",
                        "message": f"Process was killed: {error_str}",
                        "timestamp": datetime.utcnow().isoformat(),
                    }
                ],
                "execution_time": 0.0,
                "error": "Process killed",
            }
        else:
            return {
                "status": "failed",
                "mode": "real",
                "output": f"Execution error: {error_str}",
                "logs": [
                    {
                        "level": "ERROR",
                        "message": f"Error: {error_str}",
                        "timestamp": datetime.utcnow().isoformat(),
                    }
                ],
                "execution_time": 0.0,
                "error": error_str,
            }


@router.get("/tasks/{task_id}")
def get_task(task_id: int, db: Session = Depends(get_db)):
    """Get a task by ID"""
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")
    return task


@router.put("/tasks/{task_id}", response_model=TaskResponse)
def update_task(task_id: int, task_update: TaskUpdate, db: Session = Depends(get_db)):
    """Update a task"""
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    update_data = task_update.model_dump(exclude_unset=True)
    for field, value in update_data.items():
        setattr(task, field, value)

    db.commit()
    db.refresh(task)
    return task


@router.delete("/tasks/{task_id}", status_code=status.HTTP_204_NO_CONTENT)
def delete_task(task_id: int, db: Session = Depends(get_db)):
    """Delete a task"""
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    db.delete(task)
    db.commit()
    return None


@router.get("/tasks/{task_id}/logs/sorted")
def get_sorted_task_logs(
    task_id: int,
    db: Session = Depends(get_db),
    order: str = "asc",
    deduplicate: bool = True,
    level: Optional[str] = None,
    limit: Optional[int] = None,
):
    """
    Get sorted and optionally deduplicated logs for a task

    Args:
        task_id: Task ID
        order: "asc" for oldest first, "desc" for newest first
        deduplicate: Remove duplicate log entries
        level: Optional log level filter
        limit: Optional limit on number of logs

    Returns:
        Sorted list of log entries
    """
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise HTTPException(status_code=404, detail="Task not found")

    logs_entries = db.query(LogEntry).filter(LogEntry.task_id == task_id).all()

    if level:
        logs_entries = [log for log in logs_entries if log.level == level]

    logs = [
        {
            "id": log.id,
            "task_id": log.task_id,
            "session_id": log.session_id,
            "level": log.level,
            "message": log.message,
            "timestamp": log.created_at.isoformat(),
            "metadata": json.loads(log.log_metadata) if log.log_metadata else {},
        }
        for log in logs_entries
    ]

    sorted_logs = sort_logs(logs, order=order, deduplicate=deduplicate)

    if limit:
        sorted_logs = sorted_logs[:limit]

    return {
        "task_id": task_id,
        "total_logs": len(logs),
        "returned_logs": len(sorted_logs),
        "sort_order": order,
        "deduplicated": deduplicate,
        "logs": sorted_logs,
    }
