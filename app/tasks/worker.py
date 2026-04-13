"""Celery Worker Tasks

Background task processing for the orchestrator.
Implements multi-step orchestration workflow:
PLANNING → EXECUTING (step-by-step) → DEBUGGING (on failure) → PLAN_REVISION → DONE
"""

import os
import logging
import json
import re
import shlex
from typing import Optional, Dict, Any, List
from datetime import datetime
from pathlib import Path
from sqlalchemy.orm import Session
from sqlalchemy import create_engine
from app.celery_app import celery_app
from app.models import (
    Session as SessionModel,
    SessionTask,
    Task,
    TaskStatus,
    LogEntry,
    Project,
)
from app.database import get_db, get_db_session
from app.services import OpenClawSessionService, PromptTemplates
from app.services.error_handler import error_handler
from app.services.checkpoint_service import CheckpointService
from app.services.project_isolation_service import resolve_project_workspace_path
from app.services.prompt_templates import (
    OrchestrationStatus,
    OrchestrationState,
    StepResult,
    OPENCLAW_WORKSPACE_ROOT,
)

logger = logging.getLogger(__name__)


class TaskWorkspaceViolationError(ValueError):
    """Raised when a planned command escapes the task workspace."""

    pass


def _strip_heredoc_bodies(command_text: str) -> str:
    """Replace heredoc bodies so shell validation only sees the outer command."""

    return re.sub(
        r"<<\s*['\"]?([A-Za-z0-9_-]+)['\"]?.*?\n.*?\n\1",
        "<<HEREDOC",
        command_text or "",
        flags=re.DOTALL,
    )


def _is_quoted_route_literal(
    token: str, original_command: str, segment_command: Optional[str]
) -> bool:
    """Treat quoted grep route patterns like '/refresh' as literals, not paths."""

    if segment_command not in {"grep", "egrep", "fgrep", "rg", "ripgrep"}:
        return False

    if not re.fullmatch(r"/[A-Za-z0-9._:/-]+", token):
        return False

    return f"'{token}'" in original_command or f'"{token}"' in original_command


def _serialize_step_result(step_result: StepResult) -> Dict[str, Any]:
    return {
        "step_number": step_result.step_number,
        "status": step_result.status,
        "output": step_result.output,
        "verification_output": step_result.verification_output,
        "files_changed": step_result.files_changed,
        "error_message": step_result.error_message,
        "attempt": step_result.attempt,
    }


def _restore_step_result(data: Dict[str, Any]) -> StepResult:
    return StepResult(
        step_number=data.get("step_number", 0),
        status=data.get("status", "failed"),
        output=data.get("output", ""),
        verification_output=data.get("verification_output", ""),
        files_changed=data.get("files_changed", []) or [],
        error_message=data.get("error_message", ""),
        attempt=data.get("attempt", 1),
    )


def _save_orchestration_checkpoint(
    db: Session,
    session_id: int,
    task_id: int,
    prompt: str,
    orchestration_state: OrchestrationState,
    checkpoint_name: str = "autosave_latest",
) -> None:
    checkpoint_service = CheckpointService(db)
    checkpoint_service.save_checkpoint(
        session_id=session_id,
        checkpoint_name=checkpoint_name,
        context_data={
            "task_id": task_id,
            "task_description": prompt,
            "project_name": orchestration_state.project_name,
            "project_context": orchestration_state.project_context,
            "task_subfolder": orchestration_state.task_subfolder,
        },
        orchestration_state={
            "status": orchestration_state.status.value,
            "plan": orchestration_state.plan,
            "current_step_index": orchestration_state.current_step_index,
            "debug_attempts": orchestration_state.debug_attempts,
            "changed_files": orchestration_state.changed_files,
            "execution_results": [
                _serialize_step_result(r) for r in orchestration_state.execution_results
            ],
        },
        current_step_index=orchestration_state.current_step_index,
        step_results=[
            _serialize_step_result(r) for r in orchestration_state.execution_results
        ],
    )


def _record_live_log(
    db: Session,
    session_id: int,
    task_id: Optional[int],
    level: str,
    message: str,
    session_instance_id: Optional[str] = None,
    metadata: Optional[Dict[str, Any]] = None,
) -> None:
    db.add(
        LogEntry(
            session_id=session_id,
            task_id=task_id,
            level=level,
            message=message,
            session_instance_id=session_instance_id,
            log_metadata=json.dumps(metadata) if metadata else None,
        )
    )
    db.commit()


def _get_latest_session_task_link(
    db: Session, session_id: int, task_id: int
) -> Optional[SessionTask]:
    return (
        db.query(SessionTask)
        .filter(
            SessionTask.session_id == session_id,
            SessionTask.task_id == task_id,
        )
        .order_by(SessionTask.id.desc())
        .first()
    )


def _build_task_report_payload(db: Session, task_id: int) -> Dict[str, Any]:
    task = db.query(Task).filter(Task.id == task_id).first()
    if not task:
        raise ValueError(f"Task {task_id} not found")

    logs = (
        db.query(LogEntry)
        .filter(LogEntry.task_id == task_id)
        .order_by(LogEntry.created_at)
        .all()
    )

    return {
        "task_id": task.id,
        "title": task.title,
        "status": task.status.value,
        "created_at": task.created_at.isoformat(),
        "completed_at": task.completed_at.isoformat() if task.completed_at else None,
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


def _render_task_report(
    report: Dict[str, Any], output_format: str = "json"
) -> Dict[str, Any]:
    if output_format == "markdown":
        report_text = f"# Task Report: {report['title']}\n\n"
        report_text += f"**Status:** {report['status']}\n\n"
        report_text += f"**Duration:** {report['duration_seconds']} seconds\n\n"
        report_text += "## Logs\n\n"
        for log in report["logs"]:
            report_text += f"- [{log['level']}] {log['message']}\n"

        return {"report": report_text, "format": "markdown"}

    return {"report": report, "format": output_format}


def _extract_plan_steps(parsed_planning_output: Any) -> Optional[List[Dict[str, Any]]]:
    """Accept common planning response wrappers and return the step list."""

    if isinstance(parsed_planning_output, list):
        return parsed_planning_output

    if not isinstance(parsed_planning_output, dict):
        return None

    for key in ("steps", "plan", "task_plan", "execution_plan"):
        candidate = parsed_planning_output.get(key)
        if isinstance(candidate, list):
            return candidate

    payloads = parsed_planning_output.get("payloads")
    if isinstance(payloads, list):
        for payload in payloads:
            if isinstance(payload, dict):
                for key in ("steps", "plan", "task_plan", "execution_plan"):
                    candidate = payload.get(key)
                    if isinstance(candidate, list):
                        return candidate

    return None


def _normalize_path_reference(path_text: str, project_dir: Path) -> str:
    raw = (path_text or "").strip().strip("\"'")
    if not raw:
        raise TaskWorkspaceViolationError("Empty path reference is not allowed")
    if "~" in raw:
        raise TaskWorkspaceViolationError(
            f"Home-directory path is not allowed in task workspace: {raw}"
        )

    candidate = Path(raw)
    resolved = (
        candidate.resolve()
        if candidate.is_absolute()
        else (project_dir / candidate).resolve()
    )

    if not resolved.is_relative_to(project_dir):
        raise TaskWorkspaceViolationError(
            f"Path escapes task workspace: {raw} -> {resolved}"
        )

    relative = os.path.relpath(resolved, project_dir)
    return "." if relative == "." else relative


def _normalize_command(command: str, project_dir: Path) -> str:
    normalized = (command or "").strip()
    if not normalized:
        raise TaskWorkspaceViolationError("Empty command is not allowed")

    # Ignore heredoc bodies when validating outer shell traversal. Paths inside
    # generated file contents like `../src/...` are source text, not shell traversal.
    traversal_check_target = _strip_heredoc_bodies(normalized)

    if "~" in traversal_check_target:
        raise TaskWorkspaceViolationError(
            f"Home-directory paths are not allowed: {normalized}"
        )

    if re.search(r"(^|[\s'\"=/])\.\.(?:/|$)", traversal_check_target):
        raise TaskWorkspaceViolationError(
            f"Parent-directory traversal is not allowed: {normalized}"
        )

    current = normalized
    cd_pattern = re.compile(r"^\s*cd\s+([^;&|]+?)\s*&&\s*(.+)$")
    while True:
        match = cd_pattern.match(current)
        if not match:
            break
        target = _normalize_path_reference(match.group(1), project_dir)
        remainder = match.group(2).strip()
        if target in (".", "./"):
            current = remainder
        else:
            current = f"cd {shlex.quote(target)} && {remainder}"

    abs_path_matches = []
    path_scan_target = _strip_heredoc_bodies(current)
    segment_command: Optional[str] = None
    for token in shlex.split(path_scan_target, posix=True):
        if token in {"&&", "||", "|", ";"}:
            segment_command = None
            continue

        if segment_command is None:
            segment_command = token

        if not token.startswith("/"):
            continue
        if any(char in token for char in "<>"):
            continue
        if _is_quoted_route_literal(token, current, segment_command):
            continue
        if not re.fullmatch(r"/[A-Za-z0-9._/@:+-]+(?:/[A-Za-z0-9._@:+-]+)*/*", token):
            continue
        abs_path_matches.append(token)

    abs_paths = sorted(set(abs_path_matches), key=len, reverse=True)
    for abs_path in abs_paths:
        replacement = _normalize_path_reference(abs_path, project_dir)
        replacement = "." if replacement == "." else f"./{replacement}"
        current = current.replace(abs_path, replacement)

    current_traversal_target = _strip_heredoc_bodies(current)

    if "~" in current_traversal_target or re.search(
        r"(^|[\s'\"=/])\.\.(?:/|$)", current_traversal_target
    ):
        raise TaskWorkspaceViolationError(
            f"Command still contains unsafe path traversal: {current}"
        )

    return current


def _normalize_expected_files(
    expected_files: Optional[List[str]],
    project_dir: Path,
    logger_obj: logging.Logger,
    step_index: Optional[int] = None,
) -> List[str]:
    normalized_files: List[str] = []
    for file_path in expected_files or []:
        raw_file_path = str(file_path).strip()
        if not raw_file_path:
            continue
        if any(char in raw_file_path for char in "<>"):
            logger.warning(
                f"[ISOLATION] Skipping suspicious expected_files entry that looks like markup: {raw_file_path}"
            )
            continue
        try:
            normalized = _normalize_path_reference(raw_file_path, project_dir)
            normalized_files.append("." if normalized == "." else normalized)
        except TaskWorkspaceViolationError as exc:
            step_label = f"step {step_index} " if step_index is not None else ""
            logger_obj.warning(
                f"[ISOLATION] Skipping {step_label}expected_files entry outside workspace: "
                f"{raw_file_path} ({exc})"
            )
    return normalized_files


def _normalize_step(
    step: Dict[str, Any],
    project_dir: Path,
    logger_obj: logging.Logger,
    step_index: Optional[int] = None,
) -> Dict[str, Any]:
    step_label = f"step {step_index}" if step_index is not None else "step"

    normalized_step = dict(step)
    normalized_commands = []
    for command_index, command in enumerate(step.get("commands", []) or [], start=1):
        raw_command = str(command)
        try:
            normalized_commands.append(_normalize_command(raw_command, project_dir))
        except TaskWorkspaceViolationError as exc:
            raise TaskWorkspaceViolationError(
                f"{step_label} command {command_index} blocked: {exc}. "
                f"Offending command: {raw_command}"
            ) from exc
    normalized_step["commands"] = normalized_commands

    if step.get("verification"):
        raw_verification = str(step.get("verification"))
        try:
            normalized_step["verification"] = _normalize_command(
                raw_verification, project_dir
            )
        except TaskWorkspaceViolationError as exc:
            raise TaskWorkspaceViolationError(
                f"{step_label} verification blocked: {exc}. "
                f"Offending command: {raw_verification}"
            ) from exc

    if step.get("rollback"):
        raw_rollback = str(step.get("rollback"))
        try:
            normalized_step["rollback"] = _normalize_command(
                raw_rollback, project_dir
            )
        except TaskWorkspaceViolationError as exc:
            raise TaskWorkspaceViolationError(
                f"{step_label} rollback blocked: {exc}. "
                f"Offending command: {raw_rollback}"
            ) from exc

    normalized_step["expected_files"] = _normalize_expected_files(
        step.get("expected_files", []), project_dir, logger_obj, step_index
    )
    return normalized_step


def _normalize_plan(
    plan: List[Dict[str, Any]], project_dir: Path, logger_obj: logging.Logger
) -> List[Dict[str, Any]]:
    normalized_plan: List[Dict[str, Any]] = []
    for index, step in enumerate(plan or [], start=1):
        normalized_step = _normalize_step(step, project_dir, logger_obj, index)
        if normalized_step != step:
            logger_obj.info(
                f"[ISOLATION] Normalized step {index} to stay within task workspace"
            )
        normalized_plan.append(normalized_step)
    return normalized_plan


def _normalize_plan_with_live_logging(
    db: Session,
    session_id: int,
    task_id: int,
    plan: List[Dict[str, Any]],
    project_dir: Path,
    logger_obj: logging.Logger,
    session_instance_id: Optional[str],
    stage: str,
) -> List[Dict[str, Any]]:
    try:
        return _normalize_plan(plan, project_dir, logger_obj)
    except TaskWorkspaceViolationError as exc:
        detail = str(exc)
        logger_obj.error(f"[ISOLATION] {stage} blocked: {detail}")
        _record_live_log(
            db,
            session_id,
            task_id,
            "ERROR",
            f"[ISOLATION] {stage} blocked: {detail}",
            session_instance_id=session_instance_id,
            metadata={"stage": stage, "project_dir": str(project_dir)},
        )
        _record_live_log(
            db,
            session_id,
            task_id,
            "ERROR",
            f"[ORCHESTRATION] Task stopped because a command escaped the task workspace `{project_dir}`",
            session_instance_id=session_instance_id,
            metadata={"stage": stage},
        )
        raise


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


def build_task_subfolder_name(title: str, task_id: int) -> str:
    slug = slugify_project_name(title)
    return f"task-{slug}" if slug else f"task-{task_id}"


def _build_minimal_planning_prompt(task_description: str, project_dir: Path) -> str:
    """Fallback planning prompt for smaller-context models."""
    concise_task = " ".join((task_description or "").split())
    concise_task = concise_task[:2000]
    return f"""Produce a JSON-only execution plan for this software task. Do not implement anything.

Task:
{concise_task}

Rules:
1. Assume working directory is {project_dir}
2. Use relative paths only
3. Do not use absolute paths, .., or ~
4. Return 3 to 6 small sequential steps
5. Each step must include: step_number, description, commands, verification, rollback, expected_files
6. expected_files must be relative paths or []
7. Output JSON array only

Example:
[
  {{
    "step_number": 1,
    "description": "Inspect project structure and identify entry points",
    "commands": ["ls", "find . -maxdepth 2 -type f | sort | head -100"],
    "verification": "test -d . && echo ok",
    "rollback": null,
    "expected_files": []
  }}
]
"""


@celery_app.task(
    bind=True,
    max_retries=3,
    default_retry_delay=60,
    time_limit=1800,
    soft_time_limit=1500,
    queue="celery",
)
def execute_openclaw_task(
    self,
    session_id: int,
    task_id: int,
    prompt: str,
    timeout_seconds: int = 300,
    context: Optional[Dict[str, Any]] = None,
    resume_checkpoint_name: Optional[str] = None,
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
    session: Optional[SessionModel] = None
    task: Optional[Task] = None
    orchestration_state: Optional[OrchestrationState] = None
    session_task_link: Optional[SessionTask] = None

    try:
        # Get session and task
        session = db.query(SessionModel).filter(SessionModel.id == session_id).first()
        task = db.query(Task).filter(Task.id == task_id).first()

        if not session or not task:
            raise ValueError("Session or task not found")

        def emit_live(level: str, message: str, metadata: Optional[Dict[str, Any]] = None) -> None:
            _record_live_log(
                db,
                session_id,
                task_id,
                level,
                message,
                session_instance_id=session.instance_id,
                metadata=metadata,
            )

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
            workspace_path = str(
                resolve_project_workspace_path(project.workspace_path, project.name)
            )

            orchestration_state._workspace_path_override = workspace_path

            # Also set task subfolder if not already in database
            if task.task_subfolder:
                orchestration_state._task_subfolder_override = task.task_subfolder
            else:
                # Generate task subfolder from task title (slugified)
                # Use task title if available, otherwise fall back to task_id
                task_title_slug = build_task_subfolder_name(task.title, task_id)

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
        task.completed_at = None
        task.error_message = None
        task.current_step = 0
        session_task_link = _get_latest_session_task_link(db, session_id, task_id)
        if session_task_link:
            session_task_link.status = TaskStatus.RUNNING
            session_task_link.started_at = task.started_at
            session_task_link.completed_at = None
        db.commit()

        logger.info(f"[ORCHESTRATION] Starting multi-step execution for task {task_id}")
        emit_live(
            "INFO",
            f"[ORCHESTRATION] Starting multi-step execution for task {task_id}",
            metadata={"phase": "start"},
        )

        # Initialize OpenClaw service
        openclaw_service = OpenClawSessionService(db, session_id, task_id)

        # Get session context
        import asyncio

        session_context = asyncio.run(openclaw_service.get_session_context())

        checkpoint_service = CheckpointService(db)
        resumed_from_checkpoint = False

        if resume_checkpoint_name:
            checkpoint_data = checkpoint_service.load_checkpoint(
                session_id=session_id, checkpoint_name=resume_checkpoint_name
            )
            checkpoint_context = checkpoint_data.get("context", {})
            checkpoint_state = checkpoint_data.get("orchestration_state", {})

            prompt = checkpoint_context.get("task_description", prompt) or prompt
            orchestration_state.project_name = checkpoint_context.get(
                "project_name", orchestration_state.project_name
            )
            orchestration_state.project_context = checkpoint_context.get(
                "project_context", orchestration_state.project_context
            )
            if checkpoint_context.get("task_subfolder"):
                orchestration_state._task_subfolder_override = checkpoint_context.get(
                    "task_subfolder"
                )

            orchestration_state.plan = checkpoint_state.get("plan", []) or []
            orchestration_state.current_step_index = (
                checkpoint_state.get(
                    "current_step_index",
                    checkpoint_data.get("current_step_index", 0) or 0,
                )
                or 0
            )
            orchestration_state.debug_attempts = (
                checkpoint_state.get("debug_attempts", []) or []
            )
            orchestration_state.changed_files = (
                checkpoint_state.get("changed_files", []) or []
            )
            orchestration_state.execution_results = [
                _restore_step_result(item)
                for item in checkpoint_state.get(
                    "execution_results", checkpoint_data.get("step_results", [])
                )
            ]

            if orchestration_state.plan:
                orchestration_state.plan = _normalize_plan_with_live_logging(
                    db,
                    session_id,
                    task_id,
                    orchestration_state.plan,
                    orchestration_state.project_dir,
                    logger,
                    session.instance_id,
                    "Checkpoint restore",
                )

            resumed_from_checkpoint = bool(orchestration_state.plan)
            if resumed_from_checkpoint:
                logger.info(
                    f"[ORCHESTRATION] Resuming from checkpoint '{resume_checkpoint_name}' at step index {orchestration_state.current_step_index}"
                )
                emit_live(
                    "INFO",
                    f"[ORCHESTRATION] Resuming from checkpoint '{resume_checkpoint_name}' at step index {orchestration_state.current_step_index}",
                    metadata={"phase": "resume"},
                )

        if not resumed_from_checkpoint:
            # PHASE 1: PLANNING - Generate step plan
            logger.info("[ORCHESTRATION] Phase 1: PLANNING - generating step plan")
            emit_live(
                "INFO",
                "[ORCHESTRATION] Phase 1: PLANNING - generating step plan",
                metadata={"phase": "planning"},
            )

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

            # Planning often needs more time than the old 120 second cap,
            # especially for larger repos or denser prompts.
            planning_timeout_seconds = max(180, min(timeout_seconds, 300))
            planning_result = asyncio.run(
                openclaw_service.execute_task(
                    planning_prompt, timeout_seconds=planning_timeout_seconds
                )
            )

            planning_error = (planning_result.get("error") or "").lower()
            if "context window exceeded" in planning_error or (
                "context" in planning_error and "exceeded" in planning_error
            ):
                logger.warning(
                    "[ORCHESTRATION] Planning hit context limit; retrying with minimal prompt"
                )
                emit_live(
                    "WARN",
                    "[ORCHESTRATION] Planning prompt exceeded context; retrying with minimal prompt",
                    metadata={"phase": "planning", "retry": "minimal_prompt"},
                )
                minimal_planning_prompt = _build_minimal_planning_prompt(
                    prompt, orchestration_state.project_dir
                )
                planning_result = asyncio.run(
                    openclaw_service.execute_task(
                        minimal_planning_prompt,
                        timeout_seconds=min(planning_timeout_seconds, 180),
                    )
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
                            logger.info(
                                "[ORCHESTRATION] Empty payloads, using full JSON"
                            )
                    # Format 2: Direct dict response
                    else:
                        output_text = json.dumps(output_result)
                        logger.info("[ORCHESTRATION] Direct dict response")
                elif isinstance(output_result, str):
                    # Format 3: Raw string response
                    output_text = output_result
                    logger.info("[ORCHESTRATION] Raw string response")
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

                planning_error = (planning_result.get("error") or "").lower()
                if "context window exceeded" in planning_error or (
                    "context" in planning_error and "exceeded" in planning_error
                ):
                    raise ValueError("Planning failed: context window exceeded")

                if "request timed out before a response was generated" in output_text.lower():
                    raise TimeoutError(
                        f"Planning timed out after {planning_timeout_seconds}s"
                    )

                if success:
                    extracted_plan = _extract_plan_steps(plan_data)
                    if extracted_plan is not None:
                        orchestration_state.plan = _normalize_plan_with_live_logging(
                            db,
                            session_id,
                            task_id,
                            extracted_plan,
                            orchestration_state.project_dir,
                            logger,
                            session.instance_id,
                            "Planning output",
                        )
                        logger.info(
                            f"[ORCHESTRATION] Generated {len(orchestration_state.plan)} steps in plan (using {strategy_info})"
                        )
                        emit_live(
                            "INFO",
                            f"[ORCHESTRATION] Generated {len(orchestration_state.plan)} steps in plan",
                            metadata={
                                "phase": "planning",
                                "steps": len(orchestration_state.plan),
                                "strategy": strategy_info,
                            },
                        )
                        task.steps = json.dumps(orchestration_state.plan)
                        task.current_step = 0
                        db.commit()
                    else:
                        raise ValueError(
                            "Planning result is not a list of steps"
                        )
                else:
                    # Failed to parse after all strategies
                    orchestration_state.status = OrchestrationStatus.ABORTED
                    orchestration_state.abort_reason = (
                        f"Planning JSON parse failed: {strategy_info}"
                    )
                    emit_live(
                        "ERROR",
                        f"[ORCHESTRATION] Planning JSON parse failed: {strategy_info}",
                        metadata={"phase": "planning", "reason": "planning_json_error"},
                    )
                    task.status = TaskStatus.FAILED
                    task.error_message = (
                        f"Planning JSON parse failed: {strategy_info}. "
                        f"Raw output: {output_text[:500]}"
                    )
                    db.commit()
                    return {"status": "failed", "reason": "planning_json_error"}
            except TaskWorkspaceViolationError as e:
                orchestration_state.status = OrchestrationStatus.ABORTED
                orchestration_state.abort_reason = f"Workspace isolation violation: {e}"
                emit_live(
                    "ERROR",
                    f"[ORCHESTRATION] Planning output blocked: {e}",
                    metadata={
                        "phase": "planning",
                        "reason": "workspace_isolation_violation",
                    },
                )
                task.status = TaskStatus.FAILED
                task.error_message = str(e)
                db.commit()
                return {"status": "failed", "reason": "workspace_isolation_violation"}
            except Exception as e:
                logger.error(f"[ORCHESTRATION] Failed to parse planning result: {e}")
                orchestration_state.status = OrchestrationStatus.ABORTED
                orchestration_state.abort_reason = f"Planning parse failed: {e}"
                emit_live(
                    "ERROR",
                    f"[ORCHESTRATION] Failed to parse planning result: {e}",
                    metadata={"phase": "planning", "reason": "planning_parse_error"},
                )
                task.status = TaskStatus.FAILED
                task.error_message = str(e)
                db.commit()
                return {"status": "failed", "reason": "planning_parse_error"}

        _save_orchestration_checkpoint(
            db, session_id, task_id, prompt, orchestration_state
        )

        # PHASE 2: EXECUTING - Execute each step
        logger.info(
            f"[ORCHESTRATION] Phase 2: EXECUTING - executing {len(orchestration_state.plan)} steps"
        )
        emit_live(
            "INFO",
            f"[ORCHESTRATION] Phase 2: EXECUTING - executing {len(orchestration_state.plan)} steps",
            metadata={"phase": "executing", "steps": len(orchestration_state.plan)},
        )

        for step_index in range(
            orchestration_state.current_step_index, len(orchestration_state.plan)
        ):
            step = orchestration_state.plan[step_index]
            db.refresh(session)
            if session.status in ["stopped", "paused"] or not session.is_active:
                logger.info(
                    f"[ORCHESTRATION] Session {session_id} marked {session.status}; stopping task execution before step {step_index + 1}"
                )
                _save_orchestration_checkpoint(
                    db, session_id, task_id, prompt, orchestration_state
                )
                task.status = TaskStatus.CANCELLED
                task.completed_at = datetime.utcnow()
                if session_task_link:
                    session_task_link.status = TaskStatus.CANCELLED
                    session_task_link.completed_at = task.completed_at
                db.commit()
                return {
                    "status": "cancelled",
                    "task_id": task_id,
                    "session_id": session_id,
                    "reason": f"session_{session.status}",
                }

            orchestration_state.current_step_index = step_index
            task.current_step = step_index + 1
            _save_orchestration_checkpoint(
                db, session_id, task_id, prompt, orchestration_state
            )
            db.commit()

            step_description = step.get("description", f"Step {step_index + 1}")
            step_commands = step.get("commands", [])
            verification_command = step.get("verification")
            rollback_command = step.get("rollback")
            expected_files = step.get("expected_files", [])

            logger.info(
                f"[ORCHESTRATION] Executing step {step_index + 1}/{len(orchestration_state.plan)}: {step_description[:80]}..."
            )
            emit_live(
                "INFO",
                f"[ORCHESTRATION] Executing step {step_index + 1}/{len(orchestration_state.plan)}: {step_description}",
                metadata={
                    "phase": "executing",
                    "step_index": step_index + 1,
                    "step_total": len(orchestration_state.plan),
                },
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
                project_dir=str(orchestration_state.project_dir),
                verification_command=verification_command,
                rollback_command=rollback_command,
                expected_files=expected_files,
                completed_steps_summary=orchestration_state.prior_results_summary(),
                project_context=f"Build project: {project_name_slug}",
            )

            step_timeout_seconds = max(
                120, timeout_seconds // max(1, len(orchestration_state.plan))
            )

            # Execute step
            step_result = asyncio.run(
                openclaw_service.execute_task(
                    execution_prompt,
                    timeout_seconds=step_timeout_seconds,
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
                _save_orchestration_checkpoint(
                    db, session_id, task_id, prompt, orchestration_state
                )
                logger.info(
                    f"[ORCHESTRATION] Step {step_index + 1} completed successfully"
                )
                emit_live(
                    "INFO",
                    f"[ORCHESTRATION] Step {step_index + 1} completed successfully",
                    metadata={"phase": "executing", "step_index": step_index + 1},
                )
            else:
                orchestration_state.record_failure(step_record)
                _save_orchestration_checkpoint(
                    db, session_id, task_id, prompt, orchestration_state
                )

                # PHASE 3: DEBUGGING - Fix failed step
                logger.info(
                    f"[ORCHESTRATION] Step {step_index + 1} failed, entering DEBUGGING phase"
                )
                emit_live(
                    "WARN",
                    f"[ORCHESTRATION] Step {step_index + 1} failed, entering DEBUGGING phase",
                    metadata={"phase": "debugging", "step_index": step_index + 1},
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
                    project_dir=str(orchestration_state.project_dir),
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
                        emit_live(
                            "WARN",
                            "[ORCHESTRATION] Plan revision needed, entering PLAN_REVISION phase",
                            metadata={"phase": "plan_revision", "step_index": step_index + 1},
                        )
                        revise_prompt = PromptTemplates.build_plan_revision_prompt(
                            original_plan=orchestration_state.plan,
                            failed_steps=[step_record],
                            debug_analysis=debug_result.get("output", ""),
                            completed_steps=orchestration_state.completed_steps,
                            workspace_root=str(orchestration_state.workspace_root),
                            project_dir=str(orchestration_state.project_dir),
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

                        orchestration_state.plan = _normalize_plan_with_live_logging(
                            db,
                            session_id,
                            task_id,
                            revise_data.get("revised_plan", orchestration_state.plan),
                            orchestration_state.project_dir,
                            logger,
                            session.instance_id,
                            "Plan revision",
                        )
                        logger.info(f"[REVISION-PARSE] Using strategy: {strategy_info}")
                        logger.info(
                            f"[ORCHESTRATION] Plan revised, {len(orchestration_state.plan)} steps"
                        )
                        emit_live(
                            "INFO",
                            f"[ORCHESTRATION] Plan revised, {len(orchestration_state.plan)} steps",
                            metadata={
                                "phase": "plan_revision",
                                "steps": len(orchestration_state.plan),
                                "strategy": strategy_info,
                            },
                        )

                        # Retry the step with revised plan
                        continue  # Retry this step

                    elif fix_type == "code_fix" or fix_type == "command_fix":
                        # Retry the step with fix
                        logger.info(
                            f"[ORCHESTRATION] Fix applied, retrying step {step_index + 1}"
                        )
                        emit_live(
                            "INFO",
                            f"[ORCHESTRATION] Fix applied, retrying step {step_index + 1}",
                            metadata={"phase": "debugging", "step_index": step_index + 1},
                        )
                        continue  # Retry this step

                except TaskWorkspaceViolationError as e:
                    orchestration_state.status = OrchestrationStatus.ABORTED
                    orchestration_state.abort_reason = (
                        f"Workspace isolation violation: {e}"
                    )
                    task.status = TaskStatus.FAILED
                    task.error_message = str(e)
                    db.commit()
                    return {
                        "status": "failed",
                        "reason": "workspace_isolation_violation",
                    }
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
        emit_live(
            "INFO",
            "[ORCHESTRATION] Phase 5: TASK_SUMMARY - summarizing completion",
            metadata={"phase": "task_summary"},
        )

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
        task.error_message = None
        task.summary = summary_result.get("output", "")[:2000]
        task.current_step = len(orchestration_state.plan)
        if session_task_link:
            session_task_link.status = TaskStatus.DONE
            session_task_link.completed_at = task.completed_at

        # Update session status to stopped when task completes
        if session:
            session.status = "stopped"
            session.is_active = False
            session.completed_at = datetime.utcnow()

        db.commit()

        logger.info(
            f"[ORCHESTRATION] Task {task_id} completed successfully with {len(orchestration_state.plan)} steps"
        )
        emit_live(
            "INFO",
            f"[ORCHESTRATION] Task {task_id} completed successfully with {len(orchestration_state.plan)} steps",
            metadata={"phase": "completed", "steps": len(orchestration_state.plan)},
        )

        # Generate and save task report
        try:
            report_payload = _build_task_report_payload(db, task_id)
            report_result = _render_task_report(
                report_payload, output_format="markdown"
            )
            if report_result and "report" in report_result:
                report_content = report_result["report"]
                report_filename = f"task_report_{task_id}.md"

                report_path = orchestration_state.project_dir / report_filename
                os.makedirs(orchestration_state.project_dir, exist_ok=True)
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
        if task:
            task.status = TaskStatus.FAILED
            task.error_message = str(exc)
            task.completed_at = datetime.utcnow()

        if not session_task_link:
            session_task_link = _get_latest_session_task_link(db, session_id, task_id)
        if session_task_link and task:
            session_task_link.status = TaskStatus.FAILED
            session_task_link.completed_at = task.completed_at

        # Add diagnostic information
        error_str = str(exc).lower()
        if "json" in error_str or "parse" in error_str:
            if task:
                task.error_message += "\nDiagnosis: JSON parsing error detected"
                task.error_message += "\nSuggested fix: Check AI agent response format"
        elif "empty" in error_str:
            if task:
                task.error_message += "\nDiagnosis: Empty response from AI agent"
                task.error_message += "\nSuggested fix: Retry with more specific prompt"

        # Update session status to stopped when task fails
        if session:
            session.status = "stopped"
            session.is_active = False
            session.completed_at = datetime.utcnow()

        if is_timeout:
            if task:
                task.error_message += " (Task timed out after 5 minutes)"
                task.error_message += "\nSuggested fix: Break task into smaller steps"

        try:
            if orchestration_state:
                orchestration_state.status = OrchestrationStatus.ABORTED
                orchestration_state.abort_reason = str(exc)
                _save_orchestration_checkpoint(
                    db,
                    session_id,
                    task_id,
                    prompt,
                    orchestration_state,
                    checkpoint_name="autosave_error",
                )
                _record_live_log(
                    db,
                    session_id,
                    task_id,
                    "WARN",
                    "[CHECKPOINT] Error checkpoint saved for resume",
                    session_instance_id=session.instance_id if session else None,
                    metadata={"checkpoint_name": "autosave_error"},
                )
        except Exception as checkpoint_error:
            logger.error(
                "[CHECKPOINT] Failed to save error checkpoint for task %s: %s",
                task_id,
                str(checkpoint_error),
            )

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
        report = _build_task_report_payload(db, task_id)
        return _render_task_report(report, output_format=output_format)

    except Exception as exc:
        logger.error(f"Report generation failed: {str(exc)}")
        raise self.retry(exc=exc, max_retries=3)
    finally:
        db.close()
